# Stage 7.2 ECS archive

The completed continuation is `stage72-20261107T113944Z-gvxgWN`. It ran at code commit
`c01f7c4e5b25067cec676342c1193b325935c5eb` on `xskill-stage7` and continued from
`stage72-20261107T095203Z-3OwUHd`. The release tag points to the continuation's code commit.

The run manifest and package inventories in this directory were captured from the ECS host.
The host used Ubuntu 24.04, Python 3.12.3, Docker 29.1.3 and OpenClaw 2026.11.1.
`autoskill-commit.txt` pins the exact AutoSkill revision. `openclaw-image.txt` records the
image ID, tag, platform and size. `python-requirements.lock` lists the distributions installed
in the run's virtual environment, including the locally installed MisEvolve project.

The native archive preserves the directory and file structure from ECS. It includes both
Stage 7.2 runs and the Stage 7 preflight, smoke runs and replays under `misevolve-stage7/results/`.
The companion environment archive includes the build and launch inputs alongside these inventories.
[GitHub Release](https://github.com/ruihanzhu5-cyber/misevolve/releases/tag/stage72-ecs-backup-20261107)

Download the release assets to a fresh directory. Check the archived bytes before extracting
the runs and the environment or loading the exact container image.

```bash
sha256sum -c SHA256SUMS
sha256sum -c IMAGE.SHA256
tar -xzf misevolve-native-runs.tar.gz
tar -xzf misevolve-environment.tar.gz
zstd -dc openclaw-stage7-2026.11.1.docker.tar.zst | docker load
```

The reconstructed Stage 7.2 run directories contain `run.log`, `run_manifest.json`,
`model_calls.jsonl`, `judge_results.jsonl`, `pilot_summary.json` and `episode/`. `episode/`
contains the task traces, native Stores, Skill Registry, transfer manifests and provenance.
