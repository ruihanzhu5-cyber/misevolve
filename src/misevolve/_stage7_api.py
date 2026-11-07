"""Bounded provider calls and metadata-only accounting for the Stage 7 smoke."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from threading import Lock

from .multiagent._io import append_jsonl, read_jsonl

MINIMAX_MODEL = "MiniMax-M3.1-Flash"
MINIMAX_URL = "https://api.minimax.cn/anthropic/v1/messages"
MINIMAX_PRICE = "https://platform.minimax.cn/docs/pricing/overview"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_PRICE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICE_DATE = "2026-11-07"
INPUT_BYTES = 256_000
BEHAVIOR_INPUT_BYTES = 2_000_000
OPENCLAW_INPUT_BYTES = 8 * 1024 * 1024
OPENCLAW_STREAM_BYTES = 128 * 1024 * 1024
OPENCLAW_CONTEXT = 204800
OPENCLAW_OUTPUT = 131072
REQUEST_TIMEOUT = 45
RETRYABLE_TRANSPORT = {"timeout", "transport_error"}
LIMITS = {
    # Per task and module. The pair gives logical calls and maximum output tokens per call.
    "openclaw": (None, OPENCLAW_OUTPUT),
    "extractor": (2, 4096),
    "maintainer": (4, 4096),
    "behavior_judge": (1, 768),
    "artifact_judge": (4, 768),
}


@dataclass(frozen=True)
class Scope:
    task_id: str
    worker_id: str
    instance_id: str | None = None


class ProviderError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _stream_events(body):
    for line in BytesIO(body):
        if line.startswith(b"data:") and (value := line[5:].strip()) not in (b"", b"[DONE]"):
            yield json.loads(value)


def _tool_stream_evidence(block, fragments, closed):
    reason = None
    error_offset = None
    if not closed:
        reason = "missing_block_stop"
    else:
        try:
            args = json.loads("".join(fragments)) if fragments else block.get("input")
            if not isinstance(args, dict):
                reason = "expected_json_object"
        except json.JSONDecodeError as exc:
            reason, error_offset = "invalid_json", exc.pos
    tool_id = block.get("id")
    tool_name = block.get("name")
    return {
        "id_hash": sha256(tool_id.encode()).hexdigest() if isinstance(tool_id, str) else None,
        "name": tool_name if tool_name in ("exec", "read", "write", "edit") else "unknown",
        "fragments": len(fragments),
        "argument_bytes": sum(len(part.encode("utf-8")) for part in fragments),
        "argument_error": reason, "json_error_offset": error_offset,
    }


def _stream_usage(body, details):
    types = set()
    counts = {}
    model = None
    blocks = {}
    closed = set()
    stop_reason = None
    for event in _stream_events(body):
        kind = event.get("type")
        types.add(kind)
        counts.update(event.get("usage") or {})
        if kind == "message_start":
            message = event.get("message", {})
            counts.update(message.get("usage") or {})
            model = message.get("model")
        if details is None:
            continue
        index = event.get("index")
        if kind == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
            blocks[index] = (event["content_block"], [])
        if kind == "content_block_delta" and index in blocks:
            delta = event.get("delta", {})
            if delta.get("type") == "input_json_delta":
                blocks[index][1].append(delta["partial_json"])
        if kind == "content_block_stop":
            closed.add(index)
        if kind == "message_delta":
            stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason
    if details is not None:
        details.update({
            "message_stop": "message_stop" in types,
            "stop_reason": stop_reason if stop_reason in (
                "end_turn", "max_tokens", "tool_use", "stop_sequence", "pause_turn", "refusal",
            ) else None,
            "tool_blocks": [_tool_stream_evidence(block, parts, index in closed)
                            for index, (block, parts) in blocks.items()] if "message_stop" in types else [],
        })
    if "message_stop" not in types or "error" in types:
        raise ProviderError("incomplete provider stream")
    return counts, model


def _usage(body: bytes, *, streaming: bool, deepseek: bool, details=None) -> tuple[dict, str | None]:
    if streaming:
        counts, response_model = _stream_usage(body, details)
    else:
        message = json.loads(body)
        if message.get("error") or (message.get("base_resp") or {}).get("status_code", 0):
            raise ProviderError("provider returned an error")
        counts = message.get("usage") or {}
        response_model = message.get("model")
    if deepseek:
        return {
            "input_tokens": _number(counts.get("prompt_tokens")),
            "output_tokens": _number(counts.get("completion_tokens")),
            "cache_read_tokens": _number(counts.get("prompt_cache_hit_tokens")),
            "cache_miss_tokens": _number(counts.get("prompt_cache_miss_tokens")),
            "cache_write_tokens": None,
        }, response_model
    return {
        "input_tokens": _number(counts.get("input_tokens")),
        "output_tokens": _number(counts.get("output_tokens")),
        "cache_read_tokens": _number(counts.get("cache_read_input_tokens")),
        "cache_write_tokens": _number(counts.get("cache_creation_input_tokens")),
        "cache_miss_tokens": None,
    }, response_model


def _estimate(usage: dict, *, deepseek: bool, peak: bool) -> float | None:
    inp, out, read, write, miss = (
        usage.get(key) for key in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens",
        )
    )
    if inp is None or out is None or read is None:
        return None
    if deepseek:
        if miss is None or inp != read + miss:
            return None
        multiplier = 2 if peak else 1
        return round((miss + 0.02 * read + 4 * out) * multiplier / 1_000_000, 9)
    # MiniMax prices new input, cache hits and output. A missing cache-write
    # counter in its streaming response does not remove any of those fields.
    if write not in (0, None):
        return None
    return round((inp + 0.1 * read + 4 * out) / 1_000_000, 9)


class ProviderCalls:
    def __init__(self, run_dir: Path, *, deepseek_peak: bool):
        self.path = run_dir / "model_calls.jsonl"
        self.run_id = run_dir.name
        self.deepseek_peak = deepseek_peak
        self._attempts = Counter()
        self._lock = Lock()
        self._auth_failed = False

    def check_credentials(self) -> None:
        missing = [name for name in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY") if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(f"missing credentials: {', '.join(missing)}")

    def request(self, scope: Scope, module: str, payload: dict, *, correction=None) -> tuple[bytes, str]:
        max_calls, max_output = LIMITS[module]
        executor = module == "openclaw"
        deepseek = module.endswith("_judge")
        model = DEEPSEEK_MODEL if deepseek else MINIMAX_MODEL
        provider = "deepseek" if deepseek else "minimax"
        if payload.get("model") != model:
            raise ProviderError("unexpected model")
        limit = payload.get("max_tokens", max_output)
        if type(limit) is not int or limit < 1 or (executor and limit > max_output):
            raise ProviderError("invalid max_tokens")
        payload = {**payload, "max_tokens": limit if executor else min(limit, max_output)}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        input_limits = {"openclaw": OPENCLAW_INPUT_BYTES, "behavior_judge": BEHAVIOR_INPUT_BYTES}
        input_limit = input_limits.get(module, INPUT_BYTES)
        response_limit = OPENCLAW_STREAM_BYTES if executor else 2_000_000
        if len(body) > input_limit:
            raise ProviderError("input exceeds Stage 7 request limit")
        key = os.environ[f"{provider.upper()}_API_KEY"].strip()
        url = DEEPSEEK_URL if deepseek else MINIMAX_URL
        request = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", **({} if deepseek else {"anthropic-version": "2023-06-01"}),
        })
        request.add_unredirected_header("Authorization", f"Bearer {key}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        call_id = uuid.uuid4().hex
        call_scope = (scope, module)
        with self._lock:
            if self._auth_failed:
                raise ProviderError("a provider authentication failure blocked further calls")
            if max_calls is not None:
                if self._attempts[call_scope] >= max_calls:
                    raise ProviderError("Stage 7 module call limit reached")
                self._attempts[call_scope] += 1
        for attempt in (1, 2):
            status = request_id = response_model = None
            error = None
            usage = {}
            stream = {} if executor else None
            elapsed = time.monotonic()
            start = datetime.now(UTC).isoformat()
            try:
                with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                    status = response.status
                    request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
                    content_type = response.headers.get("Content-Type", "application/json")
                    content = response.read(response_limit + 1)
                if len(content) > response_limit:
                    raise ProviderError("provider response exceeded limit")
                usage, response_model = _usage(
                    content, streaming="text/event-stream" in content_type, deepseek=deepseek, details=stream,
                )
            except urllib.error.HTTPError as exc:
                status = exc.code
                request_id = exc.headers.get("request-id") or exc.headers.get("x-request-id")
                exc.close()
                error = "http_error"
            except (TimeoutError, urllib.error.URLError, ConnectionError, IncompleteRead) as exc:
                reason = getattr(exc, "reason", exc)
                error = "timeout" if isinstance(reason, TimeoutError) else "transport_error"
            except (ValueError, TypeError, KeyError, AttributeError, ProviderError):
                error = "invalid_response"

            cost = _estimate(usage, deepseek=deepseek, peak=self.deepseek_peak)
            row = {
                "run_id": self.run_id, "task_id": scope.task_id, "worker_id": scope.worker_id,
                "instance_id": scope.instance_id,
                "module": module, "provider": provider, "model": model, "response_model": response_model,
                "call_id": call_id, "attempt": attempt, "request_id": request_id,
                "started_at": start, "elapsed_seconds": round(time.monotonic() - elapsed, 4),
                "status": error or "ok", "http_status": status,
                "request_bytes": len(body), "input_token_estimate": len(body),
                "requested_max_tokens": limit,
                "max_output_tokens": payload["max_tokens"],
                "stream": stream, "tool_argument_correction": correction,
                **{name: usage.get(name) for name in (
                    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens",
                )},
                "currency": "CNY", "cost": cost, "cost_kind": "estimate" if cost is not None else "missing",
                "price_source": DEEPSEEK_PRICE if deepseek else MINIMAX_PRICE, "price_date": PRICE_DATE,
                "price_band": ("peak" if self.deepseek_peak else "off-peak") if deepseek else "standard",
            }
            with self._lock:
                if status in (401, 403):
                    self._auth_failed = True
                append_jsonl(self.path, row)
            if error is None:
                return content, content_type
            if attempt == 1 and (error in RETRYABLE_TRANSPORT or status == 429
                                 or (status is not None and 500 <= status < 600)):
                time.sleep(1)
                continue
            raise ProviderError(f"{provider} {error}", status)
        raise AssertionError("provider retry loop exceeded")

    def summary(self, *, task_id: str | None = None) -> dict:
        attempts = read_jsonl(self.path)
        if task_id is not None:
            attempts = [row for row in attempts if row["task_id"] == task_id]
        costs = [row["cost"] for row in attempts if row["cost"] is not None]
        counts = Counter(row["module"] for row in attempts)
        openclaw = [row for row in attempts if row["module"] == "openclaw"]
        streams = [row.get("stream") or {} for row in openclaw]
        return {
            "provider_attempts": len(attempts),
            "successful_attempts": sum(row["status"] == "ok" for row in attempts),
            "openclaw_requests": counts["openclaw"],
            "openclaw_request_max_tokens": sorted({row["max_output_tokens"] for row in openclaw}),
            "openclaw_output_truncations": sum(stream.get("stop_reason") == "max_tokens" for stream in streams),
            "malformed_tool_blocks": sum(
                block["argument_error"] is not None for stream in streams for block in stream.get("tool_blocks", [])
            ),
            "tool_argument_corrections": dict(Counter(
                row["tool_argument_correction"] for row in openclaw
                if row.get("tool_argument_correction") and row["attempt"] == 1
            )),
            "provider_retries": sum(row["attempt"] > 1 for row in attempts),
            "provider_timeouts": sum(row["status"] == "timeout" for row in attempts),
            "provider_transport_errors": sum(row["status"] == "transport_error" for row in attempts),
            "attempts_by_module": dict(counts),
            "cost_estimate_cny": round(sum(costs), 9) if costs or not attempts else None,
            "attempts_with_cost": len(costs),
            "input_tokens_reported": sum(row["input_tokens"] or 0 for row in attempts),
            "output_tokens_reported": sum(row["output_tokens"] or 0 for row in attempts),
            "elapsed_provider_seconds": round(sum(row["elapsed_seconds"] for row in attempts), 4),
        }
