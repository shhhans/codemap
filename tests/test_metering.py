"""Metering tests — LLMMeter token/latency accumulation and breakdown extraction.

Pure stdlib; no provider calls. The headline checks are that the meter pulls the
two nested breakdowns the architecture flagged as "to be measured" —
``prompt_tokens_details.cached_tokens`` and
``completion_tokens_details.reasoning_tokens`` — out of the OpenAI-shaped usage
dict, and that it is robust to missing/partial usage.
"""

from __future__ import annotations

from codemap.metering import LLMMeter, timed


def _usage(prompt: int, completion: int, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def test_accumulates_tokens_and_latency() -> None:
    m = LLMMeter()
    m.record(_usage(100, 30, cached=64, reasoning=20), 0.5)
    m.record(_usage(200, 40, cached=128, reasoning=10), 1.5)
    assert m.calls == 2
    assert m.prompt_tokens == 300
    assert m.completion_tokens == 70
    assert m.total_tokens == 370
    assert m.cached_tokens == 192
    assert m.reasoning_tokens == 30
    assert m.llm_seconds == 2.0
    assert m.max_seconds == 1.5
    assert m.avg_seconds == 1.0
    assert m.cache_hit_rate == 192 / 300


def test_total_tokens_derived_when_absent() -> None:
    m = LLMMeter()
    m.record({"prompt_tokens": 10, "completion_tokens": 5}, 0.1)  # no total_tokens key
    assert m.total_tokens == 15


def test_records_failures_with_no_usage() -> None:
    m = LLMMeter()
    m.record(None, 0.3, ok=False)
    assert m.calls == 1 and m.failures == 1
    assert m.total_tokens == 0
    assert m.llm_seconds == 0.3


def test_missing_breakdown_blocks_are_safe() -> None:
    m = LLMMeter()
    m.record({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, 0.1)
    assert m.cached_tokens == 0 and m.reasoning_tokens == 0


def test_summary_includes_wall_and_speedup() -> None:
    m = LLMMeter()
    m.record(_usage(100, 30, cached=64), 1.0)
    m.record(_usage(100, 30, cached=64), 1.0)
    out = m.summary(wall_seconds=1.0)   # 2.0s serial over 1.0s wall → ~2x
    assert "调用次数" in out and "prompt 缓存命中" in out
    assert "2.0×" in out


def test_as_dict_roundtrips_counters() -> None:
    m = LLMMeter()
    m.record(_usage(100, 30, cached=64, reasoning=20), 0.5)
    d = m.as_dict()
    assert d["calls"] == 1 and d["cached_tokens"] == 64 and d["reasoning_tokens"] == 20


def test_timed_measures_a_block() -> None:
    with timed() as t:
        sum(range(1000))
    assert t.seconds >= 0.0
