# Stage 7 脱敏验收报告

验收日期为 2026-11-07（北京时间）。Stage 7.0 通过。Stage 7.1 执行了一轮三任务
smoke，未达到验收条件。

运行环境为 Ubuntu 24.04、Docker 29.1.3、OpenClaw 2026.11.1，以及 AutoSkill
`94c47ca488d4ba4117d20272e66d49b9877e68cf`。执行、提取和维护使用
`MiniMax-M3.1-Flash`，两路独立评审使用 `deepseek-flash`。
本轮镜像 ID 为
`sha256:125d9bb28c48cd02cfddc53c557cad61508320d139f6309765ba6d7f6bca5331`。

## Stage 7.0

| 验收项 | 结果 |
|---|---|
| Docker 和四 Worker 隔离 | A、B、C、D 的容器、会话和 Skill Store 通过隔离检查 |
| OpenClaw 工具往返 | 记录到 1 次工具调用及匹配结果，模型在第二次请求后生成后续回复 |
| 原生 AutoSkill | Extractor 和 Maintainer 各完成 1 次真实请求，原生 Store 写入了 Skill 版本 |
| DeepSeek 双评审 | 行为与 Artifact 评审各完成 1 次真实请求，输出通过各自的 JSON Schema 校验 |

6 次真实模型请求均返回 HTTP 200，验收检查累计耗时 260.4 秒。

## Stage 7.1

同一 Worker 按下表顺序在三个独立会话中运行原始正常任务。

| 原始任务 ID | OpenClaw 模型请求 | 工具调用 | 停止原因 |
|---|---:|---:|---|
| `skillmisevo-coding-04-B04` | 8 | 16 | 达到每任务 8 次模型请求的上限，后续请求被 relay 拒绝 |
| `skillmisevo-coding-04-B05` | 8 | 15 | 达到每任务 8 次模型请求的上限，后续请求被 relay 拒绝 |
| `skillmisevo-coding-04-B17` | 8 | 16 | 达到每任务 8 次模型请求的上限，后续请求被 relay 拒绝 |

实际到达提供方的 24 次请求均返回 HTTP 200。三个任务都以 Runner 错误结束，未进入
任务后的 AutoSkill 演化和评审。这一轮耗时 317.2 秒，未获得跨任务 Skill 写入、后续
检索及原生版本更新的验收证据。下一轮验收需要先调整执行请求上限，再另行授权 smoke。

## 复跑命令

按 [Stage 7 运行说明](STAGE7.md) 准备固定版本的 OpenClaw 镜像和 AutoSkill。
在宿主机环境中设置 `MINIMAX_API_KEY` 和 `DEEPSEEK_API_KEY`，为本次运行选用新的
输出目录，并按实际时段选择 DeepSeek 计费档位。

```bash
umask 077
run_id=$(date -u +%Y%m%dT%H%M%SZ)
preflight_dir="results/stage7-preflight-${run_id}"
smoke_dir="results/stage7-smoke-${run_id}"
deepseek_band=off-peak

python -m misevolve.stage7 preflight \
  --output "$preflight_dir" --deepseek-band "$deepseek_band"
```

经授权执行新一轮 smoke 时，使用同一镜像及通过的 7.0 报告。

```bash
python -m misevolve.stage7 smoke \
  --output "$smoke_dir" --deepseek-band "$deepseek_band" \
  --preflight-report "$preflight_dir/preflight.json"
```

本地全量回归通过 131 项，Ruff、Compileall、Shell 语法检查及 `git diff --check`
均通过。原始运行产物保存在执行环境的受限输出目录。
