"""ReviewAgent — characterizes intersections as healthy or dangerous (Milestone 3).

When two mainlines check in on the same node, is that a healthy seam or
responsibility pollution? The rule (from the architecture):

  • The node is a **stable sink** for the mainline that established it, and the
    other mainline consumes that settled state → **healthy crossing**.
  • The node is an **intermediate processing step** of one mainline, and another
    mainline reaches in to consume its half-processed result → **dangerous
    crossing / responsibility pollution**.

We feed the LLM the node's source plus the role each mainline recorded for it
(sink vs processor) and ask for a verdict + a Chinese explanation for the map. A
deterministic rule (dangerous if any mainline treated the node as an
intermediate processor) backs the LLM up if it is unavailable or unparseable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from codemap.blackboard import Blackboard
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient

REVIEW_SYSTEM_PROMPT = """\
你是「代码加工厂」世界观下的**交叉点评审 Agent (Intersection Reviewer)**。
多条业务主线在某个代码节点交汇时，你判断这次交汇是否健康。

# 判定标准
- **[健康交叉 healthy]**：该节点是某条主线的**稳定沉淀点 (Sink / 稳定状态)**
  （如 `getCurrentUser()` 返回的稳定 User 对象），其他主线**消费的是这份已沉淀的稳定状态**。
  这是特性之间合理的接缝。
- **[危险交叉 dangerous]**：该节点是某条主线的**中间加工环节 (intermediate)**
  （如 `parseJWT()` 返回的半成品 claims），另一条主线**绕过稳定接口、直接摄取了这份中间结果**。
  这是「职责污染」——上游内部实现一变，下游就会被波及，应重构为依赖稳定状态。

# 输入
你会看到该交叉节点的源码，以及每条主线为它记录的角色 (sink=沉淀点 / processor=中间环节)。
经验法则：只要**有任意一条主线把它当作中间加工环节 (processor)**，而它又被多线交叉，
通常即为 dangerous；若**所有交汇主线都把它当作稳定沉淀点 (sink)**，则为 healthy。

# 输出（严格 JSON，无多余文字）
{"verdict": "healthy" | "dangerous", "description": "<一句中文说明，点明哪条主线摄取了什么>"}
"""


@dataclass
class ReviewResult:
    node_id: str
    name: str
    verdict: str
    description: str
    flows: list[str]


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
        roles = self.blackboard.roles_for_node(node_id)
        node = self.blackboard.get_node(node_id)
        name = node.name if node else node_id.rsplit(".", 1)[-1]
        flows = [f for f, _ in roles]

        snippet = await self._snippet(node_id)
        verdict, description = await self._classify(name, snippet, roles)

        # Persist for the map exporter.
        self.blackboard.record_verdict(node_id, verdict, description)
        return ReviewResult(node_id=node_id, name=name, verdict=verdict,
                            description=description, flows=flows)

    async def _snippet(self, node_id: str) -> str:
        try:
            from codemap.agents.worker import _as_dict

            snip = _as_dict(await self.mcp.get_code_snippet(self.project, node_id))
            return (snip.get("source") or snip.get("signature") or node_id)[:1200]
        except Exception:  # noqa: BLE001
            return node_id

    async def _classify(self, name: str, snippet: str,
                        roles: list[tuple[str, str]]) -> tuple[str, str]:
        # Deterministic fallback / prior: pollution if any flow treats it as an
        # intermediate processing step.
        any_processor = any(role == "processor" for _, role in roles)
        det_verdict = "dangerous" if any_processor else "healthy"

        role_lines = "\n".join(f"  - 主线 {flow}: 角色={role}" for flow, role in roles)
        window = (
            f"[交叉节点]: {name}\n"
            f"[各主线记录的角色]:\n{role_lines}\n\n"
            f"[节点源码]:\n{snippet}\n\n"
            "[请判定该交叉点是 healthy 还是 dangerous，并给出中文说明]"
        )
        try:
            reply = await asyncio.to_thread(self.llm.chat, REVIEW_SYSTEM_PROMPT, window)
            data = reply.json()
            verdict = data.get("verdict", det_verdict)
            if verdict not in {"healthy", "dangerous"}:
                verdict = det_verdict
            description = data.get("description") or self._default_desc(det_verdict, roles)
            return verdict, description
        except Exception:  # noqa: BLE001 - fall back to the deterministic rule
            return det_verdict, self._default_desc(det_verdict, roles)

    @staticmethod
    def _default_desc(verdict: str, roles: list[tuple[str, str]]) -> str:
        flows = "、".join(f for f, _ in roles)
        if verdict == "dangerous":
            return f"主线 {flows} 在此交汇，且其中存在对中间加工结果的摄取，疑似职责污染。"
        return f"主线 {flows} 在此交汇于稳定沉淀点，属于健康交叉。"
