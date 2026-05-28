"""
llm-sum/evaluator.py — Blind judge for summary quality + hallucinations
==========================================================================

For each (paper, summariser) pair this module asks a judge model to score:
    quality_score             (1-10)
    hallucination_count       (>= 0)
    hallucination_categories  (list of strings)
    confidence_score          (1-5)

THE BLIND PROTOCOL (most important rule)
-----------------------------------------
The judge prompt MUST NOT contain the name of the summariser model. An LLM
evaluating its own output systematically inflates the score — a documented
self-preference bias. For a comparative study, leaking the summariser
identity would invalidate every score.

Implementation: the (doi, summariser, summary_text) triple is loaded from
data/summaries.jsonl. The summariser key is kept only for joining the
evaluation back to its source — it never enters the message body. A unit
test asserts the rendered prompt contains none of "openai", "anthropic",
"gemini", "gpt", "claude".

STRUCTURED OUTPUT, WITH FALLBACK
---------------------------------
Real APIs are asked for native JSON mode (OpenAI/Gemini) or a tool-use
schema (Anthropic). When that contract is honoured, the result parses
cleanly. When the model still slips in conversational text, a regex
fallback extracts each field by name. If even regex fails, quality_score
is set to 99 — the sentinel flag for manual review in Phase 5.

PHASE3_MODE CONTROLS THE RUN
-----------------------------
Like the summariser, this module reads ``PHASE3_MODE`` from .env and
respects ``--mode`` / ``--limit`` on the CLI. Test mode short-circuits
to mocks; single/dev caps the paper count; batch is informational here
(the evaluator does not submit a separate batch job — judge requests
are submitted by ``batch_utils`` alongside the summariser pass, and
results are collected by ``check_batch_status.py``).
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models_config import compute_cost, get_model_spec  # noqa: E402
from file_paths import doi_to_slug  # noqa: E402
from utils import BudgetGuard, log_error, sleep_for_model  # noqa: E402
from phase3_mode import ModeProfile, resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "500"))
JUDGE_PROMPT_FILE = Path(os.getenv("JUDGE_PROMPT_FILE", "llm-sum/prompts/judge_v1.txt"))
JUDGE_MODELS = [
    p.strip() for p in os.getenv("JUDGE_MODELS", "openai").split(",") if p.strip()
]


def _is_dry_run() -> bool:
    """
    Read DRY_RUN at call time so tests (and the user toggling .env between
    runs) get the current value. ``PHASE3_MODE=test`` ALSO forces dry-run
    via ``resolve_mode().dry_run`` — the same last-guardrail rule as the
    summariser.
    """
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return True
    return resolve_mode().dry_run


DRY_RUN = _is_dry_run()

# Tokens that must NEVER appear in a rendered judge prompt.
BLIND_FORBIDDEN_TOKENS = ("openai", "anthropic", "gemini", "gpt", "claude", "chatgpt")


# ---------------------------------------------------------------------------
# Prompt loading + blind builder
# ---------------------------------------------------------------------------

def load_judge_prompt(prompt_file: Path = JUDGE_PROMPT_FILE) -> str:
    path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    if not path.exists():
        raise FileNotFoundError(f"Judge prompt not found: {path}")
    template = path.read_text(encoding="utf-8")
    for placeholder in ("{REFERENCE_TEXT}", "{CANDIDATE_SUMMARY}"):
        if placeholder not in template:
            raise ValueError(f"Judge prompt {path} missing placeholder {placeholder}")
    return template


def build_judge_prompt(reference_text: str, candidate_summary: str,
                       template: str | None = None) -> str:
    """
    Render the judge prompt with the (reference, candidate) pair. The
    summariser name is deliberately NOT a parameter — keeping it out of the
    signature makes the blind protocol enforceable by code review and by
    the dedicated unit test that scans the rendered output.
    """
    tmpl = template if template is not None else load_judge_prompt()
    return (tmpl
            .replace("{REFERENCE_TEXT}", reference_text)
            .replace("{CANDIDATE_SUMMARY}", candidate_summary))


# ---------------------------------------------------------------------------
# JSON parsing + regex fallback + "99" sentinel
# ---------------------------------------------------------------------------

VALID_HALLUCINATION_CATEGORIES = (
    "fabricated statistics", "omitted caveat", "contradiction",
)
SCORE_SENTINEL_MALFORMED = 99


def _try_parse_json_block(text: str) -> dict | None:
    """
    Try strict JSON parse, then a relaxed search for the first {...} block.
    Returns None if no valid JSON object is found.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first balanced {...} block — handles wrappers like
    # "Sure! Here is the evaluation: { ... } Let me know if you need anything."
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


_REGEX_FALLBACK = {
    "quality_score": re.compile(r'"?quality_score"?\s*[:=]\s*(\d+)'),
    "hallucination_count": re.compile(r'"?hallucination_count"?\s*[:=]\s*(\d+)'),
    "confidence_score": re.compile(r'"?confidence_score"?\s*[:=]\s*(\d+)'),
}
_REGEX_CATEGORIES = re.compile(
    r'"?hallucination_categories"?\s*[:=]\s*\[(.*?)\]', re.DOTALL,
)


def parse_judge_response(raw_text: str) -> dict:
    """
    Extract the structured fields from a judge response.

    Order of attempts:
        1. Strict JSON / first balanced {...} block.
        2. Regex over the raw text.
        3. Sentinel quality_score=99, flagged for human review.

    Always returns a dict containing all four required keys. Adds
    `parse_method` so the analysis stage can audit how each row was parsed.
    """
    obj = _try_parse_json_block(raw_text)

    if isinstance(obj, dict) and "quality_score" in obj:
        return {
            "quality_score": int(obj.get("quality_score", SCORE_SENTINEL_MALFORMED)),
            "hallucination_count": int(obj.get("hallucination_count", 0)),
            "hallucination_categories": list(obj.get("hallucination_categories") or []),
            "confidence_score": int(obj.get("confidence_score", 1)),
            "reasoning": str(obj.get("reasoning", "")),
            "parse_method": "json",
        }

    # Regex fallback — extract one field at a time.
    fields: dict[str, Any] = {}
    for key, regex in _REGEX_FALLBACK.items():
        m = regex.search(raw_text)
        if m:
            try:
                fields[key] = int(m.group(1))
            except ValueError:
                pass

    if "quality_score" in fields:
        cats_match = _REGEX_CATEGORIES.search(raw_text)
        if cats_match:
            cats_raw = cats_match.group(1)
            categories = [c.strip().strip('"').strip("'") for c in cats_raw.split(",") if c.strip()]
        else:
            categories = []
        return {
            "quality_score": fields["quality_score"],
            "hallucination_count": fields.get("hallucination_count", 0),
            "hallucination_categories": categories,
            "confidence_score": fields.get("confidence_score", 1),
            "reasoning": "",
            "parse_method": "regex",
        }

    # Sentinel: flag for manual review in Phase 5.
    return {
        "quality_score": SCORE_SENTINEL_MALFORMED,
        "hallucination_count": 0,
        "hallucination_categories": [],
        "confidence_score": 1,
        "reasoning": "Malformed judge response; sentinel set for human review.",
        "parse_method": "sentinel",
    }


def needs_human_review(parsed: dict) -> bool:
    """
    True if the row should be flagged for Phase-5 manual review. Triggers
    on low confidence OR sentinel score.
    """
    return (
        parsed.get("confidence_score", 0) < 3
        or parsed.get("quality_score") == SCORE_SENTINEL_MALFORMED
    )


# ---------------------------------------------------------------------------
# Judge call (real-time; per-pair). Batch path mirrors summariser's.
# ---------------------------------------------------------------------------

def _mock_judge_response(provider: str, reference_text: str,
                         candidate_summary: str) -> dict:
    """
    Deterministic fake response for DRY_RUN. Pulls a quality score from a
    hash of the text so different summaries get different scores.
    """
    spec = get_model_spec(provider)
    score_seed = (abs(hash(candidate_summary)) % 6) + 4   # 4..9
    confidence = (abs(hash(reference_text)) % 5) + 1       # 1..5
    fake_payload = {
        "quality_score": score_seed,
        "hallucination_count": (abs(hash(candidate_summary)) % 3),
        "hallucination_categories": [],
        "confidence_score": confidence,
        "reasoning": "[MOCK] dry-run evaluation",
    }
    return {
        "raw_text": json.dumps(fake_payload),
        "input_tokens": max(200, len(reference_text) // 4),
        "output_tokens": 80,
        "model_version": f"{spec.model_id}-DRYRUN",
    }


def call_judge(provider: str, reference_text: str, candidate_summary: str,
               *, prompt_template: str | None = None,
               max_retries: int = 3) -> dict:
    """
    Call a judge model and return the raw text + token + version metadata.
    Parsing happens in a separate step so failed parses can still log the
    raw text for inspection.
    """
    if DRY_RUN or _is_dry_run():
        return _mock_judge_response(provider, reference_text, candidate_summary)

    user_message = build_judge_prompt(reference_text, candidate_summary, prompt_template)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            if provider == "openai":
                return _call_judge_openai(user_message)
            if provider == "anthropic":
                return _call_judge_anthropic(user_message)
            if provider == "gemini":
                return _call_judge_gemini(user_message)
            raise ValueError(f"Unknown judge provider '{provider}'")
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    log_error("N/A", f"evaluate_{provider}", f"Judge failed after retries: {last_error}")
    raise RuntimeError(f"Judge {provider} failed: {last_error}")


def _call_judge_openai(user_message: str) -> dict:
    import openai  # type: ignore[import-not-found]
    spec = get_model_spec("openai")
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=spec.model_id,
        messages=[{"role": "user", "content": user_message}],
        temperature=TEMPERATURE,
        max_tokens=MAX_OUTPUT_TOKENS,
        seed=SEED,
        # Native JSON mode is the reliable contract. Text parsing of a
        # conversational response is unstable across providers and versions.
        response_format={"type": "json_object"},
    )
    return {
        "raw_text": response.choices[0].message.content or "",
        "input_tokens": int(response.usage.prompt_tokens),
        "output_tokens": int(response.usage.completion_tokens),
        "model_version": str(response.model),
    }


def _call_judge_anthropic(user_message: str) -> dict:
    import anthropic  # type: ignore[import-not-found]
    spec = get_model_spec("anthropic")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=spec.model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return {
        "raw_text": text,
        "input_tokens": int(response.usage.input_tokens),
        "output_tokens": int(response.usage.output_tokens),
        "model_version": str(response.model),
    }


def _call_judge_gemini(user_message: str) -> dict:
    import google.generativeai as genai  # type: ignore[import-not-found]
    spec = get_model_spec("gemini")
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel(spec.model_id)
    response = model.generate_content(
        user_message,
        generation_config={
            "temperature": TEMPERATURE,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
        },
    )
    usage = response.usage_metadata
    return {
        "raw_text": response.text or "",
        "input_tokens": int(usage.prompt_token_count),
        "output_tokens": int(usage.candidates_token_count),
        "model_version": spec.model_id,
    }


# ---------------------------------------------------------------------------
# Evaluation row builder + persistence
# ---------------------------------------------------------------------------

def build_evaluation_row(*, doi: str, summariser: str, judge: str,
                         input_source: str = "processed",
                         reference_text: str, candidate_summary: str,
                         judge_response: dict) -> dict:
    parsed = parse_judge_response(judge_response["raw_text"])
    return {
        "doi": doi,
        "input_source": input_source,
        "summarizer": summariser,
        "judge": judge,
        "judge_model_version": judge_response["model_version"],
        "quality_score": parsed["quality_score"],
        "hallucination_count": parsed["hallucination_count"],
        "hallucination_categories": parsed["hallucination_categories"],
        "confidence_score": parsed["confidence_score"],
        "requires_human_review": needs_human_review(parsed),
        "parse_method": parsed["parse_method"],
        "reasoning": parsed["reasoning"],
        "input_tokens": judge_response["input_tokens"],
        "output_tokens": judge_response["output_tokens"],
        "raw_response_excerpt": judge_response["raw_text"][:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def append_evaluation(row: dict, path: Path | None = None) -> None:
    resolved = path if path is not None else EVALUATIONS_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def _iter_summaries(path: Path | None = None):
    # Resolved at call time so tests can monkeypatch evaluator.SUMMARIES_PATH.
    resolved = path if path is not None else SUMMARIES_PATH
    if not resolved.exists():
        return
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_cached_text(record_or_doi) -> str | None:
    """
    Locate this paper's cleaned-text cache and return its body.

    ``record_or_doi`` is normally a row from ``data/summaries.jsonl`` —
    those rows carry ``journal`` and ``title``, so the descriptive
    filename lookup succeeds. Falls back to the legacy
    ``{doi_to_slug(doi)}.jsonl`` name automatically.
    """
    from prepare_texts import read_cached_text as _shared_read
    source = "processed"
    if not isinstance(record_or_doi, str):
        source = str(record_or_doi.get("input_source") or "processed")
    return _shared_read(record_or_doi, input_source=source)


def already_evaluated(doi: str, summariser: str, judge: str,
                      input_source: str = "processed") -> bool:
    """Check evaluations.jsonl to support a manual resume."""
    if not EVALUATIONS_PATH.exists():
        return False
    with open(EVALUATIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("doi") == doi
                    and (row.get("input_source") or "processed") == input_source
                    and row.get("summarizer") == summariser
                    and row.get("judge") == judge):
                return True
    return False


def confirm_real_judge(profile: ModeProfile, force: bool) -> bool:
    """
    Mirror summarizer's confirmation: only triggers when the profile both
    requires confirmation AND is not short-circuited to mocks.
    """
    if profile.dry_run or not profile.requires_confirm:
        return True
    if force:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = (f"[phase3:safety] evaluator confirmation bypassed via --force at "
                f"{datetime.now(timezone.utc).isoformat()}\n")
        with open(LOGS_DIR / "phase3_safety.log", "a", encoding="utf-8") as f:
            f.write(line)
        print(line.strip())
        return True
    reply = input(
        "\n[phase3:safety] About to call the judge with REAL API requests.\n"
        "Type 'yes' to confirm: "
    ).strip().lower()
    if reply != "yes":
        print("[phase3:safety] Confirmation not received; aborting.")
        return False
    return True


def run_evaluation(*, judges: list[str] | None = None, resume: bool = True,
                   paper_limit: int | None = None) -> dict[str, int]:
    judges = judges or JUDGE_MODELS
    prompt_template = load_judge_prompt()
    guard = BudgetGuard()
    counts = {"evaluated": 0, "skipped": 0, "failed": 0, "no_text": 0}

    seen_papers = 0
    for entry in _iter_summaries():
        if paper_limit is not None and seen_papers >= paper_limit:
            break
        doi = str(entry.get("doi", "")).strip()
        if not doi:
            continue
        input_source = str(entry.get("input_source") or "processed")
        # Summary entries carry journal+title, so they resolve to the
        # descriptive cache filename. The legacy slug name is the
        # automatic fallback inside read_cached_text.
        reference_text = _read_cached_text(entry)
        if reference_text is None:
            counts["no_text"] += 1
            continue
        seen_papers += 1

        for summariser, slot in (entry.get("models") or {}).items():
            if slot.get("status") != "success" or not slot.get("summary"):
                continue
            candidate_summary = slot["summary"]

            for judge in judges:
                if resume and already_evaluated(doi, summariser, judge, input_source):
                    counts["skipped"] += 1
                    continue
                sleep_for_model(judge)
                try:
                    response = call_judge(
                        judge, reference_text, candidate_summary,
                        prompt_template=prompt_template,
                    )
                except Exception as exc:
                    counts["failed"] += 1
                    log_error(doi, f"evaluate_{judge}", str(exc))
                    continue

                row = build_evaluation_row(
                    doi=doi, summariser=summariser, judge=judge,
                    input_source=input_source,
                    reference_text=reference_text,
                    candidate_summary=candidate_summary,
                    judge_response=response,
                )
                append_evaluation(row)
                counts["evaluated"] += 1
                cost = compute_cost(
                    judge,
                    int(response["input_tokens"] or 0),
                    int(response["output_tokens"] or 0),
                    batched=False,
                )
                guard.add_cost(cost)
                print(f"[phase3:evaluate] {doi} {summariser} -> {judge} "
                      f"score={row['quality_score']} via {row['parse_method']} "
                      f"(${cost:.4f})")

    print(f"\n[phase3:evaluate] done. counts={counts}  "
          f"budget_spent=${guard.total_spent:.4f}")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 — blind judge evaluator.")
    parser.add_argument("--mode", choices=VALID_MODES, default=None,
                        help="Override PHASE3_MODE from .env: test|single|dev|batch.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override the mode's default paper_limit.")
    parser.add_argument("--judges", default=",".join(JUDGE_MODELS),
                        help="Comma-separated judge provider keys.")
    parser.add_argument("--no-resume", action="store_true",
                        help="Re-evaluate even pairs already in evaluations.jsonl.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass interactive confirmation. USE WITH CAUTION.")
    args = parser.parse_args(argv)

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]

    profile = resolve_mode(args.mode)
    paper_limit = args.limit if args.limit is not None else profile.paper_limit

    print(profile.banner())
    if profile.use_batch:
        print("[phase3:evaluate] mode=batch: this script only runs the real-time judge "
              "loop. Judge batch jobs are submitted by run_phase3 summarize and collected "
              "by check_batch_status.py.")
    if args.limit is not None:
        print(f"[phase3:evaluate] --limit {args.limit} overrides mode default.")

    if not confirm_real_judge(profile, force=args.force):
        return 1

    run_evaluation(
        judges=judges,
        resume=not args.no_resume,
        paper_limit=paper_limit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
