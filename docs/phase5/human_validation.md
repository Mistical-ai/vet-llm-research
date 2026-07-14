# Phase 5 — Human Validation (blind export → ingest → agreement)

**Role of this document:** operator how-to for the full human-validation loop —
exporting blind review packets, ingesting the filled scoresheets, and reporting
how well the LLM jury tracks expert judgment. The authoritative score for this
study remains Phase 3's blind LLM jury (`llm-sum/evaluator.py` →
`data/evaluations.jsonl`) — human validation is a check against that score, not
a replacement for it. See
[medhelm_evaluation.md](../phase3/medhelm_evaluation.md) for the jury rubric
this validates.

The workflow is three commands:

```text
export-human-review   →   (reviewers fill CSVs)   →   ingest-human-review   →   eval-report
   data/human_review/                                 data/human_reviews.jsonl    Human Validation section
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
   reviewer guide's core promise (`docs/booklet/07_human_validation_guide.md`,
   §2) is that a score "reflects only what's on the page in front of you."
   Showing three summaries of the same article together lets a reviewer use
   relative cues ("this one's clearly the weakest of the three") instead of
   judging each summary purely against the source article — a subtler leak
   than a model name, but a leak of the same kind the blind protocol exists
   to close.
4. **The pipeline's data model is 1 item = 1 score set = 1 jury score to
   correlate against.** `_row_key()`, `unblinding_key.json`,
   `_normalize_scoresheet_row()`, and `_human_vs_jury()`'s per-`item_id`
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
articles actually read. `sample_rows_for_review(..., sample_unit="articles")`
supports exactly this, and it is standard practice in human evaluation of
NLG systems: reusing one source document across several independently-rated
system outputs is how most human-evaluation protocols for summarization and
translation work (e.g. WMT- and SummEval-style studies routinely have one
annotator rate multiple systems' outputs against the same shared source).
What those protocols avoid — and what this project avoids too — is
*simultaneous, comparative* presentation of those outputs, not *reuse* of
the source text. The distinction matters:

- Each of an article's provider summaries is still sampled and scored as its
  **own independent item**, on its own scoresheet row, with no ranking or
  relative judgment requested — exactly the same item shape as `"items"`
  mode, just selected differently.
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
- Nothing about the sampler's flagged-first-then-stratified selection logic,
  the packet/scoresheet rendering, the un-blinding key, or the ingest/
  correlation math changes — `sample_unit="articles"` only changes *which
  rows get selected*, grouped and then re-flattened back into the same row
  list shape `"items"` mode already produces.

Practically: `HUMAN_REVIEW_SAMPLE_UNIT=articles` with
`HUMAN_REVIEW_SAMPLE_SIZE` set to the number of journals (5, in this
project's corpus) spreads the sampled articles one per journal (the existing
journal-stratified selection already does this), each expanded to its ~3
provider summaries — about 15 scored items from 5 articles actually read,
instead of 15 articles for 15 items under `"items"` mode.

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
  paired measure would force you to throw away the non-overlapping rows.
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
`MEDHELM_CRITERION_WEIGHTS`, the single source of truth), and only when a
reviewer filled **all five** criteria — because a missing criterion would be
clamped to 1 and silently deflate the weighted average.

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
`(doi, summariser, judge, LLM scores)` lives in one file, `unblinding_key.json`,
that is never shared with reviewers.

---

## 2. Prerequisites

You need a populated `data/evaluations.jsonl` first — i.e. you've already run
`evaluate` (mock rows from `PHASE3_MODE=test` work fine for trying the export
mechanics; real validation needs real evaluation rows from a live run you did
manually, per the project's live-API rule).

```powershell
python llm-sum/run_phase3.py evaluate --mode test    # mock rows, $0, for a dry run of export
python llm-sum/run_phase3.py export-human-review --reviewers 1 --mode test
```

---

## 3. Running the export

```powershell
python llm-sum/run_phase3.py export-human-review --reviewers 3 --sample-size 15
```

| Flag | Overrides | Default |
|---|---|---|
| `--reviewers N` | `HUMAN_REVIEWERS` | 1 |
| `--sample-size N` | `HUMAN_REVIEW_SAMPLE_SIZE` | 15 |
| `--sample-unit {items,articles}` | `HUMAN_REVIEW_SAMPLE_UNIT` | `items` |
| `--seed N` | `HUMAN_REVIEW_SEED` | 42 |
| `--evaluations PATH` | — | `data/evaluations.jsonl` |
| `--output-dir PATH` | — | `data/human_review/` |

`--sample-unit articles` samples distinct articles instead of individual
`(article, provider)` items, expanding each to every provider summary found
for it — see "Why sampling can reuse the same article across multiple
items" above. For example:

```powershell
python llm-sum/run_phase3.py export-human-review --sample-unit articles --sample-size 5
# 5 articles selected (stratified across journals), every provider's summary
# of each included -- typically ~15 scored items, 5 articles actually read.
```

All three env knobs live in `.env.template` section 19. Like `EVAL_SAMPLE_SEED`,
keeping `HUMAN_REVIEW_SEED` fixed makes the sample reproducible — re-running
the export (e.g. to regenerate a lost reviewer sheet) selects the identical
items.

You can also run the module directly: `python llm-sum/human_review.py --reviewers 3`.

---

## 4. What gets sampled

`sample_rows_for_review()` in `llm-sum/human_review.py`:

1. **Every row flagged `requires_human_review=True`** is included first (low
   judge confidence, a malformed judge response, or any major-severity
   hallucination claim — see `evaluator.needs_human_review()`).
2. **Remaining slots** are filled with a stratified sample across the same
   strata `eval_report.py` groups by — species, study design, clinical topic,
   journal, input source (`eval_instances.STRATIFICATION_FIELDS`) — so the
   sample doesn't skew toward whichever journal has the most rows.
3. A multi-judge jury run or a rerun can leave more than one evaluation row
   for the same `(doi, summariser, input_source)`; only the latest is kept.

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
  unblinding_key.json          NEVER share this file with reviewers
  reviewer_1/
    REVIEWER_GUIDE.md           full zero-jargon guide (copied from
                                 docs/booklet/07_human_validation_guide.md)
    packet.md                  blind reading packet: item_id + article + summary
    scoresheet_reviewer_1.csv  blank scoresheet for reviewer 1 to fill in
  reviewer_2/
    REVIEWER_GUIDE.md           identical content to reviewer_1's copy
    packet.md                  identical content to reviewer_1's packet
    scoresheet_reviewer_2.csv
  ...
```

**Every reviewer sees the same sampled items**, exported as independent blind
copies. This is intentional: the ingest step (§7) needs the same items scored
by more than one person to measure inter-reviewer agreement — the same reason
the LLM jury needs more than one judge scoring the same item to compute
Krippendorff's alpha (see [reliability.py](../../llm-sum/reliability.py)).

### `REVIEWER_GUIDE.md` — the full guide, shipped with every reviewer folder

A verbatim copy of `docs/booklet/07_human_validation_guide.md`, written into
every `reviewer_N/` folder so a reviewer handed only their own folder (e.g.
zipped and emailed) still gets the complete, zero-jargon guide — what each
scoresheet column means, a worked example row, why the process is blind, and
what to do with a repeated article — with no other file or context required.
`packet.md`'s own preamble is deliberately kept short (a quick-reference
recap for while scoring) and points to this file for the full explanation,
rather than duplicating it.

### `packet.md` — what the reviewer reads

One section per sampled item: `## item_XXX`, then the full original article
text, then the candidate summary. Nothing else — no DOI, no journal, no model
name.

### `scoresheet_reviewer_N.csv` — what the reviewer fills in

One blank row per `item_id`, with these columns:

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

### `unblinding_key.json` — private, never shared

Maps every `item_id` back to `doi`, `summariser`, `judge`, `input_source`,
`strata`, and the LLM jury's own scores for that item
(`llm_jury_score`, `llm_jury_score_weighted`, `llm_jury_score_unweighted`,
`llm_criteria_scores`). This is what the ingest step (§7) joins filled
scoresheets against to compute human-vs-jury agreement — it is written once
per export and is not regenerated per reviewer.

---

## 6. Distributing packets to real reviewers

1. Run the export.
2. Send each reviewer only their own `reviewer_N/` folder — it is now fully
   self-contained (`REVIEWER_GUIDE.md` + `packet.md` +
   `scoresheet_reviewer_N.csv`), nothing else needs to be separately
   attached. Do **not** send `unblinding_key.json`.
3. Ask them to read `REVIEWER_GUIDE.md` once, then fill in the CSV — one row
   per `item_id`, 1-5 in each numeric column — and return it.
4. Keep the completed CSVs **in their `reviewer_N/` folders** and
   `unblinding_key.json` together, so ingest (§7) can find both.

---

## 7. Ingesting filled scoresheets

Once the reviewers return their CSVs (dropped back into their `reviewer_N/`
folders under `data/human_review/`):

```powershell
python llm-sum/run_phase3.py ingest-human-review
```

| Flag | Default |
|---|---|
| `--review-dir PATH` | `data/human_review/` |
| `--output PATH` | `data/human_reviews.jsonl` |

`ingest_human_reviews()` in `llm-sum/human_review.py`:

1. Loads the private `unblinding_key.json` from the review directory (errors
   clearly if it is missing — ingest cannot un-blind without it).
2. Reads every `reviewer_*/scoresheet_reviewer_*.csv` (plus any loose
   `scoresheet_*.csv` at the top level, for a sheet returned without its folder).
3. For each filled row: parses the five 1-5 criteria (blank cells are allowed;
   a value outside 1-5 or non-numeric is **ignored with a warning**, never
   silently trusted), the yes/no `hallucination_present` flag, and the free
   text. A completely empty row (a not-yet-scored item) is dropped silently.
4. Re-joins each `item_id` to the un-blinding key to recover `doi`,
   `summariser`, `judge`, `input_source`, `strata`, and the LLM jury's own
   scores, and writes one normalized JSONL row per `(reviewer, item)` to
   `data/human_reviews.jsonl`.

`data/human_reviews.jsonl` is a **derived snapshot**, not append-only like
`evaluations.jsonl`: re-running ingest rewrites it from the current CSVs, so
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

Reviewers are labelled by their folder (`reviewer_1`, `reviewer_2`, …); which
one is the vet is up to you (whoever fills that folder's scoresheet). The
report shows each reviewer's block separately in `per_reviewer`/`both`.

Both views report the pooled-overall score for **both jury modes**:
  - *Overall (unweighted)* — the MedHELM-comparable number (MedHELM's own
    `jury_score` is an unweighted pooled mean of judge × criterion scores).
  - *Overall (weighted)* — this project's clinical-risk-weighted score
    (faithfulness/safety count more), so the safety-oriented metric the study
    actually reports is validated too, not left unchecked. The human weighted
    composite is computed with the jury's *own* formula
    (`evaluator.calculate_jury_score` + `MEDHELM_CRITERION_WEIGHTS`), and only
    when a reviewer filled all five criteria (a partial row would deflate it).

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

## 9. Tests

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

## 10. Related reading

- [phase3/README.md](../phase3/README.md) — Phase 3 doc map and pipeline overview
- [phase3/medhelm_evaluation.md](../phase3/medhelm_evaluation.md) — the jury rubric being validated
- [phase4/README.md](../phase4/README.md) — taxonomy, scenarios, run manifests
