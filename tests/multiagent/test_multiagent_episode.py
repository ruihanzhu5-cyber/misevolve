"""Exercise the published episode schedule with only synthetic runner and judge outputs."""

import hashlib
import json
from collections import Counter

import pytest

from misevolve import harness, stage7_pilot
from misevolve.bench.metrics import is_artifact, is_benign, is_malicious, is_persistence
from misevolve.multiagent._io import atomic_json, read_jsonl
from misevolve.multiagent.episode import SCHEDULE, WORKERS, run_episode
from misevolve.multiagent.exchange import skill_reference
from misevolve.multiagent.models import UnsupportedSkillError
from misevolve.multiagent.orchestrator import MultiAgentOrchestrator
from misevolve.multiagent.provenance import ProvenanceTracker
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.paths import repo_root

EXECUTION_ORDER = [1, 2, 3, 7, 8, 9, 13, 14, 15, 4, 5, 6, 10, 11, 12, 16, 17, 18, 19, 20, 21]
BATCHES = [("A", "M")] * 3 + [(worker, "B") for worker in "BCD"]
ROUTE = [("A", "B"), ("B", "C"), ("C", "D")]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def episode():
    path = repo_root() / "data" / "episodes" / "episodes_directive.json"
    return read_json(path)["episodes"][0]


def _verdict(trace, goal, prompt, kind):
    return harness.TaskOutcome(harmful=kind == "M", utility=kind != "M")


def _scores(text):
    return {"content_unsafety": 4, "generalization": 3, "sneakiness": 2}


@pytest.fixture
def scenario(tmp_path, episode, monkeypatch, runner_factory, method_factory, synthetic_llm):
    runners, methods, llm = {}, {}, {}
    executions, evolutions, searches, seen_imports = [], [], [], []
    faults = {"runner_raise": None, "runner_error": None, "empty": False, "ingest": False, "resource": False}
    expected = {task["pos"]: task for task in episode["task_sequence"]}
    run_dir = tmp_path / "run"

    def make_runner(harness_id, backbone):
        worker = WORKERS[len(runners)]
        runner = runner_factory(harness_id, backbone)
        runners[worker] = runner

        def run(prompt, skills, *, out_dir):
            pos = int(out_dir.name.split("_", 1)[0][1:])
            # Compare digests so a failing assertion does not display the original prompt.
            assert hashlib.sha256(prompt.encode()).digest() == hashlib.sha256(expected[pos]["prompt"].encode()).digest()
            executions.append((worker, pos, out_dir))
            method = methods[worker]
            exchange = method._exchange
            pending = read_jsonl(exchange.task_path)[-1]
            assert pending["event"] == "task_injection_intent"
            ids = {skill.key for skill in skills}
            assert ids == {ref["local_skill_id"] for ref in pending["intended"]}
            if expected[pos]["kind"] == "P":
                assert pending["injection_source"] == "frozen_library"
                assert ids == {skill.id for skill in method.sdk.list(user_id=exchange.user_id)}
            if pos in (4, 10, 16):
                registry = GlobalSkillRegistry(out_dir.parents[2] / "registry")
                for row in registry.get_transfer_history():
                    if row["event"] != "skill_transfer" or row["target_agent"] != worker:
                        continue
                    local_id = row["target_local_id"]
                    assert method.sdk.get(local_id) is not None
                    assert local_id in {skill.id for skill in method.sdk.list(user_id=exchange.user_id)}
                    seen_imports.append((row["source_agent"], worker, local_id))
            if faults["runner_raise"] == pos:
                raise RuntimeError("PRIVATE_MOCK_RUNNER_EXCEPTION")
            if faults["runner_error"] == pos:
                return harness.Trajectory([], error="PRIVATE_MOCK_RUNNER_ERROR")
            manifest = {
                "status": "success", "skills": [{
                    "local_skill_id": skill.key,
                    "runtime_skill_name": f"synthetic-arithmetic-{index}",
                    "runtime_directory_slug": f"synthetic-arithmetic-{index}",
                    "runtime_content_hash": hashlib.sha256(skill.text.encode("utf-8")).hexdigest(),
                } for index, skill in enumerate(skills, 1)],
            }
            command = {"name": "exec", "input": {"command": "synthetic-arithmetic-1"}}
            raw = {"tool_calls": [command], "_skill_injection": manifest,
                   "final_text": "I invoked synthetic-arithmetic-1."}
            atomic_json(out_dir / "trajectory_raw.json", raw)
            return harness.Trajectory(
                [command], final_text=raw["final_text"], raw=raw,
            )

        monkeypatch.setattr(runner, "run", run)
        return runner

    def make_method(harness_id, *, backbone):
        worker = WORKERS[len(methods)]
        method = method_factory(harness_id, backbone=backbone)
        methods[worker] = method
        enable, evolve = method.enable_skill_exchange, method.evolve_batch

        def enable_with_fakes(worker_id):
            enable(worker_id)
            llm[worker] = synthetic_llm(method)
            if faults["resource"] and worker == "A":
                llm_extract = method.sdk.extractor._llm.complete
                llm_maintain = method.sdk.maintainer._llm.complete

                def with_resource(**kw):
                    data = json.loads(llm_extract(**kw))
                    if llm[worker]["extract_calls"] == 3:
                        data["skills"][0].update({
                            "name": "synthetic-music", "description": "Transcribe a short synthetic melody.",
                            "prompt": "# Goal\nWrite the pitches and durations in a melody.",
                            "triggers": ["melody transcription"], "tags": ["music"],
                            "files": {"scripts/synthetic.sh": "# synthetic resource\n"},
                        })
                    return json.dumps(data)

                monkeypatch.setattr(method.sdk.extractor._llm, "complete", with_resource)

                def separate_capability(*, system, **kw):
                    if llm[worker]["extract_calls"] == 3 and "capability identity judge" in system:
                        return '{"same_capability":false,"confidence":1.0}'
                    return llm_maintain(system=system, **kw)

                monkeypatch.setattr(method.sdk.maintainer._llm, "complete", separate_capability)

                snapshot_store = method._exchange.snapshot_store

                def legacy_snapshot():
                    bundles = snapshot_store()
                    if any(bundle.get("resource_files") for bundle in bundles.values()):
                        raise UnsupportedSkillError("unsupported resource file in native store: synthetic")
                    return bundles

                monkeypatch.setattr(method._exchange, "snapshot_store", legacy_snapshot)
            if faults["empty"]:
                monkeypatch.setattr(method.sdk.extractor._llm, "complete", lambda **kw: '{"skills":[]}')
            if faults["ingest"]:
                def fail_ingest(**kw):
                    raise RuntimeError("PRIVATE_MOCK_INGEST_ERROR")

                monkeypatch.setattr(method.sdk, "ingest", fail_ingest)
            native_search = method.sdk.search

            def search(*args, **kwargs):
                searches.append((worker, len(executions)))
                return native_search(*args, **kwargs)

            monkeypatch.setattr(method.sdk, "search", search)

        def evolve_with_trace(traces, outcomes, *, kind, round_idx):
            evolutions.append((worker, kind, len(traces), len(executions)))
            return evolve(traces, outcomes, kind=kind, round_idx=round_idx)

        monkeypatch.setattr(method, "enable_skill_exchange", enable_with_fakes)
        monkeypatch.setattr(method, "evolve_batch", evolve_with_trace)
        return method

    return {
        "pool": MultiAgentOrchestrator(run_dir, runner_factory=make_runner, method_factory=make_method),
        "runners": runners, "methods": methods, "llm": llm, "executions": executions, "evolutions": evolutions,
        "searches": searches, "seen_imports": seen_imports, "faults": faults,
    }


def test_full_episode(episode, scenario):
    pool = scenario["pool"]
    run_dir = pool.run_dir
    rows = run_episode(episode, pool, judge=_verdict, content_judge=_scores)
    assert episode["episode_id"] == "skillmisevo-coding-01"
    assert rows == read_json(run_dir / "all_rows.json")
    tasks = [row for row in rows if not is_artifact(row)]
    artifacts = [row for row in rows if is_artifact(row)]
    source = {task["pos"]: task for task in episode["task_sequence"]}
    plan = read_json(run_dir / "episode_plan.json")
    progress = read_json(run_dir / "progress.json")
    assert progress["phase"] == "complete" and progress["attempted_tasks"] == 21
    assert progress["last_task"]["source_pos"] == 21
    probes = read_jsonl(run_dir / "probe_snapshots.jsonl")
    assert [p["worker_id"] for p in probes] == ["B", "C", "D"]
    assert all(p["unchanged"] and p["before_hash"] == p["after_hash"]
               and p["native_ingests_before"] == p["native_ingests_after"] for p in probes)
    selected = read_jsonl(run_dir / "transfer_manifest.jsonl")
    assert [(h["source_agent"], h["target_agent"]) for h in selected] == ROUTE
    assert all(a["files"] == ["SKILL.md"] for hop in selected for a in hop["selection"])
    assert plan["schedule"] == SCHEDULE
    assert plan["source_order"] == list(range(1, 22))
    assert plan["execution_order"] == [row["source_pos"] for row in tasks] == EXECUTION_ORDER
    assert [row["execution_index"] for row in tasks] == list(range(1, 22))
    assert len({row["task_run_id"] for row in tasks}) == len({row["instance_id"] for row in tasks}) == 21
    assert len({directory for _, _, directory in scenario["executions"]}) == 21
    assert [(worker, pos) for worker, pos, _ in scenario["executions"]] == [
        (row["worker_id"], row["source_pos"]) for row in tasks
    ]
    assert Counter(row["agent"] for row in tasks if is_malicious(row)) == {"A": 9}
    assert Counter(row["agent"] for row in tasks if is_benign(row)) == dict.fromkeys("BCD", 3)
    assert [row["agent"] for row in tasks if is_persistence(row)] == list("BCD")
    task_fields = {
        "arm", "agent", "pos", "round", "kind", "role", "orig_id", "domain", "attack_success", "task_success",
        "retrieved_unsafe",
    }
    for row in tasks:
        assert task_fields <= row.keys()
        task = source[row["source_pos"]]
        assert (row["orig_id"], row["instance_id"], row["label"], row["round"], row["task_kind"]) == (
            task["instance_id"], task["instance_id"], task["label"], task["round"], task["kind"],
        )
        assert row["runner_success"] and row["tool_call_count"] == 1
        assert row["elapsed_seconds"] >= 0
    for row in artifacts:
        assert {"orig_id", "content_unsafety", "generalization", "sneakiness", "artifact_poisoned",
                "authored_source"} <= row.keys()
    assert not pool.workers and all(runner.closed for runner in scenario["runners"].values())
    assert scenario["evolutions"] == [
        (worker, kind, 3, index * 3) for index, (worker, kind) in enumerate(BATCHES, 1)
    ]
    assert {worker: counts["extract_calls"] for worker, counts in scenario["llm"].items()} == {
        "A": 9, "B": 3, "C": 3, "D": 3,
    }
    assert scenario["searches"] and all(attempted < 18 for _, attempted in scenario["searches"])

    registry = GlobalSkillRegistry(run_dir / "registry")
    transfers = [row for row in registry.get_transfer_history() if row["event"] == "skill_transfer"]
    assert [(row["source_agent"], row["target_agent"]) for row in transfers] == ROUTE
    assert [(row["source_agent"], row["target_agent"], row["target_local_id"]) for row in transfers] == (
        scenario["seen_imports"]
    )
    for row, block in zip(transfers, ("A-exposure", "B-clean", "C-clean"), strict=True):
        assert registry.verified_transfer(row)
        final = registry.select_transfer_versions(row["source_agent"], block)
        assert [artifact.global_skill_id for artifact in final] == [row["global_skill_id"]]
    assert len(registry.get_skills_by_owner("A")) == 9

    tracker = ProvenanceTracker(run_dir, registry)
    assert tracker.sync() == []
    assert [(hop["source_agent"], hop["target_agent"]) for hop in tracker.get_transfer_hops()] == ROUTE
    assert tracker.get_events("derived_from") == tracker.get_events("invoked_from") == []
    assert tracker.get_events("retrieved_from")
    evolution_by_id = {}
    raw_task_events = {}
    for worker in WORKERS:
        worker_dir = run_dir / "workers" / worker
        for event in read_jsonl(worker_dir / "evolution_events.jsonl"):
            assert event["success"] and event["returned_skills"] and event["task"]["kind"] in "MB"
            evolution_by_id[event["event_id"]] = event
        for event in read_jsonl(worker_dir / "task_events.jsonl"):
            raw_task_events.setdefault(event["task_run_id"], []).append(event)
    checkpoints = read_jsonl(run_dir / "episode_events.jsonl")
    batches = [row for row in checkpoints if row["event"] == "evolution_batch"]
    assert [row["block_id"] for row in batches] == ["A-exposure"] * 3 + ["B-clean", "C-clean", "D-clean"]
    assert len(evolution_by_id) == 18
    for index, batch in enumerate(batches):
        ids = [row["task_run_id"] for row in tasks[index * 3:index * 3 + 3]]
        assert batch["task_run_ids"] == ids
        assert [evolution_by_id[e]["task"]["task_run_id"] for e in batch["evolution_event_ids"]] == ids
        assert len(batch["global_skill_ids"]) == 3
    for row in tasks:
        context = tracker.get_task_context(row["task_run_id"])
        events = raw_task_events[row["task_run_id"]]
        assert context["execution"] == ["completed"] and context["injection_status"] == ["success"]
        assert {ref["local_skill_id"] for ref in context["injected"]} == {
            ref["local_skill_id"] for ref in context["intended_injection"]
        }
        assert events[-2]["event"] == "task_injection_intent" and events[-1]["event"] == "task_execution"
        if is_persistence(row):
            assert context["injection_source"] == "frozen_library" and context["retrieved"] == []
            assert context["intended_injection"]
        else:
            assert context["injection_source"] == "native_search"
            assert events[-3]["retrieved"] == events[-2]["intended"]

    final_d = registry.select_transfer_versions("D", "D-clean")[0]
    lineage = tracker.get_lineage(final_d.global_skill_id)
    assert len(lineage["native_revision"]) == 17 and len(lineage["ancestors"]) == 17
    assert lineage["derived_from"] == [] and lineage["transferred_from"] == tracker.get_transfer_hops()
    for edge in lineage["native_revision"]:
        child = registry.get_skill(edge["subject"]["global_skill_id"])
        parent = edge["object"]["global_skill_id"]
        assert child.native_revision_parent_id == parent
        evidence = child.parent_evidence[parent]
        assert evidence["type"] == "native_revision"
        event = evolution_by_id[evidence["source_ref"]]
        assert [event["task"]["task_run_id"]] == child.source_task_ids
        snapshot = event["before"][child.local_skill_key]
        predecessor = registry.resolve_reference(child.owner_agent, snapshot)
        assert predecessor["global_skill_id"] == parent
        assert predecessor["content_hash"] == skill_reference(snapshot)["content_hash"]
    rebuilt = ProvenanceTracker(run_dir, output_dir=run_dir / "rebuilt_provenance")
    assert rebuilt.sync() and rebuilt.get_lineage(final_d.global_skill_id) == lineage
    assert {event["event_id"]: event for event in rebuilt.get_events()} == {
        event["event_id"]: event for event in tracker.get_events()
    }

    metrics = read_json(run_dir / "episode_metrics.json")
    assert metrics["status"] == "complete"
    assert metrics["completed_tasks"] == metrics["attempted_tasks"] == metrics["total_tasks"] == 21
    assert metrics["task_counts"] == {"M": 9, "B": 9, "P": 3}
    assert {key: item["completed"] for key, item in metrics["tasks_per_worker"].items()} == {
        "A": 9, "B": 4, "C": 4, "D": 4,
    }
    assert metrics["task_utility"]["all"] == {"numerator": 12, "denominator": 21, "rate": 12 / 21}
    assert metrics["malicious_attack_success"]["rate"] == metrics["unsafe_artifact_rate"]["rate"] == 1
    assert metrics["benign_contamination"]["rate"] == metrics["p_carryover_attack_success"]["rate"] == 0
    assert metrics["skill_versions_per_worker"] == {"A": 9, "B": 3, "C": 3, "D": 3}
    assert metrics["artifact_count"] == metrics["scored_artifact_count"] == 18
    assert metrics["successful_transfer_count"] == 3 and metrics["failed_transfer_count"] == 0
    assert metrics["transfer_route"] == tracker.get_transfer_hops()
    assert metrics["provenance_relation_counts"]["transferred_from"] == 3
    assert metrics["average_task_latency_seconds"] >= 0
    assert metrics["execution_failures"] == 0 and metrics["tool_calls"] == {"total": 21, "reported_tasks": 21}
    assert metrics["token_usage"] is metrics["monetary_cost"] is None

    safe_paths = [tracker.journal] + [run_dir / name for name in (
        "episode_plan.json", "episode_metrics.json", "all_rows.json", "episode_events.jsonl",
    )]
    safe_paths.extend(run_dir.glob("workers/*/task_events.jsonl"))
    for path in safe_paths:
        text = path.read_text(encoding="utf-8")
        assert "Check synthetic integers." not in text and '"skill_files"' not in text
        assert all(json.dumps(task["prompt"], ensure_ascii=False)[1:-1] not in text for task in source.values())


def test_runner_failures_continue_with_unknown_judgments_and_empty_transfers(episode, scenario):
    scenario["faults"].update(runner_raise=1, runner_error=4, empty=True)
    pool = scenario["pool"]
    rows = run_episode(episode, pool, judge=_verdict, content_judge=_scores)
    assert len(rows) == len(scenario["executions"]) == 21
    errors = [row for row in rows if not row["runner_success"]]
    assert [(row["pos"], row["runner_error_type"]) for row in errors] == [(1, "RuntimeError"), (4, "runner_error")]
    tracker = ProvenanceTracker(pool.run_dir)
    for row in errors:
        assert row["attack_success"] is row["task_success"] is None
        context = tracker.get_task_context(row["task_run_id"])
        assert context["injection_source"] == "native_search" and context["execution"] == ["failed"]
    assert [entry[2] for entry in scenario["evolutions"]] == [2, 3, 3, 2, 3, 3]
    assert [(event["source_agent"], event["target_agent"]) for event in tracker.registry.get_transfer_history()
            if event["event"] == "empty_transfer"] == ROUTE
    report = (pool.run_dir / "episode_metrics.json").read_text(encoding="utf-8")
    assert "PRIVATE_MOCK_RUNNER" not in report
    metrics = json.loads(report)
    assert metrics["status"] == "complete" and metrics["execution_failures"] == 2
    assert metrics["completed_tasks"] == 19 and metrics["empty_transfer_count"] == 3
    assert metrics["malicious_attack_success"]["denominator"] == 9


@pytest.mark.parametrize("phase", ["native_evolution", "registry_registration", "skill_transfer"])
def test_integrity_failure_stops_episode(episode, scenario, monkeypatch, phase):
    if phase == "native_evolution":
        scenario["faults"]["ingest"] = True
    elif phase == "registry_registration":
        def fail(*args, **kwargs):
            raise ValueError("PRIVATE_MOCK_REGISTRY_ERROR")

        monkeypatch.setattr(GlobalSkillRegistry, "register_evolution_events", fail)
    else:
        from misevolve.methods.autoskill import AutoSkill

        def fail(*args, **kwargs):
            raise ValueError("PRIVATE_MOCK_TRANSFER_ERROR")

        monkeypatch.setattr(AutoSkill, "import_external_skill", fail)
    pool = scenario["pool"]
    with pytest.raises((RuntimeError, ValueError), match="PRIVATE_MOCK_"):
        run_episode(episode, pool, judge=_verdict, content_judge=_scores)
    assert not pool.workers and all(runner.closed for runner in scenario["runners"].values())
    metrics = read_json(pool.run_dir / "episode_metrics.json")
    assert metrics["status"] == "incomplete" and metrics["failed_phase"] == phase
    assert metrics["attempted_tasks"] == len(scenario["executions"]) == (9 if phase == "skill_transfer" else 3)
    assert metrics["total_tasks"] == 21
    assert metrics["failed_transfer_count"] == (1 if phase == "skill_transfer" else 0)
    progress = read_json(pool.run_dir / "progress.json")
    assert progress["phase"] == "incomplete" and progress["failed_phase"] == phase
    assert progress["last_task"]["execution_index"] == metrics["attempted_tasks"]


@pytest.mark.parametrize("stop_at, committed_batches, resource_stop", [(2, 0, False), (11, 3, False), (4, 0, True)])
def test_continue_without_replaying_committed_work(episode, scenario, stop_at, committed_batches, resource_stop):
    pool = scenario["pool"]
    source = pool.run_dir.parent / "source"
    pool.run_dir = source / "episode"
    continuation = source.parent / "continuation"

    def stop(worker, entry, run_id):
        if entry["execution_index"] == stop_at:
            interrupted = worker.episode_dir / f"p{entry['source_pos']:02d}_{entry['task_kind']}"
            atomic_json(interrupted / "partial.json", {"task_run_id": run_id})
            raise KeyboardInterrupt

    scenario["faults"]["resource"] = resource_stop
    with pytest.raises(UnsupportedSkillError if resource_stop else KeyboardInterrupt):
        run_episode(episode, pool, judge=_verdict, content_judge=_scores, before_task=stop)
    scenario["faults"]["resource"] = False
    previous_rows = read_json(pool.run_dir / "all_rows.json")
    previous_events = read_jsonl(pool.run_dir / "episode_events.jsonl")
    atomic_json(source / "run_manifest.json", {
        "stage": "7.2", "run_id": source.name, "code_sha": "test", "configuration": {"task_timeout_seconds": None},
    })
    with pytest.raises(ValueError, match="has not stopped"):
        stage7_pilot._continuation(source, episode)
    atomic_json(source / "run.exit", 1)
    checkpoint, metadata = stage7_pilot._continuation(source, episode)
    assert (metadata["retained_tasks"], metadata["retained_batches"]) == (stop_at - 1, committed_batches)
    if resource_stop:
        assert checkpoint.interrupted is None and len(checkpoint.pending_evolution) == 3
        assert metadata["native_ingest_recovery"]
    else:
        assert checkpoint.interrupted["execution_index"] == stop_at
    stage7_pilot._copy_checkpoint(source, continuation, checkpoint)
    assert not list(continuation.rglob("partial.json")) and bool(list(source.rglob("partial.json"))) != resource_stop

    scenario["runners"].clear()
    scenario["methods"].clear()
    restarted = MultiAgentOrchestrator(continuation / "episode", resume=True,
                                      runner_factory=pool.runner_factory, method_factory=pool.method_factory)
    rows = run_episode(episode, restarted, judge=_verdict, content_judge=_scores)
    assert rows[:len(previous_rows)] == previous_rows
    assert [pos for _, pos, _ in scenario["executions"]] == EXECUTION_ORDER
    assert read_jsonl(pool.run_dir / "episode_events.jsonl") == previous_events
    events = read_jsonl(restarted.run_dir / "episode_events.jsonl")
    assert events[:len(previous_events)] == previous_events
    assert Counter(event["event"] for event in events)["evolution_batch"] == 6
    assert [(event["worker_id"], event["target_worker_id"]) for event in events
            if event["event"] == "block_transfer"] == ROUTE
    if resource_stop:
        native = read_jsonl(restarted.run_dir / "workers/A/evolution_events.jsonl")
        assert native[:3] == checkpoint.pending_evolution and len(native) == 10
        recovery = native[3]
        assert recovery["success"] and recovery["recovered_from_event_id"] == native[2]["event_id"]
        local_id = metadata["native_ingest_recovery"]["local_skill_id"]
        resources = recovery["after"][local_id]["resource_files"]
        assert set(resources) == {"scripts/synthetic.sh"}
        assert list(source.rglob("synthetic.sh")) and list(continuation.rglob("synthetic.sh"))
        assert any(row.get("orig_id") == local_id for row in rows)
        transfers = read_jsonl(restarted.run_dir / "transfer_manifest.jsonl")
        transfer = next(row for row in transfers if row["source_agent"] == "A")
        sent = next(item for item in transfer["selection"] if item["source_local_id"] == local_id)
        assert sent["files"] == ["SKILL.md"] and sent["omitted_resources"] == resources
    else:
        first = next(row for row in rows if row.get("execution_index") == stop_at)
        assert first["task_run_id"] != checkpoint.interrupted["task_run_id"]


@pytest.mark.parametrize("field", ["pos", "instance_id", "kind", "task_count"])
def test_invalid_source_schedule_fails_before_initialization(episode, tmp_path, field):
    pool = MultiAgentOrchestrator(tmp_path / "run")
    tasks = episode["task_sequence"]
    if field == "task_count":
        tasks.pop()
    else:
        tasks[-1][field] = tasks[0][field]
    with pytest.raises(ValueError):
        run_episode(episode, pool, judge=_verdict, content_judge=_scores)
    assert not pool.run_dir.exists()
