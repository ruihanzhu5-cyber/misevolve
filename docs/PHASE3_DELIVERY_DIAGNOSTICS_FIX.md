# Phase 3：交接与错误诊断修复（离线验收）

基线：`dfebdded1f2323fa0940b2495f5b97595a17aa8e`，目标分支：`xskill-stage7`。
依据：已归档的 `chain-after-fixed-b06-qNZ1l80O` 运行。此次不启动模型调用、容器实验或正式 Episode。
旧运行、原始日志、评分和费用记录均保持原样。

## 问题与修改

| 旧日志中的问题 | 修改后的行为 |
| --- | --- |
| B04 的 B→A `SKILL_ACCEPT` 被 Chain 单向边拦截 | Chain 使用 `SKILL_PAYLOAD` 原生导入，已导入版本由服务识别为 `SKILL_FORWARD`。不再提供 offer/accept/reject 握手；路由也拒绝这些消息，不增加反向边。其他组织仍支持握手，提示明确 offer/accept 本身不导入。 |
| B10、B16 按 Skill UUID 查找不存在的文件或 CLI 入口 | 真实 Team Runner 在安装后把 Local ID、名称、实际文件路径、运行时哈希的对应关系加入提示，并存入轨迹。重名目录采用实际安装结果，不按排序猜对应关系。 |
| B11 的 1 次、P21 的 3 次文件交接超过每题 16 个限制 | 保留 16 文件、16 MiB 总量、4 MiB 单文件限制；每个成员看到当前剩余额度。每次分发生成本地回执，失败原因进入发送者下一轮输入，最多提供一次纠正轮次。成功动作不由程序重放；待处理失败存在时不接受最终提交。 |
| B11、B12、P21 只留下 `Unknown error` | Stage 7 API 保存退出码、信号、PID、session ID 和有界 stdout/stderr，注明原始字节数及截断状态。保留结构化 HTTP 错误；Runner 区分 `executor_process_exit` 和一般 HTTP 错误。Stage 7 不自动重试整个 OpenClaw prompt，避免副作用重放。 |
| B16 安全 Judge 返回 `invalid_schema`，不能检查原始返回 | 对启用审计的判分调用保存完整返回、校验错误、输入哈希及账本引用。原有错误仍判为失败，不增加 Judge API 重试，不自动改变评分。 |
| 只能看到 Skill 注入，缺少实际读取证据 | 记录匹配注入路径且有成功工具结果的 `read` 调用为 `SKILL_READ`；失败、无结果、重复结果、其他路径、模型自述均不能产生该证据。读取不是采用，也不建立 `derived_from`。 |

回执属于 harness 对发送者自己动作的本地反馈，不承载反向 Agent 消息或其他成员的私有轨迹。
回执报告交接结果，不自动猜测命令或代替模型重新执行沙箱操作。基础设施异常仍抛出并保存故障现场。
失败回执尚待处理时不接受最终提交；已投递的消息仍按原有组织时序处理，避免阻塞层级团队反馈。
回执和文件额度不加入原生检索查询；仅因纠正而再次进入的成员复用上一次的任务检索输入。

## 证据文件

- 每轮 `trajectory_raw.json` 新增 `_skill_runtime_map`、`_skill_reads`；结构化退出信息在 `prompt_response.detail`。
- `team_events.jsonl` 新增 `DELIVERY_RECEIPT` 和 `SKILL_READ`。读取事件的采用状态仍为 `UNKNOWN`。
- `judge_responses/<unique-id>.json` 保存判分返回；`judge_results.jsonl` 的 `response_ref` 指向它。
- `response_ref` 相对于产生该判分的运行目录。复用 Artifact Judge 缓存时，沿用原审计的 `ledger_run_id`、`run_id` 和响应引用；需保留来源运行归档，不能把缓存引用当成本轮新调用。
- Judge 返回落盘前替换环境中已知的 API Key、Token、Password、Secret 及鉴权头；这些是本地运行证据，不自动进入 Git。
- `team_manifest.json` 记录交接协议版本、纠正次数和文件限制。Judge 返回文件加入检查点哈希校验。

## 兼容性与结论边界

不修改 AutoSkill 上游、Skill 内容、作者和版本映射，不改变 M/B/P 顺序、角色、通信边、任务预算、Probe 冻结、评分规则或失败轨迹不学习的条件。
本次改变了任务交接和提示行为：后续真实验证应使用新 run ID、空 Store 和新目录，不能用新协议恢复旧轮并声称同配置续跑。
同版本代码和配置的合法检查点仍可恢复，新增 Judge 返回也受完整性校验保护。

三个 HTTP 500 前都出现过 `pkill -f`，但旧日志未保留退出信号，尚不能证明因果。
本次不批量改写模型 shell 命令，也不声称已经修好未证实的进程误杀原因。
退出诊断代码在容器 API 中：需要下一次部署时重建实际镜像并记录新摘要，单独更新宿主机 Python 文件不够。

`SKILL_READ` 只证明对应路径的 read 工具返回成功。文件可能在执行中被修改，记录不宣称读到的字节仍等于初始注入哈希。
语义采用仍需额外证据；`exec cat ...` 等其他读取方式未自动计入，不能把读取计数当成全部采用次数。
Utility 对非完整执行的保守处理保留，避免本次修复改变旧实验指标口径。

## 离线验证

| 检查 | 结果 |
| --- | --- |
| 完整 pytest 回归 | 254 项通过；包含原生 AutoSkill 转发、冻结、团队隔离和断点恢复 |
| 新增针对性用例 | 16 项，覆盖禁止反向握手、文件限制回执、有界纠正及不重发、路径映射、读取证据误报、结构化退出、Judge 原文与凭据清除、检查点防篡改 |
| `ruff check src tests docker scripts` | 通过 |
| `python -m compileall -q src docker` | 通过 |
| Shell 脚本 `bash -n` | 通过 |
| `git diff --check` | 通过 |
| 待提交文件凭据及归档检查 | 无真实凭据或私钥；没有修改日志和旧结果 |

`ruff check .` 仍报告旧实验归档中 7 个脚本的 33 项已有问题；均在
`docs/phase3-archive/2026-11-08-chain-skillmisevo-coding-04/logs/` 下。
遵照保留旧日志的要求，不格式化或改写这些归档。正式源码、测试和脚本的检查已通过。

测试使用合成 Skill、AutoSkill `94c47ca488d4ba4117d20272e66d49b9877e68cf`、Mock Runner、模拟供应商返回和模拟子进程。
容器 API 的离线用例执行实际生产函数体，以 Mock 替代 Web 框架和子进程；没有构建或运行新镜像。
不重新运行任何旧任务，不补写旧分数，不调用真实模型 API。
