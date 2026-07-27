"""
app.py — Phase 10a FastAPI backend for the graph-vs-vector web UI.

A THIN layer over the existing retrieval seam. The endpoints reuse, unchanged:
`GraphRetriever` + `VectorRetriever` (the Phase-6/9 seam) → `answer.answer()`.
The only genuinely new backend logic is `/subgraph` (structured nodes/edges for the
viz) and the shared `result` shape (§ the single render contract).

Endpoints
    GET  /health           readiness (live mode warms a 1.3 GB embedder + Neo4j)
    POST /ask              {question} -> a `result` (graph side + vector side); LIVE, costs API $
    GET  /subgraph         hub-membership OR single-event chain -> {nodes, edges, truncation}
    GET  /examples         the precomputed demo bundle (no LLM, no Neo4j) — demo mode
    GET  /metrics          Phase-9 bucket table + recall@k (static JSON) — Results tab
    GET  /  (+ static)     the single page, app.js, style.css, vendored vis-network

The `build_side_*`, `build_subgraph`, and `verdict_for` helpers are pure and importable:
`precompute.py` calls the SAME code path so demo output is byte-for-byte the live shape.
Free-form (live) questions carry NO pre-registered bucket, so `verdict_for` returns the
`unscored` chip — never a live-computed win/loss (that would reintroduce the bias Phase 9
removed).
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import risk
from answer import answer as answer_fn
from llm import LLM
from load_graph import load_records
from retrieve import Clarification, Evidence, GraphRetriever
from vector_baseline import VectorRetriever

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
BUNDLE = WEB_DIR / "demo_bundle.json"
METRICS = WEB_DIR / "metrics.json"

# vector baseline primary config, identical to the Phase-9 head-to-head
VEC_MODEL, VEC_CONFIG, VEC_K = "bge-large", "medium", 8

# How many hub spokes we DRAW before summarising the rest as a "+N more" count. The shown
# spokes are the most safety-significant (Phase-7 max outcome severity, recency as tiebreak),
# never an arbitrary slice — see build_subgraph.
SUBGRAPH_TOP_N = 20

# Head-to-head buckets carry a PRE-REGISTERED Phase-9 verdict (frozen before any retriever ran,
# see docs/journal/phase_9.md). The chip shows THIS, never a live recomputation. Per-example scores are shown
# only as secondary detail, and the showcase examples are chosen so the two never disagree.
BUCKET_VERDICT = {
    "lookup-id": "GRAPH", "lookup-content": "VECTOR", "multi-hop": "GRAPH",
    "cross-doc": "GRAPH", "negative": "TIE",
}
GRAPH_ONLY_DETAIL = {
    "risk": "vector fabricates frequencies with no denominator — not a scored head-to-head",
    "aggregation": "vector cannot assemble the corpus-wide set — not a scored head-to-head",
    "clarify": "graph asks to disambiguate rather than guess — not a scored head-to-head",
}


# --------------------------------------------------------------------------- #
# provenance: LER key -> "oracle" | "pipeline" (hand-marked vs extracted)
# --------------------------------------------------------------------------- #
def provenance_map() -> dict:
    return {rec.ler_number: src for rec, src in load_records()}


# --------------------------------------------------------------------------- #
# the single `result` shape — built identically for live and precompute
# --------------------------------------------------------------------------- #
def _cand(c: dict) -> dict:
    return {"key": c["key"], "plant": c.get("plant"), "event_date": c.get("event_date"),
            "systems": [x for x in (c.get("systems") or []) if x], "source": c.get("source"),
            "title": (c.get("title") or "").strip() or None}


def _lers_with_prov(lers: list[dict], prov: dict) -> list[dict]:
    # Prefer the report's real oracle|pipeline provenance over the retrieval-source tag
    # (VectorRetriever stamps source="vector"); both retrievers surface real LER keys.
    # De-dupe by key, preserving first-seen order: some templates emit one row per
    # (LER × consequence), so the same report can appear several times.
    seen, out = set(), []
    for l in lers:
        if l["key"] in seen:
            continue
        seen.add(l["key"])
        out.append({"key": l["key"], "source": prov.get(l["key"], l.get("source"))})
    return out


def build_side(outcome, ans: dict | None, prov: dict) -> dict:
    """One retriever's contribution to the split-pane, as a tagged union on `mode`.
    `answer`  — an Evidence outcome + its grounded answer (answerable=false => refusal).
    `clarify` — the graph's third outcome: several events matched, so it asks instead of guessing.
    """
    if isinstance(outcome, Clarification):
        return {"mode": "clarify", "intent": outcome.intent,
                "question": outcome.question,
                "candidates": [_cand(c) for c in outcome.candidates],
                "total": outcome.total, "overflow": outcome.overflow,
                "lers": [], "citations": [], "evidence_text": None}
    ev: Evidence = outcome
    ans = ans or {}
    return {"mode": "answer", "intent": ev.intent,
            "answerable": bool(ans.get("answerable")),
            "answer": ans.get("answer") or "",
            "citations": _lers_with_prov([{"key": c} for c in (ans.get("citations") or [])], prov),
            "lers": _lers_with_prov(ev.lers, prov),
            "node_keys": list(ev.node_keys),
            "evidence_text": ev.text, "empty": bool(ev.empty)}


def verdict_for(bucket: str | None, graph_score=None, vector_score=None) -> dict:
    """The verdict chip. Head-to-head buckets show their FROZEN Phase-9 verdict (+ this
    example's scores as detail). Graph-capability buckets get the third, non-head-to-head
    state. A free-form question (bucket None) is explicitly UNSCORED."""
    if bucket in BUCKET_VERDICT:
        return {"kind": "head-to-head", "bucket": bucket,
                "winner": BUCKET_VERDICT[bucket],
                "graph_score": graph_score, "vector_score": vector_score}
    if bucket in GRAPH_ONLY_DETAIL:
        return {"kind": "graph-only", "bucket": bucket,
                "winner": "GRAPH-ONLY", "detail": GRAPH_ONLY_DETAIL[bucket]}
    return {"kind": "unscored",
            "detail": "unscored — verdicts come from the evaluated set, not live questions"}


def suggest_subgraph(graph_side: dict, graph_outcome) -> dict | None:
    """Best-effort subgraph anchor for the Subgraph tab, derived from the graph outcome so a
    live question can populate the viz when it has a natural anchor."""
    if isinstance(graph_outcome, Clarification):
        return None
    anchors = getattr(graph_outcome, "anchors", {}) or {}
    intent = graph_side.get("intent")
    if intent == "mitigating_backups":
        return {"kind": "hub", "intent": "mitigating_backups"}
    # A resolved single report → its within-report chain (checked BEFORE the system hub, so a
    # multi-hop question that also names a system still shows the event chain, not the hub).
    lers = graph_side.get("lers") or []
    if intent == "failure_chain" and len(lers) == 1:
        return {"kind": "event", "ler": lers[0]["key"]}
    # A likely_outcome/probable_path question that named a system carries its EIIS code here, so the
    # Connections tab shows that system's hub — the reports behind the statistic.
    code = (anchors.get("system_code") or "").strip()
    if code and code.upper() != "ADS":
        return {"kind": "hub", "system": code}
    # A risk_ranking question has no single system anchor — surface the #1-ranked system's hub
    # (its match_key leads the Evidence.node_keys) so the tab still shows the reports behind the top
    # of the ranking rather than nothing.
    if intent == "risk_ranking":
        for mk in getattr(graph_outcome, "node_keys", []) or []:
            if isinstance(mk, str) and mk.startswith("System:"):
                sc = mk.split(":", 1)[1].strip()
                if sc and sc.upper() != "ADS":
                    return {"kind": "hub", "system": sc}
    return None


# --------------------------------------------------------------------------- #
# /subgraph — structured nodes/edges for the viz (the one new backend query)
# --------------------------------------------------------------------------- #
def _sev(ocs: list[str]) -> int:
    vals = [risk.SEVERITY[o] for o in ocs if o in risk.SEVERITY]
    return max(vals) if vals else 0


def _hub_spokes(session, *, system: str | None, intent: str | None):
    """Member LERs of a coded hub, each with its worst Phase-7 outcome severity + event date
    (the truncation ranking keys). Two hub kinds: a System hub (by EIIS code) or the
    'mitigating_backups' pseudo-hub (events with a redundant safety system available)."""
    if system:
        hub_id, hub_label, hub_kind = f"System:{system}", None, "system"
        q = ("MATCH (l:LER)-[:INVOLVES]->(s:System {eiis_code:$code}) WHERE NOT l.stub "
             "WITH s, l "
             "OPTIONAL MATCH (l)-[:HAS_CAUSE {ler_number:l.key}]->(:Cause)"
             "<-[:CAUSED_BY {ler_number:l.key}]-()-[:LEADS_TO*0.. {ler_number:l.key}]->"
             "(c:Consequence) "
             "RETURN s.display_name AS hub_label, l.key AS ler, l.plant_name AS plant, "
             "  l.event_date AS event_date, l.source AS source, "
             "  collect(DISTINCT c.outcome_class) AS ocs")
        rows = list(session.run(q, code=system))
        if rows and rows[0]["hub_label"]:
            hub_label = f"{rows[0]['hub_label']} ({system})"
    else:  # mitigating_backups pseudo-hub
        hub_id, hub_label, hub_kind = "concept:mitigating_backups", \
            "redundant safety system available", "concept"
        q = ("MATCH (l:LER)-[:HAS_CAUSE {ler_number:l.key}]->(:Cause)"
             "<-[:CAUSED_BY {ler_number:l.key}]-()-[:LEADS_TO*0.. {ler_number:l.key}]->"
             "(c:Consequence)-[:BACKED_UP_BY {ler_number:l.key}]->(:System) WHERE NOT l.stub "
             "RETURN l.key AS ler, l.plant_name AS plant, l.event_date AS event_date, "
             "  l.source AS source, collect(DISTINCT c.outcome_class) AS ocs")
        rows = list(session.run(q))
    spokes = {}
    for r in rows:  # dedupe LERs; keep the union of outcome classes
        k = r["ler"]
        s = spokes.setdefault(k, {"ler": k, "plant": r["plant"], "event_date": r["event_date"],
                                  "source": r["source"], "ocs": set()})
        s["ocs"].update(o for o in (r["ocs"] or []) if o)
    return hub_id, hub_label, hub_kind, list(spokes.values())


def build_hub_subgraph(session, *, system=None, intent=None, top_n=SUBGRAPH_TOP_N) -> dict:
    hub_id, hub_label, hub_kind, spokes = _hub_spokes(session, system=system, intent=intent)
    for s in spokes:
        s["severity"] = _sev(s["ocs"])
    # criterion-based, VISIBLE truncation: rank by worst outcome severity, recency as tiebreak.
    spokes.sort(key=lambda s: (s["severity"], s["event_date"] or ""), reverse=True)
    total = len(spokes)
    shown = spokes[:top_n]
    nodes = [{"id": hub_id, "label": hub_label or hub_id, "group": "hub",
              "provenance": "coded-hub",
              "title": f"coded hub — {total} member reports in this corpus"}]
    edges = []
    for s in shown:
        nid = f"LER:{s['ler']}"
        sev = s["severity"]
        oc = max(s["ocs"], key=lambda o: risk.SEVERITY.get(o, 0)) if s["ocs"] else None
        nodes.append({
            "id": nid, "group": "ler", "provenance": s["source"] or "pipeline",
            "label": f"{s['ler']}\n{s['plant'] or ''}".strip(),
            "severity": sev,
            "title": (f"LER {s['ler']} — {s['plant'] or '?'} — {s['event_date'] or '?'}"
                      f"\nprovenance: {s['source'] or 'pipeline'}"
                      f"\nworst outcome: {oc or 'n/a'} (severity {sev or 'n/a'})")})
        edges.append({"from": hub_id, "to": nid,
                      "label": "INVOLVES" if hub_kind == "system" else "BACKED_UP_BY"})
    truncation = None
    if total > len(shown):
        truncation = {"total": total, "shown": len(shown), "hidden": total - len(shown),
                      "criterion": "most safety-significant first (worst observed outcome), "
                                   "then most recent"}
    return {"kind": "hub", "hub_kind": hub_kind, "nodes": nodes, "edges": edges,
            "truncation": truncation,
            "legend": "provenance: oracle = hand-marked exemplar · pipeline = LLM-extracted · "
                      "coded-hub = shared EIIS node. node size ∝ worst outcome severity."}


def build_event_subgraph(session, *, ler: str) -> dict:
    """A single report's within-document chain: LER → Cause ← FailureMode → … → Consequence
    (→ backups). The 'within-report multi-hop' the graph does exactly and vector cannot pin."""
    rows = list(session.run(
        "MATCH (l:LER {key:$ler})-[:HAS_CAUSE {ler_number:$ler}]->(cause:Cause) "
        "MATCH (cause)<-[:CAUSED_BY {ler_number:$ler}]-(origin:FailureMode) "
        "MATCH path=(origin)-[:LEADS_TO*0.. {ler_number:$ler}]->(cons:Consequence) "
        "OPTIONAL MATCH (cons)-[:BACKED_UP_BY {ler_number:$ler}]->(bk:System) "
        "RETURN l{.key, .plant_name, .event_date, .source} AS ler, "
        "  cause{.display_name, .category, .match_key} AS cause, "
        "  [n IN nodes(path) | n{.display_name, .match_key, oc: n.outcome_class, "
        "     kind: [lbl IN labels(n) WHERE lbl <> 'Entity'][0]}] AS chain, "
        "  [x IN collect(DISTINCT bk) WHERE x IS NOT NULL | "
        "     x{.display_name, .eiis_code, .match_key}] AS backups", ler=ler))
    if not rows:
        raise HTTPException(404, f"no within-report chain for LER {ler}")
    meta = rows[0]["ler"]
    src = meta.get("source") or "pipeline"
    nodes: dict[str, dict] = {}
    edges = []

    def add(nid, label, group, oc=None, title=None):
        nodes.setdefault(nid, {"id": nid, "label": label, "group": group, "provenance": src,
                               "severity": risk.SEVERITY.get(oc, 0) if oc else 0,
                               "title": title or label})

    ler_id = f"LER:{meta['key']}"
    add(ler_id, f"{meta['key']}\n{meta.get('plant_name') or ''}".strip(), "ler",
        title=f"LER {meta['key']} — {meta.get('plant_name') or '?'} — provenance: {src}")
    for r in rows:
        cause = r["cause"]
        cause_id = cause.get("match_key") or f"Cause:{cause.get('category')}"
        add(cause_id, cause.get("display_name") or cause.get("category") or "cause", "cause")
        edges.append({"from": ler_id, "to": cause_id, "label": "HAS_CAUSE"})
        chain = r["chain"] or []
        for i, n in enumerate(chain):
            nid = n.get("match_key") or f"{n.get('kind')}:{i}:{n.get('display_name')}"
            grp = (n.get("kind") or "").lower() or "node"
            oc = n.get("oc")
            add(nid, n.get("display_name") or grp, grp, oc=oc,
                title=(f"{n.get('display_name')}"
                       + (f"\noutcome: {oc} (severity {risk.SEVERITY.get(oc,'?')})" if oc else "")))
            if i == 0:
                edges.append({"from": nid, "to": cause_id, "label": "CAUSED_BY"})
            else:
                prev = chain[i - 1]
                pid = prev.get("match_key") or f"{prev.get('kind')}:{i-1}:{prev.get('display_name')}"
                edges.append({"from": pid, "to": nid, "label": "LEADS_TO"})
        # backups hang off the last chain node (the consequence)
        if chain:
            last = chain[-1]
            lid = last.get("match_key") or f"{last.get('kind')}:{len(chain)-1}:{last.get('display_name')}"
            for b in r["backups"]:
                bid = b.get("match_key") or f"System:{b.get('eiis_code')}"
                add(bid, f"{b.get('display_name') or b.get('eiis_code')}", "backup")
                nodes[bid]["provenance"] = "coded-hub"
                edges.append({"from": lid, "to": bid, "label": "BACKED_UP_BY"})
    # de-dupe edges
    seen, uniq = set(), []
    for e in edges:
        key = (e["from"], e["to"], e["label"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return {"kind": "event", "ler": meta["key"], "nodes": list(nodes.values()),
            "edges": uniq, "truncation": None,
            "legend": "within-report chain — every edge pinned to this LER number "
                      "(no cross-hub fan-out). node size ∝ outcome severity."}


def build_subgraph(session, spec: dict) -> dict:
    kind = spec.get("kind")
    if kind == "event":
        return build_event_subgraph(session, ler=spec["ler"])
    if kind == "hub":
        return build_hub_subgraph(session, system=spec.get("system"),
                                  intent=spec.get("intent"))
    raise HTTPException(400, f"unknown subgraph kind: {kind!r}")


# --------------------------------------------------------------------------- #
# live retrievers — lazily warmed (a 1.3 GB embedder + Neo4j) in the background
# --------------------------------------------------------------------------- #
class _Live:
    def __init__(self):
        self.lock = threading.Lock()
        self.ready = False
        self.error: str | None = None
        self.llm = self.graph = self.vector = self.prov = None

    def warm(self):
        with self.lock:
            if self.ready or self.error:
                return
            try:
                self.llm = LLM()
                self.graph = GraphRetriever(llm=self.llm)
                self.vector = VectorRetriever(model=VEC_MODEL, config=VEC_CONFIG, k=VEC_K)
                self.prov = provenance_map()
                self.ready = True
            except Exception as e:  # noqa: BLE001 — surfaced via /health for the UI
                self.error = f"{type(e).__name__}: {e}"


LIVE = _Live()


class AskBody(BaseModel):
    question: str


app = FastAPI(title="Dynamic Risk RAG — graph vs vector")


@app.on_event("startup")
def _startup():
    threading.Thread(target=LIVE.warm, daemon=True).start()


@app.get("/health")
def health():
    return {"ready": LIVE.ready, "error": LIVE.error,
            "vector": {"model": VEC_MODEL, "config": VEC_CONFIG, "k": VEC_K},
            "demo_bundle": BUNDLE.exists(), "metrics": METRICS.exists()}


@app.post("/ask")
def ask(body: AskBody):
    """LIVE mode: run both retrievers + the shared answerer on a free-form question.
    Costs API dollars and needs Neo4j + the embedder warm. The verdict chip is WITHHELD
    (unscored) — a free-form question has no pre-registered bucket."""
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    LIVE.warm()
    if not LIVE.ready:
        raise HTTPException(503, LIVE.error or "live retrievers are still warming up")

    g_out = LIVE.graph.retrieve(q)
    g_ans = None if isinstance(g_out, Clarification) else answer_fn(q, g_out, llm=LIVE.llm)
    v_out = LIVE.vector.retrieve(q)
    v_ans = answer_fn(q, v_out, llm=LIVE.llm)

    g_side = build_side(g_out, g_ans, LIVE.prov)
    v_side = build_side(v_out, v_ans, LIVE.prov)
    return {"id": None, "question": q, "bucket": None, "mode": "live",
            "graph": g_side, "vector": v_side,
            "verdict": verdict_for(None),
            "subgraph": suggest_subgraph(g_side, g_out)}


@app.get("/subgraph")
def subgraph(kind: str, system: str | None = None, intent: str | None = None,
             ler: str | None = None):
    LIVE.warm()
    if not LIVE.ready:
        raise HTTPException(503, LIVE.error or "graph is still warming up")
    spec = {"kind": kind, "system": system, "intent": intent, "ler": ler}
    with LIVE.graph.driver.session() as s:
        return build_subgraph(s, spec)


@app.get("/examples")
def examples():
    if not BUNDLE.exists():
        raise HTTPException(404, "demo bundle not built — run: python src/precompute.py")
    return FileResponse(BUNDLE, media_type="application/json")


@app.get("/metrics")
def metrics():
    if not METRICS.exists():
        raise HTTPException(404, "metrics not built — run: python src/precompute.py")
    return FileResponse(METRICS, media_type="application/json")


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "web/index.html missing"}, status_code=404)
    return FileResponse(idx)


# static assets (app.js, style.css, vendor/…). Mounted LAST so the API routes above win.
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
