"""Transfer immutable registry snapshots through the receiver's native SDK."""

from __future__ import annotations

import uuid

from .models import WorkerRuntime
from .registry import GlobalSkillRegistry


class SkillTransferManager:
    def __init__(self, registry: GlobalSkillRegistry, workers: dict[str, WorkerRuntime]):
        self.registry = registry
        self.workers = workers

    def transfer(self, global_skill_id: str, source_agent: str, target_agent: str) -> str:
        artifact = self.registry.get_skill(global_skill_id)
        if source_agent == target_agent or source_agent not in self.workers or target_agent not in self.workers:
            raise ValueError("transfer requires two distinct initialized workers")
        if artifact.owner_agent != source_agent:
            raise ValueError("transfer source must own the registered artifact")
        event = {
            "event": "skill_transfer", "attempt_id": uuid.uuid4().hex,
            "global_skill_id": global_skill_id, "source_agent": source_agent, "target_agent": target_agent,
            "source_local_id": artifact.local_skill_key, "target_local_id": None,
            "content_hash": artifact.content_hash, "success": False,
            "integrity_verified": False, "native_retrieval_verified": False,
            "relationship": "transferred_from", "error": None,
        }
        self.registry.append_event({**event, "event": "skill_transfer_started"})
        target = self.workers[target_agent]
        try:
            local_id = target.method.import_external_skill(
                artifact.skill_files, global_skill_id, source_agent=source_agent,
                source_local_id=artifact.local_skill_key,
            )
            target.remember_import(global_skill_id, local_id)
            event.update({
                "target_local_id": local_id, "success": True,
                "integrity_verified": True, "native_retrieval_verified": True,
            })
            return local_id
        except Exception as exc:
            record = target.method._exchange.imports.get(global_skill_id, {})
            event.update({
                "error": f"{type(exc).__name__}: {exc}", "target_local_id": record.get("local_id"),
                "integrity_verified": record.get("integrity_verified", False),
                "native_retrieval_verified": record.get("native_retrieval_verified", False),
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
        return self.transfer_batch([a.global_skill_id for a in artifacts], source_agent, target_agent)

    def get_transfer_history(self) -> list[dict]:
        return self.registry.get_transfer_history()
