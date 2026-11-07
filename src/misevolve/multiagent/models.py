"""Worker state and immutable-version artifact contracts for the text-only pilot."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ._io import atomic_json, canonical_json

MAX_SKILL_BYTES = 1_000_000


class UnsupportedSkillError(ValueError):
    """The pilot cannot faithfully represent this artifact."""


class SkillOrigin(StrEnum):
    IMPORTED = "imported"
    LOCALLY_CREATED = "locally_created"
    LOCALLY_UPDATED = "locally_updated"
    UNKNOWN = "unknown"


def valid_evidence(evidence: dict, kind: str) -> bool:
    ref = evidence.get("source_ref")
    return (evidence.get("type") == kind and isinstance(ref, str) and bool(ref.strip())
            and len(ref) <= 1024 and "\n" not in ref and "\r" not in ref)


def text_skill_files(files: dict[str, str]) -> dict[str, str]:
    if not isinstance(files, dict) or set(files) != {"SKILL.md"}:
        raise UnsupportedSkillError("text-only pilot requires exactly SKILL.md; resource files are unsupported")
    text = files["SKILL.md"]
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise UnsupportedSkillError("SKILL.md must be nonempty UTF-8 text without NUL bytes")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as exc:
        raise UnsupportedSkillError("SKILL.md is not valid UTF-8 text") from exc
    if len(encoded) > MAX_SKILL_BYTES:
        raise UnsupportedSkillError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
    return {"SKILL.md": text.replace("\r\n", "\n").replace("\r", "\n")}


def content_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(canonical_json(text_skill_files(files)).encode("utf-8")).hexdigest()


def skill_version_key(bundle: dict) -> tuple:
    return bundle["content_hash"], bundle.get("resource_files", {})


def global_skill_id(owner: str, local_key: str, digest: str, resources: dict | None = None) -> str:
    identity = [owner, local_key, digest]
    if resources:
        identity.append(resources)
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillArtifact:
    global_skill_id: str
    owner_agent: str
    local_skill_key: str
    version: str
    content_hash: str
    skill_files: dict[str, str]
    native_metadata: dict[str, Any] = field(default_factory=dict)
    resource_files: dict[str, str] = field(default_factory=dict)
    parent_skill_ids: list[str] = field(default_factory=list)
    retrieved_parent_ids: list[str] = field(default_factory=list)
    invoked_parent_ids: list[str] = field(default_factory=list)
    parent_evidence: dict[str, dict] = field(default_factory=dict)
    source_task_ids: list[str] = field(default_factory=list)
    transformation_type: str = "unknown"
    safety_status: str = "unknown"
    origin: str = SkillOrigin.UNKNOWN
    imported_from_global_id: str | None = None
    native_revision_parent_id: str | None = None
    unresolved_predecessor: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkerRuntime:
    worker_id: str
    runner: Any
    method: Any
    episode_dir: Path
    local_to_global: dict[str, str] = field(default_factory=dict)
    imported_global_to_local: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.worker_id):
            raise ValueError("worker_id must be a safe nonempty path component")
        self.episode_dir = Path(self.episode_dir).resolve()

    @property
    def state_path(self) -> Path:
        return self.episode_dir / "worker_state.json"

    def save_state(self) -> None:
        atomic_json(self.state_path, {
            "schema_version": 1,
            "worker_id": self.worker_id,
            "local_to_global": self.local_to_global,
            "imported_global_to_local": self.imported_global_to_local,
        })

    def restore_state(self) -> None:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or data.get("worker_id") != self.worker_id:
                raise ValueError("worker state identity/schema mismatch")
            self.local_to_global = dict(data["local_to_global"])
            self.imported_global_to_local = dict(data["imported_global_to_local"])
        # Reconcile a crash after the bridge committed an import but before worker state saved.
        bridge = getattr(self.method, "_exchange", None)
        if bridge is not None:
            for gid, record in bridge.imports.items():
                if record.get("local_id"):
                    local_id = record["local_id"]
                    self.imported_global_to_local[gid] = local_id
                    self.local_to_global.setdefault(local_id, gid)
        for local_id in {*self.local_to_global, *self.imported_global_to_local.values()}:
            if self.method.sdk.get(local_id) is None:
                raise ValueError(f"worker mapping refers to a missing native skill: {local_id}")

    def remember_local_version(self, local_id: str, gid: str) -> None:
        self.local_to_global[local_id] = gid
        self.save_state()

    def remember_import(self, gid: str, local_id: str) -> None:
        old = self.imported_global_to_local.get(gid)
        if old is not None and old != local_id:
            raise ValueError("global ID is already mapped to a different receiver local ID")
        self.imported_global_to_local[gid] = local_id
        self.local_to_global.setdefault(local_id, gid)
        self.save_state()
