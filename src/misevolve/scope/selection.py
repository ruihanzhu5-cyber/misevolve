"""Deterministic grouping and cost-aware development-test coverage."""

from __future__ import annotations

from .models import ValidationCase


def group_candidates(store, candidates: list[str], memory: list[str]) -> list[list[str]]:
    records = {cid: store.get(cid) for cid in sorted(set(candidates))}
    groups = [{cid} for cid in records]
    memory_links = [set(store.get(mid).get("skill_ids", [])) for mid in memory]

    def related(left, right):
        a, b = records[left], records[right]
        keys = {a["skill_id"], b["skill_id"]}
        return (a["skill_id"] == b["skill_id"]
                or b["skill_id"] in a["dependencies"] or a["skill_id"] in b["dependencies"]
                or bool(set(a["mechanisms"]) & set(b["mechanisms"]))
                or any(keys <= link for link in memory_links))

    changed = True
    while changed:
        changed = False
        for i, left in enumerate(groups):
            for j in range(i + 1, len(groups)):
                if any(related(a, b) for a in left for b in groups[j]):
                    groups[i] |= groups.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [sorted(group) for group in groups]


def select_development(cases: list[ValidationCase], mechanisms: set[str], budget: int) -> list[ValidationCase]:
    # Categories make a zero-mechanism candidate still receive source/counter/history coverage.
    weights = {f"mechanism:{m}": 3 for m in mechanisms}
    weights.update({f"category:{c}": 2 for c in ("source", "historical", "counterexample", "fresh")})
    remaining = [case for case in cases if case.split == "development"]
    selected, covered = [], set()
    while remaining:
        def features(case):
            return {f"category:{case.category}", *(f"mechanism:{m}" for m in case.mechanisms)}

        choices = [case for case in remaining if case.cost <= budget]
        if not choices:
            break
        choices.sort(key=lambda case: (
            -sum(weights.get(f, 0) for f in features(case) - covered) / case.cost,
            case.cost, case.case_id,
        ))
        choice = choices[0]
        selected.append(choice)
        covered |= features(choice)
        budget -= choice.cost
        remaining.remove(choice)
    return selected
