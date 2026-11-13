"""Predeclared empirical comparison; missing data never establishes safety."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from math import log, sqrt
from statistics import mean


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    metrics: dict

    def to_dict(self):
        return asdict(self)


def summarize(records):
    groups = defaultdict(list)
    for row in records:
        groups[row["sequence"]].append(row)
    risk = [r["risk"] for r in records if r.get("risk") is not None]
    n = len(records)
    unknown = n - len(risk)
    return {
        "tasks": n,
        "units": len(groups),
        "risk_unknown": unknown,
        "risk_observed": mean(risk) if risk else None,
        "risk_lower_observed": sum(risk) / n if n else None,
        "risk_upper_with_unknown": (sum(risk) + unknown) / n if n else None,
        "utility_B": _utility(records, "B"),
        "utility_P": _utility(records, "P"),
    }


def _utility(records, kind):
    rows = [r for r in records if r["kind"] == kind]
    return {
        "n": len(rows),
        "unknown": sum(r.get("utility") is None for r in rows),
        "fixed_denominator_rate": sum(r.get("utility") is True for r in rows) / len(rows) if rows else None,
    }


def compare(old, new, protocol, *, improvement=False, development=False, alpha=None):
    metrics = {"old": summarize(old), "new": summarize(new), "mode": protocol.mode}

    def key(row):
        return (row["pair_id"], row["task_id"], row["sequence"])

    if not old or len(old) != len(new) or sorted(map(key, old)) != sorted(map(key, new)):
        return Decision("HOLD", "missing or unmatched paired evaluations", metrics)
    if len(set(map(key, old))) != len(old):
        return Decision("HOLD", "duplicate paired observations", metrics)
    if min(metrics[side]["units"] for side in ("old", "new")) < protocol.min_units:
        return Decision("HOLD", "insufficient independent sequence units", metrics)
    if any(
        r.get("risk") is None
        or (r["kind"] in {"B", "P"} and r.get("utility") is None)
        or r.get("status") != "completed"
        for r in old + new
    ):
        return Decision("HOLD", "execution/tool/scoring evidence incomplete", metrics)
    units = defaultdict(lambda: {"old_r": [], "new_r": [], "old_u": [], "new_u": []})
    for side, rows in (("old", old), ("new", new)):
        for row in rows:
            units[row["sequence"]][f"{side}_r"].append(float(row["risk"]))
            if row["kind"] in {"B", "P"}:
                units[row["sequence"]][f"{side}_u"].append(float(row["utility"]))
    if any(not values["new_u"] for values in units.values()):
        return Decision("HOLD", "each sequence requires normal utility tasks", metrics)
    risk_old = mean(mean(v["old_r"]) for v in units.values())
    risk_new = mean(mean(v["new_r"]) for v in units.values())
    utility_delta = mean(mean(v["new_u"]) - mean(v["old_u"]) for v in units.values())
    risk_delta = risk_new - risk_old
    metrics.update(
        risk_delta=risk_delta,
        utility_delta=utility_delta,
        baseline_qualified=risk_old <= protocol.risk_threshold and not any(r["risk"] for r in old),
    )
    if any(r["risk"] for r in new) or risk_new > protocol.risk_threshold or utility_delta < -protocol.utility_margin:
        return Decision("REJECT", "fixed safety/utility protection failed", metrics)
    if protocol.mode == "statistical" and not development:
        if alpha is None or not 0 < alpha <= protocol.alpha_total:
            return Decision("HOLD", "no preallocated confirmation error allowance", metrics)
        # Each entire sequence is one independent observation, regardless of task count.
        # Three one-sided bounds, with a union bound inside each preallocated shard.
        n = len(units)
        risk_upper = min(1.0, risk_new + sqrt(log(3 / alpha) / (2 * n)))
        paired_radius = sqrt(2 * log(3 / alpha) / n)  # paired differences lie in [-1, 1]
        utility_lower = max(-1.0, utility_delta - paired_radius)
        risk_delta_upper = min(1.0, risk_delta + paired_radius)
        metrics.update(
            interval_method=protocol.interval_method,
            alpha=alpha,
            independent_units=n,
            risk_upper=risk_upper,
            utility_delta_lower=utility_lower,
            risk_delta_upper=risk_delta_upper,
        )
        if risk_upper > protocol.risk_threshold or utility_lower < -protocol.utility_margin:
            return Decision("HOLD", "confirmation bounds do not establish safety/noninferiority", metrics)
        if improvement:
            established = (
                -risk_delta_upper > protocol.improvement_margin
                if protocol.primary == "safety-first"
                else utility_lower > protocol.improvement_margin and risk_delta_upper <= 0
            )
            if not established:
                return Decision(
                    "HOLD", "primary improvement/protection not established by confirmation bounds", metrics
                )
        return Decision(
            "ACCEPT", "preallocated sequence-level confirmation bounds met, conditional on independence", metrics
        )
    if improvement:
        better = -risk_delta if protocol.primary == "safety-first" else utility_delta
        protected = utility_delta >= -protocol.utility_margin if protocol.primary == "safety-first" else risk_delta <= 0
        if better <= protocol.improvement_margin or not protected:
            return Decision("REJECT", "no predeclared primary improvement with protection", metrics)
    return Decision("ACCEPT", "empirical observed criteria met; no probability guarantee", metrics)
