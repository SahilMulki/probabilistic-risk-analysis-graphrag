"""
load_graph.py — Phase 5: load the resolved LER records into Neo4j.

Entity resolution already happened in Phase 4 — every node carries a canonical
`match_key`. Phase 5 therefore reduces to two ideas:

  1. MERGE on a *graph key*, never on the record-local `id`. The `id` fields
     ("ler", "unit", "cause", "sys_bj") repeat across reports; the graph key is
     global identity, so `System:BJ` from three LERs becomes ONE node and the
     reports fuse into a connected graph.

  2. The graph key deviates from the stored `match_key` for event-specific node
     types. FailureMode / Consequence / CorrectiveAction, and provisional (TBD)
     Cause nodes, are suffixed with the LER number so they never merge across
     reports (two "HPCI inoperable" consequences with different timing must not
     collapse into one node). Only the canonical coded types — System, Component,
     non-provisional Cause (by category), Unit, Manufacturer, RegulatoryReference,
     LER (by number) — act as cross-document hubs.

Source of each record (the "cleanest path" for Quad Cities):
  * Quad Cities 254-2025-006-00 is the few-shot exemplar with no raw text, so it
    can't be re-extracted; it is loaded straight from its hand-verified oracle
    record in ground_truth.json (source="oracle").
  * Dresden and Limerick are loaded from out/ (source="pipeline").

Connectivity normalization (added at load, not in the frozen extraction):
  schema v4.1 extracts an LER as a bag of facets — the LER/Unit/affected-System
  spine and the Cause -> FailureMode chain -> Consequence -> backups spine come
  out as SEPARATE connected components (true even in the hand-built oracle). This
  loader is where the report is wired into one graph:
    * LER -[:HAS_CAUSE]-> Cause  (structural bridge; joins the two spines)
    * LER -[:INVOLVES]-> Component  (synthesized) for any component the extraction
      left dangling (component-failure-data rows with no other edge)
  Both are tagged (structural / synthesized) and stamped with ler_number so they
  are distinguishable from extracted edges. See docs/journal/phase_5.md.

Per-LER attributes that vary by report are carried on the *relationship*, not on
the de-duplicated hub node: role (System endpoints), theme + proximate_text
(HAS_CAUSE), evidence, and ler_number provenance on every edge.

Usage:
    python src/load_graph.py --dry-run     # validate + report, no database needed
    python src/load_graph.py               # MERGE-load into Neo4j (idempotent)
    python src/load_graph.py --wipe --yes  # reset the database first, then load
    python src/load_graph.py --verify      # run the gate queries against the DB

Neo4j connection is read from the git-ignored .env:
    NEO4J_URI=bolt://localhost:7687
    NEO4J_USER=neo4j
    NEO4J_PASSWORD=...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm import load_env
from models import GroundTruth, LERRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE_PATH = REPO_ROOT / "data" / "raw" / "ground_truth" / "ground_truth.json"
OUT_DIR = REPO_ROOT / "out"

# The Quad Cities exemplar has no raw text, so it is always loaded from the frozen
# oracle; every other record comes from out/ (the pipeline). At scale out/ holds the
# whole extracted corpus, so we GLOB it rather than list docs by hand (Phase 8).
ORACLE_LERS = {"254-2025-006-00"}     # Quad Cities — few-shot exemplar, source="oracle"

# Node types that are keyed per-LER (never cross-document hubs).
PER_LER_TYPES = {"FailureMode", "Consequence", "CorrectiveAction"}

# Legal Neo4j labels / relationship types (validated before string-injecting into Cypher).
NODE_LABELS = {
    "LER", "Unit", "System", "Component", "FailureMode", "Cause",
    "Consequence", "CorrectiveAction", "Manufacturer", "RegulatoryReference",
}
STRUCTURAL_REL = "HAS_CAUSE"                       # load-time bridge, not in schema v4.1
EXTRACTION_RELS = {
    "OCCURRED_AT", "INVOLVES", "LEADS_TO", "CAUSED_BY", "MITIGATED_BY",
    "BACKED_UP_BY", "REPORTED_UNDER", "MANUFACTURED_BY", "PART_OF",
    "SIMILAR_TO", "REVISES",
}
REL_TYPES = EXTRACTION_RELS | {STRUCTURAL_REL}

# Types allowed to remain unconnected (peripheral citations, referenced-but-external).
ORPHAN_ALLOWED = {"RegulatoryReference"}

CONSTRAINT_CYPHER = (
    "CREATE CONSTRAINT entity_gkey IF NOT EXISTS "
    "FOR (n:Entity) REQUIRE n.gkey IS UNIQUE"
)


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #
def graph_key(node, ler_number: str) -> str:
    """Neo4j merge key. Suffix event-specific types (and provisional causes) with
    the LER number so they stay per-report; hubs keep their stored match_key."""
    per_ler = node.type in PER_LER_TYPES or (
        node.type == "Cause" and getattr(node, "provisional", False)
    )
    return f"{node.match_key}:{ler_number}" if per_ler else node.match_key


# --------------------------------------------------------------------------- #
# node -> Neo4j properties
# --------------------------------------------------------------------------- #
def node_props(node, rec: LERRecord, src: str) -> dict:
    """Scalar/array property bag for a node. Per-LER attributes that vary across
    reports (role, theme, proximate_text) are deliberately NOT put here — they go
    on relationships. Nested free-form `properties` is JSON-stringified."""
    p: dict = {
        "match_key": node.match_key,
        "type": node.type,
        "display_name": node.display_name,
    }
    t = node.type

    if t == "LER":
        p["key"] = node.key
        p["stub"] = node.stub
        p["source"] = src
        if node.title:
            p["title"] = node.title
        if node.plant:
            p["plant"] = node.plant
        if not node.stub:                          # hoist the rich Form-366 header
            idn = rec.identity
            for f in ("event_date", "report_date", "operating_mode", "power_level",
                      "discovery_context", "status", "revision", "ens_number",
                      "ens_date", "ens_time", "title", "plant_name",
                      "accession_number"):
                v = getattr(idn, f, None)
                if v is not None:
                    p.setdefault(f, v)
            p["reported_under"] = rec.reporting_basis.reported_under
            p["ssff"] = rec.reporting_basis.ssff
            p["block_13_json"] = json.dumps([b.model_dump() for b in rec.block_13])
            if rec.chain:
                p["chain"] = rec.chain
            if rec.notes:
                p["notes"] = rec.notes
            if rec.golden_questions:
                p["golden_questions"] = rec.golden_questions

    elif t == "Unit":                              # authoritative fields from identity
        idn = rec.identity
        p["key"] = node.key
        if idn.plant_name:
            p["plant_name"] = idn.plant_name
        if idn.unit is not None:
            p["unit"] = idn.unit
        if idn.reactor_type:
            p["reactor_type"] = idn.reactor_type
        if idn.nss_vendor:
            p["nss_vendor"] = idn.nss_vendor

    elif t == "System":
        if node.eiis_code:
            p["eiis_code"] = node.eiis_code
        p["non_eiis"] = node.non_eiis
        if node.provisional:
            p["provisional"] = True

    elif t == "Component":
        for f in ("eiis_code", "identifier", "model", "manufacturer_code"):
            v = getattr(node, f, None)
            if v is not None:
                p[f] = v
        p["inferred_code"] = node.inferred_code

    elif t == "Cause":
        p["cause_code"] = node.cause_code
        p["category"] = node.category
        p["provisional"] = node.provisional

    elif t == "FailureMode":
        if node.description:
            p["description"] = node.description

    elif t == "Consequence":
        for f in ("start", "end", "duration", "tz"):
            v = getattr(node, f, None)
            if v is not None:
                p[f] = v

    elif t == "CorrectiveAction":
        p["status"] = node.status
        if node.provisional:
            p["provisional"] = True

    elif t == "Manufacturer":
        if node.code:
            p["code"] = node.code

    elif t == "RegulatoryReference":
        if node.ref_type:
            p["ref_type"] = node.ref_type

    if node.properties:
        p["properties_json"] = json.dumps(node.properties)
    return p


# --------------------------------------------------------------------------- #
# record -> local edges (extracted + structural bridge + synthesized)
# --------------------------------------------------------------------------- #
def record_edges(rec: LERRecord) -> tuple[str, list[tuple[str, str, str, dict]]]:
    """Return (primary_ler_id, edges) where each edge is (src_id, rel, tgt_id, props)
    over the record-local ids, including the connectivity-normalizing edges."""
    id2node = {n.id: n for n in rec.nodes}
    primary = next(n.id for n in rec.nodes if n.type == "LER" and not n.stub)
    edges: list[tuple[str, str, str, dict]] = []

    for e in rec.edges:
        props: dict = {"ler_number": rec.ler_number}
        if e.evidence:
            props["evidence"] = e.evidence
        # role is a per-LER attribute of a System endpoint -> carry it on the edge
        for endpoint in (e.source, e.target):
            n = id2node.get(endpoint)
            if n is not None and n.type == "System" and getattr(n, "role", None):
                props["role"] = n.role
        edges.append((e.source, e.relation, e.target, props))

    # structural bridge: LER -> HAS_CAUSE -> Cause, carrying the LER-level cause text
    for n in rec.nodes:
        if n.type == "Cause":
            props = {"ler_number": rec.ler_number, "structural": True}
            if rec.cause.theme:
                props["theme"] = rec.cause.theme
            if rec.cause.proximate_text:
                props["proximate_text"] = rec.cause.proximate_text
            edges.append((primary, STRUCTURAL_REL, n.id, props))

    # synthesized INVOLVES for any Component not otherwise reachable from the LER
    reached = _reachable(primary, edges)
    for n in rec.nodes:
        if n.type == "Component" and n.id not in reached:
            edges.append((primary, "INVOLVES", n.id,
                          {"ler_number": rec.ler_number, "synthesized": True}))
    return primary, edges


def _reachable(start: str, edges: list[tuple[str, str, str, dict]]) -> set[str]:
    adj: dict[str, set[str]] = defaultdict(set)
    for s, _, t, _ in edges:
        adj[s].add(t)
        adj[t].add(s)
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(adj[x] - seen)
    return seen


# --------------------------------------------------------------------------- #
# assemble the global graph
# --------------------------------------------------------------------------- #
def load_records() -> list[tuple[LERRecord, str]]:
    """The QC oracle record + every out/*.json pipeline record. Malformed out/
    files are skipped with a warning so one bad extraction can't sink the load."""
    gt = GroundTruth.model_validate(json.loads(ORACLE_PATH.read_text()))
    oracle = {r.ler_number: r for r in gt.lers}
    out: list[tuple[LERRecord, str]] = []
    for ler in sorted(ORACLE_LERS):
        out.append((oracle[ler], "oracle"))
    for path in sorted(OUT_DIR.glob("*.json")):
        try:
            rec = LERRecord.model_validate(json.loads(path.read_text()))
        except Exception as e:
            print(f"[warn] skipping {path.name}: {type(e).__name__}: {e}")
            continue
        if rec.ler_number in ORACLE_LERS:      # never let a stray out/ dup shadow the oracle
            continue
        out.append((rec, "pipeline"))
    return out


def build_graph(records: list[tuple[LERRecord, str]]):
    """Return (nodes, edges, per_record). nodes: gkey -> (label, props).
    edges: list of (src_gkey, rel, tgt_gkey, props). per_record: diagnostics."""
    nodes: dict[str, tuple[str, dict]] = {}
    edges: list[tuple[str, str, str, dict]] = []
    per_record = []

    for rec, src in records:
        local2g: dict[str, str] = {}
        for n in rec.nodes:
            gkey = graph_key(n, rec.ler_number)
            local2g[n.id] = gkey
            props = node_props(n, rec, src)
            props["gkey"] = gkey
            # last writer wins on hub display fields — EXCEPT never demote a real (non-stub) LER
            # record to a referenced stub. An LER that another report cites as a previous
            # occurrence is emitted as a stub node (stub=true, no event_date); if that write lands
            # after the real record's, it would clobber stub=false and hide a real event from
            # every `NOT l.stub` query. Keep the real record (which also carries the richer props).
            prev = nodes.get(gkey)
            if (prev is not None and n.type == "LER"
                    and prev[1].get("stub") is False and props.get("stub")):
                continue
            nodes[gkey] = (n.type, props)

        primary, ledges = record_edges(rec)
        struct = sum(1 for _, r, _, p in ledges if r == STRUCTURAL_REL)
        synth = sum(1 for *_, p in ledges if p.get("synthesized"))
        for s, r, t, p in ledges:
            edges.append((local2g[s], r, local2g[t], p))

        per_record.append({
            "ler": rec.ler_number, "source": src,
            "nodes": len(rec.nodes), "edges": len(ledges),
            "structural": struct, "synthesized": synth,
            "local_edges": ledges, "primary": primary,
            "id2type": {n.id: n.type for n in rec.nodes},
            "stub_ids": {n.id for n in rec.nodes if n.type == "LER" and n.stub},
        })
    return nodes, edges, per_record


# --------------------------------------------------------------------------- #
# connectivity report (dry-run)
# --------------------------------------------------------------------------- #
def _components(node_ids, pair_edges):
    adj = defaultdict(set)
    for s, t in pair_edges:
        adj[s].add(t)
        adj[t].add(s)
    seen, comps = set(), []
    for i in node_ids:
        if i in seen:
            continue
        stack, comp = [i], set()
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.add(x)
            stack.extend(adj[x] - seen)
        comps.append(comp)
    return comps


def connectivity_report(nodes, edges, per_record) -> bool:
    print("\n================= per-report connectivity =================")
    ok = True
    for pr in per_record:
        ids = list(pr["id2type"])
        pairs = [(s, t) for s, _, t, _ in pr["local_edges"]]
        # scope: exclude stub LERs and allowed-orphan types from the spine check
        keep = {i for i in ids
                if i not in pr["stub_ids"] and pr["id2type"][i] not in ORPHAN_ALLOWED}
        comps = _components(list(keep), [(s, t) for s, t in pairs if s in keep and t in keep])
        spine = max(comps, key=len) if comps else set()
        strays = [c for c in comps if c is not spine]
        status = "OK" if len(strays) == 0 else "SPLIT"
        if strays:
            ok = False
        print(f"  {pr['ler']} [{pr['source']}]: {len(keep)} spine nodes, "
              f"{len(comps)} component(s) [{status}]  "
              f"(+{pr['structural']} structural, +{pr['synthesized']} synthesized edge)")
        for c in strays:
            print(f"      stray: {sorted(f'{i}[{pr['id2type'][i]}]' for i in c)}")
        allowed = [i for i in ids
                   if pr["id2type"][i] in ORPHAN_ALLOWED
                   and i not in {x for s, t in pairs for x in (s, t)}]
        if allowed:
            print(f"      peripheral citations (allowed): "
                  f"{sorted(f'{i}[{pr['id2type'][i]}]' for i in allowed)}")

    # corpus-level
    print("\n================= corpus connectivity =================")
    core = {g for g, (lbl, _) in nodes.items() if lbl not in ORPHAN_ALLOWED}
    pairs = [(s, t) for s, _, t, _ in edges if s in core and t in core]
    comps = _components(list(core), pairs)
    comps.sort(key=len, reverse=True)
    print(f"  {len(nodes)} nodes, {len(edges)} edges")
    print(f"  core (non-citation) nodes: {len(core)} in {len(comps)} component(s); "
          f"largest = {len(comps[0]) if comps else 0}")
    if len(comps) > 1:
        for c in comps[1:]:
            print(f"      extra component: {sorted(c)}")
        ok = False
    citations = [g for g, (lbl, _) in nodes.items() if lbl in ORPHAN_ALLOWED]
    standalone = [g for g in citations
                  if g not in {x for s, _, t, _ in edges for x in (s, t)}]
    if standalone:
        print(f"  standalone citation nodes (allowed): {sorted(standalone)}")

    # cross-document hubs
    print("\n================= cross-document hubs =================")
    owners = defaultdict(set)
    for s, _, t, p in edges:
        for g in (s, t):
            owners[g].add(p["ler_number"])
    shared = {g: o for g, o in owners.items() if len(o) > 1}
    for g in sorted(shared, key=lambda k: (-len(shared[k]), k)):
        lbl = nodes[g][0]
        print(f"  {g:52} [{lbl}]  <- {sorted(shared[g])}")
    return ok


def print_stats(nodes, edges) -> None:
    by_label = defaultdict(int)
    for _, (lbl, _) in nodes.items():
        by_label[lbl] += 1
    by_rel = defaultdict(int)
    for _, r, _, _ in edges:
        by_rel[r] += 1
    print("\n================= graph stats =================")
    print("  nodes by label:", dict(sorted(by_label.items())))
    print("  edges by type: ", dict(sorted(by_rel.items())))


# --------------------------------------------------------------------------- #
# Neo4j write / verify
# --------------------------------------------------------------------------- #
def _connect():
    load_env()
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not pw:
        raise SystemExit("NEO4J_PASSWORD not set — add NEO4J_* to .env (see load_graph.py header).")
    from neo4j import GraphDatabase
    return GraphDatabase.driver(uri, auth=(user, pw))


def write_graph(nodes, edges, wipe: bool = False) -> None:
    driver = _connect()
    try:
        driver.verify_connectivity()
        with driver.session() as s:
            if wipe:
                print("[wipe] DETACH DELETE all nodes ...")
                s.run("MATCH (n) DETACH DELETE n")
            s.run(CONSTRAINT_CYPHER)
            for gkey, (label, props) in nodes.items():
                if label not in NODE_LABELS:
                    raise ValueError(f"illegal label {label!r}")
                s.run(f"MERGE (n:Entity {{gkey:$gkey}}) SET n:`{label}`, n += $props",
                      gkey=gkey, props=props)
            for src, rel, tgt, props in edges:
                if rel not in REL_TYPES:
                    raise ValueError(f"illegal relationship {rel!r}")
                s.run(
                    f"MATCH (a:Entity {{gkey:$s}}), (b:Entity {{gkey:$t}}) "
                    f"MERGE (a)-[r:`{rel}` {{ler_number:$ler}}]->(b) SET r += $props",
                    s=src, t=tgt, ler=props["ler_number"], props=props,
                )
        print(f"[ok] loaded {len(nodes)} nodes, {len(edges)} edges into Neo4j")
    finally:
        driver.close()


VERIFY_QUERIES = [
    ("node counts by label",
     "MATCH (n:Entity) UNWIND labels(n) AS l WITH l WHERE l <> 'Entity' "
     "RETURN l AS label, count(*) AS n ORDER BY n DESC"),
    ("GATE cross-doc hub — every LER on the HPCI (BJ) system",
     "MATCH (l:LER)-[:INVOLVES]->(s:System {eiis_code:'BJ'}) WHERE NOT l.stub "
     "RETURN l.key AS ler, l.plant_name AS plant ORDER BY ler"),
    ("GATE cross-doc hub — LERs sharing the ADS backup",
     "MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->"
     "(:Consequence)-[:BACKED_UP_BY]->(s:System {match_key:'System:automatic-depressurization-system'}) "
     "WHERE NOT l.stub RETURN DISTINCT l.key AS ler ORDER BY ler"),
    ("GATE cross-doc hub — LERs under 10 CFR 50.73(a)(2)(v)(D)",
     "MATCH (l:LER)-[:REPORTED_UNDER]->(r:RegulatoryReference {match_key:'RegulatoryReference:10-cfr-50-73-a-2-v-d'}) "
     "WHERE NOT l.stub RETURN l.key AS ler ORDER BY ler"),
    ("GATE cross-doc hub — LERs sharing the RCIC (BN) backup",
     "MATCH (l:LER)-[:HAS_CAUSE]->(:Cause)<-[:CAUSED_BY]-()-[:LEADS_TO*0..]->"
     "(:Consequence)-[:BACKED_UP_BY]->(s:System {eiis_code:'BN'}) "
     "WHERE NOT l.stub RETURN DISTINCT l.key AS ler ORDER BY ler"),
    ("GATE within-report multi-hop — Limerick cannon-plug -> ... -> HPCI inoperable",
     "MATCH p=(f0:FailureMode)-[:LEADS_TO*]->(c:Consequence) "
     "WHERE f0.match_key STARTS WITH 'FailureMode:cannon-plug' "
     "RETURN [n IN nodes(p) | n.display_name] AS chain, length(p) AS hops"),
    ("orphans (should be only RegulatoryReference citations)",
     "MATCH (n:Entity) WHERE NOT (n)--() AND NOT n:RegulatoryReference "
     "AND NOT n.stub RETURN labels(n) AS labels, n.display_name AS name"),
]


def verify() -> None:
    driver = _connect()
    try:
        driver.verify_connectivity()
        with driver.session() as s:
            for title, q in VERIFY_QUERIES:
                print(f"\n--- {title} ---")
                rows = list(s.run(q))
                if not rows:
                    print("   (no rows)")
                for rec in rows:
                    print("  ", dict(rec))
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-5 Neo4j graph loader.")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate + report connectivity in memory; no database")
    ap.add_argument("--wipe", action="store_true",
                    help="DETACH DELETE the whole database before loading (needs --yes)")
    ap.add_argument("--yes", action="store_true", help="confirm a destructive --wipe")
    ap.add_argument("--verify", action="store_true",
                    help="run the gate queries against the loaded database")
    args = ap.parse_args(argv)

    if args.verify and not (args.dry_run or args.wipe):
        verify()
        return 0

    records = load_records()
    nodes, edges, per_record = build_graph(records)
    print_stats(nodes, edges)
    connected = connectivity_report(nodes, edges, per_record)

    if args.dry_run:
        print(f"\n[dry-run] graph is {'CONNECTED' if connected else 'NOT connected'}; "
              f"nothing written.")
        return 0 if connected else 1

    if args.wipe and not args.yes:
        raise SystemExit("--wipe is destructive; re-run with --wipe --yes to confirm.")

    write_graph(nodes, edges, wipe=args.wipe)
    if args.verify:
        verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
