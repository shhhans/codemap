# 结构化智能层 V2.1 · 交叉点定性重构设计 (Intersection V2.1)

> 本文档是 V2.1 阶段「交叉点定性」重构的施工契约。它直接回应
> [`agent_exploration_report.md`](./agent_exploration_report.md) §4–5 留下的唯一未解缺口：
> **评审系统性过度报警**——把「设计上就该被多处复用的公共原语」误判为「职责污染」。
>
> 设计经三方（实现方 / NotebookLM / 架构顾问）多轮收敛定稿。三条不可动摇的前提：
> 1. **运行时契约高于白皮书**：以 [`mcp_tools_contract.json`](./mcp_tools_contract.json)（v0.8.1 实测）为准——
>    工具是 `trace_path`（非 `trace_call_path`）、社区检测是 **Leiden**（非 Louvain）。
> 2. **捍卫已有工程补丁**：`worker._downstream` 的 `qualified_name` 精确解析（防短名幻象）、
>    `worker._recover_dynamic` 的动态分发名称匹配补边（×0.8 置信），是不可侵犯的核心资产。
> 3. **机器取证，LLM 定罪**：确定性逻辑只负责**摆出结构证据**，绝不用 SQL 硬判 dangerous；
>    最终裁决由 LLM 法官给出，离线/不确定时一律降级为 `suspected`（黄），不落红。

---

## 1. 问题回顾（为什么二元角色判定不够）

现行 `review.py` 用一条确定性规则：**「任一主线把交叉节点记为 `processor` 即判 dangerous」**。

- 在 fixture 上**正确**：`parse_jwt`（双线 processor）判危险，`get_current_user`（双线 sink）判健康。
- 在狗粮（解析自身）上**系统性误报**：`expand_one` / `_downstream` 等**被设计为公共复用的核心原语**，
  天然是 `processor`，一旦被两条驱动交叉就必然触发危险规则。

报告的核心洞见：判定关键**不是「sink 还是 processor」，而是节点的「归属与可见性」**。

> - **污染** = 一条主线伸手摄取了**另一条主线的私有中间结果**（绕过稳定接口直取内部件）。
> - **健康共享** = 多条主线通过**公共工具/接口**正常复用同一个节点。

V2.1 不新增一类「公共枢纽」硬标签（节食的 Worker 只看 1 跳，结构上不可能认出公共枢纽），
而是把这个区分**上移到延迟评审**，交给唯一拥有全局视野的角色，用**拓扑证据 + 调用链证据**
让 LLM 自行识别。

---

## 2. 数据契约改动（Sprint 1 · 最小集）

### 2.1 `traces` 增加父指针 `parent_node_id`

「越级摄取」检测要重建「B 到达交叉点 X 的路径」，判断 B 是否绕过了 A 暴露的门面。
重建路径需要前驱信息，而现 `traces` 只有 `(node_id, flow_type, role, depth)`——**depth 给不了路径**。

关键事实：**父节点在打卡时本就在作用域里**——`worker._record` 是在
`expand_one(qualified_name=父, ...)` 内部被调用的。所以这是一处**近乎零成本**的改动：

```sql
ALTER TABLE traces ADD COLUMN parent_node_id TEXT;   -- seed 行为 NULL
```

`UNIQUE(node_id, flow_type)` 决定了每条主线对每个节点只记一次（首次到达者），
因此**每条主线的 traces 是一棵树**，父指针唯一，路径重建 = 顺着父指针上溯，O(depth)。
（注意 sink 是树的叶子：`expand_one` 不向 sink 下钻。）

### 2.2 放开第三态 `suspected`

`intersection_verdicts.verdict` 是裸 `TEXT`（无 CHECK 约束），存 `'suspected'` **无需迁移**。
仅需把 `Blackboard.record_verdict` 的白名单从 `{healthy, dangerous}` 扩为
`{healthy, dangerous, suspected}`。

### 2.3 路径重建（纯 SQL，零图谱调用）

```sql
WITH RECURSIVE path(node_id, parent_node_id, lvl) AS (
    SELECT node_id, parent_node_id, 0 FROM traces
      WHERE node_id = :X AND flow_type = :flow
  UNION ALL
    SELECT t.node_id, t.parent_node_id, p.lvl+1
      FROM traces t JOIN path p ON t.node_id = p.parent_node_id
     WHERE t.flow_type = :flow
)
SELECT node_id FROM path;   -- = 从 seed 到 X 的整条路径
```

---

## 3. 三态法官 · 取证而非定罪 (Sprint 3)

### 3.1 时序：收敛后一次性全局裁决

Coordinator 在 `queue.join()` 收敛后唤醒评审。此时全局上下文齐备，可以为少数交叉点
**付得起**「拉完整方法体 + 查全局拓扑」的代价（探索期则严格节食，只看签名）。

### 3.2 证据包 (Evidence Pack)

确定性逻辑只负责**取证**，把结构事实摆出来，**对称**地既给指向 dangerous 的证据，
也给指向 healthy 的证据（避免把 LLM 往危险带偏）：

```
[交叉点] X    各主线角色: {auth: processor, billing: processor}
[路径·auth ] seed_auth → … → X                         (递归 SQL)
[路径·billing] seed_billing → … → X
[门面证据] 在 auth 中找到节点 S=verify_token，静态图存在 S ─CALLS*1..3─> X，
          且 S ∉ billing 的路径   →「疑似绕行 (bypass)」证据  ← 指向 dangerous
[全局扇入] X 被 N 个不同调用者引用（query_graph 统计 CALLS 入边）  ← N 高则指向 healthy（公共枢纽）
[完整方法体] <X 的 body>
[置信度] 本交叉最低置信 = min(traces.confidence)；含 recovered(0.8) 边 → 偏低
→ LLM 输出: {verdict: healthy|dangerous|insufficient_context, description, [target_node_id]}
```

### 3.3 门面绕行 (Façade-Bypass) 取证算法

对交叉点 X、两条主线 A/B（X 在 A 中为 `processor`）：

1. **找门面候选 S**：取 A 的 traces 中**调用了 X**的节点（A 内部包住 X 的更外层接口）。
   静态图查询（**有界**，防爆炸）：
   ```cypher
   MATCH (s {qualified_name:$S})-[:CALLS*1..3]->(x {qualified_name:$X}) RETURN count(*) > 0
   ```
2. **判 B 是否绕行**：门面 S 是否出现在 B 到 X 的路径（§2.3）上？
   `S ∉ path_B` → B 越过门面直取内部件 →「疑似绕行」证据。
3. **关键：这只是证据，不是判决**。是否真污染由 LLM 结合扇入综合判：
   高扇入的节点即使被绕行，也可能是**公共枢纽**（健康复用）。

> **引擎能力实测（v0.8.1）**：原设计还想用「调用者横跨多少个 Leiden 社区」作为公共枢纽
> 的第二个信号，但实测引擎**不暴露 per-node 社区属性**（`f.community` 为空），`get_architecture`
> 的 cluster 只给成员**计数**+5 个代表节点、没有完整成员表，无法把任意节点的调用者映射到社区。
> 故**砍掉跨社区信号，公共枢纽判定仅依赖全局扇入**。若引擎日后提供可查询的社区属性再补。

### 3.3a 共享祖先继承 (Shared-Ancestor Inheritance)

> 这是狗粮 C(1) 暴露的缺口的修复。背景：聚焦狗粮中 `_classify` 与 `_downstream` 的结构证据
> **完全相同**（fan_in=1、唯一调用者都是 `expand_one`、两条流都只经 `expand_one` 抵达），
> 却拿到了不同判决（healthy vs suspected）——这是 LLM 在「证据不决定性」下的**非确定性抖动**，
> 而非真实差异。根因：证据包把「没有绕行」当作信号的**缺席**，太弱；缺一个信号的**在场**——
> 「两条流是经同一个公共枢纽到达的下游，而非独立摄取」。

**机制**：污染要求两条流通过**不同的门**到达 X（其一绕过门面）。若它们在 X 之前**共享一个
也是交叉点的上游节点 C**（即分叉发生在 C 之后），则 X 是 C 的下游、被「传递性地共享」，并非独立摄取。

- **取证**：求各主线到 X 的路径（§2.3）的交集，取其中**最近的、本身也是交叉点**的节点 C
  作为 `shared_ancestor`（`review._shared_ancestor`）。
- **拓扑序评审 + 继承**：按 `node_min_depth` **浅交叉点先评**；评到 X 时，若其 `shared_ancestor` C
  已有判决，则 **X 直接继承 C 的判决**，不再问 LLM（`review._review_one`）。
- **方向中立**：是「继承 C 的判决」，不是「一律 healthy」——若 C 本身被判 dangerous，X 也继承 dangerous。
- **收益**：`_classify`/`_downstream` 因「经由已判 healthy 的 `expand_one`」**确定性地**判 healthy，
  抖动消失；附带省去深层交叉点的 LLM 调用。

### 3.4 三态决策与离线降级

| 门面 S | B 绕行 | 全局扇入 | 置信度 | 证据指向 / 兜底 |
|---|---|---|---|---|
| 有 | 是 | 低 | 正常 | 强 dangerous 证据 → **交 LLM 判**（多半 dangerous） |
| 有 | 否（走正门） | — | — | 强 healthy 证据 → **交 LLM 判** |
| 无 | — | 高扇入 | 正常 | 强 healthy 证据（公共枢纽） → **交 LLM 判** |
| 任意 | 任意 | — | 含 recovered/低置信 | **直接 suspected（黄）**，不交 LLM 硬判 |

**铁律**：
- SQL **永不**硬判 dangerous——红色**只有 LLM 显式说红才成立**。
- LLM 不可用 / JSON 解析失败 / retries 超限 → 一律落 **`suspected`**，绝不落 dangerous
  （这取代了旧 `review.py` 的「离线兜底 dangerous」）。

### 3.5 `insufficient_context` 路由反刍与终止护栏

LLM 可输出 `insufficient_context`（附 `target_node_id` + 疑问理由），要求补充侦查：

- **per-node 计数器** `investigation_retries`（Max=2）：每补一次上下文（拉 target 的方法体 /
  其调用者）后二次开庭。
- **全局轮数上限**：整轮反刍也设上限，防多案交替拖死。
- **超限即降级**：任一上限触顶 → 锁定 `suspected`（黄），不硬判。

> 本阶段先实现**评审层内**的有界二次取证（满足护栏精神、可独立运行）；
> Coordinator 级的队列重注入式反刍（把特种侦探 Task 压回队列）留作后续增强。

### 3.6 路径爆炸护栏（落实顾问的担忧）

1. 门面候选**只取 A 已打卡的节点**（个位数），不对全图扫描；
2. `CALLS*1..3` 深度**不上调**，Cypher 带行数/时间预算；超预算就把该证据标「取证不全」，
   交 LLM 走 `insufficient_context`，而不是加深去硬找；
3. 找不到门面就退回扇入信号，不靠加深。

---

## 4. 可视化与导出（三态化）

- `export.py`：交叉点 `type` 直接透传 verdict（`healthy|dangerous|suspected`）；
  无 verdict 的默认值从 `dangerous` 改为 **`suspected`**（更诚实——未评审 = 待定，而非有罪）。
- `web/subway.html`：新增**黄色**第三态渲染（站点黄环 + 「疑似/待确认」标签 + 图例 + 副标题计数）。
  红/绿/黄三档对应 report §5.5 的「真危险 / 公共枢纽 / 疑似」。

---

## 5. 对「公民体系」的冲击（宪法修订）

V2.1 不是给公民体系补一个标签，而是改了它的宪法，必须在文档与注释中明确：

1. **Role 是局部意见，不是身份**。`node_role`(sink/processor) 只是**某条主线在 1-hop 局部的看法**；
   节点真正的「户籍」（私有内部件 vs 公共枢纽）是**全局属性，延迟到 Reviewer 才定**。
   公民体系因此分两层：**Worker 给的「局部角色」**（便宜、节食、只看一跳）
   vs **Reviewer 给的「全局户籍」**（昂贵、看全图）。
2. **「公共枢纽」是涌现的裁决，不是要打的标签**。节食的 Worker 结构上不可能认出公共枢纽
   （它不知道全局扇入），所以这个区分**必须**交给延迟 Reviewer——这修正了职责错配。
3. **污染（Tangling）是主线间的关系，不是节点的属性**。`dangerous` 不再是「这个节点坏」，
   而是「**B 以绕过门面的方式触碰了 A 的 X**」。判定对象从「给节点分类」升级为
   「给跨线换乘定性」——引入了**产权（私有车间 vs 公共设施）**与**通行规则（走正门 vs 闯入）**。
4. **第三户籍 `suspected`（黄）与置信度修饰**。换乘户籍从二元变三元，多了「待审」公民；
   且 `recovered(0.8)` / 低置信会把节点降级为 suspected——**「不确定」被正式写进公民模型**，
   系统被允许说「我不确定」，而不是假装总能红绿二判。

---

## 6. 施工里程碑映射

| Sprint | 内容 | 本文件章节 |
|---|---|---|
| **S1 契约固化与中枢精炼** | 保留无锁 schema / asyncio 队列；加 `parent_node_id` + 路径 SQL；放开 `suspected`；纠正幻觉 API 命名；固化 `_downstream`/`_recover_dynamic` | §2 |
| **S2 节食 Worker 纪律化** | 「绝不加载方法体」；低置信透传到 traces；父指针透传 | §2.1 |
| **S3 延迟法官与拓扑反刍** | 重写 Reviewer：证据包组装（路径 + 门面绕行 + 扇入 + 完整方法体 + 置信）、三态、有界反刍 + suspected 降级、导出/UI 三态化 | §3–4 |

---

## 7. 复现实验（fixture）

引擎 `Codebase-Memory` v0.8.1 实测确认 V2.1 所需信号全部可得（`cli query_graph`）：

```
CALLS 边:  verify_token→parse_jwt,  charge→parse_jwt(绕行),
           login→get_current_user,  charge→get_current_user
门面可达:  verify_token ─CALLS*1..3─> parse_jwt   = reaches:1
扇入:      parse_jwt fan_in = 2 (verify_token + charge) → 低 → 私有中间件
```

预期三态结果：
- `parse_jwt`：auth 门面 `verify_token` 被 billing 绕行 + 低扇入 → **dangerous**。
- `get_current_user`：双线 sink，消费稳定状态 → **healthy**。
- （狗粮）高扇入公共原语：被绕行但扇入高 → **healthy**（公共枢纽），不再误报。
