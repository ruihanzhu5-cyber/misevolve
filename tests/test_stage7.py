import io
import json
import runpy
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from misevolve._stage7_api import DEEPSEEK_MODEL, MINIMAX_MODEL, ProviderCalls, ProviderError, Scope, _usage
from misevolve._stage7_openclaw import Stage7Runner, tool_round_trip
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.multiagent._io import read_jsonl
from misevolve.paths import repo_root
from misevolve.stage7 import _benign_tasks, _complete_smoke, _judge, preflight


def test_provider_attempts_record_usage_once_and_stop_after_auth_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr("misevolve._stage7_api.time.sleep", lambda _: None)
    requests = []

    class Response(io.BytesIO):
        status = 200
        headers = {"request-id": "synthetic-request", "Content-Type": "application/json"}

    class Opener:
        def open(self, request, **kwargs):
            requests.append(json.loads(request.data))
            if len(requests) in (1, 3):
                status = 429 if len(requests) == 1 else 401
                raise HTTPError(request.full_url, status, "PRIVATE_HTTP_ERROR", {}, io.BytesIO())
            return Response(json.dumps({"model": DEEPSEEK_MODEL, "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 2,
                "prompt_cache_miss_tokens": 8,
            }}).encode())

    monkeypatch.setattr("misevolve._stage7_api.urllib.request.build_opener", lambda *args: Opener())
    calls = ProviderCalls(tmp_path, deepseek_peak=False)
    payload = {"model": DEEPSEEK_MODEL, "messages": [{"content": "PRIVATE_PROMPT"}], "max_tokens": 99999}
    calls.request(Scope("one", "A"), "behavior_judge", payload)
    with pytest.raises(ProviderError) as failure:
        calls.request(Scope("two", "A"), "behavior_judge", payload)
    assert failure.value.status == 401
    with pytest.raises(ProviderError, match="authentication failure"):
        calls.request(Scope("three", "A"), "behavior_judge", payload)
    assert len(requests) == 3 and all(request["max_tokens"] == 768 for request in requests)
    attempts = read_jsonl(calls.path)
    assert [row["http_status"] for row in attempts] == [429, 200, 401]
    assert [row["attempt"] for row in attempts] == [1, 2, 1]
    assert attempts[0]["call_id"] == attempts[1]["call_id"]
    assert attempts[1]["request_id"] == "synthetic-request"
    assert calls.summary()["input_tokens_reported"] == 10
    assert calls.summary()["cost_estimate_cny"] == pytest.approx(0.00002804)
    assert "PRIVATE_PROMPT" not in calls.path.read_text() and "synthetic-key" not in calls.path.read_text()


def test_stream_usage_uses_cumulative_provider_values():
    events = [
        {"type": "message_start", "message": {"model": MINIMAX_MODEL, "usage": {
            "input_tokens": 20, "output_tokens": 1, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0,
        }}},
        {"type": "message_delta", "usage": {"output_tokens": 2}},
        {"type": "message_delta", "usage": {"output_tokens": 8}},
        {"type": "message_stop"},
    ]
    stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
    usage, model = _usage(stream, streaming=True, deepseek=False)
    assert model == MINIMAX_MODEL and usage["input_tokens"] == 20
    assert usage["output_tokens"] == 8 and usage["cache_read_tokens"] == 5
    with pytest.raises(ProviderError, match="incomplete"):
        _usage(stream.split(b'data: {"type": "message_stop"}')[0], streaming=True, deepseek=False)


def test_openclaw_has_no_request_count_cap_and_prices_streams_without_cache_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-key")
    stream = (
        b'data: {"type":"message_start","message":{"model":"MiniMax-M3.1-Flash",'
        b'"usage":{"input_tokens":20,"cache_read_input_tokens":5}}}\n\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":8}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    requests = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/event-stream"}

    class Opener:
        def open(self, request, *, timeout):
            requests.append((json.loads(request.data), timeout))
            return Response(stream)

    monkeypatch.setattr("misevolve._stage7_api.urllib.request.build_opener", lambda *args: Opener())
    calls = ProviderCalls(tmp_path, deepseek_peak=False)
    scope = Scope("task", "A")
    for _ in range(20):
        calls.request(scope, "openclaw", {"model": MINIMAX_MODEL, "max_tokens": 9999})
    assert len(requests) == 20
    assert all(body["max_tokens"] == 2048 and timeout == 45 for body, timeout in requests)
    summary = calls.summary(task_id="task")
    assert summary["openclaw_requests"] == summary["attempts_with_cost"] == 20
    assert summary["cost_estimate_cny"] == pytest.approx(20 * 0.0000525)
    assert calls.summary(task_id="another")["openclaw_requests"] == 0
    assert all(row["cache_write_tokens"] is None for row in read_jsonl(calls.path))


def test_stage7_disables_the_prompt_wait_and_gateway_deadline(monkeypatch):
    relay = SimpleNamespace(server_address=("127.0.0.1", 8000), scope=None)
    runner = Stage7Runner("stage7", relay)
    runner.scope = Scope("task", "A")
    runner.container = "mock-container"
    commands = []
    monkeypatch.setattr(OpenClawRunner, "_wait_healthy", lambda _: None)

    def docker(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout="0")

    def http(self, method, path, body=None, *, timeout):
        assert timeout is None
        assert relay.scope == runner.scope
        return 200, {"status": "completed"}

    monkeypatch.setattr(runner, "_docker", docker)
    monkeypatch.setattr(OpenClawRunner, "_http", http)
    runner._wait_healthy()
    assert commands[-2][4:] == ["set", "agents.defaults.timeoutSeconds", "0", "--strict-json"]
    assert commands[-1][4:] == ["get", "agents.defaults.timeoutSeconds", "--json"]
    assert runner._http("POST", "/prompt", timeout=runner._timeout)[0] == 200
    assert relay.scope is None


@pytest.mark.parametrize(("module", "verdict"), [
    ("behavior_judge", {"task_success": "true", "harmful": False, "reason": "value was a string"}),
    ("artifact_judge", {"content_unsafety": 6, "generalization": 1, "sneakiness": 1}),
])
def test_invalid_judge_schema_is_not_a_verdict(tmp_path, module, verdict):
    class MockCalls:
        def request(self, scope, module, payload):
            return json.dumps({"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(verdict)},
            }]}).encode(), "application/json"

    path = tmp_path / "judge_results.jsonl"
    with pytest.raises(ValueError):
        _judge(MockCalls(), Scope("task", "A"), module, "harmless prompt", path)
    assert read_jsonl(path)[0]["verdict"] is None


def test_tool_round_trip_requires_matching_tool_result_and_followup():
    def event(role, text, **extra):
        return {"message": {"role": role, "content": [{"type": "text", "text": text}], **extra}}

    start = {"message": {"role": "assistant", "content": [{"type": "toolCall", "id": "expected"}]}}
    finish = event("assistant", "sentinel")
    wrong = event("toolResult", "sentinel", toolCallId="different")
    match = event("toolResult", "sentinel", toolCallId="expected")
    assert not tool_round_trip([start, wrong, finish], marker="sentinel")
    assert not tool_round_trip([start, match], marker="sentinel")
    assert tool_round_trip([start, match, finish], marker="sentinel")


def test_stage7_smoke_requires_native_revision_after_retrieval():
    tasks = _benign_tasks()
    assert [task["label"] for task in tasks] == [
        "healthz-endpoint", "startup-config-validation", "config-validation-module",
    ]
    assert {task["kind"] for task in tasks} == {"B"}
    rows = [{"task_run_id": str(i), "execution_ok": True, "behavior": {}, "tool_round_trip": i == 0, "retrieved": []}
            for i in range(3)]
    first = {"task_id": "0", "global_skill_id": "first", "native_revision_parent_id": None, "verdict": {}}
    second = {"task_id": "1", "global_skill_id": "second", "native_revision_parent_id": "first", "verdict": {}}
    assert not _complete_smoke(rows, [first, second])
    rows[1]["retrieved"] = [{"global_skill_id": "first"}]
    assert _complete_smoke(rows, [first, second])


def test_partial_preflight_resumes_without_repeating_provider_checks(tmp_path, monkeypatch):
    ran = []
    for function in ("_offline_isolation", "_openclaw_check", "_native_check", "_judges_check"):
        monkeypatch.setattr(f"misevolve.stage7.{function}", lambda *args, name=function: ran.append(name) or {})

    def runtime(*, docker_required):
        return {"autoskill_commit": "pinned"} | ({"image_id": "image"} if docker_required else {})

    monkeypatch.setattr("misevolve.stage7._runtime", runtime)
    calls = ProviderCalls(tmp_path, deepseek_peak=False)
    assert not preflight(calls, tmp_path, selected=("native_autoskill", "deepseek_judges"))
    assert preflight(calls, tmp_path, resume=True)
    assert ran == ["_native_check", "_judges_check", "_offline_isolation", "_openclaw_check"]
    assert json.loads((tmp_path / "preflight.json").read_text())["runtime"]["image_id"] == "image"


def test_stage7_openclaw_config_uses_compatible_fields_and_indirect_key(tmp_path, monkeypatch, capsys):
    path = repo_root() / "docker" / "openclaw"
    mapper = runpy.run_path(str(path / "config_mapper.py"))
    map_config = mapper["map_config"]
    target = tmp_path / "openclaw.json"
    monkeypatch.setitem(map_config.__globals__, "CONFIG_YAML", str(path / "config.yaml"))
    monkeypatch.setitem(map_config.__globals__, "OPENCLAW_JSON", str(target))
    monkeypatch.setenv("MISEVOLVE_API_KEY", "PRIVATE_RELAY_KEY")
    monkeypatch.setenv("MISEVOLVE_MODEL", MINIMAX_MODEL)
    monkeypatch.setenv("MISEVOLVE_OPENCLAW_STAGE7", "1")
    monkeypatch.setenv("MISEVOLVE_OPENCLAW_API", "anthropic-messages")
    assert map_config()
    config = json.loads(target.read_text())
    assert {"web", "canvasHost"}.isdisjoint(config)
    assert "lastTouchedAt" not in config["meta"]
    assert "resetOnExit" not in config["gateway"]["tailscale"]
    assert config["tools"]["allow"] == ["exec", "read", "write", "edit"]
    provider = config["models"]["providers"]["backbone"]
    assert provider["apiKey"] == "${MISEVOLVE_API_KEY}" and provider["api"] == "anthropic-messages"
    assert "PRIVATE_RELAY_KEY" not in target.read_text() + capsys.readouterr().out
