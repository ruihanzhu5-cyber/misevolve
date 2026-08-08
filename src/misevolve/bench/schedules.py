"""Exposure-timing and batch-mixing schedules from the paper."""

from __future__ import annotations

_SCHEDULES = {
    "early": ["MMM", "MMM", "MMM", "BBB", "BBB", "BBB"],
    "interleaved": ["MMM", "BBB", "MMM", "BBB", "MMM", "BBB"],
    "late": ["BBB", "BBB", "BBB", "MMM", "MMM", "MMM"],
    "batched": ["MMM", "BBB", "MMM", "BBB", "MMM", "BBB"],
    "partial": ["MMM", "BBB", "MMB", "MBB", "MMB", "MBB"],
    "fully": ["MMB", "MBB", "MMB", "MBB", "MMB", "MBB"],
}

TIMING_SCHEDULES = ("early", "interleaved", "late")
MIXING_SCHEDULES = ("batched", "partial", "fully")


def _validate(schedule: list[str]) -> list[str]:
    malicious = sum(block.count("M") for block in schedule)
    benign = sum(block.count("B") for block in schedule)
    if malicious != 9 or benign != 9:
        raise ValueError(f"schedule must contain 9M and 9B, got {malicious}M and {benign}B")
    return schedule


def _rotate(schedule: list[str], offset: int) -> list[str]:
    return [block[offset % 3 :] + block[: offset % 3] for block in schedule]


def schedule_blocks(name: str, episode_index: int) -> list[str]:
    normalized = name.lower()
    if normalized not in _SCHEDULES:
        raise KeyError(f"unknown schedule {name!r}; choose from {sorted(_SCHEDULES)}")
    schedule = list(_SCHEDULES[normalized])
    if normalized in MIXING_SCHEDULES:
        if episode_index % 2:
            schedule.reverse()
        schedule = _rotate(schedule, episode_index % 3)
    return _validate(schedule)


def checkpoint_blocks() -> dict[int, int]:
    return {0: 0, 3: 2, 6: 4, 9: 6}


def build_task_sequence(
    malicious_tasks: list[dict],
    benign_tasks: list[dict],
    persistence_tasks: list[dict],
    name: str,
    episode_index: int,
) -> list[dict]:
    if (len(malicious_tasks), len(benign_tasks), len(persistence_tasks)) != (9, 9, 3):
        raise ValueError("a scheduled episode requires 9 malicious, 9 benign, and 3 probes")
    malicious_index = benign_index = 0
    sequence: list[dict] = []
    position = 0
    for block_index, block in enumerate(schedule_blocks(name, episode_index)):
        for kind in block:
            if kind == "M":
                task = malicious_tasks[malicious_index]
                malicious_index += 1
            else:
                task = benign_tasks[benign_index]
                benign_index += 1
            position += 1
            sequence.append(
                {
                    "pos": position,
                    "block": block_index,
                    "round": block_index // 2 + 1,
                    "kind": kind,
                    "role": None,
                    "instance_id": task["instance_id"],
                    "label": task.get("label", ""),
                    "prompt": task["prompt"],
                }
            )
    for task in persistence_tasks:
        position += 1
        sequence.append(
            {
                "pos": position,
                "block": None,
                "round": 0,
                "kind": "P",
                "role": "persistence",
                "instance_id": task["instance_id"],
                "label": task.get("label", ""),
                "prompt": task["prompt"],
            }
        )
    return sequence
