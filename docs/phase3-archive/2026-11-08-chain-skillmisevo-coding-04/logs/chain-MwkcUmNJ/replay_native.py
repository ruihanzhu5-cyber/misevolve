import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from misevolve._stage7_api import INPUT_BYTES, ProviderError
from misevolve._stage7_openclaw import BACKBONE
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.backbones import resolve_agent
from misevolve.harness import Trajectory
from misevolve.multiagent._io import atomic_json, canonical_json
from misevolve.stage7 import _trace_native
from misevolve.team.executor import AgentRuntime, TeamWorkerPool

d = Path(sys.argv[1])
source = d / "episode"
audit = []
class OfflineCalls:
    def request(self, scope, module, payload):
        size = len(json.dumps(payload,ensure_ascii=False).encode())
        audit.append({"module":module,"bytes":size,"local_limit_bytes":INPUT_BYTES,"would_reject":size>INPUT_BYTES,
                      "task_run_id":scope.task_id})
        if size > INPUT_BYTES:
            raise ProviderError("input exceeds Stage 7 request limit", category="input_limit")
        return b'{"content":[{"type":"text","text":"{\\"skills\\":[]}"}],"stop_reason":"end_turn"}',"application/json"
pool = TeamWorkerPool(d/"offline_native_replay", backbone=BACKBONE,
                      runner_factory=lambda *args: OpenClawRunner(resolve_agent(*args)))
w = pool.setup_workers()["A"]
try:
    for r in json.loads((source/"all_rows.json").read_text()):
        if r["pos"] not in (1,3):
            continue
        tid = r["task_run_id"]
        p = source/"tasks"/f'{r["pos"]:02}-{tid}'/"team_trajectory.json"
        data = json.loads(p.read_text())
        turns = data["raw"]["turns"]
        runtime = AgentRuntime(w,w.runner,d/f'fake-{r["pos"]}',tid+":A")
        runtime.inputs = [t["input"] for t in turns]
        runtime.traces = [Trajectory(t["messages"],t["final_text"],t["files_changed"],None,t["trajectory"]) for t in turns]
        info = {"pos": r["pos"], "kind": "M", "round": r["round"]}
        trace = runtime.learning_trace(info)
        from misevolve._stage7_api import Scope
        _trace_native(w,OfflineCalls(),Scope(tid+":A","A"),d/"offline_native_replay"/"responses.jsonl")
        w.method.evolve_batch([trace], [], kind="M", round_idx=r["round"])
        event = w.method._last_evolution_events[-1]
        audit[-1].update(task=r["orig_id"], ingest_success=event["success"], returned=len(event["returned_skills"]))
finally:
    pool.teardown()
atomic_json(d/"offline_native_replay.json",audit)
print(json.dumps(audit, ensure_ascii=False))
