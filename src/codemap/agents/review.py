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

This yields a five-citizen taxonomy (V2.1):

  healthy-seam        all crossing lines consume it as a stable Sink (settled state).
  shared-utility      high relative fan-in + moderate fan-out → a 已飞升 public hub
                      (金色换乘站); many lines meeting here is *good* architecture.
  lightweight-utility high relative fan-in + ZERO fan-out → a pure pass-through leaf
                      (透传滤镜, format_date); folded, not a heavy station.
  pollution           low centrality intermediate (processor) whose half-processed
                      result is tapped across lines — a private implementation leak.
  god-node            high relative fan-in + high fan-out → infra-disguised mess.

The structural metrics give a deterministic verdict that *also* backs the LLM up
when it is unavailable or unparseable. Crucially, the centrality check fires
before the processor check, so a genuine shared primitive is reclassified as a
gold hub instead of a false pollution alarm — the exact dogfood over-report the
V1 ARCHITECTURE notes flagged.

Orthogonal to the verdict, a crossing is flagged **suspected** when the path that
established it rests on a low-confidence edge — a recovered dynamic dispatch
(discounted ×0.8) or an uncertain LLM call. This is deterministic (driven by the
trace confidences the worker logged) and OR-ed with the LLM's own `is_suspected`,
so a phantom-edge-driven alarm renders as 疑似/待确认 rather than a hard verdict.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from codemap.blackboard import VERDICTS, Blackboard
from codemap.config import config
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient
from codemap.metrics import (
    GOD_NODE,
    LIGHTWEIGHT_UTILITY,
    ORDINARY,
    SHARED_UTILITY,
    MetricsProbe,
    NodeMetrics,
    classify_hub,
)

REVIEW_SYSTEM_PROMPT = """\
你是「代码加工厂 (The Code Factory)」世界观下的**交叉点评审 Agent (Intersection Reviewer)**。
当多条业务主线 (Material Flow) 在某个代码节点物理交汇时，你的任务是：基于传入的上下文，
**分析数据流如何到达此处**，并判定该节点属于哪一类系统公民。

# 五类交叉公民体系 (V2.1)
所有交叉节点必须且只能归入以下五类之一：
1. **[healthy-seam 稳定接缝]**：各交汇主线消费的是它产出的**稳定状态 (Sink)**（如
   `getCurrentUser()` 返回对象，或落库操作），或它是各线**独立调用的公共启动步骤**（如加载
   配置、索引初始化）。这类「原本就该被共享调用的步骤」是健康的接缝。
2. **[shared-utility 公共枢纽 / 已飞升节点]**：被设计为系统级复用的**底层公共原语**（如连接池、
   鉴权中间件、`expand_one` 等机制）。**特征：高相对扇入 + 适度（非零）扇出 + 跨模块被广泛复用**，
   它自身还协调若干内部依赖。多主线在此交汇是极佳的架构状态（金色大换乘站），**绝对不是污染**。
3. **[lightweight-utility 轻量透传滤镜]**：**高相对扇入但零扇出**的纯叶子工具（如 `format_date`、
   `to_json`），只做一次格式转换/透传便返回，不再向下游流动。多线在此交汇是健康的，但它
   **不该被当作独立大站点打断主线**——视作管线上的一枚滤镜即可。
4. **[pollution 危险职责污染]**：某条主线的**私有中间加工结果**，被另一条主线**绕过稳定接口、
   直接伸进处理链路深处摄取**（半成品泄漏）。判定核心不在「被谁调用」，而是
   **「本应消费稳定产物，却越界截取了他人的私有中间步骤」**。
5. **[god-node 上帝节点]**：**高相对扇入 + 高扇出**，伪装成基建的「烂代码中心」或系统严重纠缠点，
   牵一发动全身。

# 数据流分析推演（核心方法：先推演，再下结论）
你会收到本节点的：**1. 各主线到达此处的调用路径；2. 节点的全局 callers 与 callees；
3. 相对扇入/扇出等结构指标与 hub 结构判定；4. 可见性（私有/公共）；
5. 各主线到达本节点的路径置信度 (Confidence)。** 据此回答：
1. **是「公共共享原语」还是「私有半成品泄漏」？**
   - 若各主线**各自从自己的入口独立、浅层地**到达此节点，且该节点被全库广泛调用 ⇒ 偏
     `shared-utility` / `lightweight-utility`（零扇出）/ `healthy-seam`。
   - 若主线 A 在其链路**深处**加工出此节点的结果，主线 B **绕过外部 API 强行伸入摄取** ⇒ `pollution`。
2. **中心度佐证**：高扇入+适度扇出⇒公共枢纽；高扇入+零扇出⇒透传滤镜；高扇入+高扇出⇒上帝节点；
   低扇入+被当作某线私有中间⇒污染。指标仅为佐证，最终以数据流语义为准。
3. **动态调用降级**：若到达本节点的路径置信度较低（<0.9，多为动态推测的补边），
   在输出中标记 `is_suspected=true`——这只是「证据可能不实」的提醒，不改变 verdict 本身。

# 输出格式（严格 JSON，无多余文字或 Markdown 标记块）
{
  "verdict": "healthy-seam" | "shared-utility" | "lightweight-utility" | "pollution" | "god-node",
  "is_suspected": true | false,
  "description": "<限80个中文字符：先陈述各主线经由什么路径到达、是否越界，再结合扇入指标给出定性结论>"
}

# 规则
- `is_suspected`：布尔值。若输入路径含低置信度（<0.9）的动态推测边，或你对判定把握不足，设为 `true`。
- `description`：一句连贯中文，限 80 字，必须先讲数据流证据再下结论。
"""


@dataclass
class Provenance:
    """Data-flow evidence for one crossing: each mainline's traced path to the
    node, the node's direct callers and callees, and per-flow confidence with
    which each mainline reached the node (recovered dynamic edges < 1.0)."""
    callers: list[str]
    callees: list[str]
    paths: dict[str, list[str]]
    confidences: dict[str, float] = field(default_factory=dict)

    def min_confidence(self) -> float:
        """Lowest confidence any involved mainline reached this node with."""
        return min(self.confidences.values()) if self.confidences else 1.0


@dataclass
class ReviewResult:
    node_id: str
    name: str
    verdict: str
    description: str
    flows: list[str]
    metrics: NodeMetrics | None = None
    suspected: bool = False


@dataclass
class ReviewAgent:
    mcp: CodebaseMemoryClient
    llm: LLMClient
    blackboard: Blackboard
    project: str
    rel_fanin_high: float = field(default_factory=lambda: config.rel_fanin_high)
    fanout_high: int = field(default_factory=lambda: config.fanout_high)
    fanin_min: int = field(default_factory=lambda: config.fanin_min)
    suspect_confidence: float = field(default_factory=lambda: config.suspect_confidence)
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
                         rel_high=self.rel_fanin_high, fanout_high=self.fanout_high,
                         fan_in=metrics.fan_in, fanin_min=self.fanin_min)
            if metrics else ORDINARY
        )
        is_private = _looks_private(name, node_id)

        snippet = await self._snippet(node_id)
        provenance = await self._provenance(node_id, [f for f, _ in roles])
        verdict, description, suspected = await self._classify(
            name, snippet, roles, metrics, hub_class, is_private, provenance
        )

        self.blackboard.record_verdict(node_id, verdict, description, suspected=suspected)
        return ReviewResult(node_id=node_id, name=name, verdict=verdict,
                            description=description, flows=flows, metrics=metrics,
                            suspected=suspected)

    async def _metrics(self, node_id: str) -> NodeMetrics | None:
        if self.probe is None:
            return None
        try:
            return await self.probe.node_metrics(node_id)
        except Exception:  # noqa: BLE001 - metrics are best-effort; backstop still runs
            return None

    async def _provenance(self, node_id: str, flows: list[str]) -> "Provenance":
        """Gather data-flow evidence the LLM reasons over: each mainline's traced
        path *to* this node (how the data arrived), plus the node's direct
        callers and callees. This is what tells 'a shared dependency both lines
        independently call' from 'one line tapping another's private chain'."""
        callers = await self._neighbors(node_id, incoming=True)
        callees = await self._neighbors(node_id, incoming=False)
        paths: dict[str, list[str]] = {}
        for flow in flows:
            names: list[str] = []
            for nid in self.blackboard.nodes_for_flow(flow):
                node = self.blackboard.get_node(nid)
                names.append(node.name if node else nid.rsplit(".", 1)[-1])
                if nid == node_id:
                    break
            paths[flow] = names
        # Per-flow confidence the node was reached with (recovered dynamic edges
        # logged at ×0.8) — drives the deterministic `suspected` tier.
        confidences = {f: c for f, c in self.blackboard.confidences_for_node(node_id).items()
                       if f in flows}
        return Provenance(callers=callers, callees=callees, paths=paths,
                          confidences=confidences)

    async def _neighbors(self, node_id: str, *, incoming: bool) -> list[str]:
        """Direct caller (or callee) short-names of a node, with a module tag."""
        pat = (f"(s)-[:CALLS]->(t {{qualified_name:'{node_id}'}})" if incoming
               else f"(t {{qualified_name:'{node_id}'}})-[:CALLS]->(s)")
        cypher = f"MATCH {pat} RETURN DISTINCT s.qualified_name AS qn, s.name AS name LIMIT 12"
        try:
            from codemap.agents.worker import _as_dict

            data = _as_dict(await self.mcp.query_graph(self.project, query=cypher))
            cols = data.get("columns") or []
            rows = [dict(zip(cols, r)) for r in data.get("rows", [])]
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for r in rows:
            qn = r.get("qn") or ""
            short = r.get("name") or qn.rsplit(".", 1)[-1]
            mod = qn.rsplit(".", 2)[-2] if qn.count(".") >= 2 else ""
            out.append(f"{short}({mod})" if mod else short)
        return out

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
        if hub_class == LIGHTWEIGHT_UTILITY:
            return "lightweight-utility"
        # Ordinary centrality: a low-fan-in intermediate (processor) tapped by
        # another line is a private-result leak → pollution. All-Sink → seam.
        # (Visibility is fed to the LLM as an extra ownership hint, but the
        # centrality gate above is what kills the V1 shared-utility false alarm.)
        any_processor = any(role == "processor" for _, role in roles)
        return "pollution" if any_processor else "healthy-seam"

    async def _classify(self, name: str, snippet: str, roles: list[tuple[str, str]],
                        metrics: NodeMetrics | None, hub_class: str,
                        is_private: bool,
                        provenance: "Provenance | None" = None) -> tuple[str, str, bool]:
        det_verdict = self._deterministic(roles, hub_class)
        # Deterministic suspicion (the reliable signal): a crossing reached via a
        # recovered dynamic edge (×0.8) or a low-confidence LLM call. The LLM's own
        # is_suspected is OR-ed on top, but this is what makes it trustworthy.
        min_conf = provenance.min_confidence() if provenance else 1.0
        det_suspected = min_conf < self.suspect_confidence

        role_lines = "\n".join(f"  - 主线 {flow}: 角色={role}" for flow, role in roles)
        m = metrics
        metric_lines = (
            f"  相对扇入 rel_fan_in={m.rel_fan_in:.4f} (fan_in={m.fan_in}, S={m.system_size})\n"
            f"  绝对扇出 fan_out={m.fan_out}\n"
            f"  结构判定 hub_class={hub_class}\n"
            if m else "  (指标不可用，依据数据流与可见性判断)\n"
        )
        prov_lines = ""
        if provenance:
            path_lines = "\n".join(
                f"    - {flow}: {' → '.join(path) or '(空)'}"
                f"  (路径置信度 {provenance.confidences.get(flow, 1.0):.2f})"
                for flow, path in provenance.paths.items()
            )
            prov_lines = (
                "[数据流证据 / Provenance]\n"
                "  各主线追踪到本节点的路径（数据如何抵达此处）:\n"
                f"{path_lines}\n"
                f"  本节点的直接调用者 callers: {', '.join(provenance.callers) or '(无)'}\n"
                f"  本节点的下游 callees: {', '.join(provenance.callees) or '(无)'}\n"
            )
        window = (
            f"[交叉节点]: {name}\n"
            f"[可见性]: {'私有实现 (_前缀/专属)' if is_private else '公共 API'}\n"
            f"{prov_lines}"
            f"[结构指标]:\n{metric_lines}"
            f"[各主线记录的角色]:\n{role_lines}\n\n"
            f"[节点源码]:\n{snippet}\n\n"
            "[请先分析数据流，再按五类公民判定该交叉点，给出中文说明]"
        )
        try:
            reply = await asyncio.to_thread(self.llm.chat, REVIEW_SYSTEM_PROMPT, window)
            data = reply.json()
            verdict = data.get("verdict", det_verdict)
            if verdict not in VERDICTS:
                verdict = det_verdict
            description = data.get("description") or self._default_desc(det_verdict, roles, metrics)
            suspected = det_suspected or bool(data.get("is_suspected", False))
            return verdict, description, suspected
        except Exception as exc:  # noqa: BLE001 - fall back to the deterministic rule
            import os
            if os.getenv("CODEMAP_DEBUG"):
                import traceback
                print(f"[review LLM fallback] {type(exc).__name__}: {exc}")
                traceback.print_exc()
            return det_verdict, self._default_desc(det_verdict, roles, metrics), det_suspected

    @staticmethod
    def _default_desc(verdict: str, roles: list[tuple[str, str]],
                      metrics: NodeMetrics | None) -> str:
        flows = "、".join(f for f, _ in roles)
        m = f"（相对扇入 {metrics.rel_fan_in:.3f} / 扇出 {metrics.fan_out}）" if metrics else ""
        if verdict == "shared-utility":
            return f"主线 {flows} 在此交汇于高复用的公共枢纽{m}，属于已飞升的健康基建。"
        if verdict == "lightweight-utility":
            return f"主线 {flows} 在此交汇于轻量透传滤镜{m}，零扇出的纯工具，折叠呈现即可。"
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
