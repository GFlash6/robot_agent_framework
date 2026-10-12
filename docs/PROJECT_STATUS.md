# 机器人 Agent 框架工程状态

更新日期：2026-10-12
工程阶段：`implementation`
整体状态：**进行中**
当前版本：`0.1.0`
维护范围：`robot_agent_framework/`

这是工程状态的统一入口，按**主线**和**支线**记录每个模块及细项的完成情况。构建顺序与退出条件见 [BUILD_ROADMAP.md](BUILD_ROADMAP.md)；待决问题见 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)；目标架构见 [DETAILED_ARCHITECTURE.md](DETAILED_ARCHITECTURE.md)；历史验证边界见 [VALIDATION.md](VALIDATION.md) 与 [UI_VALIDATION.md](UI_VALIDATION.md)。

## 0. 项目目标与方向决策

### 0.1 目标

构建**面向通用机器人本体的 Agent 系统**。核心能力共五项：

| 能力 | 含义 |
|---|---|
| 长程任务拆解 | 把高层目标拆成层级任务，执行中按需展开和修订 |
| 子技能编排 | 把原子技能组织成可执行图，管理依赖、条件、资源、fallback 和抢占 |
| 技能执行监测 | 在执行过程中跟踪进度、检测异常，并根据观测判断是否真的完成 |
| Loop 回环反馈 | 内环（技能级重试）、中环（子任务局部修复）、外环（目标级重规划或请求人工）三层闭环 |
| 世界状态维护 | 持续维护带时间、坐标系、置信度和来源的机器人与环境状态，供规划、监测和验证使用 |

执行权威、资源控制、持久恢复和证据追溯是支撑这五项能力的**执行内核**，不是最终目标。

### 0.2 方向决策（2026-10-06，用户确认）

1. **仿真与实机并行。** 两条轨道实现同一套 SkillSpec 和执行状态语义。仿真轨先关闭契约和算法问题，实机轨关闭物理问题。实机轨被外部输入阻塞时，仿真轨照常推进。
2. **G1 为首个本体。** 仿真和实机都先以 Unitree G1 落地。
3. **适配多种机器人本体。** Agent 层不依赖具体本体。本体差异由本体描述（Embodiment Profile）和技能适配层承担，并且必须用第二种本体验证。

**约束变更：** 2026-09-22 的"禁止仿真"约束从此解除。以下约束保持不变：
- mock 和替身响应不能作为验收证据；
- AI 调用必须使用环境里的真实模型配置；
- 仿真证据单独分级，不能关闭实机项。

## 1. 状态口径与证据等级

| 状态 | 判定标准 |
|---|---|
| 已完成 | 当前声明的软件范围已有生产实现，并有对应代码、测试或实际运行记录；只代表该行所写的范围。 |
| 进行中 | 已有部分实现，但仍缺生产能力、关键决策或适当等级的验收。 |
| 未完成 | 没有生产实现，或只有接口预留、研究材料、契约文档。 |

| 证据等级 | 含义 | 能关闭 | 不能关闭 |
|---|---|---|---|
| **L0 软件集成** | 实际文件、HTTP 服务、进程、账本和真实模型调用 | 契约、状态机、恢复语义、模型接入 | 任何物理行为 |
| **L1 仿真** | 仿真器中的机器人、传感器和场景（`evidence_level=simulation`） | 技能契约、世界状态逻辑、监测和反馈算法、长程分解 | 真机时延、标定、碰撞、物理停止、硬件急停 |
| **L2 实机** | 真实 G1 或其他本体的执行和独立观测（`evidence_level=hardware_observation`） | 对应技能或任务族的物理闭环 | 未覆盖的任务族和环境 |

表格中的"证据"列标明当前达到的最高等级。HTTP 成功、模型自述成功、软件测试通过、仿真通过和实机完成是逐级递增的证据，本文不会用较弱的证据替代较强的结论，也不使用百分比估算。

## 2. 总体概览

### 2.1 主线

| 编号 | 模块 | 状态 | 最高证据 | 当前结论 |
|---|---|---|---|---|
| A | 执行内核 | 进行中 | L0 | 调度、技能协议、资源、持久恢复和 HTTP fencing 已实现。缺补偿和人工接管、统一重试归属以及实机控制端隔离 |
| B | 长程任务拆解 | 进行中 | L0 | 2026-10-12 按 [L0 规划层设计](../L0%20规划层设计.md) 重写：L0 一次调用输出 TaskPlan（理解、处境、落地、可行性、子任务、回复），规则前后夹检；子任务带 achieve/check/obtain 目标并由世界状态验证；任务管理器是状态和结果的唯一写入方；重规划、澄清、验收已实现（[AGENT_LOOP.md](AGENT_LOOP.md) §6）。原 Mission 分解已删除。真实模型规划质量和评测集未验收 |
| C | 子技能编排 | 进行中 | L0 | 依赖、引用、fallback、重试、资源、抢占，以及世界状态前置条件和效果确认已实现；抽象技能目录 9 项。没有条件分支，也没有真实技能实现 |
| D | 技能执行监测 | 进行中 | L0 | 进度反馈、停滞检测、不变量监测（违反即取消）、执行中观测上报、基于独立观测的效果确认已实现。没有心跳，阈值未标定 |
| E | Loop 回环反馈 | 进行中 | L0 | 内环、中环（保留已验证进展的局部修复）、子任务重新编排和 L0 重规划（保留已成功子任务）组成升级阶梯；世界状态否定效果或不变量会触发失败并进入该阶梯。没有补偿和人工接管协议 |
| F | 世界状态维护 | 进行中 | L0 | WorldState 模型、四种有效性、乱序/超前/冲突处理、expected 与 observed 区分、地图版本失效、三值谓词和 CTX-5 Provider 已实现。没有真实或仿真状态流接入 |
| G | 本体抽象与多本体适配 | 进行中 | L0 | 抽象技能、Embodiment Profile（G1、轮式底盘）、仿真/实机轨道绑定、安全限值写入 schema 已实现。没有 ROS2 或厂商适配器 |
| H | 仿真轨 | 未完成 | — | 仿真器尚未选定，也没有 G1 仿真环境。架构文档已定义 `provider` 和 `evidence_level` |
| I | 实机轨（G1） | 未完成 | 文档 | 接入契约已整理。正在等设备连接信息，还没有只读采集和运动技能 |

### 2.2 支线

| 编号 | 模块 | 状态 | 最高证据 | 当前结论 |
|---|---|---|---|---|
| S1 | 上下文构造与会话 | 进行中 | L0 | Session、Memory、摘要和预算、Provider、完整上下文包、快照以及输入重建都有真实模型证据。WorldState Provider（CTX-5）并入主线 F |
| S2 | 应用层与工作台 | 进行中 | L0 | M1–M3 已实现并验收。M4（真机接入）并入主线 H 和 I |
| S3 | 观察 UI | 进行中 | L0 | 只读 API、DAG 可视化、问答测试和上下文查看/对比已完成。缺心跳、传感器和 3D 预览、远程认证 |
| S4 | 多模态数据与记忆 | 进行中 | L0 | 资产目录、哈希、时空元数据、MCAP、rosbag2 和全文检索已实现。没有真实或仿真传感器流，也没有地图失效和语义记忆 |
| S5 | 事件自动诊断 | 进行中 | L0 | 失败事件诊断、中断恢复和工作台已验收。只解释，不触发恢复动作 |
| S6 | 生产部署与多机器人 | 未完成 | — | 只有本地 SQLite 加单 worker |
| S7 | 工程基线与治理 | 进行中 | — | 开发环境、仓库卫生、CI 和统一回归都有缺口（见第 5 节） |
| S8 | 开源调研与许可 | 进行中 | — | 第一轮 41 个仓库调研已完成。仿真器和本体适配方向还需补充调研，许可审查未完成 |

---

## 3. 主线细项

### A. 执行内核

#### A1 调度与技能协议

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 依赖就绪调度 | 已完成 | L0 | 只派发依赖已成功且能拿到资源的步骤 | `Runtime.tick`、`tests/test_runtime.py` |
| 技能注册与冻结契约 | 已完成 | L0 | 注册参数和输出 schema、资源、取消、重入和 verifier 属性。任务保存时冻结技能目录 | `Runtime.register`、`Runtime.submit` |
| HTTP 执行协议 | 已完成 | L0 | 用固定 execution ID 执行、查询和取消，请求内容变化会被拒绝 | [技能协议](SKILL_PROTOCOL.md)、`tests/test_http_execution.py` |
| 本地文件技能 | 已完成 | L0 | `file.ingest`、`asset.verify`、`asset.verify-set`、分块 `file.copy` 操作实际文件 | `robot_agent/file_copy.py`、[多文件验收](MULTIFILE_VALIDATION.md) |
| 成功结果判定 | 已完成 | L0 | 成功必须同时满足：execution ID 一致、终态、已静止（quiescent）、输出 schema、后置条件和非空来源证据 | `robot_agent/contracts.py` |
| 动作发现契约 | 已完成 | L0 | `GET /actions` 返回目录版本、每个动作的版本和哈希、副作用类别、幂等声明 | [应用层说明](APPLICATION_LAYER.md) |

#### A2 失败、恢复与控制权

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 有界技能重试 | 已完成 | L0 | 只有声明了 `replay_safe` 的技能才能自动重试，次数受计划限制 | `robot_agent/runtime.py` |
| fallback 选择 | 已完成 | L0 | 原技能真实失败后依次换用兼容替代，结果仍按原步骤契约检查 | `test_fallback_uses_actual_failure_and_preserves_original_contract` |
| 暂停、取消与恢复 | 已完成 | L0 | 先取消正在执行的动作，确认停止后才转换状态 | `test_pause_before_dispatch_and_resume` |
| 优先级抢占 | 已完成 | L0 | 高优先级任务可以请求低优先级任务暂停，资源要等停止证据才释放 | `Runtime.preempt` |
| 未知结果处理 | 已完成 | L0 | 执行端不可达时保持 `unknown`，不重发，也不释放资源 | `test_unreachable_service_does_not_release_or_retry` |
| 进程故障恢复 | 已完成 | L0 | worker 被杀后用原 execution ID 继续查询，不重复派发 | `test_worker_kill_recovers_same_remote_execution_without_redispatch` |
| 用户控制与迟到重规划的竞争 | 已完成 | L0 | 用户取消和更新的 generation 不会被迟到的模型响应覆盖 | `test_cancel_wins_over_late_automatic_plan_revision` |
| 跨动作幂等键 | 已完成 | L0 | 任务、会话和自动化共用同一套键归属检查，冲突返回 409 | [action-idempotency-live.json](validation/action-idempotency-live.json) |
| 控制端隔离旧执行者 | 进行中 | L0 | HTTP 控制域单调令牌、旧令牌拒绝和重启后持久化已验证；新控制者可以显式核对并停止旧执行。机器人驱动端尚未接入 | [fencing-live.json](validation/fencing-live.json)、[fencing-reconciliation-live.json](validation/fencing-reconciliation-live.json)；需要 G1 适配器实现令牌拒绝（主线 I） |
| 跨层重试归属 | 进行中 | L0 | 框架层有技能预算和模型预算。接入驱动和行为层后需要确定唯一的重试责任方 | 台账 R06；在仿真里验证没有重复副作用 |
| 补偿与人工接管 | 未完成 | — | 没有补偿工作流、持物状态恢复或人工接管协议 | 台账 Q08；放在路线 P4 |
| 故障矩阵 | 进行中 | L0 | 进程退出、迟到响应、取消竞争和令牌换代已覆盖。补偿失败、"控制器报告终态但仍在运动"、传感器时间不一致这几类没有覆盖 | [DETAILED_ARCHITECTURE.md §13.2](DETAILED_ARCHITECTURE.md)；按仿真和实机分别验收 |
| 硬件急停 | 未完成 | — | 软件取消不等于硬件急停 | 台账 Q07；必须走 G1 自身的安全链路，需要现场验收 |

#### A3 资源与持久化

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 多任务账本 | 已完成 | L0 | 多个任务的状态和事件独立保存、独立推进 | `robot_agent/store.py` |
| 资源命名空间 | 已完成 | L0 | 资源统一折算成 `robot_id/resource` | `Runtime.resource_name` |
| 多资源原子获取与容量 | 已完成 | L0 | 在 SQLite 事务里一次性检查并写入全部资源，避免部分占用或超配；别名冲突不会少算容量 | `test_resource_claims_across_connections_do_not_overallocate` |
| 停止前保留资源 | 已完成 | L0 | 只有终态且已静止才释放资源，未知状态继续占用 | 架构契约、运行测试 |
| 持久账本与计划历史 | 已完成 | L0 | 保存任务、步骤、执行、控制、事件、资源和计划历史；修订时保存当时的 plan、steps 和 catalog | `Runtime.revise`、`ui/src/TaskView.tsx` |
| DBOS 持久工作流 | 已完成 | L0 | DBOS 2.31.0 负责本地工作流恢复，单 worker 锁防止重复启动 worker | `robot_agent/durable.py` |
| 本体资源模型 | 未完成 | — | G1 的腿、腰、双臂、灵巧手、头部相机和 GPU 还没有资源清单和跨资源约束 | 台账 Q05；随 Embodiment Profile 定义（主线 G） |

### B. 长程任务拆解

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 任务、步骤和计划契约 | 已完成 | L0 | 定义了状态、依赖、重试、fallback、最终验证节点和 schema 校验 | `robot_agent/contracts.py` |
| DAG 合法性检查 | 已完成 | L0 | 拒绝有环、依赖缺失、无法到达最终验证节点的计划 | `test_dag_rejects_cycles_and_unverified_completion` |
| 模型生成结构化 DAG | 已完成 | L0 | 真实模型返回的计划要通过契约检查和"最终节点必须是 verifier"检查 | `Planner.plan`、[multifile-live.json](validation/multifile-live.json)（qwen3.7-plus，七节点） |
| 目标语义分析与澄清 | 已完成 | L0 | GoalContext 记录原始输入、解释、歧义、疏漏和假设；分析和问讯使用不同的 System Prompt | `goal_analysis.py`、[goal-context-live.json](validation/goal-context-live.json) |
| 层级任务树 | 进行中 | L0 | 指令→TaskPlan 子任务（编号 t1-02-PER-Q、依赖、目标原子、预算）→Runtime Task 技能 DAG；子任务 12 种状态；`task.parent` 指回子任务；DBOS 独立工作流 | `robot_agent/l0.py`、`task_plan.py`、`goals.py`、`task_manager.py`；`tests/test_task_plan.py`、`test_agent_driver.py`、`test_l0.py`；需要真实模型规划和仿真任务族验收 |
| 按需展开 | 进行中 | L0 | 子任务依赖完成时，先检查 completion（已成立就跳过），再用当时的世界状态规划；Task 成功后由世界状态验证，否定则在预算内重新展开，用尽后重新分解剩余部分，再用尽则 needs_operator | `test_mission_retries_with_fresh_expansion_then_asks_the_operator`；需要真实模型证据 |
| 以世界状态为规划输入 | 进行中 | L0 | WorldStateProvider 把带有效性的事实送入规划、修复和分解；能力目录包含前置条件、效果和不变量 | `test_planner_sees_world_contracts_and_world_state`；需要真实模型证据 |
| 拆解质量评测集 | 未完成 | — | 没有任务族数据集，也没有评价标准 | 在仿真中定义多个任务族，按执行结果评价，不用模型自评 |
| 导航和操作任务族实现 | 未完成 | — | 探索和导航算法属于下层技能，不在框架内实现 | 由技能库提供（主线 C） |

### C. 子技能编排

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 依赖输出引用 | 已完成 | L0 | 后续步骤只能用 `$ref` 引用已成功依赖的实际输出 | `Runtime._args` |
| 编排基础语义 | 已完成 | L0 | 依赖、重试、fallback、资源和抢占 | 见 A1、A2 |
| 显式完成契约 | 已完成 | L0 | 操作者给出的完成契约独立于模型计划，任务接受后冻结；技能报告成功但验收不通过，仍判为失败 | [completion-contract-live.json](validation/completion-contract-live.json) |
| 条件分支 / 行为树 | 未完成 | — | 没有条件节点；py_trees 只用于结果条件链 | 台账 R04；根据 G1 技能需求决定是用条件 DAG 还是行为树后端 |
| 技能前置条件与效果 | 进行中 | L0 | 三值谓词。前置条件：unknown 时有界等待，false 时不派发；效果：先写 expected，再由动作结束后的他源观测确认 | `test_preconditions_gate_dispatch_on_world_state`、`test_effects_need_a_later_independent_observation`；需要仿真验收 |
| 任务族后置条件 | 进行中 | L0 | 通用检查器已实现。导航和抓取的观测指标和阈值未定义 | 台账 Q03/Q04；先在仿真定义，再到实机校准阈值 |
| 机器人技能库 | 进行中 | 契约 | 抽象技能契约 9 项（状态读取、站立、停止、速度移动、导航、物体定位、抓取、放置、物体位置验证），没有实现 | `robot_agent/embodiment.py`；仿真适配器实现后才有 L1 |

### D. 技能执行监测

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 轮询查询与执行超时 | 已完成 | L0 | 每次 tick 查询执行状态，超时后对可取消技能发出 cancel | `Runtime.tick` |
| 终态与后置条件判定 | 已完成 | L0 | 见 A1 的"成功结果判定" | `robot_agent/contracts.py` |
| 进度与反馈流 | 进行中 | L0 | 协议新增 `feedback.progress` 和 `observations`；`file.copy` 返回实际进度 | [SKILL_PROTOCOL.md](SKILL_PROTOCOL.md#世界契约)；需要 ROS2 Action 映射 |
| 不变量监测 | 进行中 | L0 | 每次查询时检查；违反或（默认情况下）未知时请求取消，即使技能报告成功也判失败 | `test_invariant_violation_cancels_real_http_execution_and_fails_step` |
| 停滞检测 | 进行中 | L0 | 基于进度增量判定，独立于总超时 | `test_stall_detection_uses_progress_not_elapsed_time`；阈值需标定 |
| 基于观测的独立验证 | 进行中 | L0 | 效果确认和子任务完成都要求有效的 observed 事实；动作技能自己上报的观测不算数 | 见 C 行；需要真实感知源 |
| worker 和机器人心跳 | 未完成 | — | UI 显示 `unobserved` | 台账 U02；API 在线、worker 在线、机器人在线、机器人已停止四个指标分开 |

### E. Loop 回环反馈

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 内环：重试和 fallback | 已完成 | L0 | 见 A2 | — |
| 外环：失败后整计划重规划 | 已完成 | L0 | 有次数上限，继承提交时的会话快照、语义目标、绑定记忆和完成契约，并读取最新的失败账本 | [replanning-context-live.json](validation/replanning-context-live.json)、[completion-recovery-live.json](validation/completion-recovery-live.json)、[recovery-budget-live.json](validation/recovery-budget-live.json) |
| 中环：局部修复 | 进行中 | L0 | `Runtime.repair`：保留同 ID、同定义、依赖都被保留且世界未否定其效果的已成功步骤；新步骤可以用带前置条件的不可逆技能；`repair` 模型方法；修复被拒时升级到外环 | `test_repair_keeps_verified_progress_and_restarts_only_the_failed_part`、`test_rejected_repair_escalates_to_the_outer_loop`；需要真实模型修复证据 |
| 世界变化触发 | 进行中 | L0 | 不变量失效会取消执行，前置条件为 false 或效果被否定都会导致失败，然后进入反馈阶梯；地图换代会让空间事实失效 | 见 D 行；还没有“暂停受影响分支”这种更细的处理 |
| 失败解释 | 已完成 | L0 | `task.explain` 和事件自动诊断给出带来源引用的解释，不改变任务状态 | [explanation-context-live.json](validation/explanation-context-live.json)、[event-automation-live.json](validation/event-automation-live.json) |
| 补偿、人工接管与重试归属 | 未完成或进行中 | — | 见 A2 | 路线 P4 |

### F. 世界状态维护

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| G1 状态源审阅 | 已完成 | 文档 | 已审阅相邻工程 G1 状态桥和运动桥的源码，并归档哈希。明确了接收时间和采样时间的区别、默认 covariance、坐标语义和停止证据缺口 | [G1 接入契约](G1_INTEGRATION_CONTRACT.md)、[world-source-discovery.json](validation/world-source-discovery.json) |
| WorldState 数据模型 | 进行中 | L0 | `entity.attribute` 事实，带时间戳、坐标系、时钟域、置信度、来源、有效期、证据等级和地图版本；目前没有关系型事实（场景图） | `robot_agent/world_state.py` |
| 观测有效性 | 进行中 | L0 | 按采样时间计算四种有效性；超前样本拒绝，乱序样本忽略 | `test_world_facts_carry_validity_and_keep_conflicting_sources` |
| 更新机制 | 进行中 | L0 | 观测来自技能响应、状态桥 API/CLI 或操作者；效果只写 expected；冲突保留在事件中 | 需要接入真实或仿真状态流 |
| WorldStateProvider（CTX-5） | 进行中 | L0 | 带有效性、坐标系、年龄和证据等级的快照；在 repair/subplanning 阶段为必选；L0 用自己的摘要视图（机器人状态、地点、已知物体） | `robot_agent/context_providers.py` |
| 地图、TF 版本与失效传播 | 进行中 | L0 | `set_map_version` 使旧版本的空间事实失效；计划和记忆的失效传播尚未实现 | 台账 Q10/R08 |

### G. 本体抽象与多本体适配

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| robot_id 与资源命名空间 | 已完成 | L0 | 见 A3 | — |
| 本体无关的技能执行协议 | 已完成 | L0 | HTTP execute/query/cancel 协议不依赖具体本体 | [SKILL_PROTOCOL.md](SKILL_PROTOCOL.md) |
| Embodiment Profile | 进行中 | L0 | `profiles/unitree_g1.json`、`profiles/wheeled_base.json`；安全限值未标定 | 需要 G1 实际资源和限值确认 |
| 抽象技能与本体绑定 | 进行中 | L0 | `embodiment.install` 合成 SkillSpec；限值写入 schema；按轨道设置证据等级；换轨道时清理旧技能 | `test_profiles_bind_abstract_skills_with_limits_and_evidence_level` |
| ROS2 Action 适配器 | 未完成 | — | — | 台账 Q02；把 execute/query/cancel/feedback 映射到 Action |
| Unitree SDK 适配器 | 未完成 | — | 现有 cmd_vel 桥没有 execution ID、令牌或停止证据 | 见 G1 接入契约第三阶段 |
| 仿真 provider | 未完成 | — | 架构已定义 `provider = isaaclab \| ros2 \| unitree_sdk` | 跟真机实现同一套 SkillSpec |
| 第二本体验证 | 进行中 | L0（契约） | 轮式底盘 Profile 装载后只有自己的能力，含 manip 技能的计划被拒绝 | `test_second_body_gets_only_its_own_capabilities`；需要仿真跑通任务 |

### H. 仿真轨

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 仿真器选型 | 未完成 | — | 架构文档写的是 Isaac Lab；MuJoCo 等更轻量的方案没有评估 | 按 G1 支持程度、SDK/DDS 接口一致性、传感器仿真、GPU 需求和许可选型，记录到 DECISIONS |
| G1 仿真环境 | 未完成 | — | — | 能读状态、能执行站立、停止和移动，接口与实机桥一致 |
| evidence_level 标记 | 进行中 | L0 | execution 和世界事实都带等级；UI 还没有区分显示 | 支线 S3 |
| 仿真场景与任务集 | 未完成 | — | — | 至少覆盖导航、开抽屉放物、故障注入（断流、物体掉落、路径阻塞） |

### I. 实机轨（G1）

| 细项 | 状态 | 证据 | 当前情况 | 证据或完成条件 |
|---|---|---|---|---|
| 接入契约 | 已完成 | 文档 | 见主线 F | [G1 接入契约](G1_INTEGRATION_CONTRACT.md) |
| 连接信息 | 未完成（阻塞） | — | 本机 ROS 图只有诊断话题；设备 ID、网卡/域、时间和坐标约定**还在等回复** | 契约"首次只读接入所需输入"中的 6 项 |
| 只读状态采集 | 未完成 | — | — | 归档实际原始消息和来源信息，记录断流和恢复 |
| 第一项运动技能 | 未完成 | — | — | 执行身份、令牌、查询、取消和独立停止观测都满足；不能把 SDK 返回 code=0 当成已静止 |
| 硬件急停链路确认 | 未完成 | — | — | 见 A2 |
| 真实传感器流 | 未完成 | — | — | 台账 Q09；RGB-D、点云、标定和频率 |

---

## 4. 支线细项

### S1 上下文构造与会话

上下文层在不替换 Runtime、Planner 和 Memory 的前提下，为每次模型调用生成一个可追溯、受预算约束的上下文视图：

```text
ContextRequest
    ├── CapabilityProvider ── Skill Registry / Policy
    ├── SessionProvider ───── Session Store / Summary
    ├── TaskStateProvider ─── Runtime Ledger
    ├── MemoryProvider ────── Memory / Asset Catalog
    ├── WorldStateProvider ── G1 / 仿真 / Map / TF   （未完成，并入主线 F）
    └── OperatorProvider ──── Approval / Override
    ▼
ContextBuilder → ContextAllocator → RoleAwareRenderer → Planner / Replanner / Verifier
    └── ContextBundle + ContextManifest → Ledger
```

| ID | 工作包 | 状态 | 当前情况 | 退出条件 |
|---|---|---|---|---|
| CTX-0 | 契约和边界 | 进行中 | GoalContext、ContextRequest、Fragment、Bundle、Manifest、Provider Protocol v1 和规划快照 v1 已实现 | 补齐其他上下文类型的跨版本序列化 |
| CTX-1 | 权限分层渲染 | 进行中 | 只有框架策略能进入 system 消息，普通上下文作为 user 数据 | 补齐真实工具消息配对和污染测试 |
| CTX-2 | 能力和任务状态 Provider | 进行中 | 任务图关系和陈旧 revision 拒绝已实现 | 子规划的 ContextProfile 随主线 B 完成 |
| CTX-3 | Session 历史和摘要 | 进行中 | 持久会话、显式和自动摘要、分块、并发保护、恢复预算都有真实验收 | 更多长程任务族验证 |
| CTX-4 | Memory 检索 | 进行中 | 按 robot ID、有效期和证据过滤的 FTS 检索，版本绑定，工作台选择 | 检索质量基准；有真实召回问题后再考虑向量检索 |
| CTX-5 | World State Provider | 进行中（L0） | **并入主线 F** | 见主线 F |
| CTX-6 | 调用点统一接入 | 进行中 | 规划、重规划、解释、诊断和摘要已接入；旧 CLI 的任意 dict 上下文路径还在 | 移除旧路径 |
| CTX-7 | 可观测与重放 | 进行中 | 完整上下文包、哈希、Manifest 和精确输入重建已实现；观察 API 和 UI 支持单条查看和两条对比 | 消融评测 |

预算降级顺序（默认）：
1. 安全策略、权限和当前目标（必选）
2. 当前计划和步骤、能力契约
3. 人工覆盖和近期会话
4. 最新且一致的世界状态
5. 检索到的记忆
6. 较旧的会话（先摘要）
7. 调试材料

**本支线暂停继续打磨追溯能力**，后续只在服务主线 B、E、F 时推进。

### S2 应用层与工作台

| 里程碑 | 状态 | 当前情况 |
|---|---|---|
| M1 统一动作与幂等命令 | 已完成（L0） | 共享动作入口、可信主体、机器人范围检查、幂等队列、原子回执，HTTP 和 CLI 都可用 |
| M2 会话与选择上下文 | 已完成（L0） | 持久会话、界面选择快照、记忆和资产绑定、来源清单 |
| M3 草稿、控制与解释 | 已完成（L0） | 草稿版本和哈希绑定、执行控制、失败节点解释 |
| M4 设备接入 | **并入主线 H、I** | 原文"禁止仿真"已改为仿真与实机并行 |

详细说明见 [APPLICATION_LAYER.md](APPLICATION_LAYER.md) 和 [APPLICATION_LAYER_PLAN.md](APPLICATION_LAYER_PLAN.md)。

### S3 观察 UI

| 细项 | 状态 | 当前情况 |
|---|---|---|
| 只读观察 API | 已完成 | 使用 SQLite 只读模式，只有 GET，不导入 Runtime |
| 任务 DAG 与执行过程可视化 | 已完成 | DAG、尝试、fallback、停止证据、计划历史、事件 |
| 资源、资产、记忆和模型页面 | 已完成 | 只读取实际账本，没有记录时显示空状态 |
| 真实问答测试 | 已完成 | [MODEL_TEST_UI.md](MODEL_TEST_UI.md) |
| 上下文单条查看和两条对比 | 已完成 | [context-browser-live.json](validation/context-browser-live.json)、[context-diff-browser-live.json](validation/context-diff-browser-live.json) |
| 断连与过期提示、脱敏 | 已完成 | 断连保留上次快照并标记过期；纳秒等大整数用字符串返回 |
| 任务控制与编辑 | 进行中 | 工作台控制已实现；现场停止交互待实机验收 |
| 事件分页 | 进行中 | 工作台事件已改为分页；任务执行详情尚未分页，也没有推送 |
| worker 和机器人心跳 | 未完成 | 并入主线 D |
| 世界状态、传感器和 3D 预览 | 未完成 | 台账 U03；先用仿真数据开发，标明证据等级 |
| 证据等级显示 | 未完成 | 区分 L0、L1、L2 |
| 跨机器认证 | 未完成 | 台账 U06 |

### S4 多模态数据与记忆

| 细项 | 状态 | 当前情况 |
|---|---|---|
| 资产分类、摄取、哈希与时空元数据 | 已完成 | RGB、深度、点云、各类地图、标定、TF、文档、录制 |
| 资产父子关系 | 已完成 | 派生资产必须引用存在且完整的父资产 |
| MCAP 与 rosbag2 录制 | 已完成 | [ros-recording.json](validation/ros-recording.json)；本机只装了 sqlite3 存储插件 |
| 基础记忆目录与全文检索 | 已完成 | `Memory.remember/search` |
| 资产版本绑定 | 已完成 | [asset-binding-live.json](validation/asset-binding-live.json) |
| 仿真和真实传感器流 | 未完成 | 先接仿真流，验证吞吐、背压和丢帧；再接实机 |
| 自动分类、矛盾处理与实体合并 | 未完成 | 台账 Q11 |
| 向量检索与场景图 | 未完成 | 台账 R09；先用真实检索问题证明有需要 |
| 数据保留、配额与背压 | 未完成 | 台账 Q12/R07 |
| DimOS memory 复用 | 未完成 | 台账 R03 |

### S5 事件自动诊断

| 细项 | 状态 | 当前情况 |
|---|---|---|
| 失败事件入队与诊断 | 已完成 | [event-automation-live.json](validation/event-automation-live.json) |
| 中断恢复与显式重新诊断 | 已完成 | [automation-interruption-live.json](validation/automation-interruption-live.json) |
| 诊断上下文包 | 已完成 | [automation-context-live.json](validation/automation-context-live.json) |
| 工作台 | 已完成 | [automation-browser-live.json](validation/automation-browser-live.json) |
| 定时或条件触发 | 未完成 | — |
| 诊断结果驱动恢复 | 未完成 | 可以作为主线 E 中环的输入，但必须经过 Runtime 的权限和契约检查 |

### S6 生产部署与多机器人

| 细项 | 状态 | 当前情况 |
|---|---|---|
| Postgres 生产部署 | 未完成 | 台账 Q13/R01 |
| 多进程和跨机器高可用 | 未完成 | — |
| 性能与容量基准 | 未完成 | — |
| 多机器人协调 | 未完成 | 先完成单本体闭环和多本体适配 |
| 跨主机租约与仲裁 | 未完成 | 当前令牌只在单个账本内单调 |

### S7 工程基线与治理

| 细项 | 状态 | 当前情况 | 完成条件 |
|---|---|---|---|
| 开发环境 | 进行中 | 文件锁已抽象为 `robot_agent/filelock.py`（POSIX 用 fcntl，Windows 用 msvcrt），资产目录 fsync 也已兼容 Windows；离线回归可在 Windows/Python 3.13 运行。`ros_recording.py` 仍依赖 POSIX；Windows 上测试临时目录路径需较短（MAX_PATH） | 仍需选定标准环境（Linux/WSL2 + ROS2 Humble）用于 ROS2 和仿真 |
| 仓库卫生 | 未完成 | 没有 `.gitignore`；`ui/node_modules`（7295 个文件）、`.runtime` 账本、`__pycache__`、`egg-info`、`ui/dist` 和大型媒体文件都进了版本库 | 添加 `.gitignore`，把产物移出版本库；验证证据保留在 `docs/validation` |
| 统一回归基线 | 未完成 | 各文档记录的是不同时间、不同范围的局部结果（30、32、38、41 项） | 在标准环境里跑一次全量离线回归，记录为新基线 |
| CI | 未完成 | 没有 | 离线 Python 回归加 UI 构建；真实模型和仿真测试单独触发 |
| 文档一致性 | 进行中 | 本次已重写状态和路线文档。README 中"此版只读"、PRODUCT.md 中的模型名等仍是旧描述 | 按新方向同步更新 README 和 PRODUCT |
| Momo 同步 | 进行中 | 图到 revision 156；多项增量和本次方向决策尚未同步 | 新增本体、仿真和世界状态等主线对象 |

### S8 开源调研与许可

| 细项 | 状态 | 当前情况 |
|---|---|---|
| 第一轮开源调研 | 已完成 | 41 个仓库，110 份固定提交材料，见 [research/REPORT.md](research/REPORT.md) |
| 第一版组件选择 | 已完成 | DBOS、py_trees、MCAP、jsonschema、httpx；React、React Flow、Vite |
| 仿真器与本体适配调研 | 未完成 | 需要评估 G1 可用的仿真方案、ROS2 Action 规范，以及多本体抽象的参考实现 |
| 分发许可审查 | 进行中 | 台账 R10 |

---

## 5. 当前已知问题

| 编号 | 问题 | 影响 | 去向 |
|---|---|---|---|
| K1 | ~~一次性扁平 DAG 加整计划替换式重规划~~ 2026-10-07 已有 L0 实现：层级任务树、按需展开、`Runtime.repair` 局部修复；2026-10-12 任务层改为 L0 TaskPlan | 尚缺真实模型和仿真证据 | 主线 B、E；路线 P4、P5 |
| K2 | ~~技能契约里没有前置条件和效果~~ 2026-10-07 已有 L0 实现 | 阈值和容差未标定 | 主线 C、F；路线 P3 |
| K3 | 世界状态模型已有（L0），但**没有真实或仿真观测流** | 监测和反馈只能用操作者观测验证 | 主线 F、H；路线 P2、P3 |
| K4 | 本体抽象已有（L0），但**没有仿真或实机适配器** | 抽象技能无法实际执行 | 主线 G、H；路线 P2 |
| K5 | 实机连接信息还没到 | 实机轨阻塞 | 主线 I；仿真轨照常推进 |
| K6 | 开发环境：离线回归已能在 Windows 上运行（2026-10-07）；ROS2 和仿真仍需 Linux/WSL2 | ROS2 录制和仿真无法在本机运行 | S7；路线 P1 |
| K9 | 13 项历史离线测试过期：`test_semantic_context_pipeline.py` 的脚本化 caller 缺少 `prepare`；`test_context_framework.py` 构造的任务缺少 `catalog` | 回归基线不干净，需要修测试或删除 | S7；路线 P1 |
| K7 | 仓库卫生与缺少 CI | 版本库体积大，无法追溯历史，没有可重复的回归 | S7；路线 P1 |
| K8 | 过去一段时间集中在上下文追溯支线 | 主线推进停滞 | 本次重排后支线降级 |

## 6. 当前工作队列

按 [BUILD_ROADMAP.md](BUILD_ROADMAP.md) 的顺序：

2026-10-07 起，Agent 层（P2–P5 的软件部分）已有 L0 实现，见 [AGENT_LOOP.md](AGENT_LOOP.md)。剩余工作集中在"让它真正跑在仿真上"：

1. **P2 仿真基座（最高优先）：** 仿真器选型；实现 G1 仿真适配器，满足 [SKILL_PROTOCOL.md](SKILL_PROTOCOL.md#世界契约)，包括 feedback 和 observations；把仿真状态流接入 WorldState。
2. **真实模型验收：** 运行 `tests/test_l0_live.py`（L0 `task_planning`/`task_acceptance`），并对 `repair` 方法做 live 测试，留存证据文件。
3. **P1 工程基线：** `.gitignore`、修复或删除 K9 中的过期测试、CI。
4. **实机轨 R1 并行：** 拿到 G1 连接信息，做只读状态采集，写入 WorldState（`evidence_level=hardware_observation`）。
5. P4 剩余：补偿技能与人工接管协议、心跳、阈值标定。

## 7. 增量记录（2026-09）

以下增量都是 L0 证据，没有仿真或实机验收。完整说明见对应证据文件和 [APPLICATION_LAYER_PLAN.md](APPLICATION_LAYER_PLAN.md)。

| 增量 | 范围要点 | 证据 |
|---|---|---|
| 第一版模型规划 | qwen3.8-max 返回合法两步 DAG，没有执行 | [model-planning-qwen3.8-max.json](validation/model-planning-qwen3.8-max.json) |
| ROS2 录制 | 发现 topic，录制诊断消息；只证明软件通路 | [ros-recording.json](validation/ros-recording.json) |
| 应用层 | 统一动作、幂等命令、会话、草稿、解释 | [application-layer-2026-09-22.json](validation/application-layer-2026-09-22.json) |
| 记忆上下文 | 按机器人范围检索、版本绑定、来源复核；模型从记忆读取路径后规划，并实际执行 | [memory-context-live.json](validation/memory-context-live.json) |
| 工作台记忆选择 | 浏览器→模型→worker 闭环 | [memory-browser-live.json](validation/memory-browser-live.json) |
| 自动重规划上下文 | 实际文件故障→模型给出备用路径→哈希核验 | [replanning-context-live.json](validation/replanning-context-live.json) |
| 历史摘要 | 显式摘要，保留近期原文，记录来源哈希 | [session-summary-live.json](validation/session-summary-live.json) |
| 分块摘要 | 超大历史分块，并发编辑时拒绝绑定不完整摘要 | [chunked-session-summary-live.json](validation/chunked-session-summary-live.json) |
| 自动预算 | 目标分析和规划超预算时自动整理 | [automatic-session-budget-live.json](validation/automatic-session-budget-live.json) |
| 工作台摘要 | 策略、固定原文、手动生成 | [summary-browser-live.json](validation/summary-browser-live.json) |
| 恢复预算 | 在不可变快照上做预算和分块摘要；摘要期间取消不会创建修订 | [recovery-budget-live.json](validation/recovery-budget-live.json) |
| Provider 拆分 | 统一 ContextProvider 接口，Manifest 记录 Provider 版本 | [context-providers-live.json](validation/context-providers-live.json) |
| 快照版本 | v1 快照、旧 v0 只读兼容、篡改后拒绝 | [context-snapshot-live.json](validation/context-snapshot-live.json)、[context-snapshot-legacy.json](validation/context-snapshot-legacy.json) |
| 上下文隔离 | Provider 输入隔离，诊断清单 | [context-isolation-live.json](validation/context-isolation-live.json) |
| 完整上下文包 | ContextBundle v1，可以精确重建实际请求 | [context-codec-live.json](validation/context-codec-live.json) |
| 多文件任务 | 七节点计划、`asset.verify-set` | [multifile-live.json](validation/multifile-live.json) |
| HTTP fencing | 控制域单调令牌、旧令牌拒绝、重启后持久化 | [fencing-live.json](validation/fencing-live.json) |
| 旧执行停止核对 | 接管后通过实际 cancel 停止旧执行，保留部分字节，没有成功冒报 | [fencing-reconciliation-live.json](validation/fencing-reconciliation-live.json) |
| 事件分页 | 绑定任务的游标，1–500 条一页 | [event-pages-live.json](validation/event-pages-live.json) |
| 工作台事件 | 分页加载、增量轮询、断连保留 | [event-browser-live.json](validation/event-browser-live.json) |
| 事件自动化 | 失败事件入队诊断，两个 worker 并发也只调用一次 | [event-automation-live.json](validation/event-automation-live.json) |
| 进程中断 | SIGKILL/SIGINT 后保持 unresolved，不自动重发 | [automation-interruption-live.json](validation/automation-interruption-live.json) |
| 自动诊断工作台 | 创建、暂停、重新诊断 | [automation-browser-live.json](validation/automation-browser-live.json) |
| 失败解释追溯 | 发送请求前原子保存完整上下文包 | [explanation-context-live.json](validation/explanation-context-live.json) |
| 目标分析追溯 | 分析和澄清保存完整上下文包；修复了 JSON 键顺序问题 | [goal-context-live.json](validation/goal-context-live.json) |
| 诊断上下文包 | 自动诊断保存完整上下文包 | [automation-context-live.json](validation/automation-context-live.json) |
| 摘要追溯 | 分块和恢复摘要保存完整上下文包 | [summary-context-live.json](validation/summary-context-live.json) |
| 上下文只读查询 | `GET /api/v1/models/{id}/context` | [context-observer-live.json](validation/context-observer-live.json) |
| 上下文面板与差异 | 单条面板、两条差异 API 与界面 | [context-browser-live.json](validation/context-browser-live.json)、[context-diff-live.json](validation/context-diff-live.json)、[context-diff-browser-live.json](validation/context-diff-browser-live.json) |
| 资产版本绑定 | AssetBinding v1，在提交、派发和恢复时复核 | [asset-binding-live.json](validation/asset-binding-live.json) |
| 显式完成契约 | 区分技能报告的状态和任务验收结果 | [completion-contract-live.json](validation/completion-contract-live.json) |
| 完成契约恢复 | 验收失败后模型改用授权的备用文件 | [completion-recovery-live.json](validation/completion-recovery-live.json) |
| 动作发现契约 | 目录版本、副作用声明 | [APPLICATION_LAYER.md](APPLICATION_LAYER.md) |
| 跨动作幂等修复 | 统一键归属，冲突返回 409 | [action-idempotency-live.json](validation/action-idempotency-live.json) |
| 世界状态接入复核 | G1 桥源码审阅，实际 ROS 图发现 | [world-source-discovery.json](validation/world-source-discovery.json) |
| Agent 闭环（2026-10-07） | 世界状态与三值谓词；技能前置条件、效果确认和不变量；进度和停滞监测；修复→重规划→人工的升级阶梯；Mission 层级分解和按需展开；Embodiment Profile（G1、轮式底盘）与轨道绑定；Windows 文件锁兼容。离线回归 131 项通过（另有 13 项过期测试，见 K9） | [AGENT_LOOP.md](AGENT_LOOP.md)、`tests/test_world_agent.py` |
| L0 规划层（2026-10-12） | 按 L0 规划层设计重写任务层：TaskPlan 契约与编号规则、目标词汇（原子→世界状态谓词）、输入汇总与规则前后夹检、澄清/重规划/验收、子任务状态机和结果、操作者计划与修订；删除 Mission 分解。离线回归 181 项通过（另有 13 项过期测试，见 K9）；L0 调用路径用本地脚本化模型端点验证；CLI+DBOS 冒烟通过。真实模型 live 测试未运行 | [AGENT_LOOP.md](AGENT_LOOP.md) §6、`tests/test_task_plan.py`、`tests/test_l0.py` |

## 8. 维护规则

1. 每次实现或验收后，更新本文件的日期、对应细项和第 6 节工作队列。**新增量写进第 7 节表格的一行，不要在文末追加段落。**
2. "已完成"必须给出代码或验证证据，并标明证据等级。只有接口、schema 或研究材料时，不能标为完成。
3. 已实现但还缺更高等级验收的条目保持"进行中"，并写清已经完成的范围和等级。仿真通过不能关闭实机项。
4. 新问题先写进 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)，再在这里加对应状态。
5. 同步更新 Momo 快照和 `WORKLOG.md`、`DECISIONS.md`、`HANDOFF.md`。
6. 历史测试报告不因状态更新而改写。
