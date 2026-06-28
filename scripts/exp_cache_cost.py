"""Prompt-cache cost measurement (ARCHITECTURE risk-table follow-up).

The design bets on prompt caching: every worker `_classify` call shares one big
leading System Prompt and only appends a small sliding window; every reviewer
call shares the (separate) review System Prompt. If the provider actually serves
those shared prefixes from cache, the per-call input cost collapses to the
window. This script *measures* that instead of assuming it.

It runs the focused two-driver trace (the same seeds as exp_overalert.py) end to
end against the real engine + LLM, then prints the run's token accounting from
`LLMClient.usage`: how many input tokens were served from cache, the realized
cache-hit rate, and an illustrative billed-vs-uncached cost.

    CODEBASE_MEMORY_BIN=/path/to/codebase-memory-mcp \
    MINIMAX_API_KEY=... python scripts/exp_cache_cost.py

Prices default to a MiniMax-ish $/Mtok; override with --in-price/--out-price for
your provider. They only scale the illustrative cost — the token counts and the
cache-hit rate are measured, not assumed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codemap.agents import Coordinator, SeedSpec
from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.llm import LLMClient, LLMError
from codemap.mcp_client import CodebaseMemoryClient

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


async def main(args: argparse.Namespace) -> int:
    repo = str(Path(".").resolve())
    print(f"→ Cache-cost experiment: indexing {repo}")
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
            seeds.append(SeedSpec(flow_type=s["flow_type"], seed=qn, material=s["material"],
                                  name=s["name"], color=s["color"],
                                  seed_name=s["ends"].rsplit(".", 1)[-1]))
        try:
            llm = LLMClient(config.llm())
        except LLMError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

        db = Path(".codemap/cachecost.sqlite")
        if db.exists():
            db.unlink()
        bb = Blackboard(db)
        coord = Coordinator(mcp=mcp, llm=llm, blackboard=bb, project=project,
                            max_workers=config.max_workers, max_depth=args.depth)
        print(f"\n→ Tracing two drivers (depth={args.depth}) — all worker/review calls "
              "share their leading System Prompt ...\n")
        result = await coord.run(seeds)
        bb.close()

        u = llm.usage
        s = u.summary()
        print("═══ Token usage (measured) ═══")
        print(f"  LLM calls           : {s['calls']}")
        print(f"  prompt  tokens      : {s['prompt_tokens']:,}")
        print(f"  cached  tokens      : {s['cached_tokens']:,}")
        print(f"  completion tokens   : {s['completion_tokens']:,}")
        print(f"  total   tokens      : {s['total_tokens']:,}")
        print(f"  ▶ cache hit rate    : {s['cache_hit_rate'] * 100:.1f}%  "
              "(share of input tokens served from cache)")

        cost = u.cost(args.in_price, args.out_price, cached_discount=args.cached_discount)
        print("\n═══ Illustrative cost "
              f"(in=${args.in_price}/Mtok, out=${args.out_price}/Mtok, "
              f"cached@{args.cached_discount:g}×) ═══")
        print(f"  billed (with cache) : ${cost['billed_usd']:.6f}")
        print(f"  if no cache         : ${cost['no_cache_usd']:.6f}")
        print(f"  ▶ saved by cache    : ${cost['saved_usd']:.6f}")
        if s["cached_tokens"] == 0:
            print("\n  NOTE: provider reported 0 cached tokens — either the endpoint does not "
                  "surface\n        prompt_tokens_details.cached_tokens, or caching did not "
                  "engage for this run.")
        print(f"\n  crossings reviewed  : {len(result.reviews)}  "
              f"(reflux rounds: {result.reflux_rounds})")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Measure prompt-cache savings over a real run.")
    p.add_argument("--depth", type=int, default=4)
    p.add_argument("--in-price", type=float, default=0.30, help="$ per Mtok input")
    p.add_argument("--out-price", type=float, default=1.20, help="$ per Mtok output")
    p.add_argument("--cached-discount", type=float, default=0.1,
                   help="cached input billed at this fraction of input price")
    raise SystemExit(asyncio.run(main(p.parse_args())))
