# run_phase3.py

## What it does

The Phase 3 orchestrator CLI. One entry point with five subcommands; every subcommand prints the active `PHASE3_MODE` banner before doing anything so you cannot run a paid call without seeing the mode first.

```
run_phase3.py extract       → prepare_texts.main()
run_phase3.py summarize     → summarizer.main()  (or --estimate → cost_estimator)
run_phase3.py evaluate      → evaluator.main()
run_phase3.py status        → reads jsonl files, prints counts
run_phase3.py clean         → deletes data/batch/*.jsonl scratch files
```

Calling `run_phase3.py <cmd>` and calling the underlying script directly do exactly the same thing. The orchestrator is just for the muscle-memory of "I want to do Phase 3 stuff" without remembering which file each step lives in.

## When to run it

Every time you want to make Phase 3 progress. Read `docs/phase3/README.md`'s "Three named recipes" — they're all written against this script.

## Inputs

Whatever the delegated subcommand needs. See the per-script docs.

## Outputs

Whatever the delegated subcommand writes. See the per-script docs.

## CLI

```powershell
python llm-sum/run_phase3.py --help               # top-level help
python llm-sum/run_phase3.py extract --help       # extract args
python llm-sum/run_phase3.py summarize --help     # summarize args
python llm-sum/run_phase3.py evaluate --help      # evaluate args

# Common combinations:
python llm-sum/run_phase3.py extract
python llm-sum/run_phase3.py summarize --estimate
python llm-sum/run_phase3.py summarize --mode single
python llm-sum/run_phase3.py summarize --mode dev --limit 3
python llm-sum/run_phase3.py summarize --mode batch
python llm-sum/run_phase3.py evaluate --mode dev
python llm-sum/run_phase3.py status
python llm-sum/run_phase3.py clean
```

Every subcommand accepts `--mode {test,single,dev,batch}` and most accept `--limit N`.

## The mode banner

Printed by every subcommand on entry. Example:

```
[phase3] mode=dev | limit=5 | real-time | confirm-required
```

If you don't see this banner, the command didn't actually start. If you see the wrong mode, abort and fix `.env` or pass `--mode`.

## status subcommand

`status` is read-only and useful any time:

```powershell
python llm-sum/run_phase3.py status
```

```
[phase3] mode=test | DRY_RUN
[phase3:status] data/processed/*.jsonl : 247 files
[phase3:status] data/summaries.jsonl  : 247 papers
        openai: success=247 failed=0 pending=0
     anthropic: success=247 failed=0 pending=0
        gemini: success=247 failed=0 pending=0
[phase3:status] data/evaluations.jsonl: 741 rows (3 flagged for human review)
```

## clean subcommand

Removes `data/batch/*.jsonl` — the scratch JSONLs used to upload batch requests. They are not needed after a batch is submitted (the provider has them) and they can clutter the directory if you run many `--mode batch` iterations. **Does not touch** `summaries.jsonl`, `evaluations.jsonl`, or `batch_jobs.jsonl`.

```powershell
python llm-sum/run_phase3.py clean
# → [phase3:clean] removed 4 files from data/batch
```

## Worked example: end-to-end batch run

```powershell
$env:PHASE3_MODE = "batch"

python llm-sum/run_phase3.py extract              # ~1 minute
python scripts/verify_extraction.py               # confirm no FAILs
python llm-sum/run_phase3.py summarize --estimate # confirm projected cost
python llm-sum/run_phase3.py summarize            # type 'yes'; submits batch jobs

# Hours later (or next day):
python llm-sum/check_batch_status.py              # collect results
python llm-sum/run_phase3.py evaluate             # judge the collected summaries
python llm-sum/run_phase3.py status               # final counts
python llm-sum/run_phase3.py clean                # tidy up
```
