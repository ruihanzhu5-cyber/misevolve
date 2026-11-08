"""Phase 3 configuration, explicit execution, and offline reporting entry points."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from threading import Condition

from misevolve._stage7_api import DEEPSEEK_MODEL, ProviderCalls, ProviderError, Scope
from misevolve._stage7_openclaw import BACKBONE
from misevolve.multiagent._io import append_jsonl, atomic_json, read_jsonl
from misevolve.paths import repo_root
from misevolve.run import load_episode
from misevolve.stage7 import _runtime, _trace_native

from .checkpoint import BOUNDARIES, exclusive
from .episode import TeamEpisodeScheduler
from .executor import TeamWorkerPool
from .judge import TeamJudge, evaluation_configuration
from .openclaw import (
    FREEZE_SECONDS,
    REMOVE_SECONDS,
    SNAPSHOT_SECONDS,
    TASK_TIMEOUT,
    TOOL_TIMEOUT_SECONDS,
    TOOL_YIELD_MS,
    isolated_openclaw_factory,
)
from .router import ChainRouter, HierarchicalRouter, PeerToPeerRouter

ROUTERS = {cls.protocol: cls for cls in (ChainRouter, HierarchicalRouter, PeerToPeerRouter)}
NATIVE_MODULES = frozenset({"extractor", "maintainer"})
_EXTRACTOR_FORMAT_RETRY = (
    "Your previous output failed JSON validation. Regenerate the complete result from the same input. "
    'Return one valid JSON object with a top-level "skills" array. Escape quotes and newlines in strings. '
    'Use {"skills": []} only if the input contains no reusable skill. Omit prose and Markdown fences.'
)


class EpisodeCalls(ProviderCalls):
    """Stop further requests after an exhausted provider/transport failure."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure = None
        self._executor_condition = Condition()
        self._executor_calls = 0

    def input_limit(self, module):
        return None if module in NATIVE_MODULES else super().input_limit(module)

    def stop(self, module, error):
        if self.failure is None:
            self.failure = (module, error.category, error.status)

    def ensure_healthy(self):
        if self.failure is not None:
            module, category, status = self.failure
            raise ProviderError(f"team provider circuit stopped: {module}, {category}, HTTP {status}",
                                category="team_provider_circuit")

    def request(self, scope, module, payload, *, correction=None):
        self.ensure_healthy()
        if module == "openclaw":
            with self._executor_condition:
                self._executor_calls += 1
        try:
            return super().request(scope, module, payload, correction=correction)
        except ProviderError as exc:
            if exc.category == "input_limit":
                append_jsonl(self.path.with_name("request_rejections.jsonl"), {
                    "run_id": self.run_id, "task_id": scope.task_id, "worker_id": scope.worker_id,
                    "instance_id": scope.instance_id, "module": module, "reason": exc.category,
                    "sent_to_provider": False, **exc.details,
                })
            if exc.category in {"timeout", "transport_error", "http_error", "invalid_response"} and not (
                module == "openclaw" and exc.category == "timeout"
            ):
                self.stop(module, exc)
            raise
        finally:
            if module == "openclaw":
                with self._executor_condition:
                    self._executor_calls -= 1
                    self._executor_condition.notify_all()

    def drain_executor(self):
        with self._executor_condition:
            if not self._executor_condition.wait_for(lambda: not self._executor_calls, timeout=100):
                raise RuntimeError("in-flight model accounting did not settle; no resumable checkpoint written")


def _configure_native(workers, calls):
    for worker in workers.values():
        exchange = worker.method._exchange
        checks = {}

        def configure_ingest(task, worker=worker, checks=checks):
            calls.ensure_healthy()
            checks.clear()
            scope = Scope(task["task_run_id"], worker.worker_id, task.get("instance_id"))

            def observe(module, system, text):
                error = _native_format_error(module, system, text)
                if module == "extractor" or error:
                    checks[module] = error
                return error

            _trace_native(worker, calls, scope, worker.episode_dir / "native_responses.jsonl",
                          on_error=calls.stop, response_check=observe)
            extractor = worker.method.sdk.extractor._llm
            complete = extractor.complete

            def complete_extractor(*, system=None, user="", temperature=0.0):
                text = complete(system=system, user=user, temperature=temperature)
                if not checks.get("extractor"):
                    return text
                # A malformed draft can look like a valid nested array to the native parser.
                # Regenerate once from the original input before handing it to AutoSkill.
                corrected_system = f"{system}\n\n{_EXTRACTOR_FORMAT_RETRY}" if system else _EXTRACTOR_FORMAT_RETRY
                text = complete(system=corrected_system, user=user, temperature=temperature)
                if checks.get("extractor"):
                    error = ProviderError("native_output_format", category="invalid_native_response")
                    calls.stop("extractor", error)
                    raise error
                return text

            extractor.complete = complete_extractor

        def check_ingest(checks=checks):
            for module, error in checks.items():
                if error:
                    calls.stop(module, ProviderError(error, category="invalid_native_response"))
            calls.ensure_healthy()

        exchange.before_ingest = configure_ingest
        exchange.after_ingest = check_ingest


def _native_format_error(module, system, text):
    from autoskill.management.extraction import _candidate_from_obj
    from autoskill.management.maintenance import _json_from_llm_decision, _normalize_action
    from autoskill.utils.json import _FENCE_RE, json_from_llm_text

    try:
        if module == "extractor":
            parsed = json.loads(_FENCE_RE.sub("", text.strip()))
            skills = parsed.get("skills") if isinstance(parsed, dict) else parsed
            valid = isinstance(skills, list) and (not skills or any(
                _candidate_from_obj(item, source=None) is not None for item in skills
            ))
        elif '"action"' in (system or ""):
            parsed = _json_from_llm_decision(text)
            valid = _normalize_action(parsed.get("action")) in {"add", "merge", "discard"}
        else:
            parsed = json_from_llm_text(text)
            valid = isinstance(parsed, dict) and bool(parsed)
            if '"same_capability"' in (system or ""):
                valid = valid and type(parsed.get("same_capability")) is bool
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    return None if valid else "native_output_format"


def run_episode(args, config):
    if config["missing_credentials"]:
        raise ValueError(f"missing credentials: {', '.join(config['missing_credentials'])}")
    root = args.out.resolve()
    with exclusive(root):
        return _run_episode(args, config, root)


def _run_episode(args, config, root):
    episode = load_episode(args.episode)
    valid_ids = {task["instance_id"] for task in episode["task_sequence"]}
    if set(config["judge"]["output_evidence_tasks"]) - valid_ids:
        raise ValueError("output evidence allowlist includes tasks outside the episode")
    resume = getattr(args, "resume", False)
    if not resume and root.exists() and any(root.iterdir()):
        raise FileExistsError("team episode requires a new output directory")
    code_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True,
    ).strip()
    calls = EpisodeCalls(root, deepseek_peak=None)
    calls.run_id = (
        json.loads((root / "team_manifest.json").read_text(encoding="utf-8"))["run_id"]
        if resume else uuid.uuid4().hex
    )
    cache_dir = (args.cache_dir or root.parent / "artifact_judge_cache").resolve()
    judge = TeamJudge(calls, root / "judge_results.jsonl", cache_dir, config)
    metadata = {key: config[key] for key in ("execution", "judge")}
    metadata["execution"] = {
        **metadata["execution"], "native_input_limit_bytes": calls.input_limit("extractor"),
        "extractor_format_retries_per_ingest": 1,
        "task_timeout_seconds": TASK_TIMEOUT, "task_deadline_shared_across_members": True,
        "tool_default_timeout_seconds": TOOL_TIMEOUT_SECONDS, "tool_yield_ms": TOOL_YIELD_MS,
        "tool_timeout_overridable_by_native_exec": True, "process_tool_enabled": True,
        "freeze_timeout_seconds": FREEZE_SECONDS, "snapshot_timeout_seconds": SNAPSHOT_SECONDS,
        "remove_timeout_seconds": REMOVE_SECONDS, "total_cost_limit_cny": None,
        "executor_task_request_limit": None, "provider_retries_per_request": 1,
    }
    metadata.update(run_id=calls.run_id, replicate_id=args.replicate_id, code_sha=code_sha,
                    artifact_cache=str(cache_dir))
    if getattr(args, "verify_runtime", False):
        metadata["runtime"] = _runtime()

    def ensure_healthy():
        calls.ensure_healthy()
        bad = Counter(row["module"] for row in read_jsonl(root / "judge_results.jsonl")
                      if row.get("status") in {"invalid_schema", "truncated", "incomplete_response"})
        if any(count >= 2 for count in bad.values()):
            raise RuntimeError("repeat Judge response format failure; episode stopped")
        if (root / "all_rows.json").exists():
            rows = json.loads((root / "all_rows.json").read_text(encoding="utf-8"))
            if sum((row.get("failure") or {}).get("stop_reason") == "model_request_timeout" for row in rows) >= 2:
                raise RuntimeError("repeat executor provider timeout; episode stopped")

    try:
        with isolated_openclaw_factory(calls) as factory:
            pool = TeamWorkerPool(root, runner_factory=factory, backbone=args.backbone, resume=resume)
            return TeamEpisodeScheduler(
                pool, ROUTERS[args.protocol], judge=judge, artifact_judge=judge.artifact,
                configure_workers=lambda workers: _configure_native(workers, calls), run_metadata=metadata,
                stop_after_pos=getattr(args, "stop_after_pos", None), ensure_healthy=ensure_healthy,
                before_checkpoint=calls.drain_executor,
            ).run(episode)
    finally:
        if calls.path.exists():
            atomic_json(root / "api_summary.json", calls.summary())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("config-check", help="validate configuration without network or Docker")
    run = commands.add_parser("run", help="explicitly start one real episode")
    for command in (check, run):
        command.add_argument("--backbone", default=BACKBONE)
        command.add_argument("--judge-model", default=DEEPSEEK_MODEL)
        command.add_argument("--judge-revision")
        command.add_argument("--output-evidence-task", action="append", default=[])
    run.add_argument("--episode", required=True)
    run.add_argument("--protocol", choices=sorted(ROUTERS), required=True)
    run.add_argument("--replicate-id", required=True)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--cache-dir", type=Path)
    run.add_argument("--stop-after-pos", type=int, choices=BOUNDARIES,
                     help="save a checkpoint and exit after this completed learning block")
    run.add_argument("--resume", action="store_true", help="resume the same cleanly paused Episode")
    run.add_argument("--verify-runtime", action="store_true", help="record the pinned image and AutoSkill revision")
    summary = commands.add_parser("summarize", help="read existing evidence without model requests")
    summary.add_argument("run_dir", type=Path)
    summary.add_argument("--out", type=Path)
    compare = commands.add_parser("compare", help="compare existing Episode summaries descriptively")
    compare.add_argument("summaries", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command in {"summarize", "compare"}:
        from .metrics import compare_episodes, write_summary

        if args.command == "summarize":
            write_summary(args.run_dir, args.out or args.run_dir)
        else:
            reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.summaries]
            print(json.dumps(compare_episodes(reports), ensure_ascii=False, indent=2))
        return
    config = evaluation_configuration(
        backbone=args.backbone, judge_model=args.judge_model, judge_revision=args.judge_revision,
        output_evidence_tasks=args.output_evidence_task, require_credentials=args.command == "run",
    )
    if args.command == "config-check":
        print(json.dumps({"ready": not config["missing_credentials"], **config}, ensure_ascii=False, indent=2))
    else:
        run_episode(args, config)


if __name__ == "__main__":
    main()
