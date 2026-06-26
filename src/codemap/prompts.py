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

# 判定规则（严格执行）
1. **物质依赖优先**：只有当下游节点在参数 / 闭包中**实际接收了原材料**，或会读取/改写
   承载该原材料的状态时，才算「继承了物质流」。
2. **过滤纯控制流**：若某调用只是控制流的附带产物（计时、加锁、打日志、参数校验、
   与原材料无关的工具调用），即使在源码里出现，也要判为**噪音 (noise)** 并剪枝。
3. **防爆墙拦截**：命中 Barrier 定义的，判为 **barrier**，停止该方向探索。
4. **不确定时不要武断剪枝**：把握不大的，给较低的 confidence 并归入 `continue`，
   由上层决定，避免误杀主线。

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
