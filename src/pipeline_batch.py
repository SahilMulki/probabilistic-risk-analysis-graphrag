"""
pipeline_batch.py — Phase 8: extract many LERs via the Anthropic Message Batches
API (50% off, async <=24h) with prompt caching on the shared schema + few-shot.

Shape: submit-all -> poll -> collect -> resolve, then re-batch only the docs whose
output failed JSON parse or schema validation (the sequential pipeline's re-ask
loop, done a round at a time). Deterministic Form-366 parsing and the resolver are
reused verbatim from pipeline.py, so extraction quality is identical to the trusted
sequential path — only the transport changes.

  python src/pipeline_batch.py --from-file data/source/accessions.txt      # full run
  python src/pipeline_batch.py --docs ML26149A009 ML26146A061  # a few (calibration)
  python src/pipeline_batch.py --limit 10 --from-file data/source/accessions.txt  # first 10
  python src/pipeline_batch.py --resume msgbatch_123           # collect an existing batch

Only the 3 oracle LERs are graded (score_all skips the rest); every doc is spot-
checkable in out/. Cost + cache split are logged to logs/batch_tokens.csv and a
per-run projection to the full corpus is printed. Resumable: docs already in out/
are skipped, and --resume re-collects a submitted batch without paying again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pydantic import ValidationError

from llm import LLM, extract_json
from models import LERRecord
from parse_form366 import load_and_parse
from pipeline import (FEWSHOT_LER, build_prefix, build_tail, load_fewshot,
                      load_template)
from resolve import GrowLogs, load_refs, resolve
from score import load_oracle, print_aggregate, print_scorecard, score_all

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = 835                       # full corpus size, for the cost projection

# Sonnet-5 per-1M-token rates. Intro (through 2026-08-31) = $2/$10; standard = $3/$15.
# Batch halves everything; cached input is 1.25x (write, 5-min) / 0.1x (read).
PRICES = {"intro": (2.0, 10.0), "standard": (3.0, 15.0)}


# --------------------------------------------------------------------------- #
# batch polling
# --------------------------------------------------------------------------- #
def wait_for_batch(llm: LLM, batch_id: str, poll_secs: int, max_wait_secs: int):
    waited = 0
    while True:
        b = llm.retrieve_batch(batch_id)
        c = b.request_counts
        print(f"    [{waited:>4}s] status={b.processing_status}  "
              f"succeeded={c.succeeded} errored={c.errored} processing={c.processing} "
              f"canceled={c.canceled} expired={c.expired}")
        if b.processing_status == "ended":
            return b
        if waited >= max_wait_secs:
            raise TimeoutError(f"batch {batch_id} still {b.processing_status} after {waited}s "
                               f"— re-collect later with --resume {batch_id}")
        time.sleep(poll_secs)
        waited += poll_secs


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
def project_cost(totals: dict, n_done: int, n_target: int) -> None:
    def run_cost(in_rate: float, out_rate: float) -> float:
        c = (totals["input"] * in_rate
             + totals["cache_creation"] * in_rate * 1.25
             + totals["cache_read"] * in_rate * 0.10
             + totals["output"] * out_rate) / 1e6
        return c * 0.5                       # batch discount
    tin = totals["input"] + totals["cache_creation"] + totals["cache_read"]
    print("\n================= cost (measured, batch) =================")
    print(f"  docs extracted this run: {n_done}")
    print(f"  input tokens : {tin:>10,}  (uncached {totals['input']:,}, "
          f"cache-write {totals['cache_creation']:,}, cache-read {totals['cache_read']:,})")
    print(f"  output tokens: {totals['output']:>10,}")
    if tin:
        hit = totals["cache_read"] / tin
        print(f"  cache-read share of input: {hit:5.1%}  "
              f"({'good reuse' if hit > 0.4 else 'low — consider 1h cache TTL for the full run'})")
    if not n_done:
        return
    for name, (ir, orr) in PRICES.items():
        here = run_cost(ir, orr)
        full = here / n_done * n_target
        print(f"  @ {name:8} pricing: ${here:6.3f} for these {n_done}  ->  "
              f"~${full:6.2f} projected for {n_target}")
    print("  NOTE: cache reuse across an async batch is not guaranteed; the full-run"
          " number is an extrapolation from this sample. Confirm live pricing at run time.")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run_batch(accessions: list[str], model: str = "claude-sonnet-5",
              out_dir: Path = REPO_ROOT / "out", raw_dir: Path = REPO_ROOT / "data" / "raw",
              include_abstract: bool = False, skip_cached: bool = True,
              max_rounds: int = 2, poll_secs: int = 20, max_wait_secs: int = 1800,
              n_target: int = DEFAULT_TARGET, resume_id: str | None = None) -> dict:
    refs = load_refs()
    oracle = load_oracle()
    llm = LLM(model=model)
    sys_tmpl, usr_tmpl = load_template()
    schema_json = json.dumps(LERRecord.model_json_schema(), indent=2)
    fewshot_json = load_fewshot()
    system, fewshot_user = build_prefix(sys_tmpl, usr_tmpl, schema_json, fewshot_json)
    logs = GrowLogs()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. deterministic parse (local); skip already-extracted; bucket parse failures
    parses: dict[str, object] = {}
    pending: dict[str, str] = {}
    skipped_cached: list[str] = []
    parse_failures: dict[str, str] = {}
    for acc in accessions:
        try:
            parse = load_and_parse(acc, raw_dir)
        except Exception as e:                      # missing text / parser edge case
            parse_failures[acc] = f"{type(e).__name__}: {e}"
            continue
        if skip_cached and (out_dir / f"{parse.ler_number}.json").exists():
            skipped_cached.append(acc)
            continue
        parses[acc] = parse
        pending[acc] = build_tail(usr_tmpl, parse, include_abstract)

    print(f"[batch] {len(accessions)} requested: {len(parses)} to extract, "
          f"{len(skipped_cached)} already in out/, {len(parse_failures)} parse-failed")

    records: dict[str, LERRecord] = {}
    failures: dict[str, str] = dict(parse_failures)
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

    # 2. extraction rounds (round 0 = first pass; later rounds re-ask only the failures)
    for rnd in range(max_rounds + 1):
        if resume_id and rnd == 0:
            batch_id = resume_id
            print(f"[batch] resuming {batch_id} (no new submission)")
        elif pending:
            reqs = [llm.batch_request(acc, system, fewshot_user, tail)
                    for acc, tail in pending.items()]
            batch_id = llm.submit_batch(reqs)
            (out_dir / "last_batch.txt").write_text(batch_id + "\n")
            print(f"[batch] round {rnd}: submitted {len(reqs)} requests -> {batch_id}")
        else:
            break

        wait_for_batch(llm, batch_id, poll_secs, max_wait_secs)

        next_pending: dict[str, str] = {}
        collected = 0
        for item in llm.batch_results(batch_id):
            acc = item.custom_id
            collected += 1
            res = item.result
            if res.type != "succeeded":
                failures[acc] = f"batch-{res.type}: {getattr(res, 'error', '')}"
                continue
            msg = res.message
            llm.log_batch_usage(acc, msg.usage)
            for k, attr in (("input", "input_tokens"), ("output", "output_tokens"),
                            ("cache_creation", "cache_creation_input_tokens"),
                            ("cache_read", "cache_read_input_tokens")):
                totals[k] += getattr(msg.usage, attr, 0) or 0
            if acc not in parses:                   # a --resume without the parse in hand
                failures[acc] = "resumed result but no local parse; re-run without --resume"
                continue
            obj = extract_json(llm.batch_text(msg))
            if obj is None:
                next_pending[acc] = (pending.get(acc, build_tail(usr_tmpl, parses[acc], include_abstract))
                                     + "\n\nYour previous reply was not valid JSON. Return ONLY "
                                       "the LERRecord JSON object, no prose or fences.")
                continue
            try:
                rec = resolve(obj, parses[acc], refs, logs)
            except ValidationError as e:
                next_pending[acc] = (pending.get(acc, build_tail(usr_tmpl, parses[acc], include_abstract))
                                     + "\n\nThe previous JSON failed schema validation with these "
                                       f"errors:\n{e}\n\nReturn a corrected LERRecord JSON only.")
                continue
            (out_dir / f"{rec.ler_number}.json").write_text(rec.model_dump_json(indent=2))
            records[acc] = rec
            failures.pop(acc, None)

        print(f"[batch] round {rnd}: collected {collected}, "
              f"{len(records)} written so far, {len(next_pending)} to retry")
        pending = next_pending
        if resume_id:                               # only collect the resumed batch, no retries
            break

    for acc in pending:                             # exhausted retries
        failures.setdefault(acc, "still invalid after retry rounds")

    logs.flush(REPO_ROOT / "logs")

    # 3. grade the oracle subset (score_all skips docs with no oracle entry)
    cards = score_all(list(records.values()), oracle)
    if cards:
        print("\n================= oracle regression (graded subset) =================")
        for c in cards:
            print_scorecard(c)
        print_aggregate(cards)

    # 4. reconcile + failures + cost
    if failures:
        fpath = out_dir / "extract_failures.csv"
        import csv as _csv
        with fpath.open("w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["accession", "error"])
            w.writerows(sorted(failures.items()))
        print(f"\n[batch] {len(failures)} failures -> {fpath}")

    n_req = len(accessions)
    accounted = len(records) + len(skipped_cached) + len(failures)
    print("\n================= reconciliation =================")
    print(f"  requested        : {n_req}")
    print(f"  written this run : {len(records)}")
    print(f"  already cached   : {len(skipped_cached)}")
    print(f"  failed           : {len(failures)}")
    print(f"  accounted for    : {accounted} / {n_req}"
          f"  {'OK' if accounted == n_req else 'MISMATCH'}")

    project_cost(totals, len(records), n_target)
    return {"records": records, "failures": failures, "skipped": skipped_cached, "totals": totals}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Phase-8 batch LER extraction (Anthropic Batches).")
    p.add_argument("--docs", nargs="*", default=[], help="accession numbers to extract")
    p.add_argument("--from-file", help="file with one accession per line")
    p.add_argument("--limit", type=int, help="only the first N accessions (calibration)")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--out", default=str(REPO_ROOT / "out"))
    p.add_argument("--raw", default=str(REPO_ROOT / "data" / "raw"))
    p.add_argument("--abstract", action="store_true", help="include block-16 abstract (A/B locked off)")
    p.add_argument("--no-skip-cached", action="store_true", help="re-extract docs already in out/")
    p.add_argument("--poll", type=int, default=20, help="seconds between status polls")
    p.add_argument("--max-wait", type=int, default=1800, help="give up polling after N seconds")
    p.add_argument("--target", type=int, default=DEFAULT_TARGET, help="corpus size for the projection")
    p.add_argument("--resume", help="collect an already-submitted batch id, skip submission")
    args = p.parse_args(argv)

    accs = list(args.docs)
    if args.from_file:
        accs += [ln.strip() for ln in Path(args.from_file).read_text().splitlines() if ln.strip()]
    accs = list(dict.fromkeys(accs))
    if args.limit:
        accs = accs[:args.limit]
    if not accs and not args.resume:
        p.error("provide --docs, --from-file, or --resume")

    run_batch(
        accessions=accs, model=args.model,
        out_dir=Path(args.out), raw_dir=Path(args.raw),
        include_abstract=args.abstract, skip_cached=not args.no_skip_cached,
        poll_secs=args.poll, max_wait_secs=args.max_wait,
        n_target=args.target, resume_id=args.resume,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
