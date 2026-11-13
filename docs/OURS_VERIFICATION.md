# Ours implementation verification

Date: 2026-11-13. Base: `4cc377300be179d71d72888347dc3e5ff1132a2d`.
Branch: `scope-ours`. Delivery is reviewable local changes, without a push or merge.

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

## Live readiness

The checked example configuration reports `ready=false`. The current process lacks
`MINIMAX_API_KEY`; `DEEPSEEK_API_KEY` is present (its value is not recorded). The example
split manifest intentionally contains no tasks and needs online, development, inner
confirmation, policy selection, outer confirmation, final learning and final evaluation
pools. The exact original online episode is available in the repository, but it is not
silently repartitioned to manufacture the missing independent pools.

The fixed Linux OpenClaw image/runtime and isolation were not revalidated in this
Windows implementation session. No real smoke, paid benchmark, dangerous task, or
real policy-improvement experiment was launched. Results are **offline engineering
validation; live unverified**. Use the commands and shard allocation contract in
`OURS.md` after supplying the data and runtime requirements.

The optional full four-branch Old/New x Frozen/Learn interaction study and paper-scale
external baselines remain outside the requested first-version scope. The one-step
successor action is implemented and disabled by default. The meta proposer remains
fixed. Statistical claims require the declared independent-sequence assumptions and
the configured preallocated confirmation allowances; fixtures do not satisfy those
research assumptions merely by producing passing software tests.
