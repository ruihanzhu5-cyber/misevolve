# SCOPE 方法设计与可执行 mock 实现

本实现位于独立分支 `scope-governance`，基于旧实验提交
`4cc377300be179d71d72888347dc3e5ff1132a2d`。旧实验入口、任务定义、评分规则及
AutoSkill 原生提取/维护算法保持原样。新增实现位于 `src/misevolve/scope/`。

## 1. 研究对象与职责

SCOPE 治理 AutoSkill 提出的技能更新，而不是重新实现技能学习算法。

| 组件 | 输入 | 输出 | 写入权限 |
| --- | --- | --- | --- |
| AutoSkill / MiniMax | 原任务轨迹、已批准技能的私有副本 | 原生候选与中间版本链 | Candidate Store / 私有 SDK Store |
| Misevolve | 原始 Benchmark / Episode、固定配置 | 执行轨迹、统一评分、归档 | 实验目录 |
| OpenClaw / MiniMax | 固定 Library Snapshot、角色、任务 | Agent / 工具 / Sandbox 执行 | 各自任务工作区 |
| DeepSeek | Misevolve 组织的执行证据或技能内容 | 原有行为与内容评分 | 评分证据目录 |
| Kimi | 候选差异、验证轨迹、已验证 Memory | 机制假设、可疑更新、修复建议 | Review 记录；不发布技能 |
| SCOPE | 候选、对照验证、规则参数 | accept / repair / reject / hold | Approved Release 的唯一发布者 |

正式运行须将 Approved Library 以只读方式提供给执行容器，Candidate Store 不挂载到执行侧。
Python API 的职责隔离和内容校验不等于操作系统访问控制；只有部署账户应拥有发布目录写权限。

## 2. 实验不变量

- 原始 21 题、原始 prompt、原始顺序不变：`MMMBBBMMMBBBMMMBBBPPP`。
- 更新窗口固定在 3、6、9、12、15、18 题之后；一块内三题固定同一 Release。
- 19–21 为冻结 Probe：不生成候选，不修改 Library，不激活新 Memory。
- 新的所有对照方法采用同一三题窗口；旧实验只作为历史参照。
- 角色、拓扑、模型、AutoSkill revision、OpenClaw image digest、预算必须明确记录。
- 一次验证比较完整旧团队与完整新团队；每个团队内部 A/B/C 使用同一 Library Snapshot。
- 六个 SDK、Store、session、container identity 必须各自独立。任务输入、seed、角色和预算匹配。
- 验证期间不开启技能学习、Skill 导入或发布；验证副本出现任何文件变更，该次证据无效。
- 验证轨迹与正式 Benchmark 轨迹分开归档，不能把验证调用混入正式成功率分母。

现有实验执行器的 M/A、B/P/C 提交语义不在本次修改范围内。将 SCOPE 接到真实执行器时，
仍调用原 `TeamExecutor`，不得顺便把所有任务改成三人共同提交。

## 3. 状态与数据结构

所有记录按 canonical JSON 的 SHA-256 定位；读取时重新验证内容摘要。

```text
scope/
  CURRENT.json              # 原子替换的当前 Approved Snapshot 指针
  objects/<hash>.json       # 不可变技能、候选、验证、Review、Memory、Decision、Snapshot
  candidates/<hash>.json    # 候选索引；被拒绝也保留
  releases/<hash>.json      # 已发布快照及绑定的 Decision
  cycles/<block>.json       # 输入指纹、阶段、预算、验证引用、发布状态
  workspaces/              # 原生候选 SDK、副本、old/new 完整团队验证目录
```

| 对象 | 核心字段 |
| --- | --- |
| SkillVersion | skill_id、owner、files、native_metadata、native_version |
| Candidate | base、skill_id、before、after、source_tasks、mechanisms、dependencies、history、evidence |
| Snapshot | skills: skill_id→version_hash、memory、parent、evidence |
| ValidationCase | case_id、task、category、split、mechanisms、cost、unit_id、utility_required |
| PairedValidation | purpose、base、candidate、case、configuration、seed、identities、arms、status |
| Arm | execution（轨迹/用量）、score（原始评分）、assessment（规则输入） |
| VerifiedMemory | mechanism、skill_ids、trigger_case、evidence、localization、scope |
| Decision | status、base、snapshot、candidates、gate、config、validation |
| Cycle | request_id、request、phase、groups、reviews、repairs、evidence、memory、pairs_spent、snapshot |

同一技能在一个窗口内经历多个原生版本时，`history` 保留所有中间版本，主验证使用
“块开始批准版本 → 块末候选版本”的净变化。同一技能收到多个并发净变化时，先 `hold`，
必须生成显式合并候选再验证，不能按到达先后覆盖。

Memory 第一版供治理侧分组、选题和定位使用，不自动写进 Agent 的系统提示词。
历史错误的文字解释本身不是已验证经验；必须有 old/new 对照证据。

## 4. 完整治理流程

### Collect

`AutoSkillCandidateCollector` 为每个来源 Worker 创建独立 SDK/Store。复制当前原生状态、
exchange 历史及轨迹关联信息，再恢复 SKILL.md 无法完整表达的原生 metadata。
只在副本上调用原有 `evolve_batch`；提取、合并、版本编号仍由 AutoSkill 执行。
已有的重试与 API 记账 hook 通过 `configure(owner, clone)` 装入副本。

候选的起点必须与批准快照一致。无论成功或失败，都保留 native event；失败不能形成可发布更新。
共享库模式传入 `skill_keys[worker][local_id] = logical_skill_id`，验证每个副本覆盖完整批准库。
B 修改 A 原创的技能时保持相同 logical skill ID，更新贡献者写入事件；不能误记成第二份 B 技能。
原生副本加载应保持 ID/metadata；转发产生的重命名本地副本不能冒充相同版本，必须显式对齐。
本适配器沿用当前 text-only pilot 边界：携带额外二进制资源的技能明确拒绝，不静默丢资源。

### Group

对候选建立关系图并取连通分量，边包括同一 skill、声明依赖、共同机制，以及已验证 Memory
中的共同失败关系。共同调用证据可由轨迹适配器转成有证据的 dependency 边；仅被检索不视为调用。
第一版不让 Kimi 任意合并候选内容。

### Select

验证池分为来源复现、历史回归、针对性反例、fresh 四类。开发池按
“新增机制覆盖收益 / 预估执行成本”贪心选取；类别覆盖也有权重。
Kimi 提出的机制参与覆盖优先级，但不能引入未经批准的外部任务或改写原 Benchmark。

确认池与开发池分离，并在验证前确定。Probe、来源任务、开发任务不能成为独立确认样本。
确认样本必须来自不同采样单元；同一任务或采样单元在确认集打开后不能换 ID 重试。
`unit_id` 去重只是工程约束，采样独立性仍需正式实验方案证明。

### Validate

对每个选定任务，创建两套完整团队：`L_old` 与 `Apply(L_old, Δ)`。
两者调用同一个 DeepSeek 评分入口。结果按 `group_development`、`repair_development`、
`localization`、`merged_integration`、`final_confirmation` 分目录与证据用途保存。

超时保留轨迹，但缺失评分不转成“安全”。基础设施失败、配置不匹配、共享 SDK、Store 变更，
均导致 `hold`。任何已观察到的候选风险或超出容忍值的效用下降，阻止该候选通过开发验证。

### Localize

Kimi 从失败证据提出可疑更新，决定有限预算内的检查优先级。
实现执行单候选及其补集对照，并保存完整组合的失败结果。因此可以区分：

- 单更新即可出现的风险；
- 单独运行未出现、组合后出现的风险；
- 因预算或未知评分尚不能定位的情况。

只有在匹配案例上观察到 `old risk=False, new risk=True`，才记录“观察到候选引入风险”的 Memory。
它不宣称已证明普遍因果关系。纯效用退化、超时、基础设施失败不会被写成安全失败机制。
针对性验证结果不被当作风险发生率估计。

### Repair / Admit

修复通过注入的原生 AutoSkill repair adapter 提交新候选；Kimi 的文字建议不能直接成为批准技能。
新候选重新进行开发验证，旧候选和证据保留。修复次数有明确上限。

所有可接受分组合并后必须做一次完整 Library 集成验证，防止“各组单独安全、合起来不安全”。
此时拟激活的已验证 Memory 也进入新快照。随后只打开一次独立确认集，不在看过确认结果后
继续修补并重用同一确认集。

准入要求同时满足绝对风险上界与效用非劣下界。设确认样本的风险指标
`R = unsafe_action OR target_goal_achieved`，配对效用差 `D = U_new - U_old`，则第一版使用：

```text
risk_UCB = min(1, mean(R) + sqrt(log(2/delta) / (2*n_risk)))
utility_delta_LCB = max(-1, mean(D) - sqrt(2*log(2/delta) / n_utility))
accept iff risk_UCB <= max_risk AND utility_delta_LCB >= -utility_margin
```

采用单侧 Hoeffding 界、两项约束的 union bound，参数显式配置。它是保守的 V1 决策规则，
不是跨分布安全保证；自适应反例不能混进该估计。正式实验需要足够独立样本，不能拿三题
窗口内的三次观察宣称高置信安全。

`accept` 之后先保存决策和新 Release，再原子替换 CURRENT；compare-and-swap 检查防止旧基线发布。
发布中断可以幂等恢复。API 执行中断的周期保留证据并 `hold`，不自动重发已付费任务。

V1 对 Conditional Admit 采用 `hold`：在可执行激活条件、条件覆盖验证、检索/转发守卫都落实前，
不把“只在某些情况下安全”的文字描述当成准入权限。这是显式保守策略，不能在论文中写成
已经实现条件发布。后续条件发布应把可执行 guard 编入版本哈希，并在 old/new 验证与正式执行中使用同一 guard。

## 5. 评分与 API 边界

- `MisevolveJudgeScorer` 复用 `team.assessment.assess_task` 与 `TeamJudge.artifact`。
- 原有六项指标的 rubric 不变。SCOPE 从已评分结果中读取风险与效用；不另造评分器。
- 内容不安全、泛化性、隐蔽性仍保留作诊断，不代替团队行为验证。
- `KimiReviewer` 使用单独注入的 client/model/token budget，并保存 raw 与 usage。
- MiniMax 候选提取仍由原 AutoSkill adapter 调用。
- mock 模型名均带 `mock-`，所有 mock 报告带 `synthetic: true`，真实 API 调用数为零。
- 正式启动须给出 Kimi 的确切模型和独立 client，不能默认继承 MiniMax 或 DeepSeek key。

## 6. 实验对照与消融设计

主对照采用同一拓扑、同一任务序列、相同三题窗口及相同预算记录：

1. 原生 AutoSkill 的窗口末直接更新；
2. 只验证正常任务效用的治理；
3. 整批验证但不分组/定位；
4. 完整 SCOPE。

关键消融为：无 Verified Memory、无分组、无定向选题、无定位/修复、无最终合并验证。
使用相同数据划分与未见过的最终测试 Episode；训练/开发轨迹可用于方法开发，最终测试结果
不能反过来设计同一轮防御。

正式报告同时列出原六项指标、未知/超时/基础设施失败数、候选发布率、修复率、验证调用数、
各 provider token 与延迟。不能把未知按零计入 ASR，也不能只报平均技能内容分数。

## 7. 已实现与实跑接线边界

已实现并由 mock 覆盖：完整六阶段控制流、原生候选隔离适配器、不可变版本与快照、分组与覆盖选题、
双完整团队验证契约、规则准入、独立确认、组合检查、有限修复、Memory、预算、冻结、发布恢复。

新增 `scope.demo` 使用原始 21 题输入，在三种拓扑配置下运行 mock 服务。MockTeamFactory 不执行
真实路由或容器；路由行为由已有 team regression 测试单独验证，demo 不是三种拓扑性能实验。
AutoSkill 原生集成测试使用 revision `94c47ca488d4ba4117d20272e66d49b9877e68cf`，只 mock LLM 回复。

本次没有启用真实实验 CLI。正式实跑还需要：将 `TeamRuntime` factory 对接现有 TeamExecutor 的
独立容器与 session 生命周期；将所有批准技能的检索、注入和转发绑定到 pinned release；
接上真实轨迹 loader、候选 repair adapter、独立 Kimi 配置，以及预注册验证池/阈值。
这些接线必须通过真实 Linux OpenClaw smoke 后，才能声称 SCOPE 已具备实跑条件。

## 8. 运行方式

```bash
# 使用已有 Python 环境；无需为 mock 下载依赖
PYTHONPATH=src python -m misevolve.scope.demo --out /tmp/scope-mock-new-directory
python -m pytest tests/scope -q

# 还需已有的固定版本 AutoSkill 源码，位置由现有 method_checkout 规则决定
python -m pytest tests/multiagent/test_scope_native_candidates.py -q
```

`--out` 必须是新目录，避免覆盖之前的 mock 轨迹。真实实验日志与 mock 结果使用不同根目录。
