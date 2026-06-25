"""Blackboard tests — pure stdlib (sqlite3), no external deps needed.

Covers the load-bearing behaviors: idempotent check-ins / visited-set, and the
intersection view that powers the responsibility-pollution health check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codemap.blackboard import Blackboard, Node, Trace


@pytest.fixture()
def bb(tmp_path: Path) -> Blackboard:
    board = Blackboard(tmp_path / "bb.sqlite")
    yield board
    board.close()


def test_log_trace_is_idempotent_and_acts_as_visited_set(bb: Blackboard) -> None:
    bb.upsert_node(Node(id="n1", name="login()", file_path="src/auth/controller.py"))

    first = bb.log_trace(Trace(agent_id="auth", node_id="n1", flow_type="auth"))
    second = bb.log_trace(Trace(agent_id="auth", node_id="n1", flow_type="auth"))

    assert first is True, "first check-in is new"
    assert second is False, "re-visiting the same (node, flow) is a no-op"
    assert bb.has_visited("n1", "auth") is True
    assert bb.has_visited("n1", "billing") is False


def test_intersection_detected_when_two_flows_share_a_node(bb: Blackboard) -> None:
    bb.upsert_node(Node(id="parseJWT", name="parseJWT()"))
    bb.log_trace(Trace(agent_id="auth", node_id="parseJWT", flow_type="auth", node_role="processor"))
    bb.log_trace(Trace(agent_id="billing", node_id="parseJWT", flow_type="billing"))

    crossings = bb.intersections()
    assert len(crossings) == 1
    x = crossings[0]
    assert x.node_id == "parseJWT"
    assert x.flow_count == 2
    assert set(x.flow_types) == {"auth", "billing"}


def test_single_flow_node_is_not_an_intersection(bb: Blackboard) -> None:
    bb.upsert_node(Node(id="n2", name="getUser()"))
    bb.log_trace(Trace(agent_id="auth", node_id="n2", flow_type="auth"))
    assert bb.intersections() == []


def test_record_verdict_round_trips(bb: Blackboard) -> None:
    bb.upsert_node(Node(id="parseJWT", name="parseJWT()"))
    bb.record_verdict("parseJWT", "dangerous", "Billing ingests an intermediate Auth result.")
    # Upsert path: re-recording updates rather than raising.
    bb.record_verdict("parseJWT", "healthy", "Reclassified after refactor.")

    with pytest.raises(ValueError):
        bb.record_verdict("parseJWT", "bogus")


def test_nodes_for_flow_orders_by_depth(bb: Blackboard) -> None:
    for nid, depth in [("c", 2), ("a", 0), ("b", 1)]:
        bb.upsert_node(Node(id=nid, name=f"{nid}()"))
        bb.log_trace(Trace(agent_id="auth", node_id=nid, flow_type="auth", depth=depth))
    assert bb.nodes_for_flow("auth") == ["a", "b", "c"]
