# Phase 7 — probabilistic / risk layer

**Goal (roadmap.md):** rank failure outcomes and paths by likelihood, computed as observed
frequencies across the 833-LER corpus — a *demonstration* of the Dynamic-PRA idea, explicitly
**NOT** a certified reactor risk model. This is where "Dynamic Risk RAG" earns the word *risk*.

**Status:** complete. Outcome classifier run over all 971 consequences (**$1.97**, Anthropic
Batches), validated at **75% accuracy** vs a 61-item hand-labeled reference; risk stats
materialized onto the System/Component/Cause hubs (**834 events, 99% classified**); the golden
suite is **23/23** (the existing 10 stayed green; 13 new risk/honesty/faceted added); the frozen 3-doc
oracle regression is untouched. A pre-existing load bug found during the build (65 real records
mis-flagged `stub`) was fixed, recovering ~8% of the corpus into every query.

Artifacts: [src/risk.py](src/risk.py), [src/classify_outcomes.py](src/classify_outcomes.py),
[prompts/outcome_classification.md](prompts/outcome_classification.md),
[data/raw/reference/outcome_labels.json](data/raw/reference/outcome_labels.json),
[src/retrieve.py](src/retrieve.py), [src/answer.py](src/answer.py),
[src/golden_eval.py](src/golden_eval.py), [graph/queries.cypher](graph/queries.cypher).

---

## 1. What "risk" means here (and what it does not)
Every number this layer produces is an **observed frequency over reportable LER events in one
selected 2020–2026 corpus** — never a certified failure rate. Three consequences are designed
*into* the layer, not bolted on as caveats:

- **The denominator is "reportable events in this corpus," not "failures."** Every LER already
  crossed a reporting threshold, and the corpus was selected (INL 2020s export, HPCI-dense) for
  recency and reportability. So `P(outcome | System:BJ)` = "among reportable LERs mentioning HPCI
  in this export, the fraction with outcome *o*" — not "the probability an HPCI failure leads to
  *o*."
- **The ranked quantity is `observed_risk_contribution`, not "risk".** It is dominated by
  `n_events`, which is a corpus/selection artifact: a high rank means **most-represented in this
  corpus**, not most-dangerous. HPCI ranking high is partly circular — we *chose* an HPCI-dense
  corpus.
- **Outcome-selection bias inflates severity.** The reporting criterion 10 CFR 50.73(a)(2)(v)(D)
  that dominates the HPCI core *is* "loss of safety function," so `loss-of-safety-function` is
  near-guaranteed for exactly the events that got reported — because that outcome is the reporting
  **trigger**, not because the system is dangerous.

These three are named explicitly in every risk answer (via `risk.OBSERVED_RISK_CAVEAT` embedded in
the evidence + a mandatory answer backstop) and are the whole point of §8.

## 2. The three load-bearing correctness rules
From the plan's second-opinion review; built *around*, not merely caveated.

1. **Count distinct EVENTS, never edges or paths.** Every conditional frequency is a fraction of
   distinct `ler_number`s. Each event contributes exactly ONE outcome — its **worst (max-severity)
   classified consequence** — so `P(outcome | entity)` is a proper distribution over events that
   sums to 1, never a double-count over an event's several consequences or edges
   (`risk.load_event_outcomes`, `risk.compute_stats`).
2. **Build the transition graph from per-LER chains first**, then sum across events — never by
   traversing the live graph through the shared `Cause` hub (that hub fans out into every unrelated
   event touching it: the Phase-8 bug). `risk.per_event_chain` groups each event's
   `(systems, cause, worst-outcome)` locally; `risk.build_transitions` sums those.
3. **Validate the classifier and gate on confidence before trusting any number** (§4).

## 3. Outcome-class taxonomy (editable data in `risk.py`)
The missing aggregatable axis: 913 of 970 `Consequence` display-names are unique, so free-text
consequences do not aggregate. Phase 7 adds a controlled `outcome_class` on each. Severity is a
hand-assigned, **contestable ordinal (1–5)**, stored as plain data so it can be argued with; the
±1 sensitivity check (§5) is what makes the subjectivity honest, not tuning the ordinals.

| outcome_class | sev | meaning |
|---|---|---|
| `loss-of-safety-function` | 5 | function lost, or both/all trains inoperable at once |
| `safety-system-inoperable` | 4 | a single train/component of a safety system inoperable (redundancy intact) |
| `reactor-trip-or-scram` | 4 | automatic or manual reactor trip / scram |
| `esf-actuation` | 3 | ECCS/HPCI/RCIC injection, AFW/EFW/EDG start — an ESF actuation other than a trip |
| `containment-isolation` | 3 | automatic containment isolation / PCIV / MSIV actuation |
| `degraded-not-lost` | 2 | degraded/non-conforming, function still maintained |
| `ts-violation-only` | 2 | a TS/LCO limit or surveillance missed, no actual loss of function |
| `other-or-no-safety-impact` | 1 | residual |

The classification (`classify_outcomes.py`) is one batched Claude pass over the 971 consequences,
each given its LER context (plant, systems, cause, causal chain). It classifies **what physically
happened, NOT how the event was reported** — the reporting criterion is withheld on purpose so the
correlation between outcome severity and the reporting rule stays *observable* downstream as
selection bias (§1), rather than baked in circularly. The prompt is versioned (`v1`).

## 4. Classifier validation — REQUIRED before any downstream number is trusted
A **61-item stratified reference set** (`data/raw/reference/outcome_labels.json`) is hand-labeled
per-item, weighted toward the load-bearing severity-5-vs-4 boundary (26 of 61 flagged `hard`). It
is deliberately a **different process** from the batch it checks (Opus per-item reasoning vs the
Sonnet batch) — but it is **not independent human-expert ground truth**, and the labels are
editable. The few-shot examples are excluded from it so the score measures generalization, not
memorization.

`classify_outcomes.py --validate` reports overall accuracy, the 5-vs-4 boundary accuracy, a
gold→predicted confusion table, and how many items the **confidence gate (`< 0.60`)** would drop.
`classifier_confidence` **gates** low-confidence consequences OUT of the statistics (flagged, not
silently counted).

**Results:** **75% overall (46/61)**, **67% on the severity-5-vs-4 boundary (20/30)**, **0 items
gated** (the classifier was confident even where it disagreed with the reference). The dominant
confusion is **6× `safety-system-inoperable` → `ts-violation-only`**: the classifier treats
*"X inoperable beyond a TS completion time"* (a startup transformer, an instrument channel) as a
Tech-Spec issue (sev 2), while the reference rule calls any functional inoperability a sev-4. This
is a genuinely contestable boundary — the classifier's call is defensible — and it *depresses*
`expected_severity` slightly for systems with many such findings rather than inflating it. Minor
confusions: 2× 4→5 (the hard single-vs-both-train call) and 3× `degraded-not-lost`→`ts-violation`
(both sev 2, no severity effect). The 75% is reported honestly rather than tuned away; the
distributions are shown alongside every scalar so a reader sees the raw class breakdown.

## 5. Statistics: `observed_risk_contribution` + the sensitivity check
Per System / Component / Cause (`risk.compute_stats`, materialized re-runnably by
`risk.py --materialize`, every stat stamped with the `n_events` it came from so a stale value is
detectable):
- `n_events` (distinct reportable events), `n_events_classified`, `P(outcome | entity)`,
  `expected_severity = Σ P(o)·sev(o)` (ordinals as interval — a demo abuse, stated), and
  `observed_risk_contribution = n_events_classified · expected_severity`.
- **The distribution is the honest object; the scalar is ranking convenience.** Every risk answer
  surfaces the full distribution + modal/max + `n_events`, never a bare scalar.
- **±1 severity sensitivity** (`risk.severity_sensitivity`, exhaustive over 3^8 = 6561 perturbed
  severity worlds): does the top-N ranking survive? If the top-3 flip under small changes, the
  ranking is presented as illustrative only.

**Results.** Corpus outcome mix over 834 events (99% classified): reactor-trip-or-scram **31%**,
safety-system-inoperable **24%**, ts-violation-only **18%**, loss-of-safety-function **15%**,
esf-actuation 6%, degraded 4%, containment-isolation 2%, other 1%.

Top systems by `observed_risk_contribution`: **Reactor Protection System (JC) 400** (n=104),
**Main Feedwater (SJ) 399** (n=103), **Auxiliary Feedwater (BA) 394** (n=103), MSIV (SB) 267,
EDG (EK) 260, RCS (AB) 257, … **HPCI (BJ) is #8 at 183** (n=48). This is the honest payoff of the
framing: **HPCI ranks 8th, not 1st** — the ranking is `n_events`-dominated, so the top slots are
the common-trip systems (RPS, feedwater), and HPCI being "most-represented relative to a random
corpus" does **not** make it the top risk contributor here.

**Sensitivity: robust.** The top-3 system set is stable in **100% of the 6561 ±1-severity worlds**;
the #1 slot itself flips (stable in only 46%) because RPS/MFW/AFW are within ~1% of each other —
which is exactly the "the ranking is driven by event counts, not severity" finding, made explicit.

HPCI's own distribution (48 events) is **safety-system-inoperable 67%**, reactor-trip 21%,
degraded 6%, ts-violation 4%, loss-of-safety-function 2% — a markedly higher single-train-inoperable
share than the 24% corpus baseline (the corpus was selected around HPCI inoperability events).

## 6. Most-probable path (per-LER-chains-first), seedable on a system OR component
`risk.most_probable_path` finds the highest-probability `entity → cause → outcome` path from a seed
via a `-log(prob)` shortest-path (Dijkstra; pure Python, **no Neo4j GDS**). The seed is a **System
OR a Component** (`Transitions` is entity-keyed): edges are `P(cause | entity)` over the entity's
coded-cause events and `P(outcome | cause)` corpus-wide. Every transition carries its supporting
event count; the path is flagged small-sample when thin. Per the roadmap: **path = `cause →
outcome`; FailureModes are NOT typed** — that is the honest ceiling this data supports.

**Component seeding** answers the "given component X degrades, what's the most probable path"
question (phase_0 Q5). It is meaningful for the EIIS-code component *category* hubs that aggregate
across events (e.g. `Component:RLY` relays, `Component:BKR` breakers, `Component:P` service-water
pumps — 8–38 events each) and correctly small-sample-flagged for the **94% of components that
appear in a single event**. A free-text component name is resolved to the most-represented hub
whose display-name contains it.

## 7. Finding: 63% of events have a provisional (uncoded) cause
A material discovery during the build: **484 of 769 non-stub LERs (63%) have a provisional/TBD
cause** — the Form-366 coded-cause field is unpopulated for most of this corpus. This partly
revises the roadmap's "cause-level stats are solid":
- The **`system → outcome`** layer is fully backed (HPCI = 45 events).
- The **`cause → outcome`** layer rests only on the ~37% coded subset (108 Design/Mfg, 97 Other,
  28 Personnel, 28 Procedure, 20 Mgmt, 4 External). HPCI has only **15** coded-cause events.

Handled exactly as the plan's sparsity discipline prescribes: **provisional causes are excluded**
from cause stats, **every transition carries `n_events`**, and paths/answers **flag the small
sample**. Component-level is structurally sparse too (most components appear once or twice), so
per-component numbers are shown only with `n` inline, never as a confident probability.

## 8. Honesty framing (non-negotiable; named mechanisms)
Every risk answer must carry (enforced by `answer.RISK_BACKSTOP` for the risk intents): the numbers
are observed reportable-event frequencies **within this corpus**, with the denominator shown; a
**"rate" question is declined** (no exposure time / reactor-years); the **corpus-selection
circularity** (most-represented ≠ most-dangerous) and the **reporting-criterion selection bias**
(loss-of-safety-function is often the reporting trigger, inflating severity) are named where they
matter; ordinal severity is flagged as treated-as-interval; small samples are flagged, not
suppressed.

## 9. Retriever intents + golden questions
Four new intents on the existing LLM router + Cypher/Python templates (routing verified to leave
the existing 10 intents unregressed): `risk_ranking`, `likely_outcome` (`P(outcome | system/cause/
component)`, also fields "failure rate / how often" phrasings), `probable_path` (system- OR
component-seeded), and — to **reduce per-question hard-coding** — a general **`faceted_frequency`**
engine.

**The general engine (one primitive, many shapes).** Most aggregate/risk questions are the same
abstract query: *"among the EVENTS matching some CONDITIONS, count / trend / compare a FACET."*
`risk.event_facets` builds a per-event facet table (systems, components, cause, worst-outcome,
plant, corrective actions, year, power level, best-effort duration, raw consequence text);
`risk.faceted_frequency(target, filters, pairs)` and `risk.compare_facets(...)` do the rest. One
small set of functions subsumes: **reverse** queries (systems/causes/plants in events that *result
in* outcome X); **combination / co-occurrence** and true **pairs** ("which pairs of components
appear together"); **compound (AND) filters** ("components in *personnel-error* events that led to
*loss of function*"); **plant counts**; **temporal trends** (`target=years`); **numeric
thresholds** (`power_level > 90`, `duration >` hours, with an operator); **corrective-action /
resolution** listings; **keyword-filtered consequence lookups** (specific events not among the 8
classes); and **side-by-side comparison** (HPCI vs RCIC). An `outcome` filter value that doesn't
resolve to a class falls back to a consequence keyword; a zero-match filter (e.g. fuel cladding,
absent here) returns an **honest "nothing to count,"** never a fabrication. The router emits
`target` + a `filters` list (+ `pairs`/`compare`); **no new template per question shape** — new
kinds of questions are answered by new *combinations* of these anchors, not new code.

Thirteen new goldens judged on **structure + grounding + honesty, never exact numbers**:
`RISK-RANK`, `LIKELY-OUTCOME`, `PROB-PATH` (system-seeded), `COMP-PATH` (component-seeded),
`CAUSE-OUTCOME`; the faceted engine's `FACET-REVERSE`, `FACET-EMPTY` (honest empty), `FACET-COMPOUND`
(AND filters), `FACET-COMPARE` (side-by-side), `FACET-TREND` (by year), `FACET-PAIRS`,
`FACET-NUMERIC` (power threshold); and the **`HONESTY-RATE`** negative golden ("failure rate of
HPCI?" → must decline the rate framing and return observed frequency + denominator + selection
bias). **Results: 23/23** — the existing 10 stayed green and all 13 new pass with genuine framing
(e.g. HONESTY-RATE answered *"No exposure time or reactor-years are given, so a rate cannot be
computed, and I decline to provide one,"* then returned the observed 48-event distribution +
denominator + selection bias).

## How to run
```
python src/classify_outcomes.py --calibrate 20     # spot-check the rubric + project cost
python src/classify_outcomes.py --run --validate   # classify all 971, stamp graph + artifact, validate
python src/risk.py --materialize                   # stamp risk stats onto the hubs (re-runnable)
python src/ask.py --golden --brief                 # 15 goldens (existing 10 + 5 risk/honesty)
python src/classify_outcomes.py --restamp          # re-stamp outcome_class from the artifact (no API), e.g. after a graph reload
```

## Load bug found + fixed during the build
The build surfaced a pre-existing **Phase-5 loader bug**: an LER that another report cites as a
previous occurrence (`SIMILAR_TO`) is emitted as a stub node, and last-writer-wins on merge let that
stub clobber the real record's `stub=false`, hiding **65 real events (~8% of the corpus)** from
*every* `NOT l.stub` query (Phase 8's golden included). `load_graph.build_graph` now never demotes a
real record to a stub; a reload recovered all 65 with their full props (HPCI went 45 → 48 events).
This is why "count distinct events" (§2) is load-bearing — the bug was silently deflating counts.

## Known limitations (triaged, accepted)
- The `n_events` axis is a corpus/reporting artifact (§1) — the honest ceiling of this
  demonstration, not a defect to fix.
- **Classifier accuracy is 75%** (§4); the risk numbers inherit that noise, mitigated by the
  confidence gate, the shown distributions, and the sensitivity check.
- `cause → outcome` rests on the 37% coded-cause subset (§7); paths flag the small sample.
- Reference labels are assistant-produced (§4), a consistency/calibration check, not clinical
  ground truth.
- Severity ordinals are subjective; the ±1 sensitivity check (§5) bounds how much that matters.
- **Cosmetic hub artifacts (pre-existing, not fixed):** `System:BJ` aggregates the HPCI events but
  its display-name resolved to "High Pressure Core Spray" (a last-writer-wins EIIS name/code
  inconsistency), so risk answers add a hedge about the label; and "Reactor Protection System"
  appears as two hubs (EIIS `JC` and an unspecified-code variant) because the resolver did not merge
  the spelled-out name with the code. Neither changes the joins or counts — a `resolve.py`
  canonicalization pass is deferred.

## Next
Vector-RAG baseline + comparison (capstone) → writeup/demo. The README still cites 3-doc numbers.
