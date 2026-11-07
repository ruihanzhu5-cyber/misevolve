import json

import pytest

from misevolve.multiagent.orchestrator import MultiAgentOrchestrator
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.multiagent.transfer import SkillTransferManager


def test_missing_native_skill_is_not_silently_accepted_on_resume(tmp_path, runner_factory, seed_skill):
    pool = MultiAgentOrchestrator(tmp_path, runner_factory=runner_factory)
    workers = pool.setup_workers()
    skill = seed_skill(workers["A"])
    workers["A"].remember_local_version(skill.id, "a" * 64)
    workers["A"].method.sdk.delete(skill.id)
    pool.teardown()
    resumed = MultiAgentOrchestrator(tmp_path, runner_factory=runner_factory, resume=True)
    with pytest.raises(ValueError, match="missing native skill"):
        resumed.setup_workers()
    assert not resumed.workers


def test_pending_imports_with_identical_text_recover_distinct_native_ids(
    tmp_path, workers, seed_skill, monkeypatch,
):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    skill = seed_skill(workers["A"])
    bundle = workers["A"].method.export_skill_bundle(skill.id)
    first = registry.register_skill("A", bundle)
    second = registry.register_skill("C", bundle)
    method = workers["B"].method
    native_import = method.sdk.import_agent_skill_dirs

    def interrupted(**kwargs):
        native_import(**kwargs)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(method.sdk, "import_agent_skill_dirs", interrupted)
    manager = SkillTransferManager(registry, workers)
    for artifact in (first, second):
        with pytest.raises(RuntimeError, match="interrupted"):
            manager.transfer(artifact.global_skill_id, artifact.owner_agent, "B")
    assert len(method.sdk.list(user_id="skillmisevo")) == 2
    monkeypatch.setattr(method.sdk, "import_agent_skill_dirs", native_import)
    ids = [manager.transfer(a.global_skill_id, a.owner_agent, "B") for a in (first, second)]
    assert len(set(ids)) == 2
    assert len(method.sdk.list(user_id="skillmisevo")) == 2


def test_imported_local_evolution_survives_sdk_restart(
    tmp_path, runner_factory, seed_skill, synthetic_llm,
):
    pool = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory)
    workers = pool.setup_workers()
    registry = GlobalSkillRegistry(tmp_path / "registry")
    skill = seed_skill(workers["A"])
    artifact = registry.register_skill("A", workers["A"].method.export_skill_bundle(skill.id))
    local_id = SkillTransferManager(registry, workers).transfer(artifact.global_skill_id, "A", "B")
    method = workers["B"].method
    synthetic_llm(method)
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=4, out_dir=tmp_path / "task")
    method.evolve_batch([trace], [], kind="B", round_idx=1)
    changed = registry.register_evolution_events("B", method._last_evolution_events, block_id="block")[0]
    workers["B"].remember_local_version(local_id, changed.global_skill_id)
    evolved_files = method.export_skill_bundle(local_id)["skill_files"]
    # Simulate a lost origin checkpoint; the flushed ingest journal must restore it.
    data = json.loads(method._exchange.state_path.read_text(encoding="utf-8"))
    data["origins"] = {}
    method._exchange.state_path.write_text(json.dumps(data), encoding="utf-8")
    pool.teardown()
    resumed = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory, resume=True)
    workers = resumed.setup_workers()
    target = workers["B"]
    received_id = SkillTransferManager(GlobalSkillRegistry(tmp_path / "registry"), workers).transfer(
        artifact.global_skill_id, "A", "B",
    )
    assert received_id == local_id
    assert target.method.export_skill_bundle(local_id)["skill_files"] == evolved_files
    assert target.method.export_skill_bundle(local_id)["origin"] == "locally_updated"
    assert target.local_to_global[local_id] == changed.global_skill_id
    resumed.teardown()
