# Proposal audit fixes and real benchmark pilot

This is a bounded engineering run on the checked-in SkillMisevo-Bench, not a
25-episode effectiveness study. Keep all prior arithmetic failures and spent
confirmation shards intact. Start a new run directory; do not resume old code.

The audit fixes enforce every final changed skill's development decision plus
the combined decision. HOLD dominates REJECT, which dominates ACCEPT. Repairs
rebuild and retest these units. A merge or dependent change is conservatively
tested as individual final store deltas; interacting changes cannot override
failed single units. This may reject useful coupled repairs, including repairs
to an unsafe skill already copied into multiple agents' stores.

Counterfactuals now execute the failed registered action, preserving receiver
delivery or the fixed E_ref successor process. Isolated-unit failures are marked
unavailable when the full library is not the same failure context. Interventions
still cover existing-version restoration only; other operation-level causal
claims remain unsupported. Prompt/condition matching does not eliminate model
sampling uncertainty and is not a statistical causal guarantee.

The fixed protocol caps changes per agent in generation and repair. Empty
effective changes consume no confirmation shard. Formal P and diagnostic frozen
evaluations both suppress positive transfer guidance; Store guards remain.
Evaluation records retain the actual execution failure in addition to missing
score status.

The first real M block returned more candidates than its declared limit of one,
so the run correctly held the update before any development or confirmation.
Generation/repair now expose that fixed limit prominently and allow at most one
structural correction request (`max_structure_retries=1`). Both responses remain
in the journal. No list is truncated, thresholds are unchanged, and a second
invalid response remains HOLD. This is format correction before evaluation, not
adaptive reuse of confirmation results. Start a fresh run for this code version.

Build the auditable source-episode partition offline:

```bash
python -m misevolve.improvement.benchmark_pilot --out /path/to/new-plan
python -m misevolve.team.cli run \
  --episode skillmisevo-coding-01 --protocol chain --replicate-id pilot-01 \
  --out /path/to/new-run --improvement fixed_theta \
  --improvement-config /path/to/new-plan/config.json \
  --verify-runtime --stop-after-pos 3
```

The complete online episode retains all 21 original tasks and text. The initial
checkpoint is after the first three original M tasks and one improvement window.
After inspecting the checkpoint, use the same command with `--resume` and
`--stop-after-pos 6` to exercise the first B block. Remove the stop flag on a later
clean resume to finish the remaining tasks including frozen P.

Development uses episode 02 B04. Six independent *source-episode* confirmation
shards use episodes 03–08 B04, one per window. Episode 09 B04–B06 and episode 10 B04
are reserved final pools, not automatically evaluated. Manifest v2 forbids a
source episode appearing in different purposes or different sealed shards.
The original dataset SHA-256 and all unchanged task IDs are recorded in plan.json.

This is one topology/episode/replicate, empirical admission, min_units=1,
max_changes_per_agent=1, and at most one repair. It exercises behavior_retest and
cross_receiver, not adaptive outer retention. Episode separation does not prove
concept, template, or lineage isolation. No safety confidence or generalization
claim follows from this run. Run only through the existing isolated Docker
factory; the adversarial task text must never execute on the host.

Remaining proposal work includes broader online history feedback, operation-level
counterfactuals, conditional validators, independent meta-sequence handling,
full controlled baselines/ablations, and final paired statistics.
