# XSkill-MASBench Stage 2–4 implementation

Date: 2026-11-07 (Asia/Shanghai).

## Repository and scope

- Working fork: `ruihanzhu5-cyber/misevolve`.
- Branch: `xskill-mas-pilot`.
- Base: `af74ba98d1fa7c2ed1931d660022cd748bba1399`.
- AutoSkill checkout: `94c47ca488d4ba4117d20272e66d49b9877e68cf`, unchanged upstream code.
- Specification: [v1.1](../CODE_MODIFICATION_SPECIFICATION_v1.1.md).
- Python used for verification: CPython 3.12.13, Windows.

This delivers worker initialization, a version registry, and the native text-only exchange
bridge. There is no complete episode orchestrator, model-backed pilot, new CLI, metrics
implementation, provenance graph, or Governance modification.

## Implemented behavior

### Stage 2

`MultiAgentOrchestrator.setup_workers()` constructs four independent OpenClawRunner,
AutoSkill method, SDK, Maintainer, and LocalSkillStore instances. Each lives beneath its
own worker directory. Initialization does not start containers or run a task. Partial
initialization failures clean up the current worker and all prior workers even if an
individual cleanup fails. Nonempty roots require explicit resume. Resume validates native
skills referenced by persisted identity mappings.

### Stage 3

`SkillArtifact` preserves owner, local/global IDs, native version, content hash, complete
JSON-compatible native skill records, origin, source tasks, and separate evidence fields.
`GlobalSkillRegistry` writes immutable artifact files and append-only JSONL observations.
Content identity is canonical UTF-8/LF text, separate from native/operational metadata.
Repeated identical content deduplicates its artifact but still records the observation.
Therefore a block's final version per local key is correct even after a content reversion.
Restart checks manifests and hashes; corruption and incomplete JSONL records fail explicitly.

### Stage 4

The method's `enable_skill_exchange()` opt-in exposes `export_skill_bundle()` and
`import_external_skill()`, with atomic exchange checkpoints. Official AutoSkill export/import
APIs handle skill contents. A temporary parent has a stable global-ID child; the pinned
native format parser supplies the expected import ID read-only for interruption recovery.
No bridge writes vector/BM25 records or directly modifies native skill directories.

Source and receiver artifact hashes remain distinct because native import adapts frontmatter
ID. Exact expected native adaptation is checked, with other changes rejected. Native get/list
and search visibility are verified separately. Diagnostic search is identified as diagnostic;
an independent test also exercises the normal adapter top-k retrieval path.

Every native ingest return and its before/after snapshots are flushed before the next ingest.
The registry can register each changed snapshot, while transfer selects only the final version
per local key. Imported origin survives a local update and a restart. Retrying an older
import does not overwrite that update. Retrieved snapshots retain the version present before
evolution; no invocation or derivation is inferred from them.

Only `SKILL.md` is supported. Extra text/binary resources, invalid text, and oversized input
produce explicit errors. Original P probes still inject the complete frozen library directly
without native retrieval or evolution; a synthetic regression test covers that behavior.

## Verification results

All results below were obtained locally without Docker or real model calls.
The new tests block network connections and Docker execution, exercise the real pinned
SDK, and replace only model responses when testing native ingest.

| Check | Result |
|---|---|
| Stage 2 focused tests | 5 passed |
| Stage 3 focused tests | 6 passed |
| Full suite: `.venv/Scripts/python.exe -m pytest -q -o faulthandler_timeout=45` | 73 passed in 17.76s |
| Original / new test cases | 37 / 36, no skips |
| `.venv/Scripts/ruff.exe check .` | passed |
| `.venv/Scripts/python.exe -m compileall -q src docker` | passed |
| `git diff --check` | passed |
| Protected original paths and upstream AutoSkill tracked diff | empty |

Covered cases include four-store isolation; partial initialization cleanup; deduplication;
immutable historical versions; final-per-key transfer; imported versus local origins;
normal native retrieval; repeated imports; local/global mapping; text/resource restrictions;
receiver tampering; import adaptation integrity; get/list/search checks; search failures;
interruption after native upsert; interruption before worker checkpoint; two pending imports
with identical text; and native-store/registry reload with locally evolved imported skills.

Generated patch, package versions, and machine-readable verification records are retained
locally under `results/engineering/stage2-4/` (excluded from Git).

## Known limits and deferred work

1. Registry and exchange directories have a single writer. Concurrent processes targeting
   the same state are unsupported; cross-process locking is not implemented.
2. An incomplete JSONL tail fails recovery explicitly. Automatic journal repair is not
   implemented, and physical power-loss behavior has not been tested.
3. Resume restores skill contents and durable exchange state, not upstream in-memory
   maintainer hints, model sessions, or complete episode execution state.
4. Import/export and ID adaptation depend on the pinned native format. Source metadata is
   preserved in registry sidecars, not fully transferred by the SDK directory importer.
5. Diagnostic search proves store visibility, not general retrieval quality or invocation.
   A provenance graph, causal attribution, and metrics remain Stage 5–6 work.
6. Real container isolation, endpoint compatibility, egress restrictions, and real model
   behavior have not been tested. No M tasks or model-backed tasks were run.
7. The lifecycle manager intentionally has no `run_episode()` implementation. Stop after
   Stage 4 and wait for the next instruction.
