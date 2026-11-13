"""Split manifests are executable access controls, not prompt conventions."""

from __future__ import annotations

from copy import deepcopy

from misevolve.multiagent._io import append_jsonl

from .policy import digest

PURPOSES = {
    "online",
    "development",
    "inner_confirmation",
    "policy_selection",
    "outer_confirmation",
    "final_learning",
    "final_evaluation",
}
PERMISSIONS = {
    "inner": {"online", "development", "policy_selection", "final_learning"},
    "validator": {"development"},
    "confirmer": {"inner_confirmation", "outer_confirmation"},
    "shadow": {"policy_selection"},
    "replay": {"final_learning"},
    "evaluator": {"final_evaluation"},
}
SEALED = {"inner_confirmation", "outer_confirmation", "final_evaluation"}


class SplitManifest:
    def __init__(self, value, access_path):
        if set(value) != {"schema_version", "tasks"} or value["schema_version"] != 1:
            raise ValueError("unsupported split manifest")
        self.value = deepcopy(value)
        self.access_path = access_path
        ids, hashes = set(), set()
        for row in self.value["tasks"]:
            required = {"purpose", "sequence", "shard", "source_sha256", "task", "episode"}
            if set(row) != required or row["purpose"] not in PURPOSES:
                raise ValueError("invalid split entry")
            task = row["task"]
            if not {"instance_id", "prompt", "kind", "round", "pos"} <= task.keys():
                raise ValueError("task is missing executor fields")
            # IDs and normalized prompt hashes cannot be relabeled to evade partition checks.
            source = digest(task["prompt"].strip())
            if row["source_sha256"] != source or task["instance_id"] in ids or source in hashes:
                raise ValueError("duplicate task/partition conflict or source hash mismatch")
            if task["kind"] not in {"M", "B", "P"} or not row["sequence"] or not row["shard"]:
                raise ValueError("invalid task kind, sequence or shard")
            if row["purpose"] in {"policy_selection", "final_learning"} and task["kind"] == "P":
                raise ValueError("Probe tasks cannot be repurposed for learning")
            if not isinstance(row["episode"].get("malicious_goal"), str):
                raise ValueError("fixed scorer requires episode malicious_goal (never sent to improver)")
            ids.add(task["instance_id"])
            hashes.add(source)

    @property
    def hash(self):
        return digest(self.value)

    def select(self, purpose, *, actor, shard=None, sequence=None):
        if purpose not in PERMISSIONS.get(actor, set()):
            raise PermissionError(f"{actor} cannot access {purpose}")
        selected = [
            row
            for row in self.value["tasks"]
            if row["purpose"] == purpose
            and (shard is None or row["shard"] == shard)
            and (sequence is None or row["sequence"] == sequence)
        ]
        append_jsonl(
            self.access_path,
            {
                "actor": actor,
                "purpose": purpose,
                "shard": shard,
                "task_ids": [r["task"]["instance_id"] for r in selected],
            },
        )
        return deepcopy(selected)

    def validate_online(self, episode):
        actual = {r["task"]["instance_id"]: r["task"] for r in self.value["tasks"] if r["purpose"] == "online"}
        if actual != {t["instance_id"]: t for t in episode["task_sequence"]}:
            raise ValueError("online split must contain the exact original episode, including frozen P tasks")


def visible_trace(trace):
    """Only member-visible conversation, never benchmark labels, goals or Judge verdicts."""
    return {"messages": deepcopy(trace.messages), "final_text": trace.final_text, "execution_error": bool(trace.error)}
