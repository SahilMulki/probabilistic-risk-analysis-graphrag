#!/usr/bin/env python3
"""
fetch_ler.py — retrieve LER plain text from the ADAMS Public Search (APS) API.

Uses the Get Document endpoint:
    GET https://adams-api.nrc.gov/aps/api/search/{accessionNumber}
    header: Ocp-Apim-Subscription-Key: <key>

For each accession it writes to <out>/:
    {ACCESSION}.json   full API response (metadata + content), cached
    {ACCESSION}.txt    the plain-text `content` field (pipeline input)
and appends a row to <out>/manifest.csv mapping accession -> project LER number.

Stdlib only (urllib), so it runs before the rest of the env is set up.

Usage
-----
    export ADAMS_APS_KEY=xxxxxxxxxxxxxxxx
    python -m pragraph.fetch_ler ML26022A036 ML25122A139 ML25308A004
    python -m pragraph.fetch_ler --from-file data/source/accessions.txt --out data/raw
    python -m pragraph.fetch_ler ML26022A036 --force        # re-fetch even if cached

Never commit your subscription key. Keep it in the ADAMS_APS_KEY env var (or a
git-ignored .env) — do not pass it on the command line on shared machines.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://adams-api.nrc.gov/aps/api/search/{acc}"


class FetchError(Exception):
    """A single document failed to fetch after retries — logged and skipped so a
    long run stays resumable rather than aborting the whole batch."""


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #
def fetch(accession: str, key: str, timeout: int = 60,
          retries: int = 4, backoff: float = 2.0) -> dict:
    """GET one document by accession number; return the parsed JSON response.

    Retries transient failures (network errors, HTTP 429/5xx) with exponential
    backoff. A persistent failure (or a client error like 401/403/404) raises
    FetchError so the caller can log-and-continue over an 835-document run."""
    req = urllib.request.Request(
        ENDPOINT.format(acc=accession),
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Cache-Control": "no-cache",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "probabilistic-risk-analysis-graphrag/fetch_ler.py",
        },
    )
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            last = f"HTTP {e.code}: {body}"
            transient = e.code == 429 or 500 <= e.code < 600
            if not transient:
                raise FetchError(f"{accession}: {last}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"network error: {getattr(e, 'reason', e)}"
        except json.JSONDecodeError as e:
            last = f"bad JSON: {e}"
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    raise FetchError(f"{accession}: {last} (after {retries + 1} attempts)")


# --------------------------------------------------------------------------- #
# parsing helpers (no network -> unit-testable)
# --------------------------------------------------------------------------- #
def extract_document(resp: dict) -> dict:
    """Get Document returns {..., 'document': {...}}; Search returns {'results':[{'document':...}]}."""
    if isinstance(resp.get("document"), dict):
        return resp["document"]
    results = resp.get("results") or []
    if results and isinstance(results[0].get("document"), dict):
        return results[0]["document"]
    raise ValueError("no 'document' object in API response")


def _first(v):
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else None)


def derive_meta(resp: dict) -> dict:
    """Pull the reliable identity fields straight from API metadata."""
    doc = extract_document(resp)
    docket = _first(doc.get("DocketNumber")) or ""
    docket_short = str(int(docket[3:])) if docket[3:].isdigit() else docket
    rn = (_first(doc.get("DocumentReportNumber")) or "").replace("LER", "").strip()
    ler_number = f"{docket_short}-{rn}" if (docket_short and rn) else ""
    content = doc.get("content") or ""
    return {
        "accession": doc.get("AccessionNumber", ""),
        "docket": docket,
        "docket_short": docket_short,
        "ler_number": ler_number,          # project format, e.g. 237-2025-003-00
        "report_date": doc.get("DocumentDate", ""),   # NOTE: report date, not event date
        "title": doc.get("DocumentTitle", ""),
        "url": doc.get("Url", ""),
        "chars": len(content),
    }


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
MANIFEST_COLS = ["accession", "ler_number", "docket", "report_date", "chars", "title"]


def save(resp: dict, outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    meta = derive_meta(resp)
    doc = extract_document(resp)
    acc = meta["accession"] or "UNKNOWN"
    (outdir / f"{acc}.json").write_text(json.dumps(resp, indent=2))
    (outdir / f"{acc}.txt").write_text(doc.get("content") or "")
    _update_manifest(outdir / "manifest.csv", meta)
    return meta


def _update_manifest(path: Path, meta: dict) -> None:
    rows = {}
    if path.exists():
        with path.open() as f:
            rows = {r["accession"]: r for r in csv.DictReader(f)}
    rows[meta["accession"]] = {k: meta.get(k, "") for k in MANIFEST_COLS}
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        w.writerows(rows.values())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fetch LER text from the ADAMS APS API.")
    p.add_argument("accessions", nargs="*", help="accession numbers, e.g. ML26022A036")
    p.add_argument("--from-file", help="file with one accession number per line")
    p.add_argument("--out", default="data/raw", help="output dir (default: data/raw)")
    p.add_argument("--key", help="subscription key (prefer the ADAMS_APS_KEY env var)")
    p.add_argument("--force", action="store_true", help="re-fetch even if cached")
    p.add_argument("--sleep", type=float, default=1.0, help="seconds between requests")
    args = p.parse_args(argv)

    key = args.key or os.environ.get("ADAMS_APS_KEY")
    if not key:
        p.error("no subscription key: set ADAMS_APS_KEY or pass --key")

    accs = list(args.accessions)
    if args.from_file:
        accs += [ln.strip() for ln in Path(args.from_file).read_text().splitlines() if ln.strip()]
    accs = [a for a in dict.fromkeys(accs)]        # de-dupe, keep order
    if not accs:
        p.error("no accession numbers given")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    fail_log = outdir / "fetch_failures.csv"
    n_ok = n_cached = 0
    failures: list[tuple[str, str]] = []
    for i, acc in enumerate(accs):
        cached = outdir / f"{acc}.json"
        if cached.exists() and not args.force:
            meta = derive_meta(json.loads(cached.read_text()))
            print(f"[cached] {acc}  {meta['ler_number']}  ({meta['chars']} chars)")
            n_cached += 1
            continue
        if i and args.sleep:
            time.sleep(args.sleep)
        try:
            meta = save(fetch(acc, key), outdir)
        except FetchError as e:
            print(f"[FAIL]   {e}", file=sys.stderr)
            failures.append((acc, str(e).split(": ", 1)[-1]))
            continue
        n_ok += 1
        print(f"[ok]     {acc}  {meta['ler_number']}  ({meta['chars']} chars) -> {outdir}/{acc}.txt")

    if failures:                                   # append so a resumed run accumulates
        new = not fail_log.exists()
        with fail_log.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["accession", "error"])
            w.writerows(failures)
    print(f"\n[fetch] {n_ok} fetched, {n_cached} cached, {len(failures)} failed "
          f"(of {len(accs)}). manifest: {outdir/'manifest.csv'}")
    if failures:
        print(f"[fetch] {len(failures)} failures logged to {fail_log} — re-run the same "
              "command to retry only the missing ones (cached docs are skipped).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
