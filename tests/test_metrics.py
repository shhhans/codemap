"""M2 tests — relative fan-in/out metrics and the structural hub classifier.

Pure stdlib for the numeric helpers; a FakeMCP exercises MetricsProbe's graph
queries (system size with File/Class labels and the all-nodes fallback).
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from codemap.metrics import (
    GOD_NODE,
    ORDINARY,
    SHARED_UTILITY,
    MetricsProbe,
    NodeMetrics,
    classify_hub,
    relative_fan_in,
)


# ── Concordia formula ───────────────────────────────────────────────────────
def test_relative_fan_in_matches_formula() -> None:
    val = relative_fan_in(fan_in=10, system_size=20)
    assert val == pytest.approx(10 / (20 * math.log(20)))


def test_relative_fan_in_degenerate_cases() -> None:
    assert relative_fan_in(5, 1) == 0.0     # one-unit system: no gradient
    assert relative_fan_in(5, 0) == 0.0
    assert relative_fan_in(0, 50) == 0.0    # no callers


def test_relative_fan_in_is_scale_free() -> None:
    # Same node centrality in a bigger system yields a smaller relative number.
    small = relative_fan_in(10, 20)
    big = relative_fan_in(10, 2000)
    assert big < small


# ── Hub classification ──────────────────────────────────────────────────────
def test_classify_hub_shared_utility() -> None:
    # central but low coupling, with real breadth → a healthy shared primitive
    assert classify_hub(0.2, 2, rel_high=0.08, fanout_high=8,
                        fan_in=40, fanin_min=4) == SHARED_UTILITY


def test_classify_hub_god_node() -> None:
    # central AND high coupling → infra-disguised mess
    assert classify_hub(0.2, 15, rel_high=0.08, fanout_high=8,
                        fan_in=40, fanin_min=4) == GOD_NODE


def test_classify_hub_ordinary() -> None:
    # not central → not a hub at all (pollution decided elsewhere)
    assert classify_hub(0.01, 1, rel_high=0.08, fanout_high=8) == ORDINARY
    assert classify_hub(0.01, 20, rel_high=0.08, fanout_high=8) == ORDINARY


def test_classify_hub_absolute_breadth_gate() -> None:
    # The tiny-fixture misfire: Concordia degenerates at small S so rel_fan_in
    # blows up (1.44), but a node reached by only 2 callers is NOT a hub. The
    # absolute floor keeps it ORDINARY so the pollution case stays pollution.
    assert classify_hub(1.44, 1, rel_high=0.08, fanout_high=8,
                        fan_in=2, fanin_min=4) == ORDINARY
    # crossing the floor restores hub classification
    assert classify_hub(1.44, 1, rel_high=0.08, fanout_high=8,
                        fan_in=4, fanin_min=4) == SHARED_UTILITY


def test_node_metrics_rel_fan_in_property() -> None:
    m = NodeMetrics("pkg.hub", fan_in=10, fan_out=2, system_size=20)
    assert m.rel_fan_in == pytest.approx(relative_fan_in(10, 20))


# ── MetricsProbe over a fake graph ──────────────────────────────────────────
class FakeMCP:
    def __init__(self, *, files: int, total: int, fan_in: int, fan_out: int) -> None:
        self.files, self.total, self._in, self._out = files, total, fan_in, fan_out

    async def query_graph(self, project: str, query: str, **_: Any) -> dict:
        if "n:File OR n:Class" in query:
            return {"columns": ["n"], "rows": [[self.files]]}
        if "MATCH (n) RETURN count(n)" in query:
            return {"columns": ["n"], "rows": [[self.total]]}
        if "<-[:CALLS]-(s)" in query:
            return {"columns": ["n"], "rows": [[self._in]]}
        if "-[:CALLS]->(t)" in query:
            return {"columns": ["n"], "rows": [[self._out]]}
        return {"columns": [], "rows": []}


def test_probe_uses_file_class_count_for_system_size() -> None:
    probe = MetricsProbe(FakeMCP(files=20, total=300, fan_in=10, fan_out=2), "p")
    m = asyncio.run(probe.node_metrics("pkg.hub"))
    assert m.system_size == 20            # File/Class labels present → used directly
    assert m.fan_in == 10 and m.fan_out == 2
    assert m.rel_fan_in == pytest.approx(relative_fan_in(10, 20))


def test_probe_falls_back_to_total_nodes_when_unlabeled() -> None:
    # Engine exposes no File/Class labels (count 0/1) → fall back to all nodes.
    probe = MetricsProbe(FakeMCP(files=0, total=338, fan_in=4, fan_out=1), "p")
    assert asyncio.run(probe.system_size()) == 338
