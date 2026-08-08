"""The BACKBONE x HARNESS resolver.

The agent axis used to be a hardcoded table of `framework-backbone` tags, which
welded the model into the harness definition and scattered one provider's name
through a dozen modules. Here the two are orthogonal:

    harness   (configs/harnesses.yaml)   what CLI runs, in what image, where its
                                         skills live — no model, no endpoint
    backbone  (configs/backbones.yaml)   what model, at what /v1 endpoint, with
                                         which key env — no harness knowledge

`resolve_agent(harness_id, backbone_id)` crosses them into the `AgentConfig` the
runners already consume, so `--harness cc --backbone grok-4.5` is a pure config
change rather than a code change.

ONE BACKBONE PER RUN, EVERYWHERE. The same resolved backbone is handed to (a) the
executor CLI inside the container and (b) any LLM a skill-evolution method uses
internally — EvoSkill's proposer/generator, SkillOpt, SkillClaw, SkillsVote's
voters. `backbone_env()` is the single place that renders those credentials, so a
method can never silently drift onto a different model than the executor it is
supposed to be evolving skills for. Judges are excluded by construction: they read
configs/judges.yaml and assert they are not the backbone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import yaml

from misevolve.paths import config_dir

_CONFIGS = config_dir()


@dataclass(frozen=True)
class Backbone:
    id: str
    model_id: str
    base_url: str
    key_env: str

    anthropic_base_url: str = ""

    def api_key(self) -> str:
        key = os.environ.get(self.key_env, "").strip()
        if not key:
            raise RuntimeError(f"backbone {self.id!r} needs ${self.key_env}; export it or put it in configs/run_env.sh")
        return key


@dataclass(frozen=True)
class Harness:
    id: str
    framework: str
    skill_path: str
    home_env: str
    image: str
    dockerfile: str


@lru_cache(maxsize=1)
def _load(name: str) -> dict:
    return yaml.safe_load((_CONFIGS / name).read_text())


@lru_cache(maxsize=None)
def get_backbone(backbone_id: str | None = None) -> Backbone:
    d = _load("backbones.yaml")
    bid = backbone_id or os.environ.get("MISEVOLVE_BACKBONE") or d["default"]
    if bid not in d["backbones"]:
        raise KeyError(f"unknown backbone {bid!r}; have {sorted(d['backbones'])}")
    return Backbone(id=bid, **d["backbones"][bid])


@lru_cache(maxsize=None)
def get_harness(harness_id: str) -> Harness:
    d = _load("harnesses.yaml")
    if harness_id not in d["harnesses"]:
        raise KeyError(f"unknown harness {harness_id!r}; have {sorted(d['harnesses'])}")
    return Harness(id=harness_id, **d["harnesses"][harness_id])


def resolve_agent(harness_id: str, backbone_id: str | None = None):
    """Cross a harness with a backbone into the AgentConfig the runners consume."""
    from misevolve import harness as _h

    hz, bb = get_harness(harness_id), get_backbone(backbone_id)
    url = bb.base_url
    if hz.framework == "claudecode":
        if not bb.anthropic_base_url:
            raise RuntimeError(
                f"harness 'cc' runs on claude-agent-sdk, which speaks Anthropic Messages, "
                f"but backbone {bb.id!r} declares no anthropic_base_url. Either add one in "
                f"configs/backbones.yaml or drop cc from this run "
                f"(HARNESSES='codex openclaw hermes')."
            )
        url = bb.anthropic_base_url
    return _h.AgentConfig(
        tag=f"{hz.id}-{bb.id}",
        framework=hz.framework,
        backbone=bb.model_id,
        skill_path=hz.skill_path,
        home_env=hz.home_env,
        image=hz.image,
        base_url=url,
        key_env=bb.key_env,
    )


def backbone_env(backbone_id: str | None = None) -> dict[str, str]:
    """Credentials for anything that talks to the backbone — the container executor
    AND a method's internal LLM. Both read this, so they cannot diverge."""
    bb = get_backbone(backbone_id)
    return {
        "MISEVOLVE_MODEL": bb.model_id,
        "MISEVOLVE_BASE_URL": bb.base_url,
        "MISEVOLVE_ANTHROPIC_BASE_URL": bb.anthropic_base_url,
        "MISEVOLVE_API_KEY": bb.api_key(),
        bb.key_env: bb.api_key(),
    }


def list_harnesses() -> list[str]:
    return sorted(_load("harnesses.yaml")["harnesses"])


def list_backbones() -> list[str]:
    return sorted(_load("backbones.yaml")["backbones"])
