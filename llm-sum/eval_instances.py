"""
llm-sum/eval_instances.py — MedHELM-style evaluation instances
===============================================================

WHY THIS MODULE EXISTS
----------------------
MedHELM separates "building benchmark instances" from "calling models" and
"scoring outputs". This module brings that separation into the veterinary
pipeline without adopting HELM as a dependency.

Each instance is one paper-summary pair plus the metadata needed for
stratified analysis. Keeping this join in one place prevents evaluator.py from
growing into a mix of file parsing, prompt construction, API calls, and
analysis logic.

In plain English:
This file is a "matchmaker". For every paper, up to three AI models each
wrote a summary. This module walks through the saved summaries, finds the
real cleaned article text that goes with each one, tags the pair with useful
labels (species, study design, journal, etc. — used to break results down
into subgroups later), and hands back one tidy bundle per (paper, AI model)
pair. Those bundles are what evaluator.py later sends to an AI "judge" to be
scored.
"""

from __future__ import annotations
# This line tells Python to treat type hints (the "-> str" style notes on
# what a function returns/expects, explained more below) as plain text
# rather than evaluating them immediately when the file is loaded. It's a
# technical parsing detail — it doesn't change what the code actually does.

from _bootstrap import *  # noqa: F401,F403
# Pulls in setup shared by every llm-sum script — for example DATA_DIR, the
# folder path where files under data/ live — so all scripts agree on where
# things are stored. The "# noqa" comments just tell automated style
# checkers to ignore this unusual "import everything" pattern; it's
# intentional here.

from dataclasses import dataclass
# A "dataclass" is a Python shortcut for defining a simple bundle of related
# values (a bit like a labeled folder holding a fixed set of named slots)
# without hand-writing the repetitive code that stores, compares, and prints
# those slots. It's used below for EvaluationInstance.
import json
from pathlib import Path
# `Path` is an object for working with file/folder locations, used instead
# of plain text strings so code can do things like "does this file exist?"
# or "list every .txt file in this folder" directly.
from typing import Any, Iterable
# `Any` and `Iterable` are "type hints" — notes for human readers (and some
# tools) about what kind of value a function expects or returns; Python does
# not enforce them at runtime. `Any` means "could be anything at all".
# `Iterable` means "something you can loop over one item at a time", such as
# a list or a generator (generators are explained below at `iter_jsonl`).

from prepare_texts import read_cached_text  # noqa: E402
# Brings in the helper (defined in prepare_texts.py) that looks up a paper's
# cleaned, previously-saved article text from the local on-disk cache.

# File locations for the saved AI summaries and the two manifest (paper
# metadata) files. DATA_DIR itself comes from the `_bootstrap` import above.
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
MANUAL_MANIFEST_PATH = DATA_DIR / "manual_manifest.jsonl"

# The subgroup categories ("strata") used later to slice results — e.g. "how
# did the AI do on cardiology papers vs. oncology papers?" This is a tuple
# (a fixed, unchangeable ordered list) naming those grouping fields.
STRATIFICATION_FIELDS = ("species", "study_design", "clinical_topic", "journal", "input_source")


@dataclass(frozen=True)
class EvaluationInstance:
    """One benchmark instance: a cleaned article and one candidate summary.

    A frozen dataclass is used instead of a loose dict so tests and downstream
    code get stable field names. The original summary and manifest records are
    still retained for compatibility with existing path-resolution helpers.

    In plain English: this is one "packet" ready to be judged — the real
    article text, the AI-written summary of it, which AI model wrote that
    summary, and a handful of descriptive labels. "frozen" means that once a
    packet is created, its fields cannot be accidentally changed afterward,
    which helps prevent bugs where something quietly edits data it shouldn't.
    """

    doi: str  # The paper's unique ID (Digital Object Identifier).
    summarizer: str  # Which AI model wrote this particular summary (e.g. "openai").
    reference_text: str  # The real, cleaned article text used as the "ground truth".
    candidate_summary: str  # The AI-written summary being evaluated against the reference text.
    input_source: str  # Where the reference text came from (e.g. "processed" text vs. raw PDF).
    strata: dict[str, Any]  # The subgroup labels (species, study design, etc.) for this paper.
    summary_record: dict[str, Any]  # The full original row from summaries.jsonl, kept for reference.
    manifest_record: dict[str, Any]  # The full matching row from the manifest file, kept for reference.


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield valid JSON objects from a JSONL file and skip malformed lines.

    JSONL is append-safe, so a partially written final line should not stop the
    whole evaluation pass. Skipping malformed rows matches the rest of the
    pipeline's corruption-resilient style.

    In plain English: "JSONL" means a text file with one JSON record per
    line (as opposed to one big JSON document covering the whole file). This
    function opens such a file and hands back each line's data one at a
    time, quietly skipping any line that's empty or broken rather than
    crashing the whole program.

    This is a "generator" function because it uses `yield` instead of
    `return`. A generator hands out results one at a time, on demand, like
    dealing cards one at a time from a deck — instead of reading the whole
    file into memory and building one giant list up front. That keeps memory
    use low even for a very large file.
    """
    if not path.exists():
        # No file on disk yet? There is nothing to hand back, so we simply
        # stop here (an empty generator that yields nothing).
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue  # Skip blank lines.
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # Skip a line that isn't valid JSON (e.g. cut off mid-write).
            if isinstance(obj, dict):
                yield obj  # Only hand back dictionary-shaped ("object") records.


def read_doi_from_dev_folder_file(path: Path) -> str | None:
    """Return the single real DOI recorded in one dev-mode readable .txt file.

    Both data/dev_summaries_jsonl/ and data/dev_evals_jsonl/ write one .txt per
    paper whose first non-empty line is ``DOI: <doi>`` (see summarizer's
    ``_format_dev_summary_entry_as_text`` and evaluator's
    ``_format_dev_eval_entry_as_text``). We parse that header rather than the
    filename on purpose: dev files are written as ``<stem>__<run_suffix>.txt``,
    so the same DOI can reappear under a new timestamped suffix. Keying off the
    DOI in the header keeps a re-generated paper recognised as already-done;
    keying off the filename stem would wrongly treat it as new.

    Returns None if no DOI header is present. A per-file companion to
    :func:`read_dois_from_dev_folder`, used when the caller needs to know which
    specific file corresponds to a DOI (e.g. hashing only the dev-summary files
    that seed a run, or scoping a pilot review to the dev pool). Lives here in
    eval_instances (not run_phase3) so both run_phase3 and pilot_human_review
    share one implementation and cannot drift apart.

    In plain English: each human-readable dev .txt file starts with a line
    like "DOI: 10.1234/example". This function opens one such file, reads
    just that first line, and returns the DOI written there — or ``None`` if
    it can't find one. It's used to figure out "which paper is this file
    about?" without needing to guess from the filename.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue  # Skip any leading blank lines before the header.
                if stripped.lower().startswith("doi:"):
                    doi = stripped.split(":", 1)[1].strip()
                    if doi and doi.lower() not in {"not recorded", "none"}:
                        return doi
                # Only the header block matters; stop at the first non-empty
                # line so a "DOI:" appearing later in prose is never picked up.
                return None
    except OSError:
        # File missing, unreadable, permissions issue, etc. — treat the same
        # as "no DOI found" rather than crashing.
        return None
    return None


def read_dois_from_dev_folder(folder: Path) -> set[str]:
    """Return the set of real DOIs recorded in a dev-mode readable folder.

    Reads every ``*.txt`` file's ``DOI: <doi>`` header via
    :func:`read_doi_from_dev_folder_file`. A missing folder (or a file without a
    parseable DOI line) contributes nothing rather than raising, matching the
    corruption-resilient style used across the pipeline.

    In plain English: this looks at every readable .txt file in a dev-mode
    output folder (like data/dev_summaries_jsonl/) and collects the DOI of
    each paper it finds into one set (a collection with no duplicates). It's
    how the pipeline knows "which papers have already been processed in dev
    mode?" so it can skip redoing them.
    """
    dois: set[str] = set()
    if not folder.exists():
        return dois  # No folder yet means no DOIs have been recorded.
    for path in sorted(folder.glob("*.txt")):
        # `folder.glob("*.txt")` lists every file in the folder ending in
        # .txt; sorting keeps the order predictable/repeatable.
        doi = read_doi_from_dev_folder_file(path)
        if doi:
            dois.add(doi)
    return dois


def load_manifest_index(
    manifest_path: Path | None = None,
    manual_manifest_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return a DOI-keyed metadata index from OA and manual manifests.

    The OA manifest is loaded first because it is generated by the pipeline and
    usually has the richest covariates. Manual rows fill gaps without replacing
    already-seen OA records.

    In plain English: this builds a lookup table (a dictionary/"index") that
    lets other code say "give me the metadata row for DOI X" instantly,
    instead of scanning the whole manifest file each time. It reads the
    automatically generated manifest first; a paper's manual manifest entry
    is only used to fill in a DOI that wasn't already found, never to
    overwrite one that was.
    """
    index: dict[str, dict[str, Any]] = {}
    for path in (
        manifest_path if manifest_path is not None else MANIFEST_PATH,
        manual_manifest_path if manual_manifest_path is not None else MANUAL_MANIFEST_PATH,
    ):
        # First loop iteration processes the OA manifest, second processes
        # the manual manifest — in that fixed order.
        for row in iter_jsonl(path):
            doi = str(row.get("doi", "")).strip()
            if doi and doi not in index:
                # Only add this row if we have a DOI and haven't already
                # recorded one for it (so the manifest read first "wins").
                index[doi] = row
    return index


def _first_present(*values: Any, default: Any = "") -> Any:
    """Return the first non-empty value while preserving list covariates."""
    # `*values` means this function can accept any number of positional
    # arguments, collected together into one tuple called `values` — handy
    # here because we want to check several possible sources in priority
    # order without hard-coding how many there are.
    # In plain English: give this function a list of candidate values (e.g.
    # "check the summary row first, then the manifest row"), and it returns
    # whichever one is filled in first, or the `default` if none of them are.
    for value in values:
        if value not in (None, "", []):
            return value
    return default


def build_strata(summary_record: dict[str, Any], manifest_record: dict[str, Any]) -> dict[str, Any]:
    """Build the clinically meaningful subgroup labels for an instance.

    Summary rows sometimes carry journal/input metadata, while manifest rows
    carry research covariates. We merge both so reporting does not depend on
    which upstream stage happened to include a field.

    In plain English: this creates the small set of labels used to slice
    results later — what species, what kind of study, what clinical topic,
    which journal, and where the source text came from. For each label it
    checks the summary row first and falls back to the manifest row, so it
    doesn't matter which file happened to record the information.
    """
    input_source = str(summary_record.get("input_source") or "processed")
    return {
        "species": _first_present(summary_record.get("species"), manifest_record.get("species"), default=[]),
        "study_design": _first_present(summary_record.get("study_design"), manifest_record.get("study_design"), default="unknown"),
        "clinical_topic": _first_present(summary_record.get("clinical_topic"), manifest_record.get("clinical_topic"), default="unknown"),
        "journal": _first_present(summary_record.get("journal"), manifest_record.get("journal"), default="unknown"),
        "input_source": input_source,
    }


def iter_evaluation_instances(
    summaries_path: Path | None = None,
    manifest_path: Path | None = None,
    manual_manifest_path: Path | None = None,
    doi_filter: set[str] | None = None,
    paper_limit: int | None = None,
) -> Iterable[EvaluationInstance]:
    """Yield one instance per successful summary slot.

    The paper limit is applied before expanding a paper into model summaries,
    matching evaluator.py's existing behavior. That keeps dev/single runs
    budget-predictable while still scoring all successful models for each
    selected paper.

    In plain English: this is the main function in the file. It walks
    through every saved paper in summaries.jsonl, and for each one that
    qualifies (has a DOI, passes any DOI filter, and we haven't hit the
    paper limit yet), it looks up that paper's real cleaned text and its
    manifest metadata, then hands out one `EvaluationInstance` bundle for
    every AI model that successfully produced a summary for that paper. Like
    `iter_jsonl` above, this is a generator (it uses `yield`), so instances
    are produced one at a time as the caller asks for them, rather than all
    being built and held in memory at once.
    """
    summary_file = summaries_path if summaries_path is not None else SUMMARIES_PATH
    manifest_index = load_manifest_index(manifest_path, manual_manifest_path)
    seen_papers = 0

    for summary_record in iter_jsonl(summary_file):
        doi = str(summary_record.get("doi", "")).strip()
        if not doi:
            continue  # Skip any row that has no DOI at all — nothing to key it by.
        if doi_filter is not None and doi not in doi_filter:
            continue  # If a specific set of DOIs was requested, skip everything else.
        if paper_limit is not None and seen_papers >= paper_limit:
            break  # We've already produced instances for enough papers — stop entirely.

        # Look up this paper's extra metadata (species, study design, etc.)
        # from the manifest index built above; use an empty dict if missing.
        manifest_record = manifest_index.get(doi, {})
        input_source = str(summary_record.get("input_source") or "processed")
        # Try to load the actual cleaned article text that matches this
        # summary. If it's not available in the cache, this paper can't be
        # evaluated, so skip it entirely (it does not count toward the limit).
        reference_text = read_cached_text(summary_record, input_source=input_source)
        if reference_text is None:
            continue

        seen_papers += 1  # Count this paper now that we know it's usable.
        # Build the subgroup labels once per paper — they're the same for
        # every AI model's summary of this paper, so no need to repeat the work.
        strata = build_strata(summary_record, manifest_record)
        for summarizer, slot in (summary_record.get("models") or {}).items():
            # `summary_record["models"]` is a dict keyed by AI model name
            # (e.g. "openai", "anthropic"), each holding that model's result
            # for this paper. Loop through each model's result in turn.
            if not isinstance(slot, dict):
                continue  # Skip anything that isn't in the expected shape.
            if slot.get("status") != "success" or not slot.get("summary"):
                continue  # Skip models that failed or produced no summary text.
            yield EvaluationInstance(
                doi=doi,
                summarizer=str(summarizer),
                reference_text=reference_text,
                candidate_summary=str(slot["summary"]),
                input_source=input_source,
                strata=strata,
                summary_record=summary_record,
                manifest_record=manifest_record,
            )
