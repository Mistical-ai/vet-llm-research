# Chapter 3 — Getting AI Summaries

**Who this is for:** a technical beginner who has never touched this
pipeline before. No assumed knowledge of the project beyond Chapters 1-2,
but comfortable running a command in a terminal.

**What you'll be able to do after reading this:** understand why this
project asks three different AI companies to summarize the same paper
under identical conditions, what a "structured schema" is and why one is
enforced, the three different ways a paper's text can be fed to an AI
model, the safety switches that stop this step from accidentally spending
money, and what to expect on screen when a real summarization run happens.

---

## 1. Why three providers, under identical conditions

Chapter 2 left you with clean article text sitting in `data/processed/`.
This step — **summarization** — sends that text to three different AI
companies (a **provider**, in this project's terminology, is one of
OpenAI, Anthropic, or Gemini) and asks each one to produce a structured
clinical summary of the paper.

The project's real research question is "which provider writes the best
veterinary summaries?" — but that question is only answerable if every
provider is compared on a **level playing field**. If OpenAI received a
more detailed prompt than Anthropic, or a longer excerpt of the paper, any
difference in summary quality could be an artifact of the *prompt*, not
the *provider*. So the pipeline deliberately holds everything else fixed
and varies only the provider:

- the same article text,
- the same instructions (the **prompt** — the block of text telling the
  AI model what to do),
- the same output structure (see section 2),
- the same generation settings (`temperature=0` and a fixed `seed=42`,
  wherever a provider supports them — settings that make a model's output
  as reproducible as possible run to run),
- the same maximum output length.

By default all three providers are handed the **exact same prompt file**,
`llm-sum/prompts/summarization_v1.txt` — this is controlled by a setting
called `PROMPT_MODE`, which defaults to `shared`. There is also a
`provider_specific` mode that loads a separate prompt file per provider
(`summarization_openai_v1.txt`, `summarization_anthropic_v1.txt`,
`summarization_gemini_v1.txt`) for deliberate prompt-engineering
experiments — but `shared` is the mode that produces the project's
fair-comparison baseline, and it's what you should assume unless a run is
explicitly labeled otherwise.

**Why isolate provider quality from prompt quality this way?** Without
this discipline, a result like "Anthropic scored higher than OpenAI" would
be scientifically meaningless — a critic could always ask "did you just
write a better prompt for Anthropic?" Holding the prompt, schema, and
settings fixed means any score difference that shows up later (Chapter 4)
can be attributed to the model itself.

---

## 2. The structured summary schema

Left alone, three different AI models will format an answer three
different ways — one might write flowing paragraphs, another might use
bullet points, and a third might skip a detail the researcher actually
needed (like sample size) entirely. That would make the summaries almost
impossible to compare side by side.

To prevent this, every provider is required to fill in the same
**schema** — a fixed set of named fields with defined types, called
`VeterinarySummary` in this project's code. A schema is enforced using
each provider's own "structured output" feature (OpenAI's native parsed
completions, Gemini's native JSON schema output, and a forced tool call
for Anthropic) rather than just asking nicely in the prompt — so a
malformed or incomplete response is caught immediately instead of slipping
through as an oddly-shaped summary.

The fields every provider must fill in are:

| Field | What goes here |
|---|---|
| `headline` | One sentence with the most clinically important takeaway. |
| `objective` | The research question, objective, or hypothesis. |
| `study_design` | The reported design, or `"Not reported"`. |
| `species` | The animal species or population studied. |
| `sample_size` | Number of animals/samples/records/cases analyzed, or `null` if not reported — the prompt explicitly forbids writing `0` unless the article says zero. |
| `key_methods` | A short list of the main methods, interventions, measurements/outcomes assessed, statistical analysis approach, or comparisons. |
| `key_findings` | A short list of the most important findings — the prompt asks for a key number only when it's essential to understanding a result, rather than trying to capture every statistic in the article. |
| `clinical_significance` | How a practicing clinician should interpret or apply the findings. |
| `limitations` | Important caveats — the prompt specifically calls out missing confirmatory diagnostics (e.g. EEG, histopathology, imaging) as a limitation worth surfacing on its own, separate from sample-size caveats. |
| `summary_text` | Short plain-language prose for a busy clinician, under 400 words (target 300-380), in a fixed section order: title/authors, Background, Methods, Results, Limitations, Conclusions. Flowing paragraphs only — no numbered headers, no bullet points, no bold. |

Every one of these rules exists to stop the model from **inventing**
information: if a fact isn't in the article, the prompt tells the model to
write `"Not reported"` rather than guess. This matters enormously for
Chapter 4, where a blind judge specifically checks summaries for exactly
this kind of invented content.

Each summarized paper ends up with **two** representations stored side by
side:

- **`summary`** — the readable prose from `summary_text`. This is what the
  blind judge in Chapter 4 actually reads and scores.
- **`structured_summary`** — the full schema as a clean dictionary
  (`headline`, `sample_size`, `key_findings`, and so on), kept for later
  analysis — for example, checking whether summaries tend to omit sample
  size more often for one provider than another.

If a provider returns a response that's missing a field or otherwise
doesn't quite fit the schema, the code repairs it by filling the gap with
a safe placeholder (`"Not reported"` or an empty list) — it never invents
a value to paper over a gap, because that would be exactly the kind of
fabrication the whole schema exists to prevent.

---

## 3. Three ways to feed a paper to the AI — and the six-summary comparison

The summarizer can send a paper to a provider three different ways,
controlled by an `--input-source` setting:

| `--input-source` | What gets sent | Role |
|---|---|---|
| `processed` (default) | The processed text from `data/processed/*.jsonl` (Chapter 2) | The scientifically preferred input — full article body, references and publisher boilerplate already removed. |
| `raw_text` | The raw text from `data/raw_text/*.jsonl` (Chapter 2) | Tests what happens if the model sees the paper *before* cleaning — references, watermark noise, and all. |
| `pdf` | The original PDF file from `data/raw/*.pdf`, sent directly | Tests whether a provider's own built-in PDF reading does better or worse than this project's own extraction (Chapter 2). |

`raw_text` and `pdf` exist for **comparison**, not for the main study —
the project's default, scientifically preferred path is `processed`.
Sending the raw PDF directly is deliberately restricted to `test` and
`single` modes (see section 4): it's a real-time, provider-specific call
whose cost can't be forecast ahead of time the way text input can, so it's
kept to small, deliberate one-paper checks rather than allowed into a
250-paper batch run.

**The six-summary comparison.** Running the `summarize-all` command for
one matched article (a paper that exists as both a PDF and a processed
JSONL file) produces six summaries total: the same three providers each
summarize the `processed` text, and then the same three providers each
summarize the `pdf` directly:

```text
processed JSONL → OpenAI summary
processed JSONL → Anthropic summary
processed JSONL → Gemini summary

direct PDF      → OpenAI summary
direct PDF      → Anthropic summary
direct PDF      → Gemini summary
```

This lets the project answer questions like "is it worth the extra
complexity of this project's own PDF-cleaning pipeline (Chapter 2), or
would just handing providers the raw PDF work just as well?" — a question
about cost and quality, not just about which provider is best.

---

## 4. The optional human-written style guide (and its one hard rule)

If you want every provider's summary to *look* a certain way — a
particular section order, tone, or level of detail — you can paste one
example summary you've written yourself into
`llm-sum/prompts/guide_summary_template.txt`. When that file is left
blank (its default state), nothing changes. When it has text, the
summarizer wraps it in warning language and inserts it into the prompt
sent to every provider.

**The one hard rule: format only, never facts.** The wrapper text
explicitly tells the model to copy only the guide's structure — section
names, order, tone, level of detail — and explicitly forbids copying the
guide's species, diseases, treatments, numbers, outcomes, or conclusions
into a summary of a *different* paper.

**Why this rule is non-negotiable for the study's validity:** the whole
point of summarizing 250 different papers is to see how each provider
handles 250 different sets of facts. If a model were allowed to lift
actual clinical facts from one example guide summary into unrelated
papers, every summary would be quietly contaminated by whatever happened
to be in that one example — a single hard-coded case report bleeding into
summaries of papers about completely different species and conditions.
Keeping the guide "format-only" preserves the guarantee that every fact in
every summary came from that summary's own source article.

---

## 5. `PHASE3_MODE` — the safety switch that protects your budget

Summarization is the first step in this pipeline that can cost real
money, so it's governed by the same single safety switch introduced for
all of Phase 3: **`PHASE3_MODE`**, set in the project's `.env` file (a
plain-text settings file, kept out of version control because it also
holds private API keys — see this project's `CLAUDE.md` rules).

| Mode | Real API calls? | Papers processed | Confirmation required? |
|---|---|---|---|
| `test` | **No** — fake, deterministic mock summaries | All | No |
| `single` | Yes | 1 (or one matched PDF/JSONL pair for `summarize-all`, six summaries) | Yes — you must type `yes` |
| `dev` | Yes, budget-capped | A small configurable number (default 5) | Yes |
| `batch` | Yes, at a discount | The full corpus (~250 papers) | Yes |

Three safety facts are worth trusting, because they're enforced in code,
not just convention:

1. **`test` mode physically cannot call a paid API**, no matter what else
   is configured — it's the backstop of last resort.
2. **Every paid mode stops and asks you to type `yes`** before the very
   first real call goes out. This confirmation prompt is the last
   guardrail before any money is spent, and it's why this booklet — like
   this project's `CLAUDE.md` rules — never instructs you to run a paid
   summarization command yourself. That decision, and that keypress, stays
   with you, executed manually in your own terminal window.
3. **A typo in the mode name falls back to `test` and warns you** rather
   than guessing and potentially spending money on an unintended mode.

There's a second layer underneath `PHASE3_MODE` worth knowing about:
`BudgetGuard`, a running total that every paid API call must pass through.
If cumulative spending would cross a configured hard-stop dollar amount,
the run refuses to continue — a backstop independent of which mode you're
in.

---

## 6. Real-time vs. batch — two ways to actually send the requests

Underneath any paid mode, requests reach the providers one of two ways:

- **Real-time.** One request goes out, and the answer comes back
  immediately — the same shape of interaction as chatting with an AI
  assistant. This is what `test`, `single`, and `dev` modes use, and it's
  the only way direct-PDF input works.
- **Batch.** Many requests are bundled together, uploaded to the provider
  as a job, and processed over the following hours — this is what `batch`
  mode uses for OpenAI and Anthropic (Gemini's batch API isn't wired into
  this project, so Gemini always runs real-time, even during a `batch`
  run). A separate command, `check_batch_status.py`, is run later to
  collect the finished results once the provider's job completes.

**Why batch for the full run?** Two reasons: batch pricing is roughly
**half the price** of real-time pricing for the providers that support
it, and a 250-paper run submitted as one batch job can be started before
you leave for the day and collected the next morning, rather than tying up
a terminal window for hours making one request after another. The
tradeoff is that batch results aren't immediate — you wait, then collect —
which is exactly why `test`/`single`/`dev` stay real-time: those modes
exist for quick, interactive checks where you want an answer in seconds,
not a job you check back on later.

---

## 7. What you'd see on screen (a worked walkthrough)

The example below shows what a **`single`-mode** real-time run looks
like — this is a paid, real command, so it is shown here only as an
illustration of the output you'd see if you (not an AI assistant) ran it
yourself in PowerShell, after deciding you're ready to spend a small
amount to sanity-check a prompt change:

```text
[phase3] mode=single | limit=1 | real-time | confirm-required
[phase3:safety] About to submit REAL real-time API calls (limit=1).
  Type 'yes' to confirm: yes
[phase3:summarize] paper 1: 10.1111/jvim.16872
  openai: success    (in=4521, out=487, ver=gpt-5.4-0325-preview, $0.0223)
  anthropic: success (in=4521, out=475, ver=claude-sonnet-4-6-20250901, $0.0214)
  gemini: success    (in=4612, out=482, ver=gemini-3.5-flash, $0.0165)
[phase3:summarize] done. counts={'success': 3, 'failed': 0, ...} budget_spent=$0.0602
```

A few things worth noticing in that output:

- The very first line, `[phase3] mode=... | ...`, always prints before
  anything else happens — if you don't see it, the command didn't
  actually start. It's a quick way to confirm which mode you're really
  in before anything gets charged.
- Each provider line reports the **exact model version string** returned
  by the provider (e.g. `gpt-5.4-0325-preview`), not just the alias you
  requested (`gpt-5.4`). Providers can silently update what a given alias
  points to over time; recording the precise version lets the project
  detect that kind of drift between two runs that were supposed to be
  identical.
- `in=4521, out=487` are **exact token counts read from the provider's
  own response**, not estimated — this is what the cost figure next to
  it (`$0.0223`) is calculated from, using the per-token prices in
  `llm-sum/models_config.py`, the one file that holds every provider's
  current pricing.
- Before spending anything, the same command can be run with an
  `--estimate` flag instead, which tokenizes the cached text offline and
  prints a projected bill with zero API calls — cheap insurance against a
  surprise charge, and safe to run freely.

**What happens in `test` mode instead:** the exact same code path runs,
but every provider call is replaced with a deterministic fake summary
(printed as `[MOCK ...]`), and the reported cost is always `$0.00`. This
means every wiring problem — a broken file path, a malformed prompt, a
missing field — surfaces for free before you ever risk spending money in
`single`, `dev`, or `batch` mode.

---

## 8. Where things end up (quick reference)

```text
data/summaries.jsonl        one row per (paper, input source); each row holds
                             all three providers' summaries and structured data
data/summaries_pdf/*.txt    readable summarize-all reports for direct-PDF input
data/summaries_txt/*.txt    readable summarize-all reports for processed-text input
data/batch_jobs.jsonl       one row per submitted batch job (batch mode only)
data/error_log.jsonl        any summarization failure, logged with its DOI and stage
```

`data/summaries.jsonl` follows the same JSONL format explained in
Chapter 2, and for the same reasons: successful provider results are
merged back into the matching row as they complete, so a run that crashes
partway through never loses the work it already finished — the next run
simply resumes.

**What's next:** three providers have now each produced a structured
summary of every paper. The next chapter explains how a fourth AI model —
a **blind judge**, one that is never told which provider wrote which
summary — scores every one of those summaries for accuracy and quality,
and the exact formula it uses to turn five separate scores into a single
number.
