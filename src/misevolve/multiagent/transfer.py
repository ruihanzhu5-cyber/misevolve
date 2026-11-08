"""Transfer immutable registry snapshots through the receiver's native SDK."""

from __future__ import annotations

import uuid

from ._io import append_jsonl
from .exchange import imported_files
from .models import WorkerRuntime
from .registry import GlobalSkillRegistry


class SkillTransferManager:
    def __init__(self, registry: GlobalSkillRegistry, workers: dict[str, WorkerRuntime]):
        self.registry = registry
        self.workers = workers

    def transfer(
        self, global_skill_id: str, source_agent: str, target_agent: str, *,
        delivery_id: str | None = None, forward_local_id: str | None = None,
    ) -> str:
        artifact = self.registry.get_skill(global_skill_id)
        if source_agent == target_agent or source_agent not in self.workers or target_agent not in self.workers:
            raise ValueError("transfer requires two distinct initialized workers")
        if artifact.owner_agent != source_agent and forward_local_id is None:
            raise ValueError("transfer source must own the registered artifact")
        if forward_local_id is not None and delivery_id is None:
            raise ValueError("forwarding requires a durable delivery ID")
        history = self.registry.get_transfer_history() if delivery_id is not None else []
        for previous in history:
            if previous.get("delivery_id") != delivery_id:
                continue
            if (previous["global_skill_id"], previous["source_agent"], previous["target_agent"],
                    previous.get("forward_local_id")) != (
                    global_skill_id, source_agent, target_agent, forward_local_id):
                raise ValueError("delivery ID is already bound to another transfer")
            if self.registry.verified_transfer(previous):
                return previous["target_local_id"]
        event = {
            "event": "skill_transfer", "attempt_id": uuid.uuid4().hex,
            "global_skill_id": global_skill_id, "source_agent": source_agent, "target_agent": target_agent,
            "source_local_id": artifact.local_skill_key, "target_local_id": None,
            "source_version": artifact.version, "target_content_hash": None,
            "content_hash": artifact.content_hash, "success": False,
            "omitted_resources": artifact.resource_files,
            "payload_scope": "SKILL.md_only",
            "integrity_verified": False, "native_retrieval_verified": False,
            "relationship": "transferred_from", "error": None,
        }
        if delivery_id is not None:
            event.update(delivery_id=delivery_id, forward_local_id=forward_local_id)
        if forward_local_id is not None:
            source = self.workers[source_agent].method._exchange.snapshot(forward_local_id)
            predecessor = next((row for row in reversed(history)
                                if row.get("global_skill_id") == global_skill_id
                                and row.get("target_agent") == source_agent
                                and row.get("target_local_id") == forward_local_id
                                and self.registry.verified_transfer(row)), None)
            if (predecessor is None or source.get("origin") != "imported"
                    or source.get("imported_from_global_id") != global_skill_id
                    or source["skill_files"] != imported_files(artifact.skill_files, forward_local_id)
                    or source.get("resource_files")):
                raise ValueError("forwarding requires an intact, verified import")
            event.update(source_local_id=forward_local_id, original_source_agent=artifact.owner_agent,
                         original_source_local_id=artifact.local_skill_key,
                         forwarded_from_attempt_id=predecessor["attempt_id"])
        self.registry.append_event({**event, "event": "skill_transfer_started"})
        target = self.workers[target_agent]
        try:
            local_id = target.method.import_external_skill(
                artifact.skill_files, global_skill_id, source_agent=artifact.owner_agent,
                source_local_id=artifact.local_skill_key,
            )
            target.remember_import(global_skill_id, local_id)
            record = target.method._exchange.imports[global_skill_id]
            event.update({
                "target_local_id": local_id, "success": True,
                "integrity_verified": True, "native_retrieval_verified": True,
                "target_content_hash": record["target_content_hash"],
                "receiver_frontmatter_id_adapted": record["target_content_hash"] != artifact.content_hash,
            })
            return local_id
        except Exception as exc:
            record = target.method._exchange.imports.get(global_skill_id, {})
            event.update({
                "error": f"{type(exc).__name__}: {exc}", "target_local_id": record.get("local_id"),
                "integrity_verified": record.get("integrity_verified", False),
                "native_retrieval_verified": record.get("native_retrieval_verified", False),
                "target_content_hash": record.get("target_content_hash"),
            })
            raise
        finally:
            self.registry.append_event(event)

    def transfer_batch(self, global_skill_ids, source_agent: str, target_agent: str) -> list[str]:
        ids = list(dict.fromkeys(global_skill_ids))
        if not ids:
            self.registry.append_event({
                "event": "empty_transfer", "source_agent": source_agent, "target_agent": target_agent,
                "global_skill_ids": [], "success": True,
            })
        return [self.transfer(gid, source_agent, target_agent) for gid in ids]

    def transfer_block(self, source_agent: str, target_agent: str, *, block_id: str) -> list[str]:
        artifacts = self.registry.select_transfer_versions(source_agent, block_id)
        append_jsonl(self.registry.directory.parent / "transfer_manifest.jsonl", {
            "block_id": block_id, "source_agent": source_agent, "target_agent": target_agent,
            "selection": [{
                "global_skill_id": a.global_skill_id, "source_local_id": a.local_skill_key,
                "version": a.version, "content_hash": a.content_hash, "files": sorted(a.skill_files),
                "omitted_resources": a.resource_files,
                "native_revision_parent_id": a.native_revision_parent_id,
            } for a in artifacts],
        })
        return self.transfer_batch([a.global_skill_id for a in artifacts], source_agent, target_agent)

    def get_transfer_history(self) -> list[dict]:
        return self.registry.get_transfer_history()
