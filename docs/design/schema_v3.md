# LER Knowledge Graph — Schema v3 (refinements + cause-code reference)

Builds on Schema v2. Node and edge types are unchanged from v2 **except** the refinements
below. Use this alongside the v2 template.

---

## Block-13 Cause-Code legend (from NUREG-1022, LER Item 13 instructions)

| Code  | Category                                             | Definition                                                                                                                                                     |
| ----- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A** | Personnel Error                                      | Human error; not following procedures or accepted practice. _Excludes_ errors from following an incorrect procedure (those → D).                               |
| **B** | Design / Manufacturing / Construction / Installation | Defective materials/components, or failures traced to design, manufacture, construction, or installation.                                                      |
| **C** | External Cause                                       | Natural phenomena (lightning, tornado, flood) or offsite manmade causes.                                                                                       |
| **D** | Defective Procedure                                  | Inadequate or incomplete written procedures/instructions.                                                                                                      |
| **E** | Management / QA Deficiency                           | Inadequate management oversight or systems — incl. preventive-maintenance program, surveillance program, QA controls, inadequate root-cause/corrective-action. |
| **X** | Other                                                | Proximate cause unidentifiable or unclassifiable.                                                                                                              |

Rule on the form: enter the single code that most closely describes the **root cause**.

### Two-layer cause normalization

1. **Official code (coarse, authoritative):** the block-13 letter. This is the canonical
   `cause_code`, with the category label as `category`.
2. **Narrative theme (fine, optional):** a short free-text theme for richer cross-document
   linking (e.g. "PM-program weakness", "test-equipment fault").

Capture `proximate_text` verbatim too. **The official code governs the category** — do not
substitute a narrative inference. Licensees apply these six buckets with judgment, so codes can
be coarse or surprising; cross-document cause queries should use whichever layer the question needs.

### Worked classifications

- Quad Cities 254-2025-006: **A** (Personnel Error) — despite the "retired PM task" narrative.
- Limerick 353-2025-001: **B** (Design/Manufacturing) — degraded connector = defective component.
  _Not_ "inadequate maintenance program" (that would be E, which the licensee did not assign).
- Dresden 237-2025-003: provisional / TBD (investigation ongoing) — no code yet.

> Note: the genuine "inadequate maintenance/management program" cluster is the set of
> **E-coded** events. None of the three above is E.

---

## Refinements to v2

**Cause node** — properties are now: `cause_code` (A–E/X), `category` (label), `proximate_text`
(verbatim), `theme` (optional fine-grained), `provisional?`. `CAUSED_BY` points from the chain's
**origin failure** to this node — never to a bare code letter, never from the Consequence.

**Status** — driven directly by Form block 14 (Supplemental Report Expected): Yes →
`supplement-expected`, No → `final`. (Drop "preliminary".) Track `revision` (-00/-01) separately;
a later revision `REVISES` the earlier one.

**ENS** — three properties on the LER node: `ens_number`, `ens_date`, `ens_time`.

**System/Component identity & dedup** — do **not** key on "is it bracketed". Build a
`systems_components.csv` reference table mapping `{eiis_code (2-letter / IEEE-803A) ↔ acronym(s) ↔ full_name}`.
Resolve any surface form in the text (bracket code `[BJ]`, acronym `RCIC`, or full name) to the
canonical EIIS code via the table. The table is the dedup mechanism; the bracket code is just the
most reliable surface form when present. (Reminder: EIIS _system_ codes are two letters; "RCIC"
is an acronym, not an EIIS code.)

**RegulatoryReference `type`** — enum: `reporting-criterion` (e.g. 10 CFR 50.73(a)(2)(v)(D)) ·
`analysis-basis` (e.g. UFSAR Ch 15) · `standard/guidance` (e.g. NEI 99-02) ·
`license-basis` (TS / LCO / COLR).

**Section 7 (plain-English chain)** — standardize on **arrow notation** as primary
(root cause → A → B → consequence [backups available]); optional one-line prose gloss. The arrow
chain should match the `LEADS_TO` edges + `CAUSED_BY` origin + `BACKED_UP_BY` branch exactly.

---

## Reference data to build (status)

- **`plants.csv`** — DONE (starter: the BWR/HPCI MVP plants + sibling units). Thermal power filled
  only where LER-confirmed; populate the rest from each plant's LER "Plant and System Identification"
  line or UFSAR.
- **`systems_components.csv`** — TODO: EIIS code ↔ acronym ↔ full name (seed from NUREG-0544 / IEEE 805 & 803A).
- **Manufacturer code map** — TODO: 4-char code ↔ name (e.g. C770 ↔ Eaton/QualTech NP), grown as encountered.
