# Booklet Style Guide & Front Matter

**Role of this file:** this is the one page every chapter-writing session
reads *first*. It exists so that nine chapters, written independently across
separate chat sessions, add up to one coherent booklet instead of nine
documents stapled together. If you are about to write a chapter, read this
whole file before touching source material.

> **`BOOKLET.md` is a hand-merged copy, not a generated one.** There is no
> build script that concatenates the numbered chapter files into
> `BOOKLET.md` — the merge was done by hand and must be maintained by hand.
> Any edit to a chapter file must be mirrored into the corresponding section
> of `BOOKLET.md` **in the same commit**, or the two silently drift and
> readers of the merged copy get stale instructions. When in doubt, grep the
> exact sentence you changed in `BOOKLET.md` before you commit.

---

## 1. How to use this booklet

This booklet explains the veterinary-LLM research pipeline in this repository
end to end — where the papers come from, how they become machine-readable
text, how three AI systems summarize them, how a blind AI judge scores those
summaries, how to read the results, how every one of those records can be
traced back to its source, the statistics behind every number, and how a
human reviewer checks the AI judge's work.

It is written for **two different audiences**, and every chapter should be
clear about which one it's speaking to:

- **Chapters 1-7** are for a **technical beginner** — someone comfortable
  running a command in a terminal and reading a config file, but who has
  never seen this specific pipeline before and has no assumed statistics
  background. Explain what things are, not just what to type.
- **Chapter 8** is for a **non-technical veterinarian reviewer** — a domain
  expert with zero coding background who has volunteered to score some
  AI-written summaries. It must never use code, file paths, JSONL, API, or
  pipeline jargon. It should work as a standalone printable handout.

A chapter should never assume the reader has just finished the previous
chapter in the same sitting — each chapter is a fresh context window for a
fresh Claude, and each *reader* may also jump in at any chapter. Define terms
on first use every time, even if a term was already defined in an earlier
chapter.

---

## 2. Terminology — one canonical term per concept

Use the **left-hand term** everywhere in every chapter. The right-hand
column exists only so you recognize the term if you see it in older repo
docs or code comments — don't introduce it as a competing term in booklet
prose unless you are explicitly explaining the legacy history.

| Canonical term (use this) | Not this (legacy / internal name) | Notes |
|---|---|---|
| **jury score** | "quality score" | `quality_score` is a derived legacy 1-10 field kept for old notebooks. When you must reference it, call it "the legacy 1-10 quality score" explicitly. |
| **processed text** | "cleaned text" | The reference-stripped, boilerplate-removed article body in `data/processed/`. |
| **raw text** | — | The uncleaned, column-aware PDF extraction in `data/raw_text/`, kept for provenance/comparison. |
| **provider** | "model", "LLM", "vendor" (when referring to OpenAI/Anthropic/Gemini collectively) | Use "provider" for the three summarizer companies. Use "judge" or "judge model" for the model scoring summaries — a provider can play either role. |
| **jury-score formula** | "the weighted formula", "the score" | Always say whether you mean `jury_score_unweighted` or `jury_score_weighted` — never just "the jury score" when precision matters, since both are always computed and stored. |
| **blind judging** | "unbiased scoring" | Specifically means the judge never sees which provider wrote a summary — not a vaguer claim about fairness in general. |
| **hallucination** | "error", "mistake" | A specific term: a claim in a summary not supported by the article text. Always define it this way on first use. |
| **corpus** | "dataset", "collection" | The set of ~250 target papers this project builds and studies. |
| **manifest** | — | `data/manifest.jsonl` — the catalogue of candidate/acquired papers, one JSON object per line. |
| **run manifest** | — | `data/run_manifests/run_manifest_<run_id>.json` — a per-evaluation-run provenance receipt (code version, dataset hash, prompt hash, judges, settings). Shares the English word "manifest" with `data/manifest.jsonl` but is a completely different file — always disambiguate with "run" when both could be meant. Covered in chapter 6. |
| **provenance** | "audit trail", "lineage" | The ability to prove where a piece of data came from and what produced it — which paper, code version, prompt, and model. Chapter 6's subject end to end. |
| **slug** | — | A DOI with every `/`, `:`, and `.` replaced by `_`, making it filesystem- and API-safe (`doi_to_slug` in `src/file_paths.py`). Distinct from the human-readable "descriptive stem" filenames, which embed the slug as a suffix. |
| **unblinding key** | "answer key" | `unblinding_key_human{N}.json` — the one file mapping a human reviewer's `item_id` values back to the real `(doi, summarizer)` pair. A sibling of, never inside, that reviewer's `humanN/` folder. |
| **PHASE3_MODE** | "safety mode", "dry run flag" | The one env var controlling test/single/dev/batch safety behavior for Phase 3 commands. |
| **ROUGE** | — | A secondary, word-overlap *recall* metric (ROUGE-1/2/L) for auditing, not the primary quality signal. Always spell out that the blind jury score remains primary. Introduced in chapter 7. |
| **cost-per-quality-point** | — | Cost to generate one summary ÷ its mean jury score (lower = better value). Reported in two flavors — real research-budget cost and a $20/month consumer-subscription cost — kept in separate columns; never conflate them. |

If you introduce a new term not in this table, add a row for it when you tick
off your chapter's checklist entry, so later chapters stay consistent.

---

## 3. Tone rules

1. **No unexplained jargon.** The first time a chapter uses a technical term
   (JSONL, DOI, CrossRef, hallucination, jury score, bootstrap, etc.), give a
   one-sentence plain-language definition — even if an earlier chapter
   already defined it. Readers may open any chapter first.
2. **Show, then explain why.** Prefer: what the command/concept is → a
   concrete worked example → why the project is built this way. Don't
   front-load abstract rationale before the reader has something concrete to
   anchor it to.
3. **Reuse real numbers from the source docs.** When a worked example already
   exists in the reference documentation (e.g. the jury-score worked example
   in `docs/phase3/medhelm_evaluation.md`), reuse the exact same numbers
   rather than inventing new ones. Consistent numbers across every doc in the
   project matter for a real study.
4. **Don't tell the reader to run live/paid commands.** Per this project's
   `CLAUDE.md`, any script that makes real API calls must only be run
   manually by the user in their own PowerShell window. Booklet chapters may
   *describe* what a command does and show *example* output, but must not
   instruct the reader to execute a paid command themselves without that
   caveat, and must never be run by an AI assistant automatically.
5. **Chapter 8 is a dialect shift, not just a simpler chapter.** No jargon at
   all, warm and respectful tone, assume no other chapter was read.

---

## 4. Master table of contents

| # | File | Chapter | One-line description |
|---|---|---|---|
| 0 | `00_style_guide.md` | Front matter | This page — terminology, tone, TOC, status checklist. |
| 1 | `01_gathering_data.md` | Gathering the Data | How the 250-paper corpus is collected and downloaded (Phase 2). |
| 2 | `02_jsonl_conversion.md` | From PDF to JSONL | What JSONL is, why it's used everywhere, and how PDFs become clean text (Phase 3 extract). |
| 3 | `03_summarization.md` | Getting AI Summaries | How three LLM providers summarize each paper under identical conditions (Phase 3 summarize). |
| 4 | `04_llm_judge.md` | The Blind AI Judge | How a blind judge scores summaries and the jury-score formula (Phase 3 evaluate). |
| 5 | `05_reading_results.md` | Reading Your Results | What `eval-report` and the publication tables actually show. |
| 6 | `06_data_provenance.md` | How Data Provenance Works | How every summary, score, and human review traces back to an exact paper, code version, prompt, and model — and how to use that trail to audit or reproduce a result. |
| 7 | `07_statistics_explained.md` | The Statistics Behind the Numbers | Every formula (Wilcoxon, Friedman, bootstrap, Krippendorff's alpha, Cohen's Kappa, TF-IDF/cosine), taught from scratch. May be split into `07a_significance_testing.md` / `07b_reliability_and_information.md`. |
| 8 | `08_human_validation_guide.md` | Human Validation Guide | Zero-jargon standalone handout for a veterinarian reviewer. |
| 9 | `BOOKLET.md` | Final assembly | All chapters merged, consistency-checked, one voice. |

---

## 5. Chunk status checklist

Tick a box only after the chapter file exists, is complete, and its
terminology matches section 2 above. Update this checklist as part of
finishing any chapter.

- [x] 0 — Style guide (`00_style_guide.md`)
- [x] 1 — Gathering the Data (`01_gathering_data.md`)
- [x] 2 — From PDF to JSONL (`02_jsonl_conversion.md`)
- [x] 3 — Getting AI Summaries (`03_summarization.md`)
- [x] 4 — The Blind AI Judge (`04_llm_judge.md`)
- [x] 5 — Reading Your Results (`05_reading_results.md`)
- [x] 6 — How Data Provenance Works (`06_data_provenance.md` — the DOI as join key, manifest/raw_text/processed/summaries/evaluations/human_reviews lineage, run manifests, seeds, and worked audit-trail walkthroughs)
- [x] 7 — The Statistics Behind the Numbers (`07_statistics_explained.md` — kept as a **single file**, not split; covers Wilcoxon, Friedman, Bonferroni/Holm/Benjamini-Hochberg, bootstrap CIs, Krippendorff's alpha, Cohen's Kappa, Pearson/Spearman/Bland-Altman, TF-IDF/cosine, ROUGE, and cost-per-quality-point, each with a worked example)
- [x] 8 — Human Validation Guide (`08_human_validation_guide.md`)
- [x] 9 — Final assembly (`BOOKLET.md`)
