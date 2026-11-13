"""Content-addressed candidates and single-writer transactional controller state."""

from __future__ import annotations

import json
from copy import deepcopy

from misevolve.multiagent._io import atomic_json

from .policy import digest


class IntegrityError(ValueError):
    """Corrupt durable evidence is fatal, never converted into an admissible missing row."""


class Store:
    def __init__(self, root, *, resume=False):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "state.json"
        self.busy = root / "pending.json"
        if self.busy.exists():
            raise ValueError("unfinished Ours window: refuse implicit model/execution/publication replay")
        if resume:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            expected = self.state.pop("integrity")
            if digest(self.state) != expected:
                raise IntegrityError("controller snapshot integrity mismatch")
            for reference in self.state["objects"]:
                self.get(reference)
        else:
            if self.path.exists():
                raise FileExistsError("controller already initialized")
            self.state = {
                "window": 0,
                "events": [],
                "objects": [],
                "spent": [],
                "policies": [],
                "rollback_streak": 0,
                "previous_policy": None,
            }

    def put(self, kind, value):
        value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        reference = f"{kind}/{digest(value)}"
        path = self.root / f"{reference}.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
            raise IntegrityError("content-address collision")
        atomic_json(path, value)
        if reference not in self.state["objects"]:
            self.state["objects"].append(reference)
        return reference

    def get(self, reference):
        path = (self.root / f"{reference}.json").resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("object path escape")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IntegrityError("missing or corrupt controller object") from exc
        if digest(value) != reference.split("/")[-1]:
            raise IntegrityError("controller object integrity mismatch")
        return value

    def begin(self, window):
        if self.busy.exists() or window != self.state["window"] + 1:
            raise ValueError("duplicate or unfinished window")
        atomic_json(self.busy, {"window": window, "prior_state": digest(self.state)})

    def reserve(self, purpose, shard):
        key = f"{purpose}:{shard}"
        if key in self.state["spent"]:
            raise ValueError("confirmation shard already consumed")
        self.state["spent"].append(key)
        # Pending marker survives a crash; no retry can spend this shard or replay calls.
        atomic_json(self.busy, {"window": self.state["window"] + 1, "spent": self.state["spent"]})

    def event(self, value):
        event = {"index": len(self.state["events"]), **deepcopy(value)}
        reference = self.put("events", event)
        self.state["events"].append(reference)
        return reference

    def commit(self, *, window, policy, library):
        self.state.update(
            window=window, policy=self.put("policies", policy.to_dict()), library=self.put("libraries", library)
        )
        atomic_json(self.path, {**self.state, "integrity": digest(self.state)})
        self.busy.unlink(missing_ok=True)

    def history(self, count):
        # Only development evidence is public to the proposer. Sealed scores are never copied.
        events = [self.get(ref) for ref in self.state["events"]]
        events = [e for e in events if e.get("type") == "improvement"][-count:]
        return [
            {key: e[key] for key in ("window", "policy", "development", "changes", "repairs", "interventions")}
            for e in events
        ]
