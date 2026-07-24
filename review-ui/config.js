// config.js — the two knobs the whole app reads.
//
// REVIEW_ROOT is the folder that DIRECTLY contains the reviewer packs
// (human1/, human2/, ...). It must contain those humanN/ folders themselves,
// not a parent of them — if it points somewhere else the app will report
// "no packs found."
//
//   - Defaults to data/human_review — the REAL study export (set once
//     `export-human-review` had produced data/human_review/human1/).
//   - To go back to rehearsing on the pilot dev pool instead, set
//     REVIEW_ROOT to data/pilot_human_review (env var, or edit below).
//   - In the zip you send a supervisor, it points at the sibling `packs/`
//     folder that travels inside the zip (make-reviewer-package.ps1 patches
//     this line automatically — see the regex it matches against, below).
//
// Override without editing this file:  set REVIEW_ROOT=...  (PowerShell:
//   $env:REVIEW_ROOT="C:\path\to\packs"; node server.js)
//
// SENDER_CONTACT (optional) — shown to the reviewer on the "I'm finished"
// screen's send-back instructions, e.g. "Send that packs.zip file back to
// Jane (jane@example.com)". Leave blank to use the generic "the same way
// you received this one" wording instead. Set it here before packaging with
// make-reviewer-package.ps1 if you want the zip you send to name you
// specifically.

const path = require("path");

function resolveReviewRoot() {
  if (process.env.REVIEW_ROOT && process.env.REVIEW_ROOT.trim()) {
    return path.resolve(process.env.REVIEW_ROOT.trim());
  }
  // Default: the real study export.
  return path.resolve(__dirname, "..", "data", "human_review");
}

module.exports = {
  REVIEW_ROOT: resolveReviewRoot(),
  PORT: process.env.PORT ? Number(process.env.PORT) : 5173,
  SENDER_CONTACT: process.env.SENDER_CONTACT || "Pranav Shah <pshah10@uoguelph.ca>",
};
