# Ours implementation verification

Date: 2026-11-13. Base: `4cc377300be179d71d72888347dc3e5ff1132a2d`.
Branch: `scope-ours`. The initial offline delivery is recorded below; subsequent
GitHub pushes and ECS smoke verification are recorded in the follow-up section.

## Environment and baseline

Verification uses Python 3.12.13 and the real AutoSkill SDK checkout at
`94c47ca488d4ba4117d20272e66d49b9877e68cf`. Model responses, task sessions and Judge
responses in the new tests are synthetic. The test fixture blocks network and Docker.
The SDK Store/search/import/export and the three-agent execution/routing code are real.

The first attempt used an incomplete prior environment and produced 60 passes, 207
dependency initialization errors and two diagnostic failures. The missing SDK was
restored in a separate pinned worktree, without modifying upstream source.

An unmodified fixed-base run then produced **267 passed, 2 failed**. The two failures
were the parameterized `test_stage7_process_failure_retains_details_without_replay`
test. Its AST fixture omitted the `tempfile` global used by the existing message-file
repair. This delivery adds that missing fixture dependency and retains every diagnostic
assertion. The diagnostic and message-file tests subsequently passed **4/4**.

## Checks

| Check | Observed result |
| --- | --- |
| New Ours tests | 33 passed (32 targeted plus the combined-installation test; all included in final full run) |
| Complete suite before diagnostic-fixture correction | 294 passed, 2 inherited failures (27 Ours tests at that point) |
| Final complete suite | 302 passed in 441.21 seconds |
| New improvement package/tests Ruff | Passed |
| Full fixed-base Ruff on the same tracked Python files | 191 pre-existing diagnostics |
| Full current Ruff | 188 diagnostics; zero newly introduced diagnostics, three removed |
| `python -m compileall -q src docker` | Passed |
| `bash -n scripts/*.sh docker/openclaw/entrypoint.sh`, plus each script separately | Passed |
| `git diff --check` | Passed |
| Team/replay/legacy CLI help and Ours config-check | Executed offline |

The full Ruff comparison reconstructs the fixed commit's tracked Python files, including
archived scripts omitted by its old sparse checkout. Archived experiment records are
not reformatted or overwritten to hide existing diagnostics. Full lint is therefore
not claimed clean. The new implementation and its dedicated tests are lint-clean.

## Behavioral coverage

The 33 new tests cover actual policy-bearing model requests; native-metadata filtering;
purpose access and split conflicts; immutable protocol and policy patch scope; empirical
and configured sequence-level statistical confirmation; missing/error HOLD; native
candidate isolation; versioned editing/deletion/deactivation; all three topology
validation paths; combined and fixed confirmation; real old-version interventions;
repair and revalidation from executed traces; fixed/adaptive cadence; actual two-policy
shadow production and independent evaluation; early/late replay reset; source/import
identity and forwarding preservation; clean committed-window recovery; corrupt-state
failure; optional E_ref successor isolation; and original 21-task/role/Probe semantics.

The full original suite covers the historical single/four-worker paths, team routing,
task scoring, native exchange and all six recovery/transport fixes. These tests do not
establish measured safety or utility on real tasks.

## Initial live readiness (before ECS deployment)

At initial delivery, the example configuration reported `ready=false`. That process lacked
`MINIMAX_API_KEY`; `DEEPSEEK_API_KEY` is present (its value is not recorded). The example
split manifest intentionally contains no tasks and needs online, development, inner
confirmation, policy selection, outer confirmation, final learning and final evaluation
pools. The exact original online episode is available in the repository, but it is not
silently repartitioned to manufacture the missing independent pools.

The fixed Linux OpenClaw image/runtime and isolation had not been revalidated in that
Windows implementation session. No real smoke, paid benchmark, dangerous task, or
real policy-improvement experiment had been launched at that point. Those results are
**offline engineering validation**. Use the commands and shard allocation contract in
`OURS.md` after supplying the data and runtime requirements.

The optional full four-branch Old/New x Frozen/Learn interaction study and paper-scale
external baselines remain outside the requested first-version scope. The one-step
successor action is implemented and disabled by default. The meta proposer remains
fixed. Statistical claims require the declared independent-sequence assumptions and
the configured preallocated confirmation allowances; fixtures do not satisfy those
research assumptions merely by producing passing software tests.

## ECS live follow-up, 2026-11-13

The implementation was subsequently pushed to `scope-ours` and deployed on Ubuntu
24.04, Python 3.12, Docker 29.1.3, with the same pinned AutoSkill revision. The restored
OpenClaw image ID was verified as
`sha256:72122ae6673582bb7f3bfe0f954f1cae23fc02a9fe3e58962cc4b7045c0afcff`.
The existing MiniMax and DeepSeek credentials authenticated successfully. No credential
values are included in the repository or verification artifacts.

The first Linux full suite passed 302 tests. Three subsequent integration corrections
were regression-tested; the complete suite then passed **309 tests in 77.60 seconds**:

* `ad8f460`: release each diagnostic worker's internal bridge, relay and firewall on
  teardown instead of retaining them until the outer factory exits. Regression tests
  cover 40 consecutive three-worker teams, repeated teardown, and setup failures.
* `b1b9f97`: explain that generated skill dependencies reference existing local skill
  IDs, not tools or interpreters. A real response had used "local Python interpreter"
  as a dependency, correctly causing HOLD. Strict validation remains enabled.
* `cf09011`: align frozen-evaluation prompts with the disabled native skill imports.
  Previously, a B-context prompt still encouraged sharing while the fixed evaluator
  rejected imports; repeated attempts exhausted delivery recovery despite C answering
  the arithmetic question. The fix removes that contradictory instruction. It does
  not permit imports, ignore failed deliveries, change scores or force acceptance.
  Tests cover all three routers and verify that the frozen stores remain unchanged.

Live smoke tasks were separately authored arithmetic fixtures, **not SkillMisevo-Bench
tasks or paper results**. Chain and peer-to-peer each completed their initial task and
received utility=true and unsafe_action=false. Hierarchical returned `not_submitted`:
A did not deliver the required post-feedback decision to C. That failed observation
was retained rather than rerun or replaced with a successful sample.

The first valid Ours candidate was installed, retrieved and read. Both single-candidate
and combined development comparisons passed, but the candidate-side confirmation
encountered the frozen-prompt issue above. The controller returned HOLD, kept the empty
official library, persisted its state and marked the confirmation shard spent. After
fixing the execution contract, a separate engineering run used fresh development and
confirmation tasks; the prior shard and failure evidence were not overwritten.

In that fresh run, all four development executions passed. The baseline confirmation
also completed, while candidate-side C computed 152 correctly but prefixed its unfenced
JSON envelope with explanatory prose. The strict parser recorded `not_submitted`, and
the controller again returned HOLD without publishing the candidate. This is the final
recorded live outcome; the smoke is **not an all-pass result**.

A final parser correction recognizes a single complete trailing JSON object on its own
line after prose containing no braces. Multiple objects, ambiguous prefixes, trailing
text, malformed schemas and forbidden routing remain rejected. The captured real C
response was checked offline against that parser; no provider calls, retrospective
scoring, confirmation reuse or promotion occurred. The persisted HOLD state reloaded
successfully with an empty official library and the confirmation shard still spent.
Successful live acceptance/publication and the full adaptive outer loop remain unverified.
The final complete Linux suite passed **311 tests in 77.12 seconds** after this parser
correction, including both framed-output variants and ambiguous-output rejection.

These runs use one synthetic sequence per comparison and empirical smoke settings.
They do not establish statistical safety, policy improvement, or benchmark performance.
The example research configuration still reports missing task pools; credentials and
the pinned SDK are no longer readiness gaps. Full research evaluation requires the
independent data allocation described in `OURS.md`.
