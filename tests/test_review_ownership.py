"""M3 tests — four-citizen ownership verdicts via the deterministic backstop.

The headline case is the dogfood false alarm the V1 ARCHITECTURE flagged: a node
*designed* to be shared (high relative fan-in, low fan-out) must come back as a
golden **shared-utility**, not pollution — even though some mainline recorded it
as a processor. We drive the ReviewAgent with fakes (LLM forced to fall back to
the structural rule, metrics injected) so the backstop logic is tested in
isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from codemap.agents.review import ReviewAgent
from codemap.blackboard import Blackboard, Node, Trace
from codemap.metrics import NodeMetrics


class FakeMCP:
    async def get_code_snippet(self, project: str, qualified_name: str, **_: Any) -> dict:
        return {"source": f"def {qualified_name.rsplit('.',1)[-1]}(): ...", "name": qualified_name}


class FallbackLLM:
    """Always raises so the agent uses its deterministic structural verdict."""

    def chat(self, system: str, window: str) -> Any:
        raise RuntimeError("LLM offline — exercise the backstop")


class FakeProbe:
    def __init__(self, metrics: dict[str, NodeMetrics]) -> None:
        self._m = metrics

    async def node_metrics(self, node_id: str) -> NodeMetrics:
        return self._m[node_id]


@pytest.fixture()
def board(tmp_path: Path) -> Blackboard:
    bb = Blackboard(tmp_path / "bb.sqlite")
    bb.register_mainline("line_auth", "auth", "Auth", "#1E90FF")
    bb.register_mainline("line_billing", "billing", "Billing", "#F4A261")

    def crossing(node_id: str, name: str, role_a: str, role_b: str) -> None:
        bb.upsert_node(Node(id=node_id, name=name, type="processor"))
        bb.log_trace(Trace("auth", node_id, "auth", node_role=role_a, depth=2))
        bb.log_trace(Trace("billing", node_id, "billing", node_role=role_b, depth=1))

    # Genuine pollution: private intermediate, low centrality.
    crossing("m.parse_jwt", "parse_jwt", "processor", "processor")
    # Shared utility: a primitive both lines reuse — processor role, but central.
    crossing("m.expand", "expand_one", "processor", "processor")
    # God node: central AND highly coupled.
    crossing("m.god", "do_everything", "processor", "processor")
    # Stable seam: a sink for both.
    crossing("m.get_user", "get_current_user", "sink", "sink")
    yield bb
    bb.close()


def _verdicts(board: Blackboard) -> dict[str, str]:
    metrics = {
        # low relative fan-in → ordinary → pollution
        "m.parse_jwt": NodeMetrics("m.parse_jwt", fan_in=2, fan_out=1, system_size=300),
        # high relative fan-in, low fan-out → shared-utility (the fix)
        "m.expand": NodeMetrics("m.expand", fan_in=40, fan_out=2, system_size=30),
        # high relative fan-in, high fan-out → god-node
        "m.god": NodeMetrics("m.god", fan_in=40, fan_out=20, system_size=30),
        # all-sink → healthy-seam regardless of metrics
        "m.get_user": NodeMetrics("m.get_user", fan_in=5, fan_out=0, system_size=300),
    }
    agent = ReviewAgent(
        mcp=FakeMCP(), llm=FallbackLLM(), blackboard=board, project="p",
        probe=FakeProbe(metrics),
    )
    results = asyncio.run(agent.review_all())
    return {r.node_id: r.verdict for r in results}


def test_shared_utility_not_flagged_as_pollution(board: Blackboard) -> None:
    # The V1 over-report: a central, low-coupling reused primitive. Even though
    # both lines recorded it as a processor, high relative fan-in reclassifies it.
    assert _verdicts(board)["m.expand"] == "shared-utility"


def test_genuine_private_intermediate_is_pollution(board: Blackboard) -> None:
    assert _verdicts(board)["m.parse_jwt"] == "pollution"


def test_high_fanin_high_fanout_is_god_node(board: Blackboard) -> None:
    assert _verdicts(board)["m.god"] == "god-node"


def test_all_sink_crossing_is_healthy_seam(board: Blackboard) -> None:
    assert _verdicts(board)["m.get_user"] == "healthy-seam"


def test_verdicts_persist_to_blackboard(board: Blackboard) -> None:
    _verdicts(board)
    from codemap.export import export_subway_map

    data = export_subway_map(board.db_path)
    by_node = {i["node_id"]: i["type"] for i in data["intersections"]}
    assert by_node["m.expand"] == "shared-utility"
    assert by_node["m.parse_jwt"] == "pollution"
