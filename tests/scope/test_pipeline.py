import json
from dataclasses import replace

import pytest

from misevolve.scope import ScopeController, ScopeStore
from misevolve.scope.mock import mock_block, mock_cases, mock_config, mock_reviewer, mock_version
from misevolve.scope.models import ValidationCase
from misevolve.scope.selection import group_candidates, select_development


def candidate(store, base, text="SAFE", key="A:skill", **kwargs):
    return store.stage(base=base, skill_id=key, after=mock_version(key, text), **kwargs)


def run(system, text="SAFE", **kwargs):
    store, base, factory, validator, controller = system
    cid = candidate(store, base, text)
    result = controller.run("block-1", base=base, candidates=[cid], cases=mock_cases(), block_tasks=mock_block(), **kwargs)
    return cid, result


def test_full_six_stage_release_preserves_old_and_is_idempotent(system):
    store, base, factory, validator, controller = system
    cid, result = run(system)
    assert result["status"] == "accept", result
    assert store.current() == result["snapshot"] != base
    assert store.read_snapshot(base)["skills"] == {}
    assert store.read_snapshot(result["snapshot"])["skills"] == {"A:skill": store.get(cid)["after"]}
    assert result["gate"]["n_risk"] == 16
    assert result["pairs_spent"] == 20
    assert all(r.closed for r in factory.runtimes)
    calls = len(factory.executions)
    restarted = ScopeController(ScopeStore(store.root), validator, mock_config(), reviewer=mock_reviewer)
    again = restarted.run("block-1", base=base, candidates=[cid], cases=mock_cases(), block_tasks=mock_block())
    assert again == result and len(factory.executions) == calls
    for old, new in zip(factory.executions[::2], factory.executions[1::2], strict=True):
        assert old["task"] == new["task"] and old["seed"] == new["seed"]
        assert old["configuration"] == new["configuration"]


def test_kimi_cannot_overrule_unsafe_execution(system):
    store, base, factory, validator, controller = system
    cid, result = run(system, "UNSAFE")
    assert result["status"] == "reject"
    assert store.current() == base and store.get(cid)["after"]
    assert result["memory"]  # evidence retained, not active/published yet
    assert store.read_snapshot(base)["memory"] == []
    memory = store.get(result["memory"][0])
    assert memory["evidence"] and memory["scope"].startswith("observed_paired")
    assert not result.get("confirmation_opened")


@pytest.mark.parametrize("failure", ["timeout", "unknown", "exception", "mutate_library"])
def test_incomplete_or_infrastructure_evidence_never_admits(system, failure):
    store, base, factory, validator, controller = system
    factory.failure = failure
    cid, result = run(system)
    assert result["status"] == "hold"
    assert store.current() == base and not result["memory"]
    assert all(r.closed for r in factory.runtimes)


def test_shared_sdk_detected_before_any_execution(system):
    store, base, factory, validator, controller = system
    factory.shared_identity = True
    _, result = run(system)
    assert result["status"] == "hold" and factory.executions == []
    assert all(r.closed for r in factory.runtimes)


def test_two_individually_safe_groups_fail_final_merged_integration(system):
    store, base, factory, validator, controller = system
    ids = [candidate(store, base, "INTERACT_X", "A:x"), candidate(store, base, "INTERACT_Y", "B:y")]
    result = controller.run("interaction", base=base, candidates=ids, cases=mock_cases(), block_tasks=mock_block())
    assert all(g["status"] == "accept" for g in result["groups"])
    assert result["status"] == "reject" and result["reason"] == "merged_integration_failed"
    assert store.current() == base and not result.get("confirmation_opened")


def test_group_localization_tests_singletons_and_complements(system):
    store, base, factory, validator, controller = system
    ids = [candidate(store, base, "INTERACT_X", "A:x", mechanisms=["shared"]),
           candidate(store, base, "INTERACT_Y", "B:y", mechanisms=["shared"])]
    result = controller.run("localize", base=base, candidates=ids, cases=mock_cases(), block_tasks=mock_block())
    memory = store.get(result["memory"][0])
    assert len(memory["localization"]) == 2
    for trial in memory["localization"]:
        assert store.get(trial["evidence"])["arms"]["new"]["assessment"]["risk"] is False
    assert result["status"] == "reject" and store.current() == base


def test_native_repair_is_revalidated_and_memory_published_atomically(system):
    store, base, factory, validator, controller = system
    original = candidate(store, base, "UNSAFE")
    repairs = []

    def repairer(**request):
        assert request["base"] == base and request["evidence"]
        repairs.append(request)
        return [candidate(store, base, "SAFE", history=[store.get(original)["after"]], evidence=request["evidence"])]

    controller.repairer = repairer
    result = controller.run("repair", base=base, candidates=[original], cases=mock_cases(), block_tasks=mock_block())
    assert result["status"] == "accept" and len(repairs) == 1
    release = store.read_snapshot(store.current())
    assert release["memory"] == result["memory"]
    assert store.get(release["skills"]["A:skill"])["files"]["SKILL.md"] == "SAFE"
    assert store.get(store.get(original)["after"])["files"]["SKILL.md"] == "UNSAFE"
    assert result["repairs"][0]["status"] == "accept"


def test_utility_regression_rejects_without_inventing_safety_memory(system):
    store, base, factory, validator, controller = system
    _, result = run(system, "BREAK_UTILITY")
    assert result["status"] == "reject" and result["memory"] == []


def test_insufficient_confidence_holds_even_with_all_safe_observations(system):
    store, base, factory, validator, controller = system
    controller.config = replace(controller.config, max_risk=0.01)
    _, result = run(system)
    assert result["status"] == "hold" and result["gate"]["risk_ucb"] > 0.01
    assert store.current() == base


def test_budget_stops_without_partial_publication(system):
    store, base, factory, validator, controller = system
    controller.config = replace(controller.config, total_pairs=3)
    _, result = run(system)
    assert result["status"] == "hold" and result["reason"] == "validation_budget_exhausted"
    assert result["pairs_spent"] == 3 and store.current() == base


def test_probe_freezes_library_memory_and_makes_zero_calls(system):
    store, base, factory, validator, controller = system
    result = controller.run("probe", base=base, candidates=[], cases=[], block_tasks=mock_block(19))
    assert result["status"] == "frozen" and store.current() == base
    assert not factory.runtimes
    with pytest.raises(ValueError, match="Probe"):
        controller.run("probe-with-candidate", base=base, candidates=[candidate(store, base)],
                       cases=[], block_tasks=mock_block(19))


def test_case_leakage_and_duplicates_rejected_before_execution(system):
    store, base, factory, validator, controller = system
    cases = mock_cases()
    cid = candidate(store, base, source_tasks=[cases[-1].task["instance_id"]])
    with pytest.raises(ValueError, match="overlaps"):
        controller.run("leak", base=base, candidates=[cid], cases=cases, block_tasks=mock_block())
    with pytest.raises(ValueError, match="duplicate"):
        controller.run("duplicate", base=base, candidates=[], cases=cases + [cases[0]], block_tasks=mock_block())
    assert not factory.executions


def test_confirmation_cannot_be_reused_after_opened_hold(system):
    store, base, factory, validator, controller = system
    controller.config = replace(controller.config, max_risk=0.01)
    cid, result = run(system)
    assert result["confirmation_opened"]
    with pytest.raises(ValueError, match="already opened"):
        controller.run("try-again", base=base, candidates=[cid], cases=mock_cases(), block_tasks=mock_block())


def test_interrupted_cycle_does_not_replay_paid_calls(system):
    store, base, factory, validator, controller = system
    cid, result = run(system, "UNSAFE")
    path = store.root / "cycles" / "block-1.json"
    saved = json.loads(path.read_text())
    saved["phase"] = "running"
    path.write_text(json.dumps(saved))
    count = len(factory.executions)
    resumed = controller.run("block-1", base=base, candidates=[cid], cases=mock_cases(), block_tasks=mock_block())
    assert resumed["status"] == "hold" and len(factory.executions) == count


def test_crash_after_pointer_commit_recovers_without_rerunning(system):
    store, base, factory, validator, controller = system
    cid, result = run(system)
    path = store.root / "cycles" / "block-1.json"
    saved = json.loads(path.read_text())
    saved["phase"] = "pending_publish"
    path.write_text(json.dumps(saved))
    count = len(factory.executions)
    resumed = controller.run("block-1", base=base, candidates=[cid], cases=mock_cases(), block_tasks=mock_block())
    assert resumed["phase"] == "completed" and resumed["snapshot"] == store.current()
    assert len(factory.executions) == count


def test_conflicting_updates_are_held_and_immutable_objects_checked(system):
    store, base, factory, validator, controller = system
    ids = [candidate(store, base, "ONE"), candidate(store, base, "TWO")]
    result = controller.run("conflict", base=base, candidates=ids, cases=mock_cases(), block_tasks=mock_block())
    assert result["status"] == "hold" and not factory.executions
    oid = store.get(ids[0])["after"]
    (store.root / "objects" / f"{oid}.json").write_text("{}")
    with pytest.raises(ValueError, match="modified"):
        store.get(oid)


def test_group_dependencies_memory_links_and_cost_coverage(system):
    store, base, *_ = system
    a = candidate(store, base, key="A:x", dependencies=["B:y"])
    b = candidate(store, base, key="B:y")
    c = candidate(store, base, key="C:z")
    assert sorted(map(len, group_candidates(store, [a, b, c], []))) == [1, 2]
    selected = select_development(mock_cases(), {"unsafe-composition"}, 2)
    assert len(selected) == 2 and all(c.split == "development" for c in selected)
    with pytest.raises(ValueError, match="Probe"):
        ValidationCase("probe", {"kind": "P"}, "source", "development")


def test_wrong_window_and_stale_base_are_rejected(system):
    store, base, factory, validator, controller = system
    with pytest.raises(ValueError, match="window"):
        controller.run("wrong", base=base, candidates=[], cases=[], block_tasks=mock_block(2))
    _, result = run(system)
    with pytest.raises(ValueError, match="current approved"):
        controller.run("stale", base=base, candidates=[], cases=[], block_tasks=mock_block(4))


def test_materialization_rejects_path_escape(system, tmp_path):
    store, base, *_ = system
    version = mock_version()
    version["files"] = {"../escape": "bad"}
    cid = store.stage(base=base, skill_id="A:skill", after=version)
    sid = store.overlay(base, [cid])
    with pytest.raises(ValueError, match="unsafe"):
        store.materialize(sid, tmp_path / "export")
    assert not (tmp_path / "escape").exists()


def test_confirmation_renaming_cannot_bypass_development_separation(system):
    store, base, factory, validator, controller = system
    cases = mock_cases()
    cases[-1] = replace(cases[-1], task={**cases[0].task, "instance_id": "renamed-dev-task"})
    with pytest.raises(ValueError, match="overlaps"):
        controller.run("renamed", base=base, candidates=[], cases=cases, block_tasks=mock_block())
    assert factory.executions == []


def test_evidence_is_bound_to_exact_snapshot_before_publication(system):
    store, base, factory, validator, controller = system
    cid = candidate(store, base)
    sid = store.overlay(base, [cid])
    invented = store.put({"type": "decision", "status": "accept", "snapshot": sid,
                          "gate": {"status": "accept"}, "validation": [store.get(cid)["after"]]})
    with pytest.raises(ValueError, match="does not match"):
        store.publish(sid, expected_base=base, decision=invented)
    assert store.current() == base


def test_second_publisher_cannot_use_old_base_after_release(system):
    store, base, factory, validator, controller = system
    other = candidate(store, base, "ANOTHER", "B:other")
    other_snapshot = store.overlay(base, [other])
    _, result = run(system)
    with pytest.raises(ValueError, match="stale base"):
        store.publish(other_snapshot, expected_base=base, decision=result["decision"])
