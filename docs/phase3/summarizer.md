# summarizer.py

## What it does

For each paper in `data/manifest.jsonl`, sends text to **three LLMs** (OpenAI, Anthropic, Gemini) with identical prompts, identical temperature/seed, and identical max-output budget. By default it uses cleaned body text from `data/processed/`. For small comparison runs, `--input-source raw_text` sends the raw extracted JSONL from `data/raw_text/` instead, so you can compare quality and cost. Records the exact provider-returned model version, response tokens, input source, and the summary text. Output goes to `data/summaries.jsonl` — one row per paper/input-source with one slot per model.

Two paths inside the same script:

* **Real-time**: one HTTP call per (paper, provider), retried up to 3× with exponential backoff. Used by `PHASE3_MODE` in `test` / `single` / `dev`.
* **Batch**: builds OpenAI/Anthropic batch JSONL, submits, persists the job ID to `data/batch_jobs.jsonl`, returns. Used by `PHASE3_MODE=batch`. Gemini stays real-time (its batch API is intentionally not wired up — the saving isn't worth the second code path).

## When to run it

* After `prepare_texts.py` has filled `data/processed/`.
* After `verify_extraction.py` shows zero FAILs.
* In `single` mode whenever the prompt template under `llm-sum/prompts/summarization_v1.txt` changes — a 16-cent sanity check before bulk.

## Inputs

| Path                                | Role                                                           |
|-------------------------------------|----------------------------------------------------------------|
| `data/manifest.jsonl`               | Paper list — iterates one record per line.                     |
| `data/processed/*.jsonl`            | Cleaned text bodies.                                           |
| `data/raw_text/*.jsonl`             | Optional raw extracted text for `single`/`dev` comparison runs. |
| `data/summaries.jsonl` (if exists)  | Read for `--resume` so success slots aren't re-run.            |
| `llm-sum/prompts/summarization_v1.txt` | Prompt template — must contain `{ARTICLE_TEXT}`.            |
| `.env`                              | `PHASE3_MODE`, `PHASE3_DEV_LIMIT`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `TEMPERATURE`, `SEED`, `MAX_INPUT_CHARS`, `MAX_OUTPUT_TOKENS`. |

## Outputs

| Path                          | Schema                                                                                  |
|-------------------------------|-----------------------------------------------------------------------------------------|
| `data/summaries.jsonl`        | One row per paper. Each `models.<provider>` slot: `status`, `summary`, `input_tokens`, `output_tokens`, `model_version`, `timestamp`. |
| `data/batch_jobs.jsonl`       | One row per submitted batch job (only in `batch` mode).                                 |
| `data/error_log.jsonl`        | Any retry-exhausted failure.                                                            |

## CLI

```powershell
python llm-sum/summarizer.py                              # uses PHASE3_MODE from .env
python llm-sum/summarizer.py --mode single                # override
python llm-sum/summarizer.py --mode dev --limit 1         # 1 paper, real-time
python llm-sum/summarizer.py --mode batch --force         # bypass interactive 'yes'
python llm-sum/summarizer.py --resume                     # skip already-success slots
python llm-sum/summarizer.py --providers openai,anthropic # subset of providers
python llm-sum/summarizer.py --mode single --input-source raw_text
```

`--limit N` always wins. `--input-source processed` is the default and is the production path. `--input-source raw_text` is for `test`, `single`, and `dev` comparison runs only. `--force` writes an audit line to `data/logs/phase3_safety.log`.

## Behaviour per PHASE3_MODE

| Mode     | What happens                                                                    |
|----------|---------------------------------------------------------------------------------|
| `test`   | All API calls return deterministic `_mock_summary(...)` dicts. Output schema is identical to live runs so downstream code never branches. No spend. |
| `single` | 1 paper, real-time, 3 providers → 3 API calls. Prompts for `yes` before the first call. |
| `dev`    | `PHASE3_DEV_LIMIT` papers, real-time. Same confirm prompt. Budget-guarded.       |
| `batch`  | Full corpus. Builds and submits OpenAI + Anthropic batch JSONL; Gemini still goes through real-time in the same run. Confirm prompt required. |

Raw-vs-processed comparison workflow:

```powershell
python llm-sum/run_phase3.py summarize --mode single --estimate --input-source processed
python llm-sum/run_phase3.py summarize --mode single --estimate --input-source raw_text
python llm-sum/run_phase3.py summarize --mode single --input-source processed
python llm-sum/run_phase3.py summarize --mode single --input-source raw_text
```

The raw-text run usually costs more because it includes references and publisher boilerplate. The processed run is the scientifically intended input because it keeps the article body while removing text that should not influence the summary.

## Scientific controls (don't change between runs you want to compare)

* `TEMPERATURE=0.0` and `SEED=42` everywhere a provider supports them.
* Model version recorded from the response (not the alias requested) — drift detection.
* Token counts pulled from `usage.*` in the response — never estimated; this is what `BudgetGuard` charges against.
* `MAX_INPUT_CHARS` is applied at the moment of the LLM call, not at extraction time, so the limit can be tuned without re-extracting.

## Common errors and fixes

| Symptom                                                       | Cause                                                                | Fix                                                                                  |
|---------------------------------------------------------------|----------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| `No cached text for <slug>`                                   | `data/processed/<stem>.jsonl` not found under either naming.         | Run `python llm-sum/run_phase3.py extract`.                                          |
| `[phase3:safety] Confirmation not received; aborting.`        | You typed something other than `yes` at the prompt.                  | Re-run; type exactly `yes`. Pass `--force` for unattended/overnight runs.            |
| `BudgetGuard.total_spent > BUDGET_HARD_STOP, exiting.`        | You hit the cap in `.env`.                                           | Increase `BUDGET_HARD_STOP` or stop the run; partial progress is on disk.            |
| Provider returns HTTP 429 repeatedly                          | Rate limit; the retry loop sleeps but the QPS may still be too high. | Raise `RATE_LIMIT_<PROVIDER>` in `.env`.                                             |
| `model_version` looks like `gpt-5.5` (no date suffix)         | Some providers return only the alias for niche models.               | Not a bug — Anthropic and OpenAI usually return the dated string for production models. |

## Worked example

```powershell
# Single-paper smoke test before a bulk run:
$env:PHASE3_MODE = "single"
python llm-sum/summarizer.py
# → [phase3] mode=single | limit=1 | real-time | confirm-required
# → [phase3:safety] About to submit REAL real-time API calls (limit=1).
# →   Type 'yes' to confirm: yes
# → [phase3:summarize] paper 1: 10.1111/jvim.16872
# →   openai: success    (in=4521, out=487, ver=gpt-5.5-0325-preview, $0.0223)
# →   anthropic: success (in=4521, out=475, ver=claude-opus-4-6-20250901, $0.0214)
# →   gemini: success    (in=4612, out=482, ver=gemini-3.1-pro, $0.0165)
# → [phase3:summarize] done. counts={'success': 3, 'failed': 0, ...} budget_spent=$0.0602
```
