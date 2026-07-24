// server.test.js — hermetic tests for the review-ui server.
//
// Builds a throwaway REVIEW_ROOT fixture (a fake human1 pack + a sibling
// unblinding key that must NEVER be served), then exercises the real Express
// app against it. No network, no live data touched.
//
// Run:  node --test   (from review-ui/)

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const ExcelJS = require("exceljs");

// --- build the fixture and point REVIEW_ROOT at it BEFORE requiring server ---
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "review-ui-test-"));
process.env.REVIEW_ROOT = TMP;
process.env.PORT = "0";

function writeFixture() {
  // A sibling key that the server must refuse to serve.
  fs.writeFileSync(path.join(TMP, "unblinding_key_human1.json"),
    JSON.stringify({ items: { item_001: { summarizer: "SECRET_MODEL" } } }));

  const pack = path.join(TMP, "human1");
  fs.mkdirSync(path.join(pack, "item_001"), { recursive: true });
  fs.writeFileSync(path.join(pack, "REVIEWER_GUIDE.md"), "# Guide\n\nScore each item 1-5.\n");
  fs.writeFileSync(path.join(pack, "packet.md"),
    "## Items\n\n| item_id | article | version | folder |\n| --- | --- | --- | --- |\n" +
    "| `item_001` | A study of cats | 1 of 1 | `item_001/` |\n");
  fs.writeFileSync(path.join(pack, "item_001", "article.md"), "# A study of cats\n\nFull text here.\n");
  fs.writeFileSync(path.join(pack, "item_001", "summary.md"), "# item_001 summary\n\nThe candidate summary.\n");
}
writeFixture();

const { app } = require("../server");

let server;
let base;
before(async () => {
  await new Promise((resolve) => {
    server = app.listen(0, () => {
      base = `http://127.0.0.1:${server.address().port}`;
      resolve();
    });
  });
});
after(() => { server.close(); fs.rmSync(TMP, { recursive: true, force: true }); });

test("GET /api/packs lists human1 with its article preview", async () => {
  const res = await fetch(base + "/api/packs");
  assert.equal(res.status, 200);
  const data = await res.json();
  const names = data.packs.map((p) => p.name);
  assert.ok(names.includes("human1"), "human1 should be listed");
  const h1 = data.packs.find((p) => p.name === "human1");
  assert.deepEqual(h1.articles, ["A study of cats"]);
  assert.equal(h1.itemCount, 1);
});

test("GET /api/pack/human1 returns items + guide", async () => {
  const res = await fetch(base + "/api/pack/human1");
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.items.length, 1);
  assert.equal(data.items[0].item_id, "item_001");
  assert.equal(data.items[0].article_kind, "md");
  assert.match(data.guideMarkdown, /Score each item/);
});

test("item files are served (summary.md, article.md)", async () => {
  const s = await fetch(base + "/packs/human1/item_001/summary.md");
  assert.equal(s.status, 200);
  assert.match(await s.text(), /candidate summary/);
  const a = await fetch(base + "/packs/human1/item_001/article.md");
  assert.equal(a.status, 200);
});

test("BLIND: unblinding key is never served (direct)", async () => {
  const res = await fetch(base + "/unblinding_key_human1.json");
  assert.equal(res.status, 403);
});

test("BLIND: unblinding key is never served (path traversal)", async () => {
  // Raw, un-normalized traversal attempt — server must not leak the key.
  const res = await fetch(base + "/packs/human1/item_001/..%2f..%2funblinding_key_human1.json");
  assert.notEqual(res.status, 200);
  const body = await res.text();
  assert.ok(!/SECRET_MODEL/.test(body), "response must not contain key contents");
});

test("a non-servable item file is refused", async () => {
  const res = await fetch(base + "/packs/human1/item_001/unblinding_key_human1.json");
  assert.notEqual(res.status, 200);
});

test("POST save writes an ingest-shaped scoresheet_human1.xlsx", async () => {
  const scores = {
    item_001: {
      faithfulness: "4", completeness: "3", clinical_usefulness: "5",
      clarity: "4", safety: "5", hallucination_present: "no",
      hallucination_notes: "", comment: "solid",
    },
  };
  const res = await fetch(base + "/api/pack/human1/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviewerLabel: "human1", items: scores }),
  });
  assert.equal(res.status, 200);

  const xlsxPath = path.join(TMP, "human1", "scoresheet_human1.xlsx");
  assert.ok(fs.existsSync(xlsxPath), "scoresheet_human1.xlsx should exist");

  // Read it back the way ingest does: header row + values by column name.
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.readFile(xlsxPath);
  const ws = wb.worksheets[0];
  const header = ws.getRow(1).values.slice(1); // exceljs is 1-indexed
  assert.deepEqual(header, [
    "item_id", "article_title", "faithfulness", "completeness",
    "clinical_usefulness", "clarity", "safety", "hallucination_present",
    "hallucination_notes", "comment",
  ]);
  const row = ws.getRow(2).values.slice(1);
  assert.equal(row[0], "item_001");
  assert.equal(String(row[2]), "4"); // faithfulness
  assert.equal(String(row[7]), "no"); // hallucination_present

  // Progress file written for resume, and it is NOT a scoresheet (ingest ignores it).
  assert.ok(fs.existsSync(path.join(TMP, "human1", "progress_human1.json")));
});

test("resume: saved idx (last-viewed item) round-trips through GET /api/pack", async () => {
  const res1 = await fetch(base + "/api/pack/human1/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviewerLabel: "human1", items: {}, idx: 0 }),
  });
  assert.equal(res1.status, 200);

  const progress = JSON.parse(fs.readFileSync(path.join(TMP, "human1", "progress_human1.json"), "utf-8"));
  assert.equal(progress.idx, 0);

  const get1 = await (await fetch(base + "/api/pack/human1")).json();
  assert.equal(get1.progress.idx, 0);

  // A malformed/missing idx in the request body must not crash the save.
  const res2 = await fetch(base + "/api/pack/human1/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviewerLabel: "human1", items: {} }),
  });
  assert.equal(res2.status, 200);
  const get2 = await (await fetch(base + "/api/pack/human1")).json();
  assert.equal(get2.progress.idx, 0, "missing idx should default to 0, not crash or persist garbage");
});
