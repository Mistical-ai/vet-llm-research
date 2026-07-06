"""
llm-sum/run_phase3.py — Phase 3 orchestrator CLI
==================================================

One CLI for every Phase 3 step:
    extract     Build / refresh the data/processed/*.jsonl cache from PDFs.
    summarize   Run the three summarisers; supports --estimate, --resume, --force.
    summarize-all
                Run paired raw-PDF and processed-text summaries, producing
                readable text files with three provider outputs per source.
    evaluate    Run the blind judge over data/summaries.jsonl.
    eval-report Summarize MedHELM-style evaluation rows by clinical strata.
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

from models_config import all_providers  # noqa: E402
from phase3_mode import resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"
RAW_DIR = DATA_DIR / "raw"
SUMMARIES_PDF_DIR = DATA_DIR / "summaries_pdf"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"
DEV_TESTS_DIR = DATA_DIR / "dev_tests"
DEV_TESTS_SUMMARIES_PDF_DIR = DEV_TESTS_DIR / "summaries_pdf"
DEV_TESTS_SUMMARIES_TXT_DIR = DEV_TESTS_DIR / "summaries_txt"
SUMMARIZE_ALL_OUTPUT_SETS = ("auto", "regular", "dev-tests")


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
    print(f"[phase3] WARNING: invalid SUMMARIZE_ALL_OUTPUT_SET={raw!r}; " "using 'auto'.")
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

    return prepare_main(
        ["--manifest", str(args.manifest)] + (["--limit", str(args.limit)] if args.limit else [])
    )


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
            print(
                "[phase3:estimate] Direct PDF cost cannot be estimated offline. "
                "Run single mode to record real token counts from the providers."
            )
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


def _load_summaries_by_journal(summaries_path: Path, manifest_path: Path) -> dict[str, list[dict]]:
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
        journal = str(entry.get("journal", "")).strip().lower() or doi_to_journal.get(
            doi, "unknown"
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


def _print_reveal_table(
    doi_filter: set[str],
    journal_map: dict[str, list[str]],
    evaluations_path: Path = EVALUATIONS_PATH,
) -> None:
    """Print the post-evaluation identity reveal table.

    Called AFTER run_evaluation() completes — never before — so model
    identities are not visible during scoring (blind protocol).
    """
    if not evaluations_path.exists():
        return

    # Build lookup: doi → list of evaluation rows
    rows_by_doi: dict[str, list[dict]] = {}
    with open(evaluations_path, encoding="utf-8") as f:
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
    print(
        f"  {'Journal':<16} {'Summarizer':<14} {'Judge':<10} "
        f"{'Score':>5}  {'Halluc':>6}  {'Review?':>7}"
    )
    print("  " + "-" * 64)

    total_cost = 0.0
    judge_model_seen: set[str] = set()
    for doi in sorted(doi_filter):
        journal = doi_to_journal.get(doi, "unknown")
        for row in rows_by_doi.get(doi, []):
            score_disp = f"{row.get('composite_score', row.get('quality_score', '?'))}"
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
            print(
                f"  {journal:<16} {summarizer:<14} {judge:<10} "
                f"{score_disp:>5}  {str(halluc):>6}  {review:>7}"
            )

    print("=" * 68)
    if judge_model_seen:
        print(f"  Judge model version(s): {', '.join(sorted(judge_model_seen))}")
    if total_cost > 0:
        print(f"  Total cost this run: ${total_cost:.4f}")
    print("=" * 68 + "\n")


def _collect_resolved_judge_versions(path: Path, judges: list[str]) -> dict[str, str]:
    """Read observed judge model versions from an evaluation artifact."""
    versions: dict[str, str] = {}
    if not path.exists():
        return versions
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            judge = str(row.get("judge") or "")
            version = str(row.get("judge_model_version") or "")
            if judge in judges and version:
                versions.setdefault(judge, version)
    return versions


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def cmd_evaluate(args: argparse.Namespace) -> int:
    profile = resolve_mode(args.mode)
    print(profile.banner())

    judges = [j.strip() for j in args.judges.split(",") if j.strip()] if args.judges else None
    active_judges = judges or [
        j.strip() for j in os.getenv("JUDGE_MODELS", "openai").split(",") if j.strip()
    ]

    # Optional reproducibility mode: load a frozen DOI set and/or write a run
    # manifest under runs/<run_id>. When omitted, the historical data/*.jsonl
    # behavior is preserved.
    run_dir = None
    manifest_path = None
    output_path = None
    frozen_dois: set[str] | None = None
    selected_instance_ids: list[str] = []
    dataset_digest = None
    if args.frozen_set:
        from validation.frozen_sets import FrozenSetChecksumError, load_frozen_set
        from core.hashing import dataset_hash

        try:
            frozen_rows = load_frozen_set(args.frozen_set, require_manifest=True)
        except FrozenSetChecksumError as exc:
            print(f"[phase3:evaluate] {exc}")
            return 1
        frozen_dois = {str(row.get("doi", "")).strip() for row in frozen_rows if row.get("doi")}
        selected_instance_ids = [
            str(row.get("instance_id") or row.get("doi"))
            for row in frozen_rows
            if row.get("instance_id") or row.get("doi")
        ]
        dataset_digest = dataset_hash(frozen_rows)

    if args.run_id or args.run_dir:
        from core.constants import get_random_seed
        from core.run_manifest import build_run_manifest, resolve_run_dir, write_run_manifest

        run_dir = resolve_run_dir(args.run_id, args.run_dir)
        output_path = run_dir / "evaluations.jsonl"
        seed = get_random_seed()
        manifest = build_run_manifest(
            run_id=run_dir.name,
            benchmark_name=os.getenv("EVAL_BENCHMARK_NAME", "vet_lit_summary_medhelm"),
            mode=profile.name,
            random_seed=seed,
            cli_args=sys.argv[1:],
            selected_instance_ids=selected_instance_ids,
            prompt_paths=[os.getenv("JUDGE_PROMPT_FILE", "llm-sum/prompts/judge_medhelm_v1.txt")],
            config_paths=[
                "configs/phase4_medhelm_eval.yaml",
                "llm-sum/eval_config/medhelm_vet_summary.yaml",
            ],
            dataset_hash=dataset_digest,
            model_ids={judge: os.getenv(f"{judge.upper()}_MODEL", "") for judge in active_judges},
            artifact_paths={"evaluations": str(output_path)},
        )
        manifest_path = write_run_manifest(manifest, run_dir)

    # test mode: simple delegation, no journal stratification (mock, $0)
    if profile.name == "test" or profile.dry_run:
        from evaluator import run_evaluation

        counts = run_evaluation(
            judges=judges,
            resume=not args.no_resume,
            paper_limit=args.limit if args.limit is not None else profile.paper_limit,
            doi_filter=frozen_dois,
            output_path=output_path,
        )
        if manifest_path is not None:
            from core.run_manifest import finalize_run_manifest

            finalize_run_manifest(
                manifest_path,
                artifact_paths={
                    "evaluations": str(output_path),
                    "run_manifest": str(manifest_path),
                },
                selected_instance_ids=selected_instance_ids or sorted(frozen_dois or []),
                resolved_model_versions=_collect_resolved_judge_versions(
                    output_path, active_judges
                ),
            )
        return 0 if counts.get("failed", 0) == 0 else 1

    # single / dev / batch modes: journal-stratified sampling
    from evaluator import run_evaluation, confirm_real_judge

    if frozen_dois is not None:
        by_journal = {}
        doi_filter = frozen_dois
        journal_map = {"frozen_set": sorted(frozen_dois)}
    else:
        by_journal = _load_summaries_by_journal(SUMMARIES_PATH, MANIFEST_PATH)

    # Warn about and log unmapped DOIs
    unknown_entries = by_journal.pop("unknown", []) if by_journal else []
    if unknown_entries:
        unknown_dois = [str(e.get("doi", "?")) for e in unknown_entries]
        msg_lines = (
            [
                f"[phase3:evaluate] WARNING: {len(unknown_dois)} summaries could not be "
                "mapped to a journal and will be excluded from stratified sampling.",
                "  DOIs without a journal mapping in manifest.jsonl:",
            ]
            + [f"    {d}" for d in unknown_dois]
            + [
                "  Add the journal field to manifest.jsonl to include these articles.",
            ]
        )
        for line in msg_lines:
            print(line)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / "eval_journal_mapping.log"
        ts = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n[{ts}]\n" + "\n".join(msg_lines) + "\n")

    if frozen_dois is None and not by_journal:
        print(
            "[phase3:evaluate] No journal-mapped summaries found. "
            "Run 'extract' and 'summarize' first, or check manifest.jsonl."
        )
        return 1

    from core.constants import get_random_seed

    seed = get_random_seed()
    if frozen_dois is None:
        per_journal = (
            args.limit
            if args.limit is not None
            else (1 if profile.name == "single" else (profile.paper_limit or 1))
        )
        doi_filter, journal_map = _sample_by_journal(by_journal, per_journal, seed=seed)
    else:
        per_journal = len(frozen_dois)

    # Pre-evaluation summary (anonymized — journal names only, no DOIs, no model names)
    print("\n[phase3:evaluate] Journal-stratified sampling plan:")
    print(f"  {'Journal':<20}  Articles selected")
    print("  " + "-" * 38)
    for journal in sorted(journal_map):
        print(f"  {journal:<20}  {len(journal_map[journal])}")
    print(f"  {'TOTAL':<20}  {len(doi_filter)}")
    print(f"  (seed={seed}, per_journal={per_journal})\n")

    if not confirm_real_judge(profile, force=args.force):
        return 1

    counts = run_evaluation(
        judges=judges,
        resume=not args.no_resume,
        paper_limit=None,
        doi_filter=doi_filter,
        output_path=output_path,
    )

    if manifest_path is not None:
        from core.run_manifest import finalize_run_manifest

        finalize_run_manifest(
            manifest_path,
            artifact_paths={
                "evaluations": str(output_path),
                "run_manifest": str(manifest_path),
            },
            selected_instance_ids=selected_instance_ids or sorted(doi_filter),
            resolved_model_versions=_collect_resolved_judge_versions(output_path, active_judges),
        )

    _print_reveal_table(doi_filter, journal_map, output_path or EVALUATIONS_PATH)
    return 0 if counts.get("failed", 0) == 0 else 1


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
        print(
            f"    {prov:>10}: success={c.get('success',0)} "
            f"failed={c.get('failed',0)} pending={c.get('pending',0)}"
        )

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
    print(
        f"[phase3:status] data/evaluations.jsonl: {eval_total} rows "
        f"({requires_review} flagged for human review)"
    )
    return 0


def cmd_eval_report(args: argparse.Namespace) -> int:
    """Print read-only aggregate reports for MedHELM-style evaluation rows."""
    from eval_report import main as report_main

    delegate = ["--evaluations", str(args.evaluations)]
    if args.json:
        delegate.append("--json")
    if args.output_dir:
        delegate += ["--output-dir", str(args.output_dir)]
    if args.bootstrap_reps:
        delegate += ["--bootstrap-reps", str(args.bootstrap_reps)]
    if args.seed is not None:
        delegate += ["--seed", str(args.seed)]
    return report_main(delegate)


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
        print(
            "[phase3:summarize-all] batch mode is not supported because direct PDF "
            "summarisation is real-time only. Use --mode single or --mode dev."
        )
        return 1

    from summarizer import (
        summarize_all_pdfs,
        summarize_all_processed_texts,
        load_provider_prompt_templates_with_optional_guide,
        confirm_real_batch,
    )

    providers = (
        [p.strip() for p in args.providers.split(",") if p.strip()] if args.providers else None
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
        print(
            f"[phase3:summarize-all] {profile.name} mode defaults to --limit 1; "
            "pass --limit N to create more matched comparison files."
        )
    elif args.limit is None and profile.name == "dev":
        print(
            "[phase3:summarize-all] dev mode defaults to "
            f"PHASE3_DEV_LIMIT={effective_limit} matched article pairs."
        )
    random_match = _summarize_all_random_match()
    unique_output = _summarize_all_unique_output()
    paired_stems = _paired_summary_stems(effective_limit, random_match=random_match)
    if not paired_stems:
        print(
            "[phase3:summarize-all] No matching article stems found between "
            "data/raw/*.pdf and data/processed/*.jsonl."
        )
        return 1
    print(f"[phase3:summarize-all] paired articles selected: {len(paired_stems)}")
    if effective_limit is not None and len(paired_stems) < effective_limit:
        print(
            "[phase3:summarize-all] WARNING: requested "
            f"{effective_limit} matched pairs, but only found {len(paired_stems)}. "
            "Only stems present in both data/raw/*.pdf and "
            "data/processed/*.jsonl can be processed."
        )
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

    total = {
        k: pdf_counts.get(k, 0) + txt_counts.get(k, 0) for k in set(pdf_counts) | set(txt_counts)
    }
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
        p.add_argument(
            "--mode",
            choices=VALID_MODES,
            default=None,
            help="Override PHASE3_MODE for this run " "(test|single|dev|batch).",
        )

    p_extract = sub.add_parser("extract", help="Build data/processed/*.jsonl cache.")
    _add_mode_arg(p_extract)
    p_extract.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    p_extract.add_argument("--limit", type=int, default=None)
    p_extract.set_defaults(func=cmd_extract)

    p_sum = sub.add_parser("summarize", help="Run the three summarisers.")
    _add_mode_arg(p_sum)
    p_sum.add_argument(
        "--limit", type=int, default=None, help="Override the mode's paper_limit (e.g. --limit 1)."
    )
    p_sum.add_argument(
        "--estimate",
        action="store_true",
        help="Print projected cost (uses tiktoken on cached texts); no API calls.",
    )
    p_sum.add_argument(
        "--resume", action="store_true", help="Skip (doi, model) pairs already at status=success."
    )
    p_sum.add_argument(
        "--force", action="store_true", help="Bypass interactive confirmation. USE WITH CAUTION."
    )
    p_sum.add_argument("--providers", default=None, help="Comma-separated subset of providers.")
    p_sum.add_argument("--manifest", type=Path, default=None)
    p_sum.add_argument(
        "--input-source",
        choices=("processed", "raw_text", "pdf"),
        default="processed",
        help=(
            "Input to summarize: processed JSONL (default), raw_text JSONL, "
            "or pdf for direct PDF comparison in test/single only."
        ),
    )
    p_sum.add_argument(
        "--guide-summary",
        type=Path,
        default=None,
        help=(
            "Optional human-written format guide file. The guide controls "
            "section style only; facts must still come from the target paper."
        ),
    )
    p_sum.set_defaults(func=cmd_summarize)

    p_sum_all = sub.add_parser(
        "summarize-all",
        help="Summarize paired PDFs (data/raw/) and processed texts (data/processed/) "
        "with OpenAI, Anthropic, and Gemini. "
        "Outputs readable .txt files; dev mode defaults to data/dev_tests/.",
    )
    _add_mode_arg(p_sum_all)
    p_sum_all.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Override the paired article limit. Without this, "
            "test/single/dev process 1 matched PDF + 1 "
            "matched processed text."
        ),
    )
    p_sum_all.add_argument(
        "--resume", action="store_true", help="Skip provider slots already at status=success."
    )
    p_sum_all.add_argument(
        "--force", action="store_true", help="Bypass interactive confirmation. USE WITH CAUTION."
    )
    p_sum_all.add_argument(
        "--providers",
        default=None,
        help="Comma-separated subset of providers " "(default: openai,anthropic,gemini).",
    )
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
    p_eval.add_argument("--limit", type=int, default=None, help="Override the mode's paper_limit.")
    p_eval.add_argument("--judges", default=None, help="Comma-separated judge provider keys.")
    p_eval.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-evaluate even pairs already in evaluations.jsonl.",
    )
    p_eval.add_argument(
        "--force", action="store_true", help="Bypass confirmation. USE WITH CAUTION."
    )
    p_eval.add_argument(
        "--run-id", default=None, help="Optional immutable run id under runs/<run_id>."
    )
    p_eval.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Optional explicit output directory for run artifacts.",
    )
    p_eval.add_argument(
        "--frozen-set",
        type=Path,
        default=None,
        help="Optional frozen_sets/*.jsonl file; limits evaluation to those DOIs.",
    )
    p_eval.set_defaults(func=cmd_evaluate)

    p_status = sub.add_parser("status", help="Print per-stage counts.")
    _add_mode_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_report = sub.add_parser("eval-report", help="Summarize evaluation rows by model and strata.")
    p_report.add_argument("--evaluations", type=Path, default=EVALUATIONS_PATH)
    p_report.add_argument(
        "--json", action="store_true", help="Print full report JSON instead of compact text."
    )
    p_report.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional directory for summary.json and CSV report exports.",
    )
    p_report.add_argument(
        "--bootstrap-reps",
        type=int,
        default=1000,
        help="Bootstrap repetitions for confidence intervals.",
    )
    p_report.add_argument(
        "--seed", type=int, default=None, help="Deterministic seed for bootstrap resampling."
    )
    p_report.set_defaults(func=cmd_eval_report)

    p_clean = sub.add_parser("clean", help="Delete temporary batch JSONL files.")
    p_clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
