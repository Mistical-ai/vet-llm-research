# How to read a `dev_detailEval_reports` file (a cell-by-cell guide)

**Who this is for:** anyone opening a file in `data/dev_detailEval_reports/`
for the first time and wondering what every heading, table, and number means.
No coding or statistics background assumed — every term is defined in plain
language the first time it appears, and again in the glossary at the end.

**What these files are:** after you run `evaluate --mode dev` (see
[dev_evaluation_guide.md](dev_evaluation_guide.md)), the pipeline writes one
Markdown (`.md`) file per article into `data/dev_detailEval_reports/`. Each
file is the **full transcript** of how the AI judges scored that one
article's three AI-written summaries — every judge's own opinion, every
criterion score, every quote it flagged as unsupported, and how much the
judges agreed with each other. It is the deepest view the pipeline offers
into a single paper's evaluation; if you only want a quick pass/fail glance
across many papers, use `data/dev_evals_jsonl/*.txt` or
`eval-report --markdown` instead (both linked at the end of this guide).

Nothing in this file is edited by hand and nothing you do to it changes any
score — it is a **read-only report**, regenerated every time you re-run
`evaluate --mode dev`. It is safe to open, close, scroll through, and ignore;
you cannot break anything by looking at it.

---

## 1. How to open it so it actually looks like this guide

These files are written in **Markdown** — a plain-text format where `#`
means "big heading," `|...|...|` means "table," and `**bold**` means bold.
If you open the raw file in Notepad or a plain text view, you will see all
those symbols instead of the formatted headings and tables — readable, but
much harder to skim. Opening it in **preview mode** renders it the way it's
meant to be read.

**In VS Code (recommended — you're already using it):**

1. Open the file: `File > Open File...` and browse to
   `data/dev_detailEval_reports/<some-file>.md`, or click it in the Explorer
   sidebar.
2. Turn on the rendered preview one of these ways:
   - Press **Ctrl+Shift+V** — opens a full-tab rendered preview.
   - Click the little **preview icon** (a page with a magnifying glass) in
     the top-right corner of the editor tab.
   - Right-click the file in the Explorer sidebar → **Open Preview**.
3. For **side-by-side** (raw text on the left, rendered on the right, so you
   can see both at once): press **Ctrl+K** then **V** (press Ctrl+K, release,
   then press V).

Tables and bold text only render in preview mode — the raw editor view shows
the literal `|` and `**` characters, which is expected and not a problem.

**Other ways to view it**, if VS Code isn't handy:

- **GitHub**: if this repo (or your fork) is pushed to GitHub, browsing to
  the file there renders it automatically — no setup needed.
- **Any Markdown viewer app** (e.g. Typora, Obsidian, MacDown): open the
  `data/dev_detailEval_reports/` folder as a workspace/vault.

---

## 2. The reading order this guide follows

Each file has the same shape every time, top to bottom:

1. **Header block** — which article this is (title, DOI, journal, and a few
   descriptive tags), plus a one-paragraph reminder of the scoring scale.
2. **One section per AI summarizer** (`anthropic`, `gemini`, `openai`) —
   repeated three times, once per AI that wrote a summary of this article.
   Each section has three parts, in order:
   - **Automatic metrics** — cheap, mechanical text-overlap numbers computed
     without any AI judge.
   - **Cross-judge agreement** — how much the (up to three) AI judges agreed
     with each other about *this* summarizer's summary of *this* article.
   - **One block per judge** — that judge's own five criterion scores, its
     reasoning for each, its overall score, and any unsupported claims it
     flagged.
3. **Footer note** — pointers to where the corpus-wide (all-papers) versions
   of these statistics live.

We'll walk through each part using a real example below.

---

## 3. The header block

```markdown
# Evaluation of right ventricular diastolic function, systolic function, and
circulating galectin-3 concentrations in dogs with pulmonary stenosis

**DOI:** [10.1111/jvim.16890](https://doi.org/10.1111/jvim.16890)
**Journal:** JVIM
**Species / Topic / Study design:** Canine · Prospective Observational · Cardiology
```

| Line | What it means |
|---|---|
| `# <title>` | The article's real title, so you can identify the paper without decoding a DOI. If a title couldn't be found, this reads `Untitled -- <doi>`. |
| `**DOI:**` | The article's [Digital Object Identifier](https://www.doi.org/) — a permanent ID for the paper. It's a clickable link to the publisher's page for the article. |
| `**Journal:**` | Which journal this article was published in (e.g. `JVIM` = *Journal of Veterinary Internal Medicine*). |
| `**Species / Topic / Study design:**` | Three quick tags: which animal species the study is about, its clinical subject area, and its study design (e.g. *Retrospective Case Series*, *Prospective Observational*). These are the same "strata" (subgroup labels) used to break down results across the whole corpus — see [medhelm_evaluation.md](medhelm_evaluation.md). |

Right after the header is a callout box (starts with `>`) that repeats the
scoring scale as a quick reminder — you can skip it once you've read this
guide once.

---

## 4. The per-summarizer section

The file has one `## <provider>` heading for each AI that summarized this
article — normally `anthropic`, `gemini`, and `openai` (these are the three
**summarizer** AIs being evaluated, not to be confused with the **judge**
AIs scoring them — the same three providers play both roles, at different
times, on different articles, which is normal).

### 4a. Automatic metrics table

```markdown
| Metric | Value |
|---|---|
| Compression ratio | 0.0705 |
| Extractive coverage | 0.9142 |
| Section coverage | 4 of 6 (0.6667) |
| ROUGE-1 / ROUGE-2 / ROUGE-L | 0.5361 / 0.2813 / 0.3722 |
```

These four numbers are computed by plain code (`llm-sum/eval_metrics.py`) —
**no AI judge is involved**, so they're fast, free, and 100% reproducible.
They're *secondary* evidence: useful sanity checks, but they cannot tell you
whether a summary is clinically safe or accurate — only the judges' scores
below can do that.

| Metric | Range | What it measures | How to read it |
|---|---|---|---|
| **Compression ratio** | 0 and up (usually well under 1) | Summary length ÷ article length, counted in words. | `0.0705` means the summary is about 7% as long as the source article — a very short summary. Lower = more compressed. There's no "correct" value; it's context for judging whether a summary might be too terse (very low) or barely a summary at all (close to 1). |
| **Extractive coverage** | 0.0 – 1.0 | What fraction of the summary's words also appear literally in the article text. | `0.9142` means 91% of the summary's words are words the article itself used. High = the summary sticks close to the article's own wording ("extractive"). Low = the summary paraphrases heavily ("abstractive"). **This is not a hallucination detector** — a summary can reuse the article's exact words and still misrepresent what they mean, or use different words and still be perfectly accurate. |
| **Section coverage** | `X of 6` (0.0 – 1.0) | Whether the summary's wording touches on each of 6 standard parts of a research summary: objective, methods, species/sample size, results, clinical significance, limitations. Detected by keyword matching, not by the AI judge. | `4 of 6 (0.6667)` means the automated keyword check found wording suggesting 4 of the 6 expected sections were touched on. This is a rough heuristic (it looks for phrasing patterns, not true understanding) — treat it as a hint, not a verdict. The judges' own **Completeness** score (below) is the real assessment of this. |
| **ROUGE-1 / ROUGE-2 / ROUGE-L** | 0.0 – 1.0 each | Standard NLP text-overlap scores comparing the summary against the source text (or the article's own abstract, when available). ROUGE-1 = single-word overlap, ROUGE-2 = two-word-phrase overlap, ROUGE-L = longest matching word sequence. | Higher = more word/phrase overlap with the source. These are the same family of metrics used across most text-summarization research, included here mainly so this study's automatic-metric numbers are comparable to other work — they are **not** used to compute any score a provider is judged on. |

### 4b. Cross-judge agreement table

```markdown
| | Mean | Spread | Judges |
|---|---|---|---|
| Jury score (unweighted) | 4.87 | 0.4 | 3 |
| Jury score (weighted) | 4.84 | 0.48 | 3 |
| Factual Accuracy | 4.67 | 1 | 3 |
| Completeness | 5.0 | 0 | 3 |
| Practical Usefulness | 5.0 | 0 | 3 |
| Clarity | 5.0 | 0 | 3 |
| Safety | 4.67 | 1 | 3 |
```

This table only appears when more than one judge scored this summary (the
default setup uses a 3-judge panel). It answers: **for this one summary, how
much did the AI judges agree with each other?**

| Column | Meaning |
|---|---|
| **Mean** | The average of that row's score across all judges who scored it. E.g. `4.87` for "Jury score (unweighted)" means the three judges' overall scores averaged to 4.87 out of 5. |
| **Spread** | The **highest judge score minus the lowest** for that row. `0` means every judge gave the exact same score — perfect agreement. A bigger number means the judges disagreed more. E.g. `Factual Accuracy` spread of `1` means the most generous judge scored it 1 point higher (on the 1–5 scale) than the least generous judge. |
| **Judges** | How many judges' scores went into that row's Mean/Spread (normally 3; fewer if a judge's response failed to parse — see `parse_method` in [medhelm_evaluation.md](medhelm_evaluation.md)). |

The first two rows are the **overall** jury score in its two flavors:

- **Jury score (unweighted)** — a flat average of the five criteria below,
  every criterion counted equally. This is the MedHELM-comparable number.
- **Jury score (weighted)** — the same five criteria, but Factual Accuracy
  and Safety count for more (this project's own clinical-risk weighting,
  since a wrong fact or a misleading safety claim is worse than a clunky
  sentence). See ["How The Score Is Calculated"](medhelm_evaluation.md#how-the-score-is-calculated)
  for the exact formula and a worked example.

The remaining five rows break the *unweighted* agreement down **criterion by
criterion**, so you can see not just "the judges agreed overall" but *which*
criterion they argued about most. In practice, Safety and Practical
Usefulness tend to be where judges disagree most, since those require more
subjective clinical judgment than, say, Completeness.

> This table is scoped to **this one paper's one summary**. For the
> corpus-wide version of the same agreement statistics (computed properly,
> correcting for chance agreement, across every paper) see the **Reliability**
> section of `eval-report` — [medhelm_evaluation.md § Reliability
> Checks](medhelm_evaluation.md#reliability-checks).

### 4c. One block per judge — `### Judge: <provider>`

This is the heart of the file: each judge's own, individually-written
assessment. There is one of these blocks per judge that scored this summary
(so normally three per summarizer section — e.g. one where `anthropic`
judged `anthropic`'s summary, one where `gemini` judged it, one where
`openai` judged it).

**Note on labels:** the same three AI providers act as both summarizers
(writing the summaries, the `## anthropic` / `## gemini` / `## openai`
headings) and judges (scoring them, the `### Judge: anthropic` etc.
headings). A judge's identity is **not hidden** in this file, but during the
actual scoring, the judge never saw *which provider* wrote the summary it
was reading (see ["Why Blind Judging
Matters"](medhelm_evaluation.md#why-blind-judging-matters)) — the model name
in `### Judge: anthropic` is only attached afterward, for your reference.

**Criterion table:**

```markdown
| Criterion | Score | Reasoning |
|---|---|---|
| Factual Accuracy | 5/5 | Every claim in the summary is directly supported by... |
| Completeness | 5/5 | All six elements are present: objective/research question... |
| Practical Usefulness | 5/5 | Species (dogs) and clinical context (pulmonary stenosis)... |
| Clarity | 5/5 | The summary reads as clear, well-organized prose with a logical Background-to-Conclusions flow... |
| Safety | 5/5 | No species generalization, no misleading clinical interpretation... |
```

The five rubric criteria, each scored **1 (poor) to 5 (excellent)** by this
judge, with the judge's own written reasoning for that score. Full scoring
guides for each criterion (what earns a 5 vs. a 3 vs. a 1) are in
[medhelm_evaluation.md § The Rubric](medhelm_evaluation.md#the-rubric); the
short version:

| Criterion | Plain-language question it answers |
|---|---|
| **Factual Accuracy** | Is every claim in the summary actually backed up by the article? (Called `faithfulness` internally.) |
| **Completeness** | Does the summary cover the standard parts of a research summary — objective, methods, species/sample size, results, clinical significance, limitations? |
| **Practical Usefulness** | Would a veterinary clinician find this summary actually useful in practice? (Called `clinical_usefulness` internally.) |
| **Clarity** | Is it well-organized, readable, and free of confusing repetition? |
| **Safety** | Could this summary mislead a clinician's interpretation — wrong species, exaggerated certainty, missing caveats? |

**Overall score line:**

```markdown
**Overall score:** weighted 5.0 / unweighted 5.0 / confidence 5/5
```

This one judge's own overall verdict for this summary: its weighted score,
its unweighted score (both computed the same way as the corpus-wide jury
score, just from this one judge's five criterion scores instead of an
average across judges), and its **confidence** — how sure the judge itself
felt about its own assessment, on a 1–5 scale. A confidence below 3 is one of
the triggers that automatically flags a row for human review (see the
glossary entry for `requires_human_review`).

If this row was flagged, you'll also see a line reading **"Flagged for human
review."** directly below this.

**Hallucination claims:**

```markdown
**Hallucination claims:**
- (minor) claimed "RV diastolic function variables and Gal-3 may be valuable
  additions to the echocardiographic assessment of dogs with PS." -- article
  only supports: "Conclusions and Clinical Importance: Dogs with PS have RV
  diastolic and systolic dysfunction..."
```

or, when the judge found nothing wrong:

```markdown
**Hallucination claims:** None
```

A **hallucination**, in this project's terminology, is any factual claim in
the AI-written summary that the article doesn't actually support — it might
be invented outright, subtly overstated, or contradicted by what the article
really says. Each one this judge flagged is listed as one bullet:

- **`(minor)` or `(major)`** — the judge's severity rating. **Major** means
  the claim could plausibly change a clinician's interpretation and is
  serious enough to automatically flag the row for human review. **Minor**
  means it's an overreach or imprecision unlikely to mislead anyone.
- **`claimed "..."`** — the exact wording from the AI summary that the judge
  is calling out.
- **`article only supports: "..."`** — the judge's quoted evidence: what the
  article actually says on that point, so you can compare the claim against
  the source yourself instead of taking the judge's word for it.

Underneath the criterion table (or the hallucination list, if present) is
one italic line — the judge's own one-sentence overall summary of its
verdict, e.g. *"The candidate summary accurately and completely represents
the reference article..."*.

---

## 5. Two ways to read a file, depending on what you need

- **Quick skim** (does this summary look OK?): read the header, then just
  the three **Overall score** lines (one per judge) and check whether any
  hallucination claims are listed as `(major)`.
- **Deep audit** (is the judge's own reasoning sound?): read every criterion
  row's Reasoning column, and for every hallucination claim, click the DOI
  link and check the `article only supports: "..."` quote against the real
  article yourself. This is exactly what a human reviewer does in the
  [human validation workflow](../phase5/human_validation.md) — these files
  are a convenient way to rehearse that same close-reading exercise
  yourself before (or alongside) recruiting a real veterinarian reviewer.

---

## 6. Full glossary

| Term | Plain-language meaning |
|---|---|
| **Summarizer** | The AI that wrote the candidate summary being judged (`anthropic`, `gemini`, or `openai`). |
| **Judge** | A separate AI call that reads the article + one candidate summary and scores it. The same three providers act as judges too, on a rotating basis, but never judge with knowledge of who wrote the summary. |
| **Jury** | All the judges that scored one summary, collectively (usually 3). "Jury score" = this jury's combined verdict. |
| **Criterion** | One of the five things being scored: Factual Accuracy, Completeness, Practical Usefulness, Clarity, Safety. |
| **Weighted / unweighted** | Two ways of averaging the five criterion scores into one number. Unweighted treats all five equally; weighted counts Factual Accuracy and Safety more heavily. Both are always computed and shown — see [medhelm_evaluation.md](medhelm_evaluation.md#weighted-vs-unweighted). |
| **Spread** | Highest score minus lowest score, among the judges who scored one item. `0` = perfect agreement. |
| **Confidence** | The judge's own 1–5 rating of how sure it is about its assessment (not a rating of the summary itself). |
| **Hallucination** | A claim in the AI summary not actually supported by the article. |
| **Severity (minor/major)** | How dangerous a given hallucination is. Major → automatically flagged for human review. |
| **Requires human review** | This item was automatically flagged because the judge's confidence was low, its response couldn't be parsed correctly, or it found a major hallucination — see [medhelm_evaluation.md § Reliability Checks](medhelm_evaluation.md#reliability-checks). |
| **Compression ratio** | Summary length ÷ article length (word count). Lower = shorter summary relative to the source. |
| **Extractive coverage** | Fraction of the summary's words that literally appear in the article. Not a hallucination check — a proxy for "does the summary reuse the article's own wording, or paraphrase heavily." |
| **Section coverage** | A rough keyword-based check for whether the summary touches on 6 standard research-summary parts (objective, methods, species/sample size, results, clinical significance, limitations). |
| **ROUGE-1 / ROUGE-2 / ROUGE-L** | Standard word/phrase-overlap text-similarity scores between the summary and the source. Higher = more overlap. Reported for comparability with other summarization research; not used to score providers against each other. |
| **DOI** | Digital Object Identifier — the article's permanent, unique ID and a link to its official publisher page. |
| **Strata** (species / topic / study design) | Subgroup labels attached to each article, used to break results down by category across the whole corpus (e.g. "how did providers do on feline oncology papers specifically"). |

---

## 7. Related reading

- [dev_evaluation_guide.md](dev_evaluation_guide.md) — the step-by-step
  operator guide for running `summarize --mode dev` / `evaluate --mode dev`,
  which is what produces these files (and their sibling folders
  `dev_summaries_jsonl/` and `dev_evals_jsonl/`).
- [medhelm_evaluation.md](medhelm_evaluation.md) — the authoritative rubric
  reference: full scoring guides for each criterion, the exact jury-score
  formula worked through by hand, the hallucination taxonomy, and the
  corpus-wide reliability statistics these per-paper numbers roll up into.
- [../phase5/human_validation.md](../phase5/human_validation.md) — the real
  human-review workflow, where a veterinarian scores the same five criteria
  independently, to check whether the AI jury's scores can be trusted.
- [eval_report.md](eval_report.md) — the corpus-wide report command
  (`eval-report --markdown`), which produces the aggregate, all-papers view
  that these single-paper deep dives are the zoomed-in complement to.
