# Phase 5 — Pilot Human Validation (trial the reviewer workflow before the real run)

**Role of this document:** operator how-to for `export-pilot-human-review`, a
**trial harness** for the human-validation workflow. It lets you experience the
reviewer side yourself — and optionally hand a folder to one or two testers —
using just the small dev-mode sample already in `data/dev_summaries_jsonl/`,
before committing to the real export in
[human_validation.md](human_validation.md).

It does **not** replace the real export or its ingest/correlation analysis; it
is a dress rehearsal so you can decide whether the packets, scoresheet, and
folder layout work for a veterinarian reviewer. The authoritative study score
is still Phase 3's blind LLM jury; real human validation still runs through
`export-human-review` → `ingest-human-review` → `eval-report`.

```text
summarize --mode dev  →  evaluate --mode dev  →  export-pilot-human-review
  data/dev_summaries_jsonl/   data/evaluations.jsonl   data/pilot_human_review/humanN/
```

---

## 1. What makes the pilot different from the real export

| | `export-human-review` (real) | `export-pilot-human-review` (this) |
|---|---|---|
| Sampling pool | whole `data/evaluations.jsonl` corpus | only articles in `data/dev_summaries_jsonl/` |
| Reviewers per run | all `--reviewers N` at once | **one** more `humanN/` per run, incremental |
| Between testers | every reviewer sees the same items | **partial overlap** (configurable ratio) |
| Output | `data/human_review/reviewer_N/` | `data/pilot_human_review/humanN/` |

Everything else is now **shared code** in `llm-sum/human_review.py`: the
stratified sampler, the blind protocol, the un-blinding key, the reviewer guide,
the interactive "how many articles?" prompt (minimum 5, one per journal), the
nested per-item folder layout (`item_NNN/article.md` + `summary.md`), the
version-labeled `.xlsx` scoresheet, and the `original_articles/` PDF copy. The
pilot (`llm-sum/pilot_human_review.py`) only adds the dev-pool scoping,
incremental `humanN/` folders, and the tester-overlap carry on top.

---

## 2. Prerequisites

You need dev summaries **and** dev evaluations for them:

```powershell
python llm-sum/run_phase3.py summarize --mode dev   # writes data/dev_summaries_jsonl/
python llm-sum/run_phase3.py evaluate  --mode dev   # writes rows to data/evaluations.jsonl
```

(Both are live-API steps — run them yourself in PowerShell, per the project's
live-call rule.) The pilot export itself is offline and makes no API calls. A
dev article that has been summarized but **not yet evaluated** is skipped with a
warning naming it — run `evaluate --mode dev` to include it.

---

## 3. Running the export

Each run creates the **next** `humanN/` folder and never touches the earlier
ones:

```powershell
python llm-sum/run_phase3.py export-pilot-human-review     # -> human1/
python llm-sum/run_phase3.py export-pilot-human-review     # -> human2/  (shares items with human1)
python llm-sum/run_phase3.py export-pilot-human-review     # -> human3/  (shares items with human2)
```

Available through `python llm-sum/run_phase3.py export-pilot-human-review`:

| Flag | Overrides | Default |
|---|---|---|
| `--overlap-ratio R` | `PILOT_HUMAN_REVIEW_OVERLAP_RATIO` | `0.6` |
| `--sample-size N` (articles) | `PILOT_HUMAN_REVIEW_SAMPLE_SIZE` | ask interactively (min 5), else one per evaluated dev article |
| `--sample-unit {articles,items}` | `PILOT_HUMAN_REVIEW_SAMPLE_UNIT` | `articles` |
| `--seed N` | `PILOT_HUMAN_REVIEW_SEED` | `42` |

The default sample unit is now **`articles`**: `--sample-size` counts distinct
articles (one per journal), each expanded to all three providers' summaries — so
5 articles → 15 scored items, ordered provider-major (article1..5 provider A,
then provider B, then C). When `--sample-size` is omitted at a real terminal, the
export **asks how many articles** you'll evaluate (minimum 5); non-interactive
runs fall back to one article per evaluated dev article.

The path-override flags below are **not** exposed through `run_phase3.py`
(same as `export-human-review`) — run the module directly instead:
`python llm-sum/pilot_human_review.py --overlap-ratio 0.6 ...`.

| Flag (module-direct only) | Default |
|---|---|
| `--evaluations PATH` | `data/evaluations.jsonl` |
| `--dev-summaries-dir PATH` | `data/dev_summaries_jsonl/` |
| `--raw-dir PATH` | `data/raw/` |
| `--output-dir PATH` | `data/pilot_human_review/` |

### The overlap ratio spans all three regimes you might want

The single `--overlap-ratio` knob covers every mode of running consecutive
testers:

- **`1.0` — identical items every run.** Every tester scores the exact same
  set. Maximises inter-reviewer agreement data; adds no new coverage.
- **`0.0` — a fresh, independent draw each run.** Maximises coverage; no shared
  items to compare testers on.
- **`0.6` (default) — partial overlap.** Carry 60% of the previous tester's
  articles, fill the rest with genuinely new ones. Worked example: with 5
  articles, `round(5 × 0.6) = 3` are shared with the previous tester and 2 are
  new — so the two testers share 9 of their 15 scored items. This is the
  recommended balance: some shared articles to check testers agree on, some new
  ones to widen coverage.

In the default `articles` unit the carry is a whole number of **articles** (all
three of a carried article's summaries come across together). The carried subset
is the previous tester's first *k* articles in `item_id` order, so it is
deterministic and reproducible.

---

## 4. What gets written

```text
data/pilot_human_review/
  unblinding_key_human1.json     PRIVATE — never hand this to a tester
  unblinding_key_human2.json
  human1/
    REVIEWER_GUIDE.md            full zero-jargon guide (docs/booklet/07_...)
    packet.md                    navigation index: one row per item_id -> folder
    scoresheet_human1.xlsx       dropdowns (1-5, yes/no), frozen header, version-labeled rows
    item_001/
      article.md                 the original article text
      summary.md                 one candidate summary to score against it
    item_002/
      ...
    item_015/                    5 articles x 3 providers = 15 item folders
    original_articles/
      javma__...__10_2460_javma_24_05_0312.pdf   real source PDF(s), copied from data/raw
  human2/
    REVIEWER_GUIDE.md
    packet.md
    scoresheet_human2.xlsx
    item_001/ ...
    original_articles/
      ...
```

Each `humanN/` folder therefore holds **15 item folders** (5 articles, each read
three times against a different provider's summary) + the guide + the packet
index + the `.xlsx` scoresheet + the `original_articles/` PDFs.

- **Self-contained folder.** Hand a tester their whole `humanN/` folder
  (e.g. zipped) — guide, packet, scoresheet, and PDFs — and they need nothing
  else, no repo access.
- **The private key is a sibling of the folders, never inside one.** So
  handing off a `humanN/` folder cannot leak which AI wrote which summary. It
  also records what the next tester's overlap carries forward.
- **The article title is shown** in `packet.md`, in each `item_NNN/summary.md`,
  and as a reference column in the scoresheet. That is blind-safe — the title
  names the *article*, not the model. The blind protocol only hides which AI
  wrote the summary, and a test asserts the reviewer-facing files (packet, the
  item folders, **and** the `.xlsx`) contain no model identifier.

### About the PDFs (a methods note worth keeping in mind)

The AI summariser only ever saw the **extracted text** (what each
`item_NNN/article.md` shows), not the PDF's figures or tables. The PDF is copied
in as *supplementary context* — easier to read, and a way to catch a
text-extraction glitch — but a reviewer should score each summary against the
**article.md text**, since that is what the model actually worked from. Scoring
against a figure the AI never received would penalise it unfairly. The reviewer
guide says this in plain language.

---

## 5. When the dev pool is small

The pilot draws from however many dev articles you've summarized+evaluated. With
the default `articles` unit, a 5-article dev pool gives each tester **15 scored
items** — all three providers' summaries of each of the 5 articles, spread one
per journal by the existing stratified sampler.

The overlap carry is counted in whole **articles**. If you ask for more
genuinely-new articles than the pool can supply (e.g. a small pool and a low
overlap ratio, so most articles were already shown to the previous tester), the
export **backfills** the shortfall with already-used articles and prints a clear
warning rather than silently under-filling. Run more `summarize --mode dev` /
`evaluate --mode dev` rounds to widen the pool.

---

## 6. Tests

```powershell
python -m pytest tests/test_pilot_human_review.py -q
```

Covers the dev-pool scoping, the self-contained folder, the blind protocol on
both `packet.md` and the `.xlsx`, the overlap fraction (and the `1.0`/`0.0`
regimes), that a later export never mutates an earlier folder, the
prerequisite/empty-pool handling, PDF copy (including the missing-PDF warning),
and the pool-exhaustion backfill. All offline against mock fixtures.

---

## 7. Related reading

- [human_validation.md](human_validation.md) — the **real** export/ingest/analysis loop this rehearses
- [../booklet/07_human_validation_guide.md](../booklet/07_human_validation_guide.md) — the zero-jargon reviewer guide shipped in every folder
- [../phase3/medhelm_evaluation.md](../phase3/medhelm_evaluation.md) — the five-criterion jury rubric humans also score
