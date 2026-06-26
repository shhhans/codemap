"""Milestone 3 — Blackboard + concurrent forking + intersection alarm.

Runs two mainlines (Auth + Billing) concurrently over the fixture app, lets them
fork independent workers at branch points, detects where they cross on the shared
Blackboard, and wakes the ReviewAgent to flag responsibility pollution. Then it
exports the subway map so the dangerous crossing shows up (red) next to the
healthy one (green).

    python -m codemap.milestones.m3_concurrent
    python -m codemap.milestones.m3_concurrent --repo fixtures/sample_app

Pair with:
    python -m codemap.export --db .codemap/blackboard.sqlite --out web/subway_map.json
    python scripts/render_subway.py web/subway_map.png
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

# The two mainlines we trace through the fixture. Line colors are kept distinct
# from the intersection rings (red=dangerous, green=healthy).
MAINLINES = [
    {"flow_type": "auth", "seed": "login", "material": "authorization header / token",
     "name": "Auth Mainline", "color": "#1E90FF"},
    {"flow_type": "billing", "seed": "charge", "material": "authorization header / amount",
     "name": "Billing Mainline", "color": "#F4A261"},
]


async def _run(args: argparse.Namespace) -> int:
    repo = str(Path(args.repo).resolve())
    print(f"→ Indexing fixture: {repo}")
    try:
        async with CodebaseMemoryClient.connect() as mcp:
            index = await mcp.index_repository(repo)
            project = index.get("project") if isinstance(index, dict) else None
            if not project:
                print("✗ Indexing did not return a project name.", file=sys.stderr)
                return 4
            print(f"  project = {project}")

            seeds: list[SeedSpec] = []
            for ml in MAINLINES:
                qn = await _resolve(mcp, project, ml["seed"])
                if qn is None:
                    print(f"✗ Could not resolve seed {ml['seed']!r}.", file=sys.stderr)
                    return 4
                seeds.append(SeedSpec(
                    flow_type=ml["flow_type"], seed=qn, material=ml["material"],
                    name=ml["name"], color=ml["color"], seed_name=ml["seed"],
                ))
                print(f"  seed[{ml['flow_type']}] = {qn}")

            try:
                llm = LLMClient(config.llm(args.provider))
            except LLMError as exc:
                print(f"✗ {exc}", file=sys.stderr)
                return 2

            # Fresh blackboard each run so the demo is reproducible.
            db = Path(args.db)
            if db.exists():
                db.unlink()
            blackboard = Blackboard(db)

            coord = Coordinator(mcp=mcp, llm=llm, blackboard=blackboard, project=project,
                                max_workers=config.max_workers, max_depth=config.max_depth)
            print(f"\n→ Running {len(seeds)} mainlines concurrently "
                  f"(max_workers={config.max_workers}) ...\n")
            result = await coord.run(seeds)

            _report(result, blackboard)

            out_json = write_subway_map(db, args.out)
            print(f"\n✓ Subway map exported → {out_json}")
            print(f"  Render with: python scripts/render_subway.py web/subway_map.png")
            blackboard.close()
            return 0

    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


async def _resolve(mcp: CodebaseMemoryClient, project: str, short: str) -> str | None:
    res = await mcp.search_graph(project, query=short, limit=8)
    if isinstance(res, dict):
        results = res.get("results", [])
        for r in results:  # prefer exact short-name match
            if r.get("name") == short:
                return r.get("qualified_name")
        if results:
            return results[0].get("qualified_name")
    return None


def _report(result, blackboard: Blackboard) -> None:
    print("═══ Concurrency / Forks ═══")
    if result.fork_events:
        for flow, node, n in result.fork_events:
            print(f"  ⑂ [{flow}] forked {n} workers at {node.rsplit('.', 1)[-1]}()")
    else:
        print("  (no forks)")

    for flow in ("auth", "billing"):
        w = result.workers.get(flow)
        if w:
            stations = " → ".join(n.name for n in w.retained)
            print(f"\n  {flow} mainline ({len(w.retained)} stations): {stations}")

    print("\n═══ Intersections (换乘枢纽) ═══")
    if not result.reviews:
        print("  (none detected)")
    for r in result.reviews:
        icon = "⚠ 危险交叉 / 职责污染" if r.verdict == "dangerous" else "✓ 健康交叉"
        print(f"\n  {icon}  —  {r.name}()   [{' × '.join(r.flows)}]")
        print(f"     {r.description}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Milestone 3: concurrent forking + intersections.")
    parser.add_argument("--repo", default="fixtures/sample_app")
    parser.add_argument("--db", default=str(config.blackboard_db))
    parser.add_argument("--out", default="web/subway_map.json")
    parser.add_argument("--provider", choices=["minimax", "dashscope"], default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
