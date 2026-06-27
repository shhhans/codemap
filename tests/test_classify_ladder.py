"""Batch-classification ladder — size-capped, failure-isolated.

The worker classifies a node's downstream in batches of 4. A reasoning model's
<think> cost scales with batch width against one hard token cap, so a wide batch
can truncate and lose *every* decision in it. The ladder splits a failed batch
and retries at 2, then 1, so a single hard candidate fails alone (no fate-sharing)
and the rest still get verdicts.

No network: a fake LLM returns unparsable replies for batches wider than a
threshold and a valid per-candidate verdict otherwise.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codemap.agents.worker import TaintWorker
from codemap.blackboard import Blackboard
from codemap.prompts import Candidate


class _LadderLLM:
    """Truncates (unparsable) when shown more than `max_ok` candidates; otherwise
    returns one `continue`/`is_sink` decision per candidate, keyed by ordinal."""

    def __init__(self, max_ok: int) -> None:
        self.max_ok = max_ok
        self.batch_sizes: list[int] = []

    def chat(self, system: str, window: str) -> Any:
        n = window.count("\n   signature/snippet:")  # one per rendered candidate
        self.batch_sizes.append(n)
        ok = n <= self.max_ok
        decisions = [] if not ok else [
            {"node": str(i), "verdict": "continue", "is_sink": False,
             "confidence": 0.9, "reason": "ok"}
            for i in range(1, n + 1)
        ]

        class _Reply:
            usage = {"completion_tokens": 8192}

            def json(self_inner) -> dict:
                if not ok:
                    raise ValueError("truncated: no JSON")  # simulate cut-off
                return {"decisions": decisions}

        return _Reply()


def _worker(tmp_path: Path, llm: Any) -> TaintWorker:
    bb = Blackboard(tmp_path / "bb.sqlite")
    return TaintWorker(mcp=None, llm=llm, project="p", flow_type="trace",
                       material="token", blackboard=bb)


def _cands(n: int) -> list[Candidate]:
    return [Candidate(ref=f"pkg.f{i}", name=f"f{i}", signature="()") for i in range(n)]


def test_wide_batch_degrades_and_isolates(tmp_path: Path) -> None:
    # The model only parses batches of <=1, so a width-4 batch must walk the
    # 4 → 2 → 1 ladder and still classify every candidate.
    llm = _LadderLLM(max_ok=1)
    w = _worker(tmp_path, llm)
    decisions = asyncio.run(w._classify("pkg.cur", _cands(4)))
    refs = sorted(d["node"] for d in decisions)
    assert refs == ["pkg.f0", "pkg.f1", "pkg.f2", "pkg.f3"]  # none lost
    # Tried width 4 (fail) → 2 (fail) → 1 (ok): the ladder was exercised.
    assert 4 in llm.batch_sizes and 2 in llm.batch_sizes and 1 in llm.batch_sizes


def test_node_normalised_to_ref_not_chunk_ordinal(tmp_path: Path) -> None:
    # With two width-2 chunks each numbered 1..2, the chunk-local ordinals must
    # resolve to the right global refs — not collapse onto f0/f1 twice.
    llm = _LadderLLM(max_ok=2)
    w = _worker(tmp_path, llm)
    decisions = asyncio.run(w._classify("pkg.cur", _cands(4)))
    assert sorted(d["node"] for d in decisions) == \
        ["pkg.f0", "pkg.f1", "pkg.f2", "pkg.f3"]


def test_unsplittable_single_failure_drops_only_itself(tmp_path: Path) -> None:
    # max_ok=0: even width 1 fails. The batch yields nothing but doesn't raise.
    llm = _LadderLLM(max_ok=0)
    w = _worker(tmp_path, llm)
    decisions = asyncio.run(w._classify("pkg.cur", _cands(2)))
    assert decisions == []
    assert llm.batch_sizes[-1] == 1  # degraded all the way down before giving up
