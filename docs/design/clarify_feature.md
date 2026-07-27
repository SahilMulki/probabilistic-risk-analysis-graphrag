# Feature — abstain / clarify when a question is ambiguous

**Goal (roadmap.md):** Phase 6 handled two outcomes — *empty → refuse* (the NEG test) and
*single clear subject → answer*. It did **not** handle **ambiguity**: a single-subject
question matching *multiple* candidate events. Silently answering one of them is a guess.
This feature adds a third outcome so the system **asks to disambiguate** instead.

**Status:** complete; the golden suite is now **12/12** end-to-end against the live Neo4j
graph (the original 9 unchanged + 3 new: `CLARIFY`, `CLARIFY-RESOLVED`, `ADV-AGG`).

Artifacts touched: [src/retrieve.py](src/retrieve.py), [src/answer.py](src/answer.py),
[src/golden_eval.py](src/golden_eval.py), [src/ask.py](src/ask.py).

---

## Design — three-way outcome, detected structurally in the retriever

`retrieve()` now returns **`Evidence | Clarification`** (`Refusal` is `Evidence(empty=True)`).
Detection lives in the **retriever**, by **candidate cardinality** — *not* in the answer LLM.
Letting the answerer pick which event you meant *is* the guessing we prevent.

For a **single-subject intent** (`SINGLE_SUBJECT_INTENTS = {"failure_chain"}`):

| candidate count | outcome |
|---|---|
| 0 | **Refusal** (empty evidence → answerer says "not in this corpus") |
| 1 | **Answer** (proceed as before) |
| >1 | **Clarification** (ask which one) |

**Candidate set (crisp definition):** *the non-stub LERs matching **ALL pinned anchors** for
that intent* — LER number (exact), plant (substring), system (membership). Resolved in
`GraphRetriever._candidate_lers()`; the cardinality branch lives in `_t_failure_chain()`.

Detection is **intent-aware**. Aggregate intents (`system_components`, `cause_distribution`,
`mitigating_backups`, `system_failure_modes`, `weak_program_events`, `shared_component_cause`)
are *meant* to span events, so they are exempt — they never clarify.

## The linchpin: intent classification

The feature's correctness rests entirely on the router's **single-subject vs aggregate** call:
a misclassified aggregate → *wrongly clarifies*; a misclassified single-subject → *silently
answers over multiple events* (the exact failure we prevent). Two defenses:

1. The router intent descriptions were sharpened at the boundary — `failure_chain` is "a
   *single-event* 'what caused / what led to X' question … use this even when only a system is
   named; if several events match, the system asks to disambiguate"; `system_failure_modes` is
   explicitly the **AGGREGATE** ("group all events", "most common", "across all reports").
2. `golden_eval` gained an **adversarial intent** case (`ADV-AGG`) that sits near the boundary
   and asserts the **routed intent**, not just the answer.

## Clarify UX — single-shot (locked)

Return the candidates; the user re-asks. Chosen over an interactive pick-loop for a real
reason beyond simplicity: it keeps the retriever **stateless**, so a `Clarification` is a
**deterministic structured return** that `golden_eval` asserts on (we assert the *candidate
set*, not the prose question).

**Load-bearing caveat:** single-shot only works if the re-ask is *resolvable*. Same-plant
events can differ only by date/title, which the router can't anchor on — so the **primary
re-ask path is by LER number** (shown in the candidate list, unambiguous). Verified
end-to-end: *"What caused HPCI inoperability?"* → clarifies across 3 events; *"…in LER
254-2025-006-00"* → router extracts `ler_key`, resolves to one, and answers. The path does
**not** dead-end.

## Candidate presentation

A `Clarification` returns a short question + candidates with distinguishing `:LER` fields
(**LER# · source · event date · systems · title**):

- **Sorted by `event_date` descending** — likely-intended (recent) events first.
- **Capped at `CANDIDATE_CAP = 8`** (never triggers at N=3).
- **On overflow, no silent "…and M more" hiding** — hidden candidates are unreachable in
  single-shot, so we tell the user how to narrow (add a year / use an LER#).

## Backstop in the answer layer

Defense-in-depth: `answer.py` appends a single-event instruction when `ev.intent ∈
SINGLE_SUBJECT_INTENTS` — if the evidence somehow spans multiple LERs, refuse-and-ask rather
than merge or pick one. The retriever already prevents this, so it's belt-and-suspenders.

## Eval

`golden_eval.judge()` is the single decision entry over both outcome types:

| id | kind | asserts |
|---|---|---|
| `CLARIFY` | clarify | *"What caused HPCI inoperability?"* → **Clarification**, `failure_chain`, candidate set = the 3 HPCI LERs |
| `CLARIFY-RESOLVED` | showcase | *"…at Quad Cities"* → **Answer**, one LER (the disambiguated re-ask) |
| `ADV-AGG` | intent | *"Across all reports, what failure modes has HPCI had?"* → routes to `system_failure_modes`, **no false clarify** |

`clarify` kind asserts the offered candidate set covers the expected events; `intent` kind
asserts the routed intent and that it did **not** misfire a clarification.

**Mechanism-test honesty:** the N=3 case is a *no-plant* mechanism test, not the realistic
same-plant ambiguity (which becomes real at scale). Don't over-tune to it; add same-plant
multi-event cases in Phase 8.

## How to run
```
python src/ask.py "What caused HPCI inoperability?"                    # -> clarifies
python src/ask.py "What caused HPCI inoperability in LER 254-2025-006-00?"  # -> answers
python src/ask.py --golden --brief                                     # 12/12
```

## Scope / non-goals
Ambiguity is **structural** (candidate cardinality), not a confidence score on answer
*content*. Deterministic and robust by construction.
