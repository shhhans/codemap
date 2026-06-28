"""Run the full codemap pipeline on an arbitrary external repo.

Generalizes exp_overalert.py: point it at any indexed directory and name two
seed functions (by qualified-name suffix), and it traces both mainlines
concurrently, reviews the crossings (healthy / dangerous / suspected), reports
reflux + token usage, and exports the subway map.

    CODEBASE_MEMORY_BIN=/path/to/codebase-memory-mcp MINIMAX_API_KEY=... \
    python scripts/exp_external.py --repo /path/to/repo \
        --seed-a engine:ExecutionEngine.crawl \
        --seed-b sched:ExecutionEngine._start_scheduled_request \
        --depth 5 --out web/subway_map.json --db .codemap/external.sqlite

`--seed-a` / `--seed-b` are `flow_type:suffix` — `suffix` is matched against
each node's qualified_name (endswith), so `ExecutionEngine.crawl` resolves the
method precisely even amid same-named functions.

`--probe a.b.C.method,...` skips the run and just reports, for each candidate,
the resolved qualified_name and its immediate downstream fan-out — use it to
pick viable seeds before committing to a full (paid) run.
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

PALETTE = ["#1E90FF", "#F4A261", "#2A9D8F", "#E76F51", "#9B5DE5", "#F15BB5", "#00BBF9"]


async def _resolve(mcp: CodebaseMemoryClient, project: str, suffix: str) -> str | None:
    short = suffix.rsplit(".", 1)[-1]
    res = await mcp.search_graph(project, query=suffix.replace(".", " "), limit=25)
    results = res.get("results", []) if isinstance(res, dict) else []
    for r in results:                                   # exact suffix wins
        if (r.get("qualified_name") or "").endswith(suffix):
            return r["qualified_name"]
    for r in results:                                   # else first short-name hit
        if r.get("name") == short:
            return r["qualified_name"]
    return results[0]["qualified_name"] if results else None


async def _downstream_count(mcp: CodebaseMemoryClient, project: str, qn: str) -> int:
    cypher = (f"MATCH (f {{qualified_name:'{qn}'}})-[:CALLS]->(t) "
              "RETURN count(DISTINCT t) AS n")
    res = await mcp.query_graph(project, query=cypher)
    rows = res.get("rows") if isinstance(res, dict) else None
    return int(rows[0][0]) if rows else 0


async def main(args: argparse.Namespace) -> int:
    repo = str(Path(args.repo).resolve())
    print(f"→ Indexing {repo}")
    async with CodebaseMemoryClient.connect() as mcp:
        index = await mcp.index_repository(repo)
        project = index.get("project")
        print(f"  project = {project} ({index.get('nodes')} nodes / {index.get('edges')} edges)")

        if args.probe:
            print("\n═══ Seed probe (resolved qualified_name · immediate downstream) ═══")
            for cand in args.probe.split(","):
                qn = await _resolve(mcp, project, cand.strip())
                n = await _downstream_count(mcp, project, qn) if qn else 0
                print(f"  {cand.strip():45s} → {qn or '<unresolved>'}  [{n} downstream]")
            return 0

        specs = []
        for i, raw in enumerate(args.seed):
            # flow:qualified_suffix[:material]  (material falls back to --material)
            parts = raw.split(":", 2)
            if len(parts) < 2:
                print(f"✗ bad --seed {raw!r}; want flow:suffix[:material]", file=sys.stderr)
                return 4
            flow, suffix = parts[0], parts[1]
            material = parts[2] if len(parts) > 2 else args.material
            qn = await _resolve(mcp, project, suffix)
            if not qn:
                print(f"✗ could not resolve seed {suffix!r}", file=sys.stderr)
                return 4
            print(f"  seed[{flow}] = {qn}  (material: {material})")
            specs.append(SeedSpec(flow_type=flow, seed=qn, material=material,
                                  name=f"{flow} mainline", color=PALETTE[i % len(PALETTE)],
                                  seed_name=suffix.rsplit(".", 1)[-1]))
        try:
            llm = LLMClient(config.llm())
        except LLMError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

        db = Path(args.db)
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            db.unlink()
        bb = Blackboard(db)
        coord = Coordinator(mcp=mcp, llm=llm, blackboard=bb, project=project,
                            max_workers=config.max_workers, max_depth=args.depth)
        print(f"\n→ Tracing {len(specs)} mainlines (depth={args.depth}, "
              f"max_workers={config.max_workers}) ...\n")
        result = await coord.run(specs)

        for flow, w in result.workers.items():
            print(f"  {flow} ({len(w.retained)} stations): "
                  + " → ".join(n.name for n in w.retained[:24])
                  + (" …" if len(w.retained) > 24 else ""))
        if result.fork_events:
            print(f"\n  forks: {len(result.fork_events)}")

        print("\n═══ Crossings ═══")
        icons = {"dangerous": "⚠ dangerous", "suspected": "? suspected", "healthy": "✓ healthy"}
        tally: dict[str, int] = {}
        if not result.reviews:
            print("  (the two mainlines did not cross)")
        for r in sorted(result.reviews, key=lambda r: r.verdict):
            tally[r.verdict] = tally.get(r.verdict, 0) + 1
            print(f"\n  {icons.get(r.verdict, r.verdict)}  {r.name}()  [{' × '.join(r.flows)}]")
            print(f"     {r.description}")
        print(f"\n  tally: {tally}   reflux rounds: {result.reflux_rounds}")

        u = llm.usage.summary()
        print(f"  LLM: {u['calls']} calls, {u['total_tokens']:,} tok "
              f"(cache hit {u['cache_hit_rate'] * 100:.0f}%)")

        out = write_subway_map(db, args.out)
        print(f"\n✓ Subway map → {out}")
        bb.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run the codemap pipeline on an external repo.")
    p.add_argument("--repo", required=True)
    p.add_argument("--seed", action="append", default=[],
                   help="flow_type:qualified_suffix[:material]; repeatable for N mainlines")
    p.add_argument("--material", default="request / response object",
                   help="default tracked material when a --seed omits its own")
    p.add_argument("--depth", type=int, default=5)
    p.add_argument("--out", default="web/subway_map.json")
    p.add_argument("--db", default=".codemap/external.sqlite")
    p.add_argument("--probe", help="comma-separated candidate suffixes; report & exit")
    raise SystemExit(asyncio.run(main(p.parse_args())))
