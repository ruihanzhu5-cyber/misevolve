"""Resolve repository resources and pinned method checkouts."""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_BUNDLE = Path(__file__).resolve().parent / "_bundled"


def repo_root() -> Path:
    override = os.environ.get("MISEVOLVE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "configs").is_dir():
        return checkout
    return _PACKAGE_BUNDLE


def config_dir() -> Path:
    override = os.environ.get("MISEVOLVE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "configs"


def method_checkout(directory: str, marker: str, *env_vars: str) -> Path:
    candidates: list[Path] = []
    for env_var in env_vars:
        value = os.environ.get(env_var, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    methods_dir = os.environ.get("MISEVOLVE_METHODS_DIR", "").strip()
    if methods_dir:
        candidates.append(Path(methods_dir).expanduser() / directory)
    candidates.append(repo_root() / "external" / "methods" / directory)
    for candidate in candidates:
        if (candidate / marker).exists():
            return candidate.resolve()
    locations = ", ".join(str(candidate) for candidate in candidates)
    names = ", ".join(env_vars)
    raise RuntimeError(
        f"{directory} checkout not found; run scripts/setup_methods.sh or set {names}. Checked: {locations}"
    )
