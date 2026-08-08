"""Utility-only governance unit tests — standalone (no docker / no minimax).

Runnable with `pytest core/governance/tests/` OR `python core/governance/tests/test_utility_only.py`.
Uses an in-memory fake method + fake store to exercise the REAL GovernedMethodHarness
wrapper and UtilityOnlyGovernance policy.
"""

from __future__ import annotations

from pathlib import Path

from misevolve import harness
from misevolve.governance.base import (
    ALLOWED_UTILITY_FIELDS,
    FORBIDDEN_FIELDS,
    Candidate,
    GovernanceConfig,
    GovernedMethodHarness,
    SkillBundle,
    Status,
    UtilitySignal,
    _SkillState,
)
from misevolve.governance.utility_only import UtilityOnlyGovernance


class FakeInner(harness.MethodHarness):
    name = "autoskill"
    needs_reward = False
    evolves_on_benign = True

    def __init__(self, author_plan=None):

        self._plan = author_plan or []
        self._call = 0
        self.skills: dict[str, dict] = {}
        self._retrieve_next: list[str] = []

    def setup(self, episode_dir, agent):
        self.agent = agent

    def set_retrieval(self, keys):
        self._retrieve_next = list(keys)

    def run_task(self, prompt, *, kind, pos, out_dir):
        self._last_retrieved_keys = [k for k in self._retrieve_next if self.skills.get(k, {}).get("status") == "active"]
        return harness.Trajectory(messages=[{"role": "assistant", "content": prompt}], final_text="ok")

    def evolve_batch(self, traces, outcomes, *, kind, round_idx):
        plan = self._plan[self._call] if self._call < len(self._plan) else []
        self._call += 1
        for key, valid in plan:
            text = f"---\nname: {key}\n---\n# {key}\nreusable procedure\n" if valid else "   "
            self.skills[key] = {"text": text, "status": "active"}

    def authored_skills(self):
        return [harness.Skill(k, v["text"], "M", 0) for k, v in self.skills.items() if v["status"] == "active"]

    def teardown(self):
        pass


class FakeStore:
    def __init__(self, inner):
        self.inner = inner

    def snapshot_skill_bundles(self):
        return [SkillBundle(k, v["text"]) for k, v in self.inner.skills.items() if v["status"] != "gone"]

    def is_valid(self, key):
        t = self.inner.skills.get(key, {}).get("text", "")
        return bool(t and t.strip()) and ("name:" in t or t.lstrip().startswith("#"))

    def replace_skill(self, key, text, referenced_files=None):
        if key in self.inner.skills:
            self.inner.skills[key]["text"] = text

    def set_skill_status(self, key, status):
        if key in self.inner.skills:
            self.inner.skills[key]["status"] = "active" if status == Status.ACTIVE else status.value

    def active_skills(self):
        return self.inner.authored_skills()

    def last_retrieved_skill_keys(self):
        return list(
            getattr(self.inner, "_last_retrieved_keys", None)
            or [s.key for s in getattr(self.inner, "_last_retrieved", [])]
        )

    def export_active_skills(self):
        return self.inner.authored_skills()


class SpyPolicy(UtilityOnlyGovernance):
    """Records every object handed to it — to assert isolation."""

    def __init__(self):
        self.seen = []

    def decide_admission(self, cand):
        self.seen.append(cand)
        return super().decide_admission(cand)


def _mk(inner, policy=None, cfg=None):
    policy = policy or UtilityOnlyGovernance()
    cfg = cfg or GovernanceConfig(capacity=32, min_observations=2, retirement_threshold=0.35, cadence_blocks=1)
    g = GovernedMethodHarness(inner, policy, cfg, store_factory=FakeStore, log_dir=Path("/tmp/_gov_test"))
    g.setup(Path("/tmp/_gov_test"), agent=object())
    return g


def test_forbidden_field_isolation():
    """UtilitySignal STRUCTURALLY cannot carry safety/label/content fields, and the
    utility-only policy only ever receives UtilitySignal (never Candidate)."""
    fields = set(UtilitySignal.__dataclass_fields__)
    assert fields <= ALLOWED_UTILITY_FIELDS, f"leaked non-allowed fields: {fields - ALLOWED_UTILITY_FIELDS}"
    assert not (fields & FORBIDDEN_FIELDS), f"forbidden field on UtilitySignal: {fields & FORBIDDEN_FIELDS}"

    sig = UtilitySignal("k", True, 0, 0, 0, 0.5, 1, 32, 0)
    for bad in ("text", "content_unsafety", "attack_success", "kind", "malicious_goal", "exposure"):
        assert not hasattr(sig, bad), f"UtilitySignal unexpectedly exposes {bad}"

    inner = FakeInner(author_plan=[[("a", True)], [("b", True)]])
    spy = SpyPolicy()
    g = _mk(inner, policy=spy)
    for pos in (1, 2, 3):
        g.run_task("p", kind="M", pos=pos, out_dir=Path("/tmp"))
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(True, None)], kind="M", round_idx=1)
    assert spy.seen, "policy never consulted"
    assert all(isinstance(x, UtilitySignal) for x in spy.seen), (
        f"utility-only policy received non-UtilitySignal: {[type(x).__name__ for x in spy.seen]}"
    )
    assert not any(isinstance(x, Candidate) for x in spy.seen)
    print("ok test_forbidden_field_isolation")


def test_neutral_missing_utility():
    """utility=None (all M tasks) is NEUTRAL — never a failure."""
    inner = FakeInner(author_plan=[[]])
    inner.skills["s0"] = {"text": "---\nname: s0\n---\n# s0\n", "status": "active"}
    g = _mk(inner)
    g._state.setdefault("s0", _SkillState("s0", 0))
    for pos in (1, 2, 3):
        inner.set_retrieval(["s0"])
        g.run_task("p", kind="M", pos=pos, out_dir=Path("/tmp"))
    traces = [harness.Trajectory([], "") for _ in range(3)]
    for t, pos in zip(traces, (1, 2, 3)):
        t._gov_pos = pos

    g.evolve_batch(traces, [harness.TaskOutcome(True, None)] * 3, kind="M", round_idx=1)
    st = g._state["s0"]
    assert st.n_success == 0 and st.n_failure == 0, f"None utility mutated stats: {st}"
    print("ok test_neutral_missing_utility")


def test_retrieval_attribution():
    """A valid benign-utility outcome is attributed to the skills retrieved for that task."""
    inner = FakeInner(author_plan=[[]])
    inner.skills["s0"] = {"text": "---\nname: s0\n---\n# s0\n", "status": "active"}
    g = _mk(inner)
    traces = []
    for pos in (1, 2, 3):
        inner.set_retrieval(["s0"])
        t = g.run_task("p", kind="B", pos=pos, out_dir=Path("/tmp"))
        traces.append(t)

    outs = [harness.TaskOutcome(None, True), harness.TaskOutcome(None, None), harness.TaskOutcome(None, False)]
    g.evolve_batch(traces, outs, kind="B", round_idx=1)
    st = g._state["s0"]
    assert st.n_success == 1 and st.n_failure == 1, f"attribution wrong: {st}"
    print("ok test_retrieval_attribution")


def test_deterministic_capacity_eviction():
    """Capacity eviction is deterministic and drops lowest-utility skills."""
    cfg = GovernanceConfig(capacity=2, min_observations=99, cadence_blocks=1)
    inner = FakeInner(author_plan=[[("a", True), ("b", True), ("c", True)]])
    g = _mk(inner, cfg=cfg)

    for k, ns, nf in (("a", 3, 0), ("b", 1, 3), ("c", 0, 5)):
        g._state[k] = _SkillState(k, 0, n_success=ns, n_failure=nf)
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(None, None)], kind="M", round_idx=1)
    active = {s.key for s in g.store.active_skills()}
    assert len(active) == 2, f"capacity not enforced: {active}"
    assert "c" not in active, f"kept lowest-utility skill c: {active}"
    assert {"a", "b"} == active, f"expected top-2 utility {{a,b}}, got {active}"

    order1 = UtilityOnlyGovernance().choose_evictions(g._active_signals(), 1)
    order2 = UtilityOnlyGovernance().choose_evictions(g._active_signals(), 1)
    assert order1 == order2
    print("ok test_deterministic_capacity_eviction")


def test_invalid_candidate_rejected():
    inner = FakeInner(author_plan=[[("good", True), ("bad", False)]])
    g = _mk(inner)
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(None, None)], kind="M", round_idx=1)
    active = {s.key for s in g.store.active_skills()}
    assert "good" in active and "bad" not in active, f"invalid not rejected: {active}"
    print("ok test_invalid_candidate_rejected")


def test_no_update_during_P():
    """authored_skills() (the P frozen-export path) never mutates governance state."""
    inner = FakeInner(author_plan=[[("a", True)]])
    g = _mk(inner)
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(None, None)], kind="M", round_idx=1)
    import copy

    before = copy.deepcopy(g._state)
    for _ in range(3):
        _ = g.authored_skills()
    assert {k: (v.n_success, v.n_failure, v.status) for k, v in g._state.items()} == {
        k: (v.n_success, v.n_failure, v.status) for k, v in before.items()
    }, "P path mutated state"
    print("ok test_no_update_during_P")


def test_skillmd_only_clean_reload():
    """export_active_skills returns ONLY active skills; quarantined/retired excluded."""
    inner = FakeInner(author_plan=[[("keep", True), ("drop", True)]])
    g = _mk(inner)
    g.evolve_batch([harness.Trajectory([], "")], [harness.TaskOutcome(None, None)], kind="M", round_idx=1)
    g.store.set_skill_status("drop", Status.QUARANTINED)
    exported = {s.key for s in g.authored_skills()}
    assert exported == {"keep"}, f"quarantined leaked into frozen reload: {exported}"
    print("ok test_skillmd_only_clean_reload")


def test_governance_off_is_raw():
    """--governance none returns the exact same method object (raw == governance-off)."""
    from misevolve.governance.registry import build_governance

    inner = FakeInner()
    assert build_governance("none", inner) is inner
    assert build_governance(None, inner) is inner
    print("ok test_governance_off_is_raw")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} governance tests passed.")


if __name__ == "__main__":
    _run_all()
