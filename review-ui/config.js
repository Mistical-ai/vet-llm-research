// config.js — the two knobs the whole app reads.
//
// REVIEW_ROOT is the folder that DIRECTLY contains the reviewer packs
// (human1/, human2/, ...). It must contain those humanN/ folders themselves,
// not a parent of them — if it points somewhere else the app will report
// "no packs found."
//
//   - In this repo (developing / testing), it defaults to
//     data/pilot_human_review, which already has human1/ and human2/.
//   - For the REAL study, set REVIEW_ROOT to data/human_review once you have
//     run `export-human-review` (env var, or edit the default below).
//   - In the zip you send a supervisor, it points at the sibling `packs/`
//     folder that travels inside the zip.
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
  // Default: the pilot packs that already exist in this repo.
  return path.resolve(__dirname, "..", "data", "pilot_human_review");
}

module.exports = {
  REVIEW_ROOT: resolveReviewRoot(),
  PORT: process.env.PORT ? Number(process.env.PORT) : 5173,
  SENDER_CONTACT: process.env.SENDER_CONTACT || "Pranav Shah <pshah10@uoguelph.ca>",
};
