"""ReviewAgent — ownership-based intersection verdicts (Milestone 3, V2).

When several mainlines check in on the same node, is that a healthy seam or
responsibility pollution? V1 answered with a single lever — "any mainline treats
it as a processor → dangerous" — which over-reported: nodes *designed* to be
shared infrastructure (idempotency checks, node-expansion primitives) got
flagged as pollution merely because many lines flow through them.

V2 replaces that with an **ownership join** over three signals:

  • **Relative fan-in** (Concordia, scale-free) — how central the node is.
  • **Absolute fan-out** — how coupled it is to its own dependencies.
  • **Visibility** — is it a private implementation detail (`_name`) or a
    public surface?

This yields a four-citizen taxonomy:

  healthy-seam    all crossing lines consume it as a stable Sink (settled state).
  shared-utility  high relative fan-in + low fan-out → a 已飞升 public hub
                  (金色换乘站); many lines meeting here is *good* architecture.
  pollution       low centrality intermediate (processor) whose half-processed
                  result is tapped across lines — a private implementation leak.
  god-node        high relative fan-in + high fan-out → infra-disguised mess.

The structural metrics give a deterministic verdict that *also* backs the LLM up
when it is unavailable or unparseable. Crucially, the centrality check fires
before the processor check, so a genuine shared primitive is reclassified as a
gold hub instead of a false pollution alarm — the exact dogfood over-report the
V1 ARCHITECTURE notes flagged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient
from codemap.metrics import (
    GOD_NODE,
    ORDINARY,
    SHARED_UTILITY,
    MetricsProbe,
    NodeMetrics,
    classify_hub,
)

REVIEW_SYSTEM_PROMPT = """\
你是「代码加工厂」世界观下的**交叉点评审 Agent (Intersection Reviewer)**。
多条业务主线在某个代码节点交汇时，你要判断这次交汇属于哪一类「代码公民」。

# 四类交叉公民（V2）
- **[healthy-seam 稳定接缝]**：所有交汇主线都把它当作**稳定沉淀点 (Sink)**
  （如 `getCurrentUser()` 返回的稳定 User 对象），各线消费的是这份已沉淀的稳定状态。
- **[shared-utility 公共枢纽 / 已飞升节点]**：被设计为系统级复用的**底层公共原语**
  （如幂等校验、节点展开函数）。特征是**高相对扇入 + 低扇出**——很多主线在此healthily
  交汇是**极佳的架构健康状态**，应标记为金色换乘站，**不是污染**。
- **[pollution 危险职责污染]**：某主线的**私有中间加工结果 (intermediate / processor)**
  被另一条主线**绕过稳定接口直接摄取**（半成品泄漏）。特征是**低相对扇入**且被当作中间环节。
- **[god-node 上帝节点]**：**高相对扇入 + 高扇出**，伪装成基建的「烂代码中心」，既被很多人
  依赖又依赖很多人，牵一发动全身。

# 归属权联合判断（务必结合下列指标，不要只看角色）
1. **相对扇入 (relative fan-in)** 高 ⇒ 是公共枢纽候选；低 ⇒ 是某主线私有环节候选。
2. **绝对扇出 (fan-out)** 高 ⇒ 耦合重，公共枢纽要警惕滑向 god-node。
3. **可见性 (visibility)**：私有实现（`_` 前缀 / 某主线专属）被跨线摄取 ⇒ 偏 pollution；
   公共 API 被复用 ⇒ 偏 shared-utility / healthy-seam。
经验法则：**先看中心度**——高相对扇入的节点优先归入 shared-utility(低扇出) 或 god-node(高扇出)；
只有**低相对扇入**的私有中间环节被跨线摄取才判 pollution；全为 Sink 即 healthy-seam。

# 输出（严格 JSON，无多余文字）
{"verdict": "healthy-seam" | "shared-utility" | "pollution" | "god-node",
 "description": "<一句中文说明，点明中心度/扇出/谁摄取了什么>"}
"""


@dataclass
class ReviewResult:
    node_id: str
    name: str
    verdict: str
    description: str
    flows: list[str]
    metrics: NodeMetrics | None = None


@dataclass
class ReviewAgent:
    mcp: CodebaseMemoryClient
    llm: LLMClient
    blackboard: Blackboard
    project: str
    rel_fanin_high: float = field(default_factory=lambda: config.rel_fanin_high)
    fanout_high: int = field(default_factory=lambda: config.fanout_high)
    probe: MetricsProbe | None = None

    def __post_init__(self) -> None:
        if self.probe is None:
            self.probe = MetricsProbe(self.mcp, self.project)

    async def review_all(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for crossing in self.blackboard.intersections():
            results.append(await self._review_one(crossing.node_id))
        return results

    async def _review_one(self, node_id: str) -> ReviewResult:
        roles = self.blackboard.roles_for_node(node_id)
        node = self.blackboard.get_node(node_id)
        name = node.name if node else node_id.rsplit(".", 1)[-1]
        flows = [f for f, _ in roles]

        metrics = await self._metrics(node_id)
        hub_class = (
            classify_hub(metrics.rel_fan_in, metrics.fan_out,
                         rel_high=self.rel_fanin_high, fanout_high=self.fanout_high)
            if metrics else ORDINARY
        )
        is_private = _looks_private(name, node_id)

        snippet = await self._snippet(node_id)
        verdict, description = await self._classify(
            name, snippet, roles, metrics, hub_class, is_private
        )

        self.blackboard.record_verdict(node_id, verdict, description)
        return ReviewResult(node_id=node_id, name=name, verdict=verdict,
                            description=description, flows=flows, metrics=metrics)

    async def _metrics(self, node_id: str) -> NodeMetrics | None:
        if self.probe is None:
            return None
        try:
            return await self.probe.node_metrics(node_id)
        except Exception:  # noqa: BLE001 - metrics are best-effort; backstop still runs
            return None

    async def _snippet(self, node_id: str) -> str:
        try:
            from codemap.agents.worker import _as_dict

            snip = _as_dict(await self.mcp.get_code_snippet(self.project, node_id))
            return (snip.get("source") or snip.get("signature") or node_id)[:1200]
        except Exception:  # noqa: BLE001
            return node_id

    # ── Verdict ──────────────────────────────────────────────────────────────
    def _deterministic(self, roles: list[tuple[str, str]], hub_class: str) -> str:
        """Ownership backstop. Centrality is checked *first* so a genuine shared
        primitive becomes a gold hub instead of a false pollution alarm; only a
        low-centrality private intermediate tapped across lines is pollution."""
        if hub_class == GOD_NODE:
            return "god-node"
        if hub_class == SHARED_UTILITY:
            return "shared-utility"
        # Ordinary centrality: a low-fan-in intermediate (processor) tapped by
        # another line is a private-result leak → pollution. All-Sink → seam.
        # (Visibility is fed to the LLM as an extra ownership hint, but the
        # centrality gate above is what kills the V1 shared-utility false alarm.)
        any_processor = any(role == "processor" for _, role in roles)
        return "pollution" if any_processor else "healthy-seam"

    async def _classify(self, name: str, snippet: str, roles: list[tuple[str, str]],
                        metrics: NodeMetrics | None, hub_class: str,
                        is_private: bool) -> tuple[str, str]:
        det_verdict = self._deterministic(roles, hub_class)

        role_lines = "\n".join(f"  - 主线 {flow}: 角色={role}" for flow, role in roles)
        m = metrics
        metric_lines = (
            f"  相对扇入 rel_fan_in={m.rel_fan_in:.4f} (fan_in={m.fan_in}, S={m.system_size})\n"
            f"  绝对扇出 fan_out={m.fan_out}\n"
            f"  结构判定 hub_class={hub_class}\n"
            if m else "  (指标不可用，依据角色与可见性判断)\n"
        )
        window = (
            f"[交叉节点]: {name}\n"
            f"[可见性]: {'私有实现 (_前缀/专属)' if is_private else '公共 API'}\n"
            f"[结构指标]:\n{metric_lines}"
            f"[各主线记录的角色]:\n{role_lines}\n\n"
            f"[节点源码]:\n{snippet}\n\n"
            "[请按四类公民判定该交叉点，并给出中文说明]"
        )
        try:
            reply = await asyncio.to_thread(self.llm.chat, REVIEW_SYSTEM_PROMPT, window)
            data = reply.json()
            verdict = data.get("verdict", det_verdict)
            from codemap.blackboard import VERDICTS

            if verdict not in VERDICTS:
                verdict = det_verdict
            description = data.get("description") or self._default_desc(det_verdict, roles, metrics)
            return verdict, description
        except Exception:  # noqa: BLE001 - fall back to the deterministic rule
            return det_verdict, self._default_desc(det_verdict, roles, metrics)

    @staticmethod
    def _default_desc(verdict: str, roles: list[tuple[str, str]],
                      metrics: NodeMetrics | None) -> str:
        flows = "、".join(f for f, _ in roles)
        m = f"（相对扇入 {metrics.rel_fan_in:.3f} / 扇出 {metrics.fan_out}）" if metrics else ""
        if verdict == "shared-utility":
            return f"主线 {flows} 在此交汇于高复用的公共枢纽{m}，属于已飞升的健康基建。"
        if verdict == "god-node":
            return f"节点 {m} 高扇入又高扇出，疑似伪装成基建的上帝节点，牵一发动全身。"
        if verdict == "pollution":
            return f"主线 {flows} 跨线摄取了某主线的私有中间结果{m}，属于职责污染。"
        return f"主线 {flows} 在此交汇于稳定沉淀点，属于健康接缝。"


def _looks_private(name: str, node_id: str) -> bool:
    """A node reads as a private implementation detail when its function or
    method short name is underscore-prefixed (Python convention)."""
    short = (name or node_id).rsplit(".", 1)[-1]
    return short.startswith("_")
