"""A disposable OpenClaw network and a model-only relay for the Stage 7 executor."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from urllib.parse import urlsplit

from ._stage7_api import (
    OPENCLAW_CONTEXT,
    OPENCLAW_INPUT_BYTES,
    OPENCLAW_OUTPUT,
    ProviderCalls,
    ProviderError,
    Scope,
)
from .adapters.openclaw_runner import OpenClawRunner
from .backbones import resolve_agent
from .multiagent._io import read_jsonl

BACKBONE = "minimax-m3.1-flash"
IMAGE = "misevolve-openclaw:stage7-2026.11.1"
OPENCLAW_VERSION = "2026.11.1"
NORMAL_STOP_REASONS = ("stop", "end_turn", "stop_sequence")
_NATIVE_CORRECTION = "The previous assistant turn's tool call was rejected before it ran"
_STAGE7_CORRECTION = "Stage 7 tool argument correction."
_MALFORMED_ARGUMENTS = "Provider completed tool call with malformed JSON arguments"
_TOOL_CALL_TYPES = {"toolCall", "tool_use", "tool_call", "toolUse"}


class _Relay(ThreadingHTTPServer):
    # Drain in-flight provider calls so the final report can count their attempts.
    daemon_threads = False

    def __init__(self, address, calls: ProviderCalls):
        self.calls = calls
        self.scope: Scope | None = None
        self.pending_correction: Scope | None = None
        self._correction_lock = Lock()
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, _Handler)

    def take_correction(self, scope):
        with self._correction_lock:
            if self.pending_correction == scope:
                self.pending_correction = None
                return "stage7"
            return None

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status):
        body = b'{"type":"error","error":{"type":"api_error","message":"Stage 7 relay rejected the request"}}'
        self._respond(status, body, "application/json")

    def do_POST(self):
        token = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii")):
            self._error(403)
            return
        scope = self.server.scope
        if urlsplit(self.path).path != "/anthropic/v1/messages" or scope is None:
            self._error(404)
            return
        fence = scope.execution
        if fence is not None and not fence.open():
            self.close_connection = True
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= OPENCLAW_INPUT_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("expected a request object")
            # The relay accepts only standard-tier calls to the fixed model.
            payload.pop("service_tier", None)
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": "low"}
            correction = self.server.take_correction(scope)
            body, content_type = self.server.calls.request(scope, "openclaw", payload, correction=correction)
        except (ValueError, TypeError, AttributeError):
            self._error(400)
            return
        except ProviderError as exc:
            if fence is not None:
                if not fence.open():
                    self.close_connection = True
                    return
                fence.provider_failure = exc.category
            self._error(exc.status or 400)
            return
        if fence is not None and (not fence.open() or self.server.scope is not scope):
            self.close_connection = True
            return
        self._respond(200, body, content_type)


@contextmanager
def isolated_relay(calls: ProviderCalls):
    name = f"misevolve-stage7-{uuid.uuid4().hex[:10]}"
    docker = OpenClawRunner._docker
    docker(["network", "create", "--internal", name])
    try:
        result = docker(["network", "inspect", name])
        config = json.loads(result.stdout)[0]
        if config["Internal"] is not True:
            raise ValueError("Stage 7 requires an internal Docker bridge")
        gateway = config["IPAM"]["Config"][0]["Gateway"]
        with _Relay((gateway, 0), calls) as relay:
            thread = Thread(target=relay.serve_forever, daemon=True)
            thread.start()
            try:
                yield name, relay
            finally:
                relay.shutdown()
                thread.join()
    finally:
        docker(["network", "rm", name], check=False)


class Stage7Runner(OpenClawRunner):
    gateway_timeout_seconds = 0

    def __init__(self, network: str, relay: _Relay):
        address, port = relay.server_address
        config = replace(resolve_agent("openclaw", BACKBONE), image=IMAGE, base_url=f"http://{address}:{port}/anthropic")
        super().__init__(config)
        self.network = network
        self.relay = relay
        self.scope: Scope | None = None
        self._timeout = None
        self.model_config: dict = {}

    def isolate(self, episode_dir: Path) -> None:
        self._episode_dir = Path(episode_dir)
        self._auth = self.relay.token
        self.container = None

    def _container_options(self) -> list[str]:
        return [
            "--network", self.network, "--cap-drop", "ALL", "--pids-limit", "256",
            "--security-opt", "no-new-privileges", "-e", "MISEVOLVE_OPENCLAW_API=anthropic-messages",
            "-e", f"MISEVOLVE_OPENCLAW_CONTEXT={OPENCLAW_CONTEXT}",
            "-e", f"MISEVOLVE_OPENCLAW_OUTPUT={OPENCLAW_OUTPUT}",
            "-e", "MISEVOLVE_OPENCLAW_PROMPT_ATTEMPTS=1", "-e", "MISEVOLVE_OPENCLAW_STAGE7=1",
        ]

    def _container_endpoint(self, name: str) -> str:
        result = self._docker(["inspect", "--format", "{{json .NetworkSettings.Networks}}", name])
        networks = json.loads(result.stdout)
        if set(networks) != {self.network}:
            raise ValueError("Stage 7 container is connected to an unexpected network")
        address = ipaddress.IPv4Address(networks[self.network]["IPAddress"])
        return f"http://{address}:8000"

    def _wait_healthy(self) -> None:
        super()._wait_healthy()
        # The HTTP sidecar can be ready before the pinned OpenClaw Gateway listens.
        probe = """import socket
import time
for attempt in range(45):
    try:
        with socket.create_connection(("127.0.0.1", 18789), timeout=1):
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit("OpenClaw Gateway did not begin listening")
"""
        self._docker(["exec", self.container, "python3", "-c", probe], timeout=95)
        # The pinned agent CLI reads this value and forwards it to the Gateway.
        config = ["exec", self.container, "openclaw", "config"]
        key = "agents.defaults.timeoutSeconds"
        self._docker([*config, "set", key, str(self.gateway_timeout_seconds), "--strict-json"])
        result = self._docker([*config, "get", key, "--json"])
        value = json.loads(result.stdout)
        if type(value) is not int or value != self.gateway_timeout_seconds:
            raise ValueError("OpenClaw did not confirm the configured task deadline")
        # The Anthropic transport otherwise defaults to at most 32,000 output tokens.
        self._docker([*config, "set", "agents.defaults.params.maxTokens", str(OPENCLAW_OUTPUT), "--strict-json"])
        inspect_model = """import json
import os
from pathlib import Path
cfg = json.loads((Path.home() / ".openclaw/openclaw.json").read_text())
provider = cfg["models"]["providers"]["backbone"]
model, = provider["models"]
print(json.dumps({
    "model": model["id"], "api": provider["api"],
    "context_window": model["contextWindow"], "max_tokens": model["maxTokens"],
    "request_max_tokens": cfg["agents"]["defaults"]["params"]["maxTokens"],
    "env_context_window": int(os.environ["MISEVOLVE_OPENCLAW_CONTEXT"]),
    "env_max_tokens": int(os.environ["MISEVOLVE_OPENCLAW_OUTPUT"]),
}))
"""
        result = self._docker(["exec", self.container, "python3", "-c", inspect_model])
        expected = {
            "model": self.cfg.backbone, "api": "anthropic-messages",
            "context_window": OPENCLAW_CONTEXT, "max_tokens": OPENCLAW_OUTPUT,
            "request_max_tokens": OPENCLAW_OUTPUT,
            "env_context_window": OPENCLAW_CONTEXT, "env_max_tokens": OPENCLAW_OUTPUT,
        }
        self.model_config = json.loads(result.stdout)
        if self.model_config != expected:
            raise ValueError("OpenClaw container model configuration did not match Stage 7")

    def _http(self, method, path, body=None, *, timeout):
        if method != "POST" or path != "/prompt":
            return super()._http(method, path, body, timeout=timeout)
        if self.scope is None:
            raise ValueError("Stage 7 prompt has no task scope")
        deadline = monotonic() + timeout if timeout is not None else None
        self.relay.scope = self.scope
        try:
            response = super()._http(method, path, body, timeout=timeout)
            if deadline is not None and response[0] != 200:
                return response
            if self.scope.execution is not None and not self.scope.execution.open():
                raise TimeoutError("OpenClaw task deadline reached before tool correction")
            correction = self._tool_correction(body["session_id"])
            if correction:
                # Keep the same session, workspace, and settled tool results.
                remaining = deadline - monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("OpenClaw task deadline reached before tool correction")
                self.relay.pending_correction = self.scope
                response = super()._http(method, path, {**body, "prompt": correction}, timeout=remaining)
            return response
        finally:
            self.relay.pending_correction = None
            self.relay.scope = None

    def _tool_correction(self, session_id):
        attempts = [row for row in read_jsonl(self.relay.calls.path)
                    if row["task_id"] == self.scope.task_id and row["module"] == "openclaw"]
        if not attempts or attempts[-1]["status"] != "ok":
            return None
        stream = attempts[-1].get("stream") or {}
        if not stream.get("message_stop") or not any(block["argument_error"] for block in stream["tool_blocks"]):
            return None
        fence = self.scope.execution
        remaining = min(60, fence.deadline - monotonic()) if fence is not None else 60
        if remaining <= 0:
            raise TimeoutError("OpenClaw task deadline reached before tool correction")
        code, data = self._http("GET", f"/sessions/{session_id}/history", timeout=remaining)
        if code != 200 or not isinstance(data, dict):
            return None
        return tool_correction(stream, data.get("history") or [])

    def run(self, prompt, inject, *, out_dir):
        self.model_config = {}
        try:
            return super().run(prompt, inject, out_dir=out_dir)
        finally:
            self._rm_container()


def _native_correction_count(messages):
    count = 0
    for message in messages:
        if message.get("role") != "user" or not message.get("provenance"):
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            block.get("text", "") for block in content or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        count += any(isinstance(text, str) and text.startswith(_NATIVE_CORRECTION) for text in texts)
    return count


def tool_correction(stream, history):
    messages = [item.get("message") or {} for item in history]
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant or assistant[-1].get("stopReason") != "error" or _native_correction_count(messages):
        return None
    terminal = assistant[-1]
    if (terminal.get("errorCode") != "malformed_tool_call_arguments"
            and terminal.get("errorMessage") != _MALFORMED_ARGUMENTS):
        return None
    calls = {block["id"] for message in assistant for block in message.get("content", [])
             if isinstance(block, dict) and block.get("type") in _TOOL_CALL_TYPES and block.get("id")}
    results = {message.get("toolCallId") or message.get("tool_call_id")
               for message in messages if message.get("role") in ("toolResult", "tool")}
    if calls - results or any(isinstance(block, dict) and block.get("type") in _TOOL_CALL_TYPES
                              for block in terminal.get("content", [])):
        return None
    invalid = [block for block in stream["tool_blocks"] if block["argument_error"]]
    names = ", ".join(sorted({block["name"] for block in invalid}))
    reason = ("the response reached its output limit before the tool arguments finished"
              if stream["stop_reason"] == "max_tokens" else "the tool arguments were not a complete JSON object")
    return (
        f"{_STAGE7_CORRECTION} OpenClaw rejected the {names} tool call before dispatch because {reason}. "
        "Regenerate only the needed call with a complete JSON object matching the tool's declared fields. "
        "Split a long operation into smaller tool calls if necessary. Preserve all completed tool results, "
        "do not repeat completed calls or restart the task, and continue from the current state."
    )


def execution_evidence(history):
    messages = [item.get("message") or {} for item in history]
    assistant = [message for message in messages if message.get("role") == "assistant"]
    terminal = assistant[-1] if assistant else {}
    return {
        "terminal_stop_reason": terminal.get("stopReason"),
        "native_tool_argument_corrections": _native_correction_count(messages),
        "tool_calls": sum(isinstance(block, dict) and block.get("type") in _TOOL_CALL_TYPES
                          for message in assistant for block in (message.get("content") or [])),
        "tool_results": sum(message.get("role") in ("toolResult", "tool") for message in messages),
    }


def tool_round_trip(history: list[dict], *, marker: str | None = None) -> bool:
    pending = set()
    result_seen = False
    for item in history:
        message = item.get("message") or {}
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        if message.get("role") in ("toolResult", "tool"):
            result_seen |= (
                (message.get("toolCallId") or message.get("tool_call_id")) in pending
                and message.get("isError") is not True
                and (marker is None or any(marker in str(block.get("text") or "") for block in blocks))
            )
        if message.get("role") == "assistant":
            texts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
            if result_seen and any(
                marker in value if marker else bool(value) for value in texts
            ):
                return True
            pending.update(block["id"] for block in blocks
                           if block.get("type") in ("toolCall", "tool_use", "tool_call") and block.get("id"))
    return False
