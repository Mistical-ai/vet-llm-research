# Chapter 2 — From PDF to JSONL

**Who this is for:** a technical beginner who has never touched this
pipeline before. No assumed knowledge of the project, but comfortable
running a command in a terminal.

**What you'll be able to do after reading this:** understand what JSONL is
and why this entire project is built around it, how a PDF becomes
machine-readable text, why the project keeps two separate copies of that
text, and how to check whether the extraction actually worked before
spending any money on summaries.

---

## 1. What JSONL is, and why this project uses it everywhere

**JSONL** stands for "JSON Lines." A regular JSON file holds one big
structure — often one big list of objects, wrapped in `[` and `]`. A JSONL
file instead holds **one independent JSON object per line**, with no
wrapping brackets and no commas between lines:

```jsonl
{"doi": "10.1111/jvim.16872", "title": "Paper A"}
{"doi": "10.1111/jvim.16873", "title": "Paper B"}
{"doi": "10.1111/jvim.16874", "title": "Paper C"}
```

Every stage of this pipeline reads and writes JSONL:
`data/manifest.jsonl`, `data/raw_text/*.jsonl`, `data/processed/*.jsonl`,
`data/summaries.jsonl`, `data/evaluations.jsonl`. That's not a coincidence
— it's a deliberate, repeated design choice, for three reasons:

1. **Crash safety.** If a script writing a regular JSON array crashes
   halfway through, the closing `]` never gets written and the *entire*
   file is corrupt — even the records that finished successfully are lost,
   because a JSON parser can't read a file that isn't syntactically
   complete. A JSONL file has no such single point of failure: every line
   that was fully written before the crash is still a complete, valid,
   independently-readable record. Only the one line still being written
   when the crash happened is ever at risk.
2. **Append-only audit trails.** Adding a new record to a JSONL file is
   just adding a new line — no need to read the whole file, parse it,
   insert into a list, and rewrite everything. This matters for files like
   `data/evaluations.jsonl`, which is meant to only ever grow, never be
   silently rewritten (see this project's `CLAUDE.md` rules on output
   safety).
3. **Partial-corruption tolerance.** If one line somehow gets mangled (a
   truncated write, a stray character), every *other* line in the file is
   still perfectly readable. A tool reading the file just skips the one
   bad line instead of refusing to open the whole file.

The tradeoff is that JSONL isn't a single structured document the way a
normal JSON file is — you can't hand it to most tools that expect one big
JSON blob. In exchange you get a format that degrades gracefully instead
of catastrophically, which matters a great deal for a pipeline that runs
unattended for hours downloading, extracting, or calling paid APIs.

Every later chapter in this booklet will mention JSONL files without
re-explaining the format — this is the one place it's taught in full.

---

## 2. The extract step: turning a PDF into text

Once Phase 2 (Chapter 1) has filled `data/raw/` with PDFs, the next command
turns each PDF into machine-readable text:

```powershell
python llm-sum/run_phase3.py extract
```

This runs `llm-sum/prepare_texts.py`, which walks every PDF in
`data/raw/` directly (not the manifest — see the note below) and produces
**two** JSONL files per PDF, not one:

```text
data/raw/jvim__title__10_1111_jvim_16872.pdf
        │
        ├──► data/raw_text/jvim__title__10_1111_jvim_16872.jsonl   (raw extraction)
        │
        └──► data/processed/jvim__title__10_1111_jvim_16872.jsonl  (processed text)
```

Notice the filenames: the JSONL cache file has **the exact same name** as
the PDF it came from, just with `.jsonl` instead of `.pdf`. That's
intentional — you can glance at a filename in `data/processed/` and
immediately know which PDF it was extracted from, with no lookup table
required. (Section 3 below explains why this matters.)

**Why walk the PDFs directly instead of using the manifest
(`data/manifest.jsonl`) to decide what to process?** The manifest can
contain duplicate entries, or rows for papers that were never actually
downloaded. Walking the filesystem guarantees exactly one output pair per
PDF that actually exists on disk — the manifest is only consulted
afterward, to enrich each output with its DOI and journal metadata.

### Getting text out of the PDF itself

Extracting readable text from a PDF is harder than it sounds, because a
PDF has no real concept of "paragraphs" — it only knows where individual
characters sit on a page. The project uses two extraction tools, tried in
order:

1. **pymupdf4llm** (primary) — converts a PDF into structured Markdown in
   one pass. It uses spatial analysis of where text blocks sit on the page
   to figure out the correct reading order, which matters enormously for
   **two-column journals** like JVIM and JAVMA. A naive extractor reads
   left-to-right across the whole page width, which — on a two-column
   page — scrambles sentences by jumping between the left and right
   columns mid-line. pymupdf4llm detects the columns and reads each one
   top-to-bottom before moving to the next, the way a human eye actually
   reads a two-column page.
2. **pdfplumber** (fallback) — used only if pymupdf4llm can't open a
   particular file. It also has a "preserve text flow" mode
   (`PDFPLUMBER_USE_TEXT_FLOW`, on by default) aimed at the same
   two-column problem, though it's a less capable tool than pymupdf4llm
   for this specific job.

### Cleaning the text

The raw extraction is not what gets sent to the cleaned cache as-is —
first it goes through two cleanup passes:

1. **Publisher watermark removal (`clean_publisher_noise`).** Wiley
   (the publisher behind several of this project's target journals)
   embeds a repeating "Downloaded from ... Creative Commons ... License"
   footer on every single page. Because it repeats once per page, in a
   long paper it can account for more than half of the raw extracted
   characters — badly distorting word counts if left in. This is removed
   with a pattern match before anything else happens.
2. **References section removal (`remove_references_section`).** The
   bibliography at the end of a paper is stripped out, because it adds
   length without adding any content an AI summarizer needs. This uses a
   plain text-pattern search (a "regex," short for regular expression — a
   pattern that matches text shapes rather than exact words) looking for
   headings like "References" or "Bibliography," rather than an AI model
   — the heading is structurally consistent enough across journals that a
   free, instant, deterministic pattern match works reliably, and it costs
   nothing. The code specifically looks for the **last** such heading in
   the document, not the first, because a paper's Methods section might
   say something like "see references 3–5" — matching on the first
   occurrence would accidentally throw away everything from Methods
   onward.

**No character limit is applied at this stage.** The cache holds the
**full** cleaned paper body, however long that is. A later stage (the
summarizer, covered in Chapter 3) applies its own length limit when it
actually builds a prompt to send to an AI model — keeping that limit
separate from extraction means it can be tuned without having to
re-extract every PDF.

---

## 3. Why keep both `raw_text` and `processed` — provenance

It would be simpler to only save the final processed text and throw away the
raw extraction. The project deliberately keeps both, because of **data
provenance** — the ability to answer "where did this exact piece of text
come from?" for any output, all the way back to the original PDF.

If a summary later looks wrong or strange, you can trace it backward
through every stage:

```text
summary → processed JSONL → raw_text JSONL → original PDF → manifest DOI
```

Keeping `raw_text` specifically lets you answer a narrower but important
question: **did the cleaning step itself introduce the problem?** If a
paper's processed text looks truncated or garbled, comparing it against
`raw_text` immediately tells you whether pymupdf4llm/pdfplumber extracted
the PDF badly, or whether the reference-removal regex ate too much. Without
`raw_text`, you'd have no way to tell those two failure modes apart short
of re-extracting the PDF from scratch.

`raw_text` also has a secondary research use: Chapter 3 explains that the
project sometimes deliberately summarizes the **raw text** instead of the
processed text, specifically to measure whether cleaning improves summary
quality.

---

## 4. Checking the extraction actually worked

A PDF can pass every one of Phase 2's validation checks (Chapter 1) and
still get mangled during extraction — for example, if the references-regex
matches the word "References" somewhere in the middle of the Discussion
section and throws away the second half of the paper. Before paying for
any AI summaries, run:

```powershell
python scripts/verify_extraction.py
```

This makes **no API calls** — it's free and safe to run as often as you
like. For every PDF, it independently re-extracts the raw text and
compares it against the cached processed text in `data/processed/`, then
prints one of three statuses:

| Status | Rule | Meaning |
|---|---|---|
| `PASS` | cleaned word count ≥ 3,000 **and** cleaned/raw word ratio ≥ 0.50 | Extraction and cleaning behaved as expected. |
| `WARN` | cleaned word count 1,500–2,999, **or** ratio 0.30–0.49 | Could be aggressive cleaning, or a genuinely short paper (short communication, case report) — worth a manual glance. |
| `FAIL` | cleaned word count < 1,500, **or** ratio < 0.30, **or** no cache found, **or** raw word count < 500 | Something is broken: an image-only PDF, a cleaning step that ate the article body, or extraction never ran for this file. |

**Why is a "healthy" ratio only 0.50–0.90 and not closer to 1.0?**
Removing references and publisher watermarks is *supposed* to shrink the
text — typically by 15–25%, since bibliographies and boilerplate are a
real (if unhelpful) chunk of a paper's raw character count. A rule that
demanded, say, 90% of the raw text survive cleaning would incorrectly flag
almost every healthy paper as broken. 0.50 is a deliberately conservative
floor: it only fires when something has gone wrong, not whenever cleaning
does its normal job.

The exit code matters too: `0` if there are zero `FAIL` rows, `1` if there
is at least one — so this check can gate a script or a manual "should I
proceed" decision without you having to read the whole table by eye.

**Example run:**

```text
PS> python scripts/verify_extraction.py --quiet
PDF filename                                                  pages      raw    clean  ratio  status
------------------------------------------------------------------------------------------------------
vru__short_communication__10_1111_vru_13322.pdf                   3     1820     1107   0.61  WARN  (ratio 0.61, cleaned 1107 words)
javma__editorial_response__10_2460_javma_99001.pdf                2      612      298   0.49  FAIL  (cleaned words 298 < 1500)

Summary: PASS=243  WARN=6  FAIL=1  (total 250)

Report written to data/verify_extraction_report.txt
```

The `FAIL` row above is an editorial that should have been filtered out
back in Phase 2's article-type gate (Chapter 1, section 1) — it slipped
through, and this check is what catches it before it wastes money on a
summary. `--quiet` only prints `WARN`/`FAIL` rows to the screen (the full
table, including every `PASS` row, is still written to
`data/verify_extraction_report.txt`).

---

## 5. A worked example: what a processed JSONL row looks like

Here is a representative (shortened) row from
`data/processed/*.jsonl`, with the `text` field truncated for readability
— the real field holds the full cleaned article body, tens of thousands of
characters long:

```json
{
  "doi": "10.2460/javma.22.12.0596",
  "slug": "10_2460_javma_22_12_0596",
  "text": "Age at gonadectomy, sex, and breed size affect risk of canine overweight...",
  "word_count": 6426,
  "char_count": 105325,
  "pdf_filename": "javma__age_at_gonadectomy_sex_and_breed_size...__10_2460_javma_22_12_0596.pdf",
  "pdf_source": "data/raw/javma__age_at_gonadectomy_sex_and_breed_size...pdf",
  "input_source": "processed",
  "extracted_at": "2026-06-01T18:12:25.220464+00:00"
}
```

A few fields worth noticing:

- **`slug`** is a filesystem- and API-safe version of the DOI (every `/`,
  `:`, and `.` replaced with `_`). This is the stable key used to join a
  paper's text, its summaries, and its evaluations back together later in
  the pipeline — the on-disk filename is descriptive for humans, but
  `slug` is what the code actually matches on.
- **`input_source`** records whether this particular row is `"processed"`
  (cleaned) or `"raw_text"` (uncleaned) — the same field name appears in
  the matching `data/raw_text/*.jsonl` row, just with the other value.
- **`word_count`** and **`char_count`** are exactly the numbers
  `verify_extraction.py` (section 4) and the cost estimator (Chapter 3)
  read — they're computed once here and reused everywhere downstream
  rather than recomputed from `text` each time.

---

## 6. Where things end up (quick reference)

```text
data/raw_text/*.jsonl    raw, uncleaned extraction — kept for provenance/comparison
data/processed/*.jsonl   processed text — the default input for AI summarization
data/verify_extraction_report.txt   PASS/WARN/FAIL audit table, overwritten each run
```

**What's next:** the processed text sitting in `data/processed/` is ready to
be sent to an AI model. The next chapter explains how three different AI
providers each summarize the same paper under identical conditions, and
why the project is built to compare them fairly.
