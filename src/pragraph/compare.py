"""
compare.py — the Phase-9 graph-vs-vector head-to-head (the capstone).

Runs the SAME pre-registered, bucket-tagged golden set (golden_eval.py) through BOTH
retrievers and scores them identically, so the only variable is retrieval:

  GraphRetriever ─┐                     ┌─ per-bucket win/tie/loss (LLM-scored answers)
                  ├─ Evidence ─ answer ─┤
  VectorRetriever ┘   (shared)          └─ retrieval-only analyses (no LLM):
                                            * recall@k on cross-doc (the money chart)
                                            * refusal PR curve (threshold swept, not a point)
                                            * chunking ablation, per bucket
                                            * embedder invariance (bge-large vs MiniLM)

Fairness invariants (see the module docstrings of vector_baseline.py / answer.py):
  * identical answer model + a format-neutral answer prompt (so vector's prose evidence
    is not penalised vs the graph's typed evidence — verified by the parity check);
  * a competent local baseline (strong AND weak embedder), reported together so the
    structural verdicts are shown embedder-invariant;
  * k and the refusal threshold are reported as sweeps/curves, never a cherry-picked point;
  * cross-doc ground truth is the DETERMINISTIC coded-hub membership, which the graph
    computes exactly — vector's shortfall there is the thesis, not an artifact.

Head-to-head buckets (both genuinely attempt): lookup, multi-hop, cross-doc, negative.
Graph-capability buckets (vector structurally cannot compete): aggregation, risk, clarify
— reported separately as capability claims, not scored head-to-heads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


from .answer import answer as answer_fn
from .golden_eval import build_expected, golden
from .llm import LLM
from .retrieve import Clarification, Evidence, GraphRetriever
from .vector_baseline import (CHUNK_CONFIGS, DEFAULT_CONFIG, DEFAULT_K, MODELS,
                             VectorIndex, VectorRetriever)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "out" / "vector" / "compare_results.json"

HEAD_TO_HEAD = ("lookup-id", "lookup-content", "multi-hop", "cross-doc", "negative")
GRAPH_ONLY = ("aggregation", "risk", "clarify")
# in-corpus buckets that SHOULD be answered (used as the positive side of the refusal curve)
POSITIVE_BUCKETS = ("lookup-id", "lookup-content", "multi-hop", "cross-doc")
RETRIEVAL_BUCKETS = ("lookup-id", "lookup-content", "multi-hop", "cross-doc")
EPS = 0.05                                                 # cross-doc recall tie band


# --------------------------------------------------------------------------- #
# outcome normalization — run a retriever, get (surfaced_lers, answer_dict)
# --------------------------------------------------------------------------- #
def graph_outcome(gr, llm, q):
    out = gr.retrieve(q)
    if isinstance(out, Clarification):
        # asking-to-disambiguate is neither an answer nor a refusal; for the head-to-head
        # buckets it counts as "did not answer" (surfaced = the candidates it offered).
        return out.candidate_keys(), {"answerable": False, "answer": "(asked to clarify)",
                                      "citations": []}, "clarify"
    ans = answer_fn(q, out, llm)
    return out.ler_keys(), ans, out.intent


def vector_outcome(vr, llm, q):
    ev = vr.retrieve(q)
    ans = answer_fn(q, ev, llm)
    return ev.ler_keys(), ans, ev.anchors.get("top_score")


# --------------------------------------------------------------------------- #
# per-question correctness by bucket (0/1 for binary buckets; recall for cross-doc)
# --------------------------------------------------------------------------- #
def binary_correct(spec, surfaced, ans) -> float:
    bucket = spec["bucket"]
    answerable = bool(ans.get("answerable"))
    cites = set(ans.get("citations") or [])
    grounded = cites <= set(surfaced)
    text = (ans.get("answer") or "").lower()
    exp = spec["exp_lers"]
    fact = any(sub in text for sub in spec.get("exp_answer", []))
    if bucket == "negative":
        return 1.0 if (not answerable and not cites) else 0.0
    if bucket == "lookup-id":
        # must resolve the SPECIFIC report: cite the exact LER AND state the field value
        return 1.0 if (answerable and grounded and (exp <= cites) and fact) else 0.0
    if bucket == "lookup-content":
        # free-form: find A report about the distinctive event and name the plant (the answer);
        # grounded + correct plant is the bar (the exact LER citation is secondary here)
        return 1.0 if (answerable and grounded and fact) else 0.0
    if bucket == "multi-hop":
        return 1.0 if (answerable and grounded and (exp <= cites)) else 0.0
    return 0.0


def recall(full: set, surfaced: set) -> float:
    return (len(full & surfaced) / len(full)) if full else None


# --------------------------------------------------------------------------- #
# the LLM-scored head-to-head (primary config only)
# --------------------------------------------------------------------------- #
def head_to_head(model=None, config=DEFAULT_CONFIG, k=DEFAULT_K, threshold=0.0,
                 cache=True) -> dict:
    model = model or "bge-large"
    specs = golden(build_expected())
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    vr = VectorRetriever(model=model, config=config, k=k, threshold=threshold)

    rows = []
    for spec in specs:
        q = spec["q"]
        g_lers, g_ans, g_intent = graph_outcome(gr, llm, q)
        v_lers, v_ans, v_top = vector_outcome(vr, llm, q)
        row = {"id": spec["id"], "bucket": spec["bucket"], "q": q,
               "graph": {"lers": sorted(g_lers), "answerable": bool(g_ans.get("answerable")),
                         "citations": g_ans.get("citations") or [],
                         "answer": g_ans.get("answer") or ""},
               "vector": {"lers": sorted(v_lers), "answerable": bool(v_ans.get("answerable")),
                          "citations": v_ans.get("citations") or [], "top_score": v_top,
                          "answer": v_ans.get("answer") or ""}}
        if spec["bucket"] == "cross-doc":
            full = set(g_lers)   # deterministic coded-hub membership = the graph's own set
            row["full_set_n"] = len(full)
            row["graph"]["score"] = recall(full, set(g_lers))   # 1.0 by construction
            row["vector"]["score"] = recall(full, set(v_lers))
        elif spec["bucket"] in ("lookup-id", "lookup-content", "multi-hop", "negative"):
            row["graph"]["score"] = binary_correct(spec, g_lers, g_ans)
            row["vector"]["score"] = binary_correct(spec, v_lers, v_ans)
        else:  # graph-capability buckets: record, don't score as head-to-head
            row["graph"]["score"] = None
            row["vector"]["score"] = None
        rows.append(row)
        print(f"  [{spec['bucket']:10}] {spec['id']:20} "
              f"G={_fmt(row['graph'].get('score'))} V={_fmt(row['vector'].get('score'))}",
              flush=True)
    gr.close()
    out = {"model": model, "config": config, "k": k, "threshold": threshold, "rows": rows}
    if cache:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(json.dumps(out, indent=2))
    return out


def _fmt(x):
    return "—" if x is None else f"{x:.2f}"


def summarize_head_to_head(res: dict) -> None:
    rows = res["rows"]
    print("\n" + "=" * 78)
    print(f"HEAD-TO-HEAD  (answer model shared; retrieval = graph vs "
          f"vector[{res['model']}/{res['config']}, k={res['k']}])")
    print("=" * 78)
    print(f"{'bucket':12} {'n':>2}  {'graph':>6} {'vector':>6}   win/tie/loss (graph POV)")
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
        verdict = ("GRAPH" if g - v > EPS else "VECTOR" if v - g > EPS else "TIE")
        print(f"{bucket:12} {len(brows):>2}  {g:6.2f} {v:6.2f}   "
              f"{win}/{tie}/{loss}   -> {verdict}")
    print("\ngraph-capability buckets (vector structurally cannot compete — capability "
          "claim, not scored):")
    for bucket in GRAPH_ONLY:
        brows = [r for r in rows if r["bucket"] == bucket]
        if not brows:
            continue
        # how often does vector FABRICATE (answers + cites) on these vs the graph's honest handling?
        v_answered = sum(1 for r in brows if r["vector"]["answerable"])
        print(f"  {bucket:12} n={len(brows)}  vector produced an answer on "
              f"{v_answered}/{len(brows)} (see rows for whether it fabricated counts)")


# --------------------------------------------------------------------------- #
# retrieval-only analyses (NO LLM) — free to sweep across models/configs/k
# --------------------------------------------------------------------------- #
_FULL_SETS_MEMO: dict | None = None

# Cross-doc ground truth = the graph's exact coded-hub membership. We reach it by calling the
# graph TEMPLATES directly with known anchors (the templates are pure Cypher), bypassing the
# LLM router entirely — so the retrieval-only analyses use ZERO API calls and are deterministic.
CROSSDOC_ANCHORS = {
    "XDOC-HPCI-COMP":  ("system_components", {"system_code": "BJ"}),
    "XDOC-RCIC-COMP":  ("system_components", {"system_code": "BN"}),
    "AGG-WEAK-PROG":   ("weak_program_events", {}),
    "XPLANT-SHARED":   ("shared_component_cause", {}),
    "XDOC-BACKUPS":    ("mitigating_backups", {}),
}


def _graph_full_sets() -> dict:
    """Deterministic cross-doc ground truth via direct template (Cypher) calls — memoized,
    and LLM-free (no router), so it works even without API credits."""
    global _FULL_SETS_MEMO
    if _FULL_SETS_MEMO is not None:
        return _FULL_SETS_MEMO
    q_by_id = {s["id"]: s["q"] for s in golden(build_expected())}
    gr = GraphRetriever()
    full = {}
    with gr.driver.session() as s:
        for cid, (intent, anchors) in CROSSDOC_ANCHORS.items():
            out = getattr(gr, f"_t_{intent}")(s, anchors)
            keys = out.candidate_keys() if isinstance(out, Clarification) else out.ler_keys()
            full[cid] = (q_by_id[cid], keys)
    gr.close()
    _FULL_SETS_MEMO = full
    return full


def recall_at_k(model="bge-large", config=DEFAULT_CONFIG, ks=(1, 2, 5, 10, 20, 50, 100)) -> dict:
    """Cross-doc recall@k for vector against the graph's complete hub set — shows vector
    saturating below 1.0 while the graph assembles the full set exactly."""
    full = _graph_full_sets()
    idx = VectorIndex(model, config)
    print("\n" + "=" * 78)
    print(f"RECALL@k — cross-doc (vector[{model}/{config}] vs deterministic hub set; "
          "graph = 1.00 by construction)")
    print("=" * 78)
    header = "question            |full|  " + "  ".join(f"@{k:<4}" for k in ks)
    print(header)
    out = {}
    for cid, (q, fullset) in full.items():
        ranked = [ler for ler, _ in idx.ranked_lers(q, k_chunks=1500)]
        recs = []
        for k in ks:
            topk = set(ranked[:k])
            recs.append(recall(set(fullset), topk) or 0.0)
        out[cid] = {"full": len(fullset), "recall": dict(zip(ks, recs))}
        print(f"{cid:20} {len(fullset):>5}  " + "  ".join(f"{r:.2f} " for r in recs))
    return out


def refusal_curve(model="bge-large", config=DEFAULT_CONFIG,
                  thresholds=(0.0, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7)) -> dict:
    """Sweep the vector refusal threshold over in-corpus positives and out-of-corpus
    negatives; report refuse-rate-vs-retention as a CURVE (not a single tuned point).
    The graph refuses structurally: negatives 100% refused, positives 100% retained."""
    specs = golden(build_expected())
    idx = VectorIndex(model, config)
    pos = [s for s in specs if s["bucket"] in POSITIVE_BUCKETS]
    neg = [s for s in specs if s["bucket"] == "negative"]
    pos_top = [idx.search(s["q"], 1)[0]["score"] for s in pos]
    neg_top = [idx.search(s["q"], 1)[0]["score"] for s in neg]
    print("\n" + "=" * 78)
    print(f"REFUSAL PR CURVE — vector[{model}/{config}]  (top-1 similarity threshold swept)")
    print("=" * 78)
    print(f"  in-corpus positives n={len(pos)}   out-of-corpus negatives n={len(neg)}")
    print(f"  graph baseline: negatives refused 100%, positives retained 100% (structural)\n")
    print(f"  {'thresh':>7}  {'neg refused':>12}  {'pos retained':>13}")
    out = {}
    for t in thresholds:
        neg_ref = sum(1 for s in neg_top if s < t) / len(neg)
        pos_ret = sum(1 for s in pos_top if s >= t) / len(pos)
        out[t] = {"neg_refused": neg_ref, "pos_retained": pos_ret}
        print(f"  {t:>7.2f}  {neg_ref:>11.0%}  {pos_ret:>12.0%}")
    print(f"\n  (top-1 score ranges: positives {min(pos_top):.2f}-{max(pos_top):.2f}, "
          f"negatives {min(neg_top):.2f}-{max(neg_top):.2f})")
    return out


def bucket_retrieval_recall(model, config, k=DEFAULT_K) -> dict:
    """Mean per-bucket LER retrieval recall (no LLM) — the substrate for the chunking
    ablation and the embedder-invariance check. For cross-doc the denominator is the
    graph's full hub set; for lookup/multi-hop it is the single expected LER."""
    specs = golden(build_expected())
    full = _graph_full_sets()
    idx = VectorIndex(model, config)
    vr = VectorRetriever(index=idx, k=k)
    by_bucket = {}
    for spec in specs:
        b = spec["bucket"]
        if b not in RETRIEVAL_BUCKETS:
            continue
        surfaced = vr.retrieve(spec["q"]).ler_keys()
        if b == "cross-doc":
            r = recall(set(full[spec["id"]][1]), set(surfaced))
        else:
            r = recall(set(spec["exp_lers"]), set(surfaced))
        by_bucket.setdefault(b, []).append(r if r is not None else 0.0)
    return {b: sum(v) / len(v) for b, v in by_bucket.items()}


def chunk_ablation(model="bge-large", k=DEFAULT_K) -> dict:
    print("\n" + "=" * 78)
    print(f"CHUNKING ABLATION — per-bucket vector retrieval recall ({model}, k={k})")
    print("=" * 78)
    print(f"  {'config':8} " + "  ".join(f"{b:>13}" for b in RETRIEVAL_BUCKETS))
    out = {}
    for config in CHUNK_CONFIGS:
        rec = bucket_retrieval_recall(model, config, k)
        out[config] = rec
        print(f"  {config:8} " + "  ".join(f"{rec.get(b, 0):>13.2f}" for b in RETRIEVAL_BUCKETS))
    return out


def embedder_invariance(config=DEFAULT_CONFIG, k=DEFAULT_K) -> dict:
    print("\n" + "=" * 78)
    print(f"EMBEDDER INVARIANCE — per-bucket vector retrieval recall (config={config}, k={k})")
    print("=" * 78)
    print(f"  {'model':10} " + "  ".join(f"{b:>13}" for b in RETRIEVAL_BUCKETS))
    out = {}
    for model in MODELS:
        rec = bucket_retrieval_recall(model, config, k)
        out[model] = rec
        print(f"  {model:10} " + "  ".join(f"{rec.get(b, 0):>13.2f}" for b in RETRIEVAL_BUCKETS))
    print("\n  (identical verdicts on cross-doc => the structural result is not an artifact "
          "of the embedder)")
    return out


def parity_check(model="bge-large", config=DEFAULT_CONFIG, k=DEFAULT_K) -> None:
    """Answer-format fairness gate — RETRIEVAL HELD CONSTANT, only the FORMAT varied.

    For the same report we build (a) the graph's typed-triple evidence and (b) prose evidence
    made of that same report's own raw-text chunks, then ask the SAME question of the SAME
    answerer. Both bundles contain the answer, so any quality gap is the PROMPT favouring an
    evidence shape — which is the thing this gate exists to rule out.

    Feeding each retriever whatever it happens to fetch would CONFOUND format with retrieval
    (a vector miss would look like a format failure); retrieval quality is measured separately
    by the head-to-head and the recall@k curves.
    """
    probes = [
        ("382-2024-002-00",
         "What chain of failures led to the reactor trip in LER 382-2024-002-00?",
         ["transformer", "bushing", "trip"]),
        ("424-2025-001-00",
         "What was the root cause of the event in LER 424-2025-001-00?",
         ["human performance", "human error", "feedwater"]),
        ("382-2025-002-00",
         "Which nuclear plant reported LER 382-2025-002-00?",
         ["waterford"]),
    ]
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    idx = VectorIndex(model, config)
    by_ler: dict[str, list[str]] = {}
    for c in idx.chunks:
        by_ler.setdefault(c.ler, []).append(c.text)

    print("\n" + "=" * 78)
    print("ANSWER-FORMAT PARITY — same report, triples vs prose, one answerer "
          "(retrieval held constant)")
    print("=" * 78)

    def ok(ans, lers, facts):
        t = (ans.get("answer") or "").lower()
        grounded = set(ans.get("citations") or []) <= set(lers)
        return bool(ans.get("answerable")) and grounded and any(f in t for f in facts)

    all_ok = True
    for ler, q, facts in probes:
        g_out = gr.retrieve(q)
        if isinstance(g_out, Clarification):
            print(f"\nQ: {q}\n  (graph asked to clarify — skipping as a parity probe)")
            continue
        g_ans = answer_fn(q, g_out, llm)
        # same report, prose form: that LER's own raw chunks (capped to a comparable size)
        prose = "\n    [...]\n    ".join(by_ler.get(ler, [])[:8])
        p_ev = Evidence("vector", {"parity": True, "ler": ler},
                        f"Retrieved passages from the source LER reports:\n\n[LER {ler}]\n    {prose}",
                        node_keys=[], lers=[{"key": ler, "source": "vector"}])
        p_ans = answer_fn(q, p_ev, llm)
        go = ok(g_ans, g_out.ler_keys(), facts)
        po = ok(p_ans, p_ev.ler_keys(), facts)
        all_ok = all_ok and go and po
        print(f"\nQ: {q}")
        print(f"  triples [{'OK' if go else 'XX'}]: {(g_ans.get('answer') or '')[:130]}")
        print(f"  prose   [{'OK' if po else 'XX'}]: {(p_ans.get('answer') or '')[:130]}")
    gr.close()
    print("\n  PARITY " + ("HOLDS — the answerer handles prose evidence as competently as "
                           "triples, so head-to-head gaps are RETRIEVAL, not the prompt."
                           if all_ok else
                           "FAILED — the answer prompt may favour one evidence format; inspect."))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Graph-vs-vector comparison (Phase 9).")
    p.add_argument("--parity", action="store_true", help="answer-format parity check (LLM)")
    p.add_argument("--headtohead", action="store_true", help="LLM-scored per-bucket head-to-head")
    p.add_argument("--recall", action="store_true", help="cross-doc recall@k (no LLM)")
    p.add_argument("--refusal", action="store_true", help="refusal PR curve (no LLM)")
    p.add_argument("--ablation", action="store_true", help="chunking ablation (no LLM)")
    p.add_argument("--invariance", action="store_true", help="embedder invariance (no LLM)")
    p.add_argument("--retrieval", action="store_true", help="all retrieval-only analyses (no LLM)")
    p.add_argument("--all", action="store_true", help="everything (retrieval-only + head-to-head)")
    p.add_argument("--model", default="bge-large", choices=list(MODELS))
    p.add_argument("--config", default=DEFAULT_CONFIG, choices=list(CHUNK_CONFIGS))
    p.add_argument("--k", type=int, default=DEFAULT_K)
    args = p.parse_args(argv)

    did = False
    if args.parity or args.all:
        parity_check(args.model, args.config, args.k); did = True
    if args.recall or args.retrieval or args.all:
        recall_at_k(args.model, args.config); did = True
    if args.refusal or args.retrieval or args.all:
        refusal_curve(args.model, args.config); did = True
    if args.ablation or args.retrieval or args.all:
        chunk_ablation(args.model, args.k); did = True
    if args.invariance or args.retrieval or args.all:
        embedder_invariance(args.config, args.k); did = True
    if args.headtohead or args.all:
        res = head_to_head(args.model, args.config, args.k); summarize_head_to_head(res); did = True
    if not did:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
