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
