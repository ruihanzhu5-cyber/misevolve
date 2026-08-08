"""CodexRunner — codex = the OpenAI-protocol `codex exec` CLI inside the
misevolve-codex image, pointed at MiniMax's OpenAI-compatible Responses
endpoint. Structurally analogous to ClaudeCodeRunner: a per-task ephemeral
container (docker run --rm -i) whose native memory is FRESH every task, so the
only cross-task channel is the method re-injecting its accumulated skill store.

codex has no "Skill tool" — its native project-context loader is AGENTS.md, so the
in-container runner writes the injected skills into /work/AGENTS.md before running.
The minimax key is passed in via container env (MINIMAX_API_KEY) and substituted
into $CODEX_HOME/config.toml by the in-container runner (never baked into the image).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from misevolve import ahz_sandbox, harness

from . import _normalize


class CodexRunner(harness.AgentRunner):
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
            "MISEVOLVE_API_KEY": self._auth,
            "MISEVOLVE_BASE_URL": self.cfg.base_url,
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
