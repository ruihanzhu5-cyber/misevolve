"""Content-addressed evidence, isolated candidates and atomic approved releases.

Single-writer operations use exclusive filesystem locks. A release is immutable;
only CURRENT is replaced. Readers pin its release ID once per task block.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from misevolve.multiagent._io import atomic_json

from .models import digest


class ScopeStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("objects", "candidates", "releases", "cycles", "workspaces"):
            (self.root / name).mkdir(exist_ok=True)

    @contextmanager
    def lock(self, name="publisher"):
        self._name(name)
        path = self.root / f".{name}.lock"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.close(fd)
            yield
        finally:
            path.unlink()

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError("unsafe object identifier")

    def put(self, value: dict) -> str:
        oid = digest(value)
        path = self.root / "objects" / f"{oid}.json"
        if path.exists():
            if self.get(oid) != json.loads(json.dumps(value)):
                raise ValueError("object collision")
        else:
            atomic_json(path, value)
        return oid

    def get(self, oid: str) -> dict:
        if not isinstance(oid, str) or not re.fullmatch(r"[a-f0-9]{64}", oid):
            raise ValueError("invalid content ID")
        value = json.loads((self.root / "objects" / f"{oid}.json").read_text(encoding="utf-8"))
        if digest(value) != oid:
            raise ValueError(f"immutable object modified: {oid}")
        return value

    def snapshot(self, skills: dict, memory=(), *, parent=None, evidence=None) -> str:
        for skill_id, version in skills.items():
            if self.get(version)["skill_id"] != skill_id:
                raise ValueError("snapshot skill/version identity mismatch")
        for record in memory:
            item = self.get(record)
            if item.get("type") != "verified_memory" or not item.get("evidence"):
                raise ValueError("unverified memory cannot be published")
        return self.put({"type": "snapshot", "skills": dict(sorted(skills.items())),
                         "memory": sorted(set(memory)), "parent": parent, "evidence": evidence})

    def read_snapshot(self, sid: str) -> dict:
        snapshot = self.get(sid)
        if snapshot.get("type") != "snapshot":
            raise ValueError("expected library snapshot")
        for skill_id, oid in snapshot["skills"].items():
            if self.get(oid)["skill_id"] != skill_id:
                raise ValueError("snapshot identity mismatch")
        for oid in snapshot["memory"]:
            self.get(oid)
        return snapshot

    def initialize(self, skills=None) -> str:
        with self.lock():
            if (self.root / "CURRENT.json").exists():
                return self.current()
            sid = self.snapshot(skills or {})
            atomic_json(self.root / "releases" / f"{sid}.json", {"snapshot": sid, "genesis": True})
            atomic_json(self.root / "CURRENT.json", {"snapshot": sid})
            return sid

    def current(self) -> str:
        sid = json.loads((self.root / "CURRENT.json").read_text(encoding="utf-8"))["snapshot"]
        self.read_snapshot(sid)
        if not (self.root / "releases" / f"{sid}.json").exists():
            raise ValueError("CURRENT refers to an unpublished snapshot")
        return sid

    def stage(self, *, base: str, skill_id: str, after: dict | None, source_tasks=(),
              mechanisms=(), dependencies=(), history=(), evidence=()) -> str:
        before = self.read_snapshot(base)["skills"].get(skill_id)
        if after is not None and after.get("skill_id") != skill_id:
            raise ValueError("candidate skill identity mismatch")
        version = self.put(after) if after is not None else None
        if before == version:
            raise ValueError("candidate has no net change")
        for oid in (*history, *evidence):
            self.get(oid)
        patch = {"type": "candidate", "base": base, "skill_id": skill_id, "before": before,
                 "after": version, "source_tasks": list(source_tasks), "mechanisms": sorted(set(mechanisms)),
                 "dependencies": sorted(set(dependencies)), "history": list(history), "evidence": list(evidence)}
        cid = self.put(patch)
        atomic_json(self.root / "candidates" / f"{cid}.json", {"candidate": cid})
        return cid

    def overlay(self, base: str, candidates: list[str], *, memory=None, evidence=None) -> str:
        original = self.read_snapshot(base)
        skills = original["skills"].copy()
        touched = set()
        for cid in candidates:
            patch = self.get(cid)
            if patch.get("type") != "candidate" or patch["base"] != base:
                raise ValueError("candidate belongs to a different base release")
            key = patch["skill_id"]
            if key in touched or skills.get(key) != patch["before"]:
                raise ValueError("conflicting updates require an explicit new native candidate")
            touched.add(key)
            if patch["after"] is None:
                skills.pop(key, None)
            else:
                skills[key] = patch["after"]
        return self.snapshot(skills, original["memory"] if memory is None else memory,
                             parent=base, evidence=evidence)

    def publish(self, sid: str, *, expected_base: str, decision: str) -> None:
        with self.lock():
            current = self.current()
            if current == sid:  # idempotent recovery after pointer commit
                return
            if current != expected_base:
                raise ValueError("stale base: another cycle already published")
            snapshot = self.read_snapshot(sid)
            record = self.get(decision)
            if snapshot["parent"] != expected_base or record.get("snapshot") != sid:
                raise ValueError("decision is not bound to this snapshot")
            if (record.get("type") != "decision" or record.get("status") != "accept"
                    or record.get("gate", {}).get("status") != "accept" or not record.get("validation")):
                raise ValueError("publication requires successful validation evidence")
            purposes = set()
            for oid in record["validation"]:
                item = self.get(oid)
                if (item.get("type") != "paired_validation" or item.get("status") != "completed"
                        or item.get("base") != expected_base or item.get("candidate") != sid):
                    raise ValueError("validation evidence does not match the release")
                purposes.add(item.get("purpose"))
            if not {"merged_integration", "final_confirmation"} <= purposes:
                raise ValueError("release lacks integration or confirmation evidence")
            atomic_json(self.root / "releases" / f"{sid}.json", {"snapshot": sid, "decision": decision})
            atomic_json(self.root / "CURRENT.json", {"snapshot": sid})

    def materialize(self, sid: str, destination: Path) -> None:
        """Export pinned files to a fresh per-team directory (never a writable live link)."""
        snapshot = self.read_snapshot(sid)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        for skill_id, oid in snapshot["skills"].items():
            root = destination / digest(skill_id)
            for name, text in self.get(oid)["files"].items():
                # Manifest files use POSIX relative paths, even on a Windows test host.
                if not name or "\\" in name or ":" in name or name.startswith("/"):
                    raise ValueError("unsafe Skill resource path")
                parts = name.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    raise ValueError("unsafe Skill resource path")
                target = root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        atomic_json(destination / "manifest.json", {"snapshot": sid, **snapshot})
