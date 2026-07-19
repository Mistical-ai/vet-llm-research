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

The pilot has its own full loop, kept strictly separate so a rehearsal never
touches the real study's numbers:
`export-pilot-human-review` → fill scoresheet(s) → `ingest-pilot-human-review`
→ `eval-report --human-reviews data/pilot_human_reviews.jsonl` (§5 below).

```text
summarize --mode dev  →  evaluate --mode dev  →  export-pilot-human-review  →  ingest-pilot-human-review
  data/dev_summaries_jsonl/   data/evaluations.jsonl   data/pilot_human_review/humanN/   data/pilot_human_reviews.jsonl
```

---

## 1. What makes the pilot different from the real export

| | `export-human-review` (real) | `export-pilot-human-review` (this) |
|---|---|---|
| Sampling pool | whole `data/evaluations.jsonl` corpus | only articles in the **top level** of `data/dev_summaries_jsonl/` (see the note below `--dev-summaries-dir`) |
| Reviewers per run | **one** more `humanN/` per run, incremental | **one** more `humanN/` per run, incremental |
| When a journal is too thin for its exact quota | errors (`JournalQuotaError`), never samples unevenly | backfills from other journals with a warning |
| Output | `data/human_review/humanN/` | `data/pilot_human_review/humanN/` |

Both commands now share the same incremental `humanN/`, exact-per-journal-quota,
and overlap-carry machinery in `llm-sum/human_review.py` — `next_human_number`,
`select_incremental_rows`, `sample_articles_from_journal_quota`, the blind
protocol, the un-blinding key, the reviewer guide, the "5/10/25 articles?"
prompt, the nested per-item folder layout (`item_NNN/article.pdf` +
`summary.md`, copied per item from `data/raw`), and the version-labeled
`.xlsx` scoresheet. The pilot (`llm-sum/pilot_human_review.py`) only adds
the dev-pool scoping and the softer backfill-on-shortfall policy on top —
appropriate for a dry-run tool whose whole point is a still-growing
dev pool.

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
python llm-sum/run_phase3.py export-pilot-human-review     # -> human2/  (overlap-carried + new)
python llm-sum/run_phase3.py export-pilot-human-review     # -> human3/  (overlap-carried + new)
```

Available through `python llm-sum/run_phase3.py export-pilot-human-review`:

| Flag | Overrides | Default |
|---|---|---|
| `--overlap-ratio R` | `PILOT_HUMAN_REVIEW_OVERLAP_RATIO` | `0.6` |
| `--sample-size N` (articles) | `PILOT_HUMAN_REVIEW_SAMPLE_SIZE` | ask interactively (5/10/25 menu), else `5` |
| `--seed N` | `PILOT_HUMAN_REVIEW_SEED` | `42` |

`--sample-size` is an **exact article count, always split evenly across the
study's 5 journals** — it must be a multiple of 5 (5 -> 1/journal, 10 ->
2/journal, 25 -> 5/journal), each expanded to all three providers' summaries —
so 5 articles → 15 scored items, interleaved so two summaries of one article
are never adjacent. When `--sample-size` is omitted at a real terminal, the
export shows a short menu and **asks you to pick 5, 10, or 25 articles**;
non-interactive runs fall back to 5.

The path-override flags below are **not** exposed through `run_phase3.py`
(same as `export-human-review`) — run the module directly instead:
`python llm-sum/pilot_human_review.py --overlap-ratio 0.6 ...`.

| Flag (module-direct only) | Default |
|---|---|
| `--evaluations PATH` | `data/evaluations.jsonl` |
| `--dev-summaries-dir PATH` | `data/dev_summaries_jsonl/` — top-level `*.txt` only, non-recursive. Point it at `data/dev_summaries_jsonl/NAME/` (an `--output-subdir` pool), `data/single_summaries_jsonl/`, or `data/batch_summaries_jsonl/` to draw the pool from there instead. This is the review-side twin of `evaluate --source-dir`. |
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
  articles from EACH journal, fill the rest with genuinely new ones. Worked
  example: with 5 articles (1/journal), `round(1 × 0.6) = 1` (rounds up) is
  shared per journal and the rest is fresh — see `human_review.select_incremental_rows`
  for how the carry stays balanced per journal so the leftover draw is always
  a valid per-journal count too. This is the recommended balance: some shared
  articles to check testers agree on, some new ones to widen coverage.

The carry is counted in whole **articles**, carried the same fraction from
every journal (not just "the first k overall") — this is what guarantees the
leftover "new" draw always splits evenly across journals too, no matter what
`--overlap-ratio` is set to. The carried subset is deterministic and
reproducible (the previous tester's articles in `item_id` order, per journal).

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
      article.pdf                 the original published article, copied from data/raw
                                   (falls back to article.md — the cached text the AI
                                   read — if no source PDF can be resolved for that DOI)
      summary.md                  one candidate summary to score against it
    item_002/
      ...
    item_015/                    5 articles x 3 providers = 15 item folders, each with
                                  its own article.pdf copy
  human2/
    REVIEWER_GUIDE.md
    packet.md
    scoresheet_human2.xlsx
    item_001/ ...
```

Each `humanN/` folder therefore holds **15 item folders** (5 articles, each read
three times against a different provider's summary, each with its own
`article.pdf`) + the guide + the packet index + the `.xlsx` scoresheet.

- **Self-contained folder.** Hand a tester their whole `humanN/` folder
  (e.g. zipped) — guide, packet, scoresheet, and every item's PDF — and they
  need nothing else, no repo access.
- **The private key is a sibling of the folders, never inside one.** So
  handing off a `humanN/` folder cannot leak which AI wrote which summary. It
  also records what the next tester's overlap carries forward.
- **The article title is shown** in `packet.md`, in each `item_NNN/summary.md`,
  and as a reference column in the scoresheet. That is blind-safe — the title
  names the *article*, not the model. The blind protocol only hides which AI
  wrote the summary, and a test asserts the reviewer-facing files (packet, the
  item folders, **and** the `.xlsx`) contain no model identifier.

### About the PDFs (a methods note worth keeping in mind)

The AI summariser only ever saw the **extracted text** cached in
`data/processed/` (the `article.md` fallback content), never the PDF's figures,
tables, or reference list — `article.pdf` is now the primary artifact a
reviewer reads, not a supplementary convenience, so this is a real methods
tradeoff worth keeping in mind: a reviewer sees more than the AI did. The
reviewer guide (`docs/booklet/08_human_validation_guide.md` §3) asks reviewers
not to penalise a summary for missing a chart's purely visual detail (the AI
never had the chance to see it), but to flag it normally if a paper's key
finding lived only in a graph/table and the summary consequently got it wrong
— that's a genuine, worth-recording AI limitation, not a foul against the
model.

---

## 5. Ingesting scores + rehearsing the analysis

Once a tester has filled in `scoresheet_humanN.xlsx` (1-5 per criterion,
yes/no for hallucination), ingest it — this reads every filled scoresheet
under `data/pilot_human_review/`, un-blinds each row against its sibling
`unblinding_key_humanN.json`, and writes one normalized JSONL file:

```powershell
python llm-sum/run_phase3.py ingest-pilot-human-review
```

| Flag | Default |
|---|---|
| `--review-dir PATH` | `data/pilot_human_review/` |
| `--output PATH` | `data/pilot_human_reviews.jsonl` |

This writes to **`data/pilot_human_reviews.jsonl`** — a separate file from
the real study's `data/human_reviews.jsonl` (see
[human_validation.md](human_validation.md)). The two are kept apart on
purpose: every `export-pilot-human-review` run stamps a `.pilot_export`
marker at the root of `data/pilot_human_review/`, and the real
`ingest-human-review` command checks for that marker (or the fixed
`pilot_human_review` folder name). But the guard's real condition is
narrower than "refuses any pilot folder": it only raises when **both**
`--review-dir` resolves to a pilot-marked folder **and** `--output` is left
at its exact default, `data/human_reviews.jsonl` (`HUMAN_REVIEWS_PATH`) —
which is exactly what `ingest-pilot-human-review` avoids by always passing
`--output data/pilot_human_reviews.jsonl`. Point `--output` at any other
path yourself and the guard does not fire, pilot folder or not — so don't
rely on it for safety beyond the default-output case.

To rehearse the analysis `eval-report` will eventually run for real, point it
at the pilot file explicitly:

```powershell
python llm-sum/run_phase3.py eval-report --human-reviews data/pilot_human_reviews.jsonl
```

`report-figures` and `stats-engine` accept the same `--human-reviews`
override. With a small pilot sample (e.g. 5 articles ≈ 15 items) expect
`eval-report` to show real Krippendorff's α / Spearman / Pearson numbers but
flag the human-vs-jury verdict as underpowered — `reliability.MIN_CORRELATION_N`
is 30 comparable items, well above what one pilot round supplies. That is
expected and does not indicate a bug; it is exactly the shape the real study
sample is sized to avoid.

---

## 6. When the dev pool is small

The pilot draws from however many dev articles you've summarized+evaluated. A
5-article request (1/journal) gives each tester **15 scored items** — all
three providers' summaries of each of the 5 articles, exactly one per journal.

If a journal's dev pool can't supply its exact share (e.g. a 10-article
request needs 2 VRU articles but only 1 has been evaluated), the pilot
**backfills** the shortfall from other journals — possibly reusing an
already-seen article as a last resort — and prints a clear warning rather
than erroring or silently under-filling; this is the one place the pilot
deliberately behaves more leniently than the real export (§1). Run more
`summarize --mode dev` / `evaluate --mode dev` rounds to widen the thin
journal(s).

---

## 7. Tests

```powershell
python -m pytest tests/test_pilot_human_review.py -q
```

Covers the dev-pool scoping, the self-contained folder, the blind protocol on
both `packet.md` and the `.xlsx`, the overlap fraction (and the `1.0`/`0.0`
regimes), that a later export never mutates an earlier folder, the
prerequisite/empty-pool handling, PDF copy (including the missing-PDF warning),
the pool-exhaustion backfill, the `.pilot_export` marker, and that
`ingest-human-review` refuses a pilot export directory while
`ingest-pilot-human-review` round-trips one into `data/pilot_human_reviews.jsonl`.
All offline against mock fixtures.

---

## 8. Related reading

- [human_validation.md](human_validation.md) — the **real** export/ingest/analysis loop this rehearses
- [../booklet/08_human_validation_guide.md](../booklet/08_human_validation_guide.md) — the zero-jargon reviewer guide shipped in every folder
- [../phase3/medhelm_evaluation.md](../phase3/medhelm_evaluation.md) — the five-criterion jury rubric humans also score
