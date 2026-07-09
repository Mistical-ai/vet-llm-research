# Chapter 1 — Gathering the Data

**Who this is for:** a technical beginner who has never touched this
pipeline before. No assumed knowledge of the project, but comfortable
running a command in a terminal.

**What you'll be able to do after reading this:** understand what the
"corpus" (the paper collection) is, why it's built the way it is, and how to
run the four Phase 2 commands that build it — plus how to read their output
and fix the most common problems.

---

## 1. The goal: 250 papers, five journals, balanced

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

### Primary vs. secondary papers

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

## 2. Step 1 — Build the candidate list (`collect.py`)

```powershell
python src/collect.py
```

This is the very first command in the whole pipeline. It doesn't download
any PDFs yet — it just builds a list of **candidate** papers (a candidate is
a paper that *might* end up in the corpus, before anyone has checked whether
a free PDF exists for it).

### Where the list comes from: CrossRef

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

### What gets filtered out immediately

CrossRef returns a lot of things that aren't actually research articles —
corrections, retractions, "Table of Contents" listing pages, editorials,
letters to the editor, book reviews. `collect.py` checks each candidate's
title against a list of patterns and throws these out before they ever touch
the manifest. It also recognizes and discards the excluded article types
(case reports, short communications, etc.) at the title level, the same
categories described in section 1.

### Building a bigger pool than you need

For a 50-PDF-per-journal target, `collect.py` actually collects around
**2,000 candidates per journal** (configurable via `COLLECT_CANDIDATES_PER_JOURNAL`
in `.env`). That's not a mistake — most candidates will never turn into a
downloaded PDF, because many of them are paywalled. Collecting a large pool
now gives the next step (downloading) plenty of room to find the ones that
*are* freely available, without needing to re-query CrossRef later.

**Why not just query CrossRef fresh every time you need more candidates?**
Because CrossRef's rate limits and the time cost of re-querying make it much
more efficient to gather a generous list once and then work through it.

### Spreading candidates across years

Collection is also **year-balanced**: rather than grabbing the 2,000 newest
papers (which would be dominated by whichever year had the most publishing
activity), the candidate quota is split evenly across 2023, 2024, 2025, and
2026 (the years in `COLLECT_YEARS`). This keeps the corpus from silently
skewing toward one recent year.

### Guessing at species, study design, and clinical topic

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

### The output: `data/manifest.jsonl`

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

## 3. Step 2 — Download open-access PDFs (`download.py`)

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

### Why disguise requests as a browser?

Some publisher websites (Wiley, AVMA, SAGE) block requests that don't look
like they're coming from a normal web browser, even for papers that *are*
freely available — this is generic bot-blocking, not a paywall. So
`download.py` sends realistic browser-style request headers, the same thing
that happens automatically when a person opens the paper in Chrome. This
does **not** unlock anything that wasn't already free; a login-only page
still gets rejected by the next check.

### Five checks every downloaded file must pass

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

### After acceptance: sorting primary vs. secondary vs. excluded

Once a PDF passes those five checks, `download.py` reads its first couple of
pages looking for the article-type label veterinary journals usually print
near the title (e.g. "Case Report", "Systematic Review"):

- **Excluded** (case report, short communication, etc.) → the file is
  deleted; it never enters `data/raw/`.
- **Secondary** (systematic review, meta-analysis) → kept, renamed with a
  `2_` prefix, doesn't count toward the 50-per-journal quota.
- **Primary** → kept as-is, counts toward the quota.

### Being a polite, rate-limited downloader

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

### The output

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

## 4. Step 3 — Check your progress (`pipeline.py`)

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

## 5. Step 4 — Fill the gaps manually

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

### Getting the actual PDFs

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

### The full manual-supplementation loop

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

## 6. Troubleshooting

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

## 7. Where things end up (quick reference)

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
