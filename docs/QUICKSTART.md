# Quick start

This walkthrough installs MisEvolve, validates the local package, runs one short cell, and
aggregates its output.

## Requirements

- Python 3.12
- `uv`
- Docker with enough capacity for one selected agent image
- a MiniMax API key for the default backbone
- an OpenRouter API key for the original judge group
- an xAI API key when scoring with the independent judge group

## 1. Install

```bash
git clone https://github.com/henrymao2004/misevolve.git
cd misevolve
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
```

## 2. Configure credentials

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
MINIMAX_API_KEY=...
OPENROUTER_API_KEY=...
MISEVOLVE_BACKBONE=minimax-m2.7
```

The grid script loads `.env`. Direct `misevolve` commands use the current environment, so
export the same variables or source the file first.

## 3. Install method checkouts

```bash
bash scripts/setup_methods.sh
```

The script checks out the exact paper revisions under `external/methods/` and creates one
virtual environment per method under `.venvs/`. Set
`MISEVOLVE_SKIP_METHOD_INSTALL=1` to fetch revisions without building environments.

## 4. Build harness images

Build all four images:

```bash
bash scripts/build_images.sh
```

Build one image while iterating:

```bash
bash scripts/build_images.sh openclaw
```

## 5. Validate the repository

```bash
pytest -q
ruff check .
python -m compileall -q src docker
bash -n scripts/*.sh docker/openclaw/entrypoint.sh
```

## 6. Run a smoke cell

```bash
MAX_TASKS=6 \
METHODS=autoskill \
HARNESSES=openclaw \
bash scripts/run_main_table.sh
```

The run writes an `all_rows.json` file below `results/main_table/`. Repeating the command
skips completed cells.

To exercise SafeEvolve directly:

```bash
set -a
source .env
set +a

misevolve \
  --episode skillmisevo-coding-01 \
  --method autoskill \
  --harness openclaw \
  --governance safeevolve \
  --max-tasks 6 \
  --out results/safeevolve-smoke
```

## 7. Aggregate results

```bash
misevolve-metrics summarize results/safeevolve-smoke/all_rows.json
misevolve-metrics main-table results/main_table
```

The main-table command reports the nine metrics and episode-level bootstrap intervals.

## Full grid

```bash
bash scripts/run_main_table.sh
```

The default matrix contains 25 episodes and 25 conditions: No Evolution and five evolution
methods crossed with four target agents, plus Hermes-native on Hermes. Of these, 21 are
combinations of an agent with an evolution method and 4 are No Evolution. This produces 625
episode cells and 13,125 task executions before retries.

Use environment variables to narrow the matrix:

```bash
METHODS="autoskill evoskill" \
HARNESSES="codex openclaw" \
CONCURRENCY=2 \
bash scripts/run_main_table.sh
```

See [`CONFIGURATION.md`](CONFIGURATION.md) for model, schedule, governance, output, and
path controls.
