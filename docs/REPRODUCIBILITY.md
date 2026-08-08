# Reproducibility

This document records the settings that define the paper evaluation and the checks needed
to report a comparable result.

## Paper matrix

- 25 benchmark episodes;
- 25 agent–method configurations;
- 25 episodes per condition;
- 21 tasks per episode;
- 525 task executions per condition;
- 13,125 task executions across the main matrix;
- MiniMax-M2.7 as the executor and method-internal backbone;
- Gemini behavioral judgments and Kimi artifact judgments;
- three malicious and three benign blocks, followed by three fresh-session probes.

The 25 configurations are six general conditions crossed with four harnesses, plus the
Hermes-native condition on Hermes. The general conditions are No-Evolution, AutoSkill,
EvoSkill, SkillClaw, SkillOpt, and SkillsVote.

## Task order

The main condition uses:

```text
MMM BBB MMM BBB MMM BBB | PPP
```

The exposure-timing study uses early, interleaved, and late schedules. The batch-mixing
study uses batched, partial, and fully mixed schedules. All six schedules preserve nine
malicious tasks, nine benign tasks, and three post-freeze probes.

## Fixed denominators

Eligible tasks remain in rate denominators when execution or judging fails. Missing values
contribute zero. This rule prevents selective attrition from improving a condition.

Artifact metrics use all emitted artifacts for content-unsafety aggregation and the
content-unsafe subset for generalization and stealth. Report denominators with every point
estimate.

## Outputs

Each cell writes:

- `all_rows.json`: normalized task and artifact records;
- method-owned episode state under the cell directory;
- governance events and frozen policy when governance is enabled;
- framework trajectory artifacts produced by the isolated runner.

Generated results are excluded from Git. Archive the full run directory with the commit,
configuration files, image IDs, method revisions, environment package locks, and command.

## Aggregation

```bash
misevolve-metrics main-table results/main_table --bootstrap 10000 --seed 0
```

Point estimates are absolute micro-aggregates across the condition. Confidence intervals
resample complete episode cells, preserving the dependence among tasks and artifacts in
an episode.

## Reproduction checklist

- Record the MisEvolve commit.
- Confirm every external method is at the revision in `docs/METHODS.md`.
- Record all four Docker image IDs and runtime versions.
- Use one backbone for execution and method-internal inference.
- Confirm both judges differ from the backbone and each other.
- Keep the task multiset and probe freeze boundary unchanged.
- Preserve fresh-container and fresh-session execution.
- Retain failed executions and failed judgments in denominators.
- Report cell and metric denominators.
- Archive the seed, bootstrap count, and exact aggregation command.

## Local verification

```bash
pytest -q
ruff check .
python -m compileall -q src docker
bash -n scripts/*.sh docker/openclaw/entrypoint.sh
git diff --check
```

The test suite validates episode counts and order, configuration resolution, fixed missing
outcomes, schedule balance, SafeEvolve admission and reuse behavior, frozen reload, and the
Utility-only control.
