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
你的任务：站在「当前节点」的滑动窗口内，判断它调用的每一个「下游候选节点」是否真正
**接收并加工**了这份原材料，从而决定追踪是继续深入、稳定沉淀，还是作为噪音剪枝。

# 核心公民体系与判定概念
1. **Sink (稳定沉淀点)**：原材料完成加工、落到一个稳定结果的地方（如 `getCurrentUser()`
   返回 User 对象、落库 `db.execute`、返回 HTTP 响应）。这是主线在该分支上的合理终点。
2. **Processor (中间加工环节)**：对原材料做了实质变换，且结果仍会继续向下游流动的业务处理
   步骤（如 `parse_jwt()`）。这是主线的「普通站点」，应保留并继续追踪。
3. **Barrier / Stub (防爆墙与外部存根)**：第三方库、框架内置方法或已声明的「完美黑盒」。
   - **只进不出**：原材料进入即被吸收/落地，判为 `barrier`，停止该分支
     （如 `logger.info`、`json.dumps`、ORM 落库底层）。
   - **有进有出 (Dual 透明转换器)**：仅做格式化/提取、把原材料原样或等价地透传给下游
     （如 `format_date`、`extract_id`），判为 `continue` 且 `is_sink=false`，保持主线连贯、
     交由下游与评审引擎继续判定；其第三方源码本就不在图中，下钻一跳即自然终止。
4. **Noise (控制流噪音)**：纯控制流或与原材料无关的附带产物（计时、加锁、探活、
   与当前原材料无关的参数校验）。

# 判定规则（最高优先级，严格执行）
1. **物质依赖优先**：只有当下游节点在参数、闭包或实例状态中**实际接收并处理了原材料**，
   才算「继承了物质流」。源码中出现调用不等于物质流入。
2. **无情过滤噪音**：即便候选节点是当前节点的重要业务逻辑，只要它**没有碰**当前追踪的
   原材料，必须立刻判为 `noise` 剪枝。
3. **识别透明转换 (Dual)**：遇到仅做数据格式化、提取（如 `extract_id`）的轻量级工具，
   应判为 `continue`（`is_sink=false`），以保持主线连贯。
4. **不确定性降级保留**：对于动态分发调用或把握不大的候选节点，不要武断剪枝。将
   `confidence` 调低并归入 `continue`，交由全局黑板与评审引擎处理。
5. **是否下钻只由 `is_sink` 决定**：`is_sink=true` → 在此打卡即止，不再展开下游；
   `is_sink=false` → 继续向其下游深入。不存在「continue 但不深入」的第三态。
6. **叶子即沉淀**：候选若标注 `[扇出=0·叶子]`，说明它在图中已无下游、无法再向下传递物质——
   它接收了原材料则判 `continue` + `is_sink=true`（沉淀点），与原材料无关则判 `noise`；
   无需纠结是否继续深入（本就无处可去）。

# 输出格式
你必须进行批量判定。请严格输出 JSON 格式，不要包含任何 Markdown 标记块 (```json) 或多余文字：
{
  "decisions": [
    {
      "node": "<候选节点的 qualified_name 或序号>",
      "verdict": "continue" | "barrier" | "noise",
      "is_sink": true | false,
      "confidence": 0.9,
      "reason": "<一句话理由，限20个中文字符内>"
    }
  ]
}

# JSON 字段严格约束：
- **node**：原样返回提供的候选节点标识，每个候选节点必须且只能出现一次。
- **verdict**：仅限 `continue`（物质流入并加工）、`barrier`（命中黑盒基建拦截）、
  `noise`（无关控制流或未摸到原材料）。
- **is_sink**：布尔值，仅对 `continue` 节点有意义。当该节点是主线的沉淀点/终点
  （稳定状态、落库、返回响应）时设为 `true`，系统将在此打卡但不再展开下游；普通加工环节
  与透明转换器设为 `false`。
- **confidence**：0.0 到 1.0 之间的浮点数，表示你对该判定的置信度（如动态推测的边可给 0.6）。
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
    # Out-degree in the call graph, filled by the worker's static fast-path. 0
    # means a leaf (no downstream) — it cannot propagate material further, so if
    # it touches the token it is a Sink, else noise. None = not measured.
    fan_out: int | None = None


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
        leaf = "  [扇出=0·叶子]" if c.fan_out == 0 else (
            f"  [扇出={c.fan_out}]" if c.fan_out else "")
        lines.append(f"{i}. {c.ref}{loc}{tag}{leaf}")
        lines.append(f"   signature/snippet: {c.signature}")
    lines += [
        "",
        "[请分析数据流并返回 JSON 指令]:",
        "- 哪些是防爆墙 (barrier)？",
        "- 哪些是无效噪音 (noise)？",
        "- 哪些需要继续追踪 (continue)，其中哪些已是稳定沉淀点 (is_sink)？",
    ]
    return "\n".join(lines)
