"""
classify_outcomes.py — Phase 7 step 2: map every per-LER free-text `Consequence` onto ONE
controlled `outcome_class` (the aggregatable axis the risk layer stands on), via one batched
Claude pass with prompt caching on the shared rubric + few-shot.

Shape mirrors pipeline_batch.py: assemble per-consequence context from the graph -> submit-all ->
poll -> collect -> parse/validate -> stamp `outcome_class` + `classifier_confidence` +
`classifier_prompt_version` onto the Consequence nodes AND onto a re-runnable artifact
(out/outcome_classes.json). The taxonomy + severities are owned by risk.py; the versioned
instructions are prompts/outcome_classification.md.

  python -m pragraph.classify_outcomes --calibrate 20     # small sample: validate output + project cost
  python -m pragraph.classify_outcomes --run              # full ~971-consequence classification (paid)
  python -m pragraph.classify_outcomes --resume msgbatch_1 # collect an already-submitted batch
  python -m pragraph.classify_outcomes --restamp          # re-stamp graph from the artifact (no API)
  python -m pragraph.classify_outcomes --validate         # score the artifact vs the hand-labeled ref

Why classify what PHYSICALLY happened, not how it was reported: the reporting criterion is withheld
from the input on purpose so the correlation between outcome severity and the reporting rule stays
observable downstream as selection bias (see docs/journal/phase_7.md), rather than being baked in circularly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


from .llm import LLM, extract_json
from .load_graph import _connect
from .pipeline_batch import PRICES, wait_for_batch
from .risk import CONFIDENCE_GATE, OUTCOME_CLASSES, OUTCOME_KEYS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "outcome_classification.md"
# NB: NOT under out/*.json — load_graph.load_records() globs that for LER records. Keep the risk
# artifact in out/risk/ so it is out of that glob (still under the git-ignored out/ tree).
ARTIFACT_PATH = REPO_ROOT / "out" / "risk" / "outcome_classes.json"
REFERENCE_PATH = REPO_ROOT / "data" / "raw" / "reference" / "outcome_labels.json"
PROMPT_VERSION = "v1"                       # bump when prompts/outcome_classification.md changes


# --------------------------------------------------------------------------- #
# prompt assembly (SYSTEM / FEWSHOT / USER blocks; {{TAXONOMY}} from risk.py)
# --------------------------------------------------------------------------- #
def render_taxonomy() -> str:
    return "\n".join(f"  - {o.key} (severity {o.severity}): {o.meaning}" for o in OUTCOME_CLASSES)


def load_prompt(path: Path = PROMPT_PATH) -> tuple[str, str, str]:
    """(system, fewshot_user, user_template) from the versioned md, taxonomy injected."""
    md = path.read_text()

    def block(header: str) -> str:
        m = re.search(rf"##\s*{header}\s*\n```[^\n]*\n(.*?)\n```", md, re.S)
        if not m:
            raise ValueError(f"classification prompt missing a fenced {header} block")
        return m.group(1).strip()

    system = block("SYSTEM").replace("{{TAXONOMY}}", render_taxonomy())
    return system, block("FEWSHOT"), block("USER")


def format_context(ctx: dict) -> str:
    """Render the {{CONTEXT}} block for one consequence (only non-empty lines)."""
    lines = [f"LER: {ctx['ler']}" + (f" — {ctx['plant']}" if ctx.get("plant") else "")]
    if ctx.get("title"):
        lines.append(f"Title: {ctx['title']}")
    if ctx.get("systems"):
        lines.append("Systems involved: " + ", ".join(ctx["systems"]))
    if ctx.get("cause"):
        lines.append(f"Cause: {ctx['cause']}")
    if ctx.get("chain"):
        lines.append("Causal chain: " + " -> ".join(ctx["chain"]))
    lines.append(f'CONSEQUENCE: "{ctx["name"]}"')
    timing = " ".join(f"{k}={ctx[k]}" for k in ("start", "duration") if ctx.get(k))
    if timing:
        lines.append(f"Timing: {timing}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# assemble per-consequence context from the graph
# --------------------------------------------------------------------------- #
def assemble_contexts(session) -> dict[str, dict]:
    """gkey -> context dict for every Consequence node. Each consequence resolves to exactly
    one ler_number via its incident (per-LER-stamped) edges; the causal chain is best-effort
    (961/971 reachable), the rest classify on display_name + systems + cause."""
    cons = {}
    for r in session.run(
        "MATCH (cons:Consequence) "
        "OPTIONAL MATCH (cons)-[e]-() "
        "WITH cons, [x IN collect(DISTINCT e.ler_number) WHERE x IS NOT NULL] AS lers "
        "RETURN cons.gkey AS gkey, cons.display_name AS name, lers, "
        "  cons.start AS start, cons.duration AS duration"):
        lers = r["lers"]
        cons[r["gkey"]] = {"gkey": r["gkey"], "name": r["name"],
                           "ler": lers[0] if lers else None,
                           "start": r["start"], "duration": r["duration"]}

    ler_ctx = {}
    for r in session.run(
        "MATCH (l:LER) WHERE NOT l.stub "
        "OPTIONAL MATCH (l)-[:INVOLVES]->(sys:System) "
        "OPTIONAL MATCH (l)-[:HAS_CAUSE {ler_number:l.key}]->(c:Cause) "
        "RETURN l.key AS ler, l.plant_name AS plant, l.title AS title, "
        "  [x IN collect(DISTINCT coalesce(sys.eiis_code, sys.display_name)) WHERE x IS NOT NULL] AS systems, "
        "  head([x IN collect(DISTINCT c.category) WHERE x IS NOT NULL AND x <> 'provisional']) AS cause"):
        ler_ctx[r["ler"]] = {"plant": r["plant"], "title": r["title"],
                             "systems": r["systems"], "cause": r["cause"]}

    chains = {}
    for r in session.run(
        "MATCH (l:LER)-[:HAS_CAUSE {ler_number:l.key}]->(cause:Cause)"
        "<-[:CAUSED_BY {ler_number:l.key}]-(origin:FailureMode) "
        "MATCH path=(origin)-[:LEADS_TO*0.. {ler_number:l.key}]->(cons:Consequence) "
        "RETURN cons.gkey AS gkey, [n IN nodes(path) | n.display_name] AS chain"):
        g, ch = r["gkey"], r["chain"]
        if g not in chains or len(ch) > len(chains[g]):
            chains[g] = ch

    for g, c in cons.items():
        c.update(ler_ctx.get(c["ler"], {}))
        if g in chains:
            c["chain"] = chains[g]
    return cons


# --------------------------------------------------------------------------- #
# batch classify
# --------------------------------------------------------------------------- #
def _parse_result(text: str) -> dict | None:
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return None
    cls = obj.get("outcome_class")
    if cls not in OUTCOME_KEYS:
        return None
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"outcome_class": cls, "confidence": max(0.0, min(1.0, conf)),
            "reason": str(obj.get("reason", ""))[:300], "version": PROMPT_VERSION}


def classify(contexts: dict[str, dict], llm: LLM, poll_secs: int, max_wait_secs: int,
             max_rounds: int = 2, resume_id: str | None = None) -> tuple[dict, dict]:
    """Return (results gkey->classification, totals token-usage). Re-asks only the
    unparseable/invalid items a round at a time (mirrors the extraction batch)."""
    system, fewshot_user, user_tmpl = load_prompt()
    # Batch API caps custom_id at 64 chars; consequence gkeys are longer, so map each to a short
    # stable id (c{i} over sorted gkeys) and translate results back. Deterministic, so --resume
    # reconstructs the same mapping from the same context set.
    gkeys = sorted(contexts)
    cid_of = {g: f"c{i}" for i, g in enumerate(gkeys)}
    gkey_of = {c: g for g, c in cid_of.items()}
    pending = {g: user_tmpl.replace("{{CONTEXT}}", format_context(contexts[g])) for g in gkeys}
    results: dict[str, dict] = {}
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

    for rnd in range(max_rounds + 1):
        if resume_id and rnd == 0:
            batch_id = resume_id
            print(f"[classify] resuming {batch_id}")
        elif pending:
            reqs = [llm.batch_request(cid_of[g], system, fewshot_user, tail)
                    for g, tail in pending.items()]
            batch_id = llm.submit_batch(reqs)
            (ARTIFACT_PATH.parent / "last_classify_batch.txt").write_text(batch_id + "\n")
            print(f"[classify] round {rnd}: submitted {len(reqs)} -> {batch_id}")
        else:
            break

        wait_for_batch(llm, batch_id, poll_secs, max_wait_secs)
        next_pending, collected = {}, 0
        for item in llm.batch_results(batch_id):
            g, res = gkey_of.get(item.custom_id), item.result
            if g is None:                         # a stray custom_id not in this context set
                continue
            collected += 1
            if res.type != "succeeded":
                continue
            llm.log_batch_usage(g, res.message.usage)
            for k, attr in (("input", "input_tokens"), ("output", "output_tokens"),
                            ("cache_creation", "cache_creation_input_tokens"),
                            ("cache_read", "cache_read_input_tokens")):
                totals[k] += getattr(res.message.usage, attr, 0) or 0
            parsed = _parse_result(llm.batch_text(res.message))
            if parsed is None:
                next_pending[g] = (pending.get(g, "") + "\n\nYour previous reply was not valid or "
                                   "used an unknown class. Return ONLY the JSON with outcome_class "
                                   "from the taxonomy.")
                continue
            results[g] = parsed
        print(f"[classify] round {rnd}: collected {collected}, {len(results)} classified, "
              f"{len(next_pending)} to retry")
        pending = next_pending
        if resume_id:
            break
    return results, totals


# --------------------------------------------------------------------------- #
# persist: artifact + graph
# --------------------------------------------------------------------------- #
def write_artifact(results: dict[str, dict], contexts: dict[str, dict] | None = None) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    for g, r in results.items():
        rec = dict(r)
        if contexts and g in contexts:
            rec["display_name"] = contexts[g]["name"]
            rec["ler"] = contexts[g]["ler"]
        payload[g] = rec
    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"[classify] wrote {len(payload)} classifications -> {ARTIFACT_PATH}")


def stamp_graph(session, results: dict[str, dict]) -> int:
    rows = [{"gkey": g, "outcome_class": r["outcome_class"],
             "confidence": r["confidence"], "version": r.get("version", PROMPT_VERSION)}
            for g, r in results.items()]
    out = session.run(
        "UNWIND $rows AS row MATCH (c:Consequence {gkey: row.gkey}) "
        "SET c.outcome_class = row.outcome_class, c.classifier_confidence = row.confidence, "
        "    c.classifier_prompt_version = row.version RETURN count(c) AS n", rows=rows).single()
    print(f"[classify] stamped outcome_class on {out['n']}/{len(rows)} Consequence nodes")
    return out["n"]


def restamp_from_artifact() -> None:
    if not ARTIFACT_PATH.exists():
        raise SystemExit(f"no artifact at {ARTIFACT_PATH}; run --run first")
    results = json.loads(ARTIFACT_PATH.read_text())
    d = _connect(); d.verify_connectivity()
    with d.session() as s:
        stamp_graph(s, results)
    d.close()


# --------------------------------------------------------------------------- #
# cost projection
# --------------------------------------------------------------------------- #
def project_cost(totals: dict, n_done: int, n_target: int) -> None:
    def run_cost(ir, orr):
        c = (totals["input"] * ir + totals["cache_creation"] * ir * 1.25
             + totals["cache_read"] * ir * 0.10 + totals["output"] * orr) / 1e6
        return c * 0.5
    tin = totals["input"] + totals["cache_creation"] + totals["cache_read"]
    print("\n================= classification cost (measured, batch) =================")
    print(f"  classified this run: {n_done}")
    print(f"  input tokens : {tin:,} (uncached {totals['input']:,}, "
          f"cache-write {totals['cache_creation']:,}, cache-read {totals['cache_read']:,})")
    print(f"  output tokens: {totals['output']:,}")
    if not n_done:
        return
    for name, (ir, orr) in PRICES.items():
        here = run_cost(ir, orr)
        full = here / n_done * n_target
        print(f"  @ {name:8} pricing: ${here:6.4f} for these {n_done}  ->  ~${full:6.3f} for {n_target}")


# --------------------------------------------------------------------------- #
# validation vs the hand-labeled reference
# --------------------------------------------------------------------------- #
def validate(results: dict[str, dict] | None = None) -> dict:
    """Score the classifier against the hand-labeled reference set: overall accuracy,
    per-class confusion, the load-bearing 5-vs-4 boundary, and how many reference items
    the confidence gate would drop. Uses the artifact if `results` not passed."""
    if results is None:
        if not ARTIFACT_PATH.exists():
            raise SystemExit(f"no artifact at {ARTIFACT_PATH}; run --run first")
        results = json.loads(ARTIFACT_PATH.read_text())
    if not REFERENCE_PATH.exists():
        raise SystemExit(f"no reference labels at {REFERENCE_PATH}")
    ref = json.loads(REFERENCE_PATH.read_text())
    ref_items = ref["labels"] if isinstance(ref, dict) else ref

    n = correct = gated = 0
    missing = []
    confusion: dict[tuple[str, str], int] = {}
    hard = {"n": 0, "correct": 0}                 # the 5-vs-4 boundary among reference items
    for item in ref_items:
        g, gold = item["gkey"], item["label"]
        pred = results.get(g)
        if pred is None:
            missing.append(g)
            continue
        n += 1
        p = pred["outcome_class"]
        confusion[(gold, p)] = confusion.get((gold, p), 0) + 1
        if pred["confidence"] < CONFIDENCE_GATE:
            gated += 1
        if p == gold:
            correct += 1
        if gold in ("loss-of-safety-function", "safety-system-inoperable"):
            hard["n"] += 1
            hard["correct"] += int(p == gold)

    acc = correct / n if n else 0.0
    print("\n================= classifier validation (vs hand-labeled reference) =================")
    print(f"  reference items scored : {n}" + (f"  ({len(missing)} not yet classified)" if missing else ""))
    print(f"  overall accuracy       : {acc:.0%}  ({correct}/{n})")
    if hard["n"]:
        print(f"  5-vs-4 boundary accuracy: {hard['correct']/hard['n']:.0%}  "
              f"({hard['correct']}/{hard['n']}) — the single-train-vs-both distinction")
    print(f"  would-be gated (<{CONFIDENCE_GATE} conf): {gated}/{n}")
    print("  confusion (gold -> predicted, mismatches only):")
    for (gold, pred), c in sorted(confusion.items(), key=lambda kv: -kv[1]):
        if gold != pred:
            print(f"    {c:3}  {gold}  ->  {pred}")
    return {"n": n, "accuracy": acc, "correct": correct, "gated": gated,
            "hard": hard, "confusion": confusion, "missing": missing}


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(calibrate: int | None = None, resume_id: str | None = None, stamp: bool = True,
        poll_secs: int = 15, max_wait_secs: int = 1800, model: str = "claude-sonnet-5",
        n_target: int | None = None) -> dict:
    llm = LLM(model=model)
    d = _connect(); d.verify_connectivity()
    with d.session() as s:
        contexts = assemble_contexts(s)
    print(f"[classify] assembled context for {len(contexts)} consequences")

    if calibrate:
        keys = sorted(contexts)[:calibrate]
        contexts = {g: contexts[g] for g in keys}
        print(f"[classify] CALIBRATION: first {len(contexts)} consequences (paid, tiny)")

    results, totals = classify(contexts, llm, poll_secs, max_wait_secs, resume_id=resume_id)
    write_artifact(results, contexts)
    if stamp and not calibrate:
        with d.session() as s:
            stamp_graph(s, results)
    elif calibrate:
        print("[classify] calibration: NOT stamping the graph (spot-check the artifact first)")
    d.close()

    project_cost(totals, len(results), n_target or 971)
    # show a few calibration outputs for a human spot-check
    if calibrate:
        print("\n  sample classifications (spot-check the rubric before the full run):")
        for g in list(results)[:12]:
            c = contexts[g]
            print(f'    [{results[g]["outcome_class"]:26} @ {results[g]["confidence"]:.2f}] '
                  f'"{c["name"][:60]}"')
    return {"results": results, "totals": totals, "contexts": contexts}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Phase-7 outcome-class classification (Anthropic Batches).")
    p.add_argument("--calibrate", type=int, help="classify only the first N (spot-check + cost)")
    p.add_argument("--run", action="store_true", help="classify all consequences (paid full run)")
    p.add_argument("--resume", help="collect an already-submitted batch id")
    p.add_argument("--restamp", action="store_true", help="re-stamp graph from the artifact (no API)")
    p.add_argument("--validate", action="store_true", help="score the artifact vs the reference set")
    p.add_argument("--no-stamp", action="store_true", help="don't write outcome_class to the graph")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--poll", type=int, default=15)
    p.add_argument("--max-wait", type=int, default=1800)
    args = p.parse_args(argv)

    if args.restamp:
        restamp_from_artifact(); return 0
    if args.validate and not (args.run or args.calibrate or args.resume):
        validate(); return 0
    if not (args.run or args.calibrate or args.resume):
        p.error("provide --calibrate N, --run, --resume, --restamp, or --validate")

    out = run(calibrate=args.calibrate, resume_id=args.resume, stamp=not args.no_stamp,
              poll_secs=args.poll, max_wait_secs=args.max_wait, model=args.model)
    if args.validate:
        validate(out["results"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
