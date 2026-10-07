# Contributing

Contributions should preserve MisEvolve's experiment invariants and keep the public package
reproducible.

## Development setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
```

External method checkouts are needed only for integration runs:

```bash
bash scripts/setup_methods.sh
```

## Before a pull request

```bash
pytest -q
ruff check .
python -m compileall -q src docker
bash -n scripts/*.sh docker/openclaw/entrypoint.sh
git diff --check
```

Add a focused test for behavioral changes. Update `AGENT.md`, architecture, configuration,
or reproducibility documentation when a public contract changes.

## Scope

Strong contributions include:

- adapters for open, reproducible skill-evolution methods;
- agent harnesses that preserve task isolation;
- governance policies and ablations with explicit contracts;
- benchmark episodes with paired malicious and related benign tasks;
- metric validation and failure-accounting tests;
- documentation and portability improvements.

Do not commit generated results, model credentials, machine-specific paths, external
repositories, virtual environments, or container layers.

## Method revisions

Upstream changes can alter results. Pin every method revision in `scripts/setup_methods.sh`,
record it in `docs/METHODS.md`, and describe the reason in `CHANGELOG.md`.

## Safety review

Treat changes to mounts, network access, subprocess execution, prompt transport, credential
handling, and framework home directories as security-sensitive. Validate them against
`SECURITY.md` and document the preserved boundary in the pull request.
