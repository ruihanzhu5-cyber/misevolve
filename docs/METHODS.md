# Methods

MisEvolve evaluates native skill-evolution implementations through a narrow adapter
contract. External code remains in independent checkouts and environments.

## Evaluated methods

| Method | Upstream | Pinned revision |
|---|---|---|
| AutoSkill | `ECNU-ICALK/AutoSkill` | `94c47ca488d4ba4117d20272e66d49b9877e68cf` |
| EvoSkill | `sentient-agi/EvoSkill` | `36f6f04952293d7054145550c2b9f0b0411bff1c` |
| SkillClaw | `AMAP-ML/SkillClaw` | `bf4dc2ee9430ecffb60e19630d26f57dfa2bd326` |
| SkillOpt | `microsoft/SkillOpt` | `57333f3406436a90a2b5feec4aad74ddb33d6e85` |
| SkillsVote | `MemTensor/skills-vote` | `86fd73915e9ea4b594ca0edbd8c3267109a8f976` |
| Hermes-native | `NousResearch/hermes-agent` | image-pinned runtime |
| No-Evolution | built in | control floor |

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
7. reload the frozen library for persistence probes.

The adapter translates schemas and paths. It does not reimplement the method's selection
or evolution algorithm.

## No-Evolution floor

No-Evolution never writes a skill. Its probe retrieval and carryover rates should remain
zero. A nonzero value indicates state leakage in a harness or runner.

## Hermes-native condition

Hermes-native exposes the framework's own skill and memory evolution and therefore runs
only with the Hermes harness. The framework-owned state remains episode-scoped and the
probe sessions still start clean.

## Governance wrappers

Utility-only and SafeEvolve wrap any common method after adapter construction. The wrapper
intercepts candidate admission and library reuse while preserving the native method's
trajectory collection, generation cadence, and base skill representation.

## Adding a method

1. Create `src/misevolve/methods/<name>.py` and implement the lifecycle.
2. Register the method in `src/misevolve/methods/__init__.py`.
3. Add a pinned checkout to `scripts/setup_methods.sh` when it is external.
4. Resolve paths through `misevolve.paths.method_checkout`.
5. Route method-internal inference through the selected backbone.
6. Add unit tests for native store discovery, retrieval, evolution, and reload.
7. Add the method to `configs/grid.yaml` only after a smoke cell succeeds.
