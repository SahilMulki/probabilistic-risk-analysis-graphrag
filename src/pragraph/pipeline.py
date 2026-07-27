"""
pipeline.py — end-to-end Phase-4 extraction: raw LER -> validated v4.1 JSON, scored.

For each LER:
  1. parse_form366  — deterministic header/blocks/abstract/narrative
  2. assemble the versioned prompt (prompts/narrative_extraction.md) from the
     Pydantic JSON schema + the Quad Cities few-shot + the deterministic fields
  3. llm.complete_json — Claude narrative extraction (JSON)
  4. resolve — merge deterministic layer, canonicalize EIIS codes, join plants,
     and validate as an LERRecord (retrying the LLM on schema-validation failure)
  5. score against ground_truth.json and write the record to out/

Quad Cities is the few-shot exemplar and is deliberately NOT in the eval set
(keeping it held out); the default corpus is Dresden + Limerick.

    python -m pragraph.pipeline                     # Dresden + Limerick, narrative-only (locked)
    python -m pragraph.pipeline --abstract          # opt back into the block-16 abstract
    python -m pragraph.pipeline --docs ML26022A036  # one document

Abstract A/B (locked): narrative-only beats narrative+abstract — equal node F1 and
higher, more stable edge F1 across 3 runs each on Dresden + Limerick (the abstract
carries nothing absent from the narrative and only adds chaining variance). So the
default is include_abstract=False; `--abstract` re-enables it for experiments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


from pydantic import ValidationError

from .llm import LLM
from .models import LERRecord
from .parse_form366 import Form366Parse, load_and_parse
from .resolve import GrowLogs, load_refs, resolve
from .score import load_oracle, print_aggregate, print_scorecard, score_all

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "narrative_extraction.md"
ORACLE_PATH = REPO_ROOT / "data" / "raw" / "ground_truth" / "ground_truth.json"
FEWSHOT_LER = "254-2025-006-00"          # Quad Cities — exemplar, held out of eval

# Dresden + Limerick: raw text + oracle both available, QC excluded.
DEFAULT_DOCS = ["ML26022A036", "ML25122A139"]


# --------------------------------------------------------------------------- #
# prompt assembly
# --------------------------------------------------------------------------- #
def load_template(path: Path = PROMPT_PATH) -> tuple[str, str]:
    """Pull the SYSTEM and USER fenced blocks out of the versioned prompt md."""
    md = path.read_text()

    def block(header: str) -> str:
        m = re.search(rf"##\s*{header}\s*\n```[^\n]*\n(.*?)\n```", md, re.S)
        if not m:
            raise ValueError(f"prompt template missing a fenced {header} block")
        return m.group(1).strip()

    return block("SYSTEM"), block("USER")


def load_fewshot(oracle_path: Path = ORACLE_PATH, ler: str = FEWSHOT_LER) -> str:
    raw = json.loads(oracle_path.read_text())
    rec = next((r for r in raw["lers"] if r["ler_number"] == ler), None)
    if rec is None:
        raise ValueError(f"few-shot exemplar {ler} not found in {oracle_path}")
    return json.dumps(rec, indent=2)


# The USER template splits here: everything before is the identical few-shot prefix
# (cacheable across all docs); everything from here on is this document's data.
FEWSHOT_MARKER = "Now extract the LER below."


def build_prefix(sys_tmpl: str, usr_tmpl: str, schema_json: str,
                 fewshot_json: str) -> tuple[str, str]:
    """The doc-independent, cacheable pieces, computed ONCE for a whole run:
    (system = schema instructions, fewshot_user = the worked example)."""
    system = sys_tmpl.replace("{{JSON_SCHEMA}}", schema_json)
    head = usr_tmpl.split(FEWSHOT_MARKER, 1)[0] if FEWSHOT_MARKER in usr_tmpl else ""
    fewshot_user = head.replace("{{FEWSHOT_RECORD}}", fewshot_json).strip()
    return system, fewshot_user


def build_tail(usr_tmpl: str, parse: Form366Parse, include_abstract: bool) -> str:
    """This document's varying part of the USER message (form fields + narrative)."""
    tail = (FEWSHOT_MARKER + usr_tmpl.split(FEWSHOT_MARKER, 1)[1]
            if FEWSHOT_MARKER in usr_tmpl else usr_tmpl)
    tail = tail.replace("{{FORM366_FIELDS}}", json.dumps(parse.form_fields_json(), indent=2))
    if include_abstract:
        tail = tail.replace("{{ABSTRACT}}", parse.abstract or "(no abstract provided)")
    else:
        # drop the entire "[ABSTRACT block 16] {{ABSTRACT}}" section (the A/B toggle)
        tail = re.sub(r"\[ABSTRACT block 16\][^\[]*", "", tail, count=1)
    return tail.replace("{{NARRATIVE}}", parse.narrative or "")


def build_messages(
    sys_tmpl: str,
    usr_tmpl: str,
    schema_json: str,
    fewshot_json: str,
    parse: Form366Parse,
    include_abstract: bool,
) -> tuple[str, str]:
    system, fewshot_user = build_prefix(sys_tmpl, usr_tmpl, schema_json, fewshot_json)
    tail = build_tail(usr_tmpl, parse, include_abstract)
    return system, f"{fewshot_user}\n\n{tail}".strip()


# --------------------------------------------------------------------------- #
# extraction with schema-validation retry
# --------------------------------------------------------------------------- #
def extract_one(
    llm: LLM,
    system: str,
    user: str,
    parse: Form366Parse,
    refs,
    logs: GrowLogs,
    retries: int = 2,
) -> LERRecord:
    tag = parse.accession or parse.ler_number
    prompt = user
    last_err = ""
    for attempt in range(retries + 1):
        obj = llm.complete_json(system, prompt, tag=tag)
        try:
            return resolve(obj, parse, refs, logs)
        except ValidationError as e:
            last_err = str(e)
            prompt = (
                user
                + "\n\nThe previous JSON failed schema validation with these errors:\n"
                + last_err
                + "\n\nReturn a corrected LERRecord JSON only."
            )
    raise RuntimeError(f"{parse.ler_number}: still invalid after {retries + 1} tries:\n{last_err}")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(
    accessions: list[str],
    include_abstract: bool = False,          # A/B locked: narrative-only wins
    model: str = "claude-sonnet-5",
    out_dir: Path = REPO_ROOT / "out",
    raw_dir: Path = REPO_ROOT / "data" / "raw",
) -> list[dict]:
    refs = load_refs()
    oracle = load_oracle()
    llm = LLM(model=model)
    sys_tmpl, usr_tmpl = load_template()
    schema_json = json.dumps(LERRecord.model_json_schema(), indent=2)
    fewshot_json = load_fewshot()
    logs = GrowLogs()

    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[LERRecord] = []
    for acc in accessions:
        print(f"\n[extract] {acc} (abstract={'on' if include_abstract else 'off'}) ...")
        parse = load_and_parse(acc, raw_dir)
        system, user = build_messages(
            sys_tmpl, usr_tmpl, schema_json, fewshot_json, parse, include_abstract
        )
        rec = extract_one(llm, system, user, parse, refs, logs)
        (out_dir / f"{rec.ler_number}.json").write_text(rec.model_dump_json(indent=2))
        print(f"[ok]      {rec.ler_number}: {len(rec.nodes)} nodes, {len(rec.edges)} edges "
              f"-> out/{rec.ler_number}.json")
        records.append(rec)

    logs.flush(REPO_ROOT / "logs")

    cards = score_all(records, oracle)
    for c in cards:
        print_scorecard(c)
    print_aggregate(cards)
    return cards


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Phase-4 LER extraction pipeline.")
    p.add_argument("--docs", nargs="*", default=DEFAULT_DOCS,
                   help="accession numbers to extract (default: Dresden + Limerick)")
    p.add_argument("--abstract", action="store_true",
                   help="include the block-16 abstract in the prompt "
                        "(default: narrative-only; the A/B ablation locked narrative-only)")
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--out", default=str(REPO_ROOT / "out"))
    args = p.parse_args(argv)

    run(
        accessions=args.docs,
        include_abstract=args.abstract,
        model=args.model,
        out_dir=Path(args.out),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
