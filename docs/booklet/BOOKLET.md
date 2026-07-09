# The Booklet: A Complete Guide to the Veterinary-LLM Research Pipeline

This is the full, merged version of the project's teaching booklet — every
chapter in one document, in reading order, with consistent terminology and
one working table of contents.

**Who this is for.** Chapters 1 through 6 are written for a **technical
beginner**: someone comfortable running a command in a terminal, but who has
never seen this specific pipeline before and has no assumed statistics
background. Chapter 7 shifts audience entirely — it's written for a
**non-technical veterinarian reviewer** with zero coding background, and it
uses no code, file paths, JSONL, or pipeline jargon anywhere. Every chapter
defines its technical terms on first use, so you can also read any single
chapter on its own without having read the others first.

**Chapter 7 is also a standalone document.** It's written to work as a
printable handout that a veterinarian reviewer can read start to finish with
no other context — see the note at the start of that chapter.

For the terminology and tone conventions this booklet follows, see
[`00_style_guide.md`](00_style_guide.md).

---

## Table of Contents

1. [Chapter 1 — Gathering the Data](#chapter-1--gathering-the-data) — how the 250-paper corpus is collected and downloaded (Phase 2).
2. [Chapter 2 — From PDF to JSONL](#chapter-2--from-pdf-to-jsonl) — what JSONL is, why it's used everywhere, and how PDFs become processed text (Phase 3 extract).
3. [Chapter 3 — Getting AI Summaries](#chapter-3--getting-ai-summaries) — how three LLM providers summarize each paper under identical conditions (Phase 3 summarize).
4. [Chapter 4 — The Blind AI Judge](#chapter-4--the-blind-ai-judge) — how a blind judge scores summaries and the jury-score formula (Phase 3 evaluate).
5. [Chapter 5 — Reading Your Results](#chapter-5--reading-your-results) — what `eval-report` and the publication tables actually show.
6. [Chapter 6 — The Statistics Behind the Numbers](#chapter-6--the-statistics-behind-the-numbers) — every formula (Wilcoxon, Friedman, bootstrap, Krippendorff's alpha, Cohen's Kappa, TF-IDF/cosine, ROUGE), taught from scratch.
7. [Chapter 7 — A Guide for Veterinarian Reviewers](#chapter-7--a-guide-for-veterinarian-reviewers-scoring-ai-written-summaries) — zero-jargon standalone handout for a veterinarian reviewer, also usable as a printable document on its own.

---


## Chapter 1 — Gathering the Data

**Who this is for:** a technical beginner who has never touched this
pipeline before. No assumed knowledge of the project, but comfortable
running a command in a terminal.

**What you'll be able to do after reading this:** understand what the
"corpus" (the paper collection) is, why it's built the way it is, and how to
run the four Phase 2 commands that build it — plus how to read their output
and fix the most common problems.

---

### 1. The goal: 250 papers, five journals, balanced

This project studies how well AI models summarize **veterinary research
papers**. Before any AI touches a single paper, the project needs an actual
collection of papers to study — this collection is called the **corpus**.

The target corpus is:

| Journal | Full name | Target |
|---|---|---|
| JVIM | Journal of Veterinary Internal Medicine | 50 papers |
| JAVMA | Journal of the American Veterinary Medical Association | 50 papers |
| Veterinary Surgery | Veterinary Surgery | 50 papers |
| VRU | Veterinary Radiology & Ultrasound | 50 papers |
| JFMS | Journal of Feline Medicine and Surgery | 50 papers |

**250 papers total, 50 per journal, published between 2023-01-01 and
2026-12-31.**

**Why exactly 50 per journal instead of "however many we can get"?** If the
corpus just took whatever was easiest to download, it would be lopsided
toward whichever journal happens to have the most freely-downloadable
papers. That would quietly bias every later comparison — imagine if 200 of
the 250 papers came from one journal with an unusual writing style. Equal
quotas per journal keep the later statistical comparisons balanced and fair
across the different sub-fields these five journals represent (internal
medicine, general practice, surgery, imaging, feline medicine).

**Why this date range?** 2023 through the end of 2026 captures recent
papers, including very fresh ones — many journals now require papers to be
freely available (open access) as soon as they're accepted, so even a paper
from a few months ago is often already downloadable.

#### Primary vs. secondary papers

Not every PDF that gets downloaded counts toward the 50-per-journal quota:

- **Primary** papers are original research articles — a study someone
  actually ran. These count toward the quota.
- **Secondary** papers are systematic reviews or meta-analyses — articles
  that summarize *other* papers rather than reporting new research. These
  are kept (they get a `2_` prefix on their filename) but don't count toward
  the 250 target.

**Article types excluded entirely:** case reports, short communications,
brief reports, and imaging-diagnosis notes. These are real articles, but
they're short, narrowly-scoped write-ups rather than full studies — including
them would let the corpus fill up with thin, low-substance papers instead of
the full-length research articles the study actually needs to compare AI
summarization against.

---

### 2. Step 1 — Build the candidate list (`collect.py`)

```powershell
python src/collect.py
```

This is the very first command in the whole pipeline. It doesn't download
any PDFs yet — it just builds a list of **candidate** papers (a candidate is
a paper that *might* end up in the corpus, before anyone has checked whether
a free PDF exists for it).

#### Where the list comes from: CrossRef

**CrossRef** is a free, non-profit registry that assigns every academic paper
a **DOI** (Digital Object Identifier — a permanent link/ID for a paper, like
`10.1111/jvim.16872`). `collect.py` asks CrossRef's public API for every
paper published in each of the five target journals between 2023 and 2026.

**Why CrossRef instead of PubMed or a paid service like Scopus?** CrossRef
needs no paid license and no API key, and it covers all five target journals
with clean, structured metadata (title, authors, abstract, publication date).
PubMed is missing coverage for some of these journals (especially the
surgery-focused ones), and Scopus requires a subscription this project
doesn't have.

#### What gets filtered out immediately

CrossRef returns a lot of things that aren't actually research articles —
corrections, retractions, "Table of Contents" listing pages, editorials,
letters to the editor, book reviews. `collect.py` checks each candidate's
title against a list of patterns and throws these out before they ever touch
the manifest. It also recognizes and discards the excluded article types
(case reports, short communications, etc.) at the title level, the same
categories described in section 1.

#### Building a bigger pool than you need

For a 50-PDF-per-journal target, `collect.py` actually collects around
**2,000 candidates per journal** (configurable via `COLLECT_CANDIDATES_PER_JOURNAL`
in `.env`). That's not a mistake — most candidates will never turn into a
downloaded PDF, because many of them are paywalled. Collecting a large pool
now gives the next step (downloading) plenty of room to find the ones that
*are* freely available, without needing to re-query CrossRef later.

**Why not just query CrossRef fresh every time you need more candidates?**
Because CrossRef's rate limits and the time cost of re-querying make it much
more efficient to gather a generous list once and then work through it.

#### Spreading candidates across years

Collection is also **year-balanced**: rather than grabbing the 2,000 newest
papers (which would be dominated by whichever year had the most publishing
activity), the candidate quota is split evenly across 2023, 2024, 2025, and
2026 (the years in `COLLECT_YEARS`). This keeps the corpus from silently
skewing toward one recent year.

#### Guessing at species, study design, and clinical topic

While it builds the list, `collect.py` also takes a cheap, free first guess
at three pieces of information about each paper, just from keywords in its
title and abstract:

- **Species** — e.g. "dog", "canine", "puppy" → tagged `Canine`.
- **Study design** — e.g. "retrospective", "case series" → tagged
  `Retrospective Case Series`.
- **Clinical topic** — e.g. "tumor", "lymphoma" → tagged `Oncology`.

**Why keyword matching instead of asking an AI to guess?** This step runs
over thousands of candidates before any PDFs even exist — it needs to be
free and instant, not an AI API call. If the keywords can't confidently
determine all three fields, the row is flagged `needs_manual_review: true`
rather than silently guessing wrong. A wrong guess here would quietly corrupt
later analysis (e.g. counting a paper as "Feline" when it wasn't); an honest
"I'm not sure" flag lets a researcher fix it by hand later instead.

#### The output: `data/manifest.jsonl`

Everything `collect.py` finds gets written to `data/manifest.jsonl` — one
line per candidate paper, containing its DOI, title, year, authors, abstract,
journal, and the guessed covariates above.

**Why JSONL (one JSON object per line) instead of one big file?** If the
collection process crashes halfway through (network hiccup, rate limit,
whatever), a JSONL file loses nothing — every line already written is a
complete, independent, valid record. The next chapter explains JSONL in full
detail, since it's used at every stage of this project, not just here.

**Running `collect.py` again adds more candidates — it never deletes.** If
you want a completely fresh list, delete `data/manifest.jsonl` first.

**Example output:**

```text
[collect] Live mode -- querying CrossRef for 5 journals (2023-01-01 to 2026-12-31).
[collect] Collecting up to 2000 candidates per journal (download quota: 50 PDFs per journal).

[collect] Fetching 'JVIM' (ISSN 1939-1676)...
[collect] JVIM: 2000 candidates  (download target: 50; years: 2023: 500, 2024: 500, 2025: 500, 2026: 500; skipped: 143)
...
[collect] Done. Total candidates written to manifest: 9847
```

---

### 3. Step 2 — Download open-access PDFs (`download.py`)

```powershell
python src/download.py
```

This is where `collect.py`'s candidate list turns into actual PDF files.
`download.py` goes through every candidate DOI and tries, in order, several
**legal, open-access-only** sources:

1. **Unpaywall** — a database that tracks legally free copies of papers.
2. **PubMed Central (PMC)** — NIH's free biomedical archive.
3. **Europe PMC** — a European mirror/companion to PMC.
4. **Semantic Scholar** — an academic search index with its own OA links.
5. **`fulltext-article-downloader`** — a helper command-line tool, tried as a fallback.
6. **Publisher-direct URLs** — trying the publisher's own predictable PDF link pattern (Wiley, AVMA, SAGE — the three publishers behind these five journals).
7. **HTML scraping** — looking inside a paper's public landing page for a PDF link.

**This never bypasses a paywall, login, or subscription.** If a paper isn't
legally free, the downloader gives up on it and moves on — it does not try
to circumvent access controls.

#### Why disguise requests as a browser?

Some publisher websites (Wiley, AVMA, SAGE) block requests that don't look
like they're coming from a normal web browser, even for papers that *are*
freely available — this is generic bot-blocking, not a paywall. So
`download.py` sends realistic browser-style request headers, the same thing
that happens automatically when a person opens the paper in Chrome. This
does **not** unlock anything that wasn't already free; a login-only page
still gets rejected by the next check.

#### Five checks every downloaded file must pass

A file arriving from any of those sources isn't accepted just because it
downloaded successfully. Every candidate PDF must pass:

1. **Starts with the `%PDF` magic bytes.** ("Magic bytes" are the first few
   bytes of a file that reveal its true type — a real PDF always starts with
   the four characters `%PDF`.) This matters because some servers return an
   HTML login page while *claiming* to be a PDF; this check catches that
   lie before the file is ever saved.
2. **At least 3 pages.** Filters out one-page flyers, abstracts-only stubs,
   and similar non-articles.
3. **Has extractable text.** A scanned image of a page contains no text a
   computer can read out — this check makes sure the PDF isn't just a
   picture.
4. **Matches at least 3 of 5 standard article sections** (Introduction,
   Methods, Results, Discussion, Conclusion — checked by recognizing common
   synonyms for each, not exact header text). A real research article has
   this structure; something that doesn't is probably not a full paper.
5. **At least 3,000 words remain after the References section is removed.**
   This is the single most important gate: it stops a short article with a
   long bibliography from *looking* long enough by word count alone. Only
   the actual article body counts.

A file that fails any of these checks is rejected and the reason is written
to `data/error_log.jsonl`, so you can look up exactly why any specific paper
didn't make it in.

#### After acceptance: sorting primary vs. secondary vs. excluded

Once a PDF passes those five checks, `download.py` reads its first couple of
pages looking for the article-type label veterinary journals usually print
near the title (e.g. "Case Report", "Systematic Review"):

- **Excluded** (case report, short communication, etc.) → the file is
  deleted; it never enters `data/raw/`.
- **Secondary** (systematic review, meta-analysis) → kept, renamed with a
  `2_` prefix, doesn't count toward the 50-per-journal quota.
- **Primary** → kept as-is, counts toward the quota.

#### Being a polite, rate-limited downloader

Several safety mechanisms keep this step from hammering servers or running
forever:

- A short pause between API requests (`DOWNLOAD_DELAY_SECONDS`, default 2
  seconds) so services like Unpaywall and Semantic Scholar aren't overloaded.
- A longer, randomized pause before requests to publisher websites
  (`PUBLISHER_DELAY_MIN/MAX_SECONDS`, default 10–25 seconds) — publisher
  sites are more sensitive to bot-like traffic than API services are.
- A stop-loss: after 300 consecutive failures for one journal
  (`MAX_FAILED_PER_JOURNAL`), the downloader gives up on that journal for
  this run rather than grinding through a mostly-paywalled journal forever.
- If Cloudflare (or a similar bot-blocking wall) is detected repeatedly for
  one journal, remaining candidates for that journal are routed straight to
  the "needs manual download" list instead of retried automatically.

#### The output

```text
data/raw/*.pdf          — accepted PDFs (your actual corpus)
data/error_log.jsonl    — every rejection, with a reason
data/missing_papers.csv — regenerated automatically if a journal falls short
```

**Example output:**

```text
[download] Downloading candidates for 'JVIM'...
  [x] 10.1111/jvim.99213 -- HTTP_403 (publisher CDN block)
  [validation] Rejected 10.1111/jvim.88410: TEXT_TOO_SHORT (1,842 words; minimum 3,000).
  [ok] 10.1111/jvim.16872 -> data/raw/jvim__pre_illness_diet__10_1111_jvim_16872.pdf
...
[download] WARNING: JVIM shortfall — 41/50 OA PDFs found, 9 paper(s) need manual supplementation.
```

---

### 4. Step 3 — Check your progress (`pipeline.py`)

```powershell
python pipeline.py
```

`pipeline.py` is the **corpus scoreboard**. It doesn't download or process
anything — it reads `data/manifest.jsonl`, `data/manual_manifest.jsonl` (see
below), and everything currently sitting in `data/raw/`, then prints a
per-journal status table.

It merges **two sources** into one view of the corpus:

1. **OA-downloaded papers** — anything `download.py` fetched automatically.
2. **Manually supplemented papers** — anything a researcher added by hand
   (see step 5 below).

If the same DOI somehow appears in both, the automatically-downloaded record
wins, since it has more complete, consistently-formatted metadata.

**Exit codes matter here**, because this command is meant to be checkable by
a script, not just read by a human:

- **Exit code 0** — at least 200 primary PDFs found (out of the 250 target).
  This is considered "good enough to proceed," since perfect 250/250 OA
  coverage is unrealistic — some papers will always need manual
  supplementation.
- **Exit code 1** — fewer than 200 primary PDFs. Not a crash, just "keep
  going."

**Example output:**

```text
====================================================================
  CORPUS STATUS
====================================================================
  Manifest entries (OA):              9847
  Manifest entries (manual):             6
  Total unique DOIs:                  9853
  PDFs confirmed — primary:            214  (count toward quota)
  PDFs confirmed — secondary (2_):      12  (reviews; not in quota)
  PDFs confirmed — total:              226
  PDFs still missing:                   24
  Corpus target (primary only):        250
  Primary quota progress:              214  (214/250)

  Journal                    Target   Primary  Secondary  Status
  ----------------------------------------------------------------
  JVIM                            50        41          3  NEED 9 MORE PRIMARY
  JAVMA                           50        50          2  ✓ OK
  Veterinary Surgery              50        47          1  NEED 3 MORE PRIMARY
  VRU                             50        38          4  NEED 12 MORE PRIMARY
  JFMS                             50        38          2  NEED 12 MORE PRIMARY
====================================================================

[pipeline] OA corpus acceptable: 214 >= 200 threshold.
[pipeline] 36 primary papers still needed (250 target).
```

---

### 5. Step 4 — Fill the gaps manually

Not every paper is legally free to download automatically — many are behind
a subscription paywall that only a library card (or the corresponding
author) can get past. `src/supplement.py` handles the "what's still
missing, and what should I do about it" question.

```powershell
python src/supplement.py
```

For every journal under quota, it prints a shopping list: DOI, title, and a
**reason** the automatic downloader couldn't get it (e.g. "CLOUDFLARE_BLOCKED
— try UoG library proxy or author email", or "No OA version found"). It also
writes the same information to `data/missing_papers.csv`.

**Why does it shuffle the list each time?** So that running it again gives
you a different slice of the missing papers to work on, rather than always
showing the same ones first. Pass `--seed 42` (or any fixed number) if you
need the exact same shuffle twice — for example to hand a reproducible list
to a librarian.

#### Getting the actual PDFs

For each paper on that list, the two legal paths are:

1. Check University of Guelph library access (`https://lib.uoguelph.ca`) —
   many "missing" papers are simply behind a subscription the university
   already pays for.
2. Email the corresponding author and ask for a preprint or accepted
   manuscript copy — a normal, legal, and common practice in academia.

Once you have a PDF (from either path), the easiest way to bring it into the
corpus is to drop it into `data/incoming_manuals/` and run:

```powershell
python src/auto_ingest_workflow.py
```

**What this one command does automatically**, in order:

1. **Retries anything in `data/incoming_manuals/../manual_inbox/failed/`** —
   a PDF might have failed last time only because its DOI wasn't in the
   manifest *yet*; retrying after enrichment can succeed the second time.
2. **Moves your new PDFs** from `data/incoming_manuals/` into a staging
   folder (`data/manual_inbox/`).
3. **Looks up each PDF's DOI in CrossRef** and adds any missing entries to
   `data/manifest.jsonl` — this matters because a manually downloaded PDF
   often doesn't have a matching manifest row yet.
4. **Matches, renames, and moves** each accepted PDF into `data/raw/`,
   following the exact same naming convention `download.py` uses.
5. **Re-runs `pipeline.py`** so you immediately see the updated scoreboard.
6. **Re-runs `supplement.py`** so `data/missing_papers.csv` reflects the
   papers you just added.
7. **Archives** anything that still failed, by date, so the evidence isn't
   lost — nothing is silently deleted.

**A PDF whose DOI can't be found:** if a manual PDF has no DOI in its
metadata, filename, or first page, it can't be automatically matched. Rename
the file to include the DOI (e.g. `10.1177_1098612X231170159.pdf`) and
re-run the workflow.

#### The full manual-supplementation loop

```text
python src/download.py               → try more OA sources
python src/supplement.py             → see the updated missing list
[download PDFs from the library or an author email]
python src/auto_ingest_workflow.py   → add the manual PDFs
python pipeline.py                   → check the scoreboard
```

Repeat until `pipeline.py` shows 250/250 primary PDFs (or you decide the
corpus is good enough to proceed with what you have — 200+ is already an
acceptable minimum).

---

### 6. Troubleshooting

| Problem | Likely meaning | What to do |
|---|---|---|
| A PDF in `incoming_manuals/` ended up in `manual_inbox/failed/` | Something about matching or validation failed | Search `data/error_log.jsonl` for the filename or DOI, then re-run `auto_ingest_workflow.py` — failed PDFs are retried automatically. |
| Error log says `pdfplumber metadata read failed` | The PDF is a scanned image or corrupted | Get a text-based PDF instead of a scan. |
| Error log says `CrossRef 404` | The DOI doesn't exist in CrossRef | Double-check the DOI is correct on crossref.org. |
| Error log says `No DOI found` | The PDF has no DOI anywhere findable | Rename the file to embed the DOI, e.g. `10.1177_1098612X231170159.pdf`. |
| `pipeline.py` exits with code 1 | Fewer than 200 primary PDFs so far | Not an error — keep collecting/downloading/supplementing. |
| `supplement.py` shows the same missing papers every time | You're not passing a fresh shuffle | It shuffles randomly by default each run — check you're not accidentally passing the same `--seed` every time. |
| A journal keeps failing with Cloudflare/403 errors | That publisher's CDN is blocking automated downloads | Route those papers through manual supplementation (library access or author email) instead of retrying automated downloads. |

---

### 7. Where things end up (quick reference)

```text
data/manifest.jsonl          candidate + accepted papers (from collect.py)
data/manual_manifest.jsonl   manually supplemented papers (same schema)
data/raw/                    accepted PDFs — your actual corpus
data/incoming_manuals/       drop new manually-downloaded PDFs here
data/manual_inbox/           staging area auto_ingest_workflow.py manages
data/quarantine/             PDFs later found to be excluded article types
data/missing_papers.csv      what's still needed (rewritten each supplement run)
data/error_log.jsonl         every failure, with a reason, never deleted
```

**What's next:** those PDFs in `data/raw/` are just files on disk — the next
chapter explains how they get turned into clean, machine-readable text
(JSONL), and why JSONL is used everywhere in this project, not just for the
manifest.


---


## Chapter 2 — From PDF to JSONL

**Who this is for:** a technical beginner who has never touched this
pipeline before. No assumed knowledge of the project, but comfortable
running a command in a terminal.

**What you'll be able to do after reading this:** understand what JSONL is
and why this entire project is built around it, how a PDF becomes
machine-readable text, why the project keeps two separate copies of that
text, and how to check whether the extraction actually worked before
spending any money on summaries.

---

### 1. What JSONL is, and why this project uses it everywhere

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

### 2. The extract step: turning a PDF into text

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

#### Getting text out of the PDF itself

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

#### Cleaning the text

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

### 3. Why keep both `raw_text` and `processed` — provenance

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

### 4. Checking the extraction actually worked

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

### 5. A worked example: what a processed JSONL row looks like

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

### 6. Where things end up (quick reference)

```text
data/raw_text/*.jsonl    raw, uncleaned extraction — kept for provenance/comparison
data/processed/*.jsonl   processed text — the default input for AI summarization
data/verify_extraction_report.txt   PASS/WARN/FAIL audit table, overwritten each run
```

**What's next:** the processed text sitting in `data/processed/` is ready to
be sent to an AI model. The next chapter explains how three different AI
providers each summarize the same paper under identical conditions, and
why the project is built to compare them fairly.


---


## Chapter 3 — Getting AI Summaries

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

### 1. Why three providers, under identical conditions

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

### 2. The structured summary schema

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
| `key_methods` | A short list of the main methods, interventions, or comparisons. |
| `key_findings` | A short list of the most important findings — the prompt specifically instructs the model to keep exact numbers (percentages, thresholds, durations) rather than compressing them into vague phrases like "significant reduction." |
| `clinical_significance` | How a practicing clinician should interpret or apply the findings. |
| `limitations` | Important caveats — the prompt specifically calls out missing confirmatory diagnostics (e.g. EEG, histopathology, imaging) as a limitation worth surfacing on its own, separate from sample-size caveats. |
| `summary_text` | Readable prose, under 400 words, in a fixed section order: Objective, Key Methods, Primary Results, Clinical Significance, Limitations. |

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

### 3. Three ways to feed a paper to the AI — and the six-summary comparison

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

### 4. The optional human-written style guide (and its one hard rule)

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

### 5. `PHASE3_MODE` — the safety switch that protects your budget

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

### 6. Real-time vs. batch — two ways to actually send the requests

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

### 7. What you'd see on screen (a worked walkthrough)

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

### 8. Where things end up (quick reference)

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


---


## Chapter 4 — The Blind AI Judge

**Who this is for:** a technical beginner. You don't need to have read the
earlier chapters. This chapter explains how the project turns three
AI-written summaries into scores you can trust — and, just as importantly,
why you should trust them.

---

### Where we are

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

### 1. Why the judge is "blind"

#### The problem: self-preference bias

Large language models have a documented habit: **when a model is asked to grade
its own output, it tends to score it higher than a neutral grader would.** This
is called *self-preference bias*. It's not malice — it's a statistical tilt in
how models recognize and reward text that looks like their own.

For a study whose whole point is to compare three providers fairly, that tilt is
poison. If the judge knew "this summary was written by OpenAI," any preference —
for or against — would contaminate the score. The comparison would no longer
measure summary quality; it would measure the judge's opinion of brand names.

#### The fix: blind judging

**Blind judging** means the judge never sees which provider wrote the summary it
is scoring. It sees only two things:

1. The **processed text** of the real article (the answer key).
2. The **candidate summary** (the thing being graded).

It does *not* see: the provider name, the model name, or even the words
"OpenAI," "Anthropic," "Gemini," "GPT," or "Claude." The provider's identity is
attached to the score afterward, as metadata, once grading is already done.

#### How blinding is *enforced*, not just intended

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

### 2. The rubric: five things the judge scores

The judge doesn't produce one holistic "good/bad" verdict. It scores **five
separate criteria**, each on a **1-to-5 scale**, where:

- **1** = poor
- **3** = acceptable but imperfect
- **5** = excellent
- **2 and 4** are used when a summary lands between those anchors.

Here are the five criteria, in plain language, with the judge's scoring guide.

#### Faithfulness — "is it *true* to the article?"

Does everything the summary claims actually come from the article? A summary
that invents a treatment effect, a species, a sample size, or a statistical
result can mislead a veterinarian even if it reads beautifully.

- **5** — Every clinically meaningful claim is supported by the article.
- **3** — Mostly supported, with minor unsupported wording or overgeneralization.
- **1** — Important claims contradict the article or aren't supported by it.

#### Completeness — "does it cover the standard pieces?"

A good research summary should touch the six elements a clinician expects:
objective/research question, study design and methods, species and sample size,
key results, clinical significance, and limitations.

- **5** — All six elements are present.
- **3** — Four or five elements are present.
- **1** — Three or fewer elements are present.

#### Clinical usefulness — "can a vet actually *use* this?"

A summary can be technically accurate yet too vague to help in practice. Species
context matters enormously here: a finding in cats should not be silently
presented as if it applies to dogs, horses, or "animals" in general.

- **5** — Species and clinical context are clear early, with an actionable take-away.
- **3** — Partly useful, but vague, delayed, or missing some species context.
- **1** — A clinician wouldn't get a reliable interpretation.

#### Clarity — "is it readable and organized?"

Confusing summaries slow interpretation and can bury important caveats. Note
that clarity is deliberately weighted *lowest* of the five (more on weights
below) — a clear-but-wrong summary is more dangerous than a clunky-but-correct
one.

- **5** — Concise, organized, readable, non-repetitive.
- **3** — Understandable, with minor flow, length, or repetition issues.
- **1** — Disorganized, confusing, or substantially repetitive.

#### Safety — "could it mislead clinical interpretation?"

The catch-all clinical-risk criterion: wrong species, wrong intervention, wrong
outcome, exaggerated certainty, or a missing caveat — anything that could change
how a veterinarian reads the paper.

- **5** — No misleading clinical interpretation and no species-generalization risk.
- **3** — Minor wording could be misunderstood but is unlikely to change practice.
- **1** — The summary could mislead a clinician.

The judge returns each score together with a one-sentence reason, so a human can
later see *why* it scored the way it did.

---

### 3. Hallucinations: catching made-up facts

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

#### The four categories

- **`fabricated_statistics`** — a number the article never gives (an invented
  p-value, sample size, or percentage).
- **`omitted_caveat`** — the summary states a finding but drops a limitation or
  qualifier the article insisted on.
- **`contradiction`** — the summary says the opposite of what the article says.
- **`unsupported_inference`** — the summary reaches a conclusion the article
  doesn't actually support (e.g. generalizing a canine-only result to all
  animals).

#### Two severities, and why `major` triggers a human

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

### 4. The jury score: turning five numbers into one

This is the heart of the chapter. The judge has handed back five criterion
scores. How do those become the single **jury score** everyone quotes?

#### Who does the arithmetic — and why it isn't the AI

**The judge model does not calculate the final score.** It only returns the five
criterion scores (plus its hallucination evidence and confidence). **Python does
the math** afterward. Three reasons this matters:

- LLMs occasionally make arithmetic mistakes, especially with weighted formulas.
  A calculator never does.
- The *exact same* formula then runs identically on every paper, every provider,
  and every re-run — no drift.
- Anyone auditing the study can open one Python file and verify the math by hand.
  It's fully transparent.

#### Two scores are *always* computed, not one

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

#### What a weighted average is

A **weighted average** simply means some items count more than others. Think of a
course grade where the final exam is worth 40% and homework is worth 10% — not
every assignment pulls the average equally. Here, faithfulness and safety pull
the jury score harder than clarity does.

#### The weights, and the reasoning behind each

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

#### The formula, step by step

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

#### A fully worked example

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

#### The scale's endpoints (a sanity check)

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

### 5. Weighted vs. unweighted: when each one is right

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

### 6. One judge, or a panel? Judge disagreement

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

#### What "judge disagreement" means in plain English

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

### 7. Run manifests: proving *how* a score was produced

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

### 8. What a finished evaluation looks like

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

### Recap

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


---


## Chapter 5 — Reading Your Results

**Who this is for:** a technical beginner who has just run (or is about to
look at the output of) an evaluation. You don't need to have read the
earlier chapters, though Chapter 4 (how the jury score is computed) is the
closest relative to this one.

**What you'll be able to do after reading this:** know which command to run
to see your results, understand what each table and artifact is actually
telling you, and be able to explain the two or three most important tables
out loud without getting lost in the statistics. This chapter deliberately
does **not** teach how the underlying math works — that's Chapter 6. Here,
the goal is navigation: what am I looking at, and what does it mean?

---

### Where we are

By this point, `data/evaluations.jsonl` has rows in it — one row per
(paper, provider) pair that a **judge** (an AI model whose job is to score a
summary; see Chapter 4) has scored. Each row carries a **jury score** (the
1-to-5 number summarizing how good one summary is — Chapter 4 covers exactly
how it's computed) plus hallucination evidence, confidence, and metadata.

That file is an append-only log, not a report. This chapter is about the
tools that turn it into something a human can actually read: `eval-report`
for a quick operator snapshot, and `eval-report --publication` for the
deeper tables a paper or a stratified veterinary analysis needs.

---

### 1. Two commands, two audiences

This project has two different reporting commands, aimed at two different
moments:

| Command | Answers | Audience |
|---|---|---|
| `python llm-sum/run_phase3.py eval-report` | "How did each provider do, broken down by species/journal/etc.?" | You, right after a run, checking things look sane. |
| `python llm-sum/run_phase3.py eval-report --publication` | "Which provider is best, is the difference statistically real, and what does quality cost?" | A methods section, a stratified analysis, a paper. |

Both are **read-only and offline** — they only read `data/evaluations.jsonl`
(and a couple of companion files) and never call a provider API. They're
safe to run as often as you like, and safe under `PHASE3_MODE=test`.

---

### 2. `eval-report`: the operator snapshot

#### What an evaluation row looks like, practically

Each line in `data/evaluations.jsonl` is one JSON object (JSONL — one
self-contained record per line — is explained fully in Chapter 2). For
this chapter, the fields that matter are the ones `eval-report` groups by:

- `summarizer` — which provider wrote the summary (openai / anthropic /
  gemini).
- `jury_score`, `jury_score_weighted`, `jury_score_unweighted` — the score,
  in both aggregation modes (Chapter 4).
- `hallucination_claims`, `confidence_score`, `parse_method` — reliability
  signals: did the judge find made-up facts, how confident was it, could
  its response even be read.
- `strata` (and some top-level fields) — `species`, `study_design`,
  `clinical_topic`, `journal`, `input_source` — the subgroup labels a
  stratified veterinary study needs.

#### How `eval-report` groups it

Run it with:

```powershell
python llm-sum/run_phase3.py eval-report
```

It reads every row and prints one table per grouping: **overall**, **by
provider**, **by species**, **by study design**, **by clinical topic**, **by
journal**, and **by input source**. Each row of each table shows how many
items were scored, both jury-score means, and three reliability rates:
hallucination rate, major-hallucination rate, and parse-failure rate (how
often the judge's reply couldn't be read cleanly at all — see Chapter 4).

Here's a real shape of what prints (numbers illustrative, but this is the
project's own worked example from `docs/phase3/medhelm_evaluation.md`):

```text
[by_summarizer]
anthropic: n=15 mean=4.0 (weighted=4.02 unweighted=4.0) halluc=0.067 major=0.0 parse_fail=0.0
gemini: n=15 mean=3.71 (weighted=3.74 unweighted=3.71) halluc=0.133 major=0.067 parse_fail=0.0
openai: n=15 mean=3.89 (weighted=3.92 unweighted=3.89) halluc=0.067 major=0.0 parse_fail=0.0

[by_species]
Canine: n=30 mean=3.95 (weighted=3.97 unweighted=3.95) halluc=0.1 major=0.033 parse_fail=0.0
Feline: n=15 mean=3.82 (weighted=3.85 unweighted=3.82) halluc=0.067 major=0.0 parse_fail=0.0
```

Reading the first line: anthropic's 15 scored summaries average a jury
score of 4.0 (both aggregation modes agree closely here), 6.7% of its
summaries had at least one hallucination, none were `major` severity, and
every judge response parsed cleanly.

A section that would only have one group — and that group is literally
"unknown" — is skipped rather than printed as noise; the report tells you
which sections were skipped and why (usually: that metadata isn't attached
to rows judged from the older `summarize-all` `.txt` workflow, since it
normally comes from `manifest.jsonl`).

#### The narrative version: `--markdown`

```powershell
python llm-sum/run_phase3.py eval-report --markdown
```

Same underlying data, rendered as plain English instead of dense tables:
a short **aggregate summary** file (headline numbers, a glossary of terms),
plus a **per-article detail** file — one section per paper, showing every
provider's score breakdown, the judge's one-sentence reasoning for each
criterion, and any hallucination claims with their quoted evidence. The
detail file exists so you can manually open the real article and check
whether the judge's verdict actually holds up — the same kind of check
Chapter 7's human reviewer does, just self-directed.

Add `--no-detail` to skip the (longer) per-article file if you only want
the aggregate summary.

#### Where results go

Every run writes its own timestamped file(s) under `data/results/` — nothing
is ever overwritten, so a report from last week survives a fresh `evaluate`
run changing `evaluations.jsonl`. Pass `--no-save` to print only.

---

### 3. `eval-report --publication`: the paper-ready layer

```powershell
python llm-sum/run_phase3.py eval-report --publication
```

This produces three linked artifacts in `data/results/`, all sharing one
timestamp:

| Artifact | What it is |
|---|---|
| `publication_report_<ts>.json` | The full report, every number, machine-readable. |
| `publication_report_<ts>.md` | The same tables as human-readable Markdown — pastable into a manuscript or slide. |
| `publication_report_<ts>_tables/` | A folder of CSVs, one per table, for a stats package or spreadsheet. |

#### The unit of analysis, in plain terms

Before the tables make sense, one framing detail matters: **one "item" is a
(paper, input channel) pair.** A paper judged from both processed text and
a direct PDF counts as *two* items. If more than one judge scored the same
item (a **jury** of judges — see Chapter 4), their scores are averaged
first, so every provider contributes exactly **one** score per item.

That matters because it makes the comparisons **paired**: the significance
tests below ask "did provider A beat provider B *on the same papers*?" —
not "does provider A's average look bigger than provider B's average across
two separate piles of papers?" Those are different, and much weaker,
questions. Pairing is what makes "A beat B" a meaningful claim.

#### What each table answers

The rest of this section walks through every table `--publication` produces,
in plain terms — **not** how the statistics behind them work. That's
Chapter 6; each subsection below points forward to it explicitly.

##### Provider comparison

One row per provider: mean jury score in **both** modes (unweighted and
weighted), each with a **95% bootstrap confidence interval** (a range that
likely contains the true average — Chapter 6 explains exactly what
"bootstrap" means and how the interval is built), mean cost, **cost-per-
quality-point**, a **subscription cost-per-quality-point** (§4 below), and
mean judge disagreement.

> **How you'd describe this table in a meeting:** *"Anthropic has the
> highest average quality at 4.0, with a confidence interval of roughly
> [3.7, 4.3] — meaning we're fairly confident the true average sits
> somewhere in that range, not that every single summary scored exactly
> 4.0. OpenAI's interval overlaps with Anthropic's, so on quality alone
> the gap between them isn't resolved yet — we'd want the significance
> table to say more."*

##### Significance

A **Friedman** test (an omnibus test — "is there a difference *somewhere*
among all providers?" — asked once per score mode) plus **pairwise
Wilcoxon signed-rank** tests for every pair of providers, each with its own
bootstrap confidence interval on the size of the difference. Run separately
for the unweighted and the weighted score.

Two things to know before you read a p-value here (a **p-value** is a
number that roughly answers "if there were really no difference, how
surprising would data like this be?" — smaller means more surprising, i.e.
more evidence of a real difference; Chapter 6 teaches this from scratch):

- **Use the `p (BH-adj)` column, not the raw p-value**, to call a pair
  "significant." Running several pairwise comparisons at once inflates the
  chance that *one* of them looks significant purely by luck; Benjamini-
  Hochberg correction (Chapter 6) adjusts for that. The raw p is kept beside
  it for transparency only.
- **`underpowered (n<10)`** means fewer than ten shared items stood behind
  that specific test. The p-value is still shown, but it's too unstable to
  lean on — collect more evaluated papers first.

> **How you'd describe this table in a meeting:** *"The Friedman test says
> there's a real difference somewhere among the three providers on the
> unweighted score. Looking at the pairwise breakdown, Anthropic beats
> Gemini with an adjusted p-value under 0.05 — that one holds up. The
> Anthropic-vs-OpenAI comparison shows a smaller gap and isn't significant
> after adjustment, so we can't yet say one beats the other — we'd need
> more evaluated papers."*

##### Per-stratum breakdowns

Provider × species / study design / clinical topic / journal cross-tabs of
the unweighted mean — the same idea as `eval-report`'s per-stratum tables
above, but computed on the item-level, paired dataset the publication
report uses throughout, so every table in this artifact is internally
consistent.

##### Processed text vs. direct PDF

A dedicated provider × input-channel table, kept separate from the clinical
strata above, answering one focused question: does feeding a provider the
cleaned **processed text** (Chapter 2) versus the raw **PDF** systematically
help or hurt its scores?

##### Inter-judge reliability

A one-line summary of **Krippendorff's alpha** (a chance-corrected agreement
score between judges — Chapter 4 introduces it as "the rigorous version of
judge disagreement"; Chapter 6 teaches the actual math) — populated only
when a **jury** of two or more judges scored the same articles. With the
default single judge, this reads "not available," not zero.

##### Information density

Word count and **TF-IDF + cosine similarity** (a way of checking whether a
summary's meaningful, specific vocabulary overlaps with the source
abstract's — Chapter 6 has the full explanation) for every summary against
its paper's own abstract. This re-runs a published 2023 benchmark (Appleby
et al.) against this study's own three providers, banding each summary
"high fidelity" (cosine ≥ 0.85), "moderate," or "lost key details"
(cosine < 0.50).

##### Covariate analysis (the "research meat")

One table per (provider × species / study design / journal) cell, joining
three numbers that normally live in separate tables: **hallucination
rate**, **mean quality**, and **Cohen's Kappa** (LLM judge vs. human
reviewer agreement on that specific cell — see Chapter 6 for how Kappa
differs from Krippendorff's alpha). This is the table that lets you ask a
genuinely clinical question directly.

> **How you'd describe this table in a meeting:** *"Breaking it down by
> species, OpenAI's Kappa against our human reviewers is 0.82 for canine
> papers but only 0.45 for equine papers — so the AI judge tracks a human's
> judgment much more closely on dog papers than horse papers, which makes
> sense given how much more canine literature these models likely saw in
> training. But read the n first: with only 15 human-reviewed items split
> across three providers and several species, most of these cells are
> thin — the report flags any cell under 5 items as `(underpowered)`, so
> treat a single cell's Kappa as a signal to investigate, not a settled
> conclusion."*

---

### 4. Cost: two different questions

The publication report answers **two separate cost questions**, and it's
worth being precise about which one a number is answering:

- **`cost_per_quality_point`** (in the provider comparison table) is a
  **research-budget** question: what did this study actually spend to
  generate each provider's summaries, priced from the summarizer's own
  logged token counts? The judge's own cost is a separate, shared overhead
  and is deliberately *not* charged to any one provider here.
- **`subscription_cost_per_quality_point`** is a **consumer-economics**
  question: *is a flat-rate AI subscription worth it to a practicing vet?*
  The assumption is a vet summarizing 500 papers a month on a $20/month
  subscription, which works out to $0.04 per summary regardless of how long
  the paper or summary actually is — unlike the real API cost, which scales
  with token count. `efficiency = mean quality ÷ $0.04` per provider, so a
  higher number means better value for the subscription price.

Neither number replaces the other — they're deliberately kept side by side
because they answer different questions ("what did the *study* spend?" vs.
"is this *worth paying for* day to day?").

---

### 5. The leaderboard and figures

```powershell
python llm-sum/run_phase3.py report-figures
```

This reuses the exact same publication-report numbers (same bootstrap seed,
same cost basis) and produces a slide- or results-page-ready summary rather
than a methods-section table:

| Artifact | What it is |
|---|---|
| `leaderboard_<ts>.json` / `.md` | One row per provider, ranked by unweighted quality, best first. |
| `figures_<ts>/*.png` + `*.svg` | Four presentation charts. |

The leaderboard's columns are all numbers you've already met above — quality
(both modes, with CIs), cost and cost-per-quality-point, per-provider
Krippendorff's alpha (scoped to *that provider's* summaries specifically,
answering "do judges agree with each other about THIS provider?"), and
per-provider Spearman correlation against human reviewers (from Chapter
7's validation data, when it exists).

The four figures:

1. **`provider_comparison`** — grouped bars, unweighted and weighted jury
   score with 95% confidence-interval error bars, one color per provider.
2. **`cost_quality`** — a scatter plot of cost vs. quality, one labeled
   point per provider (skipped if no provider has a priced summary).
3. **`criterion_heatmap`** — provider × criterion (faithfulness /
   completeness / clinical usefulness / clarity / safety) mean score, one
   color intensity per cell, with the exact value also printed on the cell
   since color alone can't carry two-decimal precision.
4. **`reliability`** — Krippendorff's alpha per criterion plus overall,
   emitted only when a jury (2+ judges) actually ran; skipped for the
   default single-judge run.

Add `--no-figures` for the leaderboard only, or `--formats png` to skip the
SVG copies.

---

### Recap

- **`eval-report`** is your quick, repeatable operator snapshot — grouped
  by provider, species, study design, clinical topic, journal, and input
  source, with a narrative `--markdown` mode for a plain-English read plus
  a per-article detail file for manual spot-checks.
- **`eval-report --publication`** builds the deeper, paper-ready layer:
  provider comparison with confidence intervals, paired significance
  tests, per-stratum and processed-vs-PDF breakdowns, inter-judge
  reliability, information density, and the covariate "research meat"
  table joining quality, hallucination rate, and Cohen's Kappa.
- **The unit of analysis is one item = one (paper, input channel) pair**,
  and providers are compared **paired by item** — the same papers, not two
  separate piles — which is what makes a "provider A beat provider B" claim
  meaningful.
- **Cost is answered twice, on purpose**: a research-spend number
  (`cost_per_quality_point`) and a separate consumer-subscription number
  (`subscription_cost_per_quality_point`).
- **The leaderboard and four figures** repackage the same underlying
  numbers for a slide or results page rather than a methods section.
- This chapter deliberately stayed at the "what does this table mean"
  level. **Chapter 6** teaches every formula behind these numbers — p-values,
  Wilcoxon, Friedman, Benjamini-Hochberg correction, bootstrap confidence
  intervals, Krippendorff's alpha, Cohen's Kappa, and TF-IDF/cosine
  similarity — from scratch, with worked examples.


---


## Chapter 6 — The Statistics Behind the Numbers

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

### What we are actually trying to prove

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

### Part 0 — The toolbox (numpy, scipy, scikit-learn)

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

### Part 1 — Four vocabulary ideas you need first

Just four. Once these click, the tests are easy.

#### 1a. The p-value — the single most important idea

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

#### 1b. "Paired" data

All three providers summarize **the same papers**. So when we compare OpenAI vs
Anthropic we can line them up **paper by paper**: on paper 1, OpenAI scored 5
and Anthropic scored 4; on paper 2, 3 vs 3; and so on. Data that lines up like
this is **paired**.

Pairing is powerful. Instead of the blurry question "is OpenAI's overall
average higher?" we can ask the sharp question "did OpenAI beat Anthropic *on
the very same paper*, more often than not?" — which automatically cancels out
the fact that some papers are just harder to summarize than others.

#### 1c. Ranks

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

#### 1d. Correlation

A **correlation** measures whether two sets of numbers move together. It runs
from `+1` (perfectly in step) through `0` (unrelated) to `-1` (perfectly
opposite). We'll use it in Part 8 to ask "when a human vet rates a summary high,
does the AI judge also rate it high?"

---

### Part 2 — The Wilcoxon signed-rank test

**The question it answers:** *"Did provider A really beat provider B, on the
same papers — or could the gap be luck?"*

**The everyday version.** Two chefs each cook the same 10 dishes; a taster
scores every dish. For each dish you note **who won and by how much**. If chef A
keeps winning, dish after dish, chef A is probably genuinely better. If they
trade wins randomly with tiny margins, it's probably a tie. That's exactly what
Wilcoxon measures.

#### How it works, step by step

1. For each paper, compute the **difference** (A's score − B's score).
2. Throw away any paper with a difference of **0** — a tie picks no winner.
3. Take the **absolute values** of the remaining differences (ignore the +/−
   sign for a moment) and **rank** them from smallest to largest.
4. Add up the ranks belonging to A's wins (call it **W⁺**) and the ranks
   belonging to B's wins (**W⁻**).
5. If one of those totals is much bigger than the other, one provider is
   consistently winning → small p-value. If they're balanced, it's a wash →
   large p-value.

#### Worked example

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

#### Why not just compare the two averages?

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

### Part 3 — The Friedman test

**The question it answers:** *"Looking at all three providers at once, is at
least one of them genuinely different from the others?"*

**The everyday version.** Now three chefs cook the same 10 dishes. Before you
dive into "A vs B, A vs C, B vs C," you ask one gatekeeper question first: *is
anyone standing out at all?* Friedman is that gatekeeper — it's the
"three-or-more-groups" cousin of Wilcoxon.

#### How it works

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

#### Worked example

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

#### Why run Friedman *before* the pairwise Wilcoxon tests?

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

### Part 4 — Multiple comparisons: why we adjust the p-values

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

#### Worked example (the same three p-values, three methods)

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

#### Why does this project pick Benjamini-Hochberg?

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

### Part 5 — Bootstrap confidence intervals

**The question it answers:** *"A provider's average score is 4.2 — but how sure
am I? Could the true value easily be 3.9 or 4.5?"*

An average from 20 papers is just an estimate. If you'd happened to collect a
*different* 20 papers, you'd get a slightly different average. A **confidence
interval** is a range — like "4.2, somewhere between 3.9 and 4.5" — that
captures how much that average would wobble. The **bootstrap** is a clever,
assumption-free way to compute that range.

#### How the bootstrap works

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

#### Why 2000 resamples?

More resamples give a smoother, more stable interval, but cost more computing
time. 2000 is a common sweet spot — enough to be stable, cheap enough to run
often. It's tunable in this project (`PUBLICATION_BOOTSTRAP_RESAMPLES`), and the
resampling is **seeded** (a fixed starting point for the randomness) so the same
data always produces the identical interval — reproducibility again.

#### How to read one

> **A confidence interval that straddles another provider's mean means the gap
> is not resolved at this sample size.** If OpenAI is 4.2 [3.6-4.8] and Anthropic
> is 4.0 [3.5-4.6], those ranges overlap heavily — you can't confidently say one
> beat the other yet. Read the interval *together with* the Wilcoxon p-value from
> Part 2, never instead of it.

**Where we use it:** [`llm-sum/report_tables.py`](../../llm-sum/report_tables.py),
via `scipy.stats.bootstrap(..., method="percentile")`, 2000 resamples by
default, seeded for exact reproducibility.

---

### Part 6 — Krippendorff's alpha

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

#### The scale (this is most of what you need to remember)

| Alpha | Meaning |
|---|---|
| **1.0** | Perfect agreement — scorers are interchangeable. |
| **0.80 and up** | Strong; safe to draw firm conclusions. |
| **0.667 – 0.80** | Acceptable; tentative conclusions only. |
| **0** | Agreement no better than random guessing. |
| **below 0** | Worse than random — scorers systematically *disagree*. |

#### The formula, and why it's not just "% identical"

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

#### Worked example

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

#### Why is this the one hand-rolled statistic?

Every other test in the project uses a validated library. Krippendorff's alpha
is written by hand in
[`llm-sum/reliability.py`](../../llm-sum/reliability.py) for one reason only:
**neither scipy nor scikit-learn ships it.** It also gracefully handles
**missing data** — e.g. a vet who only reviewed a subset of summaries — using
whatever overlap exists instead of forcing you to throw away rows. That
missing-data tolerance is exactly why it's the right choice for a jury where not
every judge scores every item.

---

### Part 7 — Cohen's Kappa (and percent agreement)

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

#### The formula

```
              (agreement you actually saw) − (agreement expected by chance)
kappa  =  ─────────────────────────────────────────────────────────────────
                        1 − (agreement expected by chance)
```

`kappa = 1` is perfect agreement; `kappa = 0` is exactly what two random
guessers would produce; negative means they disagree *more* than random guessing.

#### Worked example

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

#### One necessary wrinkle: Kappa needs whole-number categories

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

### Part 8 — Correlation: Pearson, Spearman, and Bland-Altman

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

#### 8a. Pearson correlation — do they move together *in a straight line*?

Pearson's *r* measures how tightly the two sets of numbers fall along a straight
line. Working through the arithmetic for the data above gives **r ≈ 0.97** — a
strong positive correlation. When the human's score goes up, the judge's goes up
too, almost perfectly in step.

**How to read it:** `+1` = perfect straight-line agreement, `0` = no relationship,
`−1` = perfectly opposite. A **negative** correlation between judge and human
would be alarming — it would mean the judge ranks summaries *backwards* from the
vet.

#### 8b. Spearman correlation — do they put things in the *same order*?

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

#### 8c. Bland-Altman — is the judge *systematically* biased?

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

#### Why all three?

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

### Part 9 — TF-IDF and cosine similarity (information density)

**The question it answers:** *"Does an AI summary actually carry as much
**information** as the paper's own abstract — or did it quietly drop the
important parts?"*

This directly re-runs a 2023 finding (Appleby et al.) that AI-written summaries
carried *less* information than the source abstract 79.7% of the time. To check
it, we need a way to measure "meaningful content overlap" — and that takes two
ideas working together.

#### TF-IDF — scoring how *distinctive* each word is

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

#### Cosine similarity — comparing two fingerprints

Once the abstract and the summary are each a fingerprint (a list of numbers),
**cosine similarity** compares them and returns one number from 0 to 1: how much
do their important words overlap? It's the dot product of the two fingerprints
divided by their lengths — geometrically, the angle between them. `1.0` = same
technical content; `0` = completely unrelated.

#### Worked example

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

#### The bands this project uses

- **cosine ≥ 0.85** → "high fidelity": kept almost all the technical meaning.
- **cosine < 0.50** → "lost key medical details."
- **in between** → "moderate."

The **0.50** line is also the headline cutoff: the percentage of summaries that
kept "similar-or-more" information, compared against Appleby et al.'s benchmarks.
These cutoffs are documented, tunable choices
(`stats_engine.INFORMATION_DENSITY_RETENTION_THRESHOLD` /
`_HIGH_THRESHOLD`), not a re-derivation of Appleby's exact formula.

#### Why fit TF-IDF over the *whole corpus*, not just the two documents?

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

### Part 10 — ROUGE (word-overlap recall)

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

#### ROUGE-1, ROUGE-2, ROUGE-L

- **ROUGE-1** counts overlapping **single words** (unigrams).
- **ROUGE-2** counts overlapping **word pairs** (bigrams) — a stricter test,
  because it rewards keeping words *in the same order*.
- **ROUGE-L** counts the **longest common subsequence** — the longest sequence of
  words that appears in both texts in the same order, though not necessarily
  side by side. It rewards preserved phrasing without demanding exact adjacency.

#### Worked example

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

#### How to read ROUGE — and its limits

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

### Part 11 — Cost-per-quality-point

**The question it answers:** *"Which provider gives the best quality for the
money?"*

The math here is the simplest in the whole chapter — plain division — but it's
worth stating precisely because "cost" means two different things in this
project.

#### The formula

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

#### Two costs, two questions — don't mix them up

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

### Part 12 — How it all fits together

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

### Part 13 — One-page cheat sheet

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

### Where to go next

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


---


## Chapter 7 — A Guide for Veterinarian Reviewers: Scoring AI-Written Summaries

> **Note:** This chapter is also distributed on its own as a standalone,
> printable handout for reviewers — see
> [`07_human_validation_guide.md`](07_human_validation_guide.md). It assumes
> no other chapter has been read and deliberately avoids all technical
> jargon.

Thank you for agreeing to help with this project. This guide walks you
through everything you need to do — there is nothing to install, nothing to
code, and no special software knowledge required. If you can open a document
and fill in a spreadsheet, you have everything you need.

Read this guide once, start to finish, before you begin scoring. It should
answer every question you have along the way. You do not need to have read
anything else about this project first.

---

### 1. What we're asking you to do, and why it matters

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

### 2. Why you won't be told which AI wrote which summary

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

### 3. What you'll receive

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

### 4. How to work through an item

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

### 5. Filling in the scoring sheet

Each row on the scoring sheet has several columns. Here's what each one
means, described in terms of the everyday clinical judgment you already use
every day — not in technical language.

#### The five 1-to-5 scores

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

#### The two hallucination columns

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

#### The comment column

Anything else worth flagging that doesn't fit neatly into the boxes above —
a stylistic issue, something you found impressive, a point you're unsure
about — goes here. It's optional, but genuinely useful to us, so don't be
shy about using it.

#### A filled-in example row

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

### 6. When you're finished

Once you've scored every item on the sheet, send the completed scoring sheet
back the way you received it. You don't need to send anything else, and
please don't open or forward the private matching file mentioned in
Section 3, if you happen to have received it alongside your materials — it
isn't meant for reviewers and isn't something you need to look at.

That's it. There's nothing further for you to do — we'll take it from there.

---

### 7. One last thing

There is no wrong answer here, and this is not a test of you. We are not
checking your work against some hidden correct score — we are asking for
your honest, independent clinical judgment, exactly as you'd give it to a
colleague. If two reviewers score the same summary differently, that's
useful information too, not a mistake either of you made.

Thank you again for your time and expertise — this kind of independent
review is the only way we can honestly say whether this AI system's
judgment can be trusted.
