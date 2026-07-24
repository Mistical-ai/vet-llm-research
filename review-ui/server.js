// server.js — the only component that touches disk.
//
// Responsibilities:
//   1. List reviewer packs (humanN/) under REVIEW_ROOT and preview their articles.
//   2. Serve each item's article.pdf/article.md + summary.md + the guide — and
//      NOTHING else (never an unblinding_key_*.json; never anything outside a
//      pack folder). The blind protocol is enforced here, in one place.
//   3. Write the reviewer's scores back into the pack's own
//      scoresheet_<pack>.xlsx (exact SCORESHEET_FIELDS header) so the existing
//      `ingest-human-review` / `ingest-pilot-human-review` command reads it
//      unchanged, plus a progress_<pack>.json for resume.

const express = require("express");
const path = require("path");
const fs = require("fs");
const fsp = require("fs/promises");
const ExcelJS = require("exceljs");
const { REVIEW_ROOT, PORT } = require("./config");

// The scoresheet columns, in the exact order and spelling ingest expects
// (mirrors llm-sum/human_review.py SCORESHEET_FIELDS). Ingest maps cells to
// headers BY NAME, but keeping the canonical order avoids any confusion.
const SCORESHEET_FIELDS = [
  "item_id",
  "article_title",
  "faithfulness",
  "completeness",
  "clinical_usefulness",
  "clarity",
  "safety",
  "hallucination_present",
  "hallucination_notes",
  "comment",
];
const CRITERIA = [
  "faithfulness",
  "completeness",
  "clinical_usefulness",
  "clarity",
  "safety",
];
// Only these three files are ever served out of an item folder. Everything
// else (and every key file) is refused.
const SERVABLE_ITEM_FILES = new Set(["article.pdf", "article.md", "summary.md"]);

const PACK_RE = /^human\d+$/;
const ITEM_RE = /^item_\d+$/;

// ---------------------------------------------------------------------------
// Path safety
// ---------------------------------------------------------------------------

// Resolve a pack folder, refusing anything that isn't a well-formed `humanN`
// name that actually lives directly under REVIEW_ROOT. Returns null on any
// violation (bad name, traversal, missing, or not a directory).
function safePackDir(pack) {
  if (typeof pack !== "string" || !PACK_RE.test(pack)) return null;
  const dir = path.resolve(REVIEW_ROOT, pack);
  // Containment check: the resolved path must sit directly inside REVIEW_ROOT.
  if (path.dirname(dir) !== path.resolve(REVIEW_ROOT)) return null;
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return null;
  return dir;
}

// ---------------------------------------------------------------------------
// Pack discovery + parsing
// ---------------------------------------------------------------------------

// Every humanN/ folder directly under REVIEW_ROOT, sorted numerically.
function listPackNames() {
  let entries;
  try {
    entries = fs.readdirSync(REVIEW_ROOT, { withFileTypes: true });
  } catch {
    return [];
  }
  return entries
    .filter((e) => e.isDirectory() && PACK_RE.test(e.name))
    .map((e) => e.name)
    .sort((a, b) => {
      const na = Number(a.replace("human", ""));
      const nb = Number(b.replace("human", ""));
      return na - nb;
    });
}

// Parse packet.md's item table into { item_id: {title, version} }. The table
// rows look like:  | `item_001` | Some article title | 1 of 3 | `item_001/` |
// Falls back to an empty map if packet.md is missing/unparseable; the folder
// scan below still yields the item list.
function parsePacket(packDir) {
  const map = new Map();
  let text;
  try {
    text = fs.readFileSync(path.join(packDir, "packet.md"), "utf-8");
  } catch {
    return map;
  }
  const rowRe = /^\|\s*`?(item_\d+)`?\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*`?[^|]*`?\s*\|\s*$/;
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(rowRe);
    if (!m) continue;
    const [, itemId, title, version] = m;
    map.set(itemId, { title: title.trim(), version: version.trim() });
  }
  return map;
}

// The full item list for a pack: scan item_* folders, decide pdf vs md, and
// merge in the title/version from packet.md when available.
function packItems(packDir) {
  const packet = parsePacket(packDir);
  let entries;
  try {
    entries = fs.readdirSync(packDir, { withFileTypes: true });
  } catch {
    return [];
  }
  const items = entries
    .filter((e) => e.isDirectory() && ITEM_RE.test(e.name))
    .map((e) => e.name)
    .sort();
  return items.map((itemId) => {
    const itemDir = path.join(packDir, itemId);
    const hasPdf = fs.existsSync(path.join(itemDir, "article.pdf"));
    const hasMd = fs.existsSync(path.join(itemDir, "article.md"));
    const meta = packet.get(itemId) || { title: "", version: "" };
    return {
      item_id: itemId,
      title: meta.title,
      version: meta.version,
      article_kind: hasPdf ? "pdf" : hasMd ? "md" : "missing",
    };
  });
}

// Distinct article titles in first-seen order — the "5 articles" pack preview.
function distinctArticles(items) {
  const seen = new Set();
  const out = [];
  for (const it of items) {
    const t = (it.title || "").trim();
    if (t && !seen.has(t)) {
      seen.add(t);
      out.push(t);
    }
  }
  return out;
}

function progressPath(packDir, pack) {
  return path.join(packDir, `progress_${pack}.json`);
}

function readProgress(packDir, pack) {
  try {
    return JSON.parse(fs.readFileSync(progressPath(packDir, pack), "utf-8"));
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Scoresheet write-back (exceljs)
// ---------------------------------------------------------------------------

// Build the article_title cell the way the export does — plain title, plus a
// blind-safe "— summary version k of n" suffix when the article repeats. Ingest
// ignores this column entirely, so it is purely a reviewer-usability label.
function titleCell(item) {
  const title = (item.title || "").trim();
  const v = (item.version || "").trim();
  const m = v.match(/^(\d+)\s+of\s+(\d+)$/);
  if (m && Number(m[2]) > 1) {
    const suffix = `summary version ${m[1]} of ${m[2]}`;
    return title ? `${title} — ${suffix}` : suffix;
  }
  return title;
}

// Rewrite scoresheet_<pack>.xlsx from the posted scores. One row per item in
// the pack (blank cells stay blank); header names + values are all ingest needs.
async function writeScoresheet(packDir, pack, items, scores) {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet("scoresheet");
  ws.addRow(SCORESHEET_FIELDS);
  ws.getRow(1).font = { bold: true };
  for (const item of items) {
    const s = (scores && scores[item.item_id]) || {};
    const row = SCORESHEET_FIELDS.map((field) => {
      if (field === "item_id") return item.item_id;
      if (field === "article_title") return titleCell(item);
      const val = s[field];
      return val === undefined || val === null ? "" : String(val);
    });
    ws.addRow(row);
  }
  const outPath = path.join(packDir, `scoresheet_${pack}.xlsx`);
  await wb.xlsx.writeFile(outPath);
  return outPath;
}

// ---------------------------------------------------------------------------
// App + routes
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json({ limit: "2mb" }));

// Belt-and-braces: refuse any request that even mentions an unblinding key,
// regardless of how it reached the router.
app.use((req, res, next) => {
  if (/unblinding_key/i.test(decodeURIComponent(req.path))) {
    return res.status(403).json({ error: "forbidden" });
  }
  next();
});

// Static frontend (public/ only — never the repo root, config, or server code).
app.use(express.static(path.join(__dirname, "public")));

// List packs + their 5-article previews.
app.get("/api/packs", (req, res) => {
  const packs = listPackNames().map((name) => {
    const dir = safePackDir(name);
    if (!dir) return { name, articles: [], itemCount: 0 };
    const items = packItems(dir);
    return { name, articles: distinctArticles(items), itemCount: items.length };
  });
  res.json({ reviewRoot: REVIEW_ROOT, packs });
});

// One pack's full detail: items, the guide (raw markdown), and any saved progress.
app.get("/api/pack/:pack", (req, res) => {
  const dir = safePackDir(req.params.pack);
  if (!dir) return res.status(404).json({ error: "pack not found" });
  const items = packItems(dir);
  let guideMarkdown = "";
  try {
    guideMarkdown = fs.readFileSync(path.join(dir, "REVIEWER_GUIDE.md"), "utf-8");
  } catch {
    guideMarkdown = "_(REVIEWER_GUIDE.md not found in this pack.)_";
  }
  res.json({
    pack: req.params.pack,
    items,
    guideMarkdown,
    progress: readProgress(dir, req.params.pack),
  });
});

// Serve one item file — strictly article.pdf | article.md | summary.md.
app.get("/packs/:pack/:item/:file", (req, res) => {
  const { pack, item, file } = req.params;
  const dir = safePackDir(pack);
  if (!dir) return res.status(404).end();
  if (!ITEM_RE.test(item) || !SERVABLE_ITEM_FILES.has(file)) {
    return res.status(404).end();
  }
  const filePath = path.resolve(dir, item, file);
  // Final containment check: the resolved file must live inside this pack.
  if (path.relative(dir, filePath).startsWith("..")) return res.status(404).end();
  if (!fs.existsSync(filePath)) return res.status(404).end();
  if (file === "article.pdf") res.type("application/pdf");
  else res.type("text/markdown; charset=utf-8");
  res.sendFile(filePath);
});

// Save scores: rewrite the pack's scoresheet + autosave progress for resume.
app.post("/api/pack/:pack/save", async (req, res) => {
  const dir = safePackDir(req.params.pack);
  if (!dir) return res.status(404).json({ error: "pack not found" });
  const pack = req.params.pack;
  const scores = (req.body && req.body.items) || {};
  const reviewerLabel = (req.body && req.body.reviewerLabel) || pack;
  const items = packItems(dir);
  try {
    const outPath = await writeScoresheet(dir, pack, items, scores);
    // Autosave the raw scores for resume (invisible to ingest — the scoresheet
    // discovery globs only match scoresheet_human*.{xlsx,csv}).
    await fsp.writeFile(
      progressPath(dir, pack),
      JSON.stringify({ reviewerLabel, savedAt: new Date().toISOString(), items: scores }, null, 2),
      "utf-8",
    );
    res.json({ ok: true, scoresheet: path.basename(outPath) });
  } catch (err) {
    console.error("save failed:", err);
    res.status(500).json({ error: String(err && err.message ? err.message : err) });
  }
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log("");
    console.log("  Human Validation Review UI");
    console.log("  --------------------------");
    console.log(`  Serving packs from: ${REVIEW_ROOT}`);
    console.log(`  Open your browser at: http://localhost:${PORT}`);
    console.log("");
    console.log("  (Leave this window open while reviewing. Close it when done.)");
    console.log("");
  });
}

module.exports = { app, SCORESHEET_FIELDS, safePackDir, parsePacket, packItems, writeScoresheet };
