// app.js — client-side state machine: pack picker → intro/guide → scoring.
// Talks to the local server (server.js) for pack data and score saving.

const CRITERIA = ["faithfulness", "completeness", "clinical_usefulness", "clarity", "safety"];
const SCORE_COLUMNS = [...CRITERIA]; // 1-5 dropdowns
const SHEET_COLUMNS = [
  { key: "item_id", label: "item_id" },
  { key: "article_title", label: "article" },
  { key: "faithfulness", label: "faithful" },
  { key: "completeness", label: "complete" },
  { key: "clinical_usefulness", label: "clinical use" },
  { key: "clarity", label: "clarity" },
  { key: "safety", label: "safety" },
  { key: "hallucination_present", label: "halluc?" },
  { key: "hallucination_notes", label: "halluc notes" },
  { key: "comment", label: "comment" },
];

const state = {
  pack: null,
  items: [],
  idx: 0,
  scores: {}, // item_id -> { field: value }
  guideMarkdown: "",
  saveTimer: null,
};

// ---- tiny DOM helpers ----
const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  Object.assign(n, props);
  for (const k of kids) n.append(k);
  return n;
};
function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $("#" + id).classList.add("active");
}
function md(text) {
  // marked is loaded as a UMD global.
  return window.marked ? window.marked.parse(text || "") : (text || "");
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1800);
}

// ---- screen 1: pack picker ----
async function loadPacks() {
  const list = $("#packs-list");
  try {
    const res = await fetch("/api/packs");
    const data = await res.json();
    if (!data.packs || !data.packs.length) {
      list.innerHTML = "";
      list.append(el("p", { className: "muted" },
        "No reviewer packs found. The app is reading: " + (data.reviewRoot || "(unknown)") +
        " — it must directly contain human1/, human2/, … folders."));
      return;
    }
    list.innerHTML = "";
    for (const pack of data.packs) {
      const preview = el("ul", { className: "preview" });
      for (const title of pack.articles) preview.append(el("li", {}, title));
      const card = el("div", { className: "pack-card" },
        el("h2", {}, pack.name),
        preview,
        el("div", { className: "card-foot" },
          el("span", { className: "count" }, `${pack.itemCount} items to score`),
          el("button", { className: "primary", onclick: () => openPack(pack.name) },
            `Review as ${pack.name} →`)));
      list.append(card);
    }
  } catch (err) {
    list.innerHTML = "";
    list.append(el("p", { className: "muted" }, "Could not load packs: " + err));
  }
}

// ---- screen 2: intro / guide ----
async function openPack(pack) {
  const res = await fetch("/api/pack/" + encodeURIComponent(pack));
  if (!res.ok) { toast("Could not open " + pack); return; }
  const data = await res.json();
  state.pack = pack;
  state.items = data.items || [];
  state.idx = 0;
  state.scores = (data.progress && data.progress.items) || {};
  state.guideMarkdown = data.guideMarkdown || "";

  $("#intro-identity").textContent = pack;
  $("#scoring-identity").textContent = pack;
  $("#guide-content").innerHTML = md(state.guideMarkdown);
  $("#help-content").innerHTML = md(state.guideMarkdown);
  showScreen("screen-intro");
  window.scrollTo(0, 0);
}

// ---- screen 3: scoring ----
function startScoring() {
  buildSheet();
  renderItem();
  showScreen("screen-scoring");
}

function buildSheet() {
  const thead = $("#scoresheet thead");
  const tbody = $("#scoresheet tbody");
  thead.innerHTML = "";
  tbody.innerHTML = "";
  thead.append(el("tr", {}, ...SHEET_COLUMNS.map((c) => el("th", {}, c.label))));

  state.items.forEach((item, i) => {
    const tr = el("tr", { id: "row-" + item.item_id });
    for (const col of SHEET_COLUMNS) {
      const td = el("td");
      if (col.key === "item_id") {
        td.className = "id-cell";
        td.textContent = item.item_id;
        td.title = "Jump to this item";
        td.onclick = () => { state.idx = i; renderItem(); };
      } else if (col.key === "article_title") {
        td.className = "article-cell";
        td.textContent = articleLabel(item);
      } else if (SCORE_COLUMNS.includes(col.key)) {
        td.append(makeSelect(item.item_id, col.key, ["", "1", "2", "3", "4", "5"]));
      } else if (col.key === "hallucination_present") {
        td.append(makeSelect(item.item_id, col.key, ["", "yes", "no"]));
      } else {
        td.append(makeInput(item.item_id, col.key));
      }
      tr.append(td);
    }
    tbody.append(tr);
  });
}

function articleLabel(item) {
  const title = (item.title || "").trim() || "(untitled)";
  const m = (item.version || "").match(/^(\d+)\s+of\s+(\d+)$/);
  return m && Number(m[2]) > 1 ? `${title} — v${m[1]} of ${m[2]}` : title;
}

function getScore(itemId, field) {
  return (state.scores[itemId] && state.scores[itemId][field]) || "";
}
function setScore(itemId, field, value) {
  if (!state.scores[itemId]) state.scores[itemId] = {};
  state.scores[itemId][field] = value;
  scheduleSave();
}

function makeSelect(itemId, field, options) {
  const sel = el("select");
  for (const o of options) sel.append(el("option", { value: o, textContent: o || "—" }));
  sel.value = getScore(itemId, field);
  sel.onchange = () => setScore(itemId, field, sel.value);
  return sel;
}
function makeInput(itemId, field) {
  const inp = el("input", { className: "notes", type: "text", value: getScore(itemId, field) });
  inp.oninput = () => setScore(itemId, field, inp.value);
  return inp;
}

function renderItem() {
  const item = state.items[state.idx];
  if (!item) return;
  $("#item-indicator").textContent = `Item ${state.idx + 1} of ${state.items.length} — ${item.item_id}`;
  $("#prev-btn").disabled = state.idx === 0;
  $("#next-btn").disabled = state.idx === state.items.length - 1;

  // Article pane: PDF iframe, or rendered markdown fallback, or missing note.
  const artPane = $("#article-pane");
  artPane.innerHTML = "";
  const base = `/packs/${encodeURIComponent(state.pack)}/${encodeURIComponent(item.item_id)}`;
  if (item.article_kind === "pdf") {
    artPane.append(el("iframe", { src: base + "/article.pdf", title: "article PDF" }));
  } else if (item.article_kind === "md") {
    fetchText(base + "/article.md").then((t) => {
      artPane.innerHTML = `<div class="markdown">${md(t)}</div>`;
    });
  } else {
    artPane.append(el("div", { className: "pane-fallback" }, "No article file found for this item."));
  }

  // Summary pane.
  const sumPane = $("#summary-pane");
  sumPane.innerHTML = '<p class="pane-fallback">Loading summary…</p>';
  fetchText(base + "/summary.md").then((t) => { sumPane.innerHTML = md(t); });

  // Highlight current row in the sheet and scroll it into view.
  document.querySelectorAll("#scoresheet tr.current").forEach((r) => r.classList.remove("current"));
  const row = $("#row-" + item.item_id);
  if (row) { row.classList.add("current"); row.scrollIntoView({ block: "nearest" }); }
}

async function fetchText(url) {
  try { const r = await fetch(url); return r.ok ? await r.text() : "_(file not found)_"; }
  catch { return "_(could not load file)_"; }
}

function goNext() { if (state.idx < state.items.length - 1) { state.idx++; renderItem(); } }
function goPrev() { if (state.idx > 0) { state.idx--; renderItem(); } }

// ---- saving ----
function scheduleSave() {
  setSaveStatus("saving");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNow, 800);
}
async function saveNow() {
  clearTimeout(state.saveTimer);
  try {
    const res = await fetch("/api/pack/" + encodeURIComponent(state.pack) + "/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewerLabel: state.pack, items: state.scores }),
    });
    if (!res.ok) throw new Error("save failed (" + res.status + ")");
    setSaveStatus("saved");
  } catch (err) {
    setSaveStatus("error");
    toast("Save failed: " + err.message);
  }
}
function setSaveStatus(kind) {
  const s = $("#save-status");
  if (kind === "saving") { s.textContent = "Saving…"; s.classList.add("saving"); }
  else if (kind === "saved") { s.textContent = "Saved ✓"; s.classList.remove("saving"); }
  else { s.textContent = "Not saved"; s.classList.remove("saving"); }
}

// ---- help modal ----
function openHelp() { $("#help-modal").classList.add("open"); }
function closeHelp() { $("#help-modal").classList.remove("open"); }

// ---- wire up ----
window.addEventListener("DOMContentLoaded", () => {
  loadPacks();
  $("#intro-back").onclick = () => { showScreen("screen-packs"); };
  $("#ready-btn").onclick = startScoring;
  $("#next-btn").onclick = goNext;
  $("#prev-btn").onclick = goPrev;
  $("#save-btn").onclick = () => { saveNow(); toast("Saved"); };
  $("#help-btn").onclick = openHelp;
  $("#help-close").onclick = closeHelp;
  $("#help-modal").onclick = (e) => { if (e.target.id === "help-modal") closeHelp(); };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeHelp();
  });
});
