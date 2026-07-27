"""
parse_form366.py — deterministic NRC Form-366 parser (rev 04-2024).

Turns one LER's ADAMS APS `content` text (plus the API metadata cached in the
sibling `{ACCESSION}.json`) into the *deterministic* slice of an LERRecord:
identity, reporting_basis, block-13 rows, cause_code/category, and the two text
segments the LLM stage needs (block-16 abstract, cleaned 366A narrative).

Everything here is regex/heuristic and deliberately forgiving of OCR-ish noise
(`50. 73`, `11 /17 /25`, `1 O CFR`, curly-brace `{RCIC)`), because the APS
`content` field, while far cleaner than PDF OCR, is not pristine. The LLM never
sees these fields except as grounding, and resolve.py treats them as
authoritative, so getting them right here is what makes the whole pipeline
trustworthy.

Design split (see docs/journal/phase_4.md):
  * IDENTITY that the API knows reliably  -> taken from metadata
    (accession, docket, ler_number, report_date).
  * IDENTITY that only the content states  -> parsed here
    (event_date [block 5], operating_mode, power_level, discovery_context,
     status, revision, ENS number/date/time, title).
  * STABLE plant facts (reactor_type, vendor, thermal power) -> NOT parsed;
    resolve.py joins plants.csv on docket.

Verify against the oracle:
    python -m pragraph.parse_form366 ML26022A036 ML25122A139
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional


from pydantic import BaseModel, ConfigDict, Field

from .models import Block13Row, CauseBlock, CauseCode, Identity, ReportingBasis

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- #
# Static tables
# --------------------------------------------------------------------------- #
# Block-13 cause code -> category label (NUREG-1022, Item 13). The official code
# governs the category; the LLM only supplies proximate_text/theme.
CAUSE_CATEGORY: dict[str, str] = {
    "A": "Personnel Error",
    "B": "Design/Manufacturing/Installation",
    "C": "External Cause",
    "D": "Defective Procedure",
    "E": "Management/QA Deficiency",
    "X": "Other",
    "TBD": "provisional",
}

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}


# --------------------------------------------------------------------------- #
# Output container
# --------------------------------------------------------------------------- #
class Form366Parse(BaseModel):
    """The deterministic layer of one LER. `identity` still has plant facts
    (plant_name/unit/reactor_type/nss_vendor) unset — resolve.py fills them."""

    model_config = ConfigDict(extra="ignore")

    accession: Optional[str] = None
    ler_number: str
    identity: Identity
    reporting_basis: ReportingBasis
    block_13: list[Block13Row] = Field(default_factory=list)
    cause: CauseBlock
    abstract: Optional[str] = None
    narrative: Optional[str] = None

    def form_fields_json(self) -> dict:
        """The `[FORM-366 FIELDS]` grounding block handed to the LLM prompt."""
        return {
            "identity": self.identity.model_dump(),
            "reporting_basis": self.reporting_basis.model_dump(),
            "block_13": [r.model_dump() for r in self.block_13],
            "cause_code": self.cause.cause_code,
            "category": self.cause.category,
        }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _norm_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s)


def _iso(y: int, m: int, d: int) -> Optional[str]:
    if 1 <= m <= 12 and 1 <= d <= 31 and 1900 < y < 2100:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def _iso_from_match_mdy_words(month_word: str, d: str, y: str) -> Optional[str]:
    mo = _MONTHS.get(month_word.lower())
    return _iso(int(y), mo, int(d)) if mo else None


def _iso_from_slashes(mm: str, dd: str, yy: str) -> Optional[str]:
    y = int(yy)
    if y < 100:
        y += 2000
    return _iso(y, int(mm), int(dd))


# --------------------------------------------------------------------------- #
# segmentation: cover letter | form header/blocks | abstract | 366A narrative
# --------------------------------------------------------------------------- #
def segment(text: str) -> dict:
    """Split raw content into the four regions. Markers per docs/journal/phase_4.md."""
    # first 366A marks the start of the narrative continuation sheets
    m_366a = re.search(r"NRC\s*FORM\s*366A", text, re.I)
    narrative_start = m_366a.start() if m_366a else len(text)

    # first "NRC FORM 366" (not 366A) marks the end of the cover letter
    cover_end = len(text)
    for m in re.finditer(r"NRC\s*FORM\s*366(?!A)", text, re.I):
        cover_end = m.start()
        break

    # abstract lives between "16. Abstract" and the narrative start
    abstract = None
    m_abs = re.search(r"16\.\s*Abstract[^\n]*\n", text[:narrative_start], re.I)
    if m_abs:
        abstract = text[m_abs.end():narrative_start]

    return {
        "cover": text[:cover_end],
        "header": text[cover_end:narrative_start],
        "abstract_raw": abstract,
        "narrative_raw": text[narrative_start:],
    }


# --------------------------------------------------------------------------- #
# narrative + abstract cleanup (strip repeated 366A boilerplate)
# --------------------------------------------------------------------------- #
# Header / form-furniture lines that repeat on every 366A page.
_BOILER = [
    r"^NRC\s*FORM\s*366",
    r"^\(0?[14]?[-/]?0?2[-/]?2024",
    r"^U\.?S\.?\s*NUCLEAR REGULATORY",
    r"APPROVED BY 0?MB",
    r"^LICENSEE EVENT REPORT",
    r"^CONTINUATION SHEET",
    r"^\(See\s+NUREG",
    r"^\(See Page",
    r"reading-rm/doc-collection",
    r"^https?[:.]",
    r"^b?tt[,.]?[pl]",                       # OCR-mangled http lines
    r"^NARRATIVE\b",
    r"FACILITY NAME",
    r"^\d?\.?\s*DOCKET NUMBER",
    r"LER NUMBER",
    r"Page[_\s]+\d+[_\s]*of",
    r"^\s*[■□▪0]\s*05[02]\b",
    r"^\s*05[02]\b",
    # form-field fragments that leak between pages (353 / 001 / YEAR / REV / ~-, ...)
    r"^\d{1,4}\s*$",
    r"^[Il|]$",
    r"^[~C][:;.,]",
    r"^(?:YEAR|SEQUENTIAL|REV|NO\.?|NUMBER)\b",
    r"^[-~]\s*[,\d]",
]
_BOILER_RE = [re.compile(p, re.I) for p in _BOILER]

# The OMB paperwork-burden paragraph repeats verbatim on every page. Its closing
# sentinel ("...valid OMB control number") is routinely OCR-mangled, so a stateful
# start->end skip runs away and eats real narrative. Instead we drop burden lines
# statelessly: each carries several distinctive tokens that survive OCR.
_BURDEN_KEYS = (
    "estimated burden", "collection request", "lessons learn", "licensing process",
    "fed back", "send comments", "burden estimate", "foia", "collections branch",
    "20555-0001", "nfocollects", "mb reviewer", "office of information",
    "regulatory affair", "regulatory affalf", "desk officer", "17th street",
    "20503", "conduct or sponsor", "collection of information", "control number",
    "displays a c", "valid 0mb", "valid omb",
)


def _is_boiler(stripped: str) -> bool:
    low = stripped.lower()
    if any(k in low for k in _BURDEN_KEYS):
        return True
    return any(rx.search(stripped) for rx in _BOILER_RE)


def _strip_boilerplate(segment_text: str) -> str:
    out: list[str] = []
    for ln in segment_text.splitlines():
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if _is_boiler(s):
            continue
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_abstract(abstract_raw: Optional[str]) -> Optional[str]:
    if not abstract_raw:
        return None
    txt = _strip_boilerplate(abstract_raw)
    return txt or None


def clean_narrative(narrative_raw: str) -> str:
    return _strip_boilerplate(narrative_raw)


# --------------------------------------------------------------------------- #
# field parsers
# --------------------------------------------------------------------------- #
# The block-5/6/7 numeric line: event(MM DD YYYY) LER(YYYY NNN RR) report(MM DD YYYY),
# tolerating the dashes Limerick keeps ("2025 - 001 - 00").
_BLOCK567 = re.compile(
    r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\s+"          # event M D Y
    r"(\d{4})\s*-?\s*(\d{3})\s*-?\s*(\d{2})\s+"      # LER year seq rev
    r"(\d{1,2})\s+(\d{1,2})\s+(\d{4})"              # report M D Y
)


def parse_event_date(header: str, narrative: str, abstract: str) -> Optional[str]:
    m = _BLOCK567.search(header)
    if m:
        iso = _iso(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        if iso:
            return iso
    # fallback: explicit "Event Date: <Month D, Y>" in the narrative header block
    for src in (narrative, abstract, header):
        m = re.search(r"Event Date[:\s]+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", src)
        if m:
            iso = _iso_from_match_mdy_words(*m.groups())
            if iso:
                return iso
    # last resort: first "On <Month D, Y>" in the description
    m = re.search(r"\bOn\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", narrative)
    if m:
        return _iso_from_match_mdy_words(*m.groups())
    return None


def parse_operating_mode(header: str, narrative: str) -> Optional[str]:
    for src in (narrative, header):
        for pat in (
            r"Reactor Mode[:\s]+(\d)",
            r"Operational Condition\s*\(OPCON\)\s*(\d)",
            r"\(OPCON\)\s*(\d)",
            r"\bMode\s+(\d)\b",
        ):
            m = re.search(pat, src)
            if m:
                return m.group(1)
    return None


def parse_power_level(header: str, narrative: str, abstract: str) -> Optional[int]:
    for src in (narrative, abstract, header):
        for pat in (
            r"Power Level[:\s]+(\d{1,3})\s*%",
            r"(\d{1,3})\s*percent\s+power",
            r"approximately\s+(\d{1,3})\s*%",
            r"(\d{1,3})\s*%\s*power",
        ):
            m = re.search(pat, src, re.I)
            if m:
                return int(m.group(1))
    return None


def parse_discovery_context(text: str) -> Optional[str]:
    low = text.lower()
    if "operability test" in low:
        return "operability test"
    if "surveillance test" in low or "surveillance testing" in low:
        return "surveillance test"
    if re.search(r"\binspection\b", low):
        return "inspection"
    return "normal operation"


def parse_reported_under(prose: str) -> list[str]:
    """Find the real 10 CFR criteria from PROSE only (the checkbox grid lists
    every option, so it must be excluded). Tolerates `CF[RT]`, `50. 73`."""
    out: list[str] = []
    for m in re.finditer(
        r"CF[RT]\s*50\.?\s*73((?:\s*\([a-zA-Z0-9]+\))+)", prose
    ):
        chain = re.sub(r"\s+", "", m.group(1))                 # (a)(2)(v)(D)
        cite = f"10 CFR 50.73{chain}"
        if cite not in out:
            out.append(cite)
    return out


def parse_ssff(prose: str) -> str:
    """Y only when the report actually asserts a functional failure / loss of
    safety function. The bare (v)(D) boilerplate ("could have prevented the
    fulfillment of a safety function") is NOT enough (that's Dresden -> 'not stated')."""
    low = prose.lower()
    if (
        "safety system functional failure" in low
        or "loss of safety function" in low
        or "inoperability of a single train safety system" in low
    ):
        return "Y"
    return "not stated"


def parse_status(header: str, prose: str) -> str:
    """supplement-expected if a block-15 date is present or the prose promises a
    supplement; otherwise final."""
    if re.search(r"Expected\s+Subm\w+\s+Date\)?\s*\d{2}\s+\d{2}\s+\d{4}", header, re.I):
        return "supplement-expected"
    if re.search(r"supplement(?:al)?\s+(?:report|ler)\s+will\s+be\s+submitted", prose, re.I):
        return "supplement-expected"
    return "final"


def parse_title(cover: str, header: str, meta_title: str) -> str:
    # Subject line in the cover letter is the cleanest source; the title may wrap
    # across lines until a blank line. Strip the "LER .../Licensee Event Report ..." prefix.
    m = re.search(r"Subject:\s*(.+?)(?:\n\s*\n)", cover, re.S | re.I)
    if m:
        raw = _norm_ws(m.group(1).replace("\n", " ")).strip()
        raw = re.sub(
            r"^(?:LER|Licensee Event Report)\s*[\d/\-]+\s*[,:]?\s*", "", raw, flags=re.I
        ).strip().strip('"').strip()
        if raw:
            return raw
    # fallback: metadata title "LER NNNN for PLANT, Unit N, <title>"
    m = re.search(r",\s*Unit\s*\d+,\s*(.+)$", meta_title)
    if m:
        return m.group(1).strip()
    return meta_title.strip()


def _normalize_reportable(tok: Optional[str]) -> Optional[str]:
    if not tok:
        return None
    t = tok.strip().lower()
    return {"y": "Y", "yes": "Y", "n": "N", "no": "N"}.get(t)


def _mk_row(cause, system, component, manufacturer, reportable) -> Block13Row:
    def clean(v):
        if v is None:
            return None
        v = v.strip()
        return None if v.lower() in {"n/a", "na", ""} else v

    return Block13Row(
        cause=clean(cause),
        system=clean(system),
        component=clean(component),
        manufacturer=clean(manufacturer),
        reportable=_normalize_reportable(reportable),
    )


def parse_block13(header: str) -> list[Block13Row]:
    """Block 13 linearizes to a token run after the (doubled) column header.
    Clean 5-token rows chunk cleanly (Dresden, QC); short rows are placed by
    role (Limerick 'B BJ Yes')."""
    m = re.search(
        r"Complete One Line for each Component Failure[^\n]*\n(.*?)\n\s*14\.",
        header,
        re.S | re.I,
    )
    if not m:
        return []
    region = m.group(1)
    # drop the repeated column-header words, keep only the value tokens
    region = re.sub(
        r"Cause|System|Component|Manufacturer|Reportable\s+to\s+IRIS",
        " ",
        region,
        flags=re.I,
    )
    tokens = region.split()
    if not tokens:
        return []

    rows: list[Block13Row] = []
    if len(tokens) >= 5 and len(tokens) % 5 == 0:
        for i in range(0, len(tokens), 5):
            rows.append(_mk_row(*tokens[i:i + 5]))
    elif len(tokens) >= 5:
        rows.append(_mk_row(*tokens[:5]))           # e.g. QC trailing "n/a"
    else:
        # short row: cause first, reportable last, first EIIS-shaped token = system
        cause = tokens[0] if re.fullmatch(r"[A-EX]|TBD", tokens[0]) else None
        reportable = None
        if re.fullmatch(r"(?i)y|yes|n|no", tokens[-1]):
            reportable = tokens[-1]
        mids = tokens[1:-1] if reportable else tokens[1:]
        system = next((t for t in mids if re.fullmatch(r"[A-Z]{2}", t)), None)
        rows.append(_mk_row(cause, system, None, None, reportable))
    # drop fully-empty rows
    return [r for r in rows if any(r.model_dump().values())]


def parse_ens(prose: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(number, date, time). Anchor on the ENS number, then look ±200 chars for
    a time ('at 2328 CST' / 'at 18:09') and date ('on 11/17/25' / 'on March 3, 2025')."""
    m = (
        re.search(r"\bENS\b\D{0,20}(\d{4,6})", prose, re.I)
        or re.search(r"report number\s+(\d{4,6})", prose, re.I)
        or re.search(r"notification[^.\d]{0,20}\((\d{4,6})\)", prose, re.I)
    )
    if not m:
        return None, None, None
    num = m.group(1)
    anchor = (m.start() + m.end()) // 2
    lo, hi = max(0, anchor - 200), anchor + 200
    win = prose[lo:hi]

    # Times/dates for restoration and ENS transmission sit close together and in
    # inconsistent order (Dresden: time after the number; Limerick: before), so
    # pick the candidate *closest* to the ENS-number anchor.
    def _closest(matches_with_values):
        best, best_d = None, 1 << 30
        for start, val in matches_with_values:
            if val is None:
                continue
            d = abs((lo + start) - anchor)
            if d < best_d:
                best, best_d = val, d
        return best

    times = []
    for mt in re.finditer(
        r"\bat\s+(\d{1,2}:?\d{2})\s*(CST|CDT|EST|EDT|PST|PDT|MST|MDT)?", win
    ):
        val = (mt.group(1) + (" " + mt.group(2) if mt.group(2) else "")).strip()
        times.append((mt.start(), val))
    time = _closest(times)

    dates = []
    for md in re.finditer(r"on\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", win):
        dates.append((md.start(), _iso_from_match_mdy_words(*md.groups())))
    for md in re.finditer(r"on\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", win):
        dates.append((md.start(), _iso_from_slashes(*md.groups())))
    date = _closest(dates)

    return num, date, time


# --------------------------------------------------------------------------- #
# top-level
# --------------------------------------------------------------------------- #
def parse_text(content: str, meta: dict) -> Form366Parse:
    """Parse one LER's content + API metadata into the deterministic layer."""
    seg = segment(content)
    cover, header = seg["cover"], seg["header"]
    abstract = clean_abstract(seg["abstract_raw"]) or ""
    narrative = clean_narrative(seg["narrative_raw"])
    # prose = everything that is real English (never the checkbox grid)
    prose = "\n".join([cover, abstract, narrative])

    ler_number = meta.get("ler_number") or ""
    docket = meta.get("docket") or ""
    revision = ler_number.split("-")[-1] if "-" in ler_number else "00"

    # Block-5 text first (the official form field); fall back to the INL export's
    # Event Date when text extraction separated the date labels from their values.
    event_date = parse_event_date(header, narrative, abstract) or meta.get("event_date")
    if not event_date:
        raise ValueError(f"{ler_number}: could not parse event_date (block 5)")

    ens_number, ens_date, ens_time = parse_ens(prose)

    identity = Identity(
        accession_number=meta.get("accession"),
        docket=docket,
        event_date=event_date,
        report_date=meta.get("report_date") or None,
        operating_mode=parse_operating_mode(header, narrative),
        power_level=parse_power_level(header, narrative, abstract),
        discovery_context=parse_discovery_context(narrative + " " + abstract),
        status=parse_status(header, prose),
        revision=revision,
        ens_number=ens_number,
        ens_date=ens_date,
        ens_time=ens_time,
        title=parse_title(cover, header, meta.get("title", "")),
    )

    reporting_basis = ReportingBasis(
        reported_under=parse_reported_under(prose),
        ssff=parse_ssff(prose),
    )

    block_13 = parse_block13(header)
    code: CauseCode = "TBD"
    if block_13 and block_13[0].cause in CAUSE_CATEGORY:
        code = block_13[0].cause  # type: ignore[assignment]
    cause = CauseBlock(
        cause_code=code,
        category=CAUSE_CATEGORY[code],
        provisional=(code == "TBD"),
    )

    return Form366Parse(
        accession=meta.get("accession"),
        ler_number=ler_number,
        identity=identity,
        reporting_basis=reporting_basis,
        block_13=block_13,
        cause=cause,
        abstract=abstract or None,
        narrative=narrative or None,
    )


def load_meta(accession: str, raw_dir: Path) -> dict:
    """Pull reliable identity fields from the cached APS JSON (fetch_ler.derive_meta)."""
    import json

    from .fetch_ler import derive_meta        # sibling module in src/ (already on sys.path)

    resp = json.loads((raw_dir / f"{accession}.json").read_text())
    return derive_meta(resp)


_SHEET_DATES: Optional[dict] = None


def sheet_event_date(accession: str, raw_dir: Path) -> Optional[str]:
    """Authoritative event date (ISO) from the INL export at data/raw/fetch_list.csv,
    a fallback for when text extraction separates the block-5 date labels from their
    values (~10% of recent docs). Cached on first use; returns None if unavailable."""
    global _SHEET_DATES
    if _SHEET_DATES is None:
        import csv
        _SHEET_DATES = {}
        p = raw_dir / "fetch_list.csv"
        if p.exists():
            for row in csv.DictReader(p.open()):
                mdY = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", (row.get("event_date") or "").strip())
                acc = row.get("accession")
                if acc and mdY:
                    iso = _iso_from_slashes(*mdY.groups())
                    if iso:
                        _SHEET_DATES[acc] = iso
    return _SHEET_DATES.get(accession)


def load_and_parse(accession: str, raw_dir: Optional[Path] = None) -> Form366Parse:
    raw_dir = raw_dir or (REPO_ROOT / "data" / "raw")
    content = (raw_dir / f"{accession}.txt").read_text()
    meta = load_meta(accession, raw_dir)
    meta.setdefault("event_date", sheet_event_date(accession, raw_dir))
    return parse_text(content, meta)


# --------------------------------------------------------------------------- #
# CLI: quick eyeball / oracle diff
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    accs = sys.argv[1:] or ["ML26022A036", "ML25122A139"]
    for acc in accs:
        p = load_and_parse(acc)
        print(f"\n===== {acc}  ({p.ler_number}) =====")
        print("identity       :", json.dumps(p.identity.model_dump(), indent=2))
        print("reporting_basis:", p.reporting_basis.model_dump())
        print("block_13       :", [r.model_dump() for r in p.block_13])
        print("cause          :", p.cause.model_dump())
        print(f"abstract chars : {len(p.abstract or '')}")
        print(f"narrative chars: {len(p.narrative or '')}")
        print("narrative head :", (p.narrative or "")[:200].replace("\n", " ⏎ "))
