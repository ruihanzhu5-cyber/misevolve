# Stage 5 provenance contract

`ProvenanceTracker` projects original journals without executing agents or calling models.
It retains the Stage 2–4 single-writer assumption. Full episode scheduling is deferred.

## Inputs and API

```text
<run>/registry/registry.jsonl
<run>/registry/transfer_events.jsonl
<run>/workers/<worker>/task_events.jsonl
<run>/workers/<worker>/evolution_events.jsonl
<run>/provenance/provenance_events.jsonl       # rebuildable output
```

An explicitly supplied registry may live elsewhere. `evidence_source` is relative to the
run root, allowing evidence archives to move without encoding machine-specific paths.

```python
from pathlib import Path
from misevolve.multiagent.provenance import ProvenanceTracker

tracker = ProvenanceTracker(Path("results/synthetic-run"))
new_events = tracker.sync()  # includes bookkeeping evidence, not just graph edges
events = tracker.get_events(relation="retrieved_from", worker_id="B")
context = tracker.get_task_context(task_run_id)
lineage = tracker.get_lineage(global_skill_id)
```

The tracker refreshes registry evidence at synchronization. A second sync of unchanged
sources appends nothing. Queries read the current synchronized projection; they do not
silently execute synchronization. Unknown task/artifact queries raise `KeyError`.

## Independent evidence relations

| Relation | Direction and confirmation rule |
|---|---|
| `transferred_from` | Receiver local version → source global artifact; success, integrity, diagnostic visibility, and source identity must all verify |
| `retrieved_from` | Task run → local version captured by the task's SDK search, flushed before execution |
| `injected_to` | Exact runtime skill → task run; requires a successful, complete runner manifest |
| `invoked_from` | Task run → local version; requires explicit invocation instrumentation with a nonempty source reference |
| `native_revision` | New global artifact → exact predecessor; corroborated by same-local-ID native before/after snapshots |
| `derived_from` | Child global artifact → registered parent; requires `explicit_derivation` evidence and a nonempty source reference |

Failed/partial transfers are attempts, not confirmed propagation. Stage 4 diagnostic
search never establishes task retrieval. Injection never establishes invocation. Generic
tool calls, skill-name matches, assistant statements, and temporal proximity establish
neither invocation nor derivation.

Explicit invocation instrumentation can call
`method._exchange.record_invocation_evidence(task_run_id, local_id, source_ref=...)`.
This API is a trusted evidence boundary for future runtime instrumentation, not a detector
and not an invitation to infer use from text. No current runner automatically calls it.
The referenced independent runtime evidence must be retained outside the projection.

## Local identity and global resolution

Local references contain worker ID, native local ID, and the observed content hash. They
do not use a current mutable Local→Global map to attribute an earlier task. `resolved_to`
binds that exact local version to a registered global artifact. Imported references also
require verified transfer history and an exact native-ID-adapted content hash.

`reference_observed` records the original compact reference. `runtime_from` binds a runtime
manifest item to the version supplied by the matching task retrieval. The runtime hash is
SHA-256 of the actual copied file bytes; it differs in meaning from the canonical registry
bundle hash. Duplicate display names retain distinct final runtime directory slugs.

These auxiliary edges are intentionally separate. An initially unknown reference remains
an immutable fact; a later registration appends a resolution. Fresh rebuild and incremental
synchronization produce the same event set. `get_task_context()` joins these facts and
returns `global_skill_id=None` and `resolution_status="unknown"` when unresolved. A
confirmed retrieval fact does not by itself confirm its global identity.

## Native revision and semantic parentage

`SkillArtifact.native_revision_parent_id` and `unresolved_predecessor` extend the existing
model with defaults compatible with earlier stored artifacts. Native updates populate the
revision field only when the exact predecessor resolves. `retrieved_parent_ids` are
independently resolved; `invoked_parent_ids` remain empty without explicit evidence.
New skills created after retrieval do not acquire semantic parents from that context.

For Stage 2–4 compatibility, the registry still reads older `parent_skill_ids` entries typed
as `native_revision`. Their declaration is projected as `native_revision_claim`, not a
confirmed edge. Corroborating evolution snapshots establish the confirmed native revision.
Explicit derivation is confirmed only for a registered parent with
`parent_evidence.type="explicit_derivation"` and a valid `source_ref`.

`get_lineage()` separates transfer, native revision, and explicit derivation. It returns
ancestors, descendants, and unresolved predecessors without inventing global IDs. It
deduplicates graph edges while `get_events()` retains each independent evidence record.
No causal harm attribution or propagation-depth metric is implemented here.

## Persistence and integrity

Every normalized event has `schema_version`, deterministic `event_id`, `relation`,
`subject`, `object`, `worker_id`, `task_run_id`, `evidence_source`, `source_event_id`,
`source_event_kind`, `evidence_status`, and compact evidence metadata. IDs derive from
source identity, relation, and endpoint references. Transfer start and completion share an
attempt ID but have distinct source kinds. Legacy records without IDs use stable row IDs;
legacy evolution without a task UUID does not receive an invented one.

`source_record` contains a digest of original evidence to detect rewrites even when a
mutation changes an endpoint. Different content under an existing event ID raises
`ProvenanceIntegrityError`. Synchronization validates all candidates before appending;
restart resumes after any complete appended record. Missing prior source records and
incomplete JSONL tails fail explicitly. Automatic journal repair and concurrent writers
remain unsupported.

Projection records never duplicate prompts, assistant/tool content, full skill bodies, or
native metadata snapshots. Original evolution journals retain the Stage 2–4 snapshots and
remain the source evidence. The normalized journal is a cache and can be rebuilt into a
new `output_dir` from the retained original journals.
