"""Real-pollution regression — the V2.1 reviewer must STILL catch genuine 越级摄取.

The over-alert fix made the reviewer reluctant to cry "dangerous" on shared
primitives. This guards the other direction: a real responsibility-pollution
crossing must not slip through as healthy/suspected.

It traces the canonical fixture (fixtures/sample_app) where Billing crosses Auth
twice:

  • parse_jwt        — Billing reaches Auth's INTERNAL parsing step directly,
                       bypassing the verify_token façade  → must be `dangerous`
  • get_current_user — both consume Auth's stable sink     → must be `healthy`

Exit code is non-zero if the expected verdicts don't hold, so this doubles as a
live regression check against the real engine + LLM.

    CODEBASE_MEMORY_BIN=/path/to/codebase-memory-mcp \
    MINIMAX_API_KEY=... python scripts/exp_pollution.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from codemap.agents import Coordinator, SeedSpec
from codemap.blackboard import Blackboard
from codemap.config import config
from codemap.llm import LLMClient, LLMError
from codemap.mcp_client import CodebaseMemoryClient

REPO = "fixtures/sample_app"
MAINLINES = [
    {"flow_type": "auth", "seed": "login", "material": "authorization header / token",
     "name": "Auth Mainline", "color": "#1E90FF"},
    {"flow_type": "billing", "seed": "charge", "material": "authorization header / amount",
     "name": "Billing Mainline", "color": "#F4A261"},
]
# What we expect the reviewer to conclude (the regression contract).
EXPECT = {"parse_jwt": "dangerous", "get_current_user": "healthy"}


async def _resolve(mcp: CodebaseMemoryClient, project: str, short: str) -> str | None:
    res = await mcp.search_graph(project, query=short, limit=8)
    results = res.get("results", []) if isinstance(res, dict) else []
    for r in results:
        if r.get("name") == short:
            return r.get("qualified_name")
    return results[0].get("qualified_name") if results else None


async def main() -> int:
    repo = str(Path(REPO).resolve())
    print(f"→ Pollution regression: indexing {repo}")
    async with CodebaseMemoryClient.connect() as mcp:
        index = await mcp.index_repository(repo)
        project = index.get("project")
        print(f"  project = {project} ({index.get('nodes')} nodes / {index.get('edges')} edges)")

        seeds: list[SeedSpec] = []
        for ml in MAINLINES:
            qn = await _resolve(mcp, project, ml["seed"])
            if not qn:
                print(f"✗ could not resolve seed {ml['seed']!r}", file=sys.stderr)
                return 4
            seeds.append(SeedSpec(flow_type=ml["flow_type"], seed=qn, material=ml["material"],
                                  name=ml["name"], color=ml["color"], seed_name=ml["seed"]))
            print(f"  seed[{ml['flow_type']}] = {qn}")
        try:
            llm = LLMClient(config.llm())
        except LLMError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2

        db = Path(".codemap/pollution.sqlite")
        if db.exists():
            db.unlink()
        bb = Blackboard(db)
        coord = Coordinator(mcp=mcp, llm=llm, blackboard=bb, project=project,
                            max_workers=config.max_workers, max_depth=config.max_depth)
        print("\n→ Tracing Auth × Billing ...\n")
        result = await coord.run(seeds)

        icons = {"dangerous": "⚠ dangerous", "suspected": "? suspected", "healthy": "✓ healthy"}
        verdict_by_name: dict[str, str] = {}
        for r in result.reviews:
            verdict_by_name[r.name] = r.verdict
            print(f"  {icons.get(r.verdict, r.verdict)}  {r.name}()  [{' × '.join(r.flows)}]")
            print(f"     {r.description}")
        bb.close()

    print("\n═══ Regression contract ═══")
    ok = True
    for name, want in EXPECT.items():
        got = verdict_by_name.get(name, "<not crossed>")
        mark = "✓" if got == want else "✗"
        if got != want:
            ok = False
        print(f"  {mark} {name}: expected {want}, got {got}")
    print("\nPASS — real pollution still detected." if ok else "\nFAIL — regression!")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
