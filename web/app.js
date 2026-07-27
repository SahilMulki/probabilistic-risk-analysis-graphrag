/* The single render contract: one show(result) path paints the page, fed identically by the
 * static demo bundle or a live POST /ask. DOM-building never branches on the data source. */
"use strict";

const $ = (s) => document.querySelector(s);
const state = { mode: "demo", bundle: null, metrics: null, byId: {}, current: null, net: null };

const PROV = {
  light: { oracle: "#e5a000", pipeline: "#71767a", "coded-hub": "#009ec1" },
  dark:  { oracle: "#e5b53f", pipeline: "#9aa0a8", "coded-hub": "#34c3d6" },
};
const SERIES = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
  dark:  ["#58a6e6", "#f0904a", "#3bbd8a", "#e0b34a", "#e58fb0"],
};
const isDark = () => {
  const t = document.documentElement.getAttribute("data-theme");
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
// lift LER report numbers out of the prose so text reads less like a wall of numbers
const fmtInline = (t) => esc(t).replace(/\b(\d{2,3}-\d{4}-\d{3}-\d{2})\b/g,
  '<span class="ler-ref">$1</span>');

// The graph's cross-doc / aggregation answers are long "; "-separated enumerations. When an
// answer is clearly such a list (many semicolons), render it AS a list — intro sentence kept as
// a lead, any trailing "[note] …" caveat kept as a note. Normal prose stays a paragraph.
// Risk answers embed outcome distributions as prose runs like
// "safety-system-inoperable 67% (32/48, severity 4), reactor-trip-or-scram 21% (10/48, …)".
// Pull those runs out into a compact bar list; keep the surrounding prose as paragraphs.
const DIST_RE = /([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\s+(\d{1,3})%\s*\(([^)]*)\)/g;
const prose = (s) => {
  s = s.replace(/^[\s.,;:]+/, "").trim();
  return s ? `<p class="answer-text">${fmtInline(s)}</p>` : "";
};
function renderDistributions(text) {
  const ms = [...text.matchAll(DIST_RE)];
  if (ms.length < 3) return null;
  const groups = []; let cur = [ms[0]];
  for (let i = 1; i < ms.length; i++) {
    const gap = text.slice(ms[i - 1].index + ms[i - 1][0].length, ms[i].index);
    if (/^[\s,;]*(and)?[\s,;]*$/i.test(gap)) cur.push(ms[i]);
    else { groups.push(cur); cur = [ms[i]]; }
  }
  groups.push(cur);
  let out = "", pos = 0, used = false;
  for (const g of groups) {
    if (g.length < 3) continue;
    const s = g[0].index, e = g[g.length - 1].index + g[g.length - 1][0].length;
    out += prose(text.slice(pos, s));
    out += `<ul class="dist">` + g.map((m) => {
      const pct = Math.max(0, Math.min(100, parseInt(m[2], 10)));
      return `<li><span class="dist-name">${esc(m[1].replace(/-/g, " "))}</span>` +
        `<span class="dist-track"><span class="dist-bar" style="width:${pct}%"></span></span>` +
        `<span class="dist-pct">${pct}%</span>` +
        `<span class="dist-meta">${esc(m[3])}</span></li>`;
    }).join("") + `</ul>`;
    pos = e; used = true;
  }
  if (!used) return null;
  return out + prose(text.slice(pos));
}

function fmtAnswerHtml(text) {
  const t = (text || "").trim();
  if ((t.match(/;/g) || []).length < 5) {
    return renderDistributions(t) || `<p class="answer-text lead">${fmtInline(t)}</p>`;
  }
  let body = t, lead = "", note = "";
  const ni = body.search(/\[note\]/i);
  if (ni >= 0) { note = body.slice(ni); body = body.slice(0, ni).trim(); }
  const ci = body.indexOf(": ");
  if (ci > 0 && ci < 200) { lead = body.slice(0, ci + 1); body = body.slice(ci + 2); }
  const items = body.split(/;\s*/).map((s) => s.trim().replace(/\.$/, "")).filter(Boolean);
  return (lead ? `<p class="answer-text lead">${fmtInline(lead)}</p>` : "") +
    `<ul class="answer-list">${items.map((it) => `<li>${fmtInline(it)}</li>`).join("")}</ul>` +
    (note ? `<p class="answer-note">${fmtInline(note)}</p>` : "");
}

// The graph's risk intents. A risk answer is a corpus-wide statistic — it aggregates many
// reports and cites no single one — so its citations are legitimately empty (not a bug).
const RISK_INTENTS = new Set(["likely_outcome", "risk_ranking", "probable_path", "faceted_frequency"]);

// Parse the DETERMINISTIC risk evidence text — byte-identical in demo and live, since both come
// from the same retrieve.py serializer — into a structured outcome distribution. The bars then
// render from DATA, never from however the LLM happened to phrase the prose (the old, fragile
// path that made live risk answers fall back to plain text while the demo got bars).
const RISK_ROW = /^[ \t]+([a-z][a-z0-9-]+)\s+(\d{1,3})%\s+\((\d+)\s+events?\)\s+\[sev\s+(\d)\]/gm;
function parseRiskEvidence(text) {
  if (!text) return null;
  const label = /Observed outcome distribution for (.+?) —/.exec(text);
  const entHdr = /P\(outcome \| this \w+\) over (\d+) classified events:/.exec(text);
  if (!label || !entHdr) return null;               // not a likely_outcome distribution
  const baseHdr = /corpus baseline P\(outcome\) over all (\d+) classified events/.exec(text);
  const nEvents = /n_events \(reportable events involving it\): (\d+)/.exec(text);
  const expSev = /expected_severity: ([\d.]+)/.exec(text);
  const modal = /modal outcome: ([a-z0-9-]+) \((\d+)%\)/.exec(text);
  const smpl = /\[small-sample\][^\n]*/.exec(text);
  const rows = [];
  let m; RISK_ROW.lastIndex = 0;
  while ((m = RISK_ROW.exec(text)) !== null)
    rows.push({ name: m[1], pct: +m[2], n: +m[3], sev: +m[4], at: m.index });
  const baseAt = baseHdr ? baseHdr.index : Infinity;
  const entity = rows.filter((r) => r.at > entHdr.index && r.at < baseAt);
  const baseline = rows.filter((r) => r.at > baseAt);
  if (!entity.length) return null;
  return {
    label: label[1].trim(), n_events: nEvents ? +nEvents[1] : null, n_classified: +entHdr[1],
    expected_severity: expSev ? expSev[1] : null,
    modal: modal ? { name: modal[1], pct: +modal[2] } : null,
    small_sample: smpl ? smpl[0].replace(/^\[small-sample\]\s*/, "").trim() : null,
    entity, baseline,
  };
}

function distBars(rows) {
  return `<ul class="dist">` + rows.map((r) => {
    const pct = Math.max(0, Math.min(100, r.pct));
    return `<li><span class="dist-name">${esc(r.name.replace(/-/g, " "))}</span>` +
      `<span class="dist-track"><span class="dist-bar" style="width:${pct}%"></span></span>` +
      `<span class="dist-pct">${pct}%</span>` +
      `<span class="dist-meta">${r.n} event${r.n === 1 ? "" : "s"} · severity ${r.sev}</span></li>`;
  }).join("") + `</ul>`;
}

function riskPanelHtml(d) {
  const modal = d.modal
    ? `<p class="risk-modal">Most likely: <b>${esc(d.modal.name.replace(/-/g, " "))}</b> — observed in ${d.modal.pct}% of these events.</p>`
    : "";
  const stats = `<div class="risk-stats">` +
    (d.n_events != null ? `<span><b>${d.n_events}</b> reportable events</span>` : "") +
    (d.expected_severity ? `<span>expected severity <b>${esc(d.expected_severity)}</b> / 5</span>` : "") +
    (d.small_sample ? `<span>⚠ ${esc(d.small_sample)}</span>` : "") + `</div>`;
  const baseline = d.baseline && d.baseline.length
    ? `<details class="risk-baseline"><summary>Compare with the whole-corpus baseline</summary>${distBars(d.baseline)}</details>`
    : "";
  return `<div class="risk-panel"><div class="risk-head">` +
    `<span class="risk-title">Observed outcome distribution</span>` +
    `<span class="risk-sub">${esc(d.label)} · within this corpus</span></div>` +
    modal + distBars(d.entity) + stats + baseline + `</div>`;
}

// The structured panel already shows the distribution as bars, so drop the LLM's inline
// "name NN% (…)" enumeration from the prose to avoid printing the same numbers twice; the
// narrative and the honesty caveats around it are kept.
function stripDistProse(s) {
  s = s.replace(/([a-z][a-z0-9-]+\s+\d{1,3}%\s*\([^)]*\)\s*[,;]?\s*(and\s+)?)+/gi, "⟦dist⟧");
  s = s.replace(/\bis:?\s*⟦dist⟧/gi, "is shown above");
  s = s.replace(/⟦dist⟧/g, "");
  s = s.replace(/\s*:\s*\./g, ".").replace(/\s{2,}/g, " ")
       .replace(/\s+([.,;])/g, "$1").replace(/\.(\s*\.)+/g, ".");
  return s.trim();
}

// plain-language names for the pre-decided question categories (no jargon)
const CATEGORY = {
  "cross-doc": "Connect many related reports", "multi-hop": "Trace a chain within one report",
  "lookup-id": "Find one specific report by its ID", "lookup-content": "Find a report by what happened",
  "negative": "Refuse an out-of-corpus question", "risk": "Corpus-wide statistics & risk",
  "aggregation": "Corpus-wide aggregation", "clarify": "Handle an ambiguous question",
};
const WIN_TITLE = { GRAPH: "The knowledge graph wins this category",
  VECTOR: "Vector search wins this category", TIE: "Both systems handle this equally" };

// authored, per-example explanation of the score + the reasoning (these examples are fixed)
const NOTES = {
  "XDOC-HPCI-COMP": {
    how: "The score is the fraction of the reports that genuinely involve this system that each method retrieved. The graph follows the shared-system link and returns all 47; vector search, ranking by text similarity, surfaced about 11%.",
    why: "Gathering every report that shares a system or component is a corpus-wide join — one step for a graph, and something similarity search cannot do, because no single report is “most similar” to a set spread across ~15 plants." },
  "XDOC-BACKUPS": {
    how: "Same measure: of the 274 reports where a backup safety system was available, the graph returns all of them; vector search recovers about 2%, even allowed to pull back many reports.",
    why: "The larger the shared group, the wider the gap. Following one shared link scales effortlessly; ranking hundreds of similar reports by similarity does not." },
  "MH-Limerick": {
    how: "The score is whether the method retrieved the one specific report and reconstructed its cause-and-consequence chain. The graph did; vector search never retrieved the right report.",
    why: "The answer is a chain of linked events inside a single report. The graph walks that chain step by step; similarity search can’t reliably pick the one report out of hundreds of look-alike trip reports." },
  "LOOK-CAUSE-VOGTLE": {
    how: "The score is whether the exact report asked about was found. The graph resolves it by its report number; vector search scored 0 — embeddings ignore identifiers, and many reports read alike.",
    why: "Pinning one specific report by its ID is exact-match retrieval: a graph strength and a similarity-search blind spot." },
  "LOOKC-PRAIRIE": {
    how: "The score is whether the method named the right plant from a distinctive described event. Vector search found it from the description; the graph has no template for “the event where X happened,” so it declined.",
    why: "This is open-ended semantic search — vector’s genuine strength, and a capability the graph’s fixed query templates lack. A fair comparison has to include the cases the graph loses." },
  "NEG-CHERNOBYL": {
    how: "The score is whether the method correctly refused. Chernobyl is not a U.S. Licensee Event Report, so the right answer is “not in this corpus.” Both refused.",
    why: "Declining when the corpus doesn’t contain the subject is correct: the graph refuses structurally (nothing to anchor to), and the answer model recognizes that vector’s retrieved passages are irrelevant." },
  "LIKELY-OUTCOME": {
    detail: "The graph answers from counted outcomes across all reports — a real distribution with the number of events behind it, and the honest caveat that these are observed frequencies in this report set, not certified failure rates. Vector search, handed 8 similar passages, produced confident frequency language with no denominator — it manufactures the impression of a statistic. That is why corpus-wide statistical questions are shown as a capability, not scored head-to-head: only the graph can actually count." },
  "CLARIFY-PLANT": {
    detail: "The question names a plant but not which event. The graph sees that several reports match and asks you to narrow it down, instead of silently answering about one of them. Vector search just answers from whatever was most similar. Asking rather than guessing is the safe behavior when a question is ambiguous." },
};

// --------------------------------------------------------------------------- //
async function boot() {
  initTheme();
  wireTabs(); wireMode(); wireAsk(); wireTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    syncThemeLabel(); repaintThemed();
  });
  await loadDemoData();
  renderExamples();
  syncScorecard();
  if (state.bundle && state.bundle.examples.length) selectExample(state.bundle.examples[0].id, true);
  stampFooter();
  applyDeepLink();
}

// theme ---------------------------------------------------------------------
const currentTheme = () => document.documentElement.getAttribute("data-theme")
  || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
function initTheme() {
  const saved = localStorage.getItem("prag-theme");
  // Default is LIGHT regardless of OS preference; only a saved choice (or a ?theme= deep link)
  // overrides it. index.html also ships data-theme="light" so there is no first-paint flash.
  document.documentElement.setAttribute("data-theme",
    saved === "dark" || saved === "light" ? saved : "light");
  syncThemeLabel();
}
function syncThemeLabel() {
  const b = $("#theme-toggle");
  if (b) b.textContent = currentTheme() === "dark" ? "Light" : "Dark";
}
function repaintThemed() {   // the canvas + SVG chart are painted in JS, so re-render them
  if (!$("#scorecard-section").hidden) renderResults();
  if (state.current && state.current._sg && $("#tab-subgraph").classList.contains("on"))
    drawSubgraph(state.current._sg);
}

// The scorecard is a bottom-of-page section shown in DEMO mode only (removed from Live).
function syncScorecard() {
  const sec = $("#scorecard-section");
  if (!sec) return;
  const on = state.mode === "demo";
  sec.hidden = !on;
  if (on) renderResults();
}
function wireTheme() {
  $("#theme-toggle").onclick = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("prag-theme", next);
    syncThemeLabel(); repaintThemed();
  };
}

// transient notices disappear as soon as the user moves on
const hideNotice = () => { const n = $("#livehint"); n.hidden = true; n.innerHTML = ""; };
const showNotice = (html) => { const n = $("#livehint"); n.innerHTML = html; n.hidden = false; };

async function loadDemoData() {
  try {
    const [b, m] = await Promise.all([
      fetch("demo_bundle.json").then((r) => r.json()),
      fetch("metrics.json").then((r) => r.json()),
    ]);
    state.bundle = b; state.metrics = m;
    b.examples.forEach((ex) => { state.byId[ex.id] = ex; });
  } catch (e) {
    showNotice(`Demo data not found — run <code>python src/precompute.py</code> to build it.`);
  }
}

function stampFooter() {
  const b = state.bundle;
  if (b) $("#stamp").textContent =
    `Built ${b.generated_at} · vector baseline ${b.vector.model} (k=${b.vector.k}) · captured real system output`;
}

function applyDeepLink() {
  const p = new URLSearchParams(location.search);
  const theme = p.get("theme");
  if (theme === "dark" || theme === "light") {
    document.documentElement.setAttribute("data-theme", theme);
    syncThemeLabel(); repaintThemed();
  }
  const ex = p.get("ex");
  if (ex && state.byId[ex]) selectExample(ex, true);
  const tab = (location.hash || "").replace("#", "");
  if (["compare", "subgraph"].includes(tab)) {
    const btn = document.querySelector(`.tabs button[data-tab="${tab}"]`);
    if (btn) btn.click();
  }
}

// --------------------------------------------------------------------------- //
function renderExamples() {
  const sel = $("#example-select");
  if (!state.bundle || !sel) return;
  sel.innerHTML = `<option value="">Select an example question…</option>` +
    state.bundle.examples.map((ex) =>
      `<option value="${esc(ex.id)}">${esc(ex.question)}</option>`).join("");
  sel.onchange = () => { if (sel.value) selectExample(sel.value, true); };
}

function selectExample(id, fill) {
  const ex = state.byId[id];
  if (!ex) return;
  const sel = $("#example-select");
  if (sel) sel.value = id;
  if (fill) $("#q").value = ex.question;
  hideNotice();
  // In live mode we never replay precomputed output — load the question and let the user Ask it.
  if (state.mode === "live") {
    showNotice(`Loaded into the question box. Press <b>Ask</b> to run it live against the graph and
      the search index.`);
    return;
  }
  show(ex);
}

// Live mode starts from a clean slate: no example selected, empty question, empty panes.
function resetCompare() {
  state.current = null;
  $("#q").value = "";
  const sel = $("#example-select");
  if (sel) sel.value = "";
  const qt = $("#qtype"); if (qt) { qt.hidden = true; qt.innerHTML = ""; }
  $("#qcaption").textContent = "";
  $("#g-intent").textContent = ""; $("#v-intent").textContent = "";
  $("#g-body").innerHTML = `<p class="placeholder">Ask a question to see the knowledge graph's answer.</p>`;
  $("#v-body").innerHTML = `<p class="placeholder">…and the vector-search baseline's answer.</p>`;
  $("#verdict").innerHTML = "";
  $("#sg-meta").textContent = "Ask a question to see how its reports connect.";
  $("#sg-trunc").hidden = true; $("#sg-legend").innerHTML = "";
  if (state.net) { state.net.destroy(); state.net = null; }
  $("#network").innerHTML = "";
}

// --------------------------------------------------------------------------- //
function show(result) {
  state.current = result;
  const qt = $("#qtype");
  if (result.bucket) {
    qt.hidden = false;
    qt.innerHTML = `<span class="qtype-pill"><span class="qtype-label">Question type</span>` +
      `<span class="qtype-name">${esc(CATEGORY[result.bucket] || result.bucket)}</span></span>`;
  } else { qt.hidden = true; qt.innerHTML = ""; }
  $("#qcaption").textContent = result.question;
  renderPane($("#g-body"), $("#g-intent"), result.graph, "graph");
  renderPane($("#v-body"), $("#v-intent"), result.vector, "vector");
  renderVerdict(result);
  result._sg = result.subgraph_data || null;
  prepareSubgraph(result);
}

function citeList(items) {
  if (!items || !items.length) return `<span class="muted">none</span>`;
  return items.map((l) => `<span class="badge ${l.source || "pipeline"}">${esc(l.key)}` +
    `<span class="who">${esc(l.source === "oracle" ? "hand-checked" : "auto-extracted")}</span></span>`).join(" ");
}

function renderPane(body, intentEl, side, which) {
  intentEl.textContent = "";
  if (side.mode === "clarify") {
    const cands = side.candidates.map((c) =>
      `<div class="cand"><code>${esc(c.key)}</code> · ${esc(c.plant || "?")} · ${esc(c.event_date || "?")}` +
      (c.systems && c.systems.length ? ` · ${esc(c.systems.join(", "))}` : "") +
      (c.title ? `<br><span class="muted">${esc(c.title)}</span>` : "") + `</div>`).join("");
    body.innerHTML = `<p class="clarify-q">${esc(side.question)}</p>${cands}` +
      (side.overflow ? `<p class="muted">…and more not shown — narrow by year or report number.</p>` : "");
    return;
  }
  const refused = side.answerable === false;
  const risk = (!refused && side.evidence_text) ? parseRiskEvidence(side.evidence_text) : null;
  const answerHtml = refused
    ? `<p class="refused">Not answerable from this corpus</p>
       <p class="answer-text muted">${fmtInline(side.answer)}</p>`
    : risk
      ? riskPanelHtml(risk) + fmtAnswerHtml(stripDistProse(side.answer))
      : fmtAnswerHtml(side.answer);
  // A risk answer aggregates the whole corpus and cites no single report, so "none" reads as if
  // the answer were ungrounded — say what it IS based on instead.
  const isRisk = RISK_INTENTS.has(side.intent);
  let cites = "";
  if (!refused) {
    if (side.citations && side.citations.length) {
      cites = `<div class="meta-row">Reports cited</div><div>${citeList(side.citations)}</div>`;
    } else if (isRisk) {
      const n = risk && risk.n_events ? `<b>${risk.n_events}</b> ` : "";
      cites = `<div class="meta-row">Based on</div><div class="corpus-note">A corpus-wide statistic ` +
        `over ${n}reportable events — no single report is cited.</div>`;
    } else {
      cites = `<div class="meta-row">Reports cited</div><div>${citeList(side.citations)}</div>`;
    }
  }
  const retrieved = side.lers && side.lers.length
    ? `<div class="meta-row">Retrieved ${side.lers.length} report${side.lers.length > 1 ? "s" : ""}</div>` +
      `<div>${citeList(side.lers.slice(0, 12))}` +
      (side.lers.length > 12 ? ` <span class="muted">+${side.lers.length - 12} more</span>` : "") + `</div>`
    : "";
  const ev = side.evidence_text
    ? `<details class="evidence"><summary>Show the ${which === "graph" ? "structured facts" : "retrieved passages"} behind this answer</summary>` +
      `<pre>${esc(side.evidence_text)}</pre></details>` : "";
  body.innerHTML = answerHtml + cites + retrieved + ev;
}

function renderVerdict(result) {
  const box = $("#verdict"), v = result.verdict, notes = NOTES[result.id] || {};
  if (!v) { box.innerHTML = ""; return; }
  if (v.kind === "head-to-head") {
    const s = (x) => (x == null ? "—" : Number(x).toFixed(2));
    box.innerHTML = `<div class="verdict-card ${v.winner}"><div class="verdict-head">
        <span class="who">${v.winner === "TIE" ? "TIE" : v.winner === "GRAPH" ? "GRAPH" : "VECTOR"}</span>
        <span class="verdict-title">${WIN_TITLE[v.winner]}</span>
        <span class="verdict-score">score — graph ${s(v.graph_score)} · vector ${s(v.vector_score)}</span>
      </div><div class="verdict-body">
        <div class="row"><div class="k">Category</div><div>${esc(CATEGORY[v.bucket] || v.bucket)} — the winner for this category was decided in advance, before either system ran.</div></div>
        ${notes.how ? `<div class="row"><div class="k">The score</div><div>${esc(notes.how)}</div></div>` : ""}
        ${notes.why ? `<div class="row"><div class="k">Why</div><div>${esc(notes.why)}</div></div>` : ""}
      </div></div>`;
  } else if (v.kind === "graph-only") {
    box.innerHTML = `<div class="verdict-card GRAPHONLY"><div class="verdict-head">
        <span class="who">GRAPH ONLY</span>
        <span class="verdict-title">Only the knowledge graph can do this</span>
      </div><div class="verdict-body">
        <div class="row"><div class="k">Why</div><div>${esc(notes.detail || v.detail)}</div></div>
      </div></div>`;
  } else {
    box.innerHTML = `<div class="verdict-card UNSCORED"><div class="verdict-head">
        <span class="who">UNSCORED</span>
        <span class="verdict-title">A live question — shown without a verdict</span>
      </div><div class="verdict-body"><div class="row"><div class="k">Note</div>
        <div>Verdicts come from the pre-decided evaluation set, so a question you typed is shown without a score. The two answers above are still directly comparable.</div></div>
      </div></div>`;
  }
}

// --------------------------------------------------------------------------- //
function prepareSubgraph(result) {
  const meta = $("#sg-meta");
  if (result._sg) {
    if ($("#tab-subgraph").classList.contains("on")) drawSubgraph(result._sg);
    else meta.textContent = "Open this tab to see how this question’s reports connect.";
    return;
  }
  if (result.subgraph && state.mode === "live") {
    meta.textContent = "Loading the connections…";
    fetch(`/subgraph?${new URLSearchParams(result.subgraph)}`).then((r) => r.json()).then((d) => {
      result._sg = d;
      if ($("#tab-subgraph").classList.contains("on")) drawSubgraph(d);
      else meta.textContent = "Open this tab to see how this question’s reports connect.";
    }).catch(() => { meta.textContent = "No connection view for this question."; });
    return;
  }
  meta.textContent = "This question has no connection view (the graph declined, or there is no shared anchor to draw).";
  $("#network").innerHTML = ""; $("#sg-trunc").hidden = true; $("#sg-legend").innerHTML = "";
}

function drawSubgraph(data) {
  const meta = $("#sg-meta"), trunc = $("#sg-trunc"), legend = $("#sg-legend");
  if (!data) { meta.textContent = "No connection view for this question."; $("#network").innerHTML = "";
    trunc.hidden = true; legend.innerHTML = ""; return; }
  const prov = PROV[isDark() ? "dark" : "light"];
  meta.innerHTML = data.kind === "hub"
    ? `<b>How the graph connects reports.</b> Each dot is a report; the center is a system or component they all share — every report touching that shared thing, assembled in one view. This is the link a knowledge graph makes automatically and plain search cannot.`
    : `<b>One report, start to finish.</b> What caused the problem, how it progressed step by step, and which backup safety systems were available. Every connection here belongs to this single report.`;
  if (data.truncation) {
    trunc.hidden = false;
    trunc.innerHTML = `Showing the <b>${data.truncation.shown}</b> most safety-significant of
      <b>${data.truncation.total}</b> connected reports — <b>${data.truncation.hidden} more</b> are not
      drawn. Ranked by how severe the worst outcome was, then most recent.`;
  } else trunc.hidden = true;

  const nodes = data.nodes.map((n) => {
    const shape = (n.group === "hub" || n.group === "backup" || n.group === "concept") ? "diamond"
      : n.group === "cause" ? "triangle" : n.group === "consequence" ? "square"
      : n.group === "failuremode" ? "hexagon" : "dot";
    const col = prov[n.provenance] || prov.pipeline;
    return { id: n.id, label: n.label, title: n.title, shape, size: 11 + (n.severity || 0) * 3.4,
      color: { background: col, border: col, highlight: { background: col, border: isDark() ? "#fff" : "#111" } },
      font: { color: isDark() ? "#e9ecf0" : "#1b1b1b", size: 12 } };
  });
  const edges = data.edges.map((e) => ({ from: e.from, to: e.to, label: e.label, arrows: "to",
    color: { color: isDark() ? "#3a4048" : "#c7ccd2" },
    font: { color: "#8b9098", size: 9, strokeWidth: 0, align: "middle" } }));

  if (state.net) state.net.destroy();
  state.net = new vis.Network($("#network"), { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) }, {
    physics: { stabilization: { iterations: 220 }, barnesHut: { gravitationalConstant: -6200, springLength: 130 } },
    interaction: { hover: true, tooltipDelay: 120 },
    nodes: { borderWidth: 1.5, scaling: { min: 11, max: 34 } },
    edges: { smooth: { type: "dynamic" }, width: 1 },
  });
  legend.innerHTML =
    `<span class="k oracle">gold — a report hand-checked as a reference</span>
     <span class="k pipeline">gray — extracted automatically by the model</span>
     <span class="k hub">teal — the shared system or component</span>
     <span class="muted">bigger dot = more severe worst outcome · hover for detail</span>`;
}

// --------------------------------------------------------------------------- //
function renderResults() {
  const m = state.metrics, root = $("#results-body");
  if (!m) { root.innerHTML = `<p class="placeholder">Results not found — run
    <code>python src/precompute.py --metrics</code></p>`; return; }
  const h2h = m.head_to_head.map((r) => {
    const gw = Math.round(r.graph * 90), vw = Math.round(r.vector * 90);
    return `<tr><td class="cat">${esc(CATEGORY[r.bucket] || r.bucket)}</td><td>${r.n}</td>
      <td class="barcell"><span class="bar g" style="width:${gw}px"></span>${r.graph.toFixed(2)}</td>
      <td class="barcell"><span class="bar v" style="width:${vw}px"></span>${r.vector.toFixed(2)}</td>
      <td class="win-${r.verdict}">${r.verdict === "GRAPH" ? "Graph" : r.verdict === "VECTOR" ? "Vector" : "Tie"}</td></tr>`;
  }).join("");
  root.innerHTML = `
    <div class="results-intro">
      <h3>How the two systems compare, measured</h3>
      <p>Both systems answered the same 42 evaluation questions, each sorted in advance into a
        category. For every category the table shows the average <b>score</b> — the fraction of the
        correct reports each system found (1.00 = all of them, 0.00 = none) — and which one wins.</p>
      <p><b>${esc(m.graph_suite)}.</b> ${esc(m.caption)}</p>
    </div>
    <table class="h2h">
      <thead><tr><th>Category</th><th>Questions</th><th>Graph</th><th>Vector</th><th>Winner</th></tr></thead>
      <tbody>${h2h}</tbody>
    </table>
    <div class="section-h">The hardest category: connecting every related report</div>
    <p class="section-sub">These questions ask for <em>every</em> report that shares a system or
      component. The knowledge graph returns all of them by definition (the dashed line at 1.00). The
      chart shows how much vector search recovers as it is allowed to pull back more and more reports
      — it never gets close, no matter how deep it searches.</p>
    <div class="chart-wrap">${recallChart(m.recall_at_k)}</div>`;
}

function recallChart(rk) {
  const dark = isDark(), pal = SERIES[dark ? "dark" : "light"];
  const ink = dark ? "#a4abb4" : "#565c65", grid = dark ? "#31353c" : "#dfe1e2";
  const blue = dark ? "#58a6e6" : "#005ea2";
  const W = 780, H = 380, L = 46, R = 170, T = 18, B = 42;
  const ks = rk.ks, rows = rk.rows;
  const xs = (i) => L + (i / (ks.length - 1)) * (W - L - R);
  const ys = (v) => T + (1 - v) * (H - T - B);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px" role="img"
    aria-label="How many related reports vector search recovers as it searches deeper">`;
  [0, 0.25, 0.5, 0.75, 1].forEach((g) => {
    svg += `<line x1="${L}" y1="${ys(g)}" x2="${W - R}" y2="${ys(g)}" stroke="${grid}" stroke-width="1"/>`;
    svg += `<text x="${L - 8}" y="${ys(g) + 3}" text-anchor="end" font-size="10" fill="${ink}">${g.toFixed(2)}</text>`;
  });
  ks.forEach((k, i) => { svg += `<text x="${xs(i)}" y="${H - B + 16}" text-anchor="middle" font-size="10" fill="${ink}">${k}</text>`; });
  svg += `<text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle" font-size="10.5" fill="${ink}">reports pulled back (deeper search)</text>`;
  svg += `<line x1="${L}" y1="${ys(1)}" x2="${W - R}" y2="${ys(1)}" stroke="${blue}" stroke-width="2" stroke-dasharray="6 4"/>`;
  svg += `<text x="${W - R + 8}" y="${ys(1) + 3}" font-size="10.5" font-weight="700" fill="${blue}">knowledge graph = 1.00</text>`;
  rows.forEach((row, si) => {
    const col = pal[si % pal.length];
    const pts = ks.map((k, i) => [xs(i), ys(row.recall[String(k)])]);
    svg += `<path d="${pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ")}" fill="none" stroke="${col}" stroke-width="2"/>`;
    pts.forEach((p, i) => { svg += `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3" fill="${col}"><title>${esc(shortId(row.id))} · ${ks[i]} reports · recovered ${(row.recall[String(ks[i])] * 100).toFixed(0)}%</title></circle>`; });
    const y = (ys(1) + 18 + si * 15).toFixed(1);
    svg += `<rect x="${W - R + 8}" y="${(y - 8).toFixed(1)}" width="12" height="4" rx="1" fill="${col}"/>`;
    svg += `<text x="${W - R + 24}" y="${y}" font-size="10" fill="${ink}">${esc(shortId(row.id))} (${row.full})</text>`;
  });
  return svg + `</svg>`;
}
const shortId = (id) => ({
  "XDOC-HPCI-COMP": "HPCI components", "XDOC-RCIC-COMP": "RCIC components",
  "AGG-WEAK-PROG": "weak-program events", "XPLANT-SHARED": "shared across plants", "XDOC-BACKUPS": "backup available",
}[id] || id);

// --------------------------------------------------------------------------- //
function wireTabs() {
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
      ["compare", "subgraph"].forEach((t) => $("#tab-" + t).classList.toggle("on", t === b.dataset.tab));
      if (b.dataset.tab === "subgraph" && state.current) drawSubgraph(state.current._sg);
    };
  });
}

function wireMode() {
  const set = (mode) => {
    state.mode = mode;
    $("#mode-demo").classList.toggle("on", mode === "demo");
    $("#mode-live").classList.toggle("on", mode === "live");
    $("#modenote").textContent = mode === "demo"
      ? "precomputed answers — no database or API needed"
      : "runs both systems live — needs the backend, costs API credits";
    hideNotice();
    syncScorecard();                       // scorecard shows in demo, hidden in live
    if (mode === "live") { resetCompare(); checkLive(); }
    else if (state.bundle && state.bundle.examples.length) selectExample(state.bundle.examples[0].id, true);
  };
  $("#mode-demo").onclick = () => set("demo");
  $("#mode-live").onclick = () => set("live");
}

async function checkLive() {
  try {
    const h = await fetch("/health").then((r) => r.json());
    showNotice(h.ready
      ? `Live mode is ready. Type any question and both systems will answer it fresh. A live question has no pre-decided category, so it is shown <b>without a verdict</b> — the two answers stay directly comparable.`
      : (h.error ? `Backend error: ${esc(h.error)}` : `Warming up the search index… try again in a moment.`));
  } catch (e) {
    showNotice(`Live backend not reachable. Start it with
      <code>uvicorn app:app --app-dir src</code> (needs the database + an API key). Demo mode needs no backend.`);
  }
}

function wireAsk() {
  $("#askform").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (!q) return;
    if (state.mode === "demo") {
      const hit = state.bundle && state.bundle.examples.find((x) => x.question.trim() === q);
      if (hit) return selectExample(hit.id, true);
      showNotice(`Demo mode only replays the precomputed example questions. Switch to
        <b>Live</b> to ask your own question (needs the backend running).`);
      return;
    }
    const btn = $("#askbtn"); btn.disabled = true; btn.textContent = "…";
    hideNotice();
    const sel = $("#example-select"); if (sel) sel.value = "";
    $("#qcaption").textContent = q;
    $("#g-body").innerHTML = loadingHtml("Following connections in the knowledge graph…");
    $("#v-body").innerHTML = loadingHtml("Searching by text similarity…");
    $("#verdict").innerHTML = "";
    try {
      const res = await fetch("/ask", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      show(await res.json());
    } catch (err) {
      $("#g-body").innerHTML = ""; $("#v-body").innerHTML = "";
      showNotice(`Ask failed: ${esc(err.message)}`);
    } finally { btn.disabled = false; btn.textContent = "Ask"; }
  };
}
const loadingHtml = (label) => `<div class="loading"><div class="spinner"></div><span>${esc(label)}</span></div>`;

boot();
