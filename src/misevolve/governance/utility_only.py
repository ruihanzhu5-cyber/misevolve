"""Utility-only governance control (paper RQ3 middle condition).

Generic library hygiene with NO safety-specific evidence. The policy sees only a
`UtilitySignal` (native validity + smoothed benign-utility + capacity/age); it cannot
read skill text, CU/UG/Stealth, attack_success/harmful, M/B/P kind, the malicious
goal, or exposure/source status, because none of those are on the object.

Decisions:
  * ADMIT every natively valid candidate while capacity permits; REJECT only on a
    native structural failure. No content repair (it has no content to repair).
  * Capacity eviction: retain by smoothed utility, deterministic age/key tie-break.
  * Retirement: drop skills whose smoothed utility falls below the frozen threshold,
    gated by a minimum number of attributed benign-utility observations. An
    observation without a utility score is NEUTRAL, never a failure (all M tasks).
"""

from __future__ import annotations

from .base import (
    AdmissionDecision,
    DecisionKind,
    GovernanceConfig,
    GovernancePolicy,
    UtilitySignal,
)


class UtilityOnlyGovernance(GovernancePolicy):
    name = "utility-only"
    needs_content = False
    can_repair = False

    def decide_admission(self, cand: UtilitySignal) -> AdmissionDecision:

        if not cand.native_valid:
            return AdmissionDecision(DecisionKind.REJECT, reason="native-invalid")

        return AdmissionDecision(DecisionKind.ADMIT, reason="valid")

    def choose_evictions(self, actives: list[UtilitySignal], capacity: int) -> list[str]:
        if len(actives) <= capacity:
            return []

        order = sorted(actives, key=lambda s: (s.utility_score, s.age, s.key))
        n_evict = len(actives) - capacity
        return [s.key for s in order[:n_evict]]

    def choose_retirements(self, actives: list[UtilitySignal], cfg: GovernanceConfig) -> list[str]:
        out = []
        for s in actives:
            n_obs = s.n_success + s.n_failure
            if n_obs >= cfg.min_observations and s.utility_score < cfg.retirement_threshold:
                out.append(s.key)
        return out

    def frozen_config(self) -> dict:
        return {"policy": self.name, "uses": ["native_valid", "benign_utility", "capacity", "age"]}
