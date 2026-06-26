# 结构化智能层 (Structural Intelligence Layer) — 架构设计

> 本文档是项目的权威设计说明。代码实现应以此为契约；当实现与本文档冲突时，
> 先更新本文档再改代码。

## 1. 核心世界观

面向 Vibe Coding 的轻量级本地 2C 可视化工具。彻底摒弃传统"文件树"和"静态调用图
（毛线团）"，采用 **"代码加工厂与物质流 (The Code Factory & Material Flow)"** 世界观。

- **核心目标**：提取并可视化代码仓库中的"业务主线 (Material Flow Mainline)"，并在
  主线交汇处识别架构设计的健康状态（**职责叠加** vs. **职责污染**）。
- **视觉隐喻**：地铁图 (Subway Map)。主线为地铁线，加工节点为站点，交叉点为换乘枢纽。
- **验证目标**：Dogfooding——让本工具解析自身代码库，画出自身的业务主线。

## 2. 技术栈

| 层 | 选型 |
|----|------|
| 底层图谱引擎 | `Codebase-Memory`（[DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)，C / Tree-Sitter / SQLite，单二进制，零依赖） |
| 交互协议 | MCP (Model Context Protocol)，暴露 `trace_path`、`get_code_snippet`、`search_graph` 等 |

> **契约校准（v0.8.1 实测）**：底层引擎是公开项目 `DeusData/codebase-memory-mcp`。
> 实测发现方案原稿与真实工具名有偏差，已固化于 [`docs/mcp_tools_contract.json`](./docs/mcp_tools_contract.json)：
> - 调用追踪工具真实名为 **`trace_path`**（非 `trace_call_path`），`mode='calls'` 时按 `function_name` 检索。
> - 任何查询前须先 **`index_repository(repo_path=...)`** 建图；之后所有查询都需带 `project` 参数。
> - MCP server **无参数**直接 stdio 启动（没有 `--mcp` 开关）。
> - 真实暴露 14 个工具（另有 `query_graph` 支持 Cypher 多跳查询、`get_architecture` 给社区聚类等）。
| LLM 推理端 | Minimax API + Dashscope API，经 `openai` 库统一集成 |
| 后端 / Agent | Python + 极简无锁并发调度器（`asyncio`） |
| 前端 / 可视化 | HTML5 + D3.js / ECharts 拓扑/地铁图渲染 |

## 2.1 世界观 2.0：五类代码公民（V2）

任何图谱节点在被 Agent 接触时，归入以下五类公民之一：

- **Source（涌现点）**：原材料进入主线的起点。
- **Processor（私有中间环节）**：专属于某条主线的业务加工逻辑（半成品）。
- **Sink（稳定沉淀点）**：数据产生副作用或完成持久化的合理终点。
- **Barrier / Dual（外部存根 / 防爆墙）**：第三方库不展开源码，视作黑盒。
  **无出边 → Sink（阻断）**；**有出边 → Dual（数据透传转换器，继续追踪）**。
  判别逻辑见 [`src/codemap/filtering.py`](./src/codemap/filtering.py)（M1）。
- **Shared Utility（公共枢纽 / 已飞升节点）**：被设计为系统级复用的底层公共原语
  （如幂等校验、节点展开函数）。**高相对扇入 + 低扇出**，多条主线在此交汇是**极佳的
  架构健康状态**，标记为金色换乘站——这是 V2 用来消除 V1「狗粮过度报警」的关键公民。

## 3. 核心模块

系统分为三个核心模块：**Coordinator (调度官)**、**Worker Agent (追踪工)**、
**Global Blackboard (全局黑板)**。

### 3.1 Global Blackboard（全局黑板 / 共享记忆）

轻量级内存或本地 SQLite 数据库，存储所有并发 Agent 的探索轨迹，并在其中计算交叉点。
Schema 见 [`src/codemap/blackboard/schema.sql`](./src/codemap/blackboard/schema.sql)。

- **`nodes`**：被保留的代码节点（id, name, type, file_path, snippet）。
- **`traces`**：`agent_id`, `node_id`, `flow_type`（Auth / Payment …）的"打卡"记录。
  同时兼作 DFS 的 **visited set**（去重 + 防环）。
- **`intersections`**：视图/触发器。当同一 `node_id` 拥有 >1 种 `flow_type` 时自动生成
  交叉点记录，待评审 Agent 定性。

### 3.2 Coordinator（调度官）

- **自然语言解析 (Seeding)**：接收用户输入，用 LLM + MCP 工具（`search_graph` /
  `search_code`）找出真正的起点（Source / Seed）。
- **任务派发与 Fork 控制**：当某 Worker 报告"数据流在此处分叉，且不属于基础设施防爆墙"
  时，Coordinator 实例化新 Worker 并分配新的滑动窗口追踪任务。
- **并发护栏**（防止指数爆炸）：最大深度 `MAX_DEPTH`、最大并发 Worker 数
  `MAX_WORKERS`、依赖 `traces` 去重的 visited 检查。

### 3.3 Worker Agent（并发虚拟污点追踪器）

- 执行"基于 LLM 的虚拟污点追踪 (Virtual Taint Tracking)"。
- 拥有极小的滑动窗口上下文，仅关注当前追踪的"特定原材料 (Token)"。
- 共享一个庞大的 System Prompt（充分利用服务端 Prompt Caching 降低成本）。
- 每个剪枝判定附带 **confidence**：低置信分支降级标记而非直接剪掉，避免主线被错误截断。

## 4. 核心工作流 (Agent Workflow)

**步骤一 · 按需探路 (On-demand Exploration)**
1. Worker 从 Coordinator 拿到当前节点 `Node A` 与目标追踪物 `Token`。
2. 经 MCP 调用 `trace_path`（`mode='calls'`）获取 `Node A` 下游 1 层相邻节点 `[Node B, Node C]`。
3. 经 MCP 调用 `get_code_snippet` 提取 A/B/C 的核心源码签名。

**步骤二 · 语义剪枝与打卡 (Semantic Pruning & Logging)**
- **防爆墙 (Barrier) 判定**：若 `Node B` 是 `logger.info`、内置框架方法或"已声明的完美
  黑盒"，标记为 Sink/Barrier，停止该方向探索。
- **物质传递判定**：若 `Node C` 未接收/处理 `Token`（仅控制流附带调用），直接剪枝。
- **打卡上报**：对真正继承物质流的有效节点，调用 `log_trace(node_id, flow_type)` 打卡。

**步骤三 · 深度优先滑动与分叉 (DFS & Forking)**
- 仅一条有效路径：滑动窗口前移，继续深入。
- 多条有效路径（真实业务数据分流）：Worker 挂起，请求 Coordinator Fork。

**步骤四 · 交叉点定性 (Intersection Health Check)**
当黑板发现 `Node X` 被 `Auth_Worker` 与 `Billing_Worker` 同时打卡：
1. Coordinator 唤醒评审 Agent。
2. 检查 `Node X` 在 `Auth` 主线中的状态：
   - 若是 **沉淀点 (Sink / Stable State)**（如 `getCurrentUser()`）→ **[健康交叉]**。
   - 若是 **中间加工环节**（如 `parseJWT()`）→ **[危险交叉 / 职责污染]**。

## 5. 数据结构契约 (Visualization Schema)

Agent 最终输出一张用于渲染"地铁图"的 JSON 地图。权威 JSON Schema 见
[`docs/subway_map.schema.json`](./docs/subway_map.schema.json)，示例：

```json
{
  "mainlines": [
    { "id": "line_auth", "name": "Auth Mainline", "color": "#FF0000",
      "nodes": ["node_1", "node_2", "node_3"] }
  ],
  "nodes": [
    { "id": "node_2", "name": "AuthMiddleware.verify()", "type": "processor",
      "file_path": "src/auth/middleware.ts", "snippet": "..." }
  ],
  "intersections": [
    { "node_id": "node_x", "type": "dangerous",
      "description": "Billing 主线在此处摄取了 Auth 主线的中间处理结果（Token 解析），属于职责污染。建议重构为依赖稳定状态。",
      "involved_lines": ["line_auth", "line_billing"] }
  ]
}
```

## 6. LLM Prompt 架构

**静态前置区 (System Prompt — 享受 Cache，占 ~90%)**
1. 角色设定：基于代码加工厂世界观的虚拟污点追踪引擎 (Virtual Taint Tracker)。
2. 核心概念定义：Source / Sink / Barrier（第三方库或基础设施必须拦截）/ 中间结果 / 稳定状态。
3. 判定规则：严格过滤"仅有控制流但无实际数据（原材料）依赖"的调用节点。

**动态后置区 (Sliding Window — 产生 Token 消耗，每次追加在末尾)**
```
[当前追踪主线]: Auth (目标物质: authorization header / user_id)
[当前节点]: src/auth/controller.ts:login()
[下游候选节点及签名]:
  1. util/logger.ts:log()
  2. src/auth/service.ts:verifyToken()
  3. src/db/query.ts:getUser()
[请分析数据流并返回 JSON 指令]:
  - 哪些是防爆墙？ 哪些是无效噪音？ 哪些需要继续追踪 (Fork)？
```

## 7. 开发里程碑

- **M1 基础设施连通** ✅：Python 启动并经 MCP 连上 `Codebase-Memory`，成功调用
  `index_repository` / `search_graph` / `trace_path` / `get_code_snippet`，**并把真实工具
  schema 固化为契约**（`docs/mcp_tools_contract.json`，后续三个里程碑均依赖它）。
  已用 v0.8.1 实测打通，并以 codemap 自身源码（169 节点 / 365 边）跑通真实结构化查询。
- **M2 单线 DFS 追踪** ✅（待真实 LLM 联调）：实现 System Prompt + 动态滑动窗口
  （`prompts.py`）、OpenAI 兼容 LLM 客户端（`llm.py`，MiniMax/Dashscope 统一）、DFS 污点
  追踪 Worker（`agents/worker.py`，带 visited 去重 + 深度上限 + 置信度 + Blackboard 打卡）。
  入口 `milestones/m2_single_dfs.py` 从 Seed 跑通单主线剪枝并打印地铁站点路径。
  MCP 侧（索引→search_graph→trace_path→get_code_snippet→DFS→Blackboard）已用真实数据
  端到端验证；真实 MiniMax 调用待 key 注入后联调。
- **M3 全局黑板与并发分叉** ✅（真实 LLM 跑通）：`agents/coordinator.py` 用 asyncio 队列 +
  bounded worker 池并发驱动多主线，节点展开产生多个子节点即 Fork（独立入队，并发处理）；
  `agents/review.py` 评审 Agent 给交叉点定性（健康 vs 职责污染），结果落 `intersection_verdicts`。
  fixture `fixtures/sample_app/`（Auth + Billing 双线）刻意制造危险交叉 `parse_jwt`（被两线当作
  中间环节摄取）与健康交叉 `get_current_user`（稳定沉淀点）。入口 `milestones/m3_concurrent.py`
  实测：`charge()` 处 Fork 2 worker，自动报警 `parse_jwt` 为职责污染，并渲染地铁图标红。
- **M4 界面可视化与自我解析** ✅：`export.py` 导出 SQLite → JSON 契约；`web/subway.html`
  渲染地铁图——共享节点合并为**真正的换乘站**（单节点两线汇入），危险交叉则保留在各自主线、
  用**主线外的红色虚线**相连（标注职责污染）；`scripts/render_subway.py` 经 Chromium 出 PNG。
  `milestones/m4_dogfood.py` 让 codemap 解析自身（338 节点）跑两条真实主线完成狗粮验证。
  狗粮过程中暴露并修复了两个真实问题：
  (1) `trace_path` 按短名解析下游，仓库内同名函数（多个 `_run`）会撞名产生幻象交叉——
  已改用 `query_graph` 按 qualified_name 精确解析；
  (2) 动态分发调用（如 `worker.expand_one()`，接收者来自 dict 取值）静态图无法解析、
  整条边丢失——`worker._recover_dynamic` 在 Agent 层做**名称匹配补边**：扫描节点源码提取
  被调用名，对图中**唯一同名**的 Function/Method 补一条 `recovered=True` 的低 confidence
  (×0.8) 候选边。补边后 Coordinator→Worker 的换乘骨干得以在自身狗粮图中显现。

## 7.1 V2 升级里程碑（结构化智能层 2.0）

V2 的核心突破：从纯数据流追踪，升级为带**架构所有权 (Ownership) 判定**的语义分析——
精准区分「危险的职责污染」与「健康的公共枢纽」。

- **V2-M1 过滤与存根化** ✅：[`src/codemap/filtering.py`](./src/codemap/filtering.py)。两道
  防爆网把底层库挡在 LLM 的 Context 之外：(1) **全局关系过滤**——共享黑名单
  `GLOBAL_RELATION_BLACKLIST`（从 `worker._CALL_NOISE` 抽出并扩充）剪掉 builtins / 容器方法；
  (2) **局部存根化**——`ast` 解析文件 import 得到第三方/标准库模块（含 `as` 别名与
  `from x import y` 绑定），命中即视作黑盒**严禁展开 AST**，纯靠出入边判 **Sink（无出边）/
  Dual（有出边，继续追踪）**。存根判别对「图引擎是否索引外部符号」两种情况都成立。
  接入 `TaintWorker.expand_one`：存根边界绕过 LLM，且**存根判定优先于短名黑名单**
  （`requests.get` 不会因短名 `get` 被误剪）。

  > **实测校准（v0.8.1 真实图谱）**：用 `query_graph` 实测确认 Codebase-Memory **只索引仓库
  > 自身符号**——第三方/标准库调用（如 `asyncio.to_thread`）根本不进图（既非节点也非边）。
  > 故图层的存根分区在本引擎是**良性 no-op**（为其它引擎预留）；存根在本引擎的**真实价值在
  > 动态补边路径**：源码正则会提取 `np.dot(x)` 的裸名 `dot`，若仓库恰有唯一内部 `dot()` 就会
  > 补出**幻象边**。修复：(a) import 是**文件级**的（不在函数体里），故按节点 qualified_name 的
  > **最长前缀**定位其 `Module` 节点、解析该文件源码的 import（按文件缓存）；(b) 用
  > `externals_only` **剔除项目自身的一方包**（如 `codemap`，它 import 起来像库但解析到内部可追节点），
  > 只保留真正的外部模块；(c) `stub_call_names` 据此把外部方法裸名排除出补边，内部调用
  > （如 `build_window`）照常可追。实测 `worker._classify`：外部根=stdlib、`to_thread` 被排除、
  > `build_window` 仍补回。
- **V2-M2 相对扇入/出** ✅：[`src/codemap/metrics.py`](./src/codemap/metrics.py)。Concordia
  无量纲公式 `相对扇入 = Fan-in/(S·ln S)`（S=文件/类数，缺标签时回退总节点数），消除项目
  规模差异；`classify_hub` 据「相对扇入 × 绝对扇出 × **绝对扇入下限**」给出 SHARED_UTILITY /
  GOD_NODE / ORDINARY。阈值 `CODEMAP_REL_FANIN_HIGH` / `CODEMAP_FANOUT_HIGH` /
  `CODEMAP_FANIN_MIN` 可校准。

  > **实测校准（真实 LLM 狗粮发现）**：Concordia 公式在**极小仓库**退化——S≈2 时，只被 2 条
  > 线调用的节点 `相对扇入≈1.44` 就爆表，导致 fixture 里**故意构造的污染案例 `parse_jwt`
  > 被镀金成「公共枢纽」**。修复不是调阈值，而是加**绝对扇入下限 `fanin_min`（默认 4）**：
  > 真正的公共枢纽既要相对中心度高、也要绝对调用面广；只被两条线调用的节点无论仓库多小都不算
  > 系统级基建。加下限后 `parse_jwt` 正确回落为 `pollution`、`get_current_user`（sink）为
  > `healthy-seam`。
- **V2-M3 四态评审** ✅：[`agents/review.py`](./src/codemap/agents/review.py) 升级为**归属权
  联合判断**（相对扇入 × 扇出 × 可见性），verdict 由两态扩为四态：
  `healthy-seam` / `shared-utility` / `pollution` / `god-node`。确定性兜底**先看中心度**——
  高相对扇入的节点优先归入 shared-utility(低扇出) 或 god-node(高扇出)，只有**低中心度**的
  私有中间环节被跨线摄取才判 pollution。地铁图（[`web/subway.html`](./web/subway.html)）据此
  渲染：金色枢纽 / 绿色接缝（换乘站合并）vs 红色污染 / 上帝节点（主线外红虚线）。

> **已解决（原狗粮发现）**：V1 启发式「任一主线视其为 processor 即判 dangerous」会把
> **设计上就该共享的工具节点**（如 `expand_one`/`_downstream`）误报为职责污染。V2 用「先看
> 相对扇入中心度」的归属权判定把它们重归为 `shared-utility`（金色枢纽），而真正的污染
> （fixture 的 `parse_jwt`，低中心度私有中间结果被跨线摄取）仍判 `pollution`。回归测试见
> [`tests/test_review_ownership.py`](./tests/test_review_ownership.py)。

## 8. 已知风险与设计决策

| 风险 | 缓解 |
|------|------|
| ~~MCP 工具真实 schema 未知，上层全依赖它~~ ✅ 已解决 | M1 已固化真实契约于 `docs/mcp_tools_contract.json`（v0.8.1 实测） |
| LLM 污点判定会误判（漏判断线 / 误判污染） | 每次判定带 confidence；低置信降级标记不直接剪 |
| DFS + Fork 指数爆炸 / 环形调用死循环 | `MAX_DEPTH` / `MAX_WORKERS` + `traces` 去重 visited |
| Minimax / Dashscope 的 Prompt Cache 行为与 Anthropic 不同，成本模型可能崩 | 早期独立验证两家 cache 命中是否真省钱 |
