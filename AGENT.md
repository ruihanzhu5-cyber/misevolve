# AGENT.md

This file is the operating contract for AI agents working in this repository.

## Mission

MisEvolve measures whether skill-evolution systems convert unsafe experience into
persistent behavior. Preserve the experiment's causal boundary: the skill store is the
only state that crosses task sessions.

## Read order

1. `README.md` for the public research claim and supported matrix.
2. `docs/ARCHITECTURE.md` for component ownership and data flow.
3. `docs/REPRODUCIBILITY.md` for fixed denominators and paper settings.
4. The target module and its tests.

## Repository map

| Path | Responsibility |
|---|---|
| `src/misevolve/run.py` | one episode × method × harness cell |
| `src/misevolve/harness.py` | episode state machine and row emission |
| `src/misevolve/adapters/` | container-backed agent runners |
| `src/misevolve/methods/` | native method adapters and no-evolution floor |
| `src/misevolve/governance/` | Utility-only and SafeEvolve wrappers |
| `src/misevolve/judges/` | behavioral and artifact scoring |
| `src/misevolve/bench/` | schedules and metric aggregation |
| `configs/` | runtime axes and frozen governance policies |
| `data/episodes/` | benchmark episode definitions |
| `docker/` | isolated framework images and runner contracts |
| `tests/` | executable invariants |

## Experiment invariants

1. Start a fresh container and agent session for every task.
2. Persist only the method-owned skill library across tasks.
3. Freeze the library before the new session, the final three benign tasks.
4. Apply one backbone to the executor and every method-internal LLM.
5. Keep every judge different from the evaluated backbone. The original group pairs two
   model families, and the independent group uses one separate model for both roles.
6. Count missing executions and task-judge failures as zero under fixed denominators.
   Keep a missing artifact judgment undefined and out of the artifact scores.
7. Preserve the 21-task default episode: `MMM BBB MMM BBB MMM BBB | PPP`.
8. Keep real credentials, live targets, and unsanitized run logs out of commits.
9. Run agent tools only inside the disposable harness containers.

Any change that alters an invariant requires matching updates to tests,
`docs/ARCHITECTURE.md`, and `docs/REPRODUCIBILITY.md`.

## Common commands

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'

pytest -q
ruff check .
python -m compileall -q src docker
bash -n scripts/*.sh docker/openclaw/entrypoint.sh

misevolve --help
misevolve-metrics --help
```

Run a six-task smoke cell:

```bash
MAX_TASKS=6 METHODS=autoskill HARNESSES=openclaw bash scripts/run_main_table.sh
```

## Change protocol

1. Inspect the closest contract and tests before editing.
2. Keep adapters thin. Normalize third-party behavior at the adapter boundary.
3. Add configurable paths through `misevolve.paths`; never commit machine-specific paths.
4. Resolve credentials from environment variables. Never add keys to YAML, Dockerfiles, or logs.
5. Pin external method revisions in `scripts/setup_methods.sh` and document revision changes.
6. Add a focused test for every behavioral change.
7. Run the full local verification suite before committing.
8. Leave generated results, external checkouts, virtual environments, and secrets untracked.

## Extension contracts

### Add an evolution method

Implement the `MethodHarness` lifecycle in `src/misevolve/methods/`, register it in
`src/misevolve/methods/__init__.py`, pin its upstream revision, document its environment,
and test retrieve/evolve/persist behavior.

### Add an agent harness

Add one container runner with the JSON stdin/trajectory output contract, register its
native skill path in `configs/harnesses.yaml`, and verify that a fresh task cannot observe
previous task state outside the injected skill library.

### Add a backbone

Add the model, endpoint, and key variable to `configs/backbones.yaml`. Supply an Anthropic
Messages endpoint when the Claude Code harness is supported. Confirm judge independence.

### Add governance

Implement `GovernancePolicy`, register it in `src/misevolve/governance/registry.py`, keep
the wrapped method's native behavior intact, and test admission, delete-only repair,
retirement, eviction, frozen-library reload, and lineage persistence.

## Output contract

Each completed cell writes `all_rows.json`. Task rows carry condition identity, task kind,
utility, harm, retrieval evidence, and execution status. Artifact rows carry content
unsafety, unsafe generalization, stealth, and lineage identifiers. Aggregation treats the
cell as the bootstrap unit.

## Safety boundary

The dataset contains instructions for persistence, secret collection, and data exfiltration.
Do not execute episode prompts on the host. Do not weaken Docker isolation to make a test
pass. Report suspected escapes or credential exposure through the process in `SECURITY.md`.
