"""Opt-in multi-turn adapter for the pinned Stage 7 OpenClaw image."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.error import URLError

from misevolve._stage7_api import Scope
from misevolve._stage7_openclaw import BACKBONE, NORMAL_STOP_REASONS, execution_evidence
from misevolve.adapters import _normalize
from misevolve.multiagent._io import atomic_json
from misevolve.stage7_pilot import PilotRunner, _pilot_relay

from .executor import SessionTurnError


class TeamOpenClawRunner(PilotRunner):
    def open_task_session(self, *, task_run_id, task_id, agent_id, out_dir):
        if self.container or getattr(self, "_team_session_id", None):
            raise RuntimeError("a team session is already open")
        attached = json.loads(self._docker(["network", "inspect", self.network]).stdout)[0]["Containers"]
        if attached:
            raise RuntimeError("team member requires an empty, dedicated bridge")
        if os.environ.get("OPENCLAW_EXTRA_PLUGINS", "").strip():
            raise ValueError("Phase 3 requires the pinned tools without extra OpenClaw plugins")
        self.relay.token = self._auth = secrets.token_urlsafe(32)
        self._team_session_id = uuid.uuid4().hex
        self._team_task = (task_run_id, task_id, agent_id)
        self._history = []
        self._turn = 0
        self._boot()

    def continue_task_session(self, prompt, inject, *, out_dir):
        if not getattr(self, "_team_session_id", None) or not self.container:
            raise RuntimeError("open a team session before continuing it")
        run_id, task_id, agent_id = self._team_task
        self._turn += 1
        self.scope = Scope(f"{run_id}:{agent_id}:{self._turn}", agent_id, task_id)
        self._task_dir = Path(out_dir)
        self._task_dir.mkdir(parents=True, exist_ok=False)
        injection = self._write_skills(inject, self._task_dir)
        session_id = self._team_session_id
        infrastructure_error = None
        status, response = None, {}
        try:
            status, response = self._http(
                "POST", "/prompt", {"session_id": session_id, "prompt": prompt, "create_new_session": False},
                timeout=self._timeout,
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, TimeoutError):
                status, response = 504, {"status": "task_timeout"}
            else:
                infrastructure_error = exc
        try:
            delta, transcript_status = self._transcript_delta(session_id)
        except Exception as exc:
            infrastructure_error = infrastructure_error or exc
            delta, transcript_status = [], "capture_error"
        messages = [record["message"] for record in delta if record.get("type") == "message"]
        evidence = execution_evidence([{"message": message} for message in messages])
        # Only the transcript can supply assistant output. Keep the POST body for audit.
        raw = self._history_to_raw(delta, "", session_id)
        raw["prompt_response"] = response
        raw["_skill_injection"] = injection
        # Keep results from this turn in the member's own learning trajectory.
        raw["messages"] = []
        for message in messages:
            role = message.get("role")
            call_id = message.get("toolCallId") or message.get("tool_call_id")
            if role in {"tool", "toolResult", "tool_result"} and (call_id or message.get("content")):
                raw["messages"].append({
                    "role": "tool", "content": message.get("content"),
                    "tool_call_id": call_id, "is_error": message.get("isError"),
                })
            elif role == "assistant":
                parsed = self._history_to_raw([{"type": "message", "message": message}], "", session_id)
                raw["messages"].extend(parsed["assistant_messages"] + parsed["tool_calls"])
        terminal = evidence["terminal_stop_reason"]
        terminal_message = messages[-1] if messages and messages[-1].get("role") == "assistant" else None
        raw["final_text"] = self._history_to_raw(
            [{"type": "message", "message": terminal_message}] if terminal_message else [], "", session_id,
        )["final_text"]
        timed_out = response == {"status": "task_timeout"} or status in {408, 504}
        if timed_out:
            stop_reason = "task_timeout"
        elif infrastructure_error is not None:
            stop_reason = "infrastructure_error"
        elif status != 200:
            stop_reason = f"http_{status}"
        elif transcript_status != "captured":
            stop_reason = transcript_status
        elif terminal_message is None or not isinstance(terminal, str) or not terminal:
            stop_reason = "missing_terminal"
        elif terminal in NORMAL_STOP_REASONS and not raw["final_text"]:
            stop_reason = "missing_terminal_output"
        else:
            stop_reason = terminal
        completed = stop_reason in NORMAL_STOP_REASONS
        raw["_team_execution"] = {
            **evidence, "execution_status": "completed" if completed else "timeout" if timed_out else "failed",
            "transcript_status": transcript_status, "stop_reason": stop_reason, "http_status": status,
            "evidence_completeness": "complete" if completed else "partial" if raw["messages"] else "none",
        }
        raw["_container"] = {"error": None if completed else f"OpenClaw turn stopped: {stop_reason}"}
        atomic_json(self._task_dir / "trajectory_raw.json", raw)
        trajectory = _normalize.normalize(raw)
        # The one-shot normalizer falls back to an earlier assistant message.
        trajectory.final_text = raw["final_text"]
        if infrastructure_error is not None:
            raise SessionTurnError(trajectory, infrastructure_error) from infrastructure_error
        return trajectory

    def _transcript_delta(self, session_id):
        try:
            status, data = self._http("GET", f"/sessions/{session_id}/history", timeout=60)
        except ValueError:
            # The Stage 7 capture adapter reports malformed/archived transcripts here.
            return [], "transcript_unavailable"
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("history"), list):
            return [], "transcript_unavailable"
        history = data["history"]
        if history[:len(self._history)] != self._history:
            return [], "transcript_session_mismatch"
        delta = history[len(self._history):]
        if not delta:
            return [], "transcript_empty"
        valid = [record for record in delta if self._valid_transcript_record(record)]
        if len(valid) != len(delta):
            return valid, "transcript_malformed"
        self._history = history
        return delta, "captured"

    @staticmethod
    def _valid_transcript_record(record):
        if not isinstance(record, dict):
            return False
        if record.get("type") != "message":
            return True
        message = record.get("message")
        return isinstance(message, dict) and isinstance(message.get("content"), (str, list, type(None)))

    def close_task_session(self):
        try:
            if self.container:
                self._docker(["rm", "-f", self.container], timeout=60, check=True)
                self.container = None
        finally:
            self._team_session_id = None
            self._history = []
            self.scope = None

    def teardown(self):
        self.close_task_session()


@contextmanager
def isolated_openclaw_factory(calls):
    """Each member has a separate internal bridge, firewall and scoped relay."""
    with ExitStack() as resources:
        def build(harness, backbone):
            if harness != "openclaw":
                raise ValueError("Phase 3 runner factory supports only OpenClaw")
            if backbone != BACKBONE:
                raise ValueError(f"configure the team worker pool with backbone={BACKBONE!r}")
            network, relay = resources.enter_context(_pilot_relay(calls))
            runner = TeamOpenClawRunner(network, relay)
            resources.callback(runner.close_task_session)
            return runner

        yield build
