"""Adapter-side exchange state; all native store writes go through the SDK."""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from dataclasses import asdict
from hashlib import file_digest, sha256
from pathlib import Path

from ._io import append_jsonl, atomic_json, canonical_json, read_jsonl
from .models import MAX_SKILL_BYTES, SkillOrigin, content_hash, skill_version_key, text_skill_files, valid_evidence


def skill_reference(bundle: dict) -> dict:
    """A content-free reference to the version observed at a particular boundary."""
    ref = {
        "local_skill_id": bundle.get("local_skill_key", bundle.get("local_skill_id")),
        "content_hash": bundle.get("content_hash"), "origin": bundle.get("origin", "unknown"),
        "imported_global_skill_id": bundle.get("imported_from_global_id", bundle.get("imported_global_skill_id")),
    }
    if bundle.get("resource_files"):
        ref["resource_files"] = bundle["resource_files"]
    return ref


def _resource_hashes(sdk, skill, exported):
    from autoskill.management.formats.agent_skill import load_agent_skill_dir

    resources = {name: sha256(text.encode("utf-8")).hexdigest()
                 for name, text in exported.items() if name != "SKILL.md"}
    store_path = getattr(sdk.store, "path", None)
    if not store_path:
        return resources
    # The pinned SDK reloads user Skills without their file payloads. Use the
    # public directory format to associate disk resources with the native ID.
    user_root = Path(store_path) / "Users" / skill.user_id
    for markdown in user_root.glob("*/SKILL.md"):
        root = markdown.parent
        files = [path for path in root.rglob("*") if path != markdown and path.is_file()]
        if not files or load_agent_skill_dir(str(root), user_id=skill.user_id, include_files=False).id != skill.id:
            continue
        for path in files:
            if path.is_symlink():
                raise ValueError("symbolic links are unsupported in native Skill resources")
            with path.open("rb") as handle:
                resources[path.relative_to(root).as_posix()] = file_digest(handle, "sha256").hexdigest()
        break
    return resources


def export_bundle(sdk, local_skill_key: str) -> dict:
    skill = sdk.get(local_skill_key)
    if skill is None:
        raise KeyError(local_skill_key)
    files = text_skill_files({"SKILL.md": sdk.export_skill_md(local_skill_key)})
    bundle = {
        "local_skill_key": local_skill_key,
        "version": str(skill.version),
        "skill_files": files,
        "native_metadata": json.loads(canonical_json(asdict(skill))),
        "content_hash": content_hash(files),
    }
    resources = _resource_hashes(sdk, skill, sdk.export_skill_dir(local_skill_key) or {})
    if resources:
        bundle["resource_files"] = resources
    return bundle


def imported_files(source_files: dict[str, str], local_id: str) -> dict[str, str]:
    """Only the pinned SDK's documented frontmatter-ID adaptation is allowed."""
    from autoskill.management.formats.agent_skill import upsert_skill_md_id

    files = text_skill_files(source_files)
    return {"SKILL.md": upsert_skill_md_id(files["SKILL.md"], skill_id=local_id)}


class AutoSkillExchange:
    def __init__(self, sdk, episode_dir: Path, *, user_id: str, worker_id: str):
        self.sdk = sdk
        self.episode_dir = Path(episode_dir)
        self.user_id = user_id
        self.worker_id = worker_id
        self.state_path = self.episode_dir / "exchange_state.json"
        self.imports: dict[str, dict] = {}
        self.origins: dict[str, dict] = {}
        self.events: list[dict] = []
        self.before_ingest = None
        self.after_ingest = None
        self.evolution_path = self.episode_dir / "evolution_events.jsonl"
        self.task_path = self.episode_dir / "task_events.jsonl"
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (data.get("schema_version"), data.get("user_id"), data.get("worker_id")) != (1, user_id, worker_id):
                raise ValueError("exchange state identity/schema mismatch")
            self.imports = data["imports"]
            self.origins = data["origins"]
        # The event was flushed before its origin checkpoint: replay observed native changes.
        for event in read_jsonl(self.evolution_path):
            for local_id, bundle in event.get("after", {}).items():
                if bundle.get("origin") in {SkillOrigin.LOCALLY_CREATED, SkillOrigin.LOCALLY_UPDATED}:
                    self.origins[local_id] = {
                        "origin": bundle["origin"],
                        "imported_from_global_id": bundle.get("imported_from_global_id"),
                        "content_hash": bundle["content_hash"],
                        **({"resource_files": bundle["resource_files"]} if bundle.get("resource_files") else {}),
                    }

    def save(self) -> None:
        atomic_json(self.state_path, {
            "schema_version": 1,
            "user_id": self.user_id,
            "worker_id": self.worker_id,
            "imports": self.imports,
            "origins": self.origins,
        })

    def snapshot(self, local_id: str) -> dict:
        bundle = export_bundle(self.sdk, local_id)
        origin = self.origins.get(local_id, {})
        bundle.update({
            "origin": origin.get("origin", SkillOrigin.UNKNOWN),
            "imported_from_global_id": origin.get("imported_from_global_id"),
        })
        return bundle

    def snapshot_store(self) -> dict[str, dict]:
        return {s.id: self.snapshot(s.id) for s in self.sdk.list(user_id=self.user_id)}

    def _task_event(self, kind: str, task: dict, **fields) -> dict:
        event = {
            "schema_version": 1, "event": kind, "event_id": uuid.uuid4().hex,
            "worker_id": self.worker_id, "task_run_id": task["task_run_id"],
            "pos": task.get("pos"), "kind": task.get("kind"), **fields,
        }
        append_jsonl(self.task_path, event)
        return event

    def _skill_references(self, skills) -> list[dict]:
        refs = []
        for skill in skills:
            bundle = self.snapshot(skill.key)
            if bundle["content_hash"] != content_hash({"SKILL.md": skill.text}):
                raise ValueError("executor Skill text differs from the native snapshot")
            refs.append(skill_reference(bundle))
        return refs

    def record_retrieval(self, task: dict, skills, *, status: str) -> None:
        self._task_event("task_retrieval", task, retrieval_status=status, retrieved=self._skill_references(skills))

    def run_with_intent(self, task: dict, runner, prompt: str, skills, *, out_dir: Path, source: str):
        if source not in {"native_search", "frozen_library", "frozen_native_search"}:
            raise ValueError("invalid injection source")
        if (task["kind"] == "P") != source.startswith("frozen_"):
            raise ValueError("injection source disagrees with task kind")
        self._task_event(
            "task_injection_intent", task, injection_source=source, intended=self._skill_references(skills),
        )
        try:
            trajectory = runner.run(prompt, skills, out_dir=out_dir)
        except BaseException as exc:
            self.record_execution(task, error_type=type(exc).__name__)
            raise
        self.record_execution(task, trajectory=trajectory)
        return trajectory

    def record_execution(self, task: dict, *, trajectory=None, error_type: str | None = None) -> None:
        raw = trajectory.raw if trajectory is not None and isinstance(trajectory.raw, dict) else {}
        failed = (error_type is not None
                  or (trajectory is not None and bool(trajectory.error or raw.get("prompt_error"))))
        manifest = raw.get("_skill_injection")
        # Only the manifest is copied. Prompts, assistant/tool text, and runner error messages stay out.
        safe_manifest = None
        if (isinstance(manifest, dict) and isinstance(manifest.get("skills"), list)
                and all(isinstance(item, dict) for item in manifest["skills"])):
            safe_manifest = {
                "status": manifest.get("status"),
                "skills": [{key: item.get(key) for key in (
                    "local_skill_id", "runtime_skill_name", "runtime_directory_slug", "runtime_content_hash",
                )} for item in manifest.get("skills", []) if isinstance(item, dict)],
            }
        self._task_event(
            "task_execution", task, execution_status="failed" if failed else "completed",
            error_type=error_type or ("runner_error" if failed else None), injection_manifest=safe_manifest,
            runner_raised=error_type is not None,
        )

    def record_invocation_evidence(self, task_run_id: str, local_id: str, *, source_ref: str) -> None:
        """For explicit runtime instrumentation only; never called from assistant/tool text heuristics."""
        if not valid_evidence({"type": "explicit_invocation", "source_ref": source_ref}, "explicit_invocation"):
            raise ValueError("explicit invocation requires a source_ref")
        tasks = [row for row in read_jsonl(self.task_path) if row.get("task_run_id") == task_run_id]
        if not tasks:
            raise KeyError(task_run_id)
        self._task_event("skill_invocation", tasks[0], skill=skill_reference(self.snapshot(local_id)),
                         evidence={"type": "explicit_invocation", "source_ref": source_ref})

    def ingest(self, *, task: dict, retrieved: list[dict], **kwargs):
        """Observe native ingest without adding retrieval references or changing its inputs."""
        before = self.snapshot_store()
        event = {
            "event": "native_ingest", "event_id": uuid.uuid4().hex,
            "worker_id": self.worker_id, "task": dict(task), "retrieved": retrieved,
            "before": before, "after": {}, "returned_skills": [], "success": False,
            "updated_local_skill_ids": [], "error": None,
            "invocation_evidence": [
                {"skill": row["skill"], "evidence": row["evidence"], "source_event_id": row["event_id"]}
                for row in read_jsonl(self.task_path)
                if row.get("event") == "skill_invocation" and row.get("task_run_id") == task.get("task_run_id")
            ],
        }
        try:
            if self.before_ingest:
                self.before_ingest(task)
            updated = self.sdk.ingest(**kwargs)
            event["returned_skills"] = [json.loads(canonical_json(asdict(s))) for s in updated or []]
            event["updated_local_skill_ids"] = [s.id for s in updated or []]
            if self.after_ingest:
                self.after_ingest()
            event["success"] = True
            return updated
        except Exception as exc:
            event["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                after = self.snapshot_store()
                self._observe_changes(before, after)
                event["after"] = after
            except Exception as exc:
                event["success"] = False
                event["snapshot_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                append_jsonl(self.evolution_path, event)
                self.events.append(event)
                self.save()

    def _observe_changes(self, before, after):
        for local_id, bundle in after.items():
            previous = before.get(local_id)
            if previous is None or skill_version_key(previous) != skill_version_key(bundle):
                origin = SkillOrigin.LOCALLY_CREATED if previous is None else SkillOrigin.LOCALLY_UPDATED
                old_origin = self.origins.get(local_id, {})
                self.origins[local_id] = {
                    "origin": origin, "imported_from_global_id": old_origin.get("imported_from_global_id"),
                    "content_hash": bundle["content_hash"],
                    **({"resource_files": bundle["resource_files"]} if bundle.get("resource_files") else {}),
                }
                bundle.update(self.origins[local_id])

    def _verify_visibility(self, local_id: str) -> dict:
        skill = self.sdk.get(local_id)
        skills = self.sdk.list(user_id=self.user_id)
        if skill is None or local_id not in {s.id for s in skills}:
            raise ValueError("imported skill is absent from receiver get/list")
        query = f"{skill.name} {skill.description}"
        limit = max(5, len(skills))
        hits = self.sdk.search(query, user_id=self.user_id, limit=limit, scope="user")
        visible = any(h.skill.id == local_id for h in hits)
        if not visible:
            raise ValueError("receiver native search did not return imported skill")
        return {
            "native_retrieval_verified": True, "search_kind": "diagnostic",
            "query": query, "limit": limit,
            "hits": [{"local_id": h.skill.id, "score": h.score} for h in hits],
        }

    def import_skill(
        self, skill_files: dict[str, str], global_id: str, *, source_agent: str = "", source_local_id: str = "",
    ) -> str:
        from autoskill.management.formats.agent_skill import load_agent_skill_dir

        files = text_skill_files(skill_files)
        if not re.fullmatch(r"[a-f0-9]{64}", global_id):
            raise ValueError("global ID must be a SHA-256 hex path component")
        source_hash = content_hash(files)
        record = self.imports.get(global_id)
        if record is None:
            record = {
                "source_content_hash": source_hash, "source_agent": source_agent,
                "source_local_id": source_local_id, "local_id": None,
                "before_local_ids": [s.id for s in self.sdk.list(user_id=self.user_id)],
                "integrity_verified": False, "native_retrieval_verified": False,
            }
            self.imports[global_id] = record
            self.save()  # A durable pending record also covers a crash inside native import.
        elif (record["source_content_hash"] != source_hash or record["source_agent"] != source_agent
              or record["source_local_id"] != source_local_id):
            raise ValueError("global ID already refers to different import contents/source")
        record["integrity_verified"] = False
        record["native_retrieval_verified"] = False
        self.save()

        if record["local_id"] is None:
            with tempfile.TemporaryDirectory(prefix="skill-import-", dir=self.episode_dir) as temp:
                skill_dir = Path(temp) / global_id
                skill_dir.mkdir()
                with (skill_dir / "SKILL.md").open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(files["SKILL.md"])
                # Ask the pinned native parser for its own deterministic ID. This is
                # read-only; the official SDK importer below remains the only writer.
                expected_id = load_agent_skill_dir(
                    str(skill_dir), user_id=self.user_id, deterministic_id_key=global_id,
                    ignore_frontmatter_id=True, max_file_bytes=MAX_SKILL_BYTES,
                ).id
                if expected_id in record["before_local_ids"]:
                    raise ValueError("native import ID collides with a preexisting untracked skill")
                record["expected_local_id"] = expected_id
                self.save()
                imported = self.sdk.import_agent_skill_dirs(
                    root_dir=temp, user_id=self.user_id, overwrite=False, include_files=True,
                    reassign_ids=True, max_file_bytes=MAX_SKILL_BYTES,
                )
            if len(imported) == 1:
                local_id = imported[0].id
                if local_id != expected_id:
                    raise ValueError("native importer ID differs from the pinned native format contract")
            elif not imported:
                # Retry after native upsert committed but before the bridge saved its returned ID.
                if self.sdk.get(expected_id) is None:
                    raise ValueError("pending native import is missing from receiver store")
                local_id = expected_id
            else:
                raise ValueError("native importer returned more than one skill for one artifact")
            imported_bundle = export_bundle(self.sdk, local_id)
            if imported_bundle.get("resource_files"):
                raise ValueError("native import produced files outside the SKILL.md payload")
            record["local_id"] = local_id
            record["imported_files"] = imported_bundle["skill_files"]
            self.origins[local_id] = {
                "origin": SkillOrigin.IMPORTED, "imported_from_global_id": global_id,
                "content_hash": content_hash(record["imported_files"]),
            }
            self.save()  # Preserve identity even when subsequent integrity/search checks fail.

        local_id = record["local_id"]
        expected = imported_files(files, local_id)
        if record["imported_files"] != expected:
            raise ValueError("receiver import differs beyond allowed ID/line-ending adaptation")
        current = self.snapshot(local_id)
        updated = current["skill_files"] != expected or bool(current.get("resource_files"))
        if updated:
            origin = self.origins.get(local_id, {})
            if (origin.get("origin") != SkillOrigin.LOCALLY_UPDATED
                    or skill_version_key(origin) != skill_version_key(current)):
                raise ValueError("receiver contents changed without a recorded local evolution")
        record.update({
            "integrity_verified": True, "target_content_hash": content_hash(expected),
            "current_content_hash": current["content_hash"],
            "receiver_locally_updated": updated,
            "native_retrieval_verified": False,
        })
        self.save()
        record.update(self._verify_visibility(local_id))
        self.save()
        return local_id
