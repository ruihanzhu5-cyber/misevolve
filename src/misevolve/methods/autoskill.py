"""AutoSkill — MethodHarness adapter reusing AutoSkill's NATIVE skill self-evolution.

AutoSkill (ECNU-ICALK, arXiv:2603.01145) is an "Experience-driven Lifelong Learning"
SDK: its OpenClaw plugin fires skill distillation on a session-end (agent_end) hook.
That hook body is `AutoSkill.ingest(messages=..., events=..., user_id=..., ...)`
(AutoSkill4OpenClaw/service_runtime.py:1216, and the offline trajectory extractor
autoskill/offline/trajectory/extract.py:106 both funnel into the same call). Ingest =
extract one candidate -> maintain (dedupe/merge/version) -> persist SKILL.md to the
LocalSkillStore. Retrieval is `AutoSkill.search`; serialization is `export_skill_md`.

DECOUPLING (per core/harness.py): we do NOT run AutoSkill's OpenClaw runtime for
EXECUTING tasks. The shared AgentRunner injects skills into the agent's native skill
path (file-drop) and captures the trajectory. We reuse ONLY AutoSkill's:
  * evolution  -> `sdk.ingest(...)`      (the agent_end hook body, fed our Trajectory)
  * store      -> `store={"provider":"local","path": <episode>/autoskill_store}`
  * retrieval  -> `sdk.search(...)`  +  serializer `sdk.export_skill_md(...)`
AutoSkill distills every session (no reward gate) -> evolves_on_benign=True.

Reviser LLM (extraction/maintenance) runs on the RESOLVED BACKBONE — the same model
as the executor whose trajectories it distills (misevolve.methods._backbone), over the
OpenAI-compatible /v1 endpoint with
read ONLY the ``text`` content blocks: reasoning lands in a SEPARATE ``thinking`` block
so ``text`` is clean (no ``<think>`` leak — unlike /v1/chat/completions, which cannot
suppress it). AutoSkill's extractor + maintainer each build an ``LLM``; we REPLACE both
``.complete`` methods with this anthropic call (no strip needed). Embeddings stay on
AutoSkill's dependency-free `hashing` backend (no external embedding call).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import anthropic

from misevolve import harness
from misevolve.methods import _backbone
from misevolve.paths import method_checkout


def _autoskill_root() -> Path:
    """Resolve the read-only AutoSkill repo (env AUTOSKILL_REPO overrides)."""
    return method_checkout("AutoSkill", "autoskill/__init__.py", "MISEVOLVE_AUTOSKILL_DIR", "AUTOSKILL_REPO")


def _read_backbone_key() -> str:
    return _backbone.api_key()


_MM_MAXTOK_CAP = 16384


def _mm_text(resp) -> str:
    """Concatenate an anthropic-messages response's ``text`` blocks (ignoring the
    ``thinking`` block) -> clean reviser output, no <think> leak."""
    return "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", None) == "text").strip()


def _content_to_text(v) -> str:
    """Flatten a message content field (str | list-of-parts | dict) to text."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        out = []
        for it in v:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                out.append(str(it.get("text") or it.get("content") or ""))
        return "".join(out)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("content") or "")
    return str(v)


class AutoSkill(harness.MethodHarness):
    name = "autoskill"
    needs_reward = False
    evolves_on_benign = True

    _UID = "skillmisevo"

    def __init__(self) -> None:
        self._exchange = None

    def enable_skill_exchange(self, worker_id: str) -> None:
        """Opt in after setup; the single-agent lifecycle remains unchanged by default."""
        from misevolve.multiagent.exchange import AutoSkillExchange

        self._exchange = AutoSkillExchange(self.sdk, self._dir, user_id=self._UID, worker_id=worker_id)
        self._retrieved_by_traj: dict[int, list[dict]] = {}
        self._task_by_traj: dict[int, dict] = {}
        self._last_evolution_events = []

    def export_skill_bundle(self, local_skill_key: str) -> dict:
        from misevolve.multiagent.exchange import export_bundle

        if self._exchange is not None:
            return self._exchange.snapshot(local_skill_key)
        return export_bundle(self.sdk, local_skill_key)

    def import_external_skill(self, skill_files, global_skill_id, *, source_agent="", source_local_id="") -> str:
        if self._exchange is None:
            raise RuntimeError("enable_skill_exchange must be called before importing")
        return self._exchange.import_skill(
            skill_files, global_skill_id, source_agent=source_agent, source_local_id=source_local_id,
        )

    def setup(self, episode_dir: Path, agent: "harness.AgentRunner") -> None:
        self.agent = agent
        self._dir = Path(episode_dir)

        self._store_dir = self._dir / "autoskill_store"
        self._store_dir.mkdir(parents=True, exist_ok=True)

        root = _autoskill_root()
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from autoskill import AutoSkill as _AutoSkillSDK
        from autoskill import AutoSkillConfig

        key = _read_backbone_key()
        base_url = os.environ.get("AUTOSKILL_LLM_BASE_URL") or _backbone.anthropic_base_url()
        self._model = os.environ.get("AUTOSKILL_LLM_MODEL") or _backbone.model()
        self._maxtok = min(int(os.environ.get("AUTOSKILL_LLM_MAX_TOKENS", "4096")), _MM_MAXTOK_CAP)

        cfg = AutoSkillConfig(
            llm={
                "provider": "openai",
                "model": self._model,
                "base_url": base_url,
                "api_key": key,
                "max_tokens": self._maxtok,
            },
            embeddings={"provider": "hashing", "dims": 256},
            store={"provider": "local", "path": str(self._store_dir)},
            maintenance_strategy="llm",
        )
        self.sdk = _AutoSkillSDK(cfg)

        self._areviser = anthropic.Anthropic(base_url=base_url, api_key=key)
        self._wrap_anthropic_complete()

        h = os.environ.get(
            "AUTOSKILL_HINT", "Extract any reusable workflow, procedure, or capability demonstrated in this session."
        )
        self._hint = h.strip() or None

        self._top_k = int(os.environ.get("AUTOSKILL_TOP_K", "5"))
        self._prompt_by_traj: dict[int, str] = {}
        self._prov: dict[str, tuple[str, int]] = {}
        self._last_kind = "M"
        self._last_round = 0
        self._last_retrieved: list[harness.Skill] = []

    def run_task(
        self, prompt: str, *, kind: str, pos: int, out_dir: Path, task_run_id: str | None = None,
    ) -> "harness.Trajectory":
        task = {"pos": pos, "kind": kind}
        if self._exchange is not None:
            task["task_run_id"] = task_run_id or uuid.uuid4().hex
        skills = self._retrieve(prompt)
        self._last_retrieved = skills
        if self._exchange is None:
            traj = self.agent.run(prompt, skills, out_dir=out_dir)
        else:
            self._exchange.record_retrieval(task, skills, status=self._last_retrieval_status)
            retrieved = [self._exchange.snapshot(s.key) for s in skills]
            traj = self._exchange.run_with_intent(
                task, self.agent, prompt, skills, out_dir=out_dir, source="native_search",
            )
        self._remember_task(traj, prompt, task, retrieved if self._exchange is not None else [])
        return traj

    def _remember_task(self, traj, prompt, task, retrieved):
        self._prompt_by_traj[id(traj)] = prompt
        if self._exchange is not None:
            self._retrieved_by_traj[id(traj)] = retrieved
            self._task_by_traj[id(traj)] = task

    def _retrieve(self, query: str) -> "list[harness.Skill]":
        """AutoSkill native retrieval -> harness.Skill list (full SKILL.md text)."""
        self._last_retrieval_status = "completed"
        try:
            hits = self.sdk.search(query, user_id=self._UID, limit=self._top_k, scope="all")
        except Exception as e:
            self._last_retrieval_status = "failed"
            print(f"[autoskill] search failed: {e}", flush=True)
            return []
        out: list[harness.Skill] = []
        for h in hits:
            s = getattr(h, "skill", None)
            if s is None:
                continue
            try:
                text = self.sdk.export_skill_md(s.id) or ""
            except Exception:
                self._last_retrieval_status = "partial"
                text = ""
            if not text.strip():
                continue
            src, rnd = self._prov.get(s.id, ("", 0))
            out.append(harness.Skill(key=s.id, text=text, source=src, round=rnd))
        return out

    def evolve_batch(self, traces, outcomes, *, kind: str, round_idx: int) -> None:
        self._last_kind, self._last_round = kind, round_idx
        if self._exchange is not None:
            self._exchange.events = []
            self._last_evolution_events = self._exchange.events
        for traj in traces:
            prompt = self._prompt_by_traj.get(id(traj), "")
            messages, events = self._traj_to_session(traj, prompt)
            if not messages and not events:
                continue
            try:
                ingest = self.sdk.ingest
                if self._exchange is not None:
                    def ingest(**kwargs):
                        task_info = {**self._task_by_traj.get(id(traj), {}), "kind": kind, "round": round_idx}
                        kwargs["metadata"] = {**kwargs["metadata"], "task_run_id": task_info.get("task_run_id")}
                        return self._exchange.ingest(
                            task=task_info,
                            retrieved=self._retrieved_by_traj.get(id(traj), []), **kwargs,
                        )
                ingest(
                    user_id=self._UID,
                    messages=(messages or None),
                    events=(events or None),
                    metadata={
                        "channel": "skillmisevo",
                        "trigger": "agent_end",
                        "kind": kind,
                        "round": int(round_idx),
                    },
                    hint=self._hint,
                )
            except Exception as e:
                if self._exchange is not None:
                    raise
                print(f"[autoskill] ingest failed (kind={kind} r{round_idx}): {e}", flush=True)

        self._record_prov(kind, int(round_idx))

    def _traj_to_session(self, traj: "harness.Trajectory", prompt: str):
        """Convert our normalized Trajectory into AutoSkill's ingest session input
        (OpenAI-style messages + optional tool events), mirroring the offline
        trajectory path. The user prompt is the primary reusable-preference signal;
        assistant text + tool activity form the workflow evidence."""
        messages: list[dict] = []
        events: list[dict] = []
        body_parts: list[str] = []
        if prompt and prompt.strip():
            messages.append({"role": "user", "content": prompt.strip()})
        for m in traj.messages or []:
            if not isinstance(m, dict):
                txt = str(m).strip()
                if txt:
                    body_parts.append(txt)
                continue
            name = m.get("name") or m.get("tool")
            args = m.get("input")
            if args is None:
                args = m.get("args")
            if args is None:
                args = m.get("arguments")
            if name and args is not None:
                try:
                    arg_s = json.dumps(args, ensure_ascii=False)
                except Exception:
                    arg_s = str(args)
                events.append({"event_type": "tool_call", "name": str(name), "arguments": args})
                body_parts.append(f"[tool_call {name}] {arg_s[:600]}")
                continue
            role = str(m.get("role") or "").strip().lower()
            txt = _content_to_text(m.get("content") if m.get("content") is not None else m.get("text")).strip()
            if not txt:
                continue
            if role == "user":
                messages.append({"role": "user", "content": txt})
            else:
                body_parts.append(txt)
        final = (traj.final_text or "").strip()
        if final and final not in "\n".join(body_parts):
            body_parts.append(final)
        assistant_text = "\n".join(p for p in body_parts if p).strip()
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
        return messages, events

    def _record_prov(self, kind: str, round_idx: int) -> None:
        try:
            for s in self.sdk.list(user_id=self._UID):
                if s.id not in self._prov:
                    self._prov[s.id] = (kind, round_idx)
        except Exception as e:
            print(f"[autoskill] provenance list failed: {e}", flush=True)

    def authored_skills(self) -> "list[harness.Skill]":
        out: list[harness.Skill] = []
        try:
            skills = self.sdk.list(user_id=self._UID)
        except Exception as e:
            print(f"[autoskill] authored_skills list failed: {e}", flush=True)
            return out
        for s in skills:
            try:
                text = self.sdk.export_skill_md(s.id) or ""
            except Exception:
                text = ""
            src, rnd = self._prov.get(s.id, (self._last_kind, self._last_round))
            out.append(harness.Skill(key=s.id, text=text, source=src, round=rnd))
        return out

    def teardown(self) -> None:
        client = getattr(self, "_areviser", None)
        if client is not None:
            client.close()

    def _wrap_anthropic_complete(self) -> None:
        """Replace the reviser LLM's ``complete`` on BOTH the extractor and maintainer
        with a minimax anthropic-messages call (thinking disabled), reading only clean
        ``text`` blocks. AutoSkill calls ``complete(*, system, user, temperature)`` and
        expects the raw content string back."""
        client, model, maxtok = self._areviser, self._model, self._maxtok

        def _complete(*, system=None, user="", temperature=0.0):
            req = {
                "model": model,
                "max_tokens": maxtok,
                "messages": [{"role": "user", "content": user or " "}],
                "thinking": {"type": "disabled"},
            }
            if system:
                req["system"] = system
            if temperature is not None:
                req["temperature"] = float(temperature)
            return _mm_text(client.messages.create(**req))

        _complete._minimax_anthropic = True
        for holder in (getattr(self.sdk, "extractor", None), getattr(self.sdk, "maintainer", None)):
            llm = getattr(holder, "_llm", None)
            if llm is not None and hasattr(llm, "complete"):
                llm.complete = _complete
