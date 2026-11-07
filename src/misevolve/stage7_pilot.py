"""Prepare, run, or inspect one Stage 7.2 serial four-worker pilot."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError

from . import harness
from ._stage7_api import DEEPSEEK_MODEL, MINIMAX_MODEL, OPENCLAW_OUTPUT, ProviderCalls, ProviderError, Scope
from ._stage7_openclaw import Stage7Runner, execution_evidence, isolated_relay
from .adapters.openclaw_runner import OpenClawRunner
from .judges.artifact import RUBRIC
from .judges.behavioral import _judge_prompt, _util_prompt
from .multiagent._episode_checkpoint import load_checkpoint
from .multiagent._io import append_jsonl, atomic_json, canonical_json, read_jsonl
from .multiagent.episode import _plan, run_episode
from .multiagent.orchestrator import MultiAgentOrchestrator
from .paths import repo_root
from .stage7 import BACKBONE, EPISODE, STAGES, _judge, _runtime, _smoke_configuration, _trace_native, load_credentials

POSITIONS = (1, 2, 3, 7, 8, 9, 13, 14, 15, 4, 5, 6, 10, 11, 12, 16, 17, 18, 19, 20, 21)
ROUTE = [("A", "B"), ("B", "C"), ("C", "D")]
HISTORY_BYTES = 32 * 1024 * 1024
CAPTURE_TIMEOUT = 120
TASK_TIMEOUT = 1200
TRANSFER_RULE = {
    "selection": "final_observed_version_per_local_key_in_completed_source_block",
    "export": "pinned_native_export_skill_dir",
    "import": "pinned_native_import_agent_skill_dirs",
    "files": ["SKILL.md"],
    "executor_export": "pinned_native_export_skill_md",
    "resources": "retain_in_native_store_record_omissions_from_markdown_payload",
    "allowed_adaptation": ["receiver_frontmatter_id", "normalized_line_endings"],
    "unchanged_imports_forwarded": False,
    "receiver_local_additions_and_updates_forwarded": True,
    "risk_score_filter": False,
    "empty_selection": "record_and_continue",
}

_READ_TRANSCRIPT = """
const { DatabaseSync } = require("node:sqlite");
const { zstdDecompressSync } = require("node:zlib");
const { homedir } = require("node:os");
const [key, limit] = process.argv.slice(1);
const db = new DatabaseSync(homedir() + "/.openclaw/agents/main/agent/openclaw-agent.sqlite", {readOnly: true});
db.exec("BEGIN");
const row = db.prepare("SELECT current_session_id FROM session_nodes WHERE session_key = ?").get(key);
if (!row?.current_session_id) throw new Error("OpenClaw session not found");
const windows = db.prepare("SELECT session_id FROM session_windows WHERE session_key = ?").all(key);
if (windows.length !== 1 || windows[0].session_id !== row.current_session_id) {
    throw new Error("OpenClaw session has multiple or unknown transcript generations");
}
if (db.prepare("SELECT 1 FROM session_transcript_cold_archives WHERE session_id = ?").get(row.current_session_id)) {
    throw new Error("OpenClaw session has an archived transcript");
}
let bytes = 0;
const events = db.prepare("SELECT event_json, event_zstd FROM transcript_events WHERE session_id = ? ORDER BY seq");
for (const event of events.iterate(row.current_session_id)) {
    const text = event.event_zstd ? zstdDecompressSync(event.event_zstd).toString("utf8") : event.event_json;
    if (typeof text !== "string") throw new Error("OpenClaw transcript event has no data");
    bytes += Buffer.byteLength(text) + 1;
    if (bytes > Number(limit)) throw new Error("OpenClaw transcript exceeds capture limit");
    process.stdout.write(text + "\\n");
}
db.exec("COMMIT");
db.close();
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
    return records


class PilotRunner(Stage7Runner):
    gateway_timeout_seconds = TASK_TIMEOUT

    def __init__(self, network, relay):
        super().__init__(network, relay)
        self._timeout = TASK_TIMEOUT

    def _container_options(self):
        return [*super()._container_options(), "--dns", "127.0.0.1",
                "--sysctl", "net.ipv6.conf.all.disable_ipv6=1"]

    def _http(self, method, path, body=None, *, timeout):
        if method == "POST" and path == "/prompt":
            try:
                return super()._http(method, path, body, timeout=timeout)
            except (TimeoutError, URLError) as exc:
                if isinstance(exc, URLError) and not isinstance(exc.reason, TimeoutError):
                    raise
                # Return a failed prompt response so the base runner still reads history.
                return 504, {"status": "task_timeout"}
        if method != "GET" or not path.startswith("/sessions/") or not path.endswith("/history"):
            return super()._http(method, path, body, timeout=timeout)
        session_id = path.split("/")[2]
        result = self._docker([
            "exec", self.container, "node", "-e", _READ_TRANSCRIPT,
            f"agent:main:stage7-{session_id}", str(HISTORY_BYTES),
        ], timeout=CAPTURE_TIMEOUT, check=False)
        if result.returncode:
            failures = {
                "exceeds capture limit": "transcript_capture_limit",
                "archived transcript": "transcript_archived",
                "transcript generations": "transcript_generation_mismatch",
            }
            raise ValueError(next((kind for text, kind in failures.items() if text in result.stderr),
                                  "native transcript capture failed"))
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
            timed_out = trajectory.raw.get("prompt_response") == {"status": "task_timeout"}
            evidence.update({
                "session_id": trajectory.raw.get("session_id"), "history_records": len(history),
                "history_complete": bool(history), "model_configuration": self.model_config,
                "task_timeout_seconds": self._timeout,
                "stop_reason": "task_timeout" if timed_out else evidence["terminal_stop_reason"] or "missing_terminal",
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


@contextmanager
def _pilot_relay(calls):
    with isolated_relay(calls) as (network, relay):
        info = json.loads(OpenClawRunner._docker(["network", "inspect", network]).stdout)[0]
        if info["Driver"] != "bridge" or info.get("EnableIPv6"):
            raise ValueError("the pilot requires an IPv4-only internal bridge")
        bridge = (info.get("Options") or {}).get("com.docker.network.bridge.name") or f"br-{info['Id'][:12]}"
        address, port = relay.server_address
        interface = ["-i", bridge]
        rules = [
            [*interface, "-j", "DROP"],
            [*interface, "-d", address, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"],
            [*interface, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ]
        applied = []

        def firewall(*args):
            subprocess.run(["iptables", "-w", "5", *args], check=True, capture_output=True, timeout=15)

        try:
            for rule in rules:
                firewall("-I", "INPUT", "1", *rule)
                applied.append(rule)
            yield network, relay
        finally:
            errors = []
            for rule in reversed(applied):
                try:
                    firewall("-D", "INPUT", *rule)
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("pilot bridge firewall cleanup failed", errors)


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
    if os.name != "posix" or os.geteuid() != 0 or not shutil.which("iptables"):
        raise ValueError("the isolated pilot requires Linux root and iptables")
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
        "task_timeout_seconds": TASK_TIMEOUT,
        "gateway_task_timeout_seconds": TASK_TIMEOUT,
        "stop_conditions": [*config["stop_conditions"], "task_timeout"],
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
            "bridge_host_access": "model_relay_and_replies_to_host_initiated_connections",
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


def _continuation(source, episode, manifest=None):
    source = source.resolve(strict=True)
    if not (source / "run.exit").is_file():
        raise ValueError("the source run has not stopped and written run.exit")
    if int((source / "run.exit").read_text(encoding="utf-8")) == 0:
        raise ValueError("the source run completed; there is nothing to continue")
    previous = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    if previous["stage"] != "7.2" or previous["run_id"] != source.name:
        raise ValueError("source is not a Stage 7.2 pilot run")
    checkpoint = load_checkpoint(source / "episode", *_plan(episode))
    if manifest:
        keys = ("runtime", "python", "dependencies", "preflight_sha256", "dataset_sha256", "plan",
                "episode_environment", "routes", "judges")
        ignored = {"task_timeout_seconds", "gateway_task_timeout_seconds", "stop_conditions"}
        old = {key: value for key, value in previous["configuration"].items() if key not in ignored}
        new = {key: value for key, value in manifest["configuration"].items() if key not in ignored}
        if canonical_json(old) != canonical_json(new) or any(
            canonical_json(previous[key]) != canonical_json(manifest[key]) for key in keys
        ):
            raise ValueError("source and continuation have different experiment settings")
        old_rule, new_rule = previous["transfer_rule"], manifest["transfer_rule"]
        legacy_rule = {key: value for key, value in new_rule.items() if key not in {"executor_export", "resources"}}
        if old_rule != new_rule and not (checkpoint.pending_evolution and old_rule == legacy_rule
                                         and not read_jsonl(source / "episode/transfer_manifest.jsonl")):
            raise ValueError("source and continuation have different transfer rules")
    for name in ("model_calls.jsonl", "judge_results.jsonl"):
        read_jsonl(source / name)
    committed = checkpoint.pending_evolution[-1] if checkpoint.pending_evolution else None
    return checkpoint, {
        "source_directory": str(source), "source_run_id": previous["run_id"], "source_code_sha": previous["code_sha"],
        "source_task_timeout_seconds": previous["configuration"]["task_timeout_seconds"],
        "source_continuation": previous.get("continuation"),
        "retained_tasks": len(checkpoint.tasks), "retained_batches": checkpoint.completed_batches,
        "first_execution_index": len(checkpoint.tasks) + 1, "interrupted_attempt": checkpoint.interrupted,
        "native_ingest_recovery": ({
            "worker_id": committed["worker_id"], "task_run_id": committed["task"]["task_run_id"],
            "local_skill_id": committed["updated_local_skill_ids"][0], "event_id": committed["event_id"],
        } if committed else None),
    }


def _copy_checkpoint(source, root, checkpoint):
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("the continuation must use a separate run directory")
    episode = source / "episode"
    if any(path.is_symlink() for path in (episode, *episode.rglob("*"))):
        raise ValueError("checkpoint must not contain symbolic links")
    interrupted = checkpoint.interrupted

    def ignore(directory, _names):
        if interrupted and Path(directory) == episode / "workers" / interrupted["worker_id"]:
            return [f"p{interrupted['source_pos']:02d}_{interrupted['task_kind']}"]
        return []

    shutil.copytree(episode, root / "episode", ignore=ignore)
    for name in ("model_calls.jsonl", "judge_results.jsonl"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, root / name)


def _task_prompts(episode, trajectory, goal, prompt, kind):
    evidence = json.dumps(trajectory, ensure_ascii=False, separators=(",", ":"))
    yield "harm_judge", _judge_prompt().format(
        target=goal, category=episode.get("hazard_category") or episode.get("task_surface", ""),
        comment=episode.get("mechanism", ""), jailbreak_method=episode.get("task_surface", ""),
        trajectory=evidence, detail_logs_section="",
    )
    if kind != "M":
        yield "utility_judge", _util_prompt().format(target=prompt, trajectory=evidence, detail_logs_section="")


def _pilot_summary(root, calls, started, *, error=None, continuation=None):
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
        "continuation": continuation,
        "completed_hops": completed_hops, "tasks": tasks, "api": attempts,
        "error_type": type(error).__name__ if error is not None else None,
    })
    return technical


def pilot(calls, root, episode, manifest):
    started = time.monotonic()
    continuation = manifest.get("continuation")
    scopes = {}
    current = None
    log = root / "judge_results.jsonl"

    def before_task(worker, entry, run_id):
        nonlocal current
        if calls.authentication_failed:
            raise ProviderError("a provider authentication failure stopped the pilot")
        current = scopes[run_id] = Scope(run_id, worker.worker_id, entry["instance_id"])
        worker.runner.scope = current
        if not continuation or entry["execution_index"] >= continuation["first_execution_index"]:
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
    if continuation:
        print(json.dumps({"continuation": continuation}, sort_keys=True), flush=True)
    try:
        with _pilot_relay(calls) as (network, relay):
            pool = MultiAgentOrchestrator(root / "episode", backbone=BACKBONE,
                                          resume=bool(continuation),
                                          runner_factory=lambda *_: PilotRunner(network, relay))
            run_episode(episode, pool, judge=judge, content_judge=None, before_task=before_task,
                        judge_version=judge_version, judge_failed=True)
    except BaseException as exc:
        _pilot_summary(root, calls, started, error=exc, continuation=continuation)
        raise
    return _pilot_summary(root, calls, started, continuation=continuation)


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
    parser.add_argument("mode", choices=("check", "run", "status", "resume-check"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--preflight-report", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--deepseek-band", choices=("auto", "off-peak", "peak"), default="auto")
    args = parser.parse_args()
    if args.mode in ("run", "status", "resume-check") and args.output is None:
        parser.error("--output is required for run, status and resume-check")
    if args.resume_from and args.mode != "run":
        parser.error("--resume-from is only supported in run mode")
    if args.mode == "resume-check":
        episode, _, _ = _episode_and_plan()
        try:
            _, continuation = _continuation(args.output, episode)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        print(json.dumps(continuation, ensure_ascii=False, indent=2))
        return
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
    if args.resume_from:
        source = args.resume_from.resolve(strict=True)
        checkpoint, manifest["continuation"] = _continuation(source, episode, manifest)
        _copy_checkpoint(source, root, checkpoint)
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
