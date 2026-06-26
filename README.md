# Codemap — 结构化智能层 (Structural Intelligence Layer)

面向 Vibe Coding 的轻量级本地可视化工具。摒弃传统"文件树"和"静态调用图（毛线团）"，
采用 **"代码加工厂与物质流 (The Code Factory & Material Flow)"** 世界观，把代码仓库中的
**业务主线 (Material Flow Mainline)** 提取出来，并在主线交汇处识别架构健康状态
（职责叠加 vs. 职责污染），最终渲染成一张**地铁图 (Subway Map)**。

> 视觉隐喻：主线 = 地铁线，加工节点 = 站点，交叉点 = 换乘枢纽。

完整设计见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

## 架构速览

```
                ┌─────────────────────────────────────────────┐
   用户自然语言 →│  Coordinator (调度官)                          │
                │   · Seeding: LLM + MCP 找起点                  │
                │   · Fork 控制: 数据流分叉时实例化新 Worker       │
                └───────────────┬─────────────────────────────┘
                                │ 派发 (Node, Token)
                 ┌──────────────┴───────────────┐
                 ▼                              ▼
        ┌──────────────────┐          ┌──────────────────┐
        │ Worker Agent      │   ...    │ Worker Agent      │   并发虚拟污点追踪器
        │ 小滑动窗口 + 大    │          │ (DFS + 语义剪枝)   │   (共享 System Prompt
        │ System Prompt     │          │                  │    → 吃 Prompt Cache)
        └────────┬─────────┘          └────────┬─────────┘
                 │ log_trace(node, flow)        │
                 ▼                              ▼
        ┌─────────────────────────────────────────────────┐
        │ Global Blackboard (SQLite)                       │
        │  Nodes · Traces · Intersections(交叉点自动定性)    │
        └────────────────────┬────────────────────────────┘
                             │ export
                             ▼
                   subway_map.json → 极简 HTML 地铁图
```

底层图谱由 [`Codebase-Memory`](https://github.com)（C / Tree-Sitter / SQLite，单二进制）
通过 **MCP** 暴露 `trace_call_path`、`get_code_snippet` 等结构化查询工具供上层 Agent 调用。

## 开发里程碑

| Milestone | 目标 | 状态 |
|-----------|------|------|
| **M1** 基础设施连通 | Python 经 MCP 连上 Codebase-Memory，固化工具契约 | ✅ 完成 (v0.8.1 实测) |
| **M2** 单线 DFS 追踪 | System Prompt + 滑动窗口，硬编码 Seed 跑通单主线剪枝 | ✅ 真实 MiniMax-M3 跑通 |
| **M3** 黑板与并发分叉 | SQLite 黑板 + `log_trace` + Fork，构造危险交叉并报警 | ✅ 真实 LLM 跑通 |
| **M4** 可视化与狗粮 | 导出 JSON 契约 → HTML 地铁图（真换乘站 + 污染连接），解析自身源码 | ✅ 完成 |

### V2 升级（结构化智能层 2.0 · 带架构所有权判定）

| Milestone | 目标 | 状态 |
|-----------|------|------|
| **V2-M1** 过滤与存根化 | `filtering.py`：全局关系黑名单 + import 解析的第三方存根（Sink/Dual 判别），接入 Worker | ✅ 完成 |
| **V2-M2** 相对扇入/出 | `metrics.py`：Concordia `Fan-in/(S·ln S)` + 扇出联合判定（公共枢纽 vs 上帝节点） | ✅ 完成 |
| **V2-M3** 四态评审 | ReviewAgent 归属权联合判断；verdict 升级为 healthy-seam / shared-utility / pollution / god-node，地铁图金/绿/红渲染 | ✅ 完成 |

> V2 核心：从纯数据流追踪升级为带**架构所有权 (Ownership)** 的语义分析，用「相对扇入中心度」
> 区分**健康的公共枢纽（金色换乘站）**与**危险的职责污染**，消除 V1 狗粮中的过度报警。

## 快速开始

```bash
# 1. 安装（开发模式）
pip install -e ".[dev]"

# 2. 配置（复制并填入 API key / Codebase-Memory 路径）
cp .env.example .env

# 3. Milestone 1：验证 MCP 连通性并固化工具契约
python -m codemap.milestones.m1_connectivity
# 成功后会在 docs/mcp_tools_contract.json 生成真实的工具 schema 快照

# 4. 验证 LLM 连通性（确认 MINIMAX_API_KEY 已生效）
python -m codemap.milestones.llm_check

# 5. Milestone 2：单线 DFS 污点追踪（默认 dogfood 解析本仓库）
python -m codemap.milestones.m2_single_dfs \
    --seed main --flow Trace --material "MCP request"

# 6. Milestone 3：双主线并发 + Fork + 危险交叉报警（解析 fixtures/sample_app）
python -m codemap.milestones.m3_concurrent

# 7. 渲染地铁图（健康交叉=换乘站，危险交叉=主线外红色虚线连接）
python scripts/render_subway.py web/subway_map.png

# 8. Milestone 4 狗粮：让 codemap 解析自己的源码
python -m codemap.milestones.m4_dogfood --depth 4
python scripts/render_subway.py web/subway_map.png
```

## 配置项

所有配置经环境变量注入（见 [`.env.example`](./.env.example)）：

| 变量 | 说明 |
|------|------|
| `CODEBASE_MEMORY_BIN` | `Codebase-Memory` 二进制路径 |
| `CODEBASE_MEMORY_ARGS` | 启动参数（如指向 SQLite 图谱 / 目标仓库） |
| `MINIMAX_API_KEY` / `MINIMAX_BASE_URL` | Minimax LLM 端点 |
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | Dashscope LLM 端点 |

## 项目结构

```
src/codemap/
├── config.py              # 集中式配置（环境变量）
├── mcp_client.py          # Codebase-Memory MCP 客户端封装 (M1)
├── blackboard/
│   ├── schema.sql         # 全局黑板 SQLite schema
│   └── blackboard.py      # Blackboard 读写 + 交叉点视图
└── milestones/
    └── m1_connectivity.py # M1 连通性验证 + 工具契约固化
```
