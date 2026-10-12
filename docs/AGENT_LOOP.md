# Agent 闭环：五项核心能力的实现

更新日期：2026-10-12　证据等级：**L0（软件集成）**。本文描述当前代码的实际行为，未经仿真（L1）或实机（L2）验收。状态口径见 [PROJECT_STATUS.md](PROJECT_STATUS.md)，构建顺序见 [BUILD_ROADMAP.md](BUILD_ROADMAP.md)。任务规划层的设计见 [L0 规划层设计](../L0%20规划层设计.md)，第 6 节是它的实现。

## 1. 总体结构

```text
 用户指令 ──L0(vlm0) 五步──▶ TaskPlan {understanding, situation, grounding, feasibility, subtasks, reply}
     │  需要澄清 → 问用户 → 回答后从第 1 步重跑；不可行 → 告诉用户
     ▼
 子任务 t1-02-PER-Q {category, kind, goal(achieve/check/obtain), depends_on, budget}
     │ 依赖都成功 → READY；执行类目标已成立 → SKIPPED
     ▼
 L1：子任务 → 技能 DAG（模型或预置）──▶ Runtime Task
     │          ┌───────────── Runtime.tick（每轮）─────────────────────────────┐
     │          │ 派发前：前置条件谓词 ─ unknown 等待(有界) / false 不派发         │
     │          │ 执行中：反馈进度 → 停滞检测；不变量谓词 → 发现异常即请求取消     │
     │          │ 成功后：效果写为 expected → 等待之后的独立观测确认(verifying)    │
     │          │ 失败：内环 重试/fallback → 中环 repair → 补偿 → 人工            │
     │          └───────────────────────────────────────────────────────────────┘
     ▼ Runtime Task succeeded
 子任务 goal 在世界状态中成立？ 是 → SUCCEEDED（写 result）；否 → 预算内重新编排 → 用尽 → FAILED
     │
     ├─ 必需子任务失败 / 缺失条件 / 假设被证伪 / 用户插话 → L0 重规划（plan_version+1）→ 用尽 → needs_operator
     ▼
 全部子任务结束 → L0 验收 → 回复用户

 世界状态（WorldState）：所有谓词的唯一数据源。观测来自感知技能返回的 observations、
 机器人状态桥或操作者；技能效果只写 expected，不会被当成事实。
```

| 能力 | 模块 | 关键入口 |
|---|---|---|
| 世界状态维护 | `robot_agent/world_state.py` | `WorldState.observe / expect / set_map_version / snapshot`，谓词 `evaluate` |
| 子技能编排（L1） | `robot_agent/planner.py`、`robot_agent/runtime.py`、`robot_agent/embodiment.py` | `Planner.plan`、前置条件门控、效果确认、本体绑定的抽象技能 |
| 技能执行监测 | `robot_agent/monitor.py` | `assess`（进度/停滞/不变量）、`ingest_observations` |
| Loop 回环反馈 | `robot_agent/runtime.py`、`robot_agent/task_manager.py` | Runtime 的升级阶梯；子任务重试；L0 重规划 |
| 长程任务拆解（L0） | `robot_agent/l0.py`、`robot_agent/task_plan.py`、`robot_agent/goals.py`、`robot_agent/task_manager.py` | `L0Planner.plan / accept`、`validate_task_plan`、`TaskManager.create / advance` |

## 2. 世界状态

**事实**是一个机器人的一个 `entity.attribute` 键（如 `robot.pose`、`cup.location`），带有：值、`status`（observed / expected）、`frame_id`、`clock_domain`、`stamp_ns`（采样时间）、`received_ns`、`confidence`、`ttl_s`、`source {kind, ref}`、`evidence_level`、`map_version`、`invalidated`。

| 规则 | 行为 |
|---|---|
| 有效性 | 读取时计算：`valid` / `stale`（采样时间超过 ttl）/ `unavailable`（无记录）/ `invalid`（被作废，如地图换代） |
| 时间 | `stamp_ns` 必须是账本时钟（适配器负责把仿真或设备时钟换算过来，`clock_domain` 记录原始时钟域）。超前接收时钟 1 s 以上的样本被拒绝并审计 |
| 乱序 | 比当前观测更旧的样本不覆盖，记 `late_observation_ignored` |
| 冲突 | 不同来源给出不同值时，最新样本成为当前值，旧值和来源保留在 `observation_conflict` 事件中 |
| 效果 | 技能成功后效果写为 `expected`，保留上一条观测；之后的观测产生 `effect_confirmed` 或 `effect_contradicted` |
| 地图/TF | `set_map_version(v)` 把所有带旧 `map_version` 的事实标为 `invalid`（需重新观测），记录受影响的键 |

事件写入 `world:<robot_id>` 流，可用现有事件分页 API 读取。

### 谓词语言

前置条件、不变量、效果确认、子任务完成条件共用一种 JSON 谓词：

```json
{"all": [
  {"fact": "robot.estop", "op": "eq", "value": false},
  {"fact": "robot.holding", "op": "eq", "value": {"$arg": "object"}},
  {"fact": "{object}.location", "op": "exists"},
  {"fact": "robot.pose", "op": "near", "value": {"$arg": "target"}, "tolerance": 0.3, "frame": "map"}
]}
```

运算符：`eq ne gte lte in exists near inside`；组合：`all any not`。`{name}` 和 `{"$arg": ...}` 绑定技能参数；`{"$fact": "drawer_1.region"}` 引用另一条事实（被引用的事实也必须有效，坐标系一致）。`inside` 判断位置是否在区域 `{"min": [...], "max": [...]}` 内，`near` 比较两者共有的前几维。

**场景登记**：`load_scene({"frame": "map", "objects": {"drawer_1": {"region": {...}, "approach": [...]}}})` 把桌子、抽屉等静态信息写成操作者事实，于是完成条件可以写"方块在抽屉内"：`{"fact": "red_block.location", "op": "inside", "value": {"$fact": "drawer_1.region"}}`。

求值是**三值**的：`true` / `false` / `unknown`。事实缺失、过期、作废、仅为 expected、坐标系不符、置信度不足，结果都是 `unknown`，不会被猜成 `false` 或 `true`。

## 3. 技能契约扩展

SkillSpec 新增以下可选字段（见 [SKILL_PROTOCOL.md](SKILL_PROTOCOL.md#世界契约)）：

| 字段 | 含义 | Runtime 行为 |
|---|---|---|
| `preconditions` | 派发前必须成立 | `true` 派发；`unknown` 等待，超过 `precondition_timeout`（默认 10 s）判失败；`false` 不派发直接失败，只尝试 fallback，不重试同一技能 |
| `invariants` | 执行期间必须保持 | 每次查询时检查；`false`（或默认情况下的 `unknown`）产生监测发现 → 取消 → 即使技能报告成功也判失败 |
| `effects` | 成功后世界应有的变化 | 写为 expected；步骤进入 `verifying`，直到动作结束**之后**、且**不是该执行自己上报**的观测确认，才判 `succeeded`；矛盾或超过 `effect_timeout` 判失败 |
| `monitor` | `stall_s`、`min_progress_delta`、`invariant_unknown` | 进度超过 `stall_s` 未增加即判停滞 |
| `compensation` | `{skill, args}`，失败后的补救动作 | 已派发的步骤最终失败后，Runtime 先运行补偿（例如抓取失败后收回机械臂），再进入反馈阶梯；补偿失败则任务进入 `needs_operator` |
| `evidence_level`、`provider`、`embodiment` | 证据等级和来源 | 写入每条 execution 和观测记录 |

计划中的步骤可以带 `when`（世界状态谓词）：轮到时为 true 才执行；false 时跳过（引用其输出的步骤也一并跳过），跳过算完成；unknown 时有界等待。最终验证步骤不能带 `when`。

依赖步骤要等上游效果确认后才会派发（`$ref` 要求上游 `succeeded`）。

## 4. 执行监测

- **进度反馈**：适配器在非终态响应中返回 `feedback.progress`（0–1），Runtime 保存在 execution 的 `monitor` / `feedback` 字段。内置 `file.copy` 已返回实际复制进度。
- **观测上报**：任意响应可携带 `observations` 列表，Runtime 以 `source={kind: observation, ref: execution_id}` 写入世界状态。格式错误的项目只审计、不采信。
- **发现处理**：监测器不直接停机，只产生发现；Runtime 把它当成取消请求处理，资源一直保留到适配器给出带停止证据的终态，与操作者取消路径相同。不可取消的技能只记录发现。

## 5. 三层反馈环

| 层 | 触发 | 动作 | 预算 |
|---|---|---|---|
| 内环 | 步骤真实失败、效果被否定、前置条件为 false | 可重入技能有界重试，然后依次尝试 fallback | 步骤 `retries`、`fallback` |
| 中环 | 内环用尽，任务失败 | 任务进入 `repairing`，模型以 `repair` 方法生成修复后的完整计划，交给 `Runtime.repair` | `max_repairs` |
| 外环 | 中环用尽或修复被拒 | 任务进入 `replanning`，整计划修订（原 `revise`，只允许可重入技能） | `max_replans` |
| 人工 | 全部用尽，或补偿失败 | Runtime Task 停在 `failed` / `needs_operator`；对应子任务进入 `BLOCKED`，任务停在 `needs_operator` | — |

Runtime Task 之上还有两层：子任务按 `budget.retries` 重新编排（L1），必需子任务最终失败时由 L0 重规划（见第 6 节）。

人工决定：Runtime Task 用 `Runtime.resolve(task, recover|fail)`；任务用 `TaskManager.resolve(task, confirm|retry|replan|abort, subtask)`，分别表示确认子任务已完成、重新编排该子任务、让 L0 现在重规划、中止；也可以用 `TaskManager.revise(task, plan)` 提交人工编写的新计划版本。每次决定都记入报告。

`Runtime.repair` 的保留规则：新计划中同 ID、同定义（skill、args、deps、retries、fallback）、已成功、所有依赖也被保留、且世界状态没有否定其效果的步骤，保留原结果，不再执行；其余步骤重新开始。新步骤可以使用不可重入技能，前提是该技能带有世界状态前置条件，即派发前会按当前世界重新判断。

## 6. L0 规划层与任务树（TaskPlan）

按 [L0 规划层设计](../L0%20规划层设计.md) 实现，取代原来的 Mission 分解（`missions.py`、`decomposition` 方法和 `Planner.decompose` 已删除）。

**一次 L0 调用**（`l0.py`，模型方法 `task_planning`）：

| 环节 | 实现 |
|---|---|
| 输入（设计 §2） | `gather` 汇总：对话（指令之后的最近 12 轮，作为 user/assistant 消息）；能力清单（技能类别 + 名称和一句话说明，不含参数；加上策略里的范式技能）；规则（以 system 权限渲染）；当前视野（调用方给的场景描述 + 关键帧资产 ID）；地图（策略里的文字概述 + 世界状态中带 `approach`/`region` 的命名地点）；机器人状态（一句话：所在地点、手里有什么、电量）；已知物体（名字、所在地点、上次看到距今秒数、置信度、有效性）；重规划时加任务树快照和错误；可选的相似经验 |
| 规则先查 | 调用前算出 `rule_findings`：可用/缺失的类别、电量是否低于下限、禁止区域、需要确认的类别和意图、已知的世界 ID |
| 模型五步 | 一次调用产出完整 TaskPlan，字段与设计 §4 一致 |
| 契约校验 | `task_plan.validate_task_plan`：编号格式且与 category/kind 一致；goal 只能用目标词汇；依赖无环；`$编号.value` 只能引用自己依赖的祖先；只用已有类别（规则 6）；可找或未知的对象必须先有获取类子任务（规则 3）；结论与检查项、假设、歧义、子任务、回复相互一致 |
| 规则后查 | `enforce_rules`：落地到不存在的世界 ID、声称已满足但世界状态不成立 → 拒绝候选；会进入禁止区域、电量不足又没有 SYS 子任务 → 改判不可行；需要确认而用户未确认 → 改判需要澄清。规则只会让结论更严，记入 `feasibility.rule_overrides` |
| 证据 | 每次调用保存请求、上下文包、上下文清单、原始响应和校验结果（`model_responses`）；每个计划版本存为 `task_plans/<task>@<version>` |

被拒绝的候选消耗 `max_planning_failures`，用尽后任务进入 `needs_operator`。

**目标词汇**（`goals.py`）：子任务 goal 由原子组成，每个原子对应一个固定的世界状态谓词，所以完成与否由观测判断，不由技能自述：

| 原子 | 性质 | 世界状态含义 |
|---|---|---|
| `robot_at(place)` | A | `robot.pose` 与 `place.approach` 相距 0.3 m 内 |
| `holding(obj)` / `hands_free()` / `standing()` | A | `robot.holding` = obj / `"none"`；`robot.mobility_ready` = true |
| `object_in(obj, place)` | A、C | `obj.location` 在 `place.region` 内 |
| `open(entity)` | A、C | `entity.open` = true |
| `handed_over(obj, person)` | A | `obj.handed_to` = person |
| `fact_is(key, value)` | A、C | 任意事实等于给定值 |
| `location(obj)` | Q（ObjectRef） | `obj.location` 已观测；value 为 `{object, location, frame, seen_at}` |
| `fact(key)` | Q（Value） | 任意事实已观测；value 为其值 |

判别类答出「否」也算成功（value=false）；只有答不出来（谓词一直 unknown 到超时）才失败。

**任务树**（`task_manager.py`）：任务编号 `t1, t2, …`。L0 只写计划字段；`TaskManager` 是子任务 `status` 和 `result` 的唯一写入方。

| 子任务状态 | 进入条件 |
|---|---|
| `PENDING → READY` | 依赖都 `SUCCEEDED`/`SKIPPED`；可选子任务的依赖失败时直接 `SKIPPED` |
| `READY → SKIPPED` | 执行类目标在世界状态里已经成立 |
| `READY → PLANNING → RUNNING` | 解析 `$编号.value` 引用后，L1 编排技能 DAG（预置的 `l1_plans`，否则模型 `planning` 方法）并提交为 Runtime Task（`parent` 指回子任务） |
| `RUNNING ↔ BLOCKED` | Runtime 在等前置条件或 `when`、被暂停，或补偿失败等人工；等外部条件超过 `block_timeout_s` 转为出错 |
| `RUNNING → MISSING_PREREQ` | Runtime 报告前置条件为 false |
| `RUNNING ↔ RECOVERING` | Runtime 在修复或补偿；或本层按 `budget.retries` 重新编排 |
| 终态 | 目标被观测确认 → `SUCCEEDED`；预算或 `timeout_s` 用尽 → `FAILED`（部分条件达成为 `PARTIAL`）；被移出计划或取消 → `CANCELED` |

结束时写一次 `result`：`outcome`、`success`、`goal_progress`、`value`、`produced`、`evidence`（技能证据和 Runtime Task）、`error_ref`（`error_reports` 中的错误报告）、`duration_s`。

**重规划**：触发条件是必需子任务 `FAILED`/`PARTIAL`/`CANCELED`、出现 `MISSING_PREREQ`、`assumption_failed(...)`，或用户 `say(...)` 插话。先取消进行中的子任务，等 Runtime 给出停止证据后再调用 L0（`mode=replan`，带任务树快照和错误）。新版本必须原样保留已成功的子任务；改动的子任务用新编号并写 `replaces`；失败或取消的编号不得复用（`validate_revision`）。受 `max_replans` 限制，没有模型或预算用尽时任务进入 `needs_operator`，此时可用 `revise` 提交人工计划，按同样的规则校验。

**澄清与确认**：结论为需要澄清时任务进入 `awaiting_user`，回复写入对话；`say(task, 回答, confirm=...)` 后从第 1 步重新规划（`plan_version` 加 1）。

**验收**：当前计划的子任务全部结束后，必需子任务都成功才进入 L0 验收（`task_acceptance` 方法，生成给用户的回复，说明失败的可选子任务）；没有模型时按规则验收。结果为 `succeeded` 或 `failed`。

**持久化**：DBOS worker 为每个任务运行独立工作流（`execute_agent_task`），任务等人时工作流结束；用户回答或人工决定会把 `generation` 加 1，worker 随后启动新的工作流。Runtime Task 仍走各自的工作流。

## 7. 接入仿真或 benchmark

`robot_agent/agent.py` 提供进程内驱动，不需要 DBOS worker 或 HTTP 服务：

```python
from robot_agent.agent import Agent, SkillHandler

with Agent(".runtime/bench", "g1-01", model_config) as agent:      # model_config=None 时只能用操作者计划
    agent.install(profile, binding)          # binding: {"adapter": "inprocess", "track": "simulation", ...}
    agent.handle("base.navigate_to", nav)    # 每个绑定的技能由仿真实现，见 SkillHandler
    agent.load_scene(scene)
    agent.set_policy({"rules": {"forbidden_places": ["bedroom"], "min_battery": 0.2},
                      "map": {"summary": "客厅、厨房、卧室；厨房在客厅东侧"}})
    task = agent.submit("把红色方块放进抽屉")    # 或 plan=<TaskPlan>, l1_plans={子任务编号: 技能计划}
    while not agent.done(task):                  # 结束，或在等用户/人工
        sim.step()
        agent.observe_many(sim.observations())   # [{"key", "value", "frame_id", "ttl_s", "ref"}]
        agent.step()
    print(agent.report(task, text=True))         # 文本树；不加 text=True 返回结构化报告和指标
    # agent.say(task, "放左边那个抽屉")  回答澄清问题或插话
```

技能实现：继承 `SkillHandler`，实现 `start / query / cancel`，返回 `self.running(id, progress, observations)` 或 `self.finished(id, status, output, error)`。同一 execution_id 的查询必须返回同一执行的状态；只有机器人真正停下后才能返回终态。

报告包含 TaskPlan 各字段、全部计划版本、对话、每个子任务（含已移出计划的）及其 Runtime Task。`metrics`：`success`、`status`、`duration_s`、`plan_versions`、`replans`、`runtime_tasks`、`subtask_attempts`、`executions`、`failed_executions`、`retries`、`fallbacks`、`repairs`、`compensations`、`model_calls`、`operator_actions`，以及子任务各终态的计数。CLI 用 `agent-task-report <id> --text` 查看同一份报告。

## 8. 本体抽象与多本体

| 层 | 文件 | 内容 |
|---|---|---|
| 抽象技能 | `robot_agent/embodiment.py` 中的 `ABSTRACT_SKILLS` | 与本体无关的 schema、前置条件、效果、不变量、监测参数 |
| Embodiment Profile | `profiles/unitree_g1.json`、`profiles/wheeled_base.json` | 支持哪些抽象技能、资源及容量、控制域、坐标系、安全限值 |
| 轨道绑定 | `profiles/bindings/*.json` | simulation 或 hardware 轨道、provider、endpoint、fencing |

`install(runtime, profile, binding, robot_id)`（CLI：`embodiment-install`）把三层合成为普通 SkillSpec：

- 安全限值写成输入 schema 的上下界，超限指令在派发前即被拒绝；
- 轨道决定 `evidence_level`；
- 换轨道时删除新绑定未覆盖的旧技能；
- 每个账本只服务一个机器人（技能目录是账本级的）。

第二本体（轮式底盘）没有 `robot.stand` 和 `manip.*`，含这些技能的计划在提交时就被拒绝。

世界状态约定：`robot.estop`、`robot.mobility_ready`（足式表示站立平衡，轮式表示驱动使能）、`robot.pose`、`robot.holding`、`robot.battery`（0–1）、`<object>.location`、`<object>.handed_to`、`<entity>.open`、`<place>.approach`、`<place>.region`。目标词汇建立在这些键上。

每个抽象技能带 `category`（MOV/PER/MAN/INT/MEM/SYS），L0 的能力清单和能力检查只看类别；没有声明类别的技能按名称前缀归类（如 `file.*` 归 MEM）。

## 9. CLI

```text
robot-agent embodiment-install profiles/unitree_g1.json profiles/bindings/unitree_g1.simulation.example.json --robot g1-01
robot-agent observe robot.estop false --robot g1-01 --source-ref operator-console --evidence-level simulation --ttl 2
robot-agent world --robot g1-01
robot-agent map-version map-2026-10-07 --robot g1-01
robot-agent policy-set policy.json --robot g1-01          # 规则、范式技能、地图概述
robot-agent instruct "把红色方块放进抽屉" --robot g1-01 --model-config model.json
robot-agent instruct "..." --robot g1-01 --plan task_plan.json --l1-plans l1_plans.json
robot-agent run --until-agent-task t1
robot-agent agent-tasks | agent-task-status t1 | agent-task-events t1 | agent-task-cancel t1
robot-agent agent-task-report t1 --text
robot-agent agent-task-say t1 "放左边那个抽屉" [--confirm]
robot-agent agent-task-resolve t1 retry --subtask t1-02-MAN-A --note "已手动清理"
robot-agent agent-task-revise t1 task_plan_v2.json --l1-plans l1_plans.json
robot-agent scene-load scene.json --robot g1-01 --evidence-level simulation
robot-agent task-resolve <runtime task> recover
```

## 10. 验证边界

以下全部为 L0：

- `tests/test_task_plan.py`（22 项）：TaskPlan 契约（设计 §8 示例、各项拒绝条件）、重规划规则、状态机、结果、目标词汇在真实世界状态账本上的求值。
- `tests/test_agent_driver.py`（12 项）：用确定性的"运动学小世界"夹具和操作者 TaskPlan 驱动完整任务：正常完成、抓取失败后补偿再重试、补偿失败后人工决定、获取类结果被后续子任务引用、判别类答"否"算成功、已满足目标不执行、缺失前置条件后人工修订计划、可选子任务失败不阻塞、禁止区域/电量/确认三类规则、缺少技能实现时不派发。
- `tests/test_world_agent.py` 中 3 项：用真实本地文件技能执行子任务，世界状态否定技能自报的成功时重试再停给人工，取消先停 Runtime Task。
- `tests/test_l0.py`（7 项）：L0 调用路径。模型端点是本地脚本化的 OpenAI 兼容服务，只证明输入汇总、证据记录、候选拒绝、规则改判、澄清后重规划、失败后重规划保留已完成工作、拒绝改写已完成子任务、用户插话先取消再重规划和模型验收这些流程，不证明任何真实模型的规划质量。
- 2026-10-12 做过一次 CLI 冒烟验证：`instruct --plan` 加 DBOS worker（`run --until-agent-task t1`），技能计划成功后子任务停在 `RUNNING`，直到观测到达才 `SUCCEEDED`，任务验收成功。

`tests/test_l0_live.py` 用真实模型跑 L0（需要 `RUN_LIVE_MODEL_TESTS=1`），尚未运行。

尚未证明：

- 真实模型的 L0 规划、重规划、验收和 L1 修复质量（需要运行 live 测试并归档证据）；
- 当前视野只传场景描述和关键帧资产 ID，传输层还不能发送图像；标注语义地图的渲染尚未实现，地图只有文字概述和命名地点；
- 假设被证伪目前只能由调用方通过 `assumption_failed` 报告，没有自动检测；没有依赖关系的子任务会同时编排，能否真正并行只由 Runtime 的资源账本决定，尚无专门的调度策略；
- 任何仿真或实机行为。G1 的绑定文件只是示例，endpoint 都是占位；
- 停滞阈值、效果容差和安全限值都未标定（`calibrated: false`）；
- 补偿只在最终失败后运行，不在每次重试之前运行；还不能"只暂停受影响的分支、其余继续"；人工决定目前只有 API 和 CLI，没有界面。
