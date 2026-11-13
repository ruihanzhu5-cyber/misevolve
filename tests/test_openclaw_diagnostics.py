"""Execute the container API's functions with a synthetic subprocess, without Docker."""

import ast
import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("returncode", [1, -15])
def test_stage7_process_failure_retains_details_without_replay(monkeypatch, returncode):
    # The container's web framework is not a host dependency. Load the exact production
    # function bodies, replacing only framework types and subprocess I/O.
    source = Path(__file__).resolve().parents[1] / "docker/openclaw/api_server.py"
    names = {"async_retry", "_diagnostic_stream", "send_prompt_to_openclaw", "send_prompt"}
    tree = ast.parse(source.read_text(encoding="utf-8"))
    module = ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name in names], type_ignores=[])

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail

    monkeypatch.setenv("DIAGNOSTIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "131072")
    monkeypatch.setenv("MISEVOLVE_OPENCLAW_PROMPT_ATTEMPTS", "5")
    proc = SimpleNamespace(returncode=returncode, pid=123,
                           communicate=AsyncMock(return_value=(b"partial synthetic-secret", b"")))
    spawn = AsyncMock(return_value=proc)
    namespace = {
        "os": os, "re": re, "json": json, "tempfile": tempfile, "STAGE7": True, "Dict": dict,
        "asyncio": SimpleNamespace(create_subprocess_exec=spawn, subprocess=asyncio.subprocess),
        "HTTPException": HTTPException, "_stage7_session_key": lambda s: s,
        "logger": logging.getLogger("test"), "wraps": __import__("functools").wraps,
        "PromptRequest": SimpleNamespace, "active_sessions": {},
        "app": SimpleNamespace(post=lambda *a, **kw: lambda f: f), "SessionResponse": SimpleNamespace,
    }
    exec(compile(module, str(source), "exec"), namespace)
    with pytest.raises(HTTPException) as error:
        asyncio.run(namespace["send_prompt"](SimpleNamespace(session_id="s", prompt="synthetic",
                                                             create_new_session=False)))
    assert spawn.await_count == 1
    detail = error.value.detail
    assert detail["returncode"] == returncode
    assert detail["signal"] == (15 if returncode == -15 else None)
    assert detail["stdout"]["text"] == "partial [REDACTED]"
    assert detail["stderr"]["text"] == "" and detail["pid"] == 123
    stream = namespace["_diagnostic_stream"](b"x" * 20000 + b"\nAuthorization: Bearer sample-token")
    assert stream["truncated"] and len(stream["text"]) <= 16384
    assert "sample-token" not in stream["text"]
    assert namespace["_diagnostic_stream"](b"limit=131072")["text"] == "limit=131072"
    assert "sample-token" not in namespace["_diagnostic_stream"](
        b'{"Authorization":"Bearer sample-token"}')['text']
