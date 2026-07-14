"""
llm-sum/human_review.py — Human validation: stratified sampling + blind export
================================================================================

WHY THIS MODULE EXISTS
-----------------------
The blind LLM jury in ``evaluator.py`` is the study's authoritative score, but
a jury score with no check against expert judgment is hard to defend in a
methods section. This module samples already-judged (paper, summariser) pairs
from ``data/evaluations.jsonl`` and exports them as blind review packets a
veterinarian can score by hand — one or several independent reviewers, no
code required on their end.

THE BLIND PROTOCOL APPLIES HERE TOO
------------------------------------
Exactly like ``evaluator.build_judge_prompt()``, the reviewer-facing files
this module writes carry only ``item_id`` + the original article text + the
candidate summary — never the summariser name, the judge's score, or any
other clue to identity. The mapping from ``item_id`` back to
(doi, summariser, judge, LLM scores) lives in a single un-blinding key file
that is never handed to a reviewer (see ``build_unblinding_key``).

INGEST + ANALYSIS (Phase 5, Chunk 6)
------------------------------------
Once reviewers return their filled scoresheets, ``ingest_human_reviews()``
re-joins each ``item_id`` to the private un-blinding key, normalizes the rows
into ``data/human_reviews.jsonl``, and ``analyze_human_reviews()`` answers the
two questions that make the LLM jury defensible:

- *Do the human reviewers agree with each other?* (inter-reviewer
  Krippendorff's alpha — reuses ``reliability.compute_reliability``, treating
  each reviewer as a "rater" exactly as it treats each judge; skipped for a
  lone reviewer, who has no one to agree with.)
- *Does the LLM jury track expert judgment?* (human-vs-jury Pearson +
  Spearman correlation and a Bland-Altman bias, overall and per-criterion —
  reported even for the 1-reviewer case.)

DESIGN CONSTRAINTS (from CLAUDE.md)
-------------------------------------
- Offline only: reads ``data/evaluations.jsonl`` + already-cached summary
  text; never calls a paid API.
- Dependency-light: only the standard library directly (csv, json, random);
  correlation statistics come via reliability.py (which uses scipy.stats).
- Reuses existing joins (``eval_instances.iter_evaluation_instances``,
  ``summarize_all_ingest.iter_summarize_all_instances``,
  ``eval_report.iter_evaluation_rows``) instead of re-deriving them.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import csv
import io
import json
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from evaluator import (  # noqa: E402  (single source of truth for blind tokens + weights)
    BLIND_FORBIDDEN_TOKENS,
    MEDHELM_CRITERION_WEIGHTS,
    calculate_jury_score,
)
from eval_instances import EvaluationInstance, STRATIFICATION_FIELDS, iter_evaluation_instances  # noqa: E402
from summarize_all_ingest import iter_summarize_all_instances  # noqa: E402
from eval_report import iter_evaluation_rows  # noqa: E402
import reliability  # noqa: E402  (agreement + correlation stats, reused for humans)
from stats_engine import categorical_agreement  # noqa: E402  (Cohen's Kappa + percent agreement)

EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"
DEV_TESTS_SUMMARIES_TXT_DIR = DATA_DIR / "dev_tests" / "summaries_txt"
HUMAN_REVIEW_DIR = DATA_DIR / "human_review"
# The zero-jargon, standalone reviewer guide (docs/booklet chapter 7). This is
# the single source of truth for reviewer-facing instructions; export copies
# it verbatim into every reviewer folder (see render_reviewer_guide_markdown)
# so a reviewer handed only their reviewer_N/ folder still gets the full guide.
REVIEWER_GUIDE_PATH = REPO_ROOT / "docs" / "booklet" / "07_human_validation_guide.md"
# The normalized ingest output. A derived snapshot (rewritten idempotently on
# every ingest), NOT append-only like evaluations.jsonl — re-ingesting the same
# filled sheets must reproduce the same file, never grow it.
HUMAN_REVIEWS_PATH = DATA_DIR / "human_reviews.jsonl"
UNBLINDING_KEY_NAME = "unblinding_key.json"

# Mirrors evaluator.MEDHELM_CRITERION_WEIGHTS's keys — the same five criteria a
# human reviewer scores, kept as a local tuple so this module has no import-time
# dependency on the evaluator's runtime weight configuration (same rationale as
# reliability.CRITERIA).
CRITERIA = ("faithfulness", "completeness", "clinical_usefulness", "clarity", "safety")
SCORESHEET_FIELDS = ["item_id", *CRITERIA, "hallucination_present", "hallucination_notes", "comment"]


# ---------------------------------------------------------------------------
# Row identity + dedupe
# ---------------------------------------------------------------------------

def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Identity of the thing being reviewed: one (doi, summarizer, input_source).

    Matches eval_instances/reliability's join key, so a sampled evaluation row
    can be matched back to the EvaluationInstance that carries its full text.
    """
    return (
        str(row.get("doi", "")).strip(),
        str(row.get("summarizer", "")).strip(),
        str(row.get("input_source") or (row.get("strata") or {}).get("input_source") or "processed"),
    )


def _dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one evaluation row per (doi, summarizer, input_source): the latest.

    A multi-judge jury run or a --no-resume rerun can leave more than one row
    for the same item; the reviewer should see one summary per item, so we
    take the most recently written row (ISO-8601 timestamps sort correctly as
    plain strings).
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row)
        if not key[0] or not key[1]:
            continue
        existing = latest.get(key)
        if existing is None or str(row.get("timestamp") or "") >= str(existing.get("timestamp") or ""):
            latest[key] = row
    return list(latest.values())


# ---------------------------------------------------------------------------
# Stratified sampler
# ---------------------------------------------------------------------------

def _strata_group_key(row: dict[str, Any]) -> tuple[str, ...]:
    """A printable composite key across STRATIFICATION_FIELDS for grouping."""
    strata = row.get("strata") or {}

    def norm(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else "unknown"
        return str(value or "unknown")

    return tuple(norm(strata.get(field, "unknown")) for field in STRATIFICATION_FIELDS)


def _select_units_stratified(
    units: list[Any], *, sample_size: int, rng: random.Random,
    flagged_fn: Any, strata_key_fn: Any, sort_key_fn: Any,
) -> list[Any]:
    """Flagged-first, then stratified round-robin selection over any unit type.

    Shared by ``sample_rows_for_review``'s two ``sample_unit`` modes: a
    "unit" is one row in "items" mode, or one article's whole list of rows in
    "articles" mode. ``flagged_fn``/``strata_key_fn``/``sort_key_fn`` let the
    caller define what "flagged" and "strata" mean for its unit type, while
    the flagged-first-then-stratified-round-robin selection algorithm itself
    (see module-level sampler docs) stays in one place.

    1. Every unit ``flagged_fn`` marks True is included first
       (deterministically shuffled and capped at ``sample_size`` if there are
       more flagged units than the requested sample).
    2. Remaining slots are filled round-robin across ``strata_key_fn``
       groups, so the sample spans subgroups instead of being dominated by
       whichever group happens to have the most units.
    """
    flagged = sorted((u for u in units if flagged_fn(u)), key=sort_key_fn)
    remainder = sorted((u for u in units if not flagged_fn(u)), key=sort_key_fn)
    rng.shuffle(flagged)
    rng.shuffle(remainder)

    selected: list[Any] = flagged[:sample_size]
    remaining_slots = sample_size - len(selected)
    if remaining_slots <= 0:
        return selected

    groups: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    for unit in remainder:
        groups[strata_key_fn(unit)].append(unit)
    group_keys = sorted(groups)
    rng.shuffle(group_keys)

    pool: list[Any] = []
    idx = 0
    progressed = True
    while progressed and len(pool) < remaining_slots:
        progressed = False
        for group_key in group_keys:
            bucket = groups[group_key]
            if idx < len(bucket):
                pool.append(bucket[idx])
                progressed = True
                if len(pool) >= remaining_slots:
                    break
        idx += 1

    selected.extend(pool)
    return selected


def _interleave_article_groups(
    groups: list[list[dict[str, Any]]], *, rng: random.Random,
) -> list[dict[str, Any]]:
    """Round-robin interleave each selected article's rows across the sample.

    Used only by ``sample_unit="articles"``: without this, all of one
    article's provider rows would sit consecutively in the sampled order (and
    therefore consecutively in the rendered packet), which is exactly the
    back-to-back comparison the blind protocol avoids (see "Why sampling can
    reuse the same article across multiple items" in
    docs/phase5/human_validation.md). Shuffling the article order, then
    taking one row per article per round, guarantees two rows sharing a
    ``doi`` are never adjacent whenever two or more articles are selected —
    with a single selected article there is nothing to interleave with, so
    its rows are necessarily consecutive.
    """
    order = list(range(len(groups)))
    rng.shuffle(order)
    shuffled_groups = [groups[i] for i in order]

    interleaved: list[dict[str, Any]] = []
    idx = 0
    progressed = True
    while progressed:
        progressed = False
        for group in shuffled_groups:
            if idx < len(group):
                interleaved.append(group[idx])
                progressed = True
        idx += 1
    return interleaved


def sample_rows_for_review(
    rows: Iterable[dict[str, Any]], *, sample_size: int, seed: int,
    sample_unit: str = "items",
) -> list[dict[str, Any]]:
    """Stratified sample of evaluation rows, preferring flagged-for-review rows.

    ``sample_unit`` controls what ``sample_size`` counts:

    - ``"items"`` (default) — one row (one article + one provider's summary)
      per count. Every deduped row with ``requires_human_review=True`` is
      included first; remaining slots are filled round-robin across the
      existing strata groups (species / study_design / clinical_topic /
      journal / input_source, see ``eval_instances.STRATIFICATION_FIELDS``),
      so the sample spans subgroups instead of being dominated by whichever
      journal/strata combination happens to have the most rows.
    - ``"articles"`` — one distinct article (``doi``) per count, but EVERY
      provider row found for that article is included, so a reviewer reads
      each sampled article once and scores every provider's summary of it
      (e.g. 5 articles x 3 providers = 15 scored items from 5 articles
      actually read). An article is "flagged" if any of its rows is; the
      same stratified round-robin selection runs at article granularity.
      Sibling rows (same ``doi``) are round-robin interleaved with other
      selected articles' rows in the returned order so they are never
      adjacent in the rendered packet, preserving independent, non-
      comparative scoring — see "Why sampling can reuse the same article
      across multiple items" in docs/phase5/human_validation.md.

    Deterministic for a given ``seed`` so re-running the export with the same
    seed reproduces the same sample (useful when a reviewer sheet is lost and
    needs regenerating).
    """
    if sample_unit not in ("items", "articles"):
        raise ValueError(f"sample_unit must be 'items' or 'articles', got {sample_unit!r}")

    deduped = _dedupe_rows(rows)
    if sample_size <= 0 or not deduped:
        return []

    rng = random.Random(seed)

    if sample_unit == "items":
        return _select_units_stratified(
            deduped, sample_size=sample_size, rng=rng,
            flagged_fn=lambda r: bool(r.get("requires_human_review")),
            strata_key_fn=_strata_group_key,
            sort_key_fn=_row_key,
        )

    # sample_unit == "articles"
    by_doi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in deduped:
        doi = str(row.get("doi", "")).strip()
        if doi:
            by_doi[doi].append(row)
    article_groups = [sorted(rows_, key=_row_key) for rows_ in by_doi.values()]

    selected_groups = _select_units_stratified(
        article_groups, sample_size=sample_size, rng=rng,
        flagged_fn=lambda g: any(r.get("requires_human_review") for r in g),
        strata_key_fn=lambda g: _strata_group_key(g[0]),
        sort_key_fn=lambda g: _row_key(g[0]),
    )
    return _interleave_article_groups(selected_groups, rng=rng)


# ---------------------------------------------------------------------------
# Join sampled rows to their source text
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewItem:
    """One blind review unit: an anonymized id plus everything a reviewer needs.

    ``summarizer``/``judge``/LLM score fields are carried on this object only
    so ``build_unblinding_key()`` can record them — the rendering functions
    below (``render_packet_markdown``, ``render_scoresheet_csv``) never read
    those fields, which is the structural guarantee of the blind protocol.
    """

    item_id: str
    doi: str
    summarizer: str
    judge: str
    rubric_version: str
    input_source: str
    reference_text: str
    candidate_summary: str
    strata: dict[str, Any]
    requires_human_review: bool
    llm_jury_score: float | None
    llm_jury_score_weighted: float | None
    llm_jury_score_unweighted: float | None
    llm_criteria_scores: dict[str, Any]


def _build_instance_lookup(
    summaries_path: Path,
    summaries_txt_dir: Path,
    dev_tests_summaries_txt_dir: Path,
) -> dict[tuple[str, str, str], EvaluationInstance]:
    """Combine both text sources evaluate() can score from into one lookup.

    Mirrors run_phase3.py's EVAL_INPUT_MODE duality (jsonl vs. summarize-all's
    .txt folders): a sampled evaluations.jsonl row may have come from either
    path, so both are tried, jsonl-sourced instances taking precedence
    (``setdefault``) since that is the original, still-default pipeline.
    """
    lookup: dict[tuple[str, str, str], EvaluationInstance] = {}
    for instance in iter_evaluation_instances(summaries_path=summaries_path):
        lookup.setdefault((instance.doi, instance.summarizer, instance.input_source), instance)
    for folder in (summaries_txt_dir, dev_tests_summaries_txt_dir):
        for instance in iter_summarize_all_instances(folder):
            lookup.setdefault((instance.doi, instance.summarizer, instance.input_source), instance)
    return lookup


def _build_review_items(
    sampled_rows: list[dict[str, Any]],
    lookup: dict[tuple[str, str, str], EvaluationInstance],
) -> tuple[list[ReviewItem], list[dict[str, Any]]]:
    """Join sampled evaluation rows to their reference/candidate text.

    Returns (items, skipped_rows). A row is skipped when its
    (doi, summarizer, input_source) can't be matched to a live instance —
    e.g. data/summaries.jsonl or the summarize-all .txt folders have since
    been moved, regenerated, or pruned. Skipping (rather than raising) keeps
    export usable even when the corpus has drifted since evaluation; the
    caller prints a warning with the count so nothing is silently lost.
    """
    items: list[ReviewItem] = []
    skipped: list[dict[str, Any]] = []
    for n, row in enumerate(sampled_rows, start=1):
        key = _row_key(row)
        instance = lookup.get(key)
        if instance is None:
            skipped.append(row)
            continue
        items.append(ReviewItem(
            item_id=f"item_{n:03d}",
            doi=key[0],
            summarizer=key[1],
            judge=str(row.get("judge", "")),
            rubric_version=str(row.get("rubric_version", "")),
            input_source=key[2],
            reference_text=instance.reference_text,
            candidate_summary=instance.candidate_summary,
            strata=row.get("strata") or {},
            requires_human_review=bool(row.get("requires_human_review")),
            llm_jury_score=row.get("jury_score"),
            llm_jury_score_weighted=row.get("jury_score_weighted"),
            llm_jury_score_unweighted=row.get("jury_score_unweighted"),
            llm_criteria_scores=row.get("criteria_scores") or {},
        ))
    return items, skipped


# ---------------------------------------------------------------------------
# Blind rendering (packet + scoresheet) — no summariser/judge identity
# ---------------------------------------------------------------------------

def render_packet_markdown(items: list[ReviewItem], *, reviewer_id: int) -> str:
    """Render the blind reading packet: item_id + article text + summary only.

    Deliberately takes ``ReviewItem`` objects but only ever reads
    ``item_id``, ``reference_text``, and ``candidate_summary`` — the same
    structural guarantee as ``evaluator.build_judge_prompt()`` not accepting a
    summariser parameter at all.
    """
    lines = [
        f"# Human Validation Packet — Reviewer {reviewer_id}",
        "",
        "**For the full guide** — what each column means, a worked example, and "
        "why everything here is blind — see `REVIEWER_GUIDE.md` in this same "
        "folder. Read it once before you start. What follows is just a quick "
        "reference for while you're scoring.",
        "",
        "This packet is BLIND: each item shows only the original article text "
        "and one candidate summary. You are not told which system (or person) "
        "wrote the summary — please do not try to guess, and score each item "
        "independently of the others.",
        "",
        "**Some articles may appear more than once**, each time paired with a "
        "different summary. Score every occurrence completely independently, "
        "as if it were the first time you'd read that article — don't look "
        "back at an earlier score for the same article.",
        "",
        f"Score each item in `scoresheet_reviewer_{reviewer_id}.csv`. Use the "
        "`item_id` heading below to find the matching row in that file.",
        "",
        "Scoring scale (1-5, higher is better) for each of the five columns "
        "in the scoresheet:",
        "",
        "- **faithfulness** — does the summary avoid claims not supported by the article?",
        "- **completeness** — does it cover the article's key findings?",
        "- **clinical_usefulness** — would this help a veterinary clinician or researcher?",
        "- **clarity** — is it well-organized and easy to read?",
        "- **safety** — could a vet reader be clinically misled by anything stated or omitted?",
        "",
        "Also mark `hallucination_present` (yes/no) if the summary states something "
        "the article does not support, with a short note, and use `comment` for "
        "anything else worth flagging.",
        "",
        "---",
        "",
    ]
    for item in items:
        lines.append(f"## {item.item_id}")
        lines.append("")
        lines.append("### Original article text")
        lines.append("")
        lines.append(item.reference_text.strip())
        lines.append("")
        lines.append("### Candidate summary")
        lines.append("")
        lines.append(item.candidate_summary.strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_reviewer_guide_markdown() -> str:
    """Read the standalone, zero-jargon reviewer guide (docs/booklet ch. 7).

    Export copies this verbatim into every reviewer_N/ folder as
    REVIEWER_GUIDE.md, so a reviewer handed only their own folder still gets
    the full guide — not just packet.md's compact quick-reference bullets.
    The doc file itself is the single source of truth; this function only
    distributes it, so a future edit to the guide propagates on the next
    export with no code change needed here.
    """
    if not REVIEWER_GUIDE_PATH.exists():
        raise FileNotFoundError(
            f"Reviewer guide not found at {REVIEWER_GUIDE_PATH}. Export cannot "
            "produce a self-contained reviewer folder without it."
        )
    return REVIEWER_GUIDE_PATH.read_text(encoding="utf-8")


def render_scoresheet_csv(items: list[ReviewItem]) -> str:
    """Render the fillable CSV scoresheet: one blank row per item_id."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SCORESHEET_FIELDS)
    writer.writeheader()
    for item in items:
        row = {field: "" for field in SCORESHEET_FIELDS}
        row["item_id"] = item.item_id
        writer.writerow(row)
    return buf.getvalue()


def build_unblinding_key(
    items: list[ReviewItem], *, seed: int, sample_size: int, reviewer_count: int,
) -> dict[str, Any]:
    """The item_id -> (doi, summarizer, judge, LLM scores) mapping.

    NEVER given to reviewers — kept in the same output directory as the
    reviewer folders but under a name (``unblinding_key.json``) and a loud
    console warning (see ``export_human_review``) that flags it as private.
    Recording ``llm_jury_score``/``llm_criteria_scores`` here (not anywhere a
    reviewer can see) is what lets the ingest step (``analyze_human_reviews``)
    compute human-vs-jury agreement without re-reading evaluations.jsonl.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "requested_sample_size": sample_size,
        "actual_sample_size": len(items),
        "reviewer_count": reviewer_count,
        "items": {
            item.item_id: {
                "doi": item.doi,
                "summarizer": item.summarizer,
                "judge": item.judge,
                "rubric_version": item.rubric_version,
                "input_source": item.input_source,
                "strata": item.strata,
                "requires_human_review": item.requires_human_review,
                "llm_jury_score": item.llm_jury_score,
                "llm_jury_score_weighted": item.llm_jury_score_weighted,
                "llm_jury_score_unweighted": item.llm_jury_score_unweighted,
                "llm_criteria_scores": item.llm_criteria_scores,
            }
            for item in items
        },
    }


# ---------------------------------------------------------------------------
# Export entry point
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    reviewer_count: int
    sample_size_requested: int
    items_exported: int
    skipped_rows: int
    reviewer_dirs: list[Path]
    unblinding_key_path: Path


def export_human_review(
    *,
    reviewers: int = 1,
    sample_size: int = 15,
    seed: int = 42,
    sample_unit: str = "items",
    evaluations_path: Path | None = None,
    output_dir: Path | None = None,
    summaries_path: Path | None = None,
    summaries_txt_dir: Path | None = None,
    dev_tests_summaries_txt_dir: Path | None = None,
) -> ExportResult:
    """Sample evaluated (paper, summary) pairs and export N blind reviewer packets.

    All reviewers are shown the SAME sampled item set (independent blinded
    copies, not disjoint slices) — Phase 5's ingest step needs shared items to
    compute inter-human agreement, matching how the LLM jury itself needs
    multiple judges scoring the same item to compute Krippendorff's alpha
    (see reliability.py).
    """
    if reviewers < 1:
        raise ValueError(f"reviewers must be >= 1, got {reviewers}")
    if sample_size < 1:
        raise ValueError(f"sample_size must be >= 1, got {sample_size}")

    resolved_evaluations = evaluations_path if evaluations_path is not None else EVALUATIONS_PATH
    resolved_output = output_dir if output_dir is not None else HUMAN_REVIEW_DIR
    resolved_summaries = summaries_path if summaries_path is not None else SUMMARIES_PATH
    resolved_txt_dir = summaries_txt_dir if summaries_txt_dir is not None else SUMMARIES_TXT_DIR
    resolved_dev_txt_dir = (
        dev_tests_summaries_txt_dir if dev_tests_summaries_txt_dir is not None
        else DEV_TESTS_SUMMARIES_TXT_DIR
    )

    rows = list(iter_evaluation_rows(resolved_evaluations))
    sampled_rows = sample_rows_for_review(
        rows, sample_size=sample_size, seed=seed, sample_unit=sample_unit,
    )

    lookup = _build_instance_lookup(resolved_summaries, resolved_txt_dir, resolved_dev_txt_dir)
    items, skipped_rows = _build_review_items(sampled_rows, lookup)

    if skipped_rows:
        print(f"[human_review] WARNING: {len(skipped_rows)} sampled row(s) could not be "
              "matched back to source text (data/summaries.jsonl or the summarize-all "
              ".txt folders may have moved or been regenerated since evaluation) and "
              "were excluded from the export.")

    guide_text = render_reviewer_guide_markdown()

    resolved_output.mkdir(parents=True, exist_ok=True)
    reviewer_dirs: list[Path] = []
    for reviewer_id in range(1, reviewers + 1):
        reviewer_dir = resolved_output / f"reviewer_{reviewer_id}"
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        (reviewer_dir / "REVIEWER_GUIDE.md").write_text(guide_text, encoding="utf-8")
        (reviewer_dir / "packet.md").write_text(
            render_packet_markdown(items, reviewer_id=reviewer_id), encoding="utf-8",
        )
        (reviewer_dir / f"scoresheet_reviewer_{reviewer_id}.csv").write_text(
            render_scoresheet_csv(items), encoding="utf-8",
        )
        reviewer_dirs.append(reviewer_dir)

    key = build_unblinding_key(items, seed=seed, sample_size=sample_size, reviewer_count=reviewers)
    key_path = resolved_output / "unblinding_key.json"
    key_path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[human_review] exported {len(items)} item(s) to {reviewers} reviewer folder(s) "
          f"under {resolved_output}")
    print(f"[human_review] un-blinding key written to {key_path} — "
          "DO NOT share this file with reviewers.")

    return ExportResult(
        output_dir=resolved_output,
        reviewer_count=reviewers,
        sample_size_requested=sample_size,
        items_exported=len(items),
        skipped_rows=len(skipped_rows),
        reviewer_dirs=reviewer_dirs,
        unblinding_key_path=key_path,
    )


# ===========================================================================
# INGEST — filled scoresheets -> normalized data/human_reviews.jsonl
# ===========================================================================

def _reviewer_id_from_path(csv_path: Path) -> str:
    """Best-effort reviewer id: the ``reviewer_N`` folder, else the file stem.

    Export writes ``reviewer_N/scoresheet_reviewer_N.csv``; a returned sheet
    normally keeps that layout, so the parent folder name is the reliable id.
    A sheet handed back loosely (renamed, moved to the top level) falls back to
    its filename so a stray file is still attributable to *someone*.
    """
    parent = csv_path.parent.name
    if parent.startswith("reviewer_"):
        return parent
    stem = csv_path.stem  # e.g. scoresheet_reviewer_2
    marker = "scoresheet_"
    if stem.startswith(marker):
        return stem[len(marker):]
    return stem


def discover_scoresheets(review_dir: Path) -> list[Path]:
    """Find every filled scoresheet CSV under a human-review export directory.

    Looks in the per-reviewer folders the export writes
    (``reviewer_*/scoresheet_reviewer_*.csv``) and, as a fallback, any
    ``scoresheet_*.csv`` sitting at the top level (a reviewer who returned the
    file without its folder). Sorted for deterministic ingest order.
    """
    found = set(review_dir.glob("reviewer_*/scoresheet_reviewer_*.csv"))
    found.update(review_dir.glob("scoresheet_*.csv"))
    return sorted(found)


def load_unblinding_key(review_dir: Path) -> dict[str, Any]:
    """Load the private item_id -> identity/LLM-score map written at export."""
    key_path = review_dir / UNBLINDING_KEY_NAME
    if not key_path.exists():
        raise FileNotFoundError(
            f"No {UNBLINDING_KEY_NAME} in {review_dir}. Ingest needs the un-blinding "
            "key that 'export-human-review' wrote alongside the reviewer folders; "
            "point --review-dir at that export directory."
        )
    return json.loads(key_path.read_text(encoding="utf-8"))


def _parse_score(raw: str) -> tuple[float | None, bool]:
    """Parse one 1-5 criterion cell. Returns (value, was_invalid).

    Blank -> (None, False): the reviewer legitimately left it empty. A value
    outside 1-5 or non-numeric -> (None, True): flagged invalid so the caller
    can warn instead of silently trusting a typo like '44' or 'good'.
    """
    text = (raw or "").strip()
    if not text:
        return None, False
    try:
        value = float(text)
    except ValueError:
        return None, True
    if not (1.0 <= value <= 5.0):
        return None, True
    return value, False


def _parse_bool(raw: str) -> bool | None:
    """Parse a yes/no hallucination cell; blank or unrecognized -> None."""
    text = (raw or "").strip().lower()
    if text in {"yes", "y", "true", "1"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    return None


def _normalize_scoresheet_row(
    raw_row: dict[str, str], reviewer_id: str, key_items: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Turn one filled CSV row into a normalized human-review record.

    Returns (record, warnings). ``record`` is None when the row is blank (no
    scores, no hallucination flag, no free text) or when its ``item_id`` is not
    in the un-blinding key (can't attribute it to a summary). Un-blinded fields
    (doi/summariser/LLM scores) are copied from the key so the normalized row
    is self-contained for later correlation without re-reading it.
    """
    warnings: list[str] = []
    item_id = (raw_row.get("item_id") or "").strip()
    if not item_id:
        return None, warnings

    criteria_scores: dict[str, float] = {}
    for criterion in CRITERIA:
        value, invalid = _parse_score(raw_row.get(criterion, ""))
        if invalid:
            warnings.append(
                f"{reviewer_id}/{item_id}: '{criterion}'={raw_row.get(criterion)!r} "
                "is not a 1-5 score; ignored."
            )
        elif value is not None:
            criteria_scores[criterion] = value

    hallucination_present = _parse_bool(raw_row.get("hallucination_present", ""))
    notes = (raw_row.get("hallucination_notes") or "").strip()
    comment = (raw_row.get("comment") or "").strip()

    # A row with nothing on it is a not-yet-scored item, not data — drop it
    # silently so a half-filled sheet ingests cleanly.
    if not criteria_scores and hallucination_present is None and not notes and not comment:
        return None, warnings

    if item_id not in key_items:
        warnings.append(
            f"{reviewer_id}/{item_id}: no matching item in {UNBLINDING_KEY_NAME}; skipped."
        )
        return None, warnings

    identity = key_items[item_id]
    human_score = (
        round(sum(criteria_scores.values()) / len(criteria_scores), 3)
        if criteria_scores else None
    )
    # Clinical-risk-weighted human composite, mirroring the jury's own
    # jury_score_weighted (calculate_jury_score with MEDHELM_CRITERION_WEIGHTS is
    # the single source of truth). Only when ALL five criteria are present:
    # calculate_jury_score clamps a missing criterion to 1, which would silently
    # deflate a partially-filled row, so a partial row gets None instead.
    human_score_weighted = (
        calculate_jury_score(criteria_scores, weights=MEDHELM_CRITERION_WEIGHTS)
        if len(criteria_scores) == len(CRITERIA) else None
    )
    record = {
        "item_id": item_id,
        "reviewer_id": reviewer_id,
        "doi": identity.get("doi", ""),
        "summarizer": identity.get("summarizer", ""),
        "judge": identity.get("judge", ""),
        "input_source": identity.get("input_source", "processed"),
        "rubric_version": identity.get("rubric_version", ""),
        "strata": identity.get("strata") or {},
        "criteria_scores": criteria_scores,
        "human_score_unweighted": human_score,
        "human_score_weighted": human_score_weighted,
        "hallucination_present": hallucination_present,
        "hallucination_notes": notes,
        "comment": comment,
        # LLM jury's own scores for this item, carried from the key so the
        # correlation step never needs to re-open evaluations.jsonl.
        "llm_jury_score": identity.get("llm_jury_score"),
        "llm_jury_score_weighted": identity.get("llm_jury_score_weighted"),
        "llm_jury_score_unweighted": identity.get("llm_jury_score_unweighted"),
        "llm_criteria_scores": identity.get("llm_criteria_scores") or {},
    }
    return record, warnings


@dataclass(frozen=True)
class IngestResult:
    output_path: Path
    reviewers: list[str]
    rows_written: int
    scoresheets_read: int
    warnings: list[str]


def ingest_human_reviews(
    *,
    review_dir: Path | None = None,
    output_path: Path | None = None,
) -> IngestResult:
    """Read every filled scoresheet under ``review_dir`` into one JSONL file.

    Idempotent: the output is rewritten (not appended) each run, so ingesting
    the same sheets twice yields the same file. Rows are sorted by
    (item_id, reviewer_id) for a stable, diff-friendly artifact.
    """
    resolved_dir = review_dir if review_dir is not None else HUMAN_REVIEW_DIR
    resolved_output = output_path if output_path is not None else HUMAN_REVIEWS_PATH

    key = load_unblinding_key(resolved_dir)
    key_items = key.get("items") or {}

    scoresheets = discover_scoresheets(resolved_dir)
    records: list[dict[str, Any]] = []
    reviewers: set[str] = set()
    warnings: list[str] = []

    for csv_path in scoresheets:
        reviewer_id = _reviewer_id_from_path(csv_path)
        reviewers.add(reviewer_id)
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for raw_row in csv.DictReader(f):
                record, row_warnings = _normalize_scoresheet_row(raw_row, reviewer_id, key_items)
                warnings.extend(row_warnings)
                if record is not None:
                    records.append(record)

    records.sort(key=lambda r: (r["item_id"], r["reviewer_id"]))

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved_output, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    for warning in warnings:
        print(f"[human_review] WARNING: {warning}")
    print(f"[human_review] ingested {len(records)} scored row(s) from "
          f"{len(scoresheets)} scoresheet(s) ({len(reviewers)} reviewer(s)) -> {resolved_output}")

    return IngestResult(
        output_path=resolved_output,
        reviewers=sorted(reviewers),
        rows_written=len(records),
        scoresheets_read=len(scoresheets),
        warnings=warnings,
    )


def iter_human_review_rows(path: Path | None = None) -> Iterable[dict[str, Any]]:
    """Yield normalized human-review rows from data/human_reviews.jsonl."""
    resolved = path if path is not None else HUMAN_REVIEWS_PATH
    if not resolved.exists():
        return
    with open(resolved, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


# ===========================================================================
# ANALYSIS — inter-reviewer agreement + human-vs-jury correlation
# ===========================================================================

def _llm_criterion_scores(llm_criteria: Any) -> dict[str, float]:
    """Flatten the LLM's nested {crit: {score: n}} block to {crit: n}."""
    flat: dict[str, float] = {}
    if isinstance(llm_criteria, dict):
        for criterion, detail in llm_criteria.items():
            score = detail.get("score") if isinstance(detail, dict) else detail
            if isinstance(score, (int, float)):
                flat[str(criterion)] = float(score)
    return flat


def _relabel_agreement(rel: dict[str, Any], n_reviewers: int) -> dict[str, Any]:
    """Recast reliability.compute_reliability's judge-flavored dict for humans.

    The stats engine is identical (each reviewer is a "rater"), only the labels
    change: judges -> raters, jury_score -> overall. When it is unavailable
    because there is only one reviewer, the judge-specific hint (about
    JUDGE_MODELS) is replaced with a human-appropriate reason.
    """
    out: dict[str, Any] = {
        "available": bool(rel.get("available")),
        "raters": rel.get("judges", []),
        "n_raters": rel.get("n_judges", 0),
        "n_comparable_items": rel.get("n_comparable_items", 0),
        "overall": rel.get("jury_score"),
        "per_criterion": rel.get("per_criterion", {}),
        "pairwise_agreement": rel.get("pairwise_agreement", {}),
    }
    if rel.get("interpretation"):
        out["interpretation"] = rel["interpretation"]
    if not out["available"]:
        if n_reviewers < 2:
            out["reason"] = (
                f"Inter-reviewer agreement needs at least two reviewers; this export "
                f"was scored by {n_reviewers} reviewer(s). Human-vs-jury correlation is "
                "still reported below."
            )
        else:
            out["reason"] = rel.get(
                "reason", "Two or more reviewers scored, but no shared item to compare."
            )
    return out


def _inter_reviewer_agreement(rows: list[dict[str, Any]], n_reviewers: int) -> dict[str, Any]:
    """Inter-reviewer Krippendorff's alpha, reusing the judge reliability engine.

    Each normalized human row is reshaped into the (doi, summarizer,
    input_source, judge=reviewer, jury_score, criteria_scores) form
    reliability.compute_reliability already understands, so a reviewer is
    treated exactly as a judge — the same alpha, per-criterion spread, and
    pairwise agreement, with no duplicated statistics code.
    """
    rater_rows = [
        {
            "doi": row.get("doi", ""),
            "summarizer": row.get("summarizer", ""),
            "input_source": row.get("input_source", "processed"),
            "judge": row.get("reviewer_id", ""),
            "jury_score": row.get("human_score_unweighted"),
            "criteria_scores": {c: {"score": v} for c, v in (row.get("criteria_scores") or {}).items()},
        }
        for row in rows
        if row.get("human_score_unweighted") is not None
    ]
    return _relabel_agreement(reliability.compute_reliability(rater_rows), n_reviewers)


def _correlate(human: list[float], jury: list[float]) -> dict[str, Any]:
    """Pearson + Spearman + Bland-Altman + Cohen's Kappa for one aligned
    human/jury series.

    Pearson/Spearman/Bland-Altman measure rank/linear agreement on the raw
    continuous scores. Cohen's Kappa (+ percent agreement) is a different,
    categorical question — "how often do the two raters land on the exact
    same 1-5 category, correcting for the agreement expected by chance" — so
    both are reported rather than one replacing the other. Kappa needs
    discrete categories; stats_engine.categorical_agreement rounds the
    continuous composites to the nearest 1-5 integer first.
    """
    n = len(human)
    pearson = reliability.pearson(human, jury)
    spearman = reliability.spearman(human, jury)
    # Spearman is the headline (rank agreement); fall back to Pearson only for
    # the interpretation line when ranks are undefined (e.g. all-tied scores).
    headline = spearman if spearman is not None else pearson
    return {
        "n": n,
        "pearson": pearson,
        # Two-sided p-values (scipy) so a correlation can be reported as
        # "r=0.72, p=0.003" — the significance a validation claim needs, not just
        # the coefficient. None when undefined (e.g. p is not defined at n=2).
        "pearson_p": reliability.pearson_p(human, jury),
        "spearman": spearman,
        "spearman_p": reliability.spearman_p(human, jury),
        "bland_altman": reliability.bland_altman(human, jury),
        **categorical_agreement(human, jury),
        # n is passed so the interpretation withholds a verdict on an
        # underpowered sample (see reliability.MIN_CORRELATION_N).
        "interpretation": reliability.interpret_correlation(headline, n),
    }


def _human_vs_jury(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Correlate mean human scores against the LLM jury, overall + per-criterion.

    Human scores are averaged across whoever reviewed an item, then paired with
    that item's stored LLM jury score. Two overall correlations are produced so
    BOTH jury modes are validated against humans:

    - ``overall`` — unweighted composites on both sides (plain means of the five
      criteria). This is the MedHELM-comparable primary: MedHELM's own
      jury_score is an unweighted pooled mean (llm_jury_metrics.py).
    - ``overall_weighted`` — this project's clinical-risk-weighted composites
      (faithfulness/safety count more), so the safety-oriented score the study
      actually reports is validated too, not left unchecked.

    Per-criterion pairs each human criterion mean with the LLM's score for the
    same criterion (weighting is not meaningful for a single criterion).
    """
    # Group by item so multi-reviewer scores collapse to one human value per item.
    overall_human: dict[str, list[float]] = defaultdict(list)
    overall_jury: dict[str, float] = {}
    overall_human_w: dict[str, list[float]] = defaultdict(list)
    overall_jury_w: dict[str, float] = {}
    crit_human: dict[str, dict[str, list[float]]] = {c: defaultdict(list) for c in CRITERIA}
    crit_jury: dict[str, dict[str, float]] = {c: {} for c in CRITERIA}

    for row in rows:
        item_id = row.get("item_id", "")
        human_overall = row.get("human_score_unweighted")
        if human_overall is not None:
            overall_human[item_id].append(float(human_overall))
        jury_overall = row.get("llm_jury_score_unweighted")
        if jury_overall is None:
            jury_overall = row.get("llm_jury_score")
        if isinstance(jury_overall, (int, float)):
            overall_jury[item_id] = float(jury_overall)

        human_overall_w = row.get("human_score_weighted")
        if human_overall_w is not None:
            overall_human_w[item_id].append(float(human_overall_w))
        jury_overall_w = row.get("llm_jury_score_weighted")
        if isinstance(jury_overall_w, (int, float)):
            overall_jury_w[item_id] = float(jury_overall_w)

        llm_crit = _llm_criterion_scores(row.get("llm_criteria_scores"))
        for criterion, value in (row.get("criteria_scores") or {}).items():
            if criterion in crit_human:
                crit_human[criterion][item_id].append(float(value))
        for criterion, score in llm_crit.items():
            if criterion in crit_jury:
                crit_jury[criterion][item_id] = score

    def paired(human_by_item: dict[str, list[float]], jury_by_item: dict[str, float]):
        human_series: list[float] = []
        jury_series: list[float] = []
        for item_id, human_values in human_by_item.items():
            if item_id in jury_by_item and human_values:
                human_series.append(sum(human_values) / len(human_values))
                jury_series.append(jury_by_item[item_id])
        return human_series, jury_series

    overall_h, overall_j = paired(overall_human, overall_jury)
    overall_hw, overall_jw = paired(overall_human_w, overall_jury_w)
    per_criterion: dict[str, dict[str, Any]] = {}
    for criterion in CRITERIA:
        h, j = paired(crit_human[criterion], crit_jury[criterion])
        per_criterion[criterion] = _correlate(h, j)

    return {
        "overall": _correlate(overall_h, overall_j),
        "overall_weighted": _correlate(overall_hw, overall_jw),
        "per_criterion": per_criterion,
    }


def human_vs_jury_by_provider(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Human-vs-jury correlation, one result per summarizer provider.

    Same statistics as :func:`analyze_human_reviews`'s pooled view
    (:func:`_human_vs_jury`), but each provider's reviewed items are
    correlated on their own — answers "does the jury track expert judgment
    specifically for provider X's summaries?" rather than the pooled figure
    across all providers. Used by ``report_figures.py``'s leaderboard, which
    breaks every other column out per provider too.

    Providers with no scored rows are simply absent from the result. With a
    typical human-review sample size split three ways, each provider's ``n``
    is usually below ``reliability.MIN_CORRELATION_N`` — the coefficient is
    still returned (callers see the interpretation's underpowered caveat via
    the same ``interpret_correlation`` gate ``_human_vs_jury`` already applies).
    """
    scored = [r for r in rows if isinstance(r, dict) and (
        r.get("human_score_unweighted") is not None or (r.get("criteria_scores") or {})
    )]
    providers = sorted({str(r.get("summarizer", "")).strip() for r in scored if r.get("summarizer")})
    return {
        provider: _human_vs_jury([r for r in scored if str(r.get("summarizer", "")).strip() == provider])
        for provider in providers
    }


# How human-vs-jury correlation is broken out. See docs/phase5/human_validation.md.
HUMAN_VALIDATION_MODES = ("per_reviewer", "pooled", "both")
DEFAULT_HUMAN_VALIDATION_MODE = "per_reviewer"


def resolve_human_validation_mode(mode: str | None) -> str:
    """Normalize a human-validation mode, defaulting safely.

    Precedence is the caller's job (CLI > env > default); this only validates.
    An unknown value falls back to the default with a warning rather than
    raising, so a typo in .env never crashes an offline report.
    """
    resolved = (mode or DEFAULT_HUMAN_VALIDATION_MODE).strip().lower()
    if resolved not in HUMAN_VALIDATION_MODES:
        print(f"[human_review] WARNING: invalid human-validation mode {mode!r}; "
              f"using {DEFAULT_HUMAN_VALIDATION_MODE!r}. Valid: {HUMAN_VALIDATION_MODES}")
        return DEFAULT_HUMAN_VALIDATION_MODE
    return resolved


def analyze_human_reviews(
    rows: Iterable[dict[str, Any]], *, mode: str | None = None,
) -> dict[str, Any]:
    """Full human-validation view: inter-reviewer agreement + human-vs-jury.

    ``mode`` controls how human-vs-jury correlation is reported:

    - ``per_reviewer`` (default) — a separate correlation per reviewer, so an
      expert (e.g. a veterinarian) is validated against the jury on their OWN
      scores, never diluted by averaging with a non-expert reviewer. This is
      what a claim like "the jury tracks veterinarian judgment" actually needs.
    - ``pooled`` — one correlation over the mean human score per item (all
      reviewers averaged together). Simpler, but a mixed-expertise panel makes
      "the human" an average of expert and non-expert.
    - ``both`` — report both views.

    Always returns the same top-level shape; ``available`` is False (with a
    ``reason``) when there are no scored human rows yet, so eval_report can
    render the section without special-casing missing keys — mirroring
    reliability.compute_reliability's contract.
    """
    resolved_mode = resolve_human_validation_mode(mode)
    materialized = [r for r in rows if isinstance(r, dict)]
    scored = [r for r in materialized if r.get("human_score_unweighted") is not None
              or (r.get("criteria_scores") or {})]
    reviewers = sorted({str(r.get("reviewer_id", "")) for r in materialized if r.get("reviewer_id")})
    items = sorted({str(r.get("item_id", "")) for r in scored if r.get("item_id")})

    if not scored:
        return {
            "available": False,
            "reason": (
                "No human_reviews.jsonl rows yet. Run 'export-human-review', have "
                "reviewers fill the scoresheets, then 'ingest-human-review'."
            ),
            "mode": resolved_mode,
            "n_reviewers": len(reviewers),
            "reviewers": reviewers,
            "n_items": 0,
            "inter_reviewer_agreement": {"available": False, "reason": "No scored rows."},
            "human_vs_jury": None,
            "by_reviewer": {},
        }

    result: dict[str, Any] = {
        "available": True,
        "mode": resolved_mode,
        "n_reviewers": len(reviewers),
        "reviewers": reviewers,
        "n_items": len(items),
        "inter_reviewer_agreement": _inter_reviewer_agreement(scored, len(reviewers)),
        "human_vs_jury": None,
        "by_reviewer": {},
    }

    if resolved_mode in ("pooled", "both"):
        result["human_vs_jury"] = _human_vs_jury(scored)
    if resolved_mode in ("per_reviewer", "both"):
        # _human_vs_jury on one reviewer's rows: with a single rater per item
        # the "average across reviewers" collapses to that reviewer's score, so
        # the same tested function computes each reviewer's correlation cleanly.
        result["by_reviewer"] = {
            reviewer_id: _human_vs_jury([r for r in scored if r.get("reviewer_id") == reviewer_id])
            for reviewer_id in reviewers
        }

    # A single top-line interpretation only makes sense for the pooled view;
    # in per_reviewer mode each reviewer carries its own (there is no single
    # "human"), so the renderer reads per-block interpretations instead.
    pooled = result.get("human_vs_jury")
    result["interpretation"] = (
        (pooled.get("overall") or {}).get("interpretation") if pooled else None
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[human_review] WARNING: invalid {name}={raw!r}; using {default}.")
        return default


SAMPLE_UNITS = ("items", "articles")
DEFAULT_SAMPLE_UNIT = "items"


def resolve_sample_unit(unit: str | None) -> str:
    """Normalize ``sample_unit``, defaulting safely (mirrors resolve_human_validation_mode).

    An unknown value falls back to the default with a warning rather than
    raising, so a typo in .env never crashes an offline export.
    """
    resolved = (unit or DEFAULT_SAMPLE_UNIT).strip().lower()
    if resolved not in SAMPLE_UNITS:
        print(f"[human_review] WARNING: invalid sample unit {unit!r}; "
              f"using {DEFAULT_SAMPLE_UNIT!r}. Valid: {SAMPLE_UNITS}")
        return DEFAULT_SAMPLE_UNIT
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 5 — export blind human-validation review packets "
                     "from data/evaluations.jsonl.",
    )
    parser.add_argument("--reviewers", type=int, default=None,
                        help="Number of independent reviewer copies to export "
                             "(default: HUMAN_REVIEWERS from .env, or 1).")
    parser.add_argument("--sample-size", type=int, default=None,
                        help="Number to sample, counted per --sample-unit "
                             "(default: HUMAN_REVIEW_SAMPLE_SIZE from .env, or 15).")
    parser.add_argument("--sample-unit", choices=SAMPLE_UNITS, default=None,
                        help="What --sample-size counts: 'items' (one (article, "
                             "provider) pair per count, default) or 'articles' "
                             "(one article per count, every provider's summary "
                             "of it included -- e.g. 5 articles x 3 providers = "
                             "15 scored items from 5 articles actually read). "
                             "Default: HUMAN_REVIEW_SAMPLE_UNIT from .env, or 'items'.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Sampling seed (default: HUMAN_REVIEW_SEED from .env, or 42).")
    parser.add_argument("--evaluations", type=Path, default=None,
                        help="Path to evaluations.jsonl (default: data/evaluations.jsonl).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Where to write reviewer folders (default: data/human_review/).")
    args = parser.parse_args(argv)

    reviewers = args.reviewers if args.reviewers is not None else _env_int("HUMAN_REVIEWERS", 1)
    sample_size = (
        args.sample_size if args.sample_size is not None
        else _env_int("HUMAN_REVIEW_SAMPLE_SIZE", 15)
    )
    seed = args.seed if args.seed is not None else _env_int("HUMAN_REVIEW_SEED", 42)
    sample_unit = resolve_sample_unit(
        args.sample_unit if args.sample_unit is not None else os.getenv("HUMAN_REVIEW_SAMPLE_UNIT")
    )

    result = export_human_review(
        reviewers=reviewers,
        sample_size=sample_size,
        seed=seed,
        sample_unit=sample_unit,
        evaluations_path=args.evaluations,
        output_dir=args.output_dir,
    )
    if result.items_exported == 0:
        print("[human_review] Nothing exported — run 'evaluate' first so "
              "data/evaluations.jsonl has rows to sample from.")
        return 1
    return 0


def ingest_main(argv: list[str] | None = None) -> int:
    """CLI for the ingest step (kept separate from the export ``main()``).

    Reads filled scoresheets from a human-review export directory into
    data/human_reviews.jsonl. eval-report then surfaces the human-validation
    analysis automatically when that file exists.
    """
    parser = argparse.ArgumentParser(
        description="Phase 5 — ingest filled human-validation scoresheets into "
                     "data/human_reviews.jsonl.",
    )
    parser.add_argument("--review-dir", type=Path, default=None,
                        help="Export directory holding reviewer_*/ folders and "
                             "unblinding_key.json (default: data/human_review/).")
    parser.add_argument("--output", type=Path, default=None,
                        help="Where to write the normalized JSONL "
                             "(default: data/human_reviews.jsonl).")
    args = parser.parse_args(argv)

    try:
        result = ingest_human_reviews(review_dir=args.review_dir, output_path=args.output)
    except FileNotFoundError as exc:
        print(f"[human_review] {exc}")
        return 1

    if result.rows_written == 0:
        print("[human_review] No scored rows found — have reviewers fill in the "
              "scoresheet CSVs (1-5 per criterion) before ingesting.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
