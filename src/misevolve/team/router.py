"""Phase 3 message contracts, routing rules and append-only audit events."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from misevolve.multiagent._io import append_jsonl, canonical_json

AGENTS = ("A", "B", "C")
MESSAGE_TYPES = frozenset({
    "TASK_CONTEXT", "PLAN", "SUBTASK", "RESULT", "FEEDBACK", "SKILL_OFFER",
    "SKILL_ACCEPT", "SKILL_REJECT", "SKILL_PAYLOAD", "SKILL_FORWARD", "FINAL_ANSWER", "ARTIFACT_HANDOFF",
})
SKILL_FIELDS = ("skill_id", "skill_version", "parent_version", "source_agent", "content_hash")


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TeamMessage:
    episode_id: str
    task_id: str
    sender_id: str
    receiver_id: str
    message_type: str
    payload: dict
    parent_message_id: str | None = None
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=timestamp)


@dataclass(frozen=True)
class RoutedMessage:
    message: TeamMessage
    allowed: bool
    reason: str | None = None


class TeamEventLogger:
    def __init__(self, path: Path, episode_id: str, protocol: str):
        self.path = Path(path)
        self.episode_id = episode_id
        self.protocol = protocol

    def record(self, event_type: str, task_id: str | None, **fields) -> dict:
        row = {
            "episode_id": self.episode_id, "task_id": task_id,
            "event_id": uuid.uuid4().hex, "timestamp": timestamp(),
            "event_type": event_type, "communication_protocol": self.protocol,
            "communication_channel": "internal",
            "agent_id": None, "sender_id": None, "receiver_id": None,
            **dict.fromkeys(SKILL_FIELDS), "payload_size": None,
            "allowed": None, "delivery_status": "UNKNOWN", "adoption_status": "UNKNOWN",
            "behavioral_outcome": None, **fields,
        }
        append_jsonl(self.path, row)
        return row

    def message(self, event_type: str, message: TeamMessage, **fields) -> dict:
        values = dict(message.payload)
        if message.message_type in {"SKILL_PAYLOAD", "SKILL_FORWARD"} and values.get("skill_id"):
            receiver_local_id = values.pop("local_skill_id", None)
            if receiver_local_id is not None:
                fields.setdefault("target_local_id", receiver_local_id)
        payload = canonical_json(values).encode("utf-8")
        return self.record(
            event_type, message.task_id, agent_id=message.sender_id,
            sender_id=message.sender_id, receiver_id=message.receiver_id,
            communication_channel=("skill" if message.message_type.startswith("SKILL_") else
                                   "artifact" if message.message_type == "ARTIFACT_HANDOFF" else "message"),
            message_type=message.message_type, message_id=message.message_id,
            parent_message_id=message.parent_message_id, message_timestamp=message.timestamp,
            payload_size=len(payload), payload_hash=hashlib.sha256(payload).hexdigest(),
            **{k: message.payload.get(k) for k in SKILL_FIELDS if message.message_type.startswith("SKILL_")},
            **fields,
        )


class OrganizationRouter:
    protocol: str
    edges: frozenset[tuple[str, str]]

    def __init__(self, log: TeamEventLogger):
        if log.protocol != self.protocol:
            raise ValueError("router and audit protocol differ")
        self.log = log

    def can_send(self, sender_id: str, receiver_id: str, message_type: str) -> bool:
        return message_type in MESSAGE_TYPES and (sender_id, receiver_id) in self.edges

    def allowed_recipients(self, sender_id: str) -> list[str]:
        return [receiver for receiver in AGENTS if (sender_id, receiver) in self.edges]

    def get_protocol_name(self) -> str:
        return self.protocol

    def route(self, message: TeamMessage, *, blocked_reason: str | None = None) -> RoutedMessage:
        self.log.message("MESSAGE_SENT", message)
        reason = blocked_reason
        if message.episode_id != self.log.episode_id:
            reason = "episode_mismatch"
        elif not self.can_send(message.sender_id, message.receiver_id, message.message_type):
            reason = "edge_or_type_not_allowed"
        elif message.message_type.startswith("SKILL_") and (
            any(field not in message.payload for field in SKILL_FIELDS)
            or not message.payload["skill_id"]
        ):
            reason = reason or "missing_skill_identity"
        if reason:
            self.log.message("MESSAGE_BLOCKED", message, allowed=False, delivery_status="blocked", reason=reason)
        return RoutedMessage(message, reason is None, reason)

    def delivered(self, routed: RoutedMessage) -> None:
        message = routed.message
        if (not routed.allowed or message.episode_id != self.log.episode_id
                or not self.can_send(message.sender_id, message.receiver_id, message.message_type)):
            raise ValueError("a blocked message cannot be delivered")
        self.log.message("MESSAGE_DELIVERED", message, allowed=True, delivery_status="delivered")


class HierarchicalRouter(OrganizationRouter):
    protocol = "hierarchical"
    edges = frozenset({("A", "B"), ("A", "C"), ("B", "A"), ("C", "A")})


class ChainRouter(OrganizationRouter):
    protocol = "chain"
    edges = frozenset({("A", "B"), ("B", "C")})


class PeerToPeerRouter(OrganizationRouter):
    protocol = "peer_to_peer"
    edges = frozenset((sender, receiver) for sender in AGENTS for receiver in AGENTS if sender != receiver)
