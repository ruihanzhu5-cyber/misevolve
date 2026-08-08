"""ClaudeCodeRunner — claude-code = the Claude Agent SDK inside the ar_agenthazard
image. GATE-1-verified: injected_skills land in ~/.claude/skills/<name>/SKILL.md and
the SDK loads them (Skill tool). Per-task ephemeral container (docker run --rm -i):
native memory is FRESH every task, so the only cross-task channel is the method
re-injecting its accumulated skill store each run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from misevolve import ahz_sandbox, harness

from . import _normalize


class ClaudeCodeRunner(harness.AgentRunner):
    def __init__(self, cfg: harness.AgentConfig):
        super().__init__(cfg)
        self._auth = ""
        self._episode_dir: Path | None = None
        self._timeout = int(os.environ.get("SKILLMISEVO_TASK_TIMEOUT", "1200"))

    def isolate(self, episode_dir: Path) -> None:

        self._episode_dir = Path(episode_dir)
        self._auth = _normalize.read_backbone_key(self.cfg)
        if not self._auth:
            raise RuntimeError(f"missing backbone credential ${self.cfg.key_env}")

    def run(self, prompt: str, inject: "list[harness.Skill]", *, out_dir: Path) -> harness.Trajectory:
        spec: dict = {"model": self.cfg.backbone, "decomposed_query": [prompt]}
        if inject:
            spec["injected_skills"] = [_normalize.skill_to_injected(s) for s in inject]
        env = {
            "ANTHROPIC_BASE_URL": self.cfg.base_url,
            "ANTHROPIC_AUTH_TOKEN": self._auth,
            "ANTHROPIC_API_KEY": "",
            "IS_SANDBOX": "1",
            "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
        }
        raw = ahz_sandbox.run_in_docker(
            self.cfg.image, json.dumps(spec, ensure_ascii=False), env, timeout_seconds=self._timeout
        )
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "trajectory_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        return _normalize.normalize(raw)

    def teardown(self) -> None:
        pass
