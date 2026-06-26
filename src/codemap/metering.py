"""LLM usage + latency metering.

Every Worker / ReviewAgent call funnels through :meth:`LLMClient.chat`, so a
single meter attached to the shared client captures the whole run's cost without
threading a counter through the agent layer. We record, per call:

  • token usage — prompt / completion / total, plus the two breakdowns the
    architecture flagged as "to be measured": ``cached_tokens`` (does the big
    static System Prompt actually hit the provider's prompt cache?) and
    ``reasoning_tokens`` (how much a reasoning model spends inside ``<think>``).
  • wall latency — summed per-call seconds. The agent layer offloads each chat
    to a thread (``asyncio.to_thread``) and runs them concurrently, so this sum
    is the *serial* LLM time; compare it against the run's wall clock to see how
    much concurrency actually bought.

Increments are lock-guarded because concurrent worker threads record at once.
Pure stdlib; no provider coupling, so it is unit-testable offline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMMeter:
    """Thread-safe accumulator for LLM token usage and call latency."""

    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0      # prompt tokens served from the provider's cache
    reasoning_tokens: int = 0   # completion tokens spent inside <think> (reasoning models)
    llm_seconds: float = 0.0    # summed per-call latency (serial LLM time)
    max_seconds: float = 0.0    # slowest single call
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, usage: dict[str, Any] | None, elapsed_s: float, *, ok: bool = True) -> None:
        """Fold one chat call's usage + latency into the totals."""
        with self._lock:
            self.calls += 1
            if not ok:
                self.failures += 1
            self.llm_seconds += elapsed_s
            self.max_seconds = max(self.max_seconds, elapsed_s)
            if not usage:
                return
            self.prompt_tokens += _int(usage.get("prompt_tokens"))
            self.completion_tokens += _int(usage.get("completion_tokens"))
            self.total_tokens += _int(usage.get("total_tokens")) or (
                _int(usage.get("prompt_tokens")) + _int(usage.get("completion_tokens"))
            )
            self.cached_tokens += _int(_get(usage, "prompt_tokens_details", "cached_tokens"))
            self.reasoning_tokens += _int(_get(usage, "completion_tokens_details", "reasoning_tokens"))

    @property
    def avg_seconds(self) -> float:
        return self.llm_seconds / self.calls if self.calls else 0.0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from cache (0.0 when no prompt tokens)."""
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    def summary(self, wall_seconds: float | None = None) -> str:
        """A human-readable one-block report (Chinese labels for the CLI)."""
        lines = [
            "── LLM 用量与耗时 ──────────────────────────────",
            f"  调用次数      : {self.calls}" + (f"（失败 {self.failures}）" if self.failures else ""),
            f"  Token 总计    : {self.total_tokens}"
            f"  (prompt {self.prompt_tokens} / completion {self.completion_tokens})",
            f"  prompt 缓存命中: {self.cached_tokens} tokens"
            f"（{self.cache_hit_rate * 100:.1f}% of prompt）",
            f"  推理 token    : {self.reasoning_tokens}"
            f"（<think> 内消耗，占 completion {_pct(self.reasoning_tokens, self.completion_tokens)}）",
            f"  LLM 串行耗时  : {self.llm_seconds:.2f}s"
            f"（平均 {self.avg_seconds:.2f}s/次，最慢 {self.max_seconds:.2f}s）",
        ]
        if wall_seconds is not None:
            speedup = self.llm_seconds / wall_seconds if wall_seconds > 0 else 0.0
            lines.append(
                f"  实际墙钟      : {wall_seconds:.2f}s"
                f"（并发提速 ≈ {speedup:.1f}× vs 串行 LLM 时间）"
            )
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "llm_seconds": round(self.llm_seconds, 3),
            "max_seconds": round(self.max_seconds, 3),
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _get(usage: dict[str, Any], outer: str, inner: str) -> Any:
    block = usage.get(outer)
    return block.get(inner) if isinstance(block, dict) else None


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


class timed:
    """Tiny wall-clock context manager: ``with timed() as t: ...; t.seconds``."""

    def __enter__(self) -> "timed":
        self._t0 = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *_exc: object) -> None:
        self.seconds = time.perf_counter() - self._t0
