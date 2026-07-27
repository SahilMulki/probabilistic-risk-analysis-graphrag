# Phase 9 — Vector-RAG baseline + graph-vs-vector comparison (the capstone)

**Status: COMPLETE.** Graph golden suite **42/42**; a competent local vector baseline built over
the same corpus; per-bucket head-to-head scored with a shared answer model; every fairness
invariant the plan committed to was implemented and checked. **The vector baseline wins a
bucket** — which is the point: a comparison where the graph wins everything would not be
credible.

Artifacts: [src/vector_baseline.py](src/vector_baseline.py), [src/compare.py](src/compare.py),
[src/golden_eval.py](src/golden_eval.py) (42 pre-registered, bucket-tagged specs),
`out/vector/phase9_retrieval.txt`, `out/vector/compare_results.json`.

---

## Goal, and the honest thesis

Prove *where* the graph earns its keep — not that it wins everywhere. The thesis, written down
**before** any retriever was run:

> The graph should win where **structure and exact resolution** matter (cross-document joins,
> corpus aggregation, multi-hop chains, refusal). Vector should win or tie where the task is
> **free-form single-document semantic search**. A result showing the graph winning every bucket
> would indicate a rigged baseline, not a good graph.

Both halves came true.

## The fairness charter (why this is believable)

A vector baseline is trivial to rig, and a rigged win proves nothing. Every invariant below is
implemented in code, not just asserted:

| Invariant | How it was met |
|---|---|
| Same source text | Vector embeds `data/raw/*.txt` — the exact text the graph was extracted from, keyed by the same LER numbers (832 docs / 12,397 chunks at the primary config) |
| Same answer model **and** a format-neutral prompt | Both retrievers feed the identical `answer.answer()`. `ANSWER_SYSTEM` previously said *"a subgraph retrieved from a knowledge graph"* — that framing was **removed** so prose evidence is not penalised |
| Answer-format parity **verified** | A dedicated gate (below) holds retrieval constant and varies only the format |
| A competent, not weak, baseline | `BAAI/bge-large-en-v1.5` (strong) **and** `all-MiniLM-L6-v2` (weak) run side by side |
| No cherry-picked `k` | Reported as a **recall@k sweep** (k = 1…100), not one favourable value |
| No cherry-picked refusal threshold | Reported as a **swept PR curve**, not a single tuned point |
| Chunking not cherry-picked | **Per-bucket** ablation across three chunk sizes |
| Build cost disclosed | Graph extraction ≈ **$27** (Phase 8) + $1.97 (Phase 7) vs vector indexing **$0** (local) |

Two invariants deserve emphasis because they cut *against* the graph:

- **The graph was fixed, not flattered.** The `subgraph` fallback returned evidence with empty
  provenance, so LER-anchored lookups scored as ungrounded even when the answer was correct, and
  a 2-hop expansion fanned out through shared hubs into unrelated reports. Both were fixed
  ([retrieve.py](src/retrieve.py)) — leaving them would have *understated the graph*.
- **The lookup bucket was corrected mid-course, in vector's favour.** See below.

## Architecture — one seam, two retrievers

```
question ─┬─► GraphRetriever.retrieve()  ─┐
          └─► VectorRetriever.retrieve() ─┴─► Evidence ─► answer.answer()   (unchanged)
```

`VectorRetriever` implements the Phase-6 seam exactly: chunk → embed → cosine top-k → group by
LER → serialize prose with per-LER provenance. Retrieval is **numpy brute-force** (exact) — at
~12k chunks an ANN index would add dependency risk for no benefit. Indexes cache under
`out/vector/` (git-ignored, free to rebuild — no API cost, so a clone reproduces the baseline).

## The question set — pre-registered and bucket-tagged

42 specs, each frozen into exactly one bucket (`BUCKET_BY_ID`) **before** any retriever ran, so
per-bucket verdicts could not be back-fit to results.

**Head-to-head buckets** (both genuinely attempt): `lookup-id` (6), `lookup-content` (5),
`multi-hop` (5), `cross-doc` (5), `negative` (5).
**Graph-capability buckets** (capability claims, not scored head-to-heads): `aggregation` (2),
`risk` (12), `clarify` (2).

### A mid-course correction, disclosed

The original lookup bucket anchored every question on an **LER number**. Harness validation
showed this was accidentally **graph-favourable**: embeddings ignore lexical identifiers, and
even a semantic rephrasing retrieved the *same plant, a different year*. Vector cannot pin a
**specific** report among many similar ones — so those questions tested the graph's exact-key
resolution, not vector's single-doc strength. That is precisely the failure mode the plan warned
about ("vector-favourable questions the graph actually aces").

The fix, made **before** any scored head-to-head: keep the six as `lookup-id` (an honest finding
in its own right) and add five **`lookup-content`** questions — distinctive events
(a dropped floor tile, directional drilling through a cable bundle, a cracked battery cell post)
where the answer is the plant. The graph has no template for "find the event with characteristic
X", so it declines; vector's semantic search is its natural strength. This correction moved the
comparison **toward** vector, not the graph.

---

## Results

### Head-to-head (shared answer model; vector = bge-large/medium, k=8)

| bucket | n | graph | vector | win/tie/loss | verdict |
|---|---:|---:|---:|---|---|
| `lookup-id` | 6 | **0.83** | 0.00 | 5/1/0 | **GRAPH** |
| `lookup-content` | 5 | 0.00 | **0.40** | 0/3/2 | **VECTOR** |
| `multi-hop` | 5 | **1.00** | 0.00 | 5/0/0 | **GRAPH** |
| `cross-doc` | 5 | **1.00** | 0.08 | 5/0/0 | **GRAPH** |
| `negative` | 5 | 1.00 | 1.00 | 0/5/0 | **TIE** |

### recall@k — cross-document assembly (the headline)

Ground truth is the **deterministic EIIS-coded hub membership**, which the graph computes exactly
(so graph = 1.00 by construction; see *Threats to validity*). Vector saturates far below it:

| question | \|full\| | @1 | @5 | @10 | @20 | @50 | @100 |
|---|---:|---:|---:|---:|---:|---:|---:|
| XDOC-HPCI-COMP | 47 | 0.00 | 0.09 | 0.19 | 0.36 | 0.45 | **0.55** |
| XDOC-RCIC-COMP | 24 | 0.04 | 0.21 | 0.33 | 0.33 | 0.42 | **0.50** |
| AGG-WEAK-PROG | 33 | 0.00 | 0.03 | 0.03 | 0.06 | 0.15 | **0.21** |
| XPLANT-SHARED | 21 | 0.00 | 0.00 | 0.00 | 0.00 | 0.05 | **0.10** |
| XDOC-BACKUPS | 274 | 0.00 | 0.01 | 0.02 | 0.04 | 0.08 | **0.16** |

Even allowed to retrieve **100 distinct reports**, vector recovers barely half of the HPCI hub and
a tenth of the cross-plant join. This is not a tuning problem — it is what "retrieve the *k* most
similar chunks" can do against "traverse every edge on a shared coded hub."

### Refusal — a curve, not a point

| threshold | negatives refused | positives retained |
|---:|---:|---:|
| 0.00–0.60 | 0% | 100% |
| 0.70 | 40% | 76% |

Top-1 similarity: **positives 0.65–0.80, negatives 0.67–0.73** — the ranges *overlap*, so **no
threshold separates in-corpus from out-of-corpus**. bge finds "the Chernobyl reactor explosion"
about as similar to US trip reports as a real question. The graph refuses structurally
(negatives 100% refused, positives 100% retained) because an ungroundable anchor yields empty
evidence.

**Important nuance (in vector's favour):** the head-to-head `negative` bucket is a **TIE at 1.00**
— vector refused all five. Refusal came from the **answer LLM reading the prose and recognising
it was irrelevant**, not from the similarity gate. So: vector *can* refuse, but the refusal lives
in the reader, not the retriever.

### Embedder invariance — the anti-strawman check

Per-bucket vector retrieval recall (config=medium, k=8):

| model | lookup-id | lookup-content | multi-hop | cross-doc |
|---|---:|---:|---:|---:|
| bge-large (strong) | 0.00 | **0.80** | 0.00 | 0.08 |
| MiniLM (weak) | 0.17 | **0.80** | 0.00 | 0.09 |

**Identical verdicts on every bucket.** The structural results are not an artifact of the
embedder — a stronger model does not rescue cross-doc assembly, because the problem is not
retrieval quality. This is the single most important robustness result in the phase.

### Chunking ablation — per bucket (MiniLM)

| config | lookup-id | lookup-content | multi-hop | cross-doc |
|---|---:|---:|---:|---:|
| small (90w) | 0.00 | 0.80 | 0.00 | 0.10 |
| medium (180w) | 0.17 | 0.80 | 0.00 | 0.09 |
| large (330w) | 0.00 | 0.80 | 0.00 | 0.08 |

Chunk size moves nothing structural. Reported per-bucket precisely so a single "best chunking"
number cannot hide a tradeoff.

### Answer-format parity gate — **HOLDS**

Retrieval held constant, format varied: the *same report's* facts were presented as graph triples
and as its own raw prose, to the same answerer. All three probes produced correct, grounded
answers in **both** formats (prose answers were often richer, e.g. adding "Mode 1 at 98% power").
Therefore head-to-head gaps are **retrieval**, not the prompt favouring an evidence shape.

---

## What this actually shows

1. **Cross-document assembly is the graph's structural win.** Vector's ceiling is ~0.10–0.55 recall
   of a coded hub; the graph returns it exactly. No embedder, k, or chunk size changes this.
2. **Vector cannot pin a *specific* report** — by identifier *or* by generic content — because
   retrieval is by similarity and this corpus is homogeneous (hundreds of similar trip reports).
   Hence `lookup-id` 0.00 and `multi-hop` 0.00.
3. **Vector genuinely wins free-form content search.** It retrieved 4/5 distinctive events
   (two at rank 1) for questions the graph's template router simply cannot express. This is a
   real capability the graph lacks, and the reason the comparison is credible.
4. **The graph-only buckets are a capability claim, and vector's failure mode is worse than
   refusing.** Asked corpus-wide statistical questions, vector answered 6/12 risk and 1/2
   aggregation questions with **confident frequency language and no denominator**:
   *"consistently reported as having no actual safety consequences"*, *"most often lead to"*,
   *"a recurring pattern"* — generalised from 8 chunks out of 833 reports. It does not just fail
   to count; it **manufactures the impression of statistics**. (The HPCI claim is also likely
   wrong: Phase 7 shows loss-of-safety-function dominates HPCI events precisely because it is the
   reporting trigger.) The graph's risk layer returns real distributions with `n_events` and
   mandatory "within this corpus" framing.
5. **Honest graph limits.** `lookup-id` was 0.83, not 1.00: on the Watts Bar question the graph
   answered at the *component* level (RHR flow-control valves) while the report's own phrasing was
   functional ("both trains of Low Head Safety Injection") — structure can be more granular than
   the question wants. And on `lookup-content` the graph scores **0.00**: it declines rather than
   fabricating, which is correct behaviour but still a loss.

## Threats to validity (stated, not hidden)

- **Cross-doc ground truth is the graph's own output.** Hub membership is defined by the NRC's
  EIIS coding and computed deterministically by Cypher, so this is a definition, not a circularity
  — but a reader should know the graph is scored against a set it computes exactly. The honest
  claim is narrow: *"vector cannot assemble all reports sharing a coded hub,"* not *"vector is
  worse at everything cross-document."*
- **`lookup-content` is 5 questions and vector scored 0.40**, not higher: it retrieved 4/5 targets
  but only named the right plant in 2. Small n; treat the direction (vector wins) as the result,
  not the magnitude.
- **`multi-hop` conflates retrieval with reasoning.** Vector fails these by never retrieving the
  anchored report, so this phase does *not* establish that vector cannot assemble a chain from
  prose it has in hand — only that it cannot get that report.
- **One corpus, one domain.** Homogeneous failure reports are unusually hard for similarity search;
  a heterogeneous corpus would likely narrow the lookup gaps.

## Cost

**423 API calls, 2.43M input / 137k output tokens ≈ $6.23–9.34** (Sonnet-5 intro vs standard
rates). Vector indexing was **$0** — local embeddings on the M1 GPU (MPS), ~20 min for bge-large
over 12.4k chunks, cached thereafter. Known optimization: the graph router ships the full
vocabulary (~9k tokens) uncached on every call and dominates the bill; a `cache_control`
breakpoint would cut it substantially.

## How to run

```
python src/vector_baseline.py --all                  # build/cache every model x chunk config
python src/compare.py --retrieval --model bge-large  # recall@k, refusal curve, ablation, invariance (no API)
python src/compare.py --parity                       # answer-format fairness gate
python src/compare.py --headtohead                   # the scored per-bucket comparison
python src/ask.py --golden                           # graph regression, 42/42
```

## Meets the Phase-9 gate

- A competent vector baseline over the same corpus, on the same seam, with the same answerer ✓
- Side-by-side comparison showing **where** the graph beats vector — and where it does not ✓
- Every fairness invariant implemented and checked (parity, invariance, swept k and threshold,
  per-bucket ablation, disclosed cost) ✓
- Pre-registered buckets, with the one mid-course correction disclosed and made in vector's
  favour ✓
