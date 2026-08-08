"""Runs INSIDE the misevolve-hermes container (one task = one --rm run).

Mirrors the ar_agenthazard claude_code in_container_runner contract:

  stdin  (piped by the host HermesRunner via ahz_sandbox.run_in_docker):
      {"model": "MiniMax-M2.7",
       "decomposed_query": ["<prompt>"],
       "injected_skills": [{"name","description","body"}, ...]}

  output (/harness/trajectory.json — the ONLY thing the host reads back):
      {"tool_calls": [...],
       "assistant_messages": [{"role","content","tool_calls"}, ...],
       "final_text": "...",
       "error": null}

Drives hermes' HEADLESS one-shot:  ``hermes -z "<prompt>" -m <model>
--provider minimax-cn``  with cwd ``/work``.  ``-z`` prints ONLY the final
response to stdout (no banner / spinner / session line) and auto-sets
HERMES_YOLO_MODE=1, so approvals are bypassed.

Native-memory policy (NON-hermes-native methods) — controlled by the env var
``HERMES_NATIVE_EVOLUTION`` (default "off"):
  * off  =>  curator.enabled=false (kills the background/session-start skill
             self-evolution loop), memory.memory_enabled=false (no persistent
             memory / every-N-turns writes / learning graph), skill-creation
             nudge=0, and a restricted toolset that drops the ``memory``
             persistence tool.  The `skills` toolset stays so the METHOD's
             injected skills still load + are skill_view-able.
  * on   =>  the opposite (curator + memory ON, `memory` toolset included) —
             this is what the Hermes-NATIVE method adapter sets, because
             hermes' own evolution IS the thing under test for that one cell.
A FRESH HERMES_HOME per run (the container is ephemeral / --rm) and NO
--continue / --resume mean nothing carries across tasks except via the method
re-injecting its store — OR, for Hermes-native, via the PERSISTENT bind-mounted
HERMES_HOME the host runner supplies.

TWO DRIVE PATHS (chosen by HERMES_NATIVE_EVOLUTION):
  * OFF (every non-hermes-native method) => headless ``hermes -z`` SUBPROCESS,
    UNCHANGED. Captures only the foreground turn; that is all these methods need
    (they carry their own store).
  * ON  (Hermes-native) => runs the turn IN-PROCESS via hermes' OWN oneshot
    agent builder (``hermes_cli.oneshot._run_agent``) and then JOINS hermes'
    ``bg-review`` daemon thread. Skill authoring is BACKGROUND-REVIEW ONLY:
      (a) in-turn ``skill_manage`` is SUPPRESSED — the foreground agent has it
          stripped from its advertised tools (_block_foreground_skill_manage), so
          the agent cannot author skills mid-task (a generic "save a note" is not
          the hermes-distinctive evolution under test); and
      (b) the PASSIVE background-review distillation (``agent/background_review``)
          is the sole author
          — hermes forks itself after the turn to "review the conversation and
          update the skill library", writing skills via ``skill_manage`` from a
          separate daemon thread. Under ``hermes -z`` that daemon is spawned but
          the oneshot PROCESS returns + exits before it writes, killing it (a
          ``-z`` artifact, not a hermes limitation: a real long-lived
          interactive/gateway process lets it finish). Running in-process and
          ``join()``-ing the daemon is a deterministic, faithful stand-in for
          that long-lived process, using hermes' IDENTICAL background_review
          code path with zero modification to hermes. The end-of-turn skill
          nudge is gated on tool-iteration count (creation_nudge_interval); we
          set it LOW (HERMES_SKILL_NUDGE_INTERVAL, default 1) so the passive
          review fires on our short tasks.

Session capture is best-effort:  final_text always comes from the -z stdout;
assistant_messages + tool_calls are parsed from the per-session JSON snapshot
(enabled via sessions.write_json_snapshots) under HERMES_HOME/sessions/.  If
that snapshot is missing/unparseable we still return a valid trajectory with
final_text (+ an empty tool_calls list) so the run is never scored as a
container error.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HARNESS = Path("/harness")
WORK = Path("/work")
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "/root/.hermes")


def _log(msg: str) -> None:
    print(f"[hermes-runner] {msg}", file=sys.stderr, flush=True)


def _native_evolution_on() -> bool:
    return os.environ.get("HERMES_NATIVE_EVOLUTION", "off").strip().lower() in ("1", "on", "true", "yes")


def _skill_nudge_interval() -> int:
    """Tool-iteration threshold gating hermes' background-review skill pass.

    hermes fires the passive skill review at the END of a turn only when that
    turn used >= this many tool iterations (turn_finalizer._should_review_skills
    checks ``_iters_since_skill >= _skill_nudge_interval``). The hermes default
    is 10 — too high for our short DTAP tasks — so we set it LOW (default 1) so
    the passive review fires whenever the agent touched >= 1 tool. Only used
    when native evolution is ON; native-off pins creation_nudge_interval=0."""
    try:
        return max(1, int(os.environ.get("HERMES_SKILL_NUDGE_INTERVAL", "1")))
    except (TypeError, ValueError):
        return 1


def _toolsets() -> str:
    """CLI ``-t`` toolset list. `skills` (skills_list/skill_view/skill_manage)
    is kept in both modes so injected skills load + are viewable; the `memory`
    persistence toolset is present ONLY when native evolution is ON."""
    base = ["terminal", "file", "web", "skills", "todo"]
    if _native_evolution_on():
        base.append("memory")
    return ",".join(base)


def _write_config() -> None:
    """Write HERMES_HOME/config.yaml.

    - model.default / model.provider so bare state is coherent (CLI flags win).
    - sessions.write_json_snapshots=True so we can parse assistant_messages +
      tool_calls out of HERMES_HOME/sessions/session_*.json.
    - curator.enabled + memory.memory_enabled: OFF by default (see module
      docstring / HERMES_NATIVE_EVOLUTION). The curator is the background /
      session-start self-evolution loop; disabling it + memory keeps native
      skill/memory state from auto-mutating, so a non-hermes-native method's
      injected store is the ONLY evolving channel. Skill-creation nudge OFF too.
    """
    import yaml

    evo = _native_evolution_on()
    nudge = _skill_nudge_interval() if evo else 0
    cfg = {
        "model": {"default": os.environ.get("HERMES_INFERENCE_MODEL", "MiniMax-M2.7"), "provider": "minimax-cn"},
        "sessions": {"write_json_snapshots": True},
        "curator": {"enabled": evo},
        "memory": {
            "memory_enabled": evo,
            "user_profile_enabled": evo,
            "nudge_interval": 10 if evo else 0,
            "flush_min_turns": 6 if evo else 0,
        },
        "skills": {"creation_nudge_interval": nudge},
    }
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    _log(
        f"config: native_evolution={'ON' if evo else 'OFF'} "
        f"(curator.enabled={evo}, memory_enabled={evo}, "
        f"skill_creation_nudge={nudge}), toolsets={_toolsets()}"
    )


def _write_env_key() -> None:
    """Surface the minimax-cn API key.  It already arrives as a container env
    var (MINIMAX_CN_API_KEY, set by the host runner); also drop it into
    HERMES_HOME/.env so hermes' env loader finds it regardless of how it
    resolves provider credentials."""
    key = os.environ.get("MINIMAX_CN_API_KEY", "").strip()
    if not key:
        _log("WARN: MINIMAX_CN_API_KEY is empty in the container env")
        return
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / ".env").write_text(f"MINIMAX_CN_API_KEY={key}\n", encoding="utf-8")


def _write_skills(injected_skills) -> list[str]:
    """Drop host-provided skills into HERMES_HOME/skills/<name>/SKILL.md so
    hermes indexes them into the system prompt (name+description = layer-2
    visibility) and can skill_view() the body.  Returns the skill names."""
    names: list[str] = []
    root = HERMES_HOME / "skills"
    for s in injected_skills or []:
        name = (s or {}).get("name")
        if not name:
            continue
        desc = (s.get("description") or "").replace("\n", " ").strip()
        body = s.get("body") or ""
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        frontmatter = f"---\nname: {name}\ndescription: {desc}\n---\n"
        (d / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
        names.append(name)
    _log(f"injected {len(names)} skill(s): {names}")
    return names


def _run_hermes(prompt: str, model: str) -> tuple[str, str, int]:
    """Run the one-shot and return (stdout, stderr, returncode)."""
    timeout = int(os.environ.get("HERMES_INCONTAINER_TIMEOUT", "1000"))
    toolsets = _toolsets()

    cmd = ["hermes", "-z", prompt, "-m", model, "--provider", "minimax-cn", "-t", toolsets]
    _log(
        f"exec: hermes -z <prompt {len(prompt)} chars> -m {model} "
        f"--provider minimax-cn -t {toolsets} (cwd=/work, timeout={timeout}s)"
    )
    env = dict(os.environ)
    env.setdefault("HERMES_HOME", str(HERMES_HOME))
    try:
        proc = subprocess.run(
            cmd,
            input=None,
            capture_output=True,
            text=True,
            cwd=str(WORK),
            env=env,
            timeout=timeout,
        )
        return proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as te:
        so = te.stdout or ""
        se = te.stderr or ""
        if isinstance(so, (bytes, bytearray)):
            so = so.decode("utf-8", "replace")
        if isinstance(se, (bytes, bytearray)):
            se = se.decode("utf-8", "replace")
        _log(f"hermes -z TIMED OUT after {timeout}s")
        return so, se + "\n[hermes-runner] oneshot timed out", -1


def _skill_keys() -> set:
    """Stable keys (skill-dir path relative to skills/) of every real SKILL.md
    in HERMES_HOME/skills, skipping hidden trees. Used to attribute a newly
    authored skill to the foreground (skill_manage) vs the background-review
    daemon by snapshotting before/after each."""
    root = HERMES_HOME / "skills"
    keys: set = set()
    if not root.is_dir():
        return keys
    for md in root.rglob("SKILL.md"):
        rel = md.parent.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        keys.add(str(rel))
    return keys


def _join_background_review(deadline: float) -> list[str]:
    """Join hermes' daemon ``bg-review`` thread(s) so the PASSIVE skill/memory
    review completes + writes to HERMES_HOME/skills before the process exits.

    hermes spawns background review as ``threading.Thread(daemon=True,
    name='bg-review')`` at the very end of a turn (run_agent._spawn_background_
    review). Under ``hermes -z`` the oneshot process returns and exits, killing
    the daemon before it writes. In a long-lived interactive/gateway process it
    finishes; ``join()``-ing it here is a deterministic, faithful stand-in for
    that long-lived process."""
    joined: list[str] = []
    for _ in range(4):
        alive = [t for t in threading.enumerate() if t.name == "bg-review" and t.is_alive()]
        if not alive:
            time.sleep(0.5)
            alive = [t for t in threading.enumerate() if t.name == "bg-review" and t.is_alive()]
        if not alive:
            break
        for t in alive:
            t.join(timeout=max(2.0, deadline - time.time()))
            joined.append(t.name)
        if time.time() >= deadline:
            break
    return joined


def _block_foreground_skill_manage() -> bool:
    """Make BACKGROUND-REVIEW the ONLY skill-authoring channel for Hermes-native.

    Hermes-native's DECISION: the "skill-evolution method" under test is hermes'
    GENUINE PASSIVE self-improvement loop (background-review), NOT the generic,
    agent-invoked in-turn ``skill_manage`` "save a note" tool. So we suppress the
    FOREGROUND agent's ability to author skills mid-task while leaving the
    background-review pass fully able to author.

    Mechanism (deterministic, rebuild-proof, zero hermes modification): monkey-
    patch ``run_agent.AIAgent`` so the FOREGROUND oneshot agent gets
    ``skill_manage`` STRIPPED from its advertised tools (``agent.tools``, which is
    built once at init and is exactly what the API request advertises — the model
    literally never sees the tool, so it cannot call it). Two things are
    preserved:
      * ``valid_tool_names`` is snapshotted from the FULL tool list at init
        (agent_init) BEFORE this strip, so it still contains ``skill_manage`` —
        the end-of-turn skill-review TRIGGER (turn_finalizer checks
        ``"skill_manage" in valid_tool_names``) still fires, and
        ``_iters_since_skill`` still increments.
      * The background-review fork constructs its OWN ``AIAgent`` with
        ``parent_session_id`` set (background_review._run_review_in_thread); we
        detect that and DON'T strip it, so the passive review keeps
        ``skill_manage`` and remains the sole author.
    ``skills_list`` / ``skill_view`` (retrieval) stay on the foreground, so native
    skill RETRIEVAL is unaffected — only in-turn AUTHORING is removed.

    Off switch: HERMES_BLOCK_FOREGROUND_SKILL_MANAGE=0. Returns True when active.
    """
    if os.environ.get("HERMES_BLOCK_FOREGROUND_SKILL_MANAGE", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    import run_agent as _ra

    if getattr(_ra.AIAgent, "_skillmisevo_fg_block", False):
        return True
    _Orig = _ra.AIAgent

    class _FgNoSkillManage(_Orig):
        _skillmisevo_fg_block = True

        def __init__(self, *args, **kwargs):

            _is_review_fork = bool(kwargs.get("parent_session_id"))
            super().__init__(*args, **kwargs)
            if _is_review_fork:
                return
            try:
                self.tools = [
                    t for t in (self.tools or []) if ((t or {}).get("function") or {}).get("name") != "skill_manage"
                ]
            except Exception:
                pass

    _ra.AIAgent = _FgNoSkillManage
    return True


def _run_hermes_inprocess(prompt: str, model: str) -> tuple[str, list, dict, str | None]:
    """Native-evolution path: run the turn IN-PROCESS using hermes' OWN oneshot
    agent builder, then JOIN the background-review daemon. For Hermes-native the
    PASSIVE background-review is the ONLY authoring channel (the foreground
    agent's in-turn skill_manage is suppressed via _block_foreground_skill_manage);
    authored skills land in the persistent HERMES_HOME/skills. Returns
    ``(final_text, messages, meta, error)``."""

    os.environ["HERMES_YOLO_MODE"] = "1"
    os.environ["HERMES_ACCEPT_HOOKS"] = "1"
    logging.disable(logging.CRITICAL)

    total = int(os.environ.get("HERMES_INCONTAINER_TIMEOUT", "1000"))
    toolsets = _toolsets().split(",")

    fg_block = _block_foreground_skill_manage()
    _log(
        f"in-process turn (native evolution ON): model={model} "
        f"toolsets={toolsets} skill_nudge={_skill_nudge_interval()} "
        f"foreground_skill_manage={'BLOCKED' if fg_block else 'allowed'}"
    )

    from hermes_cli.oneshot import _run_agent

    before = _skill_keys()
    resp = ""
    result: dict = {}
    error: str | None = None
    t0 = time.time()
    devnull = open(os.devnull, "w", encoding="utf-8")
    try:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            resp, result = _run_agent(prompt, model=model, provider="minimax-cn", toolsets=toolsets)
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            devnull.close()
        except Exception:
            pass
    fg_secs = time.time() - t0
    after_fg = _skill_keys()

    bg_deadline = min(t0 + total - 15.0, time.time() + float(os.environ.get("HERMES_BG_REVIEW_TIMEOUT", "300")))
    joined = _join_background_review(bg_deadline)
    after_bg = _skill_keys()

    meta = {
        "in_process": True,
        "skill_nudge_interval": _skill_nudge_interval(),
        "foreground_skill_manage_blocked": fg_block,
        "fg_authored_skills": sorted(after_fg - before),
        "bg_authored_skills": sorted(after_bg - after_fg),
        "bg_review_threads_joined": joined,
        "fg_seconds": round(fg_secs, 1),
    }
    _log(
        f"in-process done: fg_authored={meta['fg_authored_skills']} "
        f"bg_authored={meta['bg_authored_skills']} joined={joined} "
        f"(error={error})"
    )

    final = resp or (result.get("final_response") if isinstance(result, dict) else "") or ""
    messages = result.get("messages") if isinstance(result, dict) else []
    return final, (messages or []), meta, error


def _newest_session_json() -> Path | None:
    sess = HERMES_HOME / "sessions"
    if not sess.is_dir():
        return None
    cands = sorted(sess.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {"_raw": raw}
        except Exception:
            return {"_raw": raw}
    return {}


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
            else:
                parts.append(str(b))
        return "".join(p for p in parts if p)
    return "" if content is None else str(content)


def _parse_session(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse a hermes session JSON snapshot (OpenAI-style messages) into the
    ar_agenthazard trajectory shape. Used by the native-OFF ``hermes -z`` path,
    which reads back the on-disk session_*.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"session parse failed ({path.name}): {e!r}")
        return [], []
    return _parse_messages(data.get("messages") or [])


def _parse_messages(messages) -> tuple[list[dict], list[dict]]:
    """Parse OpenAI-style messages into the ar_agenthazard trajectory shape.
    Shared by the native-OFF session-JSON path and the native-ON in-process
    path (which passes ``result['messages']`` directly)."""
    assistant_messages: list[dict] = []
    tool_calls: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            norm_tcs = []
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                args = _coerce_args(fn.get("arguments", tc.get("arguments")))
                entry = {"name": name, "arguments": args, "id": tc.get("id", "")}
                tool_calls.append(entry)
                norm_tcs.append(
                    {
                        "id": entry["id"],
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                )
            assistant_messages.append(
                {
                    "role": "assistant",
                    "content": _content_to_text(m.get("content")),
                    "tool_calls": norm_tcs,
                }
            )
        elif role == "tool":
            assistant_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": _content_to_text(m.get("content")),
                }
            )
        elif role == "user":
            assistant_messages.append(
                {
                    "role": "user",
                    "content": _content_to_text(m.get("content")),
                }
            )
    return assistant_messages, tool_calls


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        (HARNESS / "trajectory.json").write_text(
            json.dumps(
                {
                    "tool_calls": [],
                    "assistant_messages": [],
                    "final_text": "",
                    "error": "empty stdin (expected input_spec JSON)",
                }
            )
        )
        return 1

    spec = json.loads(raw)
    dq = spec.get("decomposed_query") or []
    prompt = dq[0] if dq else (spec.get("prompt") or "")
    model = spec.get("model") or "MiniMax-M2.7"
    injected = spec.get("injected_skills") or []

    error: str | None = None
    rc = 0
    final_text = ""
    assistant_messages: list[dict] = []
    tool_calls: list[dict] = []
    result_meta: dict = {}
    try:
        _write_config()
        _write_env_key()
        names = _write_skills(injected)
        if names:
            _log(f"skills indexed under {HERMES_HOME}/skills: {names}")

        if _native_evolution_on():
            final_text, messages, in_meta, in_err = _run_hermes_inprocess(prompt, model)
            assistant_messages, tool_calls = _parse_messages(messages)
            error = in_err
            rc = 0 if in_err is None else 2
            result_meta = dict(in_meta)
            result_meta["returncode"] = rc
        else:
            stdout, stderr, rc = _run_hermes(prompt, model)
            final_text = (stdout or "").strip()
            sess = _newest_session_json()
            if sess is not None:
                assistant_messages, tool_calls = _parse_session(sess)
                _log(f"parsed session {sess.name}: {len(assistant_messages)} msgs, {len(tool_calls)} tool_calls")
            else:
                _log("no session_*.json snapshot found (degraded: final_text only)")
            if error is None and not final_text and not assistant_messages:
                error = f"hermes produced no final text (rc={rc}); stderr tail: {stderr[-1500:]}"
            result_meta = {"returncode": rc, "session_file": (sess.name if sess is not None else None)}
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)

    if not final_text and assistant_messages:
        for m in reversed(assistant_messages):
            if m.get("role") == "assistant" and m.get("content"):
                final_text = m["content"]
                break
    final_text = (final_text or "").strip()
    if error is None and not final_text:
        error = "hermes produced no final text"

    out = {
        "tool_calls": tool_calls,
        "assistant_messages": assistant_messages,
        "final_text": final_text,
        "error": error,
        "result_meta": result_meta,
    }
    HARNESS.mkdir(parents=True, exist_ok=True)
    (HARNESS / "trajectory.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"wrote trajectory.json (final_text {len(final_text)} chars, error={error is not None})")
    return 0 if (error is None) else 2


if __name__ == "__main__":
    sys.exit(main())
