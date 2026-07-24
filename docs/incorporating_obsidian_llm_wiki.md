# Incorporating an Obsidian "LLM Wiki" — a VRU Design Blueprint

*A future direction for this project. This is a **planning document only** — nothing here builds or changes any code. It describes how we could layer Andrej Karpathy's "LLM Wiki" pattern on top of the papers this pipeline already collects, scoped to a single journal — **VRU (Veterinary Radiology & Ultrasound)** — as a bounded first version.*

The short version:

> Instead of asking an LLM the same question against a pile of PDFs over and over (RAG), we have the LLM **read each VRU paper once and write it into a growing, interlinked set of markdown pages**. The knowledge is compiled once and then kept current — cross-references, contradictions, and synthesis already in place — so it *compounds* instead of being re-derived on every query. Obsidian is the IDE; the LLM is the writer; the wiki is the codebase.

This idea comes from Andrej Karpathy's **[LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)** pattern. This doc translates that abstract pattern into a specific, buildable plan grounded in what we already have.

---

## 1. Why this, and why VRU first

**Why the pattern.** Most LLM-plus-documents work is retrieval: upload files, retrieve chunks at query time, generate an answer. Nothing accumulates — a subtle question that needs five papers gets pieced together from scratch every single time. The LLM Wiki is different: the LLM maintains a **persistent, compounding artifact**. When a new paper arrives, the LLM reads it, extracts what matters, and *integrates* it — updating topic pages, adding cross-links, flagging where new data contradicts old claims. You curate sources and ask good questions; the LLM does the summarizing, filing, and bookkeeping.

**Why VRU as the first scope.**

- We already hold **50 VRU PDFs** in [`data/raw/`](../data) (named `vru__<title>__<doi-slug>.pdf`). VRU is one of the five study journals in [`src/collect.py`](../src/collect.py) `TARGET_JOURNALS` — *Veterinary Radiology & Ultrasound*, e-ISSN 1058-8183.
- ~50 papers is exactly the scale where Karpathy says a **simple `index.md` catalog is enough — no vector search / embeddings needed**. That happens to be true for us anyway: this repo has **no** embedding or vector-search infrastructure today (only TF-IDF used as a metric in [`stats_engine.py`](../llm-sum/stats_engine.py)). So a first VRU wiki needs nothing new on that front.
- Radiology gives naturally concrete page types — **imaging modalities** (ultrasound, CT, MRI, radiography), **anatomical regions**, **findings**, **species** — so the wiki structure isn't hand-wavy.

Starting narrow means we can prove the workflow on one journal, then widen to the other four (JVIM, JAVMA, Veterinary Surgery, JFMS) if it earns its keep.

---

## 2. RAG vs. a compounding wiki

| | Today's default (RAG) | The LLM Wiki |
|---|---|---|
| When knowledge is built | At query time, every time | Once at ingest, then kept current |
| What persists | Nothing — just the raw chunks | A growing set of interlinked pages |
| Cross-references | Re-discovered per question | Already written and maintained |
| Contradictions between papers | You hope the retrieval surfaces both | Flagged on the page when the second paper is read |
| Who does the bookkeeping | No one | The LLM, every ingest |

The key point for us: **the wiki is a new read-only-over-data layer, not a change to the study.** It sits *beside* the evaluation pipeline and reads the same raw corpus. It never touches `data/summaries.jsonl`, `data/evaluations.jsonl`, or any study ledger.

---

## 3. The three layers, mapped to VRU

Karpathy's pattern has three layers. We already have strong analogs for all three — this is the heart of why VRU is a low-effort starting point.

| LLM-Wiki layer | Karpathy's version | What this pipeline already gives us |
|---|---|---|
| **Raw sources** (immutable) | a folder of PDFs / web clips | [`data/raw/`](../data)`vru__*.pdf` — 50 VRU papers, already the immutable source of truth |
| **Page id / join key** | the filename | the **DOI-slug** (e.g. `10_1111_vru_13310`) already unifies `raw/`, `raw_text/`, `processedv2/`, `summaries.jsonl`, and `dev_detailEval_reports/*.md`; helpers in [`src/file_paths.py`](../src/file_paths.py) |
| **Pre-extracted text** (optional read shortcut) | — | [`data/raw_text/`](../data)`<slug>.jsonl` is **already markdown-ish** (`#`/`####` headings, `**bold**`), shape `{doi, slug, text}` |
| **Per-paper metadata** (ready-made frontmatter) | — | [`data/manifest.jsonl`](../data) rows carry `doi, title, pub_year, authors, abstract, journal, issn` plus inferred `species, study_design, clinical_topic` (`_infer_covariates()` in [`src/collect.py`](../src/collect.py)) |
| **Closest existing auto-page** | — | [`data/dev_detailEval_reports/`](../data)`<slug>.md` — genuine per-paper markdown keyed by DOI-slug; the nearest precedent to a wiki "source page" |
| **Control schema** | `CLAUDE.md` / `AGENTS.md` | our own [`CLAUDE.md`](../CLAUDE.md) — a short, bolded, imperative rules file (see §6) |

> **The "convert to markdown?" question, settled.** The supervisor's instinct is right: **we do not need a separate PDF-to-markdown build step.** The LLM reads the source and writes the wiki page itself. Two read options exist, and the wiki can use either per paper:
> - **Read the PDF directly** (`data/raw/vru__*.pdf`) — the agent sees figures and tables, which matters a lot for radiology. Caveat: an LLM can't read a markdown file's inline images in one pass; images are viewed as a separate step.
> - **Read the pre-extracted text** (`data/raw_text/<slug>.jsonl`) — faster, text-only, and already heading-structured. Good when the figures aren't essential.

---

## 4. A proposed VRU wiki structure (the blueprint)

A wiki is just a folder of markdown files opened in Obsidian (and, ideally, a plain git repo so you get version history for free). A concrete starting shape:

```text
vru-wiki/                         # opened in Obsidian; a plain git repo of .md files
  _schema/
    CLAUDE.md                     # the wiki control file (see §6)
  index.md                        # content catalog: every page, one-line summary, by category
  log.md                          # append-only: ## [YYYY-MM-DD] ingest | <paper title>
  sources/
    10_1111_vru_13310.md          # one page per VRU paper, keyed by DOI-slug
    10_1111_vru_13290.md
  modalities/
    ultrasound.md                 # concept pages: what VRU says about each modality
    computed-tomography.md
    mri.md
    radiography.md
  findings/
    postoperative-abdomen-on-ultrasound.md
    splenic-fna-diagnostic-yield.md
  anatomy/
    canine-abdomen.md
    feline-tympanic-bulla.md
  species/
    dog.md
    cat.md
```

Filenames in `sources/` are the **DOI-slug**, so every page traces straight back to `data/raw/vru__…__<slug>.pdf` → `data/manifest.jsonl`. That's the same provenance discipline the rest of the project already lives by.

### Page types

**Source page** — one per paper. YAML frontmatter (so Obsidian's **Dataview** plugin can build tables from it) plus a short LLM-written summary and links out to the concepts it touches. Sketch:

```markdown
---
doi: 10.1111/vru.13310
slug: 10_1111_vru_13310
title: "Sonographic features of the uncomplicated postoperative abdomen in dogs treated for pyometra by ovariohysterectomy"
journal: VRU
year: 2024
species: [dog]
modality: [ultrasound]
source_pdf: data/raw/vru__sonographic_features...__10_1111_vru_13310.pdf
---

## Summary
LLM-written synopsis of objective, methods, key findings, limitations — every
claim traceable to the source PDF.

## Links
- Modality: [[ultrasound]]
- Finding: [[postoperative-abdomen-on-ultrasound]]
- Anatomy: [[canine-abdomen]] · Species: [[dog]]

## Provenance
Source: DOI 10.1111/vru.13310 · ingested [YYYY-MM-DD] · see [[log]]
```

**Concept page** (`modalities/`, `findings/`, `anatomy/`, `species/`) — the compounding synthesis. It says what the VRU corpus *collectively* shows about a topic, with `[[wikilinks]]` to every source page that contributed. Example `findings/postoperative-abdomen-on-ultrasound.md`:

```markdown
# Postoperative abdomen on ultrasound

Normal vs. concerning sonographic features after abdominal surgery in dogs.

- Expected post-ovariohysterectomy appearance — from [[10_1111_vru_13310]]
- Free fluid / gas interpretation caveats — from [[10_1111_vru_13310]]
- (contrast with) FNA sampling of liver/spleen — see [[splenic-fna-diagnostic-yield]] ← [[10_1111_vru_13290]]

> Contradiction watch: if a later VRU paper reports different normal ranges,
> the LLM notes it here rather than silently overwriting.
```

As each new VRU paper is ingested, the LLM edits these concept pages — this is the bookkeeping humans abandon wikis over, and exactly what the LLM does cheaply.

---

## 5. Operations: Ingest → Query → Lint

### Ingest
You point the agent at one VRU paper. It:
1. Reads the PDF (or `raw_text/<slug>.jsonl`) and discusses the key takeaways with you.
2. Writes or updates `sources/<slug>.md`.
3. Updates every concept page it touches — `modalities/`, `findings/`, `anatomy/`, `species/` — and their cross-links. A single paper may touch **10–15 pages**.
4. Updates `index.md` and appends one line to `log.md`.

Do this **one paper at a time with you in the loop** at first — the same "single/dev before batch" discipline the pipeline already uses ([`PHASE3_MODE`](../.env.template)). Batch ingestion with lighter supervision is possible later.

### Query
You ask a question against the wiki (e.g. *"what does VRU say about normal splenic ultrasound in dogs?"*). The agent reads `index.md` first, drills into the relevant pages, and answers **with citations back to DOIs**. Crucially: **good answers get filed back into the wiki as new pages**, so your explorations compound just like ingested papers do.

### Lint
Periodically ask the agent to health-check the wiki:
- Orphan pages (no inbound links) and dead `[[links]]`.
- **Contradictions between papers** — e.g. two VRU studies reporting different normal reference ranges for the same measurement.
- Stale claims a newer paper has superseded.
- Concepts mentioned but lacking their own page; missing cross-references.
- Gaps a literature search could fill.

`index.md` is the **content catalog** (every page + a one-line summary, by category). `log.md` is the **chronological, append-only** record; if each entry starts `## [YYYY-MM-DD] ingest | Title`, it stays greppable:

```powershell
Select-String -Path vru-wiki/log.md -Pattern '^## \[' | Select-Object -Last 5
```

---

## 6. The wiki's control file — mirroring our `CLAUDE.md`

The thing that makes the LLM a **disciplined wiki maintainer** rather than a generic chatbot is the schema/control file. We already have the perfect model for it: this repo's own [`CLAUDE.md`](../CLAUDE.md) — a short, bolded, imperative rules file. A `vru-wiki/_schema/CLAUDE.md` would mirror its shape, and several rules map almost one-to-one onto norms this project already enforces:

| Wiki rule (proposed) | Mirrors this existing project norm |
|---|---|
| **Never modify raw sources.** `data/raw/*.pdf` and `data/manifest.jsonl` are read-only. | `.env` is off-limits; `manifest.jsonl` / `evaluations.jsonl` are append-only, never hand-edited |
| **Cite every claim to a DOI. Never invent.** If a paper doesn't report something, say so — don't fabricate. | The no-hallucination rule ("a claim not supported by the source"); summarizer fills gaps with "Not reported"; blind-judge provenance |
| **Key every page on the DOI-slug** and record provenance in frontmatter. | The DOI-slug is the universal join key across the pipeline ([`06_data_provenance.md`](booklet/06_data_provenance.md)) |
| **Keep separate streams separate.** The wiki is NOT a study ledger; never write to or blur `evaluations.jsonl` / `human_reviews.jsonl`. | Pilot vs. real human-review ledgers never merge; run-manifest ≠ manifest |
| **No auto-running paid/live LLM calls.** If the workflow ever scripts model calls, a human runs them manually; test/dry mode is the default. | [`CLAUDE.md`](../CLAUDE.md) budget-protection rule; `BudgetGuard` |
| **Conventions:** `index.md` = content catalog; `log.md` = append-only with a greppable `## [date] ingest \|` prefix. | Matches the project's append-only, auditable-trail habits |

You and the LLM co-evolve this file over time as you learn what works for VRU — exactly as `CLAUDE.md` has grown with the project.

---

## 7. Obsidian tooling that fits (all optional)

- **Graph view** — the best way to see the wiki's shape: which pages are hubs (e.g. `ultrasound.md`), which are orphans.
- **Dataview** — generates dynamic tables from the YAML frontmatter (e.g. "all dog CT papers from 2024"). Worth adding frontmatter from day one so this just works.
- **Web Clipper** — converts web articles to markdown; useful later if we add non-journal sources (guidelines, textbook chapters).
- **Marp** — markdown slide decks straight from wiki content, for lab meetings.
- **qmd** (or a simple home-grown search) — an on-device search engine over the markdown, **only if/when VRU outgrows the `index.md` approach**. Not needed at 50 papers, and net-new (we have no search tooling today) — so explicitly deferred.

---

## 8. Honest limits & open questions

In the house tradition of saying what something does *not* do yet:

- **Scale ceiling.** The `index.md`-only approach is fine for ~50 VRU papers and hundreds of pages. Past that, we'd need real search (qmd or similar) — a later problem, not a now problem.
- **Figures and tables.** Radiology is visual. An LLM can't read a markdown file's inline images in one pass — it must view images as a separate step. Reading the **PDF** (not just `raw_text`) matters more here than in most domains, and image handling will need troubleshooting.
- **How much human-in-the-loop.** One-paper-at-a-time with review is safest to start; how far we can batch is an open question.
- **Should wiki pages link to the study's own outputs?** e.g. a source page linking to its [`data/dev_detailEval_reports/<slug>.md`](../data). Possibly useful, but it risks blurring the "wiki vs. study ledger" line from §6 — decide deliberately.
- **Keeping current as `data/raw/` grows.** New VRU papers arrive over time; the ingest + lint loop has to keep the wiki honest.

Per Karpathy, the right move is to **share this doc with the LLM agent and instantiate a version that fits** — treat the specifics above as a starting point to troubleshoot together, not a fixed spec.

---

## 9. How to explain this in a meeting

> We already collect and extract VRU papers for the evaluation study. The LLM-wiki layer reuses that same corpus but points the model at a different job: read each paper once and maintain an interlinked radiology knowledge base — modalities, findings, anatomy, species — with every claim cited back to a DOI. Knowledge **accumulates** instead of being re-retrieved on every question, and the LLM does the cross-referencing and upkeep that makes a wiki actually survive. It's a read-only layer beside the study, not a change to it.

---

## 10. Where to go next

| For… | Read |
|---|---|
| Reusing/extending the pipeline for new studies | [docs/extending_the_pipeline.md](extending_the_pipeline.md) |
| How the pipeline is built and why | [docs/GUIDE.md](GUIDE.md) |
| Provenance norms the wiki must respect (DOI keys, immutability, citing sources) | [docs/booklet/06_data_provenance.md](booklet/06_data_provenance.md) |
| The control-schema analog to mirror | [../CLAUDE.md](../CLAUDE.md) |
| The 5-journal corpus design (VRU is one) | [README.md](../README.md) — "Corpus design" |
| The original pattern | Andrej Karpathy's **LLM Wiki** write-up: [gist.github.com/karpathy/442a6bf5…](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — an intentionally abstract idea file meant to be handed to your LLM agent and instantiated for your domain |
