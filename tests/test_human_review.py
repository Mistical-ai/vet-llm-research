"""
Tests for llm-sum/human_review.py — human validation sampling + blind export.

Critical assertions:
    Blind check: rendered packet + scoresheet contain NONE of the forbidden
    tokens (mirrors evaluator.py's blind judge prompt test).
    Sampler: rows flagged requires_human_review are prioritized; remaining
    slots are filled with a stratified, deterministic (seeded) sample.
    Export: --reviewers N produces N independent blind copies of the SAME
    sampled items; the un-blinding key is the only file carrying identity.
    Rows whose source text can't be resolved are skipped, not fatal.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import prepare_texts
from evaluator import BLIND_FORBIDDEN_TOKENS
from file_paths import doi_to_slug
import human_review
from human_review import (
    REVIEWER_GUIDE_PATH,
    ReviewItem,
    SCORESHEET_FIELDS,
    build_unblinding_key,
    export_human_review,
    render_packet_markdown,
    render_reviewer_guide_markdown,
    render_scoresheet_csv,
    sample_rows_for_review,
)


# ---------------------------------------------------------------------------
# Blind protocol
# ---------------------------------------------------------------------------

def _make_item(item_id: str, *, summarizer: str = "openai", judge: str = "anthropic") -> ReviewItem:
    return ReviewItem(
        item_id=item_id,
        doi="10.1111/test.review",
        summarizer=summarizer,
        judge=judge,
        rubric_version="vet_medhelm_score_v1.0",
        input_source="processed",
        reference_text="Dogs with condition X improved after treatment Y in this retrospective study.",
        candidate_summary="This study found that treatment Y improved outcomes in dogs with condition X.",
        strata={"species": ["Canine"], "study_design": "Retrospective", "clinical_topic": "Cardiology",
                "journal": "JVIM", "input_source": "processed"},
        requires_human_review=False,
        llm_jury_score=4.2,
        llm_jury_score_weighted=4.3,
        llm_jury_score_unweighted=4.1,
        llm_criteria_scores={"faithfulness": {"score": 4, "reasoning": "ok"}},
    )


def test_packet_markdown_contains_no_model_identifiers() -> None:
    items = [_make_item("item_001"), _make_item("item_002", summarizer="gemini", judge="openai")]
    rendered = render_packet_markdown(items, reviewer_id=1).lower()
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in rendered, (
            f"Reviewer packet leaked the token '{forbidden}'. The blind protocol "
            "requires reviewer-facing files to be model-agnostic."
        )


def test_scoresheet_csv_contains_no_model_identifiers() -> None:
    items = [_make_item("item_001"), _make_item("item_002", summarizer="anthropic")]
    rendered = render_scoresheet_csv(items).lower()
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in rendered


def test_scoresheet_csv_has_expected_columns_and_blank_scores() -> None:
    items = [_make_item("item_001")]
    rendered = render_scoresheet_csv(items)
    reader = csv.DictReader(io.StringIO(rendered))
    assert reader.fieldnames == SCORESHEET_FIELDS
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["item_id"] == "item_001"
    for field in SCORESHEET_FIELDS:
        if field == "item_id":
            continue
        assert rows[0][field] == ""


def test_render_reviewer_guide_markdown_returns_the_checked_in_doc() -> None:
    content = render_reviewer_guide_markdown()
    assert "A Guide for Veterinarian Reviewers" in content
    assert content == REVIEWER_GUIDE_PATH.read_text(encoding="utf-8")


def test_render_reviewer_guide_markdown_contains_no_model_identifiers() -> None:
    content = render_reviewer_guide_markdown().lower()
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in content, (
            f"Reviewer guide leaked the token '{forbidden}'; the guide must stay "
            "model-agnostic like packet.md and the scoresheet."
        )


def test_render_reviewer_guide_markdown_raises_clearly_when_missing(monkeypatch, tmp_path: Path) -> None:
    import pytest
    monkeypatch.setattr(human_review, "REVIEWER_GUIDE_PATH", tmp_path / "does_not_exist.md")
    with pytest.raises(FileNotFoundError):
        human_review.render_reviewer_guide_markdown()


def test_unblinding_key_carries_identity_and_llm_scores() -> None:
    items = [_make_item("item_001")]
    key = build_unblinding_key(items, seed=42, sample_size=1, reviewer_count=2)
    assert key["items"]["item_001"]["doi"] == "10.1111/test.review"
    assert key["items"]["item_001"]["summarizer"] == "openai"
    assert key["items"]["item_001"]["judge"] == "anthropic"
    assert key["items"]["item_001"]["llm_jury_score"] == 4.2
    assert key["reviewer_count"] == 2
    assert key["seed"] == 42


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------

def _row(doi: str, summarizer: str, *, journal: str, requires_review: bool = False,
        timestamp: str = "2026-01-01T00:00:00+00:00", input_source: str = "processed") -> dict:
    return {
        "doi": doi,
        "summarizer": summarizer,
        "input_source": input_source,
        "requires_human_review": requires_review,
        "timestamp": timestamp,
        "strata": {
            "species": ["Canine"], "study_design": "RCT", "clinical_topic": "Oncology",
            "journal": journal, "input_source": input_source,
        },
    }


def test_sampler_prioritizes_flagged_rows() -> None:
    rows = [_row(f"10.1/{i}", "openai", journal="JVIM", requires_review=True) for i in range(3)]
    rows += [_row(f"10.1/other{i}", "openai", journal="JVIM") for i in range(10)]
    sampled = sample_rows_for_review(rows, sample_size=3, seed=42)
    assert len(sampled) == 3
    assert all(r["requires_human_review"] for r in sampled)


def test_sampler_fills_remaining_slots_after_flagged() -> None:
    flagged = [_row("10.1/flag0", "openai", journal="JVIM", requires_review=True)]
    remainder = [_row(f"10.1/r{i}", "openai", journal="JVIM") for i in range(10)]
    sampled = sample_rows_for_review(flagged + remainder, sample_size=4, seed=42)
    assert len(sampled) == 4
    assert sum(1 for r in sampled if r["requires_human_review"]) == 1


def test_sampler_stratifies_across_journals() -> None:
    rows = []
    for journal in ("JVIM", "AJVR", "VRU"):
        rows += [_row(f"10.1/{journal}_{i}", "openai", journal=journal) for i in range(10)]
    sampled = sample_rows_for_review(rows, sample_size=6, seed=42)
    journals_seen = {r["strata"]["journal"] for r in sampled}
    assert len(journals_seen) == 3, (
        "A stratified sample of 6 across 3 evenly-sized journal groups should "
        f"span all three journals, saw only {journals_seen}"
    )


def test_sampler_is_deterministic_for_a_fixed_seed() -> None:
    rows = []
    for journal in ("JVIM", "AJVR"):
        rows += [_row(f"10.1/{journal}_{i}", "openai", journal=journal) for i in range(8)]
    first = sample_rows_for_review(rows, sample_size=5, seed=7)
    second = sample_rows_for_review(rows, sample_size=5, seed=7)
    assert [r["doi"] for r in first] == [r["doi"] for r in second]


def test_sampler_dedupes_to_latest_row_per_item() -> None:
    older = _row("10.1/dup", "openai", journal="JVIM", timestamp="2026-01-01T00:00:00+00:00")
    older["requires_human_review"] = False
    newer = _row("10.1/dup", "openai", journal="JVIM", timestamp="2026-02-01T00:00:00+00:00")
    newer["requires_human_review"] = True
    sampled = sample_rows_for_review([older, newer], sample_size=5, seed=1)
    assert len(sampled) == 1
    assert sampled[0]["requires_human_review"] is True


def test_sampler_returns_empty_for_zero_sample_size() -> None:
    rows = [_row("10.1/a", "openai", journal="JVIM")]
    assert sample_rows_for_review(rows, sample_size=0, seed=1) == []


def test_sampler_default_sample_unit_matches_explicit_items() -> None:
    rows = [_row(f"10.1/{i}", "openai", journal="JVIM") for i in range(5)]
    default = sample_rows_for_review(rows, sample_size=3, seed=42)
    explicit = sample_rows_for_review(rows, sample_size=3, seed=42, sample_unit="items")
    assert default == explicit


def test_sample_rows_for_review_rejects_unknown_sample_unit() -> None:
    import pytest
    rows = [_row("10.1/a", "openai", journal="JVIM")]
    with pytest.raises(ValueError):
        sample_rows_for_review(rows, sample_size=1, seed=1, sample_unit="bogus")


# ---------------------------------------------------------------------------
# Sampler — sample_unit="articles" (read once, score every provider)
# ---------------------------------------------------------------------------

def _multi_provider_rows(num_articles: int, *, journal: str = "JVIM",
                          providers: tuple[str, ...] = ("openai", "anthropic", "gemini")) -> list[dict]:
    rows = []
    for i in range(num_articles):
        doi = f"10.1/article{i}"
        rows += [_row(doi, provider, journal=journal) for provider in providers]
    return rows


def test_sampler_articles_mode_includes_every_provider_of_each_selected_article() -> None:
    rows = _multi_provider_rows(5)
    sampled = sample_rows_for_review(rows, sample_size=2, seed=42, sample_unit="articles")
    dois_seen = {r["doi"] for r in sampled}
    assert len(dois_seen) == 2
    assert len(sampled) == 6  # 2 articles x 3 providers
    for doi in dois_seen:
        summarizers = {r["summarizer"] for r in sampled if r["doi"] == doi}
        assert summarizers == {"openai", "anthropic", "gemini"}


def test_sampler_articles_mode_interleaves_sibling_rows_apart() -> None:
    rows = _multi_provider_rows(5)
    sampled = sample_rows_for_review(rows, sample_size=5, seed=42, sample_unit="articles")
    assert len(sampled) == 15
    for idx in range(len(sampled) - 1):
        assert sampled[idx]["doi"] != sampled[idx + 1]["doi"], (
            f"Sibling rows for {sampled[idx]['doi']} landed adjacent at index {idx}; "
            "articles mode must interleave same-article rows apart so a reviewer "
            "never scores two summaries of the same article back to back."
        )


def test_sampler_articles_mode_prioritizes_flagged_articles() -> None:
    flagged_doi = "10.1/flagged"
    rows = [_row(flagged_doi, p, journal="JVIM", requires_review=(p == "openai"))
            for p in ("openai", "anthropic", "gemini")]
    rows += _multi_provider_rows(5, journal="JVIM")
    sampled = sample_rows_for_review(rows, sample_size=1, seed=1, sample_unit="articles")
    assert {r["doi"] for r in sampled} == {flagged_doi}
    assert len(sampled) == 3


def test_sampler_articles_mode_stratifies_across_journals() -> None:
    rows = []
    for journal in ("JVIM", "AJVR", "VRU"):
        for i in range(10):
            doi = f"10.1/{journal}_article{i}"
            rows += [_row(doi, provider, journal=journal)
                     for provider in ("openai", "anthropic", "gemini")]
    sampled = sample_rows_for_review(rows, sample_size=6, seed=42, sample_unit="articles")
    journals_seen = {r["strata"]["journal"] for r in sampled}
    assert len(journals_seen) == 3, (
        "A stratified sample of 6 articles across 3 evenly-sized journal groups "
        f"should span all three journals, saw only {journals_seen}"
    )


def test_sampler_articles_mode_is_deterministic_for_a_fixed_seed() -> None:
    rows = _multi_provider_rows(8)
    first = sample_rows_for_review(rows, sample_size=4, seed=7, sample_unit="articles")
    second = sample_rows_for_review(rows, sample_size=4, seed=7, sample_unit="articles")
    assert [(r["doi"], r["summarizer"]) for r in first] == [(r["doi"], r["summarizer"]) for r in second]


# ---------------------------------------------------------------------------
# Full export (offline, mock corpus)
# ---------------------------------------------------------------------------

def _write_fixture_corpus(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Build a minimal evaluations.jsonl + summaries.jsonl/processed cache pair.

    Two DOIs, one summarizer slot each, both successfully judged and joinable
    back to real reference/candidate text via prepare_texts.PROCESSED_DIR
    (patched here exactly as tests/test_eval_instances.py does).
    """
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    dois = ["10.1111/test.one", "10.1111/test.two"]
    summaries_path = tmp_path / "summaries.jsonl"
    evaluations_path = tmp_path / "evaluations.jsonl"

    with open(summaries_path, "w", encoding="utf-8") as f:
        for doi in dois:
            f.write(json.dumps({
                "doi": doi,
                "journal": "JVIM",
                "models": {"openai": {"status": "success", "summary": f"Summary of {doi}."}},
            }) + "\n")

    for doi in dois:
        slug = doi_to_slug(doi)
        (processed_dir / f"{slug}.jsonl").write_text(
            json.dumps({"doi": doi, "slug": slug, "text": f"Full cleaned article text for {doi}."}) + "\n",
            encoding="utf-8",
        )

    with open(evaluations_path, "w", encoding="utf-8") as f:
        for i, doi in enumerate(dois):
            f.write(json.dumps({
                "doi": doi,
                "summarizer": "openai",
                "judge": "anthropic",
                "input_source": "processed",
                "rubric_version": "vet_medhelm_score_v1.0",
                "jury_score": 4.0,
                "jury_score_weighted": 4.0,
                "jury_score_unweighted": 4.0,
                "criteria_scores": {"faithfulness": {"score": 4, "reasoning": "ok"}},
                "requires_human_review": i == 0,
                "strata": {"species": ["Canine"], "study_design": "RCT", "clinical_topic": "Cardiology",
                          "journal": "JVIM", "input_source": "processed"},
                "timestamp": "2026-01-01T00:00:00+00:00",
            }) + "\n")

    return summaries_path, evaluations_path


def test_export_human_review_creates_reviewer_folders(tmp_path: Path, monkeypatch) -> None:
    summaries_path, evaluations_path = _write_fixture_corpus(tmp_path, monkeypatch)
    output_dir = tmp_path / "human_review"

    result = export_human_review(
        reviewers=2,
        sample_size=2,
        seed=42,
        evaluations_path=evaluations_path,
        output_dir=output_dir,
        summaries_path=summaries_path,
        summaries_txt_dir=tmp_path / "no_summaries_txt",
        dev_tests_summaries_txt_dir=tmp_path / "no_dev_summaries_txt",
    )

    assert result.items_exported == 2
    assert result.skipped_rows == 0
    assert len(result.reviewer_dirs) == 2

    expected_guide = REVIEWER_GUIDE_PATH.read_text(encoding="utf-8")
    for reviewer_id in (1, 2):
        reviewer_dir = output_dir / f"reviewer_{reviewer_id}"
        guide = (reviewer_dir / "REVIEWER_GUIDE.md").read_text(encoding="utf-8")
        packet = (reviewer_dir / "packet.md").read_text(encoding="utf-8")
        scoresheet = (reviewer_dir / f"scoresheet_reviewer_{reviewer_id}.csv").read_text(encoding="utf-8")
        assert guide == expected_guide
        assert "item_001" in packet and "item_002" in packet
        assert "Full cleaned article text for" in packet
        for forbidden in BLIND_FORBIDDEN_TOKENS:
            assert forbidden not in guide.lower()
            assert forbidden not in packet.lower()
            assert forbidden not in scoresheet.lower()

    key = json.loads((output_dir / "unblinding_key.json").read_text(encoding="utf-8"))
    assert set(key["items"]) == {"item_001", "item_002"}
    assert {item["doi"] for item in key["items"].values()} == {"10.1111/test.one", "10.1111/test.two"}
    assert all(item["summarizer"] == "openai" for item in key["items"].values())


def _write_fixture_corpus_multi_provider(tmp_path: Path, monkeypatch, *, num_articles: int = 3) -> tuple[Path, Path]:
    """Like _write_fixture_corpus, but every article has all 3 providers
    successfully summarized and evaluated -- used to test sample_unit="articles"."""
    processed_dir = tmp_path / "processed_multi"
    processed_dir.mkdir()
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    providers = ("openai", "anthropic", "gemini")
    dois = [f"10.1111/test.multi{i}" for i in range(num_articles)]
    summaries_path = tmp_path / "summaries_multi.jsonl"
    evaluations_path = tmp_path / "evaluations_multi.jsonl"

    with open(summaries_path, "w", encoding="utf-8") as f:
        for doi in dois:
            f.write(json.dumps({
                "doi": doi,
                "journal": "JVIM",
                "models": {p: {"status": "success", "summary": f"Summary variant {i} of {doi}."}
                           for i, p in enumerate(providers)},
            }) + "\n")

    for doi in dois:
        slug = doi_to_slug(doi)
        (processed_dir / f"{slug}.jsonl").write_text(
            json.dumps({"doi": doi, "slug": slug, "text": f"Full cleaned article text for {doi}."}) + "\n",
            encoding="utf-8",
        )

    with open(evaluations_path, "w", encoding="utf-8") as f:
        for doi in dois:
            for provider in providers:
                f.write(json.dumps({
                    "doi": doi,
                    "summarizer": provider,
                    "judge": "anthropic",
                    "input_source": "processed",
                    "rubric_version": "vet_medhelm_score_v1.0",
                    "jury_score": 4.0,
                    "jury_score_weighted": 4.0,
                    "jury_score_unweighted": 4.0,
                    "criteria_scores": {"faithfulness": {"score": 4, "reasoning": "ok"}},
                    "requires_human_review": False,
                    "strata": {"species": ["Canine"], "study_design": "RCT", "clinical_topic": "Cardiology",
                              "journal": "JVIM", "input_source": "processed"},
                    "timestamp": "2026-01-01T00:00:00+00:00",
                }) + "\n")

    return summaries_path, evaluations_path


def test_export_human_review_articles_mode_reads_each_article_once(tmp_path: Path, monkeypatch) -> None:
    """sample_unit="articles" with sample_size=2 should read only 2 articles
    but export all 3 providers' summaries of each (6 items total)."""
    summaries_path, evaluations_path = _write_fixture_corpus_multi_provider(tmp_path, monkeypatch, num_articles=3)
    output_dir = tmp_path / "human_review_articles"

    result = export_human_review(
        reviewers=1,
        sample_size=2,
        sample_unit="articles",
        seed=42,
        evaluations_path=evaluations_path,
        output_dir=output_dir,
        summaries_path=summaries_path,
        summaries_txt_dir=tmp_path / "no_summaries_txt",
        dev_tests_summaries_txt_dir=tmp_path / "no_dev_summaries_txt",
    )

    assert result.items_exported == 6  # 2 articles x 3 providers

    key = json.loads((output_dir / "unblinding_key.json").read_text(encoding="utf-8"))
    dois = {item["doi"] for item in key["items"].values()}
    assert len(dois) == 2
    for doi in dois:
        summarizers = {item["summarizer"] for item in key["items"].values() if item["doi"] == doi}
        assert summarizers == {"openai", "anthropic", "gemini"}

    reviewer_dir = output_dir / "reviewer_1"
    assert (reviewer_dir / "REVIEWER_GUIDE.md").exists()
    packet = (reviewer_dir / "packet.md").read_text(encoding="utf-8")
    for forbidden in BLIND_FORBIDDEN_TOKENS:
        assert forbidden not in packet.lower()


def test_export_human_review_skips_rows_missing_source_text(tmp_path: Path, monkeypatch) -> None:
    """A row whose (doi, summarizer, input_source) no longer resolves to a
    live instance (source text moved/regenerated) is skipped, not fatal."""
    summaries_path, evaluations_path = _write_fixture_corpus(tmp_path, monkeypatch)

    # Add a third evaluation row for a DOI that has no matching summaries.jsonl entry.
    with open(evaluations_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "doi": "10.1111/test.orphan",
            "summarizer": "openai",
            "judge": "anthropic",
            "input_source": "processed",
            "rubric_version": "vet_medhelm_score_v1.0",
            "jury_score": 3.0,
            "requires_human_review": True,
            "strata": {"journal": "JVIM", "input_source": "processed"},
            "timestamp": "2026-01-01T00:00:00+00:00",
        }) + "\n")

    result = export_human_review(
        reviewers=1,
        sample_size=3,
        seed=42,
        evaluations_path=evaluations_path,
        output_dir=tmp_path / "human_review",
        summaries_path=summaries_path,
        summaries_txt_dir=tmp_path / "no_summaries_txt",
        dev_tests_summaries_txt_dir=tmp_path / "no_dev_summaries_txt",
    )

    assert result.items_exported == 2
    assert result.skipped_rows == 1


def test_export_human_review_rejects_nonpositive_reviewers_or_sample_size(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(ValueError):
        export_human_review(reviewers=0, evaluations_path=tmp_path / "evaluations.jsonl")
    with pytest.raises(ValueError):
        export_human_review(sample_size=0, evaluations_path=tmp_path / "evaluations.jsonl")


def test_cli_main_reports_failure_when_nothing_to_export(tmp_path: Path) -> None:
    empty_evaluations = tmp_path / "evaluations.jsonl"
    exit_code = human_review.main([
        "--evaluations", str(empty_evaluations),
        "--output-dir", str(tmp_path / "human_review"),
    ])
    assert exit_code == 1
