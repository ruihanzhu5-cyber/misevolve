"""SkillClaw — faithful adapter around SkillClaw's NATIVE skill-evolution stack.

We reuse SkillClaw's own distillation + store + retrieval, and DELEGATE execution
to the shared AgentRunner (which already does what SkillClaw's proxy does: it
file-drops injected skills into the framework's native skill dir and captures the
trajectory). So the only SkillClaw machinery wired here is:

  * evolution  — evolve_server.pipeline.execution.evolve_skill_from_sessions (for
                 sessions that referenced an existing skill) + create_skill_from_
                 sessions (for the no-skill bucket), preceded by the native
                 summarizer (summarize_sessions_parallel) and aggregation
                 (aggregate_sessions_by_skill). This is the SAME pipeline that
                 evolve_server.engines.workflow.EvolveServer.run_once drives — we
                 mirror it, we do NOT reimplement distillation.
  * store      — skillclaw.skill_manager.SkillManager: reads/lists SKILL.md files
                 and serializes an evolved skill dict -> <dir>/<name>/SKILL.md via
                 its own add_skill / _write_skill_md.
  * retrieval  — SkillManager.retrieve(task_description, top_k) (template mode:
                 effectiveness-ranked, zero external deps).

Injection is NOT SkillClaw's proxy here — it is the runner's file-drop
(self.agent.run(prompt, skills, out_dir=...)).

Reviser model: the distiller LLM runs on the RESOLVED BACKBONE — the SAME model as the
executor — over that backbone's OpenAI-compatible /v1 endpoint (openai-completions
protocol) with ``thinking={"type":"disabled"}`` and read ONLY the ``text`` content
blocks — reasoning lands in a SEPARATE ``thinking`` block, so ``text`` is clean with no
``<think>`` leak (unlike the /v1/chat/completions path, which cannot suppress the leak).
This is exactly how the executor (cc/openclaw/hermes) consume minimax. AsyncLLMClient's
``chat`` is REPLACED by this anthropic call — no ``<think>`` strip is needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import anthropic

from misevolve import harness
from misevolve.methods import _backbone
from misevolve.paths import method_checkout

_MM_MAXTOK_CAP = 16384


def _read_backbone_key() -> str:
    return _backbone.api_key()


def _mm_text(resp) -> str:
    """Concatenate an anthropic-messages response's ``text`` blocks (ignoring the
    ``thinking`` block) -> clean reviser output, no <think> leak."""
    return "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", None) == "text").strip()


def _split_openai_messages(messages) -> tuple[str, list[dict]]:
    """OpenAI-style messages -> (anthropic system string, single-user-turn messages).
    These are one-shot distillation prompts, so we collapse all non-system content into
    ONE user turn (always a valid anthropic request)."""
    system_parts, user_parts = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            user_parts.append(str(m))
            continue
        role = str(m.get("role") or "user")
        c = m.get("content")
        text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            user_parts.append("[assistant]\n" + text)
        else:
            user_parts.append(text)
    return "\n\n".join(system_parts), [{"role": "user", "content": "\n\n".join(user_parts) or " "}]


def _skillclaw_repo() -> Path:
    return method_checkout("SkillClaw", "skillclaw/skill_manager.py", "MISEVOLVE_SKILLCLAW_DIR", "SKILLCLAW_REPO")


_SC: dict = {}


def _load_native() -> dict:
    if _SC:
        return _SC
    repo = _skillclaw_repo()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from evolve_server.core.constants import NO_SKILL_KEY, DecisionAction
    from evolve_server.core.llm_client import AsyncLLMClient
    from evolve_server.core.utils import build_skill_md
    from evolve_server.pipeline.aggregation import aggregate_sessions_by_skill
    from evolve_server.pipeline.execution import (
        create_skill_from_sessions,
        evolve_skill_from_sessions,
    )
    from evolve_server.pipeline.summarizer import summarize_sessions_parallel
    from skillclaw.skill_manager import SkillManager

    _SC.update(
        repo=repo,
        SkillManager=SkillManager,
        create_skill_from_sessions=create_skill_from_sessions,
        evolve_skill_from_sessions=evolve_skill_from_sessions,
        summarize_sessions_parallel=summarize_sessions_parallel,
        aggregate_sessions_by_skill=aggregate_sessions_by_skill,
        DecisionAction=DecisionAction,
        NO_SKILL_KEY=NO_SKILL_KEY,
        AsyncLLMClient=AsyncLLMClient,
        build_skill_md=build_skill_md,
    )
    return _SC


def _run_async(coro):
    """Drive an async coroutine to completion from sync code, event-loop-safe."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    box: dict = {}

    def _worker():
        box["r"] = asyncio.run(coro)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box.get("r")


class SkillClaw(harness.MethodHarness):
    name = "skillclaw"
    needs_reward = False
    evolves_on_benign = True

    _MODEL_CANDIDATES = [_backbone.model()]

    def setup(self, episode_dir: Path, agent: "harness.AgentRunner") -> None:
        self.agent = agent
        self._dir = Path(episode_dir)
        sc = _load_native()
        self._sc = sc

        self.skills_dir = self._dir / "skillclaw_store"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        self._top_k = int(os.environ.get("SKILLCLAW_TOP_K", "6"))
        self._provenance: dict[str, dict] = {}
        self._ctx: dict[int, dict] = {}

        key = _read_backbone_key()
        base_url = os.environ.get("SKILLCLAW_LLM_BASE_URL") or _backbone.anthropic_base_url()
        model = os.environ.get("SKILLCLAW_EVOLVE_MODEL", self._MODEL_CANDIDATES[0])
        self.model = model

        self.llm = sc["AsyncLLMClient"](api_key=key, base_url=base_url, model=model, temperature=0.4)
        self._areviser = anthropic.AsyncAnthropic(base_url=base_url, api_key=key)
        cap = int(os.environ.get("SKILLCLAW_MAX_TOKENS_CAP", str(_MM_MAXTOK_CAP)))

        async def _chat_anthropic(messages, **kw):
            system, conv = _split_openai_messages(messages)
            req = {
                "model": model,
                "max_tokens": min(int(kw.get("max_tokens", 4096) or 4096), cap),
                "messages": conv,
                "thinking": {"type": "disabled"},
            }
            if system:
                req["system"] = system
            temp = kw.get("temperature", 0.4)
            if temp is not None:
                req["temperature"] = float(temp)
            resp = await self._areviser.messages.create(**req)
            return _mm_text(resp)

        self.llm.chat = _chat_anthropic

    def _new_manager(self):
        """Fresh SkillManager over the current on-disk store (template retrieval)."""
        return self._sc["SkillManager"](
            skills_dir=str(self.skills_dir),
            retrieval_mode="template",
        )

    def _skill_full_text(self, skill: dict) -> str:
        """Full SKILL.md text (frontmatter + body) for a store skill dict."""
        fp = skill.get("file_path")
        if fp and Path(fp).exists():
            try:
                return Path(fp).read_text(encoding="utf-8")
            except OSError:
                pass
        return self._sc["build_skill_md"](skill)

    def run_task(self, prompt: str, *, kind: str, pos: int, out_dir: Path) -> "harness.Trajectory":

        sm = self._new_manager()
        retrieved = sm.retrieve(prompt, top_k=self._top_k)
        skills: list[harness.Skill] = []
        injected_names: list[str] = []
        for sk in retrieved:
            name = str(sk.get("name") or "").strip()
            if not name:
                continue
            injected_names.append(name)
            prov = self._provenance.get(name, {})
            skills.append(
                harness.Skill(
                    key=name,
                    text=self._skill_full_text(sk),
                    source=prov.get("source", "B"),
                    round=int(prov.get("round", 0)),
                )
            )
        try:
            sm.record_injection(injected_names)
        except Exception:
            pass

        traj = self.agent.run(prompt, skills, out_dir=out_dir)

        self._ctx[id(traj)] = {
            "prompt": prompt,
            "injected": injected_names,
            "kind": kind,
            "pos": pos,
        }
        return traj

    @staticmethod
    def _prompt_from_traj(traj) -> str:
        for m in traj.messages or []:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        return ""

    def _traj_to_session(self, traj, ctx: dict, idx: int) -> dict:
        raw = traj.raw if isinstance(traj.raw, dict) else {}
        prompt = ctx.get("prompt") or self._prompt_from_traj(traj)
        injected = list(ctx.get("injected") or [])

        tool_calls = []
        raw_tcs = raw.get("tool_calls") or []
        if not raw_tcs and isinstance(traj.messages, list):
            raw_tcs = [m for m in traj.messages if isinstance(m, dict) and (m.get("name") or m.get("function"))]
        for tc in raw_tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = tc.get("name") or fn.get("name") or "unknown"
            args = tc.get("arguments", fn.get("arguments"))
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            tool_calls.append(
                {"id": str(tc.get("id") or ""), "function": {"name": str(name), "arguments": str(args or "")}}
            )

        resp = traj.final_text or ""
        reasoning = raw.get("reasoning") or ""
        if reasoning:
            resp = (resp + "\n\n[reasoning]\n" + reasoning).strip() if resp else str(reasoning)
        if traj.files_changed:
            resp = (resp + "\n\n[files_changed] " + ", ".join(map(str, traj.files_changed))).strip()

        hay_parts = [resp.lower(), (self._prompt_from_traj(traj) or "").lower()]
        for tc in tool_calls:
            hay_parts.append(tc["function"]["arguments"].lower())
        for m in traj.messages or []:
            if isinstance(m, dict):
                c = m.get("content") or m.get("text") or ""
                if isinstance(c, str):
                    hay_parts.append(c.lower())
        hay = "\n".join(hay_parts)
        read_skills = []
        for n in injected:
            if not n:
                continue
            slug = n.lower()
            prose = slug.replace("-", " ").replace("_", " ")
            if slug in hay or (len(prose) >= 8 and prose in hay):
                read_skills.append({"skill_name": n})

        turn = {
            "prompt_text": prompt,
            "response_text": resp,
            "tool_calls": tool_calls,
            "tool_results": [],
            "tool_observations": [],
            "tool_errors": [],
            "injected_skills": injected,
            "read_skills": read_skills,
            "modified_skills": [],
            "prm_score": None,
        }
        sid = f"r_{ctx.get('kind', '')}_p{ctx.get('pos', idx)}"
        return {"session_id": sid, "turns": [turn]}

    def evolve_batch(self, traces, outcomes, *, kind: str, round_idx: int) -> None:
        sessions = [self._traj_to_session(t, self._ctx.get(id(t), {}), i) for i, t in enumerate(traces)]
        sessions = [s for s in sessions if s["turns"][0]["prompt_text"] or s["turns"][0]["tool_calls"]]
        if not sessions:
            return
        _run_async(self._evolve_async(sessions, kind, round_idx))

    async def _evolve_async(self, sessions, kind: str, round_idx: int) -> None:
        sc = self._sc

        await sc["summarize_sessions_parallel"](self.llm, sessions)

        grouped = sc["aggregate_sessions_by_skill"](sessions)
        no_skill = grouped.pop(sc["NO_SKILL_KEY"], [])

        sm = self._new_manager()
        existing = {str(s.get("name")): s for s in sm.get_all_skills() if s.get("name")}
        existing_names = list(existing.keys())

        for skill_name, sk_sessions in grouped.items():
            current = self._as_current_skill(existing.get(skill_name))
            try:
                res = await sc["evolve_skill_from_sessions"](self.llm, skill_name, sk_sessions, current, existing_names)
            except Exception as e:
                print(f"[skillclaw] evolve '{skill_name}' failed: {e}", flush=True)
                continue
            self._materialize(res, kind, round_idx, current)

        if no_skill:
            try:
                res = await sc["create_skill_from_sessions"](self.llm, no_skill, existing_names)
            except Exception as e:
                print(f"[skillclaw] create-from-no-skill failed: {e}", flush=True)
                res = None
            self._materialize(res, kind, round_idx, None)

    @staticmethod
    def _as_current_skill(skill: dict | None) -> dict | None:
        if not skill:
            return None
        return {
            "name": skill.get("name", ""),
            "description": skill.get("description", ""),
            "content": skill.get("content", ""),
            "category": skill.get("category", "general"),
        }

    def _materialize(self, res, kind: str, round_idx: int, current: dict | None) -> None:
        sc = self._sc
        if not res or res.get("action") == sc["DecisionAction"].SKIP:
            return
        skill = res.get("skill")
        if not isinstance(skill, dict) or not str(skill.get("name") or "").strip():
            return

        if current:
            for k in ("content", "category", "description"):
                if not skill.get(k):
                    skill[k] = current.get(k, skill.get(k, ""))
        name = str(skill["name"]).strip()
        if self._write_skill(skill):
            self._provenance[name] = {"source": kind, "round": round_idx}
            print(
                f"[skillclaw] authored/updated skill '{name}' "
                f"(action={res.get('action')}, src={kind}, round={round_idx})",
                flush=True,
            )

    def _write_skill(self, skill: dict) -> bool:
        """Serialize an evolved skill dict -> <dir>/<name>/SKILL.md via SkillClaw's store."""
        sm = self._new_manager()
        payload = {
            "name": str(skill.get("name") or "").strip(),
            "description": str(skill.get("description") or "").strip(),
            "content": str(skill.get("content") or ""),
            "category": str(skill.get("category") or "general").strip() or "general",
            "_replace": True,
        }
        try:
            if sm.add_skill(payload):
                return True
        except Exception as e:
            print(f"[skillclaw] SkillManager.add_skill error: {e}", flush=True)

        name = payload["name"]
        safe = name if name and all(c.isalnum() or c in "-_" for c in name) else None
        if not safe:
            return False
        d = self.skills_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(self._sc["build_skill_md"](skill), encoding="utf-8")
        return True

    def authored_skills(self) -> "list[harness.Skill]":
        out: list[harness.Skill] = []
        for md in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            name = md.parent.name
            prov = self._provenance.get(name, {})
            out.append(
                harness.Skill(
                    key=name,
                    text=text,
                    source=prov.get("source", "B"),
                    round=int(prov.get("round", 0)),
                )
            )
        return out

    def teardown(self) -> None:
        pass
