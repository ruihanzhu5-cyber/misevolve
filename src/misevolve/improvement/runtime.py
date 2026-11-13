"""Library operations and real isolated experiments using the existing Phase 3 engine."""

from __future__ import annotations

import difflib
import re
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict

from misevolve.multiagent._io import append_jsonl, atomic_json
from misevolve.multiagent.exchange import export_bundle
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.team.assessment import assess_task
from misevolve.team.executor import TeamExecutor, TeamTaskExecutionError, TeamWorkerPool
from misevolve.team.router import AGENTS, TeamEventLogger
from misevolve.team.skills import SkillTransferService

from .data import visible_trace
from .policy import digest
from .store import IntegrityError


def snapshot(workers):
    result = {agent: worker.method._exchange.snapshot_store() for agent, worker in workers.items()}
    root = next(iter(workers.values())).episode_dir.parent.parent
    registry = GlobalSkillRegistry(root / "registry") if (root / "registry").exists() else None
    for agent, bundles in result.items():
        exchange = workers[agent].method._exchange
        for sid, bundle in bundles.items():
            imports = {
                gid: deepcopy(record) for gid, record in exchange.imports.items() if record.get("local_id") == sid
            }
            if not imports:
                continue
            if registry is None:
                raise ValueError("imported snapshot requires its intact source registry")
            bundle["exchange_imports"] = imports
            artifacts = {}

            def collect(gid):
                if gid in artifacts:
                    return
                artifact = registry.get_skill(gid).to_dict()
                for parent in artifact["parent_skill_ids"] + (
                    [artifact["native_revision_parent_id"]] if artifact["native_revision_parent_id"] else []
                ):
                    collect(parent)
                artifacts[gid] = artifact

            for gid in imports:
                collect(gid)
            bundle["lineage"] = {
                "artifacts": list(artifacts.values()),
                "transfers": [row for row in registry.get_transfer_history() if row.get("global_skill_id") in imports],
            }
    return result


def native_skill(metadata):
    from autoskill.models import Skill, SkillExample, SkillStatus

    return Skill(
        **{
            **metadata,
            "status": SkillStatus(metadata["status"]),
            "examples": [SkillExample(**e) for e in metadata["examples"]],
        }
    )


def install(workers, library):
    if set(library) != set(AGENTS):
        raise ValueError("library must retain per-agent visibility")
    for agent, bundles in library.items():
        worker = workers[agent]
        method = worker.method
        for skill in method.sdk.list(user_id=method._UID):
            if skill.id not in bundles:
                method.sdk.delete(skill.id)
        for local_id, bundle in bundles.items():
            metadata = deepcopy(bundle["native_metadata"])
            if (
                metadata["id"] != local_id
                or metadata["user_id"] != method._UID
                or bundle.get("resource_files")
                or set(metadata.get("files", {})) - {"SKILL.md"}
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", local_id)
            ):
                raise ValueError("invalid owner, ID or executable/resource skill")
            method.sdk.store.upsert(native_skill(metadata), raw=metadata)
            if export_bundle(method.sdk, local_id)["content_hash"] != bundle["content_hash"]:
                raise ValueError("library materialization changed skill bytes")
            method._exchange.origins[local_id] = {
                "origin": bundle.get("origin", "unknown"),
                "imported_from_global_id": bundle.get("imported_from_global_id"),
                "content_hash": bundle["content_hash"],
            }
            for gid, record in bundle.get("exchange_imports", {}).items():
                if record.get("local_id") != local_id:
                    raise ValueError("snapshot import mapping has the wrong local ID")
                method._exchange.imports[gid] = deepcopy(record)
                worker.imported_global_to_local[gid] = local_id
                worker.local_to_global.setdefault(local_id, gid)
        # Retired skills keep immutable historical records, but no live pointer to a deleted SDK object.
        worker.local_to_global = {sid: gid for sid, gid in worker.local_to_global.items() if sid in bundles}
        worker.imported_global_to_local = {
            gid: sid for gid, sid in worker.imported_global_to_local.items() if sid in bundles
        }
        method._exchange.imports = {
            gid: record for gid, record in method._exchange.imports.items() if record.get("local_id") in bundles
        }
        method._exchange.origins = {sid: record for sid, record in method._exchange.origins.items() if sid in bundles}
        worker.save_state()
        method._exchange.save()


def register_snapshot(registry, workers, library, *, block="initial"):
    restored_transfers = set()
    for bundles in library.values():
        for bundle in bundles.values():
            for artifact in bundle.get("lineage", {}).get("artifacts", []):
                restored = registry.register_skill(
                    artifact["owner_agent"], artifact, artifact["source_task_ids"], artifact, block_id="lineage_restore"
                )
                if restored.global_skill_id != artifact["global_skill_id"]:
                    raise ValueError("snapshot lineage identity mismatch")
            for row in bundle.get("lineage", {}).get("transfers", []):
                key = digest(row)
                if key not in restored_transfers:
                    registry.append_event(row)
                    restored_transfers.add(key)
    for agent, bundles in library.items():
        for local_id, bundle in bundles.items():
            if bundle.get("origin") == "imported":
                ref = registry.resolve_reference(agent, bundle)
                if ref["resolution"] != "verified_import":
                    raise ValueError("imported snapshot has no verified source/transfer evidence")
                workers[agent].remember_local_version(local_id, ref["global_skill_id"])
                continue
            artifact = registry.register_skill(
                agent, bundle, [], {"transformation_type": "snapshot_restore"}, block_id=block
            )
            workers[agent].remember_local_version(local_id, artifact.global_skill_id)


class FrozenTransfers(SkillTransferService):
    """Fixed evaluation hook: messages still route normally, library writes are blocked."""

    def publish(self, message, *, frozen=False, receipt=None):
        return super().publish(message, frozen=True, receipt=receipt)


class Runtime:
    """Injected factory is the same isolated_openclaw_factory as native team runs."""

    def __init__(self, root, *, runner_factory, backbone, router_type, judge, configuration, method_factory=None):
        self.root = root
        self.runner_factory = runner_factory
        self.method_factory = method_factory
        self.backbone = backbone
        self.router_type = router_type
        self.judge = judge
        self.configuration = deepcopy(configuration)

    @contextmanager
    def branch(self, library, label):
        root = self.root / uuid.uuid4().hex[:12]
        atomic_json(root / "branch.json", {"label": label, "start_library": digest(library)})
        pool = TeamWorkerPool(
            root, runner_factory=self.runner_factory, backbone=self.backbone, method_factory=self.method_factory
        )
        workers = pool.setup_workers()
        try:
            try:
                install(workers, library)
                registry = GlobalSkillRegistry(root / "registry")
                register_snapshot(registry, workers, library)
            except (ValueError, KeyError, OSError) as exc:
                raise IntegrityError("library/registry snapshot restoration failed") from exc
            router = self.router_type(
                TeamEventLogger(root / "team_events.jsonl", "ours-isolated", self.router_type.protocol)
            )
            executor = TeamExecutor(workers, router, SkillTransferService(registry, workers, router))
            yield root, workers, executor
        finally:
            pool.teardown()

    def edit(self, library, changes, *, agent):
        if agent not in AGENTS:
            raise ValueError("unknown owner")
        required = {
            "operation",
            "skill_id",
            "parents",
            "name",
            "description",
            "instructions",
            "applicability",
            "dependencies",
            "reason",
        }
        seen = set()
        for change in changes:
            if set(change) != required or change["operation"] not in {
                "create",
                "edit",
                "merge",
                "deactivate",
                "delete",
            }:
                raise ValueError("invalid skill change schema")
            sid, parents = change["skill_id"], change["parents"]
            if not isinstance(sid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", sid) or sid in seen:
                raise ValueError("invalid or overlapping skill operation")
            seen.add(sid)
            if not isinstance(parents, dict) or any(
                p not in library[agent] or library[agent][p]["content_hash"] != value for p, value in parents.items()
            ):
                raise ValueError("parent version mismatch or cross-owner reference")
            if change["operation"] == "create":
                if sid in library[agent] or parents:
                    raise ValueError("create must have a new ID and no parents")
            elif sid not in parents or (change["operation"] == "merge" and len(parents) < 2):
                raise ValueError("edit/deletion requires exact parent; merge requires multiple parents")
            if not isinstance(change["dependencies"], list) or any(
                dep not in library[agent] for dep in change["dependencies"]
            ):
                raise ValueError("dependencies must be visible local skills")
            if any(
                not isinstance(change[k], str)
                for k in ("name", "description", "instructions", "applicability", "reason")
            ):
                raise ValueError("skill fields must be text")
            if change["operation"] not in {"delete", "deactivate"} and (
                not all(change[k].strip() for k in ("name", "description", "instructions", "applicability"))
                or len(change["instructions"].encode()) > 1_000_000
            ):
                raise ValueError("empty or oversized text skill")
        with self.branch(library, "candidate") as (_, workers, _):
            method = workers[agent].method
            for change in changes:
                sid = change["skill_id"]
                if change["operation"] in {"delete", "deactivate"}:
                    # Deactivated bytes remain in immutable candidate/history objects; absent from active search.
                    method.sdk.delete(sid)
                else:
                    method.sdk.upsert(
                        user_id=method._UID,
                        skill_id=sid,
                        name=change["name"],
                        description=change["description"],
                        instructions=change["instructions"] + "\n\nApplicability: " + change["applicability"],
                        metadata={"ours": {"parents": change["parents"], "dependencies": change["dependencies"]}},
                    )
                    if change["operation"] == "merge":
                        for parent in change["parents"]:
                            if parent != sid:
                                method.sdk.delete(parent)
            after = snapshot(workers)
            for bundle in after[agent].values():
                dependencies = bundle["native_metadata"].get("metadata", {}).get("ours", {}).get("dependencies", [])
                if set(dependencies) - after[agent].keys():
                    raise ValueError("candidate leaves an unsatisfied skill dependency")
            method._exchange._observe_changes(library[agent], after[agent])
            return after

    def evaluate(self, library, tasks, *, purpose, policy_hash, pair_id, label, action="behavior_retest"):
        records = []
        for entry in tasks:
            task = entry["task"]
            # A new whole team and fresh containers per task prevent diagnostic imports/memory leaking.
            with self.branch(library, label) as (root, workers, executor):
                if action == "cross_receiver":
                    self._deliver_context(executor, task)
                frozen = snapshot(workers)
                executor.transfers = FrozenTransfers(executor.transfers.registry, workers, executor.router)
                error = None
                try:
                    result = executor.run(task, task_run_id=uuid.uuid4().hex, out_dir=root / "task")
                except TeamTaskExecutionError as exc:
                    result, error = exc.result, type(exc).__name__
                assessment = assess_task(self.judge, result, entry["episode"], task)
                verdict = assessment["team_verdict"]
                if frozen != snapshot(workers):
                    raise ValueError("frozen evaluation changed stores")
                record = {
                    "task_id": task["instance_id"],
                    "kind": task["kind"],
                    "sequence": entry["sequence"],
                    "purpose": purpose,
                    "pair_id": pair_id,
                    "library_hash": digest(library),
                    "policy_hash": policy_hash,
                    "library_frozen": True,
                    "policy_frozen": True,
                    "risk": verdict.get("unsafe_action"),
                    "utility": verdict.get("utility"),
                    "status": result.execution_status,
                    "missing_reason": error,
                    "environment_hash": digest(self.configuration),
                    "source_sha256": entry["source_sha256"],
                    "context_library_hash": digest(frozen),
                    "topology": self.router_type.protocol,
                    "action": action,
                    "evidence": str(root),
                    "token_cost": None,
                    "cost_missing_reason": "see shared provider ledger",
                }
                if record["risk"] is None or (task["kind"] != "M" and record["utility"] is None):
                    record["missing_reason"] = record["missing_reason"] or "missing behavioral assessment"
                atomic_json(root / "evaluation.json", record)
                atomic_json(
                    root / "trajectory.json",
                    {"messages": result.trajectory.messages, "raw": result.trajectory.raw, "assessment": assessment},
                )
                records.append(record)
        return records

    @staticmethod
    def _deliver_context(executor, task):
        from misevolve.team.router import TeamMessage

        if task["kind"] != "B":
            raise ValueError("cross-receiver development context requires a B task; no Probe imports")
        # Deliver along existing directed edges only; hierarchy never introduces B->C.
        for sender, receiver in sorted(executor.router.edges):
            for skill in list(executor.workers[sender].method.sdk.list(user_id="skillmisevo")):
                message = TeamMessage(
                    episode_id=executor.log.episode_id,
                    task_id=task["instance_id"],
                    sender_id=sender,
                    receiver_id=receiver,
                    message_type="SKILL_PAYLOAD",
                    payload={"local_skill_id": skill.id},
                )
                executor.transfers.publish(message)

    def learn_block(self, library, tasks, label):
        traces = {a: [] for a in AGENTS}
        with self.branch(library, label) as (root, workers, executor):
            for entry in tasks:
                task = entry["task"]
                if task["kind"] == "P":
                    raise ValueError("frozen Probe cannot enter policy learning")
                result = executor.run(task, task_run_id=uuid.uuid4().hex, out_dir=root / digest(task["instance_id"]))
                atomic_json(
                    root / f"trace-{digest(task)}.json",
                    {"messages": result.trajectory.messages, "raw": result.trajectory.raw},
                )
                if result.execution_status != "completed":
                    raise RuntimeError("shadow/replay learning task incomplete")
                for agent, runtime in result.runtimes.items():
                    trace = runtime.learning_trace(task)
                    if trace is not None:
                        traces[agent].append({**visible_trace(trace), "source": {"instance_id": task["instance_id"]}})
            return snapshot(workers), traces


def changes_diff(before, after):
    return {
        agent: {
            sid: "".join(
                difflib.unified_diff(
                    before[agent].get(sid, {}).get("skill_files", {}).get("SKILL.md", "").splitlines(True),
                    after[agent].get(sid, {}).get("skill_files", {}).get("SKILL.md", "").splitlines(True),
                    fromfile="parent",
                    tofile="candidate",
                )
            )
            for sid in before[agent].keys() | after[agent].keys()
            if before[agent].get(sid) != after[agent].get(sid)
        }
        for agent in AGENTS
    }


def publish(workers, library, *, window, policy_hash, sources=None):
    before = snapshot(workers)
    install(workers, library)
    events = {}
    for agent, worker in workers.items():
        exchange = worker.method._exchange
        after = exchange.snapshot_store()
        exchange._observe_changes(before[agent], after)
        event = {
            "event": "ours_publication",
            "event_id": digest([window, agent, policy_hash, library]),
            "worker_id": agent,
            "source_task_ids": (sources or {}).get(agent, [f"ours-window-{window}"]),
            "task": {"task_run_id": f"ours-window-{window}", "kind": "ours"},
            "before": before[agent],
            "after": after,
            "retrieved": [],
            "invocation_evidence": [],
            "success": True,
            "policy_hash": policy_hash,
            "updated_local_skill_ids": list(after),
            "returned_skills": [asdict(worker.method.sdk.get(sid)) for sid in after],
        }
        append_jsonl(exchange.evolution_path, event)
        exchange.save()
        events[agent] = [event]
    return events
