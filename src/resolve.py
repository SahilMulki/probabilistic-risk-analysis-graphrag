"""
resolve.py — merge the deterministic parse with the LLM narrative extraction,
canonicalize every System/Component to its EIIS `(eiis_code, type)` key, join
stable plant facts from plants.csv, and emit a validated v4.1 `LERRecord`.

This is the layer that makes the JSON handed to Phase 5 canonical:

  * Deterministic fields win. identity / reporting_basis / block_13 /
    cause_code+category are copied from `Form366Parse` over whatever the LLM
    echoed, and the Cause/Unit nodes are re-stamped to match.
  * EIIS resolution. The LLM records a bracket code when the text shows one
    ("[BJ]") and otherwise leaves `eiis_code` null with the name/acronym in
    `display_name`; here we resolve the three surface forms — bracket code,
    parenthetical acronym "(RCIC)", full name — to a canonical code via
    systems_components.csv, keyed on `(code, type)` (never code alone).
  * Grow-as-encountered. Acronyms learned by name-resolution and truly unknown
    ones are appended to logs/ so the reference tables can grow from the corpus.
  * Name-slug fallback. Code-less "systems" (ADS) and descriptive components
    (cannon plug, ECCS test unit) stay name-slug with eiis_code=null.

The final `LERRecord.model_validate` also enforces edge integrity, so a bad LLM
graph (edge to a non-existent node, illegal enum) raises here and the pipeline
retries the LLM.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rapidfuzz import fuzz, process

from models import LERRecord

REPO_ROOT = Path(__file__).resolve().parent.parent

# Acronyms confirmed from the corpus but not yet seeded in systems_components.csv
# (grow-as-encountered; docs/journal/phase_4.md calls these out for Limerick).
SEED_SYS_ACRONYMS: dict[str, str] = {"CS": "BM"}     # Core Spray -> Low Pressure Core Spray

# Normalized full-name aliases for systems that are genuinely ambiguous by fuzzy
# name alone (grow-as-encountered). "Core Spray" fuzzy-ties BM (Low Pressure Core
# Spray) and BG (High Pressure Core Spray); in the BWR ECCS context of these LERs
# a bare "Core Spray" is the low-pressure system, BM.
SEED_SYS_NAME_ALIASES: dict[str, str] = {"core spray": "BM"}

# Code-less "systems" we expect to fall back to name-slug — do not log as unknown.
_KNOWN_CODELESS = ("depressurization",)              # ADS

# Canonical display for code-less systems, so the name-slug match_key is stable
# across documents no matter how the LLM phrased it ("ADS" vs "... (ADS)").
_NAME_SLUG_CANON: dict[str, str] = {
    "ads": "Automatic Depressurization System",
    "automatic depressurization": "Automatic Depressurization System",
}

# Codeless components resolvable from their description (flagged inferred_code).
_COMPONENT_INFERENCE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"primary containment isolation valve|pciv", re.I), "ISV"),
    (re.compile(r"\bisolation valve\b", re.I), "ISV"),
]

# Manufacturer code -> name (grow-as-encountered; low priority).
SEED_MANUFACTURERS: dict[str, str] = {"C770": "Eaton / QualTech NP"}

_NAME_MATCH_THRESHOLD = 88


# --------------------------------------------------------------------------- #
# reference data
# --------------------------------------------------------------------------- #
@dataclass
class RefData:
    by_code_type: dict[tuple[str, str], dict]
    sys_acronyms: dict[str, str]
    comp_acronyms: dict[str, str]
    sys_names: list[tuple[str, str]]              # (normalized_name, code)
    plants: dict[str, dict]


def _norm_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\(\{][^)}]*[\)\}]", " ", s)      # drop parentheticals / acronyms
    s = re.sub(r"\b(system|subsystem|the|unit)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_refs(ref_dir: Optional[Path] = None) -> RefData:
    ref_dir = ref_dir or (REPO_ROOT / "data" / "raw" / "reference")
    by_code_type: dict[tuple[str, str], dict] = {}
    sys_acronyms: dict[str, str] = {}
    comp_acronyms: dict[str, str] = {}
    sys_names: list[tuple[str, str]] = []

    with (ref_dir / "systems_components.csv").open() as f:
        for row in csv.DictReader(f):
            code, typ = row["eiis_code"].strip(), row["type"].strip()
            by_code_type[(code, typ)] = row
            acro_map = sys_acronyms if typ == "system" else comp_acronyms
            for a in (row.get("acronyms") or "").split("|"):
                a = a.strip().upper()
                if a:
                    acro_map.setdefault(a, code)
            if typ == "system":
                sys_names.append((_norm_name(row["canonical_name"]), code))

    for a, code in SEED_SYS_ACRONYMS.items():
        sys_acronyms.setdefault(a, code)

    plants: dict[str, dict] = {}
    with (ref_dir / "plants.csv").open() as f:
        for row in csv.DictReader(f):
            plants[row["docket"].strip()] = row

    return RefData(by_code_type, sys_acronyms, comp_acronyms, sys_names, plants)


# --------------------------------------------------------------------------- #
# grow-as-encountered logs
# --------------------------------------------------------------------------- #
@dataclass
class GrowLogs:
    learned_acronyms: set[tuple[str, str, str]] = field(default_factory=set)
    unknown_acronyms: set[tuple[str, str]] = field(default_factory=set)
    unknown_manufacturers: set[str] = field(default_factory=set)

    def flush(self, logs_dir: Path) -> None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        _append_csv(logs_dir / "learned_acronyms.csv",
                    ["acronym", "eiis_code", "resolved_from_name"],
                    sorted(self.learned_acronyms))
        _append_csv(logs_dir / "unknown_acronyms.csv",
                    ["acronym", "display_name"], sorted(self.unknown_acronyms))
        _append_csv(logs_dir / "unknown_manufacturers.csv",
                    ["manufacturer_code"], [(m,) for m in sorted(self.unknown_manufacturers)])


def _append_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    existing: set[tuple] = set()
    if path.exists():
        with path.open() as f:
            r = csv.reader(f)
            next(r, None)
            existing = {tuple(x) for x in r}
    new = [row for row in rows if tuple(map(str, row)) not in existing]
    if not new:
        return
    write_header = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerows(new)


# --------------------------------------------------------------------------- #
# EIIS resolution
# --------------------------------------------------------------------------- #
def _paren_acronym(display: str) -> Optional[str]:
    """Pull an acronym from '... (RCIC) ...' or the OCR variant '... {RCIC) ...'."""
    cands = re.findall(r"[\(\{]\s*([A-Za-z]{2,6})\s*[\)\}]", display)
    for c in cands:
        if c.isupper():
            return c
    return cands[0].upper() if cands else None


def _fuzzy_system_code(display: str, refs: RefData) -> Optional[str]:
    q = _norm_name(display)
    if not q:
        return None
    if q in SEED_SYS_NAME_ALIASES:
        return SEED_SYS_NAME_ALIASES[q]
    # WRatio (not token_set_ratio): token_set_ratio scores 100 for any subset, so
    # "reactor core" (AC) tied "reactor core isolation cooling" (BN) and won. WRatio
    # ranks the full-length match highest while still tolerating BO's extra
    # "/Low Pressure Coolant Injection" qualifier on a bare "Residual Heat Removal".
    choices = [n for n, _ in refs.sys_names]
    hit = process.extractOne(q, choices, scorer=fuzz.WRatio,
                             score_cutoff=_NAME_MATCH_THRESHOLD)
    if hit:
        return refs.sys_names[hit[2]][1]
    return None


def resolve_system(node: dict, refs: RefData, logs: GrowLogs) -> None:
    code = (node.get("eiis_code") or "").strip().upper() or None
    if code:                                       # LLM read a bracket code — trust it
        node["eiis_code"] = code
        return

    display = node.get("display_name", "")
    acro = _paren_acronym(display)
    resolved: Optional[str] = None
    via_acronym_known = False

    if acro:
        resolved = refs.sys_acronyms.get(acro)
        via_acronym_known = resolved is not None
    if not resolved:
        resolved = _fuzzy_system_code(display, refs)

    if resolved:
        node["eiis_code"] = resolved
        if acro and not via_acronym_known:         # learned a new acronym via name
            logs.learned_acronyms.add((acro, resolved, _norm_name(display)))
    else:
        node["eiis_code"] = None
        node["non_eiis"] = True
        # normalize the name-slug so cross-document dedup is stable
        clean = re.sub(r"\s*[\(\{][^)}]*[\)\}]\s*", " ", display).strip()
        canon = _NAME_SLUG_CANON.get(clean.lower()) or _NAME_SLUG_CANON.get((acro or "").lower())
        node["display_name"] = canon or clean or display
        if acro and not any(k in display.lower() for k in _KNOWN_CODELESS):
            logs.unknown_acronyms.add((acro, display))


def resolve_component(node: dict, refs: RefData, logs: GrowLogs) -> None:
    code = (node.get("eiis_code") or "").strip().upper() or None
    if code:                                       # bracket code from the narrative
        node["eiis_code"] = code
        return

    display = node.get("display_name", "")
    acro = _paren_acronym(display)
    if acro and acro in refs.comp_acronyms:
        node["eiis_code"] = refs.comp_acronyms[acro]
        return

    for rx, inferred in _COMPONENT_INFERENCE:      # e.g. PCIV -> ISV
        if rx.search(display):
            node["eiis_code"] = inferred
            node["inferred_code"] = True
            return

    # otherwise a descriptive component (cannon plug, ECCS test unit): name-slug.
    node["eiis_code"] = None


def resolve_manufacturer(node: dict, refs: RefData, logs: GrowLogs) -> None:
    code = (node.get("code") or "").strip() or None
    if code and not node.get("display_name"):
        name = SEED_MANUFACTURERS.get(code)
        if name:
            node["display_name"] = name
        else:
            logs.unknown_manufacturers.add(code)


# --------------------------------------------------------------------------- #
# previous-occurrence (SIMILAR_TO stub) LER-number normalization
# --------------------------------------------------------------------------- #
# LERs cite a prior occurrence by a short form the LLM can't fully canonicalize —
# e.g. Limerick's "LER 1-2022-001 Unit 1" means Limerick *Unit 1*, whose docket
# (05000352) the narrative never states. A docket-short prefix is 3 digits
# (237/352/353); a 1-2 digit prefix is a unit number of the *same plant*, which we
# resolve to its docket via plants.csv.
_STUB_LER_RE = re.compile(r"^(\d{1,4})-(\d{4})-(\d{1,3})(?:-(\d{1,2}))?$")


def normalize_stub_ler_key(key: str, plant_name: str, refs: RefData) -> str:
    m = _STUB_LER_RE.match((key or "").strip())
    if not m:
        return key
    lead, year, seq, rev = m.groups()
    rev = rev or "00"
    short = lead
    if len(lead) <= 2 and plant_name:                # a same-plant unit number
        unit = str(int(lead))
        for docket, row in refs.plants.items():
            if row.get("plant_name") == plant_name and str(row.get("unit")) == unit:
                short = str(int(docket[3:]))          # 05000352 -> 352
                break
    return f"{short}-{year}-{int(seq):03d}-{int(rev):02d}"


# --------------------------------------------------------------------------- #
# merge + resolve + validate
# --------------------------------------------------------------------------- #
def resolve(
    llm_dict: dict,
    parse,                                          # Form366Parse (avoid import cycle)
    refs: RefData,
    logs: Optional[GrowLogs] = None,
) -> LERRecord:
    """Overlay authoritative deterministic fields, resolve codes, join plant
    facts, and validate. Raises pydantic ValidationError on a bad graph."""
    import copy

    logs = logs if logs is not None else GrowLogs()
    d = copy.deepcopy(llm_dict)
    d["ler_number"] = parse.ler_number

    # --- identity: parse authoritative, plant facts from plants.csv ---
    ident = parse.identity.model_dump()
    prow = refs.plants.get(parse.identity.docket)
    if prow:
        ident["plant_name"] = prow.get("plant_name") or ident.get("plant_name")
        if prow.get("unit"):
            ident["unit"] = int(prow["unit"])
        ident["reactor_type"] = prow.get("reactor_type") or None
        ident["nss_vendor"] = prow.get("nss_vendor") or None
    d["identity"] = ident
    d["reporting_basis"] = parse.reporting_basis.model_dump()
    d["block_13"] = [r.model_dump() for r in parse.block_13]

    # --- cause: keep LLM proximate_text/theme, stamp official code/category ---
    llm_cause = d.get("cause") or {}
    d["cause"] = {
        **llm_cause,
        "cause_code": parse.cause.cause_code,
        "category": parse.cause.category,
        "provisional": parse.cause.provisional,
    }

    # --- nodes ---
    for n in d.get("nodes", []):
        t = n.get("type")
        if t == "System":
            resolve_system(n, refs, logs)
        elif t == "Component":
            resolve_component(n, refs, logs)
        elif t == "Manufacturer":
            resolve_manufacturer(n, refs, logs)
        elif t == "Cause":
            n["cause_code"] = parse.cause.cause_code
            n["category"] = parse.cause.category
            n["provisional"] = parse.cause.provisional
        elif t == "LER":
            if n.get("id") == "ler":                  # primary — authoritative key
                n["key"] = parse.ler_number
            else:                                     # previous-occurrence stub
                new_key = normalize_stub_ler_key(
                    n.get("key", ""), (prow or {}).get("plant_name", ""), refs
                )
                n["key"] = new_key
                if (n.get("display_name") or "").upper().startswith("LER"):
                    n["display_name"] = f"LER {new_key}"
        elif t == "Unit" and prow:
            n["key"] = parse.identity.docket
            n["display_name"] = f"{prow.get('plant_name','').strip()} Unit {prow.get('unit','')}".strip()
            props = n.setdefault("properties", {})
            if prow.get("reactor_type"):
                props.setdefault("reactor_type", prow["reactor_type"])
            if prow.get("nss_vendor"):
                props.setdefault("nss_vendor", prow["nss_vendor"])
            if prow.get("thermal_power_mwt"):
                props.setdefault("thermal_power_mwt", prow["thermal_power_mwt"])

    return LERRecord.model_validate(d)


# --------------------------------------------------------------------------- #
# CLI: resolve the oracle's own extraction shape as a smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    from parse_form366 import load_and_parse

    refs = load_refs()
    acc = sys.argv[1] if len(sys.argv) > 1 else "ML26022A036"
    parse = load_and_parse(acc)
    print(f"loaded refs: {len(refs.by_code_type)} coded rows, "
          f"{len(refs.sys_acronyms)} system acronyms, {len(refs.plants)} plants")
    print(f"parsed {parse.ler_number}: docket {parse.identity.docket} -> "
          f"{refs.plants.get(parse.identity.docket, {}).get('plant_name', '?')}")
    # tiny synthetic LLM output to exercise resolution paths
    demo = {
        "nodes": [
            {"id": "ler", "type": "LER", "key": parse.ler_number,
             "display_name": f"LER {parse.ler_number}"},
            {"id": "unit", "type": "Unit", "key": parse.identity.docket, "display_name": "?"},
            {"id": "cause", "type": "Cause", "display_name": "c", "category": "x"},
            {"id": "s1", "type": "System", "display_name": "Reactor Core Isolation (RCIC) System"},
            {"id": "s2", "type": "System", "display_name": "Core Spray (CS) System"},
            {"id": "s3", "type": "System", "display_name": "Automatic Depressurization System (ADS)"},
            {"id": "c1", "type": "Component", "display_name": "Outboard PCIV"},
        ],
        "edges": [{"source": "ler", "relation": "OCCURRED_AT", "target": "unit"}],
    }
    rec = resolve(demo, parse, refs)
    for n in rec.nodes:
        if n.type in ("System", "Component", "Unit"):
            print(f"  {n.type:9} {n.display_name!r:55} -> match_key={n.match_key}")
