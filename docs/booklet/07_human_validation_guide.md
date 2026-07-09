# A Guide for Veterinarian Reviewers: Scoring AI-Written Summaries

Thank you for agreeing to help with this project. This guide walks you
through everything you need to do — there is nothing to install, nothing to
code, and no special software knowledge required. If you can open a document
and fill in a spreadsheet, you have everything you need.

Read this guide once, start to finish, before you begin scoring. It should
answer every question you have along the way. You do not need to have read
anything else about this project first.

---

## 1. What we're asking you to do, and why it matters

We used an artificial intelligence (AI) system to read veterinary research
articles and write short summaries of them — the kind of plain-language
summary a busy clinician might want instead of reading a full article. Before
we can trust those summaries, we need to know one thing: **does the AI's own
judgment of "how good is this summary" line up with what a real veterinarian
would say?**

To find that out, we've asked you (and possibly one or two other reviewers)
to read a small set of articles and their AI-written summaries, and to score
each summary using your own clinical judgment — the same way you'd judge
whether a colleague's case write-up was accurate, complete, and safe to act
on. We then compare your scores to the AI's own scores for those same
summaries. If they agree, that's good evidence the AI's judgment can be
trusted more broadly. If they don't agree, that tells us something important
too — that the AI's scoring needs more work before anyone should rely on it.

Either way, **your honest opinion is the entire point.** There's no target
answer we're hoping you'll land on.

---

## 2. Why you won't be told which AI wrote which summary

You'll notice that nothing you're given says which AI system wrote a given
summary, or gives you any hint about it. This is deliberate, not an
oversight.

If you knew "this one came from a well-known AI system," that knowledge
could quietly shift your opinion of it — for better or worse — without you
even meaning it to. The whole reason your score is valuable is that it's
**independent**: it reflects only what's on the page in front of you, not
any expectation about who or what produced it. So we've deliberately kept
that information out of your hands. It isn't that we don't trust you — it's
that keeping everyone blind to authorship, including us, is the only way to
keep the comparison honest. You'll simply see an article and a summary,
labeled only with a plain reference number.

---

## 3. What you'll receive

You'll get two things:

1. **A reading document.** This contains a set of items, each labeled
   something like "item 001," "item 002," and so on. Each item has two
   parts: the full text of the original research article, and one
   AI-written summary of it. That's all — no author names, no journal name
   tricks, no hints about which AI wrote the summary.

2. **A scoring sheet.** This is a simple spreadsheet with one row for every
   item in the reading document. You'll fill in your scores and notes for
   each item, one row at a time.

You will **not** receive any file that reveals which AI wrote which summary.
If you're ever sent a file besides these two, or you accidentally come
across one, please don't open it — just let us know, and set it aside.
That file exists purely so we can match everything back up on our end after
all reviewers have finished; it plays no role in your scoring.

---

## 4. How to work through an item

Take the items one at a time, in order. For each one:

1. **Read the original article first**, just as you normally would when
   evaluating a piece of veterinary literature — enough to form your own
   sense of what it actually found and concluded.
2. **Then read the AI-written summary** of that same article.
3. **Ask yourself: if a colleague handed me this summary instead of the
   article, would it serve me well — and could it mislead me?**
4. **Fill in that item's row on the scoring sheet** before moving to the
   next item. Scoring one item at a time, right after reading it, works
   better than reading everything first and scoring from memory later.

There's no time limit and no required pace. Some items will be quick to
score; others — especially ones where something feels a little off — may be
worth a second read.

---

## 5. Filling in the scoring sheet

Each row on the scoring sheet has several columns. Here's what each one
means, described in terms of the everyday clinical judgment you already use
every day — not in technical language.

### The five 1-to-5 scores

For each of these, give a whole number from 1 (poor) to 5 (excellent).
Think of the scale the way you'd think about grading a student's or a
colleague's case summary:

| Column | Ask yourself | 1 means | 5 means |
|---|---|---|---|
| **Faithfulness** | Does everything the summary claims actually appear in the article? | It states things the article doesn't say, or contradicts it | Everything in it is backed up by the article |
| **Completeness** | Does it capture the important findings, or leave out something a clinician would need to know? | Major findings are missing | The key findings are all there |
| **Clinical usefulness** | If a busy clinician or researcher only read this summary, would it actually help them? | Not useful in practice | Genuinely useful, like a good colleague's summary |
| **Clarity** | Is it clearly written and easy to follow? | Confusing or poorly organized | Clear and easy to read in one pass |
| **Safety** | Could anything stated — or left out — lead someone to make a poor clinical decision? | Could genuinely mislead a reader in a way that matters clinically | Nothing in it could reasonably mislead anyone |

Score each of these independently. A summary can be beautifully written
(high clarity) while still stating something the article never said (low
faithfulness) — those are different questions, and it's fine, expected even,
for your five numbers on one item to differ from each other.

If you genuinely can't judge one of the five for a particular item, it's
fine to leave that one cell blank rather than guess — a blank is treated
honestly as "not scored," while a guessed number would be treated as your
real opinion.

### The two hallucination columns

We use the word **"hallucination"** to mean: the summary states something as
fact that the original article does not actually support — in other words,
a made-up or unsupported claim.

- **Hallucination present (yes/no):** Did you notice anything like that in
  this summary? Just yes or no.
- **Hallucination notes:** If yes, briefly say what it was, in your own
  words. A sentence or two is plenty — you don't need to write an essay.

**A concrete example** of what this might look like: suppose the original
article reports that a treatment was tested in a small pilot study with no
statistically significant result, but the AI-written summary describes it as
"proven effective." That's a hallucination — the summary is asserting a
conclusion the article doesn't actually support. You'd mark
`hallucination_present` as "yes" and, in the notes, write something like:
*"Summary says the treatment was 'proven effective'; the article only
reports a small non-significant pilot study — this overstates the finding."*
That kind of overstatement would also likely lower your faithfulness (and
possibly safety) score for that item.

### The comment column

Anything else worth flagging that doesn't fit neatly into the boxes above —
a stylistic issue, something you found impressive, a point you're unsure
about — goes here. It's optional, but genuinely useful to us, so don't be
shy about using it.

### A filled-in example row

To make all of this concrete, here's what one completed row might look
like, for a made-up item about a study on a pain medication in dogs:

| Column | Example entry |
|---|---|
| faithfulness | 4 |
| completeness | 3 |
| clinical_usefulness | 4 |
| clarity | 5 |
| safety | 4 |
| hallucination_present | no |
| hallucination_notes | *(blank)* |
| comment | Well written and easy to follow. Missed mentioning the small sample size, which I'd want flagged for a clinical reader. |

Notice that this reviewer gave high marks for how it's written and how
useful it would be, but knocked a point off completeness because something
clinically relevant (the small sample size) was left out — exactly the kind
of nuanced, independent judgment we're asking for.

---

## 6. When you're finished

Once you've scored every item on the sheet, send the completed scoring sheet
back the way you received it. You don't need to send anything else, and
please don't open or forward the private matching file mentioned in
Section 3, if you happen to have received it alongside your materials — it
isn't meant for reviewers and isn't something you need to look at.

That's it. There's nothing further for you to do — we'll take it from there.

---

## 7. One last thing

There is no wrong answer here, and this is not a test of you. We are not
checking your work against some hidden correct score — we are asking for
your honest, independent clinical judgment, exactly as you'd give it to a
colleague. If two reviewers score the same summary differently, that's
useful information too, not a mistake either of you made.

Thank you again for your time and expertise — this kind of independent
review is the only way we can honestly say whether this AI system's
judgment can be trusted.
