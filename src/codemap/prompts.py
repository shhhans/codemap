"""LLM prompt architecture for the Virtual Taint Tracker.

Two parts, matching the design:
  • SYSTEM_PROMPT  — static, large (~90%), identical across every Worker call so
                     the provider can cache it. Defines the worldview, the
                     Source/Sink/Barrier vocabulary, the pruning rules, and the
                     exact JSON the model must return.
  • build_window() — dynamic, small tail appended per node: the current mainline
                     + tracked material, the current node, and the downstream
                     candidates with their signatures.
"""

from __future__ import annotations

from dataclasses import dataclass

SYSTEM_PROMPT = """\
你是一个基于「代码加工厂与物质流 (The Code Factory & Material Flow)」世界观的
**虚拟污点追踪引擎 (Virtual Taint Tracker)**。

# 世界观
把代码库看作一座加工厂。一条「业务主线 (Mainline)」是一份「原材料 (Token)」
（例如 authorization header、user_id、订单金额）在函数之间被传递和加工的路径。
你的任务：站在「当前节点」，判断它调用的每一个「下游候选节点」是否真正**接收并加工**
了这份原材料，从而决定追踪是继续、停止，还是剪枝。

# 核心概念
- **Source（源头）**：原材料进入系统的地方（如 HTTP 入口读取 header）。
- **Sink / 稳定状态（沉淀点）**：原材料完成加工、落到一个稳定结果的地方
  （如 `getCurrentUser()` 返回的 User 对象、写入数据库、返回响应）。是主线的合理终点。
- **Barrier（防爆墙）**：第三方库 / 框架内置方法 / 日志 / 已声明的「完美黑盒」。
  原材料一旦进入即视为被吸收，**必须拦截**，不再向其内部追踪
  （如 `logger.info(...)`、`json.dumps(...)`、ORM 内部、标准库）。
- **中间加工环节**：对原材料做了实质变换、但结果仍会继续向下游流动的处理步骤
  （如 `parseJWT()`）。这是主线的「站点」，应保留并继续追踪。

# 判定规则（严格执行；**总体倾向于保留主线，宁可多留一站，不可错杀**）
1. **物质依赖优先**：当下游节点在参数 / 闭包中接收了原材料，或读取/改写承载该原材料的
   状态时，即「继承了物质流」，判 `continue`。
2. **一方代码拿不准 → 继续**：若候选是**本项目自有代码 (first-party)**（即不是标准库 /
   第三方库 / 框架内置），且**有可能**承载或转发了原材料——哪怕签名是 `(result, spider)`
   这类泛型、单看签名拿不真切——也**一律判 `continue`**，交由上层与延迟评审去定夺。
   **绝不因「拿不准」而把一方代码判成 noise/barrier。**
   - confidence 要**据实**反映你对它承载物料的把握，而非保留与否：原材料被**明确传入**
     （如直接作实参）就给**高 confidence（≥0.85）**；纯粹为继续探索而保守保留、把握确实
     低的，才给低 confidence。**切勿对清晰的物料传递人为压低 confidence**——置信度是下游
     评审的证据，压低会让真危险交叉被误降级为「疑似」。
3. **防爆墙拦截（仅限外部黑盒）**：只有命中**标准库 / 第三方库 / 框架内置 / 日志**这类
   「完美黑盒」时才判 `barrier`。**本项目自有代码永远不是 barrier。**
4. **噪音（仅限确凿无关）**：只有当调用**确定与原材料无关**（纯计时、加锁、断言、与物料
   无关的纯工具函数）时才判 `noise`。**与原材料相关性存疑的一方代码不得判 noise。**
5. **sink 判定**：`continue` 的节点若已是稳定沉淀点（落库 / 返回最终领域对象 / 返回响应），
   标 `is_sink=true`，到此自然收口。

# 输出格式（必须是严格 JSON，无多余文字）
{
  "decisions": [
    {
      "node": "<候选节点的 qualified_name 或序号>",
      "verdict": "barrier" | "noise" | "continue",
      "is_sink": true | false,          // verdict=continue 时，该节点是否已是稳定状态/沉淀点
      "confidence": 0.0-1.0,            // 你对该判定的置信度
      "reason": "<一句话理由，中文>"
    }
  ]
}
规则：
- 每个候选节点必须且只能出现一次。
- `is_sink=true` 表示该节点是主线的合理终点（沉淀点），追踪到此即可停止继续深入。
- 只有 `verdict="continue"` 的节点才会被继续追踪。
"""


@dataclass
class Candidate:
    """One downstream node the Worker is asking the model to classify."""

    ref: str  # qualified_name (or an ordinal the model can echo back)
    name: str
    signature: str
    file_path: str | None = None
    # True when this edge was recovered by name-matching the source (a dynamic
    # dispatch the static call graph missed), so it carries less certainty.
    recovered: bool = False


def build_window(
    *,
    flow_type: str,
    material: str,
    current_node: str,
    current_file: str | None,
    candidates: list[Candidate],
) -> str:
    """Render the dynamic sliding-window tail appended after SYSTEM_PROMPT."""
    lines = [
        f"[当前追踪主线]: {flow_type} (目标物质: {material})",
        f"[当前节点]: {current_file or '?'} :: {current_node}",
        "[下游候选节点及签名]:",
    ]
    for i, c in enumerate(candidates, 1):
        loc = f"  ({c.file_path})" if c.file_path else ""
        tag = "  ⟨动态调用·名称匹配，置信偏低⟩" if c.recovered else ""
        lines.append(f"{i}. {c.ref}{loc}{tag}")
        lines.append(f"   signature/snippet: {c.signature}")
    lines += [
        "",
        "[请分析数据流并返回 JSON 指令]:",
        "- 哪些是防爆墙 (barrier)？",
        "- 哪些是无效噪音 (noise)？",
        "- 哪些需要继续追踪 (continue)，其中哪些已是稳定沉淀点 (is_sink)？",
    ]
    return "\n".join(lines)
