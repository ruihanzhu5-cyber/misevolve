"""Phase 3 acceptance with offline sessions and the pinned native AutoSkill SDK."""

from __future__ import annotations

import json
import subprocess
import uuid
from itertools import product

import pytest

from misevolve import harness
from misevolve.adapters.openclaw_runner import OpenClawRunner
from misevolve.backbones import resolve_agent
from misevolve.multiagent._io import read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.run import EPISODES_JSON
from misevolve.team import (
    ChainRouter,
    HierarchicalRouter,
    PeerToPeerRouter,
    SkillTransferService,
    TeamEpisodeScheduler,
    TeamEventLogger,
    TeamExecutor,
    TeamMessage,
    TeamWorkerPool,
)
from misevolve.team.openclaw import TeamOpenClawRunner

PROTOCOLS = (HierarchicalRouter, ChainRouter, PeerToPeerRouter)
EDGES = (
    {("A", "B"), ("A", "C"), ("B", "A"), ("C", "A")},
    {("A", "B"), ("B", "C")},
    {("A", "B"), ("A", "C"), ("B", "A"), ("B", "C"), ("C", "A"), ("C", "B")},
)


def make_router(root, router_type, episode_id="episode"):
    return router_type(TeamEventLogger(root / "team_events.jsonl", episode_id, router_type.protocol))


@pytest.mark.parametrize(("router_type", "edges"), list(zip(PROTOCOLS, EDGES, strict=True)))
def test_all_message_and_skill_edges(tmp_path, router_type, edges):
    router = make_router(tmp_path, router_type)
    for sender, receiver in product("ABC", repeat=2):
        for kind in ("RESULT", "SKILL_PAYLOAD", "SKILL_FORWARD"):
            payload = {"text": "result"} if kind == "RESULT" else {
                "skill_id": "id", "skill_version": "1", "parent_version": None,
                "source_agent": "A", "content_hash": "hash",
            }
            message = TeamMessage("episode", "task", sender, receiver, kind, payload)
            routed = router.route(message)
            assert routed.allowed == ((sender, receiver) in edges)
            if routed.allowed:
                router.delivered(routed)
            else:
                with pytest.raises(ValueError, match="blocked"):
                    router.delivered(routed)
    events = read_jsonl(router.log.path)
    assert len(events) == 54
    assert sum(e["event_type"] == "MESSAGE_BLOCKED" for e in events) == (9 - len(edges)) * 3
    assert not router.can_send("outside", "A", "RESULT")
    assert not router.can_send("A", "B", "UNDECLARED")


def test_native_forward_and_replay_after_pool_restart(tmp_path, runner_factory, seed_skill, monkeypatch):
    pool = TeamWorkerPool(tmp_path, runner_factory=runner_factory)
    workers = pool.setup_workers()
    registry = GlobalSkillRegistry(tmp_path / "registry")
    router = make_router(tmp_path, ChainRouter)
    service = SkillTransferService(registry, workers, router)
    source = seed_skill(workers["A"])
    source_artifact = registry.register_skill("A", workers["A"].method.export_skill_bundle(source.id))
    seed_skill(workers["A"], skill_id=source.id, instructions="Verify synthetic arithmetic using a new revision.")
    artifact = registry.register_skill(
        "A", workers["A"].method.export_skill_bundle(source.id),
        provenance={
            "native_revision_parent_id": source_artifact.global_skill_id,
            "parent_evidence": {
                source_artifact.global_skill_id: {"type": "native_revision", "source_ref": "synthetic:revision"},
            },
        },
    )

    def publish(sender, receiver, local_id):
        return TeamMessage("episode", "task", sender, receiver, "SKILL_PAYLOAD", {"local_skill_id": local_id})

    try:
        assert service.publish(publish("A", "C", source.id)) is None
        assert not workers["C"].method.sdk.list(user_id=workers["C"].method._UID)
        to_b = service.publish(publish("A", "B", source.id))
        request = publish("B", "C", to_b.payload["local_skill_id"])
        to_c = service.publish(request)
        assert to_c.message_type == "SKILL_FORWARD"
        for key in ("skill_id", "skill_version", "parent_version", "source_agent", "content_hash"):
            assert to_c.payload[key] == to_b.payload[key]
        assert to_c.payload["parent_version"] == source_artifact.global_skill_id
        assert to_c.payload["source_agent"] == "A"
        assert to_c.payload["skill_id"] == artifact.global_skill_id
        assert to_c.payload["content_hash"] == artifact.content_hash
        hops = [e for e in registry.get_transfer_history() if registry.verified_transfer(e)]
        assert [(e["source_agent"], e["target_agent"]) for e in hops] == [("A", "B"), ("B", "C")]
        assert hops[1]["forwarded_from_attempt_id"] == hops[0]["attempt_id"]
        assert not registry.get_skills_by_owner("B") and not registry.get_skills_by_owner("C")
        assert service.publish(publish("B", "C", to_b.payload["local_skill_id"]), frozen=True) is None
    finally:
        pool.teardown()

    restored = TeamWorkerPool(tmp_path, runner_factory=runner_factory, resume=True)
    workers = restored.setup_workers()
    try:
        registry = GlobalSkillRegistry(tmp_path / "registry")
        service = SkillTransferService(registry, workers, router)

        def forbidden(*args, **kwargs):
            raise AssertionError("confirmed import must not be called again")

        monkeypatch.setattr(workers["C"].method, "import_external_skill", forbidden)
        assert service.publish(request).payload == to_c.payload
        assert len([e for e in registry.get_transfer_history() if registry.verified_transfer(e)]) == 2
        audit = read_jsonl(router.log.path)
        assert sum(e["event_type"] == "SKILL_IMPORTED" for e in audit) == 2
        assert sum(e["event_type"] == "SKILL_TRANSFER_REPLAYED" for e in audit) == 1
    finally:
        restored.teardown()


def action(receiver, kind, **payload):
    return {"receiver_id": receiver, "message_type": kind, "payload": payload}


class ScriptedRunner(OpenClawRunner):
    def __init__(self, protocol):
        super().__init__(resolve_agent("openclaw", None))
        self.protocol = protocol
        self.active = None
        self.sessions = []
        self.closed = []
        self.received = []
        self.fail_agent = None
        self.fail_open = False

    def open_task_session(self, *, task_run_id, task_id, agent_id, out_dir):
        assert self.active is None
        self.active = (task_run_id, task_id, agent_id, uuid.uuid4().hex)
        self.sessions.append(self.active)
        if self.fail_open and self.fail_agent == agent_id:
            raise RuntimeError("synthetic open failure")

    def close_task_session(self):
        if self.active:
            self.closed.append(self.active)
            self.active = None

    def teardown(self):
        self.close_task_session()

    def continue_task_session(self, prompt, inject, *, out_dir):
        assert self.active is not None
        _, task_id, agent, session = self.active
        if self.fail_agent == agent and not self.fail_open:
            raise RuntimeError("synthetic turn failure")
        self.received.append((task_id, session, prompt, [sk.key for sk in inject]))
        actions = []
        final = None
        if "Your visible input: " not in prompt:
            assert agent == "A"
            text = "Synthetic isolated exposure result."
        else:
            visible = json.loads(prompt.split("Your visible input: ", 1)[1].split("\nLocal Skill IDs", 1)[0])
            messages = visible["messages"]
            if agent == "A" and "task" in visible:
                actions.append(action("B", "SUBTASK", text="Verify synthetic arithmetic."))
                if self.protocol != "chain":
                    actions.append(action("C", "SUBTASK", text="Verify synthetic arithmetic."))
                if inject:
                    actions.append(action("B", "SKILL_PAYLOAD", local_skill_id=inject[0].key))
            elif agent == "A" and self.protocol == "hierarchical":
                actions.append(action("C", "RESULT", text="Synthetic final decision."))
            elif agent == "B":
                target = "A" if self.protocol == "hierarchical" else "C"
                actions.append(action(target, "RESULT", text="Synthetic arithmetic verified."))
                imported = next((m for m in messages if m["message_type"] == "SKILL_PAYLOAD"), None)
                if imported and target == "C":
                    actions.append(action("C", "SKILL_FORWARD", local_skill_id=imported["payload"]["local_skill_id"]))
            elif agent == "C" and self.protocol == "hierarchical" and not any(
                m["message_type"] == "RESULT" and m["sender_id"] == "A" for m in messages
            ):
                actions.append(action("A", "FEEDBACK", text="Synthetic verification feedback."))
            else:
                final = "Synthetic team answer."
            text = json.dumps({"actions": actions, "final_answer": final})
        raw = {
            "session_id": session,
            "_skill_injection": {"status": "success", "skills": [
                {"local_skill_id": sk.key, "runtime_skill_name": sk.key, "runtime_directory_slug": sk.key,
                 "runtime_content_hash": "0" * 64} for sk in inject
            ]},
            "tool_calls": [{"name": "read", "id": f"{session}:{len(self.received)}", "input": {"path": "mock"}}],
        }
        if agent in ("B", "C") and inject:
            raw["_skill_invocations"] = [{"local_skill_id": inject[0].key,
                                          "source_ref": raw["tool_calls"][0]["id"]}]
        return harness.Trajectory(
            [{"role": "tool", "content": f"PRIVATE_TOOL_RESULT_{agent}"}], final_text=text, raw=raw,
        )


def coding_episode():
    episodes = json.loads(EPISODES_JSON.read_text(encoding="utf-8"))["episodes"]
    return next(ep for ep in episodes if ep["episode_id"] == "skillmisevo-coding-04")


def test_chain_connects_native_import_injection_and_explicit_invocation(tmp_path, seed_skill):
    pool = TeamWorkerPool(tmp_path, runner_factory=lambda *_: ScriptedRunner("chain"))
    workers = pool.setup_workers()
    try:
        registry = GlobalSkillRegistry(tmp_path / "registry")
        skill = seed_skill(workers["A"])
        artifact = registry.register_skill("A", workers["A"].method.export_skill_bundle(skill.id))
        router = make_router(tmp_path, ChainRouter)
        executor = TeamExecutor(workers, router, SkillTransferService(registry, workers, router))
        result = executor.run(
            {"kind": "B", "pos": 4, "instance_id": "task", "prompt": "Verify synthetic arithmetic."},
            task_run_id="test", out_dir=tmp_path / "task",
        )
        assert result.submitter == "C"
        events = [e for e in read_jsonl(router.log.path) if e["skill_id"] == artifact.global_skill_id]
        lifecycle = [(e["event_type"], e["agent_id"]) for e in events]
        assert ("SKILL_INVOKED", "B") in lifecycle and ("SKILL_INVOKED", "C") in lifecycle
        assert lifecycle.index(("SKILL_INVOKED", "B")) < lifecycle.index(("SKILL_FORWARDED", "B"))
        assert lifecycle.index(("SKILL_FORWARDED", "B")) < lifecycle.index(("SKILL_INVOKED", "C"))
        assert not registry.get_skills_by_owner("B") and not registry.get_skills_by_owner("C")
    finally:
        pool.teardown()


@pytest.mark.parametrize("router_type", PROTOCOLS)
def test_complete_episode_same_scheduler_and_native_learning_isolation(
    tmp_path, router_type, synthetic_llm, method_factory, monkeypatch,
):
    episode = coding_episode()
    runners = []
    methods = []
    learned = []
    judged = []

    def runner_factory(*_):
        runner = ScriptedRunner(router_type.protocol)
        runners.append(runner)
        return runner

    def native_factory(*args, **kwargs):
        method = method_factory(*args, **kwargs)
        methods.append(method)
        original = method.evolve_batch

        def evolve(traces, outcomes, **context):
            if not hasattr(method, "_synthetic_configured"):
                synthetic_llm(method)
                method._synthetic_configured = True
            learned.append(("ABC"[methods.index(method)], context["kind"], list(traces)))
            return original(traces, outcomes, **context)

        monkeypatch.setattr(method, "evolve_batch", evolve)
        return method

    def judge(trace, _goal, _prompt, kind):
        judged.append((trace.final_text, kind))
        return harness.TaskOutcome(False, kind != "M")

    pool = TeamWorkerPool(tmp_path, runner_factory=runner_factory, method_factory=native_factory)
    rows = TeamEpisodeScheduler(pool, router_type, judge=judge).run(episode)
    task_rows = [r for r in rows if r["pos"] is not None]
    assert len(task_rows) == len(judged) == 21
    assert "".join(r["task_kind"] for r in task_rows) == "MMMBBBMMMBBBMMMBBBPPP"
    assert [r["orig_id"] for r in task_rows] == [
        t["instance_id"] for t in sorted(episode["task_sequence"], key=lambda t: t["pos"])
    ]
    assert all(r["runner_success"] and r["agent"] == ("A" if r["task_kind"] == "M" else "C") for r in task_rows)
    assert [len(r.sessions) for r in runners] == [21, 12, 12]
    assert len({s[3] for r in runners for s in r.sessions}) == 45
    assert all(r.closed == r.sessions for r in runners)
    assert len({method._store_dir for method in methods}) == 3
    assert len({id(method.sdk) for method in methods}) == 3
    assert [(agent, kind) for agent, kind, _ in learned] == [("A", "M"), ("A", "B"), ("B", "B"), ("C", "B")] * 3
    for agent, _, traces in learned:
        assert len(traces) == 3
        combined = json.dumps([t.messages for t in traces])
        assert f"PRIVATE_TOOL_RESULT_{agent}" in combined
        assert all(f"PRIVATE_TOOL_RESULT_{other}" not in combined for other in "ABC" if other != agent)
    malicious = [task["prompt"] for task in episode["task_sequence"] if task["kind"] == "M"]
    assert all(prompt not in seen for runner in runners[1:] for *_, seen, _ in runner.received for prompt in malicious)
    events = read_jsonl(tmp_path / "team_events.jsonl")
    assert sum(e["event_type"] == "FINAL_ANSWER" and e["agent_id"] == "C" for e in events) == 12
    assert sum(e["event_type"] == "SKILL_STORE_FROZEN" and e["after_pos"] == 18 for e in events) == 3
    probes = {e["task_id"] for e in events if e["event_type"] == "PROBE_EXECUTED" and e["stores_unchanged"]}
    assert len(probes) == 3
    assert all(e["event_type"] not in {"SKILL_IMPORTED", "SKILL_CREATED", "SKILL_UPDATED", "NATIVE_MAINTENANCE"}
               for e in events if e["task_id"] in probes)
    assert any(e["event_type"] == "SKILL_RETRIEVED" and e["task_id"] in probes for e in events)
    assert all((e["sender_id"], e["receiver_id"]) in router_type.edges
               for e in events if e["event_type"] == "MESSAGE_DELIVERED")
    sent = {e["message_id"]: e["payload_hash"] for e in events if e["event_type"] == "MESSAGE_SENT"}
    assert all(sent[e["message_id"]] == e["payload_hash"]
               for e in events if e["event_type"] in {"MESSAGE_DELIVERED", "MESSAGE_CONSUMED"})
    if router_type is HierarchicalRouter:
        for runner in (runners[0], runners[2]):
            assert any(sum(row[1] == session for row in runner.received) > 1 for _, _, _, session in runner.sessions)


@pytest.mark.parametrize("fail_open", [True, False])
def test_failure_closes_all_open_sessions(tmp_path, fail_open):
    runners = []

    def factory(*_):
        runner = ScriptedRunner("chain")
        runner.fail_agent = "B"
        runner.fail_open = fail_open
        runners.append(runner)
        return runner

    pool = TeamWorkerPool(tmp_path, runner_factory=factory)
    workers = pool.setup_workers()
    try:
        registry = GlobalSkillRegistry(tmp_path / "registry")
        router = make_router(tmp_path, ChainRouter)
        executor = TeamExecutor(workers, router, SkillTransferService(registry, workers, router))
        task = {"instance_id": "task", "kind": "B", "pos": 4, "prompt": "Synthetic arithmetic."}
        with pytest.raises(RuntimeError, match="synthetic .* failure"):
            executor.run(task, task_run_id="test", out_dir=tmp_path / "task")
        assert all(r.closed == r.sessions for r in runners)
        assert read_jsonl(router.log.path)[-1]["event_type"] == "TASK_EXECUTION_FAILED"
    finally:
        pool.teardown()


def test_openclaw_adapter_continues_same_session_without_reboot(tmp_path, monkeypatch):
    from types import SimpleNamespace

    relay = SimpleNamespace(server_address=("127.0.0.1", 0), token="mock")
    runner = TeamOpenClawRunner("synthetic-network", relay)
    boots = []
    bodies = []
    history = []
    monkeypatch.setattr(runner, "_docker", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, '[{"Containers":{}}]', "",
    ))

    def boot():
        boots.append(relay.token)
        history.clear()
        runner.container = "synthetic-container"

    def http(method, path, body=None, **kwargs):
        if method == "POST":
            bodies.append(body)
            history.append({"type": "message", "message": {"role": "assistant", "content": body["prompt"]}})
            return 200, {"response": body["prompt"]}
        return 200, {"history": list(history)}

    monkeypatch.setattr(runner, "_boot", boot)
    monkeypatch.setattr(runner, "_http", http)
    monkeypatch.setattr(runner, "_write_skills", lambda *args: {"status": "success", "skills": []})
    runner.open_task_session(task_run_id="r1", task_id="t1", agent_id="A", out_dir=tmp_path)
    first = runner.continue_task_session("one", [], out_dir=tmp_path / "one")
    second = runner.continue_task_session("two", [], out_dir=tmp_path / "two")
    assert len(boots) == 1
    assert bodies[0]["session_id"] == bodies[1]["session_id"]
    assert first.messages == [{"role": "assistant", "text": "one"}]
    assert second.messages == [{"role": "assistant", "text": "two"}]
    runner.close_task_session()
    runner.open_task_session(task_run_id="r2", task_id="t2", agent_id="A", out_dir=tmp_path)
    third = runner.continue_task_session("three", [], out_dir=tmp_path / "three")
    assert third.raw["session_id"] != first.raw["session_id"]
    assert len(set(boots)) == 2
    runner.close_task_session()
