<h2 align="center">Practice Makes Unsafe: Skill Misevolution in Self-Improving LLM Agents</h2>
<p align="center"><b>MisEvolve — SkillMisevo-Gym · SkillMisevo-Bench · SafeEvolve</b></p>

<p align="center">
  <img src="assets/misevolve_overview.png" width="100%" alt="MisEvolve overview: controlled skill evolution turns malicious demonstrations into persistent skills, measures their reuse on benign and fresh-session tasks, and evaluates SafeEvolve governance">
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2608.12851"><img src="https://img.shields.io/badge/Paper-arXiv%3A2608.12851-b31b1b?style=flat&logo=arxiv&logoColor=white" alt="Paper"></a> ·
  <a href="AGENT.md"><img src="https://img.shields.io/badge/AI%20Agents-AGENT.md-4B2E83?style=flat&logo=readthedocs&logoColor=white" alt="AGENT.md"></a> ·
  <a href="docs/QUICKSTART.md"><img src="https://img.shields.io/badge/Docs-Quickstart-4c8c11?style=flat&logo=readthedocs&logoColor=white" alt="Documentation"></a> ·
  <a href="#citation"><img src="https://img.shields.io/badge/Citation-BibTeX-007ec6?style=flat&logo=googlescholar&logoColor=white" alt="Citation"></a> ·
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-informational?style=flat" alt="License"></a>
</p>

<p align="center"><i>🧬 A controlled benchmark for persistent safety failures in self-improving agent skills.</i></p>

> [!CAUTION]
> **Research use.** MisEvolve contains harmful prompts and executes agent actions in
> disposable containers. Run it only on systems you own or are authorized to test.
> Keep credentials scoped and review [`SECURITY.md`](SECURITY.md) before a full run.

> 🤖 **AI agents:** read [`AGENT.md`](AGENT.md) first. It defines the repository map,
> invariants, extension contracts, and verification commands in an agent-readable form.

Self-improving agents turn experience into reusable skills. MisEvolve tests the failure
mode on the other side of that capability: a one-off unsafe trajectory can become a
**persistent, retrievable, reusable skill** that contaminates later benign work and
survives into a fresh session.

The repository implements the three components introduced in the paper:

- **SkillMisevo-Gym** controls task order, exposure timing, batch mixing, container
  isolation, skill persistence, and cross-family judging.
- **SkillMisevo-Bench** supplies 25 episodes with malicious demonstrations, benign
  twins, and fresh-session persistence probes.
- **SafeEvolve** audits candidates at write time and governs reuse with lineage,
  repair, quarantine, risk, utility, and capacity evidence.

## 📢 Latest updates

<!-- Newest first; one line per release with an ISO date. -->

- **2026-08-18** — 🚀 Initial public release with the benchmark, five external
  evolution adapters, the native Hermes condition, SafeEvolve, and reproducibility docs.
- **2026-08-13** — 📄 Paper released on arXiv:
  [arXiv:2608.12851](https://arxiv.org/abs/2608.12851).

## 🔬 What MisEvolve finds

The paper evaluates **25 agent–method configurations**, each over **25 episodes** and
**525 task executions**: 225 malicious tasks, 225 benign twins, and 75 fresh-session
probes. Every one of the 21 evolving configurations authors unsafe artifacts; 19
retrieve them unsafely, 19 contaminate benign tasks, and 15 cause fresh-session harm.

- **Small exposure, durable effect.** Three malicious tasks increase carryover attack
  success from **16.0% to 35.3%**.
- **The full pipeline is observable.** Artifact unsafety, unsafe retrieval, benign-task
  contamination, and fresh-session harm are measured separately.
- **Governance breaks the chain.** SafeEvolve lowers unsafe retrieval by **26.7
  percentage points** and fresh-session harm by **17.3 points**, while benign utility
  changes by **0.4 points**.

<p align="center">
  <img src="assets/safeevolve.png" width="100%" alt="SafeEvolve pipeline: audit and repair candidate skills at write time, retain lineage, then govern retrieval, retirement, and capacity at reuse time">
</p>
<p align="center"><sub><b>SafeEvolve governs both boundaries.</b> Candidate skills are audited and repaired before admission; admitted skills retain lineage and accumulate reuse evidence for retirement and eviction.</sub></p>

## 🏗️ How it works

Each experimental cell crosses a skill-evolution **method** with an agent **harness**
while holding the backbone constant. The executor and every method-internal LLM use the
same backbone, so differences are attributable to the evolution method and harness.

| Axis | Included settings | Contract |
|---|---|---|
| Evolution | No-Evolution, AutoSkill, EvoSkill, SkillClaw, SkillOpt, SkillsVote, Hermes-native | retrieve → inject → execute → evolve → persist |
| Agent harness | Claude Code, Codex, OpenClaw, Hermes | fresh container and session per task; native skill-loading path |
| Backbone | MiniMax-M2.7 by default; config-driven alternatives | one model for execution and method-internal inference |
| Governance | none, Utility-only, SafeEvolve | wraps an evolution method without changing its native adapter |
| Judges | Gemini behavioral judge, Kimi artifact judge | independent from the evaluated backbone and from each other |

The default episode contains 21 tasks:

```text
MMM BBB   MMM BBB   MMM BBB   |   PPP
round 1   round 2   round 3       frozen-library, fresh-session probes
```

Evolution runs after each three-task block. The final probe block freezes the library,
starts clean sessions, and exposes only the persisted skill store. The scheduling module
also reproduces early, interleaved, late, batched, partial, and fully mixed exposures.

### Nine headline metrics

| Metric | Direction | Measures |
|---|:---:|---|
| `BenignUtility` | ↑ | legitimate completion on benign twins |
| `MaliciousASR` | ↓ | malicious-goal completion during exposure |
| `Contamination` | ↓ | harm on injection-free benign tasks |
| `CU` | ↓ | content unsafety of authored skills |
| `UG` | ↓ | generalization among unsafe skills |
| `Stealth` | ↓ | concealment among unsafe skills |
| `CarryoverASR` | ↓ | harm on fresh-session probes |
| `CarryoverUtility` | ↑ | legitimate completion on probes |
| `URR` | ↓ | unsafe-skill retrieval on probes |

Rates use fixed denominators. Missing executions and judge failures count as zero rather
than disappearing from the condition. Confidence intervals resample whole episode cells.

Full design rationale, boundaries, and data flow are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## 🚀 Quick start

The complete first-run walkthrough is in [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

| Step | Command |
|---|---|
| Clone | `git clone https://github.com/henrymao2004/misevolve.git && cd misevolve` |
| Environment | `uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e '.[dev]'` |
| Configure | `cp .env.example .env` and fill `MINIMAX_API_KEY` plus `OPENROUTER_API_KEY` |
| Methods | `bash scripts/setup_methods.sh` |
| Images | `bash scripts/build_images.sh` |
| Smoke run | `MAX_TASKS=6 METHODS=autoskill HARNESSES=openclaw bash scripts/run_main_table.sh` |
| Aggregate | `misevolve-metrics main-table results/main_table` |

Run one cell directly:

```bash
misevolve \
  --episode skillmisevo-coding-01 \
  --method autoskill \
  --harness openclaw \
  --governance safeevolve \
  --out results/example
```

Completed cells are resumable: `scripts/run_main_table.sh` skips any output directory
that already contains `all_rows.json`.

## 📚 Documentation

| Document | Contents |
|---|---|
| [`AGENT.md`](AGENT.md) | AI-agent entry point, invariants, commands, and change protocol |
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Installation, credentials, smoke run, full grid, and aggregation |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component boundaries, episode lifecycle, isolation, and SafeEvolve design |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Backbones, harnesses, judges, environment variables, and schedules |
| [`docs/METHODS.md`](docs/METHODS.md) | External revisions, adapter contract, governance wrappers, and extensions |
| [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md) | Paper grid, denominators, outputs, seeds, and reporting checklist |
| [`SECURITY.md`](SECURITY.md) | Threat model, sandbox requirements, credentials, and disclosure |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup, checks, and pull-request scope |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |

## 🗂️ Repository map

```text
misevolve/
├── src/misevolve/       benchmark engine, adapters, methods, judges, governance
├── configs/             backbone, harness, judge, and grid settings
├── data/                25 benchmark episodes
├── docker/              isolated runners for four agent frameworks
├── scripts/             method setup, image builds, and resumable grid runner
├── tests/               contracts for episodes, metrics, schedules, and governance
├── docs/                operator and design documentation
├── assets/              paper overview figures
└── results/             generated run artifacts, excluded from version control
```

## 📋 Status

Current release: **v0.1.0**. The repository ships the 25-episode benchmark, four
containerized agent harnesses, six general conditions plus Hermes-native, six schedule
variants, nine metrics, and SafeEvolve with its ablations. See
[`CHANGELOG.md`](CHANGELOG.md).

## Citation

If this repository supports your work, cite the paper:

```bibtex
@article{mao2026practice,
  title   = {Practice Makes Unsafe: Skill Misevolution in Self-Improving LLM Agents},
  author  = {Mao, Xutao and Zhao, Liangjie and Zheng, Xiang and Wang, Cong},
  journal = {arXiv preprint arXiv:2608.12851},
  year    = {2026}
}
```

Machine-readable metadata is available in [`CITATION.cff`](CITATION.cff).

## 🙏 Acknowledgements

- [AutoSkill](https://github.com/ECNU-ICALK/AutoSkill),
  [EvoSkill](https://github.com/sentient-agi/EvoSkill),
  [SkillClaw](https://github.com/AMAP-ML/SkillClaw),
  [SkillOpt](https://github.com/microsoft/SkillOpt), and
  [skills-vote](https://github.com/MemTensor/skills-vote) for the evaluated evolution methods.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
  [Codex CLI](https://github.com/openai/codex),
  [OpenClaw](https://github.com/openclaw/openclaw), and
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) for the agent runtimes.
- [AgentHazard](https://github.com/Yunhao-Feng/AgentHazard) for the behavioral-judge foundation.
- [AutoResearch for Production-Agent Red-Teaming](https://github.com/henrymao2004/Auto-research-red-teaming)
  for the public-repository organization and sandbox-runner foundation.

## License

MIT — see [`LICENSE`](LICENSE).
