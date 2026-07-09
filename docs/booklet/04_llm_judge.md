# Chapter 4 — The Blind AI Judge

**Who this is for:** a technical beginner. You don't need to have read the
earlier chapters. This chapter explains how the project turns three
AI-written summaries into scores you can trust — and, just as importantly,
why you should trust them.

---

## Where we are

By this point in the pipeline, each veterinary paper has been summarized by
three different AI companies — OpenAI, Anthropic, and Google (Gemini). Chapter 3
covered how those summaries are produced. This chapter covers what happens next:
grading them.

A few plain definitions we'll use throughout:

- **Provider** — one of the three AI companies whose model *wrote* a summary
  (OpenAI, Anthropic, Gemini).
- **Judge** (or **judge model**) — an AI model whose job is to *score* a
  summary against the original article. A provider can play either role: OpenAI
  can write a summary in one step and judge summaries in another. The two jobs
  are kept strictly separate.
- **Processed text** — the cleaned-up body of the original article (references
  and publisher boilerplate stripped out), which lives in `data/processed/`.
  This is the "answer key" the judge compares each summary against.
- **Jury score** — the single 1-to-5 number that summarizes how good one
  summary is. Most of this chapter builds up to exactly how that number is
  computed.

The core idea is simple: *ask a separate AI model to read the real article and
the candidate summary, and score how faithful, complete, useful, clear, and
safe the summary is.* The details are where the trustworthiness lives.

---

## 1. Why the judge is "blind"

### The problem: self-preference bias

Large language models have a documented habit: **when a model is asked to grade
its own output, it tends to score it higher than a neutral grader would.** This
is called *self-preference bias*. It's not malice — it's a statistical tilt in
how models recognize and reward text that looks like their own.

For a study whose whole point is to compare three providers fairly, that tilt is
poison. If the judge knew "this summary was written by OpenAI," any preference —
for or against — would contaminate the score. The comparison would no longer
measure summary quality; it would measure the judge's opinion of brand names.

### The fix: blind judging

**Blind judging** means the judge never sees which provider wrote the summary it
is scoring. It sees only two things:

1. The **processed text** of the real article (the answer key).
2. The **candidate summary** (the thing being graded).

It does *not* see: the provider name, the model name, or even the words
"OpenAI," "Anthropic," "Gemini," "GPT," or "Claude." The provider's identity is
attached to the score afterward, as metadata, once grading is already done.

### How blinding is *enforced*, not just intended

A promise to "keep it fair" is worthless if a future code change can quietly
break it. So the project enforces blinding **structurally** — the design makes
leaking the identity hard to do by accident. Three guarantees stack up:

1. **The function that builds the judge's prompt has no way to receive a
   provider name.** In code, the prompt is built by a function whose inputs are
   only `(reference_text, candidate_summary)`. There is no `provider` or
   `model_name` parameter to pass. You can't leak a value the function refuses
   to accept.

2. **An automated test scans the finished prompt for forbidden words.** Every
   time the tests run, the rendered judge prompt is checked against a
   blocklist — `openai`, `anthropic`, `gemini`, `gpt`, `claude`, `chatgpt`. If
   any of those words ever appears in a judge prompt, the test fails loudly and
   the change can't ship.

3. **The judge's own name is recorded only after the fact.** When a judge
   finishes scoring, the record notes which model did the judging (so results
   are reproducible), but that name is never fed back into another judge's
   prompt.

This is why the project documentation calls the blind protocol
"non-negotiable." It isn't a preference — it's the thing that makes every
number in the study defensible.

---

## 2. The rubric: five things the judge scores

The judge doesn't produce one holistic "good/bad" verdict. It scores **five
separate criteria**, each on a **1-to-5 scale**, where:

- **1** = poor
- **3** = acceptable but imperfect
- **5** = excellent
- **2 and 4** are used when a summary lands between those anchors.

Here are the five criteria, in plain language, with the judge's scoring guide.

### Faithfulness — "is it *true* to the article?"

Does everything the summary claims actually come from the article? A summary
that invents a treatment effect, a species, a sample size, or a statistical
result can mislead a veterinarian even if it reads beautifully.

- **5** — Every clinically meaningful claim is supported by the article.
- **3** — Mostly supported, with minor unsupported wording or overgeneralization.
- **1** — Important claims contradict the article or aren't supported by it.

### Completeness — "does it cover the standard pieces?"

A good research summary should touch the six elements a clinician expects:
objective/research question, study design and methods, species and sample size,
key results, clinical significance, and limitations.

- **5** — All six elements are present.
- **3** — Four or five elements are present.
- **1** — Three or fewer elements are present.

### Clinical usefulness — "can a vet actually *use* this?"

A summary can be technically accurate yet too vague to help in practice. Species
context matters enormously here: a finding in cats should not be silently
presented as if it applies to dogs, horses, or "animals" in general.

- **5** — Species and clinical context are clear early, with an actionable take-away.
- **3** — Partly useful, but vague, delayed, or missing some species context.
- **1** — A clinician wouldn't get a reliable interpretation.

### Clarity — "is it readable and organized?"

Confusing summaries slow interpretation and can bury important caveats. Note
that clarity is deliberately weighted *lowest* of the five (more on weights
below) — a clear-but-wrong summary is more dangerous than a clunky-but-correct
one.

- **5** — Concise, organized, readable, non-repetitive.
- **3** — Understandable, with minor flow, length, or repetition issues.
- **1** — Disorganized, confusing, or substantially repetitive.

### Safety — "could it mislead clinical interpretation?"

The catch-all clinical-risk criterion: wrong species, wrong intervention, wrong
outcome, exaggerated certainty, or a missing caveat — anything that could change
how a veterinarian reads the paper.

- **5** — No misleading clinical interpretation and no species-generalization risk.
- **3** — Minor wording could be misunderstood but is unlikely to change practice.
- **1** — The summary could mislead a clinician.

The judge returns each score together with a one-sentence reason, so a human can
later see *why* it scored the way it did.

---

## 3. Hallucinations: catching made-up facts

A **hallucination** is a specific, defined thing in this project: **any factual
claim in the summary that is not explicitly stated or directly implied by the
article text.** It's not a synonym for "error" or "typo" — it's specifically an
unsupported claim.

Whenever the judge spots one, it must return four pieces of evidence for it — not
just flag it, but *prove* it:

| Field | What it holds |
|---|---|
| `claim` | The exact unsupported sentence, quoted from the summary. |
| `source_quote` | The article text that contradicts it or shows it isn't supported. |
| `category` | Which *kind* of hallucination it is (see below). |
| `severity` | `minor` or `major`. |

### The four categories

- **`fabricated_statistics`** — a number the article never gives (an invented
  p-value, sample size, or percentage).
- **`omitted_caveat`** — the summary states a finding but drops a limitation or
  qualifier the article insisted on.
- **`contradiction`** — the summary says the opposite of what the article says.
- **`unsupported_inference`** — the summary reaches a conclusion the article
  doesn't actually support (e.g. generalizing a canine-only result to all
  animals).

### Two severities, and why `major` triggers a human

- **`minor`** — a wording slip unlikely to change how a clinician acts.
- **`major`** — something that *could* change clinical interpretation.

Any **major** hallucination automatically sets a flag called
`requires_human_review` to true. That flag is the project's tripwire for pulling
a summary out for a human to double-check (this feeds the human-validation step
covered in Chapter 7). It fires in three situations:

1. The judge reports a **major** hallucination, **or**
2. The judge's own **confidence is low** (a confidence rating below 3 out of 5 —
   the judge is essentially telling you "I'm guessing"), **or**
3. The judge's response **couldn't be parsed** at all (a malformed reply — see
   the box below).

The logic is deliberately generous: any *one* of these is enough to flag the row.
A dangerous-sounding claim gets a human look even when the judge was otherwise
confident.

> **What "couldn't be parsed" means.** The judge is asked to reply in a strict
> machine-readable format (JSON — a structured text format of labelled fields).
> The code tries several ways to read the reply: strict parsing first, then
> pulling the first structured block out of any chatty wrapper text, then a
> last-ditch pattern search. If *all* of those fail, the row is stamped with a
> sentinel score of **99** and flagged for human review, rather than pretending
> a broken response was a real score. A score of 99 in the data always means
> "the machine couldn't read the judge here — a human needs to look."

---

## 4. The jury score: turning five numbers into one

This is the heart of the chapter. The judge has handed back five criterion
scores. How do those become the single **jury score** everyone quotes?

### Who does the arithmetic — and why it isn't the AI

**The judge model does not calculate the final score.** It only returns the five
criterion scores (plus its hallucination evidence and confidence). **Python does
the math** afterward. Three reasons this matters:

- LLMs occasionally make arithmetic mistakes, especially with weighted formulas.
  A calculator never does.
- The *exact same* formula then runs identically on every paper, every provider,
  and every re-run — no drift.
- Anyone auditing the study can open one Python file and verify the math by hand.
  It's fully transparent.

### Two scores are *always* computed, not one

From the same five criterion scores, Python actually computes **two** jury
scores every single time, and stores both on every row:

- **`jury_score_unweighted`** — a plain, flat average of the five criteria (each
  counts equally). This matches the method used by the standard MedHELM
  evaluation framework this project borrows from.
- **`jury_score_weighted`** — this project's *clinical-risk-weighted* average,
  where the more dangerous-to-get-wrong criteria count for more.

The field named simply **`jury_score`** is just an alias pointing at whichever of
those two is chosen as "primary" for the run. That choice is a one-line setting
(`JURY_AGGREGATION_MODE` in the configuration), and its **default is
`unweighted`**. Because *both* numbers are always stored, you can switch which one
is primary — or just read the other field directly — without ever re-running the
judges. Nothing has to be recomputed by the AI.

Whenever precision matters, name which one you mean — `jury_score_weighted` or
`jury_score_unweighted` — rather than just "the jury score," since both always
exist side by side.

### What a weighted average is

A **weighted average** simply means some items count more than others. Think of a
course grade where the final exam is worth 40% and homework is worth 10% — not
every assignment pulls the average equally. Here, faithfulness and safety pull
the jury score harder than clarity does.

### The weights, and the reasoning behind each

The weights encode **clinical risk** — how bad it is to get that criterion wrong:

| Criterion | Weight | Why this weight |
|---|---|---|
| Faithfulness | **1.5** | Highest. A false fact is the worst possible failure — it directly threatens the study's validity and can mislead a clinician. |
| Safety | **1.3** | High. Clinically misleading wording deserves a special penalty even when the summary reads well. |
| Clinical usefulness | **1.2** | Important. A veterinary summary has to actually help a clinician, with the right species context. |
| Completeness | **1.0** | Standard. Missing a minor section is real, but less dangerous than a false claim. |
| Clarity | **0.8** | Lowest. Readability matters, but style should never outweigh correctness. |

Add the weights together and you get the **total weight**:

```text
1.5 + 1.3 + 1.2 + 1.0 + 0.8 = 5.8
```

### The formula, step by step

For the weighted score: multiply each criterion's score by its weight, add up all
five products, then divide by the total weight (5.8).

```text
jury_score = (score₁ × weight₁ + score₂ × weight₂ + … + score₅ × weight₅)
             ÷ (weight₁ + weight₂ + … + weight₅)
```

Dividing by the total weight is what keeps the answer on the same **1-to-5
scale** as the inputs. (For the *unweighted* score, every weight is just 1, so
this same formula becomes an ordinary average: add the five scores and divide
by 5.)

### A fully worked example

Suppose the judge returns these five scores for one summary:

| Criterion | Judge score (1–5) | Weight | Score × weight |
|---|---|---|---|
| Faithfulness | 4 | 1.5 | 4 × 1.5 = **6.0** |
| Safety | 5 | 1.3 | 5 × 1.3 = **6.5** |
| Clinical usefulness | 3 | 1.2 | 3 × 1.2 = **3.6** |
| Completeness | 4 | 1.0 | 4 × 1.0 = **4.0** |
| Clarity | 4 | 0.8 | 4 × 0.8 = **3.2** |

**Step 1 — add up the weighted products:**

```text
6.0 + 6.5 + 3.6 + 4.0 + 3.2 = 23.3
```

**Step 2 — divide by the total weight (5.8):**

```text
jury_score_weighted = 23.3 ÷ 5.8 = 4.02
```

So the **weighted** jury score for this summary is **4.02**. Notice that clinical
usefulness (a 3) pulled the average down more than clarity (a 4) did — because
clinical usefulness carries a heavier weight.

Now the **unweighted** score, on the very same five numbers — just a flat
average:

```text
jury_score_unweighted = (4 + 5 + 3 + 4 + 4) ÷ 5 = 20 ÷ 5 = 4.00
```

With the default setting (`unweighted`), the primary **`jury_score` for this
summary is 4.0**, and the weighted alternative (4.02) sits on the same row for
comparison.

### The scale's endpoints (a sanity check)

If every criterion scored a **5**, the weighted formula gives the maximum:

```text
(5×1.5 + 5×1.3 + 5×1.2 + 5×1.0 + 5×0.8) ÷ 5.8 = 29.0 ÷ 5.8 = 5.0
```

If every criterion scored a **1**, it gives the minimum:

```text
(1×1.5 + 1×1.3 + 1×1.2 + 1×1.0 + 1×0.8) ÷ 5.8 = 5.8 ÷ 5.8 = 1.0
```

So the jury score is always neatly bounded between **1.0 and 5.0**, whichever
aggregation you use.

> **A note on the legacy `quality_score`.** You may spot a `quality_score` field
> ranging 1–10 in the data. That is *not* a separate judge opinion — it's the
> jury score stretched onto a 1–10 scale by a simple formula, kept only so older
> analysis notebooks don't break. New analysis should always use `jury_score`.

---

## 5. Weighted vs. unweighted: when each one is right

Both numbers are always available, so you pick based on the question you're
asking. On the worked example above they were almost identical (4.00 vs. 4.02) —
because all five scores were similar. They pull apart when a **low score lands on
a high-weight criterion.**

Here's the illustrative case: a summary that is perfectly *clear* (clarity 5) but
clinically *unsafe* (safety 1). The unweighted average treats those two as
cancelling out equally. The weighted average does not — safety (1.3) outweighs
clarity (0.8), so the weighted score drops noticeably lower. That's the whole
point of the weighting: it refuses to let a polished-but-dangerous summary look
as good as a clunky-but-safe one.

| Use the **unweighted** score when… | Use the **weighted** score when… |
|---|---|
| You want a number comparable to published MedHELM benchmarks (it's the exact same flat mean they use). | You want the score to reflect this project's clinical-risk judgment. |
| You want the simplest possible default that needs no justification. | You're reporting the paper's primary clinical results and want dangerous failures penalized harder. |

Because both are stored, you never have to commit: the results report prints the
primary mean, the weighted mean, and the unweighted mean side by side, so you can
compare the two methods directly.

---

## 6. One judge, or a panel? Judge disagreement

Everything so far assumed a single judge. That's the default — one judge
(OpenAI) keeps costs down. But "one grader" invites a fair question: *would a
different judge have scored it the same way?* A score with no measure of its own
reproducibility is hard to defend.

So the project supports a **judge panel** — running two or three judges over the
same summaries. It's a one-line configuration change with an escalation path:

| Setting | Judges | What it buys you |
|---|---|---|
| `openai` (default) | 1 | Cheapest. One opinion, no agreement estimate. |
| `openai,anthropic` | 2 | Two independent opinions; enables a disagreement measure. |
| `openai,anthropic,gemini` | 3 | Three opinions — matching the standard MedHELM jury-panel size. |

Each added judge roughly multiplies the judging cost by the number of judges, so
the panel is opt-in rather than the default.

### What "judge disagreement" means in plain English

When two or more judges score the same summary, the project records a
**`judge_disagreement`** number. At its simplest, this is just the **spread** of
their jury scores — the highest score minus the lowest.

- If two judges both give a summary 4.0, disagreement is `4.0 − 4.0 = 0.0` — they
  agree perfectly.
- If one gives 4.5 and another gives 3.0, disagreement is `1.5` — a real gap
  worth noticing.

A big spread is a caution flag: it says the judges saw the summary differently,
so that particular score deserves less confidence.

Because the append-only data file stores one row per judge, the code combines
them afterward, computing the cross-judge average **for both aggregation methods
at once** (weighted and unweighted), each with its own disagreement spread. As
with everything else here, keeping both means changing the primary aggregation
mode after a panel run never requires re-judging.

> **Beyond simple spread.** Max-minus-min is the beginner-friendly disagreement
> measure. For a proper, publishable measure of judge agreement — one that
> corrects for the agreement you'd expect purely by chance, and copes with a
> judge missing on some items — the project uses a statistic called
> **Krippendorff's alpha**. That's a chapter of its own: **Chapter 6** teaches
> how it works and why it's the right tool. Here, just know that
> `judge_disagreement` is the quick, intuitive companion to that deeper measure.

---

## 7. Run manifests: proving *how* a score was produced

Imagine two evaluation runs disagree — last month's numbers don't match this
month's. To debug that, you need to know exactly what went into each run: which
version of the code, which judge prompt, which dataset, which models, which
settings. If any of those quietly changed, that's your explanation.

To make this answerable, **before any judge is called**, each evaluation run
writes a **run manifest** — a small provenance record (a "receipt" for the run) —
into `data/run_manifests/`. It captures:

- A unique **run ID** and timestamps (start and finish).
- The **git commit and branch** the code was on — i.e. the exact code version.
- The **dataset path and a SHA-256 hash** of it. (A hash is a short fingerprint
  of a file's contents: if even one character of the data changes, the
  fingerprint changes completely. Same hash → provably the same input data.)
- The **judges used** and their resolved model versions.
- The **judge prompt's identity and its own hash** — so you can prove the rubric
  wording didn't change between runs.
- The generation **settings**: temperature, max output tokens, and the random
  seed.

After the run finishes, the manifest is patched with a terminal status —
`completed`, `failed`, or partial. Written *before* judging and finalized
*after*, it's a trustworthy record even if the run crashes midway.

Why this matters when two runs disagree: instead of guessing, you open the two
manifests and diff them. Different prompt hash? The rubric wording changed.
Different dataset hash? The input summaries changed. Different git commit? The
code changed. The manifest turns "why don't these match?" from a mystery into a
lookup.

---

## 8. What a finished evaluation looks like

Every scored summary becomes one row appended to `data/evaluations.jsonl`.
(JSONL means "one self-contained record per line" — Chapter 2 covers the format
in depth; here, just picture one grade per line.) The row carries the whole story
of that grade. Using the same worked example from Section 4, the important fields
look like this:

| Field | What it tells you |
|---|---|
| `doi` + `summarizer` | Which paper, and which provider's summary was judged. |
| `judge` | Which model did the blind judging (kept separate from the summarizer). |
| `criteria_scores` | The five 1–5 scores the judge returned, each with a reason. Python never altered these. |
| `jury_score` | The **primary** score — `4.0` here (unweighted by default). |
| `jury_score_weighted` / `jury_score_unweighted` | Both methods, always stored — `4.02` / `4.0` here. |
| `judge_disagreement` | `0.0` with one judge; the score spread once a panel is used. |
| `hallucination_claims` | Any quoted made-up-fact evidence, with source quotes. |
| `requires_human_review` | `false` here — high confidence, no major hallucination. |
| `confidence_score` | The judge's own 1–5 confidence in its grading. |
| `parse_method` | `json` means the judge's reply was read cleanly on the first try. |
| `strata` | Subgroup labels — species, study design, topic, journal — used for grouped reporting. |
| `quality_score` | The legacy 1–10 alias (8 here). Ignore it in new analysis. |

On disk, after scoring one paper's three summaries, you'd see three independent
lines — one for OpenAI's summary, one for Anthropic's, one for Gemini's — each a
complete, standalone record. Re-running evaluation skips pairs it has already
scored, so the file grows safely and never has to be rewritten.

---

## Recap

- The judge is a **separate AI model** that scores each summary against the real
  article's **processed text**.
- **Blind judging** — the judge never sees who wrote the summary — is enforced
  *structurally* (a name-free prompt function, an automated forbidden-word test,
  and after-the-fact identity logging), because self-preference bias would
  otherwise invalidate the comparison.
- The judge scores **five criteria** (faithfulness, completeness, clinical
  usefulness, clarity, safety) from 1 to 5, and hunts for **hallucinations**,
  tagging each with a category and a `minor`/`major` severity.
- **Python — not the AI — computes the jury score**, always producing both an
  **unweighted** flat average and a clinical-risk-**weighted** average. On the
  worked example the primary (unweighted) `jury_score` is **4.0**, with the
  weighted **4.02** stored alongside it.
- Any **major** hallucination, low judge confidence, or an unreadable response
  flags the row for **human review**.
- An optional **judge panel** (2–3 judges) adds a **disagreement** measure; the
  quick version is the score spread, and Chapter 6 covers the rigorous version
  (Krippendorff's alpha).
- Every run writes a **run manifest** — a hashed provenance receipt — so two
  disagreeing runs can be diffed instead of guessed about.

Next, **Chapter 5** takes these evaluation rows and shows you how to *read* them
— what the reports and publication tables actually mean.
