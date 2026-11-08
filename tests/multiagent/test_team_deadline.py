"""Deadline regressions use synthetic relay and provider results."""

from __future__ import annotations

import io
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from misevolve._stage7_api import MINIMAX_MODEL, ExecutionFence, ProviderCalls, ProviderError, Scope
from misevolve._stage7_openclaw import _Handler
from misevolve.multiagent._io import read_jsonl


@pytest.mark.parametrize("transport_timeout", [False, True])
def test_dispatched_attempt_settles_after_cutoff_without_retry_or_delivery(monkeypatch, tmp_path, transport_timeout):
    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-key")
    fence = ExecutionFence(monotonic() + 60)
    scope = Scope("task:A:1", "A", "task", fence)
    in_flight = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json", "request-id": "synthetic"}

    class Opener:
        def open(self, request, *, timeout):
            in_flight.extend(fence.close())
            if transport_timeout:
                raise TimeoutError
            return Response(json.dumps({"model": MINIMAX_MODEL, "usage": {
                "input_tokens": 20, "output_tokens": 8, "cache_read_input_tokens": 0,
            }}).encode())

    monkeypatch.setattr("misevolve._stage7_api.urllib.request.build_opener", lambda *args: Opener())
    calls = ProviderCalls(tmp_path, deepseek_peak=True)
    for _ in range(2):
        with pytest.raises(ProviderError) as error:
            calls.request(scope, "openclaw", {"model": MINIMAX_MODEL})
        assert error.value.category == "task_deadline"
    row, = read_jsonl(calls.path)
    assert in_flight == [f"{row['call_id']}:1"]
    assert row["status"] == ("timeout" if transport_timeout else "ok")
    assert (row["cost"] is None) is transport_timeout
    assert row["late_after_execution_cutoff"]
    assert read_jsonl(tmp_path / "late_executor_attempts.jsonl") == [row]


def test_relay_disconnects_instead_of_returning_a_late_tool_call():
    fence = ExecutionFence(monotonic() + 60)

    class SyntheticCalls:
        def request(self, scope, module, payload, *, correction):
            scope.execution.close()
            return b'{"type":"tool_use","name":"exec"}', "application/json"

    handler = _Handler.__new__(_Handler)
    handler.server = SimpleNamespace(calls=SyntheticCalls(), token="local-relay-token",
                                     scope=Scope("task:A:1", "A", "task", fence),
                                     take_correction=lambda _: None)
    handler.path = "/anthropic/v1/messages"
    handler.headers = {"x-api-key": handler.server.token, "Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.close_connection = False
    handler._respond = lambda *args: pytest.fail("response must not be delivered after cutoff")
    handler.do_POST()
    assert handler.close_connection
