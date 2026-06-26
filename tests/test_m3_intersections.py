"""M3 persistence tests — intersection roles + map export with verdicts.

Pure stdlib (sqlite3); no MCP/LLM needed. Mirrors the fixture's crossing shape:
parse_jwt is an intermediate in both flows (dangerous), get_current_user is a
sink in both (healthy).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codemap.blackboard import Blackboard, Node, Trace
from codemap.export import export_subway_map


@pytest.fixture()
def bb(tmp_path: Path) -> Blackboard:
    board = Blackboard(tmp_path / "bb.sqlite")
    board.register_mainline("line_auth", "auth", "Auth Mainline", "#1E90FF")
    board.register_mainline("line_billing", "billing", "Billing Mainline", "#F4A261")
    # parse_jwt: intermediate processor crossed by both → dangerous
    board.upsert_node(Node(id="m.parse_jwt", name="parse_jwt", type="processor"))
    board.log_trace(Trace("auth", "m.parse_jwt", "auth", node_role="processor", depth=2))
    board.log_trace(Trace("billing", "m.parse_jwt", "billing", node_role="processor", depth=1))
    # get_current_user: stable sink in both → healthy
    board.upsert_node(Node(id="m.get_user", name="get_current_user", type="sink"))
    board.log_trace(Trace("auth", "m.get_user", "auth", node_role="sink", depth=1))
    board.log_trace(Trace("billing", "m.get_user", "billing", node_role="sink", depth=2))
    yield board
    board.close()


def test_roles_for_node_reports_each_flow(bb: Blackboard) -> None:
    roles = dict(bb.roles_for_node("m.parse_jwt"))
    assert roles == {"auth": "processor", "billing": "processor"}
    assert dict(bb.roles_for_node("m.get_user")) == {"auth": "sink", "billing": "sink"}


def test_both_nodes_are_intersections(bb: Blackboard) -> None:
    ids = {x.node_id for x in bb.intersections()}
    assert ids == {"m.parse_jwt", "m.get_user"}


def test_export_carries_verdicts_and_involved_lines(bb: Blackboard) -> None:
    bb.record_verdict("m.parse_jwt", "pollution", "Billing taps an intermediate Auth result.")
    bb.record_verdict("m.get_user", "healthy-seam", "Both consume the stable User sink.")

    data = export_subway_map(bb.db_path)
    by_node = {i["node_id"]: i for i in data["intersections"]}

    assert by_node["m.parse_jwt"]["type"] == "pollution"
    assert set(by_node["m.parse_jwt"]["involved_lines"]) == {"line_auth", "line_billing"}
    assert by_node["m.get_user"]["type"] == "healthy-seam"
    # mainlines reference real node ids
    line_ids = {m["id"] for m in data["mainlines"]}
    assert {"line_auth", "line_billing"} <= line_ids
