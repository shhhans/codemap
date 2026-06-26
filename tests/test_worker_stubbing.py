"""M1 wiring test — the Worker routes third-party stub callees around the LLM.

Uses fakes for MCP + LLM (no network). Proves that a downstream call into an
imported third-party module is classified Sink/Dual structurally (by out-edge
count) instead of being handed to the model, while a repo-internal callee still
goes through normal LLM classification.
"""

from __future__ import annotations

import asyncio
from typing import Any

from codemap.agents.worker import TaintWorker


class FakeMCP:
    """Canned graph: caller `app.run` calls `requests.get` (third-party stub,
    no out-edges → Sink) and `app.handler` (repo-internal → LLM)."""

    def __init__(self) -> None:
        self.classified: list[str] = []

    async def get_code_snippet(self, project: str, qualified_name: str, **_: Any) -> dict:
        sources = {
            "app.run": {"source": "import requests\n\ndef run():\n    requests.get(url)\n    handler()\n",
                        "name": "run", "signature": "()", "file_path": "app.py"},
        }
        return sources.get(qualified_name, {"name": qualified_name.rsplit(".", 1)[-1],
                                            "signature": "()", "source": "", "file_path": "app.py"})

    async def query_graph(self, project: str, query: str, **_: Any) -> dict:
        # Downstream of app.run → both callees.
        if "CALLS]->(t) RETURN DISTINCT" in query and "app.run" in query:
            return {"columns": ["qn", "name"],
                    "rows": [["requests.get", "get"], ["app.handler", "handler"]]}
        # Out-edge count for the stub: requests.get is unindexed → 0 (Sink).
        if "count(t)" in query:
            return {"columns": ["n"], "rows": [[0]]}
        return {"columns": [], "rows": []}


class FakeLLM:
    """Records every node it was asked to classify; keeps app.handler going."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def chat(self, system: str, window: str) -> Any:
        # Capture which candidate refs reached the model.
        for line in window.splitlines():
            if line.strip().startswith(("1.", "2.")):
                self._sink.append(line.strip())

        class _Reply:
            def json(self_inner) -> dict:
                return {"decisions": [
                    {"node": "1", "verdict": "continue", "is_sink": True, "confidence": 0.9,
                     "reason": "internal handler"},
                ]}

        return _Reply()


def test_third_party_stub_skips_llm_and_is_sink() -> None:
    seen: list[str] = []
    worker = TaintWorker(
        mcp=FakeMCP(), llm=FakeLLM(seen), project="p",
        flow_type="trace", material="request", blackboard=None,
    )
    children = asyncio.run(worker.expand_one("app.run", depth=0))

    # The LLM only ever saw the repo-internal candidate, never requests.get.
    joined = "\n".join(seen)
    assert "app.handler" in joined
    assert "requests.get" not in joined

    # requests.get was still recorded — as a Sink (no out-edges), so it is not
    # returned as a child to keep tracing.
    recorded = {n.qualified_name: n for n in worker.retained}
    assert recorded["requests.get"].role == "sink"
    assert "requests.get" not in {c for c, _ in children}
