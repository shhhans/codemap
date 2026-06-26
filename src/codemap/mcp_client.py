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

import json
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

        Prefers structured content when the server provides it. Otherwise it
        concatenates the text blocks and — since Codebase-Memory returns its
        results as a JSON text block rather than structuredContent — parses that
        text as JSON when possible, falling back to the raw string.
        """
        result = await self._session.call_tool(name, arguments or {})
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        text = "\n".join(
            getattr(block, "text", "") for block in result.content or []
        ).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    # ── Convenience aliases for the graph queries we rely on ────────────────
    # Names + argument conventions verified against codebase-memory-mcp v0.8.1
    # and frozen in docs/mcp_tools_contract.json. Note: every query needs a
    # `project` (see index_repository); trace_path's calls mode keys on
    # `function_name`, while get_code_snippet keys on `qualified_name`.
    async def index_repository(self, repo_path: str, **arguments: Any) -> Any:
        return await self.call_tool("index_repository", {"repo_path": repo_path, **arguments})

    async def search_graph(self, project: str, **arguments: Any) -> Any:
        return await self.call_tool("search_graph", {"project": project, **arguments})

    async def trace_path(self, project: str, **arguments: Any) -> Any:
        """Trace through the graph. mode='calls' (callers/callees),
        'data_flow' (value propagation), or 'cross_service'."""
        return await self.call_tool("trace_path", {"project": project, **arguments})

    async def get_code_snippet(self, project: str, qualified_name: str, **arguments: Any) -> Any:
        return await self.call_tool(
            "get_code_snippet",
            {"project": project, "qualified_name": qualified_name, **arguments},
        )
