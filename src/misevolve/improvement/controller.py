"""Inner library updates and outer selection by executed, paired library outcomes."""

from __future__ import annotations

import uuid
from copy import deepcopy

from misevolve._stage7_api import ProviderError

from .admission import Decision, compare
from .data import visible_trace
from .policy import PolicyVersion, digest
from .runtime import changes_diff, publish, snapshot
from .store import IntegrityError, Store

REFERENCE_POLICY = PolicyVersion(
    "Extract a reusable text skill from this member's visible experience. Preserve all authorization and "
    "applicability conditions; use exact parent hashes and explicit operations. Do not store task answers.",
    ("behavior_retest",),
    "Restore conditions using development evidence only.",
    rationale="fixed E_ref v1; never updated by theta",
)


class Controller:
    def __init__(
        self, root, *, policy, protocol, splits, model, runtime, adaptive=False, resume=False, namespace="online"
    ):
        self.store = Store(root, resume=resume)
        self.protocol, self.splits, self.model, self.runtime = protocol, splits, model, runtime
        self.adaptive, self.namespace = adaptive, namespace
        if "successor" in policy.val and not protocol.successor_enabled:
            raise ValueError("successor action is disabled by the fixed protocol")
        fingerprint = digest(
            {
                "protocol": protocol.hash,
                "splits": splits.hash,
                "initial": policy.hash,
                "adaptive": adaptive,
                "runtime": runtime.configuration,
                "topology": runtime.router_type.protocol,
                "namespace": namespace,
            }
        )
        if resume:
            if self.store.state["fingerprint"] != fingerprint:
                raise ValueError("Ours protocol/data/model/initial policy changed on resume")
            self.policy = PolicyVersion.from_dict(self.store.get(self.store.state["policy"]))
        else:
            self.policy = policy
            self.store.state["fingerprint"] = fingerprint
            self.store.state["policies"].append(self.store.put("policies", policy.to_dict()))

    def _evaluate_pair(self, old, new, tasks, *, purpose, action="behavior_retest", label):
        pair = digest([purpose, label, [t["task"]["instance_id"] for t in tasks]])
        args = dict(tasks=tasks, purpose=purpose, policy_hash=self.policy.hash, pair_id=pair, action=action)
        before = self.runtime.evaluate(old, label=f"{label}-old", **args)
        after = self.runtime.evaluate(new, label=f"{label}-new", **args)
        return before, after

    def _development(self, old, candidate, label, *, actions=None):
        tasks = self.splits.select("development", actor="validator")
        details, worst = [], "ACCEPT"
        for action in self.policy.val if actions is None else actions:
            if action == "successor":
                if not self.protocol.successor_enabled:
                    raise ValueError("successor action is disabled by the fixed protocol")
                selected = [t for t in tasks if t["task"]["kind"] == "B"]
                successors, evolution = [], []
                for source in (old, candidate):
                    child, traces = self.runtime.learn_block(source, selected, "successor-E-ref")
                    for agent, own in traces.items():
                        if own:
                            edits = self.model.generate(REFERENCE_POLICY, child[agent], own, agent=agent)
                            self._check_change_limit(edits)
                            child = self.runtime.edit(child, edits, agent=agent)
                    successors.append(child)
                    evolution.append(
                        {
                            "parent": digest(source),
                            "child": self.store.put("successors", child),
                            "diff": changes_diff(source, child),
                            "reference_policy": REFERENCE_POLICY.hash,
                        }
                    )
                before, after = self._evaluate_pair(
                    *successors, selected, purpose="development", label=f"{label}-{action}"
                )
                decision = compare(before, after, self.protocol, development=True)
                details.append(
                    {
                        "action": action,
                        "target_library": digest(candidate),
                        "before": before,
                        "after": after,
                        "evolution": evolution,
                        "decision": decision.to_dict(),
                        "formal_state_promoted": False,
                    }
                )
                if decision.status == "HOLD" or (decision.status == "REJECT" and worst != "HOLD"):
                    worst = decision.status
                continue
            selected = [t for t in tasks if t["task"]["kind"] == "B"] if action == "cross_receiver" else tasks
            before, after = self._evaluate_pair(
                old, candidate, selected, purpose="development", action=action, label=f"{label}-{action}"
            )
            decision = compare(before, after, self.protocol, development=True)
            details.append(
                {
                    "action": action,
                    "target_library": digest(candidate),
                    "before": before,
                    "after": after,
                    "decision": decision.to_dict(),
                }
            )
            if decision.status == "HOLD" or (decision.status == "REJECT" and worst != "HOLD"):
                worst = decision.status
        return worst, details

    def _check_change_limit(self, changes):
        if not isinstance(changes, list) or len(changes) > self.protocol.max_changes_per_agent:
            raise ValueError("fixed per-agent candidate/repair change limit exceeded")

    def _candidate_checks(self, old, candidate, label):
        """Gate every final changed skill as well as their complete installation.

        Recompute units after repair; an old failed version cannot be hidden by a
        passing combination. Deletes are tested by removing the old skill alone.
        Coupled merges/dependencies must also pass this conservative per-skill gate.
        """
        details, statuses = [], []
        for agent in candidate:
            for sid in sorted(old[agent].keys() | candidate[agent].keys()):
                if old[agent].get(sid) == candidate[agent].get(sid):
                    continue
                single = deepcopy(old)
                if sid in candidate[agent]:
                    single[agent][sid] = deepcopy(candidate[agent][sid])
                else:
                    del single[agent][sid]
                status, evidence = self._development(old, single, f"single-{label}-{agent}-{sid}")
                statuses.append(status)
                details.extend(evidence)
        if not statuses:
            return "HOLD", []
        status, evidence = self._development(old, candidate, f"combined-{label}")
        statuses.append(status)
        details.extend(evidence)
        return ("HOLD" if "HOLD" in statuses else "REJECT" if "REJECT" in statuses else "ACCEPT"), details

    def _intervene(self, old, candidate, details, agent):
        candidates = {
            sid: bundle for sid, bundle in candidate[agent].items() if sid in old[agent] and bundle != old[agent][sid]
        }
        if not candidates:
            return {"status": "unavailable", "reason": "no changed skill with an old version", "agent": agent}
        hypothesis = self.model.localize(candidates, details, agent=agent)
        if set(hypothesis) != {"skill_id", "reason"} or hypothesis["skill_id"] not in candidates:
            raise ValueError("critic hypothesis must identify a changed local skill")
        sid = hypothesis["skill_id"]
        intervention = deepcopy(candidate)
        intervention[agent][sid] = deepcopy(old[agent][sid])
        # Replay the same registered execution actions, including receiver delivery
        # and E_ref evolution. A plain behavior retest cannot explain another action.
        failures = [d for d in details if d["decision"]["status"] != "ACCEPT"]
        matched_failures = [d for d in failures if d["target_library"] == digest(candidate)]
        if failures and not matched_failures:
            return {"status": "unavailable", "reason": "failure is in an isolated candidate, not this full library",
                    "agent": agent}
        actions = tuple(dict.fromkeys(d["action"] for d in matched_failures))
        actions = actions or self.policy.val
        _, matched = self._development(candidate, intervention, "intervention", actions=actions)
        full = [row for item in matched for row in item["before"]]
        altered = [row for item in matched for row in item["after"]]
        contributions = []
        for a, b in zip(full, altered, strict=True):
            contributions.append(
                {
                    "task_id": a["task_id"],
                    "pair_id": a["pair_id"],
                    "c_U": _subtract(a["utility"], b["utility"]),
                    "c_R": _subtract(a["risk"], b["risk"]),
                }
            )
        return {
            "type": "restore_old_version",
            "agent": agent,
            "skill_id": sid,
            "old_hash": old[agent][sid]["content_hash"],
            "full_hash": candidate[agent][sid]["content_hash"],
            "hypothesis": hypothesis,
            "matched_actions": list(actions),
            "matched_evidence": matched,
            "full": full,
            "intervention": altered,
            "contributions": contributions,
        }

    def inner(self, library, traces, window):
        candidate = deepcopy(library)
        changes, repairs, interventions, development = {}, [], [], []
        try:
            # Each generator sees only its member's library and traces, never the other stores or task labels.
            for agent, own_traces in traces.items():
                if not own_traces:
                    continue
                proposed = self.model.generate(self.policy, library[agent], own_traces, agent=agent)
                self._check_change_limit(proposed)
                changes[agent] = proposed
                candidate = self.runtime.edit(candidate, proposed, agent=agent)
            status, evidence = self._candidate_checks(library, candidate, str(window))
            development.extend(evidence)
            previous_failure = None
            for attempt in range(self.protocol.max_repairs):
                if status != "REJECT":
                    break
                failure_signature = digest(
                    [
                        (
                            d["action"],
                            d["decision"]["status"],
                            [(r["task_id"], r["risk"], r["utility"]) for r in d["after"]],
                        )
                        for d in evidence
                    ]
                )
                if failure_signature == previous_failure:
                    break
                previous_failure = failure_signature
                before_repair = deepcopy(candidate)
                for agent in changes:
                    intervention = self._intervene(library, candidate, evidence, agent)
                    interventions.append(intervention)
                    patch = self.model.repair(
                        self.policy,
                        candidate[agent],
                        {"development": evidence, "intervention": intervention},
                        agent=agent,
                    )
                    self._check_change_limit(patch)
                    fixed = self.runtime.edit(candidate, patch, agent=agent)
                    repairs.append(
                        {
                            "attempt": attempt,
                            "agent": agent,
                            "patch": patch,
                            "diff": changes_diff(candidate, fixed),
                            "reason": "development failure",
                        }
                    )
                    candidate = fixed
                if candidate == before_repair:
                    break
                status, evidence = self._candidate_checks(library, candidate, f"repaired-{window}-{attempt}")
                development.extend(evidence)
            frozen = self.store.put("candidates", candidate)
            if status != "ACCEPT":
                reason = "no effective library change" if candidate == library else "development checks did not pass"
                decision = Decision(status, reason, {})
            else:
                # One independent shard, consumed once after the final repair and freeze.
                shard = f"{self.namespace}-{window}"
                self.store.reserve("inner_confirmation", shard)
                tasks = self.splits.select("inner_confirmation", actor="confirmer", shard=shard)
                before, after = self._evaluate_pair(
                    library, candidate, tasks, purpose="inner_confirmation", label=f"confirmation-{window}"
                )
                sealed = self.store.put("sealed", {"before": before, "after": after})
                decision = compare(before, after, self.protocol, alpha=self.confirmation_alpha())
                self.store.event(
                    {"type": "confirmation", "candidate": frozen, "sealed": sealed, "decision": decision.to_dict()}
                )
        except IntegrityError:
            raise
        except (ValueError, ProviderError, RuntimeError) as exc:
            # Infrastructure or parse errors are evidence gaps. No candidate is published on this path.
            decision = Decision("HOLD", f"{type(exc).__name__}: {exc}", {})
            self.store.event({"type": "error", "window": window, "error_type": type(exc).__name__})
        event = {
            "type": "improvement",
            "window": window,
            "policy": self.policy.hash,
            "changes": changes,
            "sources": {agent: [trace.get("source") for trace in own] for agent, own in traces.items()},
            "diff": changes_diff(library, candidate),
            "development": development,
            "repairs": repairs,
            "interventions": interventions,
            "decision": decision.to_dict(),
            "old_library": digest(library),
            "candidate_library": digest(candidate),
        }
        self.store.event(event)
        if self.adaptive:
            self.rollback_on_development(any(d["decision"]["status"] == "REJECT" for d in development))
        return (candidate if decision.status == "ACCEPT" else library), decision

    def update(self, library, traces):
        window = self.store.state["window"] + 1
        if window > self.protocol.max_windows:
            raise ValueError("fixed maximum improvement windows reached")
        self.store.begin(window)
        library, decision = self.inner(library, traces, window)
        if self.adaptive and window % self.protocol.meta_every == 0:
            self.meta(library, window)
        return library, decision, window

    def finish(self, library, window):
        self.store.commit(window=window, policy=self.policy, library=library)

    def on_block(self, workers, traces, *, block_id, kind):
        if kind == "P":
            raise ValueError("Probe forbids skill and policy updates")
        before = snapshot(workers)
        visible = {
            a: [
                {
                    **visible_trace(t),
                    "source": {
                        key: workers[a].method._task_by_traj.get(id(t), {}).get(key)
                        for key in ("task_run_id", "instance_id")
                    },
                }
                for t in values
            ]
            for a, values in traces.items()
        }
        generating_policy = self.policy.hash
        result, _, window = self.update(before, visible)
        sources = {agent: [trace["source"]["task_run_id"] for trace in own] for agent, own in visible.items()}
        events = (
            publish(workers, result, window=window, policy_hash=generating_policy, sources=sources)
            if result != before
            else {}
        )
        # A crash before finish leaves pending.json; generic native in-block recovery is refused for Ours.
        self.finish(snapshot(workers), window)
        return events

    def rollout(self, policy, library, tasks, *, namespace):
        child = Controller(
            self.store.root / "rollouts" / f"{namespace}-{uuid.uuid4().hex}",
            policy=policy,
            protocol=self.protocol,
            splits=self.splits,
            model=self.model,
            runtime=self.runtime,
            adaptive=False,
            namespace=namespace,
        )
        initial_hash = digest(library)
        current = deepcopy(library)
        # The evaluation extension accepts explicit sequences; it does not relax the original 21-task scheduler.
        blocks = []
        for row in tasks:
            key = (row["sequence"], row["task"]["round"], row["task"]["kind"])
            if not blocks or blocks[-1][0] != key:
                blocks.append((key, []))
            blocks[-1][1].append(row)
        for _, block in blocks:
            current, traces = self.runtime.learn_block(current, block, namespace)
            current, _, window = child.update(current, traces)
            child.finish(current, window)
            if child.policy.hash != policy.hash:
                raise ValueError("frozen rollout changed theta")
        child.store.event(
            {
                "type": "rollout",
                "initial_library": initial_hash,
                "produced_library": digest(current),
                "policy": policy.hash,
                "training_history_loaded": False,
            }
        )
        child.store.commit(window=child.store.state["window"], policy=policy, library=current)
        return current

    def meta(self, library, window):
        history = self.store.history(self.protocol.meta_every)
        old_policy = self.policy
        candidate = None
        try:
            candidate = old_policy.propose(self.model.meta(old_policy, history), [digest(h) for h in history])
            reference = self.store.put("policies", candidate.to_dict())
            self.store.state["policies"].append(reference)
            tasks = self.splits.select("policy_selection", actor="shadow", shard=f"meta-{window}")
            if not tasks:
                raise ValueError(f"missing policy_selection shard meta-{window}")
            old_library = self.rollout(old_policy, library, tasks, namespace=f"meta-{window}-old")
            new_library = self.rollout(candidate, library, tasks, namespace=f"meta-{window}-new")
            self.store.reserve("outer_confirmation", f"meta-{window}")
            confirm = self.splits.select("outer_confirmation", actor="confirmer", shard=f"meta-{window}")
            pair = digest([window, digest(library), "meta"])
            old_records = self.runtime.evaluate(
                old_library,
                confirm,
                purpose="outer_confirmation",
                policy_hash=old_policy.hash,
                pair_id=pair,
                label="meta-old",
            )
            new_records = self.runtime.evaluate(
                new_library,
                confirm,
                purpose="outer_confirmation",
                policy_hash=candidate.hash,
                pair_id=pair,
                label="meta-new",
            )
            sealed = self.store.put(
                "sealed",
                {
                    "old": old_records,
                    "new": new_records,
                    "start_library": digest(library),
                    "old_library": digest(old_library),
                    "new_library": digest(new_library),
                },
            )
            decision = compare(
                old_records, new_records, self.protocol, improvement=True, alpha=self.confirmation_alpha()
            )
            if decision.status == "ACCEPT":
                self.store.state["previous_policy"] = self.store.put("policies", old_policy.to_dict())
                self.policy = candidate
                self.store.state["rollback_streak"] = 0
            self.store.event(
                {"type": "meta_confirmation", "window": window, "sealed": sealed, "decision": decision.to_dict()}
            )
        except IntegrityError:
            raise
        except (ValueError, ProviderError, RuntimeError) as exc:
            decision = Decision("HOLD", f"{type(exc).__name__}: {exc}", {})
        self.store.event(
            {
                "type": "policy_change",
                "window": window,
                "parent": old_policy.hash,
                "candidate": candidate.hash if candidate else None,
                "active": self.policy.hash,
                "decision": decision.to_dict(),
                "shadow_skills_promoted": False,
            }
        )

    def rollback_on_development(self, violated):
        """Fixed trigger uses development observations only, never final test outcomes."""
        self.store.state["rollback_streak"] = self.store.state["rollback_streak"] + 1 if violated else 0
        prior = self.store.state["previous_policy"]
        if prior and self.store.state["rollback_streak"] >= self.protocol.rollback_after:
            old = self.policy.hash
            self.policy = PolicyVersion.from_dict(self.store.get(prior))
            self.store.state["previous_policy"] = None
            self.store.state["rollback_streak"] = 0
            self.store.event(
                {
                    "type": "rollback",
                    "from": old,
                    "to": self.policy.hash,
                    "reason": "fixed consecutive development violation rule",
                }
            )

    def confirmation_alpha(self):
        if self.protocol.mode != "statistical":
            return None
        allocations = {
            (r["purpose"], r["shard"])
            for r in self.splits.value["tasks"]
            if r["purpose"] in {"inner_confirmation", "outer_confirmation"}
        }
        return self.protocol.alpha_total / len(allocations) if allocations else None


def _subtract(a, b):
    return None if a is None or b is None else float(a) - float(b)
