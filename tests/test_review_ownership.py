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

    async def query_graph(self, project: str, query: str, **_: Any) -> dict:
        # Provenance neighbor lookups; empty is fine for these tests.
        return {"columns": [], "rows": []}


class FallbackLLM:
    """Always raises so the agent uses its deterministic structural verdict."""

    def chat(self, system: str, window: str) -> Any:
        raise RuntimeError("LLM offline — exercise the backstop")


class StubLLM:
    """Returns a fixed four-state verdict, so we can prove the LLM's answer is
    actually used (not silently swallowed onto the deterministic backstop)."""

    def __init__(self, verdict: str, description: str = "llm verdict",
                 is_suspected: bool = False) -> None:
        self._v, self._d, self._s = verdict, description, is_suspected

    def chat(self, system: str, window: str) -> Any:
        v, d, s = self._v, self._d, self._s

        class _Reply:
            def json(self_inner) -> dict:
                return {"verdict": v, "description": d, "is_suspected": s}

        return _Reply()


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
    # Lightweight pass-through: central but ZERO fan-out (a leaf formatter).
    crossing("m.fmt", "format_date", "processor", "processor")
    # Stable seam: a sink for both.
    crossing("m.get_user", "get_current_user", "sink", "sink")
    # Suspected: billing reached this node via a recovered dynamic edge (×0.8),
    # so the crossing rests on a low-confidence edge — verdict pollution, but flagged.
    bb.upsert_node(Node(id="m.dyn", name="dynamic_hop", type="processor"))
    bb.log_trace(Trace("auth", "m.dyn", "auth", node_role="processor", depth=2))
    bb.log_trace(Trace("billing", "m.dyn", "billing", node_role="processor",
                       confidence=0.72, depth=1))
    yield bb
    bb.close()


def _results(board: Blackboard, llm: Any = None) -> dict[str, Any]:
    metrics = {
        # low relative fan-in → ordinary → pollution
        "m.parse_jwt": NodeMetrics("m.parse_jwt", fan_in=2, fan_out=1, system_size=300),
        # high relative fan-in, low fan-out → shared-utility (the fix)
        "m.expand": NodeMetrics("m.expand", fan_in=40, fan_out=2, system_size=30),
        # high relative fan-in, high fan-out → god-node
        "m.god": NodeMetrics("m.god", fan_in=40, fan_out=20, system_size=30),
        # high relative fan-in, ZERO fan-out → lightweight-utility (透传滤镜)
        "m.fmt": NodeMetrics("m.fmt", fan_in=40, fan_out=0, system_size=30),
        # all-sink → healthy-seam regardless of metrics
        "m.get_user": NodeMetrics("m.get_user", fan_in=5, fan_out=0, system_size=300),
        # low centrality → pollution; reached at conf 0.72 → suspected
        "m.dyn": NodeMetrics("m.dyn", fan_in=2, fan_out=1, system_size=300),
    }
    agent = ReviewAgent(
        mcp=FakeMCP(), llm=llm or FallbackLLM(), blackboard=board, project="p",
        probe=FakeProbe(metrics),
    )
    return {r.node_id: r for r in asyncio.run(agent.review_all())}


def _verdicts(board: Blackboard) -> dict[str, str]:
    return {nid: r.verdict for nid, r in _results(board).items()}


def test_shared_utility_not_flagged_as_pollution(board: Blackboard) -> None:
    # The V1 over-report: a central, low-coupling reused primitive. Even though
    # both lines recorded it as a processor, high relative fan-in reclassifies it.
    assert _verdicts(board)["m.expand"] == "shared-utility"


def test_genuine_private_intermediate_is_pollution(board: Blackboard) -> None:
    assert _verdicts(board)["m.parse_jwt"] == "pollution"


def test_high_fanin_high_fanout_is_god_node(board: Blackboard) -> None:
    assert _verdicts(board)["m.god"] == "god-node"


def test_lightweight_pass_through_is_lightweight_utility(board: Blackboard) -> None:
    # central but zero fan-out → folded transparent filter, not a heavy hub
    assert _verdicts(board)["m.fmt"] == "lightweight-utility"


def test_all_sink_crossing_is_healthy_seam(board: Blackboard) -> None:
    assert _verdicts(board)["m.get_user"] == "healthy-seam"


def test_llm_verdict_overrides_deterministic(board: Blackboard) -> None:
    # parse_jwt is deterministically pollution; an LLM that (via provenance)
    # judged it shared-utility must WIN. This is the regression for the swallowed
    # `from codemap.blackboard import VERDICTS` ImportError, which silently forced
    # every verdict onto the deterministic backstop and never consulted the LLM.
    metrics = {nid: NodeMetrics(nid, fan_in=2, fan_out=1, system_size=300)
               for nid in ("m.parse_jwt", "m.expand", "m.god", "m.get_user")}
    agent = ReviewAgent(mcp=FakeMCP(), llm=StubLLM("shared-utility"),
                        blackboard=board, project="p", probe=FakeProbe(metrics))
    res = {r.node_id: r for r in asyncio.run(agent.review_all())}
    assert res["m.parse_jwt"].verdict == "shared-utility"   # LLM, not deterministic
    assert res["m.parse_jwt"].description == "llm verdict"   # LLM prose, not _default_desc


def test_low_confidence_path_is_suspected(board: Blackboard) -> None:
    # billing reached m.dyn at confidence 0.72 (< 0.9 floor) → deterministically
    # suspected, even though the LLM is offline. The verdict itself is unchanged.
    res = _results(board)
    assert res["m.dyn"].verdict == "pollution"
    assert res["m.dyn"].suspected is True


def test_full_confidence_path_not_suspected(board: Blackboard) -> None:
    # parse_jwt's crossing was logged at confidence 1.0 → not suspected.
    res = _results(board)
    assert res["m.parse_jwt"].suspected is False


def test_llm_can_raise_suspicion_on_a_confident_path(board: Blackboard) -> None:
    # Even a full-confidence crossing is flagged if the LLM itself is unsure.
    res = _results(board, llm=StubLLM("pollution", is_suspected=True))
    assert res["m.parse_jwt"].suspected is True


def test_verdicts_persist_to_blackboard(board: Blackboard) -> None:
    _verdicts(board)
    from codemap.export import export_subway_map

    data = export_subway_map(board.db_path)
    by_node = {i["node_id"]: i for i in data["intersections"]}
    assert by_node["m.expand"]["type"] == "shared-utility"
    assert by_node["m.parse_jwt"]["type"] == "pollution"
    # suspected flag survives to the export contract the renderer consumes
    assert by_node["m.dyn"]["suspected"] is True
    assert by_node["m.parse_jwt"]["suspected"] is False
