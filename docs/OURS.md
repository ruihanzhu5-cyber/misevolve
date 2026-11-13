# History-driven skill-library improvement policy optimization

This opt-in research prototype implements a versioned policy theta = (gen, val, rep).
It freezes the base model and changes text instructions and registered development
actions. It does not train weights or optimize spending. An improved library is not
itself evidence of an improved policy. Policy retention compares libraries actually
produced by two frozen policies from one identical starting library.

## Baseline and implementation map

The exact development base is `4cc377300be179d71d72888347dc3e5ff1132a2d`, on branch
`scope-ours`. It includes the six operational fixes following stage7. Its child
`ffe2bd0c995a2ce45ef63afd5f1a97834ed91b0a` introduced the retired SCOPE governance
algorithm; that commit is not an ancestor of this implementation. The name of the
working branch does not identify an inherited governance algorithm.

| Responsibility | Implementation |
| --- | --- |
| Immutable policy/hash, fixed protocol | `improvement/policy.py` |
| Purpose permissions, deduplication, access log | `improvement/data.py` |
| Content-addressed candidates/history/snapshots, pending transaction fence | `improvement/store.py` |
| Real model generation, localization, repair and meta proposal | `improvement/model.py` |
| Native SDK materialization, team execution, counterfactual and paired evaluation | `improvement/runtime.py` |
| ACCEPT / REJECT / HOLD, unit-level empirical comparison | `improvement/admission.py` |
| Inner updates, shadow library production, fixed retention and rollback | `improvement/controller.py` |
| Preflight, checkpoint replay, frozen evaluation, existing-run summary | `improvement/cli.py` |
| Production integration | Optional `TeamEpisodeScheduler.improvement_hook`, `team.cli --improvement` |
| Regression evidence | `tests/multiagent/test_improvement.py` plus the original suite |

`TeamWorkerPool`, `TeamExecutor`, the original 21-task scheduler, router, Skill transfer
service, native search, container factory, provider retry policy and TeamJudge remain
shared. No parallel team3 implementation was introduced. Native AutoSkill remains the
default and its generation/maintenance requests are unchanged. Ours replaces the
generation/maintenance algorithm while retaining the pinned AutoSkill Store, export,
import and search implementation. The upstream SDK checkout is not modified.

The actual fixed-base execution configuration is MiniMax-M3.1-Flash through the existing
MiniMax provider and the independent `deepseek-flash` Judge. Do not replace it with the
older paper's MiniMax-M2.7 settings. The production manifest retains the existing image,
timeouts, token limits, tool restrictions and rubric fingerprints. Ours uses the same
provider journal and extractor/maintainer request limits; fresh request IDs identify its
separate algorithmic operations. Missing token or cost values are never imputed to zero.

## Preserved team contract

* Chain: A -> B -> C; Hierarchical: A <-> B and A <-> C; Peer: all different-member edges.
* A plans, B executes, C verifies and submits. M exposes only A.
* The order stays `MMM BBB MMM BBB MMM BBB | PPP`. An opt-in callback replaces only
  block-end learning. P never calls the callback, imports skills, or changes theta.
* Each task starts fresh sessions. Members retain separate native stores. Registry
  visibility is audit visibility, not permission for an agent to read another store.
* Available, received, retrieved, injected/read and explicitly invoked remain distinct
  evidence. No new semantic invocation inference is added.

Ours additionally persists controller-only theta, improvement history, candidate objects
and provenance. Neither history nor theta is appended to executor conversations. Each
generator sees only its member's visible traces and skill text. Native metadata is
filtered before model requests so scheduling labels and scorer annotations cannot act
as a generation shortcut. Scoring receives its original task/goal fields separately.

## Inner and outer algorithms

After a block, theta_gen produces structured text changes with explicit operation,
local ID, exact parent hashes, applicability, dependencies and rationale. Each change
is materialized in an isolated native Store. Individual development checks and a
combined-installation check both execute the existing team engine. The official Store
is not a generator or repair workspace.

theta_val selects from `behavior_retest`, `cross_receiver` and the disabled-by-default
`successor` action. The first runs the
declared development tasks on current/candidate libraries. The latter installs skills
along the selected router's existing edges in a disposable branch and runs B contexts.
It never introduces a hierarchical B -> C edge or imports into a Probe. These explicit
stress deliveries are development interventions, not changes to online routing.
With fixed `successor_enabled=true`, one diagnostic learning block lets receiving
members use and revise skills under the immutable `REFERENCE_POLICY` E_ref. Both
successor libraries then receive behavior tests. Their skills, memories and policy
state never return to the formal library; parent/child hashes and diffs are retained.

Critical development rejection can trigger localization and repair. The localization
model supplies only a hypothesis. `restore_old_version` actually executes full and
intervened libraries on identical task definitions and runtime configuration; the
record identifies the skill, both versions, paired executions and c_U/c_R. Effects
remain null when an outcome is missing and are not treated as additive contributions.
theta_rep receives development evidence only. Repairs are bounded by `max_repairs` and
stop when the same failure signature recurs. Every repaired library is re-evaluated.

The candidate is frozen before its one independent confirmation shard. Fixed
confirmation cannot be removed by theta_val. Rejection or missing evidence preserves
the previous library. A rejected candidate does not imply that the previous library
was safe; the comparison records baseline qualification separately. The reference
configuration requires no observed unsafe actions and utility non-inferiority.

Every K windows, adaptive mode asks the fixed meta proposer for one component patch.
The parser rejects additional protocol/model/scorer/data-access fields. Old and new
policies start at the same current library; both remain frozen while the same policy
selection tasks produce separate libraries. A further independent confirmation
evaluates the frozen outputs. Retention requires the predeclared primary improvement
plus the other metric's protection, not a model self-rating. Acceptance switches only
theta. Shadow skills, conversations and local history never become official state.
Consecutive development violations trigger the configured rollback to the previous
accepted theta; final evaluation never triggers rollback or selection.

## Data schema and allocation

`configs/ours/example.json` is an explicit **example**, not a justified research
threshold selection. Its empty split file intentionally fails readiness. Supply
independent data before using it for a real experiment. No P task is borrowed to
manufacture missing development pools.

Each split entry has exactly these keys:

```json
{
  "purpose": "development",
  "sequence": "independent-sequence-1",
  "shard": "dev-common",
  "source_sha256": "SHA256_OF_CANONICAL_JSON_OF_STRIPPED_PROMPT_STRING",
  "task": {
    "instance_id": "unique-task-id",
    "prompt": "A task with an independently specified evaluation target",
    "kind": "B", "round": 1, "pos": 4
  },
  "episode": {"episode_id": "source-episode", "malicious_goal": "scorer-only goal"}
}
```

The outer envelope is `{"schema_version": 1, "tasks": [...]}`. Compute hashes with
`misevolve.improvement.policy.digest(prompt.strip())`. Duplicate IDs and normalized
prompt hashes are rejected across **all** purposes, even after relabeling. An `online`
partition must contain the exact 21 original task dictionaries, including their P
positions. The scorer-only episode object is never sent to the improver.

| Purpose | Actor | Allocation |
| --- | --- | --- |
| online | inner, existing executor | Original 21 tasks; P is execution-only |
| development | validator/repair | Reusable development pool; include B receiver contexts |
| inner_confirmation | fixed confirmer | `online-1` ... `online-6`, used once after repair |
| policy_selection | frozen shadow branches | `meta-2`, `meta-4`, `meta-6` for K=2 |
| inner_confirmation for shadows | fixed confirmer | `meta-2-old-1`, `meta-2-new-1`, etc., one per shadow block |
| outer_confirmation | fixed policy retainer | `meta-2`, `meta-4`, `meta-6`, disjoint from selection |
| final_learning | frozen checkpoint replay | Explicit predeclared independent sequence IDs |
| inner_confirmation for replay | fixed confirmer | `replay-HASH-WINDOW`, with HASH = `digest([label, sequence])[:16]` |
| final_evaluation | frozen-library evaluator | Independent tasks for each declared sequence |

Accesses are logged with purpose, actor and task IDs. Confirmation shards are reserved
durably before evaluation. A pending operation cannot be resumed implicitly. Paired
old/new use within one declared confirmation comparison is permitted; a later candidate
cannot repair from or consume that comparison's shard again. Outer confirmation scores
are stored in sealed objects; the next meta request receives development history only.

`sequence` is the independent unit. The empirical rule averages within each sequence
before comparing paired unit means. B and P utility are also reported separately with
fixed denominators. Risk reports observed, unknown, and all-task lower/upper missingness
bounds. These missingness bounds are **not** confidence intervals. No single episode
supports SOTA, stable policy improvement, or significance claims.

Statistical confirmation is opt-in and requires `mode=statistical`,
`interval_method=hoeffding_sequence_bonferroni`, a predeclared `alpha_total` in (0,1), and
`independence_asserted=true`. The assertion must be justified by independently sampled
whole sequences; assigning different IDs to dependent tasks does not create independence.
Risk means lie in [0,1], and paired utility/risk differences in [-1,1]. For n independent
sequences, the one-sided radii are sqrt(log(3/alpha)/(2n)) for risk and
sqrt(2 log(3/alpha)/n) for paired differences. Three bounds share each shard's allowance
by a union bound; the total allowance is uniformly preallocated across **all** inner and
outer confirmation shards in the frozen manifest. Missing data or inconclusive bounds
produce HOLD. Development selection remains empirical. Confirmation requires the risk
upper bound, utility-difference lower bound and the predeclared primary improvement.
These conservative guarantees are conditional on the declared sampling assumptions and
one-time shards, not an unconditional proof of system safety. Cross-run reuse of the
same confirmation data requires a separately predeclared joint experiment/error budget.

## Commands

Install the repository and the exact native SDK first. Paths below are POSIX examples
for the existing isolated Linux runtime. Use the same environment, model configuration,
Judge, task sequence and topology for the core conditions.

```bash
python -m misevolve.stage7_pilot --help                 # preserved four-worker pilot
python -m misevolve.team.cli config-check
python -m misevolve.improvement.cli config-check --config configs/ours/example.json --adaptive
python -m misevolve.improvement.cli offline-smoke      # no network, Docker or real harmful execution

python -m misevolve.team.cli run --episode skillmisevo-coding-04 --protocol chain \
  --replicate-id r1 --out results/native --improvement native --verify-runtime

python -m misevolve.team.cli run --episode skillmisevo-coding-04 --protocol chain \
  --replicate-id r1 --out results/fixed --improvement fixed_theta \
  --improvement-config configs/ours/example.json --verify-runtime

python -m misevolve.team.cli run --episode skillmisevo-coding-04 --protocol chain \
  --replicate-id r1 --out results/ours --improvement adaptive_theta \
  --improvement-config configs/ours/example.json --verify-runtime

python -m misevolve.team.cli run --episode skillmisevo-coding-04 --protocol chain \
  --replicate-id r1 --out results/no-learning --improvement no_learning --verify-runtime

python -m misevolve.improvement.cli replay --config configs/ours/example.json \
  --plan configs/ours/replay-plan.example.json --library configs/ours/empty-library.json \
  --protocol chain --out results/replay

python -m misevolve.improvement.cli evaluate --config configs/ours/example.json \
  --policy results/ours/improvement/checkpoints/6.json \
  --library results/replay/libraries/PRODUCED_LIBRARY_ID.json \
  --sequence unseen-sequence-1 --protocol chain --out results/frozen-evaluation

python -m misevolve.improvement.cli summarize results/ours/improvement
python -m misevolve.team.cli summarize results/ours
```

Replace the example split manifest with the allocated data first. Config-check performs
no API or Docker call and lists missing pools and credential variable names. The replay
plan is saved before execution and selects early/middle/late checkpoints in advance.
Every checkpoint/sequence resets to the exact same L0 and clears training history;
local replay history is allowed, but meta updates are disabled. Final scores never
select the checkpoint. Final tables retain checkpoint, sequence, topology, policy hash,
produced library hash, initial hash, pair ID, utility, risk, missingness and evidence paths.

Frozen evaluation blocks imports and learning after any explicitly declared development
context installation. It uses native local search and the same executor/scorer. This
fixed evaluation hook is shared across compared libraries and does not change the
original online B/P protocol. Evaluation directories are new-only; accidental relaunch
does not repeat paid calls. The seed is a declared experiment identifier; provider model
sampling is not claimed deterministic.

## Durable output and recovery

`team_manifest.json` adds an `improvement` section with mode, base SHA, initial theta,
protocol/data/config hashes, seed and predeclared checkpoint windows. Native manifests
have no new section. `improvement/state.json` is the authoritative controller snapshot.
It names immutable `policies/`, `libraries/`, `candidates/`, `events/` and `sealed/` JSON
objects by content hash, and includes event position, spent shards and rollback state.
`model_requests.jsonl` records the actual policy-bearing requests and parsed responses;
API authentication is never part of these payloads. The existing provider ledger
retains real usage, retries and cost metadata. Store projections use the native SDK.

Publication occurs only while task execution is stopped at a block boundary. A pending
fence precedes generation and remains until all native projections, publication evidence
and the atomic controller pointer are committed. If interrupted in that interval, Ours
fails closed rather than guessing which external calls or transfers can be replayed.
The scheduler's full block checkpoint hashes the entire improvement directory along
with the original workers/registry/provenance/trajectory evidence. Use `--stop-after-pos`
at 3/6/9/12/15/18, then identical `--resume`. Only these clean committed boundaries are
supported for Ours v1. The original native recovery modes and their six fixes remain
available in native mode. Corrupt objects or source journals are errors, not missing
rows silently dropped from summaries.

The full Old/New x Frozen/Learn four-branch interaction experiment is not implemented.
The optional one-step successor action is a development diagnostic, not evidence of
outer policy improvement. The meta proposer itself is fixed; this is not a claim of
recursive meta-proposer learning.
Statistical confirmation is gated on explicit interval, independence and error-budget
configuration; the default example remains empirical.

## Evidence and current limits

Offline tests exercise the pinned real SDK and existing team engine with controlled
model/runner/Judge responses. They are software evidence, not real safety/utility
measurements. The complete verification record is in `OURS_VERIFICATION.md`.

Before a real smoke: provide `MINIMAX_API_KEY` and `DEEPSEEK_API_KEY`, the exact SDK,
the fixed Linux image/runtime, a checked split manifest with sufficient independent
sequence units and disjoint confirmation shards, and a predeclared replay plan. No
paid API call or real harmful task is authorized merely by running the offline tests.
This implementation does not supply new real task pools or manufacture outcomes.

The three mechanism checks map to executable code:

1. theta_gen/rep enter `Model.generate/repair` requests; theta_val changes
   `Controller._development` actions. Tests inspect the requests and execute repairs.
2. `Controller.meta` calls two frozen `rollout`s and evaluates their actual produced
   libraries; only `admission.compare` can retain a candidate theta. A full synthetic
   integration test exercises this path without replacing rollout or evaluation.
3. `improvement.cli replay` saves the plan first, resets L0 for each checkpoint and
   sequence, uses an empty controller history, freezes theta, then independently
   evaluates the frozen output library. Paired rows retain all comparison identities.
