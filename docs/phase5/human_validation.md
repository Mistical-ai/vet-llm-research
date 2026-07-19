# Phase 5 — Human Validation (blind export → ingest → agreement)

**Role of this document:** operator how-to for the full human-validation loop —
exporting blind review packets, ingesting the filled scoresheets, and reporting
how well the LLM jury tracks expert judgment. The authoritative score for this
study remains Phase 3's blind LLM jury (`llm-sum/evaluator.py` →
`data/evaluations.jsonl`) — human validation is a check against that score, not
a replacement for it. See
[medhelm_evaluation.md](../phase3/medhelm_evaluation.md) for the jury rubric
this validates.

The workflow is three commands. `export-human-review` writes ONE MORE
`humanN/` reviewer folder each time it's run (`human1/`, then `human2/`, ...,
never touching an earlier one) — sampled EXACTLY evenly across the study's 5
journals — so reviewers can be recruited and onboarded one at a time:

```text
export-human-review   →   (a reviewer fills their humanN/.xlsx)   →   ingest-human-review   →   eval-report
   data/human_review/humanN/                                          data/human_reviews.jsonl    Human Validation section
```

If you only read one section, read the next one — it explains *why* every
choice here is made and why it matters for a medical-grade study. The numbered
sections after it are the operator how-to.

---

## Methods rationale — why it's built this way

This section is the "why" behind the machinery. Each choice below is a
methods decision a reviewer of a veterinary-informatics paper could reasonably
challenge, so each is justified on its own terms, not just described.

### Why validate the LLM jury at all

The study's headline score comes from an **LLM jury** — AI models rating AI-written
summaries. On its own that is *"what an AI thinks of an AI,"* which no clinical
reader is obliged to trust. Human validation is the bridge from an automated
number to a **defensible** one: we show the jury's scores move in step with a
veterinarian's, and only then can jury scores stand in for expert judgement at
scale. Without this step, every downstream conclusion ("provider X writes safer
summaries") rests on an unvalidated instrument.

### Why the blind protocol extends to the human reviewers

If a reviewer knew *"this summary is from GPT-4,"* expectation bias would
contaminate their score — and since we then compare their score to the jury's,
that bias would quietly manufacture (or destroy) the very agreement we are
trying to measure. Blinding **both** the jury (`evaluator.build_judge_prompt()`)
and the humans (`packet.md` carries no model identity) keeps the two
assessments **independent**, which is the whole premise of using one to
validate the other. Blinded assessment is a bedrock of clinical evidence for
exactly this reason; here it is enforced structurally — the reviewer-facing
files physically cannot contain a model name, and a test asserts it.

### Why each summary is scored independently, never side-by-side

Every sampled item is one article + **one** blind summary — never a
side-by-side comparison of an article's three provider summaries at once,
even blinded. This is a deliberate constraint, not an oversight, for four
reasons:

1. **It has to match the construct being validated.** The LLM jury
   (`evaluator.build_judge_prompt()`) scores exactly one summary at a time,
   with zero visibility into any sibling summary of the same article. If a
   human instead scored three summaries together, the human would be doing a
   fundamentally different task — **comparative/relative rating** — while the
   jury does an **absolute/independent** one. Correlating the two would no
   longer isolate "does the jury track human judgment"; it would conflate
   that with "does an absolute-scoring jury track a comparative-scoring
   human," a different and uninterpretable question.
2. **Contrast and anchoring effects are a well-documented rating bias.**
   Rating something in the presence of similar alternatives systematically
   shifts the score relative to what it would have been in isolation — a
   mediocre summary reads better next to two weak ones and worse next to two
   strong ones. The same effect shows up in LLM-judge research comparing
   pointwise, pairwise, and listwise judging: the scoring mode itself changes
   the scores, independent of the content being judged. Since the jury this
   study validates is strictly pointwise, a comparative human score would
   contaminate the very agreement statistic the study is trying to measure
   honestly — a spurious mismatch (or spurious match) driven by scoring mode,
   not by whether the jury is actually right.
3. **It leaks comparative information even without naming a provider.** The
   reviewer guide's core promise (`docs/booklet/08_human_validation_guide.md`,
   §2) is that a score "reflects only what's on the page in front of you."
   Showing three summaries of the same article together lets a reviewer use
   relative cues ("this one's clearly the weakest of the three") instead of
   judging each summary purely against the source article — a subtler leak
   than a model name, but a leak of the same kind the blind protocol exists
   to close.
4. **The pipeline's data model is 1 item = 1 score set = 1 jury score to
   correlate against.** `_row_key()`, the per-reviewer
   `unblinding_key_human{N}.json` files, `_normalize_scoresheet_row()`, and
   `_human_vs_jury()`'s per-`item_id`
   pairing all assume exactly this shape. A side-by-side design would need a
   reshaped scoresheet, sampler, and correlation model to answer a genuinely
   different research question — **relative preference/ranking** among
   providers (the kind of question pairwise human-preference benchmarks like
   AlpacaEval ask) — not the question this study needs answered, which is
   whether the jury's *independent* score for *one* summary tracks a human's
   *independent* score for that same summary.

### Why sampling can reuse the same article across multiple items

Independent scoring (above) doesn't require independent *articles*. Reading
15 different articles to produce 15 scored items is far more reading burden
on a volunteer reviewer than reading 5 articles and independently scoring
all 3 providers' summaries of each — 15 scored items either way, but only 5
articles actually read. The export always samples this way — one distinct
article per count, every provider's summary of it included — and it is
standard practice in human evaluation of NLG systems: reusing one source
document across several independently-rated system outputs is how most
human-evaluation protocols for summarization and translation work (e.g. WMT-
and SummEval-style studies routinely have one annotator rate multiple
systems' outputs against the same shared source). What those protocols
avoid — and what this project avoids too — is *simultaneous, comparative*
presentation of those outputs, not *reuse* of the source text. The
distinction matters:

- Each of an article's provider summaries is still sampled and scored as its
  **own independent item**, on its own scoresheet row, with no ranking or
  relative judgment requested.
- **Sibling items (same article, different provider) are never adjacent in
  the rendered packet.** `_interleave_article_groups()` shuffles the
  selected articles' order with the run's seeded RNG, then takes one row per
  article per round — so a reviewer always encounters other, unrelated
  articles between two summaries of the same one. This breaks the
  short-term-memory/contrast chain that side-by-side presentation was
  rejected for in the previous section: by the time a reviewer reaches an
  article's second summary, several unrelated items have intervened. (With
  only one article sampled there is nothing to interleave with, so its rows
  are necessarily consecutive — a corner case worth avoiding by sampling at
  least a handful of articles.)
- `packet.md`'s preamble tells the reviewer directly: score every occurrence
  of a repeated article completely independently, as if seeing it for the
  first time, and don't look back at an earlier score for the same article.
- Nothing about the packet/scoresheet rendering, the un-blinding key, or the
  ingest/correlation math cares whether a row came from a brand-new article
  or one repeated across providers — sampling only changes *which rows get
  selected*.

Practically: `--sample-size` is an EXACT number of articles, always split
evenly across the study's 5 journals (`sample_articles_from_journal_quota` in
`llm-sum/human_review.py`) — 5 -> 1/journal, 10 -> 2/journal, 25 ->
5/journal — each expanded to its ~3 provider summaries: 15, 30, or 75 scored
items from only 5, 10, or 25 articles actually read. If a journal's
evaluated-article pool can't supply its exact share, the export raises
`JournalQuotaError` naming every short journal rather than silently sampling
unevenly (§4).

### Why humans score the identical 5-criterion rubric

Validity requires comparing like with like. The reviewer scores the **same
five criteria** the jury scores (faithfulness, completeness, clinical
usefulness, clarity, safety), so a correlation compares the *same construct* —
and can be broken down criterion-by-criterion, not collapsed to one opaque
number where a jury that is right on "clarity" but wrong on "safety" looks fine.

### Why Krippendorff's α for inter-reviewer agreement

Before asking "does the jury match the humans?" we must ask "do the humans even
agree with *each other*?" — because if two vets disagree, no jury could match
"the human," and the yardstick itself is broken. We measure that with
**Krippendorff's α**, chosen over simpler options for three reasons that matter
here:

- **Chance-corrected.** Raw percent-agreement overstates reliability because
  two raters agree *sometimes by luck*; α subtracts the agreement expected by
  chance, so α = 0 means "no better than coin-flips," not "0% overlap."
- **Handles missing / unbalanced data.** Your vet scores only a *subset* of the
  summaries you score. α tolerates raters not covering every item; a simpler
  paired measure would force you to throw away the non-overlapping rows. This
  is also why the export doesn't need every `humanN/` folder to hold the
  *identical* item set: the overlap ratio (§3) carries only a fraction of
  each tester's articles into the next folder, and inter-reviewer agreement
  is simply computed over whatever subset two reviewers happen to share.
- **Interval metric.** The 1–5 scores are ordered numbers, so α treats a 4-vs-5
  near-miss as a *small* disagreement and a 1-vs-5 as a large one — a
  categorical agreement statistic (Cohen's κ) would score both as simply "not
  equal," discarding the ordering.

Krippendorff's alpha is hand-rolled in `reliability.py` because `scipy` does not
implement it. (The Spearman/Pearson correlations below, by contrast, use
`scipy.stats`, which also gives their p-values.)

> **New to Krippendorff's alpha or correlation?** A plain-language primer
> (no stats background needed) explains it from scratch:
> [`docs/statistics_explained.md`](../statistics_explained.md).

### Why Spearman is the headline, with Pearson and Bland-Altman alongside

Three complementary lenses, because no single number answers "does the jury
track the expert?":

- **Spearman (rank correlation) — the headline.** The real question is whether
  the jury *orders* summaries the way the expert does (best to worst). Spearman
  measures monotonic agreement and does not assume the two 1–5 scales are
  linearly identical, which is the right, conservative assumption for ordinal
  human ratings.
- **Pearson (linear correlation) — reported alongside.** Answers the stricter
  "do they move up and down together *linearly*?" Shown for completeness; it is
  more sensitive to a single outlier, so it is not the headline.
- **Bland-Altman bias — the safety net correlation misses.** A jury can be
  *perfectly correlated* with a vet yet **systematically 0.5 points more
  generous on every summary** — correlation is blind to that constant offset.
  Bland-Altman (mean `human − jury`, plus 95% limits of agreement) is the
  standard **method-comparison** tool in medical measurement precisely to expose
  such systematic bias. It matters because a biased-but-correlated jury still
  mis-scores *every* item, which would inflate or deflate the study's absolute
  quality numbers even when the provider *ranking* looks fine.

### Why per-reviewer is the default (not pooling all humans)

When a domain expert (your vet) and a non-expert (you) both review, averaging
them into one "human" produces a score that is *neither* — and the reviewer who
scored **more** items silently dominates it. Since your non-vet sample is much
larger than the vet's, a pooled correlation would essentially measure
*"jury vs the non-vet,"* mislabelled as expert validation. Reporting **each
reviewer separately** keeps the vet's agreement clean, so the sentence
*"the LLM jury tracks veterinarian judgement"* rests on the **vet's** row and
nothing else. This is a validity decision, not a formatting one — hence it is
the default. `pooled` and `both` remain available for when reviewers are of
comparable expertise.

### Why the verdict is withheld below 30 comparable items

Correlation on a handful of items is dominated by luck: a *perfect* r = 1.0 on
3 summaries is as meaningless as calling a coin biased after 3 heads. Printing
"validated ✓" from such a sample would be a **false claim about a clinical
instrument** — the most consequential kind of error this tool could make. So
below `MIN_CORRELATION_N` (= **30**, the conventional small-sample threshold)
the plain-language verdict is *withheld* and replaced with an "underpowered —
sample more" note. Crucially, the coefficient and *n* are **still shown**, so a
statistician loses no information; only the confident sentence is earned, not
assumed. This is the single guard most responsible for the tool staying honest.

### Why both jury modes (weighted and unweighted) are validated

The study reports **two** jury scores: an *unweighted* mean of the five criteria
and a *clinical-risk-weighted* mean where faithfulness and safety count more
(a hallucinated drug dose is worse than a clunky sentence). If we validated only
the unweighted score, the **safety-oriented weighted score the study actually
draws conclusions from would be unvalidated.** So both are correlated against
humans. The **unweighted** overall doubles as the *MedHELM-comparable* anchor —
MedHELM's own `jury_score` is an unweighted pooled mean — giving the benchmark a
recognised external reference point. The human weighted composite is computed
with the jury's **own** formula (`evaluator.calculate_jury_score` +
`MEDHELM_CRITERION_WEIGHTS`, the single source of truth), renormalized over
whichever criteria the reviewer actually filled in — the same rule the
unweighted mean uses — so a partial row still yields a real weighted score
instead of a missing criterion silently deflating the average (see
`_normalize_scoresheet_row` in `llm-sum/human_review.py`).

### Why the output is reproducible and offline

The sample is drawn with a fixed seed (`HUMAN_REVIEW_SEED`) and the normalized
`human_reviews.jsonl` is a **rewritable derived snapshot**, so the entire
validation reproduces from the raw scoresheets — a reproducibility requirement
for publication. The whole loop is **offline** (no API, no network): it only
reads data already produced, so it is safe to re-run and cannot leak the blind
or incur cost.

### Methods paragraph (adapt for a write-up)

> To validate the LLM jury, *N* summaries were sampled (stratified by species,
> study design, clinical topic, journal, and input source; fixed seed) —
> either as *N* independently-drawn (article, provider) pairs, or, to reduce
> reviewer reading burden, as *K* articles with every available provider
> summary of each included (*N* ≈ 3*K* summaries from only *K* articles
> read). Each summary was scored independently by *R* blinded reviewers on
> the same five 1–5 criteria used by the jury; reviewers saw only the source
> article and candidate summary, never the generating model, and — when an
> article's summaries were drawn together — never more than one summary of
> the same article at a time, with sibling items interleaved apart in the
> reading order to prevent order/contrast bias. Inter-reviewer reliability
> was quantified with
> Krippendorff's α (interval metric). Agreement between each reviewer and the
> jury was assessed per reviewer with Spearman's ρ (primary) and Pearson's r,
> and systematic bias with Bland-Altman analysis (mean difference and 95% limits
> of agreement), for both the unweighted (MedHELM-comparable) and clinical-risk-
> weighted composite scores, overall and per criterion. Correlation verdicts
> were reported only where *n* ≥ 30 comparable items; smaller samples are
> reported as underpowered.

---

## 1. Why this exists

A blind LLM jury score is only defensible if it tracks what a human expert
would say. `llm-sum/human_review.py` samples already-judged
`(paper, summariser)` pairs from `data/evaluations.jsonl` and exports them as
plain files a veterinarian can read and score by hand — no code, no API keys,
no access to this repository required on their end.

**The blind protocol applies here too.** Exactly like the LLM judge prompt
(`evaluator.build_judge_prompt()`), the files a reviewer sees carry only an
anonymized `item_id`, the original article text, and one candidate summary —
never which AI system (or person) wrote it. The mapping back to
`(doi, summariser, judge, LLM scores)` lives in a private key file
**per reviewer** (`unblinding_key_human{N}.json`, one sibling per `humanN/`
folder), that is never shared with reviewers.

---

## 2. Prerequisites

You need a populated `data/evaluations.jsonl` first — i.e. you've already run
`evaluate` (mock rows from `PHASE3_MODE=test` work fine for trying the export
mechanics; real validation needs real evaluation rows from a live run you did
manually, per the project's live-API rule).

```powershell
python llm-sum/run_phase3.py evaluate --mode test    # mock rows, $0, for a dry run of export
python llm-sum/run_phase3.py export-human-review --mode test   # -> human1/
```

---

## 3. Running the export

Each run creates the **next** `humanN/` folder and never touches the earlier
ones — the same incremental model the pilot dry-run uses (see
[pilot_human_review.md](pilot_human_review.md)), just scoped to the whole
`data/evaluations.jsonl` corpus instead of the small dev pool:

```powershell
python llm-sum/run_phase3.py export-human-review     # -> human1/
python llm-sum/run_phase3.py export-human-review     # -> human2/  (overlap-carried + new)
python llm-sum/run_phase3.py export-human-review     # -> human3/  (overlap-carried + new)
```

Available through `python llm-sum/run_phase3.py export-human-review`:

| Flag | Overrides | Default |
|---|---|---|
| `--sample-size N` (articles) | `HUMAN_REVIEW_SAMPLE_SIZE` | ask interactively (5/10/25 menu) |
| `--overlap-ratio R` | `HUMAN_REVIEW_OVERLAP_RATIO` | `0.6` |
| `--seed N` | `HUMAN_REVIEW_SEED` | 42 |

`--evaluations PATH` (default `data/evaluations.jsonl`) and `--output-dir PATH`
(default `data/human_review/`) are **not** exposed through `run_phase3.py` —
run `python llm-sum/human_review.py` directly to override those two paths.

**`--sample-size` is an exact article count, always split evenly across the
study's 5 journals** — it must be a multiple of 5 (5 -> 1/journal, 10 ->
2/journal, 25 -> 5/journal), each article expanded to **every provider's
summary of it** — so 5 articles → 15 scored items from 5 articles actually
read (see "Why sampling can reuse the same article across multiple items"
above). When `--sample-size` is omitted (and no `HUMAN_REVIEW_SAMPLE_SIZE` is
set) at a real terminal, the export shows a short menu and **asks you to pick
5, 10, or 25 articles**; a non-interactive run falls back to 5.

```powershell
python llm-sum/run_phase3.py export-human-review --sample-size 10
# How many articles will you evaluate? (only fires if --sample-size is omitted)
#   1) 5 articles -> 15 summaries to read (1 per journal)
#   2) 10 articles -> 30 summaries to read (2 per journal)
#   3) 25 articles -> 75 summaries to read (5 per journal)
# -> 10 articles selected, EXACTLY 2 per journal, every provider's summary of
# each included -> 30 scored items, interleaved so two summaries of one
# article are never adjacent.
```

If a journal's evaluated-article pool can't supply its exact share (e.g. only
1 evaluated Veterinary Surgery article when the quota needs 2), the export
raises a `JournalQuotaError` naming every short journal and stops — it never
silently samples unevenly. Run more `evaluate` first, or choose a smaller
article count.

**`--overlap-ratio` controls comparability between consecutive testers**,
spanning three regimes (identical to the pilot's, see
[pilot_human_review.md](pilot_human_review.md) §3 for the worked example):
`1.0` = identical items every run, `0.0` = a fresh independent draw each run,
`0.6` (default) = partial overlap. The carry is **journal-balanced** — the
same fraction is carried from each journal, so the leftover "new" draw is
always a valid per-journal count too.

All three env knobs live in `.env.template` section 18. Like `EVAL_SAMPLE_SEED`,
keeping `HUMAN_REVIEW_SEED` fixed makes the sample reproducible — re-running
the export (e.g. to regenerate a lost reviewer sheet) selects the identical
items.

You can also run the module directly: `python llm-sum/human_review.py --overlap-ratio 0.6`.

---

## 4. What gets sampled

`sample_articles_from_journal_quota()` / `select_incremental_rows()` in
`llm-sum/human_review.py`:

1. For the first tester (`human1`), the FULL quota is drawn fresh: exactly
   `sample_size / 5` articles from each journal. Within a journal, any row
   already flagged `requires_human_review=True` (low judge confidence, a
   malformed judge response, or any major-severity hallucination claim — see
   `evaluator.needs_human_review()`) is included first; the rest is a seeded
   shuffle-and-take.
2. For every later tester, a proportional fraction of the previous tester's
   articles is carried **from each journal** (see `--overlap-ratio` above),
   and the exact per-journal shortfall left over is drawn fresh from articles
   the previous tester never saw.
3. A multi-judge jury run or a rerun can leave more than one evaluation row
   for the same `(doi, summariser, input_source)`; only the latest is kept.
4. If any journal can't supply its exact share (initial draw or the
   leftover-after-carry draw), `JournalQuotaError` is raised naming every
   short journal — see §3.

The candidate summary and reference text for each sampled row are re-joined
from `data/summaries.jsonl` (the default pipeline) and, as a fallback, the
`summarize-all` `.txt` comparison folders — the same two sources `evaluate`
itself can read from. If a row's source text can't be found (the corpus has
moved or been regenerated since evaluation), it is skipped and the count is
printed as a warning; nothing fails silently.

---

## 5. What gets written

```text
data/human_review/
  unblinding_key_human1.json     NEVER share this file with reviewers
  unblinding_key_human2.json
  human1/
    REVIEWER_GUIDE.md            full zero-jargon guide (copied from
                                  docs/booklet/08_human_validation_guide.md)
    packet.md                    navigation index: one row per item_id -> folder
    scoresheet_human1.xlsx       blank scoresheet (dropdowns, version-labeled rows)
    item_001/
      article.pdf                 the original published article, copied from data/raw
                                   (falls back to article.md — the cached text the AI
                                   read — if no source PDF can be resolved for that DOI)
      summary.md                  one candidate summary to score against it
    item_002/
      ...
  human2/
    REVIEWER_GUIDE.md
    packet.md
    scoresheet_human2.xlsx       (overlap-carried + new items, see §3)
    item_001/ ...
  ...
```

**Each `humanN/` folder is one reviewer, written by one export run** — not a
batch of N identical copies. Comparability between reviewers comes from the
`--overlap-ratio` carry (§3): inter-reviewer agreement (§8) is computed over
whatever items two `humanN` folders happen to share, the same way the LLM
jury only needs items with 2+ judges to compute Krippendorff's alpha (see
[reliability.py](../../llm-sum/reliability.py)) — the raters don't need to
have scored an otherwise-identical batch.

### `REVIEWER_GUIDE.md` — the full guide, shipped with every reviewer folder

A verbatim copy of `docs/booklet/08_human_validation_guide.md`, written into
every `humanN/` folder so a reviewer handed only their own folder (e.g.
zipped and emailed) still gets the complete, zero-jargon guide — what each
scoresheet column means, a worked example row, why the process is blind, and
what to do with a repeated article — with no other file or context required.
`packet.md`'s own preamble is deliberately kept short (a quick-reference
recap for while scoring) and points to this file for the full explanation,
rather than duplicating it.

### `packet.md` — the navigation index

A short blind preamble (scoring scale + "score each article independently")
followed by a table listing every `item_id`, its article title, its version
(`k of n`, when an article appears more than once), and the `item_NNN/` folder to
open. The full article text and summary no longer live inline here — each is in
its own item folder.

### `item_NNN/` — one folder per review item

`article.pdf` holds a copy of the original published article — figures, tables,
and references intact, exactly as it appeared in the journal — copied from
`data/raw` (`write_item_folders()` in `human_review.py`). Reviewers now score
against this real PDF rather than the stripped text the AI summarizer actually
read; see `docs/booklet/08_human_validation_guide.md` §3 for the reviewer-facing
explanation of why that's fair, and what to do when a paper's key finding lives
only in a graph/table the AI never saw. When no source PDF can be resolved for a
DOI (moved, renamed, or never downloaded), the item folder falls back to
`article.md` — the same full cached text from `data/processed/` the AI was given
— so export never produces an item with no article at all; `export-human-review`
prints a warning naming every DOI that fell back. `summary.md` holds the single
candidate summary to score against it. Nothing else in either file — no DOI, no
journal, no model name. The same article recurs across several `item_NNN/`
folders (one per provider's summary), each labeled with a blind-safe *version*
number.

### `scoresheet_humanN.xlsx` — what the reviewer fills in

One blank row per `item_id` (matching the folder names), with dropdown-validated
cells (1–5 on the criteria, yes/no on hallucination) and a frozen header. When an
article contributes more than one summary, its `article_title` cell is suffixed
`— summary version k of n` so the repeated rows are told apart. The columns:

| Column | Scale | Meaning |
|---|---|---|
| `faithfulness` | 1-5 | Are all claims supported by the article? |
| `completeness` | 1-5 | Does it cover the key findings? |
| `clinical_usefulness` | 1-5 | Would this help a vet clinician/researcher? |
| `clarity` | 1-5 | Is it well-organized and easy to read? |
| `safety` | 1-5 | Could a reader be clinically misled by anything stated or omitted? |
| `hallucination_present` | yes/no | Any claim the article doesn't support? |
| `hallucination_notes` | free text | What, if anything |
| `comment` | free text | Anything else worth flagging |

These five scored columns are deliberately the same five criteria the LLM
jury scores (`faithfulness`, `completeness`, `clinical_usefulness`, `clarity`,
`safety` — see `reliability.CRITERIA`), so ingest (§7) can compare human and
jury scores criterion-by-criterion, not just on one composite number.

### `unblinding_key_human{N}.json` — private, never shared

Maps every `item_id` in ONE reviewer's `humanN/` folder back to `doi`,
`summariser`, `judge`, `input_source`, `strata`, and the LLM jury's own
scores for that item (`llm_jury_score`, `llm_jury_score_weighted`,
`llm_jury_score_unweighted`, `llm_criteria_scores`). This is what the ingest
step (§7) joins that reviewer's filled scoresheet against to compute
human-vs-jury agreement — one key file is written per export run, as a
sibling of the `humanN/` folder it describes.

**Why per-reviewer, not one shared file:** every `humanN/` folder numbers its
own items starting at `item_001`, so `human2`'s `item_001` and `human1`'s
`item_001` identify two *different* (doi, summariser) pairs. Ingest always
looks a scoresheet's rows up against its OWN reviewer's key — never a
merged, global `item_id` table — so this can't get crossed.

---

## 6. Distributing packets to real reviewers

1. Run the export.
2. Send each reviewer only their own `humanN/` folder — it is now fully
   self-contained (`REVIEWER_GUIDE.md` + `packet.md` +
   `scoresheet_humanN.xlsx` + the `item_NNN/` folders, each with its own
   `article.pdf`), nothing else needs to be separately attached. Do **not**
   send `unblinding_key_human{N}.json`.
3. Ask them to read `REVIEWER_GUIDE.md` once, then work down the packet index:
   open each `item_NNN/` folder, read `article.pdf` + `summary.md`, and fill
   that `item_id`'s row in the `.xlsx` — 1-5 in each numeric column — then
   return it.
4. Keep the completed `.xlsx` sheets **in their `humanN/` folders** and every
   `unblinding_key_human{N}.json` together under `data/human_review/`, so
   ingest (§7) can find both.

---

## 7. Ingesting filled scoresheets

**Quick answer — what to actually do the moment a scoresheet comes back:**

1. Drop the returned `scoresheet_humanN.xlsx` back into its own `humanN/`
   folder under `data/human_review/` (it should already be there if you
   followed §6) — do **not** rename it or move it out of its folder, and
   keep every reviewer's `unblinding_key_human{N}.json` sitting alongside it.
2. Run `python llm-sum/run_phase3.py ingest-human-review`. This reads
   every filled sheet it can find and writes `data/human_reviews.jsonl` —
   safe to re-run any time a new sheet comes in or a correction is made (see
   "derived snapshot" below).
3. Run `python llm-sum/run_phase3.py eval-report --markdown`. The report now
   gains a **"Human Validation"** section — inter-reviewer agreement and
   human-vs-jury correlation — explained in full in §8 below.
4. When you're ready to fold these numbers into the study's final,
   paper-ready output (not just a quick look), see §9,
   **"What happens after that"** — it covers `report-figures` and
   `eval-report --publication`, the two commands that pick up
   `data/human_reviews.jsonl` automatically and turn it into the leaderboard
   and the Cohen's Kappa tables a manuscript actually cites.

The rest of this section explains exactly how ingest works under the hood,
for anyone who wants the detail; skip ahead to §8/§9 if the quick answer
above is all you need right now.

Once reviewers return their filled scoresheets (dropped back into their
`humanN/` folders under `data/human_review/`):

```powershell
python llm-sum/run_phase3.py ingest-human-review
```

| Flag | Default |
|---|---|
| `--review-dir PATH` | `data/human_review/` |
| `--output PATH` | `data/human_reviews.jsonl` |

`ingest_human_reviews()` in `llm-sum/human_review.py`:

1. Loads every `unblinding_key_human*.json` sibling in the review directory,
   one per reviewer (errors clearly if none are found — ingest cannot
   un-blind without at least one).
2. Reads every `human*/scoresheet_human*.xlsx` (older `.csv` sheets are
   still read, as is any loose `scoresheet_*.{xlsx,csv}` at the top level, for a
   sheet returned without its folder).
3. For each filled row: parses the five 1-5 criteria (blank cells are allowed;
   a value outside 1-5 or non-numeric is **ignored with a warning**, never
   silently trusted), the yes/no `hallucination_present` flag, and the free
   text. A completely empty row (a not-yet-scored item) is dropped silently.
4. Re-joins each `item_id` to **that same reviewer's own** un-blinding key
   (never another reviewer's) to recover `doi`, `summariser`, `judge`,
   `input_source`, `strata`, and the LLM jury's own scores, and writes one
   normalized JSONL row per `(reviewer, item)` to `data/human_reviews.jsonl`.

`data/human_reviews.jsonl` is a **derived snapshot**, not append-only like
`evaluations.jsonl`: re-running ingest rewrites it from the current sheets, so
correcting a scoresheet and re-ingesting simply reproduces a corrected file
(rows are sorted by `(item_id, reviewer_id)` for a stable, diffable artifact).

Each normalized row carries the human scores, the human unweighted mean
(`human_score_unweighted`, the plain average of whatever criteria were filled),
and the item's LLM jury scores side by side — self-contained so the analysis
never re-opens `evaluations.jsonl`.

---

## 8. What the analysis reports

`analyze_human_reviews()` (surfaced automatically by `eval-report` whenever
`data/human_reviews.jsonl` exists) answers the two questions that make the LLM
jury defensible:

- **Do the human reviewers agree with each other?** Inter-reviewer
  Krippendorff's alpha, reusing the *same* engine
  (`reliability.compute_reliability`) the LLM jury uses — each reviewer is
  simply treated as a rater. Reported only with **≥ 2 reviewers**; a lone
  reviewer has no one to agree with, and the section says so.
- **Does the LLM jury track expert judgment?** Human-vs-jury **Spearman**
  (the headline rank correlation) and **Pearson** correlation, plus a
  **Bland-Altman bias** (mean `human − jury`, which catches a jury that is
  reliably more generous/harsh even when perfectly correlated) — available even
  for a single reviewer.

### Per-reviewer vs pooled (`HUMAN_VALIDATION_MODE` / `--human-validation-mode`)

When more than one person reviews — e.g. **you (non-vet)** score many summaries
and **a supervising veterinarian** scores a subset — how the correlation is
computed matters a lot:

- **`per_reviewer`** *(default, recommended)* — a **separate** correlation for
  each reviewer, so the vet is validated on their *own* scores. The claim you
  want to defend ("the jury tracks *veterinarian* judgment") rests on the vet's
  row specifically; averaging a vet with a non-vet would let the reviewer who
  scored the most summaries dominate the number.
- **`pooled`** — one correlation over the mean human score per item (all
  reviewers averaged into a single "human"). Simpler, but on a
  mixed-expertise panel "the human" becomes an average of expert and non-expert.
- **`both`** — report both views.

Reviewers are labelled by their folder (`human1`, `human2`, …); which one is
the vet is up to you (whoever fills that folder's scoresheet). The report
shows each reviewer's block separately in `per_reviewer`/`both`.

Both views report the pooled-overall score for **both jury modes**:
  - *Overall (unweighted)* — the MedHELM-comparable number (MedHELM's own
    `jury_score` is an unweighted pooled mean of judge × criterion scores).
  - *Overall (weighted)* — this project's clinical-risk-weighted score
    (faithfulness/safety count more), so the safety-oriented metric the study
    actually reports is validated too, not left unchecked. The human weighted
    composite is computed with the jury's *own* formula
    (`evaluator.calculate_jury_score` + `MEDHELM_CRITERION_WEIGHTS`),
    renormalized over whichever criteria the reviewer filled in — so a
    partial row still yields a real weighted score, computed over the same
    items as the unweighted mean, rather than being deflated or dropped.

  Per-criterion correlations are kept as a subordinate **diagnostic** block
  (which criteria the jury tracks experts on), not the headline.

```powershell
# after ingest, the report gains a "Human validation" section:
python llm-sum/run_phase3.py eval-report --markdown
```

**Research-grade guard.** A correlation on too few items is statistically
unstable, so the plain-language verdict is *withheld* below
`reliability.MIN_CORRELATION_N` comparable items — set to **30**, the
conventional small-sample threshold. The coefficient and `n` are still shown
for transparency, but the line reads *"Underpowered: only N comparable item(s)
(< 30)… too unstable to treat as validation -- sample more items before
concluding."* A spurious `n=3, r=1.0` is therefore never presented as proof the
jury tracks clinicians. With ≥ 30 the usual bands apply (`Strong correlation
(r >= 0.70): the LLM jury tracks expert judgment well.`).

Practically: because the floor gates each reviewer separately (`per_reviewer`
mode), the **vet's** row needs ≈ 30 vet-scored summaries to earn a verdict. A
vet sample of 15 (e.g. 1 article/journal × 3 providers × 5 journals) will
correctly read "underpowered — sample more"; 30 (2 articles/journal) clears it.
Adjust the threshold in one place (`reliability.MIN_CORRELATION_N`) if your
validation plan dictates otherwise. Correlation itself needs at least two
comparable items; with fewer it reports `-`.

---

## 9. What happens after that — the downstream reporting path

`eval-report --markdown` (§7-8) is the quick look. `data/human_reviews.jsonl`
also feeds two other commands automatically, without any extra flag, and
those are the ones whose output actually goes in a manuscript:

### `report-figures` — folds human validation into the leaderboard

```powershell
python llm-sum/run_phase3.py report-figures
```

Every leaderboard entry gets a **human-validation cell** per provider —
`_human_validation_entry` in `llm-sum/report_figures.py` calls
`human_review.human_vs_jury_by_provider(rows)` (`llm-sum/human_review.py`),
which scopes the same Spearman/Pearson correlation from §8 to *that
provider's* summaries only. If no reviewer has scored that provider's
summaries yet, the leaderboard entry says so in plain English
(`"No human_reviews.jsonl rows for this provider yet. Run
'export-human-review', have reviewers fill the scoresheets, then
'ingest-human-review'."`) instead of silently omitting the field — so an
incomplete validation pass is always visible, never invisible. Output lands
in `data/results/leaderboard_<ts>.json` and `.md` (see
[phase6/reporting.md](../phase6/reporting.md) for the full leaderboard
format and the four figures this command also renders).

### `eval-report --publication` — the paper-ready Cohen's Kappa tables

```powershell
python llm-sum/run_phase3.py eval-report --publication
```

This is the command whose numbers actually go in a write-up. Its
**covariate analysis** table (one row per provider × species / study_design
/ journal cell) reports hallucination rate and mean quality alongside
**Cohen's Kappa** — a chance-corrected agreement statistic between the LLM
judge and the human reviewer on the *same* categorical calls
(`llm-sum/stats_engine.py`, §2 "Cohen's Kappa + percent agreement"). This is
a different, complementary number from §8's Spearman/Pearson: those measure
whether the jury's *continuous* score tracks the human's; Cohen's Kappa
measures categorical agreement (e.g. did both flag the same items as
hallucinating) and is the one typically reported alongside the correlation
in a methods section. Output lands in `data/results/publication_report_<ts>.md`
(plus the JSON and a CSV-per-table folder) — see
[phase6/reporting.md](../phase6/reporting.md) §1 for the complete table list.

**Both commands degrade gracefully** if `data/human_reviews.jsonl` doesn't
exist yet or has no rows for some provider — they still run and produce
every other table, just with that one field marked unavailable rather than
failing. You do not have to wait for every reviewer to finish before
running either command; re-run them any time to pick up newly ingested
scores.

---

## 10. Tests

```powershell
python -m pytest tests/test_human_review.py tests/test_human_review_ingest.py -q
```

`test_human_review.py` mirrors `evaluator.py`'s blind-protocol test (the
rendered packet and scoresheet never contain a summariser/provider name,
`evaluator.BLIND_FORBIDDEN_TOKENS`) plus the stratified sampler and export CLI.
`test_human_review_ingest.py` covers ingest (un-blinding, idempotence,
blank/invalid/unknown-row handling) and the agreement/correlation analysis
(inter-reviewer alpha only with ≥ 2 reviewers; correlation for the solo case).
Both run against mock fixtures — `PHASE3_MODE=test`, `$0`, no network.

---

## 11. Related reading

- [pilot_human_review.md](pilot_human_review.md) — trial this whole workflow on the dev pool first (incremental `humanN/` folders, `.xlsx` scoresheet, copied source PDFs)
- [phase3/README.md](../phase3/README.md) — Phase 3 doc map and pipeline overview
- [phase3/medhelm_evaluation.md](../phase3/medhelm_evaluation.md) — the jury rubric being validated
- [phase4/README.md](../phase4/README.md) — taxonomy, scenarios, run manifests
