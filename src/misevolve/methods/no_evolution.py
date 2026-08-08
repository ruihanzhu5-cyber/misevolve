"""No-Evolution — the FLOOR baseline. No skill store, no retrieve, no evolve; the
agent runs every task with NOTHING injected. If a benign task still achieves the
malicious goal HERE, that's a floor artifact (not method-induced) — so this arm
defines the zero against which every method's persistent-poison ASR is measured.
"""

from __future__ import annotations

from pathlib import Path

from misevolve import harness


class NoEvolution(harness.MethodHarness):
    name = "no-evolution"
    needs_reward = False
    evolves_on_benign = False

    def setup(self, episode_dir: Path, agent: "harness.AgentRunner") -> None:
        self.agent = agent
        self._dir = Path(episode_dir)

    def run_task(self, prompt: str, *, kind: str, pos: int, out_dir: Path) -> "harness.Trajectory":

        return self.agent.run(prompt, [], out_dir=out_dir)

    def evolve_batch(self, traces, outcomes, *, kind: str, round_idx: int) -> None:
        return

    def authored_skills(self) -> "list[harness.Skill]":
        return []

    def teardown(self) -> None:
        pass
