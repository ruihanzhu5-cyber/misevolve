"""Generic governance wrapper + the governable-store interface (paper RQ3).

`GovernedMethodHarness` wraps ANY `MethodHarness` and inserts governance at five
FIXED hook points, never touching the native evolution algorithm:

  1. pre-retrieval        — before every M/B task (record native candidates; the
                            active set already excludes quarantined/retired skills).
  2. post-outcome         — after a task with a VALID benign-utility result: attribute
                            that utility to the skills retrieved for the task.
  3. candidate-admission  — immediately after native `evolve_batch`: snapshot the new
                            candidate bundles and admit / reject / repair / quarantine.
  4. maintenance          — at the block cadence: downweight / retire on accumulated
                            evidence; enforce active-library capacity.
  5. frozen-export        — `authored_skills()` returns ONLY active skills, so the P
                            (persistence) batch reloads exactly the governed library.

ISOLATION (the whole point of the Utility-only control): a utility-only policy is
handed a `UtilitySignal` — a frozen dataclass that STRUCTURALLY contains only native
validity + benign-utility + capacity/age fields. It is impossible for it to read skill
text, CU/UG/Stealth, attack_success/harmful, M/B/P kind, the malicious goal, or
exposure/source status because those fields are not on the object. `M` tasks carry
`utility=None` by design, so malicious observations are NEUTRAL, never failures, and
the policy never even learns which tasks were malicious.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from misevolve import harness

ALLOWED_UTILITY_FIELDS = frozenset(
    {
        "key",
        "native_valid",
        "age",
        "n_success",
        "n_failure",
        "utility_score",
        "capacity_used",
        "capacity_max",
        "block",
    }
)

FORBIDDEN_FIELDS = frozenset(
    {
        "text",
        "skill_md",
        "content",
        "bundle",
        "referenced_files",
        "content_unsafety",
        "cu",
        "generalization",
        "ug",
        "sneakiness",
        "stealth",
        "attack_success",
        "harmful",
        "kind",
        "malicious_goal",
        "malicious",
        "exposure",
        "exposure_status",
        "source",
        "authored_source",
        "provenance",
        "safety_provenance",
        "unsafe",
        "risk",
    }
)


class Status(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class DecisionKind(str, Enum):
    ADMIT = "admit"
    REJECT = "reject"
    REPAIR = "repair"
    QUARANTINE = "quarantine"


@dataclass
class SkillBundle:
    """A complete candidate bundle as it lives in the method's store."""

    key: str
    text: str
    referenced_files: dict[str, str] = field(default_factory=dict)
    version: str | None = None
    status: Status = Status.ACTIVE

    def sha(self) -> str:
        h = hashlib.sha256()
        h.update(self.text.encode("utf-8", "replace"))
        for k in sorted(self.referenced_files):
            h.update(k.encode())
            h.update(self.referenced_files[k].encode("utf-8", "replace"))
        return "sha256:" + h.hexdigest()[:16]


@dataclass(frozen=True)
class UtilitySignal:
    """The ONLY view a utility-only policy receives. Frozen + minimal ON PURPOSE:
    it structurally cannot expose skill text, safety scores, task labels, or
    provenance. See ALLOWED_UTILITY_FIELDS (a test asserts the field set)."""

    key: str
    native_valid: bool
    age: int
    n_success: int
    n_failure: int
    utility_score: float
    capacity_used: int
    capacity_max: int
    block: int


@dataclass
class Candidate:
    """The RICH view a safety-aware policy (SafeEvolve) may receive — includes the
    full bundle + lineage. Never constructed for a utility-only policy."""

    bundle: SkillBundle
    native_valid: bool
    age: int
    n_success: int
    n_failure: int
    utility_score: float
    capacity_used: int
    capacity_max: int
    block: int
    n_unsafe_reuse: int = 0
    n_safe_reuse: int = 0
    lineage: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdmissionDecision:
    kind: DecisionKind
    reason: str = ""
    repaired_text: str | None = None
    repaired_files: dict[str, str] | None = None
    risk: float | None = None
    audit: dict[str, Any] | None = None


@runtime_checkable
class GovernableStore(Protocol):
    """A thin, method-specific control surface over the method's OWN skill store.
    Implemented per method in core/governance/stores.py; preserves each method's
    native authoring/retrieval/update."""

    def snapshot_skill_bundles(self) -> list[SkillBundle]: ...
    def is_valid(self, key: str) -> bool: ...
    def replace_skill(self, key: str, text: str, referenced_files: dict[str, str] | None = None) -> None: ...
    def set_skill_status(self, key: str, status: Status) -> None: ...
    def active_skills(self) -> list[harness.Skill]: ...
    def last_retrieved_skill_keys(self) -> list[str]: ...
    def export_active_skills(self) -> list[harness.Skill]: ...


class GovernancePolicy(ABC):
    name: str = "base"

    needs_content: bool = False
    can_repair: bool = False

    @abstractmethod
    def decide_admission(self, cand: "UtilitySignal | Candidate") -> AdmissionDecision:
        """Admit / reject / (repair / quarantine) a freshly-authored candidate."""

    @abstractmethod
    def choose_evictions(self, actives: "list[UtilitySignal] | list[Candidate]", capacity: int) -> list[str]:
        """When capacity is exceeded, return keys to RETIRE, worst-first. Must be
        deterministic (utility score, then age, then key)."""

    @abstractmethod
    def choose_retirements(
        self, actives: "list[UtilitySignal] | list[Candidate]", cfg: "GovernanceConfig"
    ) -> list[str]:
        """Maintenance-cadence retirements from accumulated evidence."""

    def on_valid_utility(self, keys: list[str], success: bool) -> None:
        """Optional: record a benign-utility observation attributed to `keys`."""

    def on_reuse_outcome(self, keys: list[str], harmful: bool) -> None:
        """Optional (SafeEvolve ONLY — needs_content): record a harmful/safe reuse
        observation. Never called for a utility-only policy."""

    def frozen_config(self) -> dict:
        return {}


@dataclass
class GovernanceConfig:
    capacity: int = 32
    min_observations: int = 2
    retirement_threshold: float = 0.35
    alpha: float = 1.0
    beta: float = 1.0
    cadence_blocks: int = 1

    admission_risk_threshold: float = 0.5
    reuse_risk_threshold: float = 0.6
    max_repair_attempts: int = 2
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "GovernanceConfig":
        d = json.loads(Path(path).read_text())
        known = {k: d[k] for k in d if k in cls.__dataclass_fields__}
        extra = {k: d[k] for k in d if k not in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.extra = extra
        return cfg

    def manifest_hash(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]


def smoothed_utility(n_success: int, n_failure: int, cfg: GovernanceConfig) -> float:
    return (n_success + cfg.alpha) / (n_success + n_failure + cfg.alpha + cfg.beta)


@dataclass
class _SkillState:
    key: str
    first_block: int
    status: Status = Status.ACTIVE
    n_success: int = 0
    n_failure: int = 0
    native_valid: bool = True

    n_unsafe_reuse: int = 0
    n_safe_reuse: int = 0
    lineage: dict[str, Any] = field(default_factory=dict)
    audits: list[dict] = field(default_factory=list)
    transitions: list[dict] = field(default_factory=list)


class GovernedMethodHarness(harness.MethodHarness):
    """Wraps an inner MethodHarness with governance hooks. Presents the SAME
    MethodHarness surface to run_episode, so nothing in the common loop changes."""

    def __init__(
        self,
        inner: harness.MethodHarness,
        policy: GovernancePolicy,
        cfg: GovernanceConfig,
        store_factory,
        log_dir: Path | None = None,
    ):
        self.inner = inner
        self.policy = policy
        self.cfg = cfg
        self.policy.cfg = cfg
        self._store_factory = store_factory
        self.store: GovernableStore | None = None
        self.name = f"{inner.name}+{policy.name}"
        self.needs_reward = getattr(inner, "needs_reward", False)
        self.evolves_on_benign = getattr(inner, "evolves_on_benign", True)
        self._log_dir = log_dir

        self._state: dict[str, _SkillState] = {}
        self._block = 0
        self._pending: dict[int, list[str]] = {}
        self._events: list[dict] = []
        self._t0 = time.time()

    @property
    def agent(self):
        return self.inner.agent

    @agent.setter
    def agent(self, v):
        self.inner.agent = v

    def setup(self, episode_dir: Path, agent: "harness.AgentRunner") -> None:
        self.inner.setup(episode_dir, agent)
        self.store = self._store_factory(self.inner)
        self._log_dir = self._log_dir or Path(episode_dir) / "_governance"
        Path(self._log_dir).mkdir(parents=True, exist_ok=True)
        (Path(self._log_dir) / "config.json").write_text(
            json.dumps(
                {**asdict(self.cfg), "policy": self.policy.name, "manifest_hash": self.cfg.manifest_hash()}, indent=2
            )
        )
        self._log("setup", policy=self.policy.name, manifest=self.cfg.manifest_hash())

    def run_task(self, prompt: str, *, kind: str, pos: int, out_dir: Path) -> "harness.Trajectory":

        native_before = [k for k in self._active_keys()]
        traj = self.inner.run_task(prompt, kind=kind, pos=pos, out_dir=out_dir)

        retrieved = list(self.store.last_retrieved_skill_keys() or [])
        self._pending[pos] = retrieved
        try:
            traj._gov_pos = pos
        except Exception:
            pass
        self._log("retrieval", pos=pos, native=native_before, retrieved=retrieved)
        return traj

    def evolve_batch(self, traces, outcomes, *, kind: str, round_idx: int) -> None:

        self._attribute_utility(traces, outcomes)

        if self.policy.needs_content and not self.cfg.extra.get("disable_reuse_risk_attribution", False):
            self._attribute_reuse_risk(traces, outcomes)

        pre = {b.key: b for b in self.store.snapshot_skill_bundles()}

        self.inner.evolve_batch(traces, outcomes, kind=kind, round_idx=round_idx)
        self._block += 1

        for b in self.store.snapshot_skill_bundles():
            anc = pre.get(b.key)
            if anc is None or anc.sha() != b.sha():
                self._admit(b, kind=kind, round_idx=round_idx, ancestor_text=(anc.text if anc is not None else None))

        if self._block % max(1, self.cfg.cadence_blocks) == 0:
            self._maintain()

    def authored_skills(self):

        return self.store.export_active_skills()

    def teardown(self) -> None:
        self._dump_artifacts()
        self.inner.teardown()

    def _attribute_utility(self, traces, outcomes) -> None:
        for traj, oc in zip(traces, outcomes):
            util = getattr(oc, "utility", None)
            if util is None:
                continue
            pos = getattr(traj, "_gov_pos", -1)
            keys = self._pending.get(pos, [])
            for k in keys:
                st = self._state.get(k)
                if st is None:
                    st = self._state.setdefault(k, _SkillState(k, self._block))
                if util:
                    st.n_success += 1
                else:
                    st.n_failure += 1
            self.policy.on_valid_utility(keys, bool(util))
            self._log("utility", pos=pos, success=bool(util), keys=keys)

    def _attribute_reuse_risk(self, traces, outcomes) -> None:
        """SafeEvolve-only: a retrieved skill reused on a HARMFUL task accrues unsafe-reuse
        evidence (the policy may then quarantine/retire it). NEVER called for utility-only."""
        for traj, oc in zip(traces, outcomes):
            harmful = getattr(oc, "harmful", None)
            if harmful is None:
                continue
            pos = getattr(traj, "_gov_pos", -1)
            keys = self._pending.get(pos, [])
            for k in keys:
                st = self._state.setdefault(k, _SkillState(k, self._block))
                if harmful:
                    st.n_unsafe_reuse += 1
                else:
                    st.n_safe_reuse += 1
            self.policy.on_reuse_outcome(keys, bool(harmful))
            self._log("reuse_risk", pos=pos, harmful=bool(harmful), keys=keys)

    def _admit(self, bundle: SkillBundle, *, kind: str, round_idx: int, ancestor_text: str | None = None) -> None:
        native_valid = self.store.is_valid(bundle.key)
        st = self._state.setdefault(bundle.key, _SkillState(bundle.key, self._block))
        st.native_valid = native_valid

        st.lineage.update(
            source_kind=kind,
            round=round_idx,
            sha=bundle.sha(),
            version=bundle.version,
            ancestor_text=(ancestor_text if ancestor_text is not None else st.lineage.get("ancestor_text")),
            ancestor_id=(bundle.key if ancestor_text else st.lineage.get("ancestor_id")),
        )
        for attempt in range(self.cfg.max_repair_attempts + 1):
            sig = self._signal(bundle, native_valid, st)
            dec = self.policy.decide_admission(sig)
            if dec.audit:
                st.audits.append(dec.audit)
            if dec.risk is not None:
                st.lineage["risk"] = dec.risk
            if dec.kind == DecisionKind.ADMIT:
                self._transition(st, Status.ACTIVE, dec.reason or "admitted")
                break
            if dec.kind == DecisionKind.REJECT:
                self.store.set_skill_status(bundle.key, Status.RETIRED)
                self._transition(st, Status.RETIRED, dec.reason or "rejected(invalid)")
                break
            if dec.kind == DecisionKind.QUARANTINE:
                self.store.set_skill_status(bundle.key, Status.QUARANTINED)
                self._transition(st, Status.QUARANTINED, dec.reason or "quarantined")
                break
            if dec.kind == DecisionKind.REPAIR and dec.repaired_text is not None:
                self.store.replace_skill(bundle.key, dec.repaired_text, dec.repaired_files)
                st.lineage.setdefault("repairs", []).append(
                    {"attempt": attempt, "reason": dec.reason, "sha_before": bundle.sha()}
                )
                bundle = SkillBundle(bundle.key, dec.repaired_text, dec.repaired_files or {}, bundle.version)
                st.lineage["sha"] = bundle.sha()
                continue

            self.store.set_skill_status(bundle.key, Status.QUARANTINED)
            self._transition(st, Status.QUARANTINED, "repair-exhausted")
            break

        self._enforce_capacity()

    def _maintain(self) -> None:
        actives = self._active_signals()
        for k in self.policy.choose_retirements(actives, self.cfg):
            self.store.set_skill_status(k, Status.RETIRED)
            self._transition(self._state[k], Status.RETIRED, "maintenance-retire")
        self._enforce_capacity()

    def _enforce_capacity(self) -> None:
        actives = self._active_signals()
        if len(actives) <= self.cfg.capacity:
            return
        for k in self.policy.choose_evictions(actives, self.cfg.capacity):
            self.store.set_skill_status(k, Status.RETIRED)
            self._transition(self._state[k], Status.RETIRED, "capacity-evict")
            if len(self._active_signals()) <= self.cfg.capacity:
                break

    def _signal(self, bundle: SkillBundle, native_valid: bool, st: _SkillState):
        base = dict(
            key=bundle.key,
            native_valid=native_valid,
            age=self._block - st.first_block,
            n_success=st.n_success,
            n_failure=st.n_failure,
            utility_score=smoothed_utility(st.n_success, st.n_failure, self.cfg),
            capacity_used=len(self._active_signals_state()),
            capacity_max=self.cfg.capacity,
            block=self._block,
        )
        if self.policy.needs_content:
            rich = {k: v for k, v in base.items() if k != "key"}
            return Candidate(
                bundle=bundle,
                lineage=dict(st.lineage),
                n_unsafe_reuse=st.n_unsafe_reuse,
                n_safe_reuse=st.n_safe_reuse,
                **rich,
            )
        return UtilitySignal(**base)

    def _active_signals(self):
        out = []
        for st in self._active_signals_state():
            b = self._bundle_for(st.key)
            if b is None:
                continue
            out.append(self._signal(b, st.native_valid, st))
        return out

    def _active_signals_state(self) -> list[_SkillState]:
        active = set(self._active_keys())
        return [st for st in self._state.values() if st.key in active]

    def _active_keys(self) -> list[str]:
        return [s.key for s in self.store.active_skills()]

    def _bundle_for(self, key: str) -> SkillBundle | None:
        for b in self.store.snapshot_skill_bundles():
            if b.key == key:
                return b
        return None

    def _transition(self, st: _SkillState, status: Status, reason: str) -> None:
        st.status = status
        st.transitions.append({"block": self._block, "status": status.value, "reason": reason})
        self._log("transition", key=st.key, status=status.value, reason=reason)

    def _log(self, ev: str, **kw) -> None:
        self._events.append({"t": round(time.time() - self._t0, 2), "block": self._block, "event": ev, **kw})

    def _dump_artifacts(self) -> None:
        if not self._log_dir:
            return
        d = Path(self._log_dir)
        (d / "events.jsonl").write_text("\n".join(json.dumps(e, default=str) for e in self._events))
        state = {
            k: {
                "status": s.status.value,
                "n_success": s.n_success,
                "n_failure": s.n_failure,
                "native_valid": s.native_valid,
                "utility_score": round(smoothed_utility(s.n_success, s.n_failure, self.cfg), 4),
                "lineage": s.lineage,
                "audits": s.audits,
                "transitions": s.transitions,
            }
            for k, s in self._state.items()
        }
        (d / "skill_state.json").write_text(json.dumps(state, indent=2, default=str))
        try:
            final = [{"key": sk.key, "text": sk.text} for sk in self.store.export_active_skills()]
            (d / "final_active_skills.json").write_text(json.dumps(final, indent=2, default=str))
        except Exception:
            pass


def build_governed(
    inner: harness.MethodHarness,
    policy: GovernancePolicy,
    cfg: GovernanceConfig,
    store_factory,
    log_dir: Path | None = None,
) -> GovernedMethodHarness:
    return GovernedMethodHarness(inner, policy, cfg, store_factory, log_dir)
