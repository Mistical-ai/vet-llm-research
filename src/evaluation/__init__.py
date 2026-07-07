"""
src/evaluation/ — Auxiliary offline scoring helpers
====================================================

WHY THIS PACKAGE EXISTS
-----------------------
Phase 3's blind MedHELM judge (``llm-sum/evaluator.py``) is the authoritative
study endpoint: live LLM calls, append-only ``data/evaluations.jsonl``, and a
separate rubric version (``vet_medhelm_score_v1.0``).

This package holds **deterministic, zero-cost** checks for local debugging:
  - ``rubric_scoring.py`` — heuristic scores from ``docs/rubrics/rubric_v1.yaml``
  - output: ``data/rubric_scores.jsonl`` (never mixed with evaluations)

Keeping the two rubric tracks separate avoids silently replacing jury scores
with keyword heuristics.  Use ``python pipeline.py --use-rubric`` only when you
want a quick sanity check before paying for judges.
"""

from evaluation.rubric_scoring import (
    DIMENSIONS,
    build_rubric_score_rows,
    load_rubric,
    score_output,
    validate_rubric,
    write_rubric_scores,
)

__all__ = [
    "DIMENSIONS",
    "build_rubric_score_rows",
    "load_rubric",
    "score_output",
    "validate_rubric",
    "write_rubric_scores",
]
