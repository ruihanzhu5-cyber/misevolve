"""Validate a stopped episode at a boundary with no in-flight Skill writes."""

import json
from dataclasses import dataclass
from pathlib import Path

from ._io import read_jsonl


@dataclass(frozen=True)
class EpisodeCheckpoint:
    rows: list[dict]
    tasks: list[dict]
    completed_batches: int
    last_task: dict | None
    interrupted: dict | None
    pending_evolution: list[dict] | None


def _unsupported_addition(directory, batch, task_rows):
    events = read_jsonl(directory / "workers" / batch.worker_id / "evolution_events.jsonl")
    ids = [row["task_run_id"] for row in task_rows if row["runner_success"]]
    pending = [event for event in events if event["task"]["task_run_id"] in ids]
    if not pending or [event["task"]["task_run_id"] for event in pending] != ids:
        raise ValueError("unsupported addition did not occur after the last required ingest")
    if any(event["worker_id"] != batch.worker_id for event in pending) or any(
        previous["after"] != following["before"] for previous, following in zip(pending, pending[1:], strict=False)
    ):
        raise ValueError("native ingest snapshots are discontinuous")
    failed = pending[-1]
    returned = failed["returned_skills"]
    if (any(event["success"] is not True for event in pending[:-1]) or failed["success"] is not False
            or failed["error"] is not None or failed["after"] or len(returned) != 1
            or not failed.get("snapshot_error", "").startswith("UnsupportedSkillError: unsupported resource file ")
            or failed["updated_local_skill_ids"] != [returned[0]["id"]]
            or returned[0]["id"] in failed["before"] or not (set(returned[0]["files"]) - {"SKILL.md"})):
        raise ValueError("checkpoint cannot isolate one newly created Skill with unsupported resources")
    return pending


def _verify_transfers(directory, routes):
    manifests = read_jsonl(directory / "transfer_manifest.jsonl")
    journal = iter(read_jsonl(directory / "registry/transfer_events.jsonl"))
    for (source, target, block, local_ids), manifest in zip(routes, manifests, strict=True):
        if (source, target, block) != (manifest["source_agent"], manifest["target_agent"], manifest["block_id"]):
            raise ValueError("transfer manifest does not match the checkpoint")
        selection = manifest["selection"]
        if not selection:
            event = next(journal, {})
            if (local_ids or event.get("event") != "empty_transfer" or event.get("success") is not True
                    or (event.get("source_agent"), event.get("target_agent")) != (source, target)):
                raise ValueError("checkpoint lacks an empty transfer event")
        for selected, local_id in zip(selection, local_ids, strict=True):
            started, finished = next(journal, {}), next(journal, {})
            expected = (source, target, selected["global_skill_id"])
            if (started.get("event") != "skill_transfer_started" or finished.get("event") != "skill_transfer"
                    or not started.get("attempt_id") or started["attempt_id"] != finished.get("attempt_id")
                    or any((event.get("source_agent"), event.get("target_agent"), event.get("global_skill_id"))
                           != expected for event in (started, finished))
                    or finished.get("success") is not True or finished.get("target_local_id") != local_id):
                raise ValueError("checkpoint lacks a committed Skill import")
    if next(journal, None) is not None:
        raise ValueError("checkpoint has transfer records without a completed block")


def load_checkpoint(directory: Path, plan: dict, batches) -> EpisodeCheckpoint:
    def read(name):
        return json.loads((directory / name).read_text(encoding="utf-8"))

    if read("episode_plan.json") != plan:
        raise ValueError("checkpoint episode plan differs from the requested plan")
    progress = read("progress.json")
    resource_stop = (progress.get("failed_phase") == "native_evolution"
                     and progress.get("error_type") == "UnsupportedSkillError")
    if progress["phase"] != "incomplete" or not (
        resource_stop or progress.get("failed_phase") in {"task_execution", "task_finished"}
    ):
        raise ValueError("continuation requires a stopped task boundary outside evolution, transfer and probe capture")
    rows = read("all_rows.json")
    tasks = [row for row in rows if row["kind"] != "artifact"]
    entries = plan["tasks"]
    if len(tasks) >= len(entries) or progress["attempted_tasks"] != len(tasks):
        raise ValueError("checkpoint task count is inconsistent or the episode is finished")
    events = read_jsonl(directory / "episode_events.jsonl")
    starts = {row["task_run_id"]: row for row in events if row["event"] == "task_started"}
    if any(not 1 <= row["execution_index"] <= len(entries)
           or any(row[k] != entries[row["execution_index"] - 1][k] for k in entries[0]) for row in starts.values()):
        raise ValueError("checkpoint contains an unexpected task attempt")
    commits = iter(row for row in events if row["event"] not in {"workers_ready", "task_started", "episode_resumed"})
    event = next(commits, None)
    finished = []
    batches_done = 0
    native_ids, artifact_ids, routes = [], set(), []
    probes = iter(read_jsonl(directory / "probe_snapshots.jsonl"))
    pending_evolution = None

    for batch in batches:
        batch_ids = []
        for _ in batch.tasks:
            if len(finished) == len(tasks):
                break
            index = len(finished)
            row, entry = tasks[index], entries[index]
            run_id = row["task_run_id"]
            if (event is None or event["event"] != "task_finished" or event["task_run_id"] != run_id
                    or run_id not in starts or run_id in finished or event["runner_success"] != row["runner_success"]
                    or any(event[k] != value or row[k] != value or starts[run_id][k] != value
                           for k, value in entry.items())):
                raise ValueError("checkpoint rows and finished-task events do not match the schedule")
            if row["runner_success"]:
                path = directory / "workers" / row["worker_id"] / f"p{row['source_pos']:02d}_{row['task_kind']}"
                if not (path / "trajectory_raw.json").is_file():
                    raise ValueError("a completed task lacks its raw trajectory")
            finished.append(run_id)
            batch_ids.append(run_id)
            event = next(commits, None)
        if len(batch_ids) != len(batch.tasks):
            break
        if batch.tasks[0]["kind"] == "P":
            probe = next(probes, None)
            if (probe is None or probe["task_run_id"] != batch_ids[0]
                    or probe["worker_id"] != batch.worker_id or probe["unchanged"] is not True):
                raise ValueError("checkpoint contains an unfinished frozen probe")
        else:
            if event is None:
                break  # All tasks in this batch finished, and no evolution has started.
            for name in ("evolution_started", "evolution_batch"):
                if (event is None or event["event"] != name or event["task_run_ids"] != batch_ids
                        or event["block_id"] != batch.block_id or event["worker_id"] != batch.worker_id):
                    raise ValueError("checkpoint contains an incomplete or mismatched evolution batch")
                if name == "evolution_batch":
                    native_ids.extend(event["evolution_event_ids"])
                    artifact_ids.update(event["global_skill_ids"])
                event = next(commits, None)
                if name == "evolution_started" and event is None and resource_stop and len(finished) == len(tasks):
                    pending_evolution = _unsupported_addition(directory, batch, tasks[-len(batch_ids):])
                    native_ids.extend(item["event_id"] for item in pending_evolution)
                    break
            if pending_evolution:
                break
            if batch.target:
                if (event is None or event["event"] != "block_transfer" or event["block_id"] != batch.block_id
                        or event["worker_id"] != batch.worker_id or event["target_worker_id"] != batch.target):
                    raise ValueError("checkpoint lacks a completed transfer for the block")
                routes.append((batch.worker_id, batch.target, batch.block_id, event["target_local_ids"]))
                event = next(commits, None)
        batches_done += 1

    actual_native = [row["event_id"] for path in (directory / "workers").glob("*/evolution_events.jsonl")
                     for row in read_jsonl(path)]
    _verify_transfers(directory, routes)
    actual_artifacts = [row["global_skill_id"] for row in rows if row["kind"] == "artifact"]
    registered = {row["global_skill_id"] for row in read_jsonl(directory / "registry/registry.jsonl")}
    if (event is not None or next(probes, None) is not None or len(finished) != len(tasks)
            or sorted(native_ids) != sorted(actual_native)
            or len(artifact_ids) != len(actual_artifacts) or set(actual_artifacts) != artifact_ids
            or artifact_ids != registered or resource_stop != bool(pending_evolution)):
        raise ValueError("checkpoint journals contain uncommitted work")
    last = progress["last_task"]
    if bool(last) != bool(finished) or (last and last["task_run_id"] != finished[-1]):
        raise ValueError("checkpoint last task disagrees with committed rows")
    pending = [row for run_id, row in starts.items()
               if run_id not in finished and row["execution_index"] == len(tasks) + 1]
    return EpisodeCheckpoint(rows, tasks, batches_done, last, pending[-1] if pending else None, pending_evolution)
