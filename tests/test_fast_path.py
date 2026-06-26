"""Static fast-path tests — the worker resolves candidates without the LLM where
it can, and annotates the rest with out-degree.

Two levers (no network; fakes for MCP + LLM):
  • a candidate this flow already checked in on is skipped before the LLM (its
    verdict would be discarded by the idempotent dedup anyway);
  • surviving candidates carry their fan-out into the window so the model treats
    a leaf (扇出=0) as a sink without deliberating about descent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codemap.agents.worker import TaintWorker
from codemap.blackboard import Blackboard, Node, Trace


class FakeMCP:
    """`cur` calls two repo-internal nodes: `pkg.a` (leaf, fan-out 0) and
    `pkg.b` (already visited by this flow). No imports, no stubs."""

    async def get_code_snippet(self, project: str, qualified_name: str, **_: Any) -> dict:
        if qualified_name == "cur":
            return {"source": "def cur():\n    a()\n    b()\n", "name": "cur",
                    "signature": "()", "file_path": "cur.py"}
        return {"name": qualified_name.rsplit(".", 1)[-1], "signature": "()",
                "source": "", "file_path": "cur.py"}

    async def query_graph(self, project: str, query: str, **_: Any) -> dict:
        if "m:Module" in query:
            return {"columns": ["qn"], "rows": []}          # no modules → no stubbing
        if "CALLS]->(t) RETURN DISTINCT" in query:
            return {"columns": ["qn", "name"], "rows": [["pkg.a", "a"], ["pkg.b", "b"]]}
        if "IN [" in query and "count(t)" in query:          # batched fan-out
            return {"columns": ["qn", "n"], "rows": [["pkg.a", 0], ["pkg.b", 3]]}
        if "t.name = " in query:                              # dynamic recovery: none
            return {"columns": ["qn"], "rows": []}
        return {"columns": [], "rows": []}


class CapturingLLM:
    """Records the candidate lines it was shown; keeps the one node going."""

    def __init__(self) -> None:
        self.window = ""

    def chat(self, system: str, window: str) -> Any:
        self.window = window

        class _Reply:
            usage = None

            def json(self_inner) -> dict:
                return {"decisions": [
                    {"node": "1", "verdict": "continue", "is_sink": True,
                     "confidence": 0.9, "reason": "leaf sink"},
                ]}

        return _Reply()


def _worker(tmp_path: Path, llm: Any) -> tuple[TaintWorker, Blackboard]:
    bb = Blackboard(tmp_path / "bb.sqlite")
    # Pre-visit pkg.b on this flow so the fast-path skips it before the LLM.
    bb.upsert_node(Node(id="pkg.b", name="b"))
    bb.log_trace(Trace("trace", "pkg.b", "trace", node_role="processor"))
    w = TaintWorker(mcp=FakeMCP(), llm=llm, project="p", flow_type="trace",
                    material="token", blackboard=bb)
    return w, bb


def test_already_visited_candidate_skips_the_llm(tmp_path: Path) -> None:
    llm = CapturingLLM()
    w, bb = _worker(tmp_path, llm)
    asyncio.run(w.expand_one("cur", depth=0))
    # pkg.b was already checked in → never shown to the model; pkg.a was.
    assert "pkg.a" in llm.window
    assert "pkg.b" not in llm.window
    assert w.skipped == 1
    bb.close()


def test_surviving_candidate_carries_fan_out_hint(tmp_path: Path) -> None:
    llm = CapturingLLM()
    w, bb = _worker(tmp_path, llm)
    asyncio.run(w.expand_one("cur", depth=0))
    # pkg.a has out-degree 0 → flagged a leaf so the model needn't reason descent.
    assert "[扇出=0·叶子]" in llm.window
    bb.close()


def test_out_edge_counts_batched_parsing(tmp_path: Path) -> None:
    w, bb = _worker(tmp_path, CapturingLLM())
    counts = asyncio.run(w._out_edge_counts(["pkg.a", "pkg.b"]))
    assert counts == {"pkg.a": 0, "pkg.b": 3}
    assert asyncio.run(w._out_edge_counts([])) == {}
    bb.close()


def test_already_seen_uses_per_flow_visited_set(tmp_path: Path) -> None:
    w, bb = _worker(tmp_path, CapturingLLM())
    assert w._already_seen("pkg.b") is True       # pre-visited on flow "trace"
    assert w._already_seen("pkg.a") is False
    bb.close()
