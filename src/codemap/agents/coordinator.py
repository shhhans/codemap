"""Coordinator — concurrent multi-mainline exploration with forking (Milestone 3).

Drives several mainlines (Auth, Billing, …) at once. Each mainline has its own
TaintWorker (sharing the Blackboard), and a bounded pool of scheduler coroutines
pulls nodes off a shared frontier queue. Expanding a node can yield several
children — each becomes an independent frontier item, which is the Fork: those
children may then be processed concurrently by different pool slots.

Dedup/cycle-breaking is handled by the Blackboard: log_trace is idempotent per
(node, flow), so a node already claimed by a mainline is never expanded twice,
even under concurrency (SQLite serializes the conflicting insert).

When two mainlines check in on the same node, it surfaces in the Blackboard's
`intersections` view; the Coordinator then wakes the ReviewAgent to characterize
each crossing as healthy or dangerous (responsibility pollution).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from codemap.agents.review import ReviewAgent, ReviewResult
from codemap.agents.worker import TaintWorker
from codemap.blackboard import Blackboard
from codemap.llm import LLMClient
from codemap.mcp_client import CodebaseMemoryClient


@dataclass
class SeedSpec:
    flow_type: str           # "auth"
    seed: str                # qualified_name of the entry function
    material: str            # tracked token, e.g. "authorization header"
    name: str                # "Auth Mainline"
    color: str               # "#FF4D4F"
    seed_name: str | None = None
    seed_file: str | None = None


@dataclass
class CoordinatorResult:
    workers: dict[str, TaintWorker]
    fork_events: list[tuple[str, str, int]]      # (flow, parent_node, n_children)
    reviews: list[ReviewResult]
    reflux_rounds: int = 0                        # how many reinjection rounds ran
    reflux_events: list[tuple[int, list[str]]] = field(default_factory=list)


@dataclass
class Coordinator:
    mcp: CodebaseMemoryClient
    llm: LLMClient
    blackboard: Blackboard
    project: str
    max_workers: int = 8
    max_depth: int = 12
    # Global termination guard for the review→reflux→re-review loop (doc §3.5):
    # a crossing the LLM can't resolve triggers at most this many reinjection
    # rounds before its provisional `suspected` is made final.
    max_reflux_rounds: int = 2

    _queue: "asyncio.Queue[tuple[str, str, int]]" = field(default_factory=asyncio.Queue, init=False)
    _workers: dict[str, TaintWorker] = field(default_factory=dict, init=False)
    _forks: list[tuple[str, str, int]] = field(default_factory=list, init=False)

    async def run(self, seeds: list[SeedSpec]) -> CoordinatorResult:
        # Set up one worker + subway line per mainline, and seed the frontier.
        for i, spec in enumerate(seeds):
            self.blackboard.register_mainline(
                f"line_{spec.flow_type}", spec.flow_type, spec.name, spec.color
            )
            worker = TaintWorker(
                mcp=self.mcp, llm=self.llm, project=self.project, flow_type=spec.flow_type,
                material=spec.material, blackboard=self.blackboard, max_depth=self.max_depth,
                agent_id=f"{spec.flow_type}-root",
            )
            self._workers[spec.flow_type] = worker
            await worker.record_source(spec.seed, name=spec.seed_name, file_path=spec.seed_file)
            self._queue.put_nowait((spec.flow_type, spec.seed, 0))

        await self._drain()  # round 0: explore the frontier to convergence

        # Characterize the crossings two mainlines created.
        reviewer = ReviewAgent(mcp=self.mcp, llm=self.llm, blackboard=self.blackboard,
                               project=self.project)
        reviews = await reviewer.review_all()
        reviews, rounds, events = await self._reflux(reviewer, reviews)
        return CoordinatorResult(workers=dict(self._workers), fork_events=list(self._forks),
                                 reviews=reviews, reflux_rounds=rounds, reflux_events=events)

    async def _reflux(self, reviewer: ReviewAgent, reviews: list[ReviewResult]
                      ) -> tuple[list[ReviewResult], int, list[tuple[int, list[str]]]]:
        """Coordinator-level反刍: re-inject the nodes the reviewer's LLM still
        wanted to see, re-explore, and re-review only those crossings. Bounded by
        `max_reflux_rounds` so an unresolvable crossing settles on suspected."""
        by_id = {r.node_id: r for r in reviews}
        events: list[tuple[int, list[str]]] = []
        rounds = 0
        while rounds < self.max_reflux_rounds:
            pending = [r.reflux for r in by_id.values() if r.reflux]
            if not pending:
                break
            rounds += 1
            targets: set[str] = set()
            for req in pending:
                # Re-inject the LLM-pointed target onto the frontier for each
                # involved flow. A node already in the flow's trace re-enters at
                # its known depth (explores any unexpanded subtree); a node the
                # flows never reached enters fresh (depth 0, bounded by max_depth).
                depth = self.blackboard.node_min_depth(req.target)
                for flow in req.flows:
                    if flow in self._workers:
                        self._queue.put_nowait((flow, req.target, depth))
                targets.add(req.node_id)
            events.append((rounds, sorted(targets)))
            await self._drain()
            for r in await reviewer.review_all(targets=targets):
                by_id[r.node_id] = r  # replace the re-reviewed crossings in place
        # Order preserved from the original review for a stable report.
        return [by_id[r.node_id] for r in reviews], rounds, events

    async def _drain(self) -> None:
        """Spin a bounded scheduler pool, consume the frontier to convergence,
        then tear the pool down. Re-callable across reflux rounds."""
        if self._queue.empty():
            return
        pool = [asyncio.create_task(self._scheduler(n)) for n in range(self.max_workers)]
        await self._queue.join()
        for task in pool:
            task.cancel()
        await asyncio.gather(*pool, return_exceptions=True)

    async def _scheduler(self, slot: int) -> None:
        """One pool slot: pull a node, expand it, enqueue its children (forks)."""
        while True:
            flow_type, qualified_name, depth = await self._queue.get()
            try:
                worker = self._workers[flow_type]
                worker.agent_id = f"{flow_type}-slot{slot}"
                children = await worker.expand_one(qualified_name, depth)
                if len(children) > 1:
                    self._forks.append((flow_type, qualified_name, len(children)))
                for child_qn, child_depth in children:
                    self._queue.put_nowait((flow_type, child_qn, child_depth))
            except Exception:  # noqa: BLE001 - one bad node must not stall the pool
                pass
            finally:
                self._queue.task_done()
