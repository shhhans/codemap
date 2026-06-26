"""Export the Blackboard into the subway-map JSON contract (Milestone 4 groundwork).

Reads the persisted nodes/traces/intersection verdicts and emits the JSON shape
defined in docs/subway_map.schema.json — the single artifact the renderer
consumes. Kept separate from the agents so visualization never reaches back into
the live exploration.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# Node role (as logged by the worker) → subway-map node type.
_ROLE_TO_TYPE = {"source": "source", "sink": "sink", "barrier": "barrier", "processor": "processor"}


def export_subway_map(db_path: str | Path) -> dict[str, Any]:
    """Build the subway-map dict from a Blackboard SQLite file."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    mainlines: list[dict[str, Any]] = []
    for m in conn.execute("SELECT id, flow_type, name, color FROM mainlines ORDER BY id"):
        node_ids = [
            r["node_id"]
            for r in conn.execute(
                "SELECT node_id FROM traces WHERE flow_type = ? ORDER BY depth, id",
                (m["flow_type"],),
            )
        ]
        mainlines.append(
            {"id": m["id"], "name": m["name"], "color": m["color"], "nodes": node_ids}
        )

    nodes: list[dict[str, Any]] = []
    for n in conn.execute("SELECT id, name, type, file_path, snippet FROM nodes ORDER BY id"):
        node: dict[str, Any] = {
            "id": n["id"],
            "name": n["name"],
            "type": _ROLE_TO_TYPE.get(n["type"], "processor"),
        }
        if n["file_path"]:
            node["file_path"] = n["file_path"]
        if n["snippet"]:
            node["snippet"] = n["snippet"]
        nodes.append(node)

    # Intersections: the derived view tells us which nodes are crossings; the
    # verdict table (if the review agent ran) supplies health + description.
    flow_to_line = {
        r["flow_type"]: r["id"] for r in conn.execute("SELECT id, flow_type FROM mainlines")
    }
    intersections: list[dict[str, Any]] = []
    for x in conn.execute("SELECT node_id, flow_types FROM intersections"):
        verdict = conn.execute(
            "SELECT verdict, suspected, description FROM intersection_verdicts WHERE node_id = ?",
            (x["node_id"],),
        ).fetchone()
        involved = [flow_to_line.get(f, f) for f in (x["flow_types"] or "").split(",") if f]
        intersections.append(
            {
                "node_id": x["node_id"],
                "type": verdict["verdict"] if verdict else "pollution",
                "suspected": bool(verdict["suspected"]) if verdict else False,
                "description": verdict["description"] if verdict else "未定性的交叉点（评审 Agent 尚未运行）",
                "involved_lines": involved,
            }
        )

    conn.close()
    return {"mainlines": mainlines, "nodes": nodes, "intersections": intersections}


def write_subway_map(db_path: str | Path, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = export_subway_map(db_path)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse

    from codemap.config import config

    parser = argparse.ArgumentParser(description="Export Blackboard → subway_map.json")
    parser.add_argument("--db", default=str(config.blackboard_db))
    parser.add_argument("--out", default="web/subway_map.json")
    ns = parser.parse_args()
    path = write_subway_map(ns.db, ns.out)
    print(f"wrote {path}")
