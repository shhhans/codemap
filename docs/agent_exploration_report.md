# 结构化智能层 · Agent 机制与狗粮发现报告

> 本报告记录 codemap 当前的 Agent 调度机制、探索机制与概念模型（"公民体系"），
> 并如实复盘在多次"吃狗粮"（让工具解析自身源码）过程中发现的三个真实问题。
> 其中第三个问题——**评审过度报警**——直接指向我们整套公民体系的一处概念缺口，
> 是本报告的重点。
>
> 适用版本：MVP（M1–M4 完成）。相关代码均给出 `文件:符号` 引用。

---

## 0. 摘要（TL;DR）

- 系统以**虚拟污点追踪**为内核：把代码库看作"代码加工厂"，把一份业务"原材料 (Token)"
  在函数间的流动追成一条"主线"，多条主线的交汇点用于体检架构健康度。
- 调度层（Coordinator）用 **asyncio 队列 + 有界 worker 池**并发驱动多主线，节点展开出
  多个有效子节点即"Fork"；探索层（Worker）对每个节点做**1 跳探路 → LLM 语义剪枝 →
  打卡/滑动**。
- 三次狗粮依次暴露了三个真实问题，全部由"工具解析自己"照出：
  1. **短名歧义 → 幻象交叉（假阳性）**：底层图查询按短函数名解析下游，同名函数撞名。
  2. **动态分发边丢失 → 骨干不可见（假阴性）**：静态图无法解析 `obj.method()` 型动态调用。
  3. **评审过度报警 → 公民体系概念缺口（本报告重点）**：判定规则把"设计上就该共享的
     公共原语"误判为"职责污染"。
- 前两个是工程精度问题，已修复；**第三个是概念模型问题**，揭示公民体系缺少"公共枢纽"
  这一类公民，本报告给出剖析与改进建议，但**尚未实现**。

---

## 1. 系统架构与机制

系统分三个并发模块 + 一个评审 Agent，围绕一块共享黑板协作。

```
   用户/Seed
      │
      ▼
┌─────────────────┐  派发 (flow, node, depth)   ┌──────────────────────────┐
│ Coordinator      │ ──────────────────────────▶ │ Worker 池 (asyncio 并发)   │
│ ·有界 worker 池   │ ◀── 子节点入队 = Fork ────── │ ·expand_one(node)         │
│ ·队列 join 收敛   │                              │  探路→剪枝→打卡            │
└────────┬────────┘                              └────────────┬─────────────┘
         │                                                    │ log_trace (打卡/去重)
         │ 收敛后唤醒                                          ▼
         │                                       ┌──────────────────────────┐
         ▼                                       │ Global Blackboard (SQLite)│
┌─────────────────┐   读 intersections 视图        │ nodes / traces / 交叉点视图 │
│ ReviewAgent      │ ◀──────────────────────────── │                          │
│ 交叉点定性        │ ── record_verdict ──────────▶ │ intersection_verdicts     │
└─────────────────┘                               └──────────────────────────┘
```

### 1.1 调度机制（Coordinator）

代码：`src/codemap/agents/coordinator.py:Coordinator`

- **多主线初始化**：对每个 `SeedSpec`（flow_type / seed / material / 颜色）建一个独立的
  `TaintWorker`（共享同一块 Blackboard），登记一条地铁线，并把 seed 作为 Source 打卡、
  推入共享 frontier 队列。
- **有界并发池**：起 `max_workers` 个调度协程（`_scheduler`），各自从
  `asyncio.Queue` 拉取 `(flow_type, node, depth)`，调用对应 worker 的 `expand_one`。
- **Fork 即并发**：`expand_one` 返回的每个待继续子节点都作为**独立 frontier 项**重新入队；
  当某次展开产出 >1 个子节点，即记一次 Fork（`coordinator.py` 的 `_forks`）——这些子节点
  随后可能被池中**不同协程并发处理**。这就是"数据流真实分叉 → 派生新 Agent"的落地。
- **收敛**：`queue.join()` 等所有 frontier 处理完毕，再取消池协程。
- **去重/防环（并发安全）**：不靠内存集合，而靠 Blackboard 的 `log_trace` 在
  `(node, flow_type)` 上幂等——重复打卡返回 `False`，即"该主线已访问过此节点"。SQLite 的
  `INSERT … ON CONFLICT DO NOTHING` 原子性保证并发下恰好一个协程拿到"新访问"。

### 1.2 探索机制（Worker）

代码：`src/codemap/agents/worker.py:TaintWorker`

单个节点的处理单元是 `expand_one(node, depth)`（M2 递归与 M3 并发共用同一单元）：

1. **按需探路** `_downstream`：取该节点 1 跳下游候选 + 各自源码签名。
   - 用 `query_graph` 按 **qualified_name 精确**查 `CALLS` 出边（见 §3.1）。
   - 叠加**动态分发补边** `_recover_dynamic`（见 §3.2）。
2. **语义剪枝** `_classify`：把"静态 System Prompt（角色/概念/规则，享受 Prompt 缓存）"
   + "动态滑动窗口（当前主线/物质、当前节点、候选签名）"喂给 LLM，要求返回严格 JSON：
   每个候选判为 `barrier`（防爆墙）/ `noise`（无关控制流）/ `continue`（继承物质流），
   并标 `is_sink`（是否已是稳定沉淀点）与 `confidence`。
3. **打卡** `_record`：对 `continue` 的节点写入 Blackboard（node + trace，含角色与置信度）。
4. **滑动/分叉**：非 sink 的新节点作为子节点返回给上层继续深入；sink 记录但不再深入。

设计要点：
- **滑动窗口极小、System Prompt 极大**：每次只追加"当前节点 + 候选"，90% 的 prompt 是静态
  规则，命中服务端 Prompt 缓存（实测 MiniMax 返回 `cached_tokens`）。
- **不确定不武断剪枝**：低置信判定降级保留（`confidence` 字段），避免误杀主线。
- **深度/并发护栏**：`max_depth` + `max_workers` 防止 DFS+Fork 指数爆炸。

### 1.3 交叉点定性（ReviewAgent）

代码：`src/codemap/agents/review.py:ReviewAgent`

- Blackboard 的 `intersections` 视图自动浮出"被 >1 主线打卡"的节点。
- 对每个交叉节点，取其源码 + 每条主线为它记录的角色（sink / processor），交给评审 LLM 判
  `healthy` / `dangerous` + 中文说明；并带一条**确定性兜底规则**（见 §2.2）。
- 结论写入 `intersection_verdicts`，供地铁图导出时标红（危险）/标绿（健康）。

---

## 2. 概念模型：代码加工厂的"公民体系"

整套判断建立在一套对代码节点的**身份分类**之上——本报告称之为"公民体系"。

### 2.1 当前的公民种类（节点角色）

定义见 `src/codemap/prompts.py:SYSTEM_PROMPT`：

| 公民 | 含义 | 行为 |
|------|------|------|
| **Source 源头** | 原材料进入系统处（如读取 HTTP header） | 主线起点 |
| **中间加工环节 Processor** | 对原材料做实质变换、结果继续向下游流动（如 `parseJWT()`） | 主线"站点"，继续追踪 |
| **Sink / 稳定状态** | 原材料完成加工、落到稳定结果（如 `getCurrentUser()`） | 主线合理终点 |
| **Barrier 防爆墙** | 第三方库 / 框架内置 / 日志 / 完美黑盒 | 拦截，不再深入 |

### 2.2 交叉点健康判定规则

设计意图（`review.py` 文档串与 `_classify`）：

- **健康交叉**：交叉节点是某主线的**稳定沉淀点 (Sink)**，其他主线消费的是这份已沉淀的稳定
  状态——特性之间合理的接缝。
- **危险交叉 / 职责污染**：交叉节点是某主线的**中间加工环节 (Processor)**，另一条主线绕过
  稳定接口、直接摄取其半成品——上游内部一变下游就被波及。

**确定性兜底规则**（`review.py:_classify` 的 `any_processor`）：
> 只要**有任意一条主线**把交叉节点记录为 `processor`（中间环节），即判 `dangerous`；
> 若**所有交汇主线都**记为 `sink`，则 `healthy`。

这条规则在刻意构造的 fixture 上完全正确——但在解析真实代码（自身）时**系统性过度报警**，
原因见 §4.3 与 §5。

---

## 3. 已修复的工程精度问题（前两次发现）

### 3.1 发现一：短名歧义 → 幻象交叉（假阳性）

**现象**：第一次狗粮（解析 codemap 自身）时，系统报告 `m2_single_dfs._run` 与
`m3_concurrent._run` 两条流在 `_project_name`、`_resolve_seed` 处"危险交叉"，并给出了
看似合理的污染说明。

**根因**：底层 `trace_path` 工具按**短函数名**解析下游。codemap 里有多个 `_run`
（M1/M2/M3 各一），短名撞车，导致一个 `_run` 的下游被错误地归给了另一个 `_run`，从而
凭空制造出共享节点。

**证据**：改用 `query_graph` 按 qualified_name 精确查同一对节点，下游**完全不相交**：

```
m2_single_dfs._run -> [_print_path, _project_name, _resolve_seed]
m3_concurrent._run -> [Blackboard, Coordinator, LLMClient, SeedSpec,
                       _report, _resolve, close, llm, run, write_subway_map]
```

**修复**：`worker._downstream` 改用
`MATCH (f {qualified_name:'…'})-[:CALLS]->(t)` 精确解析（`worker.py:_downstream`）。
教训：这正是 M1 阶段就标记过的"短名解析风险"在真实数据上的兑现——**先固化契约、再写上层**
的价值，以及"对看似合理的 LLM 结论也要交叉验证"的必要性。

### 3.2 发现二：动态分发边丢失 → 骨干不可见（假阴性）

**现象**：修好假阳性后，深度狗粮里 `Coordinator` 流与 `Worker` 流**完全不交叉**，
哪怕两者实际共用同一套 worker 引擎。

**根因**：`Coordinator._scheduler` 通过 `worker.expand_one(...)` 调用 worker，而 `worker`
来自 `self._workers[flow_type]`（字典取值），**接收者类型无法静态解析**。底层图谱对这类
动态分发调用**整条边都不生成**——实测 `_scheduler` 只有 2 条 `USAGE` 边，没有任何指向
`expand_one` 的 `CALLS` 边。于是 Coordinator→Worker 这条真实骨干在图里"看不见"。

**修复**：在 Agent 层做**名称匹配补边** `worker._recover_dynamic`：
- 读节点源码，正则提取被调用名；
- 对图中**唯一同名**的 Function/Method 补一条候选边，标 `recovered=True`、
  confidence ×0.8，并在 LLM 窗口里注明"动态调用·置信偏低"；
- 用"唯一名约束 + 噪音词表"防止乱补。

**验证**：补边后 `_scheduler` 能看到 `expand_one`；聚焦狗粮（`_walk` × `_scheduler`）随即
在 8 个共享节点交汇——`expand_one / _downstream / _classify / _signature /
_recover_dynamic / _as_dict / query_graph / build_window`，**补边前一个都不会出现**。

---

## 4. 重点发现三：评审过度报警 → 公民体系的概念缺口

### 4.1 现象

补边后那次聚焦狗粮，两条流在 8 个共享节点交汇，评审 Agent 把**全部 8 个都判为
`dangerous`（职责污染）**，并对每个都生成了一段"听起来很对"的说明，例如对 `expand_one`：

> "coord_flow 与 worker_flow 都把 expand_one 当作中间加工环节，且都直接摄取了它产出的
> (qualified_name, depth) 子节点列表这份半成品 frontier 状态……应将 fork 出的子节点封装为
> 稳定的 frontier 契约供双方消费。"

### 4.2 为什么这是"误报"

这 8 个节点（`expand_one`、`_downstream`、`_classify` 等）是 codemap **有意设计为被多个
驱动复用的核心原语**：M2 的递归驱动（`_walk`）和 M3 的并发驱动（`_scheduler`）共用同一套
worker 引擎，是**良性的代码复用**，不是职责污染。

把它判为污染，是因为确定性规则（§2.2）只看"是 sink 还是 processor"。而这些原语天然是
`processor`（它们做变换、不是某条业务线的终点），于是**只要被两条线交叉就必然触发危险规则**。

### 4.3 概念缺口：公民体系缺了"公共枢纽"这一类公民

把 fixture 的真污染与狗粮的假污染并排看，差别一目了然：

| | fixture：`parse_jwt`（真污染） | 狗粮：`expand_one`（假污染） |
|---|---|---|
| 节点性质 | **Auth 主线的私有中间环节** | **被设计为公共复用的核心原语** |
| 另一条线如何到达 | Billing **绕过 Auth 的稳定 API**（`get_current_user`），直接抓 Auth 内部的裸 claims | 两个驱动**通过 `expand_one` 既定的公共接口**各自正常调用 |
| 耦合性质 | 跨线摄取**他人私有实现** → 污染 | 共用**公共契约** → 健康 |

**结论**：判定的关键根本不是"sink 还是 processor"，而是节点的**归属与可见性**：

> 污染 = 一条主线伸手摄取了**另一条主线的私有中间结果**；
> 健康共享 = 多条主线通过**公共工具/接口**正常复用同一个节点。

而当前公民体系里**没有"公共枢纽 / Shared Utility"这一类公民**——它默认每个 `processor`
都"私有地属于"某一条主线。一旦现实中存在"不属于任何业务线、被设计为大家共用"的基础原语，
模型就会把"健康复用"误判为"职责污染"。**这说明我们的公民体系与真实代码的组织方式存在偏差，
而非个别判错。**

---

## 5. 改进建议（待实现）

围绕"补上缺失的公民 + 引入归属判定"展开，按优先级：

1. **新增公民：公共枢纽 (Shared Utility / Public Primitive)**
   在 Source / Processor / Sink / Barrier 之外，增加一类"被设计为公共复用"的节点。
   交叉发生在公共枢纽上时，默认 `healthy`。

2. **引入"归属/可见性"信号区分私有中间 vs 公共工具**（判 §4.3 的关键）。可组合的结构信号：
   - **调用者扇入广度**：被全仓库很多不同模块调用 → 更像公共工具；只被某主线链路调用 →
     更像该主线私有中间。（`query_graph` 可直接统计 `CALLS` 入边的来源分布。）
   - **跨模块 vs 同模块**：交叉的两条线是否来自该节点所在模块之外。
   - **是否绕过公共 API**：B 到达 A 的节点时，A 是否存在一个更"稳定/更外层"的同主线节点
     （sink）本应被消费——若 B 跳过它直取内部节点，才是污染。

3. **把判定从"二元角色"升级为"关系判定"**：评审输入应包含"该节点相对每条主线是私有内部
   还是公共入口"，而不仅是 sink/processor 标签。评审 Prompt 与 `_classify` 需相应改造。

4. **置信度透传到报警**：补边（`recovered`）已带 0.8 折扣，应让评审把低置信交叉降级为
   "疑似/待确认"，避免把不确定当确定报警。

5. **可视化分级**：地铁图区分"真危险（私有被摄取）/ 公共枢纽（高扇入复用）/ 疑似（低置信）"
   三档，而非当前的红/绿二元。

---

## 6. 复现实验

环境：`Codebase-Memory` v0.8.1（单二进制）+ MiniMax-M3（国内端点）。

```bash
# 真污染（fixture，刻意构造）：parse_jwt 危险 / get_current_user 健康
python -m codemap.milestones.m3_concurrent
python scripts/render_subway.py web/subway_map.png

# 假污染（狗粮，补边后自身骨干）：8 个共享原语被全判 dangerous（过度报警）
#   seed: TaintWorker._walk × Coordinator._scheduler, depth=4
#   （脚本见提交历史中的深度狗粮实验）
```

三次发现按时间顺序：**短名歧义（假阳性）→ 动态边丢失（假阴性）→ 过度报警（概念缺口）**。
三者都由"让工具解析自己"照出，印证了 dogfooding 作为终极验证的价值——
**前两个让工具更准，第三个让我们重新审视世界观本身。**

---

## 7. 当前状态小结

| 项 | 状态 |
|----|------|
| 短名歧义（假阳性） | ✅ 已修复（qualified_name 精确解析） |
| 动态分发边丢失（假阴性） | ✅ 已修复（名称匹配补边，低置信） |
| 评审过度报警（概念缺口） | ✅ **已修复（V2.1）**——见 §8 与 [`v2_intersection_design.md`](./v2_intersection_design.md) |
| Coordinator / Worker / Blackboard / Review | ✅ M1–M4 跑通，真实 LLM |

> 本报告的核心主张：**"过度报警"不是一个 bug，而是公民体系的一处结构性缺口**。
> V2.1 沿这个主张落地了修复（§8）：不新增"公共枢纽"硬标签，而是把"私有 vs 公共"的归属
> 判定上移到延迟评审，用拓扑证据让 LLM 自行识别。

---

## 8. 后续：V2.1 已修复（实测复盘）

完整设计见 [`v2_intersection_design.md`](./v2_intersection_design.md)。核心改动：

- **Role 降格为局部意见**：`node_role`(sink/processor) 只是 Worker 1-hop 的局部看法；
  节点的"户籍"（私有内部件 vs 公共枢纽）改由**延迟评审**用全局拓扑判定。
- **污染是关系，不是属性**：评审不再看"是不是 processor"，而是取证
  **「门面绕行 (越级摄取)」**——某主线是否绕过另一主线暴露的稳定门面、直取其私有半成品。
  门面经 `traces.parent_node_id`（新增）重建路径 + `CALLS*1..3` 可达性取证。
- **三态 + 机器不定罪**：`healthy / dangerous / suspected`。确定性逻辑只取证，
  **只有 LLM 能判 dangerous**；离线/低置信/反刍超限一律降级 `suspected`（黄）。

**实测（codebase-memory-mcp v0.8.1 + MiniMax-M3）**：

复刻本报告 §4 的聚焦狗粮 `TaintWorker._walk × Coordinator._scheduler`（depth 4，
脚本 `scripts/exp_overalert.py`），两条驱动在共享 worker 原语上交汇：

```
expand_one  → ✓ healthy    （旧规则下的头号误报，现判公共枢纽）
_classify   → ✓ healthy
_downstream → ? suspected  （证据不足，诚实标黄，而非硬判红）
tally: {healthy: 2, suspected: 1}   ← 0 dangerous
```

对照本报告 §4.1：**补边前同一批共享原语曾被全判 `dangerous`**。V2.1 下 0 误报。
fixture 侧（`m3_concurrent`）的真污染仍被正确标红：`parse_jwt → dangerous`、
`get_current_user → healthy`——既治了假阳性，又没放过真污染。
