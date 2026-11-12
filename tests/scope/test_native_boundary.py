from copy import deepcopy
from types import SimpleNamespace

import pytest

from misevolve.scope.native import AutoSkillCandidateCollector, _version


class SyntheticNativeMethod:
    """Test double for adapter boundaries; NOT a reimplementation of native AutoSkill."""

    def __init__(self, root):
        self._dir = root
        self._store_dir = root / "autoskill_store"
        self._store_dir.mkdir(parents=True)
        self.sdk = SimpleNamespace(store=object())
        self.skills = {}
        self._exchange = SimpleNamespace(snapshot_store=lambda: deepcopy(self.skills))
        self._last_evolution_events = []
        self.calls = []
        self.closed = False

    def evolve_batch(self, traces, outcomes, *, kind, round_idx):
        self.calls.append((traces, outcomes, kind, round_idx))
        for index, trace in enumerate(traces):
            before = deepcopy(self.skills)
            self.skills["skill"] = {"local_skill_key": "skill", "version": f"0.1.{index}",
                                    "skill_files": {"SKILL.md": f"revision {index}"},
                                    "native_metadata": {"id": "skill", "test": True}}
            event = {"success": trace != "FAIL", "before": before, "after": deepcopy(self.skills),
                     "task": {"instance_id": f"task-{index}"}}
            self._last_evolution_events.append(event)
            if trace == "FAIL":
                raise RuntimeError("synthetic native failure")

    def teardown(self):
        self.closed = True


def test_candidate_collection_calls_native_once_and_preserves_all_versions(system, tmp_path):
    store, base, *_ = system
    source = SyntheticNativeMethod(tmp_path / "source")
    clones = []

    def fork(method, destination):
        clone = SyntheticNativeMethod(destination)
        clone.skills = deepcopy(method.skills)
        clones.append(clone)
        return clone

    collector = AutoSkillCandidateCollector(store, fork=fork)
    traces = [object(), object(), object()]
    ids = collector.collect(base=base, methods={"A": source}, traces={"A": traces}, kind="M", round_idx=1)
    assert len(ids) == 1 and source.skills == {} and source.calls == []
    assert store.current() == base and clones[0].closed and not source.closed
    assert clones[0].calls == [(traces, [], "M", 1)]
    patch = store.get(ids[0])
    assert len(patch["history"]) == 3 and len(patch["evidence"]) == 3
    assert [store.get(oid)["native_version"] for oid in patch["history"]] == ["0.1.0", "0.1.1", "0.1.2"]
    assert store.get(patch["after"])["files"]["SKILL.md"] == "revision 2"


def test_failed_ingest_retains_evidence_without_changing_execution_store(system, tmp_path):
    store, base, *_ = system
    source = SyntheticNativeMethod(tmp_path / "source")
    collector = AutoSkillCandidateCollector(store, fork=lambda source, path: SyntheticNativeMethod(path))
    with pytest.raises(RuntimeError, match="synthetic native failure"):
        collector.collect(base=base, methods={"A": source}, traces={"A": ["FAIL"]}, kind="B", round_idx=1)
    assert source.skills == {} and store.current() == base
    attempts = list((store.root / "workspaces").glob("candidate-*/candidate_attempt.json"))
    assert len(attempts) == 1
    import json
    audit = store.get(json.loads(attempts[0].read_text())["evidence"])
    assert audit["events"][0]["success"] is False
    assert audit["events"][0]["after"]


def test_probe_never_calls_native_evolution(system, tmp_path):
    store, base, *_ = system
    source = SyntheticNativeMethod(tmp_path / "source")
    with pytest.raises(ValueError, match="Probe"):
        AutoSkillCandidateCollector(store).collect(base=base, methods={"A": source},
                                                   traces={"A": [object()]}, kind="P", round_idx=4)
    assert source.calls == []


def test_shared_library_keeps_logical_skill_identity_when_another_member_updates(system, tmp_path):
    store, _, *_ = system
    source = SyntheticNativeMethod(tmp_path / "member-b")
    bundle = {"local_skill_key": "skill", "version": "0.0.1",
              "skill_files": {"SKILL.md": "approved original"}, "native_metadata": {"id": "skill"}}
    source.skills = {"skill": bundle}
    version = store.put(_version("A", bundle))
    # A separate store genesis contains the same global view for all team members.
    from misevolve.scope.store import ScopeStore
    shared = ScopeStore(tmp_path / "shared")
    assert shared.put(_version("A", bundle)) == version
    base = shared.initialize({"A:skill": version})

    def fork(method, destination):
        clone = SyntheticNativeMethod(destination)
        clone.skills = deepcopy(method.skills)
        return clone

    collector = AutoSkillCandidateCollector(shared, fork=fork)
    ids = collector.collect(base=base, methods={"B": source}, traces={"B": [object()]}, kind="B", round_idx=1,
                            skill_keys={"B": {"skill": "A:skill"}})
    assert len(ids) == 1
    patch = shared.get(ids[0])
    assert patch["skill_id"] == "A:skill" and patch["before"] == version
    assert shared.get(patch["evidence"][0])["owner"] == "B"
    assert source.skills == {"skill": bundle}
