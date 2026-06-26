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
        return LLMReply(text=text, usage=usage)

    def ping(self) -> LLMReply:
        """Minimal connectivity check — the smallest useful round-trip."""
        return self.chat(
            system_prompt="You are a connectivity probe. Reply with strict JSON.",
            user_content='Return exactly {"ok": true}.',
        )


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fence
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: grab the outermost {...}.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise
