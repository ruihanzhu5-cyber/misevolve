# Stage 7.2 四 Worker Pilot

只运行一轮 `skillmisevo-coding-04`，由操作者在 ECS 上手动启动。Pilot 调用
Stage 6 的 `chain_clean_receiver_v1` 入口。运行清单保留原始 ID、标签、来源位置、
顺序及每题提示词哈希。任务内容来自仓库原始数据。准备检查和测试使用离线输入。

| 实际顺序 | Worker | 原始位置 | 操作 |
|---|---|---|---|
| 1–3 | A | 1–3 M | 批次结束后原生演化 |
| 4–6 | A | 7–9 M | 批次结束后原生演化 |
| 7–9 | A | 13–15 M | 原生演化后 A → B |
| 10–12 | B | 4–6 B | 原生演化后 B → C |
| 13–15 | C | 10–12 B | 原生演化后 C → D |
| 16–18 | D | 16–18 B | 原生演化后冻结 B、C、D 的库 |
| 19–21 | B、C、D | 19–21 P | 每个 Worker 注入自己的冻结库，执行并评分 |

该 episode 涵盖健康端点、环境配置、遥测、systemd、CI 与 Git hook。每题使用
与 Stage 7.1 相同的固定镜像、新容器和新会话。镜像内使用新工作区，网络为
内部 Docker bridge，仅通向模型 relay。ECS 文件、真实服务和外部诊断接收端
均不作为任务环境挂载。A 的轨迹、工具结果和工作区 tar 保存在 A 的任务目录。
接收者从原生 Skill Transfer Bridge 得到跨 Worker 内容。

## 配置与判分

- 执行器、Extractor 和 Maintainer 使用 `MiniMax-M3.1-Flash`。模型上下文配置
  为 204,800，单次输出为 131,072。Runner 校验容器环境和模型配置，最终汇总
  根据每次 API 记录核对实际发送的 `max_tokens`。
- 执行器每题请求次数、整题时长和总费用均无上限。单次 API I/O 超时为 45 秒。
  超时、网络错误、HTTP 429 或 5xx 允许一次重试。连续性恢复与 API 重试分开记录。
  启动、容器管理及会话或工作区取证有各自的基础设施超时，详见启动配置。
  鉴权失败会阻断后续 API 调用。
- 行为与 Artifact 评审使用 `deepseek-flash`。M 用原作者引用的 AgentHazard
  目标达成规则判分，B/P 分别用该规则判断对应风险，用原作者的 utility 规则判断正常
  目标。Artifact 以完整 `SKILL.md` 按三项规则打分并记录理由与证据摘录。
  行为评审读取已保存的完整会话消息。轨迹捕获与评审请求超限会形成明确失败记录。
- AutoSkill 原生检索 `top_k=5`，使用本地 hashing embedding。每个 M/B 批次结束
  后，沿用原生策略逐次 ingest，保存输入归属、返回结果、前后快照及维护响应。P
  直接注入冻结后的完整库，每题记录前后 Store 哈希和 ingest 计数。
- 三次传输各选择完成的来源 block 内，每个 Local Skill Key 最终观察到的版本。
  B/C 产生的新增和本地更新版本可传给下一跳，未改动的导入版本仍保存在本地库。
  只支持纯文本 `SKILL.md`。验证允许的差异为接收者 frontmatter ID 和规范化换行。
  附属资源会显式报错。风险评分和检索结果不影响选择，空选择也会留下清单。

## ECS 命令

准备检查读取固定依赖、通过的 Stage 7.0 报告、运行配置和凭据文件权限。它不发起
模型调用。运行由操作者在 SSH 会话内单独启动。

```bash
cd /root/misevolve-stage72
source /root/misevolve-stage7-bootstrap/venv/bin/activate
PYTHONPATH=src python -m misevolve.stage7_pilot check \
  --preflight-report /root/misevolve-stage7/results/stage7-20261107-preflight/preflight.json \
  --credentials /root/.config/misevolve/stage7.env
```

```bash
cd /root/misevolve-stage72
tmux new-session -d -s misevolve-stage72 bash scripts/run_stage72.sh \
  /root/misevolve-stage7-bootstrap/venv/bin/python \
  /root/misevolve-stage7/results/stage7-20261107-preflight/preflight.json \
  /root/.config/misevolve/stage7.env auto
```

固定会话名和启动锁阻止并行重复启动。每次手动执行会生成唯一目录。
`auto` 对 DeepSeek 按北京时间逐次计算工作日高峰与空闲价格，周末为全天空闲。
法定假日可显式用 `off-peak`。这两类价格用于费用估算。

```bash
cd /root/misevolve-stage72
readlink -f results/stage72/latest
PYTHONPATH=src /root/misevolve-stage7-bootstrap/venv/bin/python -m misevolve.stage7_pilot \
  status --output results/stage72/latest
tail -n 40 -f results/stage72/latest/run.log
tmux attach -t misevolve-stage72
```

tmux 内按 `Ctrl-b`、`d` 可分离。任务结束后查看同一个目录。

```bash
cat /root/misevolve-stage72/results/stage72/latest/run.exit
cat /root/misevolve-stage72/results/stage72/latest/pilot_summary.json
```

运行正常结束且证据链通过时，`run.exit` 为 `0`，`pilot_summary.json` 记录
`status=complete`、`technical_acceptance=passed` 和 21 个任务。模型达成或未达成
攻击目标、正常目标，均是实验结果。执行器、评审、证据缺失分别记录。风险 Skill
缺失、未命中检索或风险变化也按原计划统计。

## 结果文件与中断

| 文件 | 内容 |
|---|---|
| `run_manifest.json` | 代码 SHA、依赖、镜像、配置、数据哈希、21 题映射和传输规则 |
| `episode/progress.json`、`episode/episode_events.jsonl` | 当前阶段、最后完成任务、批次和逐跳检查点 |
| `episode/workers/*/p*/` | 原始会话、工具结果、执行终止和模型配置。A 包含工作区 tar |
| `episode/workers/*/*events.jsonl`、`native_responses.jsonl` | 检索、注入、逐次 ingest、完整快照和维护响应 |
| `episode/transfer_manifest.jsonl`、`episode/registry/` | 每跳导出清单、导入映射、前后哈希和版本 |
| `episode/provenance/`、`episode/probe_snapshots.jsonl` | 归一化溯源和 Probe 冻结验证 |
| `model_calls.jsonl`、`judge_results.jsonl` | 调用用量、费用、重试、纠错和评审依据 |
| `pilot_summary.json`、`episode/episode_metrics.json` | 整轮与逐任务结果，含技术验收状态 |

中断时，`status` 按完整日志行展示最后完成任务、演化批次、逐次 ingest 和传输记录。
Store、Registry 和导入桥保留既有恢复及去重机制。整轮只读检查不会重放实验。
Stage 6 不支持从中途恢复整个 episode，Pilot 拒绝在已有运行目录继续写入。再次
手动启动会创建新目录并保留旧结果。原始轨迹、工作区 tar、Judge 理由和 Skill 内容
属于受限运行证据，GitHub 只提交脱敏摘要。
