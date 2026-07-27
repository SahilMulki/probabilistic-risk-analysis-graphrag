# Phase 10a — Web UI (the graph-vs-vector demo)

**Status: COMPLETE.** A single-page app makes the Phase-9 thesis visible and clickable — the
split-pane *same question → graph answer vs vector answer*, an interactive subgraph, and a static
"rigor" tab of the measured results — **including the buckets where vector wins.** Demo mode runs
the entire core demo from a precomputed bundle of real pipeline output with **no backend, no Neo4j,
no API key, $0**; live mode answers free-form questions against the running system.

Artifacts: [src/app.py](src/app.py) (FastAPI backend), [src/precompute.py](src/precompute.py)
(demo-bundle builder + identity assert), [web/](web/) (`index.html` · `app.js` · `style.css` ·
vendored `vis-network.min.js` · `demo_bundle.json` · `metrics.json`).

---

## Goal

Phase 9 *proved* where the graph earns its keep and where it does not. Phase 10a *communicates* it
— to a viewer, not a reader — without overclaiming. This is deliberately not a chatbot: the
centrepiece is the comparison, and the honesty layer is a first-class feature, not a footnote.

## Architecture — a thin layer over the existing seam

```
 Browser (single static page, vanilla JS, ONE render contract)
   │ fetch
   ▼
 FastAPI (src/app.py)
   ├─ POST /ask       -> GraphRetriever + VectorRetriever + answer.answer()   (live; costs $)
   ├─ GET  /subgraph  -> Cypher -> {nodes, edges, truncation}                 (the one new query)
   ├─ GET  /examples  -> web/demo_bundle.json                                 (demo; no LLM)
   └─ GET  /metrics   -> web/metrics.json                                     (Results tab)
   reuses UNCHANGED: GraphRetriever · VectorRetriever · answer · risk · Neo4j
```

The only genuinely new backend logic is `/subgraph` (structured nodes/edges) and `precompute.py`.
Everything else is the Phase-6/9 retrieval seam, untouched.

## The single render contract (the vanilla-JS discipline)

One `show(result)` path paints the page, and a `result` has the **identical shape** whether it came
from the static demo bundle or a live `/ask`. The DOM-building code never branches on the data
*source* — only on the data (a graph `clarify` outcome vs an `answer`), which live mode produces
too. This is what makes vanilla strictly better than a framework here (clone-and-open, no
toolchain) and is the explicit guard against demo/live drift. Frontend is plain HTML/CSS/JS;
`vis-network` is vendored locally; nothing is fetched from a CDN.

## Views

- **Compare** — split-pane graph vs vector: each side shows the grounded answer (or an honest
  refusal), citations and retrieved reports with `oracle | pipeline` provenance badges, and a
  collapsible evidence panel (typed triples for the graph, retrieved passages for vector). Below,
  the verdict chip.
- **Subgraph** — an interactive `vis-network` view, two shapes:
  - *coded-hub membership* (a `System` hub or the `mitigating_backups` pseudo-hub + its member
    reports across plants — the cross-document join), and
  - *within-report chain* (`LER → cause ← failure-mode → … → consequence → backups` for one report
    — the multi-hop path). Node shape encodes type; node size ∝ worst Phase-7 outcome severity.
- **Scorecard** — the measured Phase-9 results from `metrics.json`: the per-bucket head-to-head
  table (score bars + pre-registered verdicts) and the cross-document **recall@k** chart (five
  coded-hub questions, all saturating far below the graph's exact 1.00). It sits at the **foot of
  the page in demo mode** and is hidden in live mode (see the second refinement pass below), rather
  than as a tab. Already-computed JSON, so cheap to include — and what separates a measured result
  from a nice anecdote.

## Credibility guardrails (the load-bearing part)

A demo that overclaims would undo nine phases of honesty discipline. Each of these is implemented,
not asserted:

1. **Verdict chips are bound to the pre-registered Phase-9 bucket**, never computed live from "who
   retrieved more." The chip shows the bucket's frozen verdict; the example's own scores appear
   only as secondary detail, and the showcase is chosen so the two never disagree. Three chip
   states: `GRAPH` / `VECTOR` / `TIE` (head-to-head) and a distinct **graph-only capability** chip
   for the risk/clarify buckets that were never scored head-to-head.
2. **The example set features where vector wins.** `LOOKC-PRAIRIE` (`lookup-content`) shows the
   graph **refusing** while vector correctly finds the plant, chip = **VECTOR**; `NEG-CHERNOBYL`
   shows both refusing, chip = **TIE**. A demo that never shows the graph losing a lookup is less
   credible, and we already had the honest result.
3. **Free-form (live) questions are shown UNSCORED.** A live question has no pre-registered bucket,
   so rendering a verdict would reintroduce exactly the rigging surface Phase 9 removed. The chip
   is replaced by *"unscored — verdicts come from the evaluated set."*
4. **Subgraph truncation is visible and criterion-based.** A large hub (e.g. `mitigating_backups`,
   274 members) draws the hub + top-20 spokes + a **"+254 more"** banner, ranked by a *stated*
   criterion (worst Phase-7 outcome severity, recency as tiebreak) — never a silent arbitrary
   slice.
5. **Provenance travels into the viz.** Member nodes carry the same `oracle | pipeline` badge as the
   citations, so the hand-marked QC exemplar node fused into the HPCI hub is visibly *not*
   extracted. (It is a real member of the `System:BJ` hub and renders amber.)
6. **Demo output is captured, not curated.** `precompute.py` writes the **real** pipeline output
   verbatim, stamped with the git SHA + timestamp it was generated at — never a hand-edited "nicer"
   version.

### The demo≡live guarantee, honestly scoped

Guardrail #6's original wording was "byte-identical to live." The answer text is **LLM-generated**,
so it is not reproducible byte-for-byte (sampling; model drift) — a literal byte-equality assert on
the prose would be a false promise. What *is* deterministic is the **retrieval layer**. So the
guarantee is scoped to it:

```
python src/precompute.py --assert     # LLM-free
```

re-runs the vector retriever (fully deterministic) and **re-dispatches the graph Cypher template**
for each stored `(intent, anchors)` — bypassing the LLM router — then checks the Evidence text, the
node keys, and the distinct LER set match the bundle exactly. The captured LLM answer is presented
as *captured output*, not re-derived live. Result: **8/8 examples pass** — the demo's retrieval is
byte-identical to a fresh live retrieval; only the prose is (unavoidably) a fixed capture.

## The showcase (8 pre-registered examples)

| id | bucket | chip | what it shows |
|---|---|---|---|
| `XDOC-HPCI-COMP` | cross-doc | GRAPH | HPCI components across ~15 plants; hub subgraph (oracle node visible) |
| `XDOC-BACKUPS` | cross-doc | GRAPH | the 274-member pseudo-hub → drives the visible-truncation demo |
| `MH-Limerick` | multi-hop | GRAPH | within-report cause→outcome chain (event subgraph) |
| `LOOK-CAUSE-VOGTLE` | lookup-id | GRAPH | exact-key resolution vector cannot pin |
| `LOOKC-PRAIRIE` | lookup-content | **VECTOR** | graph refuses, vector finds the plant — vector's honest win |
| `NEG-CHERNOBYL` | negative | **TIE** | both correctly refuse an out-of-corpus question |
| `LIKELY-OUTCOME` | risk | GRAPH-ONLY | graph gives a distribution with denominator; vector fabricates frequencies |
| `CLARIFY-PLANT` | clarify | GRAPH-ONLY | graph asks to disambiguate; vector guesses |

## Cost & performance

- **Demo mode:** $0, instant, no backend — the clone-and-run-without-paying artifact.
- **Live query:** ~3 LLM calls ≈ $0.02–0.05, ~5–8 s; needs Neo4j + a warm embedder (~1.3 GB,
  warmed in a background thread at startup, surfaced via `/health`).
- **Precompute:** one-time ~$0.50 for the 8-example bundle.

## What was left out of 10a (deliberate)

- **Live/interactive Results** (re-running k-sweeps, filtering) — new compute, deferred; the static
  table + chart already carry the rigor.
- **The router prompt-cache optimization** — kept a **separate** change, off this critical path: it
  reaches into `retrieve.py` prompt construction (a different blast radius), and demo mode never
  needs it (precomputed answers are free). It ships on its own with a before/after `logs/tokens.csv`
  cost check and a golden re-run to confirm routing is unchanged.

## Post-review refinements

After a first review, the UI was reworked for a non-expert audience and a more professional,
domain-appropriate look, and one retrieval robustness gap was fixed:

- **Plain-language throughout, no internal jargon.** A layperson intro explains LERs and the two
  approaches; the tabs are *Compare / Connections* (the measured Scorecard sits at the foot of the
  page); question categories read as
  "Connect many related reports", "Find a report by what happened", etc.; and every "Phase-N"
  reference is gone from the interface (kept only in these design docs).
- **Fleshed-out verdict card** — the winner in the title, the score, *what the score measures and
  how it's computed*, and *why* the result makes sense, authored per showcase example.
- **Answers read less like a wall of text** — report numbers render as inline chips, and the
  graph's long "; "-separated enumerations render as a bulleted list (intro sentence and any
  `[note]` caveat kept as prose). Normal prose/risk/refusal answers stay paragraphs.
- **Federal-nuclear visual language** borrowed from the U.S. Web Design System (which the NRC site
  itself uses): near-black masthead, white body, federal blue, USWDS grays, a CSS-drawn atom mark —
  **no NRC seal, wordmark, or claim of affiliation** (this is an independent project, not the NRC).
  Theme-aware light/dark, notices are neutral (no alarm-yellow), text centered where it reads well.
- **Forgiving plant-name matching** ([retrieve.py](src/retrieve.py) `_resolve_plant`) — a near-miss
  like "Brown Ferry" now fuzzy-resolves to "Browns Ferry" and returns the disambiguation prompt,
  instead of a false refusal. The exact-substring fast path is unchanged and genuinely
  out-of-corpus names (Three Mile Island, Chernobyl) still refuse, so the negative tests hold.

## Second refinement pass (usability + two bug fixes)

Hands-on use surfaced polish and two real bugs; all fixed. All frontend-only **except** the
`risk_ranking` Connections hub (a `suggest_subgraph` tweak in [src/app.py](src/app.py)); the demo
bundle was untouched, so `precompute.py --assert` still passes 8/8.

- **Light mode by default, regardless of OS.** The page ships `data-theme="light"` (no first-paint
  flash); the toggle still remembers a saved choice.
- **A cooling-tower hero + concept illustrations.** The masthead carries a darkened cooling-tower
  photo (`web/assets/hero.jpg`). The intro's two explainer cards each gain a figure: the
  knowledge-graph card shows a **real captured subgraph** — the LER 353-2025-001-00 failure chain
  (root cause → three chained failure modes → loss of safety function → four backup systems),
  rendered from the actual `subgraph_data` — and the vector card a small similarity-space diagram.
  Both are framed as white insets so they read in either theme.
- **Risk answers render a structured distribution panel — in live too (bug fix).** *Bug:* the
  outcome-distribution bars were parsed out of the **LLM's answer prose** with a regex, so they
  appeared only when the wording happened to match (the captured demo) and fell back to plain text on
  a fresh live answer. *Fix:* the bars are now parsed from the **deterministic evidence text**
  (byte-identical in demo and live, since both come from the same `retrieve.py` serializer) into an
  "Observed outcome distribution" panel — bars, event counts, severity, expected severity, and a
  collapsible whole-corpus baseline. The honesty caveats stay as prose beneath it.
- **Risk answers no longer read "Reports cited: none" (bug fix).** A corpus-wide statistic cites no
  single report *by design*; a bare "none" read as if the answer were ungrounded. Risk answers now
  show **"based on N reportable events"** instead, while non-risk answers still cite their reports.
- **The Connections tab now populates for risk questions.** A `likely_outcome` question about a
  system already showed that system's hub (its EIIS code rides in the anchors); a `risk_ranking`
  question now shows the **#1-ranked system's hub** (its match_key leads `Evidence.node_keys`)
  instead of an empty tab.
- **Scorecard moved out of the tab strip** to a standalone section at the foot of the page — shown
  in **demo** mode, hidden in **live** (a free-form live session is not a scored evaluation).
- **Layout + attribution.** Wider content column, a more prominent question-type pill, a clearer
  intro→ask divider, and a small author credit in the footer.

## Meets the gate

- Split-pane graph-vs-vector, an interactive provenance-tagged subgraph with visible truncation, and
  a static rigor tab — all from one render contract ✓
- Demo mode runs with no backend from captured real output; `--assert` proves retrieval identity
  (8/8) ✓
- Verdicts bound to pre-registered buckets, withheld on free-form, and the example set shows vector
  winning ✓
- Theme-aware (light/dark), responsive, self-contained (vendored viz, no CDN) ✓
