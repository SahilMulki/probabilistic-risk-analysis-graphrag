"""
retrieve.py — Phase 6 graph retrieval over the Neo4j LER graph.

A question is answered in two stages, behind a retriever-agnostic seam so a vector
baseline can slot in at Phase 8 without touching the answer layer:

    Retriever.retrieve(question) -> Evidence      # graph now; vector later
    answer.answer(question, evidence)             # shared, grounded, cites LERs

GraphRetriever routing is "LLM router + Cypher templates":
  1. GraphVocab pulls the graph's real controlled vocabulary (system codes+names,
     cause categories, plants, LER keys) from Neo4j.
  2. An LLM classifies the question into one of a fixed INTENTS set and extracts
     anchors *constrained to that vocabulary* — so it can only ever point at nodes
     that exist. Anything it can't ground becomes `out_of_corpus` (empty evidence),
     which is what lets the answerer refuse instead of hallucinating.
  3. Each intent dispatches to a parameterized Cypher template (the showcase paths
     from graph/queries.cypher) or a generic k-hop subgraph fallback.

Text2Cypher (LLM writes raw Cypher) is deliberately out of scope — too brittle for
a robust demo.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field


from . import risk
from .llm import LLM
from .load_graph import _connect

# --------------------------------------------------------------------------- #
# intents
# --------------------------------------------------------------------------- #
INTENTS = {
    "failure_chain":
        "the cause / ordered failure chain of a SINGLE event — a 'what caused' or "
        "'what led to X' question. Anchor on plant and/or system and/or LER number; "
        "use this even when only a system is named. If several events match, the "
        "system asks the user to disambiguate rather than guessing.",
    "system_components":
        "components that failed in/on a given system across the corpus (needs a system)",
    "system_failure_modes":
        "AGGREGATE: the TYPES of failure modes (what mechanically went wrong) for a given "
        "system grouped ACROSS the corpus — 'group all events', 'most common failure mode', "
        "'what failure modes across all reports', 'how does X fail'. This is about failure "
        "MODES (kinds of failure), NOT probabilities/rates/outcomes (a 'failure RATE', 'how "
        "often', or 'how likely' question is likely_outcome). Needs a system.",
    "mitigating_backups":
        "events where a redundant/backup safety system was available",
    "cause_distribution":
        "the distribution of cause categories across the corpus",
    "weak_program_events":
        "events attributed to personnel error / weak maintenance or procedure programs",
    "shared_component_cause":
        "events at different plants sharing BOTH a common component and a common cause",
    "risk_ranking":
        "PROBABILISTIC/RISK ranking: which systems or causes contribute the most observed risk "
        "(how often × how severe) across the corpus — 'which systems are riskiest / most "
        "significant', 'rank by risk', 'biggest risk contributors', 'highest observed risk'. An "
        "aggregate ranking of the whole corpus, never a single event.",
    "likely_outcome":
        "PROBABILISTIC outcome distribution / RATE / LIKELIHOOD for a given system, cause, or "
        "COMPONENT — 'what safety outcome is most likely / most probable when X fails', 'what "
        "usually results from X', 'probability / chance of a reactor trip given X', 'how likely "
        "is a loss of safety function for X', AND rate/frequency phrasings: 'failure RATE of X', "
        "'how OFTEN does X fail'. Needs a system_code, cause_code, OR component; aggregates over "
        "the corpus. About OUTCOMES/consequences and their probabilities/rates — NOT the types of "
        "failure modes (system_failure_modes) and NOT cause categories (cause_distribution).",
    "probable_path":
        "the single MOST-PROBABLE cause→outcome path for a given system OR COMPONENT — 'most "
        "likely failure path / sequence / progression for X', 'what is the most probable way X "
        "fails and what results', 'given X degrades, what is the most probable path to a safety "
        "consequence'. Needs a system_code OR a component. Probabilistic, aggregate over the "
        "corpus.",
    "faceted_frequency":
        "GENERAL faceted query engine — use for any 'among the EVENTS matching some CONDITIONS, "
        "count/compare/trend a FACET' shape the specific intents above do NOT cover. Covers: "
        "REVERSE ('what causes/systems/components/plants are in events that RESULT IN outcome X or "
        "consequence Y', 'what leads to loss of offsite power'); COMBINATION / co-occurrence and "
        "PAIRS ('what components co-occur / which pairs of components appear together in reactor-trip "
        "events'); COMPOUND conditions ('components in PERSONNEL-ERROR events that led to LOSS OF "
        "FUNCTION'); plant counts; TEMPORAL trends ('reactor trips by year', 'is X rising'); NUMERIC "
        "thresholds ('events above 90% power', 'outages over 24 hours'); CORRECTIVE ACTIONS / "
        "resolutions ('how were X events resolved'); and COMPARISON ('compare HPCI vs RCIC outcome "
        "profiles'). Anchors: target (systems|components|causes|outcomes|plants|corrective_actions|"
        "years = WHAT TO COUNT); filters (a LIST of facet/value conditions, ALL required); pairs=true "
        "for co-occurring pairs; compare (facet + values) for side-by-side. See the anchor rules below.",
    "subgraph":
        "a general neighborhood around one named entity (fallback)",
    "out_of_corpus":
        "the question is not answerable from this corpus of LERs",
}

# Intents that address ONE event, so a match against several distinct events is
# ambiguous and must trigger a Clarification (not a silent guess). Aggregate intents
# (system_components, cause_distribution, ...) are MEANT to span events -> exempt.
SINGLE_SUBJECT_INTENTS = {"failure_chain"}

# Phase-7 risk intents: their answers MUST carry the observed-frequency framing (denominator,
# "within this corpus", the reporting-criterion selection bias, the full distribution, and
# small-sample flags). answer.py enforces this via a mandatory backstop.
RISK_INTENTS = {"risk_ranking", "likely_outcome", "probable_path", "faceted_frequency"}

# Most candidates we list in a Clarification before telling the user to narrow instead.
CANDIDATE_CAP = 8


# --------------------------------------------------------------------------- #
# Evidence — the retriever-agnostic hand-off to the answer layer
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    intent: str
    anchors: dict
    text: str                                  # serialized subgraph for the answerer
    node_keys: list[str] = field(default_factory=list)   # match_keys surfaced (for scoring)
    lers: list[dict] = field(default_factory=list)       # [{key, source}] cited-able
    empty: bool = False

    def ler_keys(self) -> set[str]:
        return {l["key"] for l in self.lers}


# --------------------------------------------------------------------------- #
# Clarification — the third outcome besides answer / refuse
# --------------------------------------------------------------------------- #
@dataclass
class Clarification:
    """A single-subject question matched MULTIPLE candidate events, so we ask the user
    to disambiguate instead of silently picking one. Detected structurally in the
    retriever by candidate cardinality (>1) — never by letting the answer LLM guess.
    Single-shot: we return the candidates and the user re-asks (primary re-ask path is
    by LER number, which is unambiguous)."""
    intent: str
    anchors: dict
    question: str                                        # disambiguation prompt to show
    candidates: list[dict] = field(default_factory=list) # capped, sorted by event_date desc
    total: int = 0                                       # candidate count before the cap
    overflow: bool = False                               # total > CANDIDATE_CAP (some hidden)

    def candidate_keys(self) -> set[str]:
        return {c["key"] for c in self.candidates}


# --------------------------------------------------------------------------- #
# graph vocabulary (constrains the router to real nodes)
# --------------------------------------------------------------------------- #
@dataclass
class GraphVocab:
    # Only the BOUNDED controlled vocabularies go in the router prompt: systems
    # (~50 EIIS codes) and cause categories (~6). Plants (~100) and LER numbers
    # (100s–1000s) scale with the corpus, so they are NOT listed — the router
    # extracts them as free text and the retriever resolves them deterministically
    # in Cypher (plant CONTAINS, LER-number exact). Keeps the prompt O(1) in corpus
    # size and underpins the clarify feature's LER-number re-ask at scale.
    systems: list[dict]      # [{code, name}]
    causes: list[dict]       # [{code, category}]

    @classmethod
    def load(cls, session) -> "GraphVocab":
        systems = [dict(r) for r in session.run(
            "MATCH (s:System) RETURN DISTINCT s.eiis_code AS code, s.display_name AS name "
            "ORDER BY name")]
        causes = [dict(r) for r in session.run(
            "MATCH (c:Cause) RETURN DISTINCT c.cause_code AS code, c.category AS category "
            "ORDER BY code")]
        return cls(systems=systems, causes=causes)

    def as_prompt(self) -> str:
        return (
            "SYSTEMS (eiis_code — name):\n"
            + "\n".join(f"  {s['code'] or '(none)'} — {s['name']}" for s in self.systems)
            + "\n\nCAUSE CATEGORIES (code — category):\n"
            + "\n".join(f"  {c['code']} — {c['category']}" for c in self.causes)
        )


# --------------------------------------------------------------------------- #
# router
# --------------------------------------------------------------------------- #
ROUTER_SYSTEM = """You route a natural-language question to ONE retrieval intent over a
knowledge graph of U.S. NRC Licensee Event Reports (LERs), and extract anchors. Return
JSON only.

Intents:
{intents}

Rules:
- Choose exactly one `intent`.
- `anchors` may include:
  - system_code: an eiis_code from the SYSTEMS vocabulary below, or the string "ADS" for
    the Automatic Depressurization System. "HPCI" is BJ; "RCIC" is BN. Use ONLY a code that
    appears in the vocabulary; if the question's system is not there, omit system_code.
  - cause_code: a code from the CAUSE CATEGORIES vocabulary below (omit if none applies).
  - plant: the plant name as written in the question (FREE TEXT — a plant list is NOT
    provided; extract what the user wrote, e.g. "Limerick", "Quad Cities"). The retriever
    validates it against the graph.
  - ler_key: an LER number the user typed, verbatim and formatted like "237-2025-003-00"
    (FREE TEXT). Extract it whenever the user gives one — this is how a user disambiguates.
  - component: a COMPONENT the user names for a risk/likely_outcome/probable_path question, as
    FREE TEXT (a component list is NOT provided) — e.g. "relay", "motor-operated valve",
    "breaker", "service water pump", "battery charger". Extract the component noun the user
    degrades/asks about. The retriever resolves it against the graph's component hubs. Only set
    this when the question is about a component rather than a whole system.
  - target: for faceted_frequency ONLY — WHAT TO COUNT: one of "systems", "components", "causes",
    "outcomes", "plants", "corrective_actions", "years".
  - filters: for faceted_frequency ONLY — a LIST of conditions, ALL of which must hold (AND). Each
    is {{"facet": ..., "value": ..., "op": ...(optional)}}. facet is one of "outcome", "system",
    "cause", "component", "consequence", "plant", "year", "power_level", "duration". value is the
    value — an outcome phrase ("reactor trip", "loss of safety function"), a cause category, a
    system/component/plant name, a year like "2024", a FREE-TEXT consequence phrase ("loss of
    offsite power"), or a number. For "power_level" (a %) or "duration" (in hours) set "op" to one of
    >= <= > < == and value to a number. Omit filters to count over all events. The 8 outcome
    classes: loss-of-safety-function, safety-system-inoperable, reactor-trip-or-scram, esf-actuation,
    containment-isolation, degraded-not-lost, ts-violation-only, other-or-no-safety-impact.
  - pairs: for faceted_frequency ONLY — true when the user asks which PAIRS / combinations of the
    target CO-OCCUR together, not just individual frequencies.
  - compare: for faceted_frequency ONLY — {{"facet": ..., "values": [a, b, …]}} to show the target
    distribution SIDE BY SIDE for two or more entities (e.g. compare HPCI vs RCIC → facet "system",
    values ["HPCI","RCIC"]).
- Do NOT invent system/cause codes. Plant, LER-number, and free-text anchors are resolved against the
  graph, so if the plant/LER is not in the corpus the retriever returns nothing and the
  answerer refuses — that is the intended behavior; you need not pre-check them.
- Use "out_of_corpus" only when the question is clearly not about an in-corpus LER topic at
  all (e.g. a different domain, or a system/event with no relation to these reports).

Return: {{"intent": "...", "anchors": {{...}}, "reason": "one short clause"}}"""


def route(question: str, vocab: GraphVocab, llm: LLM) -> dict:
    system = ROUTER_SYSTEM.format(
        intents="\n".join(f"  - {k}: {v}" for k, v in INTENTS.items()))
    user = f"VOCABULARY:\n{vocab.as_prompt()}\n\nQUESTION:\n{question}\n\nReturn the routing JSON."
    obj = llm.complete_json(system, user, tag="route")
    obj.setdefault("intent", "out_of_corpus")
    obj.setdefault("anchors", {})
    if obj["intent"] not in INTENTS:
        obj["intent"] = "subgraph"
    return obj


# --------------------------------------------------------------------------- #
# GraphRetriever
# --------------------------------------------------------------------------- #
class GraphRetriever:
    def __init__(self, llm: LLM | None = None):
        self.driver = _connect()
        self.driver.verify_connectivity()
        self.llm = llm or LLM()
        with self.driver.session() as s:
            self.vocab = GraphVocab.load(s)
            self.plants = [r["p"] for r in s.run(
                "MATCH (l:LER) WHERE NOT l.stub AND l.plant_name IS NOT NULL "
                "RETURN DISTINCT l.plant_name AS p")]

    def close(self):
        self.driver.close()

    # -- public ------------------------------------------------------------- #
    def retrieve(self, question: str) -> "Evidence | Clarification":
        r = route(question, self.vocab, self.llm)
        intent, anchors = r["intent"], r["anchors"]
        if anchors.get("plant"):
            anchors["plant"] = self._resolve_plant(anchors["plant"])
        with self.driver.session() as s:
            handler = getattr(self, f"_t_{intent}", None)
            if handler is None:
                return self._empty(intent, anchors)
            return handler(s, anchors)

    def _resolve_plant(self, raw: str) -> str:
        """Canonicalize a free-text plant name to a corpus plant so a near-miss ('Brown
        Ferry' → 'Browns Ferry', 'Diablo' → 'Diablo Canyon') resolves instead of refusing.
        The exact-substring fast path is unchanged (existing matches keep working); only a
        non-substring name gets fuzzy-matched, and only above a high cutoff — so a genuinely
        out-of-corpus plant ('Three Mile Island', 'Chernobyl') still falls through to refusal."""
        raw = (raw or "").strip()
        if not raw:
            return raw
        low = raw.lower()
        if any(low in p.lower() for p in self.plants):
            return raw
        from rapidfuzz import fuzz, process
        m = process.extractOne(raw, self.plants, scorer=fuzz.token_set_ratio)
        return m[0] if m and m[1] >= 88 else raw

    # -- helpers ------------------------------------------------------------ #
    def _empty(self, intent, anchors) -> Evidence:
        return Evidence(intent=intent, anchors=anchors,
                        text="(no matching evidence in the corpus)", empty=True)

    def _candidate_lers(self, s, anchors) -> list[dict]:
        """The candidate set for a single-subject intent: every non-stub LER matching
        ALL pinned anchors (LER number exact; plant substring; system membership).
        Sorted by event_date descending so the most recent events show first. This is
        what the cardinality branch counts — 0 refuse / 1 answer / >1 clarify."""
        if anchors.get("ler_key"):
            preds, params = ["l.key = $ler_key"], {"ler_key": anchors["ler_key"]}
        else:
            preds, params = [], {}
            if anchors.get("plant"):
                preds.append("toLower(l.plant_name) CONTAINS toLower($plant)")
                params["plant"] = anchors["plant"]
            code = (anchors.get("system_code") or "").strip()
            if code:
                if code.upper() == "ADS":
                    preds.append("EXISTS { (l)-[:INVOLVES]->(:System "
                                 "{match_key:'System:automatic-depressurization-system'}) }")
                else:
                    preds.append("EXISTS { (l)-[:INVOLVES]->(:System {eiis_code:$system_code}) }")
                    params["system_code"] = code
        where = " AND ".join(preds) if preds else "true"
        rows = s.run(
            f"MATCH (l:LER) WHERE NOT l.stub AND {where} "
            "OPTIONAL MATCH (l)-[:INVOLVES]->(sys:System) "
            "WITH l, collect(DISTINCT coalesce(sys.eiis_code, sys.display_name)) AS systems "
            "RETURN l.key AS key, l.plant_name AS plant, l.event_date AS event_date, "
            "  l.title AS title, l.source AS source, systems "
            "ORDER BY l.event_date DESC, l.key", **params)
        return [dict(r) for r in rows]

    def _clarify(self, intent, anchors, cands) -> Clarification:
        total = len(cands)
        shown = cands[:CANDIDATE_CAP]
        overflow = total > CANDIDATE_CAP
        q = (f"That matches {total} events in the corpus — which one do you mean? "
             "Re-ask with a specific LER number"
             + (", or narrow it down by adding an event year (some matches are not shown)."
                if overflow else " from the list below."))
        return Clarification(intent=intent, anchors=anchors, question=q,
                             candidates=shown, total=total, overflow=overflow)

    @staticmethod
    def _sys_match(anchors) -> tuple[str, dict]:
        """Return (cypher_predicate, params) selecting a System by code or ADS."""
        code = (anchors.get("system_code") or "").strip()
        if code.upper() == "ADS":
            return ("s.match_key = 'System:automatic-depressurization-system'", {})
        return ("s.eiis_code = $code", {"code": code})

    # -- templates ---------------------------------------------------------- #
    def _t_failure_chain(self, s, anchors) -> "Evidence | Clarification":
        # Single-subject: resolve the candidate set, then branch on cardinality.
        # 0 -> refuse (empty); >1 -> clarify (don't guess which event); 1 -> answer.
        cands = self._candidate_lers(s, anchors)
        if not cands:
            return self._empty("failure_chain", anchors)
        if len(cands) > 1:
            return self._clarify("failure_chain", anchors, cands)
        ler = cands[0]["key"]
        # Constrain EVERY hop to this LER's own edges (ler_number). The Cause node is a
        # shared cross-document hub, so without this the chain fans out through the hub
        # into every other event sharing the cause category (a scale-only explosion).
        rows = list(s.run(
            "MATCH (l:LER {key:$ler})-[:HAS_CAUSE {ler_number:$ler}]->(cause:Cause) "
            "MATCH (cause)<-[:CAUSED_BY {ler_number:$ler}]-(origin:FailureMode) "
            "MATCH path=(origin)-[:LEADS_TO*0.. {ler_number:$ler}]->(cons:Consequence) "
            "WITH l, cause, path, cons "
            "OPTIONAL MATCH (cons)-[:BACKED_UP_BY {ler_number:$ler}]->(bk:System) "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  cause.display_name AS cause, cause.category AS category, "
            "  [n IN nodes(path) | n.display_name] AS chain, "
            "  [n IN nodes(path) | n.match_key] AS chain_keys, "
            "  collect(DISTINCT coalesce(bk.eiis_code, bk.display_name)) AS backups", ler=ler))
        if not rows:
            return self._empty("failure_chain", anchors)
        keys, lines = set(), []
        src = rows[0]["source"]
        for r in rows:
            keys.update(k for k in r["chain_keys"] if k)
            lines.append(f"LER {r['ler']} ({r['plant']}, source={r['source']}) — "
                         f"cause: {r['cause']}\n    chain: " + " -> ".join(r["chain"])
                         + (f"\n    backups available: {', '.join(b for b in r['backups'] if b)}"
                            if any(r["backups"]) else ""))
        return Evidence("failure_chain", anchors, "\n".join(lines),
                        node_keys=sorted(keys),
                        lers=[{"key": rows[0]["ler"], "source": src}])

    def _t_system_components(self, s, anchors) -> Evidence:
        pred, params = self._sys_match(anchors)
        rows = list(s.run(
            f"MATCH (l:LER)-[:INVOLVES]->(s:System) WHERE NOT l.stub AND {pred} "
            "WITH collect(DISTINCT l.key) AS lers "
            "MATCH (c:Component)-[e]-() WHERE e.ler_number IN lers "
            "WITH DISTINCT c, e.ler_number AS ler "
            "MATCH (l:LER {key:ler}) "
            "RETURN c.display_name AS component, c.eiis_code AS code, "
            "  c.match_key AS mk, ler, l.source AS source, l.plant_name AS plant "
            "ORDER BY ler, component", **params))
        if not rows:
            return self._empty("system_components", anchors)
        keys = sorted({r["mk"] for r in rows})
        lers = {(r["ler"], r["source"]) for r in rows}
        lines = [f"  {r['component']} [{r['code'] or 'no EIIS code'}] "
                 f"— LER {r['ler']} ({r['plant']}, {r['source']})" for r in rows]
        text = ("Components on the queried system across the corpus:\n" + "\n".join(lines))
        return Evidence("system_components", anchors, text, node_keys=keys,
                        lers=[{"key": k, "source": v} for k, v in sorted(lers)])

    def _t_system_failure_modes(self, s, anchors) -> Evidence:
        pred, params = self._sys_match(anchors)
        # per-LER edge filter (l.key) so the chain stays within each event and does not
        # fan out through the shared Cause hub across the whole corpus.
        rows = list(s.run(
            f"MATCH (l:LER)-[:INVOLVES]->(s:System) WHERE NOT l.stub AND {pred} "
            "MATCH (l)-[:HAS_CAUSE {ler_number:l.key}]->(:Cause)"
            "<-[:CAUSED_BY {ler_number:l.key}]-(:FailureMode)"
            "-[:LEADS_TO*0.. {ler_number:l.key}]->(fm:FailureMode) "
            "RETURN fm.match_key AS mk, fm.display_name AS name, "
            "  collect(DISTINCT l.key) AS lers, count(DISTINCT l) AS n "
            "ORDER BY n DESC, name", **params))
        if not rows:
            return self._empty("system_failure_modes", anchors)
        keys = sorted({r["mk"] for r in rows})
        lers = sorted({x for r in rows for x in r["lers"]})
        lines = [f"  {r['name']}  (in {r['n']} event(s): {', '.join(r['lers'])})" for r in rows]
        text = ("Failure modes on the queried system, grouped across the corpus by "
                "semantic key:\n" + "\n".join(lines)
                + "\n[note] with 3 documents most failure modes appear once; this "
                  "grouping produces a meaningful 'most common' only at corpus scale.")
        return Evidence("system_failure_modes", anchors, text, node_keys=keys,
                        lers=[{"key": k, "source": None} for k in lers])

    def _t_mitigating_backups(self, s, anchors) -> Evidence:
        # per-LER edge filter (l.key) to keep each event's chain within itself.
        rows = list(s.run(
            "MATCH (l:LER)-[:HAS_CAUSE {ler_number:l.key}]->(:Cause)"
            "<-[:CAUSED_BY {ler_number:l.key}]-()-[:LEADS_TO*0.. {ler_number:l.key}]->"
            "(cons:Consequence)-[:BACKED_UP_BY {ler_number:l.key}]->(bk:System) WHERE NOT l.stub "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  cons.display_name AS consequence, "
            "  collect(DISTINCT coalesce(bk.eiis_code, bk.display_name)) AS backups "
            "ORDER BY ler"))
        if not rows:
            return self._empty("mitigating_backups", anchors)
        lines = [f"  LER {r['ler']} ({r['plant']}, {r['source']}): {r['consequence']} "
                 f"— backups available: {', '.join(b for b in r['backups'] if b)}" for r in rows]
        return Evidence("mitigating_backups", anchors,
                        "Events mitigated by an available redundant safety system:\n"
                        + "\n".join(lines),
                        node_keys=[],
                        lers=[{"key": r["ler"], "source": r["source"]} for r in rows])

    def _t_cause_distribution(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l:LER)-[:HAS_CAUSE]->(c:Cause) WHERE NOT l.stub "
            "RETURN c.category AS category, c.cause_code AS code, "
            "  collect(l.key) AS lers, count(*) AS n ORDER BY n DESC, category"))
        lines = [f"  {r['category']} [{r['code']}]: {r['n']} event(s) — {', '.join(r['lers'])}"
                 for r in rows]
        lers = sorted({x for r in rows for x in r["lers"]})
        return Evidence("cause_distribution", anchors,
                        "Cause-category distribution across the corpus:\n" + "\n".join(lines),
                        node_keys=[f"Cause:{r['category']}" for r in rows],
                        lers=[{"key": k, "source": None} for k in lers])

    def _t_weak_program_events(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l:LER)-[hc:HAS_CAUSE]->(c:Cause) "
            "WHERE NOT l.stub AND c.cause_code = 'A' "
            "RETURN l.key AS ler, l.source AS source, l.plant_name AS plant, "
            "  c.category AS category, hc.theme AS theme, hc.proximate_text AS proximate "
            "ORDER BY ler"))
        if not rows:
            return self._empty("weak_program_events", anchors)
        lines = [f"  LER {r['ler']} ({r['plant']}, {r['source']}): {r['category']} — "
                 f"{r['theme'] or r['proximate'] or ''}" for r in rows]
        text = ("Events attributed to personnel error / weak program:\n" + "\n".join(lines)
                + "\n[note] the cross-plant 'weak program' pattern is a scale result; "
                  "at 3 documents only one such event exists.")
        return Evidence("weak_program_events", anchors, text,
                        node_keys=[], lers=[{"key": r["ler"], "source": r["source"]} for r in rows])

    def _t_shared_component_cause(self, s, anchors) -> Evidence:
        rows = list(s.run(
            "MATCH (l1:LER)-[:INVOLVES]->(comp:Component)<-[:INVOLVES]-(l2:LER), "
            "  (l1)-[:HAS_CAUSE]->(cause:Cause)<-[:HAS_CAUSE]-(l2) "
            "WHERE l1.key < l2.key AND l1.plant_name <> l2.plant_name "
            "RETURN l1.key AS a, l2.key AS b, comp.display_name AS component, "
            "  cause.category AS cause"))
        if not rows:
            return Evidence("shared_component_cause", anchors,
                            "No two different plants in the current corpus share BOTH a "
                            "component and a cause. This cross-document join is built into "
                            "the schema (coded components and cause categories are shared "
                            "hubs) and will surface once the corpus is scaled up.",
                            node_keys=[], lers=[], empty=True)
        lines = [f"  {r['a']} & {r['b']}: component {r['component']}, cause {r['cause']}"
                 for r in rows]
        return Evidence("shared_component_cause", anchors,
                        "Cross-plant events sharing a component and a cause:\n" + "\n".join(lines),
                        lers=[{"key": r["a"], "source": None} for r in rows]
                             + [{"key": r["b"], "source": None} for r in rows])

    # -- risk templates (Phase 7) ------------------------------------------- #
    # These read the stats materialize() stamped on the hub nodes; probable_path builds the
    # transition graph live (a graph algorithm, not a single-node read). All frequencies are
    # observed reportable-event counts within this corpus — see risk.OBSERVED_RISK_CAVEAT, which
    # is embedded in the evidence so the grounded answer can't drop it.
    @staticmethod
    def _sys_mk(anchors) -> str | None:
        code = (anchors.get("system_code") or "").strip()
        if not code:
            return None
        if code.upper() == "ADS":
            return "System:automatic-depressurization-system"
        return f"System:{code}"

    @staticmethod
    def _resolve_component(s, name: str) -> tuple[str, str] | None:
        """Free-text component name -> (match_key, display_name) of the best-matching materialized
        Component hub (the most-represented hub whose name contains the query). Components are
        fine-grained and 94% single-event, so this deliberately prefers the EIIS-code category hub
        with the most events (e.g. 'relay' -> Component:RLY across many events)."""
        name = (name or "").strip()
        if not name:
            return None
        rows = list(s.run(
            "MATCH (c:Component) WHERE c.n_events IS NOT NULL "
            "  AND toLower(c.display_name) CONTAINS toLower($q) "
            "RETURN c.match_key AS mk, c.display_name AS name ORDER BY c.n_events DESC LIMIT 1",
            q=name))
        return (rows[0]["mk"], rows[0]["name"]) if rows else None

    def _seed_entity(self, s, anchors) -> tuple[str | None, str | None]:
        """Resolve a risk-path/outcome seed to (match_key, kind) — a System (by code) or a
        Component (by free-text name). System takes precedence when both are present."""
        mk = self._sys_mk(anchors)
        if mk:
            return mk, "system"
        comp = self._resolve_component(s, anchors.get("component") or "")
        if comp:
            return comp[0], "component"
        return None, None

    def _risk_unmaterialized(self, intent, anchors) -> Evidence:
        return Evidence(intent, anchors,
                        "(the risk layer has not been materialized yet — run "
                        "`python -m pragraph.classify_outcomes --run` then "
                        "`python -m pragraph.risk --materialize`)", empty=True)

    @staticmethod
    def _dist_lines(counts: dict, n: int, order_by_prob: bool = True) -> list[str]:
        dist = risk.normalize(counts)
        items = sorted(dist.items(), key=(lambda kv: -kv[1]) if order_by_prob
                       else (lambda kv: -risk.SEVERITY[kv[0]]))
        return [f"    {o:26} {p:5.0%}  ({counts.get(o, 0)} events)  [sev {risk.SEVERITY[o]}]"
                for o, p in items]

    def _t_risk_ranking(self, s, anchors) -> Evidence:
        sysstats = risk.load_materialized_stats(s, "System")
        if not sysstats:
            return self._risk_unmaterialized("risk_ranking", anchors)
        ranked = risk.rank_by_risk(sysstats, top_n=10)
        sens = risk.ranking_sensitivity(sysstats, top_n=3)
        cranked = risk.rank_by_risk(risk.load_materialized_stats(s, "Cause"), top_n=6)
        lines = ["Observed risk-contribution ranking (ORC = n_events_classified × "
                 "expected_severity) — WITHIN THIS CORPUS.", "",
                 "Systems (top 10 by observed_risk_contribution):"]
        lines += [f"  {i:2}. {st.line()}" for i, st in enumerate(ranked, 1)]
        if cranked:
            lines += ["", "Cause categories (by observed_risk_contribution):"]
            lines += [f"  {i:2}. {st.line()}" for i, st in enumerate(cranked, 1)]
        lines += ["", "  " + sens.summary(), "",
                  "[note] " + risk.OBSERVED_RISK_CAVEAT,
                  "[note] This ranking is dominated by n_events, a corpus-SELECTION artifact: a "
                  "high rank means MOST-REPRESENTED in this 2020-2026 export, not most-dangerous."]
        return Evidence("risk_ranking", anchors, "\n".join(lines),
                        node_keys=[st.key for st in ranked], lers=[])

    def _t_likely_outcome(self, s, anchors) -> Evidence:
        mk = self._sys_mk(anchors)
        etype, stats = None, None
        sysall = risk.load_materialized_stats(s, "System")
        if not sysall:
            return self._risk_unmaterialized("likely_outcome", anchors)
        if mk:
            etype, stats = "system", sysall.get(mk)
        elif anchors.get("cause_code"):
            etype = "cause"
            stats = next((v for v in risk.load_materialized_stats(s, "Cause").values()
                          if v.code == anchors["cause_code"]), None)
        elif anchors.get("component"):
            comp = self._resolve_component(s, anchors["component"])
            if comp:
                etype, stats = "component", risk.load_materialized_stats(s, "Component").get(comp[0])
        if stats is None:
            return self._empty("likely_outcome", anchors)
        corpus_counts, n_total, n_class = risk.corpus_outcome_distribution(s)
        m = stats.modal
        lines = [f"Observed outcome distribution for {stats.label} "
                 f"[{stats.code or '—'}] — WITHIN THIS CORPUS ({etype}):",
                 f"  n_events (reportable events involving it): {stats.n_events}",
                 f"  n_classified (with a usable outcome): {stats.n_classified}  "
                 f"(coverage {stats.coverage:.0%})",
                 f"  expected_severity: {stats.expected_severity:.2f}  (ordinals 1-5 as interval)",
                 f"  modal outcome: {m[0]} ({m[1]:.0%})" if m else "  modal outcome: —",
                 f"  P(outcome | this {etype}) over {stats.n_classified} classified events:"]
        lines += self._dist_lines(stats.counts, stats.n_classified)
        lines += ["  corpus baseline P(outcome) over all "
                  f"{n_class} classified events (for comparison):"]
        lines += self._dist_lines(corpus_counts, n_class)
        if stats.small_sample:
            lines.append(f"  [small-sample] n_classified={stats.n_classified} "
                         f"(≤{risk.SMALL_SAMPLE_N}) — illustrative, not a stable probability.")
        lines += ["[note] " + risk.OBSERVED_RISK_CAVEAT]
        return Evidence("likely_outcome", anchors, "\n".join(lines),
                        node_keys=[stats.key], lers=[])

    def _t_probable_path(self, s, anchors) -> Evidence:
        if not risk.load_materialized_stats(s, "System"):
            return self._risk_unmaterialized("probable_path", anchors)
        seed_mk, kind = self._seed_entity(s, anchors)
        if not seed_mk:
            return self._empty("probable_path", anchors)          # needs a system or component
        trans = risk.build_transitions(s)
        label = trans.entity_label.get(seed_mk, seed_mk)
        path = risk.most_probable_path(trans, seed_mk)
        if path is None:
            return Evidence("probable_path", anchors,
                            f"No most-probable cause→outcome path is computable for {label} "
                            f"({kind}): its events here have mostly provisional (uncoded) causes, "
                            "so the cause layer is empty (~63% of corpus events have no coded root "
                            "cause; most individual components appear in a single event).",
                            node_keys=[seed_mk], lers=[])
        basis = ("P(cause|component) over this component's coded-cause events"
                 if kind == "component" else "P(cause|system) over this system's coded-cause events")
        sparsity = ("\n[note] Component-level is sparse — 94% of components appear in a single "
                    "event; treat this as illustrative of the technique." if kind == "component" else "")
        text = (f"Most-probable failure path from {label} ({kind}) — WITHIN THIS CORPUS:\n  "
                + path.render(label_of=trans.entity_label.get)
                + f"\n\n  Basis: {basis}; P(outcome|cause) over that cause's events corpus-wide "
                  "(distinct events).\n"
                  "[note] Rests on the coded-cause subset (~63% of corpus events have a "
                  "provisional/uncoded cause); illustrative of the technique, not a predictive "
                  f"rate.{sparsity}\n[note] " + risk.OBSERVED_RISK_CAVEAT)
        return Evidence("probable_path", anchors, text,
                        node_keys=[seed_mk], lers=[])

    def _t_faceted_frequency(self, s, anchors) -> Evidence:
        # The GENERAL engine: "among events matching FILTERS, count / trend / compare a FACET." One
        # primitive covers reverse, combination + true PAIRS, compound-AND, plant / year /
        # corrective-action facets, numeric thresholds, and side-by-side comparison — no per-shape
        # template. Everything counts DISTINCT events; the observed-frequency framing is embedded.
        target = (anchors.get("target") or "outcomes").lower()
        if target not in risk.FACETS:
            target = "outcomes"
        filters = list(anchors.get("filters") or [])
        if anchors.get("filter_facet"):                 # back-compat: legacy single filter
            filters.append({"facet": anchors["filter_facet"], "value": anchors.get("filter_value"),
                            "op": anchors.get("filter_op")})
        pairs = bool(anchors.get("pairs"))
        compare = anchors.get("compare")
        facets = risk.event_facets(s)

        # --- comparative: two+ distributions side by side --------------------
        if isinstance(compare, dict) and compare.get("facet") and compare.get("values"):
            cmp = risk.compare_facets(facets, target, compare["facet"], compare["values"], filters)
            if all(r["n_matched"] == 0 for r in cmp.values()):
                return Evidence("faceted_frequency", anchors,
                                f"No events match any of {compare['values']} for "
                                f"{compare['facet']} in this corpus.", empty=True)
            lines = [f"Side-by-side comparison of {target} — distinct events WITHIN THIS CORPUS:"]
            lers = []
            for val, r in cmp.items():
                lines.append(f"\n  {compare['facet']} = {val}: {r['n_matched']} events")
                for v, c in sorted(r["counts"].items(), key=lambda kv: -kv[1])[:8]:
                    pct = f" ({c/r['n_matched']:.0%})" if r["n_matched"] else ""
                    lines.append(f"    {c:3}{pct}  {v}")
                lers += [{"key": f.ler, "source": None} for f in r["matched"][:10]]
            lines += ["[note] " + risk.OBSERVED_RISK_CAVEAT]
            return Evidence("faceted_frequency", anchors, "\n".join(lines), node_keys=[], lers=lers)

        # --- single distribution / pairs / trend / listing ------------------
        res = risk.faceted_frequency(facets, target, filters, pairs=pairs)
        n = res["n_matched"]
        if n == 0:                                       # honest empty (e.g. fuel cladding: absent)
            return Evidence("faceted_frequency", anchors,
                            f"No events in this corpus match {res['filter_desc']}, so there is "
                            "nothing to count. (This 2020-2026 LER export may simply not contain "
                            "that kind of event.)", empty=True)
        lines = ["Faceted frequency — distinct events WITHIN THIS CORPUS.",
                 f"  filter: {res['filter_desc']}  ({n} matching events)"]

        if pairs and res["pairs"]:
            ranked = sorted(res["pairs"].items(), key=lambda kv: -kv[1])
            lines.append(f"  co-occurring {target} PAIRS (appearing together in the same event):")
            for (a, b), c in ranked[:12]:
                lines.append(f"    {c:3} event(s)   {a}  +  {b}")
            if not any(c >= 2 for _, c in ranked):
                lines.append("  [note] every pair co-occurs in only one event — no repeated "
                             "combination at this corpus scale.")
        elif target == "years":
            import datetime as _dt
            today = _dt.date.today()
            frac = today.timetuple().tm_yday / 365.0
            lines.append(f"  events by year (the trend; today is {today.isoformat()}):")
            for y in sorted(res["counts"]):
                note = ""
                try:
                    yi = int(y)
                    if yi >= today.year:                # the system KNOWS the year isn't over
                        note = (f"  (INCOMPLETE — {y} is not over yet; only ~{frac:.0%} of the year "
                                "has elapsed, so this is a year-to-date partial count)")
                    elif yi < 2020:
                        note = "  (pre-2020 straggler — a supplement citing an older event; outside the 2020-2026 window)"
                except ValueError:
                    pass
                lines.append(f"    {y}: {res['counts'][y]}{note}")
            lines.append(f"  [note] Today is {today.isoformat()}, so {today.year} is only a "
                         "year-to-date partial count and is NOT comparable to a full year. Even the "
                         "latest complete years may be slightly under-counted due to LER reporting "
                         "lag. Judge the trend only across the complete years.")
        elif target == "corrective_actions":
            lines.append(f"  {len(res['counts'])} distinct corrective actions across those events "
                         "(per-event free text, mostly unique) — a sample of how they were resolved:")
            for f in res["matched"][:10]:
                for ca in sorted(f.corrective_actions)[:2]:
                    lines.append(f"    {f.ler}: {ca[:90]}")
        else:
            ranked = sorted(res["counts"].items(), key=lambda kv: -kv[1])
            lines.append(f"  count of {target} across those events (an event can involve several):")
            for v, c in ranked[:15]:
                lines.append(f"    {c:3}  ({c/n:.0%} of matched events)  {v}")
            if len(ranked) > 15:
                lines.append(f"    … and {len(ranked) - 15} more {target}.")
            if target in ("components", "systems"):
                lines.append("  sample matching events (the co-occurring set within each event):")
                for f in res["matched"][:6]:
                    vals = sorted(risk._facet_values(f, target))
                    lines.append(f"    {f.ler} ({f.plant or '—'}): " + (", ".join(vals[:8]) or "—"))

        if n <= risk.SMALL_SAMPLE_N:
            lines.append(f"  [small-sample] only {n} matching events — illustrative, not a stable pattern.")
        lines += ["[note] " + risk.OBSERVED_RISK_CAVEAT]
        lers = [{"key": f.ler, "source": None} for f in res["matched"][:25]]
        return Evidence("faceted_frequency", anchors, "\n".join(lines), node_keys=[], lers=lers)

    def _ler_sources(self, s, keys) -> dict:
        """Map LER key -> source (oracle|pipeline) for provenance tags on subgraph evidence."""
        keys = [k for k in keys if k]
        if not keys:
            return {}
        rows = s.run("MATCH (l:LER) WHERE l.key IN $keys "
                     "RETURN l.key AS key, l.source AS source", keys=keys)
        return {r["key"]: (r["source"] or "pipeline") for r in rows}

    def _t_subgraph(self, s, anchors) -> Evidence:
        # generic fallback: anchor on a system, ler, or cause and expand 1-2 hops. Tracks the
        # LER provenance of the neighborhood (every edge is stamped with its ler_number) so an
        # LER-anchored lookup returns a GROUNDED, citable answer instead of an empty-provenance
        # blob the answerer can only echo. For an LER seed the expansion is PINNED to that
        # report's own edges (rel.ler_number = the anchor, or a structural edge with no
        # ler_number) so it does not fan out through shared System/Cause/criterion hubs into
        # unrelated events — the same discipline the failure_chain template uses.
        seed_pred, params, pin = None, {}, ""
        if anchors.get("system_code"):
            seed_pred, params = self._sys_match(anchors)
            seed_pred = f"(a:System) WHERE {seed_pred}"
        elif anchors.get("ler_key"):
            seed_pred, params = "(a:LER {key:$k})", {"k": anchors["ler_key"]}
            pin = ("WHERE all(rel IN relationships(p) "
                   "WHERE rel.ler_number = $k OR rel.ler_number IS NULL) ")
        elif anchors.get("cause_code"):
            seed_pred, params = "(a:Cause {cause_code:$k})", {"k": anchors["cause_code"]}
        if not seed_pred:
            return self._empty("subgraph", anchors)
        rows = list(s.run(
            f"MATCH {seed_pred} MATCH p=(a)-[*1..2]-(m) {pin}"
            "RETURN DISTINCT a.display_name AS anchor, type(last(relationships(p))) AS rel, "
            "  m.display_name AS neighbor, m.match_key AS mk, labels(m) AS labels, "
            "  [x IN relationships(p) | x.ler_number] AS ler_nums LIMIT 60", **params))
        if not rows:
            return self._empty("subgraph", anchors)
        lines = [f"  {r['anchor']} … {r['rel']} … {r['neighbor']} "
                 f"[{[l for l in r['labels'] if l != 'Entity'][0]}]" for r in rows]
        node_keys = sorted({r["mk"] for r in rows if r["mk"]})
        ler_nums = {ln for r in rows for ln in (r["ler_nums"] or []) if ln}
        if anchors.get("ler_key"):
            ler_nums.add(anchors["ler_key"])
        prov = self._ler_sources(s, ler_nums)
        return Evidence("subgraph", anchors,
                        f"Neighborhood of {rows[0]['anchor']}:\n" + "\n".join(lines),
                        node_keys=node_keys,
                        lers=[{"key": k, "source": prov.get(k, "pipeline")}
                              for k in sorted(ler_nums)])

    def _t_out_of_corpus(self, s, anchors) -> Evidence:
        return self._empty("out_of_corpus", anchors)
