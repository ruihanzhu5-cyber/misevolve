"""Offline regressions for failures observed in the archived Chain pilot."""

import json

import pytest
from test_team_phase3 import ScriptedRunner, action, make_router
from test_team_safety import assistant
from test_team_safety import openclaw as openclaw_fixture

from misevolve.multiagent._io import read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.team import ChainRouter, SkillTransferService, TeamExecutor, TeamMessage, TeamWorkerPool
from misevolve.team.checkpoint import load, save
from misevolve.team.handoff import MAX_TASK_FILES, deliver_artifact
from misevolve.team.openclaw import _runtime_skill_map, _skill_reads

openclaw = openclaw_fixture


def test_chain_rejects_handshake_but_preserves_forward_edges(tmp_path):
    router = make_router(tmp_path, ChainRouter)
    for kind in ("SKILL_OFFER", "SKILL_ACCEPT", "SKILL_REJECT"):
        assert not router.can_send("A", "B", kind)
        assert not router.can_send("B", "A", kind)
    assert router.can_send("A", "B", "SKILL_PAYLOAD")
    assert router.can_send("B", "C", "SKILL_FORWARD")
    assert not router.can_send("C", "B", "RESULT")


def test_file_limit_returns_failure_receipt_without_touching_receiver(tmp_path):
    router = make_router(tmp_path, ChainRouter)
    receipt = {}
    message = TeamMessage("episode", "task", "B", "C", "ARTIFACT_HANDOFF", {"relative_path": "file.bin"})
    usage = {"files": MAX_TASK_FILES, "bytes": 1}
    assert deliver_artifact(router, message, {}, usage, receipt=receipt) is None
    assert receipt == {"status": "blocked", "reason": "task_artifact_count_limit"}
    assert usage == {"files": MAX_TASK_FILES, "bytes": 1}


def test_judge_response_is_covered_by_checkpoint_integrity(tmp_path):
    manifest = {"tasks": [{"pos": i, "instance_id": f"task-{i}"} for i in range(1, 4)]}
    rows = [{"pos": i, "orig_id": f"task-{i}", "task_run_id": f"run-{i}"} for i in range(1, 4)]
    (tmp_path / "team_manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "all_rows.json").write_text(json.dumps(rows))
    evidence = tmp_path / "judge_responses" / "response.json"
    evidence.parent.mkdir()
    evidence.write_text('{"response":"synthetic"}')
    save(tmp_path, 3, {})
    assert load(tmp_path, manifest)[0] == 3
    evidence.write_text('{"response":"changed"}')
    with pytest.raises(ValueError, match="evidence changed"):
        load(tmp_path, manifest)


@pytest.mark.parametrize("repair", [True, False])
def test_sender_gets_one_correction_turn_without_replaying_successful_actions(tmp_path, repair):
    class DeliveryRunner(ScriptedRunner):
        def continue_task_session(self, prompt, inject, *, out_dir):
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            visible = json.loads(prompt.split("Your visible input: ", 1)[1].split("\nLocal Skill IDs", 1)[0])
            if self.active[2] == "B":
                # The first RESULT is already delivered. Only retry the failed action.
                if "delivery_receipts" not in visible:
                    payload = json.loads(trace.final_text)
                    payload["actions"].append(action("C", "ARTIFACT_HANDOFF", relative_path="../bad"))
                else:
                    assert any(r["status"] == "delivered" for r in visible["delivery_receipts"])
                    assert any(r["reason"] == "relative_artifact_path_required"
                               for r in visible["delivery_receipts"])
                    payload = {"actions": [] if repair else [action(
                        "C", "ARTIFACT_HANDOFF", relative_path="../bad")], "final_answer": None}
                trace.final_text = json.dumps(payload)
            return trace

    pool = TeamWorkerPool(tmp_path, runner_factory=lambda *_: DeliveryRunner("chain"))
    workers = pool.setup_workers()
    try:
        router = make_router(tmp_path, ChainRouter)
        executor = TeamExecutor(workers, router, SkillTransferService(GlobalSkillRegistry(tmp_path / "registry"),
                                                                      workers, router))
        task = {"kind": "B", "pos": 4, "instance_id": "task", "prompt": "Synthetic arithmetic"}
        result = executor.run(task, task_run_id="run", out_dir=tmp_path / "task")
        assert result.execution_status == ("completed" if repair else "not_submitted")
        if not repair:
            assert result.failure["stop_reason"] == "delivery_recovery_exhausted"
        events = read_jsonl(router.log.path)
        assert sum(e["event_type"] == "MESSAGE_DELIVERED" and e.get("message_type") == "RESULT"
                   for e in events) == 1
        assert len(workers["B"].runner.received) == 2
        assert not any(e["event_type"] == "MESSAGE_DELIVERED" and e["sender_id"] == "C" for e in events)
    finally:
        pool.teardown()


def test_runtime_mapping_is_added_after_install_and_reads_are_not_adoption(openclaw, tmp_path, monkeypatch):
    manifest = {"status": "success", "skills": [{"local_skill_id": "uuid-1", "runtime_skill_name": "check",
                "runtime_directory_slug": "check-2", "runtime_content_hash": "a" * 64}]}
    mapping = _runtime_skill_map(manifest)
    path = mapping[0]["runtime_path"]
    records = [
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "read-1", "name": "read", "arguments": {"path": path}},
        ]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "read-1",
                                         "isError": False, "content": "Synthetic Skill text"}}, assistant("stop"),
    ]
    bodies = []

    def http(method, url, body=None, **kwargs):
        if method == "POST":
            bodies.append(body)
            return 200, {}
        return 200, {"history": records}

    monkeypatch.setattr(openclaw, "_write_skills", lambda *args: manifest)
    monkeypatch.setattr(openclaw, "_http", http)
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert path in bodies[0]["prompt"] and "uuid-1" in bodies[0]["prompt"]
    read, = trace.raw["_skill_reads"]
    assert read["local_skill_id"] == "uuid-1" and read["adoption_status"] == "unknown"
    assert "_skill_invocations" not in trace.raw
    assert trace.raw["_skill_runtime_map"] == mapping


@pytest.mark.parametrize("case", ["error", "unpaired", "duplicate", "different_path", "exec", "claim"])
def test_read_evidence_requires_matching_successful_tool_result(case):
    mapping = [{"runtime_path": "/root/.openclaw/skills/check/SKILL.md", "local_skill_id": "one"}]
    call = {"name": "read", "id": "r", "input": {"path": mapping[0]["runtime_path"]}}
    result = {"role": "toolResult", "toolCallId": "r", "isError": False, "content": "synthetic"}
    messages = [result]
    if case == "error":
        result["isError"] = True
    elif case == "unpaired":
        result["toolCallId"] = "other"
    elif case == "duplicate":
        messages.append(dict(result))
    elif case == "different_path":
        call["input"]["path"] = "/tmp/SKILL.md"
    elif case == "exec":
        call["name"] = "exec"
    else:
        result["role"] = "assistant"
    assert _skill_reads(messages, [call], mapping, "session") == []


def test_process_exit_details_survive_runner_capture(openclaw, tmp_path, monkeypatch):
    detail = {"error": "openclaw_process_exit", "returncode": -15, "signal": 15,
              "stdout": {"text": "partial output"}, "stderr": {"text": ""}}
    monkeypatch.setattr(openclaw, "_http", lambda method, *a, **kw: (
        (500, {"detail": detail}) if method == "POST" else (200, {"history": [assistant("toolUse")]})
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.raw["_team_execution"]["stop_reason"] == "executor_process_exit"
    assert trace.raw["prompt_response"]["detail"] == detail
    assert trace.error
