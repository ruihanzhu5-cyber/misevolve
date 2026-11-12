"""Opt-in multi-turn adapter for the pinned Stage 7 OpenClaw image."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic
from urllib.error import URLError

from misevolve._stage7_api import ExecutionFence, Scope
from misevolve._stage7_openclaw import BACKBONE, NORMAL_STOP_REASONS, execution_evidence
from misevolve.adapters import _normalize
from misevolve.adapters.openclaw_runner import _SKILL_ROOT
from misevolve.multiagent._io import atomic_json
from misevolve.stage7_pilot import _READ_TRANSCRIPT as _NATIVE_TRANSCRIPT_READER
from misevolve.stage7_pilot import HISTORY_BYTES, TASK_TIMEOUT, PilotRunner, _decode_history, _pilot_relay

from .executor import SessionTurnError
from .handoff import read_workspace_artifact, write_workspace_artifact

TOOL_TIMEOUT_SECONDS = 120
TOOL_YIELD_MS = 5000
FREEZE_SECONDS = 15
SNAPSHOT_SECONDS = 45
REMOVE_SECONDS = 30
_AGENT_DB = "/root/.openclaw/agents/main/agent/openclaw-agent.sqlite"
_TOOLS = {"allow": ["exec", "process", "read", "write", "edit"],
          "exec": {"timeoutSeconds": TOOL_TIMEOUT_SECONDS, "backgroundMs": TOOL_YIELD_MS}}


def _runtime_skill_map(manifest):
    if not isinstance(manifest, dict) or manifest.get("status") != "success":
        return []
    return [{**item, "runtime_path": f"{_SKILL_ROOT}/{item['runtime_directory_slug']}/SKILL.md"}
            for item in manifest["skills"]]


def _skill_reads(messages, calls, mapping, session_id):
    """Record matched read-tool results, never infer semantic adoption or derivation."""
    paths = {item["runtime_path"]: item for item in mapping}
    results = {}
    for message in messages:
        if message.get("role") in {"tool", "toolResult", "tool_result"}:
            call_id = message.get("toolCallId") or message.get("tool_call_id")
            results.setdefault(call_id, []).append(message)
    reads = []
    for call in calls:
        args = call.get("input")
        matches = results.get(call.get("id"), [])
        if call.get("name") != "read" or not isinstance(args, dict) or len(matches) != 1:
            continue
        result = matches[0]
        if result.get("isError") is not False or not result.get("content"):
            continue
        path = args.get("path") or args.get("file_path")
        if not isinstance(path, str) or ".." in PurePosixPath(path).parts:
            continue
        item = paths.get(str(PurePosixPath(path.replace("~/", "/root/", 1))))
        if item is not None:
            reads.append({"local_skill_id": item["local_skill_id"], "runtime_path": item["runtime_path"],
                          "source_ref": f"{session_id}:{call['id']}", "adoption_status": "unknown",
                          "content_identity_verified": False})
    return reads


def _is_exec_notice(message):
    provenance = message.get("provenance")
    envelope = message.get("__openclaw")
    event_key = message.get("idempotencyKey")
    return (
        message.get("role") == "user"
        and isinstance(provenance, dict)
        and provenance.get("kind") == "internal_system"
        and provenance.get("sourceTool") == "exec"
        and isinstance(envelope, dict)
        and envelope.get("senderIsOwner") is False
        and isinstance(event_key, str)
        and event_key.startswith("exec:")
    )


class TeamOpenClawRunner(PilotRunner):
    task_timeout_seconds = TASK_TIMEOUT

    def _wait_healthy(self):
        super()._wait_healthy()
        config = ["exec", self.container, "openclaw", "config"]
        self._docker([*config, "set", "tools", json.dumps(_TOOLS), "--strict-json"])
        observed = json.loads(self._docker([*config, "get", "tools", "--json"]).stdout)
        if observed != _TOOLS:
            raise RuntimeError("OpenClaw did not confirm the Phase 3 process settings")

    def bind_task_deadline(self, deadline):
        self._task_deadline = deadline

    def read_artifact(self, relative_path):
        if not self.container or not getattr(self, "_team_session_id", None):
            raise RuntimeError("artifact source session is not open")
        return read_workspace_artifact(self.container, relative_path)

    def receive_artifact(self, delivery_id, relative_path, data):
        if not self.container or not getattr(self, "_team_session_id", None):
            raise RuntimeError("artifact target session is not open")
        return write_workspace_artifact(self.container, delivery_id, relative_path, data)

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
        self._task_deadline = None
        self._frozen = False
        self._boot()

    def continue_task_session(self, prompt, inject, *, out_dir):
        if not getattr(self, "_team_session_id", None) or not self.container:
            raise RuntimeError("open a team session before continuing it")
        run_id, task_id, agent_id = self._team_task
        self._turn += 1
        if self._task_deadline is None:
            self._task_deadline = monotonic() + self.task_timeout_seconds
        fence = ExecutionFence(self._task_deadline)
        self.scope = Scope(f"{run_id}:{agent_id}:{self._turn}", agent_id, task_id, fence)
        self._task_dir = Path(out_dir)
        self._task_dir.mkdir(parents=True, exist_ok=False)
        injection = None
        session_id = self._team_session_id
        infrastructure_error = None
        status, response = None, {}
        watchdog = None
        cutoff = {}

        def freeze_at_deadline():
            cutoff["in_flight"] = fence.close()
            try:
                self._docker(["pause", self.container], timeout=FREEZE_SECONDS)
                self._frozen = True
            except Exception as exc:
                cutoff["freeze_error"] = exc

        try:
            if not fence.open():
                raise TimeoutError("task deadline before Skill injection")
            injection = self._write_skills(inject, self._task_dir)
            mapping = _runtime_skill_map(injection)
            if mapping:
                prompt += (
                    "\nInstalled Skill files (local ID -> exact readable path): " + json.dumps(mapping) +
                    "\nRead these paths with the read tool. A Local Skill ID is not a filename or CLI command. "
                    "Use its local_skill_id for sharing; do not guess ID/name associations."
                )
            remaining = self._task_deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("task deadline before model dispatch")
            watchdog = Timer(remaining, freeze_at_deadline)
            watchdog.start()
            status, response = self._http(
                "POST", "/prompt", {"session_id": session_id, "prompt": prompt, "create_new_session": False},
                timeout=remaining,
            )
            if monotonic() >= self._task_deadline:
                status, response = 504, {"status": "task_timeout"}
        except Exception as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, TimeoutError):
                status, response = 504, {"status": "task_timeout"}
            else:
                infrastructure_error = exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()
            in_flight = fence.close()
        if "in_flight" in cutoff:
            in_flight = cutoff["in_flight"]
            status, response = 504, {"status": "task_timeout"}
        timed_out = response == {"status": "task_timeout"} or status in {408, 504}
        containment = "normal"
        try:
            if timed_out or status != 200 or infrastructure_error is not None:
                # Do not read the live SQLite database or resume the task process after cutoff.
                containment = "freeze_failed"
                if "freeze_error" in cutoff:
                    raise cutoff["freeze_error"]
                if not self._frozen:
                    self._docker(["pause", self.container], timeout=FREEZE_SECONDS)
                    self._frozen = True
                containment = "paused_before_snapshot"
            delta, transcript_status = self._transcript_delta(session_id)
        except Exception as exc:
            infrastructure_error = infrastructure_error or exc
            delta, transcript_status = [], "capture_error"
        finally:
            if containment != "normal":
                try:
                    self._remove_task_container()
                    containment += "_then_removed"
                except Exception as exc:
                    infrastructure_error = exc
                    containment += "_removal_unconfirmed"
        messages = [record["message"] for record in delta if record.get("type") == "message"]
        evidence = execution_evidence([{"message": message} for message in messages])
        # Only the transcript can supply assistant output. Keep the POST body for audit.
        raw = self._history_to_raw(delta, "", session_id)
        raw["prompt_response"] = response
        raw["_skill_injection"] = injection
        raw["_skill_runtime_map"] = _runtime_skill_map(injection)
        raw["_skill_reads"] = _skill_reads(messages, raw["tool_calls"], raw["_skill_runtime_map"], session_id)
        # Keep results from this turn in the member's own learning trajectory.
        raw["messages"] = []
        tool_timeouts = []
        tool_result_ids = set()
        for message in messages:
            role = message.get("role")
            call_id = message.get("toolCallId") or message.get("tool_call_id")
            if role in {"tool", "toolResult", "tool_result"} and (call_id or message.get("content")):
                if isinstance(call_id, str) and call_id:
                    tool_result_ids.add(call_id)
                details = message.get("details")
                details = details if isinstance(details, dict) else {}
                if details.get("timedOut") is True:
                    tool_timeouts.append(call_id)
                raw["messages"].append({
                    "role": "tool", "content": message.get("content"),
                    "tool_call_id": call_id,
                    "is_error": True if details.get("timedOut") is True else message.get("isError"),
                })
            elif role == "assistant":
                parsed = self._history_to_raw([{"type": "message", "message": message}], "", session_id)
                raw["messages"].extend(parsed["assistant_messages"] + parsed["tool_calls"])
        unpaired = sorted({call["id"] for call in raw["tool_calls"]
                           if isinstance(call.get("id"), str) and call["id"]} - tool_result_ids)
        terminal = evidence["terminal_stop_reason"]
        # OpenClaw can append a background exec notice after the assistant finishes.
        last_message = next((message for message in reversed(messages) if not _is_exec_notice(message)), None)
        terminal_message = last_message if last_message and last_message.get("role") == "assistant" else None
        raw["final_text"] = self._history_to_raw(
            [{"type": "message", "message": terminal_message}] if terminal_message else [], "", session_id,
        )["final_text"]
        provider_timeout = fence.provider_failure in {"timeout", "provider_recovery_timeout"}
        if infrastructure_error is not None:
            stop_reason = "infrastructure_error"
        elif timed_out:
            stop_reason = "task_timeout"
        elif provider_timeout:
            stop_reason = ("provider_recovery_timeout" if fence.provider_failure == "provider_recovery_timeout"
                           else "model_request_timeout")
        elif status != 200:
            detail = response.get("detail") if isinstance(response, dict) else None
            stop_reason = ("executor_process_exit" if isinstance(detail, dict)
                           and detail.get("error") == "openclaw_process_exit" else f"http_{status}")
        elif transcript_status != "captured":
            stop_reason = transcript_status
        elif terminal_message is None or not isinstance(terminal, str) or not terminal:
            stop_reason = "missing_terminal"
        elif terminal in NORMAL_STOP_REASONS and not raw["final_text"]:
            stop_reason = "missing_terminal_output"
        elif terminal in NORMAL_STOP_REASONS and unpaired:
            stop_reason = "unpaired_tool_calls"
        else:
            stop_reason = terminal
        if tool_timeouts and stop_reason in {"error", "toolUse", "tool_use", "missing_terminal"}:
            stop_reason = "tool_timeout"
        completed = stop_reason in NORMAL_STOP_REASONS
        is_timeout = stop_reason in {"task_timeout", "model_request_timeout", "provider_recovery_timeout", "tool_timeout"}
        raw["_team_execution"] = {
            **evidence, "execution_status": "completed" if completed else "timeout" if is_timeout else "failed",
            "transcript_status": transcript_status, "stop_reason": stop_reason, "http_status": status,
            "evidence_completeness": "complete" if completed else "partial" if raw["messages"] else "none",
            "cutoff": {"container": containment, "in_flight_attempt_ids": in_flight,
                       "remote_cancel_supported": False},
            "task_deadline_exhausted": timed_out, "provider_failure": fence.provider_failure,
            "tool_timeout_call_ids": tool_timeouts, "unpaired_tool_call_ids": unpaired,
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
            if self._frozen:
                status, data = self._read_frozen_history(session_id)
            else:
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

    def _read_frozen_history(self, session_id):
        deadline = monotonic() + SNAPSHOT_SECONDS
        with TemporaryDirectory(prefix="misevolve-transcript-") as directory:
            for suffix in ("", "-wal", "-shm"):
                result = self._docker(["cp", f"{self.container}:{_AGENT_DB}{suffix}", directory],
                                      timeout=max(.01, deadline - monotonic()), check=False)
                if result.returncode:
                    if "Could not find the file" not in result.stderr:
                        raise RuntimeError("Docker could not copy the paused transcript")
                    if not suffix:
                        return 404, {}
            reader = _NATIVE_TRANSCRIPT_READER.replace(
                'homedir() + "/.openclaw/agents/main/agent/openclaw-agent.sqlite"',
                '"/snapshot/openclaw-agent.sqlite"',
            )
            name = f"misevolve-frozen-reader-{uuid.uuid4().hex[:12]}"
            try:
                result = self._docker([
                    "run", "--rm", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--memory", "512m", "--pids-limit", "64", "--security-opt", "no-new-privileges",
                    "--mount", f"type=bind,source={directory},target=/snapshot",
                    "--entrypoint", "node", self.cfg.image, "-e", reader,
                    f"agent:main:stage7-{session_id}", str(HISTORY_BYTES),
                ], timeout=max(.01, deadline - monotonic()), check=False)
            except TimeoutExpired:
                self._docker(["rm", "-f", name], timeout=REMOVE_SECONDS, check=False)
                raise
            if result.returncode:
                raise ValueError("frozen transcript unavailable")
            (self._task_dir / "session_raw.jsonl").write_text(result.stdout, encoding="utf-8", newline="\n")
            return 200, {"history": _decode_history(result.stdout)}

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
            self._remove_task_container()
        finally:
            self._team_session_id = None
            self._history = []
            self.scope = None

    def _remove_task_container(self):
        if self.container:
            result = self._docker(["rm", "-f", self.container], timeout=REMOVE_SECONDS, check=False)
            if result.returncode and f"No such container: {self.container}" not in result.stderr:
                raise RuntimeError("could not confirm task container removal")
            self.container = None

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
