# Human Validation Review UI

A small **offline, local** web app for blind human validation of AI-written
veterinary summaries (Phase 5). A reviewer picks their pack, reads the guide,
then scores each item against its original article (PDF) and one candidate
summary, with a fixed scoresheet across the bottom of the screen. Scores are
written back into the pack's own `scoresheet_human1.xlsx` so the existing
`ingest-human-review` / `ingest-pilot-human-review` command reads them unchanged.

The app never shows — and physically cannot serve — a summarizer/model name or
any `unblinding_key_*.json`. The blind protocol is preserved.

---

## Quick start (you, on this laptop)

1. Make sure **Node.js** is installed (`node --version` in a fresh PowerShell
   window). If you just installed it and a terminal says `node` isn't found,
   close that terminal and open a new one.
2. From `review-ui/`, install dependencies once:
   ```powershell
   npm install
   ```
3. Start it:
   ```powershell
   npm start
   ```
   …or just **double-click `start.bat`**. A browser opens at
   `http://localhost:5173`. Leave the terminal/black window open while reviewing.

By default the app reads the pilot packs at `data/pilot_human_review/`. To point
it at the real study export instead:
```powershell
$env:REVIEW_ROOT = "C:\...\vet-llm-research\data\human_review"; npm start
```
`REVIEW_ROOT` must be the folder that **directly contains** `human1/`, `human2/`,
… — if it points anywhere else the app shows "no packs found."

---

## What the reviewer does

1. **Pick a pack.** The landing page lists each pack (`human1`, `human2`, …) with
   a preview of its 5 articles. Clicking a pack sets the reviewer identity — you
   are `human1` when you review the `human1` pack (this can't be changed
   independently; identity is tied to the pack because each pack has its own
   private scoring key).
2. **Read the guide** (`REVIEWER_GUIDE.md`), then click **I'm ready**.
3. **Score each item.** Top-left is the original article (`article.pdf`), top-right
   is one candidate summary. The **scoresheet stays fixed** across the bottom —
   score every item's row (1–5 on the five criteria, yes/no hallucination, free
   text). **Next ›** / **‹ Prev** swap only the article + summary at the top; the
   `?` button reopens the guide at any time. Scores autosave as you go (and a
   **Save** button forces an immediate save).

---

## Sending a pack to a reviewer (e.g. a supervisor)

Build the zip with the packaging script — it handles the folder layout,
points the shipped config at the right place, writes a plain-language
`START_HERE.md` for the reviewer, and refuses to produce a zip if it finds an
unblinding key inside it:

```powershell
.\make-reviewer-package.ps1 -Pack human1
```

Output: `review-ui/dist/review-ui-human1.zip`. Full walkthrough — including
how to actually send the zip and what to do when it comes back filled in —
is in [docs/phase5/sending_the_review_ui.md](../docs/phase5/sending_the_review_ui.md).

**Heads-up on size:** the zip carries `node_modules` plus ~15 article PDFs per
pack, so expect a few MB to a few tens of MB per pack. That's normal — email
attachment limits usually won't fit it, use a shared drive link instead.

---

## Getting the scores back into the pipeline

The `unblinding_key_human*.json` files stay in your original export folder the
whole time. When a filled `humanN/` folder comes back:

1. **Drop it back into your original export root** — the folder that still holds
   the matching `unblinding_key_human1.json` (i.e. `data/pilot_human_review/` for
   pilot data, `data/human_review/` for the real study). Ingest needs the key
   sitting next to the folder.
2. Ingest (offline, no API cost):
   - **Pilot data** (has a `.pilot_export` marker):
     ```powershell
     python llm-sum/run_phase3.py ingest-pilot-human-review
     python llm-sum/run_phase3.py eval-report --markdown --human-reviews data/pilot_human_reviews.jsonl
     ```
   - **Real study:**
     ```powershell
     python llm-sum/run_phase3.py ingest-human-review
     python llm-sum/run_phase3.py eval-report --markdown
     ```

The UI writes the exact `SCORESHEET_FIELDS` columns ingest expects, so no
conversion is needed. `progress_human1.json` (the resume file) is ignored by
ingest — only `scoresheet_human*.xlsx` is discovered.

---

## Tests

```powershell
node --test
```
Covers pack listing, item-file serving, the blind guard (direct + path-traversal
attempts on the key), and the ingest-shaped `.xlsx` write-back.

---

## How it fits the repo

- Reads pack folders written by `llm-sum/human_review.py`
  (`write_review_folder`) — no backend changes.
- Writes `scoresheet_human1.xlsx` with the same `SCORESHEET_FIELDS` header the
  Python `render_scoresheet_xlsx` uses; verified read-compatible with the ingest
  reader (`_read_scoresheet_rows` → `_normalize_scoresheet_row`).
- Pure Node (`express`, `exceljs`) + vendored `marked` for markdown; nothing
  added to `requirements.txt`.
