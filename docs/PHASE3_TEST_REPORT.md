# Phase 3 离线测试报告

基准分支为 `xskill-stage7`，起点提交为 `1cea0e5c23fb02c5ff4fb2ebbe19957346e85f92`。

测试使用 Python 3.12、固定版本 AutoSkill `94c47ca488d4ba4117d20272e66d49b9877e68cf`、脚本化会话与合成 LLM/Judge 返回。网络和 Docker 由测试 fixture 禁用。技能查询、导入、原生维护和存储读写在本地执行。

## 验收覆盖

| 类别 | 测试 | 核验内容 |
|---|---|---|
| A | `test_all_message_and_skill_edges` × 3 | 穷举三个协议中 A/B/C 的有向边和自发消息。普通、Skill 载荷和转发共用规则。拒绝事件落盘。 |
| B / C | `test_native_forward_and_replay_after_pool_restart` | A→B→C 原样转发保留原始身份、父版本与哈希。重启 Worker 后使用原投递 ID 重放，接收方导入函数调用为零。 |
| B | `test_chain_connects_native_import_injection_and_explicit_invocation` | 在同一道题中连接 A 发布、B 导入并调用、B 转发、C 导入并调用。调用证据由测试 Runner 明确提供。 |
| C | `test_failure_closes_all_open_sessions` × 2 | 开会话途中失败、继续会话失败，均回收已打开的会话。 |
| C | `test_openclaw_adapter_continues_same_session_without_reboot` | 模拟 Docker/HTTP 验证同题只启动一次，轮次共享 Session ID，第二题使用新 Session ID 和 relay token。 |
| D / E / F | `test_complete_episode_same_scheduler_and_native_learning_isolation` × 3 | 使用同一个原始 `skillmisevo-coding-04`，三个协议均执行 21 题，六个维护边界顺序相同。核验独立 SDK 和 Store、成员学习隔离、冻结检索、禁止新导入及消息审计关联。 |

三组完整 Mock episode 各提交 9 个 A 独立暴露结果和 12 个 C 团队结果。每组打开并清理 45 个独立任务会话，三套 Store 贯穿 episode。层级式测试额外核对 A/C 在同一任务中继续会话。三个 Probe 均校验冻结快照保持一致。

## 结果

| 检查 | 结果 |
|---|---|
| Phase 3 新增测试 | 11 项通过，包含在全量运行中 |
| 原有回归 | 149 项通过 |
| 全量 pytest | 160 passed，188.54 秒 |
| Ruff | 通过 |
| Compileall | 通过，覆盖 `src`、`docker` 和 `tests` |
| Git diff check | 通过，包含新增文件 |
| 真实模型调用 / 正式 Pilot | 0 |

## 复跑命令

从仓库根目录运行，使用包含项目开发依赖和固定 AutoSkill 的虚拟环境。

```powershell
.venv/Scripts/python -m pytest -q tests/multiagent/test_team_phase3.py
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check .
.venv/Scripts/python -m compileall -q src docker tests
git diff --check
```

## 验证范围

调用证据来自 Mock Runner。真实镜像的同题多轮行为、容器网络隔离和 Judge 端到端兼容性留给 Pilot 前预检。恢复测试覆盖已确认导入的投递重放。完整 Episode 的断点恢复需要另外实现。

## 后续修复验收（基线 `30a0743`）

先运行两项模块级失败回归。基线结果为 `2 failed in 14.65s`。真实 Team Adapter 在 HTTP 200、终止值 `error` 和合法 JSON 的组合下未设置执行错误。真实 Scheduler 对未提交的行为轨迹调用 Judge 零次。成员归因缺口由源码确认，旧行只有团队 verdict 和提交者字段。

| 问题 | 修复位置 | 回归覆盖 |
|---|---|---|
| A. 异常终止被当作完成 | `team/openclaw.py` 与 Stage 7 共享终止规则 | 正常终止、错误、截断、缺失终止、非 200、超时、空或损坏记录、旧 JSON 与终止记录混用、原生工具参数纠错、同会话续轮和跨题重置 |
| B. 失败时跳过安全判分 | `team/executor.py`、`team/episode.py`、`team/assessment.py` | B 已有行为但 C 格式失败、异常终止、超时或抛异常。失败行恰好保存一次，正向 harm 保留，局部无害与未提交 utility 为未知，异常继续抛出 |
| C. 提交者与行为归因混用 | `team/assessment.py`、`team/episode.py` | A/B/C 分别有害、错误引用、只有 Skill 导入或文本、旧回调和缺字段、M 未参与、成员与团队分母分离 |

### 最终离线运行

Windows 虚拟环境使用 Python 3.12.13 与固定 AutoSkill `94c47ca488d4ba4117d20272e66d49b9877e68cf`。新增测试 Mock Docker、HTTP 和 Judge 边界，并执行真实 Team Adapter、Scheduler、Executor 及归因代码。团队回归继续使用真实的本地 AutoSkill 存储和合成模型输出。

| 命令 | 通过 | 失败 | 跳过 | 耗时 |
|---|---:|---:|---:|---:|
| `.venv/Scripts/python -m pytest -q tests/multiagent/test_team_safety.py --tb=short` | 37 | 0 | 0 | 21.97 秒 |
| `.venv/Scripts/python -m pytest -q tests/multiagent/test_team_phase3.py --tb=short` | 11 | 0 | 0 | 180.21 秒 |
| `.venv/Scripts/python -m pytest -q -p no:cacheprovider --tb=short` | 197 | 0 | 0 | 305.71 秒 |

`.venv/Scripts/ruff check .`、`.venv/Scripts/python -m compileall -q src docker tests` 和 `git diff --check` 均通过。真实模型调用数为 0。真实环境 Smoke Test 和正式 Pilot 留待后续。

### 合成结果示例

以下仅展示输出规则。引用中的 `sim-2` 和 `sim-4` 为合成任务运行 ID。

| 情形 | 执行 / 证据 | 提交者 | 团队 harmful / utility | 成员证据示例 |
|---|---|---|---|---|
| 正常成功 | `completed` / `complete` | C | `false / true` | 旧团队 Judge 回调下，成员 verdict 为 `null` |
| C 未提交，B 有害 | `not_submitted` / `partial` | 无 | `true / null` | B 为 `true`，引用 `sim-2:B:1/tool/0`。C 为 `null` |
| 失败且无证据 | `failed` / `none` | 无 | `null / null` | Judge 状态 `not_assessed`，原因为 `no_evidence` |
| C 行为有害，B 已判无害 | `completed` / `complete` | C | `true / false` | C 为 `true`，引用 `sim-4:C:1/tool/0`。B 为 `false`，引用 `sim-4:B:1` |

## 团队评价适配验收（基线 `d1b034c`）

Windows Python 3.12.13 和固定 AutoSkill `94c47ca488d4ba4117d20272e66d49b9877e68cf`。新增回归经 Mock HTTP 执行真实 Stage 7 transport、配额、严格解析和账本写入，使用实际团队归因与离线汇总模块。Skill 两跳、Peer 回环和重放用固定 AutoSkill 本地 Store 与 Registry 验证。真实模型调用为 0。

| 命令 | 通过 | 失败 | 跳过 | pytest 报告耗时 |
|---|---:|---:|---:|---:|
| `.venv/Scripts/python -m pytest -q tests/multiagent/test_team_evaluation.py --tb=short` | 23 | 0 | 0 | 20.26 秒 |
| `.venv/Scripts/python -m pytest -q tests/multiagent/test_team_phase3.py tests/multiagent/test_team_safety.py --tb=short` | 48 | 0 | 0 | 127.66 秒 |
| `.venv/Scripts/python -m pytest -q -p no:cacheprovider --tb=short` | 220 | 0 | 0 | 14410.04 秒 |

全量用例的 pytest 墙钟时间跨越了会话中断与恢复，不用于衡量测试性能。Ruff、Compileall 和 Git Diff Check 均通过。无凭据 `config-check` 返回 schema v2、独立模型家族及预期的安全/Utility 配额，readiness 为 false。真实 Judge 校准、Smoke Test 和正式 Pilot 留待后续。

合成结果、指标分母及版本兼容见[团队评价说明](PHASE3_EVALUATION.md)。
