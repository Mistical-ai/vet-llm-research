"""
llm-sum/run_phase3.py — Phase 3 orchestrator CLI
==================================================

One CLI for every Phase 3 step:
    extract     Build / refresh the data/processed/*.jsonl cache from PDFs.
    summarize   Run the three summarisers; supports --estimate, --resume, --force.
    summarize-all
                Run paired raw-PDF and processed-text summaries, producing
                readable text files with three provider outputs per source.
    evaluate    Run the blind judge. Reads data/summaries.jsonl by default.
                --mode dev instead judges only the papers in
                data/dev_summaries_jsonl/ (written by `summarize --mode dev`) and
                mirrors the scores into data/dev_evals_jsonl/, skipping papers
                already evaluated there. Set EVAL_INPUT_MODE (or pass
                --input-mode) to instead judge summarize-all's
                data/dev_tests/summaries_txt or data/summaries_txt comparison files.
    eval-report Summarize MedHELM-style evaluation rows by clinical strata.
                --publication emits paper-ready tables (report_tables.py).
    report-figures
                Render publication figures (PNG/SVG) and export a named,
                taxonomy-keyed leaderboard (JSON + Markdown) from
                data/evaluations.jsonl (see docs/phase6/reporting.md).
    export-human-review
                Sample judged (paper, summary) pairs from data/evaluations.jsonl
                and export blind review packets for one or more human
                reviewers (see docs/phase5/human_validation.md).
    ingest-human-review
                Read filled-in reviewer scoresheets back into
                data/human_reviews.jsonl; eval-report then reports inter-reviewer
                agreement and human-vs-jury correlation.
    status      Print counts: extracted / summarised / evaluated / budget.
    clean       Remove temporary data/batch/*.jsonl scratch files.

Each subcommand delegates to the relevant module's main() so the modules
remain independently testable.
"""

from __future__ import annotations

from _bootstrap import *  # noqa: F401,F403

import argparse
import json
import os
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from models_config import all_providers, get_model_spec  # noqa: E402
from phase3_mode import resolve_mode, VALID_MODES  # noqa: E402
from run_manifest import (  # noqa: E402
    build_run_manifest,
    create_run_id,
    finalize_run_manifest,
    resolve_model_versions,
    sha256_file,
    sha256_file_or_empty,
    sha256_files_combined,
    write_run_manifest,
)
from scenarios import VET_TAXONOMY_V1, VeterinarySummaryQualityScenario  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
RESULTS_DIR = DATA_DIR / "results"
# Deliberately NOT derived from PROCESSED_DIR: PROCESSED_DIR_NAME lets you
# point the processed-text cache at an alternate folder (e.g. "processedv2"
# for an extraction experiment) without evaluate's run_manifest_*.json files
# following it there. Manifests always live in one stable place.
RUN_MANIFEST_DIR = DATA_DIR / "run_manifests"
RAW_DIR = DATA_DIR / "raw"
SUMMARIES_PDF_DIR = DATA_DIR / "summaries_pdf"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"
DEV_TESTS_DIR = DATA_DIR / "dev_tests"
DEV_TESTS_SUMMARIES_PDF_DIR = DEV_TESTS_DIR / "summaries_pdf"
DEV_TESTS_SUMMARIES_TXT_DIR = DEV_TESTS_DIR / "summaries_txt"
# Readable sibling of data/summaries.jsonl for just the dev-mode batch: one
# .txt per journal-random-selected paper, jsonl/raw_text only, no PDF
# involved. Distinct from DEV_TESTS_DIR, which is summarize-all's PDF-vs-
# processed-text *comparison* output — see .env.template section 11 for the
# full explanation of when to use which folder.
DEV_SUMMARIES_JSONL_DIR = DATA_DIR / "dev_summaries_jsonl"
# Readable sibling of data/summaries.jsonl for `summarize --mode single` runs
# (a one-paper smoke test of the live pipeline/prompt). Single mode doesn't
# pre-select a DOI the way dev mode does — it just walks the manifest and
# stops after paper_limit papers — so cmd_summarize figures out which DOI(s)
# actually got (re)written this run via `_dois_touched_since` instead.
SINGLE_SUMMARIES_JSONL_DIR = DATA_DIR / "single_summaries_jsonl"
# Readable sibling of data/summaries.jsonl for `summarize --mode batch` runs.
# Written by check_batch_status.merge_summarisation_results as each provider's
# results merge back, so the full 250-paper corpus ends up with the same
# bullet + prose/ pair that dev and single runs produce. Judge it with
# `evaluate --mode dev --source-dir data/batch_summaries_jsonl`.
BATCH_SUMMARIES_JSONL_DIR = DATA_DIR / "batch_summaries_jsonl"
# Readable judge-side sibling of DEV_SUMMARIES_JSONL_DIR: `evaluate --mode dev`
# writes one .txt per judged dev paper here (see _cmd_evaluate_from_dev_jsonl).
# data/evaluations.jsonl stays the append-only source of truth; this is the
# eyeball-it mirror. Distinct from data/dev_tests/ (summarize-all comparison).
DEV_EVALS_JSONL_DIR = DATA_DIR / "dev_evals_jsonl"
# Deep-dive sibling of DEV_EVALS_JSONL_DIR: same DOIs, but one Markdown file
# per paper showing every judge's per-criterion scores/reasoning, automatic
# metrics, and cross-judge agreement stats -- see write_dev_detail_eval_outputs.
DEV_DETAIL_EVAL_REPORTS_DIR = DATA_DIR / "dev_detailEval_reports"
SUMMARIZE_ALL_OUTPUT_SETS = ("auto", "regular", "dev-tests")
EVAL_INPUT_MODES = ("jsonl", "auto", "dev", "regular", "dev-jsonl")
# "No per-journal cap" sentinel for cmd_evaluate's journal-stratified sampling
# below. A large int (not float("inf")) so run-manifest slice_config stays
# cleanly JSON-serializable; _sample_by_journal's min(per_journal, len(entries))
# already does the right thing with a large sentinel.
_NO_PER_JOURNAL_CAP = 10 ** 9


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var with conservative, explicit true/false parsing."""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    print(f"[phase3] WARNING: invalid {name}={raw!r}; using {default}.")
    return default


def _summarize_all_random_match() -> bool:
    """Whether summarize-all samples matched article pairs randomly."""
    return _env_bool("SUMMARIZE_ALL_RANDOM_MATCH", True)


def _summarize_all_unique_output() -> bool:
    """Whether summarize-all writes timestamped files instead of stem.txt."""
    return _env_bool("SUMMARIZE_ALL_UNIQUE_OUTPUT", True)


def _resolve_eval_input_mode(mode_name: str, cli_override: str | None) -> str:
    """Resolve which summary source `evaluate` reads from.

    Precedence: --input-mode (CLI) > dev-mode default > EVAL_INPUT_MODE (.env)
    > 'jsonl' default.

    Dev mode is special: when no --input-mode is passed on the CLI, PHASE3_MODE=dev
    resolves to 'dev-jsonl' REGARDLESS of EVAL_INPUT_MODE. That flow reads the
    readable dev-summary folder (data/dev_summaries_jsonl/) written by
    `summarize --mode dev` and writes results to data/dev_evals_jsonl/ — this is
    the folder-driven dev loop. Anyone who still wants the old behaviour for a dev
    run can pass it explicitly on the CLI: `--input-mode jsonl` restores the
    journal-stratified data/summaries.jsonl pipeline, and `--input-mode dev`
    restores the summarize-all data/dev_tests comparison path.

    'auto' expands based on the active PHASE3_MODE, mirroring
    _summarize_all_output_dirs()'s existing auto behaviour: PHASE3_MODE=dev
    reads the dev_tests processed-text folder, every other mode reads the
    regular one. This is what makes 'switch to the regular folder once
    you're ready for the full corpus' a plain PHASE3_MODE change instead of
    an extra edit.
    """
    if cli_override is None and mode_name == "dev":
        return "dev-jsonl"
    raw = (cli_override or os.getenv("EVAL_INPUT_MODE", "jsonl")).strip().lower()
    if raw == "auto":
        return "dev" if mode_name == "dev" else "regular"
    if raw in ("jsonl", "dev", "regular", "dev-jsonl"):
        return raw
    print(f"[phase3] WARNING: invalid EVAL_INPUT_MODE={raw!r}; using 'jsonl'. "
          f"Valid: {EVAL_INPUT_MODES}")
    return "jsonl"


def _read_dois_from_dev_folder(folder: Path) -> set[str]:
    """Return the set of real DOIs recorded in a dev-mode readable folder.

    Both data/dev_summaries_jsonl/ and data/dev_evals_jsonl/ write one .txt per
    paper whose first line is ``DOI: <doi>`` (see summarizer's
    ``_format_dev_summary_entry_as_text`` and evaluator's
    ``_format_dev_eval_entry_as_text``). We parse that header rather than the
    filename on purpose: dev files are written as ``<stem>__<run_suffix>.txt``,
    so the same DOI can reappear under a new timestamped suffix. Keying skip
    logic off the DOI in the header keeps a re-generated paper recognised as
    already-done; keying off the filename stem would wrongly treat it as new.

    A missing folder (or a file without a parseable DOI line) contributes
    nothing rather than raising, matching the corruption-resilient style used
    across the pipeline.
    """
    dois: set[str] = set()
    if not folder.exists():
        return dois
    for path in sorted(folder.glob("*.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.lower().startswith("doi:"):
                        doi = stripped.split(":", 1)[1].strip()
                        if doi and doi.lower() not in {"not recorded", "none"}:
                            dois.add(doi)
                    # Only the header block matters; stop at the first non-empty
                    # line so a "DOI:" appearing later in prose is never picked up.
                    break
        except OSError:
            continue
    return dois


def _warn_about_unread_subfolders(folder: Path) -> list[str]:
    """Warn when ``folder`` has subdirectories holding .txt files we won't read.

    ``_read_dois_from_dev_folder`` globs ``*.txt`` non-recursively, which is
    deliberate: ``prose/`` is the readable prose sibling of the same papers and
    must never be counted twice. But that same non-recursion silently hides a
    ``summarize --mode dev --output-subdir NAME`` pool, whose papers then look
    like they were never summarised at all. Naming the folders here turns a
    silent no-op into an obvious "you probably meant --source-dir".

    Returns the subfolder names warned about (empty when there's nothing to
    say), so callers and tests can assert on it.
    """
    if not folder.exists():
        return []
    skipped = sorted(
        child.name for child in folder.iterdir()
        if child.is_dir() and child.name != "prose" and any(child.glob("*.txt"))
    )
    if skipped:
        print(f"[phase3] NOTE: {folder} has subfolder(s) with readable summaries "
              f"that are NOT part of this run: {', '.join(skipped)}. "
              f"Only top-level *.txt files are read. To use one of them, pass "
              f"--source-dir {folder / skipped[0]}")
    return skipped


def _dois_touched_since(
    summaries_path: Path, since: datetime, input_source: str,
) -> set[str]:
    """Return DOIs in ``summaries_path`` that got a provider result timestamped
    at/after ``since``, restricted to entries matching ``input_source``.

    `summarize --mode single` doesn't pre-select a DOI the way dev mode does
    (see ``SINGLE_SUMMARIES_JSONL_DIR``) — it just walks the manifest and
    stops after ``paper_limit`` papers. This is how ``cmd_summarize`` figures
    out, after the fact, which paper(s) that run actually touched so it can
    render them into the readable single-summaries folder.
    """
    touched: set[str] = set()
    if not summaries_path.exists():
        return touched
    with open(summaries_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(entry.get("input_source") or "processed") != input_source:
                continue
            doi = str(entry.get("doi", "")).strip()
            if not doi:
                continue
            models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
            for result in models.values():
                ts_raw = result.get("timestamp") if isinstance(result, dict) else None
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                if ts >= since:
                    touched.add(doi)
                    break
    return touched


def _summarize_all_output_set() -> str:
    """
    Read the default summarize-all output set from the environment.

    Keeping this in ``.env`` makes repeated dev runs reproducible without
    needing to remember an extra CLI flag. Underscores are accepted because env
    values are often copied from variable-style names.
    """
    raw = os.getenv("SUMMARIZE_ALL_OUTPUT_SET", "auto").strip().lower()
    normalized = raw.replace("_", "-")
    if normalized in SUMMARIZE_ALL_OUTPUT_SETS:
        return normalized
    print(
        f"[phase3] WARNING: invalid SUMMARIZE_ALL_OUTPUT_SET={raw!r}; "
        "using 'auto'."
    )
    return "auto"


def _summarize_all_output_dirs(
    profile_name: str,
    output_set: str = "auto",
) -> tuple[Path, Path, str]:
    """
    Return the PDF/text output folders for a summarize-all run.

    ``auto`` keeps normal single/test runs in the original folders, while dev
    mode writes under ``data/dev_tests`` so paid prompt experiments do not mix
    with regular comparison outputs. The explicit output set exists for the
    cases where the user wants to override that default for one command.
    """
    if output_set not in SUMMARIZE_ALL_OUTPUT_SETS:
        raise ValueError(
            f"Unknown summarize-all output set {output_set!r}; "
            f"valid values: {', '.join(SUMMARIZE_ALL_OUTPUT_SETS)}"
        )

    resolved_set = "dev-tests" if output_set == "auto" and profile_name == "dev" else output_set
    if resolved_set == "auto":
        resolved_set = "regular"

    if resolved_set == "dev-tests":
        return DEV_TESTS_SUMMARIES_PDF_DIR, DEV_TESTS_SUMMARIES_TXT_DIR, resolved_set
    return SUMMARIES_PDF_DIR, SUMMARIES_TXT_DIR, resolved_set


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def cmd_extract(args: argparse.Namespace) -> int:
    # Extract is mode-agnostic (no API calls), but we still print the
    # banner so the user knows what the next steps will do.
    print(resolve_mode(args.mode).banner())
    from prepare_texts import main as prepare_main
    return prepare_main(["--manifest", str(args.manifest)] +
                        (["--limit", str(args.limit)] if args.limit else []))


# ---------------------------------------------------------------------------
# summarize  (with --estimate short-circuit)
# ---------------------------------------------------------------------------

def cmd_summarize(args: argparse.Namespace) -> int:
    profile = resolve_mode(args.mode)
    print(profile.banner())

    # Visible confirmation of which text cache this run actually reads from —
    # PROCESSED_DIR_NAME in .env.template controls this for every mode
    # (test/single/dev/batch alike), so this line is the easiest way to
    # confirm a dev or batch run is really pointed at data/processedv2.
    if args.input_source == "processed":
        print(f"[phase3:summarize] processed-text source: {PROCESSED_DIR}")
    elif args.input_source == "raw_text":
        print(f"[phase3:summarize] raw-text source: {RAW_TEXT_DIR}")

    if args.input_source == "pdf" and profile.name not in {"test", "single"}:
        print("[phase3:summarize] direct PDF input is limited to test/single comparison runs.")
        return 1
    if args.input_source == "pdf" and profile.use_batch:
        print("[phase3:summarize] direct PDF input is real-time only; batch mode is not supported.")
        return 1

    if args.estimate:
        if args.input_source == "pdf":
            # Direct-PDF billing depends on each provider's file-ingestion path,
            # so an offline tokenizer cannot predict it reliably. The live
            # single-paper run records real token counts and cost after each call.
            print("[phase3:estimate] Direct PDF cost cannot be estimated offline. "
                  "Run single mode to record real token counts from the providers.")
            return 1
        # Import lazily so a missing tiktoken / SDK doesn't break other commands.
        from cost_estimator import run as run_estimate
        from evaluator import resolve_judges
        # Estimate against exactly the judges a real run would use (default: the
        # full 3-judge panel), honouring JURY_PRESET so the projected cost isn't
        # silently low by 3x.
        judges = resolve_judges()
        run_estimate(
            batched=profile.use_batch,
            judge_providers=judges,
            input_source=args.input_source,
        )
        return 0

    # Dev-mode journal-random selection: pick one random paper per journal
    # (round-robin for extra picks beyond PHASE3_DEV_LIMIT/--limit) BEFORE any
    # provider is called, instead of just summarizing whichever manifest rows
    # happen to come first. Skipped for pdf input_source (dev never uses it —
    # see the guard above) and for non-dev modes, which keep their existing
    # sequential/full-corpus behavior unchanged.
    doi_filter_dois: set[str] | None = None
    dev_run_suffix: str | None = None
    if profile.name == "dev" and args.input_source != "pdf":
        # --output-subdir redirects this run's readable output (and its
        # incremental skip-existing check) into a subfolder with its own
        # independent pool, without touching the module-level
        # DEV_SUMMARIES_JSONL_DIR constant that other commands (e.g.
        # `evaluate --mode dev`) still read.
        dev_output_dir = (
            DEV_SUMMARIES_JSONL_DIR / args.output_subdir
            if args.output_subdir else DEV_SUMMARIES_JSONL_DIR
        )
        effective_limit = args.limit if args.limit is not None else profile.paper_limit
        manifest_path = args.manifest if args.manifest else MANIFEST_PATH
        by_journal = _load_manifest_by_journal(manifest_path, args.input_source)
        if not by_journal:
            print(f"[phase3:summarize] No journal-mapped papers with cached "
                  f"{args.input_source} text found via {manifest_path}. Run "
                  "'extract' first, or check PROCESSED_DIR_NAME in .env.")
            return 1

        # Incremental dev sampling: drop papers already written to
        # data/dev_summaries_jsonl/ so a re-run picks NEW papers instead of the
        # same ones. --no-skip-existing forces reconsidering them. Selection
        # stays deterministic (seed + the fixed exclusion set); the round-robin
        # sampler just re-draws over the smaller remaining pool per journal.
        if not args.no_skip_existing:
            done = _read_dois_from_dev_folder(dev_output_dir)
            if done:
                by_journal = {
                    journal: [
                        r for r in records
                        if str(r.get("doi", "")).strip() not in done
                    ]
                    for journal, records in by_journal.items()
                }
                by_journal = {j: recs for j, recs in by_journal.items() if recs}
                print(f"[phase3:summarize] skipping {len(done)} paper(s) already in "
                      f"{dev_output_dir}; picking from the remainder.")
                if not by_journal:
                    print("[phase3:summarize] No un-summarised journal-mapped papers "
                          "remain. Pass --no-skip-existing to re-pick already-done "
                          f"papers, or clear {dev_output_dir}.")
                    return 1

        dev_seed = int(os.getenv("PHASE3_DEV_SAMPLE_SEED", "42"))
        selected_records = _sample_round_robin_by_journal(
            by_journal, effective_limit, seed=dev_seed,
        )
        doi_filter_dois = {
            str(r.get("doi", "")).strip() for r in selected_records if r.get("doi")
        }

        print(f"\n[phase3:summarize] Dev-mode journal-random selection "
              f"(seed={dev_seed}, {len(by_journal)} journal(s) eligible, "
              f"input_source={args.input_source}):")
        print(f"  {'Journal':<20}  Title")
        print("  " + "-" * 68)
        for record in sorted(selected_records, key=lambda r: str(r.get("journal", ""))):
            title = str(record.get("title") or "Untitled")
            print(f"  {str(record.get('journal', '')):<20}  {title[:64]}")
        print(f"  TOTAL: {len(doi_filter_dois)} paper(s)")
        if len(doi_filter_dois) < effective_limit:
            print(f"[phase3:summarize] WARNING: selected {len(doi_filter_dois)}/"
                  f"{effective_limit} papers — not enough journals with cached "
                  f"{args.input_source} text for the requested limit.")
        print()
        dev_run_suffix = _summary_run_suffix()

    from summarizer import main as summarize_main
    delegate = ["--mode", profile.name]
    if doi_filter_dois is not None:
        # --doi-filter is the precise selection; --limit would just print a
        # misleading "overrides mode default" line since it isn't what's
        # actually controlling which/how-many papers run here.
        delegate += ["--doi-filter", ",".join(sorted(doi_filter_dois))]
    elif args.limit is not None:
        delegate += ["--limit", str(args.limit)]
    if args.resume:
        delegate.append("--resume")
    if args.force:
        delegate.append("--force")
    if args.providers:
        delegate += ["--providers", args.providers]
    if args.manifest:
        delegate += ["--manifest", str(args.manifest)]
    if args.input_source:
        delegate += ["--input-source", args.input_source]
    if args.guide_summary:
        delegate += ["--guide-summary", str(args.guide_summary)]
    # Captured just before the live/mock call so `_dois_touched_since` (single
    # mode only, below) can tell which paper this specific run touched.
    run_started_at = datetime.now(timezone.utc)
    result = summarize_main(delegate)

    if doi_filter_dois is not None and result == 0:
        # Only render the readable folder once the underlying run actually
        # succeeded — a declined confirmation prompt (or any other non-zero
        # return) means nothing was summarized, so there's nothing to show.
        # Partial per-provider failures within a successful run are fine; the
        # formatter already renders a failed slot as an explicit error line.
        from summarizer import write_dev_summary_jsonl_outputs
        written = write_dev_summary_jsonl_outputs(
            doi_filter_dois,
            output_dir=dev_output_dir,
            input_source=args.input_source,
            output_suffix=dev_run_suffix,
        )
        print(f"[phase3:summarize] wrote {written} readable dev summary "
              f"file(s) to {dev_output_dir}")
    elif profile.name == "single" and result == 0:
        # Single mode has no pre-selected DOI (unlike dev, above) — it just
        # walks the manifest and stops after paper_limit papers — so figure
        # out which paper(s) actually got a fresh provider result this run.
        # Read the live summarizer.SUMMARIES_PATH (not this module's own copy
        # of the constant) so this stays correct under test monkeypatching,
        # which only ever redirects the former.
        import summarizer as summarizer_module
        touched_dois = _dois_touched_since(
            summarizer_module.SUMMARIES_PATH, run_started_at, args.input_source
        )
        if touched_dois:
            from summarizer import write_dev_summary_jsonl_outputs
            written = write_dev_summary_jsonl_outputs(
                touched_dois,
                output_dir=SINGLE_SUMMARIES_JSONL_DIR,
                input_source=args.input_source,
                run_kind="single",
            )
            print(f"[phase3:summarize] wrote {written} readable single summary "
                  f"file(s) to {SINGLE_SUMMARIES_JSONL_DIR}")
        else:
            print("[phase3:summarize] no newly-summarized paper detected; nothing "
                  f"written to {SINGLE_SUMMARIES_JSONL_DIR}.")

    return result


# ---------------------------------------------------------------------------
# evaluate helpers (journal-stratified sampling)
# ---------------------------------------------------------------------------

def _iter_summaries_simple(summaries_path: Path):
    """Minimal summary iterator used by journal-stratified helpers."""
    if not summaries_path.exists():
        return
    with open(summaries_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _load_summaries_by_journal(
    summaries_path: Path, manifest_path: Path
) -> dict[str, list[dict]]:
    """Group summary entries by journal name (lowercased).

    summaries.jsonl entries may not carry a journal field. We build a
    doi→journal map from manifest.jsonl (populated during Phase 2 enrichment)
    and fall back to 'unknown' for DOIs that can't be mapped.
    """
    doi_to_journal: dict[str, str] = {}
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    doi = str(row.get("doi", "")).strip()
                    journal = str(row.get("journal", "")).strip().lower()
                    if doi and journal:
                        doi_to_journal[doi] = journal
                except (json.JSONDecodeError, AttributeError):
                    continue

    by_journal: dict[str, list[dict]] = {}
    for entry in _iter_summaries_simple(summaries_path):
        doi = str(entry.get("doi", "")).strip()
        journal = (
            str(entry.get("journal", "")).strip().lower()
            or doi_to_journal.get(doi, "unknown")
        )
        by_journal.setdefault(journal, []).append(entry)
    return by_journal


def _sample_by_journal(
    by_journal: dict[str, list[dict]], per_journal: int, seed: int = 42
) -> tuple[set[str], dict[str, list[str]]]:
    """Randomly sample per_journal entries from each known journal.

    Returns (doi_filter_set, {journal: [doi, ...]}) — the doi set is passed
    to run_evaluation(doi_filter=...) so only selected articles are scored.
    'unknown' entries are excluded from sampling (see caller for logging).
    """
    rng = random.Random(seed)
    doi_filter: set[str] = set()
    journal_map: dict[str, list[str]] = {}
    for journal, entries in sorted(by_journal.items()):
        if journal == "unknown":
            continue
        sample = rng.sample(entries, min(per_journal, len(entries)))
        dois = [str(e.get("doi", "")).strip() for e in sample if e.get("doi")]
        if dois:
            journal_map[journal] = dois
            doi_filter.update(dois)
    return doi_filter, journal_map


# ---------------------------------------------------------------------------
# summarize dev-mode helpers (journal-random selection, BEFORE any API call)
# ---------------------------------------------------------------------------
#
# These are the manifest-side counterpart to _load_summaries_by_journal /
# _sample_by_journal above: those two sample from summaries.jsonl AFTER
# summarize has already run (used by `evaluate`'s own dev-mode stratified
# sampling). The two below sample from manifest.jsonl BEFORE any provider is
# called, so `summarize --mode dev` can pick which papers to spend money on
# in the first place — one random paper per journal, instead of just taking
# whichever manifest rows happen to come first.

def _load_manifest_by_journal(
    manifest_path: Path, input_source: str
) -> dict[str, list[dict]]:
    """Group manifest records by journal, keeping only records that already
    have cached text on disk for ``input_source``.

    manifest.jsonl includes every DOI ever considered, including ones that
    were never downloaded or whose extraction failed — those have no cached
    text and can't be summarized, so they're dropped here rather than in the
    caller. This is also what makes the noisy one-off manifest rows (a stray
    "Veterinary Record" or "Life" DOI, for example) fall out naturally: they
    were never downloaded, so they never got cached text, so they're never
    eligible for dev-mode selection.
    """
    from prepare_texts import find_cached_jsonl

    by_journal: dict[str, list[dict]] = {}
    if not manifest_path.exists():
        return by_journal
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            journal = str(record.get("journal", "")).strip().lower()
            if not journal:
                continue
            if find_cached_jsonl(record, input_source=input_source) is None:
                continue
            by_journal.setdefault(journal, []).append(record)
    return by_journal


def _sample_round_robin_by_journal(
    by_journal: dict[str, list[dict]], limit: int, seed: int = 42
) -> list[dict]:
    """Pick up to ``limit`` records, one random paper per journal per pass.

    With ``limit`` equal to the number of journals (the common case:
    PHASE3_DEV_LIMIT=5, 5 journals), this returns exactly one random paper per
    journal. If ``limit`` is larger than the number of journals, extra picks
    spill into a second (third, ...) round-robin pass across journals in
    sorted order, so no journal gets a second paper before every journal has
    gotten a first one. If a journal runs out of eligible papers it's simply
    skipped in later passes — the caller is responsible for warning if the
    total returned is short of ``limit``.
    """
    rng = random.Random(seed)
    queues: dict[str, list[dict]] = {
        journal: rng.sample(records, len(records))
        for journal, records in by_journal.items()
    }
    journals = sorted(queues)

    selected: list[dict] = []
    while len(selected) < limit and any(queues[j] for j in journals):
        for journal in journals:
            if len(selected) >= limit:
                break
            if queues[journal]:
                selected.append(queues[journal].pop())
    return selected


def _print_reveal_table(doi_filter: set[str], journal_map: dict[str, list[str]]) -> None:
    """Print the post-evaluation identity reveal table.

    Called AFTER run_evaluation() completes — never before — so model
    identities are not visible during scoring (blind protocol).
    """
    if not EVALUATIONS_PATH.exists():
        return

    # Build lookup: doi → list of evaluation rows
    rows_by_doi: dict[str, list[dict]] = {}
    with open(EVALUATIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(row.get("doi", "")).strip()
            if doi in doi_filter:
                rows_by_doi.setdefault(doi, []).append(row)

    doi_to_journal: dict[str, str] = {}
    for journal, dois in journal_map.items():
        for doi in dois:
            doi_to_journal[doi] = journal

    print("\n" + "=" * 68)
    print("  EVALUATION COMPLETE  ·  IDENTITY REVEAL")
    print("  (Summaries were scored blind; identities shown only now)")
    print("=" * 68)
    print(f"  {'Journal':<16} {'Summarizer':<14} {'Judge':<10} "
          f"{'Score':>5}  {'Halluc':>6}  {'Review?':>7}")
    print("  " + "-" * 64)

    total_cost = 0.0
    judge_model_seen: set[str] = set()
    for doi in sorted(doi_filter):
        journal = doi_to_journal.get(doi, "unknown")
        for row in rows_by_doi.get(doi, []):
            score_disp = (
                f"{row.get('composite_score', row.get('quality_score', '?'))}"
            )
            halluc = row.get("hallucination_count", "?")
            review = "Yes" if row.get("requires_human_review") else "No"
            summarizer = str(row.get("summarizer", "?"))
            judge = str(row.get("judge", "?"))
            judge_version = str(row.get("judge_model_version", ""))
            if judge_version:
                judge_model_seen.add(judge_version)
            # Approximate cost from token counts (display only)
            try:
                from models_config import compute_cost
                cost = compute_cost(
                    judge,
                    int(row.get("input_tokens") or 0),
                    int(row.get("output_tokens") or 0),
                    batched=False,
                )
                total_cost += cost
            except Exception:
                pass
            print(f"  {journal:<16} {summarizer:<14} {judge:<10} "
                  f"{score_disp:>5}  {str(halluc):>6}  {review:>7}")

    print("=" * 68)
    if judge_model_seen:
        print(f"  Judge model version(s): {', '.join(sorted(judge_model_seen))}")
    if total_cost > 0:
        print(f"  Total cost this run: ${total_cost:.4f}")
    print("=" * 68 + "\n")


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------

def cmd_evaluate(args: argparse.Namespace) -> int:
    profile = resolve_mode(args.mode)
    print(profile.banner())

    import evaluator
    from eval_instances import iter_evaluation_instances

    # resolve_judges applies the layered opt-in: --judges > --jury > JURY_PRESET
    # > JUDGE_MODELS. Default is the full 3-judge panel (openai,anthropic,gemini).
    judges = evaluator.resolve_judges(args.judges, jury=args.jury)
    active_judges = judges
    model_ids = {j: get_model_spec(j).model_id for j in active_judges}
    if len(active_judges) > 1:
        print(f"[phase3:evaluate] jury of {len(active_judges)} judges: "
              f"{', '.join(active_judges)} (reliability stats will be reported)")

    input_mode = _resolve_eval_input_mode(profile.name, args.input_mode)
    if input_mode == "dev-jsonl":
        return _cmd_evaluate_from_dev_jsonl(
            args, profile, judges=judges, active_judges=active_judges,
            model_ids=model_ids,
        )
    if input_mode != "jsonl":
        return _cmd_evaluate_from_txt_folder(
            args, profile, judges=judges, active_judges=active_judges,
            model_ids=model_ids, input_mode=input_mode,
        )

    journal_map: dict[str, list] = {}

    if profile.name == "test" or profile.dry_run:
        # test mode: no journal stratification (mock, $0)
        doi_filter = None
        paper_limit = args.limit if args.limit is not None else profile.paper_limit
        slice_config = {"type": "full_corpus_or_limit", "paper_limit": paper_limit}
    else:
        # single / dev / batch modes: journal-stratified sampling
        by_journal = _load_summaries_by_journal(SUMMARIES_PATH, MANIFEST_PATH)

        # Warn about and log unmapped DOIs
        unknown_entries = by_journal.pop("unknown", [])
        if unknown_entries:
            unknown_dois = [str(e.get("doi", "?")) for e in unknown_entries]
            msg_lines = [
                f"[phase3:evaluate] WARNING: {len(unknown_dois)} summaries could not be "
                "mapped to a journal and will be excluded from stratified sampling.",
                "  DOIs without a journal mapping in manifest.jsonl:",
            ] + [f"    {d}" for d in unknown_dois] + [
                "  Add the journal field to manifest.jsonl to include these articles.",
            ]
            for line in msg_lines:
                print(line)
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOGS_DIR / "eval_journal_mapping.log"
            ts = datetime.now(timezone.utc).isoformat()
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n[{ts}]\n" + "\n".join(msg_lines) + "\n")

        if not by_journal:
            print("[phase3:evaluate] No journal-mapped summaries found. "
                  "Run 'extract' and 'summarize' first, or check manifest.jsonl.")
            return 1

        # batch's own profile.paper_limit is None ("full corpus"), but
        # `None or 1` previously collapsed that to 1 paper/journal — silently
        # sampling 5 papers total instead of the whole corpus. dev's
        # profile.paper_limit (PHASE3_DEV_LIMIT) is never None, so it's
        # unaffected; only batch needs the explicit "no cap" sentinel.
        per_journal = (
            args.limit
            if args.limit is not None
            else (1 if profile.name == "single"
                  else (profile.paper_limit if profile.name == "dev" else _NO_PER_JOURNAL_CAP))
        )
        seed = int(os.getenv("EVAL_SAMPLE_SEED", "42"))
        doi_filter, journal_map = _sample_by_journal(by_journal, per_journal, seed=seed)
        paper_limit = None
        slice_config = {
            "type": "journal_stratified",
            "per_journal": per_journal,
            "seed": seed,
            "journal_counts": {j: len(v) for j, v in journal_map.items()},
        }

        # Pre-evaluation summary (anonymized — journal names only, no DOIs, no model names)
        print("\n[phase3:evaluate] Journal-stratified sampling plan:")
        print(f"  {'Journal':<20}  Articles selected")
        print("  " + "-" * 38)
        for journal in sorted(journal_map):
            print(f"  {journal:<20}  {len(journal_map[journal])}")
        print(f"  {'TOTAL':<20}  {len(doi_filter)}")
        print(f"  (seed={seed}, per_journal={per_journal})\n")

    # --- Build and write the run manifest before any judge is called ---
    selected_instance_ids = sorted({
        inst.doi for inst in iter_evaluation_instances(
            summaries_path=evaluator.SUMMARIES_PATH,
            doi_filter=doi_filter,
            paper_limit=paper_limit,
        )
    })

    if not evaluator.SUMMARIES_PATH.exists():
        print(f"[phase3:evaluate] WARNING: {evaluator.SUMMARIES_PATH} does not exist "
              "yet; recording an empty-file hash in the run manifest.")

    prompt_file = evaluator.JUDGE_PROMPT_FILE
    prompt_path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    evaluation_config = {
        "rubric_version": evaluator.RUBRIC_VERSION,
        "evaluator_version": evaluator.EVALUATOR_VERSION,
        "benchmark_name": evaluator.BENCHMARK_NAME,
        "jury_aggregation_mode": evaluator.JURY_AGGREGATION_MODE,
        "mode": profile.name,
        "slice_config": slice_config,
        "taxonomy": VET_TAXONOMY_V1.describe(VeterinarySummaryQualityScenario.name),
    }
    manifest = build_run_manifest(
        run_id=create_run_id(),
        dataset_path=str(evaluator.SUMMARIES_PATH),
        dataset_hash_sha256=sha256_file_or_empty(evaluator.SUMMARIES_PATH),
        judges=active_judges,
        model_ids=model_ids,
        prompt_template_id=prompt_path.name,
        prompt_path=str(prompt_path),
        prompt_sha256=sha256_file(prompt_path),
        temperature=evaluator.TEMPERATURE,
        max_output_tokens=evaluator.MAX_OUTPUT_TOKENS,
        seed=evaluator.SEED,
        evaluation_config=evaluation_config,
        selected_instance_ids=selected_instance_ids,
    )
    manifest_path = RUN_MANIFEST_DIR / f"run_manifest_{manifest.run_id}.json"
    write_run_manifest(manifest, manifest_path)

    status = "started"
    result = 1
    try:
        if profile.name == "test" or profile.dry_run:
            from evaluator import confirm_real_judge, run_evaluation
            from utils import require_positive_budget_for_real_run

            if not confirm_real_judge(profile, force=args.force):
                status = "failed"
                result = 1
            else:
                require_positive_budget_for_real_run(
                    dry_run=profile.dry_run, context="Phase 3 evaluation",
                )
                counts = run_evaluation(
                    judges=judges,
                    resume=not args.no_resume,
                    paper_limit=paper_limit,
                    doi_filter=None,
                )
                status = "completed" if counts.get("failed", 0) == 0 else "failed"
                result = 0 if status == "completed" else 1
        else:
            from evaluator import confirm_real_judge, run_evaluation

            if not confirm_real_judge(profile, force=args.force):
                status = "failed"
                result = 1
            elif profile.use_batch:
                # Split judges by their *_BATCH_ENABLED flag, mirroring the
                # summarize-side split in summarizer.py main() exactly:
                # batch-eligible judges (OpenAI/Anthropic by default) submit
                # as real batch jobs at 50% off; the rest (Gemini by default)
                # still run real-time, in the same command. doi_filter (the
                # journal-stratified sample computed above) applies to both.
                batch_judges = [j for j in judges if get_model_spec(j).supports_batch]
                realtime_judges = [j for j in judges if not get_model_spec(j).supports_batch]
                counts = {"success": 0, "failed": 0, "skipped": 0}
                if batch_judges:
                    from batch_utils import run_batch_evaluation
                    print(f"[phase3:batch] batch-API judges: {', '.join(batch_judges)}")
                    run_batch_evaluation(
                        judges=batch_judges,
                        resume=not args.no_resume,
                        doi_filter=doi_filter,
                        force=args.force,
                    )
                if realtime_judges:
                    print(f"[phase3:batch] real-time judges (batch disabled for these): "
                          f"{', '.join(realtime_judges)}")
                    counts = run_evaluation(
                        judges=realtime_judges,
                        resume=not args.no_resume,
                        paper_limit=None,
                        doi_filter=doi_filter,
                    )
                status = "completed" if counts.get("failed", 0) == 0 else "failed"
                _print_reveal_table(doi_filter, journal_map)
                result = 0 if status == "completed" else 1
            else:
                counts = run_evaluation(
                    judges=judges,
                    resume=not args.no_resume,
                    paper_limit=None,
                    doi_filter=doi_filter,
                )
                status = "completed" if counts.get("failed", 0) == 0 else "failed"
                _print_reveal_table(doi_filter, journal_map)
                result = 0 if status == "completed" else 1
        return result
    except Exception:
        status = "failed"
        raise
    finally:
        from eval_report import iter_evaluation_rows

        # PROVENANCE read, not analysis: the manifest records which model
        # versions actually answered, so it must see dry-run rows too. Filtering
        # them here would let a test-mode run produce a manifest claiming real
        # model versions — the same class of bug this filter exists to prevent.
        resolved_versions = resolve_model_versions(
            active_judges, model_ids,
            list(iter_evaluation_rows(include_dry_run=True)),
        )
        finalize_run_manifest(
            manifest_path, resolved_model_versions=resolved_versions, status=status,
        )


def _cmd_evaluate_from_txt_folder(
    args: argparse.Namespace, profile, *, judges: list[str], active_judges: list[str],
    model_ids: dict[str, str], input_mode: str,
) -> int:
    """Evaluate summarize-all's processed-text .txt comparison files directly.

    A lighter-weight sibling of cmd_evaluate's data/summaries.jsonl path: no
    journal-stratified sampling (a .txt folder is usually a hand-run batch,
    not the full corpus with a manifest to stratify against) — just every
    article found in the chosen folder, capped by the mode's paper_limit (or
    --limit). PDF-input comparison files are never read here; only the
    processed-text side (see docs/phase3/evaluator.md).
    """
    from evaluator import confirm_real_judge, run_evaluation
    from summarize_all_ingest import iter_summarize_all_instances, select_latest_txt_files
    from utils import require_positive_budget_for_real_run
    import evaluator

    txt_dir = DEV_TESTS_SUMMARIES_TXT_DIR if input_mode == "dev" else SUMMARIES_TXT_DIR
    print(f"[phase3:evaluate] input mode={input_mode}: reading processed-text "
          f"summaries from {txt_dir}")

    paper_limit = args.limit if args.limit is not None else profile.paper_limit
    selected_files = select_latest_txt_files(txt_dir, paper_limit=paper_limit)
    if not selected_files:
        print(f"[phase3:evaluate] No processed-text summaries found in {txt_dir}. "
              "Run 'summarize-all' first, or change EVAL_INPUT_MODE / --input-mode.")
        return 1

    instances = list(iter_summarize_all_instances(txt_dir, files=selected_files))
    dois = sorted({inst.doi for inst in instances})
    providers_seen = sorted({inst.summarizer for inst in instances})
    print(f"[phase3:evaluate] {len(dois)} article(s) found, {len(instances)} summary "
          f"slot(s) to judge (providers: {', '.join(providers_seen) or 'none'})")
    if not instances:
        print("[phase3:evaluate] Nothing to judge — no successful provider summaries "
              "were found in the selected files.")
        return 1

    prompt_file = evaluator.JUDGE_PROMPT_FILE
    prompt_path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    evaluation_config = {
        "rubric_version": evaluator.RUBRIC_VERSION,
        "evaluator_version": evaluator.EVALUATOR_VERSION,
        "benchmark_name": evaluator.BENCHMARK_NAME,
        "jury_aggregation_mode": evaluator.JURY_AGGREGATION_MODE,
        "mode": profile.name,
        "slice_config": {
            "type": "summarize_all_txt",
            "input_mode": input_mode,
            "source_dir": str(txt_dir),
            "paper_limit": paper_limit,
            "articles_found": len(dois),
        },
        "taxonomy": VET_TAXONOMY_V1.describe(VeterinarySummaryQualityScenario.name),
    }
    manifest = build_run_manifest(
        run_id=create_run_id(),
        dataset_path=str(txt_dir),
        dataset_hash_sha256=sha256_files_combined(selected_files),
        judges=active_judges,
        model_ids=model_ids,
        prompt_template_id=prompt_path.name,
        prompt_path=str(prompt_path),
        prompt_sha256=sha256_file(prompt_path),
        temperature=evaluator.TEMPERATURE,
        max_output_tokens=evaluator.MAX_OUTPUT_TOKENS,
        seed=evaluator.SEED,
        evaluation_config=evaluation_config,
        selected_instance_ids=dois,
    )
    manifest_path = RUN_MANIFEST_DIR / f"run_manifest_{manifest.run_id}.json"
    write_run_manifest(manifest, manifest_path)

    status = "started"
    result = 1
    try:
        if not confirm_real_judge(profile, force=args.force):
            status = "failed"
            result = 1
        else:
            require_positive_budget_for_real_run(
                dry_run=profile.dry_run, context="Phase 3 evaluation",
            )
            counts = run_evaluation(
                judges=judges, resume=not args.no_resume, instances=instances,
            )
            status = "completed" if counts.get("failed", 0) == 0 else "failed"
            result = 0 if status == "completed" else 1
        return result
    except Exception:
        status = "failed"
        raise
    finally:
        from eval_report import iter_evaluation_rows

        # PROVENANCE read, not analysis: the manifest records which model
        # versions actually answered, so it must see dry-run rows too. Filtering
        # them here would let a test-mode run produce a manifest claiming real
        # model versions — the same class of bug this filter exists to prevent.
        resolved_versions = resolve_model_versions(
            active_judges, model_ids,
            list(iter_evaluation_rows(include_dry_run=True)),
        )
        finalize_run_manifest(
            manifest_path, resolved_model_versions=resolved_versions, status=status,
        )


def _cmd_evaluate_from_dev_jsonl(
    args: argparse.Namespace, profile, *, judges: list[str], active_judges: list[str],
    model_ids: dict[str, str],
) -> int:
    """Judge exactly the papers already in data/dev_summaries_jsonl/.

    This is the folder-driven dev loop `evaluate --mode dev` uses by default
    (see _resolve_eval_input_mode). It reads back the DOIs written by
    `summarize --mode dev`, judges their matching articles (reference text from
    the processed-text cache, candidate summaries from data/summaries.jsonl —
    both resolved by iter_evaluation_instances via run_evaluation's doi_filter),
    and mirrors the scores into readable data/dev_evals_jsonl/ files so the run
    can be eyeballed. data/evaluations.jsonl stays the append-only source of
    truth.

    Incremental by design: DOIs that already have a data/dev_evals_jsonl/ file
    are skipped so repeated runs only judge newly-summarised papers. --no-resume
    turns BOTH that folder-skip and run_evaluation's per-row already_evaluated()
    skip off, forcing a full (paid) re-judge that also rewrites the readable
    mirror.
    """
    from evaluator import (
        confirm_real_judge,
        run_evaluation,
        write_dev_eval_jsonl_outputs,
        write_dev_detail_eval_outputs,
    )
    from eval_instances import load_manifest_index
    from utils import require_positive_budget_for_real_run
    import evaluator

    manifest_index = load_manifest_index()

    # --source-dir redirects which readable folder seeds this run (an
    # --output-subdir pool, data/single_summaries_jsonl/, data/batch_summaries_jsonl/).
    # Resolved once here and used everywhere below — leaving any one reference
    # pinned to the module constant would mix provenance into the run manifest.
    source_dir = args.source_dir or DEV_SUMMARIES_JSONL_DIR

    source_dois = _read_dois_from_dev_folder(source_dir)
    print(f"[phase3:evaluate] input mode=dev-jsonl: reading dev summaries from "
          f"{source_dir}")
    _warn_about_unread_subfolders(source_dir)
    if not source_dois:
        print(f"[phase3:evaluate] No dev summaries found in {source_dir}. "
              "Run 'summarize --mode dev' first.")
        return 1

    # Skip papers whose readable eval file already exists (unless --no-resume,
    # which re-includes them for a full re-judge). A DOI with rows already in
    # evaluations.jsonl but NO dev_evals_jsonl file (e.g. a crash between the
    # append and the mirror write) is not folder-present, so it stays in the
    # target set: the paid call below is then row-skipped by already_evaluated(),
    # but write_dev_eval_jsonl_outputs still backfills the missing readable file.
    done_dois = set() if args.no_resume else _read_dois_from_dev_folder(DEV_EVALS_JSONL_DIR)
    target_dois = source_dois - done_dois

    print(f"[phase3:evaluate] {len(source_dois)} dev paper(s) found; "
          f"{len(done_dois)} already in {DEV_EVALS_JSONL_DIR.name}; "
          f"{len(target_dois)} to judge.")
    if not target_dois:
        print("[phase3:evaluate] All dev articles already evaluated; nothing to do. "
              "Run 'summarize --mode dev' to add more, or pass --no-resume to re-judge.")
        return 0

    # Cap + journal-balance the pending pool, mirroring `summarize --mode
    # dev`'s round-robin-by-journal picker (_sample_round_robin_by_journal)
    # on the judging side. Skipped for --mode test (always judge the whole
    # mock pool for $0) and for --no-resume (an explicit "re-judge everything
    # in the folder" request, not "re-judge a capped sample"). Papers left out
    # by the cap are never marked done, so a follow-up run picks them up.
    pending_before_sampling = len(target_dois)
    sampling_info: dict | None = None
    apply_cap = profile.name != "test" and not args.no_resume
    if apply_cap:
        cap = args.limit if args.limit is not None else profile.paper_limit
        if cap is not None and pending_before_sampling > cap:
            mapped_by_journal: dict[str, list[dict]] = {}
            unmapped_dois: list[str] = []
            for doi in sorted(target_dois):
                journal = str(manifest_index.get(doi, {}).get("journal", "")).strip().lower()
                if journal:
                    mapped_by_journal.setdefault(journal, []).append(
                        {"doi": doi, "journal": journal}
                    )
                else:
                    unmapped_dois.append(doi)

            if unmapped_dois:
                msg_lines = [
                    f"[phase3:evaluate] WARNING: {len(unmapped_dois)} pending dev paper(s) "
                    "could not be mapped to a journal and will be excluded from the "
                    "capped sample.",
                    "  DOIs without a journal mapping in manifest.jsonl:",
                ] + [f"    {d}" for d in unmapped_dois] + [
                    "  Add the journal field to manifest.jsonl to include these articles.",
                ]
                for line in msg_lines:
                    print(line)
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                log_path = LOGS_DIR / "eval_journal_mapping.log"
                ts = datetime.now(timezone.utc).isoformat()
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"\n[{ts}]\n" + "\n".join(msg_lines) + "\n")

            seed = int(os.getenv("EVAL_SAMPLE_SEED", "42"))
            selected_records = _sample_round_robin_by_journal(mapped_by_journal, cap, seed=seed)
            target_dois = {r["doi"] for r in selected_records}
            journal_counts: dict[str, int] = {}
            for r in selected_records:
                journal_counts[r["journal"]] = journal_counts.get(r["journal"], 0) + 1
            sampling_info = {"limit": cap, "journal_counts": journal_counts}

            print(f"[phase3:evaluate] pending pool ({pending_before_sampling}) exceeds "
                  f"cap ({cap}); sampled {len(target_dois)} paper(s) balanced across "
                  f"{len(journal_counts)} journal(s).")

    # Hash the specific dev-summary files that seed this run for provenance.
    selected_files = sorted(
        p for p in source_dir.glob("*.txt")
        if _read_dois_from_dev_folder_file(p) in target_dois
    )

    prompt_file = evaluator.JUDGE_PROMPT_FILE
    prompt_path = prompt_file if prompt_file.is_absolute() else REPO_ROOT / prompt_file
    evaluation_config = {
        "rubric_version": evaluator.RUBRIC_VERSION,
        "evaluator_version": evaluator.EVALUATOR_VERSION,
        "benchmark_name": evaluator.BENCHMARK_NAME,
        "jury_aggregation_mode": evaluator.JURY_AGGREGATION_MODE,
        "mode": profile.name,
        "slice_config": {
            "type": "dev_jsonl",
            "source_dir": str(source_dir),
            "articles_found": len(source_dois),
            "articles_pending_before_sampling": pending_before_sampling,
            "articles_to_judge": len(target_dois),
            "sampling": sampling_info,
        },
        "taxonomy": VET_TAXONOMY_V1.describe(VeterinarySummaryQualityScenario.name),
    }
    manifest = build_run_manifest(
        run_id=create_run_id(),
        dataset_path=str(source_dir),
        dataset_hash_sha256=sha256_files_combined(selected_files),
        judges=active_judges,
        model_ids=model_ids,
        prompt_template_id=prompt_path.name,
        prompt_path=str(prompt_path),
        prompt_sha256=sha256_file(prompt_path),
        temperature=evaluator.TEMPERATURE,
        max_output_tokens=evaluator.MAX_OUTPUT_TOKENS,
        seed=evaluator.SEED,
        evaluation_config=evaluation_config,
        selected_instance_ids=sorted(target_dois),
    )
    manifest_path = RUN_MANIFEST_DIR / f"run_manifest_{manifest.run_id}.json"
    write_run_manifest(manifest, manifest_path)

    status = "started"
    result = 1
    try:
        if not confirm_real_judge(profile, force=args.force):
            status = "failed"
            result = 1
        else:
            require_positive_budget_for_real_run(
                dry_run=profile.dry_run, context="Phase 3 evaluation",
            )
            counts = run_evaluation(
                judges=judges, resume=not args.no_resume, doi_filter=target_dois,
            )
            written = write_dev_eval_jsonl_outputs(
                target_dois, output_dir=DEV_EVALS_JSONL_DIR, manifest_index=manifest_index,
            )
            print(f"[phase3:evaluate] wrote {written} readable dev eval "
                  f"file(s) to {DEV_EVALS_JSONL_DIR}")
            written_detail = write_dev_detail_eval_outputs(
                target_dois, output_dir=DEV_DETAIL_EVAL_REPORTS_DIR, manifest_index=manifest_index,
            )
            print(f"[phase3:evaluate] wrote {written_detail} detailed dev eval "
                  f"report(s) to {DEV_DETAIL_EVAL_REPORTS_DIR}")
            status = "completed" if counts.get("failed", 0) == 0 else "failed"
            result = 0 if status == "completed" else 1
        return result
    except Exception:
        status = "failed"
        raise
    finally:
        from eval_report import iter_evaluation_rows

        # PROVENANCE read, not analysis: the manifest records which model
        # versions actually answered, so it must see dry-run rows too. Filtering
        # them here would let a test-mode run produce a manifest claiming real
        # model versions — the same class of bug this filter exists to prevent.
        resolved_versions = resolve_model_versions(
            active_judges, model_ids,
            list(iter_evaluation_rows(include_dry_run=True)),
        )
        finalize_run_manifest(
            manifest_path, resolved_model_versions=resolved_versions, status=status,
        )


def _read_dois_from_dev_folder_file(path: Path) -> str | None:
    """Return the single real DOI recorded in one dev-mode readable .txt file.

    A per-file companion to _read_dois_from_dev_folder used when we need to know
    which specific file corresponds to a DOI (e.g. to hash only the dev-summary
    files that seed a run). Returns None if no DOI header is present.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.lower().startswith("doi:"):
                    doi = stripped.split(":", 1)[1].strip()
                    if doi and doi.lower() not in {"not recorded", "none"}:
                        return doi
                return None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    print(resolve_mode(args.mode).banner())
    raw_text = list(RAW_TEXT_DIR.glob("*.jsonl")) if RAW_TEXT_DIR.exists() else []
    processed = list(PROCESSED_DIR.glob("*.jsonl")) if PROCESSED_DIR.exists() else []
    print(f"[phase3:status] data/raw_text/*.jsonl  : {len(raw_text)} files")
    print(f"[phase3:status] data/processed/*.jsonl : {len(processed)} files")

    per_model = {p: {"success": 0, "failed": 0, "pending": 0} for p in all_providers()}
    paper_total = 0
    if SUMMARIES_PATH.exists():
        with open(SUMMARIES_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                paper_total += 1
                for prov, slot in (entry.get("models") or {}).items():
                    if prov not in per_model:
                        per_model[prov] = {"success": 0, "failed": 0, "pending": 0}
                    status = slot.get("status") or "pending"
                    per_model[prov][status] = per_model[prov].get(status, 0) + 1
    print(f"[phase3:status] data/summaries.jsonl  : {paper_total} papers")
    for prov, c in per_model.items():
        print(f"    {prov:>10}: success={c.get('success',0)} "
              f"failed={c.get('failed',0)} pending={c.get('pending',0)}")

    eval_total = 0
    dry_run_total = 0
    requires_review = 0
    if EVALUATIONS_PATH.exists():
        from evaluator import is_dry_run_row
        with open(EVALUATIONS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Count mock rows separately: reporting a raw line count would
                # overstate how much of the corpus has actually been judged.
                if is_dry_run_row(row):
                    dry_run_total += 1
                    continue
                eval_total += 1
                if row.get("requires_human_review"):
                    requires_review += 1
    suffix = f", {dry_run_total} dry-run rows excluded" if dry_run_total else ""
    print(f"[phase3:status] data/evaluations.jsonl: {eval_total} real rows "
          f"({requires_review} flagged for human review{suffix})")
    return 0


def cmd_eval_report(args: argparse.Namespace) -> int:
    """Print read-only aggregate reports for MedHELM-style evaluation rows.

    ``--publication`` switches from the operator-facing stratified summary
    (eval_report.py) to the publication tables + significance tests
    (report_tables.py): provider comparison with bootstrap CIs, paired
    Wilcoxon/Friedman tests, per-stratum and processed-vs-PDF breakdowns, and
    cost-per-quality-point. Both are read-only and offline.
    """
    if args.publication:
        from report_tables import main as publication_main
        delegate = ["--evaluations", str(args.evaluations), "--results-dir", str(args.results_dir)]
        if args.json:
            delegate.append("--json")
        if args.markdown:
            delegate.append("--markdown")
        if args.no_save:
            delegate.append("--no-save")
        return publication_main(delegate)

    from eval_report import main as report_main
    delegate = ["--evaluations", str(args.evaluations), "--results-dir", str(args.results_dir)]
    if args.json:
        delegate.append("--json")
    if args.markdown:
        delegate.append("--markdown")
    if args.no_detail:
        delegate.append("--no-detail")
    if args.no_save:
        delegate.append("--no-save")
    if args.human_validation_mode is not None:
        delegate += ["--human-validation-mode", args.human_validation_mode]
    if args.human_reviews is not None:
        delegate += ["--human-reviews", str(args.human_reviews)]
    return report_main(delegate)


# ---------------------------------------------------------------------------
# report-figures
# ---------------------------------------------------------------------------

def cmd_report_figures(args: argparse.Namespace) -> int:
    """Render publication figures + export the VetHELM-style leaderboard.

    Offline only — no API calls. Delegates to report_figures.main() so the
    module stays independently runnable/testable (same pattern as
    cmd_eval_report's --publication delegation to report_tables.py).
    """
    print(resolve_mode(args.mode).banner())
    from report_figures import main as report_figures_main

    delegate = ["--evaluations", str(args.evaluations), "--results-dir", str(args.results_dir)]
    if args.summaries is not None:
        delegate += ["--summaries", str(args.summaries)]
    if args.human_reviews is not None:
        delegate += ["--human-reviews", str(args.human_reviews)]
    if args.seed is not None:
        delegate += ["--seed", str(args.seed)]
    if args.bootstrap_resamples is not None:
        delegate += ["--bootstrap-resamples", str(args.bootstrap_resamples)]
    if args.cost_batched:
        delegate.append("--cost-batched")
    if args.no_figures:
        delegate.append("--no-figures")
    if args.formats is not None:
        delegate += ["--formats", args.formats]
    return report_figures_main(delegate)


# ---------------------------------------------------------------------------
# stats-engine
# ---------------------------------------------------------------------------

def cmd_stats_engine(args: argparse.Namespace) -> int:
    """Information density, Cohen's Kappa IRR, subscription economics, and
    covariate 'research meat' tables.

    Offline only — no API calls. Delegates to stats_engine.main() so the
    module stays independently runnable/testable (same pattern as
    cmd_eval_report's --publication delegation to report_tables.py). These
    same sections are also folded into 'eval-report --publication'; this
    subcommand is for a standalone run.
    """
    from stats_engine import main as stats_engine_main

    delegate = ["--evaluations", str(args.evaluations), "--results-dir", str(args.results_dir)]
    if args.summaries is not None:
        delegate += ["--summaries", str(args.summaries)]
    if args.human_reviews is not None:
        delegate += ["--human-reviews", str(args.human_reviews)]
    if args.subscription_price_usd is not None:
        delegate += ["--subscription-price-usd", str(args.subscription_price_usd)]
    if args.papers_per_month is not None:
        delegate += ["--papers-per-month", str(args.papers_per_month)]
    if args.json:
        delegate.append("--json")
    if args.markdown:
        delegate.append("--markdown")
    if args.no_save:
        delegate.append("--no-save")
    return stats_engine_main(delegate)


# ---------------------------------------------------------------------------
# export-human-review
# ---------------------------------------------------------------------------

def cmd_export_human_review(args: argparse.Namespace) -> int:
    """Export ONE MORE blind human-review reviewer folder (humanN/) from
    data/evaluations.jsonl.

    Offline only — no API calls are made. The mode banner is printed only for
    consistency with the other subcommands (same rationale as cmd_extract).
    """
    print(resolve_mode(args.mode).banner())
    from human_review import main as human_review_main

    delegate: list[str] = []
    if args.sample_size is not None:
        delegate += ["--sample-size", str(args.sample_size)]
    if args.overlap_ratio is not None:
        delegate += ["--overlap-ratio", str(args.overlap_ratio)]
    if args.seed is not None:
        delegate += ["--seed", str(args.seed)]
    return human_review_main(delegate)


def cmd_ingest_human_review(args: argparse.Namespace) -> int:
    """Ingest filled reviewer scoresheets into data/human_reviews.jsonl.

    Offline only — no API calls. Delegates to human_review.ingest_main (kept
    separate from the export main() so each side stays independently testable).
    """
    print(resolve_mode(args.mode).banner())
    from human_review import ingest_main

    delegate: list[str] = []
    if args.review_dir is not None:
        delegate += ["--review-dir", str(args.review_dir)]
    if args.output is not None:
        delegate += ["--output", str(args.output)]
    return ingest_main(delegate)


def cmd_export_pilot_human_review(args: argparse.Namespace) -> int:
    """Export ONE more pilot reviewer folder (humanN/) from the dev-summary pool.

    Offline only — no API calls are made. The mode banner is printed only for
    consistency with the other subcommands (same rationale as cmd_extract).
    See docs/phase5/pilot_human_review.md.
    """
    print(resolve_mode(args.mode).banner())
    from pilot_human_review import main as pilot_human_review_main

    delegate: list[str] = []
    if args.sample_size is not None:
        delegate += ["--sample-size", str(args.sample_size)]
    if args.overlap_ratio is not None:
        delegate += ["--overlap-ratio", str(args.overlap_ratio)]
    if args.seed is not None:
        delegate += ["--seed", str(args.seed)]
    return pilot_human_review_main(delegate)


def cmd_ingest_pilot_human_review(args: argparse.Namespace) -> int:
    """Ingest filled pilot reviewer scoresheets into data/pilot_human_reviews.jsonl.

    Offline only — no API calls. Delegates to pilot_human_review.ingest_main,
    which writes to the pilot-only ledger so rehearsal scores can never be
    mistaken for real study results (see human_review.ingest_human_reviews'
    pilot-folder guard). See docs/phase5/pilot_human_review.md.
    """
    print(resolve_mode(args.mode).banner())
    from pilot_human_review import ingest_main as pilot_ingest_main

    delegate: list[str] = []
    if args.review_dir is not None:
        delegate += ["--review-dir", str(args.review_dir)]
    if args.output is not None:
        delegate += ["--output", str(args.output)]
    return pilot_ingest_main(delegate)


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------

def cmd_clean(_args: argparse.Namespace) -> int:
    if BATCH_DIR.exists():
        removed = 0
        for p in BATCH_DIR.glob("*.jsonl"):
            p.unlink()
            removed += 1
        print(f"[phase3:clean] removed {removed} files from {BATCH_DIR}")
    else:
        print(f"[phase3:clean] no batch scratch dir at {BATCH_DIR}")
    return 0


# ---------------------------------------------------------------------------
# summarize-all  (folder-based: data/raw/*.pdf + data/processed/*.jsonl)
# ---------------------------------------------------------------------------

def _paired_summary_stems(
    limit: int | None,
    *,
    random_match: bool | None = None,
) -> set[str] | None:
    """
    Return article stems that exist in both raw PDFs and processed text caches.

    Matching by stem keeps the manual comparison honest: the PDF-side and
    processed-text-side summaries come from the same descriptive filename, which
    includes the DOI slug generated during extraction.
    """
    if not RAW_DIR.exists() or not PROCESSED_DIR.exists():
        return set()

    pdf_stems = {path.stem for path in RAW_DIR.glob("*.pdf")}
    processed_stems = {path.stem for path in PROCESSED_DIR.glob("*.jsonl")}
    paired = sorted(pdf_stems & processed_stems)
    should_randomize = _summarize_all_random_match() if random_match is None else random_match
    if limit is not None and limit < len(paired):
        if not should_randomize:
            return set(paired[:limit])
        paired = random.sample(paired, limit)
    return set(paired)


def _summary_run_suffix() -> str:
    """Return a unique suffix shared by paired PDF/text summarize-all outputs."""
    return datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%S%fZ")


def cmd_summarize_all(args: argparse.Namespace) -> int:
    """
    Summarise every PDF in data/raw/ AND every processed JSONL in data/processed/
    with all three providers in a single command.

    In ``single`` mode this processes one matched article stem that exists in
    both folders, yielding six summaries for the same DOI/title. ``dev`` mode
    uses ``PHASE3_DEV_LIMIT`` matched stems so prompt experiments cover the same
    small multi-article sample as the manifest-driven dev workflow.

    Outputs default by mode:
      single/test: data/summaries_pdf/<stem>.txt and data/summaries_txt/<stem>.txt
      dev:         data/dev_tests/summaries_pdf/<stem>.txt and
                   data/dev_tests/summaries_txt/<stem>.txt
    """
    profile = resolve_mode(args.mode)
    print(profile.banner())

    if profile.use_batch:
        print("[phase3:summarize-all] batch mode is not supported because direct PDF "
              "summarisation is real-time only. Use --mode single or --mode dev.")
        return 1

    from summarizer import (
        summarize_all_pdfs,
        summarize_all_processed_texts,
        load_provider_prompt_templates_with_optional_guide,
        confirm_real_batch,
    )

    providers = (
        [p.strip() for p in args.providers.split(",") if p.strip()]
        if args.providers else None
    )
    try:
        prompt_templates, guide_summary, resolved_guide_path, _prompt_paths = (
            load_provider_prompt_templates_with_optional_guide(providers, None)
        )
    except Exception as exc:
        print(f"[phase3:summarize-all] prompt validation failed: {exc}")
        return 1
    if guide_summary:
        print(f"[phase3:summarize-all] format guide ready: {resolved_guide_path}")

    if not confirm_real_batch(profile, force=args.force):
        return 1

    # summarize-all is the manual PDF-vs-processed comparison workflow. Test
    # and single stay as one-pair smoke checks; dev deliberately follows the
    # shared ModeProfile limit so PHASE3_DEV_LIMIT means the same thing across
    # summarize, evaluate, and summarize-all.
    effective_limit = (
        args.limit
        if args.limit is not None
        else (1 if profile.name in {"test", "single"} else profile.paper_limit)
    )
    if args.limit is None and profile.name in {"test", "single"}:
        print(f"[phase3:summarize-all] {profile.name} mode defaults to --limit 1; "
              "pass --limit N to create more matched comparison files.")
    elif args.limit is None and profile.name == "dev":
        print("[phase3:summarize-all] dev mode defaults to "
              f"PHASE3_DEV_LIMIT={effective_limit} matched article pairs.")
    random_match = _summarize_all_random_match()
    unique_output = _summarize_all_unique_output()
    paired_stems = _paired_summary_stems(effective_limit, random_match=random_match)
    if not paired_stems:
        print("[phase3:summarize-all] No matching article stems found between "
              "data/raw/*.pdf and data/processed/*.jsonl.")
        return 1
    print(f"[phase3:summarize-all] paired articles selected: {len(paired_stems)}")
    if effective_limit is not None and len(paired_stems) < effective_limit:
        print("[phase3:summarize-all] WARNING: requested "
              f"{effective_limit} matched pairs, but only found {len(paired_stems)}. "
              "Only stems present in both data/raw/*.pdf and "
              "data/processed/*.jsonl can be processed.")
    print(f"[phase3:summarize-all] random match: {str(random_match).lower()}")
    output_suffix = _summary_run_suffix() if unique_output else None
    if output_suffix:
        print(f"[phase3:summarize-all] output run suffix: {output_suffix}")
    else:
        print("[phase3:summarize-all] unique output disabled; writing <stem>.txt files.")
    selected_output_set = args.output_set or _summarize_all_output_set()
    pdf_output_dir, txt_output_dir, output_set = _summarize_all_output_dirs(
        profile.name,
        selected_output_set,
    )
    print(f"[phase3:summarize-all] output set: {output_set}")
    print(f"[phase3:summarize-all] PDF outputs: {pdf_output_dir}")
    print(f"[phase3:summarize-all] Text outputs: {txt_output_dir}")

    pdf_counts = summarize_all_pdfs(
        output_dir=pdf_output_dir,
        providers=providers,
        resume=args.resume,
        prompt_template=prompt_templates,
        limit=effective_limit,
        stems=paired_stems,
        output_suffix=output_suffix,
    )

    txt_counts = summarize_all_processed_texts(
        output_dir=txt_output_dir,
        providers=providers,
        resume=args.resume,
        prompt_template=prompt_templates,
        limit=effective_limit,
        stems=paired_stems,
        output_suffix=output_suffix,
    )

    total = {k: pdf_counts.get(k, 0) + txt_counts.get(k, 0)
             for k in set(pdf_counts) | set(txt_counts)}
    pdf_cost = float(getattr(pdf_counts, "budget_spent", 0.0))
    txt_cost = float(getattr(txt_counts, "budget_spent", 0.0))
    combined_cost = pdf_cost + txt_cost
    print(f"\n[phase3:summarize-all] TOTAL counts={total}")
    print("[phase3:summarize-all] COSTS")
    print(f"  PDF/raw summaries:          ${pdf_cost:.4f}")
    print(f"  Processed JSONL summaries:  ${txt_cost:.4f}")
    print(f"  Combined total:             ${combined_cost:.4f}")
    return 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_phase3.py",
        description="Phase 3 orchestrator: extract -> summarize -> evaluate.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --mode is shared by every subcommand: any of test|single|dev|batch
    # overrides PHASE3_MODE from .env for this invocation only.
    def _add_mode_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=VALID_MODES, default=None,
                       help="Override PHASE3_MODE for this run "
                            "(test|single|dev|batch).")

    p_extract = sub.add_parser("extract", help="Build data/processed/*.jsonl cache.")
    _add_mode_arg(p_extract)
    p_extract.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    p_extract.add_argument("--limit", type=int, default=None)
    p_extract.set_defaults(func=cmd_extract)

    p_sum = sub.add_parser("summarize", help="Run the three summarisers.")
    _add_mode_arg(p_sum)
    p_sum.add_argument("--limit", type=int, default=None,
                       help="Override the mode's paper_limit (e.g. --limit 1).")
    p_sum.add_argument("--estimate", action="store_true",
                       help="Print projected cost (uses tiktoken on cached texts); no API calls.")
    p_sum.add_argument("--resume", action="store_true",
                       help="Skip (doi, model) pairs already at status=success.")
    p_sum.add_argument("--force", action="store_true",
                       help="Bypass interactive confirmation. USE WITH CAUTION.")
    p_sum.add_argument("--no-skip-existing", action="store_true",
                       help="Dev mode only: reconsider papers already written to "
                            "data/dev_summaries_jsonl/ instead of skipping them. "
                            "By default dev runs are incremental and pick new papers.")
    p_sum.add_argument("--output-subdir", default=None,
                       help="Dev mode only: write the readable output into "
                            "data/dev_summaries_jsonl/<name>/ instead of the top "
                            "level, and track incremental skip-existing against "
                            "that subfolder only (papers already done in the "
                            "parent folder can still be re-picked).")
    p_sum.add_argument("--providers", default=None,
                       help="Comma-separated subset of providers.")
    p_sum.add_argument("--manifest", type=Path, default=None)
    p_sum.add_argument("--input-source", choices=("processed", "raw_text", "pdf"),
                       default="processed",
                       help=("Input to summarize: processed JSONL (default), raw_text JSONL, "
                             "or pdf for direct PDF comparison in test/single only."))
    p_sum.add_argument("--guide-summary", type=Path, default=None,
                       help=("Optional human-written format guide file. The guide controls "
                             "section style only; facts must still come from the target paper."))
    p_sum.set_defaults(func=cmd_summarize)

    p_sum_all = sub.add_parser(
        "summarize-all",
        help="Summarize paired PDFs (data/raw/) and processed texts (data/processed/) "
             "with OpenAI, Anthropic, and Gemini. "
             "Outputs readable .txt files; dev mode defaults to data/dev_tests/.",
    )
    _add_mode_arg(p_sum_all)
    p_sum_all.add_argument("--limit", type=int, default=None,
                            help=("Override the paired article limit. Without this, "
                                  "test/single/dev process 1 matched PDF + 1 "
                                  "matched processed text."))
    p_sum_all.add_argument("--resume", action="store_true",
                            help="Skip provider slots already at status=success.")
    p_sum_all.add_argument("--force", action="store_true",
                            help="Bypass interactive confirmation. USE WITH CAUTION.")
    p_sum_all.add_argument("--providers", default=None,
                            help="Comma-separated subset of providers "
                                 "(default: openai,anthropic,gemini).")
    p_sum_all.add_argument(
        "--output-set",
        choices=SUMMARIZE_ALL_OUTPUT_SETS,
        default=None,
        help=(
            "Where summarize-all writes readable .txt outputs. "
            "auto sends dev mode to data/dev_tests/ and other modes to the "
            "original data/summaries_pdf + data/summaries_txt folders; "
            "regular always uses the original folders; dev-tests always uses "
            "data/dev_tests/. Overrides SUMMARIZE_ALL_OUTPUT_SET from .env."
        ),
    )
    p_sum_all.set_defaults(func=cmd_summarize_all)

    p_eval = sub.add_parser("evaluate", help="Run the blind judge.")
    _add_mode_arg(p_eval)
    p_eval.add_argument("--limit", type=int, default=None,
                        help="Override the mode's paper_limit.")
    p_eval.add_argument("--judges", default=None,
                        help="Comma-separated judge provider keys. Overrides --jury "
                             "and JURY_PRESET.")
    p_eval.add_argument("--jury", action="store_true",
                        help="Use the full 3-judge panel (openai,anthropic,gemini); "
                             "same as JURY_PRESET=panel. Enables reliability stats.")
    p_eval.add_argument("--input-mode", choices=EVAL_INPUT_MODES, default=None,
                        help=("Where to read summaries from. NOTE: --mode dev defaults "
                              "to 'dev-jsonl' (reads data/dev_summaries_jsonl/, writes "
                              "data/dev_evals_jsonl/) regardless of EVAL_INPUT_MODE; pass "
                              "--input-mode explicitly to override. 'jsonl': "
                              "data/summaries.jsonl, the original journal-stratified "
                              "pipeline. 'dev': data/dev_tests/summaries_txt (summarize-all "
                              "output). 'regular': data/summaries_txt. 'dev-jsonl': the "
                              "folder-driven dev loop. 'auto': 'dev' when --mode is dev, "
                              "else 'regular'. Overrides EVAL_INPUT_MODE from .env for this "
                              "run. PDF-input comparison files are never evaluated."))
    p_eval.add_argument("--source-dir", type=Path, default=None,
                        help="dev-jsonl mode only: read the papers to judge from this "
                             "readable-summary folder instead of the default "
                             "data/dev_summaries_jsonl/. Use it to judge a "
                             "`summarize --mode dev --output-subdir NAME` pool "
                             "(data/dev_summaries_jsonl/NAME/), or "
                             "data/single_summaries_jsonl/ and "
                             "data/batch_summaries_jsonl/. Results still go to "
                             "data/dev_evals_jsonl/, so the incremental skip works "
                             "across sources. This is the judge-side twin of "
                             "pilot_human_review's --dev-summaries-dir.")
    p_eval.add_argument("--no-resume", action="store_true",
                        help="Re-evaluate even pairs already in evaluations.jsonl. In "
                             "dev-jsonl mode this also re-includes papers already in "
                             "data/dev_evals_jsonl/ for a full re-judge.")
    p_eval.add_argument("--force", action="store_true",
                        help="Bypass confirmation. USE WITH CAUTION.")
    p_eval.set_defaults(func=cmd_evaluate)

    p_status = sub.add_parser("status", help="Print per-stage counts.")
    _add_mode_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_report = sub.add_parser("eval-report", help="Summarize evaluation rows by model and strata.")
    _add_mode_arg(p_report)
    p_report.add_argument("--publication", action="store_true",
                          help="Emit publication tables instead of the operator summary: "
                               "provider comparison with 95%% bootstrap CIs, paired "
                               "Wilcoxon/Friedman significance tests, per-stratum and "
                               "processed-vs-PDF breakdowns, and cost-per-quality-point. "
                               "Writes JSON + Markdown + a folder of CSVs. Offline; --no-detail "
                               "and --human-validation-mode do not apply.")
    p_report.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    p_report_output = p_report.add_mutually_exclusive_group()
    p_report_output.add_argument("--json", action="store_true",
                          help="Print full report JSON instead of compact text.")
    p_report_output.add_argument("--markdown", action="store_true",
                          help="Print a plain-English Markdown report; also saves it and a "
                               "companion per-article detail file to --results-dir.")
    p_report.add_argument("--no-detail", action="store_true",
                          help="With --markdown, skip the per-article detail file.")
    p_report.add_argument("--human-validation-mode",
                          choices=("per_reviewer", "pooled", "both"), default=None,
                          help="How to report human-vs-jury correlation: per_reviewer "
                               "(default; each reviewer validated on their own scores), "
                               "pooled (all reviewers averaged), or both. Overrides "
                               "HUMAN_VALIDATION_MODE from .env.")
    p_report.add_argument("--human-reviews", type=Path, default=None,
                          help="Normalized human-review rows to validate against "
                               "(default: data/human_reviews.jsonl; missing is fine). "
                               "Point at data/pilot_human_reviews.jsonl to inspect a "
                               "pilot rehearsal instead of the real study. Does not "
                               "apply with --publication.")
    p_report.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                          help="Where to save the report snapshot (default: data/results/).")
    p_report.add_argument("--no-save", action="store_true",
                          help="Print only; don't write a report file.")
    p_report.set_defaults(func=cmd_eval_report)

    p_figures = sub.add_parser(
        "report-figures",
        help="Render publication figures (PNG/SVG) and export a VetHELM-style leaderboard.",
    )
    _add_mode_arg(p_figures)
    p_figures.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    p_figures.add_argument("--summaries", type=Path, default=None,
                           help="Source of summariser token counts for cost columns "
                                "(default: data/summaries.jsonl).")
    p_figures.add_argument("--human-reviews", type=Path, default=None,
                           help="Normalized human-review rows "
                                "(default: data/human_reviews.jsonl; missing is fine).")
    p_figures.add_argument("--seed", type=int, default=None,
                           help="Bootstrap seed (default: PUBLICATION_BOOTSTRAP_SEED or 42).")
    p_figures.add_argument("--bootstrap-resamples", type=int, default=None,
                           help="Bootstrap resamples per CI "
                                "(default: PUBLICATION_BOOTSTRAP_RESAMPLES or 2000).")
    p_figures.add_argument("--cost-batched", action="store_true",
                           help="Price summaries at batch (50%%-off) rates. "
                                "Overrides PUBLICATION_COST_BATCHED.")
    p_figures.add_argument("--no-figures", action="store_true",
                           help="Write only the leaderboard JSON/Markdown; skip PNG/SVG figures.")
    p_figures.add_argument("--formats", default=None,
                           help="Comma-separated figure formats "
                                "(default: PUBLICATION_FIGURE_FORMATS or 'png,svg').")
    p_figures.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                           help="Where to save the leaderboard + figures (default: data/results/).")
    p_figures.set_defaults(func=cmd_report_figures)

    p_stats = sub.add_parser(
        "stats-engine",
        help="Information density, Cohen's Kappa IRR, subscription economics, "
             "and provider x covariate tables (species/study_design/journal).",
    )
    _add_mode_arg(p_stats)
    p_stats.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    p_stats.add_argument("--summaries", type=Path, default=None,
                         help="Source of manifest abstracts + candidate summaries "
                              "for information density (default: data/summaries.jsonl).")
    p_stats.add_argument("--human-reviews", type=Path, default=None,
                         help="Normalized human-review rows for Cohen's Kappa "
                              "(default: data/human_reviews.jsonl; missing is fine).")
    p_stats.add_argument("--subscription-price-usd", type=float, default=None,
                         help="Override SUBSCRIPTION_COST_PER_MONTH_USD (default: $20/month).")
    p_stats.add_argument("--papers-per-month", type=int, default=None,
                         help="Override SUBSCRIPTION_PAPERS_PER_MONTH (default: 500).")
    p_stats_output = p_stats.add_mutually_exclusive_group()
    p_stats_output.add_argument("--json", action="store_true", help="Print full report JSON.")
    p_stats_output.add_argument("--markdown", action="store_true", help="Print the Markdown report.")
    p_stats.add_argument("--results-dir", type=Path, default=RESULTS_DIR,
                         help="Where to save the report (default: data/results/).")
    p_stats.add_argument("--no-save", action="store_true",
                         help="Print only; don't write a report file.")
    p_stats.set_defaults(func=cmd_stats_engine)

    p_human = sub.add_parser(
        "export-human-review",
        help="Export ONE MORE blind human-validation reviewer folder (humanN/) "
             "from data/evaluations.jsonl.",
    )
    _add_mode_arg(p_human)
    p_human.add_argument("--sample-size", type=int, default=None,
                         help="Articles to sample (must be a multiple of the number "
                              "of study journals, e.g. 5/10/25). Default: "
                              "HUMAN_REVIEW_SAMPLE_SIZE from .env, or an interactive "
                              "5/10/25 prompt at a real terminal.")
    p_human.add_argument("--overlap-ratio", type=float, default=None,
                         help="Fraction of the previous tester's articles to carry "
                              "over, balanced per journal (default: "
                              "HUMAN_REVIEW_OVERLAP_RATIO, or 0.6). 1.0 = identical "
                              "items every run, 0.0 = a fresh draw each run.")
    p_human.add_argument("--seed", type=int, default=None,
                         help="Sampling seed (default: HUMAN_REVIEW_SEED from .env, or 42).")
    p_human.set_defaults(func=cmd_export_human_review)

    p_ingest = sub.add_parser(
        "ingest-human-review",
        help="Ingest filled reviewer scoresheets into data/human_reviews.jsonl.",
    )
    _add_mode_arg(p_ingest)
    p_ingest.add_argument("--review-dir", type=Path, default=None,
                          help="Export directory with humanN/ folders and their "
                               "sibling unblinding_key_human*.json files "
                               "(default: data/human_review/).")
    p_ingest.add_argument("--output", type=Path, default=None,
                          help="Normalized JSONL output path "
                               "(default: data/human_reviews.jsonl).")
    p_ingest.set_defaults(func=cmd_ingest_human_review)

    p_pilot = sub.add_parser(
        "export-pilot-human-review",
        help="Export ONE more pilot reviewer folder (humanN/) from the dev-summary pool "
             "-- trial the human-validation workflow before the real export.",
    )
    _add_mode_arg(p_pilot)
    p_pilot.add_argument("--sample-size", type=int, default=None,
                         help="Articles to sample (must be a multiple of the number "
                              "of study journals, e.g. 5/10/25). Default: "
                              "PILOT_HUMAN_REVIEW_SAMPLE_SIZE from .env, or an "
                              "interactive 5/10/25 prompt at a real terminal.")
    p_pilot.add_argument("--overlap-ratio", type=float, default=None,
                         help="Fraction of the previous tester's articles to carry "
                              "over (default: PILOT_HUMAN_REVIEW_OVERLAP_RATIO, or "
                              "0.6). 1.0 = identical items every run, 0.0 = a fresh "
                              "draw each run.")
    p_pilot.add_argument("--seed", type=int, default=None,
                         help="Base sampling seed (default: PILOT_HUMAN_REVIEW_SEED "
                              "from .env, or 42).")
    p_pilot.set_defaults(func=cmd_export_pilot_human_review)

    p_pilot_ingest = sub.add_parser(
        "ingest-pilot-human-review",
        help="Ingest filled PILOT reviewer scoresheets into data/pilot_human_reviews.jsonl "
             "-- never the real study ledger.",
    )
    _add_mode_arg(p_pilot_ingest)
    p_pilot_ingest.add_argument("--review-dir", type=Path, default=None,
                                help="Pilot export directory with humanN/ folders and their "
                                     "sibling unblinding_key_human*.json files "
                                     "(default: data/pilot_human_review/).")
    p_pilot_ingest.add_argument("--output", type=Path, default=None,
                                help="Normalized pilot JSONL output path "
                                     "(default: data/pilot_human_reviews.jsonl).")
    p_pilot_ingest.set_defaults(func=cmd_ingest_pilot_human_review)

    p_clean = sub.add_parser("clean", help="Delete temporary batch JSONL files.")
    p_clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
