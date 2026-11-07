import json

import pytest

from misevolve import harness
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.multiagent.transfer import SkillTransferManager


def test_each_native_ingest_version_is_snapshotted_and_only_final_transferred(
    tmp_path, workers, synthetic_llm,
):
    method = workers["A"].method
    calls = synthetic_llm(method)
    traces = [method.run_task("Check synthetic arithmetic.", kind="B", pos=p, out_dir=tmp_path / str(p))
              for p in range(1, 4)]
    method.evolve_batch(traces, [harness.TaskOutcome(False, True)] * 3, kind="B", round_idx=1)
    events = method._last_evolution_events
    assert len(events) == 3 and calls == {"extract_calls": 3, "merge_calls": 2}
    assert all(event["success"] and len(event["returned_skills"]) == 1 for event in events)
    local_id = events[0]["updated_local_skill_ids"][0]
    assert events[0]["before"] == {}
    assert [e["after"][local_id]["version"] for e in events] == ["0.1.0", "0.1.1", "0.1.2"]
    assert events[0]["after"][local_id]["origin"] == "locally_created"
    assert events[1]["after"][local_id]["origin"] == "locally_updated"
    assert events[1]["before"][local_id]["skill_files"] == events[0]["after"][local_id]["skill_files"]
    assert "revision 1" in events[0]["after"][local_id]["skill_files"]["SKILL.md"]
    assert "revision 3" not in events[0]["after"][local_id]["skill_files"]["SKILL.md"]
    journal = [json.loads(line) for line in method._exchange.evolution_path.read_text(encoding="utf-8").splitlines()]
    assert len(journal) == 3 and [e["task"]["pos"] for e in journal] == [1, 2, 3]
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifacts = registry.register_evolution_events("A", events, block_id="one")
    assert len(artifacts) == 3
    assert all(a.parent_skill_ids == [] for a in artifacts)
    assert registry.select_transfer_versions("A", "one") == [artifacts[-1]]
    local_ids = SkillTransferManager(registry, workers).transfer_block("A", "B", block_id="one")
    assert len(local_ids) == 1
    assert "revision 3" in workers["B"].method.sdk.export_skill_md(local_ids[0])


def test_imported_local_update_keeps_origin_and_retry_preserves_evolution(
    tmp_path, workers, seed_skill, synthetic_llm,
):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    skill = seed_skill(workers["A"])
    source = registry.register_skill("A", workers["A"].method.export_skill_bundle(skill.id))
    manager = SkillTransferManager(registry, workers)
    local_id = manager.transfer(source.global_skill_id, "A", "B")
    method = workers["B"].method
    synthetic_llm(method)
    trace = method.run_task("Check synthetic arithmetic.", kind="B", pos=4, out_dir=tmp_path / "task")
    retrieved = method._retrieved_by_traj[id(trace)][0]
    assert retrieved["origin"] == "imported"
    method.evolve_batch([trace], [harness.TaskOutcome(False, True)], kind="B", round_idx=1)
    event = method._last_evolution_events[0]
    changed = event["after"][local_id]
    assert changed["origin"] == "locally_updated"
    assert changed["imported_from_global_id"] == source.global_skill_id
    assert event["retrieved"][0]["content_hash"] == retrieved["content_hash"]
    assert event["retrieved"][0]["content_hash"] != changed["content_hash"]
    evolved = registry.register_evolution_events("B", [event], block_id="two")[0]
    assert evolved.parent_skill_ids == []  # Retrieval alone supplies no derivation edge.
    workers["B"].remember_local_version(local_id, evolved.global_skill_id)
    before_retry = method.sdk.export_skill_md(local_id)
    assert manager.transfer(source.global_skill_id, "A", "B") == local_id
    assert method.sdk.export_skill_md(local_id) == before_retry
    assert workers["B"].local_to_global[local_id] == evolved.global_skill_id
    assert manager.transfer_block("B", "C", block_id="two")


def test_failed_ingest_retains_error_and_snapshots(tmp_path, workers, monkeypatch):
    method = workers["A"].method
    trace = method.run_task("Synthetic input.", kind="B", pos=1, out_dir=tmp_path / "task")

    def fail(**kwargs):
        raise RuntimeError("synthetic ingest failure")

    monkeypatch.setattr(method.sdk, "ingest", fail)
    with pytest.raises(RuntimeError, match="synthetic ingest failure"):
        method.evolve_batch([trace], [harness.TaskOutcome(False, True)], kind="B", round_idx=1)
    event = method._last_evolution_events[0]
    assert event["before"] == event["after"] == {}
    assert event["success"] is False
    assert "synthetic ingest failure" in event["error"]


def test_single_agent_tracking_remains_disabled_by_default(tmp_path, runner_factory, method_factory, monkeypatch):
    method = method_factory("autoskill")
    runner = runner_factory("openclaw", None)
    runner.isolate(tmp_path)
    method.setup(tmp_path, runner)
    assert method._exchange is None

    def fail(**kwargs):
        raise RuntimeError("original adapter handles ingest failure")

    monkeypatch.setattr(method.sdk, "ingest", fail)
    trace = method.run_task("Synthetic input.", kind="B", pos=1, out_dir=tmp_path / "task")
    method.evolve_batch([trace], [harness.TaskOutcome(False, True)], kind="B", round_idx=1)
    assert not (tmp_path / "evolution_events.jsonl").exists()
    assert not hasattr(method, "_retrieved_by_traj")
    method.teardown()
