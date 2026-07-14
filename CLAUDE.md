# Notepad
# Project Environment & Security Rules

## Critical Constraints
- The `.env` file is strictly off-limits. It contains private supervisor API keys and is git-ignored. Do not attempt to read, display, or modify it.
- **Budget Protection:** You are strictly forbidden from automatically executing any script that makes live API calls (such as `extract.py` or `test_keys.py`).
- If the user asks you to "test" or "run" the extraction pipeline, you must write or update the code, confirm it is saved, and instruct the user to execute it manually in their separate PowerShell window.

## Allowed Commands
- You are fully authorized to use terminal commands for environment setup, `pip install`, `git` operations, running standard non-API unit tests, and creating/editing project files.

## Phase 3 Additions (LLM Summarisation & Evaluation)
- All Phase 3 code lives in the top-level `llm-sum/` folder (sibling of `src/`, `data/`, `tests/`). Outputs are written to `data/` (`summaries.jsonl`, `evaluations.jsonl`, `batch_jobs.jsonl`, `processed/`, `dev_summaries_jsonl/`, `dev_evals_jsonl/`).
- `data/dev_summaries_jsonl/` (new) is the readable `.txt` sibling of `data/summaries.jsonl` for just a `summarize --mode dev` run — one file per journal-random-selected paper, processedv2/raw_text only, no PDF. It is also the SOURCE that `evaluate --mode dev` reads to decide which papers to judge. Don't confuse it with `data/dev_tests/`, which is `summarize-all`'s PDF-vs-processed-text comparison output; see `.env.template` section 11 for the full distinction.
- `data/dev_evals_jsonl/` (new) is the readable `.txt` sibling of `data/evaluations.jsonl` for a `evaluate --mode dev` run — one file per judged paper (keyed by DOI slug), a section per (summarizer, judge). `evaluate --mode dev` skips papers already here so the dev sample grows incrementally; `--no-resume` forces a full re-judge. See `docs/phase3/dev_evaluation_guide.md` for the step-by-step loop.
- Never execute any script that makes live API calls to OpenAI, Anthropic, or Gemini. This includes `llm-sum/summarizer.py`, `llm-sum/evaluator.py`, `llm-sum/check_batch_status.py`, and `llm-sum/run_phase3.py`. The user runs all live commands manually in PowerShell.
- Running unit tests under `tests/` is allowed because mocks replace every external API call. Verify mocking is in place before running.
- **Phase 3 mode rule:** Claude may run Phase 3 scripts only with `PHASE3_MODE=test` (mocks only, no network). Modes `single`, `dev`, and `batch` make live API calls and must be executed manually by the user in PowerShell. The interactive confirm prompt is the last guardrail before a live run. The previous `DEVELOPMENT_MODE` constant has been removed; the same safety intent now lives in `llm-sum/phase3_mode.py` and the safest mode (`test`) is the default.
- All paid API calls must flow through `BudgetGuard.add_cost()` from `src/utils.py`. Token counts must come from the provider response, never from estimation.
- Output safety: `data/evaluations.jsonl` is append-only. `data/summaries.jsonl` is a JSONL snapshot that may be atomically rewritten when provider slots are merged for resume support; never hand-edit either file or replace them with a non-JSONL format.
- The judge prompt must never contain a summariser model identifier. The blind protocol is non-negotiable for study validity.
