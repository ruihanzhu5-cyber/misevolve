"""Same-session bounded correction for malformed team envelopes; no permissive repair."""

import json

import pytest
from test_team_phase3 import ScriptedRunner, action, make_router

from misevolve.multiagent._io import read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.team import ChainRouter, SkillTransferService, TeamExecutor, TeamWorkerPool


def malformed_shell_envelope():
    # Minimized from the real B response: an extra backslash before the closing
    # quote of a shell variable breaks the enclosing JSON string.
    text = json.dumps({"actions": [action("C", "RESULT", text='echo "$payload" >> "${GITHUB_STEP_SUMMARY}"')],
                       "final_answer": None})
    text = text.replace(r'${GITHUB_STEP_SUMMARY}\"', r'${GITHUB_STEP_SUMMARY}\\"')
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    return "Files verified.\n```json\n" + text + "\n```"


def setup(tmp_path, runner_factory, *, max_rounds=8):
    pool = TeamWorkerPool(tmp_path, runner_factory=runner_factory)
    workers = pool.setup_workers()
    router = make_router(tmp_path, ChainRouter)
    executor = TeamExecutor(workers, router, SkillTransferService(GlobalSkillRegistry(tmp_path / "registry"),
                                                                  workers, router), max_rounds=max_rounds)
    return pool, executor


def execute(executor, tmp_path):
    return executor.run({"kind": "B", "pos": 4, "instance_id": "task", "prompt": "Verify synthetic arithmetic."},
                        task_run_id="test", out_dir=tmp_path / "task")


@pytest.mark.parametrize("bad_agent", ["B", "C"])
@pytest.mark.parametrize("recover", [True, False])
def test_malformed_message_has_bounded_correction_and_no_partial_dispatch(tmp_path, bad_agent, recover):
    class Runner(ScriptedRunner):
        def continue_task_session(self, prompt, inject, *, out_dir):
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            if self.active[2] == bad_agent:
                correcting = '"output_format_correction"' in prompt
                if correcting:
                    assert "none of its actions were delivered" in prompt
                    assert "Do not repeat completed task commands" in prompt
                    assert "ARTIFACT_HANDOFF" in prompt
                if not correcting or not recover:
                    trace.final_text = malformed_shell_envelope()
            return trace

    pool, executor = setup(tmp_path, lambda *_: Runner("chain"))
    try:
        result = execute(executor, tmp_path)
        events = read_jsonl(executor.log.path)
        corrections = [e for e in events if e["event_type"] == "OUTPUT_FORMAT_CORRECTION_REQUESTED"]
        assert len(corrections) == (1 if recover else 2) and corrections[0]["agent_id"] == bad_agent
        assert corrections[0]["validation_error"]["error_type"] == "JSONDecodeError"
        assert corrections[0]["validation_error"]["line"] >= 1
        assert corrections[0]["actions_dispatched"] is False
        assert len(result.runtimes[bad_agent].traces) == (2 if recover else 3)
        delivered = [(e["sender_id"], e["receiver_id"]) for e in events if e["event_type"] == "MESSAGE_DELIVERED"]
        assert delivered.count(("A", "B")) == 1
        assert delivered.count(("B", "C")) == (1 if recover or bad_agent == "C" else 0)
        if recover:
            assert result.execution_status == "completed" and result.submitter == "C"
            # Same task session; invalid and corrected responses both retained.
            turns = result.trajectory.raw["turns"]
            assert sum(t["agent_id"] == bad_agent for t in turns) == 2
            sessions = {t["trajectory"]["session_id"] for t in turns if t["agent_id"] == bad_agent}
            assert len(sessions) == 1
        else:
            assert result.execution_status == "not_submitted"
            assert result.failure == {"agent_id": bad_agent, "phase": "output_validation",
                                      "stop_reason": "output_format_recovery_exhausted"}
    finally:
        pool.teardown()


def test_second_format_correction_can_salvage_the_original_task(tmp_path):
    class Runner(ScriptedRunner):
        bad_turns = 0

        def continue_task_session(self, prompt, inject, *, out_dir):
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            if self.active[2] == "B":
                self.bad_turns += 1
                if self.bad_turns <= 2:
                    trace.final_text = malformed_shell_envelope()
            return trace

    pool, executor = setup(tmp_path, lambda *_: Runner("chain"))
    try:
        result = execute(executor, tmp_path)
        assert result.execution_status == "completed" and result.submitter == "C"
        assert len(result.runtimes["B"].traces) == 3
        assert sum(e["event_type"] == "OUTPUT_FORMAT_CORRECTION_REQUESTED"
                   for e in read_jsonl(executor.log.path)) == 2
    finally:
        pool.teardown()


def test_literal_newline_and_tab_in_json_strings_are_preserved_without_a_model_retry(tmp_path):
    pool, executor = setup(tmp_path, lambda *_: ScriptedRunner("chain"))
    try:
        text = '{"actions":[], "final_answer":"line one\nline two\tend"}'
        assert executor._response(text, "task", "C")["final_answer"] == "line one\nline two\tend"
        events = read_jsonl(executor.log.path)
        assert events[-1]["event_type"] == "OUTPUT_JSON_CONTROL_CHARACTERS_RECOVERED"
        assert not any(e["event_type"] == "INVALID_AGENT_OUTPUT" for e in events)
    finally:
        pool.teardown()


def test_valid_empty_actions_are_not_format_failure(tmp_path):
    class Empty(ScriptedRunner):
        def continue_task_session(self, prompt, inject, *, out_dir):
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            if self.active[2] == "B":
                trace.final_text = '{"actions": [], "final_answer": null}'
            return trace

    pool, executor = setup(tmp_path, lambda *_: Empty("chain"))
    try:
        result = execute(executor, tmp_path)
        assert result.failure["stop_reason"] == "mailboxes_empty"
        assert len(result.runtimes["B"].traces) == 1
        assert not any(e["event_type"] == "OUTPUT_FORMAT_CORRECTION_REQUESTED" for e in read_jsonl(executor.log.path))
    finally:
        pool.teardown()


@pytest.mark.parametrize("budget", ["round", "deadline"])
def test_format_recovery_cannot_extend_task_budget(tmp_path, monkeypatch, budget):
    clock = [0]
    monkeypatch.setattr("misevolve.team.executor.monotonic", lambda: clock[0])

    class Bounded(ScriptedRunner):
        task_timeout_seconds = 5

        def bind_task_deadline(self, deadline):
            assert deadline == 5

        def continue_task_session(self, prompt, inject, *, out_dir):
            trace = super().continue_task_session(prompt, inject, out_dir=out_dir)
            if self.active[2] == "B":
                trace.final_text = malformed_shell_envelope()
                if budget == "deadline":
                    clock[0] = 6
            return trace

    pool, executor = setup(tmp_path, lambda *_: Bounded("chain"), max_rounds=1 if budget == "round" else 8)
    try:
        result = execute(executor, tmp_path)
        assert len(result.runtimes["B"].traces) == 1
        assert result.failure["stop_reason"] == ("round_limit" if budget == "round" else "task_timeout")
    finally:
        pool.teardown()


@pytest.mark.parametrize("text", [
    '{"actions": [], "final_answer": null}\n{"actions": []}',
    '```json\n{"actions": []}\n```\n```json\n{"actions": []}\n```',
    '{"actions": "not a list", "final_answer": null}',
])
def test_ambiguous_or_invalid_schema_is_never_silently_repaired(tmp_path, text):
    pool, executor = setup(tmp_path, lambda *_: ScriptedRunner("chain"))
    try:
        assert executor._response(text, "task", "B") is None
        assert executor._response(malformed_shell_envelope(), "task", "B") is None
    finally:
        pool.teardown()
