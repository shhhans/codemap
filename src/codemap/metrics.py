"""Relative fan-in / fan-out metrics for intersection ownership (Milestone 2, V2).

The V1 review heuristic ("any mainline treats it as a processor → dangerous")
over-reports: a node that is *designed* to be shared infrastructure (idempotency
checks, node-expansion primitives) gets flagged as pollution simply because many
lines pass through it. The fix is to measure *whether the node looks like a
public hub or a private intermediate*, using its position in the call graph:

  • **Fan-in**  — how many callers depend on it (absolute in-degree).
  • **Fan-out** — how many callees it depends on (absolute out-degree).

Raw fan-in is not comparable across projects (10 callers means very different
things in a 20-file repo vs a 2000-file one), so we normalize it by system size
with the Concordia relative-concentration formula:

        relative_fan_in = fan_in / (S · ln S)

where ``S`` is the number of structural units (files / classes) in the system.
This yields a scale-free "how central is this node" number that the ReviewAgent
combines with absolute fan-out and visibility to assign ownership:

  high rel-fan-in + low  fan-out → 公共枢纽 / Shared Utility (a 已飞升 hub)
  high rel-fan-in + high fan-out → 上帝节点 / God Node (infra-disguised mess)
  low  rel-fan-in               → an ordinary node; pollution is decided by
                                  whether another line taps its private result.

Pure graph queries; no LLM. The numeric helpers are stdlib-only and unit-tested
offline, while :class:`MetricsProbe` wraps the MCP graph for live measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Ownership verdicts derived purely from the structural metrics. The ReviewAgent
# refines these with source + visibility, but they are meaningful on their own.
SHARED_UTILITY = "shared-utility"   # high centrality, low coupling → 金色枢纽
GOD_NODE = "god-node"               # high centrality, high coupling → 高危
ORDINARY = "ordinary"               # not central enough to be a hub either way


def relative_fan_in(fan_in: int, system_size: int) -> float:
    """Concordia scale-free fan-in concentration: ``fan_in / (S · ln S)``.

    ``system_size`` (S) is the count of structural units (files/classes). For
    S ≤ 1 the denominator is undefined / zero, so we return 0.0 (a one-unit
    system has no meaningful centrality gradient).
    """
    if system_size <= 1 or fan_in <= 0:
        return 0.0
    denom = system_size * math.log(system_size)
    return fan_in / denom if denom > 0 else 0.0


@dataclass(frozen=True)
class NodeMetrics:
    qualified_name: str
    fan_in: int
    fan_out: int
    system_size: int

    @property
    def rel_fan_in(self) -> float:
        return relative_fan_in(self.fan_in, self.system_size)


def classify_hub(
    rel_fan_in: float,
    fan_out: int,
    *,
    rel_high: float,
    fanout_high: int,
    fan_in: int | None = None,
    fanin_min: int = 0,
) -> str:
    """Structural ownership class from fan-in (relative + absolute) and fan-out.

      • fan_in < fanin_min                              → ORDINARY (breadth gate)
      • rel_fan_in ≥ rel_high and fan_out  < fanout_high → SHARED_UTILITY
      • rel_fan_in ≥ rel_high and fan_out ≥ fanout_high → GOD_NODE
      • rel_fan_in <  rel_high                          → ORDINARY

    A hub needs BOTH scale-free centrality *and* absolute breadth. The Concordia
    relative score degenerates on tiny codebases — with S≈2 a node reached by
    just two callers scores >1 — so without an absolute floor every crossing in a
    small repo masquerades as a 'shared utility' (this misfired on the auth/
    billing fixture, gilding the very pollution case it was built to expose). A
    node reached by only a couple of callers is not system-level infrastructure,
    however small the repo makes its relative score.
    """
    if fan_in is not None and fan_in < fanin_min:
        return ORDINARY
    if rel_fan_in >= rel_high:
        return GOD_NODE if fan_out >= fanout_high else SHARED_UTILITY
    return ORDINARY


@dataclass
class MetricsProbe:
    """Measures a node's graph centrality via the Codebase-Memory MCP client.

    System size ``S`` is cached after the first query (it is constant for a
    given indexed project). We count structural units — File and Class nodes —
    and fall back to the total node count if the engine exposes no such labels,
    so the denominator is always sane.
    """

    mcp: Any  # CodebaseMemoryClient (duck-typed for testability)
    project: str
    _system_size: int | None = None

    async def system_size(self) -> int:
        if self._system_size is not None:
            return self._system_size
        units = await self._count("MATCH (n) WHERE n:File OR n:Class RETURN count(n) AS n")
        if units <= 1:  # engine may not label files/classes — fall back to all nodes
            units = await self._count("MATCH (n) RETURN count(n) AS n")
        self._system_size = max(units, 1)
        return self._system_size

    async def fan_in(self, qualified_name: str) -> int:
        return await self._count(
            f"MATCH (t {{qualified_name:'{qualified_name}'}})<-[:CALLS]-(s) RETURN count(s) AS n"
        )

    async def fan_out(self, qualified_name: str) -> int:
        return await self._count(
            f"MATCH (f {{qualified_name:'{qualified_name}'}})-[:CALLS]->(t) RETURN count(t) AS n"
        )

    async def node_metrics(self, qualified_name: str) -> NodeMetrics:
        return NodeMetrics(
            qualified_name=qualified_name,
            fan_in=await self.fan_in(qualified_name),
            fan_out=await self.fan_out(qualified_name),
            system_size=await self.system_size(),
        )

    async def _count(self, cypher: str) -> int:
        """Run a COUNT cypher and pull the single integer out of the result."""
        result = await self.mcp.query_graph(self.project, query=cypher)
        rows = _rows(result)
        if not rows:
            return 0
        row = rows[0]
        raw = row.get("n", next(iter(row.values()), 0)) if isinstance(row, dict) else 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0


def _rows(result: Any) -> list[dict[str, Any]]:
    """Normalize query_graph's {columns, rows} payload into a list of dicts."""
    if isinstance(result, str):
        import json

        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return []
    if not isinstance(result, dict):
        return []
    cols = result.get("columns") or []
    return [dict(zip(cols, row)) for row in result.get("rows", [])]
