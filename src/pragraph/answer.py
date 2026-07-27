"""
answer.py — Phase 6 grounded answer layer (retriever-agnostic).

Takes a question and an `Evidence` bundle (from any Retriever) and asks Claude to
write an answer grounded ONLY in that evidence, returning structured JSON so the
citations can be scored rather than regex-scraped from prose. The refusal path is
first-class: empty/insufficient evidence must yield "not answerable from this
corpus" with no citations — this is what the negative/out-of-corpus test checks.
"""
from __future__ import annotations

import os
import sys


from .llm import LLM
from .retrieve import Evidence, RISK_INTENTS, SINGLE_SUBJECT_INTENTS

ANSWER_SYSTEM = """You answer questions about U.S. NRC Licensee Event Reports (LERs) using
ONLY the EVIDENCE provided. The EVIDENCE is retrieved context about the reports: it may be
structured facts (entities and relationships) or verbatim excerpts from the source reports —
treat either form the same way and ground your answer only in what it states. Return JSON only.

Rules:
- Ground every statement in the EVIDENCE. Do NOT use outside knowledge and do NOT invent
  plants, systems, components, causes, or LER numbers.
- Cite the LER number(s) you used in `citations` (e.g. "353-2025-001-00").
- If the EVIDENCE is empty or does not contain the answer, set `answerable` to false, say so
  plainly in `answer`, and return an empty `citations` list. Never guess.
- Preserve any "[note]" caveats in the EVIDENCE (e.g. a pattern that only emerges at scale) —
  reflect that honestly rather than overclaiming.

Return: {"answerable": true/false, "answer": "...", "citations": ["...", ...]}"""

# Backstop for single-event intents: the retriever already asks to disambiguate when
# several events match, so the answerer should never see multi-event evidence here — but
# if it somehow does, refuse-and-ask rather than merge or pick one (the guessing we prevent).
SINGLE_SUBJECT_BACKSTOP = (
    "\n\nThis is a SINGLE-EVENT question. If the EVIDENCE describes more than one distinct "
    "LER, do NOT merge them or pick one — set answerable=false and ask the user to specify a "
    "single LER number.")

# Non-negotiable framing for the Phase-7 risk intents. The numbers are observed reportable-event
# frequencies within one selected corpus, not certified rates — the answer must say so, must give
# the distribution (not just a scalar), must decline a "rate" framing, and must keep every caveat.
RISK_BACKSTOP = (
    "\n\nThis is a RISK / PROBABILITY question answered ONLY from observed corpus frequencies. You "
    "MUST, in the `answer`:\n"
    "- State explicitly that these are OBSERVED reportable-event frequencies WITHIN THIS CORPUS "
    "(2020-2026 LERs), NOT certified failure rates or probabilities of failure.\n"
    "- Give the actual distribution and the event counts / n_events from the EVIDENCE — never a "
    "single bare scalar.\n"
    "- If the user asked for a 'rate' (e.g. 'failure rate'), explicitly DECLINE to give a failure "
    "rate: there is no exposure time / reactor-years here, so a rate is not computable; report the "
    "observed frequency and its denominator instead.\n"
    "- Preserve every [note] and [small-sample] caveat from the EVIDENCE, including the "
    "reporting-criterion selection bias (loss-of-safety-function is often the reporting trigger, "
    "which inflates severity) and the corpus-selection point (most-represented ≠ most-dangerous).\n"
    "- Keep `citations` empty unless the EVIDENCE lists specific LER numbers. Set answerable=true "
    "when the EVIDENCE contains stats (it is a valid, if caveated, answer).")


def answer(question: str, ev: Evidence, llm: LLM | None = None) -> dict:
    llm = llm or LLM()
    backstop = SINGLE_SUBJECT_BACKSTOP if ev.intent in SINGLE_SUBJECT_INTENTS else ""
    if ev.intent in RISK_INTENTS and not ev.empty:
        backstop += RISK_BACKSTOP
    user = (f"QUESTION:\n{question}\n\n"
            f"EVIDENCE (retrieval intent = {ev.intent}):\n{ev.text}\n\n"
            f"Return the answer JSON.{backstop}")
    obj = llm.complete_json(ANSWER_SYSTEM, user, tag="answer")
    obj.setdefault("answerable", not ev.empty)
    obj.setdefault("answer", "")
    obj.setdefault("citations", [])
    if not isinstance(obj["citations"], list):
        obj["citations"] = []
    # keep only citations the retriever actually surfaced (belt-and-suspenders vs. drift)
    allowed = ev.ler_keys()
    if allowed:
        obj["citations"] = [c for c in obj["citations"] if c in allowed]
    return obj
