"""IntersectionReviewer — ternary, evidence-based crossing review (V2.1).

When two mainlines check in on the same node, is that a healthy seam, a leaked
intermediate (responsibility pollution), or simply unclear? The old binary rule
("dangerous if any mainline treated the node as a processor") over-alerted: it
flagged *public primitives that are designed to be reused* (e.g. a shared
`expand_one`) as pollution, because they are naturally processors.

V2.1 fixes this by moving the public-vs-private distinction up to a deferred
reviewer with global context, and changing what the deterministic layer does:

  • It no longer judges. It only **assembles evidence** — both the kind that
    points to pollution and the kind that points to healthy reuse — and hands it
    to the LLM, the only judge that can return `dangerous`.
  • Evidence (see `Evidence`):
      - each mainline's *path* to the crossing (reconstructed from parent
        pointers), so we can ask: did the other flow reach this node through a
        stable façade, or jump straight to the inside? (façade-bypass / 越级摄取)
      - the node's *global fan-in* and how many communities its callers span —
        a high, cross-module fan-in is the signature of a public hub (→ healthy).
      - the node's full body + the role each mainline gave it + the weakest
        confidence on the crossing.
  • Ternary verdict: `healthy` / `dangerous` / `suspected`. The LLM may also ask
    for more context (`insufficient_context`); we re-investigate up to
    MAX_RETRIES, then settle on `suspected`. Whenever the LLM is unavailable,
    unparseable, or the crossing is low-confidence, we degrade to `suspected`
    (黄) — never to a hard `dangerous`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from codemap.agents.worker import _as_dict
from codemap.blackboard import Blackboard
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient

# How many times we re-investigate an `insufficient_context` crossing before we
# stop and settle on `suspected` (the termination guard).
MAX_RETRIES = 2
# Cap on façade candidates probed per (owner, other) pair — explosion guard.
MAX_FACADE_PROBES = 12
# Fan-in at/above which a crossed processor reads as a public hub, not a private
# intermediate (a soft prior; the LLM makes the call).
PUBLIC_HUB_FANIN = 5

REVIEW_SYSTEM_PROMPT = """\
你是「代码加工厂」世界观下的**交叉点评审法官 (Intersection Judge)**。
多条业务主线在某个代码节点交汇时，你要判断这次交汇属于以下哪一类。

# 判定标准（关键不是"sink 还是 processor"，而是"归属与可见性"）
- **[健康 healthy]**：多条主线通过**公共工具/稳定接口**正常复用同一个节点。
  典型证据：该节点是某主线的**稳定沉淀点 (sink)** 被他线消费；或它**全局扇入很高、
  调用者横跨多个模块/社区**——是被设计为公共复用的**公共枢纽**，并非某条线的私货。
- **[危险 dangerous]**：一条主线**绕过另一条主线暴露的稳定门面**，直接伸手摄取了它的
  **私有中间结果**（半成品）。典型证据：存在门面 S（A 的某节点，内部 CALLS 到了交叉点 X），
  而主线 B 的到达路径**没有经过 S**，却直接拿到了 X 的内部产物——这是「职责污染 / 越级摄取」，
  上游内部一变下游就崩。
- **[证据不足 insufficient_context]**：现有证据不足以区分"公共枢纽"与"私有摄取"。
  必须返回 `target_node_id`（你想进一步查看的节点）与一句疑问理由。

# 重要原则
- **绕行本身不等于污染**：若被绕行的节点**全局扇入高、跨社区**，它更可能是公共枢纽，判 healthy。
- 只有当证据表明"被摄取的是某主线的**私有半成品**"时才判 dangerous。
- 把握不准就用 insufficient_context，不要硬判。

# 输出（严格 JSON，无多余文字）
{"verdict": "healthy" | "dangerous" | "insufficient_context",
 "description": "<一句中文说明，点明哪条主线摄取了什么 / 为何是公共枢纽>",
 "target_node_id": "<仅 insufficient_context 时必填，否则省略>"}
"""


@dataclass
class Bypass:
    owner_flow: str       # the mainline X is an internal processor of
    facade: str           # owner's node that wraps X (CALLS-reaches it)
    facade_name: str
    bypassing_flow: str   # the mainline that reached X without going through facade


@dataclass
class Evidence:
    node_id: str
    name: str
    roles: list[tuple[str, str]]            # [(flow, role), ...]
    paths: dict[str, list[str]]             # flow -> [seed..X] node_ids
    bypasses: list[Bypass]
    fan_in: int
    min_confidence: float
    body: str
    incomplete: bool = False                # evidence-gathering hit a budget/error

    @property
    def flows(self) -> list[str]:
        return [f for f, _ in self.roles]


@dataclass
class ReviewResult:
    node_id: str
    name: str
    verdict: str                            # 'healthy' | 'dangerous' | 'suspected'
    description: str
    flows: list[str]
    evidence: Evidence | None = None


@dataclass
class ReviewAgent:
    mcp: CodebaseMemoryClient
    llm: LLMClient
    blackboard: Blackboard
    project: str

    async def review_all(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for crossing in self.blackboard.intersections():
            results.append(await self._review_one(crossing.node_id))
        return results

    async def _review_one(self, node_id: str) -> ReviewResult:
        ev = await self._assemble_evidence(node_id)
        verdict, description = await self._judge(ev)
        self.blackboard.record_verdict(node_id, verdict, description)
        return ReviewResult(node_id=node_id, name=ev.name, verdict=verdict,
                            description=description, flows=ev.flows, evidence=ev)

    # ── Evidence assembly (deterministic; never a verdict) ───────────────────
    async def _assemble_evidence(self, node_id: str) -> Evidence:
        roles = self.blackboard.roles_for_node(node_id)
        node = self.blackboard.get_node(node_id)
        name = node.name if node else node_id.rsplit(".", 1)[-1]
        flows = [f for f, _ in roles]

        paths = {f: self.blackboard.path_to_node(node_id, f) for f in flows}
        incomplete = False

        try:
            bypasses = await self._find_bypasses(node_id, roles, paths)
        except Exception:  # noqa: BLE001 - graph hiccup → mark incomplete, don't crash
            bypasses, incomplete = [], True

        try:
            fan_in = await self._fan_in(node_id)
        except Exception:  # noqa: BLE001
            fan_in, incomplete = -1, True

        body = await self._body(node_id)
        return Evidence(
            node_id=node_id, name=name, roles=roles, paths=paths, bypasses=bypasses,
            fan_in=fan_in, min_confidence=self.blackboard.min_confidence(node_id),
            body=body, incomplete=incomplete,
        )

    async def _find_bypasses(self, node_id: str, roles: list[tuple[str, str]],
                             paths: dict[str, list[str]]) -> list[Bypass]:
        """For each mainline that treats X as an internal processor, find a façade
        S (one of that mainline's nodes that CALLS-reaches X) which the *other*
        mainline did not pass through → a candidate 越级摄取 (evidence, not a verdict)."""
        role_of = dict(roles)
        flows = [f for f, _ in roles]
        out: list[Bypass] = []
        for owner in flows:
            if role_of.get(owner) != "processor":
                continue  # X is a stable sink for this flow → consuming it is fine
            owners_nodes = [n for n, _ in self.blackboard.flow_node_roles(owner) if n != node_id]
            found = False
            for s in owners_nodes[:MAX_FACADE_PROBES]:
                if found:
                    break
                if not await self._calls_reach(s, node_id):
                    continue
                # s is one of owner's nodes that wraps X (CALLS-reaches it).
                s_node = self.blackboard.get_node(s)
                s_name = s_node.name if s_node else s.rsplit(".", 1)[-1]
                for other in flows:
                    if other == owner:
                        continue
                    if s not in paths.get(other, []):  # other never went through the façade
                        out.append(Bypass(owner_flow=owner, facade=s, facade_name=s_name,
                                          bypassing_flow=other))
                        found = True  # one façade-bypass per owner is enough evidence
                        break
        return out

    # ── Graph queries (bounded; wrapped by callers) ──────────────────────────
    async def _calls_reach(self, src_qn: str, dst_qn: str) -> bool:
        cypher = (
            f"MATCH (s {{qualified_name:'{src_qn}'}})-[:CALLS*1..3]->"
            f"(x {{qualified_name:'{dst_qn}'}}) RETURN count(*) AS n LIMIT 1"
        )
        rows = _rows(await self.mcp.query_graph(self.project, query=cypher))
        return bool(rows) and int(rows[0].get("n", 0)) > 0

    async def _fan_in(self, node_id: str) -> int:
        cypher = (
            f"MATCH (c)-[:CALLS]->(x {{qualified_name:'{node_id}'}}) "
            "RETURN count(DISTINCT c) AS fan_in"
        )
        rows = _rows(await self.mcp.query_graph(self.project, query=cypher))
        return int(rows[0].get("fan_in", 0)) if rows else 0

    # NOTE: a cross-community fan-in would be a strong public-hub signal, but
    # codebase-memory-mcp v0.8.1 exposes Leiden communities only as get_architecture
    # cluster summaries (member *counts* + 5 representatives) — there is no
    # per-node community property and no full membership list, so an arbitrary
    # node's callers can't be mapped to communities. We therefore lean on raw
    # fan-in alone for the public-hub signal. Revisit if the engine adds a
    # queryable community property.

    async def _body(self, node_id: str) -> str:
        try:
            snip = _as_dict(await self.mcp.get_code_snippet(self.project, node_id))
            return (snip.get("source") or snip.get("signature") or node_id)[:1600]
        except Exception:  # noqa: BLE001
            return node_id

    # ── LLM judgment (ternary, with bounded re-investigation) ────────────────
    async def _judge(self, ev: Evidence) -> tuple[str, str]:
        # Low-confidence crossings (e.g. carried by a recovered dynamic edge) are
        # never hard-judged — straight to 'suspected'.
        if ev.min_confidence < 0.85:
            return "suspected", (
                f"交叉证据置信偏低 (min_confidence={ev.min_confidence:.2f}，可能来自动态补边)，"
                f"主线 {('、'.join(ev.flows))} 在此交汇，降级为疑似/待确认。"
            )

        extra = ""
        for attempt in range(MAX_RETRIES + 1):
            window = _build_evidence_window(ev, extra)
            try:
                reply = await asyncio.to_thread(self.llm.chat, REVIEW_SYSTEM_PROMPT, window)
                data = reply.json()
            except Exception:  # noqa: BLE001 - LLM down / unparseable → honest 'suspected'
                return "suspected", self._suspected_desc(ev)

            verdict = data.get("verdict")
            description = data.get("description") or self._suspected_desc(ev)
            if verdict in {"healthy", "dangerous"}:
                return verdict, description
            if verdict == "insufficient_context" and attempt < MAX_RETRIES:
                target = data.get("target_node_id") or ev.node_id
                extra = await self._more_context(target)
                continue
            # Unknown verdict, or out of retries: settle on suspected.
            return "suspected", description

        return "suspected", self._suspected_desc(ev)

    async def _more_context(self, target: str) -> str:
        """Fetch extra evidence for a re-investigation round: the target's body
        and its global fan-in."""
        body = await self._body(target)
        try:
            fan_in = await self._fan_in(target)
        except Exception:  # noqa: BLE001
            fan_in = -1
        return (f"\n[补充侦查 · 节点 {target.rsplit('.', 1)[-1]}] 全局扇入={fan_in}\n"
                f"[其源码]:\n{body}\n")

    def _suspected_desc(self, ev: Evidence) -> str:
        return (f"主线 {('、'.join(ev.flows))} 在 {ev.name} 交汇；"
                f"证据{'不完整' if ev.incomplete else '不足以'}区分公共枢纽与私有摄取，标记为疑似/待确认。")


# ── helpers ──────────────────────────────────────────────────────────────────
def _rows(result: Any) -> list[dict[str, Any]]:
    data = _as_dict(result)
    cols = data.get("columns") or []
    return [dict(zip(cols, row)) for row in data.get("rows", [])]


def _build_evidence_window(ev: Evidence, extra: str = "") -> str:
    role_lines = "\n".join(f"  - 主线 {f}: 角色={r}" for f, r in ev.roles)
    path_lines = []
    for f, p in ev.paths.items():
        short = " → ".join(n.rsplit(".", 1)[-1] for n in p) or "(未记录)"
        path_lines.append(f"  - 主线 {f} 的到达路径: {short}")
    if ev.bypasses:
        by_lines = "\n".join(
            f"  - ⚠ 主线 {b.bypassing_flow} 绕过了主线 {b.owner_flow} 的门面 "
            f"{b.facade_name}()（该门面内部 CALLS 到了本节点），直接摄取了内部结果"
            for b in ev.bypasses
        )
    else:
        by_lines = "  - （未发现门面绕行；各主线均经正常路径到达）"

    fanin = "未知" if ev.fan_in < 0 else str(ev.fan_in)
    hub_hint = ""
    if ev.fan_in >= PUBLIC_HUB_FANIN:
        hub_hint = f"（扇入≥{PUBLIC_HUB_FANIN}，强烈指向公共枢纽 → healthy）"

    lines = [
        f"[交叉节点]: {ev.name}  ({ev.node_id})",
        "[各主线记录的角色]:",
        role_lines,
        "[各主线到达本节点的路径]:",
        *path_lines,
        "[门面绕行证据 (指向 dangerous)]:",
        by_lines,
        f"[全局扇入 (指向 healthy/公共枢纽)]: {fanin} {hub_hint}",
        f"[本交叉最低置信度]: {ev.min_confidence:.2f}",
        "[节点源码]:",
        ev.body,
    ]
    if extra:
        lines.append(extra)
    lines.append("[请综合以上证据，判定 healthy / dangerous / insufficient_context 并给出中文说明]")
    return "\n".join(lines)
