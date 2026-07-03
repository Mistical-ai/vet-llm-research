from __future__ import annotations

import json
from pathlib import Path

import eval_instances
import prepare_texts
from file_paths import doi_to_slug


def test_iter_evaluation_instances_joins_summary_text_and_covariates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    doi = "10.1111/test.instances"
    slug = doi_to_slug(doi)
    summaries = tmp_path / "summaries.jsonl"
    manifest = tmp_path / "manifest.jsonl"
    manual_manifest = tmp_path / "manual_manifest.jsonl"
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    summaries.write_text(json.dumps({
        "doi": doi,
        "journal": "JVIM",
        "models": {
            "openai": {"status": "success", "summary": "Dogs improved after treatment."},
            "anthropic": {"status": "failed", "summary": ""},
        },
    }) + "\n", encoding="utf-8")
    manifest.write_text(json.dumps({
        "doi": doi,
        "species": ["Canine"],
        "study_design": "RCT",
        "clinical_topic": "Cardiology",
        "journal": "Journal of Veterinary Internal Medicine",
    }) + "\n", encoding="utf-8")
    manual_manifest.write_text("", encoding="utf-8")
    (processed_dir / f"{slug}.jsonl").write_text(
        json.dumps({"doi": doi, "slug": slug, "text": "Full cleaned article text."}) + "\n",
        encoding="utf-8",
    )

    # The cache reader uses prepare_texts.PROCESSED_DIR, so patch that module
    # rather than duplicating path logic in the instance builder.
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    instances = list(eval_instances.iter_evaluation_instances(
        summaries_path=summaries,
        manifest_path=manifest,
        manual_manifest_path=manual_manifest,
    ))

    assert len(instances) == 1
    instance = instances[0]
    assert instance.doi == doi
    assert instance.summarizer == "openai"
    assert instance.reference_text == "Full cleaned article text."
    assert instance.strata["species"] == ["Canine"]
    assert instance.strata["study_design"] == "RCT"
    assert instance.strata["clinical_topic"] == "Cardiology"
