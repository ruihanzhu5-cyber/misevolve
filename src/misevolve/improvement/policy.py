"""Immutable policy versions and fixed experimental boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

BASE_SHA = "4cc377300be179d71d72888347dc3e5ff1132a2d"
ACTIONS = frozenset({"behavior_retest", "cross_receiver", "successor"})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PolicyVersion:
    gen: str
    val: tuple[str, ...]
    rep: str
    parent: str | None = None
    evidence: tuple[str, ...] = ()
    rationale: str = "initial policy"
    applicability: str = "text skills"
    expected_change: str = "preserve authorization conditions"

    def __post_init__(self):
        if not self.gen.strip() or not self.rep.strip() or not self.val or set(self.val) - ACTIONS:
            raise ValueError("policy requires generation/repair instructions and registered validation actions")
        if len(set(self.val)) != len(self.val):
            raise ValueError("duplicate validation actions")

    @property
    def hash(self):
        return digest(asdict(self))

    def to_dict(self):
        return {**asdict(self), "hash": self.hash}

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        expected = value.pop("hash", None)
        value["val"] = tuple(value["val"])
        value["evidence"] = tuple(value.get("evidence", ()))
        result = cls(**value)
        if expected is not None and expected != result.hash:
            raise ValueError("policy hash mismatch")
        return result

    def propose(self, patch, evidence):
        if set(patch) != {"component", "value", "rationale", "applicability", "expected_change"}:
            raise ValueError("meta proposal may only change one policy component")
        component = patch["component"]
        if component not in {"gen", "val", "rep"}:
            raise ValueError("fixed protocol, model, scorer and data permissions cannot be edited")
        values = asdict(self)
        values[component] = tuple(patch["value"]) if component == "val" else patch["value"]
        if values[component] == getattr(self, component):
            raise ValueError("proposal makes no policy change")
        values.update(
            parent=self.hash,
            evidence=tuple(evidence),
            **{key: patch[key] for key in ("rationale", "applicability", "expected_change")},
        )
        return PolicyVersion(**values)


@dataclass(frozen=True)
class Protocol:
    risk_threshold: float
    utility_margin: float
    improvement_margin: float
    primary: str
    min_units: int
    meta_every: int
    max_repairs: int
    max_windows: int
    rollback_after: int
    utility_definition: str
    risk_definition: str
    independent_unit: str
    hard_constraints: tuple[str, ...]
    mode: str = "empirical"
    missing: str = "HOLD"
    confirmation_reuse: str = "disjoint_shards"
    interval_method: str | None = None
    alpha_total: float | None = None
    independence_asserted: bool = False
    successor_enabled: bool = False
    max_changes_per_agent: int = 4
    max_structure_retries: int = 1

    def __post_init__(self):
        if self.mode not in {"empirical", "statistical"}:
            raise ValueError("unknown confirmation mode")
        if self.mode == "statistical" and (
            self.interval_method != "hoeffding_sequence_bonferroni"
            or self.alpha_total is None
            or not 0 < self.alpha_total < 1
            or self.independence_asserted is not True
        ):
            raise ValueError(
                "statistical mode requires Hoeffding intervals, total alpha and independent sequence units"
            )
        if (
            self.primary not in {"safety-first", "utility-first"}
            or self.missing != "HOLD"
            or self.confirmation_reuse != "disjoint_shards"
        ):
            raise ValueError("invalid fixed retention/missing/data-reuse protocol")
        if not all(0 <= n <= 1 for n in (self.risk_threshold, self.utility_margin, self.improvement_margin)):
            raise ValueError("rate thresholds must be in [0,1]")
        if min(self.min_units, self.meta_every, self.max_windows, self.rollback_after) < 1 or self.max_repairs < 0:
            raise ValueError("invalid stopping rules")
        if type(self.max_changes_per_agent) is not int or self.max_changes_per_agent < 1:
            raise ValueError("max_changes_per_agent must be a positive integer")
        if type(self.max_structure_retries) is not int or self.max_structure_retries not in {0, 1}:
            raise ValueError("max_structure_retries must be 0 or 1")
        if self.utility_definition != "team_utility_B_P" or self.risk_definition != "team_unsafe_action":
            raise ValueError("v1 implements team utility on B/P and assessment-v2 unsafe_action only")
        if self.independent_unit != "sequence" or set(self.hard_constraints) != {"no_observed_unsafe_action"}:
            raise ValueError("v1 requires sequence units and the fixed no-observed-unsafe-action constraint")

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["hard_constraints"] = tuple(value["hard_constraints"])
        return cls(**value)

    @property
    def hash(self):
        return digest(asdict(self))
