# Phase 4 — Extraction Pipeline (spec)

Turn raw LER text into validated **schema-v4.1 JSON**, one record per LER, scored against
`ground_truth.json`. This doc is the build spec — hand it to Claude Code alongside
`ler_schema_v4.1.md`, `ground_truth.json`, `systems_components.csv`, `plants.csv`, the raw LER
texts, and the golden questions (`phase_0.md`).

## What Phase 4 produces
One validated v4.1 JSON per LER, shaped exactly like a `ground_truth.json` record (identity,
reporting_basis, block_13, cause, nodes, edges, chain). That JSON is the artifact Phase 5 loads
into Neo4j. **EIIS resolution is pulled forward into Phase 4** (it's deterministic and cheap), so
the JSON handed to Phase 5 is already canonical.

## Model & cost
Extraction LLM = **Claude Sonnet via API**, temperature 0, behind a thin `llm.py` interface so it
can be swapped. At 2–5 documents the dev loop is cents, not the re-indexing cost the plan warns
about; keep the token-count logging habit anyway.

## Corpus status for this phase
We have **raw text for Dresden 2 and Limerick 2**, answer keys for all three marked LERs, and (now
that the APS API works) all three are fetchable by accession. Roles stay the same:
- **Quad Cities → few-shot exemplar** (full v4.1 answer key; used in the prompt, so held out of eval).
- **Dresden 2 + Limerick 2 → held-out eval set** (raw text + oracle both available).
Fetch all three plus any widening LERs with `fetch_ler.py`. To grow the eval set you must hand-mark
another LER (Browns Ferry 1 / Hatch 1 have text but no oracle yet).

---

## Confirmed NRC Form 366 block map (rev 04-2024, from the two real LERs)
Deterministic-parse targets:

| Block | Field | Notes |
|---|---|---|
| 1 | Facility Name | |
| 2 | Docket Number | raw varies (`00237` vs `353`) + a `050` prefix box → normalize to `05000XXX` |
| 3 | Page | |
| 4 | Title | |
| 5 | Event Date | MM DD YYYY — **authoritative for `event_date`** |
| 6 | LER Number | YYYY - NNN - RR |
| 7 | Report Date | MM DD YYYY |
| 8 | Other Facilities Involved | usually blank |
| 9 | Operating Mode | |
| 10 | Power Level | |
| 11 | Reporting criteria | 10 CFR checkboxes → `reported_under[]` |
| 12 | Licensee Contact | name + phone |
| 13 | Component Failure Data | columns `Cause \| System \| Component \| Manufacturer \| Reportable to IRIS`; up to two lines per row; **can be `TBD`** (Dresden) |
| 14 | Supplemental Report Expected | No→`final`, Yes→`supplement-expected` |
| 15 | Expected Submission Date | present when block 14 = Yes |
| 16 | Abstract | ≤1326 chars |
| 366A | NARRATIVE | free-form continuation sheet |

## API `content` — confirmed parsing notes (from ML26022A036, Dresden 2)
Verified against a real `Get Document` payload; the `content` text is far cleaner than PDF OCR
(EIIS brackets came through intact as `[BJ]`,`[IL]`,`[FU]`…).
- **Take identity from API metadata, not the content**, where available: `accession`←AccessionNumber,
  `docket`←DocketNumber[0] (already `05000237`), `report_date`←DocumentDate, `ler_number`←
  docket-short + DocumentReportNumber (`237` + `2025-003-00` = `237-2025-003-00`). `fetch_ler.py`
  writes these to `data/raw/manifest.csv`.
- **Parse from `content` only:** `event_date` (block 5 — DocumentDate is the *report* date, not the
  event date), operating_mode, power_level, block-13 codes, ENS number/date/time.
- **Reporting basis + supplement status: read from prose**, which is always present (cover letter,
  abstract, and narrative all state "10 CFR 50.73(a)(2)(v)(D)"; Dresden's narrative states a
  supplement is coming). Checkbox glyphs *do* survive as a fragile secondary signal — checked boxes
  render as `[g]`/`■`, unchecked as `□` — but don't rely on them alone.
- **Block 13 linearizes to one line:** `TBD BJ TBD TBD y  TBD BJ TBD TBD y` → split into groups of 5
  (one per component-failure row); `reportable` renders lowercase `y`.
- **Strip repeated 366A page boilerplate before sending the narrative to the LLM.** Each 366A page
  repeats the form header + OMB burden paragraph + facility/docket/LER-number block + `NARRATIVE`.
  Segment on markers: cover letter (before `NRC FORM 366`), Form-366 header (to `16. Abstract`),
  abstract (`16. Abstract` → first `NRC FORM 366A`), narrative (after, boilerplate removed).
- **Normalize whitespace / tolerate OCR-ish noise** (`50. 73`, `11 /17 /25`, `744 7`, `110.` for
  `10.`); use forgiving regexes.

## Findings from the real LERs that shape the design
These are the reasons the pipeline is built the way it is.

1. **Narrative section headers drift.** Dresden uses lettered sections (`A. CONDITIONS PRIOR`,
   `B. DESCRIPTION`, `C. CAUSE`, `D. SAFETY ANALYSIS`, `E. CORRECTIVE ACTIONS`, `F. PREVIOUS
   OCCURENCES`, `G. COMPONENT FAILURE DATA`); Limerick uses prose headers (`Description of the
   Event`, `Analysis of the Event`, `Safety Consequence`, `Cause of the Event`, `Corrective Actions
   Completed/Planned`, `Previous Similar Occurrences`, `Component Data`) — different labels, order,
   and lettering. **⇒ Extract the narrative semantically, never by hard-coded headers.**
2. **EIIS surface forms differ per report.** Dresden declares "codes are identified as [XX]" and
   brackets every system/component (`[BJ]`,`[IL]`,`[FU]`…). Limerick uses **no brackets** — full
   name + parenthetical acronym (`(RCIC)`,`(ADS)`,`(RHR)`,`(CS)`), with codes only in block 13 /
   `Component Data`. **⇒ The resolver must accept bracket codes, parenthetical acronyms, and full
   names, all mapped to `(eiis_code, type)`.** Limerick alone yields new acronyms to add
   grow-as-encountered: `CS→BM`, `RCIC→BN`.
3. **ADS confirmed code-less.** No bracket in Dresden, parenthetical only in Limerick, blank in
   block 13. **⇒ name-slug System node** (`eiis_code = null`, non-EIIS), exactly as schema v4.1 says.
4. **Cause code governs category — deterministic override.** QC block-13 code A (Personnel Error)
   diverges from a maintenance-heavy narrative; Limerick is B (defective component), not E
   (maintenance). **⇒ Set `cause_code`/`category` from block 13; the LLM only fills
   `proximate_text`/`theme`.**
5. **Provisional LERs exist.** Dresden's block 13 is `TBD` with a supplement expected. **⇒ Support
   `provisional`; don't penalize extraction for null codes there.**
6. **Stable plant facts aren't reliably in the narrative.** Dresden states "GE BWR, 2957 MWt";
   Limerick omits thermal power entirely. **⇒ Don't extract reactor type/vendor/thermal power —
   stamp docket and join `plants.csv` in resolution.**

## Abstract (block 16): decision
Empirically (both LERs), the abstract carries a clean event→cause→restoration spine plus reporting
basis (Limerick's even carries the ENS number), but it **systematically lacks EIIS codes, the
backup systems / `BACKED_UP_BY`, full timing, corrective-action detail, and previous occurrences** —
and nothing in it is absent from the narrative. So it can't be a primary source, but it's a cheap
anchor + completeness cross-check.

**Decision: feed the abstract as a labeled *secondary* input** alongside the narrative, with strict
instructions: it summarizes the *same single event* (do not mint duplicate entities); the narrative
is authoritative for codes/identifiers/detail; form blocks win on identity fields. Confirm with a
quick A/B (narrative-only vs narrative+abstract) on Dresden + Limerick against the oracle and keep
the winner — expected marginal-positive-to-neutral with Sonnet.

---

## Pipeline stages
1. **Ingest & segment** — load one LER text; split Form-366 header/blocks vs the 366A narrative +
   block-16 abstract. Header blocks are labeled enough for regex; the narrative is passed whole to
   the LLM (semantic, not header-split).
2. **Deterministic Form-366 parse** (`parse_form366.py`) → Pydantic object: identity, block-11
   criteria, block-13 rows, block-14 status, block-15 date, ENS (from narrative), block-16 abstract.
3. **LLM narrative extraction** (`extract_narrative.py`) → schema-constrained JSON for the semantic
   parts: FailureModes, `LEADS_TO` chain, `Cause.proximate_text`/`theme`, Consequences (+times),
   CorrectiveActions (+status), `BACKED_UP_BY` systems, `INVOLVES`, `SIMILAR_TO`. Inputs: narrative +
   labeled abstract + block-13 codes as grounding + one few-shot (QC). JSON/tool mode, Pydantic-
   validated retry on invalid.
4. **Merge, normalize, resolve** (`resolve.py`) — combine layers; stamp cause code from block 13;
   resolve every system/component surface form to `(eiis_code, type)` via `systems_components.csv`
   with name-slug fallback; join Unit facts from `plants.csv` on docket; validate against the v4.1
   Pydantic model; append unknown acronyms / manufacturer codes to grow-as-encountered logs.
5. **Score** (`score.py`) — compare to `ground_truth.json`; emit a per-LER + aggregate scorecard.
6. **Emit** one validated v4.1 JSON per LER → Phase 5 input.

## Repo layout
```
ler-graphrag/
  data/
    raw/            # LER text (APS content field), one file per LER
    reference/      # systems_components.csv, plants.csv
    ground_truth/   # ground_truth.json
  src/
    models.py            # Pydantic v4.1 schema (nodes/edges/record)
    parse_form366.py     # deterministic block parser
    extract_narrative.py # LLM extraction
    resolve.py           # EIIS resolution + plants.csv join + grow-logs
    pipeline.py          # orchestration
    score.py             # oracle scoring
    llm.py               # model interface (anthropic; ollama-swappable)
  prompts/
    narrative_extraction.md   # versioned
  logs/
    unknown_acronyms.csv  unknown_manufacturers.csv  tokens.csv
  tests/  scripts/run_extract.py  pyproject.toml
```
Libraries: `pydantic` v2, `anthropic`, `pandas`, `pytest`.

## EIIS resolution rules (`resolve.py`)
- Key every System/Component on `(eiis_code, type)`; **never `eiis_code` alone** (≈80 code-space
  collisions).
- Accept three surface forms: bracket `[BJ]`; parenthetical acronym `(RCIC)`; full name.
  Resolution order: exact code → acronym column → normalized-name match.
- Unknown acronym → resolve by name if possible, else flag; append to `unknown_acronyms.csv`.
- No EIIS code for a "system" (e.g., ADS) → name-slug System node, `eiis_code=null`, `non_eiis=True`.
- Component with a code but no code stated in-text (e.g., Limerick PCIV): allow resolver inference
  from description (`ISV`; accept `V`) and flag it as inferred.
- Manufacturer codes: map via a small code→name table, grow-as-encountered (`unknown_manufacturers.csv`).

## Scoring (`score.py`)
Canonicalize both extracted and ground-truth graphs to `match_key`, then report:
- **Node P/R/F1**, overall and per type. Coded nodes match on `Type:eiis_code[|identifier]`;
  un-coded nodes (FailureMode/Consequence/CorrectiveAction/Cause) match on `Type:name-slug` with
  **fuzzy/semantic name tolerance**.
- **Edge P/R/F1** on `(source.match_key, relation, target.match_key)` triples.
- **Identity/field checks** (exact): event_date, report_date, mode, power, status, revision, ENS
  fields, `reported_under`, SSFF.
- **Cause-code exact match** (category derived from code).
- **Tolerances:** Dresden provisional fields excluded from penalty; Limerick PCIV `ISV`≡`V`.

**Suggested gate (before scaling):** identity fields 100%; cause-code 100% on non-provisional;
edge-F1 ≥ 0.85 on Dresden + Limerick; every coded System/Component resolves.

## Extraction prompt notes (`prompts/narrative_extraction.md`, versioned)
- System: role + compact v4.1 rules — output JSON only; same single event; narrative authoritative
  for codes/detail; form blocks win on identity; don't invent nodes; use block-13 codes as given.
- User content, clearly labeled: `[FORM-366 FIELDS]` (deterministic parse incl. block-13 codes),
  `[ABSTRACT block 16]`, `[NARRATIVE 366A]`.
- Provide the Pydantic-derived JSON schema + the **Quad Cities** worked example as the single
  few-shot (we have its full answer key; it's not in the eval set).
- Emit `LEADS_TO`/`CAUSED_BY`/`BACKED_UP_BY` consistent with the §7 chain.

## Handing this to Claude Code
Good fit. Give it this file + `ler_schema_v4.1.md` + `ground_truth.json` +
`systems_components.csv` + `plants.csv` + the Dresden/Limerick raw texts + `phase_0.md`. Have it
`git init` and scaffold `models.py`, `parse_form366.py`, `resolve.py`, `score.py`, `llm.py`,
`pipeline.py`, and a first-draft prompt. **What Claude Code builds:** everything around the prompt,
with the scorer wired to the oracle. **What you own:** the extraction-prompt iteration loop, using
`score.py` for fast feedback — don't expect a one-shot good prompt. Convert the raw LER PDFs to
clean text first (prefer APS `content` over PDF OCR — the sample PDFs OCR'd `[BJ]` as `[B..1]`).

## Open items
- Pull QC (and Browns Ferry 1, Hatch 1) raw text via APS API to widen the Phase-4 sample and move
  QC from few-shot into the eval set.
- Abstract A/B — **DONE (locked: narrative-only).** 3 runs per arm on Dresden + Limerick:
  node F1 tied (~0.87), edge F1 higher and more stable without the abstract (0.73 vs 0.70; the
  abstract-off arm hit Limerick edge-F1 0.94 in all 3 runs vs the abstract-on arm bouncing
  0.81–0.94). The abstract carries nothing absent from the narrative and only added chaining
  variance, so it is dropped — `pipeline.py` defaults to `include_abstract=False`.
- Consider adding `ler INVOLVES <PCIV>` to the Limerick oracle (known answer-key gap: the PCIV
  component node is currently unconnected).

## Gate
On a held-out LER, extraction produces a v4.1 JSON whose graph fragment matches the hand-marked
oracle at the thresholds above, with all coded entities resolved and provisional cases handled.
