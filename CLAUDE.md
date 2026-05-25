# Notepad
# Project Environment & Security Rules

## Critical Constraints
- The `.env` file is strictly off-limits. It contains private supervisor API keys and is git-ignored. Do not attempt to read, display, or modify it.
- **Budget Protection:** You are strictly forbidden from automatically executing any script that makes live API calls (such as `extract.py` or `test_keys.py`).
- If the user asks you to "test" or "run" the extraction pipeline, you must write or update the code, confirm it is saved, and instruct the user to execute it manually in their separate PowerShell window.

## Allowed Commands
- You are fully authorized to use terminal commands for environment setup, `pip install`, `git` operations, running standard non-API unit tests, and creating/editing project files.

## Phase 3 Additions (LLM Summarisation & Evaluation)
- All Phase 3 code lives in the top-level `llm-sum/` folder (sibling of `src/`, `data/`, `tests/`). Outputs are written to `data/` (`summaries.jsonl`, `evaluations.jsonl`, `batch_jobs.jsonl`, `processed/`).
- Never execute any script that makes live API calls to OpenAI, Anthropic, or Gemini. This includes `llm-sum/summarizer.py`, `llm-sum/evaluator.py`, `llm-sum/check_batch_status.py`, and `llm-sum/run_phase3.py`. The user runs all live commands manually in PowerShell.
- Running unit tests under `tests/` is allowed because mocks replace every external API call. Verify mocking is in place before running.
- `DEVELOPMENT_MODE = True` is hardcoded at the top of `llm-sum/summarizer.py` and `llm-sum/evaluator.py`. Do not change this default in code; it is the last guardrail before a full-corpus run.
- All paid API calls must flow through `BudgetGuard.add_cost()` from `src/utils.py`. Token counts must come from the provider response, never from estimation.
- Append-only output: never overwrite `data/summaries.jsonl` or `data/evaluations.jsonl`. New writes append one JSON line at a time.
- The judge prompt must never contain a summariser model identifier. The blind protocol is non-negotiable for study validity.
