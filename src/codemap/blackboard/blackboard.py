"""Blackboard — thin, dependency-free wrapper over the SQLite shared memory.

Workers `upsert_node` + `log_trace` ("打卡") as they explore; the Coordinator and
review agent read `intersections()` to find and characterize crossings. The
`log_trace` UNIQUE(node_id, flow_type) constraint makes check-ins idempotent and
gives us a free per-mainline visited-set for DFS dedup / cycle breaking.

Only the stdlib `sqlite3` is used, so the blackboard has zero install cost and
can be inspected with any sqlite CLI.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class Node:
    id: str
    name: str
    type: str = "processor"
    file_path: str | None = None
    snippet: str | None = None


@dataclass
class Trace:
    agent_id: str
    node_id: str
    flow_type: str
    node_role: str = "processor"
    confidence: float = 1.0
    depth: int = 0
    # The predecessor this flow arrived from (None for the seed). Stored so the
    # reviewer can reconstruct each mainline's path to a crossing (V2.1).
    parent_node_id: str | None = None


@dataclass
class Intersection:
    node_id: str
    flow_count: int
    flow_types: list[str]


class Blackboard:
    """Concurrency note: SQLite serializes writes; we open in WAL mode so many
    Worker readers don't block the writer. Each Worker should hold its own
    Blackboard instance (its own connection)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # ── Nodes ──────────────────────────────────────────────────────────────
    def upsert_node(self, node: Node) -> None:
        self._conn.execute(
            """
            INSERT INTO nodes (id, name, type, file_path, snippet)
            VALUES (:id, :name, :type, :file_path, :snippet)
            ON CONFLICT(id) DO UPDATE SET
                name      = excluded.name,
                type      = excluded.type,
                file_path = coalesce(excluded.file_path, nodes.file_path),
                snippet   = coalesce(excluded.snippet, nodes.snippet)
            """,
            node.__dict__,
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    # ── Traces (打卡) ──────────────────────────────────────────────────────
    def log_trace(self, trace: Trace) -> bool:
        """Record that `flow_type` passed through `node_id`. Idempotent.

        Returns True if this is a *new* (node, flow) check-in, False if the
        mainline had already visited the node (i.e. the caller hit the visited
        set and should stop expanding that branch).
        """
        cur = self._conn.execute(
            """
            INSERT INTO traces
                (agent_id, node_id, flow_type, node_role, confidence, depth, parent_node_id)
            VALUES
                (:agent_id, :node_id, :flow_type, :node_role, :confidence, :depth, :parent_node_id)
            ON CONFLICT(node_id, flow_type) DO NOTHING
            """,
            trace.__dict__,
        )
        return cur.rowcount > 0

    def has_visited(self, node_id: str, flow_type: str) -> bool:
        """Visited-set check for DFS dedup / cycle breaking."""
        row = self._conn.execute(
            "SELECT 1 FROM traces WHERE node_id = ? AND flow_type = ? LIMIT 1",
            (node_id, flow_type),
        ).fetchone()
        return row is not None

    # ── Intersections (换乘枢纽) ───────────────────────────────────────────
    def intersections(self) -> list[Intersection]:
        rows = self._conn.execute("SELECT * FROM intersections").fetchall()
        return [
            Intersection(
                node_id=r["node_id"],
                flow_count=r["flow_count"],
                flow_types=(r["flow_types"] or "").split(","),
            )
            for r in rows
        ]

    def record_verdict(self, node_id: str, verdict: str, description: str = "") -> None:
        # V2.1: a third state. 'suspected' (黄) is the honest fallback whenever the
        # LLM judge is unavailable/unparseable or the evidence is low-confidence —
        # deterministic logic never hard-judges 'dangerous'.
        if verdict not in {"healthy", "dangerous", "suspected"}:
            raise ValueError(
                f"verdict must be 'healthy', 'dangerous' or 'suspected', got {verdict!r}"
            )
        self._conn.execute(
            """
            INSERT INTO intersection_verdicts (node_id, verdict, description)
            VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                verdict = excluded.verdict, description = excluded.description,
                reviewed_at = unixepoch('subsec')
            """,
            (node_id, verdict, description),
        )

    # ── Mainlines (presentation metadata) ──────────────────────────────────
    def register_mainline(self, line_id: str, flow_type: str, name: str, color: str) -> None:
        self._conn.execute(
            """
            INSERT INTO mainlines (id, flow_type, name, color)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                flow_type = excluded.flow_type, name = excluded.name, color = excluded.color
            """,
            (line_id, flow_type, name, color),
        )

    def roles_for_node(self, node_id: str) -> list[tuple[str, str]]:
        """Per-flow roles recorded for a node: [(flow_type, node_role), ...].

        Drives intersection health: a node logged as 'processor' (intermediate
        step) by some mainline, yet crossed by another, is responsibility
        pollution; one that is a 'sink' for all is a healthy crossing.
        """
        rows = self._conn.execute(
            "SELECT flow_type, node_role FROM traces WHERE node_id = ? ORDER BY flow_type",
            (node_id,),
        ).fetchall()
        return [(r["flow_type"], r["node_role"]) for r in rows]

    def nodes_for_flow(self, flow_type: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT node_id FROM traces WHERE flow_type = ? ORDER BY depth, id",
            (flow_type,),
        ).fetchall()
        return [r["node_id"] for r in rows]

    # ── V2.1: evidence the intersection reviewer needs ─────────────────────
    def path_to_node(self, node_id: str, flow_type: str) -> list[str]:
        """Reconstruct `flow_type`'s path from its seed to `node_id`.

        Walks the `parent_node_id` pointers (the per-flow trace tree) upward and
        returns the path seed → … → node_id. Empty if the flow never reached the
        node. This is the data the façade-bypass check consumes: did the *other*
        mainline pass through a node on this path, or jump straight to the inside?
        """
        rows = self._conn.execute(
            """
            WITH RECURSIVE path(node_id, parent_node_id, lvl) AS (
                SELECT node_id, parent_node_id, 0 FROM traces
                  WHERE node_id = :nid AND flow_type = :flow
              UNION ALL
                SELECT t.node_id, t.parent_node_id, p.lvl + 1
                  FROM traces t JOIN path p ON t.node_id = p.parent_node_id
                 WHERE t.flow_type = :flow
            )
            SELECT node_id FROM path ORDER BY lvl DESC
            """,
            {"nid": node_id, "flow": flow_type},
        ).fetchall()
        return [r["node_id"] for r in rows]

    def flow_node_roles(self, flow_type: str) -> list[tuple[str, str]]:
        """[(node_id, node_role), ...] for one mainline — used to find façade
        candidates (the flow's own nodes that might wrap an internal one)."""
        rows = self._conn.execute(
            "SELECT node_id, node_role FROM traces WHERE flow_type = ? ORDER BY depth, id",
            (flow_type,),
        ).fetchall()
        return [(r["node_id"], r["node_role"]) for r in rows]

    def node_min_depth(self, node_id: str) -> int:
        """Shallowest depth any flow reached this node — used to review crossings
        in topological (seed-outward) order so a deeper crossing can inherit the
        verdict of a shared upstream crossing all its flows passed through."""
        row = self._conn.execute(
            "SELECT MIN(depth) AS d FROM traces WHERE node_id = ?", (node_id,)
        ).fetchone()
        return int(row["d"]) if row and row["d"] is not None else 0

    def get_verdict(self, node_id: str) -> str | None:
        """The recorded verdict for a crossing, or None if not yet reviewed."""
        row = self._conn.execute(
            "SELECT verdict FROM intersection_verdicts WHERE node_id = ?", (node_id,)
        ).fetchone()
        return row["verdict"] if row else None

    def min_confidence(self, node_id: str) -> float:
        """Lowest taint-decision confidence recorded for a node across all flows.

        A recovered (dynamic-dispatch) edge already carries a ×0.8 discount, so a
        low value here flags an evidentiarily weak crossing → downgrade to
        'suspected' rather than hard-judging it."""
        row = self._conn.execute(
            "SELECT MIN(confidence) AS c FROM traces WHERE node_id = ?", (node_id,)
        ).fetchone()
        return float(row["c"]) if row and row["c"] is not None else 1.0

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            file_path=row["file_path"],
            snippet=row["snippet"],
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Blackboard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
