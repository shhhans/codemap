-- Global Blackboard schema — shared memory for all concurrent Worker Agents.
--
-- Three concerns:
--   nodes          : code nodes that survived semantic pruning (the stations).
--   traces         : per-agent "check-ins" (which mainline passed through a node);
--                    also doubles as the DFS visited-set for dedup + cycle breaking.
--   intersections  : derived — a node touched by >1 flow_type, awaiting health review.

PRAGMA foreign_keys = ON;

-- ── Nodes ─────────────────────────────────────────────────────────────────
-- A retained code node. `id` is the stable identifier coming from
-- Codebase-Memory (the MCP graph engine), so traces from different agents that
-- land on the same code converge on the same row.
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,           -- Codebase-Memory node id
    name        TEXT NOT NULL,              -- e.g. "AuthMiddleware.verify()"
    type        TEXT NOT NULL DEFAULT 'processor',  -- source | processor | sink | barrier
    file_path   TEXT,
    snippet     TEXT,                       -- core source signature / body
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

-- ── Traces ────────────────────────────────────────────────────────────────
-- One row = one Worker logging that a flow passed through a node ("打卡").
-- The UNIQUE(node_id, flow_type) constraint makes this idempotent and turns the
-- table into a natural visited-set per mainline (防环 + 去重).
CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    flow_type   TEXT NOT NULL,              -- "auth" | "billing" | ...
    -- Worker's role for this node within the mainline. Drives intersection
    -- health: a "sink" (stable state) is a healthy crossing, a "processor"
    -- (intermediate processing) is responsibility pollution.
    node_role   TEXT NOT NULL DEFAULT 'processor',  -- source | processor | sink | barrier
    -- LLM self-reported confidence in the pruning/taint decision [0,1].
    -- Low-confidence branches are kept (downgraded), never silently pruned.
    confidence  REAL NOT NULL DEFAULT 1.0,
    depth       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
    UNIQUE (node_id, flow_type)
);

CREATE INDEX IF NOT EXISTS idx_traces_flow ON traces(flow_type);
CREATE INDEX IF NOT EXISTS idx_traces_node ON traces(node_id);

-- ── Intersections (derived view) ──────────────────────────────────────────
-- A node is an intersection (换乘枢纽) when >1 distinct flow_type checked in.
-- We expose it as a VIEW so it is always consistent with traces; the review
-- agent reads this and writes its verdict into `intersection_verdicts`.
CREATE VIEW IF NOT EXISTS intersections AS
SELECT
    node_id,
    COUNT(DISTINCT flow_type)              AS flow_count,
    GROUP_CONCAT(DISTINCT flow_type)       AS flow_types
FROM traces
GROUP BY node_id
HAVING COUNT(DISTINCT flow_type) > 1;

-- ── Intersection verdicts ─────────────────────────────────────────────────
-- Review-agent output: is this crossing healthy (node is a stable sink for the
-- other mainline) or dangerous (node is an intermediate processing step →
-- responsibility pollution)?
CREATE TABLE IF NOT EXISTS intersection_verdicts (
    node_id     TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    verdict     TEXT NOT NULL,             -- 'healthy' | 'dangerous'
    description TEXT,
    reviewed_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

-- ── Mainlines ─────────────────────────────────────────────────────────────
-- Presentation metadata for each flow (subway line color/name).
CREATE TABLE IF NOT EXISTS mainlines (
    id          TEXT PRIMARY KEY,          -- "line_auth"
    flow_type   TEXT NOT NULL UNIQUE,      -- "auth"
    name        TEXT NOT NULL,             -- "Auth Mainline"
    color       TEXT NOT NULL DEFAULT '#888888'
);
