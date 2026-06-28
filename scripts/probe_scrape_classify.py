"""Probe: does the classifier stably drop _scrape's business-critical children?

Failure A (see analysis): at scrapy's Scraper._scrape the static graph offers all
8 callees, including the two nodes that carry the scraped payload forward
(call_spider_async → produces Items, handle_spider_output_async → routes them to
engine.crawl / item pipeline). Yet our trace kept only scrape_response_async.

This script feeds _scrape's REAL candidate set to the classifier N times at a
given temperature and tallies which candidates survive as `continue`, so we can
see whether the mis-prune is deterministic (temp 0) and how systematic it is
(temp > 0). It does NOT touch the blackboard — pure classify probe.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from codemap.agents.worker import TaintWorker
from codemap.config import config
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient

REPO = ("/tmp/claude-0/-home-user-codemap/"
        "c7b83df4-8fd5-5310-a7df-4e4fc55a896a/scratchpad/src_repos/scrapy-2.16.0")
MATERIAL = ("a downloaded Response being parsed by the spider into scraped Items "
            "and follow-up Requests")
# The business-critical children we expect a correct classifier to KEEP.
CRITICAL = {"call_spider_async", "handle_spider_output_async"}


async def main(temp: float, runs: int) -> int:
    async with CodebaseMemoryClient.connect() as mcp:
        index = await mcp.index_repository(REPO)
        project = index["project"]
        scrape = f"{project}.scrapy.core.scraper.Scraper._scrape"
        llm = LLMClient(config.llm())
        worker = TaintWorker(mcp=mcp, llm=llm, project=project,
                             flow_type="scrape", material=MATERIAL)

        candidates = await worker._downstream(scrape)
        print(f"_scrape has {len(candidates)} static candidates:")
        for c in candidates:
            print(f"   - {c.name}")
        from codemap.prompts import build_window, SYSTEM_PROMPT

        window = build_window(flow_type="scrape", material=MATERIAL,
                              current_node="_scrape", current_file=None,
                              candidates=candidates)

        kept_counter: Counter[str] = Counter()
        critical_survival = 0
        print(f"\n=== {runs} run(s) at temperature={temp} ===")
        for i in range(runs):
            reply = await asyncio.to_thread(llm.chat, SYSTEM_PROMPT, window, temperature=temp)
            try:
                decisions = reply.json().get("decisions", [])
            except Exception:  # noqa: BLE001
                decisions = []
            by_ord = {str(j): c for j, c in enumerate(candidates, 1)}
            by_ref = {c.ref: c for c in candidates}
            kept = []
            for d in decisions:
                if d.get("verdict") == "continue":
                    cand = by_ref.get(str(d.get("node"))) or by_ord.get(str(d.get("node")))
                    if cand:
                        kept.append(cand.name)
            for k in kept:
                kept_counter[k] += 1
            got_critical = CRITICAL & set(kept)
            if CRITICAL <= set(kept):
                critical_survival += 1
            print(f"  run {i+1}: kept {sorted(kept)}   "
                  f"[critical kept: {sorted(got_critical) or 'NONE'}]")

        print("\n=== survival tally across runs ===")
        for name, n in kept_counter.most_common():
            flag = " ★critical" if name in CRITICAL else ""
            print(f"   {name:32s} {n}/{runs}{flag}")
        print(f"\nboth critical children kept in {critical_survival}/{runs} runs")
        print(f"LLM: {llm.usage.summary()}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--temp", type=float, default=0.0)
    p.add_argument("--runs", type=int, default=1)
    raise SystemExit(asyncio.run(main(p.parse_args().temp, p.parse_args().runs)))
