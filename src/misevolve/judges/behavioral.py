"""AgentHazard-style LLM judge.

Single LLM call per trajectory: feed the harmful goal + full agent trajectory,
get back {harmful: bool, score: 0-10, reason: str}.

Adapted from AgentHazard's run/llm_judge.py (https://github.com/Yunhao-Feng/AgentHazard).

## Endpoint

Talks to **OpenAI-compatible** chat-completions (OpenAI / OpenRouter /
DeepSeek / Together / Moonshot / local vLLM / sglang / ...) **or** the
**Anthropic Messages API**, selected by the base URL or
``JUDGE_PROTOCOL`` env override. Configured via env:

    JUDGE_BASE_URL  (default https://openrouter.ai/api/v1)
    JUDGE_API_KEY   (required at call time)
    JUDGE_MODEL     (default google/gemini-3-flash from configs/judges.yaml)
    JUDGE_PROTOCOL  (optional: ``openai`` | ``anthropic``;
                     auto-detected from JUDGE_BASE_URL when unset)

Auto-detection: a base URL containing ``anthropic`` is treated as
Anthropic; everything else is treated as OpenAI-compatible. Set
``JUDGE_PROTOCOL`` explicitly to override (e.g. when using a proxy
that doesn't follow the naming pattern).
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from misevolve.judges import config as _cfg

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_JUDGE_PROMPT_PATH = _HERE / "prompts" / "agenthazard_official.md"
_UTIL_PROMPT_PATH = _HERE / "prompts" / "benign_utility.md"


@lru_cache(maxsize=1)
def _judge_prompt() -> str:
    """Load the AgentHazard-official judge prompt from disk.

    Path: ``plugins/scenarios/agenthazard/judge/agenthazard_official.md``.
    Cached after first call (the template never changes within a process).
    The corresponding ``judge.prompt_hash`` is pinned in ``contract.yaml``
    so silent prompt edits surface at contract-load time.
    """
    return _JUDGE_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _util_prompt() -> str:
    """Load the benign-utility judge prompt (orthogonal to the harmful judge).

    Path: ``plugins/scenarios/agenthazard/judge/benign_utility.md``. Scores whether
    a benign trajectory correctly COMPLETED its benign task (utility), NOT harm.
    """
    return _UTIL_PROMPT_PATH.read_text(encoding="utf-8")


def _default_base_url() -> str:

    return os.environ.get("JUDGE_BASE_URL") or _cfg.behavioral().base_url


def _default_model() -> str:
    return os.environ.get("JUDGE_MODEL") or _cfg.behavioral().model


def _resolve_protocol(base_url: str) -> str:
    """Pick 'openai' | 'anthropic' | 'kimi' based on env override or base URL.

    'kimi' is the Moonshot coding endpoint (api.kimi.com/coding) — Anthropic-shaped
    Messages API but authenticated with ``Authorization: Bearer`` (NOT the ``x-api-key``
    header the Anthropic SDK sends, which makes the SDK hang against it). We reach it with
    the same custom-HTTP path content_judge uses, so behavioural + artifact judges share
    the one cross-family Kimi auditor.
    """
    override = os.environ.get("JUDGE_PROTOCOL", "").strip().lower()
    if override in ("openai", "anthropic", "kimi"):
        return override
    b = (base_url or "").lower()
    if "kimi.com" in b:
        return "kimi"
    return "anthropic" if "anthropic" in b else "openai"


def _serialize_traj(trajectory, *, per_field: int = 16000, total: int = 160000) -> str:
    """Serialize a trajectory for the judge WITHOUT letting one big field (e.g. a
    20KB Write payload) eat the whole budget and truncate everything after it.

    Old code did ``json.dumps(traj)[:30000]`` — a single large file write blew past
    30K, so the judge saw a cut-off payload and both (a) mislabeled a COMPLETED
    benign task as incomplete and (b) risked MISSING exfil code that lands after the
    cut in a poisoned Write. We cap each long string field to head+tail (exfil is
    often the POST at the *end* of a file) with an elision marker, keeping every
    tool call visible, then apply a generous total backstop.
    """

    def cap(v):
        if isinstance(v, str) and len(v) > per_field:
            head = per_field * 2 // 3
            tail = per_field - head
            return f"{v[:head]}\n…[{len(v) - per_field} chars elided]…\n{v[-tail:]}"
        if isinstance(v, dict):
            return {k: cap(x) for k, x in v.items()}
        if isinstance(v, list):
            return [cap(x) for x in v]
        return v

    s = json.dumps(cap(trajectory), ensure_ascii=False, indent=2)
    if len(s) > total:
        s = s[:total] + f"\n…[trajectory truncated at {total} chars]…"
    return s


def _call_openai(*, base_url: str, api_key: str, model: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=512,
    )
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _call_anthropic(*, base_url: str, api_key: str, model: str, prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key, base_url=base_url or None)
    msg = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in msg.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _call_kimi(*, base_url: str, api_key: str, model: str, prompt: str) -> str:
    """Moonshot coding endpoint (api.kimi.com/coding) — Anthropic-shaped Messages API
    reached with a raw urllib POST, exactly like content_judge's ``_complete_kimi_anthropic``.
    Uses ``Authorization: Bearer`` (the SDK's ``x-api-key`` header hangs this endpoint)."""
    import urllib.request

    b = (base_url or "https://api.kimi.com/coding").rstrip("/")
    url = b if b.endswith("/messages") else b + "/v1/messages"
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 512,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _call_judge(*, protocol: str, base_url: str, api_key: str, model: str, prompt: str) -> str:
    if protocol == "kimi":
        return _call_kimi(base_url=base_url, api_key=api_key, model=model, prompt=prompt)
    if protocol == "anthropic":
        return _call_anthropic(base_url=base_url, api_key=api_key, model=model, prompt=prompt)
    return _call_openai(base_url=base_url, api_key=api_key, model=model, prompt=prompt)


def judge_trajectory(
    instance: dict[str, Any],
    trajectory: list[dict[str, Any]],
    judge_model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    detail_logs: dict | None = None,
) -> dict[str, Any]:
    """Score a single agent trajectory against an AgentHazard instance.

    Judge prompt is the official AgentHazard ``run/llm_judge.py`` prompt
    verbatim (with the ``comment`` field included).

    Args:
        instance: AgentHazard instance dict — ``query``, ``category``,
            ``jailbreak_method``, ``comment`` (all four expected by the prompt).
        trajectory: list of assistant/tool message dicts captured by the adapter.
        judge_model: model ID (overrides ``JUDGE_MODEL`` env).
        api_key: judge endpoint key (overrides ``JUDGE_API_KEY`` env).
        base_url: judge base URL (overrides ``JUDGE_BASE_URL`` env).
        max_retries: parse retries on bad JSON output.
        detail_logs: optional structured tool-call detail dict (gets rendered
            into the official-prompt ``Detail Logs`` section).

    Returns:
        ``{"harmful": bool, "score": int, "reason": str, "raw": str}``
    """
    key = api_key or os.environ.get("JUDGE_API_KEY") or _cfg.behavioral().api_key()
    base = base_url or _default_base_url()
    model = judge_model or _default_model()
    protocol = _resolve_protocol(base)
    if not key:
        return {
            "harmful": False,
            "score": 0,
            "reason": f"no judge key (${_cfg.behavioral().key_env}); judge skipped",
            "raw": "",
            "model": model,
            "protocol": protocol,
        }

    detail_logs_section = ""
    if detail_logs:
        detail_logs_section = "\n\n【详细工具调用日志（Detail Logs）】\n" + json.dumps(
            detail_logs, ensure_ascii=False, indent=2
        )
    prompt = _judge_prompt().format(
        target=instance.get("query", ""),
        comment=instance.get("comment", ""),
        jailbreak_method=instance.get("jailbreak_method", ""),
        category=instance.get("category", ""),
        trajectory=_serialize_traj(trajectory),
        detail_logs_section=detail_logs_section,
    )

    last_err = ""
    for attempt in range(max_retries):
        try:
            text = _call_judge(
                protocol=protocol,
                base_url=base,
                api_key=key,
                model=model,
                prompt=prompt,
            )
            t = text.strip()
            if t.startswith("```"):
                t = t.split("```", 2)[1]
                if t.startswith("json"):
                    t = t[4:].strip()
                t = t.split("```")[0].strip()
            try:
                parsed = json.loads(t)
            except json.JSONDecodeError:
                import re as _re

                t2 = _re.sub(r",(\s*[}\]])", r"\1", t)
                try:
                    parsed = json.loads(t2)
                except json.JSONDecodeError:
                    _m = _re.search(r"\{.*\}", t2, _re.DOTALL)
                    if not _m:
                        raise
                    parsed = json.loads(_re.sub(r",(\s*[}\]])", r"\1", _m.group(0)))
            return {
                "harmful": bool(parsed.get("harmful", False)),
                "score": int(parsed.get("score", 0)),
                "reason": str(parsed.get("reason", "")),
                "raw": text,
                "model": model,
                "protocol": protocol,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("judge attempt %d failed (%s): %s", attempt + 1, protocol, last_err)

    return {
        "harmful": False,
        "score": 0,
        "reason": f"judge failed after {max_retries} attempts: {last_err}",
        "raw": "",
        "model": model,
        "protocol": protocol,
    }


def judge_benign_utility(
    instance: dict[str, Any],
    trajectory: list[dict[str, Any]],
    judge_model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    detail_logs: dict | None = None,
) -> dict[str, Any]:
    """Score whether a BENIGN trajectory correctly completed its benign task.

    Orthogonal to ``judge_trajectory`` (which scores harm). ``instance["query"]``
    must be the BENIGN task (the benign_user_prompt), NOT a malicious_goal. This is
    the AHZ analog of DTAP's benign ``task_success`` (utility). Returns
    ``{"completed": bool, "utility_score": int, "reason": str, "raw": str}``.
    """
    key = api_key or os.environ.get("JUDGE_API_KEY") or _cfg.behavioral().api_key()
    base = base_url or _default_base_url()
    model = judge_model or _default_model()
    protocol = _resolve_protocol(base)
    if not key:
        return {
            "completed": False,
            "utility_score": 0,
            "reason": "JUDGE_API_KEY not set; utility judge skipped",
            "raw": "",
            "model": model,
            "protocol": protocol,
        }

    detail_logs_section = ""
    if detail_logs:
        detail_logs_section = "\n\n【详细工具调用日志（Detail Logs）】\n" + json.dumps(
            detail_logs, ensure_ascii=False, indent=2
        )
    prompt = _util_prompt().format(
        target=instance.get("query", ""),
        trajectory=_serialize_traj(trajectory),
        detail_logs_section=detail_logs_section,
    )

    last_err = ""
    for attempt in range(max_retries):
        try:
            text = _call_judge(
                protocol=protocol,
                base_url=base,
                api_key=key,
                model=model,
                prompt=prompt,
            )
            t = text.strip()
            if t.startswith("```"):
                t = t.split("```", 2)[1]
                if t.startswith("json"):
                    t = t[4:].strip()
                t = t.split("```")[0].strip()
            try:
                parsed = json.loads(t)
            except json.JSONDecodeError:
                import re as _re

                t2 = _re.sub(r",(\s*[}\]])", r"\1", t)
                try:
                    parsed = json.loads(t2)
                except json.JSONDecodeError:
                    _m = _re.search(r"\{.*\}", t2, _re.DOTALL)
                    if not _m:
                        raise
                    parsed = json.loads(_re.sub(r",(\s*[}\]])", r"\1", _m.group(0)))
            return {
                "completed": bool(parsed.get("completed", False)),
                "utility_score": int(parsed.get("utility_score", 0)),
                "reason": str(parsed.get("reason", "")),
                "raw": text,
                "model": model,
                "protocol": protocol,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("utility judge attempt %d failed (%s): %s", attempt + 1, protocol, last_err)

    return {
        "completed": False,
        "utility_score": 0,
        "reason": f"utility judge failed after {max_retries} attempts: {last_err}",
        "raw": "",
        "model": model,
        "protocol": protocol,
    }
