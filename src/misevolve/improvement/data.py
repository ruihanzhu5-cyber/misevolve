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
        version = value.get("schema_version")
        keys = {"schema_version", "tasks"} | ({"grouping", "dataset_sha256"} if version == 2 else set())
        if set(value) != keys or version not in {1, 2}:
            raise ValueError("unsupported split manifest")
        if version == 2 and (value["grouping"] != "source_episode" or len(value["dataset_sha256"]) != 64):
            raise ValueError("v2 requires source-episode grouping and pinned dataset hash")
        self.value = deepcopy(value)
        self.access_path = access_path
        ids, hashes = set(), set()
        groups = {}
        for row in self.value["tasks"]:
            required = {"purpose", "sequence", "shard", "source_sha256", "task", "episode"}
            if version == 2:
                required.add("group_id")
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
            if version == 2:
                group = row["group_id"]
                if not group or group != row["episode"].get("episode_id"):
                    raise ValueError("group_id must identify the original source episode")
                partition = (row["purpose"], row["shard"] if row["purpose"] in SEALED else None)
                if group in groups and groups[group] != partition:
                    raise ValueError("source episode crosses purposes or sealed confirmation shards")
                groups[group] = partition
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
