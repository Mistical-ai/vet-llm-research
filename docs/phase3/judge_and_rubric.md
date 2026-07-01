# How the Judge Works (Plain English)

This page explains **how summaries get scored** in Phase 3. No code knowledge required.

If you want CLI details and file paths, see [evaluator.md](evaluator.md).

---

## The idea in one sentence

After three different LLMs write summaries of the same paper, a **separate judge LLM** reads the original article text and each summary, scores how good the summary is, and checks for **made-up facts** — **without knowing which LLM wrote the summary**.

---

## What happens, step by step

```
  Original paper text          Summary from Model A, B, or C
  (cleaned, from PDF)          (the judge does NOT know which model)
         │                              │
         └──────────┬───────────────────┘
                    ▼
            ┌───────────────┐
            │  Judge LLM    │  ← reads both, scores quality,
            │  (blind)      │    counts hallucinations
            └───────┬───────┘
                    ▼
         data/evaluations.jsonl
         (one score row per paper + summary + judge)
```

1. **Summarise first** — OpenAI, Anthropic, and Gemini each write a summary of the same paper (`data/summaries.jsonl`).
2. **Evaluate second** — For each summary, the judge compares it to the **original cleaned article text** (not the abstract alone).
3. **Save scores** — Each result is appended to `data/evaluations.jsonl`. You can re-run safely; already-scored rows are skipped unless you use `--no-resume`.

---

## Why “blind” matters

If the judge knew *“this summary was written by GPT”*, it might score that model higher (self-preference bias). So:

- The judge prompt contains **only** the article text and the summary text.
- The judge **never** sees words like “OpenAI”, “Claude”, “Gemini”, or “GPT”.
- Which model wrote the summary is stored in the data file **after** scoring, for your analysis — not shown to the judge.

This is required for a fair model comparison.

---

## The rubric: Vet-Score v2.0

The judge uses a **veterinary-specific checklist** defined in `llm-sum/prompts/judge_v2.txt`. It is **not** a vague “rate 1–10 from the gut.”

The judge scores **four dimensions**, each from **1 (poor)** to **3 (excellent)**:

### 1. Factual accuracy + species (most important)

| Score | Meaning |
|-------|---------|
| **3** | Everything is correct; the right species is named (e.g. “dogs”, “cats”) — not vague “animals” |
| **2** | Small mistakes, or species is slightly vague |
| **1** | Wrong facts, or keeps saying “animals” when the paper was only about one species |

*Why it matters:* A summary that gets the species wrong could mislead a clinician.

### 2. Completeness

Did the summary cover these **six parts** of a vet paper?

1. Objective / research question  
2. Study design and methods  
3. Species and sample size  
4. Key results  
5. Clinical significance (“so what for practice?”)  
6. Limitations  

| Score | Meaning |
|-------|---------|
| **3** | All six present |
| **2** | Four or five present |
| **1** | Three or fewer present |

### 3. Clinical relevance

| Score | Meaning |
|-------|---------|
| **3** | Species named early; clear take-home message for clinicians |
| **2** | Species mentioned somewhere; advice is vague |
| **1** | Species missing or buried; no useful clinical guidance |

### 4. Organization

| Score | Meaning |
|-------|---------|
| **3** | Easy to read, logical order, not repetitive |
| **2** | Mostly fine, minor flow issues |
| **1** | Messy or very repetitive |

---

## Hallucinations (made-up facts)

A **hallucination** here means: something in the summary that is **not supported** by the article text (not explicitly stated and not a fair direct inference).

For each hallucination found, the judge records:

| Field | What it is |
|-------|------------|
| **claim** | The exact wrong sentence from the summary |
| **source_quote** | Text from the article that contradicts or does not support it |
| **category** | Type of error (see below) |
| **severity** | `minor` or `major` |

**Categories:**

| Category | Simple meaning |
|----------|----------------|
| `fabricated_statistics` | Numbers or p-values that don’t appear in the paper |
| `omitted_caveat` | Important warning left out (e.g. “small sample”) |
| `contradiction` | Summary says the opposite of the paper |
| `unsupported_inference` | Logical leap not justified by the text |

**Severity:**

- **minor** — Annoying but probably wouldn’t change clinical decisions  
- **major** — Could mislead a clinician; flagged for human review  

---

## How the final score (1–10) is calculated

The judge returns four scores (1–3). **Python computes the final number**, not the LLM — so the math is always the same.

**Weighted formula:**

```
raw = (Factual × 1.5) + (Completeness × 1.0) + (Clinical × 1.2) + (Organization × 0.8)
```

Factual accuracy counts **most** (×1.5). Clinical relevance counts more than completeness or organization.

That raw score is then **scaled to 1–10**:

```
quality_score = round( (raw / 13.5) × 9 + 1 )
```

**Example:** all fours dimensions scored **3** (best):

- raw = (3×1.5) + (3×1.0) + (3×1.2) + (3×0.8) = **13.5**
- quality_score = **10**

**Example:** mixed scores (3, 2, 3, 2):

- raw = 4.5 + 2.0 + 3.6 + 1.6 = **11.7**
- quality_score ≈ **9**

You will see both `composite_score` (decimal) and `quality_score` (rounded integer) in `data/evaluations.jsonl`.

---

## What else the judge returns

| Field | Range | Meaning |
|-------|-------|---------|
| `confidence_score` | 1–5 | How sure the judge is (1 = guessing, 5 = certain) |
| `reasoning` | text | One-sentence explanation |
| `hallucination_count` | ≥ 0 | Number of unsupported claims |
| `requires_human_review` | true/false | See below |

---

## When a row is flagged for human review

`requires_human_review = true` if **any** of these happen:

1. Judge confidence is low (`confidence_score` < 3)  
2. The judge response was broken and could not be parsed (score set to **99**)  
3. Any hallucination marked **major** severity  

These rows are meant for a later Phase 5 manual check — not ignored, but marked as “needs a human eye.”

---

## Which model is the judge?

Controlled by `.env`:

```env
JUDGE_MODELS=openai
```

Default: **OpenAI** judges all summaries. You can add more judges (e.g. `openai,anthropic`) for a second opinion; each combination gets its own row in `data/evaluations.jsonl`.

The judge is **separate** from the three summarisers. Summarisers write; the judge only reads and scores.

---

## Journal-stratified sampling (single / dev runs)

In `single` or `dev` mode, evaluation does **not** score every paper in the corpus. It randomly picks **one paper per journal** (five journals → five papers), using a fixed seed (`EVAL_SAMPLE_SEED=42`) so the same sample is chosen every time unless you change the seed.

That keeps paid test runs cheap while still covering all five target journals.

Full **batch** runs evaluate everything.

---

## How to run evaluation

**Free mock (no API cost):**

```powershell
python llm-sum/run_phase3.py evaluate --mode test
```

**Small real run (after summarise):**

```powershell
python llm-sum/run_phase3.py evaluate --mode single
```

**See results:**

```powershell
python llm-sum/run_phase3.py status
```

---

## Quick reference: old vs new rubric

| | **judge_v1** (old) | **judge_v2** (current) |
|--|-------------------|------------------------|
| Prompt file | `judge_v1.txt` | `judge_v2.txt` |
| Scoring | Single 1–10 from judge | Four 1–3 dimensions + computed 1–10 |
| Hallucinations | Count + category list | Count + quoted claims with severity |
| Domain focus | Generic | Veterinary (species, clinical checklist) |
| Env setting | `JUDGE_PROMPT_FILE=.../judge_v1.txt` | `JUDGE_PROMPT_FILE=.../judge_v2.txt` (default) |

Do **not** compare v1 and v2 scores directly in analysis — they use different rubrics.

---

## One paragraph for a meeting or report

> We evaluate each LLM summary with a blind judge that sees only the original article and the summary text, not the model name. The judge uses a veterinary rubric (factual accuracy with species checks, completeness of six standard sections, clinical relevance, and organization), detects hallucinations with quoted evidence, and outputs structured scores. Python combines the four dimension scores into a 1–10 quality score with factual accuracy weighted highest. Low-confidence, malformed, or clinically dangerous hallucinations are flagged for human review in a later phase.
