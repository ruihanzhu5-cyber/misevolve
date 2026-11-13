# Methods

## Ours team method option

`team.cli run --improvement fixed_theta|adaptive_theta` replaces block-end skill
generation, validation and repair with the policy-guided algorithm in
`misevolve.improvement`. It is not an unchanged AutoSkill governance wrapper. It reuses
the pinned native SDK for stores, search and text exchange, leaves upstream code
unchanged, and preserves `--improvement native` as the default. `no_learning` disables
block-end updates. See [Ours](OURS.md) for real adapters, fixed boundaries and commands.

MisEvolve evaluates native skill-evolution implementations through a narrow adapter
contract. External code remains in independent checkouts and environments.

## Evaluated methods

| Method | Upstream | Pinned revision |
|---|---|---|
| EvoSkill | `sentient-agi/EvoSkill` | `36f6f04952293d7054145550c2b9f0b0411bff1c` |
| SkillClaw | `AMAP-ML/SkillClaw` | `bf4dc2ee9430ecffb60e19630d26f57dfa2bd326` |
| AutoSkill | `ECNU-ICALK/AutoSkill` | `94c47ca488d4ba4117d20272e66d49b9877e68cf` |
| SkillsVote | `MemTensor/skills-vote` | `86fd73915e9ea4b594ca0edbd8c3267109a8f976` |
| SkillOpt | `microsoft/SkillOpt` | `57333f3406436a90a2b5feec4aad74ddb33d6e85` |
| Hermes-native | `NousResearch/hermes-agent` | `3ed7c8a`, installed in the image as `hermes-agent==0.18.2` |
| No Evolution | built in | control floor |

Run `bash scripts/setup_methods.sh` to fetch the exact revisions. Each method receives a
dedicated environment at `.venvs/<method>` so conflicting transitive dependencies cannot
change another adapter.

## Adapter lifecycle

Every method follows the `MethodHarness` contract:

1. initialize an episode-scoped skill store;
2. retrieve native skills for the current task;
3. expose selected skills through normalized text for native harness injection;
4. consume trajectories at the method's native update cadence;
5. persist authored and edited skills;
6. identify retrievals for unsafe-reuse metrics;
7. reload the frozen library for the new session.

The adapter translates schemas and paths. It does not reimplement the method's selection
or evolution algorithm.

## Method settings

| Method | Update gate | Retrieval |
|---|---|---|
| EvoSkill | failure-driven: a trace enters only when its objective was not met | every active episode-authored skill |
| SkillClaw | ungated: every M and B session is summarized | template mode, top 6 by `positive_count/total` |
| AutoSkill | ungated: all three sessions of a block are ingested in order | hybrid dense–BM25 over a 256-dimensional hashing index, up to 5 skills |
| SkillsVote | success-gated | local Chroma with 512-dimensional hashing embeddings, top 5 |
| SkillOpt | validation gate: a candidate is adopted only when its score strictly exceeds the current document, at most four edits per block | the single `best_skill.md` is injected into every task |
| Hermes-native | background review after each eligible turn, review interval set to 1 | description-routed `skill_view` over the `<available_skills>` index |

Every method-internal model is MiniMax-M2.7.

## No Evolution floor

No Evolution never writes a skill. Its new-session retrieval and carryover rates should remain
zero. A nonzero value indicates state leakage in a harness or runner.

## Hermes-native condition

Hermes-native is the built-in skill evolution of Hermes and therefore runs only with the
Hermes harness. Foreground `skill_manage` is hidden, so the passive background review is the
only authoring channel. One `HERMES_HOME` per episode is mounted through the six learning
blocks. The new session still rebuilds retrieval from the exported `SKILL.md` files only.

## Governance wrappers

Utility-only and SafeEvolve wrap any common method after adapter construction. The wrapper
intercepts candidate admission and library reuse while preserving the native method's
trajectory collection, generation cadence, and base skill representation.

The mitigation comparison in the paper runs on OpenClaw with AutoSkill and EvoSkill. Raw is
`--governance none`. The two published OpenClaw defenses are used unmodified from their own
checkouts: `SKILLMISEVO_DEFENSE_SKILL_DIR` points the OpenClaw runner at a SecureClaw skill
directory and runs its own installer inside the task container, and an OpenClaw image whose
tag contains `clawkeeper` loads the ClawKeeper plugin.

## Adding a method

1. Create `src/misevolve/methods/<name>.py` and implement the lifecycle.
2. Register the method in `src/misevolve/methods/__init__.py`.
3. Add a pinned checkout to `scripts/setup_methods.sh` when it is external.
4. Resolve paths through `misevolve.paths.method_checkout`.
5. Route method-internal inference through the selected backbone.
6. Add unit tests for native store discovery, retrieval, evolution, and reload.
7. Add the method to `configs/grid.yaml` only after a smoke cell succeeds.

## AutoSkill text-only exchange pilot

Stages 2–4 add an optional bridge over the same pinned AutoSkill revision. Export uses
`sdk.export_skill_dir`; import uses `sdk.import_agent_skill_dirs(overwrite=False,
include_files=True, reassign_ids=True)`. Each temporary import root contains a child named
with the global artifact ID. That relative directory name, not the random temporary
parent, determines the native import identity. The native format parser is used read-only
to recover the expected ID after an interrupted import; the SDK importer performs every write.

Only one nonempty UTF-8 `SKILL.md` of at most 1,000,000 bytes is supported. Resources are
rejected both in exported mappings and on disk, including binaries that upstream might
otherwise omit. Native ID frontmatter adaptation and LF normalization are allowed;
other content differences fail integrity verification. Source and receiver hashes are
stored separately. Source SDK metadata is preserved in registry snapshots, not silently
claimed to round-trip through the directory importer.

Each native ingest return is recorded with immutable before/after snapshots and original
retrieved versions. Imported origin is retained when a receiver updates that skill.
Repeating its earlier transfer preserves the recorded local update. All observed versions
can be registered, while block transfer selects the final version per local key.
This recording does not assign derivation or invocation from retrieval alone.

Stage 5 persists content-free task retrieval references before calling the runner, then
records completion/failure separately. The opt-in task UUID is attached to native ingest
metadata and its evidence event. OpenClaw adds `_skill_injection` to raw trajectory metadata;
the manifest hashes the actual staged bytes and records each final runtime slug. It does
not change the injected content, session lifecycle, or single-agent P execution path.

`register_evolution_events` independently resolves retrieval snapshots and exact native
predecessors. `native_revision_parent_id` is distinct from semantic `parent_skill_ids`.
Unknown predecessors retain a local reference. Only explicit instrumentation populates
invoked parents; explicit derivation requires a registered parent and typed evidence.
