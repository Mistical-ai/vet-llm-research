#!/usr/bin/env python3
"""
scripts/reset_phantom_pending_slots.py — un-strand summary rows whose
"pending" status has no batch job behind it.

Background
----------
``llm-sum/batch_utils.py::run_batch_summarisation`` used to initialise every
brand-new paper's provider slots with ``status="pending"`` the moment the
row was first created (via ``summarizer._empty_model_slot()``), *before* a
request was ever built or submitted. Combined with ``--resume``'s skip rule
(``status in ("success", "pending")``), this meant a paper's very first
appearance in ``summaries.jsonl`` could be silently skipped forever — its
slot looked identical to a genuinely in-flight batch submission, even
though no request was ever built or sent to the provider. That default has
been fixed (``_empty_model_slot()`` now starts at ``status=None``; "pending"
is set only once a request is actually queued for submission — see
``run_batch_summarisation``), but the fix is not retroactive: any slot the
old code already wrote to disk as "pending" without a real job behind it
stays stuck that way until reset.

This script finds those stranded ("phantom") slots and resets them back to
the not-yet-attempted state, so a subsequent ``--resume`` run will actually
build and submit a request for them.

How a slot is classified
-------------------------
``data/batch/{provider}_sum_*.jsonl`` is written by ``write_batch_jsonl``
immediately BEFORE every submission attempt — its existence only proves a
request was built, not that the provider actually accepted it (a rejected
submission, e.g. a 400 for an invalid custom_id, still leaves this file on
disk). So file existence alone is not reliable ground truth; a request file
whose line count doesn't match any recorded job is exactly what a rejected
submission looks like.

The reliable signal is ``data/batch_jobs.jsonl``, which only ever gets an
entry via ``record_batch_job()`` — called strictly AFTER a provider's batch
API accepted the submission. So for each provider, a ``{provider}_sum_*``
file's custom_ids only count as "genuinely submitted" if that file's line
count matches the ``request_count`` of at least one recorded job for that
provider. Any summaries.jsonl slot at ``status == "pending"`` whose
``custom_id`` isn't in that genuinely-submitted set has no accepted job
behind it — it's a phantom, safe to reset. Slots already covered by a real
accepted job (e.g. OpenAI's still-unresolved 180 pending, or plain
successes) are left completely untouched.

No live API calls, no network — pure local file reads plus one atomic
rewrite of summaries.jsonl.

Run:
    python scripts/reset_phantom_pending_slots.py            # dry-run report
    python scripts/reset_phantom_pending_slots.py --apply     # perform the reset
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
BATCH_DIR = DATA_DIR / "batch"
SUMMARIES_PATH = DATA_DIR / "summaries.jsonl"
BATCH_JOBS_PATH = DATA_DIR / "batch_jobs.jsonl"

PROVIDERS = ("openai", "anthropic", "gemini")


def _recorded_request_counts(provider: str) -> set[int]:
    """request_count values from every batch_jobs.jsonl row actually
    recorded for this provider's summarize stage — i.e. submissions the
    provider's API genuinely accepted (record_batch_job only runs after
    a successful submit call).
    """
    counts: set[int] = set()
    if not BATCH_JOBS_PATH.exists():
        return counts
    with BATCH_JOBS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                job = json.loads(line)
            except json.JSONDecodeError:
                continue
            if job.get("provider") == provider and job.get("stage") == "summarize":
                count = job.get("request_count")
                if isinstance(count, int):
                    counts.add(count)
    return counts


def _genuinely_submitted_custom_ids(provider: str) -> set[str]:
    """Union of custom_ids across every request file for this provider whose
    line count matches a request_count actually recorded in
    batch_jobs.jsonl — i.e. a submission the provider's API genuinely
    accepted, not merely built-then-rejected (see module docstring).
    """
    accepted_counts = _recorded_request_counts(provider)
    ids: set[str] = set()
    for path in sorted(BATCH_DIR.glob(f"{provider}_sum_*.jsonl")):
        lines = [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(lines) not in accepted_counts:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("custom_id")
            if cid:
                ids.add(cid)
    return ids


def find_phantom_slots() -> dict[str, list[dict]]:
    """Return {provider: [{"doi": ..., "custom_id": ...}, ...]} for every
    phantom-pending slot found in summaries.jsonl.
    """
    real_ids = {provider: _genuinely_submitted_custom_ids(provider) for provider in PROVIDERS}
    phantom: dict[str, list[dict]] = {provider: [] for provider in PROVIDERS}

    if not SUMMARIES_PATH.exists():
        return phantom

    with SUMMARIES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(entry.get("doi", "")).strip()
            custom_id = entry.get("custom_id")
            for provider in PROVIDERS:
                slot = (entry.get("models") or {}).get(provider) or {}
                if slot.get("status") != "pending":
                    continue
                if custom_id not in real_ids[provider]:
                    phantom[provider].append({"doi": doi, "custom_id": custom_id})
    return phantom


def _reset_slot() -> dict:
    return {
        "status": None,
        "summary": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_version": None,
        "timestamp": None,
    }


def apply_reset(phantom: dict[str, list[dict]]) -> int:
    phantom_dois_by_provider = {
        provider: {row["doi"] for row in rows} for provider, rows in phantom.items()
    }

    lines = SUMMARIES_PATH.read_text(encoding="utf-8").splitlines()
    reset_count = 0
    out_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        doi = str(entry.get("doi", "")).strip()
        models = entry.get("models") or {}
        for provider in PROVIDERS:
            if doi in phantom_dois_by_provider.get(provider, set()):
                models[provider] = _reset_slot()
                reset_count += 1
        out_lines.append(json.dumps(entry, ensure_ascii=False))

    tmp = SUMMARIES_PATH.with_suffix(SUMMARIES_PATH.suffix + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(SUMMARIES_PATH)
    return reset_count


def run(*, apply: bool) -> int:
    phantom = find_phantom_slots()
    total = sum(len(rows) for rows in phantom.values())

    print(f"[reset-phantom] {total} phantom-pending slot(s) found across {SUMMARIES_PATH}:")
    for provider, rows in phantom.items():
        print(f"  {provider}: {len(rows)}")
        for row in rows:
            print(f"    {row['doi']}")

    if total == 0:
        print("\n[reset-phantom] Nothing to reset.")
        return 0

    if not apply:
        print(f"\n[reset-phantom] DRY RUN — re-run with --apply to reset {total} slot(s).")
        return 0

    reset_count = apply_reset(phantom)
    print(f"\n[reset-phantom] Reset {reset_count} slot(s) to status=None in {SUMMARIES_PATH}.")
    print("[reset-phantom] Re-run `summarize --mode batch --providers <provider>,... --resume` "
          "to submit real requests for them.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reset summaries.jsonl slots stuck at status='pending' with no "
            "batch job behind them, so --resume can actually submit them."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the reset to summaries.jsonl. Default is a dry-run report only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    return run(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
