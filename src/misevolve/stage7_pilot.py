"""Prepare, run, or inspect one Stage 7.2 serial four-worker pilot."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import signal
import subprocess
import time
from collections import Counter
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from . import harness
from ._stage7_api import DEEPSEEK_MODEL, MINIMAX_MODEL, OPENCLAW_OUTPUT, ProviderCalls, ProviderError, Scope
from ._stage7_openclaw import Stage7Runner, execution_evidence, isolated_relay
from .adapters.openclaw_runner import OpenClawRunner
from .judges.artifact import RUBRIC
from .judges.behavioral import _judge_prompt, _util_prompt
from .multiagent._io import append_jsonl, atomic_json, read_jsonl
from .multiagent.episode import _plan, run_episode
from .multiagent.orchestrator import MultiAgentOrchestrator
from .paths import repo_root
from .stage7 import BACKBONE, EPISODE, STAGES, _judge, _runtime, _smoke_configuration, _trace_native, load_credentials

POSITIONS = (1, 2, 3, 7, 8, 9, 13, 14, 15, 4, 5, 6, 10, 11, 12, 16, 17, 18, 19, 20, 21)
ROUTE = [("A", "B"), ("B", "C"), ("C", "D")]
HISTORY_BYTES = 32 * 1024 * 1024
CAPTURE_TIMEOUT = 120
TRANSFER_RULE = {
    "selection": "final_observed_version_per_local_key_in_completed_source_block",
    "export": "pinned_native_export_skill_dir",
    "import": "pinned_native_import_agent_skill_dirs",
    "files": ["SKILL.md"],
    "allowed_adaptation": ["receiver_frontmatter_id", "normalized_line_endings"],
    "unchanged_imports_forwarded": False,
    "receiver_local_additions_and_updates_forwarded": True,
    "risk_score_filter": False,
    "empty_selection": "record_and_continue",
}

_READ_TRANSCRIPT = """
import json
import sys
from pathlib import Path

root = (Path.home() / ".openclaw/agents/main/sessions").resolve()
entry = json.loads((root / "sessions.json").read_text())[sys.argv[1]]
path = Path(entry.get("sessionFile") or (entry["sessionId"] + ".jsonl"))
path = (root / path).resolve()
if not path.is_relative_to(root) or not path.is_file():
    raise ValueError("session transcript is outside its store")
with path.open("rb") as handle:
    data = handle.read(int(sys.argv[2]) + 1)
if len(data) > int(sys.argv[2]):
    raise ValueError("session transcript exceeds capture limit")
sys.stdout.buffer.write(data)
"""


def _decode_history(data: str) -> list[dict]:
    if not data.endswith("\n"):
        raise ValueError("incomplete OpenClaw transcript")
    records = [json.loads(line) for line in data.split("\n") if line.strip()]
    if not records or records[0].get("type") != "session":
        raise ValueError("missing OpenClaw session header")
    messages = [row for row in records if row.get("type") == "message"]
    if not messages or any(not isinstance(row.get("message"), dict) for row in messages):
        raise ValueError("missing or invalid OpenClaw messages")
    return messages


class PilotRunner(Stage7Runner):
    def _http(self, method, path, body=None, *, timeout):
        if method != "GET" or not path.startswith("/sessions/") or not path.endswith("/history"):
            return super()._http(method, path, body, timeout=timeout)
        session_id = path.split("/")[2]
        result = self._docker([
            "exec", self.container, "python3", "-c", _READ_TRANSCRIPT,
            f"agent:main:stage7-{session_id}", str(HISTORY_BYTES),
        ], timeout=CAPTURE_TIMEOUT)
        (self._task_dir / "session_raw.jsonl").write_text(result.stdout, encoding="utf-8", newline="\n")
        history = _decode_history(result.stdout)
        return 200, {"history": history}

    def _archive_source(self):
        path = self._task_dir / "workspace.tar"
        temporary = path.with_suffix(".tar.part")
        with temporary.open("xb") as handle:
            result = subprocess.run([
                "docker", "cp", f"{self.container}:/root/.openclaw/workspace/.", "-",
            ], stdout=handle, stderr=subprocess.DEVNULL, timeout=CAPTURE_TIMEOUT, check=False)
        if result.returncode != 0:
            raise RuntimeError("source workspace archive failed")
        temporary.replace(path)

    def run(self, prompt, inject, *, out_dir):
        self._task_dir = Path(out_dir)
        self.model_config = {}
        evidence = {"workspace_archive": "not_applicable", "history_complete": False}
        attached = json.loads(self._docker(["network", "inspect", self.network]).stdout)[0]["Containers"]
        if attached:
            raise RuntimeError("previous task container remains attached to the pilot network")
        # A token seen by one task cannot authenticate during a later task.
        self.relay.token = self._auth = secrets.token_urlsafe(32)
        try:
            trajectory = OpenClawRunner.run(self, prompt, inject, out_dir=out_dir)
            history = trajectory.raw.get("history") or []
            evidence.update(execution_evidence(history))
            evidence.update({
                "session_id": trajectory.raw.get("session_id"), "history_records": len(history),
                "history_complete": bool(history), "model_configuration": self.model_config,
                "stop_reason": evidence["terminal_stop_reason"] or "missing_terminal",
            })
            if evidence["stop_reason"] not in ("stop", "end_turn", "stop_sequence"):
                trajectory.error = trajectory.error or "OpenClaw did not reach a normal terminal state"
            trajectory.raw["_pilot_execution"] = evidence
            return trajectory
        finally:
            try:
                if self.container and self.scope.worker_id == "A":
                    try:
                        self._archive_source()
                    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                        evidence["workspace_archive"] = type(exc).__name__
                    else:
                        evidence["workspace_archive"] = "saved"
                if self._task_dir.is_dir():
                    atomic_json(self._task_dir / "execution_evidence.json", evidence)
            finally:
                self._rm_container()


def _episode_and_plan():
    path = repo_root() / "data/episodes/episodes_directive.json"
    dataset = path.read_bytes()
    episode = next(row for row in json.loads(dataset)["episodes"] if row["episode_id"] == EPISODE)
    plan, _ = _plan(episode)
    if tuple(plan["execution_order"]) != POSITIONS:
        raise ValueError("the source episode does not match the authorized 21-task order")
    prompts = {task["pos"]: task["prompt"] for task in episode["task_sequence"]}
    for entry in plan["tasks"]:
        entry["prompt_sha256"] = sha256(prompts[entry["source_pos"]].encode()).hexdigest()
    return episode, plan, sha256(dataset).hexdigest()


def _git(*args):
    return subprocess.check_output(["git", "-C", str(repo_root()), *args], text=True).strip()


def _configuration(calls, proof_path: Path):
    episode, plan, dataset_sha = _episode_and_plan()
    proof_bytes = proof_path.read_bytes()
    proof = json.loads(proof_bytes)
    runtime = _runtime()
    if (proof.get("stage") != "7.0" or proof.get("passed") is not True or proof.get("runtime") != runtime
            or proof.get("models") != [MINIMAX_MODEL, DEEPSEEK_MODEL]
            or any(proof.get("checks", {}).get(name, {}).get("status") != "passed" for name in STAGES)):
        raise ValueError("matching Stage 7.0 acceptance evidence is required")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("the pilot requires a clean tracked checkout")
    overrides = [key for key in (
        "SKILLMISEVO_DEFENSE_SKILL_DIR", "OPENCLAW_EXTRA_PLUGINS", "AUTOSKILL_HINT", "AUTOSKILL_TOP_K",
        "AUTOSKILL_LLM_MODEL", "AUTOSKILL_LLM_BASE_URL", "AUTOSKILL_LLM_MAX_TOKENS",
    ) if os.environ.get(key)]
    if overrides:
        raise ValueError("unexpected pilot environment overrides: " + ", ".join(overrides))
    config = _smoke_configuration(calls)
    config.update({
        "deepseek_band": "china_weekday_by_attempt" if calls.deepseek_peak is None else config["deepseek_band"],
        "module_request_and_output_limits": {
            key: value for key, value in config["module_request_and_output_limits"].items() if key != "behavior_judge"
        },
        "openclaw_boot_timeout_seconds": int(os.environ.get("SKILLMISEVO_BOOT_TIMEOUT", "120")),
        "gateway_start_probe_timeout_seconds": 95,
        "relay_client_socket_timeout_seconds": 15, "docker_cli_default_timeout_seconds": 120,
        "sandbox_cleanup_timeout_seconds": 60,
        "transcript_and_workspace_capture_timeout_seconds": CAPTURE_TIMEOUT,
        "transcript_capture_bytes": HISTORY_BYTES, "autoskill_top_k": 5,
        "openclaw_cpus": os.environ.get("SKILLMISEVO_CPUS", "2"),
        "openclaw_memory": os.environ.get("SKILLMISEVO_MEM", "4g"),
        "openclaw_pids_limit": 256, "openclaw_prompt_attempts": 1,
    })
    manifest = {
        "stage": "7.2", "run_id": calls.run_id, "code_sha": _git("rev-parse", "HEAD"),
        "runtime": runtime, "python": platform.python_version(),
        "dependencies": {name: version(name) for name in ("anthropic", "openai", "pyyaml")},
        "preflight_sha256": sha256(proof_bytes).hexdigest(), "dataset_sha256": dataset_sha,
        "plan": plan, "episode_environment": {
            "domain": episode["task_domain"], "surface": episode["task_surface"],
            "hazard_category": episode["hazard_category"], "workspace": "fresh_image_workspace",
            "host_mounts": False, "external_network": False, "external_fixtures": False,
        },
        "transfer_rule": TRANSFER_RULE, "routes": ROUTE,
        "judges": {
            "harm": sha256(_judge_prompt().encode()).hexdigest(),
            "utility": sha256(_util_prompt().encode()).hexdigest(),
            "artifact": sha256(RUBRIC.encode()).hexdigest(),
        },
        "configuration": config,
    }
    return episode, manifest


def _task_prompts(episode, trajectory, goal, prompt, kind):
    evidence = json.dumps(trajectory, ensure_ascii=False, separators=(",", ":"))
    yield "harm_judge", _judge_prompt().format(
        target=goal, category=episode.get("hazard_category") or episode.get("task_surface", ""),
        comment=episode.get("mechanism", ""), jailbreak_method=episode.get("task_surface", ""),
        trajectory=evidence, detail_logs_section="",
    )
    if kind != "M":
        yield "utility_judge", _util_prompt().format(target=prompt, trajectory=evidence, detail_logs_section="")


def _pilot_summary(root, calls, started, *, error=None):
    episode = root / "episode"
    metrics_path = episode / "episode_metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    rows_path = episode / "all_rows.json"
    rows = json.loads(rows_path.read_text(encoding="utf-8")) if rows_path.is_file() else []
    tasks = [row for row in rows if "task_kind" in row]
    checkpoints = read_jsonl(episode / "episode_events.jsonl")
    probes = read_jsonl(episode / "probe_snapshots.jsonl")
    completed_hops = [(r["worker_id"], r["target_worker_id"]) for r in checkpoints if r["event"] == "block_transfer"]
    retrieval = {}
    ingests = {}
    judges = {}
    for verdict in read_jsonl(root / "judge_results.jsonl"):
        if verdict["module"] in ("harm_judge", "utility_judge"):
            judges.setdefault(verdict["task_id"], {})[verdict["module"]] = verdict["status"]
    for worker in "ABCD":
        path = episode / "workers" / worker
        retrieval.update({r["task_run_id"]: r["retrieval_status"] for r in read_jsonl(path / "task_events.jsonl")
                          if r["event"] == "task_retrieval"})
        ingests.update({r["task"]["task_run_id"]: r["success"] for r in read_jsonl(path / "evolution_events.jsonl")})
    attempts = calls.summary()
    for row in tasks:
        per_task = calls.summary(task_id=row["task_run_id"])
        execution = row.get("execution_evidence", {})
        count = execution.get("native_tool_argument_corrections", 0)
        if count:
            per_task["tool_argument_corrections"]["native"] = count
            attempts["tool_argument_corrections"]["native"] = (
                attempts["tool_argument_corrections"].get("native", 0) + count
            )
        row["api"] = per_task
        row["native_retrieval_status"] = retrieval.get(row["task_run_id"])
        row["native_ingest_ok"] = ingests.get(row["task_run_id"])
        row["judge_status"] = judges.get(row["task_run_id"], {})
    issues = {
        "execution_failures": [r["instance_id"] for r in tasks if not r["runner_success"]],
        "judge_failures": [r["instance_id"] for r in tasks if r["attack_success"] is None
                           or (r["task_kind"] != "M" and r["task_success"] is None)],
        "artifact_judge_failures": [r["global_skill_id"] for r in rows
                                   if r["kind"] == "artifact" and r["content_unsafety"] is None],
        "incomplete_histories": [r["instance_id"] for r in tasks
                                 if not r.get("execution_evidence", {}).get("history_complete")],
        "source_archives_missing": [r["instance_id"] for r in tasks if r["worker_id"] == "A"
                                    and r.get("execution_evidence", {}).get("workspace_archive") != "saved"],
        "executor_budget_mismatch": [r["instance_id"] for r in tasks
                                     if r["api"]["openclaw_request_max_tokens"] != [OPENCLAW_OUTPUT]],
        "native_evidence_failures": [r["instance_id"] for r in tasks if (
            (r["task_kind"] != "P" and (r["native_retrieval_status"] != "completed"
                                        or r["native_ingest_ok"] is not True))
            or (r["task_kind"] == "P" and (r["native_retrieval_status"] is not None
                                          or r["native_ingest_ok"] is not None))
        )],
    }
    complete = metrics.get("status") == "complete" and len(tasks) == 21 and error is None
    technical = (complete and not any(issues.values()) and completed_hops == ROUTE
                 and sum(row["event"] == "evolution_batch" for row in checkpoints) == 6
                 and [row["worker_id"] for row in probes if row["unchanged"]] == ["B", "C", "D"])
    atomic_json(root / "pilot_summary.json", {
        "stage": "7.2", "run_id": calls.run_id, "status": "complete" if complete else "incomplete",
        "technical_acceptance": "passed" if technical else "incomplete", "issues": issues,
        "attempted_tasks": len(tasks), "elapsed_seconds": round(time.monotonic() - started, 3),
        "completed_hops": completed_hops, "tasks": tasks, "api": attempts,
        "error_type": type(error).__name__ if error is not None else None,
    })
    return technical


def pilot(calls, root, episode, manifest):
    started = time.monotonic()
    scopes = {}
    current = None
    log = root / "judge_results.jsonl"

    def before_task(worker, entry, run_id):
        nonlocal current
        if calls.authentication_failed:
            raise ProviderError("a provider authentication failure stopped the pilot")
        current = scopes[run_id] = Scope(run_id, worker.worker_id, entry["instance_id"])
        worker.runner.scope = current
        print(f"[{entry['execution_index']:02d}/21] {worker.worker_id} {current.instance_id}", flush=True)
        worker.method._exchange.before_ingest = lambda task: _trace_native(
            worker, calls, scopes[task["task_run_id"]], worker.episode_dir / "native_responses.jsonl",
        )

    def judge(trajectory, goal, prompt, kind):
        raw = trajectory.raw
        if not raw.get("_pilot_execution", {}).get("history_complete"):
            for module in ("harm_judge", "utility_judge") if kind != "M" else ("harm_judge",):
                append_jsonl(log, {
                    "task_id": current.task_id, "module": module, "status": "missing_history", "verdict": None,
                })
            return harness.TaskOutcome(None, None)
        verdicts = {}
        for module, text in _task_prompts(episode, raw["history"], goal, prompt, kind):
            try:
                verdicts.update(_judge(calls, current, module, text, log))
            except (ProviderError, ValueError):
                continue
        return harness.TaskOutcome(verdicts.get("harmful"), verdicts.get("completed"))

    def judge_version(artifact):
        scope = scopes[artifact.source_task_ids[-1]]
        return _judge(calls, scope, "artifact_judge", RUBRIC % artifact.skill_files["SKILL.md"],
                      log, artifact_id=artifact.global_skill_id, detailed_artifact=True)

    print(json.dumps(manifest["configuration"], sort_keys=True), flush=True)
    try:
        with isolated_relay(calls) as (network, relay):
            pool = MultiAgentOrchestrator(root / "episode", backbone=BACKBONE,
                                          runner_factory=lambda *_: PilotRunner(network, relay))
            run_episode(episode, pool, judge=judge, content_judge=None, before_task=before_task,
                        judge_version=judge_version, judge_failed=True)
    except BaseException as exc:
        _pilot_summary(root, calls, started, error=exc)
        raise
    return _pilot_summary(root, calls, started)


def _status(root):
    episode = root / "episode"
    result = {}
    for name, path in (
        ("progress", episode / "progress.json"), ("summary", root / "pilot_summary.json"),
        ("exit_code", root / "run.exit"),
    ):
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if name == "summary":
                data = {key: value for key, value in data.items() if key not in ("tasks", "api")}
            result[name] = data
    journals = [episode / "episode_events.jsonl", episode / "registry/transfer_events.jsonl"]
    journals.extend(sorted(episode.glob("workers/*/evolution_events.jsonl")))
    result["durable_events"] = {}
    result["latest_checkpoints"] = {}
    for path in journals:
        counts = Counter()
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            lines = text.split("\n")
            if lines[-1]:
                counts["incomplete_tail"] += 1
            for line in lines[:-1]:
                try:
                    row = json.loads(line)
                    event = row["event"]
                    counts[event] += 1
                    if event in ("task_finished", "evolution_batch", "block_transfer",
                                 "skill_transfer_started", "skill_transfer", "native_ingest"):
                        key = f"{path.relative_to(episode)}:{event}"
                        result["latest_checkpoints"][key] = {field: row[field] for field in (
                            "worker_id", "instance_id", "execution_index", "source_pos", "runner_success",
                            "block_id", "task_run_id", "task_run_ids", "evolution_event_ids", "global_skill_ids",
                            "source_agent", "target_agent", "global_skill_id", "attempt_id", "success", "event_id",
                        ) if field in row}
                except (ValueError, KeyError, TypeError):
                    counts["invalid_record"] += 1
                    break
        result["durable_events"][str(path.relative_to(episode))] = counts
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "run", "status"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--preflight-report", type=Path)
    parser.add_argument("--deepseek-band", choices=("auto", "off-peak", "peak"), default="auto")
    args = parser.parse_args()
    if args.mode in ("run", "status") and args.output is None:
        parser.error("--output is required for run and status")
    if args.mode == "status":
        if not args.output.is_dir():
            parser.error("run directory does not exist")
        print(json.dumps(_status(args.output), ensure_ascii=False, indent=2))
        return
    if args.credentials is None or args.preflight_report is None:
        parser.error("--credentials and --preflight-report are required")
    os.umask(0o077)
    load_credentials(args.credentials)
    root = args.output.resolve() if args.output else Path("stage72-check")
    peak = {"auto": None, "off-peak": False, "peak": True}[args.deepseek_band]
    calls = ProviderCalls(root, deepseek_peak=peak)
    calls.check_credentials()
    episode, manifest = _configuration(calls, args.preflight_report)
    if args.mode == "check":
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(item.name != "run.log" for item in root.iterdir()):
        raise FileExistsError("the pilot requires a fresh run directory")
    with (root / "run_manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(0 if pilot(calls, root, episode, manifest) else 1)


if __name__ == "__main__":
    main()
