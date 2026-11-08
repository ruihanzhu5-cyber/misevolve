"""Route native Skill imports without creating replacement author identities."""

from __future__ import annotations

from dataclasses import replace

from misevolve.multiagent.exchange import imported_files
from misevolve.multiagent.transfer import SkillTransferManager

from .router import TeamMessage


class SkillTransferService:
    def __init__(self, registry, workers, router):
        self.registry = registry
        self.workers = workers
        self.router = router
        self.bridge = SkillTransferManager(registry, workers)

    @staticmethod
    def identity(artifact) -> dict:
        return {
            "skill_id": artifact.global_skill_id, "skill_version": artifact.version,
            "parent_version": artifact.native_revision_parent_id, "source_agent": artifact.owner_agent,
            "content_hash": artifact.content_hash, "original_local_id": artifact.local_skill_key,
            "parent_skill_ids": artifact.parent_skill_ids,
        }

    def reference(self, sender: str, local_id: str):
        bundle = self.workers[sender].method._exchange.snapshot(local_id)
        ref = self.registry.resolve_reference(sender, bundle)
        if not ref["global_skill_id"]:
            raise ValueError("Skill has no verified Registry version")
        return self.registry.get_skill(ref["global_skill_id"]), ref["resolution"] == "verified_import"

    def publish(self, message: TeamMessage, *, frozen: bool = False) -> TeamMessage | None:
        # Enforce scope before touching either local store.
        if (frozen or message.episode_id != self.router.log.episode_id
                or not self.router.can_send(message.sender_id, message.receiver_id, message.message_type)):
            self.router.route(message, blocked_reason="probe_store_frozen" if frozen else None)
            return None
        local_id = message.payload.get("local_skill_id")
        try:
            artifact, forwarded = self.reference(message.sender_id, local_id)
        except (ValueError, KeyError):
            self.router.route(message, blocked_reason="unverified_source_version")
            return None
        message = replace(message, message_type="SKILL_FORWARD" if forwarded else "SKILL_PAYLOAD",
                          payload=self.identity(artifact))
        routed = self.router.route(message)
        if not routed.allowed:
            return None
        self.router.log.message("SKILL_PUBLISHED", message, allowed=True, delivery_status="pending")
        worker = self.workers[message.receiver_id]
        existing = worker.imported_global_to_local.get(artifact.global_skill_id)
        if existing is not None:
            current = worker.method._exchange.snapshot(existing)
            if current["skill_files"] != imported_files(artifact.skill_files, existing) or current.get(
                "resource_files",
            ):
                self.router.log.message("MESSAGE_BLOCKED", message, allowed=False, delivery_status="blocked",
                                        reason="receiver_local_version_changed")
                return None
        replay = any(row.get("delivery_id") == message.message_id and self.registry.verified_transfer(row)
                     for row in self.registry.get_transfer_history())
        try:
            receiver_local_id = self.bridge.transfer(
                artifact.global_skill_id, message.sender_id, message.receiver_id,
                delivery_id=message.message_id, forward_local_id=local_id if forwarded else None,
            )
        except Exception as exc:
            self.router.log.message("SKILL_TRANSFER_FAILED", message, allowed=True, delivery_status="failed",
                                    error_type=type(exc).__name__)
            raise
        event = next(row for row in reversed(self.registry.get_transfer_history())
                     if row.get("delivery_id") == message.message_id and self.registry.verified_transfer(row))
        delivery = {
            "allowed": True, "delivery_status": "delivered", "target_local_id": receiver_local_id,
            "attempt_id": event["attempt_id"], "forwarded_from_attempt_id": event.get("forwarded_from_attempt_id"),
            "omitted_resources": artifact.resource_files,
        }
        self.router.log.message("SKILL_TRANSFER_REPLAYED" if replay else "SKILL_TRANSFERRED", message, **delivery)
        if not replay:
            self.router.log.message("SKILL_IMPORTED", message, **delivery, origin="imported")
            if forwarded:
                self.router.log.message("SKILL_FORWARDED", message, **delivery, origin="forwarded_unchanged")
        self.router.delivered(routed)
        return replace(message, payload={**message.payload, "local_skill_id": receiver_local_id})
