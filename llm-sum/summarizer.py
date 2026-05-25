"""
llm-sum/summarizer.py — Multi-model summarisation router
==========================================================

This module is the heart of Phase 3's "fair comparison" guarantee. Every
provider (OpenAI, Anthropic, Gemini) is called with:
    - the same prompt (loaded from PROMPT_FILE),
    - the same temperature (0.0) and seed (42) where supported,
    - the same max output tokens.

Every successful call returns the SAME dict shape so downstream code never
branches on provider:

    {
        "status": "success",
        "summary": "<text>",
        "input_tokens": int,
        "output_tokens": int,
        "model_version": "<exact-id-from-provider>",
        "timestamp": "<UTC ISO-8601>",
    }

DESIGN DECISIONS
----------------
* `DEVELOPMENT_MODE = True` is hardcoded at module top. When True, the run
  caps at 2 papers and forces real-time mode (no batch submissions). This is
  the last guardrail before a full-corpus spend.

* Model versions are recorded EXACTLY as the provider returns them
  (e.g. "gpt-5.5-0325-preview", not "gpt-5"). Providers silently update
  models behind a stable alias — logging the exact string is how we detect
  performance drift across reruns.

* Token counts come from `usage.*` in the provider response. Estimating
  after the fact accumulates ±5% per call; over 1000 calls that mismatch
  is large enough to break a tight research budget.

* The provider router is a dispatch dict, not an if/elif chain. Adding a
  fourth provider in the future is a one-line change.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from models_config import all_providers, compute_cost, get_model_spec  # noqa: E402
from file_paths import doi_to_slug  # noqa: E402
from utils import BudgetGuard, log_error, sleep_for_model  # noqa: E402
from extract import truncate_to_limit  # noqa: E402

# ---------------------------------------------------------------------------
# Hardcoded development guardrail
# ---------------------------------------------------------------------------
# Hardcoded, not env-driven. The whole point is that a misconfigured .env
# cannot accidentally bypass this safety. Flip to False ONLY for production.
DEVELOPMENT_MODE: bool = True
DEV_MODE_PAPER_LIMIT: int = 2

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))
PROMPT_FILE = Path(os.getenv("PROMPT_FILE", "llm-sum/prompts/summarization_v1.txt"))

# Maximum characters of article text sent to the LLM. 0 = no limit (use
# full cached text). Set via MAX_INPUT_CHARS in .env to tune cost vs coverage.
# 40 000 chars ≈ 10 000 tokens ≈ a full 15-page research article.
MAX_INPUT_CHARS: int = int(os.getenv("MAX_INPUT_CHARS", "0"))


def _is_dry_run() -> bool:
    """
    Read DRY_RUN at call time so tests (and the user toggling .env between
    runs) get the current value. Reading at module-import time bakes the
    initial state into the process and is hard to override safely.
    """
    return os.getenv("DRY_RUN", "true").lower() == "true"


# Backwards-compatible module attribute. Tests that monkeypatch
# `summarizer.DRY_RUN` still work; runtime code goes through `_is_dry_run()`.
DRY_RUN = _is_dry_run()


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_prompt(prompt_file: Path = PROMPT_FILE) -> str:
    """
    Load the summarisation prompt template. Must contain '{ARTICLE_TEXT}'.
    """
    path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    template = path.read_text(encoding="utf-8")
    if "{ARTICLE_TEXT}" not in template:
        raise ValueError(
            f"Prompt template {path} must contain the '{{ARTICLE_TEXT}}' placeholder."
        )
    return template


def build_user_message(article_text: str, template: str | None = None) -> str:
    """
    Substitute the article text into the prompt template, applying
    MAX_INPUT_CHARS truncation if configured.

    The cache (data/processed/*.jsonl) holds the full cleaned paper text.
    Truncation happens HERE — at the point of LLM call — so the limit
    can be tuned via .env without re-running extraction.
    """
    if MAX_INPUT_CHARS > 0 and len(article_text) > MAX_INPUT_CHARS:
        article_text = truncate_to_limit(article_text, MAX_INPUT_CHARS)
    tmpl = template if template is not None else load_prompt()
    return tmpl.replace("{ARTICLE_TEXT}", article_text)


# ---------------------------------------------------------------------------
# DRY_RUN mock summary (no API calls, deterministic)
# ---------------------------------------------------------------------------

def _mock_summary(model_name: str, article_text: str, title_hint: str = "") -> dict:
    """
    Generate a fake-but-schema-valid summary for DRY_RUN mode.

    Returned shape matches the real success shape so downstream code never
    has to branch on dry-run vs live.
    """
    spec = get_model_spec(model_name)
    snippet = (article_text.strip().split("\n", 1)[0])[:120]
    summary = f"[MOCK {model_name}] {title_hint or snippet or 'summary'}".strip()
    return {
        "status": "success",
        "summary": summary,
        # Deterministic fake token counts proportional to input size so cost
        # estimates from DRY_RUN are non-zero and the smoke test catches
        # missing usage fields.
        "input_tokens": max(100, len(article_text) // 4),
        "output_tokens": 120,
        "model_version": f"{spec.model_id}-DRYRUN",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Provider-specific real-time callers
# ---------------------------------------------------------------------------

def _call_openai(article_text: str, *, prompt_template: str | None) -> dict:
    """
    Real-time OpenAI call. Imports the SDK lazily so unit tests that mock
    this function don't require the package to be installed.
    """
    import openai  # type: ignore[import-not-found]

    spec = get_model_spec("openai")
    user_message = build_user_message(article_text, prompt_template)
    client = openai.OpenAI()  # picks up OPENAI_API_KEY from env

    response = client.chat.completions.create(
        model=spec.model_id,
        messages=[{"role": "user", "content": user_message}],
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
        seed=SEED,
    )

    return {
        "status": "success",
        "summary": response.choices[0].message.content or "",
        # Pull tokens FROM the response, not estimated. This is the only way
        # BudgetGuard.total_spent will match the OpenAI dashboard charge.
        "input_tokens": int(response.usage.prompt_tokens),
        "output_tokens": int(response.usage.completion_tokens),
        # `response.model` is the exact version OpenAI routed to, e.g.
        # "gpt-5.5-0325-preview" — not the alias "gpt-5.5" we requested.
        # Logging this lets us detect silent model updates across runs.
        "model_version": str(response.model),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _call_anthropic(article_text: str, *, prompt_template: str | None) -> dict:
    """Real-time Anthropic call."""
    import anthropic  # type: ignore[import-not-found]

    spec = get_model_spec("anthropic")
    user_message = build_user_message(article_text, prompt_template)
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env

    response = client.messages.create(
        model=spec.model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": user_message}],
    )

    # Anthropic returns content as a list of TextBlock objects.
    summary_text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )

    return {
        "status": "success",
        "summary": summary_text,
        "input_tokens": int(response.usage.input_tokens),
        "output_tokens": int(response.usage.output_tokens),
        "model_version": str(response.model),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _call_gemini(article_text: str, *, prompt_template: str | None) -> dict:
    """Real-time Gemini call (no batch API)."""
    import google.generativeai as genai  # type: ignore[import-not-found]

    spec = get_model_spec("gemini")
    user_message = build_user_message(article_text, prompt_template)

    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel(spec.model_id)
    response = model.generate_content(
        user_message,
        generation_config={
            "temperature": TEMPERATURE,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            # Gemini exposes a `seed` parameter via REST but the Python SDK
            # may ignore it; we still set TEMPERATURE=0.0 for determinism.
        },
    )

    usage = response.usage_metadata
    return {
        "status": "success",
        "summary": response.text or "",
        "input_tokens": int(usage.prompt_token_count),
        "output_tokens": int(usage.candidates_token_count),
        "model_version": spec.model_id,  # Gemini does not return a version string.
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


PROVIDER_CALLERS: dict[str, Callable[..., dict]] = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
}


# ---------------------------------------------------------------------------
# Public router
# ---------------------------------------------------------------------------

def generate_summary(
    model_name: str,
    text: str,
    *,
    prompt_template: str | None = None,
    max_retries: int = 3,
) -> dict:
    """
    Route a summarisation request to the correct provider and return a
    normalised result dict.

    Retries with exponential backoff (1s, 2s, 4s) on transient errors.
    On final failure, returns {"status": "failed", "error": "<message>", ...}
    instead of raising — so a single paper's failure doesn't kill the run.
    """
    if model_name not in PROVIDER_CALLERS:
        raise KeyError(f"Unknown provider '{model_name}'")

    # Tests may monkeypatch `summarizer.DRY_RUN` directly; production code
    # may toggle the env between runs. Honour either.
    if DRY_RUN or _is_dry_run():
        return _mock_summary(model_name, text)

    caller = PROVIDER_CALLERS[model_name]
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return caller(text, prompt_template=prompt_template)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[phase3:summarize] {model_name} attempt {attempt+1} failed: {exc}; "
                      f"retrying in {wait}s")
                time.sleep(wait)

    log_error(
        doi="N/A",
        stage=f"summarize_{model_name}",
        message=f"All {max_retries} attempts failed: {last_error}",
    )
    return {
        "status": "failed",
        "error": str(last_error),
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Persistence: data/summaries.jsonl
# ---------------------------------------------------------------------------

def _empty_model_slot() -> dict:
    return {
        "status": "pending",
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": None,
    }


def _new_summary_entry(record: dict) -> dict:
    """Build an empty summary row from a manifest record."""
    doi = str(record.get("doi", "")).strip()
    return {
        "doi": doi,
        "custom_id": doi_to_slug(doi),
        "journal": record.get("journal"),
        "species": record.get("species"),
        "study_design": record.get("study_design"),
        "clinical_topic": record.get("clinical_topic"),
        "title": record.get("title"),
        "models": {p: _empty_model_slot() for p in all_providers()},
    }


def load_existing_summaries(path: Path = SUMMARIES_PATH) -> dict[str, dict]:
    """Load existing summaries keyed by DOI for resume support."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(entry.get("doi", "")).strip()
            if doi:
                out[doi] = entry
    return out


def _write_all_summaries(path: Path, summaries_by_doi: dict[str, dict]) -> None:
    """
    Rewrite the summaries file from scratch.

    Phase 2 conventions are append-only, but `--resume` requires merging
    per-(doi,model) results into an existing row. The compromise: we
    rewrite the whole file in one atomic step at the end of each run. Each
    line is still valid JSON, so any partial-completion analysis still works
    on the snapshot from before the rewrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in summaries_by_doi.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Confirmation guard
# ---------------------------------------------------------------------------

def confirm_real_batch(use_batch: bool, force: bool) -> bool:
    """
    Final interactive guardrail. Returns True if it's safe to proceed.

    Triggers ONLY when all three conditions hold:
        USE_BATCH_API=true  AND  DEVELOPMENT_MODE=False  AND  DRY_RUN=False
    Otherwise no confirmation needed.

    `--force` bypasses the prompt for overnight/scheduled use, and writes
    an audit line to data/logs/ so the override is traceable.
    """
    if not (use_batch and (not DEVELOPMENT_MODE) and (not _is_dry_run())):
        return True

    if force:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        audit_line = (
            f"[phase3:safety] confirmation bypassed via --force at "
            f"{datetime.now(timezone.utc).isoformat()}\n"
        )
        with open(LOGS_DIR / "phase3_safety.log", "a", encoding="utf-8") as f:
            f.write(audit_line)
        print(audit_line.strip())
        return True

    reply = input(
        "\n[phase3:safety] About to submit REAL batch jobs to paid APIs.\n"
        "Type 'yes' to confirm: "
    ).strip().lower()
    if reply != "yes":
        print("[phase3:safety] Confirmation not received; aborting.")
        return False
    return True


# ---------------------------------------------------------------------------
# Real-time run loop
# ---------------------------------------------------------------------------

def _iter_manifest(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_cached_text(slug: str) -> str | None:
    path = PROCESSED_DIR / f"{slug}.jsonl"
    if not path.exists():
        return None
    line = path.read_text(encoding="utf-8").strip()
    if not line:
        return None
    try:
        return json.loads(line).get("text")
    except (json.JSONDecodeError, AttributeError):
        return None


def run_realtime(
    *,
    manifest_path: Path = MANIFEST_PATH,
    resume: bool = False,
    paper_limit: int | None = None,
    providers: list[str] | None = None,
) -> dict[str, int]:
    """
    Sequentially summarise each paper across each provider in real time.
    """
    providers = providers or list(all_providers())
    prompt_template = load_prompt()
    existing = load_existing_summaries() if resume else {}
    guard = BudgetGuard()

    counts = {"success": 0, "failed": 0, "skipped": 0, "no_text": 0}
    processed_papers = 0

    summaries_by_doi: dict[str, dict] = dict(existing)

    for record in _iter_manifest(manifest_path):
        if paper_limit is not None and processed_papers >= paper_limit:
            break

        doi = str(record.get("doi", "")).strip()
        if not doi:
            continue

        slug = doi_to_slug(doi)
        article_text = _read_cached_text(slug)
        if article_text is None:
            counts["no_text"] += 1
            log_error(doi, "summarize", f"No cached text at data/processed/{slug}.jsonl")
            continue

        entry = summaries_by_doi.get(doi) or _new_summary_entry(record)
        summaries_by_doi[doi] = entry

        processed_papers += 1
        print(f"\n[phase3:summarize] paper {processed_papers}: {doi}")

        for provider in providers:
            slot = entry["models"].setdefault(provider, _empty_model_slot())
            if resume and slot.get("status") == "success":
                counts["skipped"] += 1
                print(f"  {provider}: already success, skipping")
                continue

            sleep_for_model(provider)
            result = generate_summary(provider, article_text, prompt_template=prompt_template)

            entry["models"][provider] = result

            if result["status"] == "success":
                counts["success"] += 1
                cost = compute_cost(
                    provider,
                    int(result["input_tokens"] or 0),
                    int(result["output_tokens"] or 0),
                    batched=False,
                )
                guard.add_cost(cost)
                print(f"  {provider}: success "
                      f"(in={result['input_tokens']}, out={result['output_tokens']}, "
                      f"ver={result['model_version']}, ${cost:.4f})")
            else:
                counts["failed"] += 1
                print(f"  {provider}: FAILED — {result.get('error')}")

    _write_all_summaries(SUMMARIES_PATH, summaries_by_doi)
    print(f"\n[phase3:summarize] done. counts={counts}  budget_spent=${guard.total_spent:.4f}")
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 — multi-model summariser.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip (doi, model) pairs already at status=success.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the interactive batch confirmation. USE WITH CAUTION.")
    parser.add_argument("--providers", default=",".join(all_providers()),
                        help="Comma-separated provider keys (subset of openai,anthropic,gemini).")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    use_batch_env = os.getenv("USE_BATCH_API", "false").lower() == "true"
    effective_batch = use_batch_env and not DEVELOPMENT_MODE

    if DEVELOPMENT_MODE:
        print(f"[phase3:summarize] DEVELOPMENT_MODE active — capping at "
              f"{DEV_MODE_PAPER_LIMIT} papers, forcing real-time.")
        paper_limit = DEV_MODE_PAPER_LIMIT
    else:
        paper_limit = None

    if not confirm_real_batch(use_batch=effective_batch, force=args.force):
        return 1

    if effective_batch:
        # Lazy import so unit tests don't need the batch dependencies loaded.
        from batch_utils import run_batch_summarisation
        run_batch_summarisation(
            manifest_path=args.manifest,
            resume=args.resume,
            providers=providers,
        )
        return 0

    run_realtime(
        manifest_path=args.manifest,
        resume=args.resume,
        paper_limit=paper_limit,
        providers=providers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
