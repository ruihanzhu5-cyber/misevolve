"""Governance registry — resolve a `--governance <name>` flag to a wrapped method.

none          -> the raw method, unchanged (RQ1/RQ2 behavior; governance-off == raw)
utility-only  -> UtilityOnlyGovernance   (config/utility_only.json)
safeevolve    -> SafeEvolveGovernance    (config/safeevolve.json)   [Prompt 2]
secureclaw    -> SecureClawGovernance    (config/secureclaw.json)   [published baseline,
                 adversa-ai/secureclaw used UNMODIFIED from external/secureclaw]
"""

from __future__ import annotations

from pathlib import Path

from .base import GovernanceConfig, build_governed
from .stores import build_store_control
from .utility_only import UtilityOnlyGovernance

_HERE = Path(__file__).resolve().parent
_CONFIG_DIR = _HERE / "config"

GOVERNANCES = {"none", "utility-only", "safeevolve"}


def _load_safeevolve():
    from .safeevolve import SafeEvolveGovernance

    return SafeEvolveGovernance


def build_governance(name: str, inner_method, *, log_dir: Path | None = None, config_path: str | Path | None = None):
    """Wrap `inner_method` with the named governance. `none` returns it unchanged."""
    name = (name or "none").strip().lower()
    if name in ("none", "raw", ""):
        return inner_method
    if name == "utility-only":
        cfg = GovernanceConfig.load(config_path or _CONFIG_DIR / "utility_only.json")
        policy = UtilityOnlyGovernance()
    elif name == "safeevolve":
        cfg = GovernanceConfig.load(config_path or _CONFIG_DIR / "safeevolve.json")
        policy = _load_safeevolve()()
    else:
        raise ValueError(f"unknown --governance {name!r} (choose from {sorted(GOVERNANCES)})")
    return build_governed(inner_method, policy, cfg, store_factory=build_store_control, log_dir=log_dir)
