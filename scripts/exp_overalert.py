"""Over-alert reproduction experiment (V2.1 validation).

The dogfooding report (docs/agent_exploration_report.md §4) reproduced the
over-alert by tracing two *focused* seeds that both drive the shared worker
engine:

    TaintWorker._walk  ×  Coordinator._scheduler   (depth 4)

Under the OLD binary rule, the 8 shared primitives they converge on
(expand_one / _downstream / _classify / _signature / _recover_dynamic /
_as_dict / query_graph / build_window) were ALL judged `dangerous` — false
positives, because those are public primitives designed for reuse.

This script runs the same focused seeds through the V2.1 evidence-based ternary
reviewer and prints the verdict per crossing, so we can see whether the shared
primitives now read as healthy / suspected (correct) instead of dangerous.

    CODEBASE_MEMORY_BIN=/path/to/codebase-memory-mcp python scripts/exp_overalert.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from codemap.agents import Coordinator, SeedSpec
from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.export import write_subway_map
from codemap.llm import LLMClient, LLMError
from codemap.mcp_client import CodebaseMemoryClient

# Focused seeds: the two drivers of the shared worker engine.
SEEDS = [
    {"flow_type": "walk", "ends": ".TaintWorker._walk", "material": "node / candidate",
     "name": "Recursive Driver (_walk)", "color": "#1E90FF"},
    {"flow_type": "sched", "ends": ".Coordinator._scheduler", "material": "frontier task / node",
     "name": "Concurrent Driver (_scheduler)", "color": "#F4A261"},
]


async def _resolve(mcp: CodebaseMemoryClient, project: str, ends: str) -> str | None:
    short = ends.rsplit(".", 1)[-1]
    res = await mcp.search_graph(project, query=ends.replace(".", " ").strip(), limit=15)
    results = res.get("results", []) if isinstance(res, dict) else []
    for r in results:
        if (r.get("qualified_name") or "").endswith(ends):
            return r["qualified_name"]
    for r in results:
        if r.get("name") == short:
            return r["qualified_name"]
    return results[0]["qualified_name"] if results else None


async def main() -> int:
    repo = str(Path(".").resolve())
    print(f"→ Over-alert experiment: indexing {repo}")
    async with CodebaseMemoryClient.connect() as mcp:
        index = await mcp.index_repository(repo)
        project = index.get("project")
        print(f"  project = {project} ({index.get('nodes')} nodes / {index.get('edges')} edges)")

        seeds: list[SeedSpec] = []
        for s in SEEDS:
            qn = await _resolve(mcp, project, s["ends"])
            if not qn:
                print(f"✗ could not resolve {s['ends']!r}", file=sys.stderr)
                return 4
            print(f"  seed[{s['flow_type']}] = {qn}")
            seeds.append(SeedSpec(flow_type=s["flow_type"], seed=qn, material=s["material"],
                                  name=s["name"], color=s["color"],
                                  seed_name=s["ends"].rsplit(".", 1)[-1]))
        try:
            llm = LLMClient(config.llm())
        except LLMError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

        db = Path(".codemap/overalert.sqlite")
        if db.exists():
            db.unlink()
        bb = Blackboard(db)
        coord = Coordinator(mcp=mcp, llm=llm, blackboard=bb, project=project,
                            max_workers=config.max_workers, max_depth=4)
        print("\n→ Tracing _walk × _scheduler (depth=4) ...\n")
        result = await coord.run(seeds)

        for flow, w in result.workers.items():
            print(f"  {flow} ({len(w.retained)} stations): "
                  + " → ".join(n.name for n in w.retained))

        print("\n═══ Crossings on shared worker primitives ═══")
        icons = {"dangerous": "⚠ dangerous", "suspected": "? suspected", "healthy": "✓ healthy"}
        tally: dict[str, int] = {}
        if not result.reviews:
            print("  (the two drivers did not cross)")
        for r in sorted(result.reviews, key=lambda r: r.verdict):
            tally[r.verdict] = tally.get(r.verdict, 0) + 1
            print(f"\n  {icons.get(r.verdict, r.verdict)}  {r.name}()  [{' × '.join(r.flows)}]")
            print(f"     {r.description}")
        print(f"\n  tally: {tally}")

        write_subway_map(db, "web/subway_map.json")
        bb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
