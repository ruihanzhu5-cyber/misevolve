"""OpenClawRunner — openclaw (real OpenClaw CLI) on the MiniMax backbone, inside the
misevolve-openclaw image (AgentHazard's ClawMonitor container, adapted to Node 24 +
a `minimax` anthropic-messages provider).

Unlike the claude-code/codex/hermes one-shot runners (`docker run --rm -i`, stdin
spec), openclaw drives a LONG-LIVED gateway + a FastAPI api-server (:8000). It still
needs a FRESH filesystem PER TASK (an M task writes git-hooks / /etc/profile.d / cron
/ workspace files that would otherwise leak into a later benign task and fake
contamination), so this runner boots a clean container per task:

  isolate(episode_dir): resolve auth + record the episode dir; DON'T boot yet
    (self.container = None). The per-task boot below is what enforces isolation.

  run(prompt, inject, out_dir): (0) `_rm_container()` the previous container then
    `_boot()` a clean one (`docker run -d` the image with `-e MINIMAX_API_KEY=<key>`
    + `-e MINIMAX_BASE_URL=<cfg.base_url>`, publish :8000 on an ephemeral host port,
    wait for /health) — a fresh ephemeral fs, so the ONLY cross-task channel is the
    method re-injecting its accumulated skill store; (1) clear + write the injected skills into openclaw's
    native managed skills dir (/root/.openclaw/skills/<name>/SKILL.md — verified
    eligible + modelVisible) via `docker cp`; (2) POST the prompt to the api-server
    under a NEW session id (no resume); (3) GET /sessions/{id}/history and convert
    the openclaw session-jsonl records into {assistant_messages, tool_calls,
    final_text, error}; (4) dump raw to out_dir and return _normalize.normalize(raw).

  teardown(): `docker rm -f` the episode container.

Each task uses a fresh session id, so cross-task carry is only via injected skills.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from misevolve import harness

from . import _normalize

_SKILL_ROOT = "/root/.openclaw/skills"


class OpenClawRunner(harness.AgentRunner):
    def __init__(self, cfg: harness.AgentConfig):
        super().__init__(cfg)
        self._auth = ""
        self._episode_dir: Path | None = None
        self._port: int | None = None
        self._base = ""
        self._timeout = int(os.environ.get("SKILLMISEVO_TASK_TIMEOUT", "1200"))
        self._boot_timeout = int(os.environ.get("SKILLMISEVO_BOOT_TIMEOUT", "120"))

    @staticmethod
    def _docker(
        args: list[str], *, timeout: int = 120, check: bool = True, capture: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout, check=check)

    def _http(self, method: str, path: str, body: dict | None = None, *, timeout: int) -> tuple[int, dict | str]:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.getcode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            code = e.code
        try:
            return code, json.loads(raw)
        except json.JSONDecodeError:
            return code, raw

    def isolate(self, episode_dir: Path) -> None:

        self._episode_dir = Path(episode_dir)
        self._auth = _normalize.read_backbone_key(self.cfg)
        if not self._auth:
            raise RuntimeError(f"missing backbone credential ${self.cfg.key_env}")
        self.container = None

    def _boot(self) -> None:
        """Boot a FRESH container (clean fs) + wait for the gateway/api-server."""
        name = f"skillmisevo-oc-{uuid.uuid4().hex[:12]}"
        run_args = [
            "run",
            "-d",
            "--name",
            name,
            "--cpus",
            os.environ.get("SKILLMISEVO_CPUS", "2"),
            "--memory",
            os.environ.get("SKILLMISEVO_MEM", "4g"),
            "-p",
            "127.0.0.1::8000",
            "-e",
            f"MISEVOLVE_API_KEY={self._auth}",
            "-e",
            f"MISEVOLVE_BASE_URL={self.cfg.base_url}",
            "-e",
            f"MISEVOLVE_MODEL={self.cfg.backbone}",
        ]

        _extra_plugins = os.environ.get("OPENCLAW_EXTRA_PLUGINS", "").strip()
        if "clawkeeper" in (self.cfg.image or ""):
            _extra_plugins = ",".join(dict.fromkeys([p for p in (_extra_plugins.split(",") + ["clawkeeper"]) if p]))
        if _extra_plugins:
            run_args += ["-e", f"OPENCLAW_EXTRA_PLUGINS={_extra_plugins}"]
        run_args += [self.cfg.image]
        proc = self._docker(run_args, timeout=120, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed for {self.cfg.image}: {proc.stderr.strip()}")
        self.container = name

        self._port = self._resolve_port(name)
        self._base = f"http://127.0.0.1:{self._port}"

        self._wait_healthy()

    def _rm_container(self) -> None:
        if self.container:
            self._docker(["rm", "-f", self.container], timeout=60, check=False)
            self.container = None

    def _resolve_port(self, name: str) -> int:
        deadline = time.time() + 30
        last = ""
        while time.time() < deadline:
            p = self._docker(["port", name, "8000/tcp"], timeout=20, check=False)
            out = (p.stdout or "").strip()
            last = out or (p.stderr or "").strip()

            for line in reversed(out.splitlines()):
                line = line.strip()
                if ":" in line:
                    try:
                        return int(line.rsplit(":", 1)[1])
                    except ValueError:
                        continue
            time.sleep(1)
        raise RuntimeError(f"could not resolve published :8000 port for {name}: {last!r}")

    def _wait_healthy(self) -> None:
        deadline = time.time() + self._boot_timeout
        last = ""
        while time.time() < deadline:
            insp = self._docker(["inspect", "-f", "{{.State.Running}}", self.container or ""], timeout=20, check=False)
            if (insp.stdout or "").strip() != "true":
                logs = self._tail_logs()
                raise RuntimeError(f"openclaw container {self.container} exited during boot:\n{logs}")
            try:
                code, data = self._http("GET", "/health", timeout=10)
                if code == 200 and isinstance(data, dict):
                    last = json.dumps(data)
                    if data.get("status") == "healthy" and data.get("openclaw_configured"):
                        return
            except Exception as e:
                last = repr(e)
            time.sleep(2)
        logs = self._tail_logs()
        raise RuntimeError(f"openclaw api-server not healthy within {self._boot_timeout}s (last={last}). logs:\n{logs}")

    def _tail_logs(self, n: int = 60) -> str:
        if not self.container:
            return ""
        p = self._docker(["logs", "--tail", str(n), self.container], timeout=30, check=False)
        return ((p.stdout or "") + (p.stderr or "")).strip()[-4000:]

    def run(self, prompt: str, inject: "list[harness.Skill]", *, out_dir: Path) -> harness.Trajectory:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        self._rm_container()
        self._boot()

        self._write_skills(inject, out_dir)

        session_id = uuid.uuid4().hex
        err: str | None = None
        prompt_resp: dict | str = ""
        history: list = []
        try:
            code, prompt_resp = self._http(
                "POST",
                "/prompt",
                {"session_id": session_id, "prompt": prompt, "create_new_session": False},
                timeout=self._timeout,
            )
            if code != 200:
                err = f"/prompt returned {code}: {str(prompt_resp)[:800]}"
            hc, hist = self._http("GET", f"/sessions/{session_id}/history", timeout=60)
            if hc == 200 and isinstance(hist, dict):
                history = hist.get("history") or []
            elif err is None:
                err = f"/history returned {hc}: {str(hist)[:400]}"
        except Exception as e:
            err = f"openclaw run error: {e!r}"

        raw = self._history_to_raw(history, prompt_resp, session_id)
        if err and not raw["final_text"]:
            raw["_container"] = {"error": err}
        else:
            raw["_container"] = {"error": None}
        if err:
            raw["prompt_error"] = err

        (out_dir / "trajectory_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        return _normalize.normalize(raw)

    def _write_skills(self, inject: "list[harness.Skill]", out_dir: Path) -> None:

        self._docker(
            [
                "exec",
                self.container or "",
                "sh",
                "-c",
                f"rm -rf {_SKILL_ROOT}/* {_SKILL_ROOT}/.[!.]* 2>/dev/null; mkdir -p {_SKILL_ROOT}",
            ],
            timeout=30,
            check=False,
        )

        self._install_defense_skill(out_dir)
        if not inject:
            return
        stage = out_dir / "_skills_stage"
        if stage.exists():
            import shutil

            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        for sk in inject:
            entry = _normalize.skill_to_injected(sk)
            name = (entry.get("name") or "skill").strip() or "skill"
            slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)[:64] or "skill"
            base = slug
            i = 1
            while slug in used:
                i += 1
                slug = f"{base}-{i}"
            used.add(slug)
            sk_dir = stage / slug
            sk_dir.mkdir(parents=True, exist_ok=True)
            (sk_dir / "SKILL.md").write_text(self._skill_md(entry), encoding="utf-8")

            self._docker(["cp", str(sk_dir), f"{self.container}:{_SKILL_ROOT}/{slug}"], timeout=60, check=True)

    def _install_defense_skill(self, out_dir: Path) -> None:
        """Install a third-party runtime defense skill using ITS OWN installer, unmodified.

        Gated on SKILLMISEVO_DEFENSE_SKILL_DIR (e.g. external/secureclaw/secureclaw/skill).
        We stage the skill dir into the container and run its `scripts/install.sh` with
        HOME=/root, which is exactly how a user installs it: it populates
        /root/.openclaw/skills/<tool>/ AND /root/.openclaw/workspace/skills/<tool>/, and
        registers the tool in the workspace TOOLS.md + AGENTS.md. We do not reimplement any
        of that. A failed install invalidates the arm, so it raises rather than degrading
        silently into an unprotected run.
        """
        src = os.environ.get("SKILLMISEVO_DEFENSE_SKILL_DIR", "").strip()
        if not src:
            return
        p = Path(src)
        if not p.is_dir():
            raise RuntimeError(f"SKILLMISEVO_DEFENSE_SKILL_DIR is not a directory: {src}")
        self._docker(["cp", str(p), f"{self.container}:/tmp/_defense_skill"], timeout=120, check=True)
        proc = self._docker(
            [
                "exec",
                self.container or "",
                "sh",
                "-c",
                "cd /tmp/_defense_skill && HOME=/root bash scripts/install.sh 2>&1",
            ],
            timeout=180,
            check=False,
        )
        log = ((proc.stdout or "") + (proc.stderr or "")).strip()
        try:
            (Path(out_dir) / "_defense_skill_install.log").write_text(log, encoding="utf-8")
        except Exception:
            pass
        if proc.returncode != 0:
            raise RuntimeError(f"defense skill installer failed (rc={proc.returncode}) for {src}:\n{log[-2000:]}")

    @staticmethod
    def _skill_md(entry: dict) -> str:

        name = entry.get("name") or "skill"
        desc = entry.get("description") or ""
        body = entry.get("body") or ""
        fm = (
            f"---\nname: {json.dumps(name, ensure_ascii=False)}\n"
            f"description: {json.dumps(desc, ensure_ascii=False)}\n---\n\n"
        )
        return fm + body

    @staticmethod
    def _history_to_raw(history: list, prompt_resp, session_id: str) -> dict:
        assistant_messages: list[dict] = []
        tool_calls: list[dict] = []
        final_text = ""
        for rec in history or []:
            if not isinstance(rec, dict) or rec.get("type") != "message":
                continue
            msg = rec.get("message") or {}
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            texts: list[str] = []
            if isinstance(content, str):
                if content.strip():
                    texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text"):
                        texts.append(b["text"])
                    elif bt in ("toolCall", "tool_call", "tool_use", "toolUse"):
                        tool_calls.append(
                            {
                                "name": b.get("name") or b.get("toolName") or "",
                                "input": b.get("arguments") or b.get("input") or {},
                                "id": b.get("id") or b.get("toolCallId") or "",
                            }
                        )
            atext = "\n".join(t for t in texts if t).strip()
            if atext:
                assistant_messages.append({"role": "assistant", "text": atext})
                final_text = atext

        if not final_text:
            if isinstance(prompt_resp, dict):
                final_text = str(prompt_resp.get("response") or "").strip()
            elif isinstance(prompt_resp, str):
                final_text = prompt_resp.strip()

        return {
            "assistant_messages": assistant_messages,
            "tool_calls": tool_calls,
            "final_text": final_text,
            "session_id": session_id,
            "history": history,
            "prompt_response": prompt_resp,
        }

    def teardown(self) -> None:
        self._rm_container()
