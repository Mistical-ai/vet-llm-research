"""
llm-sum/run_phase3.py — Phase 3 orchestrator CLI
==================================================

One CLI for every Phase 3 step:
    extract     Build / refresh the data/processed/*.jsonl cache from PDFs.
    summarize   Run the three summarisers; supports --estimate, --resume, --force.
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
import shutil
import sys
from pathlib import Path

from models_config import all_providers  # noqa: E402
from phase3_mode import resolve_mode, VALID_MODES  # noqa: E402

SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
EVALUATIONS_PATH = DATA_DIR / "evaluations.jsonl"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"


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

    if args.estimate:
        # Import lazily so a missing tiktoken / SDK doesn't break other commands.
        import os
        from cost_estimator import run as run_estimate
        judges = [j.strip() for j in os.getenv("JUDGE_MODELS", "openai").split(",") if j.strip()]
        run_estimate(batched=profile.use_batch, judge_providers=judges)
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
    processed = list(PROCESSED_DIR.glob("*.jsonl")) if PROCESSED_DIR.exists() else []
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
    p_sum.set_defaults(func=cmd_summarize)

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
