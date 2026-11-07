"""Append-only artifact registry and block observations (one writer per directory)."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from ._io import append_jsonl, canonical_json, read_jsonl
from .exchange import imported_files, skill_reference
from .models import SkillArtifact, content_hash, global_skill_id, text_skill_files, valid_evidence


class GlobalSkillRegistry:
    def __init__(self, registry_dir: Path):
        self.directory = Path(registry_dir)
        self.artifacts_dir = self.directory / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.directory / "registry.jsonl"
        self.transfer_journal = self.directory / "transfer_events.jsonl"
        self._artifacts: dict[str, SkillArtifact] = {}
        self._observations: list[dict] = []
        for row in read_jsonl(self.journal):
            gid = row["global_skill_id"]
            if row["event"] == "skill_registered":
                if gid in self._artifacts:
                    raise ValueError(f"duplicate registry registration: {gid}")
                self._artifacts[gid] = self._read_artifact(gid)
            elif row["event"] != "skill_observed" or gid not in self._artifacts:
                raise ValueError("invalid registry observation")
            artifact = self._artifacts[gid]
            if (row["owner_agent"], row["local_skill_key"]) != (artifact.owner_agent, artifact.local_skill_key):
                raise ValueError("registry observation identity mismatch")
            self._observations.append(row)
        # Validate the transfer journal too; do not hide an incomplete write on restart.
        read_jsonl(self.transfer_journal)

    @property
    def checkpoint(self) -> int:
        return len(self._observations)

    @staticmethod
    def _validate_id(gid: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", gid):
            raise ValueError("invalid global skill ID")

    def _read_artifact(self, gid: str) -> SkillArtifact:
        self._validate_id(gid)
        root = self.artifacts_dir / gid
        if set(p.name for p in root.iterdir()) != {"SKILL.md", "metadata.json"}:
            raise ValueError(f"unsupported or incomplete registry artifact: {gid}")
        data = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        files = {"SKILL.md": (root / "SKILL.md").read_text(encoding="utf-8")}
        if data.get("skill_files") != files:
            raise ValueError(f"artifact manifest mismatch: {gid}")
        artifact = SkillArtifact(**data)
        digest = content_hash(files)
        if (artifact.content_hash != digest or artifact.global_skill_id != gid
                or global_skill_id(artifact.owner_agent, artifact.local_skill_key, digest) != gid):
            raise ValueError(f"artifact hash/identity mismatch: {gid}")
        return artifact

    def register_skill(
        self, owner_agent: str, skill_bundle: dict, source_task_ids=(), provenance: dict | None = None,
        *, block_id: str | None = None,
    ) -> SkillArtifact:
        provenance = copy.deepcopy(provenance or {})
        files = text_skill_files(skill_bundle["skill_files"])
        local_key = str(skill_bundle["local_skill_key"])
        if not owner_agent or not local_key:
            raise ValueError("owner and local key must be nonempty")
        parents = list(provenance.get("parent_skill_ids", []))
        evidence = dict(provenance.get("parent_evidence", {}))
        for parent in parents:
            item = evidence.get(parent, {})
            if (parent not in self._artifacts
                    or item.get("type") not in {"native_revision", "explicit_derivation"}):
                raise ValueError("parent requires a registered artifact and independent derivation evidence")
            if not valid_evidence(item, item["type"]):
                raise ValueError("parent evidence requires a source_ref")
        revision_parent = provenance.get("native_revision_parent_id")
        if revision_parent is not None:
            item = evidence.get(revision_parent, {})
            if revision_parent not in self._artifacts or not valid_evidence(item, "native_revision"):
                raise ValueError("native revision requires a registered predecessor and evidence")
        digest = content_hash(files)
        gid = global_skill_id(owner_agent, local_key, digest)
        artifact = SkillArtifact(
            global_skill_id=gid, owner_agent=owner_agent, local_skill_key=local_key,
            version=str(skill_bundle.get("version", "unknown")), content_hash=digest, skill_files=files,
            native_metadata=copy.deepcopy(skill_bundle.get("native_metadata", {})),
            parent_skill_ids=parents, parent_evidence=evidence,
            retrieved_parent_ids=list(provenance.get("retrieved_parent_ids", [])),
            invoked_parent_ids=list(provenance.get("invoked_parent_ids", [])),
            source_task_ids=list(source_task_ids),
            transformation_type=provenance.get("transformation_type", "unknown"),
            safety_status=provenance.get("safety_status", "unknown"),
            origin=skill_bundle.get("origin", "unknown"),
            imported_from_global_id=skill_bundle.get("imported_from_global_id"),
            native_revision_parent_id=revision_parent,
            unresolved_predecessor=copy.deepcopy(provenance.get("unresolved_predecessor")),
        )
        is_new = gid not in self._artifacts
        if is_new:
            root = self.artifacts_dir / gid
            if root.exists():
                # Adopt only an exact complete orphan from a crash before journal append.
                if self._read_artifact(gid) != artifact:
                    raise ValueError(f"conflicting uncommitted artifact: {gid}")
            else:
                root.mkdir()
                for filename, text in {"SKILL.md": files["SKILL.md"],
                                       "metadata.json": canonical_json(artifact.to_dict()) + "\n"}.items():
                    with (root / filename).open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write(text)
                        handle.flush()
                        os.fsync(handle.fileno())
        else:
            # Revalidate disk rather than allowing tampered history to pass through a live cache.
            self._read_artifact(gid)
        row = {
            "event": "skill_registered" if is_new else "skill_observed",
            "global_skill_id": gid, "owner_agent": owner_agent, "local_skill_key": local_key,
            "block_id": block_id, "source_task_ids": list(source_task_ids), "provenance": provenance,
        }
        append_jsonl(self.journal, row)
        if is_new:
            self._artifacts[gid] = artifact
        self._observations.append(row)
        return copy.deepcopy(self._artifacts[gid])

    def get_skill(self, gid: str) -> SkillArtifact:
        if gid not in self._artifacts:
            raise KeyError(gid)
        return self._read_artifact(gid)

    def register_evolution_events(self, owner_agent: str, events: list[dict], *, block_id: str) -> list[SkillArtifact]:
        """Persist every observed changed snapshot, never invent derivation from retrieval."""
        artifacts = []
        for event in events:
            if event["worker_id"] != owner_agent:
                raise ValueError("evolution event owner mismatch")
            for local_id, bundle in event["after"].items():
                before = event["before"].get(local_id)
                if (bundle.get("local_skill_key") != local_id
                        or (before is not None and before.get("local_skill_key") != local_id)):
                    raise ValueError("native evolution snapshot local ID mismatch")
                if before is not None and before["content_hash"] == bundle["content_hash"]:
                    continue
                task = event["task"]
                task_id = (task.get("task_run_id") or task.get("task_id")
                           or f"{owner_agent}:{task.get('kind')}:{task.get('pos')}")
                retrieved = [self.resolve_reference(owner_agent, ref)["global_skill_id"]
                             for ref in event.get("retrieved", [])]
                invoked = [self.resolve_reference(owner_agent, item["skill"])["global_skill_id"]
                           for item in event.get("invocation_evidence", [])
                           if valid_evidence(item.get("evidence", {}), "explicit_invocation")]
                provenance = {
                    "transformation_type": "native_update", "source_event_id": event["event_id"],
                    "retrieved_parent_ids": list(dict.fromkeys(gid for gid in retrieved if gid)),
                    "invoked_parent_ids": list(dict.fromkeys(gid for gid in invoked if gid)),
                }
                if before is not None:
                    predecessor = self.resolve_reference(owner_agent, before)
                    if predecessor["global_skill_id"]:
                        parent = predecessor["global_skill_id"]
                        provenance["native_revision_parent_id"] = parent
                        provenance["parent_evidence"] = {
                            parent: {"type": "native_revision", "source_ref": event["event_id"]},
                        }
                    else:
                        provenance["unresolved_predecessor"] = skill_reference(before)
                artifacts.append(self.register_skill(
                    owner_agent, bundle, [task_id], provenance,
                    block_id=block_id,
                ))
        return artifacts

    def verified_transfer(self, event: dict) -> bool:
        if (event.get("event") != "skill_transfer"
                or not all(event.get(key) is True for key in (
                    "success", "integrity_verified", "native_retrieval_verified",
                ))):
            return False
        gid = event.get("global_skill_id")
        if (gid not in self._artifacts or not isinstance(event.get("target_local_id"), str)
                or not event["target_local_id"] or not isinstance(event.get("target_agent"), str)
                or not event["target_agent"]):
            return False
        artifact = self.get_skill(gid)
        return (artifact.owner_agent == event.get("source_agent")
                and artifact.local_skill_key == event.get("source_local_id")
                and artifact.content_hash == event.get("content_hash")
                and event.get("target_agent") != event.get("source_agent"))

    def resolve_reference(self, worker_id: str, snapshot: dict) -> dict:
        """Resolve the exact observed version; a current local/global map is insufficient."""
        ref = skill_reference(snapshot)
        result = {**ref, "global_skill_id": None, "resolution": "unknown"}
        digest, local_id = ref["content_hash"], ref["local_skill_id"]
        if "skill_files" in snapshot and content_hash(snapshot["skill_files"]) != digest:
            raise ValueError("snapshot content hash mismatch")
        if not digest or not local_id:
            return result

        def resolve_import():
            for transfer in self.get_transfer_history():
                if (transfer.get("target_agent") != worker_id or transfer.get("target_local_id") != local_id
                        or not self.verified_transfer(transfer)):
                    continue
                gid = transfer["global_skill_id"]
                if ref["imported_global_skill_id"] and gid != ref["imported_global_skill_id"]:
                    continue
                expected = imported_files(self.export_skill(gid), local_id)
                if content_hash(expected) == digest:
                    return {**result, "global_skill_id": gid, "resolution": "verified_import"}
            return None

        if ref["origin"] == "imported":
            imported = resolve_import()
            if imported:
                return imported
        candidate = global_skill_id(worker_id, local_id, digest)
        if candidate in self._artifacts:
            self.get_skill(candidate)
            return {**result, "global_skill_id": candidate, "resolution": "exact_registered"}
        return resolve_import() or result

    def get_observations(self) -> list[dict]:
        return copy.deepcopy(self._observations)

    def export_skill(self, gid: str) -> dict[str, str]:
        return self.get_skill(gid).skill_files

    def get_skills_by_owner(self, owner_agent: str) -> list[SkillArtifact]:
        return [self.get_skill(gid) for gid, a in self._artifacts.items() if a.owner_agent == owner_agent]

    def get_new_versions(self, owner_agent: str, since_checkpoint: int) -> list[SkillArtifact]:
        if not 0 <= since_checkpoint <= self.checkpoint:
            raise ValueError("checkpoint outside journal")
        ids = dict.fromkeys(r["global_skill_id"] for r in self._observations[since_checkpoint:]
                            if r["owner_agent"] == owner_agent)
        return [self.get_skill(gid) for gid in ids]

    def select_transfer_versions(self, owner_agent: str, block_id: str) -> list[SkillArtifact]:
        """Final observation per local key, including deduplicated content reversions."""
        latest = {}
        for row in self._observations:
            if row["owner_agent"] == owner_agent and row["block_id"] == block_id:
                latest[row["local_skill_key"]] = row["global_skill_id"]
        return [self.get_skill(gid) for gid in latest.values()]

    def append_event(self, event: dict) -> None:
        append_jsonl(self.transfer_journal, event)

    def get_transfer_history(self) -> list[dict]:
        return read_jsonl(self.transfer_journal)
