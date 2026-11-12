"""Pinned real AutoSkill SDK; only LLM completions are mocked by existing fixtures."""

from misevolve.scope.native import AutoSkillCandidateCollector, _version, fork_autoskill
from misevolve.scope.store import ScopeStore


def test_scope_real_native_evolution_is_candidate_only(tmp_path, workers, synthetic_llm):
    store = ScopeStore(tmp_path / "scope")
    base = store.initialize()
    source = workers["A"].method
    traces = [source.run_task("Check synthetic arithmetic.", kind="B", pos=i, out_dir=tmp_path / str(i))
              for i in range(1, 4)]
    calls = []

    def configure(owner, clone):
        assert owner == "A" and clone.sdk is not source.sdk
        calls.append(synthetic_llm(clone))

    collector = AutoSkillCandidateCollector(store, configure=configure)
    ids = collector.collect(base=base, methods={"A": source}, traces={"A": traces}, kind="B", round_idx=1)
    assert source._exchange.snapshot_store() == {}
    assert source._last_evolution_events == [] and not source._exchange.evolution_path.exists()
    assert store.current() == base and len(ids) == 1
    assert calls == [{"extract_calls": 3, "merge_calls": 2}]
    patch = store.get(ids[0])
    assert [store.get(oid)["native_version"] for oid in patch["history"]] == ["0.1.0", "0.1.1", "0.1.2"]
    assert len(patch["evidence"]) == 3


def test_scope_fork_keeps_native_metadata_and_existing_approved_version(
    tmp_path, workers, seed_skill, synthetic_llm,
):
    source = workers["A"].method
    skill = seed_skill(workers["A"])
    before = source.export_skill_bundle(skill.id)
    store = ScopeStore(tmp_path / "scope")
    version = store.put(_version("A", before))
    base = store.initialize({f"A:{skill.id}": version})
    clone = fork_autoskill(source, tmp_path / "fork")
    try:
        assert clone._exchange.snapshot_store() == source._exchange.snapshot_store()
        assert clone.sdk is not source.sdk and clone.sdk.store is not source.sdk.store
    finally:
        clone.teardown()
    trace = source.run_task("Check synthetic arithmetic.", kind="B", pos=4, out_dir=tmp_path / "task")
    collector = AutoSkillCandidateCollector(store, configure=lambda owner, method: synthetic_llm(method))
    ids = collector.collect(base=base, methods={"A": source}, traces={"A": [trace]}, kind="B", round_idx=1)
    assert source.export_skill_bundle(skill.id) == before
    assert store.current() == base and len(ids) == 1
    assert store.get(ids[0])["before"] == version
    assert store.get(store.get(ids[0])["after"])["native_version"] != before["version"]
