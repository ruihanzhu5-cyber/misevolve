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

The existing pilot becomes an operational continuation, not an entirely matched code
comparison: the two recovered conditions use repaired transport behavior, while the
already-running Peer process stays unchanged. Existing infrastructure failures remain
in the results. Do not claim topology alone explains differences from this pilot.

Focused synthetic checks cover repeated 429, Retry-After, independent Judge availability,
authentication failure, task cutoff, and recovery at task 2/3 without duplicate execution
or scoring. They use the pinned native SDK with model responses replaced by fixtures.
