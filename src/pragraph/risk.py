"""
risk.py — Phase 7 probabilistic / risk layer over the LER graph.

*Dynamic Risk RAG* earns its name here: rank failure outcomes and paths by observed
frequency across the corpus. This is a DEMONSTRATION of the Dynamic-PRA idea, explicitly
NOT a certified reactor risk model. Read `docs/journal/phase_7.md` for the honesty framing; the short
version lives in `OBSERVED_RISK_CAVEAT` below and is threaded into every risk answer.

Three statistical-soundness rules are load-bearing (from the plan's review) and are built
into this module, not merely caveated:

  1. COUNT DISTINCT EVENTS, never edges or paths. Every conditional frequency is a fraction
     of distinct `ler_number`s. Each event contributes ONE outcome — its worst (max-severity)
     outcome class — so `P(outcome | entity)` is a proper distribution over events that sums
     to 1. See `event_outcomes()`.

  2. BUILD THE TRANSITION GRAPH FROM PER-LER CHAINS FIRST, then sum across LERs — never by
     traversing the live graph through a shared hub (the Phase-8 fan-out bug: a shared `Cause`
     node silently joins every unrelated event that touches it). See `transition_counts()`.

  3. THE DENOMINATOR IS "reportable events in this corpus," NOT "failures." Every LER already
     crossed a reporting threshold, and the 2020-2026 export is HPCI-dense by selection. So the
     ranked quantity is `observed_risk_contribution` (not "risk"), and the n_events axis is a
     corpus/reporting artifact, not a true failure rate. `expected_severity` is additionally
     inflated by outcome-selection bias (loss-of-safety-function is often the reporting
     *trigger*, 10 CFR 50.73(a)(2)(v)(D)). Both are named in answers and docs/journal/phase_7.md.

Layout:
  * Taxonomy + severity (editable data)      — this file, top.
  * Risk formulas + ±1 sensitivity check     — this file.
  * Distinct-event stats + materialize       — this file (added in the stats step).
  * Transition graph + most-probable path    — this file (added in the path step).
Outcome classification (the batched LLM pass that stamps `outcome_class` onto Consequences)
lives in `classify_outcomes.py`, which imports the taxonomy from here.
"""
from __future__ import annotations

import datetime
import itertools
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Outcome-class taxonomy + severity — EDITABLE DATA (the whole point).
#
# Severity is a hand-assigned, CONTESTABLE ordinal (1-5), not a learned constant. It is
# deliberately stored here as plain data so it can be argued with and perturbed; the ±1
# sensitivity check below is what makes the subjectivity honest, not tuning these numbers.
# The `meaning` strings are the SINGLE SOURCE OF TRUTH for the classification rubric
# (classify_outcomes.py builds the prompt from them) and the docs.
#
# Ordered worst-first. The severity-4 tie (reactor-trip-or-scram vs safety-system-inoperable)
# is intentional and noted in the roadmap: a clean scram is a *designed* safe response, but we
# do not relitigate ordinals — the sensitivity check handles it.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutcomeClass:
    key: str
    severity: int          # 1 (least) .. 5 (most)
    meaning: str


OUTCOME_CLASSES: tuple[OutcomeClass, ...] = (
    OutcomeClass("loss-of-safety-function", 5,
                 "actual loss of a safety function, or both/all trains of a safety system "
                 "inoperable at once (the function itself was lost, not just degraded)"),
    OutcomeClass("safety-system-inoperable", 4,
                 "a safety system rendered inoperable on a SINGLE train / component "
                 "(the redundant train remained available, so the function was not lost)"),
    OutcomeClass("reactor-trip-or-scram", 4,
                 "an automatic or manual reactor trip / scram (RPS actuation shutting the "
                 "reactor down)"),
    OutcomeClass("esf-actuation", 3,
                 "actuation of an engineered safety feature other than a reactor trip — "
                 "ECCS/HPCI/RCIC injection, AFW/EFW start, EDG start, PCIS, etc."),
    OutcomeClass("containment-isolation", 3,
                 "an automatic containment isolation / PCIV or MSIV closure / containment "
                 "isolation-signal actuation"),
    OutcomeClass("degraded-not-lost", 2,
                 "a degraded or non-conforming condition where the safety function was still "
                 "maintained (operable-but-degraded, seismic/environmental qualification gap)"),
    OutcomeClass("ts-violation-only", 2,
                 "a condition prohibited by Technical Specifications / an LCO exceeded or "
                 "missed surveillance, with NO actual loss of function"),
    OutcomeClass("other-or-no-safety-impact", 1,
                 "everything else — administrative/reporting issues, minor events, or no "
                 "discernible safety impact"),
)

# fast lookups
SEVERITY: dict[str, int] = {o.key: o.severity for o in OUTCOME_CLASSES}
OUTCOME_KEYS: tuple[str, ...] = tuple(o.key for o in OUTCOME_CLASSES)
OUTCOME_MEANING: dict[str, str] = {o.key: o.meaning for o in OUTCOME_CLASSES}

# The class assigned when the classifier can't decide / a consequence has no outcome_class
# yet. Excluded from all statistics (never silently counted as sev-1).
UNCLASSIFIED = "unclassified"

# Confidence below this gates a Consequence OUT of the statistics (flagged, not counted) —
# rule 2 of the review's "validate + gate before trusting any number."
CONFIDENCE_GATE = 0.60

# Small-sample flag: at or below this many distinct events, a per-entity number is labelled
# illustrative (shown with n, never suppressed). Component-level is structurally sparse.
SMALL_SAMPLE_N = 5

# The mandatory framing every risk answer must carry (answer.py enforces it verbatim-in-spirit).
OBSERVED_RISK_CAVEAT = (
    "These are OBSERVED FREQUENCIES over reportable LER events in a 2020-2026 corpus, not "
    "certified failure rates. The denominator is 'reportable events in this corpus' (every LER "
    "already crossed a reporting threshold, and the corpus was selected HPCI-dense), so a high "
    "count means 'most-represented here,' not 'most dangerous.' Severity is additionally "
    "inflated by outcome-selection bias: loss-of-safety-function is often the reporting trigger "
    "itself (10 CFR 50.73(a)(2)(v)(D)). No exposure time / reactor-years — not comparable to a "
    "PRA failure rate."
)


# --------------------------------------------------------------------------- #
# Risk formulas
# --------------------------------------------------------------------------- #
def normalize(counts: dict[str, int | float]) -> dict[str, float]:
    """Counts over outcome classes -> a probability distribution P(o) that sums to 1.
    Empty input -> empty dict. `UNCLASSIFIED` mass is dropped BEFORE normalizing (gated
    out), so probabilities are conditional on having a usable classification."""
    usable = {k: v for k, v in counts.items() if k in SEVERITY and v}
    total = sum(usable.values())
    if not total:
        return {}
    return {k: v / total for k, v in usable.items()}


def expected_severity(dist: dict[str, float], severity: dict[str, int] | None = None) -> float:
    """E[severity] = Σ_o P(o) · severity(o).

    A mean over ORDINALS treated as interval data — a standard demo abuse, stated as such
    in every answer. `dist` is P(o|entity) (need not be pre-normalized; we renormalize over
    the classes we have severities for). `severity` override enables the sensitivity check.
    """
    sev = severity or SEVERITY
    p = normalize({k: v for k, v in dist.items() if k in sev})
    return sum(pv * sev[k] for k, pv in p.items())


def observed_risk_contribution(n_events: int, dist: dict[str, float],
                               severity: dict[str, int] | None = None) -> float:
    """The ranked quantity: n_events · expected_severity.

    NAMED `observed_risk_contribution` (not "risk") on purpose — the n_events factor is a
    corpus/reporting artifact, so this ranks "observed contribution within this corpus," not
    intrinsic danger. Both factors are surfaced separately in answers; this scalar is ranking
    convenience over the honest object (the distribution).
    """
    return n_events * expected_severity(dist, severity)


def modal_outcome(dist: dict[str, float]) -> tuple[str, float] | None:
    """The single most frequent outcome class and its probability (the 'modal/max' the
    review wants surfaced alongside the scalar). None if the distribution is empty."""
    p = normalize(dist)
    if not p:
        return None
    k = max(p, key=lambda o: (p[o], SEVERITY[o]))   # ties -> higher severity
    return k, p[k]


# --------------------------------------------------------------------------- #
# ±1 severity sensitivity check
# --------------------------------------------------------------------------- #
# The review's insistence: report whether the top-N ranking SURVIVES a ±1 perturbation of the
# severity ordinals. If the top-3 flip under small changes, the ranking is illustrative only —
# this robustness note is worth more than getting the ordinals "right." In practice we expect
# the ranking to be dominated by n_events (a corpus artifact), so it should be robust to
# severity changes — itself an honest, informative finding to surface.
# --------------------------------------------------------------------------- #
@dataclass
class SensitivityResult:
    top_n: int
    baseline_top: list[str]                 # entity ids, baseline severity, best-first
    n_perturbations: int
    frac_top_set_stable: float              # fraction of ±1 severity worlds with same top-N set
    frac_rank1_stable: float                # fraction keeping the SAME #1 entity
    robust: bool                            # top-N set stable in >=95% of perturbations

    def summary(self) -> str:
        verdict = ("robust (top-%d stable under ±1 severity perturbation)" % self.top_n
                   if self.robust else
                   "ILLUSTRATIVE ONLY (top-%d flips under ±1 severity perturbation)" % self.top_n)
        return (f"severity sensitivity: {verdict}; "
                f"top-{self.top_n} set stable in {self.frac_top_set_stable:.0%} of "
                f"{self.n_perturbations} ±1 worlds, #1 stable in {self.frac_rank1_stable:.0%}")


def _perturbed_severities(delta: int = 1):
    """Yield every severity assignment where each class is shifted by -delta..+delta,
    clamped to [1,5]. 8 classes × 3 shifts = 3^8 = 6561 worlds (cheap, exhaustive)."""
    keys = OUTCOME_KEYS
    shifts = range(-delta, delta + 1)
    for combo in itertools.product(shifts, repeat=len(keys)):
        yield {k: min(5, max(1, SEVERITY[k] + d)) for k, d in zip(keys, combo)}


def severity_sensitivity(entity_dists: dict[str, dict[str, float]],
                         n_events: dict[str, int],
                         top_n: int = 3, delta: int = 1) -> SensitivityResult:
    """How stable is the observed_risk_contribution top-N ranking under ±`delta` shifts of the
    severity ordinals? `entity_dists[id] = P(o|id)`, `n_events[id] = distinct-event count`."""
    ids = [e for e in entity_dists if n_events.get(e)]

    def ranking(sev: dict[str, int]) -> list[str]:
        return sorted(ids, key=lambda e: observed_risk_contribution(n_events[e], entity_dists[e], sev),
                      reverse=True)

    baseline_top = ranking(SEVERITY)[:top_n]
    baseline_set = set(baseline_top)
    baseline_1 = baseline_top[0] if baseline_top else None

    worlds = list(_perturbed_severities(delta))
    same_set = same_1 = 0
    for sev in worlds:
        top = ranking(sev)[:top_n]
        if set(top) == baseline_set:
            same_set += 1
        if top and top[0] == baseline_1:
            same_1 += 1
    n = len(worlds)
    frac_set = same_set / n if n else 1.0
    frac_1 = same_1 / n if n else 1.0
    return SensitivityResult(top_n=top_n, baseline_top=baseline_top, n_perturbations=n,
                             frac_top_set_stable=frac_set, frac_rank1_stable=frac_1,
                             robust=frac_set >= 0.95)


# --------------------------------------------------------------------------- #
# Distinct-event statistics (RULE 1: count events, worst-outcome-per-event)
# --------------------------------------------------------------------------- #
# Each reportable event (LER) contributes exactly ONE outcome to a distribution — its worst
# (max-severity) classified consequence — so P(outcome | entity) is a proper distribution over
# EVENTS that sums to 1, never a double-count over the several consequences or edges an event may
# have. Low-confidence classifications are gated out here (not silently counted).
# --------------------------------------------------------------------------- #
def _worst(classes: list[str]) -> str | None:
    """The max-severity outcome class in a list (ties -> the earlier/worse in taxonomy order)."""
    usable = [c for c in classes if c in SEVERITY]
    if not usable:
        return None
    return max(usable, key=lambda c: (SEVERITY[c], -OUTCOME_KEYS.index(c)))


def load_event_outcomes(session, confidence_gate: float = CONFIDENCE_GATE) -> dict[str, dict]:
    """ler_number -> {worst, classes, n_consequences, n_gated}. Built PER EVENT: gather each
    event's consequence classifications, drop those below the confidence gate, take the worst
    of what remains. Events with no usable classification have worst=None (excluded from stats,
    not counted as severity-1)."""
    by_ler: dict[str, dict] = defaultdict(lambda: {"classes": [], "n_consequences": 0, "n_gated": 0})
    rows = session.run(
        "MATCH (cons:Consequence) WHERE cons.outcome_class IS NOT NULL "
        "OPTIONAL MATCH (cons)-[e]-() "
        "WITH cons, [x IN collect(DISTINCT e.ler_number) WHERE x IS NOT NULL][0] AS ler "
        "RETURN ler, cons.outcome_class AS oc, cons.classifier_confidence AS conf")
    for r in rows:
        ler, oc, conf = r["ler"], r["oc"], (r["conf"] if r["conf"] is not None else 0.0)
        if ler is None:
            continue
        b = by_ler[ler]
        b["n_consequences"] += 1
        if oc in SEVERITY and conf >= confidence_gate:
            b["classes"].append(oc)
        else:
            b["n_gated"] += 1
    for ler, b in by_ler.items():
        b["worst"] = _worst(b["classes"])
    return dict(by_ler)


def _valid_event_lers(session) -> set[str]:
    return {r["k"] for r in session.run("MATCH (l:LER) WHERE NOT l.stub RETURN l.key AS k")}


def entity_event_map(session) -> dict[str, dict[str, dict]]:
    """{entity_type: {match_key: {label, code, lers:set}}} over distinct non-stub events.
    System/Component by INVOLVES + any incident per-LER edge (a component is 'in' an event if
    any of its edges is stamped with that event); Cause by the per-LER HAS_CAUSE bridge,
    EXCLUDING provisional (TBD) causes — 'provisional' is 'unknown', not a category."""
    valid = _valid_event_lers(session)
    out: dict[str, dict[str, dict]] = {"System": {}, "Component": {}, "Cause": {}}

    for r in session.run(
        "MATCH (l:LER)-[:INVOLVES]->(s:System) WHERE NOT l.stub "
        "RETURN s.match_key AS mk, coalesce(s.eiis_code,'') AS code, s.display_name AS name, "
        "  collect(DISTINCT l.key) AS lers"):
        out["System"][r["mk"]] = {"label": r["name"], "code": r["code"],
                                  "lers": set(r["lers"]) & valid}

    for r in session.run(
        "MATCH (c:Component)-[e]-() WHERE e.ler_number IS NOT NULL "
        "RETURN c.match_key AS mk, coalesce(c.eiis_code,'') AS code, c.display_name AS name, "
        "  [x IN collect(DISTINCT e.ler_number) WHERE x IS NOT NULL] AS lers"):
        out["Component"][r["mk"]] = {"label": r["name"], "code": r["code"],
                                     "lers": set(r["lers"]) & valid}

    for r in session.run(
        "MATCH (l:LER)-[:HAS_CAUSE {ler_number:l.key}]->(c:Cause) "
        "WHERE NOT l.stub AND c.category <> 'provisional' "
        "RETURN c.match_key AS mk, coalesce(c.cause_code,'') AS code, c.category AS name, "
        "  collect(DISTINCT l.key) AS lers"):
        out["Cause"][r["mk"]] = {"label": r["name"], "code": r["code"],
                                 "lers": set(r["lers"]) & valid}
    return out


@dataclass
class RiskStats:
    entity_type: str                 # 'System' | 'Component' | 'Cause'
    key: str                         # match_key
    label: str
    code: str
    n_events: int                    # distinct reportable events involving the entity
    n_classified: int               # of those, with a usable worst-outcome
    counts: dict[str, int] = field(default_factory=dict)   # worst-outcome class -> event count

    @property
    def dist(self) -> dict[str, float]:
        return normalize(self.counts)

    @property
    def expected_severity(self) -> float:
        return expected_severity(self.dist)

    @property
    def observed_risk_contribution(self) -> float:
        # literal observed severity mass = n_classified * mean worst-severity (count events).
        return self.n_classified * self.expected_severity

    @property
    def small_sample(self) -> bool:
        return self.n_classified <= SMALL_SAMPLE_N

    @property
    def modal(self) -> tuple[str, float] | None:
        return modal_outcome(self.counts)

    @property
    def coverage(self) -> float:
        return self.n_classified / self.n_events if self.n_events else 0.0

    def line(self) -> str:
        m = self.modal
        modal = f"{m[0]} {m[1]:.0%}" if m else "—"
        flag = "  [small-sample]" if self.small_sample else ""
        return (f"{self.label} [{self.code or '—'}]: ORC={self.observed_risk_contribution:.1f} "
                f"(n_events={self.n_events}, classified={self.n_classified}, "
                f"E[sev]={self.expected_severity:.2f}, modal={modal}){flag}")


def compute_stats(session, entity_types=("System", "Component", "Cause")) -> dict[str, dict[str, RiskStats]]:
    """Per-entity RiskStats over distinct events with worst-outcome-per-event distributions."""
    events = load_event_outcomes(session)
    emap = entity_event_map(session)
    stats: dict[str, dict[str, RiskStats]] = {t: {} for t in entity_types}
    for etype in entity_types:
        for mk, info in emap.get(etype, {}).items():
            lers = info["lers"]
            worst = [events[l]["worst"] for l in lers if l in events and events[l]["worst"]]
            stats[etype][mk] = RiskStats(
                entity_type=etype, key=mk, label=info["label"], code=info["code"],
                n_events=len(lers), n_classified=len(worst), counts=dict(Counter(worst)))
    return stats


def corpus_outcome_distribution(session) -> tuple[dict[str, int], int, int]:
    """(worst-outcome counts over ALL events, n_events_total, n_events_classified) — the corpus
    baseline a per-entity distribution is compared against (used by the honesty answer)."""
    events = load_event_outcomes(session)
    valid = _valid_event_lers(session)
    worst = [events[l]["worst"] for l in valid if l in events and events[l]["worst"]]
    return dict(Counter(worst)), len(valid), len(worst)


# --------------------------------------------------------------------------- #
# Materialization (RULE from review: re-runnable, every stat stamped with n_events)
# --------------------------------------------------------------------------- #
MATERIALIZE_PROPS = ("outcome_counts_json", "outcome_dist_json", "expected_severity",
                     "observed_risk_contribution", "n_events", "n_events_classified",
                     "risk_small_sample", "risk_prompt_version", "risk_materialized_at")


def materialize(session, prompt_version: str = "v1") -> dict[str, int]:
    """Recompute from scratch and stamp risk stats onto every System/Component/Cause hub node.
    Every stat carries the `n_events` it was computed from, so a stale stat (after a reload /
    corpus extend) is DETECTABLE rather than silently wrong. Idempotent; safe to re-run."""
    stats = compute_stats(session)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    written = {}
    for etype, by_key in stats.items():
        rows = [{
            "mk": s.key,
            "outcome_counts_json": json.dumps(s.counts, sort_keys=True),
            "outcome_dist_json": json.dumps({k: round(v, 4) for k, v in s.dist.items()}, sort_keys=True),
            "expected_severity": round(s.expected_severity, 4),
            "observed_risk_contribution": round(s.observed_risk_contribution, 4),
            "n_events": s.n_events, "n_events_classified": s.n_classified,
            "risk_small_sample": s.small_sample, "risk_prompt_version": prompt_version,
            "risk_materialized_at": now,
        } for s in by_key.values()]
        res = session.run(
            f"UNWIND $rows AS row MATCH (n:`{etype}` {{match_key: row.mk}}) "
            "SET n.outcome_counts_json = row.outcome_counts_json, "
            "    n.outcome_dist_json = row.outcome_dist_json, "
            "    n.expected_severity = row.expected_severity, "
            "    n.observed_risk_contribution = row.observed_risk_contribution, "
            "    n.n_events = row.n_events, n.n_events_classified = row.n_events_classified, "
            "    n.risk_small_sample = row.risk_small_sample, "
            "    n.risk_prompt_version = row.risk_prompt_version, "
            "    n.risk_materialized_at = row.risk_materialized_at RETURN count(n) AS n",
            rows=rows).single()
        written[etype] = res["n"]
    return written


# --------------------------------------------------------------------------- #
# Transition graph + most-probable path (RULE 2: per-LER chains first)
# --------------------------------------------------------------------------- #
# The aggregated System -> Cause -> Outcome transition graph is built by grouping EACH EVENT's
# (systems, cause, worst-outcome) locally and summing across events — never by traversing the
# live graph through the shared Cause hub (that hub fans out into every unrelated event, the
# Phase-8 bug). The cause layer excludes provisional (TBD) causes. Provisional-cause coverage is
# high in this corpus (~63% of events), so P(cause|system) rests only on an event's CODED-cause
# subset — every transition therefore carries its supporting event count so sparsity is visible.
# --------------------------------------------------------------------------- #
def per_event_chain(session) -> dict[str, dict]:
    """ler -> {systems:set, cause:str|None, worst:str|None}. One local row per event; the
    transition counts are summed FROM these, honoring 'per-LER chains first'."""
    events = load_event_outcomes(session)
    out: dict[str, dict] = {}
    for r in session.run(
        "MATCH (l:LER) WHERE NOT l.stub "
        "OPTIONAL MATCH (l)-[:INVOLVES]->(s:System) "
        "OPTIONAL MATCH (l)-[:HAS_CAUSE {ler_number:l.key}]->(c:Cause) "
        "RETURN l.key AS ler, "
        "  [x IN collect(DISTINCT s.match_key) WHERE x IS NOT NULL] AS systems, "
        "  head([x IN collect(DISTINCT c.category) WHERE x IS NOT NULL AND x <> 'provisional']) AS cause"):
        out[r["ler"]] = {"systems": set(r["systems"]),
                         "cause": r["cause"],
                         "worst": events.get(r["ler"], {}).get("worst")}
    return out


@dataclass
class Transitions:
    # ENTITY = System OR Component — the path's first hop can be seeded on either. Counts are over
    # distinct events; the cause layer excludes provisional (uncoded) causes. Most components are
    # single-event, so component-seeded paths are usually small-sample (flagged, not hidden).
    entity_cause: Counter = field(default_factory=Counter)         # (entity_mk, cause) -> events
    cause_outcome: Counter = field(default_factory=Counter)        # (cause, outcome) -> events
    entity_cause_total: Counter = field(default_factory=Counter)   # entity_mk -> events w/ a coded cause
    cause_outcome_total: Counter = field(default_factory=Counter)  # cause -> events w/ an outcome
    entity_label: dict = field(default_factory=dict)               # match_key -> display name

    def p_cause_given_entity(self, entity: str, cause: str) -> float:
        t = self.entity_cause_total[entity]
        return self.entity_cause[(entity, cause)] / t if t else 0.0

    def p_outcome_given_cause(self, cause: str, outcome: str) -> float:
        t = self.cause_outcome_total[cause]
        return self.cause_outcome[(cause, outcome)] / t if t else 0.0

    def _edges_from(self, node):
        kind, key = node
        if kind == "E":
            for (ent, cause), n in self.entity_cause.items():
                if ent == key and n:
                    yield ("C", cause), -math.log(self.p_cause_given_entity(ent, cause)), n
        elif kind == "C":
            for (cause, outcome), n in self.cause_outcome.items():
                if cause == key and n:
                    yield ("O", outcome), -math.log(self.p_outcome_given_cause(cause, outcome)), n


def build_transitions(session) -> Transitions:
    """Aggregated Entity -> Cause -> Outcome transition graph, summed from per-LER chains (RULE 2).
    ENTITY spans Systems AND Components, so a path can be seeded on either."""
    chains = per_event_chain(session)
    emap = entity_event_map(session)
    event_entities: dict[str, set] = defaultdict(set)      # ler -> {system + component match_keys}
    labels: dict[str, str] = {}
    for etype in ("System", "Component"):
        for mk, info in emap[etype].items():
            labels[mk] = info["label"]
            for ler in info["lers"]:
                event_entities[ler].add(mk)
    t = Transitions(entity_label=labels)
    for ler, c in chains.items():
        cause, worst = c["cause"], c["worst"]
        if not cause:
            continue
        for mk in event_entities.get(ler, ()):            # per-event: each system + component
            t.entity_cause[(mk, cause)] += 1
            t.entity_cause_total[mk] += 1
        if worst:                                          # once per event: the cause->outcome layer
            t.cause_outcome[(cause, worst)] += 1
            t.cause_outcome_total[cause] += 1
    return t


@dataclass
class PathStep:
    kind: str            # 'entity' (seed system/component) | 'cause' | 'outcome'
    key: str             # match_key for the entity seed; category / class for cause / outcome
    prob: float          # conditional prob of THIS step given the previous (1.0 for the seed)
    n_events: int        # events supporting this transition


@dataclass
class ProbablePath:
    steps: list[PathStep]
    joint_prob: float                          # product of the conditional step probs
    min_support: int                           # smallest supporting n_events along the path

    @property
    def small_sample(self) -> bool:
        return self.min_support <= SMALL_SAMPLE_N

    def render(self, label_of=None) -> str:
        def nm(s):
            return label_of(s.key) if (label_of and s.kind == "entity") else s.key
        body = "  ->  ".join(
            f"{nm(s)}" + ("" if s.kind == "entity" else f" (p={s.prob:.2f}, n={s.n_events})")
            for s in self.steps)
        flag = "  [small-sample — illustrative]" if self.small_sample else ""
        return f"{body}   [joint p={self.joint_prob:.3f}]{flag}"


def most_probable_path(trans: Transitions, seed_mk: str) -> ProbablePath | None:
    """Highest-probability entity -> cause -> outcome path from a seed System OR Component, via a
    -log(prob) shortest-path (Dijkstra). Pure Python, no GDS. Returns None if the seed has no
    coded-cause events (the corpus's provisional-cause sparsity), which the answer surfaces
    honestly."""
    import heapq
    start = ("E", seed_mk)
    dist = {start: 0.0}
    prev: dict = {}                                   # node -> (prev_node, step_prob, n_events)
    pq = [(0.0, start)]
    while pq:
        cost, node = heapq.heappop(pq)
        if cost > dist.get(node, math.inf):
            continue
        if node[0] == "O":                            # reached an outcome -> reconstruct
            steps, support, cur = [], math.inf, node
            while cur in prev:
                pnode, sp, n = prev[cur]
                kind = {"C": "cause", "O": "outcome"}[cur[0]]
                steps.append(PathStep(kind, cur[1], sp, n))
                support = min(support, n)
                cur = pnode
            steps.append(PathStep("entity", seed_mk, 1.0, trans.entity_cause_total[seed_mk]))
            steps.reverse()
            return ProbablePath(steps=steps, joint_prob=math.exp(-cost), min_support=support)
        for nbr, w, n in trans._edges_from(node):
            nc = cost + w
            if nc < dist.get(nbr, math.inf):
                dist[nbr] = nc
                prev[nbr] = (node, math.exp(-w), n)
                heapq.heappush(pq, (nc, nbr))
    return None


def cause_outcome_distribution(trans: Transitions, cause: str) -> tuple[dict[str, float], int]:
    """P(outcome | cause) with its supporting event count — the corpus-wide, well-populated
    layer (used by the CAUSE->OUTCOME golden)."""
    counts = {o: n for (c, o), n in trans.cause_outcome.items() if c == cause}
    return normalize(counts), trans.cause_outcome_total[cause]


# --------------------------------------------------------------------------- #
# General faceted-frequency engine (one primitive, many query shapes)
# --------------------------------------------------------------------------- #
# Rather than a hard-coded template per question shape, most risk/aggregate questions are the same
# abstract query: "among the EVENTS matching a set of FILTERS, show the frequency distribution of
# some FACET." One per-event facet table + these functions subsume: forward (P(outcome|system)),
# REVERSE (which systems are in loss-of-safety-function events), CO-OCCURRENCE / COMBINATION and
# true PAIRS, keyword-filtered consequence lookups, COMPOUND (AND) filters, NUMERIC thresholds
# (power/duration), TEMPORAL trends (by year), CORRECTIVE-ACTION listings, and COMPARISON of two
# entities side by side. Everything is counted over DISTINCT events (RULE 1).
# --------------------------------------------------------------------------- #
# Categorical facets can be a target (counted) or an equality/substring filter; numeric facets are
# filter-only, compared with an operator.
FACETS = ("systems", "components", "causes", "outcomes", "plants", "corrective_actions", "years")
NUMERIC_FACETS = ("power_level", "duration_hours")

_FILTER_ALIASES = {
    "system": "systems", "systems": "systems", "component": "components", "components": "components",
    "cause": "cause", "causes": "cause", "outcome": "outcome", "outcomes": "outcome",
    "plant": "plants", "plants": "plants", "consequence": "consequence", "consequences": "consequence",
    "corrective_action": "corrective_actions", "corrective_actions": "corrective_actions",
    "correctiveaction": "corrective_actions", "resolution": "corrective_actions",
    "year": "years", "years": "years", "power_level": "power_level", "power": "power_level",
    "powerlevel": "power_level", "duration": "duration_hours", "duration_hours": "duration_hours",
    "duration_hour": "duration_hours",
}
_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        "<": lambda a, b: a < b, "==": lambda a, b: a == b, "=": lambda a, b: a == b}


@dataclass
class EventFacets:
    ler: str
    plant: str | None
    systems: set[str]                # "display_name [code]"
    components: set[str]             # component display names
    cause: str | None                # coded (non-provisional) category
    outcome: str | None              # worst outcome class
    consequences: list[str]          # raw consequence display names (for keyword filters)
    corrective_actions: set[str] = field(default_factory=set)
    year: str | None = None
    power_level: int | None = None
    duration_hours: float | None = None      # best-effort max consequence duration


def _facet_values(f: EventFacets, facet: str) -> set[str]:
    return {
        "systems": f.systems, "components": f.components,
        "causes": {f.cause} if f.cause else set(),
        "outcomes": {f.outcome} if f.outcome else set(),
        "plants": {f.plant} if f.plant else set(),
        "corrective_actions": f.corrective_actions,
        "years": {f.year} if f.year else set(),
    }.get(facet, set())


def _parse_duration(text: str | None) -> float | None:
    """Best-effort parse of the free-text Consequence.duration into hours. Handles 'H:MM',
    'N hours/days/minutes' (and combinations), tolerates leading >, ~ . None if unparseable
    (the field is inconsistent, so numeric duration filters are approximate + low-coverage)."""
    t = (text or "").strip().lstrip("><~≈ ").lower()
    if not t:
        return None
    m = re.match(r"^(\d{1,4}):(\d{2})$", t)                     # H:MM
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    hours, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(days?|hours?|hrs?|minutes?|mins?)", t):
        found = True
        n = float(num)
        hours += n * 24 if unit.startswith("day") else (n / 60 if unit.startswith("min") else n)
    return hours if found else None


def event_facets(session) -> dict[str, EventFacets]:
    """One row per non-stub event with all its facets — the table the general engine queries."""
    ev = load_event_outcomes(session)
    facets: dict[str, EventFacets] = {}
    for r in session.run(
        "MATCH (l:LER) WHERE NOT l.stub "
        "OPTIONAL MATCH (l)-[:INVOLVES]->(sys:System) "
        "OPTIONAL MATCH (l)-[:HAS_CAUSE {ler_number:l.key}]->(c:Cause) "
        "RETURN l.key AS ler, l.plant_name AS plant, left(l.event_date, 4) AS year, "
        "  l.power_level AS power, "
        "  [x IN collect(DISTINCT (sys.display_name + ' [' + coalesce(sys.eiis_code,'') + ']')) "
        "     WHERE x IS NOT NULL] AS systems, "
        "  head([x IN collect(DISTINCT c.category) WHERE x IS NOT NULL AND x <> 'provisional']) AS cause"):
        facets[r["ler"]] = EventFacets(
            ler=r["ler"], plant=r["plant"], systems=set(r["systems"]), components=set(),
            cause=r["cause"], outcome=ev.get(r["ler"], {}).get("worst"), consequences=[],
            year=r["year"], power_level=r["power"])
    for r in session.run(
        "MATCH (c:Component)-[e]-() WHERE e.ler_number IS NOT NULL "
        "WITH e.ler_number AS ler, [x IN collect(DISTINCT c.display_name) WHERE x IS NOT NULL] AS comps "
        "RETURN ler, comps"):
        if r["ler"] in facets:
            facets[r["ler"]].components = set(r["comps"])
    for r in session.run(
        "MATCH (ca:CorrectiveAction)-[e]-() WHERE e.ler_number IS NOT NULL "
        "WITH e.ler_number AS ler, [x IN collect(DISTINCT ca.display_name) WHERE x IS NOT NULL] AS cas "
        "RETURN ler, cas"):
        if r["ler"] in facets:
            facets[r["ler"]].corrective_actions = set(r["cas"])
    for r in session.run(
        "MATCH (cons:Consequence) OPTIONAL MATCH (cons)-[e]-() "
        "WITH cons, [x IN collect(DISTINCT e.ler_number) WHERE x IS NOT NULL][0] AS ler "
        "WHERE ler IS NOT NULL "
        "WITH ler, collect(DISTINCT cons.display_name) AS cs, collect(DISTINCT cons.duration) AS durs "
        "RETURN ler, cs, durs"):
        if r["ler"] in facets:
            facets[r["ler"]].consequences = [x for x in r["cs"] if x]
            parsed = [d for d in (_parse_duration(x) for x in r["durs"]) if d is not None]
            facets[r["ler"]].duration_hours = max(parsed) if parsed else None
    return facets


def resolve_outcome_class(text: str) -> str | None:
    """Map a free-text outcome phrase to one of the 8 classes (or None). Used to interpret a
    router-supplied filter value; when it returns None the caller falls back to a keyword filter."""
    t = (text or "").lower().strip()
    if not t:
        return None
    if t in SEVERITY:
        return t
    kw = {
        "loss-of-safety-function": ["loss of safety", "loss of function", "both trains", "all trains"],
        "safety-system-inoperable": ["inoperable", "out of service", "single train"],
        "reactor-trip-or-scram": ["reactor trip", "scram", "rps", "trip"],
        "esf-actuation": ["esf", "eccs", "injection", "afw", "efw", "edg", "actuation", "start"],
        "containment-isolation": ["containment isolation", "pciv", "msiv", "isolation"],
        "degraded-not-lost": ["degraded", "non-conform", "nonconform"],
        "ts-violation-only": ["tech spec", "technical specification", "ts ", "lco", "surveillance"],
        "other-or-no-safety-impact": ["no safety impact", "administrative", "other"],
    }
    for cls, words in kw.items():
        if any(w in t for w in words):
            return cls
    return None


def _normalize_filters(filters) -> list[dict]:
    """Accept a list of {facet, op?, value} filter specs (or a single such dict); drop empties."""
    if filters is None:
        return []
    if isinstance(filters, dict):
        filters = [filters]
    out = []
    for flt in filters:
        if isinstance(flt, dict) and flt.get("facet") and flt.get("value") not in (None, ""):
            out.append(flt)
    return out


def _match_one(f: EventFacets, facet: str, op: str | None, value) -> bool:
    key = _FILTER_ALIASES.get((facet or "").lower(), (facet or "").lower())
    if key in NUMERIC_FACETS:
        actual = getattr(f, key)
        if actual is None:
            return False
        try:
            v = float(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return False
        return _OPS.get(op or ">=", _OPS[">="])(actual, v)
    lv = str(value).lower().strip()
    if key == "consequence":
        return any(lv in c.lower() for c in f.consequences)
    if key == "outcome":
        cls = resolve_outcome_class(str(value))
        return f.outcome == cls if cls else any(lv in c.lower() for c in f.consequences)
    if key == "cause":
        return f.cause is not None and lv in f.cause.lower()
    if key == "years":
        return f.year == str(value).strip()
    if key == "systems":                                # accept common aliases / codes (HPCI->BJ)
        alias = {"hpci": "bj", "rcic": "bn", "hpcs": "bg", "ads": "automatic-depress"}.get(lv, lv)
        return any(lv in x.lower() or f"[{alias}]" in x.lower() for x in f.systems)
    return any(lv in x.lower() for x in _facet_values(f, key))     # generic categorical substring


def _filter_desc(filters: list[dict]) -> str:
    if not filters:
        return "all events"
    parts = [f"{flt['facet']} {(flt.get('op') + ' ') if flt.get('op') else ''}{flt['value']}"
             for flt in filters]
    return "events where " + " AND ".join(parts)


def faceted_frequency(facets_all: dict[str, EventFacets], target: str,
                      filters=None, pairs: bool = False):
    """Core primitive. Distribution of `target` over DISTINCT events matching ALL `filters`
    (compound AND). `pairs=True` counts co-occurring unordered PAIRS of the target within events
    (true combination). Returns counts, optional pair-counts, the matched events, and a description.

    Back-compat: `filters` may be a single {facet, op?, value} dict or a list of them."""
    filters = _normalize_filters(filters)
    if target not in FACETS:
        target = "outcomes"
    counts: Counter = Counter()
    pair_counts: Counter = Counter()
    matched: list[EventFacets] = []
    for f in facets_all.values():
        if not all(_match_one(f, flt.get("facet"), flt.get("op"), flt.get("value")) for flt in filters):
            continue
        matched.append(f)
        vals = _facet_values(f, target)
        for v in vals:
            counts[v] += 1
        if pairs:
            for a, b in itertools.combinations(sorted(vals), 2):
                pair_counts[(a, b)] += 1
    return {"target": target, "filter_desc": _filter_desc(filters), "counts": dict(counts),
            "pairs": dict(pair_counts) if pairs else None,
            "n_matched": len(matched), "matched": matched}


def compare_facets(facets_all: dict[str, EventFacets], target: str, compare_facet: str,
                   values: list[str], filters=None):
    """Run the engine once per value of `compare_facet` (e.g. system HPCI vs RCIC), sharing any
    base `filters` — the COMPARATIVE shape, two+ distributions side by side."""
    base = _normalize_filters(filters)
    return {v: faceted_frequency(facets_all, target, base + [{"facet": compare_facet, "value": v}])
            for v in values}


# --------------------------------------------------------------------------- #
# ranking (risk_ranking intent) + the sensitivity verdict over materialized stats
# --------------------------------------------------------------------------- #
def load_materialized_stats(session, etype: str) -> dict[str, RiskStats]:
    """Reconstruct RiskStats from the props materialize() stamped on the hub nodes (what the
    retriever reads at query time). Empty if the risk layer hasn't been materialized yet."""
    out: dict[str, RiskStats] = {}
    for r in session.run(
        f"MATCH (n:`{etype}`) WHERE n.observed_risk_contribution IS NOT NULL "
        "RETURN n.match_key AS mk, n.display_name AS label, "
        "  coalesce(n.eiis_code, n.cause_code, '') AS code, n.outcome_counts_json AS counts, "
        "  n.n_events AS ne, n.n_events_classified AS nc"):
        out[r["mk"]] = RiskStats(etype, r["mk"], r["label"], r["code"] or "",
                                 r["ne"], r["nc"], json.loads(r["counts"]))
    return out


def rank_by_risk(stats_by_key: dict[str, RiskStats], top_n: int = 10,
                 min_classified: int = 1) -> list[RiskStats]:
    elig = [s for s in stats_by_key.values() if s.n_classified >= min_classified]
    return sorted(elig, key=lambda s: s.observed_risk_contribution, reverse=True)[:top_n]


def ranking_sensitivity(stats_by_key: dict[str, RiskStats], top_n: int = 3) -> SensitivityResult:
    # Prune the ±1 sweep to the entities that could plausibly reach the top-N: since severity is
    # bounded to [1,5], ORC ranges over [n·1, n·5], so only the highest-n entities can enter the
    # top under any perturbation. Top-40-by-n is a safe, fast margin (top-3 by ORC live in top-10).
    cand = sorted((s for s in stats_by_key.values() if s.n_classified),
                  key=lambda s: s.n_classified, reverse=True)[:40]
    dists = {s.key: s.dist for s in cand}
    nev = {s.key: s.n_classified for s in cand}
    return severity_sensitivity(dists, nev, top_n=top_n)


# --------------------------------------------------------------------------- #
# self-check: print the taxonomy + a tiny formula demo (no DB needed)
# --------------------------------------------------------------------------- #
def _print_taxonomy() -> None:
    print("outcome-class taxonomy (severity 1-5, editable in risk.py):\n")
    for o in OUTCOME_CLASSES:
        print(f"  [{o.severity}] {o.key}")
        print(f"        {o.meaning}")
    print(f"\n  gate: confidence < {CONFIDENCE_GATE} excluded from stats; "
          f"small-sample flag at n <= {SMALL_SAMPLE_N}")


def _print_stats(session) -> None:
    """Compute (without writing) and print the risk summary — the numbers docs/journal/phase_7.md quotes."""
    counts, n_tot, n_cls = corpus_outcome_distribution(session)
    print(f"\ncorpus: {n_tot} reportable events, {n_cls} with a usable worst-outcome "
          f"({n_cls/n_tot:.0%} coverage)")
    print("corpus outcome distribution (worst-outcome per event):")
    for o, p in sorted(normalize(counts).items(), key=lambda kv: -kv[1]):
        print(f"    {o:26} {p:5.0%}  ({counts[o]} events)  [sev {SEVERITY[o]}]")

    stats = compute_stats(session)
    print("\ntop 10 systems by observed_risk_contribution (n_events-dominated — see docs/journal/phase_7.md):")
    for i, s in enumerate(rank_by_risk(stats["System"], top_n=10), 1):
        print(f"  {i:2}. {s.line()}")
    print("\ncause categories by observed_risk_contribution:")
    for i, s in enumerate(rank_by_risk(stats["Cause"], top_n=8), 1):
        print(f"  {i:2}. {s.line()}")
    print("\n" + ranking_sensitivity(stats["System"], top_n=3).summary())
    bj = stats["System"].get("System:BJ")
    if bj:
        print(f"\nHPCI (System:BJ): {bj.line()}")
        for o, p in sorted(bj.dist.items(), key=lambda kv: -kv[1]):
            print(f"    {o:26} {p:5.0%}  ({bj.counts.get(o,0)} events)")


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Phase-7 risk layer: taxonomy, stats, materialize.")
    p.add_argument("--materialize", action="store_true",
                   help="recompute + stamp risk stats onto System/Component/Cause hubs (re-runnable)")
    p.add_argument("--stats", action="store_true", help="print the risk summary without writing")
    p.add_argument("--taxonomy", action="store_true", help="print the outcome-class taxonomy")
    args = p.parse_args(argv)

    if not (args.materialize or args.stats or args.taxonomy):
        _print_taxonomy()
        return 0
    if args.taxonomy:
        _print_taxonomy()
    if args.materialize or args.stats:
        from .load_graph import _connect
        d = _connect(); d.verify_connectivity()
        try:
            with d.session() as s:
                if args.materialize:
                    written = materialize(s)
                    print(f"[materialize] stamped risk stats onto hubs: {written}")
                _print_stats(s)
        finally:
            d.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
