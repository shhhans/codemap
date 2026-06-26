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
from codemap.filtering import (
    StubModules,
    classify_boundary,
    is_global_noise,
    parse_stub_modules,
    stub_call_names,
)
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
    skipped: int = 0  # candidates the static fast-path resolved without the LLM
    _seen: set[str] = field(default_factory=set)
    # Per-module imported-module cache + the project's Module qualified-names
    # (resolved once). Imports live at file scope, not in a node's body, so
    # stubbing reads the file's Module node, found by longest-prefix match.
    _file_stubs: dict[str, StubModules] = field(default_factory=dict)
    _module_qns: list[str] | None = None
    _internal_segs: set[str] | None = None

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

        # Fetch the node's snippet once (body source + file path), then resolve
        # the file's imports for stubbing. Imports are file-scoped, so stubbing
        # reads the file's Module node — not this node's body.
        snip = await self._node_snippet(qualified_name)
        source = snip.get("source") or ""
        stubs = await self._file_imports(qualified_name)
        candidates = await self._downstream(qualified_name, source, stubs)
        if not candidates:
            return []

        # M1 two-net partition: third-party stub boundaries skip the LLM entirely
        # (严禁展开 AST) and are classified Sink/Dual purely structurally; only the
        # remaining repo-internal candidates cost an LLM round-trip.
        normal: list[Candidate] = []
        children: list[tuple[str, int]] = []
        for c in candidates:
            if stubs.is_stub_call(c.ref) or stubs.is_stub_call(c.name):
                # Stub boundary takes precedence: a qualified external call like
                # `requests.get` must not be dropped just because its short name
                # (`get`) collides with the global blacklist.
                children.extend(await self._handle_boundary(c, depth))
            elif is_global_noise(c.name):
                self.pruned.append((c.name, "noise", "global relation blacklist"))
            else:
                normal.append(c)
        if not normal:
            return children

        # Static fast-path: a candidate this flow has already checked in on (or
        # the node itself) needs no LLM — the idempotent dedup would discard its
        # verdict anyway, so classifying it just burns tokens/reasoning. Dropping
        # it from the window is a free, correctness-preserving reduction.
        ask: list[Candidate] = []
        for c in normal:
            if c.ref == qualified_name or self._already_seen(c.ref):
                self.skipped += 1
            else:
                ask.append(c)
        if not ask:
            return children

        # Annotate each surviving candidate with its out-degree (one batched
        # query) so the model can resolve leaves (扇出=0) as sinks without
        # deliberating about descent — the "扇出为0必是 Sink" structural signal.
        fan_outs = await self._out_edge_counts([c.ref for c in ask])
        for c in ask:
            c.fan_out = fan_outs.get(c.ref, 0)

        decisions = await self._classify(qualified_name, ask)
        by_ref = {c.ref: c for c in ask}
        by_ord = {str(i): c for i, c in enumerate(ask, 1)}

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
    async def _node_snippet(self, qualified_name: str) -> dict[str, Any]:
        """Fetch the node's snippet dict (body source + file_path; {} if absent)."""
        try:
            return _as_dict(await self.mcp.get_code_snippet(self.project, qualified_name))
        except Exception:  # noqa: BLE001 - a missing snippet shouldn't kill the walk
            return {}

    async def _node_source(self, qualified_name: str) -> str:
        """Fetch the node's body source (empty string if unavailable)."""
        return (await self._node_snippet(qualified_name)).get("source") or ""

    async def _module_qualified_names(self) -> list[str]:
        """All Module qualified-names in the project, longest first (cached).

        Module nodes don't carry a usable file_path property, so we match a
        node to its file by longest-prefix over these instead."""
        if self._module_qns is None:
            try:
                rows = self._rows(await self.mcp.query_graph(
                    self.project, query="MATCH (m:Module) RETURN m.qualified_name AS qn"))
                self._module_qns = sorted(
                    (r.get("qn") for r in rows if r.get("qn")), key=len, reverse=True)
            except Exception:  # noqa: BLE001
                self._module_qns = []
        return self._module_qns

    async def _file_imports(self, qualified_name: str) -> StubModules:
        """Imported modules of the file containing ``qualified_name`` (cached).

        Imports sit at file scope, not in a node's body, so we resolve the
        node's Module (the longest Module qualified-name that prefixes it) and
        parse *its* source. The graph indexes only repo-internal symbols
        (verified by the M1 probe), so this is the only place third-party calls
        are observable — it lets stubbing guard the source-regex recovery
        against phantom edges on library method names.
        """
        module_qn = next(
            (m for m in await self._module_qualified_names()
             if qualified_name == m or qualified_name.startswith(m + ".")),
            None,
        )
        if not module_qn:
            return StubModules()
        if module_qn in self._file_stubs:
            return self._file_stubs[module_qn]
        stubs = StubModules()
        try:
            msnip = _as_dict(await self.mcp.get_code_snippet(self.project, module_qn))
            parsed = parse_stub_modules(msnip.get("source") or "")
            # Drop the project's own packages: they import like libraries but
            # resolve to internal, traceable nodes.
            stubs = parsed.externals_only(await self._internal_roots())
        except Exception:  # noqa: BLE001 - stubbing is best-effort
            stubs = StubModules()
        self._file_stubs[module_qn] = stubs
        return stubs

    async def _internal_roots(self) -> set[str]:
        """Top-level package/module segments that exist in the indexed graph —
        i.e. the project's own (first-party) import roots (cached)."""
        if self._internal_segs is None:
            segs: set[str] = set()
            for m in await self._module_qualified_names():
                segs.update(m.split("."))
            self._internal_segs = segs
        return self._internal_segs

    async def _downstream(self, qualified_name: str, source: str = "",
                          stubs: StubModules | None = None) -> list[Candidate]:
        """1-hop downstream callees + their signatures.

        Uses query_graph keyed on the exact qualified_name (not trace_path's
        short name), so callees are resolved precisely even when the repo has
        several same-named functions — otherwise distinct mainlines collide on
        a shared short name and produce phantom intersections.

        Returns *all* callees; the global blacklist and third-party stub nets are
        applied by ``expand_one`` (stubs must win over the blacklist so a
        qualified `requests.get` isn't dropped on its short name `get`).
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
            short = row.get("name") or qn.rsplit(".", 1)[-1]
            seen.add(qn)
            sig = await self._signature(qn)
            out.append(Candidate(ref=qn, name=short,
                                 signature=sig.text, file_path=sig.file_path))

        out.extend(await self._recover_dynamic(qualified_name, seen, source, stubs))
        return out

    # ── M1 boundary handling (third-party stubs) ─────────────────────────────
    async def _handle_boundary(self, cand: Candidate, depth: int) -> list[tuple[str, int]]:
        """Classify a stubbed third-party call Sink vs Dual from its edges alone
        (no AST expansion, no LLM). Dual boundaries keep flowing material onward
        and are returned as children; Sinks are recorded and terminate."""
        kind = classify_boundary(await self._out_edge_count(cand.ref))
        role = "sink" if kind == "sink" else "processor"
        is_new = await self._record(
            cand.ref, role=role, confidence=0.7, depth=depth + 1,
            reason=f"third-party stub → {kind}", name=cand.name, file_path=cand.file_path,
        )
        return [(cand.ref, depth + 1)] if (kind == "dual" and is_new) else []

    async def _out_edge_count(self, qualified_name: str) -> int:
        """Number of outgoing CALLS edges from a node (0 if absent from graph)."""
        cypher = (
            f"MATCH (f {{qualified_name:'{qualified_name}'}})-[:CALLS]->(t) "
            "RETURN count(t) AS n"
        )
        rows = self._rows(await self.mcp.query_graph(self.project, query=cypher))
        if not rows:
            return 0
        raw = rows[0].get("n", rows[0].get("count(t)", 0))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _already_seen(self, ref: str) -> bool:
        """Has this flow already checked in on `ref`? Authoritative dedup still
        lives in `_record`; this is the best-effort fast-path gate."""
        if self.blackboard is not None:
            return self.blackboard.has_visited(ref, self.flow_type)
        return ref in self._seen

    async def _out_edge_counts(self, refs: list[str]) -> dict[str, int]:
        """Out-degree (CALLS) for several nodes in one query — refs missing from
        the result are leaves (0). Best-effort: a query failure just leaves the
        fan-out hint off (callers default to 0)."""
        if not refs:
            return {}
        in_list = ", ".join("'" + r.replace("'", "") + "'" for r in refs)
        cypher = (
            f"MATCH (f)-[:CALLS]->(t) WHERE f.qualified_name IN [{in_list}] "
            "RETURN f.qualified_name AS qn, count(t) AS n"
        )
        try:
            rows = self._rows(await self.mcp.query_graph(self.project, query=cypher))
        except Exception:  # noqa: BLE001 - fan-out hint is optional
            return {}
        out: dict[str, int] = {}
        for r in rows:
            qn = r.get("qn")
            if qn is None:
                continue
            try:
                out[qn] = int(r.get("n", 0))
            except (TypeError, ValueError):
                out[qn] = 0
        return out

    async def _recover_dynamic(self, qualified_name: str, already: set[str],
                               source: str = "", stubs: StubModules | None = None) -> list[Candidate]:
        """Recover dynamic-dispatch callees the static graph dropped.

        The static call graph cannot resolve calls like ``worker.expand_one()``
        when the receiver's type is unknown (e.g. it came out of a dict). We
        read the node's source, pull the called names, and for any name that
        maps to exactly one Function/Method in the graph (so it's unambiguous),
        add it as a recovered, lower-confidence candidate.

        Third-party method calls (``np.dot(...)``) are excluded via import-aware
        stubbing: their bare attr name (``dot``) must not be name-matched to a
        coincidentally unique internal ``dot()`` — that would be a phantom edge.
        """
        import re

        if not source:
            source = await self._node_source(qualified_name)
        if not source:
            return []
        own = qualified_name.rsplit(".", 1)[-1]
        # File-level imports (from expand_one) tell us which call sites in this
        # body are third-party (e.g. `np.dot`) so we don't name-match their bare
        # attr to a coincidentally-unique internal node.
        stub_names = stub_call_names(source, stubs or StubModules())
        names = {
            n for n in re.findall(r"([A-Za-z_]\w*)\s*\(", source)
            if n != own and not is_global_noise(n) and not n.startswith("__")
            and n not in stub_names
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
        except Exception as exc:  # noqa: BLE001 - malformed JSON: treat as all-noise, don't crash
            # Most often a truncated answer (completion hit max_tokens on a wide,
            # high-fan-out node). Silent before; gate a diagnostic on CODEMAP_DEBUG
            # so the empty expansion isn't a mystery (this is what hid the dogfood
            # _run no-children case until the meter showed completion == the cap).
            import os
            if os.getenv("CODEMAP_DEBUG"):
                ct = (reply.usage or {}).get("completion_tokens")
                print(f"[worker _classify unparsable] {current.rsplit('.',1)[-1]}: "
                      f"{type(exc).__name__} (completion_tokens={ct}, "
                      f"candidates={len(candidates)})")
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
