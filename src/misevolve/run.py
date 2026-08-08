"""Run one episode, evolution method, and agent harness cell."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from misevolve import harness
from misevolve.adapters import build_runner
from misevolve.judges import artifact as _content
from misevolve.judges import behavioral as ahz_judge
from misevolve.methods import build_method
from misevolve.paths import repo_root

_REPO = repo_root()
EPISODES_JSON = _REPO / "data" / "episodes" / "episodes_directive.json"


def load_episode(episode_id: str, episodes_json: Path | None = None) -> dict:
    d = json.loads(Path(episodes_json or EPISODES_JSON).read_text())
    eps = d["episodes"] if isinstance(d, dict) else d
    for ep in eps:
        if ep.get("episode_id") == episode_id:
            return ep
    raise KeyError(f"episode {episode_id!r} not in {episodes_json or EPISODES_JSON}")


def _traj_msgs(traj: harness.Trajectory) -> list[dict]:
    """Return the assistant messages and tool calls consumed by the judge."""
    if traj.messages:
        return traj.messages
    raw = traj.raw or {}
    return list(raw.get("assistant_messages") or []) + list(raw.get("tool_calls") or [])


def make_judge(ep: dict) -> "harness.JudgeFn":
    """Build the task-level harm and utility judge for an episode."""
    category = ep.get("hazard_category") or ep.get("task_surface", "")
    comment = ep.get("mechanism", "")
    jb = ep.get("task_surface", "")
    want_util = os.environ.get("AHZ_JUDGE_UTILITY", "1") == "1"

    def judge(traj, mal_goal, prompt, kind) -> harness.TaskOutcome:
        if traj.error:
            return harness.TaskOutcome(harmful=None, utility=None)
        msgs = _traj_msgs(traj)

        harmful = None
        try:
            hv = ahz_judge.judge_trajectory(
                {"query": mal_goal, "category": category, "comment": comment, "jailbreak_method": jb}, msgs
            )
            harmful = None if (hv.get("raw") == "" or hv.get("error")) else hv.get("harmful")
        except Exception as e:
            print(f"[judge] harmful judge error: {e}", file=sys.stderr)

        utility = None
        if want_util and kind != "M":
            try:
                uv = ahz_judge.judge_benign_utility({"query": prompt, "category": category}, msgs)
                utility = None if (uv.get("raw") == "" or uv.get("error")) else uv.get("completed")
            except Exception as e:
                print(f"[judge] utility judge error: {e}", file=sys.stderr)
        return harness.TaskOutcome(harmful=harmful, utility=utility)

    return judge


def content_judge_fn(skill_text: str) -> dict:
    """Return content-unsafety, generalization, and stealth scores for one skill."""
    try:
        return _content.judge_one(skill_text)
    except Exception as e:
        print(f"[content_judge] error: {e}", file=sys.stderr)
        return {}


def run(
    episode_id: str,
    method_name: str,
    harness_id: str,
    run_dir: Path,
    *,
    backbone: str | None = None,
    max_tasks: int | None = None,
    episodes_json: Path | None = None,
    schedule: str | None = None,
    episode_index: int = 0,
    governance: str = "none",
    governance_config: Path | None = None,
) -> list[dict]:
    ep = load_episode(episode_id, episodes_json)
    if schedule:
        from misevolve.bench.schedules import build_task_sequence

        tasks = ep["task_sequence"]
        malicious = [task for task in tasks if task["kind"] == "M"]
        benign = [task for task in tasks if task["kind"] == "B"]
        persistence = [task for task in tasks if task["kind"] == "P"]
        ep = dict(ep)
        ep["task_sequence"] = build_task_sequence(malicious, benign, persistence, schedule, episode_index)
    if max_tasks is not None:
        ep = dict(ep)
        ep["task_sequence"] = ep["task_sequence"][:max_tasks]

    from misevolve.judges import config as judge_config

    judge_config.assert_independent(backbone)

    runner = build_runner(harness_id, backbone)
    method = build_method(method_name, backbone=backbone)
    if governance.lower() not in ("", "none", "raw"):
        from misevolve.governance.registry import build_governance

        method = build_governance(
            governance,
            method,
            log_dir=Path(run_dir) / "_governance",
            config_path=governance_config,
        )
    judge = make_judge(ep)
    return harness.run_episode(ep, method, runner, judge, content_judge_fn, Path(run_dir))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one (episode x method x harness) cell of the main table.")
    ap.add_argument("--episode", required=True, help="episode_id in the episodes JSON")
    ap.add_argument(
        "--method",
        default="no-evolution",
        help="no-evolution | skillclaw | autoskill | skillsvote | evoskill | skillopt | hermes-native",
    )
    ap.add_argument("--harness", default="cc", help="cc | codex | openclaw | hermes  (configs/harnesses.yaml)")
    ap.add_argument(
        "--backbone",
        default=None,
        help="model for the executor AND every method-internal LLM; omit to use the default in configs/backbones.yaml",
    )
    ap.add_argument("--out", required=True, help="run_dir for outputs (all_rows.json)")
    ap.add_argument("--max-tasks", type=int, default=None, help="smoke: cap to first N tasks")
    ap.add_argument("--episodes-json", default="")
    ap.add_argument("--schedule", default="", help="early | interleaved | late | batched | partial | fully")
    ap.add_argument("--episode-index", type=int, default=0)
    ap.add_argument("--governance", default="none", help="none | utility-only | safeevolve")
    ap.add_argument("--governance-config", default="")
    args = ap.parse_args()
    rows = run(
        args.episode,
        args.method,
        args.harness,
        Path(args.out),
        backbone=args.backbone,
        max_tasks=args.max_tasks,
        episodes_json=(Path(args.episodes_json) if args.episodes_json else None),
        schedule=(args.schedule or None),
        episode_index=args.episode_index,
        governance=args.governance,
        governance_config=(Path(args.governance_config) if args.governance_config else None),
    )
    print(f"\nwrote {len(rows)} rows -> {Path(args.out).resolve() / 'all_rows.json'}")


if __name__ == "__main__":
    main()
