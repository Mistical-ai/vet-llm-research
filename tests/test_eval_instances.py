"""
tests/test_eval_instances.py — checks for llm-sum/eval_instances.py
=====================================================================

In plain English: this file contains an automated test. A test is a small
script that sets up a fake, tiny version of the real data, runs the actual
code being checked, and then asserts ("insists") that the result looks
exactly the way it should. If any `assert` line turns out false, the test
fails loudly, which is how bugs get caught before they reach real data. This
particular test makes sure `eval_instances.iter_evaluation_instances`
correctly joins a saved AI summary with its matching article text and
metadata labels — no real files, network calls, or paid APIs are involved;
everything is written to a temporary, throwaway folder for the duration of
the test.
"""

from __future__ import annotations
# See eval_instances.py for what this line does; in short, it's a parsing
# detail related to type hints and does not affect runtime behaviour.

import json
from pathlib import Path
# `Path` represents a file/folder location as an object rather than a plain
# string — used here to build paths inside the temporary test folder.

import eval_instances
import prepare_texts
from file_paths import doi_to_slug
# `doi_to_slug` turns a DOI (e.g. "10.1111/test.instances") into a
# filesystem-safe short name ("slug") — the same naming scheme the real
# pipeline uses when it saves one cache file per paper.


def test_iter_evaluation_instances_joins_summary_text_and_covariates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # `tmp_path` and `monkeypatch` are pytest "fixtures" — ready-made helper
    # objects that pytest automatically creates and hands to any test
    # function that asks for them by name (just by listing them as
    # parameters). `tmp_path` is a brand-new, empty temporary folder that
    # exists only for this one test and is cleaned up afterward. `monkeypatch`
    # lets a test temporarily override a value elsewhere in the code (here,
    # a file path) and automatically restores the original value when the
    # test ends, so tests don't interfere with each other or the real code.
    #
    # In plain English, this test: (1) builds a fake summaries.jsonl,
    # manifest.jsonl, manual_manifest.jsonl, and one cached article-text
    # file inside the temporary folder; (2) points the code at those fake
    # files instead of the real data/ files; (3) runs
    # iter_evaluation_instances(); and (4) checks that it produced exactly
    # one correctly-joined EvaluationInstance.
    doi = "10.1111/test.instances"
    slug = doi_to_slug(doi)
    summaries = tmp_path / "summaries.jsonl"
    manifest = tmp_path / "manifest.jsonl"
    manual_manifest = tmp_path / "manual_manifest.jsonl"
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    # Write one fake paper into summaries.jsonl with two AI models' results:
    # "openai" succeeded with a summary, but "anthropic" failed (empty
    # summary). Only the successful one should turn into an instance later.
    summaries.write_text(json.dumps({
        "doi": doi,
        "journal": "JVIM",
        "models": {
            "openai": {"status": "success", "summary": "Dogs improved after treatment."},
            "anthropic": {"status": "failed", "summary": ""},
        },
    }) + "\n", encoding="utf-8")
    # Write the matching manifest row with the subgroup metadata (species,
    # study design, clinical topic, journal) that should get picked up and
    # attached to the resulting instance's `strata`.
    manifest.write_text(json.dumps({
        "doi": doi,
        "species": ["Canine"],
        "study_design": "RCT",
        "clinical_topic": "Cardiology",
        "journal": "Journal of Veterinary Internal Medicine",
    }) + "\n", encoding="utf-8")
    # The manual manifest is left empty on purpose — this test only checks
    # the OA-manifest path of load_manifest_index.
    manual_manifest.write_text("", encoding="utf-8")
    # Write the fake cached article text for this paper, using the same
    # slug-based filename the real cache uses, so read_cached_text() can
    # find it.
    (processed_dir / f"{slug}.jsonl").write_text(
        json.dumps({"doi": doi, "slug": slug, "text": "Full cleaned article text."}) + "\n",
        encoding="utf-8",
    )

    # The cache reader uses prepare_texts.PROCESSED_DIR, so patch that module
    # rather than duplicating path logic in the instance builder.
    # In plain English: this temporarily tells the real cache-reading code
    # "look in our fake temp folder instead of the real data/processed/
    # folder", just for the duration of this test.
    monkeypatch.setattr(prepare_texts, "PROCESSED_DIR", processed_dir)

    # Run the actual function being tested, pointed at our fake files
    # instead of the real data/ files. `list(...)` collects everything the
    # generator yields into an ordinary list so it can be inspected below.
    instances = list(eval_instances.iter_evaluation_instances(
        summaries_path=summaries,
        manifest_path=manifest,
        manual_manifest_path=manual_manifest,
    ))

    # Now check the results. Each `assert` line insists on one fact; if any
    # of them is false, the test fails and reports which line broke.
    assert len(instances) == 1  # Only the successful "openai" summary should produce an instance.
    instance = instances[0]
    assert instance.doi == doi
    assert instance.summarizer == "openai"
    assert instance.reference_text == "Full cleaned article text."
    assert instance.strata["species"] == ["Canine"]
    assert instance.strata["study_design"] == "RCT"
    assert instance.strata["clinical_topic"] == "Cardiology"
