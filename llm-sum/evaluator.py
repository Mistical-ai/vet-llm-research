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
import hashlib
import importlib
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
from utils import BudgetGuard, log_error, require_positive_budget_for_real_run, sleep_for_model  # noqa: E402
from phase3_mode import ModeProfile, resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
# Bumped from 500 to 1500: the v2 rubric requests source quotes per hallucination,
# which can be verbose. 500 tokens would truncate valid responses.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1500"))
# Default changed to judge_v2.txt (vet-specific rubric). Set JUDGE_PROMPT_FILE in
# .env to llm-sum/prompts/judge_v1.txt to revert to the generic 1-10 rubric.
JUDGE_PROMPT_FILE = Path(os.getenv("JUDGE_PROMPT_FILE", "llm-sum/prompts/judge_v2.txt"))
JUDGE_MODELS = [
    p.strip() for p in os.getenv("JUDGE_MODELS", "openai").split(",") if p.strip()
]

RUBRIC_VERSION = "vet_score_v2.0"
EVALUATOR_VERSION = "evaluator-v2.0"


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
    "fabricated statistics", "omitted caveat", "contradiction", "unsupported_inference",
)
SCORE_SENTINEL_MALFORMED = 99


def _clamp(value: Any, lo: int, hi: int) -> int:
    """Clamp an LLM-provided integer score to [lo, hi].

    An LLM occasionally returns 0 or a value outside the specified range.
    Without clamping, a score of 0 on a 1-3 scale would silently lower the
    composite and skew all downstream statistics.
    """
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def calculate_composite_score(scores: dict) -> float:
    """Compute the Vet-Score v2.0 composite from four dimension scores.

    DECISION: Python computes this, not the LLM. LLMs make arithmetic errors
    with weighted formulas; having Python do it guarantees reproducibility and
    makes the formula unit-testable. The LLM just outputs four integers (1-3).

    Formula:
        composite = (FA × 1.5) + (C × 1.0) + (CR × 1.2) + (O × 0.8)
        max       = (3×1.5) + (3×1.0) + (3×1.2) + (3×0.8) = 13.5
        normalized = (composite / 13.5) × 9 + 1   → range [1.0, 10.0]
    """
    fa = _clamp(scores.get("factual_accuracy",  1), 1, 3)
    c  = _clamp(scores.get("completeness",       1), 1, 3)
    cr = _clamp(scores.get("clinical_relevance", 1), 1, 3)
    o  = _clamp(scores.get("organization",       1), 1, 3)
    composite = (fa * 1.5) + (c * 1.0) + (cr * 1.2) + (o * 0.8)
    return round((composite / 13.5) * 9 + 1, 2)


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
    # v2 dimension fields (checked first — if found, use vet rubric path)
    "factual_accuracy":   re.compile(r'"?factual_accuracy"?\s*[:=]\s*(\d+)'),
    "completeness":       re.compile(r'"?completeness"?\s*[:=]\s*(\d+)'),
    "clinical_relevance": re.compile(r'"?clinical_relevance"?\s*[:=]\s*(\d+)'),
    "organization":       re.compile(r'"?organization"?\s*[:=]\s*(\d+)'),
    # legacy / shared fields
    "quality_score":      re.compile(r'"?quality_score"?\s*[:=]\s*(\d+)'),
    "hallucination_count": re.compile(r'"?hallucination_count"?\s*[:=]\s*(\d+)'),
    "confidence_score":   re.compile(r'"?confidence_score"?\s*[:=]\s*(\d+)'),
}
_REGEX_CATEGORIES = re.compile(
    r'"?hallucination_categories"?\s*[:=]\s*\[(.*?)\]', re.DOTALL,
)


def parse_judge_response(raw_text: str) -> dict:
    """
    Extract the structured fields from a judge response.

    Order of attempts:
        1. Strict JSON / first balanced {...} block.
           1a. v2 schema (has 'factual_accuracy') → vet rubric path, compute composite.
           1b. Legacy schema (has 'quality_score' only) → old path, no composite.
        2. Regex over the raw text (tries v2 dimension fields first, then legacy).
        3. Sentinel quality_score=99, flagged for human review.

    Always returns a dict with at minimum quality_score, hallucination_count,
    hallucination_categories, confidence_score, reasoning, and parse_method.
    New v2 fields (factual_accuracy, completeness, clinical_relevance, organization,
    composite_score, hallucination_present, hallucination_claims) are present in v2
    responses and set to None/[] in legacy and sentinel paths.
    """
    obj = _try_parse_json_block(raw_text)

    if isinstance(obj, dict) and "factual_accuracy" in obj:
        # --- v2 (vet rubric) schema ---
        composite = calculate_composite_score(obj)
        halluc_block = obj.get("hallucination") or {}
        claims = list(halluc_block.get("claims") or [])
        confidence = _clamp(obj.get("confidence_score", 1), 1, 5)
        return {
            "factual_accuracy":   _clamp(obj.get("factual_accuracy",  1), 1, 3),
            "completeness":       _clamp(obj.get("completeness",       1), 1, 3),
            "clinical_relevance": _clamp(obj.get("clinical_relevance", 1), 1, 3),
            "organization":       _clamp(obj.get("organization",       1), 1, 3),
            "composite_score":    composite,
            "hallucination_present":   bool(halluc_block.get("present", len(claims) > 0)),
            "hallucination_count":     int(halluc_block.get("count", len(claims))),
            "hallucination_claims":    claims,
            "hallucination_categories": list({c.get("category", "") for c in claims if c.get("category")}),
            "confidence_score": confidence,
            "reasoning": str(obj.get("reasoning", "")),
            # quality_score kept for backward compat with any Phase 5 code
            "quality_score": round(composite),
            "parse_method": "json",
        }

    if isinstance(obj, dict) and "quality_score" in obj:
        # --- legacy schema (judge_v1 responses) ---
        confidence = _clamp(obj.get("confidence_score", 1), 1, 5)
        return {
            "factual_accuracy": None, "completeness": None,
            "clinical_relevance": None, "organization": None,
            "composite_score": None,
            "hallucination_present": None, "hallucination_claims": [],
            "quality_score": int(obj.get("quality_score", SCORE_SENTINEL_MALFORMED)),
            "hallucination_count": int(obj.get("hallucination_count", 0)),
            "hallucination_categories": list(obj.get("hallucination_categories") or []),
            "confidence_score": confidence,
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

    if "factual_accuracy" in fields:
        # v2 regex path
        composite = calculate_composite_score(fields)
        confidence = _clamp(fields.get("confidence_score", 1), 1, 5)
        return {
            "factual_accuracy":   _clamp(fields.get("factual_accuracy",  1), 1, 3),
            "completeness":       _clamp(fields.get("completeness",       1), 1, 3),
            "clinical_relevance": _clamp(fields.get("clinical_relevance", 1), 1, 3),
            "organization":       _clamp(fields.get("organization",       1), 1, 3),
            "composite_score":    composite,
            "hallucination_present": None, "hallucination_claims": [],
            "hallucination_count": fields.get("hallucination_count", 0),
            "hallucination_categories": [],
            "confidence_score": confidence,
            "reasoning": "",
            "quality_score": round(composite),
            "parse_method": "regex",
        }

    if "quality_score" in fields:
        # legacy regex path
        cats_match = _REGEX_CATEGORIES.search(raw_text)
        if cats_match:
            cats_raw = cats_match.group(1)
            categories = [c.strip().strip('"').strip("'") for c in cats_raw.split(",") if c.strip()]
        else:
            categories = []
        confidence = _clamp(fields.get("confidence_score", 1), 1, 5)
        return {
            "factual_accuracy": None, "completeness": None,
            "clinical_relevance": None, "organization": None,
            "composite_score": None,
            "hallucination_present": None, "hallucination_claims": [],
            "quality_score": fields["quality_score"],
            "hallucination_count": fields.get("hallucination_count", 0),
            "hallucination_categories": categories,
            "confidence_score": confidence,
            "reasoning": "",
            "parse_method": "regex",
        }

    # Sentinel: flag for manual review in Phase 5.
    return {
        "factual_accuracy": None, "completeness": None,
        "clinical_relevance": None, "organization": None,
        "composite_score": None,
        "hallucination_present": None, "hallucination_claims": [],
        "quality_score": SCORE_SENTINEL_MALFORMED,
        "hallucination_count": 0,
        "hallucination_categories": [],
        "confidence_score": 1,
        "reasoning": "Malformed judge response; sentinel set for human review.",
        "parse_method": "sentinel",
    }


def needs_human_review(parsed: dict) -> bool:
    """
    True if the row should be flagged for Phase-5 manual review. Triggers on:
    - Low confidence (< 3): the judge is guessing
    - Sentinel score (99): response was malformed
    - Any major-severity hallucination claim: a finding that could mislead a
      clinician must be checked by a human even if overall confidence is high
    """
    any_major = any(
        c.get("severity") == "major"
        for c in (parsed.get("hallucination_claims") or [])
    )
    return (
        parsed.get("confidence_score", 0) < 3
        or parsed.get("quality_score") == SCORE_SENTINEL_MALFORMED
        or any_major
    )


# ---------------------------------------------------------------------------
# Judge call (real-time; per-pair). Batch path mirrors summariser's.
# ---------------------------------------------------------------------------

def _stable_hash_int(text: str) -> int:
    """Use SHA-256 instead of Python's salted hash() for reproducible dry runs."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _mock_judge_response(provider: str, reference_text: str,
                         candidate_summary: str) -> dict:
    """
    Deterministic fake response for DRY_RUN. Uses hashes of the text so
    different summaries get different (but reproducible) scores. Returns
    the v2 schema so dry-run/test mode exercises the new parse path.
    """
    spec = get_model_spec(provider)
    h_cand = _stable_hash_int(candidate_summary)
    h_ref  = _stable_hash_int(reference_text)
    # Dimension scores: deterministic, spread across 1-3
    fa = (h_cand % 3) + 1
    c  = (h_ref  % 3) + 1
    cr = ((h_cand + h_ref) % 3) + 1
    o  = ((h_cand * 31) % 3) + 1
    confidence = (h_ref % 5) + 1
    fake_payload = {
        "factual_accuracy":   fa,
        "completeness":       c,
        "clinical_relevance": cr,
        "organization":       o,
        "hallucination": {
            "present": False,
            "count": 0,
            "claims": [],
        },
        "confidence_score": confidence,
        "reasoning": "[MOCK] dry-run evaluation",
    }
    return {
        "raw_text": json.dumps(fake_payload),
        "input_tokens": max(200, len(reference_text) // 4),
        "output_tokens": 120,
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
    if _is_dry_run():
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
        "system_fingerprint": getattr(response, "system_fingerprint", None),
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
    try:
        genai = importlib.import_module("google.genai")
        types = importlib.import_module("google.genai.types")
    except ImportError as exc:
        raise ImportError(
            "Gemini judge requires the google-genai package in the active "
            "virtual environment. Run: python -m pip install -r requirements.txt"
        ) from exc

    spec = get_model_spec("gemini")
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=spec.model_id,
        contents=user_message,
        config=types.GenerateContentConfig(
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
        ),
    )
    usage = response.usage_metadata
    return {
        "raw_text": response.text or "",
        "input_tokens": int(usage.prompt_token_count),
        "output_tokens": int(usage.candidates_token_count),
        "model_version": spec.model_id,
        "system_fingerprint": None,
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
        "system_fingerprint": judge_response.get("system_fingerprint"),
        # v2 vet-rubric fields (None for legacy judge_v1 responses)
        "rubric_version":       RUBRIC_VERSION,
        "factual_accuracy":     parsed.get("factual_accuracy"),
        "completeness":         parsed.get("completeness"),
        "clinical_relevance":   parsed.get("clinical_relevance"),
        "organization":         parsed.get("organization"),
        "composite_score":      parsed.get("composite_score"),
        "hallucination_present": parsed.get("hallucination_present"),
        "hallucination_claims": parsed.get("hallucination_claims", []),
        # legacy / shared fields (always present)
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
                   paper_limit: int | None = None,
                   doi_filter: set[str] | None = None) -> dict[str, int]:
    """Run the blind judge loop over summaries.jsonl.

    doi_filter: when provided, only evaluate DOIs in this set. Used by
    run_phase3.py's journal-stratified sampling to pass a pre-selected
    subset without changing evaluator logic.
    """
    judges = judges or JUDGE_MODELS
    prompt_template = load_judge_prompt()
    guard = BudgetGuard()
    counts = {"evaluated": 0, "skipped": 0, "failed": 0, "no_text": 0}

    seen_papers = 0
    for entry in _iter_summaries():
        doi = str(entry.get("doi", "")).strip()
        if not doi:
            continue
        # Journal-stratified filter: skip DOIs not in the selected subset.
        if doi_filter is not None and doi not in doi_filter:
            continue
        if paper_limit is not None and seen_papers >= paper_limit:
            break
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
    require_positive_budget_for_real_run(
        dry_run=profile.dry_run,
        context="Phase 3 evaluation",
    )

    run_evaluation(
        judges=judges,
        resume=not args.no_resume,
        paper_limit=paper_limit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
