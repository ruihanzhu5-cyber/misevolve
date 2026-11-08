"""Phase 3 configuration, explicit execution, and offline reporting entry points."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path

from misevolve._stage7_api import DEEPSEEK_MODEL, ProviderCalls, Scope
from misevolve._stage7_openclaw import BACKBONE
from misevolve.paths import repo_root
from misevolve.run import load_episode
from misevolve.stage7 import _trace_native

from .episode import TeamEpisodeScheduler
from .executor import TeamWorkerPool
from .judge import TeamJudge, evaluation_configuration
from .openclaw import isolated_openclaw_factory
from .router import ChainRouter, HierarchicalRouter, PeerToPeerRouter

ROUTERS = {cls.protocol: cls for cls in (ChainRouter, HierarchicalRouter, PeerToPeerRouter)}


def _configure_native(workers, calls):
    def configure_ingest(worker, task):
        scope = Scope(task["task_run_id"], worker.worker_id)
        _trace_native(worker, calls, scope, worker.episode_dir / "native_responses.jsonl")

    for worker in workers.values():
        worker.method._exchange.before_ingest = lambda task, worker=worker: configure_ingest(worker, task)


def run_episode(args, config):
    if config["missing_credentials"]:
        raise ValueError(f"missing credentials: {', '.join(config['missing_credentials'])}")
    episode = load_episode(args.episode)
    valid_ids = {task["instance_id"] for task in episode["task_sequence"]}
    if set(config["judge"]["output_evidence_tasks"]) - valid_ids:
        raise ValueError("output evidence allowlist includes tasks outside the episode")
    root = args.out.resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("team episode requires a new output directory")
    code_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True,
    ).strip()
    calls = ProviderCalls(root, deepseek_peak=None)
    calls.run_id = uuid.uuid4().hex
    cache_dir = args.cache_dir or root.parent / "artifact_judge_cache"
    judge = TeamJudge(calls, root / "judge_results.jsonl", cache_dir, config)
    metadata = {key: config[key] for key in ("execution", "judge")}
    metadata.update(run_id=calls.run_id, replicate_id=args.replicate_id, code_sha=code_sha)
    with isolated_openclaw_factory(calls) as factory:
        pool = TeamWorkerPool(root, runner_factory=factory, backbone=args.backbone)
        return TeamEpisodeScheduler(
            pool, ROUTERS[args.protocol], judge=judge, artifact_judge=judge.artifact,
            configure_workers=lambda workers: _configure_native(workers, calls), run_metadata=metadata,
        ).run(episode)


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
