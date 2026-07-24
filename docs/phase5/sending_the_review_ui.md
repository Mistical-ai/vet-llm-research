# Sending the Review UI to your supervisor — operator guide

**Who this is for:** you, packaging a reviewer pack to send. For the
non-technical instructions your supervisor gets, see the separate
`START_HERE.md` that lands inside the zip (built automatically — §1). For how
the app works internally, see [review_ui.md](review_ui.md).

---

## 1. Build the zip (one command)

From `review-ui/`, run:

```powershell
.\make-reviewer-package.ps1 -Pack human1
```

This does everything for you:

- Copies `review-ui/` (app code + already-installed `node_modules/`) into a
  throwaway staging folder — your real `review-ui/` folder is never modified.
- Copies **only** the pack you named (e.g. `human1/`) into a sibling `packs/`
  folder — never the `unblinding_key_human1.json` that lives outside it.
- Points the shipped `config.js` at that sibling `packs/` folder, so it works
  out of the box wherever your supervisor unzips it.
- Writes a plain-language `START_HERE.md` at the top of the zip.
- Zips everything, then **checks the zip's actual contents** for any stray
  `unblinding_key_*.json` and refuses to produce the zip (deletes it and
  throws) if it finds one. This is a real safety net, not just a promise —
  it inspects the finished archive, not just the source folders.
- Cleans up the staging folder either way.

Output lands at `review-ui/dist/review-ui-human1.zip`.

**Sending a different pack, or the real study once it's exported:**

```powershell
# another pilot pack
.\make-reviewer-package.ps1 -Pack human2

# the real study export (once you've run export-human-review yourself)
.\make-reviewer-package.ps1 -Pack human1 -SourceRoot "..\data\human_review"
```

`node_modules/` must already exist in `review-ui/` (`npm install`, one time)
— the script checks for it and stops with a clear message if it's missing.

---

## 2. Before you actually send it — a 30-second sanity check

The script's own guard already checked for a leaked key, but it's cheap
insurance to look yourself:

```powershell
# List everything in the zip; eyeball it — should be review-ui/, packs/humanN/,
# START_HERE.md, and NOTHING named unblinding_key_*.
Expand-Archive -Path .\dist\review-ui-human1.zip -DestinationPath .\dist\_peek -Force
Get-ChildItem .\dist\_peek -Recurse -Name | Select-String -Pattern "unblinding" 
# ^ should print NOTHING
Remove-Item .\dist\_peek -Recurse -Force
```

If that `Select-String` line prints anything, stop and don't send it —
something is wrong and worth investigating before it leaves your machine.

---

## 3. Send it

The zip will be several MB to a few tens of MB (mostly `node_modules` +
~15 article PDFs). That's too big for most email attachment limits
(Outlook/Gmail are typically capped around 20–25MB), so pick whichever of
these you already have:

- **OneDrive** (you're already working inside a OneDrive-synced folder) —
  drop the zip in a OneDrive folder, right-click → "Share", and send your
  supervisor the link. No size limit to worry about.
- **Any other shared drive / Google Drive / Dropbox** you both already use —
  same idea.
- **A direct file-transfer tool** if your institution has one.

Whichever you use, send **only the zip** — don't separately attach anything
else, and specifically don't attach `unblinding_key_human1.json` (it isn't in
the zip in the first place, but worth saying explicitly: it should never
leave your machine by any channel).

**What to tell them in the email/message itself** (keep it short — the zip's
`START_HERE.md` has the real instructions):

> Hi [name], attached/linked is your review packet. Unzip it anywhere, open
> the folder, and read `START_HERE.md` first — it walks you through
> everything. When you run `start.bat`, Windows or your antivirus may show a
> warning since it's not from an app store — that's expected, click "More
> info" → "Run anyway" (or "Allow" in antivirus). Let me know if anything
> doesn't open.

**Why this warning appears at all:** `start.bat`/`node.exe` aren't digitally
code-signed by a recognized publisher (that requires a paid certificate),
which is exactly what triggers Windows SmartScreen and some antivirus
heuristics on *any* unsigned executable from outside an app store — it isn't
a sign of anything actually wrong with the app, and `start.bat`'s own
`START_HERE.md` warns them about it too so it isn't a surprise mid-review.

---

## 4. When they send it back

They'll return their `packs/humanN/` folder (or just the `humanN/` folder —
either is fine), now containing a filled `scoresheet_humanN.xlsx`.

1. **Drop that `humanN/` folder into your original export root** — the one
   that still has the matching `unblinding_key_humanN.json` sitting next to
   it (`data/pilot_human_review/` for pilot data, `data/human_review/` for
   the real study). Ingest needs the key alongside the folder — it can't
   un-blind scores without it. Overwrite the blank folder that's already
   there with their filled one (or just copy their `scoresheet_humanN.xlsx`
   over the blank one — that's the only file that changed).
2. Ingest (offline, no cost):
   ```powershell
   # pilot data
   python llm-sum/run_phase3.py ingest-pilot-human-review
   python llm-sum/run_phase3.py eval-report --markdown --human-reviews data/pilot_human_reviews.jsonl

   # real study
   python llm-sum/run_phase3.py ingest-human-review
   python llm-sum/run_phase3.py eval-report --markdown
   ```

---

## 5. Re-sending / sending to a second reviewer

Nothing to update in the app — just run the script again with a different
`-Pack`. If you export a brand-new pack later (`human3`, `human4`, …), the
UI already discovers it automatically (see the earlier discussion in this
conversation) — you only need to re-run the packaging script naming that new
pack when you're ready to send it.
