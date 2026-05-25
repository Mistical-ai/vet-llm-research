#!/usr/bin/env python3
"""
Script: auto_ingest_workflow.py
Purpose: One command from data/incoming_manuals/ to an updated corpus in data/raw/.

Design choices explained (deeper detail in README § Design Decisions):
- Why run enrich_manifest_from_pdfs.py before ingest? Manual PDFs often arrive
  before their DOI was collected in bulk; per-PDF CrossRef lookup appends missing
  manifest rows so ingest can match without hand-editing manifest.jsonl.
- Why retry failed/ at the start? A previous run may have failed only because the
  manifest was incomplete; enrichment plus retry avoids permanent false negatives.
- Why subprocess calls instead of imports? Each step is a standalone script researchers
  can run alone; subprocess keeps logs visible and exit codes isolated.
- Why supplement.py after pipeline.py? The missing-papers CSV should reflect PDFs
  just ingested into raw/, not the pre-ingest gap list.
- Why archive failed/ at the end? Preserves evidence by date without cluttering the
  inbox; skipped duplicates are deleted because they are benign.

Researcher steps: drop PDFs in data/incoming_manuals/ → run this script → read
data/logs/auto_ingest_workflow.log and pipeline.py output.

See README \"Guide: Manually adding papers to your corpus\".
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

MANUAL_INBOX = REPO_ROOT / "data" / "manual_inbox"
SKIPPED_DIR = MANUAL_INBOX / "skipped_existing_in_raw"
FAILED_DIR = MANUAL_INBOX / "failed"
ARCHIVE_FAILED_ROOT = MANUAL_INBOX / "archive_failed"

LOG_PATH = REPO_ROOT / "data" / "logs" / "auto_ingest_workflow.log"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)


def log_line(message: str, *, echo: bool = True) -> None:
    """Append timestamped diagnostics to disk + stdout."""
    rendered = f"[{_utc_stamp()}] {message}"
    if echo:
        print(rendered)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(rendered + "\n")


def _unique_path(folder: Path, filename: str) -> Path:
    """Avoid overwriting siblings when staging inbound PDFs."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower() or ".pdf"
    counter = 1
    while True:
        probe = folder / f"{stem}__incoming_dup{counter}{suffix}"
        if not probe.exists():
            return probe
        counter += 1


def _run_python_script(rel_script: Path, *, description: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``python <repo>/<rel_script>`` from repo root without swallowing stdout."""
    cmd = [sys.executable, str(REPO_ROOT / rel_script)]
    log_line(f"RUN {' '.join(cmd)}  # {description}")
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        check=False,
        text=True,
    )


def _retry_failed_pdfs(failed_dir: Path, inbox_root: Path) -> list[str]:
    """
    Move PDFs from manual_inbox/failed/ back to the inbox root for another attempt.

    Why at workflow start? Enrichment may add the manifest row that failed ingest
    last time; retrying before new incoming files avoids leaving fixable PDFs buried.
    """
    if not failed_dir.exists():
        log_line("retry_failed: failed/ does not exist — nothing to retry.")
        return []

    pdf_candidates = sorted(
        p for p in failed_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )

    if not pdf_candidates:
        log_line("retry_failed: failed/ is empty — nothing to retry.")
        return []

    retried: list[str] = []
    for pdf_path in pdf_candidates:
        destination = _unique_path(inbox_root, pdf_path.name)
        shutil.move(str(pdf_path), str(destination))
        detail = f"failed/{pdf_path.name} → manual_inbox/{destination.name}"
        retried.append(detail)
        log_line(f"retry_failed: moved {detail}")

    log_line(f"retry_failed: returned {len(retried)} PDF(s) to inbox for retry.")
    return retried


def _stage_incoming_pdfs(incoming_root: Path, inbox_root: Path) -> list[str]:
    """
    Move ``incoming_root/*.pdf`` into ``inbox_root``.

    Returns log-friendly summaries for each moved object (empty list if none).
    """
    inbox_root.mkdir(parents=True, exist_ok=True)

    transfers: list[str] = []
    pdf_candidates = sorted(
        path for path in incoming_root.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"
    )

    if not pdf_candidates:
        log_line("stage_incoming: no PDF files detected — continuing.")
        return transfers

    for pdf_path in pdf_candidates:
        destination = _unique_path(inbox_root, pdf_path.name)
        shutil.move(str(pdf_path), str(destination))
        detail = f"{pdf_path.name} → manual_inbox/{destination.name}"
        transfers.append(detail)
        log_line(f"stage_incoming: moved {detail}")

    log_line(f"stage_incoming: queued {len(transfers)} PDF(s).")
    return transfers


def _clean_manual_inbox(*, archive_day_folder: Path) -> None:
    """Archive skipped duplicates and failed PDFs into dated folders.

    Why archive instead of delete for skipped_existing_in_raw/?
    When DOI matching incorrectly identifies a new paper as a duplicate of one
    already ingested this same run, the file lands in skipped_existing_in_raw/.
    Deleting it would destroy a paper the researcher never recovered.  Archiving
    keeps it recoverable at archive_failed/<date>/skipped/ indefinitely.
    """
    archive_day_folder.mkdir(parents=True, exist_ok=True)

    # --- skipped_existing_in_raw: archive (not delete) ---------------------------
    if SKIPPED_DIR.exists():
        skipped_archive = archive_day_folder / "skipped"
        archived_skipped = 0
        for path in sorted(SKIPPED_DIR.iterdir()):
            if path.is_file():
                skipped_archive.mkdir(parents=True, exist_ok=True)
                target = _unique_path(skipped_archive, path.name)
                shutil.move(str(path), str(target))
                archived_skipped += 1
                rel_arc = target.relative_to(REPO_ROOT)
                log_line(f"clean: archived skipped/{path.name} → {rel_arc}")
        if archived_skipped == 0:
            log_line("clean: skipped_existing_in_raw already empty.")
    else:
        log_line("clean: skipped_existing_in_raw absent — nothing to archive.")

    # --- failed/: archive PDF evidence -------------------------------------------
    if not FAILED_DIR.exists():
        log_line("clean: failed/ absent — nothing to archive.")
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        return

    archived = 0
    for artifact in sorted(FAILED_DIR.iterdir()):
        target = _unique_path(archive_day_folder, artifact.name)
        shutil.move(str(artifact), str(target))
        archived += 1
        rel_arc = target.relative_to(REPO_ROOT)
        log_line(f"clean: archived failed/{artifact.name} → {rel_arc}")

    if archived == 0:
        log_line("clean: failed/ already empty.")

    FAILED_DIR.mkdir(parents=True, exist_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automate staging + ingestion + corpus reporting for manual OA PDF drops.",
    )
    parser.add_argument(
        "--incoming",
        type=Path,
        default=Path("data") / "incoming_manuals",
        help="Drop-zone folder for freshly downloaded PDFs (default: data/incoming_manuals/)",
    )
    parser.add_argument(
        "--no-supplement",
        action="store_true",
        help="Skip regenerating data/missing_papers.csv via supplement.py.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Preserve skipped_existing_in_raw + failed/ artifacts untouched.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    incoming = (REPO_ROOT / args.incoming).expanduser().resolve()
    inbox_root = MANUAL_INBOX.resolve()

    incoming.mkdir(parents=True, exist_ok=True)
    inbox_root.mkdir(parents=True, exist_ok=True)

    _configure_logging(LOG_PATH)

    log_line("=== auto_ingest_workflow start ===")

    # --- Retry previously failed PDFs -------------------------------------------
    # Move PDFs from failed/ back into the inbox BEFORE staging new ones so
    # they participate in manifest enrichment and get a fresh ingest attempt.
    retried = _retry_failed_pdfs(FAILED_DIR, inbox_root)

    # --- Stage incoming → inbox --------------------------------------------------
    transfers = _stage_incoming_pdfs(incoming, inbox_root)

    # --- Pre-ingest manifest enrichment -----------------------------------------
    # Scan all inbox PDFs, extract metadata DOIs, look up unknowns in CrossRef,
    # and append missing entries to manifest.jsonl BEFORE ingest runs.
    # Non-fatal: if this step fails, ingest still runs with the existing manifest.
    log_line("enrich_manifest: starting pre-ingest CrossRef enrichment…")
    enrich_proc = _run_python_script(
        Path("src") / "enrich_manifest_from_pdfs.py",
        description="pre-ingest manifest enrichment via CrossRef",
    )
    if enrich_proc.returncode != 0:
        log_line(
            "WARN enrich_manifest_from_pdfs.py exited non-zero; "
            "proceeding with ingest using existing manifest.",
            echo=True,
        )

    # --- ingest_manual_pdfs ------------------------------------------------------
    ingest_proc = _run_python_script(Path("src") / "ingest_manual_pdfs.py", description="manual ingest bridge")

    if ingest_proc.returncode != 0:
        log_line(
            f"ERROR ingest_manual_pdfs.py exited {ingest_proc.returncode}; "
            "incoming staging already consumed — inspect manual_inbox/failed/. "
            "pipeline.py NOT executed.",
            echo=True,
        )
        return ingest_proc.returncode

    # --- corpus snapshot ---------------------------------------------------------
    pipeline_proc = _run_python_script(Path("pipeline.py"), description="corpus counts")

    log_line(f"pipeline.py finished with exit code {pipeline_proc.returncode}")

    # --- supplement reconciliation -----------------------------------------------
    # Regenerate missing_papers.csv now that new PDFs are in data/raw/.
    # Running after pipeline.py means the CSV reflects the actual post-ingestion
    # state — papers just ingested are automatically excluded from the suggestions.
    # Failure is non-fatal: the PDFs are already ingested; this is reporting only.
    if args.no_supplement:
        log_line("SKIP supplement reconciliation (--no-supplement).")
    else:
        supplement_proc = _run_python_script(
            Path("src") / "supplement.py",
            description="reconcile missing_papers.csv post-ingestion",
        )
        log_line(f"supplement.py finished (exit code {supplement_proc.returncode})")

    # --- housekeeping -------------------------------------------------------------
    if args.no_clean:
        log_line("SKIP inbox cleanup (--no-clean).")
    else:
        archive_day_folder = ARCHIVE_FAILED_ROOT / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rel_arc_root = archive_day_folder.relative_to(REPO_ROOT)
        log_line(f"clean: archiving failures under {rel_arc_root}")
        _clean_manual_inbox(archive_day_folder=archive_day_folder)

    log_line(
        f"=== auto_ingest_workflow complete "
        f"(retried={len(retried)}, staged={len(transfers)}) pipeline_rc={pipeline_proc.returncode} ==="
    )

    # Preserve pipeline semantics for downstream CI hooks while signalling ingest OK.
    return pipeline_proc.returncode


if __name__ == "__main__":
    sys.exit(main())
