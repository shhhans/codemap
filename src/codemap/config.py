"""Centralized configuration, sourced from environment variables (.env).

Everything the rest of the system needs to know about *where things live* and
*how big we let the search get* funnels through here, so there is exactly one
place to look when wiring up a new environment.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime; env vars still work.
    pass


def _split_args(raw: str) -> list[str]:
    """Parse a space-separated arg string the way a shell would."""
    return shlex.split(raw) if raw else []


@dataclass(frozen=True)
class CodebaseMemoryConfig:
    """How to launch the Codebase-Memory MCP server (stdio transport)."""

    # Verified against codebase-memory-mcp v0.8.1: running the binary with no
    # args starts the MCP server on stdio (there is no `--mcp` flag).
    binary: str = field(
        default_factory=lambda: os.getenv("CODEBASE_MEMORY_BIN", "codebase-memory-mcp")
    )
    args: list[str] = field(
        default_factory=lambda: _split_args(os.getenv("CODEBASE_MEMORY_ARGS", ""))
    )


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI-compatible endpoint config for one provider."""

    provider: str
    api_key: str | None
    base_url: str
    model: str


@dataclass(frozen=True)
class Config:
    codebase_memory: CodebaseMemoryConfig = field(default_factory=CodebaseMemoryConfig)
    blackboard_db: Path = field(
        default_factory=lambda: Path(os.getenv("BLACKBOARD_DB", "./.codemap/blackboard.sqlite"))
    )
    max_depth: int = field(default_factory=lambda: int(os.getenv("CODEMAP_MAX_DEPTH", "12")))
    max_workers: int = field(default_factory=lambda: int(os.getenv("CODEMAP_MAX_WORKERS", "8")))
    llm_provider: str = field(default_factory=lambda: os.getenv("CODEMAP_LLM_PROVIDER", "dashscope"))

    def llm(self, provider: str | None = None) -> LLMConfig:
        """Resolve the LLM endpoint config for `provider` (defaults to llm_provider)."""
        provider = (provider or self.llm_provider).lower()
        if provider == "minimax":
            return LLMConfig(
                provider="minimax",
                api_key=os.getenv("MINIMAX_API_KEY"),
                base_url=os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"),
                model=os.getenv("MINIMAX_MODEL", "abab6.5s-chat"),
            )
        if provider == "dashscope":
            return LLMConfig(
                provider="dashscope",
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url=os.getenv(
                    "DASHSCOPE_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
                model=os.getenv("DASHSCOPE_MODEL", "qwen-max"),
            )
        raise ValueError(f"Unknown LLM provider: {provider!r} (expected 'minimax' or 'dashscope')")


# Importable singleton; cheap to construct, reads env at import time.
config = Config()
