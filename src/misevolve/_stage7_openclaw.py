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
from threading import Thread
from urllib.parse import urlsplit

from ._stage7_api import INPUT_BYTES, LIMITS, ProviderCalls, ProviderError, Scope
from .adapters.openclaw_runner import OpenClawRunner
from .backbones import resolve_agent

BACKBONE = "minimax-m3.1-flash"
IMAGE = "misevolve-openclaw:stage7-2026.11.1"
OPENCLAW_VERSION = "2026.11.1"


class _Relay(ThreadingHTTPServer):
    def __init__(self, address, calls: ProviderCalls):
        self.calls = calls
        self.scope: Scope | None = None
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, _Handler)


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
        if urlsplit(self.path).path != "/anthropic/v1/messages" or self.server.scope is None:
            self._error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= INPUT_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("expected a request object")
            # The relay accepts only standard-tier calls to the fixed model.
            payload.pop("service_tier", None)
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": "low"}
            body, content_type = self.server.calls.request(self.server.scope, "openclaw", payload)
        except (ValueError, TypeError):
            self._error(400)
            return
        except ProviderError as exc:
            self._error(exc.status or 400)
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
    def __init__(self, network: str, relay: _Relay):
        address, port = relay.server_address
        config = replace(resolve_agent("openclaw", BACKBONE), image=IMAGE, base_url=f"http://{address}:{port}/anthropic")
        super().__init__(config)
        self.network = network
        self.relay = relay
        self._timeout = min(self._timeout, 150)

    def isolate(self, episode_dir: Path) -> None:
        self._episode_dir = Path(episode_dir)
        self._auth = self.relay.token
        self.container = None

    def _container_options(self) -> list[str]:
        return [
            "--network", self.network, "--cap-drop", "ALL", "--pids-limit", "256",
            "--security-opt", "no-new-privileges", "-e", "MISEVOLVE_OPENCLAW_API=anthropic-messages",
            "-e", "MISEVOLVE_OPENCLAW_CONTEXT=32768", "-e", f"MISEVOLVE_OPENCLAW_OUTPUT={LIMITS['openclaw'][1]}",
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

    def run(self, prompt, inject, *, out_dir):
        try:
            return super().run(prompt, inject, out_dir=out_dir)
        finally:
            self._rm_container()


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
