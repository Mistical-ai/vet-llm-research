# Phase 3 — a beginner's how-to for the `llm-sum/` scripts

This is a hands-on walkthrough for turning your collected PDFs into LLM summaries and quality scores. It assumes **no prior knowledge** of the pipeline. Read it top to bottom the first time; after that, the [mode cheat-sheet](#the-one-control-you-need-to-understand-phase3_mode) and [recipes](#copy-paste-recipes) are all you'll need.

> **Golden rule:** the pipeline always starts in **test mode** (free, no API calls). You have to *deliberately* switch to a paid mode. So you can't accidentally spend money by following these steps — experiment freely.

---

## 1. What you're about to build (the big picture)

Phase 2 left you with ~250 cleaned veterinary PDFs in `data/raw/`. Phase 3 does two things with them:

1. **Summarise** each paper with three different LLMs — OpenAI, Anthropic, Gemini — using the *exact same prompt* so the comparison is fair.
2. **Judge** every summary with a "blind" LLM judge that scores quality and counts hallucinations, *without knowing which model wrote the summary* (so it can't play favourites).

The end product is two data files you'll analyse in Phase 4–5:

```
data/summaries.jsonl      one row per paper, holding all three models' summaries
data/evaluations.jsonl    one row per (paper, summary, judge) — the scores
```

Everything you run lives in two folders:

```
llm-sum/     the Phase 3 engine (summariser, judge, orchestrator, ...)
scripts/     two helper tools you run by hand (verify, migrate)
```

---

## 2. The pipeline is four steps, always in this order

```
   STEP 1            STEP 2              STEP 3            STEP 4
  ┌────────┐       ┌──────────┐       ┌──────────┐      ┌────────┐
  │extract │  ──▶  │summarize │  ──▶  │ evaluate │ ──▶  │ status │
  └────────┘       └──────────┘       └──────────┘      └────────┘
 PDFs become      each paper →        each summary →    read-only
 clean text       3 summaries         a judge score     scoreboard

 data/raw/*.pdf   data/processed/  →  data/summaries  → data/evaluations
                  *.jsonl             .jsonl            .jsonl
```

You can run each step two ways — **pick one style and stick with it**:

* **The easy way (recommended for beginners):** one orchestrator command, `run_phase3.py`, with a sub-command per step:
  ```powershell
  python llm-sum/run_phase3.py extract
  python llm-sum/run_phase3.py summarize
  python llm-sum/run_phase3.py evaluate
  python llm-sum/run_phase3.py status
  ```
* **The direct way (for fine control):** call each script yourself:
  ```powershell
  python llm-sum/prepare_texts.py
  python llm-sum/summarizer.py
  python llm-sum/evaluator.py
  ```

Both do *exactly* the same work. The orchestrator just saves you from remembering which file each step lives in, and it prints a status banner so you always know what mode you're in.

**Why split it into separate steps instead of one big "do everything" button?** Because each step is slow and costs money, and you want to *check the result of one step before paying for the next*. You extract text once and inspect it; only then do you pay to summarise; only then do you pay to judge. If something's wrong, you find out at the cheap stage. This is the single most important design idea in Phase 3.

---

## 3. The one control you need to understand: `PHASE3_MODE`

Everything in Phase 3 is governed by a single switch in your `.env` file called `PHASE3_MODE`. It decides **whether real (paid) API calls happen and how many papers get processed.** That's it — one knob, four settings:

| Mode     | Real API calls? | Papers processed                | Asks you to confirm? | When you'd use it                                    |
|----------|-----------------|----------------------------------|----------------------|------------------------------------------------------|
| `test`   | **No** (fakes)  | All                              | No                   | Learning the pipeline, running tests — **the safe default** |
| `single` | Yes, ~$0.16     | **1**                            | Yes (type `yes`)     | Checking your prompt works before spending real money |
| `dev`    | Yes, budget-capped | `PHASE3_DEV_LIMIT` (default 5) | Yes                  | A small real run to sanity-check end-to-end          |
| `batch`  | Yes, 50% cheaper| All (~250)                       | Yes                  | The real, full run                                   |

How to set it — open `.env` and edit one line:

```
PHASE3_MODE=test
```

Or override it for a single command without touching `.env`:

```powershell
python llm-sum/run_phase3.py summarize --mode single
```

**Why a single knob instead of several settings?** An earlier version had two separate switches that interacted in confusing ways — it was easy to *think* you were safe when you weren't. Collapsing everything into one named mode means there's exactly one thing to check, and its name tells you what will happen. `test` mode is also the default, so if you forget to set anything, nothing gets charged.

**Three safety facts worth trusting:**

1. `test` mode physically cannot call a paid API, even if another setting says otherwise — it's the last line of defence.
2. Every paid mode (`single`/`dev`/`batch`) stops and asks you to type `yes` before the first real call.
3. If you typo the mode (e.g. `PHASE3_MODE=produciton`), it falls back to `test` and prints a warning, rather than guessing and spending money.

You'll see this banner at the top of every command, confirming what's about to happen:

```
[phase3] mode=test | real-time | DRY_RUN
```

If you ever *don't* see that line, the command didn't actually start.

---

## 4. Your first run — do this exactly once, for free

Before spending a cent, run the whole pipeline in `test` mode. It uses fake summaries so it costs nothing, but it proves your files, paths, and prompts are all wired up correctly.

```powershell
# 1. Make sure .env says: PHASE3_MODE=test   (this is the default)

# 2. Turn your PDFs into clean text (this step is always free — no API).
python llm-sum/run_phase3.py extract

# 3. Check the extraction actually worked (also free, read-only).
python scripts/verify_extraction.py

# 4. Run a full mock summarise + judge — fake data, $0.00.
python llm-sum/run_phase3.py summarize
python llm-sum/run_phase3.py evaluate

# 5. See the scoreboard.
python llm-sum/run_phase3.py status
```

**What you should see:**

* `extract` prints one line per PDF and ends with `done — extracted=247 cached=3 failed=0`.
* `verify_extraction` prints a table and a summary like `PASS=243 WARN=6 FAIL=1`.
* `summarize` and `evaluate` run instantly and report `[MOCK ...]` summaries with `$0.00` spent.
* `status` shows counts for processed / summarised / evaluated.

If all of that works, your pipeline is healthy and you're ready to spend real money in the next section. **If anything fails here, fix it now** — it's free to debug at this stage, and every error you'd hit with real API calls also shows up in test mode.

**Why do a free dry run first?** Because a 250-paper paid run takes hours and costs ~$40. Finding out *after* you paid that your prompt file had a typo, or a PDF didn't extract, is the expensive way to learn. Test mode gives you the same code path, same files, same output shapes — just with fake LLM responses — so every wiring bug surfaces for free.

---

## 5. Step-by-step: what each script does and what to expect

### Step 1 — `extract` (always free)

```powershell
python llm-sum/run_phase3.py extract
```

Reads every PDF in `data/raw/`, strips the references section and publisher boilerplate, and saves the clean text to `data/processed/<name>.jsonl`. The output file is named after the PDF so you can always tell which is which:

```
data/raw/jvim__pre_illness_diet__10_1111_jvim_16872.pdf
data/processed/jvim__pre_illness_diet__10_1111_jvim_16872.jsonl
```

*Expect:* one `.jsonl` per PDF, and a final `extracted=… cached=… failed=…` line. Re-running is cheap and safe — it skips PDFs that haven't changed.

*Why it's a separate step:* extraction is deterministic and free, so you do it once and reuse the result for every summariser and the judge. You never make an LLM read a raw PDF — you feed it the cleaned text, which is cheaper and more reliable.

### Step 1.5 — `verify_extraction` (always free, highly recommended)

```powershell
python scripts/verify_extraction.py
```

Compares the raw PDF text against the cleaned cache and flags anything suspicious as PASS / WARN / **FAIL**. A FAIL usually means an image-only PDF or text that got over-trimmed.

*Expect:* a table plus a tally. Investigate every FAIL before paying to summarise — a garbled input produces a garbage summary you paid for.

*Why this matters for the project:* the whole study rests on the summaries being of *real* paper text. This check is your guarantee that you're not silently feeding the models broken inputs.

### Step 2 — `summarize` (free in test, paid otherwise)

```powershell
python llm-sum/run_phase3.py summarize --estimate   # forecast cost first — no API calls
python llm-sum/run_phase3.py summarize              # do it
```

Sends each paper's clean text to all three models with identical settings (`temperature=0`, fixed seed) and records each summary plus the exact model version and token counts.

*Expect (in `single` mode):*
```
[phase3] mode=single | limit=1 | real-time | confirm-required
[phase3:safety] About to submit REAL real-time API calls (limit=1).
  Type 'yes' to confirm: yes
  openai: success    (in=4521, out=487, ver=gpt-5.5-0325-preview, $0.0223)
  anthropic: success (in=4521, out=475, ver=claude-opus-4-6-..., $0.0214)
  gemini: success    (in=4612, out=482, ver=gemini-3.1-pro, $0.0165)
```

*Why `--estimate` first:* it tokenises your cached texts offline and tells you the projected bill *before* you commit. Cheap insurance against a surprise charge.

*Why record the exact model version and real token counts:* models change silently behind their names; logging the precise version lets you detect "drift" across reruns. And the token counts come straight from the API response, so your budget tracker matches the real invoice to the cent.

### Step 3 — `evaluate` (free in test, paid otherwise)

```powershell
python llm-sum/run_phase3.py evaluate
```

For each summary, a judge model scores quality (1–10), counts hallucinations, and flags low-confidence cases for human review — **without being told which model wrote the summary**.

*Expect:* one line per (paper, summariser, judge) with a score and how it was parsed (`json` / `regex` / `sentinel`).

*Why "blind":* an LLM asked to grade a summary it knows it wrote will inflate the score. Hiding the author is what makes the model comparison scientifically trustworthy — it's non-negotiable for the manuscript.

### Step 4 — `status` (always free)

```powershell
python llm-sum/run_phase3.py status
```

A read-only scoreboard: how many papers extracted, how many summarised per model, how many evaluated, how many flagged for human review. Run it any time to see where you are.

---

## 6. Copy-paste recipes

Once you understand the steps, these are the three real workflows.

### Recipe A — "Test my prompt on one paper" (~$0.16)

Do this whenever you change the prompt in `llm-sum/prompts/`.

```powershell
# .env: PHASE3_MODE=single
python llm-sum/run_phase3.py summarize      # type 'yes' → 1 paper × 3 models
python llm-sum/run_phase3.py evaluate
```
Read the resulting summary by hand. If it looks good, scale up. If not, fix the prompt and repeat — you've spent 16 cents, not $40.

### Recipe B — "Small real run on 5 papers"

```powershell
# .env: PHASE3_MODE=dev   (and optionally PHASE3_DEV_LIMIT=5)
python llm-sum/run_phase3.py summarize --estimate
python llm-sum/run_phase3.py summarize      # type 'yes'
python llm-sum/run_phase3.py evaluate
python llm-sum/run_phase3.py status
```

### Recipe C — "The full ~250-paper run" (~$40)

Batch mode is half-price and runs overnight at the provider's end.

```powershell
# .env: PHASE3_MODE=batch
python scripts/verify_extraction.py             # 1. confirm zero FAILs
python llm-sum/run_phase3.py summarize --estimate   # 2. confirm the projected bill
python llm-sum/run_phase3.py summarize          # 3. type 'yes' → submits batch jobs, returns

#   ... wait up to 24h while the providers process ...

python llm-sum/check_batch_status.py            # 4. collect finished results
python llm-sum/run_phase3.py evaluate           # 5. judge the collected summaries
python llm-sum/run_phase3.py status             # 6. final scoreboard
```

*Why batch for the real run:* you send all 250 requests at once and the provider returns them within 24 hours at **50% off**. You start it before leaving for the day and collect in the morning. While you wait you can even switch `.env` to `single` to tinker with prompts — `check_batch_status.py` ignores the current mode and always collects whatever jobs are pending, so the two don't interfere.

---

## 7. The full script list

Each script has its own detailed guide (same layout every time: what it does / when to run / inputs / outputs / CLI / per-mode behaviour / common errors / worked example).

| Script | What it's for | Guide |
|--------|---------------|-------|
| `prepare_texts.py` | PDFs → cleaned-text cache | [prepare_texts.md](prepare_texts.md) |
| `verify_extraction.py` | Audit extraction quality (PASS/WARN/FAIL) | [verify_extraction.md](verify_extraction.md) |
| `summarizer.py` | Run the 3 summarisers (real-time or batch) | [summarizer.md](summarizer.md) |
| `evaluator.py` | Blind judge scoring | [evaluator.md](evaluator.md) |
| `check_batch_status.py` | Collect finished batch jobs | [check_batch_status.md](check_batch_status.md) |
| `cost_estimator.py` | Forecast cost offline (no API) | [cost_estimator.md](cost_estimator.md) |
| `run_phase3.py` | Orchestrator wrapping all steps | [run_phase3.md](run_phase3.md) |

One-shot helper (not part of the normal flow): [`scripts/migrate_processed_filenames.py`](../../scripts/migrate_processed_filenames.py) renames any old-style cache files to the new descriptive names. Run it once with `--dry-run` to preview, then without to apply.

---

## 8. Where everything ends up

```
data/
├── processed/<name>.jsonl          ← extract  : cleaned text, one file per PDF
├── summaries.jsonl                 ← summarize: all 3 models' summaries per paper
├── evaluations.jsonl               ← evaluate : the judge scores
├── batch_jobs.jsonl                ← batch    : provider job IDs + status
├── batch/                          ← batch    : scratch upload files (safe to delete)
├── verify_extraction_report.txt    ← verify   : the audit table
└── error_log.jsonl                 ← any step : anything that failed
```

`summaries.jsonl` and `evaluations.jsonl` are **append-only** — never edit them by hand. New results are added one line at a time, so if a run crashes halfway you keep everything completed so far and simply resume. That crash-safety is exactly why the project uses JSON-Lines files instead of one big document: a power cut costs you the current paper, not all 250.

---

## 9. If something goes wrong

* **Re-run safely.** Every step is resumable. `summarize --resume` and `evaluate` (resume is on by default) skip work that already succeeded, so you never pay twice for the same paper.
* **Check `data/error_log.jsonl`** — every failure across the project is logged there with the DOI and the stage it failed at.
* **Start from test mode.** If a paid run misbehaves, reproduce it with `PHASE3_MODE=test` — same code, no cost — and debug there.
* **Per-script help:** `python llm-sum/run_phase3.py <step> --help`, or open that step's `.md` guide above.
