# Reproducibility Protocol

## Exact Rerun Steps

1. Check out the recorded git branch and SHA from `run_manifest.json`.
2. Create a clean Python environment.
3. Install dependencies from `requirements-dev-lock.txt` for tests or
   `requirements-lock.txt` for runtime-only use.
4. Verify the frozen-set checksum against its manifest sidecar.
5. Run Phase 3 in `PHASE3_MODE=test` first to confirm mocks and paths.
6. For live runs, execute the command manually in PowerShell after reviewing the
   budget guard and confirmation prompt.

## Required Artifacts

Each manuscript-grade run should archive:

- `runs/<run_id>/run_manifest.json`
- `runs/<run_id>/summaries.jsonl` when summarization was part of the run
- `runs/<run_id>/evaluations.jsonl`
- `runs/<run_id>/reports/summary.json`
- `runs/<run_id>/reports/*.csv`
- frozen set JSONL and manifest sidecar

## Environment Capture

The run manifest records:

- git branch and SHA,
- Python version and platform,
- dependency lock hash,
- prompt/config hashes,
- dataset/frozen-set hash,
- random seed,
- requested model IDs and observed model versions when available.

## Safety

Live API scripts must not be run by automation. CI and tests must use
`PHASE3_MODE=test` and `DRY_RUN=true`.
