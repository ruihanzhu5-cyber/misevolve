import json
from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest

from misevolve import _stage7_openclaw, harness, stage7_pilot
from misevolve._stage7_api import OPENCLAW_OUTPUT, ProviderCalls
from misevolve.multiagent._io import append_jsonl, read_jsonl


def test_published_plan_and_full_history():
    episode, plan, _ = stage7_pilot._episode_and_plan()
    assert plan["episode_id"] == "skillmisevo-coding-04"
    assert plan["execution_order"] == list(stage7_pilot.POSITIONS)
    assert [entry["worker_id"] for entry in plan["tasks"]] == list("AAAAAAAAABBBCCCDDDBCD")
    prompts = {task["pos"]: task["prompt"] for task in episode["task_sequence"]}
    assert all(sha256(prompts[entry["source_pos"]].encode()).hexdigest() == entry["prompt_sha256"]
               for entry in plan["tasks"])
    raw = [{"type": "session", "id": "synthetic"}] + [
        {"type": "message", "message": {"role": "toolResult", "content": "test output" * 180}}
        for _ in range(105)
    ] + [{"type": "message", "message": {"role": "assistant", "content": "FINAL_TOOL_EVIDENCE"}}]
    text = "".join(json.dumps(row) + "\n" for row in raw)
    history = stage7_pilot._decode_history(text)
    assert len(history) == 107 and history[-1]["message"]["content"] == "FINAL_TOOL_EVIDENCE"
    with pytest.raises(ValueError, match="incomplete"):
        stage7_pilot._decode_history(text[:-1])
    for kind, modules in (("M", ["harm_judge"]), ("B", ["harm_judge", "utility_judge"]),
                          ("P", ["harm_judge", "utility_judge"])):
        rendered = list(stage7_pilot._task_prompts(episode, history, "synthetic risk", "synthetic utility", kind))
        assert [module for module, _ in rendered] == modules
        assert all("FINAL_TOOL_EVIDENCE" in prompt and len(prompt) > 160_000 for _, prompt in rendered)


def test_mock_pilot_attributes_each_call_and_accepts_empty_transfers(tmp_path, monkeypatch, runner_factory):
    class MockCalls(ProviderCalls):
        def request(self, scope, module, payload, **kwargs):
            append_jsonl(self.path, {
                "task_id": scope.task_id, "worker_id": scope.worker_id, "instance_id": scope.instance_id,
                "module": module, "status": "ok", "attempt": 1, "cost": 0,
                "input_tokens": 10, "output_tokens": 10, "elapsed_seconds": 0,
                "max_output_tokens": payload.get("max_tokens"),
            })
            if module == "openclaw":
                return b"{}", "application/json"
            if module == "extractor":
                return b'{"content":[{"type":"text","text":"{\\"skills\\":[]}"}]}', "application/json"
            value = {"reason": "Synthetic result."}
            if module == "harm_judge":
                value.update(harmful=False, score=0)
            else:
                assert module == "utility_judge"
                value.update(completed=False, utility_score=0)
            return json.dumps({"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(value)},
            }]}).encode(), "application/json"

    calls = MockCalls(tmp_path, deepseek_peak=False)

    @contextmanager
    def offline_relay(provider):
        assert provider is calls
        yield "synthetic-network", None

    def runner(*_):
        mock = runner_factory("openclaw", stage7_pilot.BACKBONE)

        def execute(prompt, skills, *, out_dir):
            calls.request(mock.scope, "openclaw", {"max_tokens": OPENCLAW_OUTPUT})
            archive = "saved" if mock.scope.worker_id == "A" else "not_applicable"
            evidence = {"history_complete": True, "workspace_archive": archive, "stop_reason": "stop"}
            return harness.Trajectory(
                [{"role": "assistant", "text": "Synthetic evidence."}], raw={
                    "history": [{"message": {"role": "assistant", "content": "Synthetic evidence."}}],
                    "_pilot_execution": evidence, "_skill_injection": {"status": "success", "skills": []},
                },
            )

        mock.run = execute
        return mock

    monkeypatch.setattr(stage7_pilot, "_pilot_relay", offline_relay)
    monkeypatch.setattr(stage7_pilot, "PilotRunner", runner)
    episode, _, _ = stage7_pilot._episode_and_plan()
    assert stage7_pilot.pilot(calls, tmp_path, episode, {"configuration": {}})
    report = json.loads((tmp_path / "pilot_summary.json").read_text(encoding="utf-8"))
    assert report["technical_acceptance"] == "passed" and report["attempted_tasks"] == 21
    rows = report["tasks"]
    assert all(row["attack_success"] is False for row in rows)
    assert [row["task_success"] for row in rows] == [None] * 9 + [False] * 12
    assert all(row["native_ingest_ok"] is None for row in rows[-3:])
    journal = read_jsonl(calls.path)
    scopes = {row["task_run_id"]: row["instance_id"] for row in rows}
    assert all(scopes[row["task_id"]] == row["instance_id"] for row in journal)
    assert sum(row["module"] == "extractor" for row in journal) == 18
    assert sum(row["module"] == "harm_judge" for row in journal) == 21
    assert sum(row["module"] == "utility_judge" for row in journal) == 12
    assert report["completed_hops"] == [["A", "B"], ["B", "C"], ["C", "D"]]


def test_status_preserves_complete_records_before_a_torn_tail(tmp_path):
    path = tmp_path / "episode" / "episode_events.jsonl"
    append_jsonl(path, {"event": "task_finished", "execution_index": 8})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event":')
    report = stage7_pilot._status(tmp_path)
    counts = next(iter(report["durable_events"].values()))
    assert counts == {"task_finished": 1, "incomplete_tail": 1}
    assert next(iter(report["latest_checkpoints"].values()))["execution_index"] == 8


def test_task_timeout_shares_budget_with_correction_and_captures_history(monkeypatch, tmp_path):
    relay = SimpleNamespace(server_address=("127.0.0.1", 1234), scope=None)
    runner = stage7_pilot.PilotRunner("synthetic", relay)
    runner.scope = SimpleNamespace(worker_id="B")
    observed = []
    clock = iter([0, 1195])
    monkeypatch.setattr(_stage7_openclaw, "monotonic", lambda: next(clock))

    def http(self, method, path, body=None, *, timeout):
        assert method == "POST" and path == "/prompt"
        observed.append(timeout)
        if len(observed) == 1:
            return 200, {}
        raise TimeoutError

    records = [{"type": "session"}, {"type": "message", "message": {"role": "assistant", "content": "Test"}}]

    def docker(args, **kwargs):
        if args[0] == "network":
            return SimpleNamespace(stdout='[{"Containers":{}}]')
        return SimpleNamespace(returncode=0, stdout="".join(json.dumps(row) + "\n" for row in records))

    monkeypatch.setattr(stage7_pilot.OpenClawRunner, "_http", http)
    monkeypatch.setattr(runner, "_tool_correction", lambda _: "synthetic correction")
    monkeypatch.setattr(runner, "_docker", docker)
    monkeypatch.setattr(runner, "_boot", lambda: None)
    monkeypatch.setattr(runner, "_rm_container", lambda: None)
    monkeypatch.setattr(runner, "_write_skills", lambda *args: {})
    trajectory = runner.run("synthetic task", [], out_dir=tmp_path)
    assert observed == [1200, 5] and relay.scope is None
    assert trajectory.error and trajectory.raw["history"] == records
    evidence = json.loads((tmp_path / "execution_evidence.json").read_text(encoding="utf-8"))
    assert evidence["stop_reason"] == "task_timeout" and evidence["task_timeout_seconds"] == 1200


def test_pilot_firewall_is_bridge_scoped_and_cleaned_on_failure(monkeypatch):
    @contextmanager
    def relay(_calls):
        yield "test-bridge", SimpleNamespace(server_address=("172.28.0.1", 8123))

    monkeypatch.setattr(stage7_pilot, "isolated_relay", relay)
    info = [{"Driver": "bridge", "EnableIPv6": False, "Id": "123456789abc000", "Options": {}}]
    monkeypatch.setattr(stage7_pilot.OpenClawRunner, "_docker",
                        lambda *a: SimpleNamespace(stdout=json.dumps(info)))
    commands = []
    monkeypatch.setattr(stage7_pilot.subprocess, "run", lambda args, **kw: commands.append(args))
    with pytest.raises(RuntimeError, match="synthetic"):
        with stage7_pilot._pilot_relay(None):
            assert len(commands) == 3
            raise RuntimeError("synthetic")
    inserted = [command[6:] for command in commands[:3]]
    removed = [command[5:] for command in commands[3:]]
    assert inserted[::-1] == removed
    assert all(rule[:2] == ["-i", "br-123456789abc"] for rule in inserted)
    assert "--dport" in inserted[1] and "8123" in inserted[1]
