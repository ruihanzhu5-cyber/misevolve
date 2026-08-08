"""SafeEvolve governance unit tests — standalone (no LLM / no docker).

The admission critic + repair are MONKEYPATCHED to deterministic fakes so the write/reuse
governance logic is tested without network. Run: `python core/governance/tests/test_safeevolve.py`
or `pytest core/governance/tests/`.
"""

from __future__ import annotations

from pathlib import Path

import misevolve.governance.safeevolve as SE
from misevolve import harness
from misevolve.governance.base import (
    Candidate,
    GovernanceConfig,
    GovernedMethodHarness,
    SkillBundle,
    Status,
)
from misevolve.governance.safeevolve import SafeEvolveGovernance

from .test_utility_only import FakeInner, FakeStore

_CONFIG_DIR = Path(SE.__file__).resolve().parent / "config"


def _audit(decision, risk):
    return {
        "decision": decision,
        "risk": risk,
        "risk_categories": [],
        "unsafe_generalization": "",
        "omitted_verification": [],
        "authorization_required": [],
        "irreversible_effects": [],
        "egress_provenance": [],
        "secret_handling": [],
        "evidence_spans": [],
        "required_repairs": ["narrow scope"],
        "confidence": 0.9,
        "model": "fake",
    }


def _mk(inner, cfg=None):
    cfg = cfg or GovernanceConfig.load(_CONFIG_DIR / "safeevolve.json")
    g = GovernedMethodHarness(
        inner, SafeEvolveGovernance(), cfg, store_factory=FakeStore, log_dir=Path("/tmp/_gov_se_test")
    )
    g.setup(Path("/tmp/_gov_se_test"), agent=object())
    return g


def _evolve_one(g):
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(None, None)], kind="M", round_idx=1)


def test_unsafe_candidate_rejected(monkeypatch):
    monkeypatch.setattr(SE, "audit_candidate", lambda b, a, *, cfg: _audit("reject", 0.95))
    inner = FakeInner(author_plan=[[("bad", True)]])
    g = _mk(inner)
    _evolve_one(g)
    assert "bad" not in {s.key for s in g.store.active_skills()}, "unsafe skill not rejected"
    assert g._state["bad"].status == Status.RETIRED
    print("ok test_unsafe_candidate_rejected")


def test_repair_then_readmit(monkeypatch):
    seen = []

    def audit(b, a, *, cfg):
        seen.append(b.text)
        return _audit("accept", 0.1) if "REPAIRED" in b.text else _audit("repair", 0.7)

    monkeypatch.setattr(SE, "audit_candidate", audit)
    monkeypatch.setattr(
        SE, "repair_candidate", lambda b, audit, a, *, cfg: "---\nname: k\n---\n# k REPAIRED\nconfirm + validate\n"
    )
    inner = FakeInner(author_plan=[[("k", True)]])
    g = _mk(inner)
    _evolve_one(g)
    active = {s.key: s.text for s in g.store.active_skills()}
    assert "k" in active, "repaired skill not admitted"
    assert "REPAIRED" in active["k"], "store not updated with repaired text"
    assert len(seen) >= 2, f"repaired bundle not RE-AUDITED (audit calls={len(seen)})"
    print("ok test_repair_then_readmit")


def test_verification_removal_forces_repair(monkeypatch):

    monkeypatch.setattr(SE, "audit_candidate", lambda b, a, *, cfg: _audit("accept", 0.1))
    repaired = {"n": 0}

    def repair(b, audit, a, *, cfg):
        repaired["n"] += 1
        return "---\nname: k\n---\n# k\nAlways confirm and validate before running.\n"

    monkeypatch.setattr(SE, "repair_candidate", repair)

    inner = FakeInner(author_plan=[[("k", True)]])
    inner.skills["k"] = {
        "text": "---\nname: k\n---\n# k\nAlways confirm and validate before running.\n",
        "status": "active",
    }
    g = _mk(inner)

    def evolve(traces, outcomes, *, kind, round_idx):
        inner.skills["k"]["text"] = "---\nname: k\n---\n# k\nJust run it directly.\n"

    inner.evolve_batch = evolve
    _evolve_one(g)
    assert repaired["n"] >= 1, "verification removal did not force repair"
    print("ok test_verification_removal_forces_repair")


def test_quarantine_excluded_from_retrieval(monkeypatch):
    monkeypatch.setattr(SE, "audit_candidate", lambda b, a, *, cfg: _audit("quarantine", 0.8))
    monkeypatch.setattr(SE, "repair_candidate", lambda b, audit, a, *, cfg: None)
    inner = FakeInner(author_plan=[[("q", True)]])
    g = _mk(inner)
    _evolve_one(g)
    assert "q" not in {s.key for s in g.authored_skills()}, "quarantined skill leaked into P reload"
    assert g._state["q"].status == Status.QUARANTINED
    print("ok test_quarantine_excluded_from_retrieval")


def test_reuse_risk_retirement(monkeypatch):
    monkeypatch.setattr(SE, "audit_candidate", lambda b, a, *, cfg: _audit("accept", 0.1))
    inner = FakeInner(author_plan=[[("k", True)]])
    g = _mk(inner)
    _evolve_one(g)
    assert "k" in {s.key for s in g.store.active_skills()}

    g._state["k"].n_unsafe_reuse = 3
    actives = g._active_signals()
    retire = SafeEvolveGovernance().choose_retirements(actives, g.cfg)
    assert "k" in retire, "repeated unsafe reuse not retired"
    print("ok test_reuse_risk_retirement")


def test_ablation_no_paired_deleter_retains_native(monkeypatch):
    monkeypatch.setattr(SE, "audit_candidate", lambda b, a, *, cfg: _audit("repair", 0.8))
    monkeypatch.setattr(
        SE,
        "repair_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deleter was called")),
    )
    cfg = GovernanceConfig.load(_CONFIG_DIR / "safeevolve_no_paired_deleter.json")
    inner = FakeInner(author_plan=[[("native", True)]])
    g = _mk(inner, cfg)
    _evolve_one(g)
    assert "REPAIRED" not in inner.skills["native"]["text"]
    assert g._state["native"].lineage["risk"] == 0.8
    assert g._state["native"].audits, "critic evidence was not retained"
    assert all(t["status"] != Status.QUARANTINED.value for t in g._state["native"].transitions)

    assert any(t["reason"] == "maintenance-retire" for t in g._state["native"].transitions)
    print("ok test_ablation_no_paired_deleter_retains_native")


def test_ablation_no_reuse_risk_attribution(monkeypatch):
    cfg = GovernanceConfig.load(_CONFIG_DIR / "safeevolve_no_reuse_risk_attribution.json")
    inner = FakeInner(author_plan=[[]])
    inner.skills["k"] = {"text": "---\nname: k\n---\n# k\n", "status": "active"}
    g = _mk(inner, cfg)
    from misevolve.governance.base import _SkillState

    g._state["k"] = _SkillState("k", 0)
    traj = harness.Trajectory([], "")
    traj._gov_pos = 1
    g._pending[1] = ["k"]
    g.evolve_batch([traj], [harness.TaskOutcome(None, True)], kind="M", round_idx=1)
    assert g._state["k"].n_unsafe_reuse == 0
    assert not any(e.get("event") == "reuse_risk" for e in g._events)
    print("ok test_ablation_no_reuse_risk_attribution")


def test_ablation_no_safety_retirement_keeps_risky_skill():
    cfg = GovernanceConfig.load(_CONFIG_DIR / "safeevolve_no_safety_retirement.json")
    policy = SafeEvolveGovernance()
    policy.cfg = cfg
    risky = Candidate(
        bundle=SkillBundle("k", "# k"),
        native_valid=True,
        age=1,
        n_success=0,
        n_failure=0,
        utility_score=0.5,
        capacity_used=1,
        capacity_max=32,
        block=1,
        n_unsafe_reuse=10,
        lineage={"risk": 0.95},
    )
    assert policy.choose_retirements([risky], cfg) == []

    low_utility = Candidate(
        bundle=SkillBundle("u", "# u"),
        native_valid=True,
        age=1,
        n_success=0,
        n_failure=3,
        utility_score=0.2,
        capacity_used=1,
        capacity_max=32,
        block=1,
        lineage={"risk": 0.0},
    )
    assert policy.choose_retirements([low_utility], cfg) == ["u"]
    print("ok test_ablation_no_safety_retirement_keeps_risky_skill")


def test_no_eval_judge_leakage():

    import re

    bad = [
        r"^\s*import\s+(content_judge|ahz_judge)\b",
        r"^\s*from\s+(content_judge|ahz_judge)\b",
        r"\b(content_judge|ahz_judge)\.\w",
    ]
    for mod in (
        SE,
        __import__("misevolve.governance.audit", fromlist=["x"]),
        __import__("misevolve.governance.repair", fromlist=["x"]),
    ):
        src = open(mod.__file__).read()
        for pat in bad:
            m = re.search(pat, src, re.M)
            assert not m, f"{mod.__name__} leaks the eval judge: {m.group(0)!r}"
    print("ok test_no_eval_judge_leakage")


def test_shared_budget_with_utility_only():
    se = GovernanceConfig.load(_CONFIG_DIR / "safeevolve.json")
    uo = GovernanceConfig.load(_CONFIG_DIR / "utility_only.json")
    for k in ("capacity", "min_observations", "cadence_blocks", "alpha", "beta", "retirement_threshold"):
        assert getattr(se, k) == getattr(uo, k), f"budget mismatch on {k}"
    print("ok test_shared_budget_with_utility_only")


class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, val):
        old = getattr(obj, name)
        self._undo.append((obj, name, old))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo = []


def _run_all():
    import inspect

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        mp = _MP()
        try:
            fn(mp) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            passed += 1
        finally:
            mp.undo()
    print(f"\nALL {passed}/{len(fns)} SafeEvolve tests passed.")


if __name__ == "__main__":
    _run_all()
