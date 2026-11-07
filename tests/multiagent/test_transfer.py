import copy
from pathlib import Path

import pytest

from misevolve.multiagent.exchange import imported_files
from misevolve.multiagent.models import UnsupportedSkillError, content_hash
from misevolve.multiagent.orchestrator import MultiAgentOrchestrator
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.multiagent.transfer import SkillTransferManager


def registered(registry, worker, seed_skill):
    skill = seed_skill(worker)
    artifact = registry.register_skill(
        worker.worker_id, worker.method.export_skill_bundle(skill.id), ["synthetic-task"],
    )
    worker.remember_local_version(skill.id, artifact.global_skill_id)
    return artifact


def test_native_transfer_integrity_mapping_and_task_retrieval(tmp_path, workers, seed_skill, monkeypatch):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    manager = SkillTransferManager(registry, workers)
    target = workers["B"]
    native_import = target.method.sdk.import_agent_skill_dirs
    calls = []

    def check_layout(**kwargs):
        calls.append(kwargs)
        children = list(Path(kwargs["root_dir"]).iterdir())
        assert [p.name for p in children] == [artifact.global_skill_id]
        assert list(children[0].iterdir())[0].name == "SKILL.md"
        assert kwargs["overwrite"] is False
        assert kwargs["reassign_ids"] is True
        assert kwargs["include_files"] is True
        return native_import(**kwargs)

    monkeypatch.setattr(target.method.sdk, "import_agent_skill_dirs", check_layout)
    local_id = manager.transfer(artifact.global_skill_id, "A", "B")
    assert len(calls) == 1
    assert local_id != artifact.local_skill_key
    received = target.method.export_skill_bundle(local_id)
    assert received["skill_files"] == imported_files(artifact.skill_files, local_id)
    assert received["content_hash"] != artifact.content_hash
    assert received["origin"] == "imported"
    assert artifact.native_metadata["metadata"]["nested"] == {"value": 7}
    assert target.imported_global_to_local[artifact.global_skill_id] == local_id
    assert target.local_to_global[local_id] == artifact.global_skill_id
    assert target.method.sdk.get(local_id) is not None
    # Exercise the actual adapter's normal top-k retrieval, in addition to diagnostic search.
    assert local_id in {s.key for s in target.method._retrieve("synthetic arithmetic result")}
    traj = target.method.run_task("Check two synthetic integers.", kind="B", pos=4, out_dir=tmp_path / "task")
    assert target.method._retrieved_by_traj[id(traj)][0]["local_skill_key"] == local_id
    event = manager.get_transfer_history()[-1]
    assert event["success"] and event["native_retrieval_verified"] and event["integrity_verified"]
    assert event["relationship"] == "transferred_from"
    assert "derived_from" not in event


def test_transfer_idempotency_and_restart_reconciliation(tmp_path, runner_factory, seed_skill):
    pool = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory)
    workers = pool.setup_workers()
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    manager = SkillTransferManager(registry, workers)
    local_id = manager.transfer(artifact.global_skill_id, "A", "B")
    assert manager.transfer(artifact.global_skill_id, "A", "B") == local_id
    assert len(workers["B"].method.sdk.list(user_id="skillmisevo")) == 1
    pool.teardown()
    resumed = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory, resume=True)
    workers = resumed.setup_workers()
    manager = SkillTransferManager(GlobalSkillRegistry(tmp_path / "registry"), workers)
    assert workers["B"].imported_global_to_local[artifact.global_skill_id] == local_id
    assert manager.transfer(artifact.global_skill_id, "A", "B") == local_id
    assert len(workers["B"].method.sdk.list(user_id="skillmisevo")) == 1
    assert workers["C"].method.sdk.get(local_id) is None
    resumed.teardown()


@pytest.mark.parametrize("crash_point", ["native_return", "worker_checkpoint"])
def test_recovery_after_native_import_before_mapping_commit(
    tmp_path, runner_factory, seed_skill, monkeypatch, crash_point,
):
    pool = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory)
    workers = pool.setup_workers()
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    target = workers["B"]
    if crash_point == "native_return":
        original = target.method.sdk.import_agent_skill_dirs

        def interrupted(**kwargs):
            original(**kwargs)
            raise RuntimeError("synthetic interruption after native upsert")

        monkeypatch.setattr(target.method.sdk, "import_agent_skill_dirs", interrupted)
    else:
        def interrupted(*args):
            raise RuntimeError("synthetic interruption before worker checkpoint")

        monkeypatch.setattr(target, "remember_import", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        SkillTransferManager(registry, workers).transfer(artifact.global_skill_id, "A", "B")
    assert len(target.method.sdk.list(user_id="skillmisevo")) == 1
    pool.teardown()
    resumed = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory, resume=True)
    workers = resumed.setup_workers()
    local_id = SkillTransferManager(GlobalSkillRegistry(tmp_path / "registry"), workers).transfer(
        artifact.global_skill_id, "A", "B",
    )
    assert len(workers["B"].method.sdk.list(user_id="skillmisevo")) == 1
    assert workers["B"].local_to_global[local_id] == artifact.global_skill_id
    resumed.teardown()


def test_search_failure_is_visible_and_retry_does_not_duplicate(tmp_path, workers, seed_skill, monkeypatch):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    manager = SkillTransferManager(registry, workers)
    sdk = workers["B"].method.sdk
    original = sdk.search
    monkeypatch.setattr(sdk, "search", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="native search"):
        manager.transfer(artifact.global_skill_id, "A", "B")
    failed = manager.get_transfer_history()[-1]
    assert failed["success"] is False and failed["integrity_verified"] is True
    assert failed["native_retrieval_verified"] is False and failed["target_local_id"]
    monkeypatch.setattr(sdk, "search", original)
    local_id = manager.transfer(artifact.global_skill_id, "A", "B")
    assert len(sdk.list(user_id="skillmisevo")) == 1
    assert sdk.get(local_id) is not None


def test_native_resources_are_kept_and_recorded_on_markdown_transfer(tmp_path, workers, seed_skill):
    worker = workers["A"]
    skill = seed_skill(worker)
    files = worker.method.export_skill_bundle(skill.id)["skill_files"]
    extra = {**files, "references/extra.md": "synthetic resource"}
    with pytest.raises(UnsupportedSkillError):
        workers["B"].method.import_external_skill(extra, "a" * 64)
    assert workers["B"].method.sdk.list(user_id="skillmisevo") == []
    skill.files["references/extra.md"] = "synthetic"
    assert set(worker.method.export_skill_bundle(skill.id)["resource_files"]) == {"references/extra.md"}
    del skill.files["references/extra.md"]
    md = next((worker.episode_dir / "autoskill_store" / "Users").rglob("SKILL.md"))
    (md.parent / "extra.bin").write_bytes(b"\x00\x01")
    bundle = worker.method.export_skill_bundle(skill.id)
    assert set(bundle["resource_files"]) == {"extra.bin"} and bundle["skill_files"] == files
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle, block_id="a")
    manager = SkillTransferManager(registry, workers)
    first, = manager.transfer_block("A", "B", block_id="a")
    assert manager.transfer(artifact.global_skill_id, "A", "B") == first
    assert worker.method.sdk.get(skill.id) is not None and (md.parent / "extra.bin").read_bytes() == b"\x00\x01"
    assert "resource_files" not in workers["B"].method.export_skill_bundle(first)
    event = registry.get_transfer_history()[-1]
    assert registry.verified_transfer(event) and event["omitted_resources"] == bundle["resource_files"]


def test_tampering_is_rejected_without_overwriting_receiver(tmp_path, workers, seed_skill):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    manager = SkillTransferManager(registry, workers)
    local_id = manager.transfer(artifact.global_skill_id, "A", "B")
    sdk = workers["B"].method.sdk
    skill = sdk.get(local_id)
    skill.files["SKILL.md"] += "UNRECORDED CHANGE\n"
    with pytest.raises(ValueError, match="without a recorded local evolution"):
        manager.transfer(artifact.global_skill_id, "A", "B")
    assert "UNRECORDED CHANGE" in sdk.export_skill_md(local_id)
    assert len(sdk.list(user_id="skillmisevo")) == 1


def test_integrity_verifier_rejects_unapproved_native_import_changes(tmp_path, workers, seed_skill, monkeypatch):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    sdk = workers["B"].method.sdk
    original = sdk.import_agent_skill_dirs

    def corrupt(**kwargs):
        result = original(**kwargs)
        result[0].files["SKILL.md"] += "unexpected body change\n"
        return result

    monkeypatch.setattr(sdk, "import_agent_skill_dirs", corrupt)
    with pytest.raises(ValueError, match="beyond allowed"):
        SkillTransferManager(registry, workers).transfer(artifact.global_skill_id, "A", "B")
    assert len(sdk.list(user_id="skillmisevo")) == 1
    assert registry.get_transfer_history()[-1]["integrity_verified"] is False


def test_block_transfer_selects_final_version_per_local_key(tmp_path, workers, seed_skill):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    source = workers["A"]
    artifacts = []
    for revision in range(3):
        skill = seed_skill(source, instructions=f"Synthetic revision {revision}.")
        artifacts.append(registry.register_skill(
            "A", source.method.export_skill_bundle(skill.id), block_id="block-1",
        ))
    manager = SkillTransferManager(registry, workers)
    imported = manager.transfer_block("A", "B", block_id="block-1")
    assert len(registry.get_skills_by_owner("A")) == 3
    assert len(imported) == 1
    assert workers["B"].imported_global_to_local == {artifacts[-1].global_skill_id: imported[0]}
    assert manager.transfer_block("C", "D", block_id="empty") == []
    assert manager.get_transfer_history()[-1]["event"] == "empty_transfer"


def test_global_id_cannot_be_rebound_to_different_content(tmp_path, workers, seed_skill):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registered(registry, workers["A"], seed_skill)
    manager = SkillTransferManager(registry, workers)
    manager.transfer(artifact.global_skill_id, "A", "B")
    files = copy.deepcopy(artifact.skill_files)
    files["SKILL.md"] += "different content\n"
    assert content_hash(files) != artifact.content_hash
    with pytest.raises(ValueError, match="different import contents"):
        workers["B"].method.import_external_skill(
            files, artifact.global_skill_id, source_agent="A", source_local_id=artifact.local_skill_key,
        )
