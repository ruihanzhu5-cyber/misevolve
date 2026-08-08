"""Hermes-native — the SPECIAL method: the "skill-evolution method" under test IS
hermes' OWN built-in evolution (curator + memory/learning + in-session skill
authoring). It runs ONLY on the hermes agent (the one framework whose native
self-evolution we are measuring). Every OTHER method keeps hermes' native
evolution OFF and re-injects its own store; here we leave it ON and hermes'
evolved ``HERMES_HOME/skills`` IS the store the artifact judge scores.

HOW THE STORE PERSISTS + EVOLVES (the core problem):
  hermes runs in a per-task ephemeral ``--rm`` container, so a fresh in-container
  HERMES_HOME would reset the native store every task. We opt the HermesRunner
  into PERSISTENT-HERMES_HOME mode (``agent.use_persistent_home``): a single
  episode-scoped HOST dir is bind-mounted as the container's HERMES_HOME on every
  run, so ``skills/`` + memory + sessions ACCUMULATE across the episode's tasks,
  with ``HERMES_NATIVE_EVOLUTION=on``. hermes then NATIVELY retrieves its
  accumulated skills (system-prompt index + skill_view) AND authors/updates them
  in-session — no external injection.

SKILL AUTHORING = BACKGROUND-REVIEW ONLY (the design decision — verified live):
  The "skill-evolution method" under test IS hermes' GENUINE PASSIVE self-
  improvement loop, NOT the generic agent-invoked in-turn tool. So Hermes-native
  authors skills through EXACTLY ONE channel:
  * Background-review "learning" — the PASSIVE path: nudge-triggered, hermes
    forks itself AFTER the response in a ``daemon=True`` thread to "review the
    conversation and update the skill library", writing skills via skill_manage
    from that fork (``agent/background_review.py``). Under a plain ``hermes -z``
    oneshot the process returns and exits before the daemon writes, killing it —
    a ``-z`` ARTIFACT, not a hermes limitation (a real long-lived interactive/
    gateway process lets it finish). The in-container runner (persistent-home /
    native-on mode ONLY) therefore drives the turn IN-PROCESS and ``join()``s the
    ``bg-review`` daemon so it completes + writes — a deterministic, faithful
    stand-in for a long-lived process, using hermes' identical background_review
    code with zero hermes modification. The end-of-turn skill nudge is lowered
    (HERMES_SKILL_NUDGE_INTERVAL, default 1) so the passive review fires on our
    short tasks. This makes Hermes-native a proper "online-intrinsic" control:
    the SYSTEM distills skills the agent never consciously chose to save.
  * In-turn ``skill_manage`` (the agent explicitly saving a skill mid-task) is
    SUPPRESSED: the in-container runner strips ``skill_manage`` from the
    FOREGROUND agent's advertised tools (``_block_foreground_skill_manage``), so
    the agent CANNOT author in-turn — a generic "save a note" action is not the
    hermes-distinctive evolution we are measuring. ``valid_tool_names`` still
    carries it (so the review TRIGGER fires) and the bg-review fork still has it
    (so the passive review can author). ``skills_list`` / ``skill_view``
    (retrieval) stay available. Result: ``fg_authored_skills`` is ALWAYS [].
  * Curator (``hermes curator run`` / session-start ``maybe_run_curator``) — a
    prune / consolidate / archive MAINTENANCE pass over EXISTING agent-created
    skills; it does NOT author new skills, and in oneshot it never even runs
    (the oneshot path bypasses the CLI session-start hook).
  => ``evolve_batch`` is a NO-OP: the single authoring channel (background-review)
     already fired in-session during run_task — the fork spawns before run_task
     returns and its writes are flushed by the in-container join. There is no
     separate tick that AUTHORS skills.
"""

from __future__ import annotations

from pathlib import Path

from misevolve import harness


class HermesNative(harness.MethodHarness):
    name = "hermes-native"
    needs_reward = False
    evolves_on_benign = True

    def setup(self, episode_dir: Path, agent: "harness.AgentRunner") -> None:
        cfg = getattr(agent, "cfg", None)
        framework = getattr(cfg, "framework", None)
        if framework != "hermes":
            raise RuntimeError(
                "Hermes-native is hermes-only (the method under test IS hermes' "
                f"own native evolution); got framework={framework!r} "
                f"(agent {getattr(agent, 'tag', agent)!r}). Use a hermes-* agent."
            )
        self.agent = agent
        self._dir = Path(episode_dir)

        self._home = self._dir / "hermes_native_home"
        self._skills_root = self._home / "skills"
        self._skills_root.mkdir(parents=True, exist_ok=True)
        agent.use_persistent_home(self._home)

        self._provenance: dict[str, dict] = {}
        self._baseline: set[str] = self._snapshot_keys()

    def _snapshot_keys(self) -> set[str]:
        """Stable keys (skill-dir path relative to skills/) of every real SKILL.md
        in the persistent store, skipping hidden trees (.archive, .curator_backups)."""
        keys: set[str] = set()
        root = self._skills_root
        if not root.is_dir():
            return keys
        for md in root.rglob("SKILL.md"):
            rel = md.parent.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            keys.add(str(rel))
        return keys

    @staticmethod
    def _round_from_pos(kind: str, pos: int) -> int:

        if kind == "P":
            return 0
        return min(3, (max(1, pos) - 1) // 6 + 1)

    def run_task(self, prompt: str, *, kind: str, pos: int, out_dir: Path) -> "harness.Trajectory":

        before = self._snapshot_keys()
        traj = self.agent.run(prompt, [], out_dir=out_dir)
        after = self._snapshot_keys()
        rnd = self._round_from_pos(kind, pos)
        for key in sorted(after - before):
            self._provenance.setdefault(key, {"source": kind, "round": rnd})
        return traj

    def evolve_batch(self, traces, outcomes, *, kind: str, round_idx: int) -> None:

        return

    def authored_skills(self) -> "list[harness.Skill]":
        """Every SKILL.md hermes natively authored into the persistent store this
        episode (baseline-excluded), full text, for the artifact judge."""
        out: list[harness.Skill] = []
        root = self._skills_root
        if not root.is_dir():
            return out
        for md in sorted(root.rglob("SKILL.md")):
            rel = md.parent.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            key = str(rel)
            if key in self._baseline:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            prov = self._provenance.get(key, {})
            out.append(
                harness.Skill(
                    key=key,
                    text=text,
                    source=prov.get("source", "B"),
                    round=int(prov.get("round", 0)),
                )
            )
        return out

    def teardown(self) -> None:
        pass
