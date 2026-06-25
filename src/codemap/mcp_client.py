"""Codebase-Memory MCP client (Milestone 1).

Wraps an MCP stdio session to the Codebase-Memory binary so the rest of the
system can call graph queries (`trace_call_path`, `get_code_snippet`, ...) as
plain async methods instead of juggling the MCP protocol.

Design intent: the *real* tool schemas exposed by Codebase-Memory are the
contract every later milestone depends on, but we don't control that binary's
exact I/O shape. So this client deliberately stays schema-agnostic — it
`list_tools()` to discover what's actually there and `call_tool(name, args)`
generically — and Milestone 1 dumps the discovered schemas to disk to freeze
the contract.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from codemap.config import CodebaseMemoryConfig, config


class MCPDependencyError(RuntimeError):
    """Raised when the `mcp` package isn't installed."""


@dataclass
class ToolInfo:
    name: str
    description: str | None
    input_schema: dict[str, Any]


class CodebaseMemoryClient:
    """Async wrapper around an MCP stdio session to Codebase-Memory.

    Use via the `connect()` context manager, which owns the subprocess and
    session lifetimes:

        async with CodebaseMemoryClient.connect() as client:
            tools = await client.list_tools()
            edges = await client.trace_call_path(node_id="...")
    """

    def __init__(self, session: Any):
        self._session = session

    # ── Lifecycle ──────────────────────────────────────────────────────────
    @classmethod
    @asynccontextmanager
    async def connect(
        cls, cm_config: CodebaseMemoryConfig | None = None
    ) -> AsyncIterator["CodebaseMemoryClient"]:
        """Launch Codebase-Memory over MCP stdio and yield a connected client."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - depends on env
            raise MCPDependencyError(
                "The 'mcp' package is required. Install with: pip install -e ."
            ) from exc

        cm_config = cm_config or config.codebase_memory
        params = StdioServerParameters(command=cm_config.binary, args=cm_config.args)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield cls(session)

    # ── Generic MCP surface ──────────────────────────────────────────────────
    async def list_tools(self) -> list[ToolInfo]:
        result = await self._session.list_tools()
        return [
            ToolInfo(
                name=t.name,
                description=t.description,
                input_schema=getattr(t, "inputSchema", {}) or {},
            )
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and return its parsed payload.

        Prefers structured content when the server provides it, otherwise falls
        back to concatenated text blocks.
        """
        result = await self._session.call_tool(name, arguments or {})
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        texts = [getattr(block, "text", "") for block in result.content or []]
        return "\n".join(t for t in texts if t)

    # ── Convenience aliases for the graph queries we rely on ────────────────
    # These mirror the planned Codebase-Memory tool names. Argument names are
    # best-effort guesses until M1 freezes the real contract; keep them thin so
    # adapting to the discovered schema is a one-line change.
    async def trace_call_path(self, **arguments: Any) -> Any:
        return await self.call_tool("trace_call_path", arguments)

    async def get_code_snippet(self, **arguments: Any) -> Any:
        return await self.call_tool("get_code_snippet", arguments)

    async def search_graph(self, **arguments: Any) -> Any:
        return await self.call_tool("search_graph", arguments)
