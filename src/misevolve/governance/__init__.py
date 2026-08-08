"""SkillMisevo-Gym — governance layer (paper RQ3).

A GOVERNANCE wrapper sits at the persistent-update boundary of an existing
evolution method (AutoSkill / EvoSkill / SkillsVote) WITHOUT reimplementing the
method. Two policies share identical hooks / budget / capacity / cadence:

  * UtilityOnlyGovernance  — decisions use ONLY native validity + benign-utility
    evidence (generic library hygiene). Blind to safety by construction.
  * SafeEvolveGovernance   — may additionally audit/repair candidate content and
    use safety provenance/lineage.

The wrapper enforces policy decisions on the method's own skill store through a
per-method GovernableStore adapter (core/governance/stores.py) — EvoSkill/SkillsVote
hide skills by moving the on-disk bundle out of the active dir; AutoSkill drives
its native LocalSkillStore (SkillStatus.ARCHIVED) since its retrieval is cached.
"""

from .base import (
    ALLOWED_UTILITY_FIELDS,
    FORBIDDEN_FIELDS,
    AdmissionDecision,
    Candidate,
    GovernableStore,
    GovernanceConfig,
    GovernancePolicy,
    GovernedMethodHarness,
    SkillBundle,
    UtilitySignal,
    build_governed,
)

__all__ = [
    "ALLOWED_UTILITY_FIELDS",
    "FORBIDDEN_FIELDS",
    "AdmissionDecision",
    "Candidate",
    "GovernableStore",
    "GovernanceConfig",
    "GovernancePolicy",
    "GovernedMethodHarness",
    "SkillBundle",
    "UtilitySignal",
    "build_governed",
]
