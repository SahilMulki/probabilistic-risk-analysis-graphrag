# Demo guide — asking your own questions

A practical, **comprehensive** guide to everything the system can answer, written for someone with
**no nuclear background.** Every question type is listed with a fill-in-the-blank **template** and a
real example, followed by a vocabulary of plants, systems, causes, and outcomes to drop in.

> **Which mode?** The [hosted demo](https://sahilmulki.github.io/probabilistic-risk-analysis-graphrag/)
> replays **8 precomputed example questions** — no typing. To ask **your own** questions (everything
> below), run **Live mode** locally (needs Neo4j + an Anthropic API key — see the README's _Run it
> yourself_). Live mode routes whatever you type to the right kind of query automatically, so exact
> wording doesn't matter — the templates are just a guide.

Short summary of the data: when something breaks at a U.S. nuclear plant, the operator files a
public **Licensee Event Report (LER)** describing what failed, why, and the safety impact. This system
has read **833 of them (2020–2026)** and turned them into a connected graph. Every answer below comes
_only_ from those reports, with citations.

**Placeholders** used in the templates (see the [Vocabulary](#vocabulary-you-can-plug-in) for values):
`<SYSTEM>` · `<PLANT>` · `<LER>` (a report number) · `<CAUSE>` · `<OUTCOME>` · `<COMPONENT>` · `<YEAR>`.

---

## 1. Questions about one specific report

**Trace the failure chain** — walk the cause → failure → consequence sequence inside a single report.

| Template                                                 | Example                                                                    |
| -------------------------------------------------------- | -------------------------------------------------------------------------- |
| `What chain of failures led to <OUTCOME> in LER <LER>?`  | _"What chain of failures led to the reactor trip in LER 382-2024-002-00?"_ |
| `What sequence of failures caused <OUTCOME> at <PLANT>?` | _"What sequence of failures caused the scram in LER 368-2020-001-00?"_     |

**Look up a fact about a report by its number** — pin one report and ask a specific field.

| Template                                                               | Example                                                                            |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `What was the root cause of the event in LER <LER>?`                   | _"What was the root cause of the event in LER 424-2025-001-00?"_                   |
| `Which nuclear plant reported LER <LER>?`                              | _"Which nuclear plant reported LER 382-2025-002-00?"_                              |
| `What is the subject of LER <LER>?`                                    | _"What is the subject of LER 366-2023-002-00?"_                                    |
| `What equipment was rendered inoperable in LER <LER>?`                 | _"What equipment was rendered inoperable in LER 391-2024-003-00?"_                 |
| `Under which 10 CFR 50.73 reporting criterion was LER <LER> reported?` | _"Under which 10 CFR 50.73 reporting criterion was LER 321-2021-001-00 reported?"_ |

**Find a report by describing what happened** — no ID needed; describe the event and get the plant.
_(This open-ended "find the report where X happened" search is the one category where plain vector
search beats the graph — the graph often declines it.)_

| Template                                       | Example                                                                                                |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `Which plant had <a described event>?`         | _"At which plant did directional drilling damage a DC control cable bundle and cause a reactor trip?"_ |
| `Which plant experienced <a described event>?` | _"Which plant experienced an automatic reactor trip caused by a lightning strike?"_                    |

---

## 2. Patterns across the whole corpus

These fuse **many** reports through something they share — the questions ordinary search can't assemble.

| Question type                                | Template                                                                                 | Example                                                                                         |
| -------------------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Components that failed in a system           | `What components have failed in the <SYSTEM> system across the whole corpus?`            | _"What components have failed in the RCIC system across the whole corpus?"_                     |
| How a system fails (failure modes)           | `Across all the reports, what failure modes has the <SYSTEM> system had?`                | _"Across all the reports, what failure modes has the HPCI system had?"_                         |
| Events with a backup available               | `Which events were mitigated by a redundant safety system being available?`              | _(same)_                                                                                        |
| Cause-category distribution                  | `What is the distribution of cause categories across all the reports?`                   | _(same)_                                                                                        |
| Weak-program / personnel-error events        | `Which events trace back to a weak maintenance or procedure program (<CAUSE>)?`          | _"Which events across all these plants trace back to a weak maintenance or procedure program?"_ |
| Shared component **and** cause across plants | `Find events at different plants that share both a common component and a common cause.` | _(same)_                                                                                        |

---

## 3. Probabilistic / risk questions

The risk layer reports **observed frequencies within this corpus** — always with the distribution, the
event count, and honest caveats (never a bare "failure rate").

| Question type                       | Template                                                                                | Example                                                                                                                                            |
| ----------------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Rank systems (or causes) by risk    | `Which systems contribute the most observed risk across the corpus?`                    | _"Which systems contribute the most observed risk across the whole corpus?"_                                                                       |
| Most likely outcome for a system    | `What safety outcome is most likely when the <SYSTEM> system is involved in an event?`  | _(same)_                                                                                                                                           |
| Most likely outcome for a cause     | `What safety outcomes most often result from <CAUSE> events?`                           | _"What safety outcomes most often result from personnel-error events across the corpus?"_                                                          |
| Most-probable failure path          | `What is the most probable cause-to-outcome failure path for the <SYSTEM> system?`      | _"What is the most probable cause-to-outcome failure path for the HPCI system?"_                                                                   |
| Most-probable path from a component | `Given a <COMPONENT> degrades, what is the most probable path to a safety consequence?` | _"Given a relay degrades, what is the most probable path to a safety consequence?"_                                                                |
| **The honesty test**                | `What is the failure rate of the <SYSTEM> system?`                                      | _"What is the failure rate of the HPCI system?"_ → it **declines** the "rate" framing and gives observed frequencies with the denominator instead. |

---

## 4. Flexible counting — the general engine

One engine handles "among the events matching some **conditions**, count / compare / trend a
**facet**." These are the shapes it covers:

| Shape                                            | Template                                                                     | Example                                                                                   |
| ------------------------------------------------ | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Reverse** (outcome → what caused it)           | `Which systems / components / causes appear most often in <OUTCOME> events?` | _"Which systems appear most often in loss-of-safety-function events?"_                    |
| **Compound** (multiple conditions, all required) | `What components fail in <CAUSE> events that led to <OUTCOME>?`              | _"What components fail in personnel-error events that led to a loss of safety function?"_ |
| **Combination** (which set together produced X)  | `What combination of components have produced <OUTCOME>?`                    | _"What combination of components have produced fuel cladding failures?"_                  |
| **Co-occurrence / pairs**                        | `Which pairs of components most often co-occur in <OUTCOME> events?`         | _"Which pairs of components most often co-occur in reactor-trip events?"_                 |
| **Temporal trend** (by year)                     | `How many <OUTCOME> events happened each year across the corpus?`            | _"How many reactor trips happened each year across the corpus?"_                          |
| **Numeric threshold**                            | `What outcomes occur in events that happened above <N>% power?`              | _"What outcomes occur in events that happened above 90% power?"_                          |
| **Corrective actions / resolutions**             | `How were <CAUSE> events resolved?`                                          | _"How were personnel-error events resolved?"_                                             |
| **Side-by-side comparison**                      | `Compare the outcome profiles of <SYSTEM> and <SYSTEM>.`                     | _"Compare the outcome profiles of HPCI and RCIC."_                                        |
| **Plant counts**                                 | `Which plants had the most <OUTCOME> events?`                                | _"Which plants had the most reactor trips?"_                                              |

---

## 5. The honest edges

| Behavior                                   | Template                                       | Example                                                                                                                                                   |
| ------------------------------------------ | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Refuses** out-of-corpus questions        | `What caused <a non-U.S. or fictional event>?` | _"What caused the reactor explosion at Chernobyl?"_ · _"…at Fukushima Daiichi?"_ · _"…at the Springfield Nuclear Power Plant?"_ — all correctly declined. |
| **Asks you to narrow down** when ambiguous | `What caused an event at <PLANT>?`             | _"What caused an HPCI event at Browns Ferry?"_ → many reports match, so it asks for an LER number or year instead of guessing.                            |

---

## Vocabulary you can plug in

### Plants — `<PLANT>` (a sample that appears in the data)

Browns Ferry · Limerick · Hatch · Dresden · Quad Cities · Cooper · Fermi · Susquehanna · Monticello ·
FitzPatrick · Peach Bottom · Perry · Vogtle · Watts Bar · Oconee · Davis-Besse · Millstone · South
Texas · Wolf Creek · Arkansas Nuclear

_(Just use the plant name in a question. Big plants have many reports, so a plant-only question may ask
you to narrow down — see the "clarify" example above.)_

### Systems — `<SYSTEM>` (name them plainly or by acronym)

The data is richest in **boiling-water-reactor (BWR)** emergency systems:

- **HPCI** — High-Pressure Coolant Injection (emergency water into the core at high pressure)
- **RCIC** — Reactor Core Isolation Cooling
- **RHR** — Residual Heat Removal
- **Core Spray** (HPCS / LPCS) — sprays cooling water onto the reactor core
- **ADS** — Automatic Depressurization System
- **EDG** — Emergency Diesel Generators (backup electrical power)

Pressurized-water-reactor (PWR) plants also appear, e.g. **AFW** — Auxiliary/Emergency Feedwater.

### Components — `<COMPONENT>` (examples)

relay · motor-operated valve (MOV) · circuit breaker · pump · battery / battery cell · transformer ·
fuse · diesel generator · transfer switch · control cable · connector.

### Root-cause categories — `<CAUSE>` (the official NRC codes)

| Code | Category                              |
| ---- | ------------------------------------- |
| A    | Personnel Error                       |
| B    | Design / Manufacturing / Installation |
| C    | External Cause                        |
| D    | Defective Procedure                   |
| E    | Management / QA Deficiency            |
| X    | Other                                 |

_Use the phrase, e.g. "personnel-error events" or "events caused by a defective procedure."_

### Safety outcomes — `<OUTCOME>`

reactor trip / scram · loss of safety function · a safety system made inoperable · emergency-system
actuation (ECCS / AFW / diesel start) · containment isolation · a degraded (but still working)
condition · a technical-specification violation · loss of offsite power · fuel cladding failure.

### Referring to a specific report — `<LER>`

An LER number looks like **`353-2025-001-00`** = _docket–year–sequence–revision._ A few real ones to
try: `353-2025-001-00` (Limerick), `237-2025-003-00` (Dresden), `254-2025-006-00` (Quad Cities),
`424-2025-001-00` (Vogtle), `382-2024-002-00`, `368-2020-001-00`.

---

## Tips

- **Anchor your question** on at least one concrete thing — a system, a plant, a cause, an outcome, a
  component, or an LER number. That's what the system latches onto.
- **Phrasing is flexible.** "How does HPCI fail?", "what usually goes wrong with HPCI", and "HPCI
  failure modes" all land in the same place.
- **Ambiguous?** If a question matches many reports, the system asks you to narrow down instead of
  guessing — add an LER number or a year.
- **Out of scope?** Questions about non-U.S. events (Chernobyl, Fukushima) or anything not in the
  reports are refused honestly, rather than answered with a guess.
