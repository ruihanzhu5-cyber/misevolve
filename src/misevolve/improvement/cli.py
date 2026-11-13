"""Preflight, offline verification, frozen-policy replay and frozen-library evaluation."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from misevolve._provider_retry import TEAM_RETRY_POLICY
from misevolve.multiagent._io import atomic_json
from misevolve.paths import method_checkout, repo_root
from misevolve.team.checkpoint import exclusive
from misevolve.team.cli import ROUTERS, EpisodeCalls
from misevolve.team.judge import TeamJudge, evaluation_configuration
from misevolve.team.openclaw import isolated_openclaw_factory

from .admission import summarize
from .config import load_config
from .controller import Controller
from .model import Model
from .policy import BASE_SHA, PolicyVersion, digest
from .runtime import Runtime
from .store import Store


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def preflight(config_path, *, adaptive=False):
    policy, protocol, splits, manifest = load_config(config_path, Path("unused-access.jsonl"))
    config = evaluation_configuration()
    gaps = list(config["missing_credentials"])
    try:
        sdk = method_checkout("AutoSkill", "autoskill/__init__.py", "MISEVOLVE_AUTOSKILL_DIR", "AUTOSKILL_REPO")
        pin = subprocess.check_output(["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True).strip()
        if pin != "94c47ca488d4ba4117d20272e66d49b9877e68cf":
            gaps.append("wrong AutoSkill revision")
    except (RuntimeError, subprocess.CalledProcessError):
        gaps.append("pinned AutoSkill checkout")
    purposes = Counter(r["purpose"] for r in splits.value["tasks"])
    for purpose in ("online", "development", "inner_confirmation", "final_learning", "final_evaluation"):
        if not purposes[purpose]:
            gaps.append(f"task pool: {purpose}")
    if adaptive:
        for purpose in ("policy_selection", "outer_confirmation"):
            if not purposes[purpose]:
                gaps.append(f"task pool: {purpose}")
    return {
        "ready": not gaps,
        "gaps": gaps,
        "base_sha": BASE_SHA,
        "mode": protocol.mode,
        "policy": policy.hash,
        "split_counts": dict(purposes),
        "manifest": manifest,
        "runtime": config,
        "live_verified": False,
        "isolation_requirement": "pinned Linux OpenClaw image and existing isolated_openclaw_factory",
    }


def report(root):
    store = Store(root, resume=True)
    events = [store.get(ref) for ref in store.state["events"]]
    improvements = [e for e in events if e["type"] == "improvement"]
    policies = [e for e in events if e["type"] == "policy_change"]
    return {
        "window": store.state["window"],
        "active_policy": store.state["policy"],
        "library": store.state["library"],
        "improvement_decisions": dict(Counter(e["decision"]["status"] for e in improvements)),
        "policy_decisions": dict(Counter(e["decision"]["status"] for e in policies)),
        "skill_operations": dict(
            Counter(c["operation"] for e in improvements for changes in e["changes"].values() for c in changes)
        ),
        "repairs": sum(len(e["repairs"]) for e in improvements),
        "interventions": sum(len(e["interventions"]) for e in improvements),
        "rollbacks": sum(e["type"] == "rollback" for e in events),
        "cost": None,
        "cost_missing_reason": "aggregate the original model_calls.jsonl; no imputation",
        "legacy_metrics": "unchanged: python -m misevolve.team.cli summarize RUN_DIR",
        "claim": "implementation observations only; no statistical safety or improvement guarantee",
    }


def execute(args):
    if args.out.exists() and any(args.out.iterdir()):
        raise FileExistsError("replay/evaluation requires a new output directory; no silent paid-call replay")
    policy, protocol, splits, manifest = load_config(args.config, args.out / "access.jsonl")
    config = evaluation_configuration(require_credentials=True)
    initial = read(args.library)
    if set(initial) != {"A", "B", "C"}:
        raise ValueError("initial library must have exactly A/B/C stores")
    plan = read(args.plan) if args.command == "replay" else None
    if plan is not None:
        if (
            set(plan) != {"checkpoints", "sequences", "primary_comparison"}
            or not plan["checkpoints"]
            or not plan["sequences"]
        ):
            raise ValueError("predeclare checkpoints, independent sequences and primary_comparison")
        if len(set(plan["sequences"])) != len(plan["sequences"]):
            raise ValueError("duplicate replay sequence")
        checkpoints = []
        for checkpoint in plan["checkpoints"]:
            if (
                set(checkpoint) != {"label", "path", "window"}
                or checkpoint["window"] not in manifest["checkpoint_windows"]
            ):
                raise ValueError("checkpoint was not predeclared in training config")
            frozen = PolicyVersion.from_dict(read(args.plan.parent / checkpoint["path"]))
            checkpoints.append((checkpoint["label"], frozen))
        labels = [label for label, _ in checkpoints]
        if len(set(labels)) != len(labels) or any(label not in labels for label in plan["primary_comparison"]):
            raise ValueError("invalid checkpoint labels or primary comparison")
    else:
        checkpoints = [("frozen", PolicyVersion.from_dict(read(args.policy)))]
    atomic_json(
        args.out / "run_manifest.json",
        {
            "base_sha": BASE_SHA,
            **manifest,
            "runtime": config,
            "plan": plan,
            "initial_library": digest(initial),
            "topology": args.protocol,
            "policy_frozen": True,
            "training_history_loaded": False,
        },
    )
    calls = EpisodeCalls(args.out, deepseek_peak=None, retry_policy=TEAM_RETRY_POLICY)
    results = []
    try:
        judge = TeamJudge(calls, args.out / "judge_results.jsonl", args.out / "artifact_cache", config)
        with isolated_openclaw_factory(calls) as factory:
            runtime = Runtime(
                args.out / "branches",
                runner_factory=factory,
                backbone=config["execution"]["backbone"],
                router_type=ROUTERS[args.protocol],
                judge=judge,
                configuration=config,
            )
            for label, frozen in checkpoints:
                for sequence in plan["sequences"] if plan else [args.sequence]:
                    safe_id = digest([label, sequence])[:16]
                    controller = Controller(
                        args.out / "controllers" / safe_id,
                        policy=frozen,
                        protocol=protocol,
                        splits=splits,
                        model=Model(calls, args.out / "model_requests.jsonl"),
                        runtime=runtime,
                        adaptive=False,
                        namespace=f"replay-{safe_id}",
                    )
                    if plan:
                        learning = splits.select("final_learning", actor="replay", sequence=sequence)
                        if not learning:
                            raise ValueError(f"missing final_learning sequence {sequence}")
                        produced = controller.rollout(frozen, initial, learning, namespace=f"replay-{safe_id}")
                    else:
                        produced = initial
                    atomic_json(args.out / "libraries" / f"{safe_id}.json", produced)
                    evaluation = splits.select("final_evaluation", actor="evaluator", sequence=sequence)
                    if not evaluation:
                        raise ValueError(f"missing final_evaluation sequence {sequence}")
                    rows = runtime.evaluate(
                        produced,
                        evaluation,
                        purpose="final_evaluation",
                        policy_hash=frozen.hash,
                        pair_id=digest([sequence, manifest, args.protocol]),
                        label="final-frozen",
                    )
                    results.extend({**r, "checkpoint": label, "initial_library_hash": digest(initial)} for r in rows)
                    atomic_json(args.out / "paired_evaluations.json", results)
    finally:
        if calls.path.exists():
            atomic_json(args.out / "api_summary.json", calls.summary())
    atomic_json(
        args.out / "summary.json",
        {
            "by_policy": {
                label: summarize([r for r in results if r["checkpoint"] == label]) for label, _ in checkpoints
            },
            "selection_from_final_scores": False,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("config-check")
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--adaptive", action="store_true")
    commands.add_parser("offline-smoke")
    summary = commands.add_parser("summarize")
    summary.add_argument("root", type=Path, help="run/improvement directory")
    replay = commands.add_parser("replay")
    replay.add_argument("--plan", type=Path, required=True)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--policy", type=Path, required=True)
    evaluation.add_argument("--sequence", required=True)
    for command in (replay, evaluation):
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--library", type=Path, required=True)
        command.add_argument("--protocol", choices=sorted(ROUTERS), required=True)
        command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "config-check":
        print(json.dumps(preflight(args.config, adaptive=args.adaptive), ensure_ascii=False, indent=2))
    elif args.command == "summarize":
        print(json.dumps(report(args.root), ensure_ascii=False, indent=2))
    elif args.command == "offline-smoke":
        import sys

        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/multiagent/test_improvement.py"], cwd=repo_root(), check=True
        )
    else:
        with exclusive(args.out.resolve()):
            execute(args)


if __name__ == "__main__":
    main()
