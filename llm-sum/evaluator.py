"""
llm-sum/evaluator.py — Blind MedHELM-style judge for veterinary summaries
=========================================================================

IN PLAIN ENGLISH: Three AI models (OpenAI, Anthropic, Gemini) each write a
summary of a veterinary research paper (that happens in summarizer.py, not
this file). This file takes those summaries and asks a separate AI model —
the "judge" — to grade each one, without telling the judge which AI wrote it
(a "blind" grading, like a blind taste test, so the judge can't play
favourites). The judge scores five things — faithfulness, completeness,
clinical usefulness, clarity, and safety — each on a 1-5 scale, and flags any
made-up or unsupported claims ("hallucinations"). Python (not the AI) then
does the arithmetic to turn those five numbers into one final score, because
a fixed formula is more trustworthy and checkable than trusting an AI to add
numbers correctly. The results are saved to data/evaluations.jsonl. This file
also contains code that writes easy-to-read .txt and .md report files for a
small sample of papers ("dev mode"), and code that shows a safety confirmation
prompt before any real (paid) API call is allowed to go out.

For each (paper, summariser) pair this module asks a separate judge model to
score a candidate summary against the cleaned article text. The current default
rubric is MedHELM-style: the judge returns criterion-level scores for
faithfulness, completeness, clinical usefulness, clarity, and safety, then
Python calculates the final jury score deterministically.

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
--------------------------------
Real APIs are asked for native JSON mode (OpenAI/Gemini) or a tool-use
schema (Anthropic). When that contract is honoured, the result parses
cleanly. When the model still slips in conversational text, a regex
fallback extracts each field by name. If even regex fails, quality_score
is set to 99 — the sentinel flag for manual review in Phase 5.

WHY KEEP quality_score?
    Older analysis notebooks expect a 1-10 quality_score. MedHELM rows now use
    jury_score (1-5) as the primary field, and quality_score is only a derived
    compatibility alias so historical code keeps running.

PHASE3_MODE CONTROLS THE RUN
-----------------------------
Like the summariser, this module reads ``PHASE3_MODE`` from .env and
respects ``--mode`` / ``--limit`` on the CLI. Test mode short-circuits
to mocks; single/dev caps the paper count; batch is informational here
(the evaluator does not submit a separate batch job — judge requests
are submitted by ``batch_utils`` alongside the summariser pass, and
results are collected by ``check_batch_status.py``).
"""

# This turns on a newer Python behaviour where type hints (the "-> dict"
# and ": str" notes on functions below) are treated as plain text instead of
# being evaluated immediately. You don't need to understand type hints to
# read this file — they're just notes for programmers about what kind of
# value goes in or out of a function; Python does not enforce them at runtime.
from __future__ import annotations

# "import" pulls in code that lives in another file so this file can use it.
# "from X import *" (the star) means "bring in everything that file makes
# available" — here, shared setup like DATA_DIR and REPO_ROOT used below.
from _bootstrap import *  # noqa: F401,F403

# Each of these lines below loads one built-in Python toolkit (or, further
# down, one of this project's own files) so this module can use the tools it
# provides — e.g. `json` reads/writes JSON text, `re` does pattern matching
# over text ("regular expressions"), `time` lets code pause/sleep.
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
from typing import Any, Iterable

# These lines import specific helper functions/classes from other files in
# this same project (llm-sum/*.py), rather than a general Python toolkit.
from models_config import compute_cost, get_model_spec  # noqa: E402
from file_paths import doi_to_slug  # noqa: E402
from utils import BudgetGuard, log_error, require_positive_budget_for_real_run, sleep_for_model  # noqa: E402
from phase3_mode import ModeProfile, resolve_mode, VALID_MODES  # noqa: E402
from eval_instances import EvaluationInstance, iter_evaluation_instances, load_manifest_index  # noqa: E402
from eval_metrics import calculate_automatic_metrics  # noqa: E402

# File locations: where the AI-written summaries are read FROM, and where the
# judge's scores are written TO. DATA_DIR comes from the `_bootstrap import *`
# above (it points at the project's top-level data/ folder).
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"

# Settings that control how the judge AI behaves, read from the .env file (or
# a sensible default if not set there). TEMPERATURE=0.0 asks the AI to be as
# consistent/deterministic as possible rather than creative. SEED helps make
# repeated runs reproducible where the AI provider supports it.
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
SEED = int(os.getenv("SEED", "42"))
# Bumped from 500 to 1500: the v2 rubric requests source quotes per hallucination,
# which can be verbose. 500 tokens would truncate valid responses.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1500"))
# Default changed to the MedHELM-style rubric. Set JUDGE_PROMPT_FILE in .env to
# llm-sum/prompts/judge_v2.txt only when you intentionally need the older
# Vet-Score v2.0 rubric for comparison.
JUDGE_PROMPT_FILE = Path(os.getenv("JUDGE_PROMPT_FILE", "llm-sum/prompts/judge_medhelm_v1.txt"))
# The full MedHELM-style jury panel and its 2-judge subset. Named constants so
# the default (the full panel) and the one-word JURY_PRESET switches all draw
# from a single source. ``duo`` keeps the two flagship judges; drop to ``solo``
# (openai only) for the cheapest single-grader runs.
# A "list" (the square-bracket notation below) is simply an ordered sequence
# of values — here, the names of the three AI providers that can act as judges.
JURY_PANEL = ["openai", "anthropic", "gemini"]
JURY_DUO = ["openai", "anthropic"]

# Default judge set is the FULL 3-judge panel: evaluation should be a real jury,
# not a single grader. Override with JUDGE_MODELS=... (any comma list),
# JURY_PRESET=solo|duo|panel, or --judges / --jury for a single run.
JUDGE_MODELS = [
    p.strip() for p in os.getenv("JUDGE_MODELS", ",".join(JURY_PANEL)).split(",") if p.strip()
]


def _resolve_preset_judges() -> list[str] | None:
    """Expand JURY_PRESET (read at call time) into a judge list, or None.

    IN PLAIN ENGLISH: Looks up the JURY_PRESET setting from .env (a shortcut
    word like "panel", "duo", or "solo") and turns it into the actual list of
    judge names it stands for. If no shortcut word is set, returns nothing
    (None) so the caller falls back to a different setting instead.

    ``panel`` → the full three-provider jury; ``duo`` → openai + anthropic;
    ``solo`` → a single openai judge. Read from the environment here (not
    captured at import) so a test or a between-run .env edit takes effect
    without reimporting the module. An unknown value is ignored with a warning
    so a typo cannot silently change who judges.
    """
    preset = os.getenv("JURY_PRESET", "").strip().lower()
    if preset == "panel":
        return list(JURY_PANEL)
    if preset == "duo":
        return list(JURY_DUO)
    if preset == "solo":
        return ["openai"]
    if preset:
        print(f"[phase3:evaluate] Unknown JURY_PRESET={preset!r}; "
              "falling back to JUDGE_MODELS.")
    return None


def resolve_judges(cli_judges: str | None = None, *, jury: bool = False) -> list[str]:
    """Resolve the active judge list from the layered opt-in controls.

    IN PLAIN ENGLISH: Decides which AI models will act as judges for this run,
    by checking several possible settings in priority order and using the
    first one that was actually supplied. See the numbered list below for the
    priority order.

    Precedence (first match wins), so the explicit choice always beats a preset:
        1. ``--judges openai,anthropic`` — an explicit comma-separated list.
        2. ``--jury`` — the convenience flag expanding to the full panel.
        3. ``JURY_PRESET=panel|duo|solo`` in .env.
        4. ``JUDGE_MODELS`` in .env (default: the full 3-judge panel).

    With no CLI flag and no preset this returns ``JUDGE_MODELS``, which now
    defaults to the full openai,anthropic,gemini panel.
    """
    if cli_judges:
        # A "list comprehension" — the `[... for ... in ...]` pattern below —
        # is a compact way to build a new list by processing each item from
        # an existing sequence. This one splits "openai, anthropic" on commas,
        # trims stray spaces off each piece, and drops any empty pieces.
        return [j.strip() for j in cli_judges.split(",") if j.strip()]
    if jury:
        return list(JURY_PANEL)
    preset = _resolve_preset_judges()
    if preset is not None:
        return preset
    return list(JUDGE_MODELS)

# Labels stamped onto every output row so later analysis always knows exactly
# which benchmark/rubric/code-version produced a given score.
BENCHMARK_NAME = os.getenv("EVAL_BENCHMARK_NAME", "vet_lit_summary_medhelm")
RUBRIC_VERSION = os.getenv("EVAL_RUBRIC_VERSION", "vet_medhelm_score_v1.0")
EVALUATOR_VERSION = os.getenv("EVALUATOR_VERSION", "evaluator-medhelm-v1.0")

# A "dict" (dictionary, the curly-brace notation below) is like a lookup
# table: you give it a key (here, a criterion name such as "faithfulness")
# and it hands back the matching value (here, how heavily that criterion
# counts toward the final score). This dict is the single runtime source of truth for criterion weights.
# llm-sum/eval_config/medhelm_vet_summary.yaml carries a documentation mirror
# of these same values for human readers; tests/test_weight_consistency.py
# asserts the two never silently drift apart. Keeping the weights here (not
# read from YAML) means evaluator math stays unit-testable and never relies
# on the judge to perform arithmetic correctly.
_DEFAULT_MEDHELM_CRITERION_WEIGHTS = {
    "faithfulness": 1.5,
    "completeness": 1.0,
    "clinical_usefulness": 1.2,
    "clarity": 0.8,
    "safety": 1.3,
}


def _load_criterion_weights() -> dict[str, float]:
    """Read JURY_CRITERION_WEIGHTS (a JSON object) if set, else the default.

    IN PLAIN ENGLISH: Lets you override the built-in criterion weights (above)
    from .env instead of editing code, but only if what you typed is valid —
    otherwise it quietly uses the safe defaults instead of crashing.

    Lets a researcher customise clinical-risk weighting without touching code.
    Falls back to the documented defaults on missing/invalid JSON so a typo in
    .env can't silently corrupt every score.
    """
    raw = os.getenv("JURY_CRITERION_WEIGHTS")
    if not raw:
        return dict(_DEFAULT_MEDHELM_CRITERION_WEIGHTS)
    # "try/except" means: attempt the code inside `try`, and if it raises an
    # error (of one of the listed types) instead of crashing the program,
    # jump to the `except` block and continue from there. Here: attempt to
    # read the user's custom JSON; if it's broken, just fall through below
    # and use the safe defaults instead.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and set(parsed) == set(_DEFAULT_MEDHELM_CRITERION_WEIGHTS):
            return {k: float(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    print("[phase3:evaluate] JURY_CRITERION_WEIGHTS is invalid or missing a criterion; "
          "using default weights.")
    return dict(_DEFAULT_MEDHELM_CRITERION_WEIGHTS)


MEDHELM_CRITERION_WEIGHTS = _load_criterion_weights()

# The MedHELM-style flat mean: every criterion counts equally (a weight of 1.0
# each, instead of the clinical-risk weights above). Reuses
# calculate_jury_score()'s weighted-average formula with all weights set to 1,
# rather than a second formula, so both modes share one tested code path.
UNWEIGHTED_CRITERION_WEIGHTS = {k: 1.0 for k in MEDHELM_CRITERION_WEIGHTS}

# Which aggregation becomes the primary `jury_score` field. Stock MedHELM uses
# an unweighted mean, so that is the default here; "weighted" opts into this
# project's clinical-risk-weighted formula instead. Both are always computed
# and stored regardless of this setting — see docs/phase3/medhelm_evaluation.md.
JURY_AGGREGATION_MODE = os.getenv("JURY_AGGREGATION_MODE", "unweighted").strip().lower()
if JURY_AGGREGATION_MODE not in ("unweighted", "weighted"):
    JURY_AGGREGATION_MODE = "unweighted"


def _is_dry_run() -> bool:
    """
    IN PLAIN ENGLISH: A "dry run" means practising without spending real
    money — instead of calling a real AI judge, the code makes up a fake
    (but consistent) answer. This function checks whether dry-run mode is
    currently switched on.

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

# Loads the judge's instructions ("prompt template") from a text file on
# disk — e.g. llm-sum/prompts/judge_medhelm_v1.txt — and checks it looks
# valid before handing it back. This is the fixed script that tells the judge
# AI how to grade a summary; it contains two fill-in-the-blank markers,
# {REFERENCE_TEXT} and {CANDIDATE_SUMMARY}, that get replaced with the real
# article text and the real summary before the prompt is sent to the judge.
def load_judge_prompt(prompt_file: Path = JUDGE_PROMPT_FILE) -> str:
    path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    if not path.exists():
        raise FileNotFoundError(f"Judge prompt not found: {path}")
    template = path.read_text(encoding="utf-8")
    # Make sure both fill-in-the-blank markers actually exist in the prompt
    # file, so a typo'd or edited prompt fails loudly here instead of quietly
    # sending a broken prompt to the judge later.
    for placeholder in ("{REFERENCE_TEXT}", "{CANDIDATE_SUMMARY}"):
        if placeholder not in template:
            raise ValueError(f"Judge prompt {path} missing placeholder {placeholder}")
    return template


def build_judge_prompt(reference_text: str, candidate_summary: str,
                       template: str | None = None) -> str:
    """
    IN PLAIN ENGLISH: Takes the prompt template loaded above and fills in its
    two blanks with the real article text and the real candidate summary,
    producing the exact message that gets sent to the judge AI.

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

# The list of hallucination "category" labels a judge is allowed to use when
# flagging a made-up or unsupported claim (e.g. did the summary invent a
# statistic, or leave out an important caveat?). SCORE_SENTINEL_MALFORMED
# (99) is a special "impossible" score value used purely as a red flag: if
# you ever see quality_score == 99, it means the judge's response could not
# be understood at all, not that the summary scored 99 out of anything.
VALID_HALLUCINATION_CATEGORIES = (
    "fabricated statistics", "omitted caveat", "contradiction", "unsupported_inference",
)
SCORE_SENTINEL_MALFORMED = 99


def _clamp(value: Any, lo: int, hi: int) -> int:
    """Clamp an LLM-provided integer score to [lo, hi].

    IN PLAIN ENGLISH: "Clamping" means forcing a number to stay inside an
    allowed range — e.g. if the allowed range is 1-5 and the judge somehow
    returns 0 or 9, this pushes it back to the nearest valid value (1 or 5).

    An LLM occasionally returns 0 or a value outside the specified range.
    Without clamping, a score of 0 on a 1-3 scale would silently lower the
    composite and skew all downstream statistics.

    Unparseable input falls back to ``lo``. That is right for the legacy
    Vet-Score v2.0 path (see calculate_composite_score) which has no way to
    represent "absent", but WRONG for the MedHELM criteria — use
    ``_coerce_score`` there, which distinguishes the two.
    """
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def _hallucination_count(halluc_block: dict, claims: list) -> int:
    """Reconcile the judge's stated count against the claims it actually listed.

    Two traps here. First, ``dict.get("count", default)`` does NOT fall back
    when the key is present but null — a judge answering ``"count": null``
    reached ``int(None)`` and crashed the whole response. Second, the stated
    count and the claims list can disagree; the claims are the evidence, so a
    count that undercounts them is raised to match. A count ABOVE the listed
    claims is kept, since a judge may legitimately report claims it did not
    enumerate individually.
    """
    stated = halluc_block.get("count")
    try:
        stated_int = int(stated)
    except (TypeError, ValueError):
        return len(claims)
    return max(stated_int, len(claims))


def _coerce_score(value: Any, lo: int, hi: int) -> float | int | None:
    """Clamp a score to [lo, hi], or return None when it isn't a score at all.

    The distinction ``_clamp`` cannot make: a judge that returned 9 gave a
    score (clamp it to 5); a judge that returned null, "n/a" or "4/5", or
    omitted the criterion entirely, gave NO score. Collapsing the second case
    onto the floor states the worst possible result on the judge's behalf and
    keeps full weight in the denominator, so a parse failure is indistinguishable
    from a genuine 1 and drags every downstream mean toward it.

    Fractional values are preserved rather than truncated, and an integral
    result is returned as an ``int`` so ordinary judge output (1-5 integers)
    keeps its existing shape in the JSONL. Truncating would silently corrupt
    the averaged criterion scores that report_tables produces when one judge
    rated the same item twice — a merged 4.5 must not become 4.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        clamped = max(float(lo), min(float(hi), float(value)))
    except (TypeError, ValueError):
        return None
    return int(clamped) if clamped.is_integer() else clamped


def calculate_composite_score(scores: dict) -> float:
    """Compute the Vet-Score v2.0 composite from four dimension scores.

    IN PLAIN ENGLISH: This is the OLDER scoring formula (from before the
    current MedHELM-style rubric) kept here so old data can still be read.
    It takes four 1-3 scores the judge gave, multiplies each by its own
    importance weight, adds them up, and rescales the result onto a 1-10
    scale. See ``calculate_jury_score`` below for the current formula.

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


def calculate_jury_score(criteria_scores: dict, *,
                         weights: dict[str, float] | None = None) -> float:
    """Compute a jury score on a 1-5 scale as a weighted average of criteria.

    IN PLAIN ENGLISH: This is the CURRENT, primary scoring formula. It takes
    the five 1-5 criterion scores the judge gave (faithfulness, completeness,
    clinical usefulness, clarity, safety), multiplies each by how much it
    should count (its "weight"), and divides by the total weight to get one
    overall number, still on a 1-5 scale. Call it once with the project's
    clinical-risk weights, and once with all weights equal, to get both the
    "weighted" and "unweighted" versions of the score.

    The judge supplies criterion scores only. Python performs the weighted
    average because deterministic arithmetic is easier to audit, unit test, and
    explain in a methods section than asking every judge response to do math.

    Pass ``weights=MEDHELM_CRITERION_WEIGHTS`` for this project's clinical-risk
    weighting, or ``weights=UNWEIGHTED_CRITERION_WEIGHTS`` for stock MedHELM's
    flat unweighted mean — both share this one formula and code path.
    """
    resolved_weights = weights or MEDHELM_CRITERION_WEIGHTS
    weighted_sum = 0.0
    weight_total = 0.0
    # Walk through each (criterion name, weight) pair — e.g. ("faithfulness",
    # 1.5) — look up the judge's score for that criterion, clamp it into
    # range, and add "score x weight" to a running total (and the weight
    # itself to a separate running total, used as the divisor below).
    #
    # A criterion the judge never scored contributes to NEITHER total, so the
    # average renormalizes over the criteria actually present. Previously a
    # missing criterion was imputed as 1 while keeping its full weight in the
    # divisor: with faithfulness (weight 1.5 of 5.5) absent and the other four
    # scored 4, that produced 3.18 instead of 4.00. This matches how
    # human_review scores a partially-filled reviewer sheet, which is what lets
    # the human and LLM composites be correlated against each other at all.
    for criterion, weight in resolved_weights.items():
        raw_value = criteria_scores.get(criterion)
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("score")
        score = _coerce_score(raw_value, 1, 5)
        if score is None:
            continue
        weighted_sum += score * weight
        weight_total += weight
    if weight_total == 0:
        # No criterion was scored at all — there is no composite to report.
        # None (not 0.0) so downstream means exclude it instead of averaging in
        # a value below the bottom of the 1-5 scale.
        return None
    return round(weighted_sum / weight_total, 2)


def missing_criteria(criteria_scores: dict,
                     *, weights: dict[str, float] | None = None) -> list[str]:
    """Which criteria carry no usable score, in the canonical weight order.

    Companion to ``calculate_jury_score`` — that function silently renormalizes
    over what is present, so this reports what was absent. Written onto every
    parsed row as ``missing_criteria`` so a partially-scored judgement is
    visible in the data rather than only in the arithmetic.

    Kept as a sibling rather than a second return value from
    ``calculate_jury_score`` so that function keeps returning a bare score and
    its callers (and their tests) do not have to unpack a tuple.
    """
    resolved_weights = weights or MEDHELM_CRITERION_WEIGHTS
    absent: list[str] = []
    for criterion in resolved_weights:
        raw_value = criteria_scores.get(criterion)
        if isinstance(raw_value, dict):
            raw_value = raw_value.get("score")
        if _coerce_score(raw_value, 1, 5) is None:
            absent.append(criterion)
    return absent


def scale_jury_to_quality_score(jury_score: float | None) -> int:
    """Map the 1-5 jury scale to the legacy 1-10 quality_score field.

    IN PLAIN ENGLISH: Converts the current 1-5 jury_score into the older
    1-10 quality_score format, purely so old spreadsheets/notebooks that
    expect a 1-10 number keep working. It is not a second, independent score.

    The legacy field is retained so older analysis scripts do not crash, but
    new analysis should prefer `jury_score` for MedHELM-style rows.
    """
    # None means no criterion was scored at all, which is exactly the
    # "unreadable judgement" case the 99 sentinel exists to mark.
    if jury_score is None or jury_score <= 0:
        return SCORE_SENTINEL_MALFORMED
    return int(round(((jury_score - 1) / 4) * 9 + 1))


def _mean_and_spread(values: list[float]) -> tuple[float | None, float | None]:
    """Return (mean, max-minus-min) for a list of scores, or (None, None).

    IN PLAIN ENGLISH: Given a list of numbers (e.g. one score per judge for
    the same paper), returns the average, and how "spread apart" the judges
    were (the highest score minus the lowest). A spread of 0 means every
    judge gave the exact same score; a bigger spread means more disagreement.

    Max-minus-min is used as the disagreement measure because it is easy to
    explain to a beginner reader and needs no scipy. Krippendorff's alpha in
    reliability.py is the chance-corrected companion for multi-judge runs.
    """
    if not values:
        return None, None
    return round(sum(values) / len(values), 2), round(max(values) - min(values), 2)


def aggregate_jury_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate multiple judge rows for the same paper-summary pair.

    IN PLAIN ENGLISH: When more than one judge grades the same summary, this
    combines their individual scores into one summary view: the average
    score, how many judges took part, and how much they disagreed. It does
    this separately for the "weighted" score and the "unweighted" score.

    MedHELM-style reliability mode can use several judges. The append-only
    JSONL design stores one row per judge, so this helper computes the
    cross-judge view without rewriting history.

    Both aggregation methods are averaged across judges — not just the primary
    ``jury_score`` — so switching ``JURY_AGGREGATION_MODE`` after a multi-judge
    run never needs the judges re-run. Each mode also carries its own
    disagreement spread, because judges can agree on the weighted score while
    disagreeing on the unweighted one (or vice versa).
    """
    # A small helper (defined right here, only used inside this function)
    # that pulls out every judge's numeric value for one field name (e.g.
    # "jury_score"), skipping any row where that field is missing or not a
    # number, and returns them as a plain list of numbers.
    def _values(field: str) -> list[float]:
        return [
            float(row[field])
            for row in rows
            if isinstance(row.get(field), (int, float))
        ]

    primary_mean, primary_spread = _mean_and_spread(_values("jury_score"))
    weighted_mean, weighted_spread = _mean_and_spread(_values("jury_score_weighted"))
    unweighted_mean, unweighted_spread = _mean_and_spread(_values("jury_score_unweighted"))

    return {
        "jury_score": primary_mean,
        "judge_count": len(rows),
        "valid_judge_count": len(_values("jury_score")),
        "judge_disagreement": primary_spread,
        # Both aggregation methods preserved across judges (see docstring).
        "jury_score_weighted_mean": weighted_mean,
        "jury_score_unweighted_mean": unweighted_mean,
        "judge_disagreement_weighted": weighted_spread,
        "judge_disagreement_unweighted": unweighted_spread,
    }


def _normalize_criteria_scores(raw_scores: Any) -> dict[str, dict[str, Any]]:
    """Return a complete criterion-score mapping with clamped integer scores.

    IN PLAIN ENGLISH: Cleans up whatever the judge sent back for the five
    criteria into one consistent, predictable shape — always exactly five
    criteria, each with a reasoning note and either a valid 1-5 score or None
    when the judge did not give one.

    Judges occasionally omit a criterion or return a string instead of an
    integer. The score is then ``None``, NOT 1. Filling it with the lowest
    valid score reads as "the judge said this summary is terrible on
    faithfulness" when the judge in fact said nothing, and — because the
    criterion kept full weight in the composite's divisor — dragged every
    downstream mean toward the floor with no flag to find it by.

    All five keys are always present so the record shape never varies; absence
    is expressed by ``score: None`` and reported separately by
    ``missing_criteria``. Consumers must therefore isinstance-check the score
    before arithmetic (report_tables._criterion_scores and
    reliability._criterion_value both already do).
    """
    normalized: dict[str, dict[str, Any]] = {}
    source = raw_scores if isinstance(raw_scores, dict) else {}
    # Go through the five expected criteria one at a time (ignoring whatever
    # order the judge might have used) so the result always has the same five
    # keys in the same order, never more, never fewer.
    for criterion in MEDHELM_CRITERION_WEIGHTS:
        item = source.get(criterion, {})
        if isinstance(item, dict):
            score = item.get("score")
            reasoning = str(item.get("reasoning", ""))
        else:
            score = item
            reasoning = ""
        normalized[criterion] = {
            "score": _coerce_score(score, 1, 5),
            "reasoning": reasoning,
        }
    return normalized


def _try_parse_json_block(text: str) -> dict | None:
    """
    IN PLAIN ENGLISH: Tries to find and read the judge's answer as JSON (a
    structured, computer-friendly text format using {} and "quotes"), even if
    the AI wrapped it in extra chatty sentences like "Sure! Here's my
    evaluation: {...}". Returns None (nothing) if no usable JSON is found
    anywhere in the text.

    Try strict JSON parse, then a relaxed search for the first {...} block.
    Returns None if no valid JSON object is found.
    """
    text = text.strip()
    # First, the easy case: maybe the whole response IS valid JSON already.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first balanced {...} block — handles wrappers like
    # "Sure! Here is the evaluation: { ... } Let me know if you need anything."
    # This walks character-by-character, counting "{" as +1 and "}" as -1, to
    # find the point where the curly braces balance back out to zero — that
    # marks the end of one complete, self-contained JSON object.
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


# A "regular expression" (or "regex", built with re.compile) is a pattern
# used to search text for something that matches a shape — like "the digits
# right after the word factual_accuracy and a colon" — even when the
# surrounding text isn't valid JSON. This dict maps each field name to the
# regex pattern used to fish that one field's number out of raw judge text
# as a last-resort fallback, when JSON parsing has already failed.
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
    IN PLAIN ENGLISH: This is the heart of turning a judge AI's raw text
    answer into clean, structured data Python can work with. It tries several
    approaches in order, each one a fallback for when the previous one
    fails, ending in a "we have no idea what happened" sentinel if nothing
    worked — so a broken response never silently vanishes, it just gets
    flagged for a human to check.

    Extract the structured fields from a judge response.

    Order of attempts:
        1. Strict JSON / first balanced {...} block.
           1a. MedHELM schema ('criteria_scores') -> weighted jury_score.
           1b. Vet-Score v2 schema ('factual_accuracy') -> old composite.
           1c. Legacy schema ('quality_score') -> old 1-10 score.
        2. Regex over the raw text (supports old schemas only).
        3. Sentinel quality_score=99, flagged for human review.

    Always returns the shared fields needed by build_evaluation_row(). New
    MedHELM rows include criteria_scores and jury_score. Older schemas still
    parse so previously generated batch results and tests remain usable.
    """
    # Step 1: try to read the response as JSON at all (see
    # _try_parse_json_block above). `obj` will be a dict if this worked, or
    # None if the text wasn't JSON-shaped in any recognisable way.
    obj = _try_parse_json_block(raw_text)

    if isinstance(obj, dict) and "criteria_scores" in obj:
        # --- Path 1a: MedHELM-style schema (the current, expected format) ---
        # criteria_scores -> deterministic jury_score. This is the normal,
        # happy-path branch: the judge answered in the current five-criteria
        # format, so Python computes both the weighted and unweighted jury
        # scores and packages up the hallucination claims it reported.
        criteria_scores = _normalize_criteria_scores(obj.get("criteria_scores"))
        jury_score_weighted = calculate_jury_score(criteria_scores, weights=MEDHELM_CRITERION_WEIGHTS)
        jury_score_unweighted = calculate_jury_score(criteria_scores, weights=UNWEIGHTED_CRITERION_WEIGHTS)
        jury_score = jury_score_weighted if JURY_AGGREGATION_MODE == "weighted" else jury_score_unweighted
        halluc_block = obj.get("hallucination") or {}
        claims = list(halluc_block.get("claims") or [])
        confidence = _clamp(obj.get("confidence_score", 1), 1, 5)
        return {
            "criteria_scores": criteria_scores,
            "jury_score": jury_score,
            "jury_score_weighted": jury_score_weighted,
            "jury_score_unweighted": jury_score_unweighted,
            # Which criteria the judge left unscored. Empty on the happy path.
            # The composite above renormalizes over what IS present, so without
            # this the renormalization would be invisible in the stored row.
            "missing_criteria": missing_criteria(criteria_scores),
            "jury_aggregation_mode": JURY_AGGREGATION_MODE,
            "judge_count": 1,
            "valid_judge_count": 1,
            "judge_disagreement": 0.0,
            # Legacy dimensional aliases keep existing notebooks readable. They
            # are not the primary MedHELM scores; new analysis should use
            # criteria_scores and jury_score. None when the judge omitted that
            # criterion — the aliases inherit the same absent-vs-1 distinction.
            "factual_accuracy": criteria_scores["faithfulness"]["score"],
            "completeness": criteria_scores["completeness"]["score"],
            "clinical_relevance": criteria_scores["clinical_usefulness"]["score"],
            "organization": criteria_scores["clarity"]["score"],
            "composite_score": jury_score,
            "hallucination_present": bool(halluc_block.get("present", len(claims) > 0)),
            "hallucination_count": _hallucination_count(halluc_block, claims),
            "hallucination_claims": claims,
            "hallucination_categories": list({c.get("category", "") for c in claims if c.get("category")}),
            "confidence_score": confidence,
            "reasoning": str(obj.get("reasoning", "")),
            "quality_score": scale_jury_to_quality_score(jury_score),
            "parse_method": "json",
        }

    if isinstance(obj, dict) and "factual_accuracy" in obj:
        # --- Path 1b: v2 (older vet rubric) schema ---
        # The response wasn't in the current criteria_scores format, but it
        # does look like the older four-dimension "Vet-Score v2.0" format
        # (factual_accuracy/completeness/clinical_relevance/organization).
        # Handled so old batch results and comparison runs still parse.
        composite = calculate_composite_score(obj)
        halluc_block = obj.get("hallucination") or {}
        claims = list(halluc_block.get("claims") or [])
        confidence = _clamp(obj.get("confidence_score", 1), 1, 5)
        return {
            "criteria_scores": {},
            "jury_score": None,
            "jury_score_weighted": None,
            "jury_score_unweighted": None,
            "jury_aggregation_mode": JURY_AGGREGATION_MODE,
            "judge_count": 1,
            "valid_judge_count": 1,
            "judge_disagreement": 0.0,
            "factual_accuracy":   _clamp(obj.get("factual_accuracy",  1), 1, 3),
            "completeness":       _clamp(obj.get("completeness",       1), 1, 3),
            "clinical_relevance": _clamp(obj.get("clinical_relevance", 1), 1, 3),
            "organization":       _clamp(obj.get("organization",       1), 1, 3),
            "composite_score":    composite,
            "hallucination_present":   bool(halluc_block.get("present", len(claims) > 0)),
            "hallucination_count":     _hallucination_count(halluc_block, claims),
            "hallucination_claims":    claims,
            "hallucination_categories": list({c.get("category", "") for c in claims if c.get("category")}),
            "confidence_score": confidence,
            "reasoning": str(obj.get("reasoning", "")),
            # quality_score kept for backward compat with any Phase 5 code
            "quality_score": round(composite),
            "parse_method": "json",
        }

    if isinstance(obj, dict) and "quality_score" in obj:
        # --- Path 1c: legacy schema (judge_v1 responses) ---
        # Neither of the newer schemas matched, but the response is valid
        # JSON with the oldest "quality_score" field (a direct 1-10 number,
        # no per-criterion breakdown). Handled so the earliest study runs
        # remain readable.
        confidence = _clamp(obj.get("confidence_score", 1), 1, 5)
        return {
            "criteria_scores": {}, "jury_score": None,
            "jury_score_weighted": None, "jury_score_unweighted": None,
            "jury_aggregation_mode": JURY_AGGREGATION_MODE,
            "judge_count": 1, "valid_judge_count": 1, "judge_disagreement": 0.0,
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

    # --- Step 2: none of the JSON paths above matched (obj was None, or a
    # dict missing all three known field names), so fall back to hunting for
    # individual numbers in the raw text using the regex patterns defined
    # earlier. Regex fallback extracts older rubric fields one at a time. The
    # MedHELM schema is nested JSON, so a broken MedHELM response falls
    # through to the sentinel instead of pretending that partial nested data
    # is reliable.
    fields: dict[str, Any] = {}
    for key, regex in _REGEX_FALLBACK.items():
        m = regex.search(raw_text)
        if m:
            try:
                fields[key] = int(m.group(1))
            except ValueError:
                pass

    if "factual_accuracy" in fields:
        # v2 regex path — the regex found the older four-dimension field
        # names in the raw text, so compute the old composite from those.
        composite = calculate_composite_score(fields)
        confidence = _clamp(fields.get("confidence_score", 1), 1, 5)
        return {
            "criteria_scores": {}, "jury_score": None,
            "jury_score_weighted": None, "jury_score_unweighted": None,
            "jury_aggregation_mode": JURY_AGGREGATION_MODE,
            "judge_count": 1, "valid_judge_count": 1, "judge_disagreement": 0.0,
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
        # legacy regex path — the regex found an old-style "quality_score:"
        # number in the raw text, so use that directly, plus whatever
        # hallucination category words can be pulled out below.
        cats_match = _REGEX_CATEGORIES.search(raw_text)
        if cats_match:
            cats_raw = cats_match.group(1)
            categories = [c.strip().strip('"').strip("'") for c in cats_raw.split(",") if c.strip()]
        else:
            categories = []
        confidence = _clamp(fields.get("confidence_score", 1), 1, 5)
        return {
            "criteria_scores": {}, "jury_score": None,
            "jury_score_weighted": None, "jury_score_unweighted": None,
            "jury_aggregation_mode": JURY_AGGREGATION_MODE,
            "judge_count": 1, "valid_judge_count": 1, "judge_disagreement": 0.0,
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

    # --- Step 3: total failure. Nothing above could make sense of the raw
    # text — not as JSON, not even with regex pattern-hunting. Rather than
    # crashing or silently dropping this result, return the special
    # SCORE_SENTINEL_MALFORMED (99) score so it's obviously wrong at a
    # glance, and mark it for a human to check in Phase 5.
    # Sentinel: flag for manual review in Phase 5. We preserve the raw response
    # excerpt in the final row, so a human can inspect what the judge returned.
    return {
        "criteria_scores": {},
        "jury_score": None,
        "jury_score_weighted": None,
        "jury_score_unweighted": None,
        "jury_aggregation_mode": JURY_AGGREGATION_MODE,
        "judge_count": 1,
        "valid_judge_count": 0,
        "judge_disagreement": None,
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
    IN PLAIN ENGLISH: Decides yes/no whether this judge's verdict is
    trustworthy enough to accept as-is, or whether a human should double
    check it. Returns True (needs review) if any red flag below is present.

    True if the row should be flagged for Phase-5 manual review. Triggers on:
    - Low confidence (< 3): the judge is guessing
    - Sentinel score (99): response was malformed
    - Any major-severity hallucination claim: a finding that could mislead a
      clinician must be checked by a human even if overall confidence is high
    """
    # `any(... for ... in ...)` checks a list and returns True the moment it
    # finds even one item matching the condition — here, at least one
    # hallucination claim marked "major" severity.
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
    """Use SHA-256 instead of Python's salted hash() for reproducible dry runs.

    IN PLAIN ENGLISH: A "hash" turns any text into a big number in a way
    that's consistent — the same text always produces the same number, but
    even a tiny change in the text produces a totally different number. This
    is used below to make up fake-but-repeatable scores during dry runs,
    without needing an actual AI call.
    """
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _mock_judge_response(provider: str, reference_text: str,
                         candidate_summary: str) -> dict:
    """
    IN PLAIN ENGLISH: Stands in for a real judge AI call when running in
    dry-run/test mode (no money spent, no network call). Makes up scores
    that look plausible and are always the same for the same input text, so
    tests are repeatable.

    Deterministic fake response for DRY_RUN. Uses hashes of the text so
    different summaries get different (but reproducible) scores. Returns
    the MedHELM schema so dry-run/test mode exercises the default parse path.
    """
    spec = get_model_spec(provider)
    h_cand = _stable_hash_int(candidate_summary)
    h_ref  = _stable_hash_int(reference_text)
    # Criterion scores: deterministic, spread across 1-5 so dry-run exercises
    # the MedHELM parser without spending money or depending on random state.
    faithfulness = (h_cand % 5) + 1
    completeness = (h_ref % 5) + 1
    clinical_usefulness = ((h_cand + h_ref) % 5) + 1
    clarity = ((h_cand * 31) % 5) + 1
    safety = ((h_ref * 17) % 5) + 1
    confidence = (h_ref % 5) + 1
    fake_payload = {
        "criteria_scores": {
            "faithfulness": {"score": faithfulness, "reasoning": "[MOCK] source support"},
            "completeness": {"score": completeness, "reasoning": "[MOCK] checklist coverage"},
            "clinical_usefulness": {"score": clinical_usefulness, "reasoning": "[MOCK] clinical value"},
            "clarity": {"score": clarity, "reasoning": "[MOCK] readability"},
            "safety": {"score": safety, "reasoning": "[MOCK] clinical safety"},
        },
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
        "model_version": f"{spec.model_id}{DRY_RUN_MODEL_SUFFIX}",
    }


# ---------------------------------------------------------------------------
# Dry-run contamination guards
# ---------------------------------------------------------------------------

# WHY THIS EXISTS: on 2026-07-09 a PHASE3_MODE=test run appended 16 mock rows
# to data/evaluations.jsonl. Nothing downstream distinguished them from real
# judge output, so every report for the next five days averaged hash-derived
# scores in with real ones, and one paper (10.1111/jvim.16872) was locked out
# of ever being judged because the resume check saw its mock row and skipped
# it. These predicates are the single definition of "this came from a mock",
# shared by every reader and writer so the two can never drift apart.

# The suffix _mock_judge_response (above) and summarizer._mock_summary_payload
# both stamp onto model_version. Matching the suffix rather than a specific
# model id means a model upgrade cannot silently defeat the filter.
DRY_RUN_MODEL_SUFFIX = "-DRYRUN"

# Secondary signal: the mock's fixed reasoning string. Catches a row that
# somehow lost its model_version but kept its body.
DRY_RUN_REASONING = "[MOCK] dry-run evaluation"


def dry_run_sibling(path: Path) -> Path:
    """Where a dry-run write goes instead of ``path``: a ``_dryrun`` sibling.

    Derived from the target rather than hardcoded to data/, so redirecting
    stays inside whatever directory the caller was already writing to. A
    fixed constant would send a test writing to a tmp path into the real
    data/ folder instead — the guard must not itself become a way to write
    somewhere unexpected.
    """
    return path.with_name(f"{path.stem}_dryrun{path.suffix}")


def is_dry_run_row(row: Any) -> bool:
    """True when an evaluation row came from the dry-run mock, not a judge.

    IN PLAIN ENGLISH: answers "was this score made up by the offline test
    stand-in instead of a real AI judge?" Mock rows must never reach a report,
    a resume decision, or the production ledger.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("judge_model_version") or "").endswith(DRY_RUN_MODEL_SUFFIX):
        return True
    return str(row.get("reasoning") or "").strip() == DRY_RUN_REASONING


def is_dry_run_slot(slot: Any) -> bool:
    """True when a summaries.jsonl per-provider model slot holds mock output.

    Summaries carry the dry-run marker one level down — inside each provider's
    slot under ``models`` — rather than at row level, so the row-shaped
    predicate above does not apply.
    """
    if not isinstance(slot, dict):
        return False
    return str(slot.get("model_version") or "").endswith(DRY_RUN_MODEL_SUFFIX)


def is_production_path(candidate: Path | None, canonical: Path) -> bool:
    """True when ``candidate`` refers to the same file as ``canonical``.

    IN PLAIN ENGLISH: "is the caller about to write to the real data file?"

    Compares fully resolved, case-normalised paths rather than the Path
    objects themselves. A bare ``==`` would let ``DATA_DIR / "evaluations.jsonl"``
    slip past ``EVALUATIONS_PATH`` even though both name one file, and this
    repo lives on a OneDrive-backed Windows path where case-insensitivity and
    reparse points make raw Path equality unreliable. A path that cannot be
    resolved (permissions, a vanished parent) is treated as NOT production:
    the guard should never crash a run, and the fallback is a redirect target
    rather than the real ledger.
    """
    if candidate is None:
        return True  # None means "use the default", which is the production file
    try:
        left = os.path.normcase(os.path.realpath(str(candidate)))
        right = os.path.normcase(os.path.realpath(str(canonical)))
    except (OSError, ValueError):
        return False
    return left == right


def call_judge(provider: str, reference_text: str, candidate_summary: str,
               *, prompt_template: str | None = None,
               max_retries: int = 3) -> dict:
    """
    IN PLAIN ENGLISH: The single entry point for asking one specific judge
    ("openai", "anthropic", or "gemini") to grade one summary. In dry-run
    mode it calls the fake mock instead. On a real run, it builds the blind
    prompt, sends it to the right provider's function below, and retries a
    few times (waiting a little longer each time) if something goes wrong
    before finally giving up and raising an error.

    Call a judge model and return the raw text + token + version metadata.
    Parsing happens in a separate step so failed parses can still log the
    raw text for inspection.
    """
    if _is_dry_run():
        return _mock_judge_response(provider, reference_text, candidate_summary)

    user_message = build_judge_prompt(reference_text, candidate_summary, prompt_template)

    last_error: Exception | None = None
    # Try up to `max_retries` times. Real API calls sometimes fail for
    # temporary reasons (network hiccup, rate limit) — retrying with a short
    # pause first is standard practice before giving up entirely.
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
                # Wait longer after each failed attempt (1s, then 2s, then
                # 4s, ...) — a common pattern called "exponential backoff"
                # that gives a struggling service more time to recover.
                time.sleep(2 ** attempt)

    log_error("N/A", f"evaluate_{provider}", f"Judge failed after retries: {last_error}")
    raise RuntimeError(f"Judge {provider} failed: {last_error}")


# The three functions below (_call_judge_openai, _call_judge_anthropic,
# _call_judge_gemini) each know how to talk to one specific AI provider's API
# using that provider's own Python library. They all do the same conceptual
# job — send the judge prompt, get back the AI's raw text answer plus how
# many "tokens" (word-pieces) were used — just with each provider's own
# function names and response shape.
def _call_judge_openai(user_message: str) -> dict:
    import openai  # type: ignore[import-not-found]
    spec = get_model_spec("openai")
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=spec.model_id,
        messages=[{"role": "user", "content": user_message}],
        temperature=TEMPERATURE,
        # Newer reasoning-capable OpenAI models (e.g. gpt-5.x) reject the
        # older `max_tokens` name and require `max_completion_tokens`
        # instead — see summarizer._openai_completion_token_arg().
        max_completion_tokens=MAX_OUTPUT_TOKENS,
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


# Sends the judge prompt to Anthropic's Claude API and returns its answer.
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


# Sends the judge prompt to Google's Gemini API and returns its answer.
def _call_judge_gemini(user_message: str) -> dict:
    # This loads the Gemini library by name at runtime (rather than a normal
    # `import` line at the top of the file) so the rest of this file — and
    # every test that doesn't touch Gemini — still works even if that
    # optional package isn't installed. If it's missing, give a clear,
    # actionable error message instead of a confusing crash.
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

# IN PLAIN ENGLISH: Builds one complete "row" of output — everything that
# will be saved to data/evaluations.jsonl for one (paper, summariser, judge)
# combination. It takes the judge's raw answer, runs it through
# parse_judge_response to get clean numbers, adds identifying details (which
# paper, which summariser, which judge, which rubric version), and returns it
# all as one dictionary ready to be written out.
def build_evaluation_row(*, doi: str, summariser: str, judge: str,
                         input_source: str = "processed",
                         reference_text: str, candidate_summary: str,
                         judge_response: dict,
                         strata: dict[str, Any] | None = None,
                         automatic_metrics: dict[str, Any] | None = None) -> dict:
    # Parse first, then enrich. Keeping parsing separate from row construction
    # makes it possible to test malformed judge responses without creating fake
    # DOI/source metadata.
    parsed = parse_judge_response(judge_response["raw_text"])
    # Automatic metrics are optional caller input because run_evaluation()
    # computes them once per summary and reuses them across multiple judges.
    # Direct unit tests can omit them and still get a complete row.
    resolved_metrics = (
        automatic_metrics
        if automatic_metrics is not None
        else calculate_automatic_metrics(reference_text, candidate_summary)
    )
    return {
        # Benchmark identity fields are written into every row so later analyses
        # never have to guess which rubric produced a score.
        "benchmark_name": BENCHMARK_NAME,
        "doi": doi,
        "input_source": input_source,
        # Summarizer and judge are metadata only. The summarizer name is never
        # passed into build_judge_prompt(), preserving the blind protocol.
        "summarizer": summariser,
        "judge": judge,
        "judge_model_version": judge_response["model_version"],
        "system_fingerprint": judge_response.get("system_fingerprint"),
        "evaluator_version": EVALUATOR_VERSION,
        "rubric_version":       RUBRIC_VERSION,
        # MedHELM primary scoring fields. jury_score is the primary endpoint for
        # new evaluation; quality_score below is only a compatibility alias.
        "criteria_scores":      parsed.get("criteria_scores", {}),
        "jury_score":           parsed.get("jury_score"),
        # Always computed together so the two aggregation methods can be
        # compared without re-running judges — see docs/phase3/medhelm_evaluation.md.
        "jury_score_weighted":   parsed.get("jury_score_weighted"),
        "jury_score_unweighted": parsed.get("jury_score_unweighted"),
        "jury_aggregation_mode": parsed.get("jury_aggregation_mode", JURY_AGGREGATION_MODE),
        "judge_count":          parsed.get("judge_count", 1),
        "valid_judge_count":    parsed.get("valid_judge_count", 1),
        "judge_disagreement":   parsed.get("judge_disagreement", 0.0),
        # Automatic metrics are stored as secondary evidence, not as the main
        # score, because word overlap cannot prove clinical correctness.
        "automatic_metrics":    resolved_metrics,
        # Strata carry species/study/topic/journal labels for MedHELM-style
        # subgroup reporting.
        "strata":               strata or {"input_source": input_source},
        # Legacy/v2 aliases stay present so existing notebooks do not fail.
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


# Saves one evaluation row (built by build_evaluation_row above) to
# data/evaluations.jsonl by adding it as a new line at the end of the file
# ("a" below means "append" — add to the end, never overwrite what's already
# there). This is why the file is called "append-only": once a row is
# written, this function never goes back and edits or deletes older rows.
def append_evaluation(row: dict, path: Path | None = None) -> None:
    resolved = path if path is not None else EVALUATIONS_PATH

    # Dry-run rows must never enter the production ledger. This is keyed off
    # "is the target the real file?" rather than "was path omitted?" because
    # check_batch_status calls append_evaluation(row, EVALUATIONS_PATH) with
    # the path explicit — a `path is None` test would miss it entirely.
    # Redirect rather than drop, so test mode still leaves an audit trail.
    # Never raises: a test-mode run must still complete.
    if is_dry_run_row(row) and is_production_path(path, EVALUATIONS_PATH):
        resolved = dry_run_sibling(EVALUATIONS_PATH)
        print(f"[phase3] dry-run row → {resolved.name} (not production)")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Readable dev-eval mirrors (data/dev_evals_jsonl/, data/dev_detailEval_reports/)
# ---------------------------------------------------------------------------

# -----------------------------------------------------------------------
# Everything from here down builds human-readable report files (.txt and
# .md) that mirror what's already in data/evaluations.jsonl, purely so a
# person can read a paper's judge results without opening raw JSON. These
# functions never change evaluations.jsonl itself — they only read from it.
# -----------------------------------------------------------------------

# Plain-English display labels for the five MedHELM criteria, in report order.
CRITERION_LABELS: dict[str, str] = {
    "faithfulness": "Factual Accuracy",
    "completeness": "Completeness",
    "clinical_usefulness": "Practical Usefulness",
    "clarity": "Clarity",
    "safety": "Safety",
}


def _rows_by_doi_for(
    dois: set[str], evaluations_path: Path | None = None,
) -> dict[str, list[dict]]:
    """Read data/evaluations.jsonl rows for the given DOIs, grouped by doi.

    IN PLAIN ENGLISH: Reads through the evaluations file and collects every
    row that belongs to one of the requested papers (identified by DOI, a
    paper's unique ID), bundling all of one paper's rows together — since
    each paper can have multiple rows (one per summariser/judge pair).

    Shared read path for both readable mirrors below, so a re-judge's rows
    are grouped identically regardless of which mirror is being written.
    """
    source = evaluations_path if evaluations_path is not None else EVALUATIONS_PATH

    # In a LIVE run, mock rows are strays and must never print "[MOCK] ..."
    # reasoning beside real judge output. In a DRY run they are the expected
    # content — and they live in the _dryrun sibling the write guard diverted
    # them to — so read that instead and keep them. Otherwise test mode would
    # always produce empty mirrors.
    dry_run = resolve_mode().dry_run
    sources = [source]
    if dry_run:
        sibling = dry_run_sibling(source)
        if sibling != source:
            sources.append(sibling)

    rows_by_doi: dict[str, list[dict]] = {}
    for row in (r for s in sources for r in _iter_summaries(s)):
        if not dry_run and is_dry_run_row(row):
            continue
        doi = str(row.get("doi", "")).strip()
        if doi and doi in dois:
            # `.setdefault(doi, [])` means "if this DOI doesn't have a list
            # yet, start one"; either way, add this row onto that DOI's list.
            rows_by_doi.setdefault(doi, []).append(row)
    return rows_by_doi


def _lookup_title(doi: str, manifest_index: dict[str, dict] | None) -> str:
    """Best-effort article title for ``doi`` from a manifest index.

    IN PLAIN ENGLISH: Looks up the human-readable title of a paper given its
    DOI, so report files can show "A Study of X in Cats" instead of just a
    DOI number. Returns an empty string if no title can be found.

    ``manifest_index`` is normally ``eval_instances.load_manifest_index()``'s
    output (keyed off the long-lived data/manifest.jsonl, not the prunable
    data/summaries.jsonl snapshot). Collapses stray whitespace/newlines that
    raw manifest titles sometimes carry, same reasoning as
    ``eval_report._clean_text``.
    """
    if not doi or not manifest_index:
        return ""
    title = (manifest_index.get(doi) or {}).get("title")
    return " ".join(str(title).split()) if title else ""


def _format_dev_eval_entry_as_text(
    doi: str, rows: list[dict], *, manifest_index: dict[str, dict] | None = None,
) -> str:
    """Render one paper's judge rows into a plain-English report.

    IN PLAIN ENGLISH: Builds the actual text content of one .txt report file
    for one paper — a short header (DOI, journal, title) followed by one
    section per (summariser, judge) pair showing that judge's score and
    reasoning. This is the text-building step; the function below it
    (write_dev_eval_jsonl_outputs) is what actually saves this text to disk.

    The readable sibling of ``summarizer._format_dev_summary_entry_as_text``:
    the eval-side counterpart written to ``data/dev_evals_jsonl/`` so a human
    can eyeball dev judge results without parsing JSON. The header begins with
    ``DOI:`` so ``eval_instances.read_dois_from_dev_folder`` can read it back for
    the incremental skip. ``data/evaluations.jsonl`` remains the source of truth.
    """
    strata = rows[0].get("strata") if rows and isinstance(rows[0].get("strata"), dict) else {}
    journal = str(strata.get("journal") or "Not recorded")
    input_source = str(strata.get("input_source") or rows[0].get("input_source") or "processed")
    title = _lookup_title(doi, manifest_index)

    lines = [
        f"DOI: {doi or 'Not recorded'}",
        f"Journal: {journal}",
        f"Title: {title or 'Not recorded'}",
        f"Input Source: {input_source}",
        "",
        "This file mirrors data/evaluations.jsonl for this DOI in a human-readable",
        "form so a dev-mode judge run can be eyeballed; the JSONL file remains the",
        "source of truth. One section per (summarizer, judge) pair below.",
    ]

    # `sorted(..., key=lambda r: ...)` sorts the rows using a small one-line
    # function (a "lambda") that says what to sort by — here, first by
    # summariser name, then by judge name — so the file's section order is
    # always the same regardless of which order the rows were originally
    # written in.
    # Deterministic ordering so the readable file is stable across re-runs.
    for row in sorted(
        rows, key=lambda r: (str(r.get("summarizer")), str(r.get("judge")))
    ):
        score = row.get("jury_score")
        if score is None:
            score = row.get("quality_score")
        reasoning = str(row.get("reasoning") or "").strip() or "Not recorded"
        lines.extend([
            "",
            "=" * 78,
            f"SUMMARIZER: {row.get('summarizer') or '?'}   JUDGE: {row.get('judge') or '?'}",
            "=" * 78,
            f"Score: {score}",
            f"Quality Score (1-10 alias): {row.get('quality_score')}",
            f"Hallucination Count: {row.get('hallucination_count')}",
            f"Requires Human Review: {'Yes' if row.get('requires_human_review') else 'No'}",
            f"Parse Method: {row.get('parse_method') or 'Not recorded'}",
            f"Judge Model Version: {row.get('judge_model_version') or 'Not recorded'}",
            "",
            "Reasoning:",
            reasoning,
        ])

    return "\n".join(lines).rstrip() + "\n"


def write_dev_eval_jsonl_outputs(
    dois: set[str],
    *,
    output_dir: Path,
    evaluations_path: Path | None = None,
    manifest_index: dict[str, dict] | None = None,
) -> int:
    """Render a readable ``.txt`` file per DOI into ``data/dev_evals_jsonl/``.

    IN PLAIN ENGLISH: For each requested paper, gathers its judge rows, turns
    them into readable text (using the function above), and saves one .txt
    file per paper into data/dev_evals_jsonl/. Re-running this overwrites
    each paper's file in place rather than piling up duplicate files.

    The eval-side sibling of ``summarizer.write_dev_summary_jsonl_outputs``: it
    reads the judge rows ``run_evaluation`` just appended to
    ``data/evaluations.jsonl`` for the given ``dois`` and writes one file per
    paper. It never modifies ``evaluations.jsonl`` (that file stays append-only,
    the single source of truth — see CLAUDE.md's Phase 3 rules).

    One file per DOI, keyed by the DOI slug so a re-judge overwrites in place
    rather than accumulating timestamped duplicates. Returns the number of files
    written. ``manifest_index`` (doi -> manifest row, for the Title line) is
    loaded from data/manifest.jsonl if not supplied.
    """
    if manifest_index is None:
        manifest_index = load_manifest_index()

    rows_by_doi = _rows_by_doi_for(dois, evaluations_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for doi, rows in rows_by_doi.items():
        out_path = output_dir / f"{doi_to_slug(doi)}.txt"
        # Write to a temporary ".tmp" file first, then rename it into place
        # (`.replace`). This "write-then-rename" trick means the real output
        # file is never left half-written if something goes wrong partway
        # through — the old file stays intact until the new one is fully
        # ready to take its place.
        tmp_path = out_path.with_suffix(".txt.tmp")
        tmp_path.write_text(
            _format_dev_eval_entry_as_text(doi, rows, manifest_index=manifest_index),
            encoding="utf-8",
        )
        tmp_path.replace(out_path)
        written += 1
    return written


def _format_dev_detail_eval_entry_as_markdown(
    doi: str, rows: list[dict], *, manifest_index: dict[str, dict] | None = None,
) -> str:
    """Render one paper's judge rows into a detailed, skimmable Markdown report.

    IN PLAIN ENGLISH: Like ``_format_dev_eval_entry_as_text`` above, but much
    more detailed — this is the "deep dive" version. Instead of only showing
    the final aggregate score, it shows each individual judge's score AND
    written reasoning for every one of the five criteria, the automatic text
    metrics, any hallucination claims in detail, and how much the judges
    agreed or disagreed with each other. Written in Markdown (a simple
    text formatting style using # for headings and | for tables) so it reads
    nicely both as plain text and if opened in a Markdown viewer.

    The deep-dive sibling of ``_format_dev_eval_entry_as_text``: shows every
    judge's own per-criterion score AND reasoning (not just the aggregate),
    automatic text metrics, and cross-judge agreement stats, for
    ``data/dev_detailEval_reports/``. Visual style mirrors
    ``eval_report._render_markdown_detail`` (title heading, DOI link, per-
    criterion bullets, hallucination-claim phrasing), but shows every judge
    individually instead of collapsing to one -- that per-judge view is
    exactly what the corpus-level report defers to its separate reliability
    section, and is the reason this sibling report exists.
    """
    record = (manifest_index or {}).get(doi) or {}
    title = _lookup_title(doi, manifest_index)
    heading = title if title else f"Untitled -- {doi or 'no DOI'}"

    strata = rows[0].get("strata") if rows and isinstance(rows[0].get("strata"), dict) else {}
    journal = strata.get("journal") or record.get("journal")
    species = strata.get("species") or record.get("species")
    species_str = ", ".join(species) if isinstance(species, list) else species
    descriptors = [
        str(d) for d in (
            species_str,
            strata.get("study_design") or record.get("study_design"),
            strata.get("clinical_topic") or record.get("clinical_topic"),
        ) if d
    ]

    lines = [
        f"# {heading}",
        "",
        f"**DOI:** [{doi}](https://doi.org/{doi})" if doi else "**DOI:** unknown",
    ]
    if journal:
        lines.append(f"**Journal:** {journal}")
    if descriptors:
        lines.append(f"**Species / Topic / Study design:** {' · '.join(descriptors)}")
    lines.extend([
        "",
        "> **How to read this file:** each provider's summary is scored 1-5 on five "
        "criteria -- **Factual Accuracy** (is it supported by the article?), "
        "**Completeness** (does it cover the article's key points?), **Practical "
        "Usefulness** (would a clinician find it useful?), **Clarity** (is it easy to "
        "read?), and **Safety** (could it mislead a clinician?). \"Spread\" below is the "
        "highest judge score minus the lowest for that item -- 0 means every judge "
        "agreed exactly.",
        "",
    ])

    # A "set" (the curly-brace `{...}` comprehension below, like a dict but
    # with no values, just keys) automatically drops duplicates. This builds
    # the list of distinct summariser names that have any rows for this
    # paper, sorted alphabetically, then loops over each one to build its own
    # section of the report (one "## summariser-name" heading per provider).
    summarizers = sorted({str(r.get("summarizer") or "?") for r in rows})
    for summarizer in summarizers:
        # Keep only this summariser's rows (there's one row per judge that
        # scored it) to build this section.
        srows = [r for r in rows if str(r.get("summarizer") or "?") == summarizer]
        lines.append(f"## {summarizer}")
        lines.append("")

        # Automatic metrics (word overlap, coverage, etc.) are computed once
        # per summary and are identical across judges, so it's safe to just
        # read them off the first row.
        metrics = srows[0].get("automatic_metrics") or {}
        if metrics:
            section_coverage = metrics.get("section_coverage") or {}
            lines.extend([
                "**Automatic metrics** (computed once per summary; same for every judge)",
                "",
                "| Metric | Value |",
                "|---|---|",
                f"| Compression ratio | {metrics.get('compression_ratio', '-')} |",
                f"| Extractive coverage | {metrics.get('extractive_coverage', '-')} |",
                f"| Section coverage | {section_coverage.get('covered_count', '-')} of 6 "
                f"({section_coverage.get('coverage_ratio', '-')}) |",
                f"| ROUGE-1 / ROUGE-2 / ROUGE-L | {metrics.get('rouge_1', '-')} / "
                f"{metrics.get('rouge_2', '-')} / {metrics.get('rouge_l', '-')} |",
                "",
            ])

        agg = aggregate_jury_scores(srows)
        lines.extend([
            "**Cross-judge agreement** (this paper only; see `eval-report` for the "
            "corpus-wide version)",
            "",
            "| | Mean | Spread | Judges |",
            "|---|---|---|---|",
            f"| Jury score (unweighted) | {agg.get('jury_score_unweighted_mean', '-')} | "
            f"{agg.get('judge_disagreement_unweighted', '-')} | {agg.get('valid_judge_count', '-')} |",
            f"| Jury score (weighted) | {agg.get('jury_score_weighted_mean', '-')} | "
            f"{agg.get('judge_disagreement_weighted', '-')} | {agg.get('valid_judge_count', '-')} |",
        ])
        # For each of the five criteria, gather every judge's score for it
        # (safely skipping rows with missing/malformed data) and show the
        # mean and the spread, so a reader can see at a glance which
        # criterion the judges disagreed on most for this summary.
        for criterion, label in CRITERION_LABELS.items():
            values = [
                r["criteria_scores"][criterion]["score"]
                for r in srows
                if isinstance(r.get("criteria_scores"), dict)
                and isinstance(r["criteria_scores"].get(criterion), dict)
                and isinstance(r["criteria_scores"][criterion].get("score"), (int, float))
            ]
            if len(values) < 2:
                continue  # spread is meaningless with fewer than 2 judges
            mean, spread = _mean_and_spread(values)
            lines.append(f"| {label} | {mean} | {spread} | {len(values)} |")
        lines.append("")

        # Now show each individual judge's own full breakdown — one
        # "### Judge: name" subsection per judge, with a table of that
        # judge's own score and written reasoning for all five criteria.
        for row in sorted(srows, key=lambda r: str(r.get("judge") or "")):
            judge = row.get("judge") or "?"
            lines.append(f"### Judge: {judge}")
            lines.append("")

            criteria_scores = row.get("criteria_scores") or {}
            lines.append("| Criterion | Score | Reasoning |")
            lines.append("|---|---|---|")
            for criterion, label in CRITERION_LABELS.items():
                detail = criteria_scores.get(criterion)
                if isinstance(detail, dict):
                    reasoning = str(detail.get("reasoning") or "").strip() or "Not recorded"
                    lines.append(f"| {label} | {detail.get('score', '-')}/5 | {reasoning} |")
                else:
                    lines.append(f"| {label} | - | Not recorded (legacy schema) |")
            lines.append("")

            overall_bits = [
                f"weighted {row.get('jury_score_weighted', '-')}",
                f"unweighted {row.get('jury_score_unweighted', '-')}",
            ]
            confidence = row.get("confidence_score")
            if confidence is not None:
                overall_bits.append(f"confidence {confidence}/5")
            lines.append("**Overall score:** " + " / ".join(overall_bits))
            if row.get("requires_human_review"):
                lines.append("")
                lines.append("**Flagged for human review.**")
            lines.append("")

            # List each hallucination claim this judge flagged (if any),
            # showing exactly what the summary claimed versus what the
            # source article actually supports.
            claims = [c for c in (row.get("hallucination_claims") or []) if isinstance(c, dict)]
            if claims:
                lines.append("**Hallucination claims:**")
                for claim in claims:
                    lines.append(
                        f"- ({claim.get('severity', 'unknown')}) claimed "
                        f"\"{claim.get('claim', '')}\" -- article only supports: "
                        f"\"{claim.get('source_quote', '')}\""
                    )
            else:
                lines.append("**Hallucination claims:** None")
            lines.append("")

            reasoning = str(row.get("reasoning") or "").strip() or "Not recorded"
            lines.append(f"> {reasoning}")
            lines.append("")

    lines.extend([
        "---",
        "",
        "> Corpus-wide significance tests (Wilcoxon/Friedman) and reliability "
        "(Krippendorff's alpha) live in `eval-report`, `eval-report --publication`, "
        "and `stats-engine` -- this file only covers this one paper. The full "
        "candidate summary text is in `data/dev_summaries_jsonl/`.",
    ])

    return "\n".join(lines).rstrip() + "\n"


def write_dev_detail_eval_outputs(
    dois: set[str],
    *,
    output_dir: Path,
    evaluations_path: Path | None = None,
    manifest_index: dict[str, dict] | None = None,
) -> int:
    """Render a detailed Markdown file per DOI into ``data/dev_detailEval_reports/``.

    IN PLAIN ENGLISH: Same save-to-disk pattern as
    ``write_dev_eval_jsonl_outputs`` above (read this paper's rows, build the
    text, write to a .tmp file, then rename into place so a partial write
    never corrupts the real file) but writes the detailed Markdown report
    instead of the shorter plain-text one.

    The deep-dive sibling of ``write_dev_eval_jsonl_outputs``: same DOI-grouped
    read of ``data/evaluations.jsonl`` (via ``_rows_by_doi_for``) and the same
    atomic write-in-place-by-slug pattern, but renders every judge's full
    per-criterion breakdown via ``_format_dev_detail_eval_entry_as_markdown``
    instead of the aggregate-only plain-text mirror. Returns the number of
    files written.
    """
    if manifest_index is None:
        manifest_index = load_manifest_index()

    rows_by_doi = _rows_by_doi_for(dois, evaluations_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for doi, rows in rows_by_doi.items():
        out_path = output_dir / f"{doi_to_slug(doi)}.md"
        tmp_path = out_path.with_suffix(".md.tmp")
        tmp_path.write_text(
            _format_dev_detail_eval_entry_as_markdown(doi, rows, manifest_index=manifest_index),
            encoding="utf-8",
        )
        tmp_path.replace(out_path)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

# IN PLAIN ENGLISH: Reads a JSONL file (one JSON object per line — "JSONL"
# means "JSON Lines") one line at a time and hands back each parsed row.
# `yield` (instead of `return`) makes this a "generator": rather than reading
# the whole file into memory and returning one big list, it produces rows one
# at a time as the caller asks for them — more memory-efficient for a
# potentially large file, and it can be used in a normal `for row in ...`
# loop just like a list. Blank lines and lines that aren't valid JSON are
# silently skipped rather than crashing the whole read.
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
    IN PLAIN ENGLISH: Finds and loads the cleaned, pre-processed full text of
    one paper (the "reference text" the judge compares the summary against),
    from wherever it was cached on disk earlier in the pipeline.

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
                      input_source: str = "processed",
                      rubric_version: str = RUBRIC_VERSION) -> bool:
    """Check evaluations.jsonl to support a manual resume.

    IN PLAIN ENGLISH: Before spending money to have a judge re-grade
    something, this checks whether that exact combination (this paper, this
    summariser, this judge, this rubric version) has already been graded and
    saved. If it's a "resume" run, matching rows get skipped instead of
    re-judged. Answers a plain True/False question — "have we already done
    this one?"

    Rubric version is part of the resume key so a new methodology can be run
    next to older scores without silently skipping already-seen DOI/model pairs.
    """
    if not EVALUATIONS_PATH.exists():
        return False
    # Scan every existing row looking for an exact match on all five fields.
    # This is a simple linear search, acceptable here because it only runs
    # once per (paper, judge) pair being considered, not for every row ever.
    with open(EVALUATIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A mock row is not evidence that this pair was ever judged.
            # Before this check existed, a dry-run row satisfied the resume
            # key exactly (same doi/summarizer/judge/rubric_version) and made
            # a real, unjudged paper look complete forever.
            if is_dry_run_row(row):
                continue
            if (row.get("doi") == doi
                    and (row.get("input_source") or "processed") == input_source
                    and row.get("summarizer") == summariser
                    and row.get("judge") == judge
                    and row.get("rubric_version") == rubric_version):
                return True
    return False


def confirm_real_judge(profile: ModeProfile, force: bool) -> bool:
    """
    IN PLAIN ENGLISH: This is the safety checkpoint that stands between the
    code and a real, money-spending API call. If the current mode is
    dry-run/test, it just says "go ahead" automatically (True) since nothing
    real will be spent. Otherwise it either logs that --force was used to
    skip the prompt, or actually stops and asks the person running the
    script to type "yes" before continuing — a real human confirmation, not
    something this code (or Claude) can answer on someone's behalf.

    Mirror summarizer's confirmation: only triggers when the profile both
    requires confirmation AND is not short-circuited to mocks.
    """
    if profile.dry_run or not profile.requires_confirm:
        return True
    if force:
        # --force skips the interactive prompt, but the bypass is written to
        # a permanent log file so there's still a record that it happened.
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = (f"[phase3:safety] evaluator confirmation bypassed via --force at "
                f"{datetime.now(timezone.utc).isoformat()}\n")
        with open(LOGS_DIR / "phase3_safety.log", "a", encoding="utf-8") as f:
            f.write(line)
        print(line.strip())
        return True
    # `input(...)` pauses the program and waits for the person at the
    # keyboard to type something and press Enter. Only the exact word "yes"
    # counts as confirmation; anything else (including just pressing Enter)
    # cancels the run.
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
                   doi_filter: set[str] | None = None,
                   instances: Iterable[EvaluationInstance] | None = None) -> dict[str, int]:
    """Run the blind judge loop over a set of evaluation instances.

    IN PLAIN ENGLISH: This is the main engine that actually runs an
    evaluation pass. For every paper-summary pair, and every judge in the
    judge list, it: skips ones already graded (if resuming), calls the judge,
    turns the response into a clean row, saves that row, and keeps a running
    tally of how many succeeded/failed/were skipped and how much money was
    spent. Everything above in this file (parsing, scoring, prompt-building)
    exists to support this one loop.

    doi_filter: when provided, only evaluate DOIs in this set. Used by
    run_phase3.py's journal-stratified sampling to pass a pre-selected
    subset without changing evaluator logic.

    instances: when provided, judge exactly these instances instead of
    building them from summaries.jsonl (paper_limit/doi_filter are ignored in
    that case — the caller already resolved which instances to include). This
    is how run_phase3.py's alternate EVAL_INPUT_MODE feeds evaluate() from
    summarize-all's .txt comparison files without changing anything about how
    judging itself works — the judge loop below is identical either way.
    """
    judges = judges or JUDGE_MODELS
    prompt_template = load_judge_prompt()
    # BudgetGuard (from utils.py) tracks running spend across this whole run
    # and can stop things if a spending limit is exceeded — the project-wide
    # safety net for cost control.
    guard = BudgetGuard()
    counts = {"evaluated": 0, "skipped": 0, "failed": 0, "no_text": 0}

    # Either use the caller-supplied list of instances directly, or build the
    # list of (paper, summariser) pairs to judge from summaries.jsonl.
    instance_iter = instances if instances is not None else iter_evaluation_instances(
        summaries_path=SUMMARIES_PATH,
        doi_filter=doi_filter,
        paper_limit=paper_limit,
    )

    # Outer loop: one pass per (paper, summariser) pair to judge.
    for instance in instance_iter:
        # Automatic metrics are computed once per paper-summary pair and then
        # attached to every judge row. This avoids recomputing deterministic
        # text overlap metrics when reliability mode uses multiple judges.
        automatic_metrics = calculate_automatic_metrics(
            instance.reference_text,
            instance.candidate_summary,
            reference_summary=instance.manifest_record.get("abstract"),
        )

        # Inner loop: for this one paper-summary pair, ask every judge in the
        # active jury to grade it (so with the 3-judge panel, each summary
        # gets graded three separate times, once per judge).
        for judge in judges:
            # Resume support: if this exact (paper, summariser, judge, rubric)
            # combination is already in evaluations.jsonl, skip it instead of
            # paying to re-judge it.
            if resume and already_evaluated(
                instance.doi, instance.summarizer, judge, instance.input_source,
            ):
                counts["skipped"] += 1
                continue
            sleep_for_model(judge)  # be polite to the provider's rate limits
            try:
                response = call_judge(
                    judge, instance.reference_text, instance.candidate_summary,
                    prompt_template=prompt_template,
                )
            except Exception as exc:
                # A failed judge call for one pair should not stop the whole
                # run — log it, count it as a failure, and move on to the
                # next judge/pair.
                counts["failed"] += 1
                log_error(instance.doi, f"evaluate_{judge}", str(exc))
                continue

            row = build_evaluation_row(
                doi=instance.doi, summariser=instance.summarizer, judge=judge,
                input_source=instance.input_source,
                reference_text=instance.reference_text,
                candidate_summary=instance.candidate_summary,
                judge_response=response,
                strata=instance.strata,
                automatic_metrics=automatic_metrics,
            )
            append_evaluation(row)
            counts["evaluated"] += 1
            # Work out how much this one judge call cost (based on how many
            # tokens/word-pieces it used) and add it to the running budget
            # total, so the user can see spend accumulate as the run goes.
            cost = compute_cost(
                judge,
                int(response["input_tokens"] or 0),
                int(response["output_tokens"] or 0),
                batched=False,
            )
            guard.add_cost(cost)
            score = row.get("jury_score") or row.get("quality_score")
            print(f"[phase3:evaluate] {instance.doi} {instance.summarizer} -> {judge} "
                  f"score={score} via {row['parse_method']} "
                  f"(${cost:.4f})")

    # Final progress summary, printed once the whole run finishes.
    print(f"\n[phase3:evaluate] done. counts={counts}  "
          f"budget_spent=${guard.total_spent:.4f}")
    return counts


# IN PLAIN ENGLISH: This is what actually runs when someone types
# `python evaluator.py ...` at the command line — it's the "CLI" (command
# line interface) entry point. It reads whatever flags/options were typed
# in (like --mode or --judges), works out the resulting settings, runs the
# safety confirmation, and then kicks off run_evaluation() above. It returns
# 0 for "everything ran" and 1 for "stopped, e.g. confirmation declined" —
# the standard success/failure convention for command-line programs.
def main(argv: list[str] | None = None) -> int:
    # argparse is Python's standard toolkit for reading command-line flags
    # (like --mode dev or --limit 5) and turning them into ordinary Python
    # values. Each `.add_argument(...)` call below defines one flag: its
    # name, allowed values, default, and the help text shown for --help.
    parser = argparse.ArgumentParser(description="Phase 3 — blind judge evaluator.")
    parser.add_argument("--mode", choices=VALID_MODES, default=None,
                        help="Override PHASE3_MODE from .env: test|single|dev|batch.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override the mode's default paper_limit.")
    parser.add_argument("--judges", default=None,
                        help="Comma-separated judge provider keys. Overrides --jury "
                             "and JURY_PRESET. Default: JUDGE_MODELS from .env "
                             f"({','.join(JUDGE_MODELS)}).")
    parser.add_argument("--jury", action="store_true",
                        help="Convenience switch for the full 3-judge panel "
                             f"({','.join(JURY_PANEL)}); same as JURY_PRESET=panel.")
    parser.add_argument("--no-resume", action="store_true",
                        help="Re-evaluate even pairs already in evaluations.jsonl.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass interactive confirmation. USE WITH CAUTION.")
    args = parser.parse_args(argv)

    # Work out which judges to use and which mode profile (test/single/dev/
    # batch) governs this run, combining CLI flags with .env settings per the
    # precedence rules documented on resolve_judges() and resolve_mode().
    judges = resolve_judges(args.judges, jury=args.jury)

    profile = resolve_mode(args.mode)
    paper_limit = args.limit if args.limit is not None else profile.paper_limit

    print(profile.banner())
    if profile.use_batch:
        print("[phase3:evaluate] mode=batch: this script only runs the real-time judge "
              "loop. Judge batch jobs are submitted by run_phase3 summarize and collected "
              "by check_batch_status.py.")
    if args.limit is not None:
        print(f"[phase3:evaluate] --limit {args.limit} overrides mode default.")

    # Safety checkpoint (see confirm_real_judge above) — stops here if this
    # would be a real, paid run and the person didn't confirm it.
    if not confirm_real_judge(profile, force=args.force):
        return 1
    # A second safety check: refuses to proceed with a real (non-dry-run) call
    # if the configured budget is zero or negative, catching a misconfigured
    # .env before any money is spent.
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


# This only runs if evaluator.py is executed directly as a script (e.g.
# `python evaluator.py --mode dev`), not if it's imported by another file
# (like the test file does). `sys.exit(...)` reports main()'s return value
# (0 or 1) back to the operating system as the program's exit code.
if __name__ == "__main__":
    sys.exit(main())
