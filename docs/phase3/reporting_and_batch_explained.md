# Reporting commands & batch mode — explained simply

There are a lot of commands with similar-sounding names (`eval-report`,
`eval-report --publication`, `report-figures`, `stats-engine`,
`check_batch_status.py`, `evaluate --mode batch --max-requests 50`, ...).
This page exists to untangle exactly that confusion, in one place, with
plain-language explanations and copy-paste examples. If you remember nothing
else from this page, remember the two tables in **Part 0**.

Every command on this page is **free and offline** except the ones explicitly
marked **PAID** in Part 2 — none of the reporting commands ever call an LLM
provider; they only re-read files you already have on disk
(`data/evaluations.jsonl`, `data/summaries.jsonl`).

---

## Part 0 — the quick answer

### "I want to see X — which command do I run?"

| You want... | Run this | Free? |
|---|---|---|
| A quick scoreboard of how many papers are done at each stage | `python llm-sum/run_phase3.py status` | Free |
| A one-page summary of scores, right now, on screen | `python llm-sum/run_phase3.py eval-report` | Free |
| That same summary saved as a Markdown file I can open/share | `python llm-sum/run_phase3.py eval-report --markdown` | Free |
| One deep-dive file **per paper** (every judge's full reasoning) | `python llm-sum/run_phase3.py eval-report --rich-detail` | Free |
| Statistical proof one model beats another (p-values) | `python llm-sum/run_phase3.py eval-report --publication` | Free |
| Confidence intervals around each model's mean score | `python llm-sum/run_phase3.py eval-report --publication` (or `report-figures`, see below) | Free |
| Whether the judges even agree with each other (Krippendorff's alpha) | `python llm-sum/run_phase3.py eval-report --markdown` (already included) | Free |
| Whether AI summaries keep the info the abstract has (TF-IDF/cosine) | `python llm-sum/run_phase3.py eval-report --publication` (or `stats-engine`, see below) | Free |
| PNG/SVG charts for a slide deck or paper | `python llm-sum/run_phase3.py report-figures` | Free |
| A single "leaderboard" file ranking the 3 providers | `python llm-sum/run_phase3.py report-figures` | Free |
| To collect results from a batch job I submitted earlier | `python llm-sum/check_batch_status.py` | Free (polls only) |
| To have AI actually write summaries | `python llm-sum/run_phase3.py summarize` (see Part 2 for batch mode) | **PAID** unless `--mode test` |
| To have AI actually judge summaries | `python llm-sum/run_phase3.py evaluate` (see Part 2 for batch mode) | **PAID** unless `--mode test` |

All twelve full commands above, one per line, ready to paste:

```powershell
python llm-sum/run_phase3.py status
python llm-sum/run_phase3.py eval-report
python llm-sum/run_phase3.py eval-report --markdown
python llm-sum/run_phase3.py eval-report --rich-detail
python llm-sum/run_phase3.py eval-report --publication
python llm-sum/run_phase3.py report-figures
python llm-sum/run_phase3.py stats-engine
python llm-sum/check_batch_status.py
python llm-sum/run_phase3.py summarize
python llm-sum/run_phase3.py evaluate
```

### The four-stage pipeline these commands sit inside

```
 STAGE 1          STAGE 2            STAGE 3              STAGE 4
extract    ──▶   summarize    ──▶   evaluate      ──▶    REPORT ON IT
(free)          (PAID: writes      (PAID: writes         (free: everything
                 summaries.jsonl)   evaluations.jsonl)     in this page)
```

Everything on this page lives in **Stage 4** — it never spends money, and you
can run it as many times as you like, in any order, against whatever is
currently in `data/evaluations.jsonl`. It's the only stage where re-running a
command costs you nothing but time.

---

## Part 1 — the reporting family (Stage 4)

There isn't one "report" command — there are **six**, because they answer
six different questions. This section explains each one, what question it
answers, and exactly what file it writes.

### 1.1 The six report commands, side by side

| Command | Question it answers | What it contains | Output file(s) |
|---|---|---|---|
| `eval-report` | "How are things looking, overall and per model?" | Scores, hallucination rates, **Krippendorff's alpha** (do the judges agree?), corpus-wide automatic metrics (compression/coverage/ROUGE) | `data/results/json/eval_report_<ts>.json` always; add `--markdown` for `data/results/reports/eval_report_<ts>.md` |
| `eval-report --rich-detail` | "Show me everything about *this one paper*" | Per-paper: every judge's individual score + full reasoning + automatic metrics for that paper alone | `data/results/detail_rich/<doi-slug>.md` — one file per paper |
| `eval-report --publication` | "Is Model A *statistically* better than Model B, and by how much, with what confidence?" | **p-values** (Wilcoxon/Friedman), **bootstrap confidence intervals**, **TF-IDF/cosine** info-density check | `data/results/publication_report_<ts>.{json,md}` + `data/results/publication_report_<ts>_tables/*.csv` |
| `report-figures` | "Give me one chart-ready leaderboard file + PNG/SVG figures" | Same bootstrap-CI provider comparison as `--publication`, rendered as charts and a ranked leaderboard | `data/results/leaderboard_<ts>.{json,md}` + `data/results/figures_<ts>/*.png,*.svg` |
| `stats-engine` | "Standalone deep-dive on information density + inter-rater Kappa + cost economics" | TF-IDF/cosine info density, Cohen's Kappa, subscription-cost tables, provider × species/journal/study-design breakdowns | `data/results/stats_engine_report_<ts>.{json,md}` |
| `status` | "How far along am I?" | Raw counts per pipeline stage — no scores | Printed only, nothing saved |

**Every filename above includes its own timestamp (`<ts>`) and is never
overwritten** — every time you run one of these commands, it adds a new
snapshot file rather than replacing the old one. So `data/results/` fills up
with a history of every report you've ever generated; nothing is silently
lost when `data/evaluations.jsonl` gets more rows later.

### 1.2 Why isn't it just ONE command?

Because the six answer genuinely different questions and cost different
amounts of *your* time to read:

- **`eval-report`** is the one you run constantly — it's fast, cheap to read,
  and tells you if anything looks wrong (a hallucination spike, judges
  disagreeing, etc.). This is your day-to-day dashboard.
- **`--rich-detail`** is what you open when `eval-report` flags something
  suspicious about *one specific paper* and you want to read the judge's
  actual reasoning, word for word.
- **`--publication`**, `report-figures`, and `stats-engine` are what you run
  **once you're ready to write the results up** — they're heavier, slower to
  read, and full of statistics that only matter once you're comparing models
  formally (for a report/paper) rather than just sanity-checking.

Splitting them means a quick daily check (`eval-report`) doesn't force you to
scroll past six pages of p-value tables you don't need yet.

### 1.3 What "--markdown" means (it's not the same everywhere)

`--markdown` is a flag on `eval-report` specifically. It doesn't change *what*
gets computed — the same numbers are always computed — it only changes
**how they're shown to you**:

| Without `--markdown` | With `--markdown` |
|---|---|
| Prints a dense text table straight to your terminal | Prints a nicer plain-English report *and* saves it as a `.md` file you can open in any Markdown viewer (VS Code, GitHub, Obsidian, ...) |

`--rich-detail` and `--publication` always save Markdown regardless — you
never need to add `--markdown` to those; that flag is specific to the plain
`eval-report` mode. `report-figures` and `stats-engine` also always save
Markdown by default (no flag needed).

### 1.4 Where each statistic actually lives (cross-reference to your last question)

| Statistic | Full command to run | Section heading to look for |
|---|---|---|
| Krippendorff's alpha | `python llm-sum/run_phase3.py eval-report --markdown` | `## Reliability (inter-judge agreement)` |
| Wilcoxon / Friedman p-values | `python llm-sum/run_phase3.py eval-report --publication` | `## Significance (unweighted score)` / `(weighted score)` |
| Bootstrap 95% confidence intervals | `python llm-sum/run_phase3.py eval-report --publication` (also in `report-figures`) | `## Provider comparison` (shown as `mean [low, high]`) |
| TF-IDF + cosine similarity | `python llm-sum/run_phase3.py eval-report --publication` (also in `stats-engine`) | `## Information density (summary vs. abstract)` |
| Corpus-wide automatic metrics (compression/coverage/ROUGE) | `python llm-sum/run_phase3.py eval-report --markdown` | `## Headline` + `### Automatic Metrics (averaged per summary)` under `## By Provider` |

```powershell
python llm-sum/run_phase3.py eval-report --markdown         # Krippendorff's alpha + automatic metrics
python llm-sum/run_phase3.py eval-report --publication      # Wilcoxon/Friedman p-values, bootstrap CIs, TF-IDF/cosine
python llm-sum/run_phase3.py report-figures                 # bootstrap CIs again, as charts + leaderboard
python llm-sum/run_phase3.py stats-engine                   # TF-IDF/cosine again, standalone deep-dive
```

### 1.5 Copy-paste — run all six, in the order you'd normally want them

```powershell
python llm-sum/run_phase3.py status                        # 1. where am I?
python llm-sum/run_phase3.py eval-report --markdown         # 2. daily dashboard
python llm-sum/run_phase3.py eval-report --rich-detail      # 3. per-paper deep dive (all papers)
python llm-sum/run_phase3.py eval-report --publication      # 4. p-values, CIs, info density
python llm-sum/run_phase3.py report-figures                 # 5. charts + leaderboard
python llm-sum/run_phase3.py stats-engine                   # 6. standalone deep stats
```

All six are free, offline, and safe to run in any order, as many times as
you like — none of them touch `data/evaluations.jsonl`, they only read it.

---

## Part 2 — batch mode, for BOTH summarizing and judging

This is the part that trips people up: **batch mode isn't one thing that
happens once** — it happens twice in the pipeline, once for `summarize` and
once for `evaluate`, and the exact same collector script
(`check_batch_status.py`) handles both.

### 2.1 The mental model

Batch mode means: instead of the AI answering you immediately (real-time),
you hand a big pile of requests to the provider, they work through it in the
background (minutes to 24 hours), and you come back later to collect the
results. It's **50% cheaper** than real-time, which is why it's used for the
full ~250-paper run.

```
   SUBMIT                    WAIT                      COLLECT
┌──────────┐          ┌─────────────────┐        ┌──────────────────────┐
│ summarize│  ──▶      │ minutes to 24h  │  ──▶   │ check_batch_status.py│
│ --mode   │           │ (do other work  │        │ merges results into  │
│  batch   │           │  while you wait)│        │ data/summaries.jsonl │
└──────────┘          └─────────────────┘        └──────────────────────┘

┌──────────┐          ┌─────────────────┐        ┌──────────────────────┐
│ evaluate │  ──▶      │ minutes to 24h  │  ──▶   │ check_batch_status.py│
│ --mode   │           │ (same script    │        │ merges results into  │
│  batch   │           │  works for both)│        │ data/evaluations.jsonl│
└──────────┘          └─────────────────┘        └──────────────────────┘
```

**`check_batch_status.py` is the same script for both rows above.** It
doesn't need to be told whether it's collecting summaries or judge scores —
it just looks at every job in `data/batch_jobs.jsonl` and merges each one into
whichever file it belongs to. Run it once, and it collects *everything* that's
ready, summarize jobs and evaluate jobs together.

### 2.2 Submitting a summarize batch (Stage 2)

```powershell
python llm-sum/run_phase3.py summarize --estimate --mode batch   # free — forecast cost first
python llm-sum/run_phase3.py summarize --mode batch              # PAID — submits, type 'yes', returns immediately
```

### 2.3 Submitting a judge (evaluate) batch (Stage 3) — this is your "batch 50 openai" question

Same idea, but for judging instead of summarizing. The extra piece is
`--max-requests`, which limits how many new judge requests get submitted **for
one provider at a time**, so you don't trip a provider's token cap:

```powershell
python llm-sum/run_phase3.py evaluate --mode batch --max-requests 50 --judges openai
```

Reading that command left to right:
- `evaluate` — this is the judge step, not the summarize step.
- `--mode batch` — submit as an async batch job (50% cheaper), not real-time.
- `--max-requests 50` — only queue **50 new judge requests** in this
  submission. (Why 50? OpenAI has a per-model cap on how many *prompt tokens*
  can be queued across all your in-flight batches at once — around 900,000
  tokens for the model this project uses. This project's judge prompts run
  roughly 12,000-12,500 tokens each, so ~50-60 requests is the safe chunk size
  that stays under that cap. See `batch_mode.md`'s Problem 2 for the full
  story if you ever hit `token_limit_exceeded`.)
- `--judges openai` — only submit for OpenAI's judge this time, leaving
  Anthropic/Gemini alone (useful if only one provider is close to its cap, or
  you're chunking one provider at a time).

**Then collect it, exactly the same way as a summarize batch:**

```powershell
python llm-sum/check_batch_status.py
```

**Then, once it's done, repeat the same command to sweep the next chunk** —
`--resume` is on by default for `evaluate`, so it automatically picks up
where the last chunk left off instead of resubmitting the same 50 papers:

```powershell
python llm-sum/run_phase3.py evaluate --mode batch --max-requests 50 --judges openai
#  ... wait, then collect again ...
python llm-sum/check_batch_status.py
#  ... repeat until evaluate reports nothing left to submit ...
```

Full walkthrough of a real chunked run, plus every batch-specific flag
(`--resume`, `--force`, `--limit` for summarize; `--max-requests`,
`--judges` for evaluate) and every failure this project has actually hit:
[batch_mode.md](batch_mode.md).

### 2.4 One collector, both stages — worked example

```powershell
# Morning: submit both stages' batch jobs (they don't need to wait for each other)
python llm-sum/run_phase3.py summarize --mode batch
python llm-sum/run_phase3.py evaluate --mode batch --max-requests 50 --judges openai

# ... go do something else for a few hours ...

# Afternoon: one command collects whatever finished, from either stage
python llm-sum/check_batch_status.py
# -> merges any completed summarize jobs into data/summaries.jsonl
# -> merges any completed evaluate jobs into data/evaluations.jsonl
# -> jobs still "in_progress" are left alone and reported as such

# Anything still pending? Just run it again later — always safe, never re-submits.
python llm-sum/check_batch_status.py
```

### 2.5 What "batch status" actually means

There is no separate command called "batch status" — it's short for running
`check_batch_status.py`, which prints the **status** of every batch job:

```
[phase3:batch] openai_sum_2026-05-25T14-22-01Z status=completed; downloading result file
[phase3:batch] merged 248 summaries into data/summaries.jsonl
[phase3:batch] anthropic_sum_2026-05-25T14-22-04Z status=in_progress; skipping
```

You can also check status **without** downloading/merging anything, if you
just want to peek:

```powershell
python llm-sum/check_batch_status.py --no-download
```

This is always safe and free — it only polls the provider for a status
string, same as checking your email for a reply.

---

## Part 3 — glossary (plain English)

| Term | Plain-English meaning |
|---|---|
| **Real-time mode** | The AI answers you right away, in the same command. Costs full price. Used for `single`/`dev`/`test`. |
| **Batch mode** | You submit a pile of requests, wait, then collect. Half price. Used for `batch` (the full ~250-paper run). |
| **`--markdown`** | Flag on `eval-report` only — same numbers, nicer/saved Markdown output instead of a terminal table. |
| **`--publication`** | A *different* command (`report_tables.py`) that `eval-report` can delegate to — heavier statistics for writing results up formally. |
| **p-value** | How likely it is that one model's score being higher than another's is just random noise, not a real difference. Smaller = more confident it's real. |
| **Bootstrap confidence interval** | A range around a mean score reflecting how much that mean might wobble if you'd sampled slightly different papers. |
| **Krippendorff's alpha** | A single number (usually 0-1) measuring how much the judges *agree with each other*. Low alpha = don't trust the individual scores too much yet. |
| **TF-IDF + cosine similarity** | A word-overlap-based similarity score between a candidate summary and the paper's abstract — checks whether the summary "kept" the abstract's information, not whether it's factually correct. |
| **`--rich-detail`** | Flag on `eval-report` — writes one deep-dive Markdown file *per paper* instead of one bundled file for the whole run. |
| **`check_batch_status.py`** | The one script that collects results for BOTH `summarize --mode batch` and `evaluate --mode batch` jobs. Never submits anything new; always safe to re-run. |
| **`--max-requests N`** | Judge-batch-only flag: caps how many new judge requests get submitted *per judge* in one `evaluate --mode batch` call, to stay under a provider's token cap. |

---

## See also

- [eval_report.md](eval_report.md) — full flag reference for `eval-report` (plain, `--markdown`, `--rich-detail`).
- [batch_mode.md](batch_mode.md) — every batch flag, on-disk state, and the troubleshooting playbook for real batch failures.
- [check_batch_status.md](check_batch_status.md) — full CLI reference for the collector script.
- [../phase6/reporting.md](../phase6/reporting.md) — technical detail behind `--publication`, `report-figures`, `stats-engine`.
- [../statistics_explained.md](../statistics_explained.md) — what each statistic means and the exact formula/library behind it.
- [README.md](README.md) — the beginner walkthrough for Stages 1-3 (extract/summarize/evaluate).
