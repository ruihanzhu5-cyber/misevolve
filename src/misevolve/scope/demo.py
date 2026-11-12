"""Run the complete SCOPE control flow without API, Docker, or native SDK calls.

python -m misevolve.scope.demo --out /tmp/scope-mock
Every result is marked synthetic. Never merge these rows into benchmark metrics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from misevolve.multiagent._io import atomic_json
from misevolve.paths import repo_root

from .controller import ScopeController
from .mock import MOCK_CONFIGURATION, MockTeamFactory, mock_cases, mock_config, mock_reviewer, mock_scorer, mock_version
from .models import digest
from .store import ScopeStore
from .validation import PairedValidator


def run_demo(out: Path) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    data = json.loads((repo_root() / "data/episodes/episodes_directive.json").read_text(encoding="utf-8"))
    episodes = data["episodes"] if isinstance(data, dict) else data
    episode = next(ep for ep in episodes if ep["episode_id"] == "skillmisevo-coding-04")
    tasks = episode["task_sequence"]
    assert [task["pos"] for task in tasks] == list(range(1, 22))
    report = {"synthetic": True, "real_api_calls": 0, "native_autoskill_calls": 0,
              "episode": episode["episode_id"], "task_sequence_hash": digest(tasks), "conditions": {}}
    for topology in ("chain", "hierarchical", "peer-to-peer"):
        store = ScopeStore(out / topology)
        store.initialize()
        configuration = {**MOCK_CONFIGURATION, "topology": topology}
        factory = MockTeamFactory(store)
        validator = PairedValidator(store, factory, mock_scorer, configuration)
        controller = ScopeController(store, validator, mock_config(), reviewer=mock_reviewer)
        cycles, task_records = [], []
        for start in range(0, 21, 3):
            block = tasks[start:start + 3]
            base = store.current()
            # All three benchmark tasks see the same approved release and unchanged original prompts.
            for task in block:
                path = out / topology / "benchmark" / str(task["pos"])
                path.mkdir(parents=True)
                store.materialize(base, path / "library")
                runtime = factory(snapshot_id=base, library_dir=path / "library", out_dir=path,
                                  configuration=configuration)
                try:
                    result = runtime.run(task, seed=0, out_dir=path)
                finally:
                    runtime.close()
                row = {"synthetic": True, "task": task, "snapshot": base, "execution": result}
                atomic_json(path / "result.json", row)
                task_records.append(row)
            ids = []
            cases = []
            if block[0]["kind"] != "P":
                # Mock Candidate Store input. Real collection uses AutoSkillCandidateCollector.
                key = f"A:mock-block-{start // 3 + 1}"
                text = "UNSAFE" if start == 6 else "SAFE"
                ids = [store.stage(base=base, skill_id=key, after=mock_version(key, text),
                                   source_tasks=[task["instance_id"] for task in block])]
                cases = mock_cases(prefix=f"block-{start}")
            cycles.append(controller.run(f"block-{start // 3 + 1}", base=base, candidates=ids,
                                         cases=cases, block_tasks=block))
        report["conditions"][topology] = {
            "task_count": len(task_records), "task_order": [row["task"]["instance_id"] for row in task_records],
            "task_sequence_hash": digest([row["task"] for row in task_records]),
            "cycle_statuses": [cycle["status"] for cycle in cycles],
            "validation_pairs": sum(cycle["pairs_spent"] for cycle in cycles),
            "runtime_executions": len(factory.executions), "all_runtimes_closed": all(r.closed for r in factory.runtimes),
            "probe_snapshot_fixed": len({row["snapshot"] for row in task_records[-3:]}) == 1,
            "final_snapshot": store.current(), "configuration": configuration,
        }
    atomic_json(out / "mock_report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = run_demo(args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
