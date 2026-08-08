"""Judge configuration + the independence guarantee.

Both judges read configs/judges.yaml through here, so their models are declared in
one place instead of in per-module env-var defaults.

THE INVARIANT THIS FILE EXISTS TO ENFORCE: a judge must never be the backbone. The
behavioral judge decides whether a trajectory achieved the malicious goal; the
artifact judge rates the skills the method authored. If either ran on the model
under test, the benchmark would be asking a model to grade its own work — so
`assert_independent()` compares both judges against the resolved backbone and
refuses to run on a collision. It is called at import time; there is no flag to
disable it.

Env overrides exist for one-off reruns (MISEVOLVE_BEHAVIORAL_MODEL,
MISEVOLVE_ARTIFACT_MODEL) and are checked by the same assertion.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import yaml

from misevolve.paths import config_dir

_CONFIGS = config_dir()


@dataclass(frozen=True)
class JudgeConfig:
    role: str
    model: str
    base_url: str
    key_env: str
    temperature: float = 0.0

    def api_key(self) -> str | None:
        return os.environ.get(self.key_env, "").strip() or None


@lru_cache(maxsize=1)
def _raw() -> dict:
    return yaml.safe_load((_CONFIGS / "judges.yaml").read_text())


def _get(role: str, env_override: str) -> JudgeConfig:
    d = dict(_raw()[role])
    model = os.environ.get(env_override, "").strip() or d["model"]
    return JudgeConfig(
        role=role,
        model=model,
        base_url=d["base_url"],
        key_env=d["key_env"],
        temperature=float(d.get("temperature", 0.0)),
    )


@lru_cache(maxsize=1)
def behavioral() -> JudgeConfig:
    return _get("behavioral", "MISEVOLVE_BEHAVIORAL_MODEL")


@lru_cache(maxsize=1)
def artifact() -> JudgeConfig:
    return _get("artifact", "MISEVOLVE_ARTIFACT_MODEL")


def _norm(model: str) -> str:
    """Compare on the bare model name so `x-ai/grok-4.5` and `grok-4.5` collide."""
    return model.strip().lower().rsplit("/", 1)[-1]


def assert_independent(backbone_id: str | None = None) -> None:
    """Refuse to run if either judge is the model under test, or if the two judges
    are the same model (they are meant to be cross-family, so a shared judge would
    correlate the behavioural and artifact scores)."""
    from misevolve.backbones import get_backbone

    try:
        bb = get_backbone(backbone_id)
    except Exception:
        return
    b, a = behavioral(), artifact()
    for j in (b, a):
        if _norm(j.model) == _norm(bb.model_id):
            raise RuntimeError(
                f"{j.role} judge ({j.model}) is the backbone under test ({bb.model_id}). "
                f"A model must not grade its own work — pick a different judge in "
                f"configs/judges.yaml or a different --backbone."
            )
    if _norm(b.model) == _norm(a.model):
        raise RuntimeError(
            f"behavioral and artifact judges are both {b.model}; they are meant to be "
            f"cross-family so their scores stay independent."
        )


def summary() -> str:
    b, a = behavioral(), artifact()
    return f"judges: behavioral={b.model} artifact={a.model}"
