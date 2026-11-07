# Stage 6 implementation

`multiagent.episode` owns validation, fixed scheduling, task rows, batch checkpoints
and lifecycle coordination. `multiagent.episode_metrics` uses the existing benchmark
predicates and artifact safety threshold to report the condition and its workers.
The existing worker pool, native AutoSkill adapter, version registry, transfer manager
and provenance tracker supply the underlying operations. The tracker exposes verified
transfer hops for both lineage queries and episode metrics.

The Stage 5.1 prerequisite adds a durable injection intent ahead of every exchange-enabled
runner call. It validates exact manifest coverage, resolves imported versions through
verified transfers, and retains transfer hops independently of artifact ancestry.
Stage 5 normalized output uses an older schema. Rebuild it in a fresh projection
directory with the original source journals. Historical runs without intent retain
unknown injection coverage. See [the provenance evidence contract](PROVENANCE.md).

The Stage 6 synthetic integration loads `skillmisevo-coding-01`, executes 21 distinct
mock sessions, evolves six batches and checks three native transfers and three frozen
probes. It verifies all original task metadata, exact intent coverage, complete
native revision evidence, provenance reconstruction, row compatibility and summary
redaction. Additional tests exercise invalid input, runner failures, empty transfer
selection, and fatal native ingest, registry and transfer failures.

The pilot uses one controlled serial contamination chain. The episode is opt-in via
Python with explicit judges, requires a fresh output directory and has no full episode
resume. The supported payload is a text-only `SKILL.md`. The existing OpenClaw
runner supplies tool-call counts and leaves usage and costs unavailable. Verification
covers synthetic sessions with the pinned local SDK. Real container and model behavior
remain outside that evidence. See [the episode contract](MULTIAGENT_EPISODE.md) and
[verification commands](REPRODUCIBILITY.md).
