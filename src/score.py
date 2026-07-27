"""
score.py — score an extracted `LERRecord` against the hand-marked oracle.

The fast-feedback loop for prompt iteration (docs/journal/phase_4.md). Canonicalize both
graphs to `match_key`, align nodes, then report:

  * Node P/R/F1, overall and per type. Coded nodes (LER/Unit/coded
    System·Component/Cause/Manufacturer) align on exact `match_key`; un-coded
    nodes (FailureMode/Consequence/CorrectiveAction/RegulatoryReference and
    name-slug System·Component) align by type with fuzzy (rapidfuzz) name
    tolerance, since the LLM will phrase them slightly differently each run.
  * Edge P/R/F1 on `(source, relation, target)` triples, evaluated in the
    *aligned* key space so a fuzzy node rename doesn't sink its edges.
  * Identity / reporting-basis / cause-code exact checks.

Tolerances (oracle stays frozen — encoded here):
  * Limerick PCIV: Component code `ISV` ≡ `V`.
  * Dresden provisional (block-13 TBD): the cause-code check is excused, and a
    missing provisional CorrectiveAction placeholder isn't a recall penalty.

Gate (docs/journal/phase_4.md): identity 100%, cause-code 100% on non-provisional,
edge-F1 ≥ 0.85, every coded System/Component resolved.

    python src/score.py out/            # score every extracted JSON in out/
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rapidfuzz import fuzz

from models import GroundTruth, LERRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE_PATH = REPO_ROOT / "data" / "raw" / "ground_truth" / "ground_truth.json"

FUZZY_TYPES = {"FailureMode", "Consequence", "CorrectiveAction", "RegulatoryReference"}
IDENTITY_FIELDS = [
    "event_date", "report_date", "operating_mode", "power_level",
    "status", "revision", "ens_number", "ens_date", "ens_time",
]
DEFAULT_THRESHOLD = 85


# --------------------------------------------------------------------------- #
# key helpers
# --------------------------------------------------------------------------- #
def _norm_key(k: str) -> str:
    """Apply the frozen-oracle tolerances at the key level (Limerick ISV ≡ V)."""
    return k.replace("Component:ISV", "Component:V")


def _strategy(node) -> str:
    if node.type in FUZZY_TYPES:
        return "fuzzy"
    if node.type in ("System", "Component") and getattr(node, "eiis_code", None) is None:
        return "fuzzy"
    return "exact"


def _prf(tp: int, n_pred: int, n_true: int) -> tuple[float, float, float]:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_true if n_true else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


# --------------------------------------------------------------------------- #
# node alignment
# --------------------------------------------------------------------------- #
def align_nodes(ext_nodes, gt_nodes, threshold: int) -> list[tuple]:
    """Return matched (ext_node, gt_node) pairs. Exact keys first, then a greedy
    best-first fuzzy pass within each type."""
    matches: list[tuple] = []
    used_gt: set[int] = set()

    gt_exact: dict[str, object] = {}
    for g in gt_nodes:
        if _strategy(g) == "exact":
            gt_exact.setdefault(_norm_key(g.match_key), g)
    for e in ext_nodes:
        if _strategy(e) == "exact":
            g = gt_exact.get(_norm_key(e.match_key))
            if g is not None and id(g) not in used_gt:
                matches.append((e, g))
                used_gt.add(id(g))

    pairs = []
    ext_fuzzy = [e for e in ext_nodes if _strategy(e) == "fuzzy"]
    gt_fuzzy = [g for g in gt_nodes if _strategy(g) == "fuzzy"]
    for e in ext_fuzzy:
        for g in gt_fuzzy:
            if e.type == g.type:
                s = fuzz.token_set_ratio(e.display_name.lower(), g.display_name.lower())
                pairs.append((s, e, g))
    pairs.sort(key=lambda x: -x[0])
    used_e: set[int] = set()
    for s, e, g in pairs:
        if s < threshold:
            break
        if id(e) in used_e or id(g) in used_gt:
            continue
        matches.append((e, g))
        used_e.add(id(e))
        used_gt.add(id(g))

    return matches


# --------------------------------------------------------------------------- #
# per-record scoring
# --------------------------------------------------------------------------- #
def score_record(
    ext: LERRecord,
    gt: LERRecord,
    threshold: int = DEFAULT_THRESHOLD,
    provisional_tol: bool = True,
) -> dict:
    matches = align_nodes(ext.nodes, gt.nodes, threshold)
    matched_gt = {id(g) for _, g in matches}
    matched_ext = {id(e) for e, _ in matches}

    # ---- Dresden-style provisional tolerance: excuse unmatched provisional CAs
    excused_gt = 0
    if provisional_tol:
        for g in gt.nodes:
            if (
                id(g) not in matched_gt
                and g.type == "CorrectiveAction"
                and getattr(g, "provisional", False)
            ):
                excused_gt += 1

    # ---- node P/R/F1 (overall) ----
    tp = len(matches)
    node_p, node_r, node_f = _prf(tp, len(ext.nodes), len(gt.nodes) - excused_gt)

    # ---- per-type node counts ----
    per_type: dict[str, dict] = {}
    types = {n.type for n in ext.nodes} | {n.type for n in gt.nodes}
    for t in sorted(types):
        tp_t = sum(1 for e, g in matches if g.type == t)
        pred_t = sum(1 for n in ext.nodes if n.type == t)
        true_t = sum(1 for n in gt.nodes if n.type == t)
        p, r, f = _prf(tp_t, pred_t, true_t)
        per_type[t] = {"tp": tp_t, "pred": pred_t, "true": true_t,
                       "p": p, "r": r, "f1": f}

    # ---- edge P/R/F1 in aligned key space ----
    align_map = {e.match_key: _norm_key(g.match_key) for e, g in matches}

    def tr(k: str) -> str:
        return align_map.get(k, _norm_key(k))

    ext_tr = {(tr(s), rel, tr(t)) for (s, rel, t) in ext.edge_triples()}
    gt_tr = {(_norm_key(s), rel, _norm_key(t)) for (s, rel, t) in gt.edge_triples()}
    etp = len(ext_tr & gt_tr)
    edge_p, edge_r, edge_f = _prf(etp, len(ext_tr), len(gt_tr))

    # ---- identity / reporting-basis field checks ----
    fields: dict[str, bool] = {}
    for f in IDENTITY_FIELDS:
        fields[f] = getattr(ext.identity, f) == getattr(gt.identity, f)
    fields["reported_under"] = set(ext.reporting_basis.reported_under) == set(
        gt.reporting_basis.reported_under
    )
    fields["ssff"] = ext.reporting_basis.ssff == gt.reporting_basis.ssff

    # ---- cause-code (excused when the oracle is provisional/TBD) ----
    gt_tbd = gt.cause.cause_code == "TBD"
    cause_excused = provisional_tol and gt_tbd
    cause_code_ok = None if cause_excused else (
        ext.cause.cause_code == gt.cause.cause_code
        and ext.cause.category == gt.cause.category
    )

    # ---- unresolved coded entities (gate: every coded System/Component resolves).
    # A coded oracle System/Component that we failed to align = a resolution miss
    # (either not extracted, or emitted with the wrong/name-slug key).
    unresolved = [
        g.match_key for g in gt.nodes
        if id(g) not in matched_gt
        and g.type in ("System", "Component")
        and getattr(g, "eiis_code", None)
    ]

    return {
        "ler_number": ext.ler_number,
        "nodes": {"tp": tp, "pred": len(ext.nodes), "true": len(gt.nodes),
                  "excused": excused_gt, "p": node_p, "r": node_r, "f1": node_f},
        "per_type": per_type,
        "edges": {"tp": etp, "pred": len(ext_tr), "true": len(gt_tr),
                  "p": edge_p, "r": edge_r, "f1": edge_f},
        "fields": fields,
        "cause_code_ok": cause_code_ok,
        "cause_excused": cause_excused,
        "unmatched_ext": [n.match_key for n in ext.nodes if id(n) not in matched_ext],
        "missed_gt": [n.match_key for n in gt.nodes
                      if id(n) not in matched_gt
                      and not (provisional_tol and n.type == "CorrectiveAction"
                               and getattr(n, "provisional", False))],
        "missed_edges": sorted(gt_tr - ext_tr),
        "spurious_edges": sorted(ext_tr - gt_tr),
        "unresolved_coded": unresolved,
    }


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _fmt_prf(d: dict) -> str:
    return f"P={d['p']:.2f} R={d['r']:.2f} F1={d['f1']:.2f}  (tp={d['tp']} pred={d['pred']} true={d['true']})"


def print_scorecard(sc: dict) -> None:
    print(f"\n=========== {sc['ler_number']} ===========")
    print(f"nodes : {_fmt_prf(sc['nodes'])}"
          + (f"  [excused {sc['nodes']['excused']} provisional]" if sc['nodes']['excused'] else ""))
    for t, d in sc["per_type"].items():
        if d["pred"] or d["true"]:
            print(f"    {t:20} P={d['p']:.2f} R={d['r']:.2f} F1={d['f1']:.2f}"
                  f"  (tp={d['tp']} pred={d['pred']} true={d['true']})")
    print(f"edges : {_fmt_prf(sc['edges'])}")

    failed = [f for f, ok in sc["fields"].items() if not ok]
    print(f"fields: {len(sc['fields']) - len(failed)}/{len(sc['fields'])} exact"
          + (f"   MISMATCH: {failed}" if failed else "  ✓"))
    cc = sc["cause_code_ok"]
    print("cause : " + ("excused (provisional)" if sc["cause_excused"]
                        else ("✓" if cc else "✗ MISMATCH")))
    if sc["missed_gt"]:
        print(f"missed nodes : {sc['missed_gt']}")
    if sc["unmatched_ext"]:
        print(f"spurious nodes: {sc['unmatched_ext']}")
    if sc["missed_edges"]:
        print(f"missed edges : {sc['missed_edges']}")
    if sc["spurious_edges"]:
        print(f"spurious edges: {sc['spurious_edges']}")
    if sc["unresolved_coded"]:
        print(f"UNRESOLVED coded: {sc['unresolved_coded']}")


def gate_check(scorecards: list[dict]) -> dict:
    """Evaluate the phase_4 pre-scale gate over a set of records."""
    id_ok = all(all(s["fields"].values()) for s in scorecards)
    cause_ok = all(s["cause_code_ok"] in (True, None) for s in scorecards)
    edge_ok = all(s["edges"]["f1"] >= 0.85 for s in scorecards)
    resolved_ok = all(not s["unresolved_coded"] for s in scorecards)
    passed = id_ok and cause_ok and edge_ok and resolved_ok
    return {"identity_100": id_ok, "cause_code_100": cause_ok,
            "edge_f1_0.85": edge_ok, "all_coded_resolved": resolved_ok,
            "PASS": passed}


def print_aggregate(scorecards: list[dict]) -> None:
    if not scorecards:
        return

    def micro(key):
        tp = sum(s[key]["tp"] for s in scorecards)
        pred = sum(s[key]["pred"] for s in scorecards)
        true = sum(s[key]["true"] for s in scorecards) - (
            sum(s["nodes"]["excused"] for s in scorecards) if key == "nodes" else 0
        )
        return _prf(tp, pred, true), tp, pred, true

    (np_, nr, nf), *_ = micro("nodes")
    (ep, er, ef), *_ = micro("edges")
    print("\n=========== AGGREGATE (micro) ===========")
    print(f"nodes : P={np_:.2f} R={nr:.2f} F1={nf:.2f}")
    print(f"edges : P={ep:.2f} R={er:.2f} F1={ef:.2f}")

    gate = gate_check(scorecards)
    print("\n--- gate (phase_4 pre-scale) ---")
    for k, v in gate.items():
        if k == "PASS":
            continue
        print(f"    {'✓' if v else '✗'} {k}")
    print(f"    ==> {'PASS ✅' if gate['PASS'] else 'NOT YET ❌'}")


# --------------------------------------------------------------------------- #
# loading / public API
# --------------------------------------------------------------------------- #
def load_oracle(path: Optional[Path] = None) -> dict[str, LERRecord]:
    path = path or ORACLE_PATH
    gt = GroundTruth.model_validate(json.loads(Path(path).read_text()))
    return {r.ler_number: r for r in gt.lers}


def score_all(ext_records: list[LERRecord], oracle: Optional[dict] = None,
              threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    oracle = oracle or load_oracle()
    cards = []
    for ext in ext_records:
        gt = oracle.get(ext.ler_number)
        if gt is None:
            print(f"[warn] no oracle for {ext.ler_number}; skipping")
            continue
        cards.append(score_record(ext, gt, threshold=threshold))
    return cards


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "out"
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    records = [LERRecord.model_validate(json.loads(p.read_text())) for p in files]
    cards = score_all(records)
    for c in cards:
        print_scorecard(c)
    print_aggregate(cards)
