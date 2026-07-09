# Chapter 6 — The Statistics Behind the Numbers

**Who this chapter is for:** a technical beginner with **zero statistics
background**. If you have ever seen a word like *Wilcoxon*, *p-value*,
*Krippendorff*, or *cosine similarity* in this project's reports and thought
"I have no idea what that means," this chapter is for you. You do not need to
have read any earlier chapter. Every idea is built from the ground up, every
formula is shown with a small worked example you can follow with a calculator,
and for each one we explain *why this specific tool was chosen* over the
obvious alternatives.

This is the longest chapter in the booklet on purpose. Statistics is where a
project like this earns — or loses — the right to say "provider A is better
than provider B" in a way a scientific reviewer will believe. Take it slowly.
Each part stands on its own, so you can read one, close the file, and come back
to the next later.

---

## What we are actually trying to prove

This project compares how well three AI **providers** (the three companies whose
models write summaries — OpenAI, Anthropic, and Gemini) summarize veterinary
papers. A blind AI **judge** (a model that scores summaries without being told
which provider wrote them) gives every summary a **jury score** — a 1-to-5
quality number. (When two or three judges score the same summary, we average
their scores; that average is the *jury* score. "Jury" because it can be one
judge or a panel.)

But raw averages are not enough for a paper. We have to answer harder questions
with real numbers instead of gut feeling:

| # | The question in plain English | The tool | Part |
|---|---|---|---|
| 1 | Did provider A *really* beat provider B, or was it luck? | Wilcoxon signed-rank test | 2 |
| 2 | Looking at all three providers at once, is *anyone* standing out? | Friedman test | 3 |
| 3 | I ran several tests — how do I keep "lucky" results from sneaking in? | Bonferroni / Holm / Benjamini-Hochberg | 4 |
| 4 | How sure am I about a provider's average score? | Bootstrap confidence intervals | 5 |
| 5 | Do the judges agree with each other enough to trust the scores? | Krippendorff's alpha | 6 |
| 6 | Do an AI judge and a human vet pick the *same* score box? | Cohen's Kappa + percent agreement | 7 |
| 7 | Does the AI judge's opinion track a human vet's opinion at all? | Pearson / Spearman / Bland-Altman | 8 |
| 8 | Does a summary keep the *meaningful* content of the abstract? | TF-IDF + cosine similarity | 9 |
| 9 | How much of the source wording did the summary actually reuse? | ROUGE | 10 |
| 10 | Is this provider good *value* for the money? | Cost-per-quality-point | 11 |

We'll take them in that order. Part 1 first teaches four vocabulary ideas that
every later part leans on.

---

## Part 0 — The toolbox (numpy, scipy, scikit-learn)

Before any statistics, three Python libraries do the heavy lifting. You don't
have to use them to understand this chapter, but you'll see their names in the
code and reports, so here's what each one is:

- **numpy** — a library for doing math on big lists of numbers *fast*. Think of
  it as a spreadsheet column that knows how to do arithmetic on itself. Almost
  every scientific Python tool sits on top of it.
- **scipy** — built on numpy, it ships a box of **pre-written, peer-reviewed
  statistical tests**. Instead of coding the Wilcoxon test ourselves (and
  risking a subtle bug), we call `scipy.stats.wilcoxon(...)` and trust an
  implementation thousands of scientists have already checked.
- **scikit-learn** (imported as `sklearn`) — sits alongside scipy and provides
  two more validated tools scipy doesn't: Cohen's Kappa
  (`sklearn.metrics.cohen_kappa_score`) and the TF-IDF / cosine-similarity
  machinery.

**Why lean on libraries instead of hand-writing the math?** Three reasons that
matter for a real study:

1. **Trust and citability.** A reviewer trusts `scipy.stats.wilcoxon` far more
   than home-made math, and you can *cite* scipy in a methods section.
2. **Correctness at small samples.** scipy computes *exact* answers when you
   only have a handful of data points — which is often our situation.
3. **Reproducibility.** The exact library versions are pinned in
   [`requirements.txt`](../../requirements.txt), so every computer runs the
   identical math and gets the identical result.

**The one exception:** Krippendorff's alpha (Part 6) is written by hand in
[`llm-sum/reliability.py`](../../llm-sum/reliability.py) — *not* because we
prefer home-made math, but because neither scipy nor scikit-learn includes it.
Everything else uses a validated library.

---

## Part 1 — Four vocabulary ideas you need first

Just four. Once these click, the tests are easy.

### 1a. The p-value — the single most important idea

A **p-value** answers exactly one question: *"If there were truly no real
difference, how likely is it that I'd see a difference this big just by luck?"*

- A **small p-value** (by convention, **below 0.05**) means "this would be a
  surprising fluke if nothing were really going on — so the difference is
  probably **real**." We call that *statistically significant*.
- A **large p-value** (say 0.5) means "this could easily happen by chance — so
  I can't claim a real difference."

**The coin analogy.** Flip a coin 10 times and get 6 heads — not surprising
(large p); you wouldn't call the coin rigged. Flip it 100 times and get 60
heads — *that* is surprising (small p), and you'd start to believe the coin is
genuinely biased. A p-value is just that "how surprising is this?" feeling,
turned into a precise number between 0 and 1.

> ⚠️ **A small p-value says a difference is *probably real*; it does NOT say the
> difference is *large or important*.** A provider could beat another by a
> statistically real but clinically meaningless 0.05 points. Always read a
> p-value *next to* the actual size of the gap, never instead of it.

### 1b. "Paired" data

All three providers summarize **the same papers**. So when we compare OpenAI vs
Anthropic we can line them up **paper by paper**: on paper 1, OpenAI scored 5
and Anthropic scored 4; on paper 2, 3 vs 3; and so on. Data that lines up like
this is **paired**.

Pairing is powerful. Instead of the blurry question "is OpenAI's overall
average higher?" we can ask the sharp question "did OpenAI beat Anthropic *on
the very same paper*, more often than not?" — which automatically cancels out
the fact that some papers are just harder to summarize than others.

### 1c. Ranks

Instead of using raw scores, many tests use their **rank** — their position
after you sort them. The scores `[3, 5, 4]` have ranks `[1, 3, 2]` (smallest
gets rank 1). When two values tie, they share the *average* of the ranks they'd
occupy: `[4, 4, 5]` has ranks `[1.5, 1.5, 3]`.

Why bother? Ranks are **robust**: one weird outlier can't distort them, and
they don't assume the 1-5 scale is perfectly evenly spaced (is the gap from 4
to 5 really the same "size" as 1 to 2? Ranks don't have to care). Tests built on
ranks are called *non-parametric*, which just means "they don't assume the data
follows a neat bell curve." Our 1-5 scores are lumpy and small, so rank-based
tests are the honest choice.

### 1d. Correlation

A **correlation** measures whether two sets of numbers move together. It runs
from `+1` (perfectly in step) through `0` (unrelated) to `-1` (perfectly
opposite). We'll use it in Part 8 to ask "when a human vet rates a summary high,
does the AI judge also rate it high?"

---

## Part 2 — The Wilcoxon signed-rank test

**The question it answers:** *"Did provider A really beat provider B, on the
same papers — or could the gap be luck?"*

**The everyday version.** Two chefs each cook the same 10 dishes; a taster
scores every dish. For each dish you note **who won and by how much**. If chef A
keeps winning, dish after dish, chef A is probably genuinely better. If they
trade wins randomly with tiny margins, it's probably a tie. That's exactly what
Wilcoxon measures.

### How it works, step by step

1. For each paper, compute the **difference** (A's score − B's score).
2. Throw away any paper with a difference of **0** — a tie picks no winner.
3. Take the **absolute values** of the remaining differences (ignore the +/−
   sign for a moment) and **rank** them from smallest to largest.
4. Add up the ranks belonging to A's wins (call it **W⁺**) and the ranks
   belonging to B's wins (**W⁻**).
5. If one of those totals is much bigger than the other, one provider is
   consistently winning → small p-value. If they're balanced, it's a wash →
   large p-value.

### Worked example

Six papers. The differences (A − B) come out to:

```
+1, +2, +1, +3, +1, +2      (A won every paper — all positive, no ties)
```

Take absolute values and rank them (smallest = rank 1, ties share the average):

| Paper | diff | abs | rank |
|---|---|---|---|
| 1 | +1 | 1 | 2   (the three 1's occupy ranks 1,2,3 → average 2) |
| 3 | +1 | 1 | 2 |
| 5 | +1 | 1 | 2 |
| 2 | +2 | 2 | 4.5 (the two 2's occupy ranks 4,5 → average 4.5) |
| 6 | +2 | 2 | 4.5 |
| 4 | +3 | 3 | 6 |

Every difference is positive, so **W⁺ = 2 + 2 + 2 + 4.5 + 4.5 + 6 = 21** and
**W⁻ = 0**. (Sanity check: the ranks 1 through 6 always sum to
6·7/2 = 21, and here they all landed on A. ✓)

W⁻ = 0 is as lopsided as it gets. How surprising is "A wins all 6"? If the two
providers were truly equal, each paper is a 50/50 coin flip, so the chance of A
sweeping all six is 1 in 2⁶ = 1 in 64. The two-sided p-value doubles that (a
sweep by *either* provider would be equally surprising):
**p = 2 × 1/64 = 0.03125**. That's below 0.05 → "A really did beat B."

Notice how few papers it took (six) — and also how, with only *five* clean wins,
p would have been 2 × 1/32 = 0.0625, *just above* 0.05. Small samples are
fragile; this is why the real study wants the whole corpus, not six papers.

### Why not just compare the two averages?

Because an average hides consistency. Provider A could have a higher average
purely because it aced one paper while quietly losing most of the others.
Wilcoxon looks at the **pattern of paper-by-paper wins**, so that one lucky
paper can't fool it. And because it uses ranks, it doesn't care whether the
gap from 4-to-5 is "really" the same size as 1-to-2.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py),
via `scipy.stats.wilcoxon(..., method="auto")`, for every head-to-head pair of
providers. `method="auto"` tells scipy to use the **exact** calculation when the
sample is small — precisely the situation where a rougher shortcut would be
least trustworthy.

---

## Part 3 — The Friedman test

**The question it answers:** *"Looking at all three providers at once, is at
least one of them genuinely different from the others?"*

**The everyday version.** Now three chefs cook the same 10 dishes. Before you
dive into "A vs B, A vs C, B vs C," you ask one gatekeeper question first: *is
anyone standing out at all?* Friedman is that gatekeeper — it's the
"three-or-more-groups" cousin of Wilcoxon.

### How it works

For **each paper**, rank the three providers (best = 3, worst = 1). Then add up
each provider's ranks across all papers. If one provider keeps grabbing the top
rank, its rank total towers over the others → small p-value → "yes, someone's
different." If the ranks are jumbled paper to paper, everyone's total ends up
similar → large p-value → "no clear standout."

The formula that turns those rank totals into a number is:

```
              12
χ²_F  =  ─────────────  ×  ( R₁² + R₂² + R₃² )   −   3 · n · (k + 1)
          n · k · (k+1)
```

where **n** = number of papers, **k** = number of providers, and **Rⱼ** = the
sum of ranks provider *j* collected. You don't need to memorize this — scipy
computes it — but seeing it once demystifies the "chi-square statistic" the
report prints.

### Worked example

Six papers, three providers (A, B, C). Say A always scored best and C always
worst — a perfectly consistent ordering:

Every paper ranks as A = 3, B = 2, C = 1. Over six papers the rank sums are:

```
R_A = 6 × 3 = 18      R_B = 6 × 2 = 12      R_C = 6 × 1 = 6
```

Plug in (n = 6, k = 3):

```
χ²_F = [12 / (6 · 3 · 4)] × (18² + 12² + 6²)  −  3 · 6 · 4
     = [12 / 72]         × (324 + 144 + 36)   −  72
     = (1/6)             × 504                 −  72
     = 84 − 72
     = 12
```

This statistic has **k − 1 = 2 degrees of freedom** (the number of providers
minus one). Looking up χ² = 12 with 2 degrees of freedom gives **p ≈ 0.0025** —
well below 0.05. So: yes, someone is genuinely different. (You read the p-value
the usual way; you never have to interpret the "12" itself.)

### Why run Friedman *before* the pairwise Wilcoxon tests?

Because testing many pairs separately is like buying many lottery tickets — run
enough comparisons and one will look "significant" by pure chance (more on this
in Part 4). Friedman is a single up-front check: if it says "nobody differs,"
you treat any individual pair that looks exciting with healthy suspicion. It
guards against fooling yourself.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py),
via `scipy.stats.friedmanchisquare(...)`, computed only over papers that **all**
providers scored (so the ranking is fair — every provider gets ranked on every
paper in the test).

---

## Part 4 — Multiple comparisons: why we adjust the p-values

**The problem it solves:** *"I ran several tests. How do I stop 'lucky' results
from sneaking in?"*

Here's the trap. A p-value below 0.05 means "this would happen by luck less than
5% of the time." But if you run **20** tests where nothing is really going on,
then on average **one** of them will still cross 0.05 by pure chance. Run enough
comparisons and you're *guaranteed* a false "discovery" eventually.

**The lottery analogy.** Buy one lottery ticket and winning is a genuine
surprise. Buy a hundred tickets and winning *one* says nothing about your luck —
it's expected. The more tests you run, the more you should *discount* any single
"win."

With three providers we run three head-to-head Wilcoxon tests per score mode
(A-vs-B, A-vs-C, B-vs-C) — few, but still more than one, so the risk is real.
The fix is to **adjust the p-values upward** to account for how many tests you
ran. There are three common ways to do this, from strictest to gentlest.

### Worked example (the same three p-values, three methods)

Suppose our three pairwise tests produced raw p-values:

```
0.01,  0.03,  0.04
```

All three look "significant" (all below 0.05). But we ran three tests, so let's
correct them.

**Method 1 — Bonferroni (the bluntest).** Multiply *every* p-value by the number
of tests (here, 3):

```
0.01 × 3 = 0.03      0.03 × 3 = 0.09      0.04 × 3 = 0.12
```

Now only the first survives (0.03 < 0.05). The other two are thrown out.
Bonferroni is simple but harsh — it protects against *even a single* false
positive, and in doing so it discards real findings when you run many tests.

**Method 2 — Holm-Bonferroni (a smarter version of Bonferroni).** Sort the
p-values smallest to largest, then multiply the smallest by 3, the next by 2,
the last by 1 (each by "how many tests remain"), enforcing that the adjusted
values never decrease as you go:

```
smallest 0.01 × 3 = 0.03
middle   0.03 × 2 = 0.06
largest  0.04 × 1 = 0.04  → bumped up to 0.06 to stay non-decreasing
```

Result: 0.03, 0.06, 0.06 → again only the first survives, but Holm is uniformly
*less* harsh than plain Bonferroni (it can only ever keep the same findings or
more, never fewer). Both Bonferroni and Holm control the chance of **even one**
false positive — the "family-wise error rate."

**Method 3 — Benjamini-Hochberg (what this project uses).** Sort smallest to
largest and multiply the *i*-th smallest by (total tests ÷ its rank), then
enforce the values never *increase* as you scan from largest down to smallest:

```
smallest 0.01 × 3/1 = 0.03
middle   0.03 × 3/2 = 0.045
largest  0.04 × 3/3 = 0.04
```

enforcing monotonicity from the top: largest stays 0.04; middle becomes
min(0.045, 0.04) = 0.04; smallest stays 0.03. Result: **0.03, 0.04, 0.04 — all
three survive.**

### Why does this project pick Benjamini-Hochberg?

Look at what happened to the same data:

| Method | What it controls | Survivors (of 3) |
|---|---|---|
| Bonferroni | chance of *any* false positive | 1 |
| Holm-Bonferroni | chance of *any* false positive | 1 |
| Benjamini-Hochberg (FDR) | *proportion* of "discoveries" that are false | 3 |

Bonferroni and Holm control the **family-wise error rate** — they'd rather miss
real effects than admit one false one. That's too strict for a **stratified
study** that runs *many* comparisons (per species, per study design, per
journal), because it throws away genuine findings (low statistical power).
Benjamini-Hochberg instead controls the **false discovery rate** (FDR) — it
accepts that, say, 5% of your flagged results might be false, in exchange for
catching far more of the real ones. It's the standard modern choice in
biomedical work, which is why this project uses it. You'll sometimes see an
FDR-adjusted p-value called a **q-value**.

**How to read our tables.** Each pairwise row shows both the **raw Wilcoxon p**
(for transparency) and **`p (BH-adj)`** (the corrected one). *Always use the
adjusted value to decide whether a provider truly beat another.* If only one
comparison was testable, the adjusted value equals the raw one — a family of one
needs no correction.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py),
via `scipy.stats.false_discovery_control(..., method="bh")`.

---

## Part 5 — Bootstrap confidence intervals

**The question it answers:** *"A provider's average score is 4.2 — but how sure
am I? Could the true value easily be 3.9 or 4.5?"*

An average from 20 papers is just an estimate. If you'd happened to collect a
*different* 20 papers, you'd get a slightly different average. A **confidence
interval** is a range — like "4.2, somewhere between 3.9 and 4.5" — that
captures how much that average would wobble. The **bootstrap** is a clever,
assumption-free way to compute that range.

### How the bootstrap works

The word "bootstrap" comes from "pulling yourself up by your own bootstraps" —
you squeeze extra mileage out of the data you already have, by **resampling** it:

1. Take your real scores, e.g. five papers scoring `[4, 5, 3, 4, 5]` (mean 4.2).
2. Draw a new sample of the same size **with replacement** — meaning the same
   paper can be picked more than once, and some skipped. You might draw
   `[5, 5, 4, 3, 5]` (mean 4.4), or `[4, 4, 4, 5, 3]` (mean 4.0).
3. Record that resample's mean.
4. Repeat **2000 times**, collecting 2000 slightly different means.
5. The middle 95% of those 2000 means is your **95% confidence interval** —
   say, `[3.6, 4.8]`.

You're simulating "what if I'd drawn slightly different data?" using only the
data you have. No bell-curve assumption required, which suits our lumpy 1-5
scores.

### Why 2000 resamples?

More resamples give a smoother, more stable interval, but cost more computing
time. 2000 is a common sweet spot — enough to be stable, cheap enough to run
often. It's tunable in this project (`PUBLICATION_BOOTSTRAP_RESAMPLES`), and the
resampling is **seeded** (a fixed starting point for the randomness) so the same
data always produces the identical interval — reproducibility again.

### How to read one

> **A confidence interval that straddles another provider's mean means the gap
> is not resolved at this sample size.** If OpenAI is 4.2 [3.6-4.8] and Anthropic
> is 4.0 [3.5-4.6], those ranges overlap heavily — you can't confidently say one
> beat the other yet. Read the interval *together with* the Wilcoxon p-value from
> Part 2, never instead of it.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py),
via `scipy.stats.bootstrap(..., method="percentile")`, 2000 resamples by
default, seeded for exact reproducibility.

---

## Part 6 — Krippendorff's alpha

**The question it answers:** *"Do our scorers agree with each other — enough that
we can trust the scores at all?"*

This is a *different kind* of question. Wilcoxon and Friedman compare
**providers**. Krippendorff's alpha checks the **measuring instrument itself**.
Our "instrument" is a scorer: usually an AI judge, or (in human validation) a
veterinarian. If we run several judges over the same summaries and they wildly
disagree, then *any* score is shaky and every comparison built on it is suspect.
Alpha puts a single number on that agreement.

**The everyday version.** Three judges score the same gymnastics routines. If
they hand out nearly identical scores, the scoring is **reliable** — you can
trust it. If judge 1 says 9, judge 2 says 4, judge 3 says 7 for the same
routine, the scoring is noise. Alpha measures how close to "identical" the
judges are.

### The scale (this is most of what you need to remember)

| Alpha | Meaning |
|---|---|
| **1.0** | Perfect agreement — scorers are interchangeable. |
| **0.80 and up** | Strong; safe to draw firm conclusions. |
| **0.667 – 0.80** | Acceptable; tentative conclusions only. |
| **0** | Agreement no better than random guessing. |
| **below 0** | Worse than random — scorers systematically *disagree*. |

### The formula, and why it's not just "% identical"

Alpha compares the disagreement you *actually observed* to the disagreement
you'd *expect by pure chance*:

```
alpha = 1 −  (disagreement observed)
             ─────────────────────────
             (disagreement expected by chance)
```

Two things make this smarter than simply asking "what % of the time did the
judges give the exact same score?":

1. **Luck inflates raw agreement.** On a 1-5 scale, two people guessing randomly
   still match about 20% of the time by chance. Alpha *subtracts that out*, so
   `alpha = 0` honestly means "no better than coin-flips," not "0% overlap."
2. **A 4-vs-5 is a near-miss; a 1-vs-5 is a blowup.** Plain "% identical" treats
   both as merely "not equal." Alpha knows the scores are *ordered numbers*, so
   it measures disagreement by the *squared distance* between scores — a 4-vs-5
   counts as a small disagreement, a 1-vs-5 as a large one.

### Worked example

Two judges score three summaries:

```
Summary 1:  judge A = 4,  judge B = 5
Summary 2:  judge A = 3,  judge B = 3
Summary 3:  judge A = 5,  judge B = 4
```

**Step 1 — observed disagreement.** For each summary, measure how far apart the
two judges are, squared:

```
Summary 1: (4−5)² = 1
Summary 2: (3−3)² = 0
Summary 3: (5−4)² = 1
```

The project's formula counts each ordered pair (A-then-B *and* B-then-A), so each
of these doubles, and it averages over the 6 individual ratings:

```
observed = (2·1 + 2·0 + 2·1) / 6 = 4 / 6 = 0.667
```

**Step 2 — expected disagreement.** Now pretend the judges' scores were tossed
into one bucket — `[4, 5, 3, 3, 5, 4]` — and shuffled. How far apart would two
randomly drawn scores be, on average (squared)? Working through every pair in
that bucket gives:

```
expected = 1.6
```

**Step 3 — combine:**

```
alpha = 1 − 0.667 / 1.6 = 1 − 0.417 = 0.583
```

Alpha ≈ **0.58** — which lands in the "weak agreement" band (0 to 0.667). Read
plainly: these two judges are *somewhat* aligned, but not reliably enough to
draw firm conclusions. You'd want them tighter (0.80+) before fully trusting the
jury scores.

### Why is this the one hand-rolled statistic?

Every other test in the project uses a validated library. Krippendorff's alpha
is written by hand in
[`llm-sum/reliability.py`](../../llm-sum/reliability.py) for one reason only:
**neither scipy nor scikit-learn ships it.** It also gracefully handles
**missing data** — e.g. a vet who only reviewed a subset of summaries — using
whatever overlap exists instead of forcing you to throw away rows. That
missing-data tolerance is exactly why it's the right choice for a jury where not
every judge scores every item.

---

## Part 7 — Cohen's Kappa (and percent agreement)

**The question it answers:** *"When the AI judge and a human vet score the same
summary, how often do they land on the **exact same** 1-5 category — more than
chance would predict?"*

This sounds like Part 6, but it's a deliberately *different* question. Alpha
treats scores as an ordered number line (a 4-vs-5 barely counts) and tolerates
missing raters. Cohen's Kappa asks the blunter, more literal question: **did
they pick the same box?** That's the specific metric this study's
human-validation design calls for when checking whether an AI judge can stand in
for a human expert.

**The everyday version.** Two vets independently sort 20 x-rays into "normal,"
"mild," or "severe." **Percent agreement** is simply: how many of the 20 did they
sort into the *same* bucket? But raw percent agreement is misleading — even two
vets guessing randomly would land in the same bucket sometimes by luck. Cohen's
Kappa *subtracts out that luck*.

### The formula

```
              (agreement you actually saw) − (agreement expected by chance)
kappa  =  ─────────────────────────────────────────────────────────────────
                        1 − (agreement expected by chance)
```

`kappa = 1` is perfect agreement; `kappa = 0` is exactly what two random
guessers would produce; negative means they disagree *more* than random guessing.

### Worked example

A human vet and the AI judge each score 20 summaries, rounded to categories 3, 4,
or 5. Here's the tally of how their scores paired up (rows = human, columns =
judge):

|          | judge 3 | judge 4 | judge 5 | **human total** |
|----------|---------|---------|---------|-----------------|
| human 3  | 4       | 1       | 0       | 5   |
| human 4  | 1       | 6       | 1       | 8   |
| human 5  | 0       | 2       | 5       | 7   |
| **judge total** | 5 | 9   | 6       | **20** |

**Step 1 — observed agreement** (they matched on the diagonal: 4 + 6 + 5):

```
p_observed = (4 + 6 + 5) / 20 = 15 / 20 = 0.75
```

So **percent agreement = 75%.** Sounds great! But now correct for chance.

**Step 2 — agreement expected by chance.** For each category, multiply the two
raters' rates of using it, then add up:

```
p_chance = (5/20)(5/20) + (8/20)(9/20) + (7/20)(6/20)
         = 0.0625 + 0.18 + 0.105
         = 0.3475
```

(Even by pure luck, they'd match about 35% of the time.)

**Step 3 — combine:**

```
kappa = (0.75 − 0.3475) / (1 − 0.3475) = 0.4025 / 0.6525 = 0.617
```

So percent agreement of a shiny 75% shrinks to a **Kappa of 0.62** once you
remove the luck — "moderate" agreement, not the near-perfect the raw 75%
suggested. **That gap is the whole reason Kappa exists**, and why the project
reports *both*: percent agreement is the intuitive number, Kappa is the
defensible one for a methods section.

### One necessary wrinkle: Kappa needs whole-number categories

The overall jury score is an *average* of five 1-5 criteria, so it can land on
4.2, not just a whole number. Kappa can't work with 4.2 — it needs discrete
boxes — so before computing Kappa, the composite score is **rounded to the
nearest whole category** (4.2 → 4). Pearson and Spearman (Part 8) don't need
this step because they work on continuous scores directly; Kappa does.

**Where we use it:** [`llm-sum/stats_engine.py`](../../llm-sum/stats_engine.py),
via `sklearn.metrics.cohen_kappa_score`, broken out by species / study design /
journal in the covariate report (see
[`docs/phase6/reporting.md`](../phase6/reporting.md)).

---

## Part 8 — Correlation: Pearson, Spearman, and Bland-Altman

Parts 6 and 7 asked "do raters agree?" This part asks a slightly different
validation question: *"Does the AI judge's opinion **track** a human vet's
opinion — when the vet rates a summary high, does the judge too?"* Three lenses
answer it, each catching something the others miss.

Take a small paired dataset — five summaries scored by a human and by the AI
judge:

```
human:  3.0,  4.0,  4.0,  5.0,  2.0
judge:  3.5,  4.0,  4.5,  4.8,  2.5
```

### 8a. Pearson correlation — do they move together *in a straight line*?

Pearson's *r* measures how tightly the two sets of numbers fall along a straight
line. Working through the arithmetic for the data above gives **r ≈ 0.97** — a
strong positive correlation. When the human's score goes up, the judge's goes up
too, almost perfectly in step.

**How to read it:** `+1` = perfect straight-line agreement, `0` = no relationship,
`−1` = perfectly opposite. A **negative** correlation between judge and human
would be alarming — it would mean the judge ranks summaries *backwards* from the
vet.

### 8b. Spearman correlation — do they put things in the *same order*?

Spearman is just **Pearson computed on the ranks** (Part 1c) instead of the raw
scores. It asks the gentler question: do the judge and the human *rank* the
summaries in the same order, even if the relationship isn't a perfect straight
line? For our data the two rankings are nearly identical, so **Spearman ≈ 0.97**
too.

**Why prefer Spearman for validating a judge against a human?** Because we mostly
care whether the judge *orders* summaries like the vet does (best to worst), not
whether the numbers line up on an exact straight line. Spearman doesn't assume a
linear relationship, which makes it the more honest tool for "does the judge
track expert judgment?" This project treats Spearman as the headline
human-validation correlation.

### 8c. Bland-Altman — is the judge *systematically* biased?

Here's what correlation *cannot* catch: a judge that is **always 0.5 points more
generous** than the human is *perfectly correlated* (r = 1.0) yet consistently
wrong. Correlation only sees whether they move together, not whether one sits
higher. Bland-Altman catches that offset.

It looks at the **differences** (judge − human) directly:

```
diffs = 0.5, 0.0, 0.5, −0.2, 0.5
mean bias = (0.5 + 0.0 + 0.5 − 0.2 + 0.5) / 5 = 0.26
```

So the judge runs about **0.26 points more generous** than the human on average —
a bias a correlation of 0.97 completely hid. Bland-Altman also reports "limits of
agreement" (roughly, the bias ± 1.96 × the spread of the differences) bounding
where individual disagreements fall — here about **−0.40 to +0.92**.

### Why all three?

| Lens | Catches | Misses |
|---|---|---|
| Pearson | straight-line agreement | non-linear tracking; constant bias |
| Spearman | same ordering, even if non-linear | constant bias |
| Bland-Altman | constant bias / offset | — (its whole job) |

A trustworthy judge needs **all three** to look good: it tracks the human
(Spearman high), the relationship is tight (Pearson high), *and* it isn't
quietly running generous or harsh (Bland-Altman bias near zero).

> **A safety guard worth knowing:** a correlation from a tiny sample is
> unstable — one item added or removed can swing it wildly. So the code refuses
> to *interpret* a correlation built on fewer than 30 comparable items
> (`reliability.MIN_CORRELATION_N`); it still shows the number for transparency
> but withholds the "the judge tracks the vet" verdict until the sample is big
> enough to mean something.

**Where we use it:** [`llm-sum/reliability.py`](../../llm-sum/reliability.py),
via `scipy.stats.pearsonr` and `scipy.stats.spearmanr` (both validated, citable)
plus a small hand-written Bland-Altman helper.

---

## Part 9 — TF-IDF and cosine similarity (information density)

**The question it answers:** *"Does an AI summary actually carry as much
**information** as the paper's own abstract — or did it quietly drop the
important parts?"*

This directly re-runs a 2023 finding (Appleby et al.) that AI-written summaries
carried *less* information than the source abstract 79.7% of the time. To check
it, we need a way to measure "meaningful content overlap" — and that takes two
ideas working together.

### TF-IDF — scoring how *distinctive* each word is

**TF-IDF** stands for **term frequency × inverse document frequency**. It's a way
of scoring how much *information* a word carries:

- **Term frequency (TF)** — how often a word appears in *this* document. A word
  used a lot here is probably important here.
- **Inverse document frequency (IDF)** — how *rare* the word is across *all*
  documents. The formula is roughly:

  ```
  IDF(word) = log( total documents / documents containing the word )
  ```

  Common filler words ("the", "and", "is") appear in *every* document, so
  `documents containing the word` ≈ `total documents`, the ratio ≈ 1, and
  `log(1) = 0` — they score **zero**. Rare, specific words ("osteosarcoma",
  "stifle joint") appear in only a few documents, so the ratio is large and IDF
  is high.

Multiply them: **TF-IDF is high only for words that are both frequent *here* and
rare *elsewhere*** — exactly the distinctive medical terms that carry real
meaning. TF-IDF turns a document into a list of (word, importance) numbers — its
**fingerprint** of meaningful content. (scikit-learn uses a lightly smoothed
version of the IDF formula and then scales each fingerprint to unit length, but
the intuition above is exactly right.)

### Cosine similarity — comparing two fingerprints

Once the abstract and the summary are each a fingerprint (a list of numbers),
**cosine similarity** compares them and returns one number from 0 to 1: how much
do their important words overlap? It's the dot product of the two fingerprints
divided by their lengths — geometrically, the angle between them. `1.0` = same
technical content; `0` = completely unrelated.

### Worked example

Reduce two documents to three meaningful terms —
`[osteosarcoma, canine, chemotherapy]` — with these TF-IDF fingerprints:

```
abstract:  [0.8, 0.5, 0.3]
summary:   [0.7, 0.5, 0.0]     ← the summary never mentioned chemotherapy
```

```
dot product  = 0.8·0.7 + 0.5·0.5 + 0.3·0.0 = 0.56 + 0.25 + 0 = 0.81
length(abstract) = √(0.8² + 0.5² + 0.3²) = √0.98 = 0.990
length(summary)  = √(0.7² + 0.5² + 0.0²) = √0.74 = 0.860

cosine = 0.81 / (0.990 × 0.860) = 0.81 / 0.851 = 0.952
```

Cosine ≈ **0.95** — very high, so despite dropping "chemotherapy" the summary
kept almost all the technical meaning. Had it dropped more key terms, the number
would fall.

### The bands this project uses

- **cosine ≥ 0.85** → "high fidelity": kept almost all the technical meaning.
- **cosine < 0.50** → "lost key medical details."
- **in between** → "moderate."

The **0.50** line is also the headline cutoff: the percentage of summaries that
kept "similar-or-more" information, compared against Appleby et al.'s benchmarks.
These cutoffs are documented, tunable choices
(`stats_engine.INFORMATION_DENSITY_RETENTION_THRESHOLD` /
`_HIGH_THRESHOLD`), not a re-derivation of Appleby's exact formula.

### Why fit TF-IDF over the *whole corpus*, not just the two documents?

Because with only two documents, any word appearing in both gets
`documents containing the word` = 2 out of 2, its IDF collapses to ≈ 0, and it's
treated as worthless filler — even if it's "osteosarcoma," the exact meaningful
term we're trying to detect. Fitting TF-IDF once over the **whole corpus** (every
abstract and every candidate summary in the run) gives each word a *real* rarity
score based on hundreds of papers, so "importance" reflects the whole study, not
one accidental pair.

**Where we use it:** [`llm-sum/stats_engine.py`](../../llm-sum/stats_engine.py),
via `sklearn.feature_extraction.text.TfidfVectorizer` and
`sklearn.metrics.pairwise.cosine_similarity`.

---

## Part 10 — ROUGE (word-overlap recall)

**The question it answers:** *"How much of the source text's actual wording did
the summary reuse?"*

ROUGE (**Recall-Oriented Understudy for Gisting Evaluation**) is a classic,
long-standing way to score summaries automatically by counting **overlapping
runs of words** between a summary and a reference text. It's cheaper and simpler
than TF-IDF/cosine — pure word matching, no importance-weighting — which is why
this project treats it as a **secondary, auditing metric**, not the main quality
signal. (The blind jury score is always the primary signal; ROUGE is a cheap
sanity check that can be recomputed anywhere with no API calls.)

There are three flavors, all computed as **recall** here — "of the reference's
words, what fraction did the summary capture?" Recall is chosen deliberately: the
research question is *how much source content the summary keeps*, and a short
summary that correctly leaves out irrelevant detail shouldn't be punished for it.

### ROUGE-1, ROUGE-2, ROUGE-L

- **ROUGE-1** counts overlapping **single words** (unigrams).
- **ROUGE-2** counts overlapping **word pairs** (bigrams) — a stricter test,
  because it rewards keeping words *in the same order*.
- **ROUGE-L** counts the **longest common subsequence** — the longest sequence of
  words that appears in both texts in the same order, though not necessarily
  side by side. It rewards preserved phrasing without demanding exact adjacency.

### Worked example

```
reference (abstract):  "the dog received chemotherapy for osteosarcoma"
summary:               "the dog was treated with chemotherapy"
```

**ROUGE-1 (single words).** The reference has 6 words. Words the summary shares
with it: *the*, *dog*, *chemotherapy* → 3 overlaps.

```
ROUGE-1 recall = 3 / 6 = 0.50
```

**ROUGE-2 (word pairs).** The reference's 5 bigrams are: "the dog", "dog
received", "received chemotherapy", "chemotherapy for", "for osteosarcoma". The
summary shares only **"the dog"** → 1 overlap.

```
ROUGE-2 recall = 1 / 5 = 0.20
```

ROUGE-2 is much lower than ROUGE-1 because although the summary reused the same
individual words, it rephrased them into different pairings — which is exactly
what "keeping the words but reordering them" looks like.

**ROUGE-L (longest common subsequence).** The longest in-order shared sequence is
*"the dog … chemotherapy"* — length 3.

```
ROUGE-L recall = 3 / 6 = 0.50
```

### How to read ROUGE — and its limits

A **high** ROUGE means the summary copied a lot of the source's actual wording; a
**low** ROUGE means it rephrased heavily (abstractive summarizing) — which isn't
necessarily bad. That ambiguity is the key caveat: ROUGE measures *word overlap,
not correctness or meaning.* A summary can score low ROUGE while being an
excellent, faithful paraphrase, or score decent ROUGE while stitching copied
phrases into nonsense. This is precisely why ROUGE sits in the "secondary
evidence" bucket and the blind jury remains the primary judge of quality.

**Where we use it:** [`llm-sum/eval_metrics.py`](../../llm-sum/eval_metrics.py),
computed with a small dependency-free tokenizer (no external ROUGE package
needed), reported alongside other lightweight automatic metrics.

---

## Part 11 — Cost-per-quality-point

**The question it answers:** *"Which provider gives the best quality for the
money?"*

The math here is the simplest in the whole chapter — plain division — but it's
worth stating precisely because "cost" means two different things in this
project.

### The formula

```
cost-per-quality-point =  mean cost to generate one summary  ÷  mean jury score
```

**Worked example.** Suppose a provider costs **$0.012** to generate the average
summary, and its average (unweighted) jury score is **4.2**:

```
cost-per-quality-point = 0.012 / 4.2 = $0.00286
```

**Lower is better** — you're paying less per unit of quality. A provider that's
slightly cheaper *and* slightly better wins on this metric twice over; a provider
that's cheap but low-quality might still look worse than a pricier, much better
one.

### Two costs, two questions — don't mix them up

This project reports the cost-per-quality-point in **two** flavors that answer
genuinely different questions:

- **Research-budget cost** (above) — the *real* per-token API price this study
  actually paid to generate summaries, from the logged token counts. Answers
  *"what did this research run cost?"*
- **Subscription cost** — assumes a practicing vet on a flat **$20/month**
  subscription summarizing ~500 papers/month, so **$0.04/summary** regardless of
  length. Answers *"is a consumer subscription worth it to a working vet?"* Its
  cost-per-quality-point is `0.04 / 4.2 = $0.0095`.

Neither replaces the other, and the reports keep them in **separate columns** so
a reader can always tell which question a number answers.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py)
(research-budget) and
[`llm-sum/stats_engine.py`](../../llm-sum/stats_engine.py) (subscription).

---

## Part 12 — How it all fits together

Each test above answers one narrow question. Put in the right order, they tell
one coherent story:

| The question | The tool | Compares… |
|---|---|---|
| Did A beat B? | Wilcoxon signed-rank | two providers, paper by paper |
| Is anyone different at all? | Friedman | all providers at once |
| Am I fooled by running many tests? | Benjamini-Hochberg (FDR) | a family of pairwise p-values |
| How sure am I of an average? | Bootstrap confidence interval | one provider's mean |
| Do the judges agree with each other? | Krippendorff's alpha | judges (or human reviewers) |
| Do a judge and a human pick the same box? | Cohen's Kappa + percent agreement | judge vs human, categorically |
| Does the judge track a human's opinion? | Pearson / Spearman / Bland-Altman | jury scores vs human scores |
| Does the summary keep the abstract's meaning? | TF-IDF + cosine similarity | summary vs abstract |
| How much source wording was reused? | ROUGE | summary vs source, word overlap |
| Is this good value? | Cost-per-quality-point | cost ÷ quality |

**The order matters.** A healthy write-up reads: *the judges agree with each
other* (alpha is high) **and** *a human vet agrees with the judges* (strong
Spearman, near-zero Bland-Altman bias) — so the jury scores are trustworthy —
**and only then** do Friedman + Wilcoxon (BH-corrected) tell us which providers
genuinely differ on those trustworthy scores, with bootstrap intervals showing
how firm each gap is. **Reliability first, comparison second.** A significant
Wilcoxon result built on scores the judges couldn't agree on is a house built on
sand.

---

## Part 13 — One-page cheat sheet

- **p-value** — "how likely is this by luck?" Below 0.05 = probably real. Small p
  ≠ big or important, just unlikely-to-be-luck.
- **paired data** — the same papers scored by each provider, so we compare
  paper-by-paper and cancel out "some papers are just harder."
- **rank** — a score's position after sorting; robust to outliers and uneven
  scales.
- **Wilcoxon signed-rank** — *two* providers, paired: did one consistently win?
  Uses the ranks of the paper-by-paper differences.
- **Friedman** — *three or more* providers, paired: is anyone a standout? Run
  this first; it guards against false alarms from testing many pairs.
- **Bonferroni / Holm / Benjamini-Hochberg** — three ways to adjust a *family* of
  p-values so running several tests doesn't manufacture a false "win." Bonferroni
  and Holm are strict (control *any* false positive); **Benjamini-Hochberg (FDR)
  is the one this project uses** — gentler, controls the *proportion* of false
  discoveries, keeps more real findings. Read the *adjusted* p (`p (BH-adj)`) to
  call a pair significant.
- **bootstrap confidence interval** — resample your data 2000× to see how much an
  average would wobble; the middle 95% is the interval. Overlapping intervals =
  gap not yet resolved.
- **Krippendorff's alpha** — do the scorers agree? 1 = perfect, 0 = chance, below
  0 = systematic disagreement. Chance-corrected, handles missing raters. The one
  hand-rolled statistic (no library has it).
- **Cohen's Kappa** — do two scorers pick the *exact same category*, more than
  chance predicts? Needs whole-number categories (continuous scores rounded
  first). **Percent agreement** is its simpler, non-chance-corrected cousin.
- **Pearson / Spearman** — do two number sets move together? Pearson = straight
  line; Spearman = same ordering (Pearson on ranks). Spearman is the headline for
  judge-vs-human validation.
- **Bland-Altman** — catches a *constant bias* (e.g. the judge is always 0.5
  points generous) that correlation alone hides.
- **TF-IDF + cosine similarity** — does a summary's *meaningful* vocabulary (rare,
  specific terms, not filler) overlap with the abstract's? 1 = same technical
  content, 0 = unrelated. Fit over the whole corpus so word "importance" is real.
- **ROUGE** — word-overlap *recall* (ROUGE-1 words, ROUGE-2 word-pairs, ROUGE-L
  longest in-order run). Measures reused wording, not meaning — a secondary,
  auditing metric, never the primary quality signal.
- **cost-per-quality-point** — cost to make one summary ÷ its mean jury score;
  lower is better. Reported twice: real research-budget cost, and a $20/month
  consumer-subscription cost — different questions, separate columns.
- **numpy / scipy / scikit-learn** — fast number arrays; validated, citable
  statistical tests; and Cohen's Kappa + TF-IDF/cosine. Pinned in
  `requirements.txt` so every machine computes the identical result.

---

## Where to go next

- The plain-language primer this chapter expands on:
  [`docs/statistics_explained.md`](../statistics_explained.md).
- The publication tables that *use* these tests, with the exact scipy function
  names for a methods section:
  [`docs/phase6/reporting.md`](../phase6/reporting.md) (see §8).
- The code, written to be read — the docstrings in
  [`reliability.py`](../../llm-sum/reliability.py),
  [`report_tables.py`](../../llm-sum/report_tables.py),
  [`stats_engine.py`](../../llm-sum/stats_engine.py), and
  [`eval_metrics.py`](../../llm-sum/eval_metrics.py) explain each function in
  the same plain style as this chapter.
