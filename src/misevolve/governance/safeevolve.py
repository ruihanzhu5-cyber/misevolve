"""SafeEvolve governance policy (paper RQ3, Algorithm 1).

Safety-aware governance at the write and reuse boundaries, sharing its hooks, budget,
capacity, and cadence with the Utility-only control but also using safety evidence:

  WRITE-TIME (decide_admission, driven by the wrapper in base.py):
    1. a non-blocking critic (audit.py) audits the complete candidate with its lineage and
       returns a risk score with the unsafe spans;
    2. verification preservation (lineage.py) against the ancestor counts as a finding;
    3. a paired deleter (repair.py) may only delete or narrow the flagged spans. The wrapper
       re-audits each repair for at most two rounds and keeps it only when it stays valid with
       lower risk. Otherwise the native candidate enters the library with its audit attached.
  REUSE-TIME (choose_retirements / choose_evictions):
    retrieval risk is attributed to retrieved skills. A skill retires after two harmful reuses,
    once effective risk reaches the threshold, or when utility stays low. Capacity eviction
    removes the skill with the lowest utility minus risk.

The library keeps lineage (ancestor, exposure, audits, repairs, risk, status) for every
admitted skill. The minimal lineage is embedded in SKILL.md frontmatter so it survives the
clean reload for the new session.
"""

from __future__ import annotations

from .audit import audit_candidate
from .base import (
    AdmissionDecision,
    Candidate,
    DecisionKind,
    GovernanceConfig,
    GovernancePolicy,
)
from .lineage import preserves_verification, verification_signals
from .repair import repair_candidate


class SafeEvolveGovernance(GovernancePolicy):
    name = "safeevolve"
    needs_content = True
    can_repair = True

    def decide_admission(self, cand: Candidate) -> AdmissionDecision:
        if not cand.native_valid:
            return AdmissionDecision(DecisionKind.REJECT, reason="native-invalid")
        anc = cand.lineage.get("ancestor_text")
        audit = audit_candidate(cand.bundle, anc, cfg=self.cfg)
        ver_ok, dropped = preserves_verification(cand.bundle.text, anc)
        risk = float(audit.get("risk", 0.5))
        decision = audit.get("decision", "quarantine")
        audit_rec = {
            **audit,
            "verification_dropped": sorted(dropped),
            "verification_ok": ver_ok,
            "verification_required": sorted(verification_signals(cand.bundle.text)),
        }

        if decision == "accept" and ver_ok:
            return AdmissionDecision(DecisionKind.ADMIT, reason="audit-accept", risk=risk, audit=audit_rec)

        if self.cfg.extra.get("disable_paired_deleter", False):
            return AdmissionDecision(DecisionKind.ADMIT, reason="audit-retain-no-deleter", risk=risk, audit=audit_rec)

        if self.can_repair and not self.cfg.extra.get("disable_repair", False):
            repaired = repair_candidate(cand.bundle, audit, anc, cfg=self.cfg)
            if repaired and repaired.strip() and repaired != cand.bundle.text:
                why = "verification-restore" if not ver_ok else "audit-delete"
                return AdmissionDecision(
                    DecisionKind.REPAIR, reason=why, repaired_text=repaired, risk=risk, audit=audit_rec
                )
        return AdmissionDecision(DecisionKind.ADMIT, reason="audit-retain-native", risk=risk, audit=audit_rec)

    def choose_evictions(self, actives: list[Candidate], capacity: int) -> list[str]:
        if len(actives) <= capacity:
            return []

        def rank(c: Candidate):
            risk = float(c.lineage.get("risk", 0.0) or 0.0)
            return (c.utility_score - risk, c.age, c.bundle.key)

        order = sorted(actives, key=rank)
        return [c.bundle.key for c in order[: len(actives) - capacity]]

    def choose_retirements(self, actives: list[Candidate], cfg: GovernanceConfig) -> list[str]:
        max_unsafe = int(cfg.extra.get("max_unsafe_reuse", 2))
        disable_safety = bool(cfg.extra.get("disable_safety_retirement", False))
        out = []
        for c in actives:
            risk = float(c.lineage.get("risk", 0.0) or 0.0)
            eff_risk = risk + 0.2 * c.n_unsafe_reuse
            n_obs = c.n_success + c.n_failure
            if not disable_safety and (c.n_unsafe_reuse >= max_unsafe or eff_risk >= cfg.reuse_risk_threshold):
                out.append(c.bundle.key)
            elif n_obs >= cfg.min_observations and c.utility_score < cfg.retirement_threshold:
                out.append(c.bundle.key)
        return out

    def frozen_config(self) -> dict:
        return {
            "policy": self.name,
            "uses": [
                "audit_critic",
                "verification_preservation",
                "repair",
                "lineage",
                "provenance_risk",
                "unsafe_reuse",
                "benign_utility",
                "capacity",
                "age",
            ],
        }
