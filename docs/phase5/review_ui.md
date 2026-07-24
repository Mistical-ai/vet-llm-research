# Human Validation Review UI — access guide, accessibility, and how it works

**Role of this document:** a complete reference for `review-ui/`, the local
offline web app reviewers use to score blind summary packets instead of
hand-opening PDFs/markdown/Excel. Covers three things: how accessible it
actually is (with real gaps named, not glossed over), how to get to it (you,
and a reviewer you send it to), and how it works end to end. For the export/
ingest machinery this UI sits on top of, see
[human_validation.md](human_validation.md) and
[pilot_human_review.md](pilot_human_review.md) — this doc does not repeat that
material. For the step-by-step of actually packaging and sending a pack to a
reviewer, see [sending_the_review_ui.md](sending_the_review_ui.md) — §2 below
is the short version.

---

## 1. How accessible is it? (short answer: easy to launch, not screen-reader accessible)

"Accessible" means two different things here, and the UI scores very
differently on each.

### 1a. Ease of access for a non-technical reviewer — good

The whole point of this app is that a reviewer with no coding background can
use it:

- **One prerequisite:** Node.js installed (a one-time, one-click Windows
  installer — no command-line setup needed after that).
- **One action to launch:** double-click `start.bat`. It starts a local
  server and opens the default browser to `http://localhost:5173`
  automatically. No terminal commands required.
- **No account, no login, no internet connection needed.** Everything —
  articles, summaries, the guide — is a plain file already on the reviewer's
  disk. Nothing is uploaded anywhere.
- **No install step beyond Node itself.** The zip you send bundles
  `node_modules/` already installed, so there's no `npm install` for the
  reviewer to run.
- **Self-explanatory flow.** Pick your pack → read the guide → score. No
  hidden menus, no configuration screens.

This is the sense of "accessible" the whole build was optimized for, and it
holds up.

### 1b. Assistive-technology / WCAG-style accessibility — has real gaps

This was **not** a design goal when the UI was built, and an honest read of
the markup turns up concrete gaps a screen-reader or keyboard-only user would
hit. None of these are exotic to fix, but none have been fixed yet either —
listed here so you know exactly what's true today:

| Gap | What actually happens | Who's affected |
|---|---|---|
| Scoresheet inputs have no labels | Each score cell is a bare `<select>`/`<input>` with no `<label>`/`aria-label` tying it to its criterion + item. A screen reader announces "combobox" with no context. | Screen reader users |
| No `aria-live` regions | "Saving…" / "Saved ✓" status and the toast notification update visually only (`textContent`); nothing is announced to assistive tech. | Screen reader users |
| Help modal isn't a real dialog | No `role="dialog"`, `aria-modal="true"`, focus isn't moved into it on open or returned to the `?` button on close, and there's no focus trap (Tab can escape behind the overlay). Only closes via mouse-click or Escape. | Screen reader + keyboard-only users |
| Clicking an `item_id` to jump rows is mouse-only | The scoresheet's `item_id` cell (click-to-jump) is a `<td>` with an `onclick`, not a `<button>` — no `tabindex`, no keyboard handler. | Keyboard-only users |
| No focus management between screens | Switching pack picker → intro → scoring doesn't move focus to the new screen's heading, so a keyboard/screen-reader user's focus can be left behind. | Screen reader + keyboard-only users |
| Current-item row is a color highlight only | The active row in the scoresheet is marked by a CSS class (light blue background), not `aria-current` or any text cue. | Screen reader + low-vision users relying on high-contrast modes |
| Small touch targets, no mobile layout | The `?` button is 34×34px (below the ~44px touch-target guideline); the whole 4-pane layout has no responsive breakpoint and assumes a full desktop-sized window. | Touch/mobile users, motor-impairment users |
| Color contrast not independently audited | Colors were chosen for a clean look, not run through a contrast checker. Body text and primary buttons look fine visually but haven't been verified against WCAG AA thresholds. | Low-vision users |

**What already works reasonably well, by contrast:** every interactive
control that *is* keyboard-native (buttons, `<select>`, `<input>`) is a real
HTML element, so Tab order and Enter/Space activation work without any extra
code. The scoresheet is a genuine `<table>` with `<thead>`/`<tbody>`, so a
screen reader can navigate it as a table (row/column announcements), just
without labeled cells. `<html lang="en">` and a real `<title>` are set. PDF
figures/tables are shown via the browser's own PDF viewer, which has its own
(inconsistent, browser-dependent) accessibility — outside this app's control,
and dependent on whether the source PDF itself is tagged.

**Bottom line:** if your supervisor uses a mouse/trackpad and a standard
browser, none of this matters — the flow is smooth. If they rely on a screen
reader, switch access, or keyboard-only navigation, several parts of the
scoring screen will be hard or impossible to use as-is. None of the fixes
above are large (labels, `aria-live`, a proper modal, one keyboard handler) —
say the word and they can be added; they just weren't in scope for the
original build.

### 1c. Device / browser / network requirements

- **OS:** built and tested on Windows; the server itself (Node) is
  cross-platform, so macOS/Linux would work too, just untested here.
- **Browser:** any modern Chrome, Edge, or Firefox. PDF rendering relies on
  the browser's built-in PDF viewer (`<iframe src="article.pdf">`) — all
  three render inline reliably; Safari has historically been less consistent
  with inline PDF frames.
- **Screen size:** designed for a normal laptop/desktop window (4-pane
  layout: PDF, summary, and a fixed scoresheet all visible without scrolling
  the page itself). Not usable on a phone.
- **Network:** none needed once launched — everything is served from
  `localhost`. No data leaves the machine.

---

## 2. How to access it

### 2a. On your own machine (development / trying it yourself)

1. Make sure Node.js is installed: open a **fresh** terminal (must be opened
   *after* Node was installed, or it won't see it — see §4 below) and run
   `node --version`.
2. From the repo root:
   ```powershell
   cd review-ui
   npm install     # first time only
   npm start        # or just double-click start.bat
   ```
3. Open `http://localhost:5173` in your browser.
4. By default it reads packs from `data/pilot_human_review/`. To point it at
   the real study export instead:
   ```powershell
   $env:REVIEW_ROOT = "C:\...\vet-llm-research\data\human_review"; npm start
   ```

### 2b. Sending it to a reviewer (e.g. your supervisor)

```powershell
cd review-ui
.\make-reviewer-package.ps1 -Pack human1
```

This builds `review-ui/dist/review-ui-human1.zip`: the app + `node_modules/`,
only the named pack (never an `unblinding_key_*.json` — the script verifies
the finished zip's actual contents and refuses to produce it if one leaked
in), a `config.js` pre-pointed at the right folder, and a plain-language
`START_HERE.md` for the reviewer. They unzip anywhere, double-click
`start.bat`, and a browser opens to the pack picker — no further setup.

Full walkthrough (including how to actually transfer the zip, and what to do
when it comes back filled in) is in
[sending_the_review_ui.md](sending_the_review_ui.md).

---

## 3. How it works

### 3a. Architecture

A small **Node + Express server** (`review-ui/server.js`) is the only thing
that touches disk. It serves a **plain HTML/CSS/JS frontend**
(`review-ui/public/`) — no build step, no framework, no bundler — plus a
vendored copy of `marked` (markdown → HTML) so nothing is fetched from a CDN
at runtime. PDFs render via the browser's native inline PDF viewer.

```
review-ui/
  server.js        Express app: pack discovery, file serving, save endpoint
  config.js         REVIEW_ROOT (which folder holds humanN/ packs) + PORT
  public/
    index.html      the 3 screens (pack picker / intro / scoring), all in one page
    app.js           client-side state machine + rendering
    styles.css       4-pane grid + sticky scoresheet layout
    vendor/marked.umd.js
  start.bat          double-click launcher (falls back to full Node path)
```

### 3b. The three screens

1. **Pack picker** — `GET /api/packs` lists every `humanN/` folder found
   directly under `REVIEW_ROOT`, along with the 5 distinct article titles
   parsed out of that pack's `packet.md` (so packs are discovered fresh on
   every page load — export a `human3/` later and it just appears).
2. **Intro** — `GET /api/pack/:pack` returns that pack's items plus the full
   text of `REVIEWER_GUIDE.md`, rendered client-side with `marked`. The
   reviewer identity shown ("You are reviewing as **human1**") is a fixed
   label, not an editable dropdown — see §3e for why.
3. **Scoring** — a CSS grid: top-left is `article.pdf` in an `<iframe>` (or
   rendered `article.md` when no PDF was resolved at export time), top-right
   is the rendered `summary.md`. **Next ›** / **‹ Prev** swap only those two
   panes; the scoresheet underneath spans the full width and never moves.
   The `?` button reopens the guide in a modal (Escape or the Close button
   dismisses it).

### 3c. Saving — see the separate walkthrough above in this conversation for
the exact debounce/autosave mechanics. In short: every score change autosaves
~800ms after you stop typing; the **Save** button forces an immediate write.
Both paths call `POST /api/pack/:pack/save`, which rewrites
`scoresheet_<pack>.xlsx` from scratch (via `exceljs`) with the exact column
header `ingest-human-review` expects, plus a `progress_<pack>.json` used only
for resuming your place — ingest never reads that file.

### 3d. Blind protocol enforcement

The server refuses to serve `unblinding_key_*.json` under any request path —
including path-traversal attempts — and only ever serves `article.pdf`,
`article.md`, or `summary.md` from inside a validated `humanN/item_NNN/`
folder. This is enforced in the server (not just "the frontend doesn't link
to it"), and is covered by an automated test
(`review-ui/test/server.test.js`).

### 3e. Identity ↔ pack coupling

Each `humanN/` pack has its own private `unblinding_key_humanN.json` mapping
`item_001`, `item_002`, … back to which AI wrote each summary. `human2`'s
`item_001` is a *different* (article, summary) pair from `human1`'s
`item_001`. Because of that, the UI locks reviewer identity to whichever pack
was opened — there's no way to score `human1`'s pack but save it under
`human2`'s name, which would silently misattribute scores to the wrong key
during ingest.

### 3f. Compatibility with the existing pipeline

No backend code changed. The `.xlsx` this UI writes was verified — both by an
automated test and by a live round-trip through the real Python reader
(`human_review._read_scoresheet_rows` → `_normalize_scoresheet_row`) — to be
byte-for-byte compatible with what `ingest-human-review` /
`ingest-pilot-human-review` already expect. See
[README_UI.md](../../review-ui/README_UI.md) for the exact ingest commands.

---

## 4. One gotcha worth knowing: stale terminal PATH

If a terminal says `node`/`npm` "is not recognized" right after installing
Node, it's not broken — that terminal (or the whole app that spawned it, e.g.
Cursor/VS Code) was already running before the installer updated the system
PATH, so it's holding a stale environment snapshot. Closing a single terminal
tab and opening a new one **does not** fix this if the parent app itself was
already running — the whole app (not just the terminal) needs to restart so
it re-reads the updated PATH. `start.bat` is written to sidestep this
entirely by trying the full install path (`C:\Program Files\nodejs\node.exe`)
as a fallback, so a reviewer double-clicking it never hits this problem.
