# V2 交接 Handout（结构化智能层 2.0）

> 面向下一个 session 的接力文档。读完这份 + `ARCHITECTURE.md` 的 §2.1 / §7.1 即可接手。

## 0. 一句话现状

V2 的 **M1（过滤存根化）/ M2（相对扇入指标）/ M3（四态归属权评审）** 已全部实现、用
**真实 codebase-memory 图谱 + 真实 MiniMax LLM** 端到端验证并提交推送。**44 个测试全绿**。
M4（工作记忆 / hypothesis_git）按原方案标注为可选，**未做**。

- 分支：`claude/sil-v2-architecture-discussion-0u85mm`（已 push，HEAD = `b7e683c`）
- 工作树干净，无未提交改动。

## 1. 本轮做了什么（V2 commits，从旧到新）

| commit | 内容 |
|---|---|
| `544871d` | **M1** 关系过滤 + 第三方存根化（`filtering.py`：全局黑名单 + import 解析 + Sink/Dual） |
| `be80a5f` | **M2** 相对扇入/出指标（`metrics.py`：Concordia `Fan-in/(S·ln S)` + `classify_hub`） |
| `511bf52` | **M3** 四态评审（verdict 扩为 `healthy-seam`/`shared-utility`/`pollution`/`god-node` + 地铁图金/红渲染） |
| `69d4ff9` | **M1 实测校准**：装上真实引擎实测——它**只索引仓库自身符号**；存根改走文件级 import + 剔除一方包，守护动态补边防幻象 |
| `7178a10` | **M2 修复**：小仓库 Concordia 退化 → 加**绝对扇入下限 `fanin_min`**，修掉 fixture 把 `parse_jwt` 镀金的误判 |
| `b7e683c` | **数据流证据 + Agent 主判** + 修两个潜伏 bug（见 §3） |

## 2. 关键文件地图

```
src/codemap/
├── filtering.py        # M1: GLOBAL_RELATION_BLACKLIST, StubModules(import解析/别名/externals_only), classify_boundary(Sink/Dual), stub_call_names
├── metrics.py          # M2: relative_fan_in(Concordia), classify_hub(rel×fanout×fanin_min), MetricsProbe(查S/fan-in/out)
├── config.py           # 阈值旋钮: CODEMAP_REL_FANIN_HIGH=0.08 / CODEMAP_FANOUT_HIGH=8 / CODEMAP_FANIN_MIN=4
├── llm.py              # max_tokens=4096(推理模型headroom); _extract_json 容忍<think>块
├── agents/
│   ├── worker.py       # M1接入: _file_imports(最长前缀定位Module/缓存), _handle_boundary, 动态补边带stub守护
│   └── review.py       # M3: 四态Prompt + Provenance(路径/callers/callees) + LLM主判, 确定性兜底, CODEMAP_DEBUG门控打印
├── blackboard/         # VERDICTS常量(已从__init__重导出), schema.sql, blackboard.py
└── milestones/         # m3_concurrent(fixture双线), m4_dogfood(自我解析); 注意seed写死在MAINLINES列表
web/subway.html         # 四态渲染: 金色枢纽/绿色接缝(合并换乘站) vs 红色污染/上帝节点(主线外红虚线)
```

## 3. 真实狗粮挖出并修掉的 3 个 bug（重要！）

1. **`VERDICTS` 未从包 `__init__` 重导出** → `review.py` 的 `from codemap.blackboard import
   VERDICTS` 每次评审都静默抛错 → **此前所有评审都回退到确定性兜底，LLM 从未被真正咨询**。
   已修 + 加回归测试。**教训：bare `except` 吞掉了关键 ImportError，靠 `CODEMAP_DEBUG` 才抓到。**
2. **推理模型 `<think>` 吃 token**，未设 `max_tokens` → JSON 被截断 → 解析失败回退。已设 4096。
3. **M2 Concordia 在小 S 退化**（S≈2 时 2 个调用者就 rel≈1.44）→ 加绝对扇入下限。

## 4. 环境（已就绪，但 session 重启后需重装）

- **codebase-memory**：`pip install codebase-memory-mcp==0.8.1`（启动器，首次运行从 GitHub
  Release 拉 266MB 静态二进制到 `~/.cache/codebase-memory-mcp/0.8.1/`）。
  - ⚠️ 启动器自带 `urllib` 拉大文件会被代理截断；若失败，手动
    `curl -fSL --retry 5 -C -` 下 `codebase-memory-mcp-linux-amd64-portable.tar.gz`
    （SHA256 对 release 的 `checksums.txt`，已验证 `6ab87a6c…f5abd`），解压放进缓存目录。
- **LLM**：`MINIMAX_API_KEY` 在环境变量里（session 重启可能需重设）。`CODEMAP_LLM_PROVIDER=minimax`，
  model `MiniMax-M3`（推理模型，prompt cache 命中正常）。
- **依赖**：`pip install -e ".[dev]"`（PyJWT 冲突时加 `--ignore-installed PyJWT`）。

## 5. 怎么跑 / 验证

```bash
python -m pytest -q                                   # 44 passed
python -m codemap.milestones.llm_check                # 确认 LLM 连通
python -m codemap.milestones.m3_concurrent --repo fixtures/sample_app   # 双线fixture(确定性交叉)
python -m codemap.milestones.m4_dogfood --depth 8     # 解析自身(LLM追踪随机,交叉点不保证复现)
python scripts/render_subway.py web/subway_map.png    # 渲染地铁图(需playwright: pip install playwright)
```

**实测确认有效的结论**（构造镜像场景喂真实 LLM，确定性可复现）：
- `index_repository` → **healthy-seam**（两线各自浅层独立调用的公共启动步骤）
- `parse_jwt` → **pollution**（billing 绕过稳定接口截取 auth 私有 claims，LLM 引用了 docstring）

## 6. 待办 / 下一步（按优先级）

1. **[悬而未决·需你定夺]** `_decode` 链式一致性：LLM 现在逐节点独立判断，出现「父节点
   `parse_jwt` 判 pollution，但其叶子 `_decode` 被判金色枢纽」的局部张力。选项：给评审带上
   **上游已判定上下文**做链式约束，或接受 LLM 独立判断。（上轮结尾问的就是这个。）
2. **阈值校准**：`CODEMAP_REL_FANIN_HIGH/FANOUT_HIGH/FANIN_MIN` 默认值是拍的；fixture 太小、
   指标退化，**应在真实大仓库上重新校准**（dogfood S≈478 才有意义）。
3. **`--seeds` CLI**：当前主线 seed 写死在 `m3_concurrent.py`/`m4_dogfood.py` 的 `MAINLINES`
   列表；架构本身**不限 seed 数量**（`Coordinator.run` 接受任意 `list[SeedSpec]`，`max_workers`
   只限并发槽位）。可加一个 `--seeds flow:seed:material,...` 让任意条主线免改代码。
4. **M4（可选/高阶，未做）**：全局黑板加 `hypotheses`/`checkpoints` 表 + `hypothesis_git`
   CLI 骨架（计划/分支/回滚）。与测绘主线正交。
5. **dogfood 自相交不稳定**：LLM 追踪是随机的，两条主线在 depth=8 不保证相交。若要稳定验证
   自我交叉，可加深 depth 或选更易收敛的 seed。

## 7. 设计要点速记（避免重复踩坑）

- **图引擎只索引仓库自身符号**：第三方/stdlib 调用根本不进图。所以 M1 候选级存根分区是良性
  no-op；存根的真实价值在**动态补边路径**（守护 `np.dot` 的裸名 `dot` 不被误匹配到唯一内部 `dot()`）。
- **import 是文件级的**：解析方法体拿不到 import；要按节点 qualified_name 的**最长前缀**定位其
  `Module` 节点（`Module.file_path` 属性是空的，别用它匹配）。
- **一方包要剔除**：项目自身包（如 `codemap`）import 起来像库但解析到内部可追节点，用
  `externals_only(内部段集合)` 去掉。
- **评审 LLM 是主判官**，指标是佐证；确定性规则仅兜底。改判定逻辑时优先改 Prompt + Provenance，
  而非确定性规则。
