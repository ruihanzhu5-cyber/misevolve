"""Phase 3 configuration, explicit execution, and offline reporting entry points."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from misevolve._stage7_api import BEHAVIOR_INPUT_BYTES, DEEPSEEK_MODEL, ProviderCalls, ProviderError, Scope
from misevolve._stage7_openclaw import BACKBONE
from misevolve.multiagent._io import append_jsonl, atomic_json, read_jsonl
from misevolve.paths import repo_root
from misevolve.run import load_episode
from misevolve.stage7 import _runtime, _trace_native

from .checkpoint import BOUNDARIES, exclusive
from .episode import TeamEpisodeScheduler
from .executor import TeamWorkerPool
from .judge import TeamJudge, evaluation_configuration
from .openclaw import isolated_openclaw_factory
from .router import ChainRouter, HierarchicalRouter, PeerToPeerRouter

ROUTERS = {cls.protocol: cls for cls in (ChainRouter, HierarchicalRouter, PeerToPeerRouter)}
NATIVE_MODULES = frozenset({"extractor", "maintainer"})


class EpisodeCalls(ProviderCalls):
    """Stop further requests after an exhausted provider/transport failure."""

    failure = None

    def input_limit(self, module):
        # AutoSkill repeats some trajectory material in its structured request.
        # The byte guard is separate from the provider's token/context limit.
        return BEHAVIOR_INPUT_BYTES if module in NATIVE_MODULES else super().input_limit(module)

    def ensure_healthy(self):
        if self.failure is not None:
            module, category, status = self.failure
            raise ProviderError(f"team provider circuit stopped: {module}, {category}, HTTP {status}",
                                category="team_provider_circuit")

    def request(self, scope, module, payload, *, correction=None):
        self.ensure_healthy()
        try:
            return super().request(scope, module, payload, correction=correction)
        except ProviderError as exc:
            if exc.category == "input_limit":
                append_jsonl(self.path.with_name("request_rejections.jsonl"), {
                    "run_id": self.run_id, "task_id": scope.task_id, "worker_id": scope.worker_id,
                    "instance_id": scope.instance_id, "module": module, "reason": exc.category,
                    "sent_to_provider": False, **exc.details,
                })
            if (exc.category in {"timeout", "transport_error", "http_error", "invalid_response"}
                    or module in NATIVE_MODULES and exc.category in {"input_limit", "context_limit"}):
                self.failure = (module, exc.category, exc.status)
            raise


def _configure_native(workers, calls):
    def configure_ingest(worker, task):
        calls.ensure_healthy()
        scope = Scope(task["task_run_id"], worker.worker_id, task.get("instance_id"))
        _trace_native(worker, calls, scope, worker.episode_dir / "native_responses.jsonl")

    for worker in workers.values():
        exchange = worker.method._exchange
        exchange.before_ingest = lambda task, worker=worker: configure_ingest(worker, task)
        # The pinned SDK may catch provider exceptions and return an empty list.
        # Keep the actual result and snapshot, but do not mark that ingest successful.
        exchange.after_ingest = calls.ensure_healthy


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
    metadata["execution"] = {**metadata["execution"], "native_input_limit_bytes": calls.input_limit("extractor")}
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

    with isolated_openclaw_factory(calls) as factory:
        pool = TeamWorkerPool(root, runner_factory=factory, backbone=args.backbone, resume=resume)
        try:
            return TeamEpisodeScheduler(
                pool, ROUTERS[args.protocol], judge=judge, artifact_judge=judge.artifact,
                configure_workers=lambda workers: _configure_native(workers, calls), run_metadata=metadata,
                stop_after_pos=getattr(args, "stop_after_pos", None), ensure_healthy=ensure_healthy,
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
