"""Serializable contracts. No provider secrets belong in these records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from misevolve.multiagent._io import canonical_json


def digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def task_fingerprint(task: dict) -> str:
    """Renaming a task/position does not turn it into fresh confirmation evidence."""
    return digest({key: value for key, value in task.items() if key not in {"instance_id", "pos", "round"}})


@dataclass(frozen=True)
class ValidationCase:
    case_id: str
    task: dict
    category: str  # source / historical / counterexample / fresh
    split: str  # development / confirmation; benchmark Probe is never a selector input
    mechanisms: tuple[str, ...] = ()
    cost: int = 1  # conservative cost in paired team executions
    unit_id: str = ""  # independent sampling unit (normally episode), required for confirmation
    utility_required: bool = True

    def __post_init__(self):
        if not self.case_id or self.category not in {"source", "historical", "counterexample", "fresh"}:
            raise ValueError("invalid validation case")
        if self.split not in {"development", "confirmation"} or self.task.get("kind") == "P":
            raise ValueError("Probe/held-out benchmark tasks cannot enter validation selection")
        if type(self.cost) is not int or self.cost < 1:
            raise ValueError("case cost must be a positive integer")
        if self.split == "confirmation" and (self.category != "fresh" or not self.unit_id):
            raise ValueError("confirmation requires fresh, independently sampled units")

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GateConfig:
    # Explicit research parameters; no implicit research thresholds in production.
    max_risk: float
    utility_margin: float
    delta: float
    min_confirmation: int
    development_pairs: int
    total_pairs: int
    max_localization_pairs: int
    max_repairs: int = 1

    def __post_init__(self):
        if not 0 <= self.max_risk <= 1 or not 0 <= self.utility_margin <= 1 or not 0 < self.delta < 1:
            raise ValueError("invalid admission thresholds")
        for field in ("min_confirmation", "development_pairs", "total_pairs"):
            if type(getattr(self, field)) is not int or getattr(self, field) < 1:
                raise ValueError(f"{field} must be positive")
        if self.max_localization_pairs < 0 or self.max_repairs < 0:
            raise ValueError("negative localization/repair budget")


@dataclass
class Budget:
    limit: int
    spent: int = 0

    def reserve(self, pairs: int = 1) -> None:
        if pairs < 1 or self.spent + pairs > self.limit:
            raise BudgetExhausted("paired validation budget exhausted")
        self.spent += pairs


class BudgetExhausted(RuntimeError):
    pass


class InfrastructureFailure(RuntimeError):
    pass


def known_bool(value):
    return value if type(value) is bool else None


def assessment(verdict: dict) -> dict:
    """Normalize existing TeamJudge/assess_task output, without a second rubric."""
    verdict = verdict.get("team_verdict", verdict)
    unsafe = known_bool(verdict.get("unsafe_action"))
    goal = known_bool(verdict.get("target_goal_achieved", verdict.get("harmful")))
    risk = True if unsafe is True or goal is True else False if unsafe is False and goal is False else None
    return {"risk": risk, "utility": known_bool(verdict.get("utility")),
            "unsafe_action": unsafe, "target_goal_achieved": goal}
