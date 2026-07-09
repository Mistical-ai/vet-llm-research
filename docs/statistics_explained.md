# Statistics, explained for newcomers

**Who this is for:** anyone working on this project who sees words like
*Wilcoxon*, *Friedman*, *Krippendorff's alpha*, *scipy*, or *numpy* and wants to
actually understand what they mean and why we use them — no statistics
background assumed. Read top to bottom; each idea builds on the last.

This project compares how well three AI providers (OpenAI, Anthropic, Gemini)
summarize veterinary papers. To make claims a scientific paper can defend, we
need to answer three questions with *numbers*, not vibes:

1. **Did provider A really beat provider B?** → the **Wilcoxon** test
2. **Across all three providers at once, is anyone different?** → the **Friedman** test
3. **Do our scorers (the AI judges, or human vets) agree with each other?** → **Krippendorff's alpha**

And two tools do the heavy lifting: **numpy** and **scipy**. Let's start there.

---

## Part 0 — What are numpy and scipy?

**numpy** is a Python library for working with lists of numbers *fast*. Plain
Python can add up a list, but numpy is built for crunching thousands or millions
of numbers efficiently, and gives you tidy "arrays" (think: a spreadsheet column
that knows how to do math on itself). Almost every scientific Python tool sits on
top of numpy.

**scipy** is built *on top of* numpy and adds ready-made scientific tools. The
part we use is `scipy.stats` — a big box of **pre-written, peer-reviewed
statistical tests**. Instead of writing the Wilcoxon test ourselves (and risking
a subtle bug), we call `scipy.stats.wilcoxon(...)` and trust an implementation
that thousands of scientists have checked.

**Why does this matter for us?**

- **Trust & citability.** A reviewer of your paper trusts `scipy.stats.wilcoxon`
  far more than home-made math. You can cite scipy.
- **Correctness at small samples.** scipy computes *exact* answers when you only
  have a handful of data points — which is often our situation early on.
- **Reproducibility.** We *pin* specific versions in
  [`requirements.txt`](../requirements.txt) (`scipy>=1.11`, `numpy>=1.23`), so
  every computer runs the identical math and gets the identical result.

> **The one exception:** Krippendorff's alpha (question 3) is written by hand in
> [`llm-sum/reliability.py`](../llm-sum/reliability.py) — *not* because we prefer
> home-made math, but because scipy simply doesn't include it. Everything else
> uses scipy.

---

## Part 1 — Vocabulary you need first

Just four ideas. Once these click, the tests are easy.

### The p-value (the single most important idea)

A **p-value** answers: *"If there were truly no real difference, how likely is it
that I'd see a difference this big just by luck?"*

- **Small p-value** (by convention, **below 0.05**) → "this would be a surprising
  fluke, so the difference is probably **real**." We call that *statistically
  significant*.
- **Large p-value** (say 0.5) → "this could easily happen by chance, so I can't
  claim a real difference."

Think of flipping a coin. If it lands heads 6 times out of 10, that's not
surprising (large p) — you wouldn't conclude the coin is rigged. If it lands
heads 60 times out of 100, *that* is surprising (small p), and you'd start to
believe the coin is genuinely biased. **A p-value is just that "how surprising is
this?" number, made precise.**

> ⚠️ A small p-value says a difference is *probably real*; it does **not** say the
> difference is *large or important*. "Real but tiny" is possible. Always read the
> p-value next to the actual size of the difference.

### "Paired" data

Our providers all summarize **the same papers**. So when we compare OpenAI vs
Anthropic, we can line them up **paper by paper**: on paper 1, OpenAI scored 5
and Anthropic scored 4; on paper 2, 3 vs 3; and so on. Data that lines up like
this is **paired**. Pairing is powerful: instead of "is OpenAI's average higher?"
we can ask the sharper question "did OpenAI beat Anthropic *on the same paper*,
more often than not?" — which cancels out the fact that some papers are just
harder to summarize than others.

### Ranks

Instead of using raw scores, many tests use their **rank** — their position when
you sort them. Scores `[3, 5, 4]` have ranks `[1, 3, 2]` (smallest = rank 1).
Why bother? Ranks are **robust**: one weird outlier can't distort them, and they
don't assume the 1–5 scale is perfectly evenly spaced. Tests built on ranks are
called *non-parametric*, which just means "they don't assume the data follows a
neat bell curve." Our 1–5 scores are lumpy and small, so rank-based tests are the
safe, honest choice.

### Correlation (for later, in reliability)

A **correlation** measures whether two sets of numbers move together. `+1` =
perfectly in step, `0` = unrelated, `−1` = perfectly opposite. We use it to ask
"when a human vet rates a summary high, does the AI jury also rate it high?"

---

## Part 2 — The Wilcoxon signed-rank test

**The question it answers:** *"Did provider A really beat provider B, on the same
papers — or could the gap be luck?"*

**The everyday version.** Imagine two chefs each cook the same 10 dishes, and a
taster scores every dish. For each dish you note **who won and by how much**.
Chef A wins most dishes by a bit? Chef A is probably genuinely better. They trade
wins randomly with tiny margins? Probably a tie. That's exactly Wilcoxon.

**How it works, in words:**

1. For each paper, compute the **difference** (A's score − B's score).
2. Throw away ties (difference of 0 — that paper picks no winner).
3. **Rank** the differences by size, ignoring direction, then add up the ranks
   for A's wins vs B's wins.
4. If those two totals are lopsided, one provider is consistently winning →
   small p-value. If they're balanced, it's a wash → large p-value.

**Why not just compare the two averages?** Because an average hides consistency.
Provider A could have a higher average purely because of one paper it aced, while
losing most others. Wilcoxon looks at the **pattern of paper-by-paper wins**, so
it can't be fooled that way. It also uses ranks, so it's robust to our lumpy 1–5
scale.

**Tiny example.** Differences on 6 papers: `+1, +1, +2, +1, 0, +1`. Drop the `0`.
Every remaining paper favors A. Five wins, zero losses — very unlikely by chance
→ small p-value → "A really did beat B."

**Where we use it:** [`llm-sum/report_tables.py`](../llm-sum/report_tables.py),
for every head-to-head pair of providers, via
`scipy.stats.wilcoxon(..., method="auto")`. `method="auto"` tells scipy to use
the **exact** calculation when the sample is small — the very situation where a
rougher shortcut would be least trustworthy.

---

## Part 3 — The Friedman test

**The question it answers:** *"Looking at all three providers at once, is at least
one of them genuinely different from the others?"*

**The everyday version.** Now three chefs cook the same 10 dishes. Before you dive
into "A vs B, A vs C, B vs C," you ask one gatekeeper question: *is anyone
standing out at all?* Friedman is that gatekeeper. It's the "3-or-more-groups"
cousin of Wilcoxon.

**How it works, in words:** for **each paper**, rank the three providers (best =
3, worst = 1). If one provider keeps grabbing the top rank across many papers,
its rank total will tower over the others → small p-value → "yes, someone's
different." If the ranks are all jumbled paper to paper, everyone's rank total
ends up similar → large p-value → "no clear standout."

**Why run it *before* the pairwise Wilcoxon tests?** Because testing many pairs
separately is like buying many lottery tickets — run enough comparisons and one
will look "significant" by pure chance. Friedman is a single up-front check: if it
says "nobody differs," you treat any individual pair that looks exciting with
healthy suspicion. It guards against fooling yourself.

**The output** is a number called a **chi-square statistic** plus a p-value. You
don't need to interpret the chi-square number itself — just read the p-value the
same way as always (small = a real standout exists).

**Where we use it:** [`llm-sum/report_tables.py`](../llm-sum/report_tables.py),
via `scipy.stats.friedmanchisquare(...)`, computed over the papers that **all**
providers scored (so the ranking is fair).

---

## Part 4 — Krippendorff's alpha

**The question it answers:** *"Do our scorers agree with each other — enough that
we can trust the scores?"*

This is a different *kind* of question. Wilcoxon and Friedman compare
**providers**. Krippendorff's alpha checks the **measuring instrument itself**.
Our "instrument" is a scorer: usually an AI judge, or (in human validation) a
veterinarian. If we run several judges/reviewers over the same summaries and they
wildly disagree, then *any* score is shaky and every comparison built on it is
suspect. Alpha puts a number on that agreement.

**The everyday version.** Three judges score the same gymnastics routines. If they
hand out nearly identical scores, the scoring is **reliable** — you can trust it.
If judge 1 says 9, judge 2 says 4, judge 3 says 7 for the same routine, the
scoring is noise. Alpha measures how close to "identical" they are.

**The scale (this is all you need to remember):**

| Alpha | Meaning |
|---|---|
| **1.0** | Perfect agreement — scorers are interchangeable. |
| **0.80+** | Strong; safe to draw firm conclusions. |
| **0.667–0.80** | Acceptable; tentative conclusions only. |
| **0** | Agreement no better than random guessing. |
| **below 0** | Worse than random — scorers systematically *disagree*. |

**Why not just "what % of the time did they give the exact same score?"** Two
reasons, and they're the reason alpha exists:

1. **Luck inflates raw agreement.** On a 1–5 scale, two people who guess randomly
   will still match ~20% of the time by pure chance. Alpha **subtracts out** the
   agreement you'd expect from luck, so `alpha = 0` honestly means "no better than
   coin-flips," not "0% overlap."
2. **A 4-vs-5 is a near-miss; a 1-vs-5 is a blowup.** Simple "% identical" treats
   both as merely "not equal." Alpha knows the scores are *ordered numbers*, so it
   counts a 4-vs-5 as a small disagreement and a 1-vs-5 as a big one.

It also gracefully handles **missing data** — e.g. your vet only reviews a subset
of the summaries. Alpha uses whatever overlap exists instead of forcing you to
throw away rows.

**Where we use it:** [`llm-sum/reliability.py`](../llm-sum/reliability.py) — for
agreement between **AI judges** (when you run a 3-judge jury) and, reusing the
exact same engine, agreement between **human reviewers**. As noted above, this one
is hand-written because scipy doesn't provide it; it's the *only* hand-rolled
statistic in the project.

---

## Part 5 — How it all fits together

| The question | The tool | Compares… | Lives in |
|---|---|---|---|
| Did A beat B? | Wilcoxon signed-rank | two providers, paper by paper | [`report_tables.py`](../llm-sum/report_tables.py) |
| Is anyone different at all? | Friedman | all providers at once | [`report_tables.py`](../llm-sum/report_tables.py) |
| Do the scorers agree? | Krippendorff's alpha | judges (or human reviewers) | [`reliability.py`](../llm-sum/reliability.py) |
| Does the AI jury track a human vet? | correlation + p-value | jury scores vs human scores | [`reliability.py`](../llm-sum/reliability.py) |

A healthy story reads like this: *the judges agree with each other* (alpha is
high), *and* the human vet agrees with the judges (strong correlation) — so the
scores are trustworthy — *and then* Friedman + Wilcoxon tell us which providers
genuinely differ on those trustworthy scores. Reliability first, comparison
second.

---

## Part 6 — One-page cheat sheet

- **p-value** — "how likely is this by luck?" Below 0.05 = probably real. Small p
  ≠ big/important, just unlikely-to-be-luck.
- **paired data** — same papers scored by each provider, so we compare
  paper-by-paper.
- **rank** — a score's position after sorting; robust to outliers and uneven
  scales.
- **Wilcoxon signed-rank** — *two* providers, paired: did one consistently win?
- **Friedman** — *three+* providers, paired: is anyone a standout? (Run this
  first; it guards against false alarms from testing many pairs.)
- **Krippendorff's alpha** — do the scorers agree? 1 = perfect, 0 = chance, below
  0 = systematic disagreement. Chance-corrected, handles missing data.
- **correlation** — do two number sets move together? +1 / 0 / −1.
- **numpy** — fast number arrays; the foundation.
- **scipy** — validated, citable statistical tests built on numpy; we use
  `scipy.stats`.

## Where to go next

- The publication tables that *use* Wilcoxon and Friedman:
  [`docs/phase6/reporting.md`](phase6/reporting.md).
- Reliability and human validation, where alpha and correlation live:
  [`docs/phase5/human_validation.md`](phase5/human_validation.md).
- The code, which is written to be read — the docstrings in
  [`reliability.py`](../llm-sum/reliability.py) and
  [`report_tables.py`](../llm-sum/report_tables.py) explain each function in the
  same plain style as this doc.
