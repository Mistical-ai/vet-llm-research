# Chapter 6 — How Data Provenance Works

**Who this is for:** a technical beginner who has read (or skimmed) Chapters
1-5 and now has a pipeline that's produced summaries, judge scores, and maybe
some human reviews. This chapter answers a different question than Chapter 5
did: not "what does this number mean," but **"where did this number actually
come from, and how do I prove it?"** You don't need to have read the earlier
chapters — every term is defined here from scratch — but the worked examples
reuse the same paper Chapter 2 introduced, so reading that chapter first will
feel familiar.

**What you'll be able to do after reading this:** look at any row in
`data/summaries.jsonl`, `data/evaluations.jsonl`, or `data/human_reviews.jsonl`
and know exactly which other files to open, in what order, and which field to
match on, to trace it all the way back to the original PDF — and be able to
tell the difference between what this project *can* prove about a piece of
data and what it honestly can't.

---

## Where we are

By this point in the booklet, one paper has traveled through the whole
pipeline: it started as a row in `data/manifest.jsonl` (Chapter 1), became a
PDF and then a cleaned JSONL row (Chapter 2), was summarized by three AI
providers (Chapter 3), had those summaries scored by a blind AI judge
(Chapter 4), and you now know how to read those scores in `eval-report`
(Chapter 5). Along the way, at every single stage, the pipeline quietly wrote
down *how* it got there — which paper, which code, which prompt, which model,
which run. That trail is what this chapter is about.

---

## 1. What "provenance" means, and why this project bothers

**Provenance** just means: a record of where something came from and what
produced it. It's the same idea as a chain of custody for evidence, or a
receipt for a purchase — not the *thing* itself, but proof of how it was
made. A grocery receipt doesn't just say "$40 was spent"; it says which
items, at what price, at which store, on what date. Provenance in this
project plays the same role for a summary or a score: it doesn't just say
"this summary scored 4.0," it lets you answer *which paper, which AI
provider, which model version, and which run* produced that 4.0.

Why a research pipeline needs this, concretely:

- **Reproducibility.** A published paper needs to be able to say exactly
  which code, prompts, and models produced its numbers — "trust me" isn't
  good enough for a methods section.
- **Debugging disagreements.** If this month's evaluation numbers don't match
  last month's, provenance turns "why don't these match?" from a guessing
  game into a lookup: diff two records and see exactly what changed.
- **Catching mix-ups.** With three AI providers, three input sources, and
  potentially dozens of re-runs, it's easy to accidentally compare the wrong
  two things. A shared, consistent identifier at every stage is what
  prevents that.
- **Resuming safely and cheaply.** Summarizing and judging cost real money
  (Chapter 3). The pipeline uses provenance fields to know "have I already
  paid for this exact thing?" before spending again — covered in Section 5.
- **Protecting the blind-judging study design.** Chapter 4 introduced **blind
  judging** — the judge never sees which provider wrote a summary. Someone
  still has to know the real answer eventually (to compute "did the AI
  judges agree with human reviewers?"), and provenance is exactly the
  mechanism that keeps that secret answer key separate from everything a
  judge or a human reviewer ever sees. Section 3 (human review) and Section
  6 cover this in detail.

None of this requires new tools — it's built from things you've already
seen: JSONL files (Chapter 2), a small set of shared identifiers, and a
couple of dedicated provenance files. The rest of this chapter walks through
exactly how it fits together.

---

## 2. The DOI: the one key that ties everything together

A **DOI** (Digital Object Identifier — a permanent, unique ID assigned to a
published paper, like `10.2460/javma.22.12.0596`) is the single thread that
runs through every file this pipeline writes: `manifest.jsonl`, the
`raw_text`/`processed` JSONL, `summaries.jsonl`, `evaluations.jsonl`, and
`human_reviews.jsonl` all carry a `"doi"` field holding the exact same
string for the same paper. If you only remember one fact from this chapter,
it's this: **whenever you're trying to trace something, look for the `doi`
field first.**

### From DOI to filename: slugs

A DOI like `10.2460/javma.22.12.0596` can't be used directly as a filename —
it contains `/` and `.`, which mean something different to a filesystem.
So the code converts every DOI into a **slug**: a filesystem- and API-safe
version of it. The conversion (`doi_to_slug` in `src/file_paths.py`) is
simple — replace every `/`, `:`, and `.` with `_`:

```text
"10.2460/javma.22.12.0596"  →  "10_2460_javma_22_12_0596"
```

This exact slug is what you saw as the `"slug"` field in Chapter 2's worked
JSONL example. It's also used to build the `custom_id` values that batch-API
requests use to match responses back to the right paper. One function
(`doi_to_slug`) is the *only* place this conversion happens, so every stage
of the pipeline agrees on the same slug for the same paper — nobody
accidentally invents a slightly different filename convention downstream.

### Descriptive filenames: not just the slug

If you look inside `data/raw/` or `data/processed/`, filenames aren't bare
slugs — they're more readable, like:

```text
javma__age_at_gonadectomy_sex_and_breed_size_affect_risk...__10_2460_javma_22_12_0596.pdf
```

This is built by `descriptive_stem()` (also in `src/file_paths.py`) as
`{journal}__{title}__{doi-suffix}`, so a human skimming a folder can tell
papers apart without opening each one — but the DOI is still embedded at the
end, and the code always extracts it back out by taking the last `__`-
separated segment of the filename. A PDF and its cleaned-text cache always
share the same stem, so `data/raw/<stem>.pdf` and
`data/processed/<stem>.jsonl` stay lined up automatically. (There's also an
older, plainer `<slug>.pdf` filename style from before descriptive names were
introduced — the code checks for the descriptive name first and falls back
to the plain one, so both eras of filenames still resolve correctly.)

### When a bare DOI isn't specific enough: composite keys

A single paper doesn't produce just one summary or one score — it produces
one per AI **provider** (OpenAI, Anthropic, Gemini — see Chapter 3), and
sometimes one per **input source** (`processed`, `raw_text`, or `pdf` — also
Chapter 3) on top of that. So most of the time, the *real* join key isn't
just the DOI on its own — it's a small combination of fields:

| Where | The actual join key |
|---|---|
| A summary in `data/summaries.jsonl` | `(doi, input_source)` — one row per input source, with a slot per provider inside it |
| An evaluation row in `data/evaluations.jsonl` | `(doi, input_source, summarizer, judge, rubric_version)` |
| An item in a human reviewer's folder | `item_id` (e.g. `item_003`) — but only meaningful *within that one reviewer's own folder*, see Section 3 |

Keep this table in mind for the rest of the chapter — "just match the DOI"
is the right first instinct, but for evaluations and summaries you usually
need the fuller combination to land on exactly the right row.

---

## 3. Following one paper's trail, stage by stage

Let's continue the exact paper Chapter 2 used as its worked example — the
JAVMA paper on gonadectomy timing and canine overweight risk, DOI
`10.2460/javma.22.12.0596` — and see what provenance gets attached to it at
every stage.

### Stage 1 — `data/manifest.jsonl` (Chapter 1)

This is where the paper's journey starts. Each row is built from CrossRef
metadata plus a few keyword-guessed fields (**covariates** — not written by
an AI, just simple keyword matching over the title/abstract):

```json
{
  "doi": "10.2460/javma.22.12.0596",
  "title": "Age at gonadectomy, sex, and breed size affect risk of canine overweight...",
  "year": 2023,
  "pub_year": 2023,
  "authors": ["Smith A", "Jones B"],
  "abstract": "...",
  "journal": "JAVMA",
  "issn": "0003-1488",
  "species": ["Canine"],
  "study_design": "Retrospective cohort",
  "clinical_topic": "Obesity",
  "needs_manual_review": false
}
```

`manifest.jsonl` is **append-only** — running the collection step again adds
new candidate papers, it never rewrites or deletes existing rows. Notice
what's *not* here: no PDF filename, no download source URL. Those are
resolved on demand later by matching the DOI, not stored permanently on the
manifest row itself.

### Stage 2 — `data/raw_text/` and `data/processed/` (Chapter 2)

Both folders hold the **exact same schema** — the only real differences are
the `text` field's content and the `input_source` label. Chapter 2's worked
example showed the `processed` version; here's what provenance fields it
carries:

```json
{
  "doi": "10.2460/javma.22.12.0596",
  "slug": "10_2460_javma_22_12_0596",
  "text": "...",
  "word_count": 6426,
  "char_count": 105325,
  "pdf_filename": "javma__age_at_gonadectomy...__10_2460_javma_22_12_0596.pdf",
  "pdf_source": "data/raw/javma__age_at_gonadectomy...pdf",
  "input_source": "processed",
  "extracted_at": "2026-06-01T18:12:25.220464+00:00"
}
```

`pdf_filename` and `pdf_source` point straight back to the source PDF;
`extracted_at` is a UTC timestamp of when extraction ran. This is honestly
*most* of what this stage records — there is deliberately no
extraction-tool-version field and no content hash on these rows (more on
what that means in Section 7). Re-running extraction is cheap to skip
safely anyway: the code just compares file modification times — if the
cached JSONL is newer than the source PDF, it's left alone rather than
redone.

### Stage 3 — `data/summaries.jsonl` (Chapter 3)

One row per `(doi, input_source)`, with a `models` dictionary holding one
slot per provider. Continuing the example, here's an abbreviated view of
the OpenAI slot (this is also the exact summary Chapter 4 used for its jury
score worked example, so its scores will look familiar in the next stage):

```json
{
  "doi": "10.2460/javma.22.12.0596",
  "input_source": "processed",
  "journal": "JAVMA",
  "species": ["Canine"],
  "study_design": "Retrospective cohort",
  "clinical_topic": "Obesity",
  "title": "Age at gonadectomy, sex, and breed size affect risk of canine overweight...",
  "models": {
    "openai": {
      "summary": "...",
      "structured_summary": { "...": "the full VeterinarySummary fields" },
      "status": "success",
      "input_tokens": 5211,
      "output_tokens": 612,
      "model_version": "gpt-5.4-0325-preview",
      "timestamp": "2026-06-02T09:14:03.881221+00:00"
    },
    "anthropic": { "...": "..." },
    "gemini": { "...": "..." }
  }
}
```

Two fields worth calling out:

- **`model_version`** is recorded *exactly* as the provider's API returned
  it (e.g. `"gpt-5.4-0325-preview"`, never simplified to `"gpt-5"`). Providers
  occasionally update a model without changing its public name, so logging
  the literal string returned at the time is how you'd ever detect that a
  score shift was caused by a silent model update rather than a real
  difference in the paper.
- **Manifest metadata is copied in at write time** — `journal`, `species`,
  `study_design`, `clinical_topic`, and `title` are duplicated from
  `manifest.jsonl` right onto the summary row, so later stages (like
  `eval-report`'s grouping in Chapter 5) don't need to re-open the manifest
  just to know a paper's species or journal.

An important gap, honestly stated: **this row does not record which prompt
file or prompt version was used, or what temperature/seed generated it.**
Unlike the evaluation side (next), the summarization side does not hash or
version-stamp its prompt onto the output. If you ever need to know exactly
which prompt template produced an archived summary, the only way to
recover that is from the git commit history and the `.env` configuration
active at the time the run happened — it isn't stored on the row itself.

### Stage 4 — `data/evaluations.jsonl` (Chapter 4)

Chapter 4, Section 8 already walked through what a finished evaluation row
looks like field-by-field, so this section won't repeat that table. From a
provenance standpoint, the two fields that matter most for *tracing* are:

- **`judge_model_version`** — the same "exactly as returned" convention as
  `model_version` above, but for whichever model did the judging.
- **`rubric_version`** — which version of the MedHELM-style scoring rubric
  was used. This one is deliberately part of how the pipeline decides
  whether something has "already been judged" (Section 5) — a new rubric
  version re-judges everything rather than silently reusing old scores
  under a new methodology's name.

Using Chapter 4's own worked example, the OpenAI summary above would carry
`jury_score: 4.0`, `jury_score_weighted: 4.02`, and the five underlying
`criteria_scores` — each traceable back through `(doi, input_source,
summarizer="openai")` to the exact summary row shown above.

### Stage 5 — Run manifests: the pipeline's own paper trail

Chapter 4, Section 7 introduced **run manifests** as evaluation's "receipt."
Here's the complete picture, including exactly where to find one and what's
in it.

**Careful with the name** — a *run manifest* is a completely different thing
from `data/manifest.jsonl` (the paper catalogue from Section "Stage 1"
above). They happen to share the English word "manifest," but one is the
list of candidate papers, and the other is a per-evaluation-run receipt.
This chapter uses "run manifest" specifically to keep them apart.

Before any judge is called, `evaluate` writes a file to:

```text
data/run_manifests/run_manifest_<run_id>.json
```

where `<run_id>` looks like `run_20260602T091403Z` — a UTC timestamp
baked right into the filename, so you can match a manifest to a run just by
wall-clock time. The file's status starts as `"started"`. After the run
finishes — whether it succeeds, partially fails, or crashes outright — the
same file is patched with a terminal status (`"completed"` or `"failed"`)
and a `finished_utc` timestamp, in code that runs even when the evaluation
run raises an exception. That's deliberate: a manifest that only got written
on success would be useless for debugging the runs that *didn't* succeed.

A run manifest's full field list:

| Field | What it captures |
|---|---|
| `run_id`, `timestamp_utc`, `finished_utc`, `status` | Identity and lifecycle of this one run. |
| `git_commit_sha`, `branch`, `code_version` | Exactly which version of the code ran (`code_version` is just `branch@sha[:12]`, e.g. `main@a1b2c3d4e5f6`, since this research pipeline has no formal version number — git identity is the honest stand-in). |
| `dataset_path`, `dataset_hash_sha256` | Which file was evaluated, and a SHA-256 fingerprint of its exact contents at that moment (see the box below on hashing). |
| `selected_instance_ids` | The sorted list of every DOI actually judged in this run. |
| `judges`, `model_ids`, `resolved_model_versions` | Which judge(s) were configured, and the exact model version string each one turned out to use. |
| `prompt_template_id`, `prompt_path`, `prompt_sha256` | Which judge-prompt file was used, and a hash of its exact wording. |
| `temperature`, `max_output_tokens`, `seed`, `top_p` | The generation settings in force for this run. |
| `evaluation_config` | Extra run-specific settings (e.g. rubric version, which mode/slice was evaluated). |
| `model_tier` | Which `MODEL_TIER` (`regular` or `premium`, `.env.template` section 16) resolved the judge models for this run. Defaults to `"unknown"` for manifests written before this field existed. |
| `judge_prompt_shape` | The judge-prompt layout used, e.g. `"segmented_v1"` (rubric/reference/candidate sent as separate message blocks — see [04_llm_judge.md](04_llm_judge.md)). `None`/absent for manifests written before the segmented shape landed. |

> **What "SHA-256 hash" means, in plain language:** a hash is a short
> fingerprint computed from a file's exact contents. Change even one
> character anywhere in the file, and the fingerprint comes out completely
> different. Two files with the *same* hash are provably identical — this
> is how a run manifest proves "the judge prompt's wording didn't change
> between these two runs" without having to store (and manually re-compare)
> the entire prompt file every time.

**Why this matters in practice:** if two evaluation runs produce different
numbers for what should be the same comparison, you don't have to guess why
— open both runs' manifests and diff them. A different `prompt_sha256`
means the rubric wording changed. A different `dataset_hash_sha256` means
the input summaries changed. A different `git_commit_sha` means the code
changed. Whichever field differs is your answer.

**A real limitation, stated honestly:** an evaluation row in
`evaluations.jsonl` does **not** carry the `run_id` of the run manifest that
produced it. There's no direct pointer from a score back to its exact
manifest. In practice you join them by *proximity* instead — find the
manifest whose `timestamp_utc`/`finished_utc` window contains the
evaluation row's own `timestamp`, and whose `selected_instance_ids`,
`judges`, and `rubric_version` are consistent with the row you're looking
at. It's a small amount of extra legwork, but it's always resolvable because
run manifests are timestamped and evaluation rows are too.

### Stage 6 — Human review: the unblinding key

Chapter 4 explained *why* judging (and human review) is blind — the judge,
and the reviewer, never see which provider wrote a summary. Someone still
has to remember the real answer, or nobody could ever check "did the human
agree with the AI jury?" That's the job of one small, carefully-guarded
file: the **unblinding key**.

When `export-human-review` builds a `humanN/` folder for a reviewer, it also
writes a sibling file — **not inside** the folder the reviewer receives —
called `unblinding_key_human{N}.json`. "Sibling" matters: it sits next to
`humanN/` in `data/human_review/`, never nested inside it, so there's no way
to accidentally zip it up and hand it to a reviewer along with their
packet. It looks like this:

```json
{
  "generated_at": "2026-06-10T12:00:00+00:00",
  "seed": 42,
  "requested_sample_size": 10,
  "actual_sample_size": 10,
  "reviewer_count": 1,
  "items": {
    "item_001": {
      "doi": "10.2460/javma.22.12.0596",
      "summarizer": "openai",
      "judge": "openai",
      "rubric_version": "vet_medhelm_score_v1.0",
      "input_source": "processed",
      "strata": { "species": ["Canine"], "study_design": "Retrospective cohort", "...": "..." },
      "requires_human_review": false,
      "llm_jury_score": 4.0,
      "llm_jury_score_weighted": 4.02,
      "llm_criteria_scores": { "...": "the same five scores from Stage 4" }
    }
  }
}
```

Everything a reviewer actually sees — `packet.md`, the `item_NNN/` folders,
the `.xlsx` scoresheet — carries only the `item_id`, the article, and the
candidate summary. It **never** carries `summarizer`. The unblinding key is
the *only* file in the whole export that maps an `item_id` back to which AI
wrote it — which is exactly why it copies the AI jury's own scores
(`llm_jury_score` and friends) onto each item too: when it's time to compute
"did the human agree with the AI jury," the ingest step can read both sides
of that comparison from one file, without ever having to re-open
`evaluations.jsonl` and re-derive the match itself.

When the filled-in scoresheet comes back, `ingest-human-review` looks up
each `item_id` in **that same reviewer's own key** and writes a fully
un-blinded row to `data/human_reviews.jsonl`:

```json
{
  "item_id": "item_001",
  "reviewer_id": "human1",
  "doi": "10.2460/javma.22.12.0596",
  "summarizer": "openai",
  "judge": "openai",
  "input_source": "processed",
  "rubric_version": "vet_medhelm_score_v1.0",
  "strata": { "...": "..." },
  "criteria_scores": { "...": "the reviewer's own 1-5 scores" },
  "human_score_unweighted": 3.8,
  "human_score_weighted": 3.83,
  "hallucination_present": false,
  "llm_jury_score": 4.0,
  "llm_jury_score_weighted": 4.02,
  "llm_criteria_scores": { "...": "..." }
}
```

One deliberate design choice worth understanding: unblinding keys from
different reviewers are **never merged into one shared lookup table**.
`human2`'s `item_001` is a completely different `(doi, summarizer)` pair
from `human1`'s `item_001` — the item numbering restarts fresh for every
reviewer. Looking up an `item_id` in the wrong reviewer's key would silently
attach the wrong paper's identity to a score. So ingest always keeps each
reviewer's key strictly separate, and `data/human_reviews.jsonl` itself is
re-written from scratch (not appended to) every time you run ingest, so it
never accumulates a stale, half-merged version of an earlier run.

---

## 4. Reproducibility seeds — five dials that make random choices repeatable

Several steps in this pipeline make an apparently-random choice — which
five papers to spot-check, which articles to sample for human review, how
to resample data for a bootstrap confidence interval (Chapter 7 explains
what that means). "Random" would normally mean "different every time," which
is bad for reproducibility — so every one of these choices is actually
**pseudo-random**, driven by a numeric **seed**: the same seed number always
produces the exact same sequence of "random" choices. Change the seed, and
you get a different (but equally valid) sample.

| Seed (`.env` variable) | Default | What it controls |
|---|---|---|
| `EVAL_SAMPLE_SEED` | 42 | Which papers get journal-stratified-sampled for `single`/`dev`-mode evaluation |
| `PHASE3_DEV_SAMPLE_SEED` | 42 | Which papers `summarize --mode dev` journal-randomly picks |
| `HUMAN_REVIEW_SEED` | 42 | The stratified sample of already-judged papers pulled into a real `humanN/` export |
| `PILOT_HUMAN_REVIEW_SEED` | 42 | The same, for the practice/dry-run pilot export |
| `PUBLICATION_BOOTSTRAP_SEED` | 42 | The resampling seed behind every bootstrap confidence interval in the publication tables/figures |

Every one of these defaults to `42` — not for any statistical reason, just
convention (a common placeholder in this style of code). The human-review
seed is itself provenance: it's saved as the `"seed"` field right inside the
unblinding key you saw above, so a reviewer's exact sample can be regenerated
later if needed. The evaluation seed, temperature, and model settings live
in the run manifest, as Section 3 showed.

---

## 5. How the pipeline uses provenance to avoid doing (or paying for) the same work twice

Summarizing and judging cost real money and API quota (Chapter 3). Every
resume/skip mechanism in this pipeline works by checking a specific
provenance field — never by guessing:

- **Extraction (Chapter 2)** skips a PDF if its cached JSONL file is
  *newer* than the source PDF (a filesystem modification-time check, not a
  content check).
- **Summarization (Chapter 3)** skips a provider slot if that exact
  `(doi, input_source)` row already has `status: "success"` for that
  provider. Nothing else is checked — if it's marked successful, it's
  trusted.
- **Evaluation (Chapter 4)** skips a `(doi, summarizer)` pair only if
  `evaluations.jsonl` already has a row matching **all five** of: `doi`,
  `input_source`, `summarizer`, `judge`, and `rubric_version`. That last one
  is deliberate — publish a new rubric version, and every paper gets
  re-judged under it rather than silently keeping old scores mislabeled as
  belonging to the new methodology.
- **The dev-mode incremental workflow** (`docs/phase3/dev_evaluation_guide.md`)
  checks for the plain existence of a per-paper file in
  `data/dev_evals_jsonl/` — if a DOI's result file is already there, it's
  considered done and skipped on the next run.
- **Human-review exports** never touch an earlier reviewer's folder: the
  code just looks for the highest existing `humanN` folder number and
  always creates the next one — `human1`, then `human2`, and so on.

The common thread: every "have I already done this?" check reads a field
that was written down as provenance in an earlier stage — none of it is
inferred or guessed.

---

## 6. Two worked examples: tracing a result all the way back

### Scenario A — "This evaluation score for the gonadectomy paper's OpenAI summary looks off. Where do I look?"

1. **Open `data/evaluations.jsonl`.** Find the row where `doi ==
   "10.2460/javma.22.12.0596"`, `summarizer == "openai"`. Read `jury_score`,
   `criteria_scores` (each with the judge's own stated reasoning),
   `parse_method` (if this says `"regex"` or `"sentinel"` instead of
   `"json"`, the judge's reply didn't parse cleanly — that alone can explain
   a strange score), and `timestamp`.
2. **Find the matching run manifest.** In `data/run_manifests/`, find the
   file whose `timestamp_utc`/`finished_utc` window contains the evaluation
   row's `timestamp`, and whose `selected_instance_ids` includes this DOI.
   Check `prompt_sha256`, `git_commit_sha`, and `resolved_model_versions` to
   confirm exactly what code/prompt/model produced the score.
3. **Open `data/summaries.jsonl`.** Find the row where `doi` matches and
   `input_source` matches the evaluation row's `input_source`. Inside
   `models.openai`, read the exact `summary`/`structured_summary` text the
   judge actually graded, and its own `model_version` and `timestamp`.
4. **Open the matching `data/processed/<stem>.jsonl` row** (the reference
   text the judge compared the summary against). Confirm `pdf_source` and
   `extracted_at`.
5. **Compare against `data/raw_text/<stem>.jsonl`** (same stem) if you
   suspect the cleaning step itself introduced a problem — Chapter 2,
   Section 3 covers exactly this comparison.
6. **Open the original `data/raw/<stem>.pdf`** to check against the real,
   published document.
7. **Confirm against `data/manifest.jsonl`** that `journal`, `species`,
   `study_design`, and `clinical_topic` match the `strata` recorded on the
   evaluation row — this closes the loop all the way back to the original
   CrossRef record.

The one field that stays identical through all seven steps is `doi`;
`input_source` tells you which of the three input channels you're following
at steps 1, 3, and 4; the descriptive filename stem carries you through
steps 4-6.

### Scenario B — "A human reviewer's score in `data/human_reviews.jsonl` looks off. Where do I look?"

1. **Open `data/human_reviews.jsonl`.** Find the row by `(item_id,
   reviewer_id)` — it already carries the un-blinded `doi`, `summarizer`,
   `judge`, the reviewer's own `criteria_scores`, and the AI jury's matching
   `llm_jury_score`/`llm_criteria_scores` side by side.
2. **Open `data/human_review/unblinding_key_human{N}.json`** (matching the
   row's `reviewer_id` number). Look up `items[item_id]` to double-check the
   identity mapping, and to see the export's own `seed` and `generated_at`
   provenance.
3. **Open that reviewer's own filled `scoresheet_human{N}.xlsx`** and find
   the row for this `item_id` — useful for confirming exactly what the
   reviewer originally typed, in case something didn't parse the way you'd
   expect during ingest.
4. **Open `data/human_review/human{N}/item_{NNN}/article.pdf` and
   `summary.md`** — the exact article and candidate summary the reviewer
   actually read (reviewers read the real published PDF, not the cleaned
   text the AI saw — Chapter 8, Section 3 explains why).
5. **Cross into `data/evaluations.jsonl` and `data/summaries.jsonl`** using
   the now-known `(doi, summarizer, input_source)`, exactly as in Scenario A
   steps 1 and 3, to compare the human's score against the AI jury's on the
   identical item.

The join chain here is: `item_id` (meaningful only within one reviewer's own
folder) → the unblinding key → `(doi, summarizer, input_source)` → the same
universal keys used everywhere else in the pipeline.

---

## 7. What this system can't (yet) prove — honest limits

Provenance in this project is thorough, but it isn't total. Knowing the
gaps is part of using it correctly:

- **No prompt hash on `summaries.jsonl` rows.** As noted in Section 3, a
  summary row doesn't record which prompt file, prompt version, temperature,
  or seed produced it — only the *evaluation* side hashes its prompt into
  the run manifest. If you need to know exactly which prompt template made
  an archived summary, you're relying on git history and the `.env` state
  at the time, not a field on the row itself.
- **No content hash of the article text at any stage.** A run manifest
  hashes the *whole* `summaries.jsonl` dataset file as one unit — but no
  individual `raw_text`/`processed` row, and no individual summary, carries
  its own content hash. Text-level trust rests on deterministic extraction
  and the shared DOI/slug, not on cryptographic proof that a specific piece
  of text is untouched.
- **No back-pointer from an evaluation row to its run manifest.** As
  Section 3 explained, you join them by timestamp proximity and matching
  `selected_instance_ids`/`judges`/`rubric_version` — there's no single
  `run_id` field sitting on the evaluation row itself.

None of these are bugs so much as scope decisions — the parts of the
pipeline that are expensive to get wrong (which rubric scored something,
which code version ran, whether the judge prompt's wording changed) are
hashed and versioned tightly. The parts that are cheap to re-derive (which
exact prompt string is behind a given summary) lean on git and the DOI/slug
system instead. Knowing which category a question falls into tells you
whether to expect a clean lookup or a bit of detective work.

---

## Recap

- **Provenance** means being able to prove where a piece of data came from
  and what produced it — not just trusting the number on its face.
- **The DOI is the universal join key.** Every file in the pipeline carries
  the same `doi` string for the same paper; a **slug** (DOI with `/`, `:`,
  `.` replaced by `_`) makes it filesystem- and API-safe, and a
  **descriptive stem** (`journal__title__doi-suffix`) makes filenames
  human-readable while still embedding that slug.
- **Most joins need more than the bare DOI** — summaries key on
  `(doi, input_source)`; evaluations key on `(doi, input_source, summarizer,
  judge, rubric_version)`; human-review items are keyed by `item_id`, but
  only within one reviewer's own folder.
- **A run manifest** (`data/run_manifests/run_manifest_<run_id>.json`) is a
  complete receipt for one evaluation run — code version, dataset hash,
  prompt hash, judges, model versions, and settings — written before
  judging starts and finalized even if the run crashes.
- **The unblinding key** (`unblinding_key_human{N}.json`, a sibling of each
  `humanN/` folder, never inside it) is the one file that remembers which
  AI wrote each summary a reviewer scored — kept strictly separate from
  what reviewers ever see, and never merged across reviewers.
- **Resume logic always checks a specific provenance field** — never
  guesses — whether that's a file's modification time, a `status`
  string, or a five-field match including `rubric_version`.
- **The system has real, named limits**: no prompt hash on summaries, no
  per-text content hash, and no direct evaluation-row-to-run-manifest
  pointer. Knowing these means knowing when a lookup will be clean and when
  it'll take a bit of extra digging through git history instead.

Next, **Chapter 7** teaches every statistical formula this project uses —
p-values, Wilcoxon, Friedman, Krippendorff's alpha, Cohen's Kappa, and more —
from scratch, with worked examples.
