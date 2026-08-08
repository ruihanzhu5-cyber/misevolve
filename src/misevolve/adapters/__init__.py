"""misevolve.adapters — concrete AgentRunner implementations (one per harness).

Each runner drives ONE harness inside its own docker container and normalizes the
run to harness.Trajectory. The method (MethodHarness) is orthogonal: it hands
skills to inject and consumes the Trajectory; it never picks the harness.

  build_runner("cc")                      -> default backbone (configs/backbones.yaml)
  build_runner("cc", "grok-4.5")          -> explicit backbone
"""

from __future__ import annotations


def build_runner(harness_id: str, backbone_id: str | None = None):
    """Instantiate the AgentRunner for one (harness x backbone) cell."""
    from misevolve.backbones import resolve_agent

    cfg = resolve_agent(harness_id, backbone_id)
    if cfg.framework == "claudecode":
        from .claudecode_runner import ClaudeCodeRunner

        return ClaudeCodeRunner(cfg)
    if cfg.framework == "codex":
        from .codex_runner import CodexRunner

        return CodexRunner(cfg)
    if cfg.framework == "openclaw":
        from .openclaw_runner import OpenClawRunner

        return OpenClawRunner(cfg)
    if cfg.framework == "hermes":
        from .hermes_runner import HermesRunner

        return HermesRunner(cfg)
    raise ValueError(f"no runner for framework {cfg.framework!r} (tag {cfg.tag!r})")
