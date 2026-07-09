"""
llm-sum/run_phase3.py — Phase 3 orchestrator CLI
==================================================

One CLI for every Phase 3 step:
    extract     Build / refresh the data/processed/*.jsonl cache from PDFs.
    summarize   Run the three summarisers; supports --estimate, --resume, --force.
    summarize-all
                Run paired raw-PDF and processed-text summaries, producing
                readable text files with three provider outputs per source.
    evaluate    Run the blind judge. Reads data/summaries.jsonl by default;
                set EVAL_INPUT_MODE (or pass --input-mode) to instead judge
                summarize-all's data/dev_tests/summaries_txt or
                data/summaries_txt comparison files.
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
SUMMARIZE_ALL_OUTPUT_SETS = ("auto", "regular", "dev-tests")
EVAL_INPUT_MODES = ("jsonl", "auto", "dev", "regular")


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

    Precedence: --input-mode (CLI) > EVAL_INPUT_MODE (.env) > 'jsonl' default
    (the original data/summaries.jsonl pipeline, unchanged).

    'auto' expands based on the active PHASE3_MODE, mirroring
    _summarize_all_output_dirs()'s existing auto behaviour: PHASE3_MODE=dev
    reads the dev_tests processed-text folder, every other mode reads the
    regular one. This is what makes 'switch to the regular folder once
    you're ready for the full corpus' a plain PHASE3_MODE change instead of
    an extra edit.
    """
    raw = (cli_override or os.getenv("EVAL_INPUT_MODE", "jsonl")).strip().lower()
    if raw == "auto":
        return "dev" if mode_name == "dev" else "regular"
    if raw in ("jsonl", "dev", "regular"):
        return raw
    print(f"[phase3] WARNING: invalid EVAL_INPUT_MODE={raw!r}; using 'jsonl'. "
          f"Valid: {EVAL_INPUT_MODES}")
    return "jsonl"


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
        import os
        from cost_estimator import run as run_estimate
        judges = [j.strip() for j in os.getenv("JUDGE_MODELS", "openai").split(",") if j.strip()]
        run_estimate(
            batched=profile.use_batch,
            judge_providers=judges,
            input_source=args.input_source,
        )
        return 0

    from summarizer import main as summarize_main
    delegate = ["--mode", profile.name]
    if args.limit is not None:
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
    return summarize_main(delegate)


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
    # > JUDGE_MODELS. Default stays a single openai judge.
    judges = evaluator.resolve_judges(args.judges, jury=args.jury)
    active_judges = judges
    model_ids = {j: get_model_spec(j).model_id for j in active_judges}
    if len(active_judges) > 1:
        print(f"[phase3:evaluate] jury of {len(active_judges)} judges: "
              f"{', '.join(active_judges)} (reliability stats will be reported)")

    input_mode = _resolve_eval_input_mode(profile.name, args.input_mode)
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

        per_journal = (
            args.limit
            if args.limit is not None
            else (1 if profile.name == "single" else (profile.paper_limit or 1))
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

        resolved_versions = resolve_model_versions(
            active_judges, model_ids, list(iter_evaluation_rows()),
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

        resolved_versions = resolve_model_versions(
            active_judges, model_ids, list(iter_evaluation_rows()),
        )
        finalize_run_manifest(
            manifest_path, resolved_model_versions=resolved_versions, status=status,
        )


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
    requires_review = 0
    if EVALUATIONS_PATH.exists():
        with open(EVALUATIONS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eval_total += 1
                if row.get("requires_human_review"):
                    requires_review += 1
    print(f"[phase3:status] data/evaluations.jsonl: {eval_total} rows "
          f"({requires_review} flagged for human review)")
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
# export-human-review
# ---------------------------------------------------------------------------

def cmd_export_human_review(args: argparse.Namespace) -> int:
    """Sample + export blind human-review packets from data/evaluations.jsonl.

    Offline only — no API calls are made. The mode banner is printed only for
    consistency with the other subcommands (same rationale as cmd_extract).
    """
    print(resolve_mode(args.mode).banner())
    from human_review import main as human_review_main

    delegate: list[str] = []
    if args.reviewers is not None:
        delegate += ["--reviewers", str(args.reviewers)]
    if args.sample_size is not None:
        delegate += ["--sample-size", str(args.sample_size)]
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
                        help=("Where to read summaries from. 'jsonl' (default): "
                              "data/summaries.jsonl, the original pipeline. "
                              "'dev': data/dev_tests/summaries_txt (summarize-all "
                              "output). 'regular': data/summaries_txt. 'auto': "
                              "'dev' when --mode is dev, else 'regular'. Overrides "
                              "EVAL_INPUT_MODE from .env for this run. PDF-input "
                              "comparison files are never evaluated."))
    p_eval.add_argument("--no-resume", action="store_true",
                        help="Re-evaluate even pairs already in evaluations.jsonl.")
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

    p_human = sub.add_parser(
        "export-human-review",
        help="Export blind human-validation review packets from data/evaluations.jsonl.",
    )
    _add_mode_arg(p_human)
    p_human.add_argument("--reviewers", type=int, default=None,
                         help="Number of independent reviewer copies to export "
                              "(default: HUMAN_REVIEWERS from .env, or 1).")
    p_human.add_argument("--sample-size", type=int, default=None,
                         help="Number of (paper, summary) items to sample "
                              "(default: HUMAN_REVIEW_SAMPLE_SIZE from .env, or 15).")
    p_human.add_argument("--seed", type=int, default=None,
                         help="Sampling seed (default: HUMAN_REVIEW_SEED from .env, or 42).")
    p_human.set_defaults(func=cmd_export_human_review)

    p_ingest = sub.add_parser(
        "ingest-human-review",
        help="Ingest filled reviewer scoresheets into data/human_reviews.jsonl.",
    )
    _add_mode_arg(p_ingest)
    p_ingest.add_argument("--review-dir", type=Path, default=None,
                          help="Export directory with reviewer_*/ folders and "
                               "unblinding_key.json (default: data/human_review/).")
    p_ingest.add_argument("--output", type=Path, default=None,
                          help="Normalized JSONL output path "
                               "(default: data/human_reviews.jsonl).")
    p_ingest.set_defaults(func=cmd_ingest_human_review)

    p_clean = sub.add_parser("clean", help="Delete temporary batch JSONL files.")
    p_clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
