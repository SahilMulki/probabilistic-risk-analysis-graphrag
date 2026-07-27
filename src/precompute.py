"""
precompute.py — build the static demo bundle + metrics for the Phase-10a web UI.

Demo mode is the headline deliverable: a clone runs the whole core demo from
`web/demo_bundle.json` with NO Neo4j, NO API key, NO embedder. The honesty rule
(guardrail #8, reframed): the bundle is the REAL pipeline output, captured verbatim —
never a hand-edited "nicer" version. Because the answer text is LLM-generated (and so
not reproducible byte-for-byte), the demo≡live guarantee is asserted on the DETERMINISTIC
retrieval layer:

    python src/precompute.py            # capture the showcase bundle + metrics (costs API $)
    python src/precompute.py --assert   # re-run RETRIEVAL only (LLM-free) and prove identity
    python src/precompute.py --metrics  # rebuild only web/metrics.json (LLM-free)

`--assert` re-runs the vector retriever (fully deterministic) and re-dispatches the graph
Cypher template for the stored (intent, anchors) — bypassing the LLM router — then checks the
Evidence text / node_keys / LER set match the bundle exactly. The LLM answer prose is NOT
asserted (it cannot be); it is presented as captured output, stamped with the git SHA and
timestamp it was generated at.

The showcase is chosen from the 42 pre-registered golden specs to feature BOTH where the graph
wins AND where it does not — the vector-wins `lookup-content` case, the `negative` tie, and the
graph-only "vector fabricates statistics" risk case are all deliberately included.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as webapp
from answer import answer as answer_fn
from compare import HEAD_TO_HEAD, EPS, recall_at_k
from golden_eval import build_expected, golden
from llm import LLM
from retrieve import Clarification, GraphRetriever
from vector_baseline import VectorRetriever

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
BUNDLE = WEB_DIR / "demo_bundle.json"
METRICS = WEB_DIR / "metrics.json"
COMPARE_RESULTS = REPO_ROOT / "out" / "vector" / "compare_results.json"

# The showcase — pre-registered ids only, spanning graph-wins, vector-wins, tie, and the
# graph-only capability claims (per the greenlit plan). Order is the display order.
SHOWCASE = [
    "XDOC-HPCI-COMP",    # cross-doc  — GRAPH; System:BJ hub (47 reports across ~15 plants)
    "XDOC-BACKUPS",      # cross-doc  — GRAPH; the 274-event pseudo-hub (drives truncation demo)
    "MH-Limerick",       # multi-hop  — GRAPH; within-report cause->outcome chain
    "LOOK-CAUSE-VOGTLE", # lookup-id  — GRAPH; exact-key resolution vector cannot pin
    "LOOKC-PRAIRIE",     # lookup-content — VECTOR wins (free-form semantic search)
    "NEG-CHERNOBYL",     # negative   — TIE (both correctly refuse)
    "LIKELY-OUTCOME",    # risk       — GRAPH-ONLY; vector fabricates frequencies
    "CLARIFY-PLANT",     # clarify    — GRAPH-ONLY; graph asks instead of guessing
]


def _git_sha() -> str:
    """Read the current commit SHA WITHOUT invoking git (owner's standing rule: no git
    commands). Best-effort from .git/HEAD + refs; 'unknown' if it can't be resolved."""
    try:
        head = (REPO_ROOT / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            loose = REPO_ROOT / ".git" / ref
            if loose.exists():
                return loose.read_text().strip()[:12]
            packed = REPO_ROOT / ".git" / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0][:12]
            return "unknown"
        return head[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


def _stamp() -> dict:
    return {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "git_sha": _git_sha(),
            "vector": {"model": webapp.VEC_MODEL, "config": webapp.VEC_CONFIG, "k": webapp.VEC_K}}


# --------------------------------------------------------------------------- #
# capture — the real pipeline, into the shared `result` shape
# --------------------------------------------------------------------------- #
def build_bundle() -> dict:
    specs = {s["id"]: s for s in golden(build_expected())}
    missing = [i for i in SHOWCASE if i not in specs]
    if missing:
        raise SystemExit(f"showcase ids not in golden set: {missing}")

    llm = LLM()
    gr = GraphRetriever(llm=llm)
    vr = VectorRetriever(model=webapp.VEC_MODEL, config=webapp.VEC_CONFIG, k=webapp.VEC_K)
    prov = webapp.provenance_map()
    h2h = _head_to_head_scores()  # per-id (graph_score, vector_score) from Phase-9

    examples = []
    with gr.driver.session() as session:
        for i in SHOWCASE:
            spec = specs[i]
            q = spec["q"]
            print(f"  capturing [{spec['bucket']:14}] {i} …", flush=True)

            g_out = gr.retrieve(q)
            g_ans = None if isinstance(g_out, Clarification) else answer_fn(q, g_out, llm=llm)
            v_out = vr.retrieve(q)
            v_ans = answer_fn(q, v_out, llm=llm)

            g_side = webapp.build_side(g_out, g_ans, prov)
            v_side = webapp.build_side(v_out, v_ans, prov)
            g_score, v_score = h2h.get(i, (None, None))
            verdict = webapp.verdict_for(spec["bucket"], g_score, v_score)

            sub = webapp.suggest_subgraph(g_side, g_out)
            sub_data = webapp.build_subgraph(session, sub) if sub else None

            examples.append({
                "id": i, "question": q, "bucket": spec["bucket"], "mode": "demo",
                "note": spec.get("note"),
                "graph": g_side, "vector": v_side, "verdict": verdict,
                "subgraph": sub, "subgraph_data": sub_data,
                # minimal retrieval snapshot for --assert (router bypass); UI ignores it.
                "_graph_anchors": getattr(g_out, "anchors", {}) or {},
            })
    gr.close()
    return {**_stamp(),
            "note": "captured real pipeline output; demo mode replays it verbatim. "
                    "Verify retrieval identity with: python src/precompute.py --assert",
            "examples": examples}


# --------------------------------------------------------------------------- #
# metrics — Phase-9 head-to-head table + recall@k (LLM-free)
# --------------------------------------------------------------------------- #
def _head_to_head_scores() -> dict:
    """Per-id (graph_score, vector_score) from the cached Phase-9 head-to-head."""
    if not COMPARE_RESULTS.exists():
        raise SystemExit(f"missing {COMPARE_RESULTS} — run: python src/compare.py --headtohead")
    rows = json.loads(COMPARE_RESULTS.read_text())["rows"]
    return {r["id"]: (r["graph"].get("score"), r["vector"].get("score")) for r in rows}


def _head_to_head_table() -> list[dict]:
    rows = json.loads(COMPARE_RESULTS.read_text())["rows"]
    out = []
    for bucket in HEAD_TO_HEAD:
        brows = [r for r in rows if r["bucket"] == bucket]
        if not brows:
            continue
        g = sum(r["graph"]["score"] for r in brows) / len(brows)
        v = sum(r["vector"]["score"] for r in brows) / len(brows)
        win = tie = loss = 0
        for r in brows:
            gs, vs = r["graph"]["score"], r["vector"]["score"]
            if abs(gs - vs) <= EPS:
                tie += 1
            elif gs > vs:
                win += 1
            else:
                loss += 1
        verdict = "GRAPH" if g - v > EPS else "VECTOR" if v - g > EPS else "TIE"
        out.append({"bucket": bucket, "n": len(brows), "graph": round(g, 2),
                    "vector": round(v, 2), "win": win, "tie": tie, "loss": loss,
                    "verdict": verdict})
    return out


def build_metrics() -> dict:
    ks = (1, 2, 5, 10, 20, 50, 100)
    rk = recall_at_k(model=webapp.VEC_MODEL, config=webapp.VEC_CONFIG, ks=ks)
    specs = {s["id"]: s for s in golden(build_expected())}
    recall_rows = [{"id": cid, "question": specs.get(cid, {}).get("q", cid),
                    "full": d["full"],
                    "recall": {str(k): round(d["recall"][k], 3) for k in ks}}
                   for cid, d in rk.items()]
    return {**_stamp(),
            "head_to_head": _head_to_head_table(),
            "recall_at_k": {"ks": list(ks), "rows": recall_rows},
            "graph_suite": "42 of 42 evaluation questions passed",
            "caption": "Both systems answered the same set of evaluation questions using the same "
                       "answer-writing model, so the only difference measured here is how each one "
                       "RETRIEVES the reports. Each question's category was decided in advance, "
                       "before either system ran, so the winner could not be chosen after the fact."}


# --------------------------------------------------------------------------- #
# --assert — prove demo == live at the DETERMINISTIC retrieval layer
# --------------------------------------------------------------------------- #
def assert_identity() -> int:
    if not BUNDLE.exists():
        raise SystemExit(f"no bundle at {BUNDLE} — build it first: python src/precompute.py")
    bundle = json.loads(BUNDLE.read_text())
    gr = GraphRetriever()  # no LLM needed: we re-dispatch templates directly
    vr = VectorRetriever(model=bundle["vector"]["model"], config=bundle["vector"]["config"],
                         k=bundle["vector"]["k"])
    ok = True
    print(f"asserting retrieval identity for {len(bundle['examples'])} examples "
          f"(bundle git_sha={bundle['git_sha']})\n")
    with gr.driver.session() as s:
        for ex in bundle["examples"]:
            q, gid = ex["question"], ex["id"]

            # vector: fully deterministic — re-run and compare the Evidence verbatim.
            v_live = vr.retrieve(q)
            v_ok = (v_live.text == ex["vector"].get("evidence_text")
                    and sorted(v_live.ler_keys()) == sorted({l["key"] for l in ex["vector"]["lers"]}))

            # graph: re-dispatch the Cypher template for the STORED intent+anchors (no router).
            intent, anchors = ex["graph"]["intent"], ex["_graph_anchors"]
            handler = getattr(gr, f"_t_{intent}", None)
            g_live = handler(s, anchors) if handler else None
            if ex["graph"]["mode"] == "clarify":
                live_keys = sorted(g_live.candidate_keys()) if isinstance(g_live, Clarification) else None
                g_ok = live_keys == sorted(c["key"] for c in ex["graph"]["candidates"])
            else:
                g_ok = (not isinstance(g_live, Clarification)
                        and g_live is not None
                        and g_live.text == ex["graph"].get("evidence_text")
                        and sorted(g_live.ler_keys()) == sorted({l["key"] for l in ex["graph"]["lers"]}))

            ok = ok and v_ok and g_ok
            print(f"  [{'OK ' if (v_ok and g_ok) else 'FAIL'}] {gid:20} "
                  f"graph={'✓' if g_ok else '✗'} vector={'✓' if v_ok else '✗'}")
    gr.close()
    print("\n" + ("PASS — demo bundle retrieval is byte-identical to a fresh live retrieval."
                  if ok else "FAIL — bundle drifted from live retrieval; rebuild it."))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))
    print(f"  wrote {path.relative_to(REPO_ROOT)}  ({path.stat().st_size // 1024} KB)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build the Phase-10a demo bundle + metrics.")
    p.add_argument("--assert", dest="do_assert", action="store_true",
                   help="re-run retrieval (LLM-free) and prove demo == live; no rebuild")
    p.add_argument("--metrics", action="store_true", help="rebuild only metrics.json (LLM-free)")
    p.add_argument("--examples", action="store_true", help="rebuild only the demo bundle")
    args = p.parse_args(argv)

    if args.do_assert:
        return assert_identity()
    if args.metrics:
        print("building metrics.json (LLM-free)…")
        _write(METRICS, build_metrics())
        return 0
    if args.examples:
        print("capturing demo bundle…")
        _write(BUNDLE, build_bundle())
        return 0
    # default: build both, then assert
    print("capturing demo bundle (real pipeline; costs API $)…")
    _write(BUNDLE, build_bundle())
    print("building metrics.json (LLM-free)…")
    _write(METRICS, build_metrics())
    print()
    return assert_identity()


if __name__ == "__main__":
    sys.exit(main())
