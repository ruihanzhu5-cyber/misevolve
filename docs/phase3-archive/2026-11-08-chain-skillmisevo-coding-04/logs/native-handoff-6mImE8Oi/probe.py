import hashlib
import json
import subprocess
import sys
from pathlib import Path

from misevolve._stage7_openclaw import BACKBONE
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.backbones import resolve_agent
from misevolve.multiagent._io import atomic_json, read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.team.executor import TeamWorkerPool
from misevolve.team.handoff import ArtifactRejected, deliver_artifact
from misevolve.team.openclaw import isolated_openclaw_factory
from misevolve.team.router import ChainRouter, TeamEventLogger, TeamMessage
from misevolve.team.skills import SkillTransferService

root = Path(sys.argv[1])
def msg(sender, receiver, kind, **payload):
    return TeamMessage("acceptance", "synthetic-handoff", sender, receiver, kind, payload)

if sys.argv[2] == "store":
    path = root / "native"
    factory = lambda harness, backbone: OpenClawRunner(resolve_agent(harness, backbone))
    pool = TeamWorkerPool(path, runner_factory=factory, backbone=BACKBONE)
    workers = pool.setup_workers()
    log = TeamEventLogger(path / "team_events.jsonl", "acceptance", "chain")
    router = ChainRouter(log)
    registry = GlobalSkillRegistry(path / "registry")
    try:
        a = workers["A"].method
        source = a.sdk.upsert(user_id=a._UID, skill_id="synthetic-native-probe",
                              name="synthetic-arithmetic", description="Verify synthetic arithmetic result",
                              instructions="Check the sum of two synthetic integers.")
        artifact = registry.register_skill("A", a.export_skill_bundle(source.id))
        service = SkillTransferService(registry, workers, router)
        assert service.publish(msg("A", "C", "SKILL_PAYLOAD", local_skill_id=source.id)) is None
        b = service.publish(msg("A", "B", "SKILL_PAYLOAD", local_skill_id=source.id))
        request = msg("B", "C", "SKILL_FORWARD", local_skill_id=b.payload["local_skill_id"])
        c = service.publish(request)
        assert c.message_type == "SKILL_FORWARD"
        identity = service.identity(artifact)
        assert all(b.payload[k] == c.payload[k] == v for k, v in identity.items())
        assert identity["source_agent"] == "A" and identity["skill_version"] == source.version
        assert not registry.get_skills_by_owner("B") and not registry.get_skills_by_owner("C")
    finally:
        pool.teardown()
    restarted = TeamWorkerPool(path, runner_factory=factory, backbone=BACKBONE, resume=True)
    try:
        workers = restarted.setup_workers()
        registry = GlobalSkillRegistry(path / "registry")
        service = SkillTransferService(registry, workers, router)
        def forbidden(*args, **kwargs):
            raise AssertionError("repeat invoked native import")
        workers["C"].method.import_external_skill = forbidden
        assert service.publish(request).payload == c.payload
        hops = [e for e in registry.get_transfer_history() if registry.verified_transfer(e)]
        assert [(e["source_agent"], e["target_agent"]) for e in hops] == [("A", "B"), ("B", "C")]
        assert all(e["native_retrieval_verified"] and e["integrity_verified"] for e in hops)
        assert hops[1]["forwarded_from_attempt_id"] == hops[0]["attempt_id"]
        assert sum(e["event_type"] == "SKILL_IMPORTED" for e in read_jsonl(log.path)) == 2
        atomic_json(root / "store_result.json", {"passed": True, "identity": identity, "hops": hops,
                                                "B_local_id": b.payload["local_skill_id"],
                                                "C_local_id": c.payload["local_skill_id"],
                                                "replayed_without_native_import": True})
        print("Native Store A->B->C and restart replay passed")
    finally:
        restarted.teardown()
else:
    router = ChainRouter(TeamEventLogger(root / "binary_events.jsonl", "acceptance", "chain"))
    data = bytes(range(256)) * 4 + b"\0\xffMisevolve"
    source = "synthetic-binary.bin"
    workspace = "/root/.openclaw/workspace/"
    usage = {"bytes": 0, "files": 0}
    with isolated_openclaw_factory(object()) as make:
        runners = {}
        for agent in "BC":
            runner = make("openclaw", BACKBONE)
            runner.isolate(root / ("binary-" + agent))
            runner.open_task_session(task_run_id="binary-01", task_id="binary-01", agent_id=agent, out_dir=root / agent)
            runners[agent] = runner
        b, c = runners["B"], runners["C"]
        subprocess.run(["docker", "exec", "-i", b.container, "python3", "-c",
                        "import sys;open(sys.argv[1],'wb').write(sys.stdin.buffer.read())", workspace + source],
                       input=data, check=True, timeout=20)
        subprocess.run(["docker", "exec", b.container, "ln", "-s", source, workspace + "alias.bin"],
                       check=True, timeout=20)
        assert deliver_artifact(router, msg("B","C","ARTIFACT_HANDOFF",relative_path="alias.bin"), runners, usage) is None
        assert deliver_artifact(router, msg("B","C","ARTIFACT_HANDOFF",relative_path="../"+source), runners, usage) is None
        assert deliver_artifact(router, msg("C","B","ARTIFACT_HANDOFF",relative_path=source), runners, usage) is None
        try:
            c.read_artifact(source)
        except ArtifactRejected:
            pass
        else:
            raise AssertionError("receiver already had source file")
        result = deliver_artifact(router, msg("B","C","ARTIFACT_HANDOFF",relative_path=source), runners, usage)
        assert result.payload["sha256"] == hashlib.sha256(data).hexdigest()
        assert c.read_artifact(result.payload["relative_path"]) == data
        atomic_json(root / "binary_result.json", {"passed": True, "receipt": result.payload,
                                                 "sender": "B", "receiver": "C", "usage": usage})
    print("Binary handoff and denied path/edge checks passed")
