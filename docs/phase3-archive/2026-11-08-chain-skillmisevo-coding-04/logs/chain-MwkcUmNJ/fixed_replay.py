import json
import sys
from pathlib import Path
from unittest.mock import patch

from misevolve._stage7_api import INPUT_BYTES
from misevolve._stage7_openclaw import BACKBONE
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.backbones import resolve_agent
from misevolve.harness import Trajectory
from misevolve.multiagent._io import atomic_json, read_jsonl
from misevolve.team.checkpoint import load as load_checkpoint
from misevolve.team.cli import EpisodeCalls, _configure_native
from misevolve.team.executor import AgentRuntime, TeamWorkerPool

d = Path(sys.argv[1])
root = d / "offline_fixed_replay"
original = d / "episode"
old = json.loads((d/"offline_native_replay.json").read_text())
try:
    load_checkpoint(original, json.loads((original/"team_manifest.json").read_text()))
except ValueError:
    print("Verified interrupted original run is not a resumable checkpoint")
else:
    raise AssertionError("Unexpectedly accepted unsafe resume")
calls = EpisodeCalls(root, deepseek_peak=True)
audit = []
class Response:
    status=200
    headers={"Content-Type":"application/json","request-id":"offline-stub"}
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return None
    def read(self, _):
        return json.dumps({"model":"MiniMax-M3.1-Flash","content":[{"type":"text","text":'{"skills":[]}'}],
                           "stop_reason":"end_turn","usage":{"input_tokens":0,"output_tokens":0,
                           "cache_read_input_tokens":0,"cache_creation_input_tokens":0}}).encode()
class OfflineOpener:
    def open(self, request, **_):
        body = request.data
        spec = json.loads(body)
        assert spec["model"]=="MiniMax-M3.1-Flash"
        audit.append({"bytes":len(body),"body_unchanged_from_original_attempt":len(body)==old[len(audit)]["bytes"],
                      "offline_provider_stub":True})
        assert INPUT_BYTES < len(body) <= calls.input_limit("extractor")
        return Response()
pool = TeamWorkerPool(root, backbone=BACKBONE,runner_factory=lambda *args:OpenClawRunner(resolve_agent(*args)))
workers = pool.setup_workers()
w = workers["A"]
try:
    _configure_native(workers,calls)
    with patch("misevolve._stage7_api.urllib.request.build_opener",lambda *_:OfflineOpener()):
        for r in json.loads((original/"all_rows.json").read_text()):
            if r["pos"] not in (1,3): continue
            tid = r["task_run_id"]
            p = original/"tasks"/f'{r["pos"]:02}-{tid}'/"team_trajectory.json"
            turns = json.loads(p.read_text())["raw"]["turns"]
            runtime = AgentRuntime(w,w.runner,root/f'fake-{r["pos"]}',tid+":A")
            runtime.inputs = [t["input"] for t in turns]
            runtime.traces = [Trajectory(t["messages"],t["final_text"],t["files_changed"],None,t["trajectory"]) for t in turns]
            trace = runtime.learning_trace({"instance_id":r["orig_id"],"pos":r["pos"],"kind":"M","round":r["round"]})
            w.method.evolve_batch([trace],[],kind="M",round_idx=r["round"])
            assert w.method._last_evolution_events[-1]["success"]
            audit[-1]["task"] = r["orig_id"]
    assert len(audit)==len(read_jsonl(calls.path))==2 and all(a["body_unchanged_from_original_attempt"] for a in audit)
finally:
    pool.teardown()
atomic_json(d/"offline_fixed_replay.json",{"requests":audit,"real_requests":0,"original_run_resumable":False})
print(json.dumps(audit))
