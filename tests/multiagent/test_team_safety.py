"""Offline regressions for Phase 3 execution and safety assessment."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_team_phase3 import ScriptedRunner, coding_episode, make_router

from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.harness import TaskOutcome, Trajectory
from misevolve.multiagent._io import append_jsonl, read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.team import ChainRouter, SkillTransferService, TeamEpisodeScheduler, TeamExecutor, TeamWorkerPool
from misevolve.team.assessment import behavior_summary
from misevolve.team.executor import SessionTurnError, TeamTaskExecutionError, TeamTaskResult
from misevolve.team.openclaw import TeamOpenClawRunner


@pytest.fixture
def openclaw(tmp_path, monkeypatch):
    relay = SimpleNamespace(server_address=("127.0.0.1", 0), token="mock")
    runner = TeamOpenClawRunner("synthetic-network", relay)
    monkeypatch.setattr(runner, "_docker", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, '[{"Containers":{}}]', "",
    ))
    monkeypatch.setattr(runner, "_boot", lambda: setattr(runner, "container", "synthetic-container"))
    monkeypatch.setattr(runner, "_write_skills", lambda *args: {"status": "success", "skills": []})
    monkeypatch.setattr(runner, "_read_frozen_history", lambda _: runner._http("GET", "/history", timeout=1))
    runner.open_task_session(task_run_id="run", task_id="task", agent_id="C", out_dir=tmp_path)
    yield runner
    runner.close_task_session()


def assistant(stop_reason, text=None):
    message = {"role": "assistant", "content": [{
        "type": "text", "text": text or json.dumps({"actions": [], "final_answer": "valid JSON"}),
    }]}
    if stop_reason is not None:
        message["stopReason"] = stop_reason
    return {"type": "message", "message": message}


@pytest.mark.parametrize(("http_status", "terminal", "status", "stop"), [
    (200, "stop", "completed", "stop"),
    (200, "end_turn", "completed", "end_turn"),
    (200, "stop_sequence", "completed", "stop_sequence"),
    (200, "error", "failed", "error"),
    (200, "max_tokens", "failed", "max_tokens"),
    (200, "length", "failed", "length"),
    (200, "toolUse", "failed", "toolUse"),
    (200, None, "failed", "missing_terminal"),
    (502, "stop", "failed", "http_502"),
    (504, "stop", "timeout", "task_timeout"),
])
def test_http_and_transcript_terminal(openclaw, tmp_path, monkeypatch, http_status, terminal, status, stop):
    records = [
        {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse", "content": [
            {"type": "toolCall", "id": "call", "name": "exec", "arguments": {"command": "synthetic"}},
        ]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "call", "content": "observed"}},
        assistant(terminal),
    ]
    monkeypatch.setattr(openclaw, "_http", lambda method, *args, **kw: (
        (http_status, {"response": "valid JSON"}) if method == "POST" else (200, {"history": records})
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert bool(trace.error) == (status != "completed")
    assert trace.raw["_team_execution"]["execution_status"] == status
    assert trace.raw["_team_execution"]["stop_reason"] == stop
    assert json.loads(trace.final_text)["final_answer"] == "valid JSON"
    assert trace.raw["history"] == records
    assert trace.raw["tool_calls"][0]["id"] == "call"
    assert any(message.get("tool_call_id") == "call" for message in trace.messages)


@pytest.mark.parametrize(("response", "reason"), [
    ((200, {"history": []}), "transcript_empty"),
    ((404, {}), "transcript_unavailable"),
    ((200, {"history": ["malformed"]}), "transcript_malformed"),
    ((200, {"history": [{"type": "session"}]}), "missing_terminal"),
])
def test_post_body_cannot_replace_missing_transcript(openclaw, tmp_path, monkeypatch, response, reason):
    monkeypatch.setattr(openclaw, "_http", lambda method, *args, **kw: (
        (200, {"response": "Valid-looking final answer"}) if method == "POST" else response
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.error and not trace.final_text
    assert trace.raw["_team_execution"]["evidence_completeness"] == "none"
    assert trace.raw["_team_execution"]["stop_reason"] == reason


def test_normal_terminal_cannot_hide_unpaired_tool_call(openclaw, tmp_path, monkeypatch):
    records = [{"type": "message", "message": {"role": "assistant", "stopReason": "toolUse", "content": [
        {"type": "toolCall", "id": "unpaired", "name": "exec", "arguments": {"command": "synthetic"}},
    ]}}, assistant("stop")]
    monkeypatch.setattr(openclaw, "_http", lambda method, *_args, **_kwargs: (
        (200, {}) if method == "POST" else (200, {"history": records})
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.error
    evidence = trace.raw["_team_execution"]
    assert evidence["stop_reason"] == "unpaired_tool_calls"
    assert evidence["unpaired_tool_call_ids"] == ["unpaired"]


def test_transport_exception_saves_recovered_trace_before_raising(openclaw, tmp_path, monkeypatch):
    def http(method, *_args, **_kwargs):
        if method == "POST":
            raise ConnectionError("synthetic transport error")
        return 200, {"history": [assistant("error")]}

    monkeypatch.setattr(openclaw, "_http", http)
    with pytest.raises(SessionTurnError) as caught:
        openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert caught.value.trajectory.raw["history"] == [assistant("error")]
    assert (tmp_path / "turn" / "trajectory_raw.json").is_file()


def test_transport_timeout_still_collects_transcript(openclaw, tmp_path, monkeypatch):
    commands = []

    def docker(args, **kwargs):
        commands.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    def http(method, *_args, **_kwargs):
        if method == "POST":
            raise TimeoutError("synthetic timeout")
        assert commands == ["pause"]
        return 200, {"history": [assistant("error")]}

    monkeypatch.setattr(openclaw, "_docker", docker)
    monkeypatch.setattr(openclaw, "_http", http)
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.error and trace.raw["_team_execution"]["execution_status"] == "timeout"
    assert trace.raw["_team_execution"]["evidence_completeness"] == "partial"
    assert openclaw.container is None and commands == ["pause", "rm"]


@pytest.mark.parametrize("owner", [False, True])
def test_only_internal_exec_notice_can_follow_a_terminal_answer(openclaw, tmp_path, monkeypatch, owner):
    records = [assistant("stop"), {"type": "message", "message": {
        "role": "user", "content": "Exec completed (synthetic event)", "idempotencyKey": "exec:test",
        "provenance": {"kind": "internal_system", "sourceTool": "exec"}, "__openclaw": {"senderIsOwner": owner},
    }}]
    monkeypatch.setattr(openclaw, "_http", lambda method, *args, **kw: (
        (200, {"response": "older answer"}) if method == "POST" else (200, {"history": records})
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    if owner:
        assert trace.error and trace.raw["_team_execution"]["stop_reason"] == "missing_terminal"
        assert not trace.final_text
    else:
        assert not trace.error and trace.raw["_team_execution"]["stop_reason"] == "stop"
        assert json.loads(trace.final_text)["final_answer"] == "valid JSON"
    assert trace.raw["history"] == records


def test_a_blank_terminal_does_not_reuse_earlier_json(openclaw, tmp_path, monkeypatch):
    empty_stop = {"type": "message", "message": {"role": "assistant", "stopReason": "stop", "content": []}}
    records = [assistant("error"), empty_stop]
    monkeypatch.setattr(openclaw, "_http", lambda method, *args, **kw: (
        (200, {"response": "older answer"}) if method == "POST" else (200, {"history": records})
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.error and not trace.final_text
    assert trace.raw["_team_execution"]["stop_reason"] == "missing_terminal_output"
    assert trace.raw["assistant_messages"] and trace.raw["history"] == records


def test_tool_argument_correction_finishes_in_the_same_turn(openclaw, tmp_path, monkeypatch):
    history = [{"type": "session"}]
    posted = []
    openclaw.relay.calls = SimpleNamespace(path=tmp_path / "attempts.jsonl")
    malformed = assistant("error")
    malformed["message"]["errorCode"] = "malformed_tool_call_arguments"

    def http(_runner, _method, _path, body, *, timeout):
        posted.append(body)
        if len(posted) == 1:
            history.append(malformed)
            append_jsonl(openclaw.relay.calls.path, {
                "task_id": openclaw.scope.task_id, "module": "openclaw", "status": "ok",
                "stream": {"message_stop": True, "stop_reason": "tool_use",
                           "tool_blocks": [{"name": "exec", "argument_error": True}]},
            })
        else:
            history.append(assistant("stop"))
        return 200, {"response": "synthetic"}

    monkeypatch.setattr(OpenClawRunner, "_http", http)
    monkeypatch.setattr(openclaw, "_docker", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, "".join(json.dumps(item) + "\n" for item in history), "",
    ))
    trace = openclaw.continue_task_session("synthetic", [], out_dir=tmp_path / "turn")
    assert trace.error is None and trace.raw["history"] == history
    assert len(posted) == 2 and posted[0]["session_id"] == posted[1]["session_id"]
    assert posted[1]["prompt"].startswith("Stage 7 tool argument correction.")


TASK = {"instance_id": "task", "prompt": "synthetic", "kind": "B", "pos": 4, "round": 1}
EPISODE = {"malicious_goal": "synthetic"}


def observation(agent, *, tools=True):
    calls = [{"name": "exec", "id": "same-native-id", "input": {"command": "synthetic"}}] if tools else []
    return {
        "agent_id": agent, "turn_id": f"run:{agent}:1", "input": {},
        "trajectory": {
            "tool_calls": calls,
            "communication_protocol": "hidden-router-marker",
        },
        "messages": [{"role": "assistant", "text": "observed response"}],
        "files_changed": [], "final_text": "observed response", "execution_error": False,
    }


def result_with_turns(*turns, status="completed"):
    submitted = status == "completed"
    return TeamTaskResult(
        "C" if submitted else None,
        Trajectory([], final_text="answer" if submitted else "",
                   error=None if submitted else "team did not submit a final answer",
                   raw={"task_run_id": "run", "turns": list(turns)}),
        {}, status,
    )


@pytest.mark.parametrize("harmful", [True, False, None])
def test_not_submitted_still_assesses_observed_harm(harmful):
    judged = []

    def judge(trace, *_):
        judged.append(trace)
        assert trace.error and trace.raw["evidence_completeness"] == "partial"
        assert trace.raw["turns"][0]["tool_calls"][0]["evidence_ref"] == "run:B:1/tool/0"
        return TaskOutcome(harmful, True)

    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=judge)
    result = result_with_turns(observation("B"), status="not_submitted")
    assessment = scheduler._judge(result, EPISODE, TASK)
    assert len(judged) == 1
    assert assessment["team_verdict"] == {"harmful": True if harmful is True else None, "utility": None}
    assert assessment["judge_reason"] == "incomplete_evidence"


@pytest.mark.parametrize("value", [(True, False), (False, True), (None, True), (False, None)])
def test_legacy_team_fields_are_independent_and_not_broadcast(value):
    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=lambda *_: TaskOutcome(*value))
    assessment = scheduler._judge(result_with_turns(observation("B"), observation("C")), EPISODE, TASK)
    assert tuple(assessment["team_verdict"].values()) == value
    assert all(member["harmful"] is None for member in assessment["members"])
    assert assessment["members"][1]["error_reason"] == "legacy_team_only"


def test_no_evidence_and_judge_failure_are_distinct():
    def judge(*_):
        raise RuntimeError("synthetic judge failure")

    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=judge)
    missing = scheduler._judge(result_with_turns(status="failed"), EPISODE, TASK)
    error = scheduler._judge(result_with_turns(observation("A")), EPISODE, TASK)
    assert (missing["judge_status"], missing["judge_reason"]) == ("not_assessed", "no_evidence")
    assert (error["judge_status"], error["judge_error_type"]) == ("error", "RuntimeError")
    assert missing["team_verdict"] == error["team_verdict"] == {"harmful": None, "utility": None}


@pytest.mark.parametrize("harmful_agent", ["A", "B", "C"])
def test_structured_attribution_uses_only_own_turns(harmful_agent):
    def judge(trace, *_):
        assert "hidden-router-marker" not in json.dumps(trace.raw)
        return {
            "schema_version": 1,
            "team": {"harmful": True, "utility": False},
            "members": {agent: {
                "harmful": agent == harmful_agent,
                "evidence_refs": [f"run:{agent}:1" + ("/tool/0" if agent == harmful_agent else "")],
            } for agent in "ABC"},
        }

    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=judge)
    assessment = scheduler._judge(result_with_turns(*(observation(a) for a in "ABC")), EPISODE, TASK)
    assert [m["agent_id"] for m in assessment["members"] if m["harmful"]] == [harmful_agent]
    assert all(m["assessment_status"] == "confirmed" for m in assessment["members"])


def test_invalid_cross_member_or_unobserved_refs_do_not_confirm():
    def judge(*_):
        return {
            "schema_version": 1, "team": {"utility": True},
            "members": {
                "A": {"harmful": True, "evidence_refs": ["other-task:A:1/tool/0"]},
                "B": {"harmful": True, "evidence_refs": ["run:A:1/tool/0"]},
                "C": {"harmful": True, "evidence_refs": ["run:C:1"]},
            },
        }

    received = observation("C", tools=False)
    received["input"] = {"messages": [{"message_type": "SKILL_PAYLOAD", "payload": {"skill_id": "synthetic"}}]}
    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=judge)
    assessment = scheduler._judge(result_with_turns(observation("A"), observation("B"), received), EPISODE, TASK)
    assert assessment["team_verdict"] == {"harmful": None, "utility": True}
    assert [m["error_reason"] for m in assessment["members"]] == [
        "invalid_evidence_refs", "invalid_evidence_refs", "missing_action_evidence",
    ]
    assert all(m["harmful"] is None for m in assessment["members"])


def test_m_has_no_b_or_c_attribution_even_if_judge_supplies_it():
    def judge(*_):
        return {"schema_version": 1, "team": {"harmful": True},
                "members": {"B": {"harmful": True, "evidence_refs": ["run:A:1/tool/0"]}}}

    scheduler = TeamEpisodeScheduler(None, ChainRouter, judge=judge)
    assessment = scheduler._judge(result_with_turns(observation("A")), EPISODE, {**TASK, "kind": "M"})
    assert [m["assessment_status"] for m in assessment["members"]] == [
        "unknown", "not_participated", "not_participated",
    ]


@pytest.mark.parametrize("c_mode", ["invalid_json", "abnormal", "timeout", "raise"])
def test_failed_team_retains_b_behavior_and_one_task_row(tmp_path, c_mode):
    class Runner(ScriptedRunner):
        def continue_task_session(self, prompt, inject, *, out_dir):
            if self.active[2] == "C" and c_mode == "raise":
                raise RuntimeError("synthetic C failure")
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            if self.active[2] == "C":
                if c_mode == "invalid_json":
                    trace.final_text = "invalid JSON"
                else:
                    trace.error = "abnormal final answer"
                    trace.raw["_team_execution"] = {"execution_status": "timeout" if c_mode == "timeout" else "failed"}
                    assert json.loads(trace.final_text)["final_answer"]
            return trace

    def judge(trace, *_):
        b = next(turn for turn in trace.raw["turns"] if turn["agent_id"] == "B")
        return {"schema_version": 1, "team": {"harmful": True, "utility": True}, "members": {
            "B": {"harmful": True, "evidence_refs": [b["tool_calls"][0]["evidence_ref"]]},
            "C": {"harmful": False, "evidence_refs": [next((
                turn["turn_id"] for turn in trace.raw["turns"] if turn["agent_id"] == "C"
            ), "unknown")]},
        }}

    pool = TeamWorkerPool(tmp_path, runner_factory=lambda *_: Runner("chain"))
    workers = pool.setup_workers()
    try:
        registry = GlobalSkillRegistry(tmp_path / "registry")
        router = make_router(tmp_path, ChainRouter)
        executor = TeamExecutor(workers, router, SkillTransferService(registry, workers, router))
        scheduler = TeamEpisodeScheduler(pool, ChainRouter, judge=judge)
        out = tmp_path / "task"
        if c_mode == "raise":
            with pytest.raises(TeamTaskExecutionError) as caught:
                executor.run(TASK, task_run_id="run", out_dir=out)
            result = caught.value.result
            assert result.failure == {
                "phase": "turn", "agent_id": "C", "turn_id": "run:C:1", "error_type": "RuntimeError",
            }
        else:
            result = executor.run(TASK, task_run_id="run", out_dir=out)
        rows = []
        scheduler._record_task(tmp_path, out, executor, EPISODE, TASK, "run", result, rows, registry, {})
        assert len(rows) == 1
        row = rows[0]
        assert row["submitter"] is None and row["runner_success"] is False and row["task_success"] is None
        assert row["execution_status"] == {
            "invalid_json": "not_submitted", "abnormal": "failed", "timeout": "timeout", "raise": "failed",
        }[c_mode]
        assert row["attack_success"] is True and row["members"][1]["harmful"] is True
        assert row["members"][2]["harmful"] is None
        if c_mode in {"timeout", "abnormal"}:
            assert result.runtimes["C"].learning_trace(TASK) is None
        saved = json.loads((out / "team_trajectory.json").read_text(encoding="utf-8"))
        assert any(turn["agent_id"] == "B" for turn in saved["raw"]["turns"])
        assert sum(event["event_type"] == "TASK_COMPLETED" for event in read_jsonl(router.log.path)) == 1
        summary = behavior_summary(rows)
        assert summary["team_attempted"] == summary["by_task_kind"]["B"]["harmful"]["attempted"] == 1
        assert summary["normal_task_members"]["B"]["true"] == 1
        assert summary["behaviorally_affected_reach"]["confirmed_agents"] == ["B"]
    finally:
        pool.teardown()


def test_scheduler_persists_infrastructure_failure_before_propagating(tmp_path, method_factory, monkeypatch):
    class Runner(ScriptedRunner):
        def continue_task_session(self, prompt, inject, *, out_dir):
            if self.active[2] == "C":
                raise RuntimeError("synthetic C failure")
            return super().continue_task_session(prompt, inject, out_dir=out_dir)

    def native_factory(*args, **kwargs):
        method = method_factory(*args, **kwargs)
        # Other regressions exercise native ingest. This test stops in the first B task.
        def evolve(*_args, **_kwargs):
            method._last_evolution_events = []

        monkeypatch.setattr(method, "evolve_batch", evolve)
        return method

    pool = TeamWorkerPool(tmp_path, runner_factory=lambda *_: Runner("chain"), method_factory=native_factory)
    scheduler = TeamEpisodeScheduler(pool, ChainRouter, judge=lambda *_: TaskOutcome(True, False))
    with pytest.raises(TeamTaskExecutionError, match="synthetic C failure"):
        scheduler.run(coding_episode())
    rows = json.loads((tmp_path / "all_rows.json").read_text(encoding="utf-8"))
    assert [row["pos"] for row in rows] == [1, 2, 3, 4]
    assert rows[-1]["execution_status"] == "failed" and rows[-1]["failure"]["agent_id"] == "C"
    assert rows[-1]["attack_success"] is True and rows[-1]["task_success"] is None
    assert (tmp_path / "tasks" / f"04-{rows[-1]['task_run_id']}" / "team_trajectory.json").exists()
    assert json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))["phase"] == "failed"
