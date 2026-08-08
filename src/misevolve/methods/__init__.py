"""core/methods — MethodHarness adapters (one per skill-evolution method).

Orthogonal to core/adapters (AgentRunners): a method OWNS only its skill store +
native retrieve + native evolve; it DELEGATES execution to the AgentRunner bound at
setup(episode_dir, agent). The (method x agent) grid is full — every method runs on
every one of the 4 fixed agents.

  build_method(name) -> MethodHarness
"""

from __future__ import annotations

from misevolve import harness


def build_method(name: str, *, backbone: str | None = None) -> "harness.MethodHarness":
    """Build a skill-evolution method.

    `backbone` is threaded through so that a method's INTERNAL LLM (EvoSkill's
    proposer/generator, SkillOpt, SkillClaw, SkillsVote's voters) runs on the same
    model as the executor it authors skills for. It is exported to the environment
    rather than passed positionally because each method reaches its own LLM client
    differently; misevolve.backbones.backbone_env is the single renderer.
    """
    import os

    from misevolve.backbones import backbone_env

    os.environ.update(backbone_env(backbone))
    n = (name or "no-evolution").lower().replace("_", "-")
    if n in ("no-evolution", "none", "floor"):
        from .no_evolution import NoEvolution

        return NoEvolution()
    if n == "skillclaw":
        from .skillclaw import SkillClaw

        return SkillClaw()
    if n == "evoskill":
        from .evoskill import EvoSkill

        return EvoSkill()
    if n == "autoskill":
        from .autoskill import AutoSkill

        return AutoSkill()
    if n == "skillsvote":
        from .skillsvote import SkillsVote

        return SkillsVote()
    if n == "hermes-native":
        from .hermes_native import HermesNative

        return HermesNative()
    if n == "skillopt":
        from .skillopt import SkillOpt

        return SkillOpt()
    raise ValueError(f"unknown method {name!r}")
