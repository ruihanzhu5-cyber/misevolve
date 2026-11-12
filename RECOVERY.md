# Interrupted pilot continuation

The November 12 pilot encountered repeated HTTP 429 responses. Its original one-second
transport retry exhausted and the shared provider circuit stopped Chain at task 3 and
Hierarchical at task 2. Peer-to-peer continues on its original process and revision.

Team execution now retries an identical HTTP request at most eight times over a
180-second recovery window, honoring Retry-After and the existing task deadline.
Exhaustion during execution is a task timeout; independent Judge calls can still run.
Authentication and permanent protocol failures remain fatal. Native evolution must
complete successfully and is never silently skipped or restarted.

`--recover-initial-block` handles only an interrupted first M block with no native
ingestion, skill mutation, or imports. It validates the original episode and runtime,
preserves every recorded outcome (including failures), restores learning inputs from
the durable successful A trajectories, and executes only unrecorded tasks. Existing
task execution and Judge calls are never repeated. Native AutoSkill evolution then
runs at the original block boundary using those recovered inputs. A separate recovery
manifest records old/new code, transport policy, original manifest, and evidence hashes.
Other interruptions still require a clean learning-block checkpoint.

The user subsequently corrected the continuation positions: retry Chain task 3 and
Hierarchical task 2. `--retry-failed-initial-task`, used with the recovery option,
archives the trailing failed row and retries only that task with a new attempt ID.
All earlier completed task and Judge results stay unchanged. The full original rows
remain in `pre_recovery_rows.json`, and the superseded failure is also embedded in
`recovery_manifest.json`; original trajectories and API/Judge journals remain intact.
The active 21-task result table includes one selected attempt per task; reporting must
also disclose the archived infrastructure failure and the explicit retry policy.

The existing pilot is an operational continuation, not an entirely matched code
comparison. Recovery history records the code/image transition; do not attribute all
differences to topology when prefixes used the original runtime.

Focused synthetic checks cover repeated 429, Retry-After, independent Judge availability,
authentication failure, task cutoff, and recovery at task 2/3 without duplicate execution
or scoring. They use the pinned native SDK with model responses replaced by fixtures.

## Peer task 11: prompt argv and in-block recovery

Peer task 11 exposed an adapter bug: the complete prompt was passed in OpenClaw's
`--message` argument. A native `pkill -f` cleanup command matched that parent argv
and terminated the caller (exit 143). The pinned image reproduces this with a loopback
fixture and no external network. The adapter now uses OpenClaw's supported
`--message-file`, with identical UTF-8 bytes in a private temporary file. The fixed
image completes the same fixture. No prompts, native tools, topology or evolution
algorithm change.

`recovery_prepare.prepare` creates a separate directory from durable evidence. It
retains completed tasks 1–10 and their judges, registry/transfer history and full native
Skill metadata. Stores are restored to the pre-task-11 state: evolution through task 9
plus imports delivered during task 10. Superseded task 11/12 outcomes and later learning
remain in the original directory and archive, not in the active fork. Full call ledgers
are retained for cost accounting, including abandoned attempts.

`--recover-prepared-block` checks the fork's hashes, task prefix and approved code/image.
Task 10's durable trace and retrieved bundles rejoin the pending block; only task 11
onward executes. Native `evolve_batch` still receives tasks 10/11/12 at the original
block boundary. A focused SDK test checks prefix preservation, learning inputs and
absence of replay. Runtime image changes are recorded in both recovery mechanisms;
AutoSkill revision and all non-repair configuration must match.
