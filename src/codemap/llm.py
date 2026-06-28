"""LLM client — a thin wrapper over the OpenAI SDK.

MiniMax and Dashscope both expose OpenAI-compatible endpoints, so one client
serves both; pick the provider via config. The agent layer only needs one
operation — "given a big cached system prompt + a small dynamic window, return
the model's JSON verdict" — so that's all this exposes.

Prompt-caching note: we keep the large System Prompt as a single leading
`system` message and only ever append to the tail (the sliding window). Whether
the cache actually saves money depends on the provider's server-side behavior
(this differs from Anthropic's explicit cache_control) — to be measured before
relying on it for cost, per the architecture's risk table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codemap.config import LLMConfig, config


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    text: str
    usage: dict[str, Any] | None = None

    def json(self) -> Any:
        """Parse the reply as JSON, tolerating ```json fences and stray prose."""
        return _extract_json(self.text)


@dataclass
class UsageMeter:
    """Run-level token accounting, so we can *measure* prompt-cache savings
    rather than assume them (ARCHITECTURE risk table).

    OpenAI-compatible endpoints (MiniMax / Dashscope) report server-side prompt
    caching via ``usage.prompt_tokens_details.cached_tokens``. A high
    ``cache_hit_rate`` means the large leading System Prompt is being reused —
    the design's whole bet (keep the system prompt fixed, only append the
    sliding window). When the field is absent we count zero cached tokens (an
    honest floor, not an optimistic guess)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        # Some endpoints nest it; a few flatten it to `cached_tokens` at top level.
        self.cached_tokens += int(
            (details.get("cached_tokens") if isinstance(details, dict) else 0)
            or usage.get("cached_tokens") or 0
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from cache (0..1)."""
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    def cost(self, in_per_mtok: float, out_per_mtok: float, cached_discount: float = 0.1) -> dict:
        """Illustrative cost given per-million-token prices. Cached input tokens
        are billed at ``cached_discount`` of the input price (provider-specific;
        ~0.1 is typical). Returns billed/uncached costs so the saving is explicit."""
        fresh = self.prompt_tokens - self.cached_tokens
        billed_in = (fresh + self.cached_tokens * cached_discount) / 1_000_000 * in_per_mtok
        billed_out = self.completion_tokens / 1_000_000 * out_per_mtok
        nocache_in = self.prompt_tokens / 1_000_000 * in_per_mtok
        return {
            "billed_usd": round(billed_in + billed_out, 6),
            "no_cache_usd": round(nocache_in + billed_out, 6),
            "saved_usd": round(nocache_in - billed_in, 6),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
        }


class LLMClient:
    def __init__(self, llm_config: LLMConfig | None = None):
        self.cfg = llm_config or config.llm()
        if not self.cfg.api_key:
            raise LLMError(
                f"No API key for provider {self.cfg.provider!r}. "
                f"Set {self.cfg.provider.upper()}_API_KEY in your environment/.env."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The 'openai' package is required. Install with: pip install -e .") from exc
        self._client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url)
        # Accumulates token usage across every call this client makes — the
        # whole run shares one client, so this is the run's total.
        self.usage = UsageMeter()

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        *,
        temperature: float = 0.1,
        force_json: bool = True,
    ) -> LLMReply:
        """One-shot chat: cached system prompt + dynamic user window → reply.

        `temperature` is low by default: taint pruning should be near
        deterministic, not creative.
        """
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if force_json:
            # Supported by OpenAI-compatible endpoints incl. MiniMax/Dashscope.
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface provider errors uniformly
            # Some endpoints reject response_format; retry once without it.
            if force_json:
                kwargs.pop("response_format", None)
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise LLMError(f"{self.cfg.provider} chat failed: {exc}") from exc

        text = resp.choices[0].message.content or ""
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
        self.usage.add(usage)
        return LLMReply(text=text, usage=usage)

    def ping(self) -> LLMReply:
        """Minimal connectivity check — the smallest useful round-trip."""
        return self.chat(
            system_prompt="You are a connectivity probe. Reply with strict JSON.",
            user_content='Return exactly {"ok": true}.',
        )


def _extract_json(text: str) -> Any:
    text = text.strip()
    # Reasoning models (e.g. MiniMax-M3) prepend a <think>...</think> block that
    # itself can contain braces — strip it before hunting for the JSON object.
    if "<think>" in text:
        import re

        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # An unterminated think block (truncated): drop everything up to the
        # last </think>, or up to the first '{' if the tag never closed.
        if "<think>" in text:
            tail = text.rsplit("</think>", 1)
            text = (tail[1] if len(tail) > 1 else text[text.find("{"):]).strip()
    if text.startswith("```"):
        # strip ```json ... ``` fence
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Best-effort: scan each '{' and let the JSON decoder consume the first
    # position that yields a valid object (robust to braces in leftover prose).
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
            return obj
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON object found in LLM reply", text, 0)
