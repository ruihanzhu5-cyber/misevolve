# MisEvolve → XSkill-MASBench
## Code Modification Specification v1.1

**Experiment:** Multi-Agent Clean Receiver Pilot

**Updated:** 2026-11-07

**Working repository:** `ruihanzhu5-cyber/misevolve`

**MisEvolve base:** `af74ba98d1fa7c2ed1931d660022cd748bba1399`

**Development branch:** `xskill-mas-pilot`

**AutoSkill:** `ECNU-ICALK/AutoSkill@94c47ca488d4ba4117d20272e66d49b9877e68cf`

This revision supersedes v1.0. The Stage 2–4 foundation contract is retained below.
The authorized follow-up ends after Stage 6 synthetic verification, as recorded in section 8.
The original specification supplied with the request is the design source; this
document incorporates the decisions accepted after Stage 1 inspection.

## 1. Decisions changed or clarified in v1.1

1. Register **every observed version** produced within a block. Cross-agent transfer
   selects **only the final version of each local skill key in that block**. Selection
   is independent of harm judgments. A later return to an earlier content hash still
   counts as the final version; deduplication must not change selection order.
2. P tasks retain original MisEvolve semantics: freeze the complete library, inject it
   directly into a fresh executor session, and never evolve. This is **not** an
   AutoSkill native retrieval event. Implementing P execution is outside Stage 2–4.
3. Pilot v1 supports exactly one UTF-8 text file, `SKILL.md`, per artifact. Additional
   resources, binary content, oversized content, or malformed artifacts cause an explicit
   unsupported/error result. Never silently discard resources. Registry metadata is a
   sidecar, not an imported resource. Preserve all supported native metadata separately.
4. `transferred_from`, `retrieved_from`, `invoked_from`, and `derived_from` are distinct
   relationships. Retrieval is not invocation or derivation. Unknown evidence remains
   unknown. This phase stores evidence fields but does not implement a provenance graph.
5. Distinguish Imported, Locally Created, and Locally Updated skill versions. A local
   update of an imported skill retains its imported origin and source global ID.
   Enumerating `authored_skills()` alone does not establish local authorship.
6. Capture each `sdk.ingest()` result immediately, including returned skill records,
   immutable before/after snapshots, task identity, retrieved versions, and errors.
   Capture intermediate versions before the next ingest can replace them.
7. Preserve the original single-agent path. Exchange tracking is opt-in. Do not modify
   AutoSkill upstream, Governance, the original episode JSON, or original metrics.

## 2. Architecture and isolation

Four separately constructed workers A/B/C/D each own an OpenClawRunner, AutoSkill
MethodHarness, SDK, LocalSkillStore, evolution state, episode directory, task workspace,
and sessions. Sharing read-only Python code and a common backbone is permitted.
Sharing SDK objects, store roots, writable framework homes, or conversation history is not.

Worker roots are `<run>/workers/<id>/`; native stores are `autoskill_store/` beneath each.
Fresh initialization refuses nonempty worker directories. Resume is explicit and restores
native skill contents and durable exchange mappings, not conversation history or SDK
maintainer internals. Initialization failure cleans up the currently initializing worker
and all earlier workers; cleanup attempts continue if another teardown fails.

The Stage 2 `MultiAgentOrchestrator` is only a worker lifecycle manager. It must not
implement or execute a complete episode, scheduling loop, model calls, or M tasks yet.

## 3. Global Skill Registry

Store immutable artifacts under `registry/artifacts/<global_skill_id>/` with `SKILL.md`
and `metadata.json`; use append-only `registry.jsonl` for observations and
`transfer_events.jsonl` for transfer attempts/results. Never replace an existing artifact.

SkillArtifact includes global ID, owner agent, local key, native version, content hash,
native skill metadata, parent/retrieved/invoked IDs, source task IDs, transformation type,
safety status, and origin. Parent IDs require independently supplied evidence.

Compute SHA-256 from a canonical path→text mapping (sorted keys, UTF-8, LF line endings).
Compute global identity from an unambiguous canonical encoding of owner, local key, and
content hash. Registry operational sidecars are excluded from the content hash.
Identical owner/key/content deduplicates; different content preserves a new artifact.
Record each block observation separately so final-version selection and a content reversion
remain correct even when the artifact itself already exists. No judgment-based selection.

Recovery validates recorded IDs/hashes and artifact manifests, and fails explicitly on
corruption. A final incomplete JSONL record is an error, not silently accepted data.
An uncommitted artifact left before its JSONL record may be adopted only after exact
validation against the new registration. Initial implementation is single-writer;
concurrent independent writers to one registry or worker are unsupported.

## 4. Native AutoSkill exchange bridge

Export through official `sdk.export_skill_dir(local_id)`; validate exactly `SKILL.md`.
Import through official `sdk.import_agent_skill_dirs` using:

```python
overwrite=False
include_files=True
reassign_ids=True
```

Use a unique temporary parent containing `<global_skill_id>/SKILL.md`. Pass the parent
as `root_dir`; the stable relative name must be the global ID, never `.` or a random name.
The pinned importer derives a deterministic UUID from user ID and that relative name.
Read the returned skill ID; on recovery use the same official importer and verify existing
contents rather than directly manipulating its records or vector index.

Persist source global ID → receiver local ID mappings, original receiver snapshot,
source owner/local key, native ID adaptation, integrity evidence, and diagnostic search
result. Same global ID must not create a duplicate receiver skill after retry or restart.
Different versions use different global IDs and can coexist as independent imports.
Do not replace a receiver's locally evolved version when retrying an earlier import.

Native import rewrites the YAML `id`. Keep source and receiver raw/canonical hashes
separate. Integrity permits only the pinned SDK's ID frontmatter adaptation and LF
normalization; verify the remainder against the exact documented transformation.
Always verify receiver `get`/`list` and official `search` visibility. Diagnostic search
is logged as diagnostic, never as task retrieval or invocation evidence.

Import success and native-retrieval verification are separate event fields. If post-import
verification fails, retain enough durable state to recover without a duplicate import;
report failure explicitly. Empty transfer is recorded and does not manufacture a skill.

## 5. Clean receiver execution (Stage 6)

The `chain_clean_receiver_v1` schedule preserves all 21 original task definitions
and source positions. For the first published episode, it executes the following batches in order.

| Source positions | Kind | Worker | Transfer after batch evolution |
|---|---|---|---|
| 1–3 | M | A | none |
| 7–9 | M | A | none |
| 13–15 | M | A | A → B, final version per changed local key across all three M batches |
| 4–6 | B | B | B → C, final version per changed local key |
| 10–12 | B | C | C → D, final version per changed local key |
| 16–18 | B | D | none |
| 19 | P | B | none; no evolution |
| 20 | P | C | none; no evolution |
| 21 | P | D | none; no evolution |

A handles all exposure. B, C and D handle clean tasks and probes. Ingest completed
trajectories in source-position order after each learning batch. Imported skill state
is the only cross-agent channel. Keep complete failure rows and distinguish unknown
causal attribution. The plan records both source order and execution order.
See [the episode contract](docs/MULTIAGENT_EPISODE.md).

## 6. Development stages and tests

| Stage | Scope | Required evidence |
|---|---|---|
| 1 | inspection | completed report |
| 2 | worker initialization and cleanup | four isolated stores, failure cleanup, mock runner |
| 3 | registry, all versions, final-per-key selection | hash, dedup, immutable history, restart recovery |
| 4 | native export/import bridge | idempotency, ID mapping, integrity, native retrieval, resources, restart |
| 5 | provenance tracker | typed evidence, honest unknowns, no retrieval-derived inference |
| 6 | complete orchestrator and independent metrics | 21-task mock episode, P semantics, complete logging |
| 7 | real isolated pilot | only after container/network checks and separate authorization |
| 8 | final regression | original and added checks |

Stage 2–4 tests use synthetic skills and Mock Runner; SDK integration tests use the pinned
real AutoSkill SDK with local hashing embeddings. Block outbound network in these tests.
Mock only the LLM response for ingest tests; exercise native maintenance/store behavior.
No Docker launch, credentials from personal workloads, real model API calls, or M tasks.
Do not silently skip native integration tests when the pinned dependency is missing.

After each implemented stage run its focused tests. At the end run:

```bash
pytest -q
ruff check .
python -m compileall -q src docker
git diff --check
```

## 7. Stage 2–4 deliverables and non-goals (historical scope)

Deliver the v1.1 specification, Stage 2–4 code, synthetic/native integration tests,
reviewable diff, exact verification results, dependency pin, and known limitations.
Pause after Stage 4. Do not implement complete Orchestrator execution, full provenance
graph, multiagent metrics, Governance, trust scoring, alternative topologies, memory
sharing, forced rewrite/composition experiments, or a multi-method grid this round.

## 8. Stages 5–6 follow-up authorization

The separately supplied Stage 5 task authorizes provenance tracking on top of commit
`53bd0afc7a898afa7e8f0cbcb1faf9be021cdaa8`. The next task authorizes Stage 5.1 hardening
and Stage 6 synthetic verification on top of
`67692583cb327fa7b920b5e1cfed8220e4adec19`. Stage 6 ends at the complete mock episode,
with real experiments and Governance reserved for separate work. See
[`docs/PROVENANCE.md`](docs/PROVENANCE.md) and
[`docs/STAGE6_IMPLEMENTATION.md`](docs/STAGE6_IMPLEMENTATION.md).
