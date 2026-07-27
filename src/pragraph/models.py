"""
models.py — Pydantic v2 schema for the LER knowledge-graph, schema v4.1.

This is the canonical shape for both:
  * pipeline extraction output (one LERRecord per LER), and
  * the scoring oracle (ground_truth.json -> GroundTruth).

Design notes
------------
* Nodes are a discriminated union on `type`. Each node exposes a computed `match_key`
  used by the scorer to align extracted vs. ground-truth graphs.
* Nodes tolerate the oracle's storage style: known fields nested under `properties`
  are hoisted to typed fields, an inbound computed `match_key` is dropped, and any
  leftover keys stay in `properties`. So `GroundTruth.model_validate(json.load(...))`
  works directly on ground_truth.json.
* Deterministic-vs-LLM split: the parser fills identity / reporting_basis / block_13 /
  cause_code+category; the LLM fills the narrative nodes+edges and cause proximate_text/
  theme. resolve.py merges them and treats the deterministic fields as authoritative.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


# --------------------------------------------------------------------------- #
# Controlled vocabularies
# --------------------------------------------------------------------------- #
CauseCode = Literal["A", "B", "C", "D", "E", "X", "TBD"]
DiscoveryContext = Literal[
    "surveillance test", "operability test", "normal operation", "inspection"
]
LERStatus = Literal["final", "supplement-expected"]
CAStatus = Literal["completed", "planned"]
RegRefType = Literal[
    "reporting-criterion", "analysis-basis", "standard/guidance", "license-basis"
]
Relation = Literal[
    "OCCURRED_AT", "INVOLVES", "LEADS_TO", "CAUSED_BY", "MITIGATED_BY",
    "BACKED_UP_BY", "REPORTED_UNDER", "MANUFACTURED_BY", "PART_OF",
    "SIMILAR_TO", "REVISES",
]


# --------------------------------------------------------------------------- #
# Node base + hoisting logic
# --------------------------------------------------------------------------- #
class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    properties: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _absorb_properties(cls, data):
        """Drop inbound computed match_key; hoist known-field keys out of `properties`."""
        if not isinstance(data, dict):
            return data
        data.pop("match_key", None)
        props = data.get("properties")
        if isinstance(props, dict):
            props = dict(props)
            for key in list(props):
                if key == "type":          # never clobber the discriminator
                    continue
                if key in cls.model_fields and key not in data:
                    data[key] = props.pop(key)
            data["properties"] = props
        return data

    # each concrete subclass defines its own `match_key` computed field below.


class LERNode(_NodeBase):
    type: Literal["LER"] = "LER"
    key: str                                   # LER number, e.g. 254-2025-006-00
    stub: bool = False                         # referenced-but-outside-corpus (SIMILAR_TO target)
    title: Optional[str] = None
    plant: Optional[str] = None

    @computed_field
    @property
    def match_key(self) -> str:
        return f"LER:{self.key}"


class UnitNode(_NodeBase):
    type: Literal["Unit"] = "Unit"
    key: str                                   # docket, e.g. 05000254

    @computed_field
    @property
    def match_key(self) -> str:
        return f"Unit:{self.key}"


class SystemNode(_NodeBase):
    type: Literal["System"] = "System"
    eiis_code: Optional[str] = None            # None => name-slug key (e.g. ADS)
    non_eiis: bool = False
    provisional: bool = False
    role: Optional[str] = None

    @computed_field
    @property
    def match_key(self) -> str:
        return f"System:{self.eiis_code or slug(self.display_name)}"


class ComponentNode(_NodeBase):
    type: Literal["Component"] = "Component"
    eiis_code: Optional[str] = None
    identifier: Optional[str] = None           # tag number, e.g. 1-2301-3
    model: Optional[str] = None
    manufacturer_code: Optional[str] = None
    inferred_code: bool = False                # code resolver-inferred, not stated in LER

    @computed_field
    @property
    def match_key(self) -> str:
        if self.eiis_code:
            return f"Component:{self.eiis_code}" + (f"|{self.identifier}" if self.identifier else "")
        return f"Component:{slug(self.display_name)}"


class FailureModeNode(_NodeBase):
    type: Literal["FailureMode"] = "FailureMode"
    description: Optional[str] = None

    @computed_field
    @property
    def match_key(self) -> str:
        return f"FailureMode:{slug(self.display_name)}"


class CauseNode(_NodeBase):
    type: Literal["Cause"] = "Cause"
    cause_code: CauseCode = "TBD"
    category: str                              # canonical category, or "provisional"
    theme: Optional[str] = None
    proximate_text: Optional[str] = None
    provisional: bool = False

    @computed_field
    @property
    def match_key(self) -> str:
        return f"Cause:{self.category}"


class ConsequenceNode(_NodeBase):
    type: Literal["Consequence"] = "Consequence"
    start: Optional[str] = None
    end: Optional[str] = None
    duration: Optional[str] = None
    tz: Optional[str] = None

    @computed_field
    @property
    def match_key(self) -> str:
        return f"Consequence:{slug(self.display_name)}"


class CorrectiveActionNode(_NodeBase):
    type: Literal["CorrectiveAction"] = "CorrectiveAction"
    status: CAStatus = "planned"
    provisional: bool = False

    @computed_field
    @property
    def match_key(self) -> str:
        return f"CorrectiveAction:{slug(self.display_name)}"


class ManufacturerNode(_NodeBase):
    type: Literal["Manufacturer"] = "Manufacturer"
    code: Optional[str] = None

    @computed_field
    @property
    def match_key(self) -> str:
        return f"Manufacturer:{self.code or slug(self.display_name)}"


class RegulatoryReferenceNode(_NodeBase):
    type: Literal["RegulatoryReference"] = "RegulatoryReference"
    ref_type: Optional[RegRefType] = None      # oracle stores this under properties['type']

    @computed_field
    @property
    def match_key(self) -> str:
        return f"RegulatoryReference:{slug(self.display_name)}"


Node = Annotated[
    Union[
        LERNode, UnitNode, SystemNode, ComponentNode, FailureModeNode,
        CauseNode, ConsequenceNode, CorrectiveActionNode, ManufacturerNode,
        RegulatoryReferenceNode,
    ],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# Edges
# --------------------------------------------------------------------------- #
class Edge(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: str                                # node id
    relation: Relation
    target: str                                # node id
    evidence: Optional[str] = None


# --------------------------------------------------------------------------- #
# Deterministic / header blocks
# --------------------------------------------------------------------------- #
class Identity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    accession_number: Optional[str] = None
    docket: str
    plant_name: Optional[str] = None
    unit: Optional[int] = None
    reactor_type: Optional[str] = None
    nss_vendor: Optional[str] = None
    event_date: str                            # ISO YYYY-MM-DD (form block 5 authoritative)
    report_date: Optional[str] = None
    operating_mode: Optional[str] = None
    power_level: Optional[int] = None
    discovery_context: Optional[DiscoveryContext] = None
    status: LERStatus
    revision: str = "00"
    ens_number: Optional[str] = None
    ens_date: Optional[str] = None
    ens_time: Optional[str] = None
    title: str


class ReportingBasis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reported_under: list[str] = Field(default_factory=list)
    ssff: str = "not stated"                   # "Y" | "N" | "not stated"


class Block13Row(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cause: Optional[str] = None
    system: Optional[str] = None
    component: Optional[str] = None
    manufacturer: Optional[str] = None
    reportable: Optional[str] = None


class CauseBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    proximate_text: Optional[str] = None
    cause_code: CauseCode = "TBD"
    category: str = "provisional"
    theme: Optional[str] = None
    provisional: bool = False


# --------------------------------------------------------------------------- #
# Record
# --------------------------------------------------------------------------- #
class LERRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ler_number: str
    identity: Identity
    reporting_basis: ReportingBasis
    block_13: list[Block13Row] = Field(default_factory=list)
    cause: CauseBlock
    nodes: list[Node]
    edges: list[Edge]
    chain: Optional[str] = None
    golden_questions: list[str] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _check_edge_integrity(self):
        ids = {n.id for n in self.nodes}
        bad = [(e.source, e.relation, e.target) for e in self.edges
               if e.source not in ids or e.target not in ids]
        if bad:
            raise ValueError(f"{self.ler_number}: edges reference unknown node ids: {bad}")
        return self

    # convenience for the scorer
    def node_keys(self) -> set[str]:
        return {n.match_key for n in self.nodes}

    def edge_triples(self) -> set[tuple[str, str, str]]:
        by_id = {n.id: n.match_key for n in self.nodes}
        return {(by_id[e.source], e.relation, by_id[e.target]) for e in self.edges}


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: str
    description: Optional[str] = None
    matching_notes: list[str] = Field(default_factory=list)
    lers: list[LERRecord]


if __name__ == "__main__":
    import json, pathlib, sys

    gt_path = sys.argv[1] if len(sys.argv) > 1 else "ground_truth.json"
    gt = GroundTruth.model_validate(json.loads(pathlib.Path(gt_path).read_text()))
    print(f"loaded {len(gt.lers)} LER records (schema {gt.schema_version})")
    for r in gt.lers:
        print(f"  {r.ler_number}: {len(r.nodes)} nodes, {len(r.edges)} edges, "
              f"{len(r.edge_triples())} triples")
