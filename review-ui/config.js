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
};
