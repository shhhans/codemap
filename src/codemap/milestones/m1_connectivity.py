"""Milestone 1 — Infrastructure connectivity (The Scaffolding).

Goal: prove Python can launch and talk to Codebase-Memory over MCP, then
*freeze the tool contract* every later milestone depends on by dumping the
real, discovered tool schemas to docs/mcp_tools_contract.json.

This is intentionally robust to a missing binary / missing `mcp` package: it
won't crash with a stack trace, it prints an actionable diagnostic and exits
non-zero, so it doubles as an environment health check.

Run:
    python -m codemap.milestones.m1_connectivity
    python -m codemap.milestones.m1_connectivity --probe trace_call_path '{"node_id": "..."}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codemap.config import config
from codemap.mcp_client import CodebaseMemoryClient, MCPDependencyError, ToolInfo

# Tools M1 expects Codebase-Memory to expose. Missing ones are warnings, not
# hard failures — the binary's real surface is whatever list_tools() returns.
EXPECTED_TOOLS = ["trace_call_path", "get_code_snippet", "search_graph"]

CONTRACT_PATH = Path("docs/mcp_tools_contract.json")


def _freeze_contract(tools: list[ToolInfo]) -> Path:
    """Persist discovered tool schemas as the canonical contract snapshot."""
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {
            "binary": config.codebase_memory.binary,
            "args": config.codebase_memory.args,
        },
        "tools": [asdict(t) for t in tools],
    }
    CONTRACT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return CONTRACT_PATH


async def _run(probe: tuple[str, dict[str, Any]] | None) -> int:
    print(f"→ Launching Codebase-Memory: {config.codebase_memory.binary} "
          f"{' '.join(config.codebase_memory.args)}")
    try:
        async with CodebaseMemoryClient.connect() as client:
            tools = await client.list_tools()
            print(f"✓ Connected. Discovered {len(tools)} MCP tool(s):\n")
            for t in tools:
                marker = "•" if t.name in EXPECTED_TOOLS else " "
                desc = (t.description or "").strip().splitlines()[0:1]
                print(f"  {marker} {t.name:<24} {desc[0] if desc else ''}")

            found = {t.name for t in tools}
            missing = [name for name in EXPECTED_TOOLS if name not in found]
            if missing:
                print(f"\n⚠  Expected tools not found: {', '.join(missing)}")
                print("   (Adjust EXPECTED_TOOLS / convenience aliases once the real "
                      "surface is known.)")

            path = _freeze_contract(tools)
            print(f"\n✓ Tool contract frozen → {path}")

            if probe:
                name, args = probe
                print(f"\n→ Probing {name}({json.dumps(args, ensure_ascii=False)}) ...")
                result = await client.call_tool(name, args)
                print("✓ Result:")
                print(json.dumps(result, indent=2, ensure_ascii=False)
                      if isinstance(result, (dict, list)) else result)
            return 0

    except MCPDependencyError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(
            f"\n✗ Could not launch '{config.codebase_memory.binary}'.\n"
            "  • Is the Codebase-Memory binary installed and on PATH?\n"
            "  • Set CODEBASE_MEMORY_BIN / CODEBASE_MEMORY_ARGS in your .env.",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"\n✗ MCP session failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _parse_probe(argv: list[str]) -> tuple[str, dict[str, Any]] | None:
    parser = argparse.ArgumentParser(description="Milestone 1: MCP connectivity check.")
    parser.add_argument(
        "--probe",
        nargs=2,
        metavar=("TOOL", "JSON_ARGS"),
        help='Call one tool after connecting, e.g. --probe get_code_snippet \'{"id":"x"}\'',
    )
    ns = parser.parse_args(argv)
    if not ns.probe:
        return None
    name, raw = ns.probe
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"--probe JSON_ARGS is not valid JSON: {exc}")
    if not isinstance(args, dict):
        parser.error("--probe JSON_ARGS must be a JSON object")
    return name, args


def main() -> None:
    probe = _parse_probe(sys.argv[1:])
    raise SystemExit(asyncio.run(_run(probe)))


if __name__ == "__main__":
    main()
