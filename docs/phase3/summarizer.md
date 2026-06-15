# summarizer.py

## What it does

For each paper in `data/manifest.jsonl`, sends the paper to **three LLMs** (OpenAI, Anthropic, Gemini) with identical prompts, identical temperature/seed, and identical max-output budget. By default it uses cleaned body text from `data/processed/`. For small comparison runs, `--input-source raw_text` sends the raw extracted JSONL from `data/raw_text/`, and `--input-source pdf` sends the original PDF from `data/raw/` directly to each provider's PDF API.

Real-time summarisation now uses a single Pydantic schema, `VeterinarySummary`, across all three providers. OpenAI uses native parsed completions, Gemini uses native JSON schema output, and Anthropic uses a forced tool call. Each model slot stores both:

* `summary` — readable prose from `VeterinarySummary.summary_text`, used by the blind evaluator.
* `structured_summary` — the full schema as a clean dictionary for later analysis.

## Method Map

The summarizer is built from a few simple methods that work together:

1. **Load the prompt**  
   `load_prompt()` reads `llm-sum/prompts/summarization_v1.txt` and checks that it still contains `{ARTICLE_TEXT}`. That placeholder is required because it marks where the target paper text will be inserted.

2. **Optionally add your guide summary**  
   `load_optional_guide_summary()` reads `llm-sum/prompts/guide_summary_template.txt` if it has text. `apply_guide_summary_to_prompt()` wraps that guide with warnings saying "format only, do not copy facts."

3. **Build the provider message**  
   `build_user_message()` inserts processed JSONL text into the prompt. `build_pdf_user_message()` keeps the same instructions but tells the provider to read the attached PDF.

4. **Call each provider**  
   `generate_summary()` routes text inputs to OpenAI, Anthropic, or Gemini. `generate_summary_from_pdf()` routes direct-PDF inputs to the provider-specific PDF code.

5. **Normalize the output**  
   `coerce_veterinary_summary()` repairs partial structured responses by filling missing fields with safe placeholders such as `"Not reported"` or empty lists. It never invents findings.

6. **Write results**  
   `run_realtime()` writes one row per paper/input source to `data/summaries.jsonl`. Each row has one slot for OpenAI, one for Anthropic, and one for Gemini.

Provider client pattern:

```text
OpenAI    -> openai.OpenAI()
Anthropic -> anthropic.Anthropic()
Gemini    -> google.genai.Client()
```

Model names come from `.env` through `OPENAI_MODEL`, `ANTHROPIC_MODEL`, and `GEMINI_MODEL`.

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
| `data/raw/*.pdf`                    | Optional direct-PDF input for `test`/`single` comparison runs only. |
| `data/summaries.jsonl` (if exists)  | Read for `--resume` so success slots aren't re-run.            |
| `llm-sum/prompts/summarization_v1.txt` | Prompt template — must contain `{ARTICLE_TEXT}`.            |
| `llm-sum/prompts/guide_summary_template.txt` | Optional human-written guide summary used for format only. Leave blank to disable. |
| `.env`                              | `PHASE3_MODE`, `PHASE3_DEV_LIMIT`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `TEMPERATURE`, `SEED`, `MAX_INPUT_CHARS`, `MAX_OUTPUT_TOKENS`, optional `GUIDE_SUMMARY_FILE`. |

## Outputs

| Path                          | Schema                                                                                  |
|-------------------------------|-----------------------------------------------------------------------------------------|
| `data/summaries.jsonl`        | One row per paper. Each `models.<provider>` slot: `status`, `summary`, `structured_summary`, `input_tokens`, `output_tokens`, `model_version`, `timestamp`. |
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
python llm-sum/summarizer.py --mode single --input-source pdf
python llm-sum/summarizer.py --mode single --guide-summary llm-sum/prompts/guide_summary_template.txt
```

`--limit N` always wins. `--input-source processed` is the default and is the production path. `--input-source raw_text` is for `test`, `single`, and `dev` comparison runs. `--input-source pdf` is stricter: it is only allowed in `test` and `single`, because it makes real-time provider-specific PDF calls and is meant for one-paper PDF-vs-JSONL comparisons. `--force` writes an audit line to `data/logs/phase3_safety.log`.

## Optional Format Guide

If you write one summary yourself, paste it into:

```text
llm-sum/prompts/guide_summary_template.txt
```

When that file is blank or missing, nothing changes. When it contains text, the summarizer inserts it into the prompt as a **format guide only**. The prompt explicitly tells the LLM to copy only the section names, order, tone, and level of detail. It also tells the model not to copy species, diseases, treatments, numbers, outcomes, conclusions, or any other factual claims from the guide.

Use this when you want all LLM summaries to follow your preferred structure while still forcing every fact to come from the target paper or PDF.

## Behaviour per PHASE3_MODE

| Mode     | What happens                                                                    |
|----------|---------------------------------------------------------------------------------|
| `test`   | All API calls return deterministic `_mock_summary(...)` dicts. Output schema is identical to live runs so downstream code never branches. No spend. |
| `single` | 1 paper, real-time, 3 providers → 3 API calls. Prompts for `yes` before the first call. |
| `dev`    | `PHASE3_DEV_LIMIT` papers, real-time. Same confirm prompt. Budget-guarded.       |
| `batch`  | Full corpus. Builds and submits OpenAI + Anthropic batch JSONL; Gemini still goes through real-time in the same run. Confirm prompt required. |

PDF-vs-JSONL comparison workflow for one paper:

```powershell
python llm-sum/run_phase3.py summarize --mode single --estimate --input-source processed
python llm-sum/run_phase3.py summarize --mode single --input-source processed
python llm-sum/run_phase3.py summarize --mode single --input-source pdf
```

That produces 6 summaries for the same paper: 3 from the processed JSONL text and 3 from the original PDF. The direct-PDF run cannot be cost-estimated offline because token accounting happens inside each provider's PDF ingestion system; the live `single` run records real input/output token counts and cost after each provider returns.

## Scientific controls (don't change between runs you want to compare)

* `TEMPERATURE=0.0` and `SEED=42` everywhere a provider supports them.
* Real-time outputs are validated against one Pydantic `VeterinarySummary` schema before being written.
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
| Provider fails with a schema/validation error                  | The model response did not satisfy `VeterinarySummary`, or the provider SDK does not support the requested structured-output feature. | Check SDK versions from `requirements.txt`, then retry in `single` mode.             |
| `model_version` looks like `gpt-5.4` (no date suffix)         | Some providers return only the alias for niche models.               | Not a bug — Anthropic and OpenAI usually return the dated string for production models. |

## Worked example

```powershell
# Single-paper smoke test before a bulk run:
$env:PHASE3_MODE = "single"
python llm-sum/summarizer.py
# → [phase3] mode=single | limit=1 | real-time | confirm-required
# → [phase3:safety] About to submit REAL real-time API calls (limit=1).
# →   Type 'yes' to confirm: yes
# → [phase3:summarize] paper 1: 10.1111/jvim.16872
# →   openai: success    (in=4521, out=487, ver=gpt-5.4-0325-preview, $0.0223)
# →   anthropic: success (in=4521, out=475, ver=claude-sonnet-4-6-20250901, $0.0214)
# →   gemini: success    (in=4612, out=482, ver=gemini-3.5-flash, $0.0165)
# → [phase3:summarize] done. counts={'success': 3, 'failed': 0, ...} budget_spent=$0.0602
```
