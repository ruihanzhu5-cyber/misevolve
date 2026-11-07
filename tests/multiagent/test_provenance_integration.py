import hashlib
import subprocess

import pytest

from misevolve import harness
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.multiagent._io import append_jsonl, read_jsonl
from misevolve.multiagent.provenance import ProvenanceIntegrityError, ProvenanceTracker
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.multiagent.transfer import SkillTransferManager


def registry_for(workers):
    return GlobalSkillRegistry(workers["A"].episode_dir.parents[1] / "registry")


def mock_native_runner(monkeypatch, runner, *, fail_copy_number=None):
    """Exercise real run/_write_skills, replacing only Docker/HTTP side effects."""
    copies = []

    def docker(args, **kwargs):
        if args[0] == "cp":
            copies.append(args)
            if len(copies) == fail_copy_number:
                raise subprocess.CalledProcessError(1, ["docker", *args])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner, "_docker", docker)
    monkeypatch.setattr(runner, "_rm_container", lambda: None)
    monkeypatch.setattr(runner, "_boot", lambda: setattr(runner, "container", "synthetic-container"))
    monkeypatch.setattr(runner, "_install_defense_skill", lambda out_dir: None)
    monkeypatch.setattr(runner, "_http", lambda method, path, *args, **kwargs: (
        (200, {"response": "Synthetic result."}) if method == "POST" else (200, {"history": []})
    ))
    monkeypatch.setattr(runner, "run", lambda prompt, skills, *, out_dir: OpenClawRunner.run(
        runner, prompt, skills, out_dir=out_dir,
    ))
    return copies


def test_retrieval_is_durable_before_execution_and_raised_failure_preserves_it(
    tmp_path, workers, seed_skill, monkeypatch,
):
    worker = workers["A"]
    registry = registry_for(workers)
    skill = seed_skill(worker)
    artifact = registry.register_skill("A", worker.method.export_skill_bundle(skill.id))
    task_ids = []

    def fail(prompt, skills, *, out_dir):
        events = read_jsonl(worker.method._exchange.task_path)
        assert events[-1]["event"] == "task_retrieval"
        assert events[-1]["retrieved"][0]["local_skill_id"] == skill.id
        assert prompt not in worker.method._exchange.task_path.read_text(encoding="utf-8")
        task_ids.append(events[-1]["task_run_id"])
        raise RuntimeError("PRIVATE_ERROR_WITH_PROMPT")

    monkeypatch.setattr(worker.runner, "run", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="PRIVATE_ERROR"):
            worker.method.run_task("PRIVATE_RAW_TASK_PROMPT", kind="B", pos=1, out_dir=tmp_path / "task")
    assert len(set(task_ids)) == 2
    assert "PRIVATE_ERROR" not in worker.method._exchange.task_path.read_text(encoding="utf-8")
    tracker = ProvenanceTracker(worker.episode_dir.parents[1], registry)
    tracker.sync()
    for task_id in task_ids:
        context = tracker.get_task_context(task_id)
        assert context["retrieved"][0]["global_skill_id"] == artifact.global_skill_id
        assert context["execution"] == ["failed"]
        assert context["injection_status"] == ["unknown"] and context["invoked"] == []


def test_real_runner_manifest_tracks_name_collisions_and_exact_copied_bytes(
    tmp_path, workers, seed_skill, monkeypatch,
):
    worker = workers["A"]
    registry = registry_for(workers)
    for key in ("first-local", "second-local"):
        skill = seed_skill(worker, skill_id=key)
        registry.register_skill("A", worker.method.export_skill_bundle(skill.id))
    copies = mock_native_runner(monkeypatch, worker.runner)
    trajectory = worker.method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    manifest = trajectory.raw["_skill_injection"]
    assert manifest["status"] == "success" and len(copies) == 2
    assert {item["runtime_directory_slug"] for item in manifest["skills"]} == {
        "synthetic-arithmetic", "synthetic-arithmetic-2",
    }
    for item in manifest["skills"]:
        path = tmp_path / "task" / "_skills_stage" / item["runtime_directory_slug"] / "SKILL.md"
        assert item["runtime_content_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
    task_id = worker.method._task_by_traj[id(trajectory)]["task_run_id"]
    tracker = ProvenanceTracker(worker.episode_dir.parents[1], registry)
    tracker.sync()
    context = tracker.get_task_context(task_id)
    assert len(context["injected"]) == 2 and all(ref["global_skill_id"] for ref in context["injected"])
    assert context["invoked"] == [] and context["injection_status"] == ["success"]


def test_partial_copy_failure_is_not_a_successful_injection_manifest(
    tmp_path, workers, seed_skill, monkeypatch,
):
    worker = workers["A"]
    for key in ("first-local", "second-local"):
        seed_skill(worker, skill_id=key)
    mock_native_runner(monkeypatch, worker.runner, fail_copy_number=2)
    with pytest.raises(subprocess.CalledProcessError):
        worker.method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    rows = read_jsonl(worker.method._exchange.task_path)
    assert rows[-1]["execution_status"] == "failed" and rows[-1]["injection_manifest"] is None
    tracker = ProvenanceTracker(worker.episode_dir.parents[1], registry_for(workers))
    tracker.sync()
    assert tracker.get_events("injected_to") == []
    assert tracker.get_task_context(rows[0]["task_run_id"])["injection_status"] == ["unknown"]


def test_a_create_b_import_retrieve_native_revision_c_import(
    tmp_path, workers, seed_skill, synthetic_llm, monkeypatch,
):
    registry = registry_for(workers)
    skill = seed_skill(workers["A"], instructions="PRIVATE_SKILL_BODY")
    s1 = registry.register_skill("A", workers["A"].method.export_skill_bundle(skill.id))
    manager = SkillTransferManager(registry, workers)
    local_b = manager.transfer(s1.global_skill_id, "A", "B")
    method = workers["B"].method
    synthetic_llm(method)
    mock_native_runner(monkeypatch, workers["B"].runner)
    trace = method.run_task("PRIVATE_RAW_TASK_PROMPT", kind="B", pos=4, out_dir=tmp_path / "task")
    task_id = method._task_by_traj[id(trace)]["task_run_id"]
    method.evolve_batch([trace], [harness.TaskOutcome(False, True)], kind="B", round_idx=1)
    event = method._last_evolution_events[0]
    assert event["task"]["task_run_id"] == task_id
    assert event["returned_skills"][0]["metadata"]["task_run_id"] == task_id
    s2 = registry.register_evolution_events("B", [event], block_id="block-b")[0]
    assert s2.retrieved_parent_ids == [s1.global_skill_id]
    assert s2.invoked_parent_ids == [] and s2.parent_skill_ids == []
    assert s2.native_revision_parent_id == s1.global_skill_id
    assert s2.parent_evidence[s1.global_skill_id] == {"type": "native_revision", "source_ref": event["event_id"]}
    workers["B"].remember_local_version(local_b, s2.global_skill_id)
    local_c = manager.transfer(s2.global_skill_id, "B", "C")
    assert local_c != local_b
    tracker = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry)
    tracker.sync()
    context = tracker.get_task_context(task_id)
    assert [r["global_skill_id"] for r in context["retrieved"]] == [s1.global_skill_id]
    assert [r["global_skill_id"] for r in context["injected"]] == [s1.global_skill_id]
    assert context["invoked"] == []
    lineage = tracker.get_lineage(s2.global_skill_id)
    assert lineage["ancestors"] == [s1.global_skill_id]
    assert len(lineage["native_revision"]) == 1 and lineage["derived_from"] == []
    assert len(lineage["transferred_from"]) == 2
    assert tracker.get_events("derived_from") == tracker.get_events("invoked_from") == []
    original = tracker.journal.read_bytes()
    assert b"PRIVATE_RAW_TASK_PROMPT" not in original and b"PRIVATE_SKILL_BODY" not in original
    assert b"skill_files" not in original and b"instructions" not in original
    assert tracker.sync() == []
    restarted = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry)
    assert restarted.sync() == [] and restarted.get_lineage(s2.global_skill_id) == lineage


def test_new_skill_after_retrieval_has_context_but_no_revision_or_derivation(
    tmp_path, workers, seed_skill, synthetic_llm, monkeypatch,
):
    registry = registry_for(workers)
    method = workers["A"].method
    skill = seed_skill(workers["A"])
    s1 = registry.register_skill("A", method.export_skill_bundle(skill.id))
    synthetic_llm(method)
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    # Add a distinct synthetic capability through the real native ingest/store path.
    monkeypatch.setattr(method.sdk.extractor._llm, "complete", lambda **kw: (
        '{"skills":[{"name":"synthetic-colors","description":"Classify synthetic color tokens.",'
        '"prompt":"Classify red and blue synthetic tokens.","triggers":["synthetic colors"],"tags":["colors"]}]}'
    ))
    monkeypatch.setattr(method.sdk.maintainer._llm, "complete", lambda **kw: (
        '{"action":"add","same_capability":false,"confidence":0.0}'
    ))
    method.evolve_batch([trace], [], kind="B", round_idx=1)
    event = method._last_evolution_events[0]
    new = registry.register_evolution_events("A", [event], block_id="new")
    assert new and new[-1].local_skill_key != s1.local_skill_key
    assert new[-1].retrieved_parent_ids == [s1.global_skill_id]
    assert new[-1].native_revision_parent_id is None and new[-1].parent_skill_ids == []
    tracker = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry)
    tracker.sync()
    assert tracker.get_lineage(new[-1].global_skill_id)["ancestors"] == []
    assert tracker.get_events("derived_from") == []


def test_unknown_predecessor_is_preserved_without_a_global_id(tmp_path, workers, seed_skill, synthetic_llm):
    registry = registry_for(workers)
    method = workers["A"].method
    seed_skill(workers["A"])  # Deliberately unregistered predecessor.
    synthetic_llm(method)
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    method.evolve_batch([trace], [], kind="B", round_idx=1)
    artifact = registry.register_evolution_events("A", method._last_evolution_events, block_id="one")[0]
    assert artifact.native_revision_parent_id is None
    assert artifact.unresolved_predecessor["local_skill_id"] == "synthetic-source"
    tracker = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry)
    tracker.sync()
    lineage = tracker.get_lineage(artifact.global_skill_id)
    assert lineage["native_revision"] == []
    assert lineage["unresolved_predecessors"]
    assert all(e["object"]["global_skill_id"] is None for e in lineage["unresolved_predecessors"])


def test_generic_tools_do_not_invoke_but_explicit_instrumentation_can(
    tmp_path, workers, seed_skill, synthetic_llm, monkeypatch,
):
    registry = registry_for(workers)
    method = workers["A"].method
    skill = seed_skill(workers["A"])
    source = registry.register_skill("A", method.export_skill_bundle(skill.id))
    synthetic_llm(method)
    monkeypatch.setattr(workers["A"].runner, "run", lambda *a, **kw: harness.Trajectory(
        [{"name": "exec", "input": {"command": "synthetic-arithmetic"}}],
        final_text="I invoked synthetic-arithmetic.",
    ))
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    task_id = method._task_by_traj[id(trace)]["task_run_id"]
    tracker = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry)
    tracker.sync()
    assert tracker.get_events("invoked_from") == []
    method._exchange.record_invocation_evidence(task_id, skill.id, source_ref="runtime:explicit-call-1")
    method.evolve_batch([trace], [], kind="B", round_idx=1)
    artifact = registry.register_evolution_events("A", method._last_evolution_events, block_id="one")[0]
    assert artifact.invoked_parent_ids == [source.global_skill_id]
    tracker.sync()
    assert len(tracker.get_events("invoked_from")) == 1
    assert tracker.get_events("derived_from") == []


def test_evolution_reference_mismatch_is_rejected(tmp_path, workers, seed_skill, synthetic_llm):
    method = workers["A"].method
    seed_skill(workers["A"])
    synthetic_llm(method)
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    method.evolve_batch([trace], [], kind="B", round_idx=1)
    event = method._last_evolution_events[0]
    event["before"]["synthetic-source"]["local_skill_key"] = "different-local-id"
    with pytest.raises(ValueError, match="local ID mismatch"):
        registry_for(workers).register_evolution_events("A", [event], block_id="one")


def test_invalid_invocation_source_does_not_create_an_edge(tmp_path, workers, seed_skill):
    method = workers["A"].method
    skill = seed_skill(workers["A"])
    trace = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    task_id = method._task_by_traj[id(trace)]["task_run_id"]
    with pytest.raises(ValueError):
        method._exchange.record_invocation_evidence(task_id, skill.id, source_ref="")
    append_jsonl(method._exchange.task_path, {
        "event": "skill_invocation", "event_id": "bad-explicit", "worker_id": "A", "task_run_id": task_id,
        "skill": {"local_skill_id": skill.id}, "evidence": {"type": "tool_name_match", "source_ref": "tool:1"},
    })
    tracker = ProvenanceTracker(workers["A"].episode_dir.parents[1], registry_for(workers))
    with pytest.raises(ProvenanceIntegrityError, match="independent explicit evidence"):
        tracker.sync()
    assert not tracker.journal.exists()


def test_single_agent_native_execution_only_adds_manifest_metadata(
    tmp_path, runner_factory, method_factory, monkeypatch,
):
    method = method_factory("autoskill")
    runner = runner_factory("openclaw", None)
    runner.isolate(tmp_path)
    method.setup(tmp_path, runner)
    method.sdk.upsert(user_id=method._UID, name="synthetic", description="Synthetic arithmetic.",
                      instructions="Check synthetic integers.")
    copies = mock_native_runner(monkeypatch, runner)
    trajectory = method.run_task("Synthetic arithmetic.", kind="B", pos=1, out_dir=tmp_path / "task")
    assert method._exchange is None
    assert len(copies) == 1 and trajectory.final_text == "Synthetic result." and trajectory.error is None
    assert trajectory.raw["_skill_injection"]["status"] == "success"
    assert not (tmp_path / "task_events.jsonl").exists()
    assert not (tmp_path / "evolution_events.jsonl").exists()
    method.teardown()
