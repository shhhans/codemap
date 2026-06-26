"""TaintWorker — a single-mainline DFS taint tracker (Milestone 2).

Given a seed function and a tracked material (Token), the worker walks the call
graph downstream one hop at a time, asks the LLM to classify each downstream
candidate (barrier / noise / continue, plus is_sink), prunes accordingly, and
keeps sliding deeper along the `continue` nodes.

M2 scope: one mainline, no forking — when several candidates survive we follow
them all in a single depth-first walk (concurrency + true Fork is M3). The
worker logs every retained node to the Blackboard, so M2 already populates the
nodes/traces tables that M3 builds on.

The worker is async for the MCP I/O; the (sync) LLM call is offloaded with
asyncio.to_thread so one worker doesn't block the event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from codemap.blackboard import Blackboard, Node, Trace
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient
from codemap.prompts import SYSTEM_PROMPT, Candidate, build_window


@dataclass
class RetainedNode:
    qualified_name: str
    name: str
    role: str  # "processor" | "sink"
    confidence: float
    depth: int
    reason: str
    file_path: str | None = None


@dataclass
class TaintWorker:
    mcp: CodebaseMemoryClient
    llm: LLMClient
    project: str
    flow_type: str
    material: str
    blackboard: Blackboard | None = None
    max_depth: int = 12
    agent_id: str = "worker-0"

    retained: list[RetainedNode] = field(default_factory=list)
    pruned: list[tuple[str, str, str]] = field(default_factory=list)  # (name, verdict, reason)
    _seen: set[str] = field(default_factory=set)

    async def trace(self, seed_qualified_name: str) -> list[RetainedNode]:
        """Run the DFS from `seed_qualified_name` and return the retained path."""
        await self._record(seed_qualified_name, role="source", confidence=1.0, depth=0,
                            reason="seed")
        await self._walk(seed_qualified_name, depth=0)
        return self.retained

    # ── DFS ─────────────────────────────────────────────────────────────────
    async def _walk(self, qualified_name: str, depth: int) -> None:
        if depth >= self.max_depth or qualified_name in self._seen:
            return
        self._seen.add(qualified_name)

        candidates = await self._downstream(qualified_name)
        if not candidates:
            return

        decisions = await self._classify(qualified_name, candidates)

        # Map decisions back to candidates by ordinal or qualified_name.
        by_ref = {c.ref: c for c in candidates}
        by_ord = {str(i): c for i, c in enumerate(candidates, 1)}

        for d in decisions:
            cand = by_ref.get(str(d.get("node"))) or by_ord.get(str(d.get("node")))
            if cand is None:
                continue
            verdict = d.get("verdict", "noise")
            reason = d.get("reason", "")
            if verdict != "continue":
                self.pruned.append((cand.name, verdict, reason))
                continue

            is_sink = bool(d.get("is_sink", False))
            confidence = float(d.get("confidence", 0.5))
            role = "sink" if is_sink else "processor"
            await self._record(cand.ref, role=role, confidence=confidence, depth=depth + 1,
                               reason=reason, name=cand.name, file_path=cand.file_path)
            # A sink is a stable end-state: keep it, but stop descending.
            if not is_sink:
                await self._walk(cand.ref, depth + 1)

    # ── MCP queries ──────────────────────────────────────────────────────────
    async def _downstream(self, qualified_name: str) -> list[Candidate]:
        """1-hop downstream callees + their signatures."""
        short = qualified_name.rsplit(".", 1)[-1]
        result = await self.mcp.trace_path(
            self.project, mode="calls", function_name=short
        )
        callees = _as_dict(result).get("callees", []) or []
        out: list[Candidate] = []
        for c in callees:
            if c.get("hop") not in (1, None):  # immediate downstream only
                continue
            qn = c.get("qualified_name") or c.get("name")
            if not qn or qn == qualified_name:
                continue
            sig = await self._signature(qn)
            out.append(
                Candidate(ref=qn, name=c.get("name", qn), signature=sig.text,
                          file_path=sig.file_path)
            )
        return out

    @dataclass
    class _Sig:
        text: str
        file_path: str | None

    async def _signature(self, qualified_name: str) -> "TaintWorker._Sig":
        try:
            snip = _as_dict(await self.mcp.get_code_snippet(self.project, qualified_name))
        except Exception:  # noqa: BLE001 - a missing snippet shouldn't kill the walk
            return TaintWorker._Sig(text=qualified_name, file_path=None)
        sig = snip.get("signature") or ""
        ret = snip.get("return_type")
        head = f"{snip.get('name', '')}{sig}" + (f" -> {ret}" if ret else "")
        doc = (snip.get("docstring") or "").strip().splitlines()[:1]
        text = head + (f"  # {doc[0]}" if doc else "")
        return TaintWorker._Sig(text=text.strip() or qualified_name,
                                file_path=snip.get("file_path"))

    # ── LLM classification ───────────────────────────────────────────────────
    async def _classify(self, current: str, candidates: list[Candidate]) -> list[dict[str, Any]]:
        window = build_window(
            flow_type=self.flow_type,
            material=self.material,
            current_node=current.rsplit(".", 1)[-1],
            current_file=None,
            candidates=candidates,
        )
        reply = await asyncio.to_thread(self.llm.chat, SYSTEM_PROMPT, window)
        try:
            data = reply.json()
        except Exception:  # noqa: BLE001 - malformed JSON: treat as all-noise, don't crash
            return []
        return data.get("decisions", []) if isinstance(data, dict) else []

    # ── Bookkeeping ──────────────────────────────────────────────────────────
    async def _record(self, qualified_name: str, *, role: str, confidence: float, depth: int,
                       reason: str, name: str | None = None, file_path: str | None = None) -> None:
        node = RetainedNode(
            qualified_name=qualified_name, name=name or qualified_name.rsplit(".", 1)[-1],
            role=role, confidence=confidence, depth=depth, reason=reason, file_path=file_path,
        )
        self.retained.append(node)
        if self.blackboard is not None:
            self.blackboard.upsert_node(
                Node(id=qualified_name, name=node.name,
                     type=role if role in {"source", "sink"} else "processor",
                     file_path=file_path)
            )
            self.blackboard.log_trace(
                Trace(agent_id=self.agent_id, node_id=qualified_name, flow_type=self.flow_type,
                      node_role=role, confidence=confidence, depth=depth)
            )


def _as_dict(payload: Any) -> dict[str, Any]:
    """MCP results may arrive structured (dict) or as a JSON text block."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        import json

        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
