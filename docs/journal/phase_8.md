# Phase 8 — scale-up to the full 2020–2026 corpus

**Goal (roadmap.md):** run the trusted pipeline over every LER with an event date in
2020–2026, build the full graph, and re-run/expand the golden questions so the thesis
(cross-document hubs + within-report multi-hop, grounded and cited) is demonstrated at a
corpus large enough to be real. The vector-RAG baseline stays deferred to the capstone.

**Status:** complete. **833 pipeline records + the QC oracle → a 12,474-node / 17,431-edge
Neo4j graph**; the scaled golden suite is **10/10**; the frozen 3-doc oracle regression is
still green (node F1 0.88 / edge F1 0.72).

Artifacts: [build_fetch_list.py](build_fetch_list.py), [fetch_ler.py](fetch_ler.py),
[src/pipeline_batch.py](src/pipeline_batch.py), [src/llm.py](src/llm.py),
[src/parse_form366.py](src/parse_form366.py), [src/retrieve.py](src/retrieve.py),
[src/load_graph.py](src/load_graph.py), [src/golden_eval.py](src/golden_eval.py).

---

## 1. Fetch — 835 documents, resumable
`build_fetch_list.py` parses the INL "LER Search" export (`2020s_LERs.xlsx`), pulls the
`ML…` accession out of each HTML cell, and **de-dups on LER number keeping the latest
revision** (the export is already latest-per-event; the two "combined filings" collapse to
unique accessions) → **835 accessions**. `fetch_ler.py` was hardened for the run: retry with
exponential backoff on transient errors, **log-and-continue** on persistent failures (so one
bad doc can't abort the batch), and a resumable summary. Result: **835/835 fetched, 0
failures.**

## 2. Extraction — Anthropic Message Batches + prompt caching
`pipeline_batch.py` restructures the sequential pipeline into **submit-all → poll → collect →
resolve**, re-batching only the docs whose output failed JSON parse or schema validation (the
sequential re-ask loop, a round at a time). Deterministic Form-366 parsing and the resolver
are reused verbatim, so **extraction quality is identical to the trusted path** — only the
transport changes. `llm.py` gained batch wrappers + a cached-request builder placing two
`cache_control` breakpoints on the identical prefix (schema system block + few-shot ≈ **88%
of each prompt**); `custom_id = accession`; per-result usage (incl. cache split) logged to
`logs/batch_tokens.csv`.

**Calibration first (10 docs), then the paid run**, per the roadmap gate. Two batches (the
main 750 + an 82-doc recovery, see §3) plus calibration:

| | docs | cache-read share | cost (intro) |
|---|---|---|---|
| calibration | 10 | 78% | $0.24 |
| main batch | 749 | 42% | $24.56 |
| recovery batch | 82 | 82% | $1.89 |
| **total** | **833** | 46% overall | **$26.69** |

**The cache-TTL caveat was real:** on the 750-doc batch the 5-min ephemeral cache expired
across the longer processing window, so cache-read share fell to 42% (vs ~80% on small
batches) — the run still came in **under the $38.50 budget** at **$26.69** (intro pricing,
active through 2026-08-31; ~$40 at standard). One 37k-char supplement truncated at the output
cap and was recovered with a doubled `max_tokens`. Net extraction failures: **0**.

## 3. Parser fix — recovering ~10% of the corpus
Calibration surfaced that the deterministic parser was **silently dropping 82 valid LERs** on
`could not parse event_date (block 5)`: text extraction separates the block-5 date *labels*
from their *values*, so the adjacency regex can't match. Rather than a fragile regex, the fix
falls back to the INL export's authoritative **Event Date** (already saved to
`data/raw/fetch_list.csv` when the fetch list was built), threaded into the parse via
`meta`. Verified: **0 hard parse failures**, up from 82; those 82 became the recovery batch.
(Residue: 19 docs have no extractable narrative and yield thin records — their Form-366
identity + cause still populate the graph for aggregation.)

## 4. Graph — 12,474 nodes, 99.8% connected
`load_graph.py` now globs `out/*.json` + the QC oracle (was a hardcoded 3). Rebuilt with
`--wipe --yes`:

- **12,474 nodes** (359 System, 2,379 Component, 531 Cause, 3,129 FailureMode, 971
  Consequence, 2,787 CorrectiveAction, 1,258 LER incl. stubs, 252 Manufacturer, 711
  RegulatoryReference, 97 Unit), **17,431 edges**.
- **Connectivity: 11,735 of 11,763 core nodes in one component (99.8%)**; **zero degree-0
  orphans**. The ~28 nodes in 22 tiny strays are extraction edge-cases (a System/Manufacturer
  emitted without its connecting edge, or a failure sub-chain missing its `CAUSED_BY` link) —
  0.2%, negligible for the thesis.
- **Cross-document hubs are dense:** the HPCI (BJ) hub joins **45 events across ~15 plants**
  (Browns Ferry, Fermi, FitzPatrick, Hatch, Susquehanna, …); at N=3 this returned 3.
- **Same-plant ambiguity is now real** (Browns Ferry has 8 HPCI events, Vogtle/JC has 12) —
  the clarify feature finally has genuine same-plant cases to disambiguate.
- Corpus: **769 distinct real LERs across 56 plant sites, 359 systems.**

## 5. Scale bug — the shared-hub fan-out
The `failure_chain` template traverses `Cause <-CAUSED_BY- FailureMode -LEADS_TO->
Consequence`. `Cause` is a **cross-document hub** (keyed by category), so at scale a single
event's chain **fanned out through the hub into every other event sharing that cause
category** (hundreds of consequences). Fix: constrain every hop to the anchored LER's own
edges (`{ler_number: …}`, already stamped on each edge) — keeping the chain within-report
while still letting the coded hubs do cross-document work elsewhere. Applied to
`failure_chain`, `system_failure_modes`, and `mitigating_backups`. (Templates that join *on*
the hub — `system_components`, `shared_component_cause`, `cause_distribution` — are correct
as-is.) This class of bug is exactly what only shows up at scale.

Also: **router-vocab scaling** — `GraphVocab` no longer lists every LER in the router prompt
(835 lines/call); systems + cause categories stay (bounded), while plant and LER-number
anchors are extracted as free text and resolved deterministically in Cypher. Keeps the prompt
O(1) in corpus size and hardens the clarify feature's LER-number re-ask.

## 6. Golden set — rescaled, 10/10
The N=3 golden broke at scale *because the system now behaves correctly* (former single-answer
questions became ambiguous → clarify; the cross-plant join became non-empty). Rewritten in
`golden_eval.py` for the scaled corpus:

| id | kind | what it proves | result |
|---|---|---|---|
| MH-Limerick / MH-QuadCities | showcase | within-report multi-hop, **anchored on an LER number** (plants now have many events) | PASS |
| XDOC-HPCI-COMP | xdoc | HPCI components across **44 LERs / ~15 plants** (known 3-doc set is a subset) | PASS |
| AGG-CAUSE | aggregation | cause distribution over **769 LERs** | PASS |
| AGG-WEAK-PROG | aggregation | cross-plant personnel-error pattern (**28 LERs**) | PASS |
| XPLANT-SHARED | payoff | cross-plant shared component+cause — **empty at N=3, non-empty now** | PASS |
| CLARIFY-PLANT | clarify | same-plant ambiguity: Browns Ferry ×8 → **ask** | PASS |
| CLARIFY-BROAD | clarify | ~45 HPCI events → clarify with overflow guidance | PASS |
| ADV-AGG | intent | aggregate must NOT be read as one event | PASS |
| NEG | negative | Fukushima (verified out-of-corpus) → **refuse** | PASS |

Pass rules are scale-appropriate: single-answers are LER-anchored and must cite that LER;
aggregates are scored on **known-subset recall + corpus breadth + grounding** (not an exact
3-doc citation match); the payoff must be non-empty; clarify is asserted structurally. The
**frozen 3-doc oracle regression** (`score.py`) remains the separate extraction-quality gate
and is unchanged at **node F1 0.88 / edge F1 0.72**.

## 7. Known data-quality limitations (triaged, accepted)
- **37 / 769 real LERs (~4.8%) have malformed keys** (`265-022-003-00`, `277-2-2021-002-00`)
  from inconsistent ADAMS `DocumentReportNumber` formatting. Cosmetic: each is still a unique,
  correctly-extracted event; the coded-hub joins and aggregation counts are unaffected. A
  re-keying pass (canonicalizing from `fetch_list.csv`) is deferred as not worth the risk now.
- **19 narrative-less docs** → thin records (identity + cause only).
- **22 tiny stray components** (0.2%) as above.

## Meets the Phase-8 gate
Full graph built + 99.8% connected ✓ · cross-document hubs dense (HPCI 45 across ~15 plants)
✓ · scaled golden answered 10/10 ✓ · 3-doc oracle regression green ✓ · clarify exercises real
same-plant ambiguity ✓ · extraction failures triaged (0 hard, 19 thin, 37 cosmetic keys) ✓.

## How to run
```
python build_fetch_list.py                                   # -> accessions.txt
python fetch_ler.py --from-file accessions.txt --out data/raw
python src/pipeline_batch.py --from-file accessions_batch.txt --out out   # batch extract
python src/load_graph.py --wipe --yes --verify               # rebuild + gate queries
python src/ask.py --golden --brief                           # 10/10
```

## Next
Phase 7 (probabilistic layer on the scaled graph) → vector-RAG baseline capstone → writeup.
The README still cites the 3-doc numbers and needs updating to this corpus.
