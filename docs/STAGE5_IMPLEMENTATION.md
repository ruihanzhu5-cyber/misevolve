# Stage 5 — Provenance Tracker implementation report

Date: 2026-11-07 (Asia/Shanghai).

## Verified starting point

- Repository: `ruihanzhu5-cyber/misevolve`.
- Branch: `xskill-mas-pilot`.
- HEAD verified before edits: `53bd0afc7a898afa7e8f0cbcb1faf9be021cdaa8`.
- Original base: `af74ba98d1fa7c2ed1931d660022cd748bba1399`.
- AutoSkill: `94c47ca488d4ba4117d20272e66d49b9877e68cf`, unchanged.
- Environment: CPython 3.12.13, Windows.

## Delivered behavior

`ProvenanceTracker` reads registry observations, transfer attempts/results, worker task
events, and worker evolution events. It produces an append-only normalized journal with
deterministic IDs, source references, source digests, endpoint references, and evidence
status. It exposes `sync`, `get_events`, `get_task_context`, and `get_lineage`.

The projection distinguishes transfer, retrieval, injection, invocation, native revision,
and explicit derivation. Failed imports remain attempts. Diagnostic native-search
visibility is never projected as a task retrieval or invocation.

Opt-in AutoSkill execution generates a task UUID before retrieval, flushes compact retrieval
references before agent execution, and records completion/failure separately. A raised
exception preserves already-written retrieval evidence. The UUID follows the trace into
native ingest and the native skill metadata. Task/provenance records do not copy prompts,
assistant/tool content, or complete skill bodies.

OpenClaw returns an injection manifest containing local ID, display name, actual directory
slug, and SHA-256 of copied file bytes. It attaches the manifest as `_skill_injection` in
raw trajectory metadata. Partial copy failures and missing/invalid manifests do not
establish confirmed injection. Injection is independent of task completion and invocation.

Registry evolution registration resolves exact retrieval versions and same-local-ID
predecessors, including verified native imports whose ID was adapted. Native revisions use
`native_revision_parent_id`; semantic parents remain independently typed. An unresolved
predecessor remains a compact local reference. New skills following retrieval gain retrieval
context but no automatic parent edge. Explicit derivation needs a registered parent and a
nonempty typed source reference.

Local observations and global resolution are separate immutable facts. Later registration
appends `resolved_to` without rewriting earlier events, and a rebuild produces the same
event set. Conflicting event contents, source rewrites/removals, and incomplete JSONL tails
are explicit errors. Complete-record interruption recovery is idempotent.

## Verification

Focused Stage 5 tests were run before the final full suite:

| Command/check | Actual result |
|---|---|
| `pytest -q tests/multiagent/test_provenance.py tests/multiagent/test_provenance_integration.py -o faulthandler_timeout=45` | 32 passed in 17.49s |
| `pytest -q -o faulthandler_timeout=45` | 105 passed in 26.21s, no skips |
| Existing / new cases | 73 / 32 |
| `ruff check .` | passed |
| `python -m compileall -q src docker` | passed |
| `git diff --check` | passed |

Commands used `.venv/Scripts/python.exe` and `.venv/Scripts/ruff.exe` locally. The
multiagent fixture blocks socket connect/connect_ex, connection creation, DNS lookup, and
the real Docker wrapper. Runtime manifest tests use in-process Docker/HTTP fakes; the
pinned real SDK uses local hashing and synthetic model responses. Real API calls, real
containers started, and real M tasks executed: all zero.

The tests cover all requested evidence boundaries, lookup/filter APIs, successful and
partial transfers, durable pre-execution retrieval, raised execution failure, successful
and missing manifests, duplicate runtime names, explicit invocation, rejected untyped
evidence, native revision, new-skill retrieval without derivation, unknown predecessors,
late reference resolution, source/event integrity conflicts, idempotent synchronization,
restart/rebuild equality, and content-free normalized logs.

The synthetic integration is:

```text
A creates S1 → B imports S1 → B retrieves/injects S1
    → B natively revises S1 into S2 → C imports S2
```

It verifies two confirmed transfers, one native revision, the correct original task
retrieval version, and no inferred semantic derivation or invocation. Separate tests
exercise positive explicit derivation/invocation evidence.

## Compatibility and limitations

- Original `harness.py`, `run.py`, P probe logic, Governance, worker initialization,
  transfer scheduling policy, dependency pins, and AutoSkill upstream are unchanged.
  AutoSkill logging remains opt-in. OpenClaw adds raw metadata without changing skill
  content, execution order, or session isolation; a single-agent native-run regression
  test verifies this, with Docker/HTTP replaced by fakes.
- Old artifact metadata loads through default values for the new model fields. Legacy
  revision declarations remain claims unless a native evolution journal corroborates
  them; they are not converted into semantic derivation.
- Explicit invocation/derivation APIs are trusted instrumentation/attestation boundaries.
  Independent referenced runtime evidence must be retained; there is no automatic
  detector or semantic proof checker.
- The single-writer assumption remains. Each sync scans retained evidence; concurrent
  writers, source-log rotation, automatic damaged-journal repair, and physical power-loss
  testing are outside this implementation.
- Query methods report the last synchronized projection. Unknown global identity,
  invocation, or unfinished execution stays unknown. No causal harm attribution or
  propagation-depth metric is claimed.
- Full 21-task orchestration, scheduling changes, Governance, real model behavior, and
  real sandbox/network validation remain deferred. Stop after Stage 5.

The detailed schema/API contract is [PROVENANCE.md](PROVENANCE.md). Local generated
verification artifacts and the review patch are under `results/engineering/stage5/`,
excluded from Git.
