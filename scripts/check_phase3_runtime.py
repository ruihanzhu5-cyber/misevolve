"""Exercise the deployed Phase 3 container through a synthetic, local Anthropic relay."""

from __future__ import annotations

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from misevolve._stage7_api import MINIMAX_MODEL
from misevolve._stage7_openclaw import BACKBONE
from misevolve.team.openclaw import isolated_openclaw_factory


def stream(model, turn, name=None, arguments=None):
    stop = "tool_use" if name else "end_turn"
    block = {"type": "tool_use", "id": f"toolu_offline_{turn}", "name": name, "input": {}} if name else {
        "type": "text", "text": "",
    }
    delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)} if name else {
        "type": "text_delta", "text": "synthetic final result",
    }
    events = [
        {"type": "message_start", "message": {"id": f"msg_offline_{turn}", "type": "message", "role": "assistant",
          "content": [], "model": model, "stop_reason": None, "stop_sequence": None,
          "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events).encode()


class LocalScript:
    """Only the loopback test script can produce a response; no provider client is used."""

    def __init__(self, path):
        self.path = path
        self.turn = 0
        self.delay = 0
        self.commands = []
        self.finished_after_cutoff = False

    def request(self, scope, module, payload, *, correction=None):
        assert module == "openclaw" and correction is None
        assert payload["model"] == MINIMAX_MODEL and payload["max_tokens"] == 131072
        assert {"exec", "process"} <= {tool["name"] for tool in payload["tools"]}
        self.turn += 1
        if self.commands:
            cmd = self.commands.pop(0)
            return stream(MINIMAX_MODEL, self.turn, "exec", cmd), "text/event-stream"
        if self.delay:
            scope.execution.admit("synthetic-provider:1")
            time.sleep(self.delay)
            self.finished_after_cutoff = scope.execution.settle("synthetic-provider:1")
            return stream(MINIMAX_MODEL, self.turn, "exec", {
                "command": "echo late-tool-must-not-run",
            }), "text/event-stream"
        return stream(MINIMAX_MODEL, self.turn), "text/event-stream"


def main():
    with TemporaryDirectory(prefix="phase3-offline-") as directory:
        root = Path(directory)
        calls = LocalScript(root / "unused-ledger.jsonl")
        with isolated_openclaw_factory(calls) as factory:
            runner = factory("openclaw", BACKBONE)
            runner.open_task_session(task_run_id="normal", task_id="normal", agent_id="A", out_dir=root)
            calls.commands = [{"command": "echo early-tool-result", "awaitResults": True}]
            normal = runner.continue_task_session("Synthetic tool round trip.", [], out_dir=root / "normal")
            assert normal.error is None and normal.raw["_team_execution"]["tool_results"] == 1, normal.raw
            assert "early-tool-result" in json.dumps(normal.messages)
            runner.close_task_session()

            runner.open_task_session(task_run_id="deadline", task_id="deadline", agent_id="A", out_dir=root)
            calls.commands = [{"command": "echo captured-before-cutoff; exec sleep 60", "background": True}]
            calls.delay = 50
            runner.bind_task_deadline(time.monotonic() + 45)
            expired = runner.continue_task_session("Synthetic cutoff.", [], out_dir=root / "expired")
            cutoff = expired.raw["_team_execution"]
            assert cutoff["stop_reason"] == "task_timeout" and cutoff["transcript_status"] == "captured", cutoff
            assert cutoff["cutoff"]["container"] == "paused_before_snapshot_then_removed", cutoff
            assert cutoff["cutoff"]["in_flight_attempt_ids"] == ["synthetic-provider:1"], cutoff
            assert cutoff["tool_calls"] == cutoff["tool_results"] == 1, cutoff
            assert runner.container is None and "late-tool-must-not-run" not in json.dumps(expired.raw)
            runner.close_task_session()

            runner.open_task_session(task_run_id="active-tool", task_id="active-tool", agent_id="A", out_dir=root)
            calls.commands = [{"command": "echo partial-tool-output; exec sleep 60", "awaitResults": True}]
            calls.delay = 0
            runner.bind_task_deadline(time.monotonic() + 45)
            active_tool = runner.continue_task_session("Synthetic foreground wait.", [], out_dir=root / "active-tool")
            cutoff = active_tool.raw["_team_execution"]
            assert cutoff["stop_reason"] == "task_timeout" and cutoff["transcript_status"] == "captured", cutoff
            assert cutoff["tool_calls"] == 1 and cutoff["tool_results"] == 0, cutoff
            assert not cutoff["cutoff"]["in_flight_attempt_ids"] and runner.container is None
        assert calls.finished_after_cutoff
    print(json.dumps({"normal_tool_round_trip": "passed", "deadline_paused_copy_removed": "passed",
                      "late_tool_not_dispatched": "passed", "active_tool_stopped": "passed",
                      "external_model_calls": 0}))


if __name__ == "__main__":
    main()
