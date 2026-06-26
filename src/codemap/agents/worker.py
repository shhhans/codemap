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

    # ── DFS (M2: single worker recurses through its own children) ────────────
    async def _walk(self, qualified_name: str, depth: int) -> None:
        for child, child_depth in await self.expand_one(qualified_name, depth):
            await self._walk(child, child_depth)

    async def record_source(self, qualified_name: str, name: str | None = None,
                            file_path: str | None = None) -> None:
        """Record the seed node as this mainline's Source (depth 0)."""
        await self._record(qualified_name, role="source", confidence=1.0, depth=0,
                           reason="seed", name=name, file_path=file_path)

    # ── Single-node expansion (shared by M2 recursion and M3 coordinator) ────
    async def expand_one(self, qualified_name: str, depth: int) -> list[tuple[str, int]]:
        """Expand one node by exactly one hop: fetch downstream, let the LLM
        prune, record survivors to the Blackboard, and return the children that
        should be explored further as ``(qualified_name, depth)`` pairs.

        Sinks are recorded but not returned (stable end-state). A node already
        visited by this flow returns no children (dedup / cycle break). This is
        the unit of work the M3 Coordinator schedules concurrently — each
        returned child becomes an independent frontier item (a Fork).
        """
        if depth >= self.max_depth:
            return []
        candidates = await self._downstream(qualified_name)
        if not candidates:
            return []

        decisions = await self._classify(qualified_name, candidates)
        by_ref = {c.ref: c for c in candidates}
        by_ord = {str(i): c for i, c in enumerate(candidates, 1)}

        children: list[tuple[str, int]] = []
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
            if cand.recovered:  # name-matched dynamic edge: discount certainty
                confidence *= 0.8
            role = "sink" if is_sink else "processor"
            is_new = await self._record(
                cand.ref, role=role, confidence=confidence, depth=depth + 1,
                reason=reason, name=cand.name, file_path=cand.file_path,
            )
            # Descend only into fresh, non-sink nodes.
            if is_new and not is_sink:
                children.append((cand.ref, depth + 1))
        return children

    # ── MCP queries ──────────────────────────────────────────────────────────
    async def _downstream(self, qualified_name: str) -> list[Candidate]:
        """1-hop downstream callees + their signatures.

        Uses query_graph keyed on the exact qualified_name (not trace_path's
        short name), so callees are resolved precisely even when the repo has
        several same-named functions — otherwise distinct mainlines collide on
        a shared short name and produce phantom intersections.
        """
        cypher = (
            f"MATCH (f {{qualified_name:'{qualified_name}'}})-[:CALLS]->(t) "
            "RETURN DISTINCT t.qualified_name AS qn, t.name AS name"
        )
        rows = self._rows(await self.mcp.query_graph(self.project, query=cypher))
        out: list[Candidate] = []
        seen: set[str] = set()
        for row in rows:
            qn = row.get("qn") or row.get("name")
            if not qn or qn == qualified_name or qn in seen:
                continue
            seen.add(qn)
            sig = await self._signature(qn)
            out.append(Candidate(ref=qn, name=row.get("name") or qn.rsplit(".", 1)[-1],
                                 signature=sig.text, file_path=sig.file_path))

        out.extend(await self._recover_dynamic(qualified_name, seen))
        return out

    # Names that look like calls in source but are never mainline nodes.
    _CALL_NOISE = frozenset({
        "if", "for", "while", "return", "print", "len", "range", "int", "str",
        "float", "bool", "dict", "list", "set", "tuple", "super", "isinstance",
        "getattr", "setattr", "hasattr", "enumerate", "zip", "max", "min", "sum",
        "open", "type", "format", "join", "append", "get", "put_nowait", "add",
        "split", "strip", "items", "keys", "values", "and", "or", "not",
    })

    async def _recover_dynamic(self, qualified_name: str, already: set[str]) -> list[Candidate]:
        """Recover dynamic-dispatch callees the static graph dropped.

        The static call graph cannot resolve calls like ``worker.expand_one()``
        when the receiver's type is unknown (e.g. it came out of a dict). We
        read the node's source, pull the called names, and for any name that
        maps to exactly one Function/Method in the graph (so it's unambiguous),
        add it as a recovered, lower-confidence candidate.
        """
        import re

        snip = _as_dict(await self.mcp.get_code_snippet(self.project, qualified_name))
        source = snip.get("source") or ""
        if not source:
            return []
        own = qualified_name.rsplit(".", 1)[-1]
        names = {
            n for n in re.findall(r"([A-Za-z_]\w*)\s*\(", source)
            if n != own and n not in self._CALL_NOISE and not n.startswith("__")
        }
        out: list[Candidate] = []
        for name in names:
            cypher = (
                f"MATCH (t) WHERE t.name = '{name}' AND (t:Function OR t:Method) "
                "RETURN DISTINCT t.qualified_name AS qn"
            )
            rows = self._rows(await self.mcp.query_graph(self.project, query=cypher))
            # Only unambiguous (unique-name) matches, and not already an edge.
            if len(rows) != 1:
                continue
            qn = rows[0].get("qn")
            if not qn or qn == qualified_name or qn in already:
                continue
            already.add(qn)
            sig = await self._signature(qn)
            out.append(Candidate(ref=qn, name=name,
                                 signature=f"⟨动态调用⟩ {sig.text}", file_path=sig.file_path,
                                 recovered=True))
        return out

    @staticmethod
    def _rows(result: Any) -> list[dict[str, Any]]:
        """Turn query_graph's {columns, rows} payload into a list of dicts."""
        data = _as_dict(result)
        cols = data.get("columns") or []
        return [dict(zip(cols, row)) for row in data.get("rows", [])]

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
                       reason: str, name: str | None = None,
                       file_path: str | None = None) -> bool:
        """Record a retained node. Returns True if this flow had not visited it
        before (the shared Blackboard is the source of truth when present, so
        concurrent workers on the same flow dedup correctly)."""
        node = RetainedNode(
            qualified_name=qualified_name, name=name or qualified_name.rsplit(".", 1)[-1],
            role=role, confidence=confidence, depth=depth, reason=reason, file_path=file_path,
        )
        self.retained.append(node)
        if self.blackboard is None:
            is_new = qualified_name not in self._seen
            self._seen.add(qualified_name)
            return is_new

        self.blackboard.upsert_node(
            Node(id=qualified_name, name=node.name,
                 type=role if role in {"source", "sink"} else "processor",
                 file_path=file_path)
        )
        # log_trace is idempotent on (node, flow) and returns False on a repeat,
        # which is exactly the per-flow visited check.
        return self.blackboard.log_trace(
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
