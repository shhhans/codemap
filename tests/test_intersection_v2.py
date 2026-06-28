"""V2.1 intersection reviewer tests — offline (no real MCP binary / LLM).

Covers the load-bearing new behavior:
  • parent-pointer path reconstruction (the data façade-bypass needs),
  • the ternary 'suspected' verdict + the rule that deterministic logic never
    hard-judges 'dangerous' (LLM down / low-confidence → suspected),
  • evidence assembly distinguishing the three canonical crossing shapes against
    a stub graph that mirrors the fixture:
      - parse_jwt     : private intermediate, reached by Billing via a bypass,
      - get_current_user: stable sink consumed by both → no bypass,
      - hub           : public primitive, high cross-caller fan-in.

A stub graph + stub LLM keep this hermetic; the live engine run is a separate
script (scripts/exp_intersection_v2.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codemap.agents.review import PUBLIC_HUB_FANIN, ReviewAgent
from codemap.blackboard import Blackboard, Node, Trace

# Fixture-shaped qualified names.
A_LOGIN, A_VERIFY, A_PARSE, A_GETUSER = "app.auth.login", "app.auth.verify_token", \
    "app.auth.parse_jwt", "app.auth.get_current_user"
B_CHARGE = "app.billing.charge"
HUB = "app.core.hub"

# Static CALLS graph (caller -> callee). parse_jwt is wrapped by verify_token in
# Auth; Billing's charge calls parse_jwt DIRECTLY (the bypass). hub is called by
# many distinct callers (a public primitive).
CALLS = [
    (A_LOGIN, A_VERIFY), (A_LOGIN, A_GETUSER), (A_VERIFY, A_PARSE),
    (B_CHARGE, A_PARSE), (B_CHARGE, A_GETUSER), (B_CHARGE, HUB),
    (A_LOGIN, HUB), (A_VERIFY, HUB), ("app.x.d1", HUB), ("app.x.d2", HUB),
    ("app.x.d3", HUB),
]


class FakeGraph:
    """Minimal stand-in for CodebaseMemoryClient covering the reviewer's queries."""

    def __init__(self, calls: list[tuple[str, str]]):
        self.adj: dict[str, list[str]] = {}
        for a, b in calls:
            self.adj.setdefault(a, []).append(b)

    def _reach(self, src: str, dst: str, max_hops: int = 3) -> bool:
        frontier, seen = [(src, 0)], {src}
        while frontier:
            node, hops = frontier.pop()
            if hops >= max_hops:
                continue
            for nxt in self.adj.get(node, []):
                if nxt == dst:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append((nxt, hops + 1))
        return False

    def _callers(self, dst: str) -> list[str]:
        return [a for a, b in ((a, b) for a, bs in self.adj.items() for b in bs) if b == dst]

    async def query_graph(self, project: str, query: str, **_: object) -> dict:
        lits = re.findall(r"qualified_name:'([^']+)'", query)
        if "fan_in" in query:                          # _fan_in
            return {"columns": ["fan_in"], "rows": [[len(set(self._callers(lits[0])))]]}
        if "CALLS*1..3" in query:                      # _calls_reach
            src, dst = lits[0], lits[1]
            return {"columns": ["n"], "rows": [[1 if self._reach(src, dst) else 0]]}
        return {"columns": [], "rows": []}

    async def get_code_snippet(self, project: str, qualified_name: str, **_: object) -> dict:
        return {"source": f"def {qualified_name.rsplit('.', 1)[-1]}(...): ...  # stub body"}


class FakeReply:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeLLM:
    """Records calls; returns a scripted sequence of replies (or raises)."""

    def __init__(self, replies: list[dict] | None = None, raise_exc: bool = False):
        self.replies = list(replies or [])
        self.raise_exc = raise_exc
        self.calls = 0

    def chat(self, system_prompt: str, user_content: str, **_: object) -> FakeReply:
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("LLM endpoint unreachable")
        return FakeReply(self.replies.pop(0) if self.replies else {"verdict": "healthy",
                                                                    "description": "ok"})


@pytest.fixture()
def board(tmp_path: Path) -> Blackboard:
    """Blackboard pre-loaded with the fixture crossing shape + parent pointers."""
    bb = Blackboard(tmp_path / "bb.sqlite")
    bb.register_mainline("line_auth", "auth", "Auth", "#1E90FF")
    bb.register_mainline("line_billing", "billing", "Billing", "#F4A261")
    for nid, nm, ty in [
        (A_LOGIN, "login", "source"), (A_VERIFY, "verify_token", "processor"),
        (A_PARSE, "parse_jwt", "processor"), (A_GETUSER, "get_current_user", "sink"),
        (B_CHARGE, "charge", "source"), (HUB, "hub", "processor"),
    ]:
        bb.upsert_node(Node(id=nid, name=nm, type=ty))

    def t(node, flow, role, depth, parent, conf=1.0):
        bb.log_trace(Trace(agent_id=flow, node_id=node, flow_type=flow, node_role=role,
                           depth=depth, parent_node_id=parent, confidence=conf))

    # Auth path: login → verify_token → parse_jwt ; login → get_current_user ; → hub
    t(A_LOGIN, "auth", "source", 0, None)
    t(A_VERIFY, "auth", "processor", 1, A_LOGIN)
    t(A_PARSE, "auth", "processor", 2, A_VERIFY)
    t(A_GETUSER, "auth", "sink", 1, A_LOGIN)
    t(HUB, "auth", "processor", 2, A_VERIFY)
    # Billing path: charge → parse_jwt (BYPASS) ; charge → get_current_user ; → hub
    t(B_CHARGE, "billing", "source", 0, None)
    t(A_PARSE, "billing", "processor", 1, B_CHARGE)
    t(A_GETUSER, "billing", "sink", 1, B_CHARGE)
    t(HUB, "billing", "processor", 1, B_CHARGE)
    yield bb
    bb.close()


def _reviewer(board: Blackboard, llm: FakeLLM | None = None) -> ReviewAgent:
    return ReviewAgent(mcp=FakeGraph(CALLS), llm=llm or FakeLLM(), blackboard=board,
                       project="stub")


# ── Sprint 1: parent pointers + path reconstruction ──────────────────────────
def test_path_to_node_reconstructs_parent_chain(board: Blackboard) -> None:
    assert board.path_to_node(A_PARSE, "auth") == [A_LOGIN, A_VERIFY, A_PARSE]
    # Billing reached parse_jwt straight from its seed — the bypass shows up as a
    # path that skips verify_token.
    assert board.path_to_node(A_PARSE, "billing") == [B_CHARGE, A_PARSE]
    assert A_VERIFY not in board.path_to_node(A_PARSE, "billing")


def test_record_verdict_accepts_suspected(board: Blackboard) -> None:
    board.record_verdict(A_PARSE, "suspected", "待确认")
    with pytest.raises(ValueError):
        board.record_verdict(A_PARSE, "bogus")


# ── Sprint 3: evidence assembly distinguishes the three shapes ───────────────
async def test_evidence_detects_bypass_for_private_intermediate(board: Blackboard) -> None:
    ev = await _reviewer(board)._assemble_evidence(A_PARSE)
    assert ev.bypasses, "Billing reached parse_jwt without going through Auth's façade"
    b = ev.bypasses[0]
    assert b.owner_flow == "auth" and b.bypassing_flow == "billing"
    assert ev.fan_in == 2 and ev.fan_in < PUBLIC_HUB_FANIN  # low → private, not a hub


async def test_no_bypass_when_crossing_is_a_shared_sink(board: Blackboard) -> None:
    ev = await _reviewer(board)._assemble_evidence(A_GETUSER)
    assert ev.bypasses == []  # both flows treat it as a sink → consuming it is fine


async def test_public_hub_shows_high_fanin_evidence(board: Blackboard) -> None:
    ev = await _reviewer(board)._assemble_evidence(HUB)
    # The hub is crossed and even "bypassed", but its high cross-caller fan-in is
    # the evidence that should let the judge call it healthy, not pollution.
    assert ev.fan_in >= PUBLIC_HUB_FANIN


# ── Sprint 3: ternary verdict + "never hard-dangerous offline" ───────────────
async def test_low_confidence_crossing_is_suspected_without_llm(board: Blackboard) -> None:
    # A crossing carried by a recovered dynamic edge (confidence 0.8×0.8=0.64).
    bb = board
    bb.upsert_node(Node(id="app.dyn.x", name="x", type="processor"))
    bb.log_trace(Trace(agent_id="auth", node_id="app.dyn.x", flow_type="auth",
                       node_role="processor", depth=1, parent_node_id=A_LOGIN, confidence=0.64))
    bb.log_trace(Trace(agent_id="billing", node_id="app.dyn.x", flow_type="billing",
                       node_role="processor", depth=1, parent_node_id=B_CHARGE, confidence=0.64))
    llm = FakeLLM(replies=[{"verdict": "dangerous", "description": "should be ignored"}])
    verdict, _ = await _reviewer(bb, llm)._judge(
        await _reviewer(bb, llm)._assemble_evidence("app.dyn.x"))
    assert verdict == "suspected"  # low confidence → never reaches the LLM
    assert llm.calls == 0


async def test_llm_unavailable_degrades_to_suspected_not_dangerous(board: Blackboard) -> None:
    llm = FakeLLM(raise_exc=True)
    rev = _reviewer(board, llm)
    verdict, _ = await rev._judge(await rev._assemble_evidence(A_PARSE))
    assert verdict == "suspected"  # the old code hard-defaulted to dangerous here


async def test_llm_can_return_each_ternary_state(board: Blackboard) -> None:
    for want in ("healthy", "dangerous"):
        llm = FakeLLM(replies=[{"verdict": want, "description": "x"}])
        rev = _reviewer(board, llm)
        verdict, _ = await rev._judge(await rev._assemble_evidence(A_PARSE))
        assert verdict == want


async def test_insufficient_context_reinvestigates_then_settles(board: Blackboard) -> None:
    # Judge keeps asking for more context → after MAX_RETRIES we settle on suspected.
    llm = FakeLLM(replies=[{"verdict": "insufficient_context", "target_node_id": A_VERIFY,
                            "description": "need more"}] * 5)
    rev = _reviewer(board, llm)
    verdict, _ = await rev._judge(await rev._assemble_evidence(A_PARSE))
    assert verdict == "suspected"
    assert llm.calls == 3  # initial + MAX_RETRIES(2)


# ── Shared-ancestor inheritance (the _downstream fix) ────────────────────────
SX, SY, C_HUB, D_DOWN = "app.x.sx", "app.y.sy", "app.core.c", "app.core.d"
HUB_CALLS = [(SX, C_HUB), (SY, C_HUB), (C_HUB, D_DOWN)]


@pytest.fixture()
def hub_board(tmp_path: Path) -> Blackboard:
    """Two flows that both reach D only by passing through a shared hub C.

    flowX: sx → C → D      flowY: sy → C → D
    C and D are both crossings; D is C's private downstream helper (fan-in 1),
    structurally just like codemap's _downstream under expand_one.
    """
    bb = Blackboard(tmp_path / "hb.sqlite")
    bb.register_mainline("line_x", "x", "X", "#1E90FF")
    bb.register_mainline("line_y", "y", "Y", "#F4A261")
    for nid, nm in [(SX, "sx"), (SY, "sy"), (C_HUB, "c"), (D_DOWN, "d")]:
        bb.upsert_node(Node(id=nid, name=nm, type="processor"))

    def t(node, flow, depth, parent):
        bb.log_trace(Trace(agent_id=flow, node_id=node, flow_type=flow,
                           node_role="processor", depth=depth, parent_node_id=parent))
    t(SX, "x", 0, None)
    t(C_HUB, "x", 1, SX)
    t(D_DOWN, "x", 2, C_HUB)
    t(SY, "y", 0, None)
    t(C_HUB, "y", 1, SY)
    t(D_DOWN, "y", 2, C_HUB)
    yield bb
    bb.close()


async def test_downstream_of_shared_hub_has_shared_ancestor(hub_board: Blackboard) -> None:
    rev = ReviewAgent(mcp=FakeGraph(HUB_CALLS), llm=FakeLLM(), blackboard=hub_board, project="stub")
    ev = await rev._assemble_evidence(D_DOWN)
    assert ev.shared_ancestor == C_HUB        # both flows reached D through C
    ev_c = await rev._assemble_evidence(C_HUB)
    assert ev_c.shared_ancestor is None        # the hub itself forks the two flows


async def test_private_downstream_inherits_hub_verdict_without_llm(hub_board: Blackboard) -> None:
    # C is judged healthy by the LLM; D must INHERIT healthy deterministically —
    # if it wrongly re-asked the LLM it would get the scripted 'dangerous'.
    llm = FakeLLM(replies=[{"verdict": "healthy", "description": "C is a shared hub"},
                           {"verdict": "dangerous", "description": "should never be used"}])
    rev = ReviewAgent(mcp=FakeGraph(HUB_CALLS), llm=llm, blackboard=hub_board, project="stub")
    results = {r.name: r for r in await rev.review_all()}
    assert results["c"].verdict == "healthy"
    assert results["d"].verdict == "healthy"   # inherited, not LLM-judged
    assert llm.calls == 1                       # only C went to the LLM


async def test_inheritance_is_direction_neutral(hub_board: Blackboard) -> None:
    # A shared *dangerous* hub propagates dangerous to its downstream, too.
    llm = FakeLLM(replies=[{"verdict": "dangerous", "description": "C taps a private result"}])
    rev = ReviewAgent(mcp=FakeGraph(HUB_CALLS), llm=llm, blackboard=hub_board, project="stub")
    results = {r.name: r for r in await rev.review_all()}
    assert results["c"].verdict == "dangerous"
    assert results["d"].verdict == "dangerous"  # inherits the hub's verdict
    assert llm.calls == 1
