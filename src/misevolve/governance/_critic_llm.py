"""LLM transport for the SafeEvolve critic and paired deleter."""

from __future__ import annotations

import os

from misevolve.methods import _backbone


def _complete_backbone(prompt: str, model: str, max_tokens: int) -> str:
    anthropic_url = os.environ.get("MISEVOLVE_ANTHROPIC_BASE_URL", "").strip()
    if not anthropic_url:
        return _backbone.complete(prompt, max_tokens=max_tokens, temperature=0.0)
    import anthropic

    client = anthropic.Anthropic(base_url=anthropic_url, api_key=_backbone.api_key())
    response = client.messages.create(
        model=model,
        max_tokens=min(max_tokens, 16384),
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
        thinking={"type": "disabled"},
    )
    return "".join(
        getattr(block, "text", "") for block in (response.content or []) if getattr(block, "type", None) == "text"
    ).strip()


def _complete_override(prompt: str, model: str, max_tokens: int) -> str:
    from openai import OpenAI

    base_url = os.environ.get("SAFEEVOLVE_BASE_URL", "https://openrouter.ai/api/v1")
    key = os.environ.get("SAFEEVOLVE_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("SAFEEVOLVE_API_KEY or OPENROUTER_API_KEY is required")
    client = OpenAI(api_key=key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def complete(prompt: str, model: str, *, max_tokens: int = 900) -> str:
    override = os.environ.get("SAFEEVOLVE_MODEL", "").strip()
    if override:
        return _complete_override(prompt, override, max_tokens)
    resolved = _backbone.model() if model in ("", "$BACKBONE") else model
    if model not in ("", "$BACKBONE") and "/" in model:
        return _complete_override(prompt, resolved, max_tokens)
    return _complete_backbone(prompt, resolved, max_tokens)
