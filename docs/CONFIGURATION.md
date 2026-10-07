# Configuration

MisEvolve keeps experimental axes in versioned configuration and secrets in environment
variables.

## Environment variables

| Variable | Purpose |
|---|---|
| `MINIMAX_API_KEY` | default MiniMax-M2.7 backbone |
| `OPENROUTER_API_KEY` | original judge group; OpenRouter backbones |
| `XAI_API_KEY` | independent judge group; optional xAI backbone |
| `MISEVOLVE_JUDGE_GROUP` | `original` by default, or `independent` |
| `MISEVOLVE_BACKBONE` | default backbone key from `configs/backbones.yaml` |
| `MISEVOLVE_ROOT` | alternate repository/resource root |
| `MISEVOLVE_CONFIG_DIR` | alternate configuration directory |
| `MISEVOLVE_METHODS_DIR` | root for external method checkouts |
| `MISEVOLVE_AUTOSKILL_DIR` | explicit AutoSkill checkout |
| `MISEVOLVE_EVOSKILL_DIR` | explicit EvoSkill checkout |
| `MISEVOLVE_SKILLCLAW_DIR` | explicit SkillClaw checkout |
| `MISEVOLVE_SKILLOPT_DIR` | explicit SkillOpt checkout |
| `MISEVOLVE_SKILLSVOTE_DIR` | explicit skills-vote checkout |
| `AHZ_JUDGE_UTILITY` | set to `0` to disable utility judgments |
| `SAFEEVOLVE_API_KEY` | optional dedicated SafeEvolve critic key |
| `SAFEEVOLVE_BASE_URL` | optional SafeEvolve critic endpoint |
| `SAFEEVOLVE_MODEL` | optional SafeEvolve critic model |

## Backbones

`configs/backbones.yaml` maps a short ID to a model, OpenAI-compatible endpoint, key
variable, and optional Anthropic Messages endpoint. Claude Code requires the Anthropic
endpoint. The default is MiniMax-M2.7.

The selected backbone applies to the executor and every method-internal LLM. Select it
with `--backbone` or `MISEVOLVE_BACKBONE`.

## Harnesses

`configs/harnesses.yaml` defines the framework, image, native skill path, and isolated home
variable for Claude Code, Codex, OpenClaw, and Hermes. Harness definitions contain no model
choice.

## Judges

`configs/judges.yaml` defines two judge groups. The original group uses Gemini-3-Flash for
harmful trajectory and benign completion decisions and Kimi-K2-0905 for skill scores. The
independent group uses Grok-4.5 for every role and is selected with
`MISEVOLVE_JUDGE_GROUP=independent`. Startup validation prevents a judge from matching the
tested backbone.

`null_policy: zero` is part of the paper protocol: an eligible task with no execution or
verdict remains in the denominator and contributes zero. Artifact scores follow a separate
rule: a missing judgment is retried and then stays undefined.

## Grid controls

`scripts/run_main_table.sh` reads `configs/grid.yaml` and accepts environment overrides:

| Variable | Effect |
|---|---|
| `METHODS` | space-separated method IDs |
| `HARNESSES` | space-separated harness IDs |
| `BACKBONE` | backbone ID for every selected cell |
| `GOVERNANCE` | governance policy for every selected cell |
| `GOVERNANCE_CONFIG` | governance JSON applied to every selected cell |
| `SCHEDULE` | exposure or mixing schedule for every selected cell |
| `MAX_TASKS` | truncate each episode for smoke testing |
| `CONCURRENCY` | maximum cells in flight |
| `CELL_TIMEOUT` | seconds allowed per cell |
| `OUT_ROOT` | output root |

## CLI controls

`misevolve` runs one cell. Required flags are `--episode` and `--out`; the remaining axes
have explicit defaults.

```bash
misevolve --help
```

Key controls:

- `--method`: `no-evolution`, `autoskill`, `evoskill`, `skillclaw`, `skillopt`,
  `skillsvote`, or `hermes-native`;
- `--harness`: `cc`, `codex`, `openclaw`, or `hermes`;
- `--schedule`: `early`, `interleaved`, `late`, `batched`, `partial`, or `fully`;
- `--episode-index`: rotation and reversal index for mixed schedules;
- `--governance`: `none`, `utility-only`, or `safeevolve`;
- `--governance-config`: alternate frozen governance JSON;
- `--episodes-json`: alternate episode collection;
- `--max-tasks`: smoke-test truncation.

## SafeEvolve policies

The default and ablation policies live in `src/misevolve/governance/config/`:

| File | Policy |
|---|---|
| `safeevolve.json` | full write-time and reuse-time governance |
| `safeevolve_no_paired_deleter.json` | retain candidates while preserving risk evidence |
| `safeevolve_no_reuse_risk_attribution.json` | disable trajectory-to-skill reuse-risk attribution |
| `safeevolve_no_safety_retirement.json` | disable safety-triggered retirement |
| `utility_only.json` | capacity and utility control without safety evidence |

Pass a policy file with `--governance-config` to keep an ablation explicit in each run
command and output directory.
