# Phase 5 — Resolve entities & build the Neo4j graph

**Goal (plan.md):** merge duplicates and load a clean, connected, EIIS-keyed graph
into Neo4j; be able to hand-trace golden-question paths in Cypher, including a
cross-document link.

**Status:** graph builder complete and verified in memory (`--dry-run`). Loading
into a live Neo4j instance is the one step that needs the database running.

Artifacts: [src/load_graph.py](src/load_graph.py), [graph/queries.cypher](graph/queries.cypher),
`neo4j==5.28.4` pinned in [requirements.txt](requirements.txt).

---

## Entity resolution was already done (Phase 4)

Every extracted node carries a canonical `match_key` (`System:BJ`,
`Component:V|1-2301-3`, `Cause:Design/Manufacturing/Installation`, …). So Phase 5's
"entity resolution" collapses to a single loader rule: **MERGE on a graph key, never
on the record-local `id`.** The `id` fields (`ler`, `unit`, `cause`, `sys_bj`) repeat
across reports; the graph key is global identity, so one `System:BJ` node is shared
by all three LERs and the reports fuse into one graph.

## The graph key deviates from `match_key` for event-specific nodes

`match_key` is the Phase-4 *scoring* key. For the *graph* we need some node types to
stay per-report so distinct events don't collapse:

| Node types | Graph key | Why |
|---|---|---|
| System, Component, Cause **(coded, non-provisional)**, Unit, Manufacturer, RegulatoryReference, LER | `match_key` (unchanged) | canonical → these are the **cross-document hubs** |
| FailureMode, Consequence, CorrectiveAction, **provisional Cause** | `match_key + ":" + ler_number` | event-specific → never merge across reports |

This deviation is deliberate. Concretely it fixes a **timing collision**: Dresden and
Quad Cities both produce `Consequence:hpci-inoperable-unable-to-inject`, but with
different `start`/`end`/`duration`. Keying consequences per-LER keeps them as two
nodes with their own timing instead of one node whose timing is overwritten. Same
logic protects failure modes, corrective actions, and provisional (TBD) causes — a
TBD cause is *not yet* a shared category, so merging all TBDs into one node would be
meaningless. `graph_key()` in `load_graph.py` is the single source of this rule.

## Shared hubs vs. per-report attributes

Because hubs are de-duplicated, any attribute that varies by report cannot live on
the hub node — it goes on the **relationship**:

- `role` ("affected system" / "backup; operable") → on the `INVOLVES` / `BACKED_UP_BY` edge
- `theme`, `proximate_text` (the LER's own cause wording) → on the `HAS_CAUSE` edge
- `evidence` → on the edge
- **`ler_number` on every edge** — provenance. Since hubs are shared, edges are how a
  single report is reconstructed and how corpus-wide vs. single-report questions are
  answered. (Chosen model: edge-stamped provenance, no separate per-report subgraph.)

The rich Form-366 header is hoisted onto the `:LER` node (event_date, power_level,
operating_mode, status, ens_*, title, plant_name, reported_under, ssff, chain,
golden_questions) and `:Unit` gets docket/plant/unit/**reactor_type**/**nss_vendor**
from the authoritative `identity` block. `block_13` is kept as a `block_13_json`
provenance string. Each `:LER` also records `source: "oracle" | "pipeline"`.

## Connectivity normalization (added at load — this is what Phase 5 is *for*)

A finding that only surfaced once the graph was assembled: schema v4.1 extracts an
LER as a **bag of facets**, and those facets come out as *separate* connected
components — the LER / Unit / affected-System / CorrectiveAction spine on one side,
and the `Cause → FailureMode chain → Consequence → backups` spine on the other, with
no edge between them. **This is true even in the hand-built Quad Cities oracle**
(8 components) — so it is a property of the schema, not an extraction error. There is
simply no schema edge tying the failure chain back to the event.

Left as-is, the corpus would load as two large disconnected blobs and no golden
question could be traversed. Phase 5 is exactly where the report is wired into one
graph, using edges derived from unambiguous, already-present facts:

1. **`(:LER)-[:HAS_CAUSE]->(:Cause)`** — a structural bridge (one per report). Because
   the Cause is the articulation point of the failure spine (`Cause ← CAUSED_BY ←
   FailureMode → … → Consequence → backups`), this single edge joins the two spines.
   `HAS_CAUSE` is a **load-time relationship, not part of schema v4.1**; it is tagged
   `structural: true`.
2. **`(:LER)-[:INVOLVES]->(:Component)`** (tagged `synthesized: true`) for any
   Component the extraction emitted but left dangling — the component-failure-data
   rows (breaker, coil, roller, fuse, indicating light, pumps, test unit, cannon
   plug) that have no other edge. INVOLVES is the schema's Event→Component relation,
   so this is a normalization, not an invention. Only components not otherwise
   reachable from the LER are wired (e.g. Limerick's PCIV, already `PART_OF` HPCI, is
   left alone).

Both are stamped with `ler_number` and are distinguishable from extracted edges. A
future extraction iteration could emit these directly (e.g. tie each FailureMode to
its Component); doing it at load keeps the scored Phase-4 artifacts (`out/*.json`,
the frozen oracle) untouched.

**Orphan policy:** after normalization the only unconnected nodes are peripheral
`RegulatoryReference` citations (Quad Cities' *UFSAR Ch 15* and *NEI 99-02*, cited as
analysis basis rather than reporting criteria). These are allowed to stand alone —
forcing a `REPORTED_UNDER` edge would mislabel them — and their `ref_type` is
preserved. The connectivity check excludes `RegulatoryReference` and stub LERs.

## Quad Cities — the clean path

QC (`254-2025-006-00`) is the few-shot exemplar and has **no raw text**, so it cannot
be re-extracted and must not be scored (leaky). It is loaded straight from its
hand-verified **oracle** record; Dresden and Limerick load from **`out/`**. All three
are the identical `LERRecord` shape, so one uniform loader handles them — the only
difference is the `source` tag. QC is in the graph (so cross-document HPCI/ADS/RCIC
joins are real) without ever entering the eval set.

## Verification (dry-run, no database)

`python src/load_graph.py --dry-run` builds and checks the whole graph in memory:

- **57 nodes, 60 edges.** Per report: **1 connected component each** (+1 structural,
  +2–4 synthesized edges). Corpus: **53 core nodes in 1 component**, plus the 2
  allowed QC citations.
- **Cross-document hubs actually formed:**

  | Hub | Links |
  |---|---|
  | `System:BJ` (HPCI) | all three LERs |
  | `System:automatic-depressurization-system` (ADS backup) | all three |
  | `RegulatoryReference:10-cfr-50-73-a-2-v-d` | all three |
  | `System:BN` (RCIC backup) | Quad Cities + Limerick |

## Honest caveat — the shared-*cause* link is at scale, not yet

`plan.md`'s gate wants a cross-document link "through a shared **cause**." In this
3-doc corpus the three cause categories are all distinct (A / B / TBD), so that
specific link has no instance yet. The mechanism is built and correct — non-provisional
causes are keyed on category, so the moment Phase 8 adds a second event with the same
category they will share a `:Cause` hub (see query 4c in `queries.cypher`). Today the
demonstrable cross-document links are the shared **System** (HPCI), shared **backup**
(ADS / RCIC), and shared **reporting criterion** — an equally strong showcase.

## Meets the Phase-5 gate

- Connected ✓ (1 component per report; 1 corpus core component)
- EIIS-keyed ✓ (coded System/Component nodes are the hubs)
- Hand-traceable golden-question paths in Cypher ✓ (queries 2–4)
- A path linking two reports ✓ (HPCI / ADS / RCIC / reporting-criterion hubs;
  shared-cause pending scale)

## How to run

1. Start Neo4j (Neo4j Desktop, local Community Edition; Bolt on `localhost:7687`).
2. Add to `.env` (git-ignored):
   ```
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USER=neo4j
   NEO4J_PASSWORD=your-db-password
   ```
3. `pip install -r requirements.txt`
4. `python src/load_graph.py --dry-run`   # optional in-memory check
5. `python src/load_graph.py`             # MERGE-load (idempotent); `--wipe --yes` to reset
6. `python src/load_graph.py --verify`    # run the gate queries, or paste graph/queries.cypher into Browser
