import copy
import json

import pytest

from misevolve.multiagent._io import append_jsonl, canonical_json, read_jsonl
from misevolve.multiagent.exchange import imported_files
from misevolve.multiagent.models import content_hash
from misevolve.multiagent.provenance import ProvenanceIntegrityError, ProvenanceTracker
from misevolve.multiagent.registry import GlobalSkillRegistry


def bundle(text="PRIVATE_SKILL_BODY", key="skill-1"):
    return {"local_skill_key": key, "version": "0.1.0", "origin": "locally_created",
            "skill_files": {"SKILL.md": f"---\nname: synthetic\n---\n{text}\n"}}


def reference(artifact):
    return {"local_skill_id": artifact.local_skill_key, "content_hash": artifact.content_hash,
            "origin": "locally_created", "imported_global_skill_id": None}


def task_event(run_dir, kind, *, worker="A", task="task-1", **fields):
    row = {"schema_version": 1, "event": kind, "event_id": f"{kind}:{task}",
           "worker_id": worker, "task_run_id": task, "pos": 1, "kind": "B", **fields}
    append_jsonl(run_dir / "workers" / worker / "task_events.jsonl", row)
    return row


def transfer(registry, artifact, **fields):
    row = {
        "event": "skill_transfer", "attempt_id": "attempt-1", "source_agent": artifact.owner_agent,
        "target_agent": "B", "source_local_id": artifact.local_skill_key, "target_local_id": "receiver-local",
        "global_skill_id": artifact.global_skill_id, "content_hash": artifact.content_hash,
        "success": True, "integrity_verified": True, "native_retrieval_verified": True, **fields,
    }
    registry.append_event(row)
    return row


def test_successful_transfer_is_not_task_retrieval_or_invocation(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    transfer(registry, artifact)
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    assert len(tracker.get_events("transferred_from", worker_id="B")) == 1
    assert tracker.get_events("retrieved_from") == tracker.get_events("invoked_from") == []
    lineage = tracker.get_lineage(artifact.global_skill_id)
    assert len(lineage["transferred_from"]) == 1
    assert lineage["native_revision"] == lineage["derived_from"] == []


@pytest.mark.parametrize("field", ["success", "integrity_verified", "native_retrieval_verified"])
def test_failed_or_partial_transfer_remains_an_attempt(tmp_path, field):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    transfer(registry, artifact, **{field: False})
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    assert tracker.get_events("transferred_from") == []
    attempts = tracker.get_events("transfer_attempt")
    assert len(attempts) == 1 and attempts[0]["evidence_status"] == "unconfirmed"


def test_unregistered_references_remain_unknown_and_later_resolution_is_append_only(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    pending = bundle()
    ref = {"local_skill_id": pending["local_skill_key"], "content_hash": content_hash(pending["skill_files"])}
    task_event(tmp_path, "task_retrieval", retrieval_status="completed", retrieved=[ref])
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    first_bytes = tracker.journal.read_bytes()
    assert tracker.get_task_context("task-1")["retrieved"][0]["global_skill_id"] is None
    assert tracker.get_task_context("task-1")["retrieved"][0]["resolution_status"] == "unknown"
    assert tracker.get_task_context("task-1")["execution"] == ["unknown"]
    assert tracker.sync() == []
    artifact = registry.register_skill("A", pending)
    assert tracker.sync()
    assert tracker.journal.read_bytes().startswith(first_bytes)
    assert tracker.get_task_context("task-1")["retrieved"][0]["global_skill_id"] == artifact.global_skill_id
    # Rebuild after late registration reproduces the same event set, not obsolete guesses.
    rebuilt = ProvenanceTracker(tmp_path, registry, output_dir=tmp_path / "rebuilt")
    rebuilt.sync()
    assert sorted(tracker.get_events(), key=lambda e: e["event_id"]) == sorted(
        rebuilt.get_events(), key=lambda e: e["event_id"],
    )


def test_import_claim_requires_successful_transfer_and_exact_adapted_hash(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    ref = {"local_skill_id": "receiver-local", "content_hash": "0" * 64,
           "origin": "imported", "imported_global_skill_id": artifact.global_skill_id}
    assert registry.resolve_reference("B", ref)["global_skill_id"] is None
    transfer(registry, artifact)
    assert registry.resolve_reference("B", ref)["global_skill_id"] is None
    ref["content_hash"] = content_hash(imported_files(artifact.skill_files, "receiver-local"))
    assert registry.resolve_reference("B", ref)["global_skill_id"] == artifact.global_skill_id


@pytest.mark.parametrize("manifest", [None, {}, {"status": "unknown", "skills": []},
                                      {"status": "success", "skills": [{"local_skill_id": "skill-1"}]}])
def test_missing_or_incomplete_manifest_does_not_establish_injection(tmp_path, manifest):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    task_event(tmp_path, "task_retrieval", retrieved=[reference(artifact)], retrieval_status="completed")
    task_event(tmp_path, "task_execution", execution_status="completed", injection_manifest=manifest,
               assistant_text="I invoked the synthetic skill.", tool_calls=[{"name": "exec", "input": "synthetic"}])
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    context = tracker.get_task_context("task-1")
    assert tracker.get_events("injected_to") == tracker.get_events("invoked_from") == []
    assert context["injection_status"] == ["unknown"] and context["invocation_status"] == "unknown"
    assert tracker.get_events("derived_from") == []


def test_successful_manifest_is_injection_only_and_uses_runtime_identity(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    task_event(tmp_path, "task_retrieval", retrieved=[reference(artifact)], retrieval_status="completed")
    task_event(tmp_path, "task_execution", execution_status="failed", error_type="RuntimeError", injection_manifest={
        "status": "success", "skills": [{"local_skill_id": artifact.local_skill_key,
        "runtime_skill_name": "synthetic", "runtime_directory_slug": "synthetic-2", "runtime_content_hash": "a" * 64}],
    })
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    context = tracker.get_task_context("task-1")
    assert context["execution"] == ["failed"] and context["injection_status"] == ["success"]
    assert context["injected"][0]["global_skill_id"] == artifact.global_skill_id
    assert context["injected"][0]["runtime_content_hash"] == "a" * 64
    assert context["invoked"] == []


def test_explicit_invocation_is_independent_evidence(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    task_event(tmp_path, "task_retrieval", retrieved=[reference(artifact)], retrieval_status="completed")
    task_event(tmp_path, "skill_invocation", skill=reference(artifact),
               evidence={"type": "explicit_invocation", "source_ref": "runtime:tool-call-7"})
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    assert tracker.get_task_context("task-1")["invoked"][0]["global_skill_id"] == artifact.global_skill_id
    assert tracker.get_events("derived_from") == []


def test_explicit_derivation_and_legacy_revision_are_not_conflated(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    parent = registry.register_skill("A", bundle())
    child = registry.register_skill("B", bundle("new"), provenance={
        "retrieved_parent_ids": [parent.global_skill_id],
        "parent_skill_ids": [parent.global_skill_id],
        "parent_evidence": {parent.global_skill_id: {"type": "explicit_derivation", "source_ref": "manual:evidence"}},
    })
    legacy = registry.register_skill("C", bundle("legacy"), provenance={
        "parent_skill_ids": [parent.global_skill_id],
        "parent_evidence": {parent.global_skill_id: {"type": "native_revision", "source_ref": "legacy:claim"}},
    })
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    assert len(tracker.get_events("derived_from")) == 1
    assert tracker.get_lineage(child.global_skill_id)["ancestors"] == [parent.global_skill_id]
    assert tracker.get_lineage(legacy.global_skill_id)["native_revision"] == []
    assert len(tracker.get_events("native_revision_claim")) == 1


@pytest.mark.parametrize("evidence,registered", [
    ({"type": "explicit_derivation", "source_ref": "evidence"}, False),
    ({"type": "explicit_derivation", "source_ref": ""}, True),
    ({"type": "retrieved_from", "source_ref": "evidence"}, True),
    ({"type": "explicit_derivation", "source_ref": ["not-a-reference-string"]}, True),
    ({"type": "explicit_derivation", "source_ref": "  \n"}, True),
])
def test_invalid_derivation_is_rejected(tmp_path, evidence, registered):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    parent = registry.register_skill("A", bundle()).global_skill_id if registered else "a" * 64
    with pytest.raises(ValueError):
        registry.register_skill("B", bundle("child"), provenance={
            "parent_skill_ids": [parent], "parent_evidence": {parent: evidence},
        })


def test_sync_restart_rebuild_and_content_free_schema(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    artifact = registry.register_skill("A", bundle())
    transfer(registry, artifact)
    task_event(tmp_path, "task_retrieval", retrieved=[reference(artifact)], retrieval_status="completed",
               prompt="PRIVATE_RAW_PROMPT")
    tracker = ProvenanceTracker(tmp_path, registry)
    assert tracker.sync()
    original = tracker.journal.read_bytes()
    assert b"PRIVATE_RAW_PROMPT" not in original and b"PRIVATE_SKILL_BODY" not in original
    assert tracker.sync() == [] and tracker.journal.read_bytes() == original
    resumed = ProvenanceTracker(tmp_path, registry)
    assert resumed.sync() == [] and resumed.get_events() == tracker.get_events()
    rebuilt = ProvenanceTracker(tmp_path, registry, output_dir=tmp_path / "rebuilt")
    rebuilt.sync()
    assert rebuilt.get_events() == resumed.get_events()
    required = {"schema_version", "event_id", "relation", "subject", "object", "evidence_source",
                "source_event_id", "evidence_status"}
    assert all(required <= set(event) for event in tracker.get_events())
    assert tracker.get_events(worker_id="B")
    assert all(event["worker_id"] == "B" for event in tracker.get_events(worker_id="B"))
    assert tracker.get_events(task_run_id="absent") == []
    with pytest.raises(KeyError):
        tracker.get_task_context("absent")


def test_source_rewrite_or_event_id_conflict_is_an_integrity_error(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    task_event(tmp_path, "task_retrieval", retrieved=[], retrieval_status="completed")
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    original = tracker.journal.read_bytes()
    path = tmp_path / "workers" / "A" / "task_events.jsonl"
    row = read_jsonl(path)[0]
    row["retrieval_status"] = "failed"
    path.write_text(canonical_json(row) + "\n", encoding="utf-8")
    with pytest.raises(ProvenanceIntegrityError, match="different content"):
        tracker.sync()
    assert tracker.journal.read_bytes() == original


def test_source_removal_and_incomplete_projection_are_reported(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    task_event(tmp_path, "task_retrieval", retrieved=[], retrieval_status="completed")
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    (tmp_path / "workers" / "A" / "task_events.jsonl").unlink()
    with pytest.raises(ProvenanceIntegrityError, match="append-only"):
        tracker.sync()
    with tracker.journal.open("a", encoding="utf-8") as handle:
        handle.write('{"partial":')
    with pytest.raises(ValueError, match="incomplete JSONL"):
        ProvenanceTracker(tmp_path, registry)


def test_normalized_event_content_conflict_is_not_silently_accepted(tmp_path):
    registry = GlobalSkillRegistry(tmp_path / "registry")
    task_event(tmp_path, "task_retrieval", retrieved=[], retrieval_status="completed")
    tracker = ProvenanceTracker(tmp_path, registry)
    tracker.sync()
    events = copy.deepcopy(tracker.get_events())
    events[-1]["evidence_status"] = "altered"
    tracker.journal.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    resumed = ProvenanceTracker(tmp_path, registry)
    with pytest.raises(ProvenanceIntegrityError, match="different content"):
        resumed.sync()
