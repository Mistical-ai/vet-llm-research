# Guide: How This Research Pipeline Works

This guide explains the project in plain language so you can understand the structure, methods, and engineering decisions behind the pipeline. The short version is:

> The project builds a reproducible pipeline that collects veterinary research PDFs, cleans and validates their text, sends each paper to multiple LLMs under controlled conditions, then evaluates the summaries in a blinded way.

The goal is not just to "get summaries." The goal is to compare summarization quality, cost, and reliability across LLM providers in a way that can be defended in a research setting.

---

## 1. The Big Picture

The pipeline has five major stages:

```text
1. Collect paper metadata
   CrossRef / journal metadata -> data/manifest.jsonl

2. Acquire PDFs
   Open-access download + manual library supplementation -> data/raw/*.pdf

3. Extract and clean text
   data/raw/*.pdf -> data/raw_text/*.jsonl and data/processed/*.jsonl

4. Summarize with LLMs
   processed text or direct PDF -> OpenAI, Anthropic, Gemini -> data/summaries.jsonl

5. Evaluate summaries
   summaries -> blind LLM judge -> data/evaluations.jsonl
```

Each stage writes files to disk before the next stage starts. This is intentional: if one step fails, the previous work is still saved.

---

## 2. Why The Project Is Structured This Way

The structure is based on three research-engineering principles:

### 2.1 Data Provenance

Data provenance means being able to answer: "Where did this output come from?"

The project keeps every stage separate:

```text
data/raw/          original PDFs
data/raw_text/     text extracted directly from PDFs
data/processed/    cleaned text sent to LLMs by default
data/summaries*    model summaries
data/evaluations*  blind judge scores
data/error_log*    errors with stage names
```

This matters because if a summary looks wrong, you can trace it backwards:

```text
summary -> processed JSONL -> raw extracted text -> original PDF -> manifest DOI
```

### 2.2 Budget Safety

LLM calls cost money, so the pipeline has safety switches:

```text
PHASE3_MODE=test     mock summaries, no API calls
PHASE3_MODE=single   one paper, real API calls
PHASE3_MODE=dev      a small number of papers
PHASE3_MODE=batch    full run, batch APIs where supported
```

Every paid mode asks for confirmation before calling APIs. This prevents accidental spending.

### 2.3 Reproducibility

The pipeline tries to keep model comparisons fair:

- Same paper input
- Same prompt
- Same output schema
- Same temperature where supported
- Same output-token limit
- Exact model version recorded
- Exact token counts recorded from provider responses

This lets you later say: "These summaries were generated under the same controlled conditions."

---

## 3. Folder Structure Explained

### Root Files

```text
README.md
```

The GitHub landing page. It explains the whole project at a high level and links to deeper documentation.

```text
.env.template
```

A safe template showing every environment variable. It has placeholders for API keys and explains what each setting does.

```text
requirements.txt
```

Python package dependencies.

```text
pipeline.py
```

A status/scoreboard script for the corpus acquisition phase.

### `src/`

This folder handles corpus building and PDF acquisition.

Important scripts:

```text
src/collect.py
```

Builds the candidate paper list from CrossRef.

```text
src/download.py
```

Attempts legal open-access PDF downloads.

```text
src/supplement.py
```

Shows which papers are still missing and suggests manual supplementation.

```text
src/auto_ingest_workflow.py
```

Takes manually downloaded PDFs and tries to match them to known papers.

```text
src/extract.py
```

Contains PDF text extraction and cleaning helpers used by Phase 3.

### `llm-sum/`

This folder handles Phase 3: extraction, summarization, evaluation, and costs.

Important scripts:

```text
llm-sum/run_phase3.py
```

The main orchestrator. It gives you one command with subcommands:

```powershell
python llm-sum/run_phase3.py extract
python llm-sum/run_phase3.py summarize
python llm-sum/run_phase3.py evaluate
python llm-sum/run_phase3.py status
```

```text
llm-sum/prepare_texts.py
```

Extracts text from PDFs and creates both raw and cleaned JSONL text caches.

```text
llm-sum/summarizer.py
```

Calls OpenAI, Anthropic, and Gemini and writes summaries.

```text
llm-sum/evaluator.py
```

Runs a blind judge over summaries.

```text
llm-sum/models_config.py
```

Single source of truth for model names and pricing.

```text
llm-sum/cost_estimator.py
```

Estimates cost before real API calls.

```text
llm-sum/check_batch_status.py
```

Collects completed batch API results.

### `tests/`

Unit tests for the pipeline. These protect against common failures like:

- Prompt missing `{ARTICLE_TEXT}`
- Wrong provider request shape
- Broken summary schema
- Broken filename matching
- Broken extraction assumptions
- Broken cost calculations

### `docs/`

Human documentation. The most important Phase 3 guide is:

```text
docs/phase3/README.md
```

This guide explains how to run summarization and evaluation step by step.

---

## 4. How Data Moves Through The System

### Step 1: Build The Paper List

Command:

```powershell
python src/collect.py
```

Output:

```text
data/manifest.jsonl
```

What it contains:

- DOI
- Title
- Journal
- Year
- Metadata
- Research covariates when available

Why JSONL?

JSONL means "JSON Lines": one JSON object per line. It is used because it is safer for long-running pipelines. If a run crashes, only the current line is at risk, not the entire file.

### Step 2: Download PDFs

Command:

```powershell
python src/download.py
```

Output:

```text
data/raw/*.pdf
```

The downloader only uses legal/open-access sources. It does not bypass paywalls.

### Step 3: Extract Text

Command:

```powershell
python llm-sum/run_phase3.py extract
```

Outputs:

```text
data/raw_text/*.jsonl
data/processed/*.jsonl
```

The difference:

```text
raw_text    = direct text extraction from PDF
processed   = cleaned article body, references and publisher noise removed
```

The default LLM input is `processed` because it should be cheaper and cleaner.

### Step 4: Summarize

Command:

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source processed
```

Output:

```text
data/summaries.jsonl
```

Each row stores one paper/input-source combination. Inside each row:

```text
models.openai
models.anthropic
models.gemini
```

Each model has:

```text
status
summary
structured_summary
input_tokens
output_tokens
model_version
timestamp
```

### Step 5: Evaluate

Command:

```powershell
python llm-sum/run_phase3.py evaluate
```

Output:

```text
data/evaluations.jsonl
```

The evaluator is "blind": it scores summaries without being told which model wrote them.

This reduces bias because the judge cannot favor a particular model name.

---

## 5. The Three Summary Input Types

The summarizer can use three input sources:

### `processed`

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source processed
```

Uses:

```text
data/processed/*.jsonl
```

This is the default and scientifically preferred input.

### `raw_text`

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source raw_text
```

Uses:

```text
data/raw_text/*.jsonl
```

This tests what happens if the LLM sees text before reference removal and cleaning.

### `pdf`

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source pdf
```

Uses:

```text
data/raw/*.pdf
```

This sends the PDF directly to providers that support PDF input.

Direct PDF input is only allowed in `test` and `single`, because it is provider-specific, real-time, and harder to cost-estimate.

---

## 6. The Six-Summary Comparison

To compare JSONL versus PDF for the same article:

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source processed
python llm-sum/run_phase3.py summarize --mode single --input-source pdf
```

This gives:

```text
processed JSONL -> OpenAI summary
processed JSONL -> Anthropic summary
processed JSONL -> Gemini summary

direct PDF -> OpenAI summary
direct PDF -> Anthropic summary
direct PDF -> Gemini summary
```

Total:

```text
6 summaries for 1 article
```

Why this is useful:

- You can compare cost.
- You can compare summary quality.
- You can see whether provider PDF reading is better or worse than your own extraction.

---

## 7. Prompt And Guide Summary Design

The main prompt is:

```text
llm-sum/prompts/summarization_v1.txt
```

It must contain:

```text
{ARTICLE_TEXT}
```

That placeholder is where the target article text is inserted.

The optional human-written guide is:

```text
llm-sum/prompts/guide_summary_template.txt
```

Purpose:

- Teach format
- Teach section order
- Teach tone
- Teach level of detail

Not allowed:

- Copying guide facts
- Copying guide numbers
- Copying guide species
- Copying guide outcomes
- Copying guide conclusions

The code wraps the guide in warning text telling the LLM that the guide is for structure only.

---

## 8. Provider Client Method

The provider setup follows this pattern:

```text
Load .env once
Read model names from environment variables
Create provider clients only when needed
Send the same prompt/input to each provider
Parse output into the same schema
Record exact token counts and model versions
```

### OpenAI

Uses:

```python
openai.OpenAI()
```

Important details:

- Model name comes from `OPENAI_MODEL`.
- Uses structured parsing into `VeterinarySummary`.
- Uses `max_completion_tokens` for newer models.
- If a model rejects `temperature`, the code retries without temperature.

### Anthropic

Uses:

```python
anthropic.Anthropic()
```

Important details:

- Model name comes from `ANTHROPIC_MODEL`.
- Uses a forced tool call named `VeterinarySummary`.
- If the provider returns a partial object, missing fields are safely filled as `Not reported` or empty lists instead of inventing facts.

### Gemini

Uses the current SDK:

```python
from google import genai
client = genai.Client()
```

Important details:

- Model name comes from `GEMINI_MODEL`.
- Uses `google-genai`, not deprecated `google-generativeai`.
- Uses a cleaned JSON schema because Gemini rejects some Pydantic metadata.

---

## 9. Structured Summary Schema

All providers must produce the same logical fields:

```text
headline
objective
study_design
species
sample_size
key_methods
key_findings
clinical_significance
limitations
summary_text
```

Why use a schema?

Without a schema, each model might answer in a different shape. One might give paragraphs, another might give bullets, and another might omit sample size. The schema makes downstream comparison easier.

The readable summary used by the evaluator is:

```text
summary
```

The structured dictionary used for analysis is:

```text
structured_summary
```

---

## 10. Budget And Cost Methods

The cost system has two parts:

### Offline Estimate

Command:

```powershell
python llm-sum/run_phase3.py summarize --estimate
```

This estimates cost before calling APIs.

It works well for text inputs because token counts can be estimated offline.

Direct PDF cost cannot be estimated reliably because providers process PDFs differently.

### Runtime Cost Tracking

When real API calls run, token counts come from provider responses:

```text
input_tokens
output_tokens
```

The pipeline then computes cost using prices in:

```text
llm-sum/models_config.py
```

This is more accurate than guessing after the fact.

---

## 11. Safety Methods

The pipeline uses multiple safety layers:

### Test Mode

```text
PHASE3_MODE=test
```

No real API calls. Mock summaries only.

### Single Mode

```text
PHASE3_MODE=single
```

One paper only. Good for real paid smoke tests.

### Confirmation Prompt

Paid modes ask:

```text
Type 'yes' to confirm:
```

This prevents accidental spending.

### Prompt Validation

Before asking for confirmation, the code validates that the prompt still contains:

```text
{ARTICLE_TEXT}
```

This prevents paying for a run with a broken prompt.

### Budget Guard

`BudgetGuard` stops runs if spending exceeds the configured cap.

---

## 12. Batch Versus Real-Time

### Real-Time

One request is sent immediately and the result comes back immediately.

Used for:

```text
test
single
dev
direct PDF comparisons
```

### Batch

Many requests are uploaded to a provider and processed later.

Used for:

```text
full corpus text summarization
```

Benefits:

- Cheaper where provider batch APIs support discounts
- Better for large runs

Tradeoff:

- Slower
- More complex result collection
- Not suitable for direct PDF comparison in this project

---

## 13. Testing Method

The test suite checks the system without making real API calls.

Examples:

```powershell
python -m pytest tests/ -q --tb=no
```

Tests cover:

- DOI filename conversion
- PDF path matching
- Extraction outputs
- Prompt validation
- Provider request shapes
- Schema repair
- Cost math
- Mode safety
- Evaluator parsing

This means most mistakes are caught before spending money.

---

## 14. How To Explain This In A Meeting

You can say:

> I built a staged, reproducible pipeline for comparing LLM summarization of veterinary papers. First, we collect and validate open-access PDFs. Then we extract both raw and cleaned text so we can compare input quality. The summarization stage sends the same article to OpenAI, Anthropic, and Gemini using the same prompt and structured schema. The system records exact model versions, token counts, and costs. It also supports a six-summary comparison where one article is summarized from processed JSONL and directly from PDF. Finally, summaries are evaluated by a blind judge so the scoring is less biased.

If someone asks why the system is reliable, say:

> The pipeline is broken into separate stages with saved outputs, uses JSONL for crash safety, validates prompts before paid runs, has test and single-paper modes for safety, logs errors by stage, and has a unit test suite that checks provider request formats and parsing.

If someone asks why this is scientifically useful, say:

> It lets us compare not only which LLM writes better summaries, but also whether cleaned extracted text or direct PDF input gives better quality per dollar.

---

## 15. Commands To Know

Free setup/check:

```powershell
python llm-sum/run_phase3.py extract
python scripts/verify_extraction.py
python llm-sum/run_phase3.py summarize --mode test
python llm-sum/run_phase3.py status
```

One real processed-text run:

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source processed
```

One real PDF run:

```powershell
python llm-sum/run_phase3.py summarize --mode single --input-source pdf
```

Run tests:

```powershell
python -m pytest tests/ -q --tb=no
```

Find summaries:

```text
data/summaries.jsonl
```

Find evaluation scores:

```text
data/evaluations.jsonl
```

