"""Regression cases from the proposal audit: real controller and native SDK."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_improvement import Responses, change, empty, policy, protocol, runtime, splits

from misevolve.improvement.benchmark_pilot import build
from misevolve.improvement.config import load_config
from misevolve.improvement.controller import REFERENCE_POLICY, Controller
from misevolve.improvement.data import SplitManifest
from misevolve.improvement.model import Model
from misevolve.improvement.policy import digest


def controlled_runtime(tmp_path, score):
    rt = runtime(tmp_path)
    calls = []

    def evaluate(library, tasks, **kwargs):
        calls.append((deepcopy(library), deepcopy(tasks), kwargs.copy()))
        risk, status = score(library, kwargs)
        return [{"task_id": t["task"]["instance_id"], "kind": t["task"]["kind"],
                 "sequence": t["sequence"], "pair_id": kwargs["pair_id"], "risk": risk,
                 "utility": True, "status": status} for t in tasks]

    rt.evaluate = evaluate
    return rt, calls


@pytest.mark.parametrize("single_status", ["REJECT", "HOLD"])
def test_failed_single_cannot_be_overridden_by_safe_combination(tmp_path, single_status):
    def score(library, kwargs):
        if len(library["A"]) == 1:
            return (True, "completed") if single_status == "REJECT" else (None, "not_submitted")
        return False, "completed"

    rt, evaluated = controlled_runtime(tmp_path, score)
    responses = Responses()
    responses.result = {"changes": [change(), {**change("second"), "name": "second"}]}
    ctl = Controller(tmp_path / "ctl", policy=policy(), protocol=protocol(max_repairs=0),
                     splits=splits(tmp_path), model=Model(responses, tmp_path / "requests.jsonl"), runtime=rt)
    result, decision, _ = ctl.update(empty(), {"A": [{}]})
    assert result == empty() and decision.status == single_status
    assert not ctl.store.state["spent"]
    assert all(call[2]["purpose"] == "development" for call in evaluated)
    event = ctl.store.get(ctl.store.state["events"][-1])
    assert [d["decision"]["status"] for d in event["development"]] == [single_status, single_status, "ACCEPT"]


def test_repaired_combination_rechecks_each_final_skill(tmp_path):
    def score(library, kwargs):
        texts = [s["native_metadata"]["instructions"] for s in library["A"].values()]
        return any("initial-risk" in text for text in texts) or len(texts) == 1, "completed"

    rt, evaluated = controlled_runtime(tmp_path, score)

    class Repair:
        def generate(self, *args, **kwargs):
            return [change("first", instructions="initial-risk"), {**change("second"), "name": "second"}]

        def repair(self, policy, library, failures, *, agent):
            return [change("first", "edit", {"first": library["first"]["content_hash"]}, "repaired")]

    ctl = Controller(tmp_path / "ctl", policy=policy(), protocol=protocol(), splits=splits(tmp_path),
                     model=Repair(), runtime=rt)
    result, decision, _ = ctl.update(empty(), {"A": [{}]})
    assert result == empty() and decision.status == "REJECT"
    assert not ctl.store.state["spent"]
    assert any("single-repaired" in args["label"] for _, _, args in evaluated)


@pytest.mark.parametrize("action", ["cross_receiver", "successor"])
def test_intervention_replays_failure_action_and_reference_evolution(tmp_path, action):
    rt, evaluated = controlled_runtime(tmp_path, lambda *_: (False, "completed"))
    old = rt.edit(empty(), [change()], agent="A")
    new = rt.edit(old, [change(operation="edit", parents={"synthetic": old["A"]["synthetic"]["content_hash"]},
                               instructions="changed procedure")], agent="A")
    learned, generated = [], []

    def learn(library, tasks, namespace):
        learned.append(deepcopy(library))
        return deepcopy(library), {"A": [{}]}

    rt.learn_block = learn

    class Localizer:
        def localize(self, *args, **kwargs):
            return {"skill_id": "synthetic", "reason": "test hypothesis"}

        def generate(self, theta, *args, **kwargs):
            generated.append(theta.hash)
            return []

    ctl = Controller(tmp_path / "ctl", policy=replace(policy(), val=(action,)),
                     protocol=protocol(successor_enabled=True), splits=splits(tmp_path),
                     model=Localizer(), runtime=rt)
    details = [{"action": action, "target_library": digest(new), "decision": {"status": "REJECT"}}]
    result = ctl._intervene(old, new, details, "A")
    assert result["matched_actions"] == [action]
    assert len(evaluated) == 2
    if action == "cross_receiver":
        assert all(args["action"] == action for _, _, args in evaluated)
        assert not learned
    else:
        assert learned == [new, old]
        assert generated == [REFERENCE_POLICY.hash, REFERENCE_POLICY.hash]
        assert len(result["matched_evidence"][0]["evolution"]) == 2


@pytest.mark.parametrize("changes,reason", [([], "no effective"), ([change(), change("second")], "limit exceeded")])
def test_no_change_and_over_limit_do_not_spend_confirmation(tmp_path, changes, reason):
    rt, evaluated = controlled_runtime(tmp_path, lambda *_: (False, "completed"))
    responses = Responses()
    responses.result = {"changes": changes}
    ctl = Controller(tmp_path / "ctl", policy=policy(), protocol=protocol(max_changes_per_agent=1),
                     splits=splits(tmp_path), model=Model(responses, tmp_path / "requests.jsonl"), runtime=rt)
    result, decision, _ = ctl.update(empty(), {"A": [{}]})
    assert result == empty() and decision.status == "HOLD" and reason in decision.reason
    assert not evaluated and not ctl.store.state["spent"]


def test_real_pilot_preserves_online_tasks_and_separates_source_episodes(tmp_path):
    from misevolve.run import load_episode

    config = build(tmp_path / "plan")
    _, fixed, split, _ = load_config(config, tmp_path / "access.jsonl")
    split.validate_online(load_episode("skillmisevo-coding-01"))
    assert fixed.min_units == 1 and fixed.mode == "empirical"
    assert len(split.value["tasks"]) == 32
    corrupted = deepcopy(split.value)
    row = corrupted["tasks"][-1]
    row["group_id"] = row["episode"]["episode_id"] = "skillmisevo-coding-01"
    with pytest.raises(ValueError, match="crosses purposes"):
        SplitManifest(corrupted, tmp_path / "bad-access.jsonl")


def test_confirmation_source_episode_cannot_be_reused_across_shards(tmp_path):
    config = build(tmp_path / "plan")
    _, _, split, _ = load_config(config, tmp_path / "access.jsonl")
    rows = [r for r in split.value["tasks"] if r["purpose"] == "inner_confirmation"]
    rows[1]["group_id"] = rows[1]["episode"]["episode_id"] = rows[0]["group_id"]
    with pytest.raises(ValueError, match="crosses purposes"):
        SplitManifest(split.value, tmp_path / "bad-access.jsonl")


def test_new_confirmation_family_block_does_not_reuse_prior_tasks(tmp_path):
    prior = build(tmp_path / "prior")
    fresh = build(tmp_path / "fresh", confirmation_episode_start=11)
    old_split = load_config(prior, tmp_path / "old-access.jsonl")[2]
    new_split = load_config(fresh, tmp_path / "new-access.jsonl")[2]
    previous = {r["task"]["instance_id"] for r in old_split.value["tasks"]}
    confirmation = [r for r in new_split.value["tasks"] if r["purpose"] == "inner_confirmation"]
    assert len(confirmation) == 6
    assert not previous & {r["task"]["instance_id"] for r in confirmation}
    assert confirmation[0]["task"]["instance_id"] == "skillmisevo-coding-11-B04"
    assert confirmation[0]["shard"] == "online-1"


@pytest.mark.parametrize("start", [1, 7, 21])
def test_confirmation_family_block_must_be_disjoint_and_present(tmp_path, start):
    with pytest.raises(ValueError, match="confirmation episodes"):
        build(tmp_path / "invalid", confirmation_episode_start=start)


@pytest.mark.parametrize("role", ["generate", "repair"])
def test_bounded_structure_correction_replaces_instead_of_truncating_candidates(tmp_path, role):
    class Sequenced(Responses):
        def request(self, *args, **kwargs):
            self.result = {"changes": [change("first"), change("second")]} if not self.requests else {
                "changes": [change("corrected")]
            }
            return super().request(*args, **kwargs)

    calls = Sequenced()
    model = Model(calls, tmp_path / "requests.jsonl")
    model.configure_protocol(protocol(max_changes_per_agent=1))
    result = getattr(model, role)(policy(), {}, [], agent="A")
    assert [c["skill_id"] for c in result] == ["corrected"]
    assert len(calls.requests) == 2
    request = calls.requests[1][1]
    assert "at most 1 operation" in request["system"]
    data = json.loads(request["messages"][0]["content"])
    assert "received 2, maximum 1" in data["format_correction"]


def test_repeated_oversized_generation_is_bounded_and_never_evaluated(tmp_path):
    calls = Responses()
    calls.result = {"changes": [change("first"), change("second")]}
    rt, evaluated = controlled_runtime(tmp_path, lambda *_: (False, "completed"))
    ctl = Controller(tmp_path / "ctl", policy=policy(), protocol=protocol(max_changes_per_agent=1),
                     splits=splits(tmp_path), model=Model(calls, tmp_path / "requests.jsonl"), runtime=rt)
    result, decision, _ = ctl.update(empty(), {"A": [{}]})
    assert decision.status == "HOLD" and result == empty()
    assert len(calls.requests) == 2 and not evaluated and not ctl.store.state["spent"]
