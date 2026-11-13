"""Build a bounded real-benchmark engineering pilot, without changing task text.

Source-episode isolation is enforced. This is not a concept/template-disjoint
research partition, a full 25-episode comparison, or a statistical effectiveness test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from misevolve.multiagent._io import atomic_json
from misevolve.paths import repo_root

from .config import load_config
from .policy import digest


def build(output: Path, dataset: Path | None = None):
    dataset = dataset or repo_root() / "data" / "episodes" / "episodes_directive.json"
    raw = dataset.read_bytes()
    episodes = {e["episode_id"]: e for e in json.loads(raw)["episodes"]}
    rows = []

    def add(number, purpose, positions, shard):
        episode = episodes[f"skillmisevo-coding-{number:02}"]
        for task in episode["task_sequence"]:
            if task["pos"] not in positions:
                continue
            rows.append({"purpose": purpose, "sequence": episode["episode_id"], "shard": shard,
                         "group_id": episode["episode_id"], "source_sha256": digest(task["prompt"].strip()),
                         "task": deepcopy(task),
                         "episode": {k: deepcopy(v) for k, v in episode.items() if k != "task_sequence"}})

    add(1, "online", range(1, 22), "online")
    add(2, "development", [4], "development")
    for window in range(1, 7):
        add(window + 2, "inner_confirmation", [4], f"online-{window}")
    add(9, "final_learning", [4, 5, 6], "reserved-learning")
    add(10, "final_evaluation", [4], "reserved-evaluation")
    manifest = {"schema_version": 2, "grouping": "source_episode",
                "dataset_sha256": hashlib.sha256(raw).hexdigest(), "tasks": rows}
    config = json.loads((repo_root() / "configs" / "ours" / "example.json").read_text(encoding="utf-8"))
    config["policy"]["gen"] += " Propose at most one change per member per window."
    config["policy"]["rep"] += " Propose at most one repair change per member per attempt."
    config["protocol"].update(min_units=1, max_changes_per_agent=1)
    config["splits"] = "splits.json"
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "splits.json", manifest)
    atomic_json(output / "config.json", config)
    _, _, checked, _ = load_config(output / "config.json", output / "access.jsonl")
    checked.validate_online(episodes["skillmisevo-coding-01"])
    atomic_json(output / "plan.json", {
        "dataset": "SkillMisevo-Bench", "dataset_sha256": manifest["dataset_sha256"],
        "dataset_episodes": len(episodes), "dataset_tasks": sum(len(e["task_sequence"]) for e in episodes.values()),
        "episode": "skillmisevo-coding-01", "topology": "chain", "improvement": "fixed_theta",
        "first_checkpoint_pos": 3, "second_checkpoint_pos": 6, "full_episode_tasks": 21,
        "grouping": "source_episode", "min_units": 1, "confirmation_mode": "empirical",
        "limitations": ["engineering pilot, not effectiveness evidence", "one episode and one replicate",
                        "no concept/template/lineage-disjoint claim", "no adaptive outer-loop experiment",
                        "final pools reserved; not automatically evaluated"],
        "assignments": [{k: row[k] for k in ("purpose", "sequence", "shard", "group_id")}
                        | {"task_id": row["task"]["instance_id"]} for row in rows],
    })
    return output / "config.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.out))


if __name__ == "__main__":
    main()
