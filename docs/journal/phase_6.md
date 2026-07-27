# Phase 6 — Graph retrieval + grounded answering

**Goal (plan.md):** answer the golden questions from the graph, and prove the graph
earns its keep on multi-hop / cross-document questions. **The plain-vector baseline
is deliberately deferred to Phase 8** — at N=3 there is no retrieval pressure (top-k
grabs the whole corpus), so a comparison now would be non-robust and understate the
thesis. Phase 6 is graph-only behind a `Retriever` seam so the baseline drops in later.

**Status:** complete; the MVP-now golden set scores **9/9** end-to-end against the
live Neo4j graph.

Artifacts: [src/retrieve.py](src/retrieve.py), [src/answer.py](src/answer.py),
[src/golden_eval.py](src/golden_eval.py), [src/ask.py](src/ask.py).

---

## Architecture — three thin layers

1. **`Retriever` seam** (`retrieve(question) -> Evidence`). Graph now; a `VectorRetriever`
   slots in at Phase 8 without touching the answer layer.
2. **`GraphRetriever` = LLM router + Cypher templates.**
   - `GraphVocab` pulls the graph's real controlled vocabulary (system codes+names,
     cause categories, plants, LER keys) from Neo4j.
   - An LLM classifies the question into one of a fixed set of **intents** and extracts
     anchors **constrained to that vocabulary** — it can only point at nodes that exist.
     Anything it can't ground → `out_of_corpus` (empty evidence), which is what lets the
     answerer refuse instead of hallucinating. **Text2Cypher is intentionally out of
     scope** (too brittle for a robust demo).
   - Each intent dispatches to a parameterized Cypher template (the showcase paths from
     [graph/queries.cypher](graph/queries.cypher)) or a generic k-hop fallback, and
     serializes the subgraph to evidence text with **per-LER provenance** on every fact.
3. **`answer.answer()`** — Claude, grounded ONLY in the evidence, returns structured JSON
   `{answerable, answer, citations}` (so citations are scored, not regex-scraped).
   Empty/insufficient evidence must yield `answerable=false` with no citations.

## Eval — retrieval scored separately from answering

`golden_eval.py` hand-specifies each MVP-now question (from [phase_0.md](phase_0.md))
with an **expected set of node match_keys and LER numbers derived from the records**
(`build_expected()` reads the QC oracle + Dresden/Limerick `out/`). `ask.py --golden`
runs the full system and scores **retrieval** (did the retriever surface the expected
nodes/LERs?) independently of **answering** (is it grounded, does it cite the expected
LERs?).

`python src/ask.py --golden` → **9/9 pass**:

| id | kind | intent | node recall | LER recall | result |
|---|---|---|---|---|---|
| **Q1-Limerick** | showcase | failure_chain | 1.00 | 1.00 | PASS — **pipeline-extracted** multi-hop chain (the lead demo) |
| Q1-QuadCities | showcase | failure_chain | 1.00 | 1.00 | PASS — flagged **oracle**-sourced |
| Q3 | showcase | system_components | 1.00 | 1.00 | PASS — 14 components across all 3 plants (shared `System:BJ` hub) |
| Q4 | showcase | mitigating_backups | — | 1.00 | PASS — all 3 events + available backups |
| Q11 | aggregation | cause_distribution | 1.00 | 1.00 | PASS — with small-sample caveat |
| Q14 | scale | system_failure_modes | 1.00 | 1.00 | PASS — grouping works; **no winner claimed** at N=3 |
| Q2 | scale | weak_program_events | — | 1.00 | PASS — the one personnel-error event, honestly |
| Q13 | scale | shared_component_cause | — | — | PASS — **honestly empty**, surfaces at scale |
| NEG | negative | out_of_corpus | — | — | PASS — **refused**, no citations |

Design points the notes asked for, all reflected above:
- **Retrieval vs answering are separate scores** — the router hitting the wrong intent
  or a template missing a node shows up in retrieval, independent of the prose.
- **Materialize-at-scale is honest** (Q2/Q13/Q14): the mechanism runs, but the runner
  claims no graph-vs-vector "winner" that 3 documents cannot support. The answerer
  preserves the "[note]" scale caveats verbatim.
- **Provenance tracked**: every surfaced/cited LER is tagged `oracle | pipeline`. The
  multi-hop showcase leads with **Limerick (pipeline)**; QC's chain is flagged as
  oracle-derived so the pipeline result carries the demo.
- **Negative/no-hallucination test** (NEG): an out-of-corpus question (Diablo Canyon
  steam-generator rupture) is refused with `answerable=false` and no citations.

## What this proves (and what it doesn't yet)

The graph answers **within-report multi-hop** (Q1) and **cross-document** questions
(Q3 components across plants, Q4 shared backups, Q11 cause distribution) *now*, grounded
and cited. The cross-document ones (Q3/Q4) are structurally what a flat vector retriever
cannot assemble at scale — they require the shared coded hubs. The rigorous
graph-vs-vector head-to-head is **Phase 8**, on a corpus large enough to be robust.

## Cost
Near-zero: one router call + one answer call per question (~2 calls × 9), logged to
`logs/tokens.csv`. No embeddings (those arrive with the Phase-8 baseline; note Anthropic
has no embeddings API — use local `sentence-transformers` or Voyage AI).

## Meets the Phase-6 gate
- Golden questions answered from the graph, grounded, with LER citations ✓
- Multi-hop (Q1) and cross-document (Q3/Q4/Q11) demonstrated ✓
- Honest scale handling + no-hallucination refusal ✓
- Vector baseline + comparison correctly scoped to Phase 8 ✓

## How to run
```
python src/ask.py "What components have failed in HPCI across the corpus?"
python src/ask.py --golden          # full eval with scoring
python src/ask.py --golden --brief  # one line per question
```

## Known polish (optional)
- Backups are serialized by EIIS code (e.g. answers say "BN" not "RCIC"); mapping codes
  to names in the serializer would read better. Grounded and correct as-is.
- The generic `subgraph` fallback is lightly used by the golden set; it's there for
  open questions outside the template library.
