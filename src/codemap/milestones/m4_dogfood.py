"""Milestone 4 — Dogfooding: codemap traces its own source.

The ultimate validation: point the tool at its *own* repository and trace two
real mainlines through it, then render the subway map. Where the two flows cross
in codemap's own code, the review agent says whether the shared node is a clean
seam (healthy interchange) or a leaked intermediate (responsibility pollution).

Two mainlines, both entering through a milestone's `_run`:
  • "M2 单线追踪入口"  — how a single-flow trace request is processed.
  • "M3 并发编排入口"  — how the concurrent coordinator processes a run.
They are expected to converge on the shared TaintWorker / MCP plumbing.

    python -m codemap.milestones.m4_dogfood
    python -m codemap.milestones.m4_dogfood --depth 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codemap.agents import Coordinator, SeedSpec
from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.export import write_subway_map
from codemap.llm import LLMClient, LLMError
from codemap.mcp_client import CodebaseMemoryClient

MAINLINES = [
    {"flow_type": "m2flow", "seed": "m2_single_dfs._run",
     "material": "单线追踪请求 (seed / flow / material)",
     "name": "M2 单线追踪", "color": "#1E90FF"},
    {"flow_type": "m3flow", "seed": "m3_concurrent._run",
     "material": "并发编排请求 (seeds / 多主线)",
     "name": "M3 并发编排", "color": "#F4A261"},
]


async def _run(args: argparse.Namespace) -> int:
    repo = str(Path(args.repo).resolve())
    print(f"→ Dogfooding: indexing codemap itself at {repo}")
    try:
        async with CodebaseMemoryClient.connect() as mcp:
            index = await mcp.index_repository(repo)
            project = index.get("project") if isinstance(index, dict) else None
            if not project:
                print("✗ Indexing returned no project name.", file=sys.stderr)
                return 4
            print(f"  project = {project} "
                  f"({index.get('nodes')} nodes / {index.get('edges')} edges)")

            seeds: list[SeedSpec] = []
            for ml in MAINLINES:
                qn = await _resolve(mcp, project, ml["seed"])
                if qn is None:
                    print(f"✗ Could not resolve seed {ml['seed']!r}.", file=sys.stderr)
                    return 4
                seeds.append(SeedSpec(flow_type=ml["flow_type"], seed=qn, material=ml["material"],
                                      name=ml["name"], color=ml["color"],
                                      seed_name=ml["seed"].rsplit(".", 1)[-1]))
                print(f"  seed[{ml['flow_type']}] = {qn}")

            try:
                llm = LLMClient(config.llm(args.provider))
            except LLMError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                return 2

            db = Path(args.db)
            if db.exists():
                db.unlink()
            blackboard = Blackboard(db)
            coord = Coordinator(mcp=mcp, llm=llm, blackboard=blackboard, project=project,
                                max_workers=config.max_workers, max_depth=args.depth)
            print(f"\n→ Tracing 2 mainlines through codemap (max_depth={args.depth}) ...\n")
            result = await coord.run(seeds)
            _report(result)

            out = write_subway_map(db, args.out)
            print(f"\n✓ Subway map exported → {out}")
            print("  Render: python scripts/render_subway.py web/subway_map.png")
            blackboard.close()
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


async def _resolve(mcp: CodebaseMemoryClient, project: str, seed: str) -> str | None:
    short = seed.rsplit(".", 1)[-1]
    res = await mcp.search_graph(project, query=seed.replace(".", " "), limit=10)
    if not isinstance(res, dict):
        return None
    results = res.get("results", [])
    # Prefer a hit whose qualified_name ends with the dotted seed.
    for r in results:
        if (r.get("qualified_name") or "").endswith(seed):
            return r["qualified_name"]
    for r in results:
        if r.get("name") == short:
            return r["qualified_name"]
    return results[0]["qualified_name"] if results else None


def _report(result) -> None:
    print("═══ Forks ═══")
    for flow, node, n in result.fork_events or []:
        print(f"  ⑂ [{flow}] forked {n} at {node.rsplit('.', 1)[-1]}()")
    if not result.fork_events:
        print("  (none)")
    for flow, worker in result.workers.items():
        print(f"\n  {flow} ({len(worker.retained)} stations): "
              + " → ".join(n.name for n in worker.retained))
    print("\n═══ Intersections in codemap's own architecture ═══")
    if not result.reviews:
        print("  (none — the two mainlines did not cross)")
    icons = {"dangerous": "⚠ 危险", "suspected": "? 疑似", "healthy": "✓ 健康"}
    for r in result.reviews:
        icon = icons.get(r.verdict, "? 疑似")
        print(f"\n  {icon}  {r.name}()  [{' × '.join(r.flows)}]\n     {r.description}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Milestone 4: dogfood codemap on itself.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--depth", type=int, default=5, help="Max DFS depth (keep modest).")
    parser.add_argument("--db", default=str(config.blackboard_db))
    parser.add_argument("--out", default="web/subway_map.json")
    parser.add_argument("--provider", choices=["minimax", "dashscope"], default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
