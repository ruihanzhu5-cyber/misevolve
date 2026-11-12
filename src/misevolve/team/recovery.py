"""Recover a first M block, optionally retrying its trailing failed task.

Intentionally narrow: recovery after native evolution requires a clean checkpoint.
"""
import hashlib
import json
from datetime import datetime, timezone

from misevolve.harness import Trajectory
from misevolve.multiagent._io import atomic_json, canonical_json, read_jsonl
from .executor import AgentRuntime


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_first_block(root, expected, *, retry_failed=False):
    if (root / "recovery_manifest.json").exists():
        raise ValueError("this interrupted block already has a recovery attempt")
    old = read(root / "team_manifest.json")
    rows = read(root / "all_rows.json")
    if not 1 <= len(rows) <= 3 or any(
        row.get("pos") != i or row.get("task_kind") != "M"
        or row.get("orig_id") != expected["tasks"][i - 1]["instance_id"]
        for i, row in enumerate(rows, 1)
    ):
        raise ValueError("only the initial, partially recorded M block can be recovered")
    if old.get("recovery_history"):
        raise ValueError("multiple interrupted recoveries require an explicit migration")
    for key in ("episode_id", "communication_protocol", "edges", "agents", "max_rounds", "tasks",
                "exposure_submitter", "team_submitter", "probe_policy", "delivery_policy",
                "skill_read_evidence", "assessment_schema_version", "episode_sha256", "run_id",
                "replicate_id", "artifact_cache"):
        if canonical_json(old.get(key)) != canonical_json(expected.get(key)):
            raise ValueError(f"recovery would change experiment field: {key}")
    allowed = {
        "execution": {"image", "maintainer_format_retries_per_request", "provider_retries_per_request", "provider_transport_policy", "repeat_executor_timeouts_stop_episode"},
        "judge": {"fingerprint", "max_transport_attempts_per_request", "provider_transport_policy"},
        "runtime": {"image_id"},
    }
    for section, excluded in allowed.items():
        before = {k:v for k,v in (old.get(section) or {}).items() if k not in excluded}
        after = {k:v for k,v in (expected.get(section) or {}).items() if k not in excluded}
        if before != after:
            raise ValueError(f"recovery changes non-transport {section} configuration")
    events = read_jsonl(root / "team_events.jsonl")
    if any(e["event_type"] in {"NATIVE_MAINTENANCE", "SKILL_CREATED", "SKILL_UPDATED", "SKILL_IMPORTED"} for e in events):
        raise ValueError("recovery after Skill mutation requires a clean checkpoint")
    completed = {e.get("task_run_id"): e for e in events if e["event_type"] == "TASK_COMPLETED"}
    hashes = {}
    for row in rows:
        event = completed.get(row["task_run_id"])
        if not event or event.get("task_id") != row["orig_id"] or event.get("execution_status") != row["execution_status"]:
            raise ValueError("recorded task and completion journal disagree")
        path = root / "tasks" / f"{row['pos']:02}-{row['task_run_id']}" / "team_trajectory.json"
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("recovery trajectory outside run directory")
        record = read(path)
        if record["task_run_id"] != row["task_run_id"] or record["execution_status"] != row["execution_status"]:
            raise ValueError("trajectory identity differs from recorded row")
        if row["execution_status"] == "completed" and (
            not record["raw"]["turns"] or any(t["agent_id"] != "A" or t["execution_error"]
                                             for t in record["raw"]["turns"])
        ):
            raise ValueError("successful M task has incomplete learning evidence")
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (root / "workers").glob("*/evolution_events.jsonl"):
        if read_jsonl(path):
            raise ValueError("native ingest has already been attempted")
    superseded = []
    if retry_failed:
        if rows[-1]["execution_status"] != "failed" or any(
            row["execution_status"] != "completed" for row in rows[:-1]
        ):
            raise ValueError("retry requires one trailing failed task after a completed prefix")
        superseded = [rows[-1]]
        rows = rows[:-1]
    audit = {"recovered_at": datetime.now(timezone.utc).isoformat(), "prior_manifest": old,
             "new_code_sha": expected["code_sha"], "new_execution": expected["execution"],
             "new_judge": expected["judge"], "new_runtime": expected.get("runtime"),
             "preserved_task_ids": [r["orig_id"] for r in rows],
             "prior_rows_sha256": hashlib.sha256((root / "all_rows.json").read_bytes()).hexdigest(),
             "trajectory_sha256": hashes, "task_replay": retry_failed, "judge_replay": retry_failed,
             "successful_task_replay": False, "successful_judge_replay": False,
             "superseded_task_rows": superseded,
             "retry_positions": [row["pos"] for row in superseded],
             "reason": ("User requested retry of the trailing failed task; preserve completed tasks and prior failure evidence."
                        if retry_failed else "Resume after transient provider circuit stop; keep all recorded outcomes.")}
    return rows, audit


def record_recovery(root, manifest, audit):
    if audit["superseded_task_rows"]:
        prior = read(root / "all_rows.json")
        atomic_json(root / "pre_recovery_rows.json", prior)
    atomic_json(root / "recovery_manifest.json", audit)
    if audit["superseded_task_rows"]:
        replaced = {row["task_run_id"] for row in audit["superseded_task_rows"]}
        active = [row for row in prior if row["task_run_id"] not in replaced]
        atomic_json(root / "all_rows.json", active)
        from .assessment import behavior_summary
        atomic_json(root / "behavior_summary.json", behavior_summary(active))
    atomic_json(root / "team_manifest.json", {**manifest, "recovery_history": [{
        "prior_code_sha": audit["prior_manifest"]["code_sha"], "new_code_sha": manifest["code_sha"],
        "after_recorded_pos": len(audit["preserved_task_ids"]), "details": "recovery_manifest.json",
        "retry_positions": audit["retry_positions"],
    }]})


def restore_learning_trace(root, row, task, workers, audit):
    """Reconstruct exactly the existing learning_trace input from durable turns."""
    if row["execution_status"] != "completed":
        return None
    path = root / "tasks" / f"{row['pos']:02}-{row['task_run_id']}" / "team_trajectory.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != audit["trajectory_sha256"][path.relative_to(root).as_posix()]:
        raise ValueError("recorded trajectory changed during recovery")
    turns = read(path)["raw"]["turns"]
    worker = workers["A"]
    runtime = AgentRuntime(worker, worker.runner, path.parent / "A", f"{row['task_run_id']}:A")
    for turn in turns:
        runtime.inputs.append(turn["input"])
        runtime.traces.append(Trajectory(turn["messages"], turn["final_text"], turn["files_changed"],
                                         None, turn["trajectory"]))
    return runtime.learning_trace(task)
