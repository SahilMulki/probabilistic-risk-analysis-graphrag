"""
ask.py — Phase 6 CLI: ask the LER graph a question, or run the golden-question eval.

    python -m pragraph.ask "What components have failed in HPCI across the corpus?"
    python -m pragraph.ask --golden        # run the MVP-now golden set with scoring
    python -m pragraph.ask --golden --brief # one line per question

Graph retrieval (LLM router + Cypher templates) -> grounded answer (cites LERs).
The --golden runner prints, per question: the routed intent, the nodes/LERs the
retriever actually surfaced (with oracle|pipeline provenance), the grounded answer
and its citations, and a retrieval/answer PASS-FAIL — retrieval scored separately
from answering, and materialize-at-scale questions judged honestly (no N=3 "winner").
"""
from __future__ import annotations

import argparse
import os
import sys


from .answer import answer
from .golden_eval import build_expected, golden, judge
from .llm import LLM
from .load_graph import load_records
from .retrieve import Clarification, GraphRetriever


def _provenance_map() -> dict:
    return {rec.ler_number: src for rec, src in load_records()}


def _fmt_lers(lers, prov) -> str:
    return ", ".join(f"{k} [{prov.get(k, '?')}]" for k in lers) or "(none)"


def _fmt_candidate(c) -> str:
    date = c.get("event_date") or "?"
    syss = ", ".join(x for x in (c.get("systems") or []) if x) or "—"
    title = (c.get("title") or "").strip()
    return (f"      - {c['key']} [{c.get('source') or '?'}] · {date} · systems: {syss}"
            + (f"\n          {title}" if title else ""))


def _render_clarify(outcome: Clarification) -> None:
    print(f"\n  [clarify] {outcome.question}")
    for c in outcome.candidates:
        print(_fmt_candidate(c))
    if outcome.overflow:
        hidden = outcome.total - len(outcome.candidates)
        print(f"      … and {hidden} more not shown — add an event year or use an LER "
              "number to narrow the list.")


# --------------------------------------------------------------------------- #
# single question
# --------------------------------------------------------------------------- #
def ask_one(question: str) -> None:
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    prov = _provenance_map()
    try:
        outcome = gr.retrieve(question)
        ans = None if isinstance(outcome, Clarification) else answer(question, outcome, llm=llm)
    finally:
        gr.close()

    print(f"\nQ: {question}")
    print(f"  routed intent : {outcome.intent}  anchors={outcome.anchors}")

    if isinstance(outcome, Clarification):          # third outcome: ask, don't guess
        _render_clarify(outcome)
        return

    print(f"  retrieved LERs: {_fmt_lers(sorted(outcome.ler_keys()), prov)}")
    if outcome.node_keys:
        print(f"  retrieved nodes: {', '.join(outcome.node_keys)}")
    print(f"\n  answerable: {ans['answerable']}")
    print(f"  answer    : {ans['answer']}")
    print(f"  citations : {_fmt_lers(ans.get('citations', []), prov)}")


# --------------------------------------------------------------------------- #
# golden runner
# --------------------------------------------------------------------------- #
def run_golden(brief: bool = False) -> int:
    llm = LLM()
    gr = GraphRetriever(llm=llm)
    prov = _provenance_map()
    specs = golden(build_expected())
    results = []
    try:
        for spec in specs:
            outcome = gr.retrieve(spec["q"])
            ans = None if isinstance(outcome, Clarification) else answer(spec["q"], outcome, llm=llm)
            ok, why, detail = judge(spec, outcome, ans)
            results.append((spec, outcome, ans, ok, why, detail))
    finally:
        gr.close()

    if brief:
        _print_brief(results, prov)
    else:
        for r in results:
            _print_full(*r, prov=prov)
    return _print_summary(results)


def _rec(x):
    return "—" if x is None else f"{x:.2f}"


def _routed(detail) -> str:
    return (detail["clar"] if "clar" in detail else detail["rs"])["routed_intent"]


def _print_full(spec, outcome, ans, ok, why, detail, prov) -> None:
    print("\n" + "=" * 78)
    print(f"[{spec['id']}] ({spec['kind']}, provenance={spec['provenance']})  {spec['q']}")
    intent_ok = (detail["clar"] if "clar" in detail else detail["rs"])["intent_ok"]
    print(f"  routed intent : {_routed(detail)}"
          f"  ({'expected' if intent_ok else 'EXPECTED ' + spec['intent']})")

    if "clar" in detail:                            # Clarification outcome
        cs = detail["clar"]
        print("  --- clarification ---")
        print(f"    prompt      : {outcome.question}")
        print(f"    candidates  : {cs['total']} events"
              + ("  (capped — overflow, narrow by year/LER#)" if cs["overflow"] else ""))
        for c in outcome.candidates:
            print(_fmt_candidate(c))
    else:                                           # Evidence outcome
        rs, as_ = detail["rs"], detail["as"]
        print("  --- retrieval ---")
        print(f"    node recall : {_rec(rs['node_recall'])}"
              + (f"   missing: {rs['missing_nodes'][:6]}" if rs["missing_nodes"] else ""))
        print(f"    LER  recall : {_rec(rs['ler_recall'])} (known subset)"
              + (f"   missing: {rs['missing_lers']}" if rs["missing_lers"] else ""))
        sl = rs["surfaced_lers"]
        more_l = "" if len(sl) <= 8 else f"   (+{len(sl) - 8} more, {len(sl)} total)"
        print(f"    surfaced LERs: {_fmt_lers(sl[:8], prov)}{more_l}")
        if rs["surfaced_nodes"]:
            shown = rs["surfaced_nodes"][:8]
            more = "" if len(rs["surfaced_nodes"]) <= 8 else f"  (+{len(rs['surfaced_nodes']) - 8} more)"
            print(f"    surfaced nodes: {', '.join(shown)}{more}")
        print("  --- answer ---")
        print(f"    answerable  : {as_['answerable']}")
        print(f"    answer      : {ans['answer'] if ans else '(skipped — clarification)'}")
        print(f"    citations   : {_fmt_lers(as_['citations'], prov)}")
    print(f"  {'PASS' if ok else 'FAIL'}: {why}")
    print(f"  note: {spec['note']}")


def _print_brief(results, prov) -> None:
    print(f"\n{'id':16} {'kind':10} {'intent':22} {'detail':>16}  result")
    for spec, outcome, ans, ok, why, detail in results:
        if "clar" in detail:
            d = f"clar×{detail['clar']['total']}"
        else:
            rs = detail["rs"]
            d = f"nR={_rec(rs['node_recall'])} lR={_rec(rs['ler_recall'])}"
        print(f"{spec['id']:16} {spec['kind']:10} {_routed(detail):22} "
              f"{d:>16}  {'PASS' if ok else 'FAIL'} — {why}")


def _print_summary(results) -> int:
    from collections import Counter
    by_kind = Counter()
    pass_kind = Counter()
    for spec, outcome, ans, ok, why, detail in results:
        by_kind[spec["kind"]] += 1
        if ok:
            pass_kind[spec["kind"]] += 1
    npass = sum(1 for r in results if r[3])
    print("\n" + "=" * 78)
    print("SUMMARY")
    for kind in ("showcase", "xdoc", "aggregation", "payoff", "clarify", "intent",
                 "risk", "honesty", "scale", "negative", "lookup"):
        if by_kind[kind]:
            print(f"  {kind:12}: {pass_kind[kind]}/{by_kind[kind]} pass")
    print(f"  {'TOTAL':12}: {npass}/{len(results)} pass")
    print("\n  Interpretation (~830-doc corpus): showcase proves within-report multi-hop")
    print("  (anchored on an LER number); xdoc/aggregation prove the cross-document joins a")
    print("  flat retriever cannot assemble (HPCI components across ~15 plants, cause")
    print("  distribution over ~770 LERs); payoff is the cross-plant join that was empty at")
    print("  N=3; clarify asks-not-guesses on real same-plant ambiguity; intent guards the")
    print("  router boundary; negative is the no-hallucination check. The graph-vs-vector")
    print("  baseline is the capstone (after the probabilistic layer).")
    return 0 if npass == len(results) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ask the LER graph, or run the golden eval.")
    p.add_argument("question", nargs="?", help="a natural-language question")
    p.add_argument("--golden", action="store_true", help="run the golden-question eval")
    p.add_argument("--brief", action="store_true", help="one line per golden question")
    args = p.parse_args(argv)

    if args.golden:
        return run_golden(brief=args.brief)
    if not args.question:
        p.error("provide a question, or use --golden")
    ask_one(args.question)
    return 0


if __name__ == "__main__":
    sys.exit(main())
