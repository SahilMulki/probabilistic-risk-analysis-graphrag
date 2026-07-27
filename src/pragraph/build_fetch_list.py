#!/usr/bin/env python3
"""
build_fetch_list.py — Phase 8 step 1: turn the INL LER Search export into a
de-duplicated fetch list for fetch_ler.py.

Input : data/source/2020s_LERs.xlsx  (INL "LER Search" export; header on row 5;
        the "Accession #" column is an HTML <a> whose <strong> holds the ML… number
        and whose href filename ends in R<NN>.pdf — the revision).
Output: data/source/accessions.txt  one ML accession per line (fetch_ler.py --from-file)
        data/raw/fetch_list.csv  sheet metadata kept for golden-set expansion /
                                 spot-checks (event_date is here; the ADAMS API
                                 only returns report_date, so this is the source
                                 of truth for event dates at scale).

De-dup rule (roadmap): group by LER number (docket+YYYY+SSS, revision-free) and
keep the HIGHEST revision, so two revisions of one event never both fetch and
double-count in the aggregation questions. This export already lists one row per
event at its latest revision, so the rule is a safeguard here rather than a fix;
we still collapse the two "combined filings" (one ADAMS doc under two LER numbers)
down to unique accessions.

    python -m pragraph.build_fetch_list             # -> data/source/accessions.txt, data/raw/fetch_list.csv
    python -m pragraph.build_fetch_list --xlsx X --out data/source/accessions.txt
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_XLSX = REPO_ROOT / "data" / "source" / "2020s_LERs.xlsx"
HEADER_ROW = 4                                   # 0-indexed; the real header is the 5th row

ACC_RE = re.compile(r"(ML\w+)")
REV_RE = re.compile(r"R(\d+)\.pdf")


def parse_cell(cell: str) -> tuple[str | None, int]:
    """(ML accession, revision int) from an 'Accession #' HTML cell."""
    m = ACC_RE.search(str(cell))
    r = REV_RE.search(str(cell))
    return (m.group(1) if m else None), (int(r.group(1)) if r else 0)


def build(xlsx: Path) -> list[dict]:
    import pandas as pd

    df = pd.read_excel(xlsx, header=HEADER_ROW, dtype=str)
    need = {"LER Number", "Plant Name", "Event Date", "Report Date", "Accession #", "Title/Abstract"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"{xlsx.name}: missing expected columns {sorted(missing)}")

    # one candidate row per sheet row, then reduce by LER number keeping max revision
    best: dict[str, dict] = {}
    for _, row in df.iterrows():
        acc, rev = parse_cell(row["Accession #"])
        ler = str(row["LER Number"]).strip()
        if not acc or not ler:
            continue
        rec = {
            "accession": acc,
            "ler_sheet": ler,                    # docket+YYYY+SSS (revision-free)
            "revision": rev,
            "plant": str(row["Plant Name"]).strip(),
            "event_date": str(row["Event Date"]).strip(),
            "report_date": str(row["Report Date"]).strip(),
            "title": str(row["Title/Abstract"]).strip(),
        }
        cur = best.get(ler)
        if cur is None or rev > cur["revision"]:
            best[ler] = rec

    # collapse to unique accessions (combined filings share one ADAMS document),
    # preserving first-seen order from the sheet (newest-first as exported)
    seen: set[str] = set()
    out: list[dict] = []
    for rec in best.values():
        if rec["accession"] in seen:
            continue
        seen.add(rec["accession"])
        out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a de-duplicated ADAMS fetch list from the LER export.")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "source" / "accessions.txt"))
    ap.add_argument("--csv", default=str(REPO_ROOT / "data" / "raw" / "fetch_list.csv"))
    args = ap.parse_args(argv)

    recs = build(Path(args.xlsx))

    Path(args.out).write_text("\n".join(r["accession"] for r in recs) + "\n")
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["accession", "ler_sheet", "revision", "plant", "event_date", "report_date", "title"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(recs)

    revs = sum(1 for r in recs if r["revision"] > 0)
    print(f"[ok] {len(recs)} unique accessions -> {args.out}")
    print(f"[ok] metadata ({len(recs)} rows, {revs} at revision >0) -> {args.csv}")
    print(f"     plants: {len({r['plant'] for r in recs})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
