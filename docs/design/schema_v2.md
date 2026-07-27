# LER Knowledge Graph — Schema v2 & Hand-Marking Template

Supersedes the Phase 2 draft schema. Consolidates the decisions resolved while hand-marking
Dresden 2 (237/2025-003-00) and Limerick 2 (353/2025-001-00). Use this template going forward.

---

## Decisions log

1. **Node renamed `Event` → `LER`**, keyed by LER number.
2. **One edge = one (source, relation, target) triple.** Never pack multiple targets into one edge.
3. **Dates** stored ISO `YYYY-MM-DD`; use the Form 366 block-5 date as canonical event date.
4. **Docket** canonicalized to 8 digits (`05000353`) as the Unit key.
5. **`LEADS_TO`** = forward propagation chain (earlier→later), including the terminal step into the
   Consequence. **`CAUSED_BY`** used once, from the chain's origin failure → the one normalized Cause
   (effect→cause). `RESULTS_IN` dropped.
6. **EIIS code is the node key** for systems/components when present in the text; else key on a
   normalized name-slug. Only fill `eiis_code` when it's bracketed in the LER.
7. **`PART_OF` is explicit-only** — assert only when the LER literally states containment; never infer.
8. **Manufacturer is a node** with a `manufacturer_code` property.
9. **Previous occurrences** modeled as a `SIMILAR_TO` edge (stub node if the referenced LER is
   outside the corpus).
10. **Backups are `System` nodes**; the backup role is the `BACKED_UP_BY` edge, not a node type.
11. **ENS notification** is a property on the LER (`ens_number`, `ens_time`), not a `REPORTED_UNDER` edge.
12. **Timing** (inoperable from/to/duration) is a property of the **Consequence** node.
13. **Reactor type / vendor / thermal power** come from an external `plants.csv` keyed by docket,
    NOT per-LER extraction.
14. **Narrative extraction is semantic**, not header-driven. Form 366 blocks parsed deterministically;
    366A narrative extracted by meaning.
15. **Provisional / ongoing investigations**: LER gets a `status`; TBD nodes flagged `provisional`;
    supplements modeled as a new revision via `REVISES`.
16. **Cause normalization** anchored to the Form 366 block-13 Cause code (controlled vocabulary;
    legend in full NUREG-1022). Capture proximate cause (verbatim) + normalized category, and
    distinguish proximate vs programmatic cause.
17. **Discovery context** (`surveillance test` / `operability test` / `normal operation` / `inspection`)
    is a property on the LER, not an Activity node.

---

## Node types (v2)

| Type                    | Key (identity)                             | Properties                                                                                                                                                                                                                         |
| ----------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **LER**                 | LER number (e.g. `254-2025-006-00`)        | accession_number, docket, plant_name, unit, event_date, report_date, operating_mode, power_level, reporting_criteria[], ens_number, ens_time, status {preliminary\|final\|supplement-expected}, revision, discovery_context, title |
| **Unit**                | docket (`05000254`)                        | plant_name, unit, reactor_type, vendor, thermal_power_MWt _(from plants.csv)_                                                                                                                                                      |
| **System**              | eiis_code (e.g. `BJ`)                      | display_name                                                                                                                                                                                                                       |
| **Component**           | eiis_code if present, else name-slug       | display_name, eiis_code?, identifier (e.g. `1-2301-3`), manufacturer_code?                                                                                                                                                         |
| **FailureMode**         | name-slug                                  | description                                                                                                                                                                                                                        |
| **Cause**               | normalized category                        | proximate_text (verbatim), category, cause_code (block-13, e.g. A/B), provisional?                                                                                                                                                 |
| **Consequence**         | name-slug                                  | description, start_time, end_time, duration                                                                                                                                                                                        |
| **CorrectiveAction**    | name-slug                                  | description, status {completed\|planned}                                                                                                                                                                                           |
| **Manufacturer**        | manufacturer_code or name                  | name, code                                                                                                                                                                                                                         |
| **RegulatoryReference** | citation (e.g. `10 CFR 50.73(a)(2)(v)(D)`) | type {reporting-criterion\|analysis-basis\|standard}                                                                                                                                                                               |

_(Previous-occurrence references are just `LER` nodes — stub them if outside the corpus.)_

## Edge types (v2)

| Relation          | Direction                               | When to use                                                |
| ----------------- | --------------------------------------- | ---------------------------------------------------------- |
| `OCCURRED_AT`     | LER → Unit                              | always                                                     |
| `INVOLVES`        | LER → System / Component                | the systems/components the event concerns                  |
| `LEADS_TO`        | FailureMode → FailureMode / Consequence | forward propagation chain (earlier → later)                |
| `CAUSED_BY`       | origin FailureMode → Cause              | once per chain; effect → cause                             |
| `MITIGATED_BY`    | LER → CorrectiveAction                  | one edge per corrective action                             |
| `BACKED_UP_BY`    | Consequence → System                    | systems stated operable/available during the inoperability |
| `REPORTED_UNDER`  | LER → RegulatoryReference               | the 10 CFR criterion (NOT the ENS number)                  |
| `MANUFACTURED_BY` | Component → Manufacturer                | when manufacturer is given                                 |
| `PART_OF`         | Component → Component / System          | **explicit-only**, never inferred                          |
| `SIMILAR_TO`      | LER → LER                               | previous similar occurrence                                |
| `REVISES`         | LER (rev -01) → LER (rev -00)           | supplements                                                |

## Reference data to build once

- **`plants.csv`** keyed by docket → reactor_type, vendor, thermal_power, location.
- **Manufacturer code map** (e.g. `C770` → Eaton/QualTech NP), grown as you encounter codes.
- **Block-13 Cause-code legend** extracted from the full NUREG-1022 → canonical cause categories.
