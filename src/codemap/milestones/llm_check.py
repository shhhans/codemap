"""LLM connectivity check — run this first (especially after a fresh session
once MINIMAX_API_KEY is injected) to confirm the provider is reachable.

    python -m codemap.milestones.llm_check
    python -m codemap.milestones.llm_check --provider minimax

Prints the provider/model/base_url it's using, does the smallest possible JSON
round-trip, and reports token usage. Exits non-zero with an actionable message
if the key is missing or the endpoint rejects the call.
"""

from __future__ import annotations

import argparse
import sys

from codemap.config import config
from codemap.llm import LLMClient, LLMError


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM connectivity smoke test.")
    parser.add_argument("--provider", choices=["minimax", "dashscope"], default=None)
    ns = parser.parse_args()

    cfg = config.llm(ns.provider)
    print(f"→ Provider : {cfg.provider}")
    print(f"  Model    : {cfg.model}")
    print(f"  Base URL : {cfg.base_url}")
    print(f"  API key  : {'set (len=%d)' % len(cfg.api_key) if cfg.api_key else 'MISSING'}")

    try:
        client = LLMClient(cfg)
        reply = client.ping()
    except LLMError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ Call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  • Check the model id (MINIMAX_MODEL) and base_url are correct for your account.",
              file=sys.stderr)
        raise SystemExit(1)

    print(f"\n✓ Round-trip OK. Reply: {reply.text.strip()[:200]}")
    if reply.usage:
        print(f"  Tokens: {reply.usage}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
