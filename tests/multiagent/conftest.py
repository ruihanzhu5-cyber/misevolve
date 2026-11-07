"""Synthetic tests using the pinned real SDK; forbid network and Docker execution."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from misevolve import harness
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.methods import build_method

PIN = "94c47ca488d4ba4117d20272e66d49b9877e68cf"


@pytest.fixture(scope="session", autouse=True)
def pinned_sdk():
    import autoskill

    root = Path(autoskill.__file__).resolve().parents[1]
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert actual == PIN, f"native tests require AutoSkill@{PIN}, got {actual}"
    return autoskill


@pytest.fixture(autouse=True)
def no_model_or_docker(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("multiagent tests must not use network, real models, or Docker")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenClawRunner, "_docker", forbidden)
    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-test-key")
    for key in ("AUTOSKILL_LLM_MODEL", "AUTOSKILL_LLM_BASE_URL", "AUTOSKILL_REPO", "MISEVOLVE_AUTOSKILL_DIR"):
        monkeypatch.delenv(key, raising=False)


class MockRunner(OpenClawRunner):
    """Keep real runner instance state; replace only execution and cleanup."""

    closed = False

    def run(self, prompt, inject, *, out_dir):
        return harness.Trajectory([], final_text="Synthetic task completed.")

    def teardown(self):
        self.closed = True


@pytest.fixture
def runner_factory():
    from misevolve.backbones import resolve_agent

    return lambda harness_id, backbone: MockRunner(resolve_agent(harness_id, backbone))


@pytest.fixture
def method_factory():
    return build_method


@pytest.fixture
def seed_skill():
    def seed(worker, *, skill_id="synthetic-source", instructions="Check the sum of two synthetic integers."):
        return worker.method.sdk.upsert(
            user_id=worker.method._UID, skill_id=skill_id,
            name="synthetic-arithmetic", description="Verify a synthetic arithmetic result.",
            instructions=instructions, tags=["synthetic"],
            metadata={"test": True, "nested": {"value": 7}}, source={"type": "synthetic"},
        )

    return seed


@pytest.fixture
def workers(tmp_path, runner_factory):
    from misevolve.multiagent.orchestrator import MultiAgentOrchestrator

    pool = MultiAgentOrchestrator(tmp_path / "run", runner_factory=runner_factory)
    initialized = pool.setup_workers()
    yield initialized
    pool.teardown()


@pytest.fixture
def synthetic_llm(monkeypatch):
    """Mock only LLM outputs; run SDK extraction parsing and maintenance unchanged."""
    def configure(method):
        state = {"extract_calls": 0, "merge_calls": 0}

        def fields():
            return {
                "name": "synthetic-arithmetic", "description": "Verify a synthetic arithmetic result.",
                "prompt": ("# Goal\nCheck synthetic integers.\n# Constraints & Style\n"
                           f"Apply revision {state['extract_calls']}."),
                "triggers": ["synthetic arithmetic"], "tags": ["synthetic"], "confidence": 0.9,
            }

        def extract(**kwargs):
            state["extract_calls"] += 1
            return json.dumps({"skills": [fields()]})

        def maintain(*, system, **kwargs):
            if "Skill Merger" in system:
                state["merge_calls"] += 1
                return json.dumps(fields())
            return json.dumps({"action": "add", "reason": "synthetic"})

        monkeypatch.setattr(method.sdk.extractor._llm, "complete", extract)
        monkeypatch.setattr(method.sdk.maintainer._llm, "complete", maintain)
        return state

    return configure
