"""Shared helpers: SKILL.md -> injected-skill entry, and AHZ-style trajectory dict
-> harness.Trajectory. Reused by the claude-code / codex / hermes one-shot runners
(all normalize the same ar_agenthazard-style trajectory.json shape)."""

from __future__ import annotations

import os

from misevolve import harness


def read_backbone_key(cfg) -> str:
    """The resolved backbone's credential, from the env var that backbone declares
    (configs/backbones.yaml :: key_env). Provider-neutral on purpose: swapping the
    backbone must not require touching an adapter.

    Falls back to MISEVOLVE_API_KEY, which backbones.backbone_env() also exports, so
    a method subprocess and its executor read the same credential.
    """
    for name in (getattr(cfg, "key_env", "") or "", "MISEVOLVE_API_KEY"):
        if name:
            v = os.environ.get(name, "").strip()
            if v:
                return v
    return ""


def skill_to_injected(sk: harness.Skill) -> dict:
    """Parse a SKILL.md (YAML frontmatter name/description + markdown body) into the
    ar_agenthazard injected_skills entry {name, description, body}."""
    text = sk.text or ""
    name, desc, body = None, "", text
    stripped = text.lstrip()
    if stripped.startswith("---"):
        rest = stripped[3:]
        end = rest.find("\n---")
        if end != -1:
            fm = rest[:end]
            body = rest[end + 4 :].lstrip("\n")
            for line in fm.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    k = k.strip().lower()
                    v = v.strip().strip("\"'")
                    if k == "name":
                        name = v
                    elif k == "description":
                        desc = v
    if not name:
        name = (sk.key or "skill").replace("/", "-").replace(" ", "-")[:64] or "skill"
    return {"name": name, "description": desc, "body": body}


def _files_from_tool_calls(tool_calls: list) -> list[str]:
    """Best-effort world-state: paths touched by Write/Edit/create-style tool calls."""
    out: list[str] = []
    for tc in tool_calls or []:
        name = (tc.get("name") or tc.get("tool") or "").lower()
        args = tc.get("input") or tc.get("args") or tc.get("arguments") or {}
        if not isinstance(args, dict):
            continue
        if any(w in name for w in ("write", "edit", "create", "str_replace")):
            p = args.get("file_path") or args.get("path") or args.get("filename")
            if p:
                out.append(str(p))
    return out


def normalize(raw: dict) -> harness.Trajectory:
    """ar_agenthazard-style trajectory dict -> harness.Trajectory."""
    assistant = list(raw.get("assistant_messages") or [])
    tools = list(raw.get("tool_calls") or [])
    messages = list(raw.get("messages") or []) or (assistant + tools)
    final = raw.get("final_text") or ""
    if not final and assistant:
        last = assistant[-1]
        final = last.get("text") or last.get("content") or "" if isinstance(last, dict) else str(last)
    container = raw.get("_container") or {}
    return harness.Trajectory(
        messages=messages,
        final_text=str(final or ""),
        files_changed=raw.get("files_changed") or _files_from_tool_calls(tools),
        error=container.get("error"),
        raw=raw,
    )
