# Phase 3：后台工具与整题截止

11 月 7 日 Stage 7.2 的 7 次整题超时有不同证据。B05、B11、B12 和 P21 支持后台服务或 Git hook 包装 shell 持有输出流；M08 和 B10 的工具已经返回，截止时在等待模型；M07 的最后一条工具调用距截止约 2.3 秒，不能确定根因。旧结果继续按原状态统计。6 道 M/B 的超时轨迹未进入原生学习；C 的三个 B 都超时，在旧的仅转发本地新增/更新规则下没有产生 C→D 投递。

## 执行规则

- Phase 3 在原固定镜像上开启 OpenClaw 自带的 `process` 工具。`exec` 默认等待 5 秒后给出进程会话，默认工具时限为 120 秒（原生 `timeoutSeconds` 可覆盖）。`exec(background=true)` 管理整条命令，包括包装 shell；stdout 和 stderr 都由进程会话持续接收，普通非交互命令无需继承宿主输入。对需要等待的前台命令，`running` 表示未完成；通过 `process` 查日志、等待终态或请求停止后核对终态。超时保留返回的输出与失败状态。没有 shell 文本改写，也不会自动重放命令。
- 整题的 1,200 秒从隔离容器准备好后开始，由所有成员和轮次共享。截止关闭 relay 派发门，包括重试；已发出的请求继续记账。迟到条目同时索引到 `late_executor_attempts.jsonl`，在主账本只计一次。供应商不能取消的请求不把响应送回 Gateway。
- 截止时暂停当前执行容器，复制 SQLite 主文件及存在的 WAL/SHM，交给同镜像且无网络的独立读取容器。随后移除执行容器，退出任务时清理其他成员容器。Judge 读取已经写好的 `team_trajectory.json` 对应的快照。未确认容器停止时保留证据并跳过判分；超时不会作为正常学习输入。
- `tool_timeout_call_ids` 记录已返回的原生工具超时；`provider_failure` 记录执行模型请求失败类型；`task_deadline_exhausted` 与 `cutoff` 区分整题截止及当时的在途请求。仍在运行且尚无结果的工具只保留已经落盘的调用与已有记录，不推测它的退出码或补造输出。
- Extractor/Maintainer 仍发送完整输入。接收端记录原生响应和格式状态，允许 Extractor 的原生修复流程；最后仍损坏的响应与合法的空候选分别记账。SDK 即使吞掉底层错误，调用方也会保留前后快照并标为失败。没有修改 AutoSkill 上游算法和评分规则。

任意 shell 可以自己创建脱离其会话的后台进程。项目不猜测或改写这些命令，也不把管道结束解释为服务成功；任务容器的移除是最终清理边界。已经完成的外部副作用不会被取消操作回滚。`awaitResults` 会保留原生前台语义，仍受工具时限和整题截止约束。

## 验证与受控恢复

使用 ECS 上实际镜像 `misevolve-openclaw:stage7-2026.11.1`，镜像 ID `sha256:125d9bb28c48cd02cfddc53c557cad61508320d139f6309765ba6d7f6bca5331`，无重新构建。两个脚本只运行合成命令、合成响应和本机隔离 relay，不访问模型供应商：

```bash
docker run --rm --network none -i --entrypoint node misevolve-openclaw:stage7-2026.11.1 \
  --input-type=module - < scripts/check_phase3_process.mjs
PYTHONPATH=src python scripts/check_phase3_runtime.py
```

首个脚本比较旧的同步等待与原生 `process`：继承 stdout/stderr、仅重定向 stdout、Git `post-merge` 外层包装及真实前台等待都先得到可查询的 `running`，时限后得到失败和已保留的输出。两个独立的服务会话可以各自读日志和停止。第二个脚本经过实际 Gateway、工具、SQLite 和 relay，验证正常工具往返、模型迟到及执行中的工具在截止时分别留下什么证据。

ECS 全量离线回归：234 项通过。Ruff（排除冻结的旧日志归档）、Compileall、shell 语法和 `git diff --check` 通过。全仓 Ruff 仍报告旧归档下已有的 33 处风格问题；原始归档没有改动。

正式验收使用新目录和空 Store，`run --stop-after-pos 6 --verify-runtime` 完整完成两块后停下。进程退出后检查完整检查点，再用原配置和目录 `run --resume --verify-runtime`。检查点同时核验迟到调用索引和主账本；仍有未结算的执行器调用时等待，无法结算就不写可恢复的检查点。旧轮的轨迹、49 条调用账本和真实原生预检均独立保留，没有复制到新运行。
