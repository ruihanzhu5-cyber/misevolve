"""HermesRunner — hermes-agent (the Hermes CLI) inside the misevolve-hermes
image, driven headless via ``hermes -z`` on a fixed backbone (MiniMax-M2.7 over
the minimax-cn Anthropic-compatible endpoint). Structurally analogous to
ClaudeCodeRunner: a per-task ephemeral container (docker run --rm -i), the spec
piped on stdin, the trajectory read back from /harness/trajectory.json.

NATIVE-MEMORY POLICY: this is the runner every NON-hermes-native method uses, so
hermes' OWN skill/memory evolution (curator / learning / memory) is OFF —
``native_memory_off = True``. The container is fresh per task (HERMES_HOME is a
fresh dir by construction), memory + skill-creation nudges are disabled in the
in-container config, and every run is a NEW oneshot (no --continue/--resume), so
the ONLY cross-task channel is the method re-injecting its accumulated skill
store each run. (The Hermes-native METHOD adapter, built separately, flips
hermes' evolution back on and IS the thing under test — this runner's default
is OFF.)

PERSISTENT-HERMES_HOME mode (Hermes-native ONLY; opt-in, backward-compatible):
``use_persistent_home(home_dir)`` switches this runner from the default per-task
ephemeral HERMES_HOME to a single episode-scoped HOST dir bind-mounted as the
container's HERMES_HOME (``-v <home_dir>:/root/.hermes``). Because the SAME host
dir is mounted into every per-task ``--rm`` container, hermes' own ``skills/`` +
memory + sessions ACCUMULATE across the episode's tasks, and this mode forces
``HERMES_NATIVE_EVOLUTION=on`` so hermes' native curator/learning/skill authoring
is enabled. The default path (no ``use_persistent_home`` call) is UNCHANGED —
ephemeral + native-off — so every other method still gets a fresh, isolated
HERMES_HOME per task. (The in-container runner never clears the skills dir and
only writes what it is handed as ``injected_skills``, so an empty inject leaves
the accumulated native store intact.)

DRIVE PATH (in-container, keyed off ``HERMES_NATIVE_EVOLUTION``): the DEFAULT
(native-off) still drives ``hermes -z`` as a subprocess and reads back the
on-disk session snapshot. PERSISTENT-HERMES_HOME (native-on, Hermes-native ONLY)
instead runs the turn IN-PROCESS via hermes' own oneshot agent builder and then
JOINS hermes' ``bg-review`` daemon thread, so BOTH native authoring channels
land in the persistent store: explicit in-session ``skill_manage`` AND the
PASSIVE background-review distillation (which ``hermes -z`` spawns but exits
before it can write). We pass ``HERMES_SKILL_NUDGE_INTERVAL`` (default 1) so the
passive skill review fires on our short tasks, and ``HERMES_BG_REVIEW_TIMEOUT``
(default 300s) to bound how long we wait for the daemon to write.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from misevolve import ahz_sandbox, harness

from . import _normalize


class HermesRunner(harness.AgentRunner):
    native_memory_off = True

    _CONTAINER_HERMES_HOME = "/root/.hermes"

    def __init__(self, cfg: harness.AgentConfig):
        super().__init__(cfg)
        self._auth = ""
        self._episode_dir: Path | None = None
        self._timeout = int(os.environ.get("SKILLMISEVO_TASK_TIMEOUT", "1200"))

        self._persistent_home: Path | None = None

    def use_persistent_home(self, home_dir: Path) -> None:
        """Opt into PERSISTENT-HERMES_HOME mode (Hermes-native method ONLY).

        Bind-mounts ``home_dir`` (a stable HOST dir) as the container's
        HERMES_HOME on every subsequent ``run`` so hermes' own skills/ + memory +
        sessions ACCUMULATE across the episode's per-task ``--rm`` containers, and
        flips ``HERMES_NATIVE_EVOLUTION=on``. Idempotent; call once in the
        method's setup(), AFTER isolate(). Leaves the ephemeral default untouched
        for every other method (which never calls this)."""
        self._persistent_home = Path(home_dir)
        (self._persistent_home / "skills").mkdir(parents=True, exist_ok=True)
        self.native_memory_off = False

    def isolate(self, episode_dir: Path) -> None:

        self._episode_dir = Path(episode_dir)
        self._auth = _normalize.read_backbone_key(self.cfg)
        if not self._auth:
            raise RuntimeError(f"missing backbone credential ${self.cfg.key_env}")

    def run(self, prompt: str, inject: "list[harness.Skill]", *, out_dir: Path) -> harness.Trajectory:
        spec: dict = {"model": self.cfg.backbone, "decomposed_query": [prompt]}
        if inject:
            spec["injected_skills"] = [_normalize.skill_to_injected(s) for s in inject]

        persistent = self._persistent_home is not None
        env = {
            "HERMES_PROVIDER_TRANSPORT": "openai_chat",
            "HERMES_INFERENCE_BASE_URL": self.cfg.base_url,
            "OPENAI_API_KEY": self._auth,
            "HERMES_INFERENCE_MODEL": self.cfg.backbone,
            "HERMES_INCONTAINER_TIMEOUT": str(max(60, self._timeout - 90)),
            "HERMES_NATIVE_EVOLUTION": "on" if persistent else os.environ.get("HERMES_NATIVE_EVOLUTION", "off"),
        }
        if persistent:
            env["HERMES_SKILL_NUDGE_INTERVAL"] = os.environ.get("HERMES_SKILL_NUDGE_INTERVAL", "1")
            env["HERMES_BG_REVIEW_TIMEOUT"] = os.environ.get("HERMES_BG_REVIEW_TIMEOUT", "300")
        spec_json = json.dumps(spec, ensure_ascii=False)
        if persistent:
            raw = self._run_in_docker_persistent(spec_json, env)
        else:
            raw = ahz_sandbox.run_in_docker(self.cfg.image, spec_json, env, timeout_seconds=self._timeout)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "trajectory_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        return _normalize.normalize(raw)

    def _run_in_docker_persistent(self, spec_json: str, env: dict) -> dict:
        """Like ahz_sandbox.run_in_docker but ALSO bind-mounts the episode HOST
        HERMES_HOME (``self._persistent_home``) as the container's HERMES_HOME so
        native skills/memory ACCUMULATE across the episode's per-task containers.
        ahz_sandbox.run_in_docker exposes no host-HOME volume, so this mirrors its
        --tmpfs /work + -v <harness>:/harness + trajectory.json readback and adds
        the -v <home>:/root/.hermes mount. Same spec-on-stdin contract."""
        home = self._persistent_home
        home.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="ahz_hermes_") as tmp:
            harness_dir = Path(tmp) / "harness"
            harness_dir.mkdir()
            cmd = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--cpus",
                "2.0",
                "--memory",
                "4g",
                "--tmpfs",
                "/work:rw,size=2g,mode=1777",
                "-v",
                f"{harness_dir}:/harness",
                "-v",
                f"{home}:{self._CONTAINER_HERMES_HOME}",
            ]
            for key, value in env.items():
                cmd += ["-e", f"{key}={value}"]
            cmd += [self.cfg.image]
            try:
                proc = subprocess.run(cmd, input=spec_json, capture_output=True, text=True, timeout=self._timeout)
            except subprocess.TimeoutExpired as te:
                se = te.stderr
                if isinstance(se, (bytes, bytearray)):
                    se = se.decode("utf-8", "replace")
                return {
                    "_container": {
                        "rc": -1,
                        "stderr_tail": (se or "")[-4000:],
                        "error": "container timed out",
                        "image": self.cfg.image,
                        "duration_seconds": time.time() - t0,
                    }
                }
            except Exception as e:
                return {
                    "_container": {
                        "rc": -1,
                        "stderr_tail": "",
                        "error": f"docker error: {e}",
                        "image": self.cfg.image,
                        "duration_seconds": time.time() - t0,
                    }
                }

            traj_path = harness_dir / "trajectory.json"
            if not traj_path.exists():
                return {
                    "_container": {
                        "rc": proc.returncode,
                        "stderr_tail": (proc.stderr or "")[-2000:],
                        "error": "container exited without writing trajectory.json",
                        "image": self.cfg.image,
                        "duration_seconds": time.time() - t0,
                    }
                }
            parsed = json.loads(traj_path.read_text())
            parsed["_container"] = {
                "rc": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-2000:],
                "image": self.cfg.image,
                "duration_seconds": time.time() - t0,
                "error": None,
            }
            return parsed

    def teardown(self) -> None:
        pass
