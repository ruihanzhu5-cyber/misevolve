"""Candidate-only adapter around AutoSkill's unchanged native evolve_batch.

The fork is a private SDK/Store, never the executing worker or approved release.
Caller installs the existing native retry/accounting hooks on the fork via
``configure``. No candidate generation or merge algorithm is implemented here.
"""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from misevolve.multiagent._io import atomic_json

from .models import digest


class NoExecution:
    def run(self, *args, **kwargs):
        raise RuntimeError("candidate evolution cannot execute benchmark tasks")


def fork_autoskill(source, destination: Path):
    """Clone quiescent native state, including resources and provenance, without an ingest."""
    from autoskill.models import Skill, SkillExample, SkillStatus

    destination = Path(destination).resolve()
    source_root = source._dir.resolve()
    if destination == source_root or destination.is_relative_to(source_root) or source_root.is_relative_to(destination):
        raise ValueError("candidate and source directories must be independent")
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("autoskill_store", "exchange_state.json", "evolution_events.jsonl", "task_events.jsonl"):
        path = source_root / name
        if path.exists():
            if path.is_symlink() or (path.is_dir() and any(p.is_symlink() for p in path.rglob("*"))):
                raise ValueError("candidate stores must not contain links to live state")
            if path.is_dir():
                shutil.copytree(path, destination / name)
            else:
                shutil.copy2(path, destination / name)
    clone = type(source)()
    try:
        clone.setup(destination, NoExecution())
        clone.enable_skill_exchange(source._exchange.worker_id)
        for name in ("_model", "_maxtok", "_hint", "_top_k"):
            if getattr(source, name) != getattr(clone, name):
                raise ValueError("native candidate configuration differs from execution source")
        if str(source._areviser.base_url) != str(clone._areviser.base_url):
            raise ValueError("native candidate endpoint differs from execution source")
        # LocalStore's SKILL.md reload loses native fields; restore using the existing SDK API.
        expected = source._exchange.snapshot_store()
        for local_id, bundle in expected.items():
            metadata = deepcopy(bundle["native_metadata"])
            native = Skill(**{**metadata, "status": SkillStatus(metadata["status"]),
                              "examples": [SkillExample(**example) for example in metadata["examples"]]})
            clone.sdk.store.upsert(native, raw=metadata)
        if clone._exchange.snapshot_store() != expected:
            raise ValueError("candidate fork did not preserve native state")
        for name in ("_prov", "_last_kind", "_last_round", "_prompt_by_traj", "_retrieved_by_traj", "_task_by_traj"):
            setattr(clone, name, deepcopy(getattr(source, name)))
        return clone
    except BaseException:
        clone.teardown()
        raise


def _version(owner, bundle, skill_id=None):
    if bundle.get("resource_files"):
        # Do not silently lose binary/supporting resources in the text-only publication adapter.
        raise ValueError("resource-bearing native skill requires a verified resource exporter")
    key = skill_id or f"{owner}:{bundle['local_skill_key']}"
    return {"type": "skill_version", "skill_id": key, "owner": key.split(":")[0],
            "files": bundle["skill_files"], "native_metadata": bundle["native_metadata"],
            "native_version": bundle["version"]}


class AutoSkillCandidateCollector:
    def __init__(self, store, *, fork=fork_autoskill, configure=None):
        self.store, self.fork, self.configure = store, fork, configure

    def collect(self, *, base: str, methods: dict, traces: dict, kind: str, round_idx: int,
                skill_keys: dict | None = None) -> list[str]:
        if kind not in {"M", "B"}:
            raise ValueError("native evolution is forbidden during Probe")
        approved = self.store.read_snapshot(base)
        candidates = []
        for owner, source in methods.items():
            local_traces = traces.get(owner, [])
            if not local_traces:
                continue
            before = source._exchange.snapshot_store()
            if skill_keys is not None and owner not in skill_keys:
                raise ValueError("shared native view is missing a worker skill map")
            mapping = (skill_keys or {}).get(owner)
            if mapping is not None:
                if set(mapping) != set(before) or len(set(mapping.values())) != len(mapping):
                    raise ValueError("global native view requires a bijective local-to-logical skill map")
                if set(mapping.values()) != set(approved["skills"]):
                    raise ValueError("shared native view must contain the complete approved library")
            else:
                mapping = {local_id: f"{owner}:{local_id}" for local_id in before}
            # Candidate evolution must start from the pinned approved native view.
            for local_id, bundle in before.items():
                key = mapping[local_id]
                if approved["skills"].get(key) != digest(_version(owner, bundle, key)):
                    raise ValueError("native source contains unapproved or mismatched skills")
            owned = {key for key in approved["skills"] if key.startswith(f"{owner}:")}
            if skill_keys is None and owned != set(mapping.values()):
                raise ValueError("native source is missing approved skills")
            target = self.store.root / "workspaces" / f"candidate-{uuid4().hex}"
            clone = self.fork(source, target)
            events = []
            try:
                if clone is source or clone.sdk is source.sdk or clone.sdk.store is source.sdk.store:
                    raise ValueError("native candidate fork shares live SDK/Store")
                if Path(clone._store_dir).resolve().is_relative_to(source._dir.resolve()):
                    raise ValueError("native candidate Store aliases live execution directory")
                if self.configure:
                    self.configure(owner, clone)
                clone.evolve_batch(local_traces, [], kind=kind, round_idx=round_idx)
                events = list(clone._last_evolution_events)
                if any(not event.get("success") for event in events):
                    raise ValueError("failed native evolution cannot yield admissible candidates")
                after = clone._exchange.snapshot_store()
                event_ids = [self.store.put({"type": "native_evolution", "owner": owner, "event": e}) for e in events]
                histories = {}
                for event in events:
                    for local_id, bundle in event.get("after", {}).items():
                        key = mapping.get(local_id, f"{owner}:{local_id}")
                        histories.setdefault(local_id, []).append(self.store.put(_version(owner, bundle, key)))
                for local_id in sorted(set(before) | set(after)):
                    key = mapping.get(local_id, f"{owner}:{local_id}")
                    old = _version(owner, before[local_id], key) if local_id in before else None
                    new = _version(owner, after[local_id], key) if local_id in after else None
                    if old == new:
                        continue
                    sources = sorted({e.get("task", {}).get("instance_id") for e in events
                                      if e.get("task", {}).get("instance_id")})
                    candidates.append(self.store.stage(
                        base=base, skill_id=key, after=new, source_tasks=sources,
                        history=histories.get(local_id, []), evidence=event_ids,
                    ))
            finally:
                # Failed native ingests retain their partial versions and error evidence too.
                audit = {"type": "native_candidate_attempt", "base": base, "owner": owner,
                         "events": deepcopy(getattr(clone, "_last_evolution_events", events))}
                oid = self.store.put(audit)
                atomic_json(target / "candidate_attempt.json", {"evidence": oid})
                clone.teardown()
                if source._exchange.snapshot_store() != before:
                    raise RuntimeError("native candidate generation changed the execution Store")
        return candidates
