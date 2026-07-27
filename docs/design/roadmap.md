# Roadmap — remaining work (Phase 7 onward)

> Companion to `plan.md`. **Self-contained for a reviewer without repo access.** Personal
> learning project — **not research**; pragmatism over rigor; probabilities may be rough
> estimates (owner's explicit call).
> Updated after **Phase 8 (scale-up) completed**, then **revised after a second-opinion review**
> of the Phase 7 plan (statistical-soundness fixes marked *[review]*).

## Current state (context for the reviewer)

Graph RAG over NRC Licensee Event Reports (LERs). **Phases 0–6, the abstain/clarify feature,
and Phase 8 (scale-up) are complete and committed.**

Pipeline: deterministic NRC Form-366 parse **+** LLM (`claude-sonnet-5`) narrative extraction
→ Pydantic **schema v4.1** (10 node types, 11 edge types) → `resolve.py` canonicalizes EIIS
system/component codes → **Neo4j** → `retrieve.py` (LLM router + Cypher templates, **no
Text2Cypher**) → `answer.py` (grounded, cites LER numbers, stamps `oracle|pipeline`).

**Corpus (Phase 8): 833 LERs, event dates 2020–2026**, fetched from NRC ADAMS. Graph =
**12,474 nodes / 17,431 edges**, 99.8% one connected component, **0 orphans**; cross-document
hubs dense (the HPCI system joins **45 events across ~15 plants**). Extraction cost **$27**
(Anthropic Message Batches + prompt caching). Frozen 3-doc oracle regression: node F1 **0.88**
/ edge F1 **0.72**. Scaled golden suite: **10/10**.

### Graph structure the reviewer must know for Phase 7
- **Coded hubs are shared across documents** and aggregate cleanly: `System`, `Component`,
  and non-provisional `Cause` (by category; ~6 categories) are single nodes that many LERs
  point at (e.g. one `System:BJ` hub for all 45 HPCI events).
- **`FailureMode`, `Consequence`, `CorrectiveAction` are PER-LER** — keyed with the LER
  number, phrased differently each time, each appearing once. **They do NOT aggregate without
  a semantic layer.** This is the central constraint on Phase 7.
- Every edge is stamped with `ler_number`. The causal chain is
  `Cause <-CAUSED_BY- FailureMode -LEADS_TO-> … -> Consequence`, optionally
  `-BACKED_UP_BY-> System`. A load-time `LER-[:HAS_CAUSE]->Cause` bridge joins each report.
- **Scale lesson directly relevant to Phase 7:** a traversal that goes *through* a shared hub
  fans out into every unrelated event sharing that hub (a Phase-8 bug: the `failure_chain`
  query exploded through the shared `Cause` node until each hop was pinned to `{ler_number}`).
  Phase 7's aggregations must be **deliberate about which axis is shared vs per-LER.**

## Remaining sequence
1. ~~**Phase 7 — probabilistic / risk layer**~~ — **COMPLETE** (see [phase_7.md](../journal/phase_7.md)).
2. ~~**Vector-RAG baseline + comparison** — capstone.~~ — **COMPLETE** (see [phase_9.md](../journal/phase_9.md)).
   Graph suite **42/42**; per-bucket head-to-head with a shared answer model and a verified
   format-neutral answer prompt. Graph wins `cross-doc` (vector recall@100 only 0.10–0.55 of a
   coded hub vs 1.00), `multi-hop`, and `lookup-id`; **vector wins `lookup-content`** (free-form
   semantic search the template router cannot express); `negative` ties. Verdicts shown
   **embedder-invariant** (bge-large ≡ MiniLM) and chunk-size-invariant. ≈$6–9.
3. **Phase 10 — writeup / demo (incl. the web UI).** Deliberately kept separate from Phase 9:
   Phase 9 is an evaluation with a correctness bar, Phase 10 is communication — folding them
   would invite writing the narrative while the numbers are still being generated. The UI's
   centrepiece is the split-pane *same question → graph answer vs vector answer* view, which
   only became buildable once the baseline existed.
   - **Phase 10a — web UI — COMPLETE** (see [phase_10a.md](../journal/phase_10a.md)). Single-page app
     ([web/](web/)) + FastAPI backend ([src/app.py](src/app.py)); split-pane compare, interactive
     provenance-tagged subgraph with visible criterion-based truncation, and a static Phase-9
     "rigor" tab. Demo mode runs the whole core demo from a precomputed bundle of **real** pipeline
     output ($0, no backend); `precompute.py --assert` proves demo≡live at the retrieval layer
     (8/8). Verdict chips bound to pre-registered buckets, withheld on free-form; the showcase
     features the buckets where **vector wins**.
   - *Remaining in Phase 10:* the narrative writeup; and the **router prompt-cache** optimization
     kept as a separate change off the UI critical path (a `cache_control` breakpoint on the ~9k-token
     router vocab prefix, with a before/after `logs/tokens.csv` check + a golden re-run).

Deferred by explicit decision (not worth it now): the README update to the 833-doc numbers;
a re-keying pass for 37 (~5%) cosmetically malformed LER keys.

---

# Phase 7 plan — probabilistic / risk layer

> **STATUS: COMPLETE** — implemented as planned (all three load-bearing review fixes built in).
> Classifier run over 971 consequences ($1.97), 75% vs a 61-item hand-labeled reference; 834
> events materialized; golden **23/23** (existing 10 green + 13 risk/honesty, incl. component-seeded
> paths and a general faceted-frequency engine: reverse, combination/pairs, compound-AND, temporal,
> numeric, corrective-action, and comparative queries). A pre-existing loader
> stub-merge bug found during the build was fixed (recovered ~8% of the corpus). Full write-up in
> **[phase_7.md](../journal/phase_7.md)**. The plan below is retained as the design record.

## Framing
*Dynamic Risk RAG* — Phase 7 is where "risk" earns its name. Goal (from plan.md): **rank
failure outcomes and paths by likelihood, computed as observed frequencies across the corpus**
— a *demonstration* of the Dynamic-PRA idea, explicitly **not** a certified reactor risk model.
Scaling before this (the 8→7 reorder) was so the frequencies come from real counts. **The
review's verdict: build close to as written; the frequencies are now meaningful (833 docs), but
three statistical-soundness fixes are load-bearing and must be built *around*, not merely
caveated.**

## The core challenge — and what the numbers actually mean
Two constraints, both load-bearing.

**(a) Aggregate over shared axes, counting distinct EVENTS.** Coded hubs aggregate; free-text
`Consequence`/`FailureMode` do not — so Phase 7 adds a controlled **`outcome_class`** on
Consequences (the missing aggregatable axis, the enabler for *P(outcome | X)*). **Every
conditional frequency is computed over distinct `ler_number`s — count events, never edges or
paths.** *[review]* The aggregated transition graph is built by **grouping each LER's chain
first, then summing across LERs — never by traversing the live graph through shared hubs** (that
is the Phase-8 fan-out bug; through a shared `Cause` node it silently double-counts every
unrelated event touching that hub). **This is the single most likely place the numbers come out
wrong-but-plausible.**

**(b) The denominator is "reportable events in this corpus," not "failures."** *[review]* Every
LER already crossed a reporting threshold, and the 833 were selected (2020–2026 INL export,
HPCI-dense) for recency and reportability. So `P(outcome | System:BJ)` = "among reportable LERs
mentioning HPCI in this export, the fraction with outcome o" — **not** "probability an HPCI
failure leads to o." Three consequences the design is built around:
- **`n_events` is a corpus/reporting artifact, not true failure frequency.** The ranked
  quantity is therefore named **`observed_risk_contribution`**, and the answer layer says
  **"within this corpus"** on every risk statement. HPCI ranking high is partly circular (we
  *chose* an HPCI-dense corpus) — the honest reading is "most-represented in this corpus," not
  "riskiest system." The **severity axis is defensible; the n_events axis is a corpus artifact.**
- **Outcome selection bias inflates severity.** The reporting criterion 10 CFR 50.73(a)(2)(v)(D)
  that dominates the HPCI core *is* "loss of safety function," so `loss-of-safety-function`
  (sev 5) is near-guaranteed for exactly the events that got reported — because that outcome is
  the reporting **trigger**, not because the system is dangerous. `expected_severity` is
  inflated by the reporting rule, most for over-sampled systems. Mitigation: **name this
  mechanism explicitly** in risk answers + `phase_7.md`, and **report the outcome distribution
  conditioned on reporting criterion** so the artifact is visible, not hidden.

## Confirmed scope (owner's three forks)
Full risk layer · LLM-classified outcome typing (one batched pass over ~971 Consequences,
~$1–3) · risk = **frequency × severity** with **both factors surfaced**.

## Outcome-class taxonomy (severity 1–5, hand-assigned, **stored as editable data in `risk.py`**)
| outcome_class | sev | meaning |
|---|---|---|
| `loss-of-safety-function` | 5 | actual loss of a safety function / both trains |
| `safety-system-inoperable` | 4 | a safety system inoperable (single train/component) |
| `reactor-trip-or-scram` | 4 | automatic or manual reactor trip / scram |
| `esf-actuation` | 3 | ECCS/AFW/EDG-start/other engineered-safety-feature actuation |
| `containment-isolation` | 3 | automatic isolation / PCIS actuation |
| `degraded-not-lost` | 2 | degraded condition, function maintained |
| `ts-violation-only` | 2 | condition prohibited by TS / LCO exceeded, no functional loss |
| `other-or-no-safety-impact` | 1 | everything else |

(The sev-4 `reactor-trip-or-scram` vs `safety-system-inoperable` tension is noted — a clean
scram is a *designed* safe response — but not relitigated; severity is subjective by design and
handled by the sensitivity check below rather than by tuning ordinals.)

## The ranked quantity: `observed_risk_contribution` *[renamed per review]*
- `expected_severity(entity) = Σ_o P(o | entity) × severity(o)` — a mean over **ordinals
  treated as interval data**, a standard demo abuse, stated as such.
- `observed_risk_contribution(entity) = n_events(entity) × expected_severity(entity)`.
- **The distribution is the honest object; the scalar is ranking convenience.** *[review]* Every
  risk answer surfaces the **full outcome distribution + the modal/max class + `n_events`**,
  e.g. *"expected_severity 4.2 — 3 of 5 events were loss-of-safety-function (within this
  corpus)."*
- **Severity sensitivity check** *[review]*: report whether the top-N ranking survives a **±1
  perturbation** of the severity ordinals. If the top-3 flip under small changes, present the
  ranking as **illustrative only** — this robustness note is worth more than getting the
  ordinals "right."

## Architecture (pieces)
1. **Outcome classes.** Batched LLM classification of ~971 Consequences → `outcome_class` +
   `classifier_confidence`, stamped on the graph. **The classification prompt is versioned like
   the extraction prompt** (a load-bearing model artifact). *[review]*
2. **Classifier validation — REQUIRED before any downstream number is trusted.** *[review, new]*
   Hand-label a **stratified ~40–60 Consequence sample**; measure accuracy + per-class
   confusion; keep the labeled set as a regression check. Focus the hardest boundary:
   `loss-of-safety-function` (5) vs `safety-system-inoperable` (4) = the single-train-vs-both
   distinction the HPCI corpus lives on — smear it and every risk score is off.
   **`classifier_confidence` GATES low-confidence nodes** into a flagged bucket, excluded from
   the stats — not merely recorded.
3. **Empirical statistics.** Per System/Component/Cause: `n_events` (distinct LERs), outcome
   distribution `P(o|entity)`, `expected_severity`, `observed_risk_contribution` — **all counts
   over distinct `ler_number`s.** Optionally condition the outcome distribution on reporting
   criterion (to expose the selection artifact).
4. **Most-probable path.** An aggregated transition graph **built from per-LER chains first**
   (group each LER's `cause → outcome` locally, then sum across LERs), edges weighted by
   observed conditional frequency (distinct events); most-probable `cause → outcome` path from a
   seed system/component via `-log(prob)`. Pure Python — **no Neo4j GDS.**
5. **Retriever intents.** `risk_ranking`, `likely_outcome` (`P(outcome | system/cause/
   component)`), `probable_path` → new templates on the existing LLM router.
6. **Answer layer.** Probabilities with **mandatory** framing: explicit denominator, "within
   this corpus," "observed reportable-event frequency, not a certified rate," the
   reporting-criterion mechanism where relevant, small-sample flags, and the full distribution
   (not just the scalar).
7. **Honesty framing** (non-negotiable) — see below.

## Data-sparsity discipline *[review]*
~6 cause categories over 833 events → **cause-level stats are solid**. But **component-level is
structurally sparse** (many components appear once or twice), so most `P(o|component)` is
single-event. Component-level risk is presented **only in aggregate or with `n` shown inline —
never as a confident per-component probability.** Small-sample flag at `n < 5` (label, don't
suppress).

## Materialization discipline *[review]*
Materialized hub stats go stale on any reload/extend (Phase 9 / baseline may reload). So:
`risk.py --materialize` **recomputes from scratch, re-runnably**, and **every stat written to a
node carries the `n_events` it was computed from**, so a stale stat is detectable rather than
silently wrong.

## Honesty framing (non-negotiable; named mechanisms) *[expanded per review]*
Every number is an observed frequency over **reportable LER events in this 2020–2026 export**,
denominator shown. Named in answers and `phase_7.md`:
- **Corpus-selection circularity** — an HPCI-dense export makes HPCI look "risky"; it's
  most-represented, not most-dangerous.
- **Outcome = reporting criterion** — loss-of-safety-function is often the reporting *trigger*
  (10 CFR 50.73(a)(2)(v)(D)), inflating severity for over-sampled systems.
- **Not a true rate** — no exposure time / reactor-years; not comparable to a PRA failure rate.
- **Ordinal severity treated as interval** — a demo convenience; the distribution is shown too.
- **Component sparsity** — per-component numbers are illustrative, `n` always shown.

## Golden questions (structure + grounding + honesty, not exact numbers)
- `RISK-RANK`, `LIKELY-OUTCOME`, `PROB-PATH`, `CAUSE→OUTCOME` — each with `n_events` + the full
  distribution + the "within this corpus" framing.
- **Honesty / negative golden** *[review, new]*: *"What's the failure rate of HPCI?"* → the
  system must **decline the 'rate' framing** and return the observed reportable-event frequency
  with its denominator, naming the selection bias — not a rate. Directly tests the
  non-negotiable framing rather than trusting it.
- Keep the existing 10/10 green.

## Implementation sequence
1. Taxonomy + severity (editable data) in `src/risk.py`.
2. Versioned classification prompt + outcome-classification batch → `outcome_class` +
   `classifier_confidence`.
3. **Classifier validation on the hand-labeled sample; gate on confidence — before any stats.**
4. Stats layer (distinct-event counts) + `risk.py --materialize` (n_events-stamped, re-runnable).
5. Most-probable path (per-LER-chains-first transition graph).
6. Retriever intents + templates.
7. Answer-layer framing + caveats.
8. Golden additions (incl. the honesty golden) + re-run.
9. `phase_7.md` + risk gates in `graph/queries.cypher`.

## Cost / effort
~$1–3 (classification batch) + a couple hours hand-labeling the ~40–60 validation sample; rest
is local compute. ~3 days of build.

## Open decisions — converged by the review
- **Path = `cause → outcome`; do NOT type FailureModes** (reviewer: strongly agree — that is the
  honest ceiling this data supports; typing per-LER failure modes doubles cost and
  unvalidated-classifier risk on the noisiest axis).
- **Severity:** editable data + a ±1 sensitivity check in the writeup; ordinals not relitigated.
- **Small-sample:** flag `n < 5`, don't suppress; component-level only in aggregate / with `n`.
- **Materialize:** yes, but re-runnable `--materialize` with `n_events` stamped per stat.

**The three things to get right before trusting any output** (review's insistence): (1) count
distinct events, and build the transition graph from per-LER chains — not by traversing shared
hubs; (2) validate the outcome classifier against the hand-labeled sample and gate on
confidence; (3) frame the `n_events` axis as "observed contribution within this corpus" and name
the reporting-criterion selection bias. Severity ordinals matter less — the distribution + the
sensitivity check handle the subjectivity.
