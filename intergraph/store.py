"""SQLite graph store for intergraph.

Schema: nodes + typed/weighted edges + meta. Deliberately boring — the
ecosystem is ~250 nodes, so recursive CTEs cover path/impact queries fine.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  description TEXT,
  source TEXT,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS edges (
  source TEXT NOT NULL REFERENCES nodes(id),
  target TEXT NOT NULL REFERENCES nodes(id),
  type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  evidence TEXT,
  last_seen TEXT,
  PRIMARY KEY (source, target, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "intergraph.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def reset(con: sqlite3.Connection) -> None:
    con.executescript("DELETE FROM edges; DELETE FROM nodes;")


def upsert_node(con, id: str, kind: str, label: str,
                description: str | None = None, source: str | None = None,
                meta: dict | None = None) -> None:
    con.execute(
        "INSERT INTO nodes (id, kind, label, description, source, meta) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, label=excluded.label, "
        "description=excluded.description, source=excluded.source, meta=excluded.meta",
        (id, kind, label, description, source, json.dumps(meta or {})),
    )


def upsert_edge(con, source: str, target: str, type: str,
                weight: float = 1.0, evidence: str | None = None,
                last_seen: str | None = None) -> None:
    con.execute(
        "INSERT INTO edges (source, target, type, weight, evidence, last_seen) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(source,target,type) DO UPDATE SET "
        "weight=excluded.weight, evidence=excluded.evidence, last_seen=excluded.last_seen",
        (source, target, type, weight, evidence, last_seen),
    )


def set_meta(con, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def update_node_meta(con, id: str, **kv) -> None:
    """Merge keys into a node's meta JSON without touching other fields."""
    row = con.execute("SELECT meta FROM nodes WHERE id=?", (id,)).fetchone()
    if not row:
        return
    meta = json.loads(row["meta"] or "{}")
    meta.update(kv)
    con.execute("UPDATE nodes SET meta=? WHERE id=?", (json.dumps(meta), id))


def get_meta(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --- Queries (MCP tools map 1:1 onto these) ---

def stats(con) -> dict:
    nodes = con.execute("SELECT kind, COUNT(*) c FROM nodes GROUP BY kind").fetchall()
    edges = con.execute("SELECT type, COUNT(*) c FROM edges GROUP BY type").fetchall()
    return {
        "nodes": sum(r["c"] for r in nodes),
        "edges": sum(r["c"] for r in edges),
        "by_kind": {r["kind"]: r["c"] for r in nodes},
        "by_edge_type": {r["type"]: r["c"] for r in edges},
        "generated": get_meta(con, "generated"),
    }


def get_node(con, id: str) -> dict | None:
    row = con.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def neighbors(con, id: str, direction: str = "both") -> dict:
    out, in_ = [], []
    if direction in ("both", "out"):
        out = [dict(r) for r in con.execute(
            "SELECT target AS node, type, weight FROM edges WHERE source=?", (id,))]
    if direction in ("both", "in"):
        in_ = [dict(r) for r in con.execute(
            "SELECT source AS node, type, weight FROM edges WHERE target=?", (id,))]
    return {"id": id, "outgoing": out, "incoming": in_}


def path(con, a: str, b: str, max_depth: int = 8) -> list[str] | None:
    """BFS shortest path over undirected edges."""
    if a == b:
        return [a]
    seen = {a}
    frontier = [(a, [a])]
    for _ in range(max_depth):
        nxt = []
        for node, p in frontier:
            rows = con.execute(
                "SELECT target AS n FROM edges WHERE source=? "
                "UNION SELECT source AS n FROM edges WHERE target=?", (node, node))
            for r in rows:
                m = r["n"]
                if m in seen:
                    continue
                if m == b:
                    return p + [m]
                seen.add(m)
                nxt.append((m, p + [m]))
        frontier = nxt
    return None


def impact(con, id: str, max_depth: int = 4) -> list[dict]:
    """Reverse-transitive dependents: everything that reaches `id` via inbound edges."""
    rows = con.execute(
        """WITH RECURSIVE reach(node, depth) AS (
             SELECT source, 1 FROM edges WHERE target=?
             UNION
             SELECT e.source, reach.depth+1 FROM edges e
             JOIN reach ON e.target=reach.node
             WHERE reach.depth < ?
           )
           SELECT DISTINCT node, MIN(depth) AS depth FROM reach
           GROUP BY node ORDER BY depth, node""", (id, max_depth))
    return [dict(r) for r in rows]


def dead_nodes(con, kind: str = "plugin") -> list[dict]:
    """Nodes of `kind` that nothing references (no inbound 'references'/'depends-on')."""
    rows = con.execute(
        """SELECT n.id, n.label, n.description FROM nodes n
           WHERE n.kind=? AND NOT EXISTS (
             SELECT 1 FROM edges e
             WHERE e.target=n.id AND e.type IN ('references','depends-on')
           ) ORDER BY n.id""", (kind,))
    return [dict(r) for r in rows]


def suggested_connections(con, limit: int = 20) -> list[dict]:
    """Plugin pairs with strong one-way references but no edge in the other direction."""
    rows = con.execute(
        """SELECT e.source, e.target, e.weight FROM edges e
           WHERE e.type='references'
             AND e.source IN (SELECT id FROM nodes WHERE kind='plugin')
             AND e.target IN (SELECT id FROM nodes WHERE kind='plugin')
             AND NOT EXISTS (
               SELECT 1 FROM edges r
               WHERE r.source=e.target AND r.target=e.source AND r.type='references'
             )
           ORDER BY e.weight DESC LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


def drift(con, limit: int = 50) -> dict:
    """Declared vs. observed edges, both directions.

    declared_not_observed: 'references' edges whose pair never co-occurs in
    sessions (declared coupling the ecosystem doesn't exercise).
    observed_not_declared: 'co-occurs' edges whose pair has no 'references'
    edge in either direction (behavioral coupling nobody declared).
    """
    declared = con.execute(
        """SELECT e.source, e.target, e.weight FROM edges e
           WHERE e.type='references'
             AND e.source IN (SELECT id FROM nodes WHERE kind='plugin')
             AND e.target IN (SELECT id FROM nodes WHERE kind='plugin')
             AND NOT EXISTS (
               SELECT 1 FROM edges c
               WHERE c.type='co-occurs'
                 AND ((c.source=e.source AND c.target=e.target)
                   OR (c.source=e.target AND c.target=e.source))
             ) ORDER BY e.weight DESC LIMIT ?""", (limit,))
    observed = con.execute(
        """SELECT c.source, c.target, c.weight FROM edges c
           WHERE c.type='co-occurs'
             AND NOT EXISTS (
               SELECT 1 FROM edges e
               WHERE e.type='references'
                 AND ((e.source=c.source AND e.target=c.target)
                   OR (e.source=c.target AND e.target=c.source))
             ) ORDER BY c.weight DESC LIMIT ?""", (limit,))
    return {
        "declared_not_observed": [dict(r) for r in declared],
        "observed_not_declared": [dict(r) for r in observed],
    }
