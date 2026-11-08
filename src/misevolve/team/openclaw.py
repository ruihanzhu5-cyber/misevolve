"""Opt-in multi-turn adapter for the pinned Stage 7 OpenClaw image."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from misevolve._stage7_api import Scope
from misevolve._stage7_openclaw import BACKBONE
from misevolve.adapters import _normalize
from misevolve.multiagent._io import atomic_json
from misevolve.stage7_pilot import PilotRunner, _pilot_relay


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
        status, response = self._http(
            "POST", "/prompt", {"session_id": session_id, "prompt": prompt, "create_new_session": False},
            timeout=self._timeout,
        )
        history_status, data = self._http("GET", f"/sessions/{session_id}/history", timeout=60)
        if history_status != 200 or not isinstance(data, dict) or not isinstance(data.get("history"), list):
            raise ValueError("missing OpenClaw team transcript")
        history = data["history"]
        if history[:len(self._history)] != self._history or len(history) <= len(self._history):
            raise ValueError("OpenClaw did not append to the same team session transcript")
        delta = history[len(self._history):]
        self._history = history
        raw = self._history_to_raw(delta, response, session_id)
        raw["_skill_injection"] = injection
        raw["_container"] = {"error": None if status == 200 else f"OpenClaw prompt returned HTTP {status}"}
        # Keep results from this turn in the member's own learning trajectory.
        raw["messages"] = []
        for record in delta:
            message = record.get("message") or {}
            role = message.get("role")
            if role in {"tool", "toolResult", "tool_result"}:
                raw["messages"].append({"role": "tool", "content": message.get("content")})
            elif role == "assistant":
                parsed = self._history_to_raw([record], "", session_id)
                raw["messages"].extend(parsed["assistant_messages"] + parsed["tool_calls"])
        atomic_json(self._task_dir / "trajectory_raw.json", raw)
        return _normalize.normalize(raw)

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
