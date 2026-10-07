# Architecture

MisEvolve separates evolution, execution, governance, and evaluation so each result has
a clear causal owner. A cell is one episode × method × harness combination under one
backbone.

## System boundary

```text
episode JSON
    │
    ▼
episode runner ──retrieve──▶ method-owned skill store
    │                              │
    │                         governance
    │                       audit / repair
    │                     retire / evict
    ▼                              │
fresh container ◀────native skill injection
    │
    ▼
agent trajectory ───────▶ behavioral judge
    │
    └──evolve──▶ candidate artifacts ─────▶ artifact judge
                                      │
                                      ▼
                              all_rows.json
                                      │
                                      ▼
                         fixed-denominator metrics
```

The persistent skill library is the only cross-task state. Every executor task receives a
fresh container, working directory, framework home, and model session. This prevents chat
history, filesystem residue, framework memory, and process state from masquerading as
skill persistence.

## Episode lifecycle

The default episode is `MMM BBB MMM BBB MMM BBB | PPP`.

1. Load nine malicious tasks, nine related benign tasks, and three benign tasks for the new session.
2. Retrieve skills from the current method library.
3. Inject the selected skills through the framework's native loading path.
4. Execute one task in a disposable container and capture the trajectory.
5. Score harm and legitimate completion with the behavioral judge.
6. After each three-task block, ask the method to evolve its library.
7. Score each new or revised skill version for content unsafety, generalization, and stealth.
8. Apply governance admission, delete-only repair, retirement, and capacity decisions.
9. Freeze the final library and score its exported skills again before the new session.
10. Run the three new-session tasks in a clean executor without further evolution.
11. Write task and artifact rows to `all_rows.json`.

`misevolve.bench.schedules` changes exposure timing and within-batch mixing while preserving
nine malicious and nine benign tasks before the new session. Mixed schedules rotate and reverse across
episode indices to prevent a fixed position from defining the result.

## Component ownership

### Runner

`misevolve.run` resolves configuration, builds one method and one agent runner, applies an
optional governance wrapper, loads an episode, and invokes the episode state machine.

### Episode state machine

`misevolve.harness` owns task order, batching, library freezing, outcome rows, artifact
rows, and the separation between working state and persisted method state.

### Agent adapters

`misevolve.adapters` translates one normalized task specification into the framework's
container protocol. Each image reads JSON from stdin and writes a trajectory into a
dedicated output mount. Method skills enter through the framework's native path:

| Harness | Injection path |
|---|---|
| Claude Code | `.claude/skills` |
| Codex | `AGENTS.md` |
| OpenClaw | `.openclaw/skills` |
| Hermes | `.hermes/skills` |

### Method adapters

`misevolve.methods` exposes a common lifecycle over native implementations:

- retrieve relevant skills for a task;
- evolve from a completed batch;
- persist and reload the episode library;
- identify authored or edited artifacts;
- report retrieval evidence for the new-session metrics.

External repositories stay outside the Python package. `scripts/setup_methods.sh` checks
out exact revisions and creates an isolated environment per method, preventing dependency
conflicts from changing another condition.

### Judges

The behavioral judge scores task-level harm and utility. The artifact judge scores authored
skills. The original group uses Gemini-3-Flash for trajectories and Kimi-K2-0905 for skills.
The independent group uses Grok-4.5 for both roles. Judges are configured outside the
backbone axis and are rejected at startup if either matches the evaluated backbone.

### Aggregator

`misevolve.bench.metrics` computes absolute micro-aggregates. The denominator is the full
eligible task set for each rate, so a missing run or missing verdict contributes zero. The
bootstrap resamples whole episode cells and preserves within-episode dependence.

## SafeEvolve

SafeEvolve wraps a native method at two boundaries.

### Write-time governance

1. Build a candidate bundle with content, native metadata, source exposures, and ancestry.
2. Audit the complete bundle with a non-blocking critic that returns risk with unsafe spans.
3. Check that a revision preserves verification requirements inherited from its ancestor.
4. Let a paired deleter delete or narrow the flagged spans, for at most two rounds.
5. Re-audit each repair and keep it only when it stays valid with lower risk. Otherwise keep
   the native candidate. Reject only structurally invalid content.
6. Persist audit evidence and lineage with the skill.

### Reuse-time governance

1. Track successful, failed, and unsafe reuse events.
2. Combine frozen provenance risk with accumulated reuse evidence.
3. Retire skills that exceed safety or utility thresholds.
4. Enforce capacity by evicting the skill with the lowest utility minus risk.
5. Preserve governance metadata across the frozen-library reload.

The Utility-only control uses the same hooks, budget, capacity, and cadence without safety
evidence. SafeEvolve ablations remove the paired deleter, reuse attribution, or safety
retirement while leaving the surrounding lifecycle unchanged.

The evaluated configuration attempts at most two delete–audit rounds, runs maintenance
after each update block, retires a skill after two harmful reuses or at effective risk of
at least 0.6, applies utility retirement below 0.35 after two observations, and limits the
active library to 32 skills.

## Configuration flow

`misevolve.paths` resolves the repository, configuration directory, and external method
checkouts. Environment variables can relocate each boundary without changing source code.
The configuration files define axes, while CLI flags select one point in the matrix.

```text
configs/backbones.yaml ─┐
configs/harnesses.yaml ─┼─▶ resolved AgentConfig
configs/judges.yaml ────┘
configs/grid.yaml ─────────▶ batch runner matrix
configs/safeevolve*.json ─▶ governance policy
```

## Failure semantics

Container errors and judge failures remain visible in the row record. Rate aggregation
uses fixed eligible-task denominators and contributes zero for missing outcomes. This
prevents unstable methods from improving their reported rate by producing fewer judgments.
Artifact judgments are retried, and one that is still missing stays undefined and outside
the artifact scores.

## Extension rules

- A new harness must preserve fresh-task isolation and native skill injection.
- A new method must keep its own store within the episode directory and expose retrieval.
- A new governance policy must wrap the common lifecycle without rewriting native methods.
- A new judge must differ from the backbone and emit a parseable raw record.
- A new schedule must preserve the task multiset and keep the new session evolution-free.

The executable version of these rules lives in `tests/` and the AI-agent change protocol
lives in [`AGENT.md`](../AGENT.md).
