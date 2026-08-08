"""The one place a METHOD reaches the backbone.

Several methods run their own LLM: AutoSkill's reviser, SkillsVote's voters,
SkillClaw's and SkillOpt's revisers, EvoSkill's proposer/generator. Each used to
hardcode its own provider, endpoint and key lookup, which made "the method's LLM"
and "the executor's LLM" two independent things that could silently drift apart.

They all read from here instead, and `build_method()` has already exported the
resolved backbone into the environment (misevolve.backbones.backbone_env), so a
method's internal calls always land on the same model as the executor whose
trajectories it is distilling. That is what makes a result attributable to the
method rather than to a mismatch between its parts.

Everything is OpenAI-compatible /v1, so a method needs one client shape, not one
per provider.
"""

from __future__ import annotations

import os


def model() -> str:
    m = os.environ.get("MISEVOLVE_MODEL", "").strip()
    if not m:
        raise RuntimeError(
            "MISEVOLVE_MODEL is unset — a method's LLM must run on the resolved "
            "backbone. Build methods through misevolve.methods.build_method()."
        )
    return m


def base_url() -> str:
    value = os.environ.get("MISEVOLVE_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("MISEVOLVE_BASE_URL is unset; build methods through misevolve.methods.build_method().")
    return value


def anthropic_base_url() -> str:
    value = os.environ.get("MISEVOLVE_ANTHROPIC_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("the resolved backbone does not provide an Anthropic Messages endpoint")
    return value


def api_key() -> str:
    k = os.environ.get("MISEVOLVE_API_KEY", "").strip()
    if not k:
        raise RuntimeError(
            "MISEVOLVE_API_KEY is unset — export the backbone's key_env (configs/backbones.yaml) before running."
        )
    return k


def client():
    """An OpenAI client bound to the backbone. Every backbone speaks /v1."""
    from openai import OpenAI

    return OpenAI(api_key=api_key(), base_url=base_url())


def complete(prompt: str, *, max_tokens: int = 2048, temperature: float = 0.0) -> str:
    """Single-turn completion on the backbone — the shared path for method revisers."""
    resp = client().chat.completions.create(
        model=model(), temperature=temperature, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}]
    )
    return (resp.choices[0].message.content or "").strip()
