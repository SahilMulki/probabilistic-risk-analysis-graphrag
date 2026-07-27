# Graph RAG over NRC Licensee Event Reports — Personal Project Plan

A build plan for a personal project: a Graph RAG system over NRC Licensee Event Reports (LERs)
that can answer multi-hop, relational questions about nuclear-plant failures — the kind plain
vector search can't. Built to learn knowledge graphs, retrieval, and LLM pipelines, with an
optional probabilistic-reasoning stretch goal that connects to Dynamic PRA.

---

## How to use this plan

This is a **personal learning project**, not a thesis — so there's no proposal to defend, no
novelty to establish, no results section to publish. What's kept from a research-style approach
are the steps that make you _understand what you're building_: reading your data by hand,
designing a schema, writing down the questions you want to answer, and building a baseline so
you can see the graph actually earning its keep. Those aren't academic ceremony; they're how
you avoid building something you don't understand.

Each phase ends with a **gate** — a concrete "you're done when..." so you always know whether
to move on. The ordering keeps **cost near zero** until the very end: everything through Phase 7
runs on a tiny set of documents and a local model on your M1. You spend real API money once, in
Phase 8, on a pipeline you already trust.

**MVP first.** Phases 1–6 are the core project — a working Graph RAG system you can be proud of.
Phase 7 (probabilistic layer) and Phase 9 (writeup) are stretch / polish. If you only ever finish
through Phase 6, you've built something real.

**Decisions already made (so you don't stall):**

- **Graph store: Neo4j Community Edition, via Neo4j Desktop, running locally.** No free-tier node
  cap to worry about, no cloud account, $0, same Cypher you'd use anywhere. (Revisit AuraDB only
  if you later want a hosted demo link.)
- **Graph framework: still open** — decided in Phase 3 with a small hands-on comparison, because
  the right choice depends on how much control you need over the schema and the optional
  probabilistic layer. A recommendation is noted there.

---

## Phase 0 — Frame it (an afternoon, no code)

**Goal:** Write down what you're building and what "done" looks like, so you don't drift.

1. **One-paragraph project description.** What it is, why it's interesting to you, what it should
   do. This is your personal README, not a proposal. Keep it honest and small.
2. **Write your "golden questions" (8–15 of them).** The actual questions you want to be able to
   ask the finished system — these are your north star, your demo, and later your test set. Aim for
   the multi-hop, relational kind that show off a graph, for example:
   - "What chain of failures led to HPCI being inoperable at Quad Cities?"
   - "Which events across all these plants trace back to a weak maintenance or procedure program?"
   - "What components have failed in the HPCI system across the whole corpus?"
   - "Which events were mitigated by a redundant safety system being available?"
3. **Learn just enough domain context.** You don't need to be a nuclear engineer — the LERs are
   self-documenting. Skim the NRC's LER guidance (NUREG-1022) enough to recognize the standard
   sections and what the EIIS code tags mean. That's plenty to start. You'll learn the rest by
   reading reports.

**GATE:** You have a short project description and a written list of 8–15 golden questions.

---

## Phase 1 — Get the data and read it (this is where you learn the domain)

**Goal:** Acquire a small set of LERs and understand them by hand. For a non-expert, this phase
_is_ your domain education — don't rush it.

1. **Download ~20–50 LERs** from the NRC ADAMS public library (they're free). Start narrow: a
   single system (e.g. HPCI-related events) or a couple of plants. Narrow = cheaper, cleaner, and
   the cross-document links show up faster.
2. **Read 10–15 of them yourself.** Notice the repeating skeleton every LER shares: Event
   Description → Cause → Safety Assessment → Corrective Actions → Previous Occurrences →
   Component Failure Data, plus the Form 366 header fields. This structure is what makes your
   schema easy.
3. **Hand-mark 3–5 reports.** On paper or in a doc, highlight every component, system, cause,
   effect, and corrective action, and draw the arrows between them. You're not annotating like an
   expert — you're using the document's own labels. This gives you (a) your schema and (b) a small
   answer key to check your automated extraction against later.

**GATE:** You can read a new LER and, in a few minutes, sketch its failure chain by hand.

---

## Phase 2 — Design the schema (grounded in the real reports)

**Goal:** Decide the node types, edge types, and properties — mirroring the LER structure.

A starting schema drawn directly from your two sample reports:

- **Node types:**
  - `Plant` / `Unit` — Vogtle Unit 3 (AP1000 PWR), Quad Cities Unit 1 (GE BWR). Props: reactor type, docket number.
  - `Event` — the LER itself. Props: LER number, event date, report date, operating mode, power level, reporting criterion (10 CFR §), title.
  - `System` — **keyed by EIIS code.** HPCI (`BJ`), rod control (`AA`).
  - `Component` — **keyed by EIIS code + identifier.** Breaker (`BKR`), MOV `1-2301-3` (`V`), opening coil (`CL`), control rod banks (`ROD`). Props: manufacturer, model/part number.
  - `Cause` — "inadequate maintenance strategy", "inadequate procedure guidance". **Normalize these into shared categories — this is what links unrelated events.**
  - `FailureMode` — "failed to open", "coil failed / rollers binding", "overlap one step under limit".
  - `Consequence` — "HPCI inoperable / no injection", "missed LCO entries".
  - `CorrectiveAction` — "replace coil", "revise maintenance strategy", "revise surveillance procedure".
  - `SafetyFunction` / backup system — RCIC, ADS, LPCI, Core Spray (the defense-in-depth angle).

- **Edge types:**
  - `OCCURRED_AT` (Event → Unit), `PART_OF` (Component → System, Unit → Plant)
  - `INVOLVES` (Event → System/Component)
  - `CAUSED_BY` (FailureMode → Cause), `LEADS_TO` (the causal chain between failure modes/consequences)
  - `RESULTS_IN` (FailureMode → Consequence)
  - `MITIGATED_BY` (Event/Consequence → CorrectiveAction)
  - `BACKED_UP_BY` (System → SafetyFunction) — the redundancy edges
  - `REPORTED_UNDER` (Event → regulatory reference)

- **Reserved for the optional probabilistic layer (Phase 7):** probability / frequency properties
  on `LEADS_TO` and `CAUSED_BY` edges, and failure-rate priors on `Component` nodes.

Keep v1 minimal — you can always add node types later. Draw it as a diagram.

**GATE:** A written schema diagram, and every golden question (Phase 0) maps to a path over it.

---

## Phase 3 — Pick the framework (a small, timeboxed comparison)

**Goal:** Choose how you'll build the graph — by trying the top options on your hand-marked sample.

Run each candidate on the _same_ 5 reports and compare against your Phase 1 answer key. Judge on:
indexing cost, how much **control over the schema** you get (you need custom node/edge types and,
later, probability properties), and how easy it is to set up and understand.

| Option                                                      | What to weigh                                                                                                                                                                                         |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Microsoft GraphRAG (full)**                               | Most features (community summaries), most expensive, least control over a custom FMEA-style schema.                                                                                                   |
| **LightRAG / LazyGraphRAG**                                 | Much cheaper indexing; check how much custom-schema control each gives you.                                                                                                                           |
| **Custom extraction (Python + an LLM, no heavy framework)** | Most control and the most you'll learn; more work. Given you wanted the construction challenge and you'll want to inject probabilities later, this is the recommended default for a personal project. |

**Recommendation:** lean toward the custom route unless the comparison shows a framework saving you
serious time without taking away schema control. You learn the most, and nothing is hidden from you.

**GATE:** A decision (a few sentences + the comparison notes) on your build approach.

---

## Phase 4 — Build the extraction pipeline (the core work)

**Goal:** Turn raw LERs into schema-shaped entities and relationships.

1. **Work on the tiny sample only (5–10 reports). Don't scale yet.**
2. **Run a local model on your M1** (via Ollama) for development. Cost: ~$0. You'll run this many
   times while tuning — local keeps it free.
3. **Use a mixed extraction strategy:**
   - **Parse the structured fields directly** — the Form 366 header, the component-failure table
     (Cause/System/Component/Manufacturer codes), the "Component Failure Data" block. No LLM needed.
   - **LLM-extract the narrative** — Event Description, Cause, Corrective Actions — into your schema as JSON.
4. **Check against your hand-marked answer key.** Does it find the components, causes, and the
   causal chain you marked? Iterate the prompt until it's reliably close. Expect this to take the
   most time of any phase.

**GATE:** On a new sample report, extraction produces a graph fragment that matches your hand-sketch reasonably well.

---

## Phase 5 — Resolve entities and build the graph (de-risked by EIIS codes)

**Goal:** Merge duplicates and load a clean, connected graph into Neo4j.

1. **Entity resolution — easier here than usual.** Anchor `Component` and `System` nodes to their
   **EIIS codes** as canonical keys; that handles most of the "same thing, different wording"
   problem for free. For causes, normalize the free-text causes into a small set of shared
   categories (this is what creates the cross-document links).
2. **Load into Neo4j** (local, via Neo4j Desktop). Learn just enough Cypher to create nodes/edges
   and run a few traversals.
3. **Eyeball it in Neo4j Browser.** Visualize a couple of reports' subgraphs. Check the causal
   chains are connected end-to-end and the shared-cause nodes actually link different events.

**GATE:** The graph is connected, EIIS-keyed, and you can hand-trace at least two golden-question
paths in Cypher — including one that links two different reports through a shared cause.

---

## Phase 6 — Build retrieval + the vector baseline (the satisfying part)

**Goal:** Answer your golden questions from the graph, and prove to yourself the graph helps.

1. **Graph retrieval + answer generation.** For a question, traverse the relevant nodes/edges,
   pull the connected context, and have an LLM write the grounded answer.
2. **Build a plain vector RAG baseline** over the same LERs (chunk → embed → top-k → answer).
3. **Run both on your golden questions side by side.** This is the payoff: you'll see the graph
   win on the multi-hop and cross-document questions, and probably tie or lose on simple lookups —
   which is exactly the honest, interesting result. The "show every event from a weak
   maintenance/procedure program" question is your showcase, because vector search structurally
   cannot connect those reports.

**Verifying answers without being an expert:** for most golden questions, the correct answer is
_stated in the source LER itself_ (the cause, the chain, the corrective action are all written
down). So you can check correctness against the documents — no domain expertise required.

**GATE:** A working system that answers your golden questions, plus a side-by-side comparison
showing where the graph beats vector search. **This is a complete project. Everything below is bonus.**

---

## Phase 7 — Probabilistic layer (optional stretch — the PRA angle)

**Goal:** Add probability-weighted reasoning over the failure graph. Inviting but not required,
and you don't need to be a PRA expert to do a _demonstration_ version.

1. **Put weights on the causal edges** — rough conditional probabilities or frequencies. For a
   personal project, reasonable estimates or counts from how often a cause appears across your
   corpus are fine; you're demonstrating the idea, not certifying a reactor.
2. **Implement probabilistic path-finding** — "given this component degrades, what's the most
   probable path to a safety consequence?" Start simple (weighted shortest/most-probable path)
   before reaching for full Bayesian-network tooling like `pgmpy`.
3. **Honest scope note:** a _rigorous_ PRA model needs real failure-rate data and domain expertise.
   A _demonstration_ that ranks failure paths by likelihood is very achievable and is a great
   learning stretch. Frame it as the latter.

**GATE:** The system can return a probability-ranked failure path for at least a couple of questions.

---

## Phase 8 — Scale up and final index (the only phase that costs money)

**Goal:** Run your now-stable pipeline over the full 20–50 reports.

1. **Scale the corpus** now that everything works on the small set.
2. **If you want higher extraction quality than local models gave**, do one indexing run with a
   cheap hosted model (e.g. GPT-4o-mini) via the Batch API (~50% off). Preview token cost first.
   Otherwise, just run your local pipeline over everything for free.
3. **Re-run your golden questions** on the full graph — this is when the cross-document links get interesting.

**Cost reminder:** the danger was never one index run, it was re-indexing during development.
You've avoided that by keeping Phases 4–7 local and tiny. Realistic total spend for this project:
roughly **$0–20.**

**GATE:** Full graph built and your golden questions answered over the whole corpus.

---

## Phase 9 — Show it off (optional polish — great for a CS portfolio)

**Goal:** Make it presentable. This is portfolio gold for an undergrad.

1. **Visualize** a compelling failure chain and a cross-document link (Neo4j Browser, or a small
   web view with pyvis/D3).
2. **Write it up as a blog post or README** — what it does, the graph-vs-vector comparison, what
   you learned, what surprised you. The "when does the graph actually help" finding is the
   interesting story.
3. **Record a short demo** of 2–3 golden questions where the graph clearly beats vector search.

**GATE:** Something you'd be happy to link on a résumé or GitHub.

---

## Cross-cutting habits

- **Version control** code, prompts, schema, and your golden-question list from day one.
- **One narrow corpus, one schema** until it works — scope creep is the main risk for a solo project.
- **Log token counts** even on local runs, so cost never surprises you.
- **Keep your hand-marked answer key** — it's your cheapest, most reliable check throughout.

## Where the effort actually goes

| Phase                            | Effort                                  | Money       |
| -------------------------------- | --------------------------------------- | ----------- |
| 0–2 Frame, read, schema          | Medium (and where you learn the domain) | $0          |
| 3 Framework pick                 | Low                                     | ~$0         |
| 4–5 Extraction + graph build     | **Highest**                             | ~$0 (local) |
| 6 Retrieval + baseline           | Medium                                  | ~$0 (local) |
| 7 Probabilistic layer (optional) | Medium                                  | ~$0         |
| 8 Scale-up + final index         | Low                                     | **$0–20**   |
| 9 Writeup/demo (optional)        | Low–Medium                              | $0          |

The two heaviest phases are extraction (4) and the graph build / entity resolution (5) — though
the EIIS codes make resolution much gentler than it would be on messier data. Don't over-invest
in agonizing over the framework choice; the data work is where the real learning is.
