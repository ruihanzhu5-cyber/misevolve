"""Explicit synthetic services for API-free integration tests, never research results."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .models import GateConfig, ValidationCase


MOCK_CONFIGURATION = {
    "backbone": "mock-minimax", "judge": "mock-deepseek", "reviewer": "mock-kimi",
    "autoskill_revision": "mock-native-engine", "image_digest": "mock-no-container",
    "roles": {"A": "planner", "B": "executor", "C": "verifier and final submitter"},
    "topology": "peer-to-peer", "budget": {"max_rounds": 8}, "synthetic": True,
}


def mock_config(**overrides):
    # Illustrative bounds, deliberately NOT a recommended research configuration.
    return GateConfig(**{**dict(max_risk=0.4, utility_margin=0.7, delta=0.1, min_confirmation=16,
                               development_pairs=2, total_pairs=80, max_localization_pairs=8, max_repairs=1),
                         **overrides})


def mock_cases(prefix="test"):
    cases = [ValidationCase(f"{prefix}-dev-{i}", {"instance_id": f"{prefix}-dev-{i}", "kind": "B",
                            "prompt": f"Synthetic development input {prefix} {i}"}, category, "development",
                            mechanisms=("unsafe-composition",))
             for i, category in enumerate(("source", "counterexample"))]
    cases.extend(ValidationCase(f"{prefix}-confirm-{i}",
                               {"instance_id": f"{prefix}-confirm-{i}", "kind": "B",
                                "prompt": f"Synthetic fresh input {prefix} {i}"},
                               "fresh", "confirmation", unit_id=f"{prefix}-unit-{i}") for i in range(16))
    return cases


def mock_version(skill_id="A:skill", text="SAFE"):
    return {"type": "skill_version", "skill_id": skill_id, "owner": skill_id.split(":")[0],
            "files": {"SKILL.md": text}, "native_metadata": {}, "native_version": "0.1.0"}


def mock_reviewer(request):
    return {"synthetic": True, "recommendation": "accept", "mechanisms": ["unsafe-composition"],
            "note": "Deliberately permissive mock: admission must still depend on validation."}


def mock_scorer(result, task, *, out_dir):
    return {"synthetic": True, "team_verdict": result["mock_verdict"]}


class MockTeamFactory:
    def __init__(self, store, *, shared_identity=False, failure=None):
        self.store, self.shared_identity, self.failure = store, shared_identity, failure
        self.runtimes = []
        self.executions = []

    def __call__(self, *, snapshot_id, library_dir, out_dir, configuration):
        factory = self

        class Runtime:
            def __init__(self):
                self.snapshot_id = snapshot_id
                self.configuration = deepcopy(configuration)
                self.identities = {category: {agent: uuid4().hex for agent in ("A", "B", "C")}
                                   for category in ("sdk", "store", "session", "container")}
                if factory.shared_identity:
                    self.identities["sdk"]["A"] = "shared-sdk"
                self.closed = False

            def run(self, task, *, seed, out_dir):
                factory.executions.append({"snapshot": self.snapshot_id, "task": deepcopy(task),
                                           "seed": seed, "configuration": deepcopy(self.configuration)})
                if factory.failure == "exception":
                    raise RuntimeError("synthetic infrastructure error")
                snapshot = factory.store.read_snapshot(self.snapshot_id)
                texts = [factory.store.get(oid)["files"]["SKILL.md"] for oid in snapshot["skills"].values()]
                risk = "UNSAFE" in texts or ("INTERACT_X" in texts and "INTERACT_Y" in texts)
                utility = "BREAK_UTILITY" not in texts
                if factory.failure == "unknown":
                    risk = utility = None
                if factory.failure == "mutate_library":
                    (library_dir / "manifest.json").write_text("{}", encoding="utf-8")
                return {"status": "timeout" if factory.failure == "timeout" else "completed",
                        "mock_verdict": {"unsafe_action": risk, "target_goal_achieved": False, "utility": utility},
                        "trajectory": {"synthetic": True, "messages": [{"role": "assistant", "content": "mock"}]},
                        "usage": {"input_tokens": 0, "output_tokens": 0, "real_api_calls": 0}}

            def close(self):
                self.closed = True

        runtime = Runtime()
        self.runtimes.append(runtime)
        return runtime


def mock_block(start=1):
    kinds = "MMMBBBMMMBBBMMMBBBPPP"
    return [{"pos": i, "kind": kinds[i - 1], "instance_id": f"mock-benchmark-{i}",
             "prompt": f"Synthetic benchmark input {i}", "round": (i - 1) // 6 + 1}
            for i in range(start, start + 3)]
