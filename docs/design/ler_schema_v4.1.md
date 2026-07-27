# LER Knowledge Graph — Schema v4.1 (consolidated)

Single source of truth. Supersedes v2, v3, and v4. Built and pressure-tested by hand-marking
Quad Cities 1, Dresden 2, and Limerick 2 (see `answer_key.md` for the worked ground truth).

**v4.1 changes.** (1) `systems_components.csv` is built and DONE (from IEEE 805-1984 + IEEE 803.1-1992,
not NUREG-0544). (2) System nodes may fall back to a **name-slug** key when no EIIS system code exists
(e.g. ADS). (3) Dedup keys on `(eiis_code, type)` because the system and component code spaces overlap.

---

## Node types

| Type | Key (identity) | Properties |
|---|---|---|
| **LER** | LER number (e.g. `254-2025-006-00`) | accession_number, docket, plant_name, unit, event_date (ISO, form block 5), report_date, operating_mode, power_level, reporting_criteria[], ens_number, ens_date, ens_time, status {final \| supplement-expected}, revision, discovery_context {surveillance test \| operability test \| normal operation \| inspection}, title |
| **Unit** | docket (`05000254`) | plant_name, unit, reactor_type, nss_vendor, thermal_power_mwt *(from plants.csv)* |
| **System** | eiis_code (2-letter, IEEE 805) if present, else **name-slug** | display_name, eiis_code?, provisional? *(name-slug + eiis_code=null when no EIIS system code exists — e.g. ADS)* |
| **Component** | eiis_code if present, else name-slug | display_name, eiis_code?, identifier (e.g. `1-2301-3`), manufacturer_code? |
| **FailureMode** | name-slug | description |
| **Cause** | normalized category | cause_code (A–E/X), category (label), proximate_text (verbatim), theme (optional, fine-grained), provisional? |
| **Consequence** | name-slug | description, start_time, end_time, duration |
| **CorrectiveAction** | name-slug | description, status {completed \| planned} |
| **Manufacturer** | manufacturer_code or name | name, code |
| **RegulatoryReference** | citation | type {reporting-criterion \| analysis-basis \| standard/guidance \| license-basis} |

*(Previous-occurrence references are `LER` nodes — stub if outside the corpus.)*

## Edge types

| Relation | Direction | When to use |
|---|---|---|
| `OCCURRED_AT` | LER → Unit | always |
| `INVOLVES` | LER → System / Component | systems/components the event concerns |
| `LEADS_TO` | FailureMode → FailureMode / Consequence | forward propagation chain (earlier → later), incl. terminal step into the Consequence |
| `CAUSED_BY` | origin FailureMode → Cause | once per chain; effect → cause; target is the Cause node (never a bare code) |
| `MITIGATED_BY` | LER → CorrectiveAction | one edge per corrective action |
| `BACKED_UP_BY` | Consequence → System | systems stated operable/available during the inoperability |
| `REPORTED_UNDER` | LER → RegulatoryReference | the 10 CFR criterion (NOT the ENS number — that's a property) |
| `MANUFACTURED_BY` | Component → Manufacturer | when manufacturer given |
| `PART_OF` | Component → Component / System | **explicit-only**, never inferred |
| `SIMILAR_TO` | LER → LER | previous similar occurrence |
| `REVISES` | LER (-01) → LER (-00) | supplements |

---

## Conventions

**Naming & dedup.** Resolve every system/component mention — bracket code `[BJ]`, parenthetical
acronym `(RCIC)`, or full name — to a canonical entry via the `systems_components.csv` reference
table. The table is the dedup mechanism; the bracket code is the most reliable surface form when
present. **Key on `(eiis_code, type)`, never `eiis_code` alone** — the system and component code
spaces overlap on ~80 two-letter strings (e.g. `IL` = Radiation Monitoring *system* vs Indicator
Light *component*; `BM` = Low Pressure Core Spray *system* vs Brine Maker *component*), so the
`type` column is what disambiguates (collisions are flagged in the table's `notes`). EIIS *system*
codes are two letters (IEEE 805); component function identifiers are 1–4 chars (IEEE 803.1).
Acronyms like RCIC/HPCI are **not** EIIS codes — they live in the table's `acronyms` column, which
**grows as encountered** from the LER corpus (LERs define acronym↔name↔code on first use).

**Name-slug System nodes (v4.1).** Some things an LER calls a "system" have no EIIS system code —
notably **ADS** (Automatic Depressurization System), which is a function/subsystem, not a coded
IEEE 805 system. Assert `eiis_code` when the mention resolves in `systems_components.csv`; otherwise
key the System node by **name-slug** (mirroring the `Component` fallback), set `eiis_code = null`,
and flag `provisional`/non-EIIS. Never force-fit such a node onto the nearest EIIS code — that would
corrupt cross-document dedup. (By contrast, RHR and LPCI legitimately *share* one EIIS code, `BO`,
and correctly collapse to a single System node.)

**LEADS_TO vs CAUSED_BY.** `LEADS_TO` runs the forward propagation chain. `CAUSED_BY` is used once,
from the chain's origin failure to the single Cause node (effect → cause). The §7 arrow chain must
match the LEADS_TO edges + CAUSED_BY origin + BACKED_UP_BY branch.

**PART_OF** — assert only when the LER literally states containment; never infer. Connectivity comes
from the LEADS_TO chain and LER-level INVOLVES, not a forced part hierarchy.

**Cause (two-layer normalization).** (1) Official block-13 code = canonical `cause_code` + `category`
label. (2) Optional free-text `theme` for finer cross-document linking. Capture `proximate_text`
verbatim. The official code governs the category — do not substitute a narrative inference.

Block-13 Cause-Code legend (NUREG-1022, Item 13):

| Code | Category | Definition |
|---|---|---|
| A | Personnel Error | Human error; not following procedures/accepted practice. (Errors from following an *incorrect* procedure → D.) |
| B | Design / Manufacturing / Construction / Installation | Defective materials/components or design/manufacture/construction/installation failures. |
| C | External Cause | Natural phenomena (lightning, tornado, flood) or offsite manmade. |
| D | Defective Procedure | Inadequate/incomplete written procedures or instructions. |
| E | Management / QA Deficiency | Inadequate management oversight/systems — incl. PM program, surveillance program, QA, inadequate root-cause/corrective-action. |
| X | Other | Proximate cause unidentifiable/unclassifiable. |

Rule on the form: enter the single code that best describes the **root cause**. (Note: licensees
apply these six coarse buckets with judgment — codes can diverge from the narrative emphasis, so
trust the code for `category` and capture the nuance in `theme`/`proximate_text`.)

**Status** — from Form block 14 (Supplemental Report Expected): Yes → `supplement-expected`,
No → `final`. Track `revision` (-00/-01) separately; a later revision `REVISES` the earlier.

**ENS** — three LER properties: `ens_number`, `ens_date`, `ens_time`.

**RegulatoryReference type** — `reporting-criterion` (10 CFR 50.73…) · `analysis-basis` (UFSAR Ch 15)
· `standard/guidance` (NEI 99-02) · `license-basis` (TS / LCO / COLR).

**§7 plain-English chain** — standardize on arrow notation (root cause → A → B → consequence
[backups available]); optional one-line prose gloss.

---

## Extraction approach (Phase 4 constraints)
- Parse the **Form 366 blocks deterministically** (identity, block 11 criteria, block 13 codes, dates).
- Extract the **366A narrative semantically** (by meaning, not by hard-coded section headers — titles drift).
- Stable plant facts (reactor type, vendor, thermal power) come from `plants.csv`, not per-LER extraction.

## Reference data (status)
- **`plants.csv`** — DONE (all 95 operating units). PWR vendors + most thermal powers to backfill from NUREG-1350 App. A.
- **`systems_components.csv`** — DONE: 1,055 rows (200 EIIS systems + 855 component function identifiers), columns `eiis_code, type, canonical_name, reactor_scope, acronyms, source_standard, notes`. Built from **IEEE 805-1984** (systems) + **IEEE 803.1-1992** (components) — *not* NUREG-0544, which is only an NRC abbreviations dictionary. `acronyms` is seeded for common systems and **grows as encountered**; ~80 code-space collisions are flagged in `notes`.
- **Manufacturer code map** — grow-as-encountered (no complete public source; EPIX-maintained). Low priority.

---

## TEMPLATE (copy per LER)

### 1. Identity
- Accession # · LER # · Plant / Unit · Docket (05000XXX) · Event date (YYYY-MM-DD, block 5) · Report date · Operating mode / Power · Discovery context · Status + revision · ENS number / date / time · Title
- *(Reactor type/vendor: from plants.csv)*

### 2. Reporting basis
- Reported under 10 CFR §(s) · SSFF? (Y / N / not stated)

### 3. Block 13 (one row each)
| Cause code | System code | Component code | Manufacturer | Reportable |
|---|---|---|---|---|

### 4. Cause
- Proximate (verbatim) · cause_code · category · theme (optional) · provisional?

### 5. Nodes
| Type | display_name | eiis_code (resolve via table) | properties | source |

### 6. Edges (one triple/row)
| Source | Relation | Target | Evidence |

### 7. Plain-English chain (arrow notation)

### 8. Notes / ambiguities / extraction-difficulty flags

### 9. Golden questions this LER helps answer
