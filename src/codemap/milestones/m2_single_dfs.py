"""Milestone 2 — Single-mainline DFS taint tracker.

Wires the System Prompt + dynamic sliding window to a real DFS over the
Codebase-Memory graph: pick a seed, track one material down the call graph, let
the LLM prune barriers/noise, and print the retained mainline path.

By default it dogfoods — indexes this very repo and traces a mainline through
the agent layer — so a single command exercises MCP + LLM + Blackboard end to end.

    # after MINIMAX_API_KEY is set:
    python -m codemap.milestones.m2_single_dfs
    python -m codemap.milestones.m2_single_dfs \
        --repo /path/to/repo --seed main --flow Auth \
        --material "authorization header / user_id"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codemap.agents import TaintWorker
from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.llm import LLMClient, LLMError
from codemap.mcp_client import CodebaseMemoryClient


async def _run(args: argparse.Namespace) -> int:
    repo = str(Path(args.repo).resolve())
    print(f"→ Connecting to Codebase-Memory and indexing: {repo}")
    try:
        async with CodebaseMemoryClient.connect() as mcp:
            index = await mcp.index_repository(repo)
            project = _project_name(index, repo)
            print(f"  indexed project = {project}")

            seed_qn = await _resolve_seed(mcp, project, args.seed)
            if seed_qn is None:
                print(f"✗ Could not resolve a seed for {args.seed!r} in {project}.",
                      file=sys.stderr)
                return 4
            print(f"  seed = {seed_qn}\n")

            try:
                llm = LLMClient(config.llm(args.provider))
            except LLMError as exc:
                print(f"✗ {exc}\n  Run `python -m codemap.milestones.llm_check` first.",
                      file=sys.stderr)
                return 2

            blackboard = Blackboard(config.blackboard_db)
            blackboard.register_mainline(
                f"line_{args.flow.lower()}", args.flow.lower(), f"{args.flow} Mainline",
                args.color,
            )
            worker = TaintWorker(
                mcp=mcp, llm=llm, project=project, flow_type=args.flow.lower(),
                material=args.material, blackboard=blackboard, max_depth=config.max_depth,
            )

            print(f"→ Tracing '{args.flow}' mainline (material: {args.material}) ...\n")
            retained = await worker.trace(seed_qn)
            _print_path(retained, worker.pruned)
            blackboard.close()
            return 0

    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        print(f"\n✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _project_name(index_result: object, repo: str) -> str:
    if isinstance(index_result, dict) and index_result.get("project"):
        return index_result["project"]
    # Fallback: Codebase-Memory derives the name from the path.
    return Path(repo).as_posix().strip("/").replace("/", "-")


async def _resolve_seed(mcp: CodebaseMemoryClient, project: str, seed: str) -> str | None:
    """Accept either a qualified_name or a fuzzy term; return a qualified_name."""
    if "." in seed and seed.startswith(project):
        return seed
    res = await mcp.search_graph(project, query=seed, limit=5)
    if isinstance(res, dict):
        results = res.get("results", [])
        # Prefer an exact short-name match, else the top hit.
        for r in results:
            if r.get("name") == seed:
                return r.get("qualified_name")
        if results:
            return results[0].get("qualified_name")
    return None


def _print_path(retained, pruned) -> None:
    print("═══ Retained mainline (subway stations) ═══")
    for n in retained:
        indent = "  " * n.depth
        tag = {"source": "◉ SOURCE", "sink": "■ SINK ", "processor": "● stop "}.get(
            n.role, "● stop "
        )
        conf = f"{n.confidence:.2f}"
        print(f"{indent}{tag} [{conf}] {n.name}")
        if n.reason and n.role != "source":
            print(f"{indent}         ↳ {n.reason}")
    print(f"\n  retained nodes: {len(retained)}")

    if pruned:
        print("\n═══ Pruned (not part of the mainline) ═══")
        for name, verdict, reason in pruned:
            print(f"  ✕ [{verdict}] {name} — {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Milestone 2: single-mainline DFS taint tracker.")
    parser.add_argument("--repo", default=".", help="Repository to index & trace (default: cwd).")
    parser.add_argument("--seed", default="main", help="Seed function (name or qualified_name).")
    parser.add_argument("--flow", default="Trace", help="Mainline name, e.g. Auth.")
    parser.add_argument("--material", default="input payload",
                        help="The tracked material (Token), e.g. 'authorization header'.")
    parser.add_argument("--color", default="#3366FF", help="Subway line color.")
    parser.add_argument("--provider", choices=["minimax", "dashscope"], default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
