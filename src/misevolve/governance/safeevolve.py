"""SafeEvolve governance policy (paper RQ3).

Safety-aware governance at the write + reuse boundaries, sharing SafeEvolve's hooks /
budget / capacity / cadence with the Utility-only control but ALSO using safety evidence:

  WRITE-TIME (decide_admission):
    1. an INDEPENDENT admission critic (audit.py) audits the complete candidate bundle;
    2. verification-preservation (lineage.py) is checked against the ancestor and OVERRIDES
       a lenient "accept" — a revision that silently drops a requirement is repaired/quarantined;
    3. accept (low risk, preserved) -> admit; otherwise targeted repair (repair.py) then
       RE-AUDIT (the wrapper loops up to max_repair_attempts) -> admit if it now passes,
       else quarantine; reject only structural/unsalvageable.
  REUSE-TIME (choose_retirements / choose_evictions):
    combine benign utility with frozen provenance risk + accumulated unsafe-reuse evidence
    to retire / downweight risky skills; capacity eviction prefers useful & low-risk skills.

The library keeps lineage (ancestor, exposure, audits, repairs, risk, status) for every
admitted skill; the minimal lineage is embedded in SKILL.md frontmatter so it survives the
clean P reload.
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

        if decision == "accept" and ver_ok and risk < self.cfg.admission_risk_threshold:
            return AdmissionDecision(DecisionKind.ADMIT, reason="audit-accept", risk=risk, audit=audit_rec)

        if self.cfg.extra.get("disable_paired_deleter", False):
            return AdmissionDecision(DecisionKind.ADMIT, reason="audit-retain-no-deleter", risk=risk, audit=audit_rec)

        if decision == "reject":
            return AdmissionDecision(DecisionKind.REJECT, reason="audit-reject", risk=risk, audit=audit_rec)

        if self.can_repair and not self.cfg.extra.get("disable_repair", False):
            repaired = repair_candidate(cand.bundle, audit, anc, cfg=self.cfg)
            if repaired and repaired.strip() and repaired != cand.bundle.text:
                why = (
                    "audit-repair"
                    if decision == "repair"
                    else ("verification-restore" if not ver_ok else "risk-repair")
                )
                return AdmissionDecision(
                    DecisionKind.REPAIR, reason=why, repaired_text=repaired, risk=risk, audit=audit_rec
                )
        return AdmissionDecision(DecisionKind.QUARANTINE, reason="unrepairable-risk", risk=risk, audit=audit_rec)

    def choose_evictions(self, actives: list[Candidate], capacity: int) -> list[str]:
        if len(actives) <= capacity:
            return []

        def rank(c: Candidate):
            risk = float(c.lineage.get("risk", 0.0) or 0.0)
            return (c.utility_score - 0.5 * risk, c.age, c.bundle.key)

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
