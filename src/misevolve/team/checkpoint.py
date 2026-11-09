"""Resume only a cleanly paused team Episode, after a whole learning block."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from misevolve.multiagent._io import atomic_json, canonical_json

BOUNDARIES = (3, 6, 9, 12, 15, 18)
CHECKPOINT = "team_checkpoint.json"
_ROOT_FILES = (
    "team_manifest.json", "all_rows.json", "behavior_summary.json", "team_events.jsonl",
    "model_calls.jsonl", "judge_results.jsonl", "frozen_stores.json", "request_rejections.jsonl",
    "late_executor_attempts.jsonl",
)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _state(root):
    paths = [root / name for name in _ROOT_FILES if (root / name).is_file()]
    for name in ("workers", "registry", "provenance", "tasks", "judge_responses"):
        paths.extend(path for path in (root / name).rglob("*") if path.is_file())
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("checkpoint state contains a path outside the run")
        with path.open("rb") as handle:
            result[path.relative_to(root).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def save(root, last_pos, snapshots):
    if last_pos not in BOUNDARIES:
        raise ValueError("pause must be at a completed learning-block boundary")
    atomic_json(root / CHECKPOINT, {
        "schema_version": 1, "last_completed_pos": last_pos,
        "stores": snapshots, "file_hashes": _state(root),
    })
    atomic_json(root / "progress.json", {"phase": "paused", "last_completed_pos": last_pos, "next_pos": last_pos + 1})


def load(root, expected_manifest):
    try:
        progress = _read(root / "progress.json")
        checkpoint = _read(root / CHECKPOINT)
        manifest = _read(root / "team_manifest.json")
        rows = _read(root / "all_rows.json")
    except (OSError, ValueError) as exc:
        raise ValueError("resume requires an intact, cleanly paused team Episode") from exc
    last = checkpoint.get("last_completed_pos")
    if (checkpoint.get("schema_version") != 1 or last not in BOUNDARIES
            or progress != {"phase": "paused", "last_completed_pos": last, "next_pos": last + 1}):
        raise ValueError("resume requires a clean pause at a learning-block boundary")
    if canonical_json(manifest) != canonical_json(expected_manifest):
        raise ValueError("resume configuration, code or task plan changed")
    if checkpoint.get("file_hashes") != _state(root):
        raise ValueError("persisted episode evidence changed after the pause; refusing to replay")
    tasks = [row for row in rows if row["pos"] is not None]
    if ([(r["pos"], r["orig_id"]) for r in tasks] !=
            [(r["pos"], r["instance_id"]) for r in manifest["tasks"][:last]]
            or len({r["task_run_id"] for r in tasks}) != last):
        raise ValueError("completed task rows do not match the checkpoint")
    return last, rows, checkpoint["stores"]


def restore_native_metadata(workers, expected_stores, observed_stores):
    """LocalStore reloads from SKILL.md, which does not encode all native fields."""
    from autoskill.models import Skill, SkillExample, SkillStatus

    for agent, before in expected_stores.items():
        after = observed_stores[agent]
        if set(before["skills"]) != set(after["skills"]) or any(
            before[key] != after[key] for key in ("ingests", "imports")
        ):
            raise ValueError("restored Skill Store identity or history differs from the checkpoint")
        worker = workers[agent]
        worker.method._prov = {key: tuple(value) for key, value in before["method_provenance"].items()}
        worker.method._last_kind, worker.method._last_round = before["last_learning"]
        for local_id, skill in before["skills"].items():
            current = after["skills"][local_id]
            if {k: v for k, v in skill.items() if k != "native_metadata"} != {
                k: v for k, v in current.items() if k != "native_metadata"
            }:
                raise ValueError("restored Skill bytes, version or origin differs from the checkpoint")
            metadata = skill["native_metadata"]
            if current["native_metadata"] == metadata:
                continue
            if metadata["id"] != local_id or metadata["user_id"] != worker.method._UID:
                raise ValueError("checkpoint native metadata has the wrong owner or ID")
            native = Skill(**{
                **metadata, "status": SkillStatus(metadata["status"]),
                "examples": [SkillExample(**example) for example in metadata["examples"]],
            })
            # Use the native Store API to keep both metadata and retrieval in sync.
            # There is no new ingest, native import, or Registry version here.
            worker.method.sdk.store.upsert(native, raw=metadata)


@contextmanager
def exclusive(root):
    """Keep parallel launches from writing or resuming the same directory."""
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / f".{root.name}.phase3.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()}\n")
        yield
    finally:
        lock.unlink()
