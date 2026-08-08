"""Lineage + verification-preservation (SafeEvolve, paper RQ3).

Verification preservation (paper §SafeEvolve): "Every revision preserves existing
verification and reversibility requirements unless the update supplies evidence that
they are obsolete." We detect, by robust surface signals, which safety REQUIREMENTS a
SKILL.md asserts (authorization, confirmation, validation, provenance, integrity,
reversibility, scope-narrowing, secret-handling). A revision that DROPS a requirement its
ancestor had — without an explicit obsolescence justification — fails preservation and is
repaired or quarantined rather than admitted.

This is deliberately a lightweight lexical detector (no LLM): it is the deterministic
guard the auditor's LLM decision is checked against, so a lenient/degraded auditor can
never silently wave through a requirement removal.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_SIGNALS: dict[str, list[str]] = {
    "authorization": [
        r"\bauthoriz",
        r"\bpermission",
        r"\baccess control",
        r"\brbac\b",
        r"\bprivilege",
        r"\ballow\s*list",
        r"\ballowlist",
        r"\bwhitelist",
    ],
    "confirmation": [
        r"\bconfirm",
        r"\bare you sure",
        r"\bexplicit(ly)? (approv|consent)",
        r"\buser (approval|consent)",
        r"\bprompt the user",
        r"\bdouble.?check",
    ],
    "validation": [
        r"\bvalidat",
        r"\bverif(y|ication)",
        r"\bcheck (that|the|for)",
        r"\bsanitiz",
        r"\bschema",
        r"\bassert",
        r"\bprecondition",
    ],
    "provenance": [
        r"\bprovenance",
        r"\btrusted (source|endpoint)",
        r"\bverify.*(source|origin|endpoint)",
        r"\bpin(ned)?\b",
        r"\bknown[- ]good",
        r"\bsource of truth",
    ],
    "integrity": [
        r"\bchecksum",
        r"\bhash\b",
        r"\bsignature",
        r"\bintegrity",
        r"\bsha256",
        r"\bgpg\b",
        r"\bverify.*(hash|signature|checksum)",
    ],
    "reversibility": [
        r"\brollback",
        r"\bdry[- ]?run",
        r"\bundo\b",
        r"\brevers",
        r"\bbackup",
        r"\bidempoten",
        r"\brecover",
    ],
    "scope": [
        r"\bonly (when|if|for)",
        r"\bnarrow",
        r"\bscope",
        r"\bapplies only",
        r"\brestrict",
        r"\bnever (run|use|apply)",
        r"\bdo not .* unless",
    ],
    "secret": [
        r"\bsecret",
        r"\bcredential",
        r"\bapi key",
        r"\bnever (log|print|echo|expose)",
        r"\bredact",
        r"\bmask",
        r"\bdata minim",
    ],
}


def verification_signals(text: str) -> set[str]:
    """The set of safety requirement families a SKILL.md asserts."""
    t = (text or "").lower()
    out = set()
    for fam, pats in _SIGNALS.items():
        if any(re.search(p, t) for p in pats):
            out.add(fam)
    return out


_OBSOLETE = re.compile(
    r"(no longer (needed|required|applies)|obsolete|deprecated|superseded|"
    r"redundant with|已(过时|废弃|不再需要)|不再适用)",
    re.I,
)


def preserves_verification(candidate_text: str, ancestor_text: str | None) -> tuple[bool, set[str]]:
    """True iff the candidate keeps every requirement family its ancestor asserted (or the
    candidate text explicitly justifies the removal as obsolete). Returns (ok, dropped)."""
    if not ancestor_text:
        return True, set()
    anc = verification_signals(ancestor_text)
    cand = verification_signals(candidate_text)
    dropped = anc - cand
    if dropped and _OBSOLETE.search(candidate_text or ""):
        return True, set()
    return (not dropped), dropped


@dataclass
class Lineage:
    """Per-skill lineage recorded on admission (embedded in SKILL.md frontmatter for the
    clean P reload; full detail also kept in the governance sidecar)."""

    skill_id: str
    version: str | None = None
    ancestor_id: str | None = None
    source_tasks: list[str] = field(default_factory=list)
    source_kind: str = ""
    round: int = 0
    risk: float | None = None
    audits: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    verification_required: list[str] = field(default_factory=list)
    rollback_target: str | None = None
    status: str = "active"

    def to_frontmatter(self) -> dict:
        """The minimal lineage that must survive the SKILL.md-only clean P reload."""
        return {
            "governance_skill_id": self.skill_id,
            "governance_version": self.version,
            "governance_ancestor": self.ancestor_id,
            "governance_exposure": self.source_kind,
            "governance_risk": self.risk,
            "governance_status": self.status,
            "governance_verification": ",".join(self.verification_required),
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
