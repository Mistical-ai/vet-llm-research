# summarizer.py

## What it does

For each paper in `data/manifest.jsonl`, sends the paper to **three LLMs** (OpenAI, Anthropic, Gemini) with identical prompts, identical temperature/seed, and identical max-output budget. By default it uses cleaned body text from `data/processed/` (folder name configurable via `PROCESSED_DIR_NAME`; ships as `processedv2` by default). For small comparison runs, `--input-source raw_text` sends the raw extracted JSONL from `data/raw_text/`, and `--input-source pdf` sends the original PDF from `data/raw/` directly to each provider's PDF API.

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
* **Batch**: builds per-provider batch JSONL, submits, persists the job ID to `data/batch_jobs.jsonl`, returns. Used by `PHASE3_MODE=batch`, split per provider by its `*_BATCH_ENABLED` flag: `OPENAI_BATCH_ENABLED`/`ANTHROPIC_BATCH_ENABLED` default `true`; `GEMINI_BATCH_ENABLED` defaults `false`, so Gemini stays real-time by default in the same run (flip the flag once you've smoke-tested Gemini's batch path to submit it as a batch job too — see `.env.template` section 12).

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
| `data/summaries.jsonl`        | One row per paper/input source. Each `models.<provider>` slot: `status`, `summary`, `structured_summary`, `input_tokens`, `output_tokens`, `model_version`, optional `system_fingerprint`, `timestamp`. |
| `data/summaries_pdf/*.txt`    | Readable `summarize-all` reports for raw PDFs (`single`/`test`); one file per matched article source. `dev` mode writes to `data/dev_tests/summaries_pdf/*.txt` instead. |
| `data/summaries_txt/*.txt`    | Readable `summarize-all` reports for processed JSONL text (`single`/`test`); one file per matched article source. `dev` mode writes to `data/dev_tests/summaries_txt/*.txt` instead. |
| `data/dev_summaries_jsonl/*.txt` | Readable **structured-bullet** view of `summaries.jsonl`, one file per paper, written after a `summarize --mode dev` run. List fields are capped for readability (`key_methods` 4, `key_findings` 5, `limitations` 4); `summaries.jsonl` keeps the full lists. `--output-subdir NAME` redirects this to a `NAME/` subfolder with its own skip-existing pool. |
| `data/dev_summaries_jsonl/prose/*.txt` | Readable **flowing-prose** view of the same papers: the `summary_text` the blind judge scores and human reviewers grade, with a `Summary (prose): N words` line and an `[OVER 400]` flag. The 300-340-word budget applies only here. Kept in a subfolder so the non-recursive `*.txt` readers never count a paper twice. |
| `data/single_summaries_jsonl/` | Same two surfaces for `summarize --mode single`, for whichever paper that run actually touched. A fully-resumed run writes nothing here and says so. |
| `data/batch_summaries_jsonl/` | Same two surfaces for `batch` mode, written by `check_batch_status.py` as each batch provider's results merge back — so the finished corpus is ready for judging and human validation. Rewritten per provider merge; with `GEMINI_BATCH_ENABLED=false` (default) Gemini's real-time result rides along on the first OpenAI/Anthropic merge for the same paper instead of arriving as its own job. |
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
python llm-sum/run_phase3.py summarize-all --mode single  # same article, 6 summaries
python llm-sum/run_phase3.py summarize-all --mode dev     # same one-pair default
python llm-sum/summarizer.py --mode single --guide-summary llm-sum/prompts/guide_summary_template.txt
```

`--limit N` always wins. `--input-source processed` is the default and is the production path. `--input-source raw_text` is for `test`, `single`, and `dev` comparison runs. `--input-source pdf` is stricter: it is only allowed in `test` and `single`, because it makes real-time provider-specific PDF calls and is meant for one-paper PDF-vs-JSONL comparisons. `--force` writes an audit line to `data/logs/phase3_safety.log`. `--doi-filter` (comma-separated DOIs) restricts the run to exactly those papers, bypassing `--limit`'s sequential slicing; it's set internally by `run_phase3.py`'s dev-mode journal-stratified selection and not usually passed by hand.

## Optional Format Guide

`llm-sum/prompts/guide_summary_template.txt` ships pre-populated with 4 example summaries (short title/author line, then Background/Methods/Results/Limitations/Conclusions prose paragraphs) showing the target length and tone. To use your own instead, replace its contents:

```text
llm-sum/prompts/guide_summary_template.txt
```

When that file is blank or missing, nothing changes. When it contains text, the summarizer inserts it into the prompt as a **format guide only**. The prompt explicitly tells the LLM to copy only the section names, order, tone, and level of detail. It also tells the model not to copy species, diseases, treatments, numbers, outcomes, conclusions, or any other factual claims from the guide.

Use this when you want all LLM summaries to follow your preferred structure while still forcing every fact to come from the target paper or PDF.

## Behaviour per PHASE3_MODE

| Mode     | What happens                                                                    |
|----------|---------------------------------------------------------------------------------|
| `test`   | All API calls return deterministic `_mock_summary(...)` dicts. Output schema is identical to live runs so downstream code never branches. No spend. |
| `single` | 1 paper, real-time, 3 providers → 3 API calls. For `summarize-all`, 1 matched PDF/JSONL pair → 6 summaries. Prompts for `yes` before the first call. Also writes readable output to `data/single_summaries_jsonl/` (+ `prose/`) for the paper it touched. |
| `dev`    | `PHASE3_DEV_LIMIT` papers for normal `summarize`; for `summarize-all`, the default is also 1 matched PDF/JSONL pair → 6 summaries unless `--limit N` is passed, written to `data/dev_tests/summaries_pdf/` and `data/dev_tests/summaries_txt/` instead of the top-level folders. Same confirm prompt. Budget-guarded. |
| `batch`  | Full corpus. Builds and submits batch JSONL for whichever providers have `*_BATCH_ENABLED=true` (OpenAI + Anthropic by default); the rest — Gemini by default — go through real-time in the same run. Confirm prompt required. Readable output for batch-submitted providers appears later, when `check_batch_status.py` merges results back into `data/batch_summaries_jsonl/` (+ `prose/`). |

PDF-vs-JSONL comparison workflow for one matched DOI/title:

```powershell
python llm-sum/run_phase3.py summarize-all --mode single
# or
python llm-sum/run_phase3.py summarize-all --mode dev
```

That produces 6 summaries for the same paper: 3 from the processed JSONL text and 3 from the original PDF. The script matches by the shared article stem in `data/raw` and `data/processed`, which includes the title-derived filename and DOI slug. The direct-PDF calls cannot be cost-estimated offline because token accounting happens inside each provider's PDF ingestion system; the live run records real input/output token counts and cost after each provider returns.

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
| `Cannot start Phase 3 summarization with BUDGET_HARD_STOP=$0.00` or hard-stop output | The budget cap in `.env` is still zero or has been exceeded. | Set a positive `BUDGET_HARD_STOP` before paid modes, or stay in `PHASE3_MODE=test`; partial progress is on disk. |
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
