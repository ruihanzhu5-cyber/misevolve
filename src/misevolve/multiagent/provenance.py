"""Rebuildable, append-only projection of independent lifecycle evidence.

Local-version facts never change when a registry artifact becomes available later.
Separate resolved_to edges bind those references to registered global artifacts.
One writer per output directory; original journals remain the source of truth.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from pathlib import Path

from ._io import append_jsonl, canonical_json, read_jsonl
from .exchange import imported_files, skill_reference
from .models import content_hash
from .models import valid_evidence as _explicit
from .registry import GlobalSkillRegistry


class ProvenanceIntegrityError(ValueError):
    """Source evidence or a previously normalized event changed identity/content."""


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _event_id(event: dict) -> str:
    return _digest([event[key] for key in (
        "schema_version", "evidence_source", "source_event_id", "source_event_kind",
        "relation", "subject", "object", "worker_id", "task_run_id",
    )])


def _artifact(gid: str) -> dict:
    return {"kind": "skill_artifact", "global_skill_id": gid}


def _task(worker_id: str, task_run_id: str) -> dict:
    return {"kind": "task_run", "worker_id": worker_id, "task_run_id": task_run_id}


class ProvenanceTracker:
    def __init__(self, run_dir: Path, registry: GlobalSkillRegistry | None = None, *, output_dir: Path | None = None):
        self.run_dir = Path(run_dir).resolve()
        self.registry = registry or GlobalSkillRegistry(self.run_dir / "registry")
        self.journal = Path(output_dir or self.run_dir / "provenance") / "provenance_events.jsonl"
        self._events: dict[str, dict] = {}
        for event in read_jsonl(self.journal):
            try:
                valid = event["schema_version"] == 1 and event["event_id"] == _event_id(event)
            except KeyError as exc:
                raise ProvenanceIntegrityError("invalid normalized event schema") from exc
            if not valid:
                raise ProvenanceIntegrityError("normalized event ID/schema mismatch")
            self._insert(self._events, event)

    @staticmethod
    def _insert(events: dict, event: dict) -> None:
        previous = events.get(event["event_id"])
        if previous is not None and previous != event:
            raise ProvenanceIntegrityError(f"event ID has different content: {event['event_id']}")
        events[event["event_id"]] = event

    def _emit(self, source, relation, subject, obj, *, worker_id=None, task_run_id=None,
              evidence_status="confirmed", **metadata):
        path, source_id, source_kind, _ = source
        event = {
            "schema_version": 1, "relation": relation, "subject": subject, "object": obj,
            "worker_id": worker_id, "task_run_id": task_run_id, "evidence_source": path,
            "source_event_id": source_id, "source_event_kind": source_kind,
            "evidence_status": evidence_status, "evidence_metadata": metadata,
        }
        event["event_id"] = _event_id(event)
        self._insert(self._pending, event)

    def _sources(self):
        paths = [self.registry.journal, self.registry.transfer_journal]
        for worker in sorted((self.run_dir / "workers").glob("*")):
            if worker.is_dir():
                paths.extend([worker / "task_events.jsonl", worker / "evolution_events.jsonl"])
        for path in paths:
            relative = os.path.relpath(path, self.run_dir).replace(os.sep, "/")
            for line, row in enumerate(read_jsonl(path), 1):
                source_id = row.get("event_id") or row.get("attempt_id") or f"row:{line}"
                yield (relative, str(source_id), row.get("event", "unknown"), row)

    def _observe(self, worker_id, snapshot, source, task_run_id=None):
        compact = skill_reference(snapshot)
        digest = compact["content_hash"]
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProvenanceIntegrityError("invalid content hash in source reference")
        local = {
            "kind": "local_skill_version", "worker_id": worker_id,
            "local_skill_id": compact["local_skill_id"], "content_hash": digest,
        }
        self._emit(source, "reference_observed", local, {"kind": "reference"}, worker_id=worker_id,
                   task_run_id=task_run_id, evidence_status="observed", origin=compact["origin"],
                   imported_global_skill_id=compact["imported_global_skill_id"])
        resolved = self.registry.resolve_reference(worker_id, snapshot)
        if resolved["global_skill_id"]:
            self._emit(source, "resolved_to", local, _artifact(resolved["global_skill_id"]),
                       worker_id=worker_id, task_run_id=task_run_id, resolution=resolved["resolution"])
        return local, resolved["global_skill_id"]

    def _project_transfer(self, source):
        row = source[3]
        kind = row["event"]
        if kind == "empty_transfer":
            self._emit(source, "transfer_attempt", {"kind": "worker", "worker_id": row.get("source_agent")},
                       {"kind": "worker", "worker_id": row.get("target_agent")},
                       evidence_status="empty")
            return
        if kind not in {"skill_transfer", "skill_transfer_started"}:
            return
        confirmed = self.registry.verified_transfer(row)
        self._emit(source, "transfer_attempt", {"kind": "worker", "worker_id": row.get("source_agent")},
                   {"kind": "worker", "worker_id": row.get("target_agent")},
                   worker_id=row.get("target_agent"), evidence_status="confirmed" if confirmed else "unconfirmed",
                   claimed_global_skill_id=row.get("global_skill_id"),
                   integrity_verified=row.get("integrity_verified") is True,
                   diagnostic_visibility_verified=row.get("native_retrieval_verified") is True)
        if confirmed:
            gid = row["global_skill_id"]
            files = imported_files(self.registry.export_skill(gid), row["target_local_id"])
            receiver, _ = self._observe(row["target_agent"], {
                "local_skill_id": row["target_local_id"], "content_hash": content_hash(files),
                "origin": "imported", "imported_global_skill_id": gid,
            }, source)
            self._emit(source, "transferred_from", receiver, _artifact(gid), worker_id=row["target_agent"],
                       source_agent=row["source_agent"], target_agent=row["target_agent"],
                       source_local_id=row["source_local_id"], target_local_id=row["target_local_id"])

    def _project_registration(self, source):
        row = source[3]
        if row["event"] not in {"skill_registered", "skill_observed"}:
            return
        artifact = self.registry.get_skill(row["global_skill_id"])
        child_reference, _ = self._observe(artifact.owner_agent, {
            "local_skill_id": artifact.local_skill_key, "content_hash": artifact.content_hash,
            "origin": artifact.origin, "imported_global_skill_id": artifact.imported_from_global_id,
        }, source)
        provenance = row.get("provenance", {})
        parents = list(provenance.get("parent_skill_ids", []))
        if provenance.get("native_revision_parent_id"):
            parents.append(provenance["native_revision_parent_id"])
        for parent in dict.fromkeys(parents):
            evidence = provenance.get("parent_evidence", {}).get(parent, {})
            if _explicit(evidence, "explicit_derivation"):
                relation = "derived_from"
            elif _explicit(evidence, "native_revision"):
                # A registry declaration alone is not the native before/after evidence.
                # Confirmed native_revision edges are emitted from the evolution journal.
                relation = "native_revision_claim"
            else:
                raise ProvenanceIntegrityError("registered parent lacks typed evidence")
            self.registry.get_skill(parent)
            self._emit(source, relation, _artifact(artifact.global_skill_id), _artifact(parent),
                       worker_id=artifact.owner_agent, source_ref=evidence["source_ref"],
                       evidence_status="confirmed" if relation == "derived_from" else "observed")
        if provenance.get("unresolved_predecessor"):
            predecessor, _ = self._observe(artifact.owner_agent, provenance["unresolved_predecessor"], source)
            self._emit(source, "revision_predecessor", child_reference, predecessor, worker_id=artifact.owner_agent,
                       evidence_status="observed")

    @staticmethod
    def _valid_manifest(manifest):
        if (not isinstance(manifest, dict) or manifest.get("status") != "success"
                or not isinstance(manifest.get("skills"), list)):
            return False
        seen = set()
        for item in manifest["skills"]:
            if not isinstance(item, dict):
                return False
            if (not isinstance(item.get("local_skill_id"), str) or not item["local_skill_id"]
                    or item["local_skill_id"] in seen
                    or not isinstance(item.get("runtime_skill_name"), str) or not item["runtime_skill_name"]
                    or not isinstance(item.get("runtime_directory_slug"), str)
                    or not isinstance(item.get("runtime_content_hash"), str)
                    or not re.fullmatch(r"[\w-]{1,128}", item.get("runtime_directory_slug", ""))
                    or not re.fullmatch(r"[a-f0-9]{64}", item.get("runtime_content_hash", ""))):
                return False
            seen.add(item["local_skill_id"])
        return True

    def _project_task(self, source):
        row = source[3]
        worker, task_id = row["worker_id"], row["task_run_id"]
        task = _task(worker, task_id)
        kind = row["event"]
        if kind == "task_retrieval":
            status = row.get("retrieval_status", "unknown")
            self._emit(source, "retrieval_status", task, {"kind": "retrieval", "status": status},
                       worker_id=worker, task_run_id=task_id, count=len(row.get("retrieved", [])),
                       evidence_status="confirmed" if status == "completed" else "unknown")
            for ref in row.get("retrieved", []):
                local, _ = self._observe(worker, ref, source, task_id)
                self._emit(source, "retrieved_from", task, local, worker_id=worker, task_run_id=task_id,
                           evidence_status="confirmed" if status != "failed" else "unknown")
        elif kind == "task_execution":
            self._emit(source, "execution_status", task,
                       {"kind": "execution", "status": row.get("execution_status", "unknown")},
                       worker_id=worker, task_run_id=task_id, error_type=row.get("error_type"))
            manifest = row.get("injection_manifest")
            valid = self._valid_manifest(manifest)
            self._emit(source, "injection_status", task,
                       {"kind": "injection", "status": "success" if valid else "unknown"},
                       worker_id=worker, task_run_id=task_id, evidence_status="confirmed" if valid else "unknown")
            if valid:
                references = self._retrievals.get((worker, task_id), {})
                for item in manifest["skills"]:
                    runtime = {
                        "kind": "runtime_skill", "worker_id": worker, "task_run_id": task_id,
                        "local_skill_id": item["local_skill_id"],
                        "runtime_content_hash": item["runtime_content_hash"],
                        "runtime_directory_slug": item["runtime_directory_slug"],
                    }
                    self._emit(source, "injected_to", runtime, task, worker_id=worker, task_run_id=task_id,
                               runtime_skill_name=item["runtime_skill_name"])
                    if item["local_skill_id"] in references:
                        local, _ = self._observe(worker, references[item["local_skill_id"]], source, task_id)
                        self._emit(source, "runtime_from", runtime, local, worker_id=worker, task_run_id=task_id)
        elif kind == "skill_invocation":
            evidence = row.get("evidence", {})
            if not _explicit(evidence, "explicit_invocation"):
                raise ProvenanceIntegrityError("invocation requires independent explicit evidence")
            local, _ = self._observe(worker, row["skill"], source, task_id)
            self._emit(source, "invoked_from", task, local, worker_id=worker, task_run_id=task_id,
                       source_ref=evidence["source_ref"])

    def _project_evolution(self, source):
        event = source[3]
        worker = event["worker_id"]
        task_id = event.get("task", {}).get("task_run_id")
        for snapshot in event.get("retrieved", []):
            reference, _ = self._observe(worker, snapshot, source, task_id)
            self._emit(source, "evolution_retrieval_context", {
                "kind": "evolution_event", "source_event_id": event["event_id"],
            }, reference, worker_id=worker, task_run_id=task_id, evidence_status="observed")
        for local_id, after in event.get("after", {}).items():
            before = event.get("before", {}).get(local_id)
            if (after.get("local_skill_key") != local_id
                    or (before is not None and before.get("local_skill_key") != local_id)):
                raise ProvenanceIntegrityError("native evolution snapshot local ID mismatch")
            if before is not None and before["content_hash"] == after["content_hash"]:
                continue
            child, child_gid = self._observe(worker, after, source, task_id)
            self._emit(source, "native_update_observed", child, {"kind": "evolution"}, worker_id=worker,
                       task_run_id=task_id, evidence_status="observed")
            if before is not None:
                predecessor, parent_gid = self._observe(worker, before, source, task_id)
                self._emit(source, "revision_predecessor", child, predecessor, worker_id=worker,
                           task_run_id=task_id, evidence_status="observed")
                if child_gid and parent_gid:
                    self._emit(source, "native_revision", _artifact(child_gid), _artifact(parent_gid),
                               worker_id=worker, task_run_id=task_id, source_ref=event["event_id"])

    def sync(self) -> list[dict]:
        self.registry = GlobalSkillRegistry(self.registry.directory)
        sources = list(self._sources())
        self._pending: dict[str, dict] = {}
        self._retrievals = {}
        for source in sources:
            row = source[3]
            if row.get("event") == "task_retrieval":
                key = (row["worker_id"], row["task_run_id"])
                refs = {ref["local_skill_id"]: ref for ref in row.get("retrieved", [])}
                if key in self._retrievals and self._retrievals[key] != refs:
                    raise ProvenanceIntegrityError("task_run_id has conflicting retrieval evidence")
                self._retrievals[key] = refs
        for source in sources:
            row = source[3]
            self._emit(source, "source_record", {"kind": "source_event", "source_event_id": source[1]},
                       {"kind": "journal", "path": source[0]}, evidence_status="recorded",
                       source_digest=_digest(row),
                       worker_id=row.get("worker_id", row.get("target_agent", row.get("owner_agent"))),
                       task_run_id=row.get("task_run_id", row.get("task", {}).get("task_run_id")))
            if source[0].endswith("transfer_events.jsonl"):
                self._project_transfer(source)
            elif source[0].endswith("task_events.jsonl"):
                self._project_task(source)
            elif source[0].endswith("evolution_events.jsonl"):
                self._project_evolution(source)
            else:
                self._project_registration(source)
        # Validate the entire projection before appending any new event.
        for gid, previous in self._events.items():
            if gid in self._pending and self._pending[gid] != previous:
                raise ProvenanceIntegrityError(f"event ID has different content: {gid}")
            if previous["relation"] == "source_record" and gid not in self._pending:
                raise ProvenanceIntegrityError("source journal is no longer append-only")
        added = [event for gid, event in self._pending.items() if gid not in self._events]
        for event in added:
            append_jsonl(self.journal, event)
            self._events[event["event_id"]] = event
        return copy.deepcopy(added)

    def get_events(self, relation=None, worker_id=None, task_run_id=None) -> list[dict]:
        return copy.deepcopy([event for event in self._events.values()
                              if (relation is None or event["relation"] == relation)
                              and (worker_id is None or event["worker_id"] == worker_id)
                              and (task_run_id is None or event["task_run_id"] == task_run_id)])

    def _resolved(self, reference: dict) -> dict:
        if reference.get("kind") == "skill_artifact":
            return {**reference, "resolution_status": "confirmed"}
        resolutions = {e["object"]["global_skill_id"] for e in self._events.values()
                       if e["relation"] == "resolved_to" and e["subject"] == reference}
        for event in self._events.values():
            if event["relation"] == "runtime_from" and event["subject"] == reference:
                linked = self._resolved(event["object"])
                if linked["global_skill_id"]:
                    resolutions.add(linked["global_skill_id"])
        if len(resolutions) > 1:
            raise ProvenanceIntegrityError("local version has conflicting global resolutions")
        return {**reference, "global_skill_id": next(iter(resolutions), None),
                "resolution_status": "confirmed" if resolutions else "unknown"}

    def get_task_context(self, task_run_id: str) -> dict:
        events = self.get_events(task_run_id=task_run_id)
        if not events:
            raise KeyError(task_run_id)
        def references(relation, endpoint):
            return [self._resolved(e[endpoint]) for e in events if e["relation"] == relation]
        return {
            "task_run_id": task_run_id, "worker_ids": sorted({e["worker_id"] for e in events if e["worker_id"]}),
            "retrieved": references("retrieved_from", "object"),
            "injected": references("injected_to", "subject"),
            "invoked": references("invoked_from", "object"),
            "invocation_status": "observed" if any(e["relation"] == "invoked_from" for e in events) else "unknown",
            "execution": [e["object"]["status"] for e in events if e["relation"] == "execution_status"] or ["unknown"],
            "injection_status": ([e["object"]["status"] for e in events if e["relation"] == "injection_status"]
                                 or ["unknown"]),
            "events": events,
        }

    def get_lineage(self, global_skill_id: str) -> dict:
        self.registry.get_skill(global_skill_id)
        relations = {"native_revision": [], "derived_from": [], "transferred_from": []}
        seen = set()
        for event in self._events.values():
            if event["relation"] in relations and event["evidence_status"] == "confirmed":
                subject, obj = self._resolved(event["subject"]), self._resolved(event["object"])
                key = (event["relation"], canonical_json(subject), canonical_json(obj))
                if key not in seen:
                    relations[event["relation"]].append({"subject": subject, "object": obj})
                    seen.add(key)
        edges = relations["native_revision"] + relations["derived_from"]

        def closure(start, reverse=False):
            reached, pending = set(), [start]
            while pending:
                node = pending.pop()
                for edge in edges:
                    left = edge["object" if reverse else "subject"]["global_skill_id"]
                    right = edge["subject" if reverse else "object"]["global_skill_id"]
                    if left == node and right not in reached:
                        reached.add(right)
                        pending.append(right)
            return reached - {start}

        ancestors, descendants = closure(global_skill_id), closure(global_skill_id, reverse=True)
        connected = ancestors | descendants | {global_skill_id}
        result = {relation: [edge for edge in items if edge["subject"]["global_skill_id"] in connected
                            and edge["object"]["global_skill_id"] in connected]
                  for relation, items in relations.items()}
        unresolved = {}
        for event in self._events.values():
            if event["relation"] == "revision_predecessor":
                child, parent = self._resolved(event["subject"]), self._resolved(event["object"])
                if child["global_skill_id"] in connected and parent["global_skill_id"] is None:
                    unresolved[canonical_json([child, parent])] = {"subject": child, "object": parent}
        result["unresolved_predecessors"] = list(unresolved.values())
        return {"global_skill_id": global_skill_id, "ancestors": sorted(ancestors),
                "descendants": sorted(descendants), **result}
