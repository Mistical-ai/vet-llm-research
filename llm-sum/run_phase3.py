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
PROCESSED_DIR = DATA_DIR / "processed"
SUMMARIES_PDF_DIR = DATA_DIR / "summaries_pdf"
SUMMARIES_TXT_DIR = DATA_DIR / "summaries_txt"


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
# evaluate
# ---------------------------------------------------------------------------

def cmd_evaluate(args: argparse.Namespace) -> int:
    profile = resolve_mode(args.mode)
    print(profile.banner())

    from evaluator import main as evaluate_main
    delegate = ["--mode", profile.name]
    if args.limit is not None:
        delegate += ["--limit", str(args.limit)]
    if args.force:
        delegate.append("--force")
    if args.no_resume:
        delegate.append("--no-resume")
    if args.judges:
        delegate += ["--judges", args.judges]
    return evaluate_main(delegate)


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

    In ``single`` and ``dev`` mode this processes one matched article stem that
    exists in both folders, yielding six summaries for the same DOI/title: one
    PDF source and one processed-text source, each across three providers.

    Outputs:
      data/summaries_pdf/<stem>.txt  — one readable file per PDF
      data/summaries_txt/<stem>.txt  — one readable file per processed text
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

    # summarize-all is the manual PDF-vs-processed comparison workflow. Keep
    # its default narrow in test/single/dev: 1 matched article x 2 source types
    # x 3 providers = 6 summaries. Users can still pass --limit N explicitly
    # when they want more matched article pairs.
    effective_limit = (
        args.limit
        if args.limit is not None
        else (1 if profile.name in {"test", "single", "dev"} else profile.paper_limit)
    )
    if args.limit is None and profile.name in {"test", "single", "dev"}:
        print(f"[phase3:summarize-all] {profile.name} mode defaults to --limit 1; "
              "pass --limit N to create more matched comparison files.")
    random_match = _summarize_all_random_match()
    unique_output = _summarize_all_unique_output()
    paired_stems = _paired_summary_stems(effective_limit, random_match=random_match)
    if not paired_stems:
        print("[phase3:summarize-all] No matching article stems found between "
              "data/raw/*.pdf and data/processed/*.jsonl.")
        return 1
    print(f"[phase3:summarize-all] paired articles selected: {len(paired_stems)}")
    print(f"[phase3:summarize-all] random match: {str(random_match).lower()}")
    output_suffix = _summary_run_suffix() if unique_output else None
    if output_suffix:
        print(f"[phase3:summarize-all] output run suffix: {output_suffix}")
    else:
        print("[phase3:summarize-all] unique output disabled; writing <stem>.txt files.")

    pdf_counts = summarize_all_pdfs(
        output_dir=SUMMARIES_PDF_DIR,
        providers=providers,
        resume=args.resume,
        prompt_template=prompt_templates,
        limit=effective_limit,
        stems=paired_stems,
        output_suffix=output_suffix,
    )

    txt_counts = summarize_all_processed_texts(
        output_dir=SUMMARIES_TXT_DIR,
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
             "Outputs readable .txt files in data/summaries_pdf/ and data/summaries_txt/.",
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
    p_sum_all.set_defaults(func=cmd_summarize_all)

    p_eval = sub.add_parser("evaluate", help="Run the blind judge.")
    _add_mode_arg(p_eval)
    p_eval.add_argument("--limit", type=int, default=None,
                        help="Override the mode's paper_limit.")
    p_eval.add_argument("--judges", default=None,
                        help="Comma-separated judge provider keys.")
    p_eval.add_argument("--no-resume", action="store_true",
                        help="Re-evaluate even pairs already in evaluations.jsonl.")
    p_eval.add_argument("--force", action="store_true",
                        help="Bypass confirmation. USE WITH CAUTION.")
    p_eval.set_defaults(func=cmd_evaluate)

    p_status = sub.add_parser("status", help="Print per-stage counts.")
    _add_mode_arg(p_status)
    p_status.set_defaults(func=cmd_status)

    p_clean = sub.add_parser("clean", help="Delete temporary batch JSONL files.")
    p_clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
