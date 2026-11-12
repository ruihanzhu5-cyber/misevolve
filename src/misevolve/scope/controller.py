"""Collect → Group → Select → Validate → Localize → Admit.

One cycle per original three-task block. Validation and repair finish before the
single confirmation batch is opened. All decisions retain content-addressed evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from misevolve.multiagent._io import atomic_json, canonical_json

from .models import Budget, BudgetExhausted, GateConfig, ValidationCase, digest, task_fingerprint
from .selection import group_candidates, select_development
from .validation import confirmation_gate, development_status


class ScopeController:
    def __init__(self, store, validator, config: GateConfig, *, reviewer, repairer=None):
        self.store, self.validator, self.config = store, validator, config
        self.reviewer, self.repairer = reviewer, repairer

    def run(self, cycle_id: str, *, base: str, candidates: list[str], cases: list[ValidationCase],
            block_tasks: list[dict]) -> dict:
        self.store._name(cycle_id)
        if len(block_tasks) != 3 or len({t["kind"] for t in block_tasks}) != 1:
            raise ValueError("SCOPE uses one unchanged three-task block per update window")
        positions = [t["pos"] for t in block_tasks]
        if positions[0] not in (1, 4, 7, 10, 13, 16, 19) or positions != list(range(positions[0], positions[0] + 3)):
            raise ValueError("update window does not match original task positions")
        kind = block_tasks[0]["kind"]
        expected_kind = "MMMBBBMMMBBBMMMBBBPPP"[positions[0] - 1]
        if kind != expected_kind:
            raise ValueError("task kind differs from original M/B/P sequence")
        request = {"base": base, "candidates": sorted(set(candidates)), "cases": [c.record() for c in cases],
                   "block_tasks": block_tasks, "config": asdict(self.config),
                   "runtime_configuration": self.validator.configuration_id}
        request = json.loads(canonical_json(request))
        request_id = digest(request)
        path = self.store.root / "cycles" / f"{cycle_id}.json"
        with self.store.lock("cycle"):
            if path.exists():
                prior = json.loads(path.read_text(encoding="utf-8"))
                if prior["request_id"] != request_id:
                    raise ValueError("cycle ID reused with different inputs")
                if prior["phase"] == "pending_publish":
                    self.store.publish(prior["snapshot"], expected_base=base, decision=prior["decision"])
                    prior["phase"] = "completed"
                    atomic_json(path, prior)
                if prior["phase"] == "completed":
                    return prior
                # A running operation may have consumed API calls. Never silently replay it.
                return {**prior, "status": "hold", "reason": "interrupted_cycle_requires_recovery"}
            if self.store.current() != base:
                raise ValueError("cycle base is not the current approved release")
            if kind == "P":
                if candidates:
                    raise ValueError("Probe freezes Library and Memory; candidates are forbidden")
                result = {"phase": "completed", "status": "frozen", "request_id": request_id,
                          "snapshot": base, "pairs_spent": 0, "request": request}
                atomic_json(path, result)
                return result
            self._check_cases(cases, candidates)
            state = {"phase": "running", "request_id": request_id, "request": request,
                     "snapshot": base, "evidence": [], "reviews": [], "groups": [], "repairs": [],
                     "memory": [], "pairs_spent": 0}
            atomic_json(path, state)
            budget = Budget(self.config.total_pairs)
            try:
                result = self._cycle(base, candidates, cases, budget, state, path)
            except BudgetExhausted:
                result = {"status": "hold", "reason": "validation_budget_exhausted", "snapshot": base}
            except Exception as exc:
                result = {"status": "hold", "reason": "governance_error", "error_type": type(exc).__name__,
                          "snapshot": base}
            state.update(result, pairs_spent=budget.spent)
            state["phase"] = "pending_publish" if state["status"] == "accept" else "completed"
            atomic_json(path, state)
            if state["status"] == "accept":
                self.store.publish(state["snapshot"], expected_base=base, decision=state["decision"])
                state["phase"] = "completed"
                atomic_json(path, state)
            return state

    def _check_cases(self, cases, candidates):
        ids = [c.case_id for c in cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate validation case IDs")
        dev = {task_fingerprint(c.task) for c in cases if c.split == "development"}
        sources = {source for cid in candidates for source in self.store.get(cid)["source_tasks"]}
        confirm = [c for c in cases if c.split == "confirmation"]
        consumed = set()
        for path in (self.store.root / "cycles").glob("*.json"):
            prior = json.loads(path.read_text(encoding="utf-8"))
            consumed.update(prior.get("confirmation_opened", []))
        for case in confirm:
            if task_fingerprint(case.task) in dev or case.task.get("instance_id") in sources:
                raise ValueError("confirmation overlaps candidate source/development tasks")
            if {f"unit:{case.unit_id}", f"task:{task_fingerprint(case.task)}"} & consumed:
                raise ValueError("confirmation evidence was already opened in an earlier cycle")
        if len({c.unit_id for c in confirm}) != len(confirm):
            raise ValueError("confirmation samples must use distinct independent units")
        if len({task_fingerprint(c.task) for c in confirm}) != len(confirm):
            raise ValueError("confirmation tasks cannot be renamed duplicates")

    def _validate(self, base, candidate, cases, budget, state, path, purpose):
        evidence = []
        for case in cases:
            oid = self.validator.run(base, candidate, case, budget=budget, purpose=purpose)
            evidence.append(oid)
            state["evidence"].append(oid)
            state["pairs_spent"] = budget.spent
            atomic_json(path, state)
        return evidence

    def _status(self, evidence):
        return development_status([self.store.get(oid) for oid in evidence],
                                  utility_margin=self.config.utility_margin)

    def _cycle(self, base, candidates, cases, budget, state, path):
        approved = self.store.read_snapshot(base)
        groups = group_candidates(self.store, candidates, approved["memory"])
        accepted = []
        held = False
        for group in groups:
            patches = [self.store.get(cid) for cid in group]
            # No confirmation task or benchmark result is sent to the reviewer.
            review = self.reviewer({"stage": "group", "candidates": patches,
                                    "verified_memory": [self.store.get(mid) for mid in approved["memory"]]})
            review_id = self.store.put({"type": "review", "provider": "kimi", "response": review})
            state["reviews"].append(review_id)
            if not isinstance(review, dict) or review.get("status") == "invalid":
                state["groups"].append({"candidates": group, "status": "hold", "reason": "invalid_review"})
                held = True
                continue
            mechanisms = {m for patch in patches for m in patch["mechanisms"]}
            # Reviewer proposals influence coverage only within the allowlisted development pool.
            hints = review.get("mechanisms", []) if isinstance(review, dict) else []
            mechanisms.update(hint for hint in hints if isinstance(hint, str))
            selected = select_development(cases, mechanisms, self.config.development_pairs)
            entry = {"candidates": group, "review": review_id, "selected": [c.case_id for c in selected]}
            state["groups"].append(entry)
            try:
                candidate = self.store.overlay(base, group)
            except ValueError:
                entry.update(status="hold", reason="conflicting_candidates")
                held = True
                continue
            evidence = self._validate(base, candidate, selected, budget, state, path, "group_development")
            status = self._status(evidence)
            entry.update(status=status, evidence=evidence)
            if status == "reject":
                self._localize(base, group, selected, evidence, budget, state, path)
                # Repair is delegated to the native AutoSkill adapter, never text accepted from Kimi.
                for attempt in range(self.config.max_repairs if self.repairer else 0):
                    repair_review = self.reviewer({"stage": "repair", "candidates": patches,
                                                   "validation": [self.store.get(e) for e in evidence]})
                    repair_review_id = self.store.put({"type": "review", "provider": "kimi",
                                                      "response": repair_review})
                    state["reviews"].append(repair_review_id)
                    if not isinstance(repair_review, dict) or repair_review.get("status") == "invalid":
                        break
                    repaired = self.repairer(base=base, candidates=list(group), review=repair_review,
                                             evidence=list(evidence), attempt=attempt)
                    if not repaired or set(repaired) == set(group):
                        break
                    self._check_cases(cases, repaired)
                    candidate = self.store.overlay(base, repaired)
                    evidence = self._validate(base, candidate, selected, budget, state, path, "repair_development")
                    status = self._status(evidence)
                    state["repairs"].append({"parents": group, "candidates": repaired, "evidence": evidence,
                                             "review": repair_review_id, "status": status})
                    if status == "accept":
                        repair_memory = {"type": "verified_memory", "mechanism": "observed_repair",
                                         "skill_ids": [self.store.get(cid)["skill_id"] for cid in repaired],
                                         "parent_candidates": group, "repaired_candidates": repaired,
                                         "evidence": entry["evidence"] + evidence,
                                         "scope": "passed_selected_development_cases_only"}
                        state["memory"].append(self.store.put(repair_memory))
                    group = repaired
                    patches = [self.store.get(cid) for cid in group]
                    if status != "reject":
                        break
            entry["final_status"] = status
            entry["final_candidates"] = group
            if status == "accept":
                accepted.extend(group)
            elif status == "hold":
                held = True
        if not accepted:
            # Verified localization evidence is retained but only becomes active memory in a validated release.
            return {"status": "hold" if held or not candidates else "reject", "reason": "no_admissible_candidates",
                    "snapshot": base}
        memory = approved["memory"] + state["memory"]
        merged = self.store.overlay(base, accepted, memory=memory)
        # Required integration test: individually safe groups can interact unsafely.
        mechanisms = {m for cid in accepted for m in self.store.get(cid)["mechanisms"]}
        selected = select_development(cases, mechanisms, self.config.development_pairs)
        integration = self._validate(base, merged, selected, budget, state, path, "merged_integration")
        if self._status(integration) != "accept":
            if self._status(integration) == "reject":
                self._localize(base, accepted, selected, integration, budget, state, path)
            return {"status": self._status(integration), "reason": "merged_integration_failed", "snapshot": base}
        confirmation = [case for case in cases if case.split == "confirmation"]
        # Confirmation is preselected and never repaired against within this cycle.
        if not confirmation:
            return {"status": "hold", "reason": "missing_independent_confirmation", "snapshot": base}
        state["confirmation_opened"] = [key for case in confirmation
                                        for key in (f"unit:{case.unit_id}", f"task:{task_fingerprint(case.task)}")]
        atomic_json(path, state)
        evidence = self._validate(base, merged, confirmation, budget, state, path, "final_confirmation")
        gate = confirmation_gate([self.store.get(oid) for oid in evidence], self.config)
        state["gate"] = gate
        if gate["status"] != "accept":
            return {"status": gate["status"], "reason": gate["reason"], "snapshot": base}
        decision = self.store.put({"type": "decision", "status": "accept", "base": base,
                                   "snapshot": merged, "candidates": accepted, "gate": gate,
                                   "config": asdict(self.config), "validation": integration + evidence})
        return {"status": "accept", "reason": "validated_release", "snapshot": merged, "decision": decision}

    def _localize(self, base, group, cases, evidence, budget, state, path):
        # Kimi hypotheses cannot become Verified Memory without a controlled observed contrast.
        failures = [(case, oid) for case, oid in zip(cases, evidence, strict=True)
                    if self.store.get(oid)["status"] == "completed"
                    and self.store.get(oid)["arms"]["old"]["assessment"]["risk"] is False
                    and self.store.get(oid)["arms"]["new"]["assessment"]["risk"] is True]
        if not failures:
            return
        case, full_evidence = failures[0]
        review = self.reviewer({"stage": "localize", "candidates": [self.store.get(cid) for cid in group],
                                "validation": [self.store.get(full_evidence)]})
        review_id = self.store.put({"type": "review", "provider": "kimi", "response": review})
        state["reviews"].append(review_id)
        suspects = review.get("suspect_skill_ids", []) if isinstance(review, dict) else []
        suspects = {key for key in suspects if isinstance(key, str)} if isinstance(suspects, list) else set()
        group = sorted(group, key=lambda cid: (self.store.get(cid)["skill_id"] not in suspects, cid))
        localization = []
        remaining = self.config.max_localization_pairs
        # Test singleton and complement; preserve all results, including unknowns.
        subsets = []
        for cid in group:
            for subset in ([cid], [other for other in group if other != cid]):
                if subset and subset != group and subset not in subsets:
                    subsets.append(subset)
        for subset in subsets:
            if case.cost > remaining:
                break
            sid = self.store.overlay(base, subset)
            result = self._validate(base, sid, [case], budget, state, path, "localization")[0]
            localization.append({"candidates": subset, "evidence": result})
            remaining -= case.cost
        record = {"type": "verified_memory", "mechanism": "observed_candidate_introduced_risk",
                  "skill_ids": [self.store.get(cid)["skill_id"] for cid in group],
                  "trigger_case": case.record(), "evidence": [full_evidence] + [r["evidence"] for r in localization],
                  "localization": localization, "hypothesis_review": review_id,
                  "scope": "observed_paired_case_not_universal_causal_claim"}
        state["memory"].append(self.store.put(record))
