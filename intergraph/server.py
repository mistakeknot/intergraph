"""intergraph MCP stdio server — newline-delimited JSON-RPC, stdlib only.

Exposes the graph to any MCP host (Claude Code, Codex, Kimi Code). Each tool
maps 1:1 onto a store query. DB path: $INTERGRAPH_DB or data/intergraph.db
under the plugin root; auto-ingests on first run if the DB is missing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__, behavior, code, export, ingest, store

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "graph_stats",
        "description": "Node/edge counts by kind and edge type, plus last ingest time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "graph_neighbors",
        "description": "Direct edges for a node (plugins, skills, commands, agents, mcp-servers, hook-events).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "node id, e.g. 'interflux' or 'interflux:skill:flux-engine'"},
                "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "both"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "graph_path",
        "description": "Shortest undirected path between two nodes (BFS).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string"}, "b": {"type": "string"},
                "max_depth": {"type": "integer", "default": 8},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "graph_impact",
        "description": "Reverse-transitive dependents: everything that reaches a node via inbound edges. Ask before changing a plugin.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "max_depth": {"type": "integer", "default": 4},
            },
            "required": ["id"],
        },
    },
    {
        "name": "graph_dead_nodes",
        "description": "Nodes of a kind that nothing references — deprecation candidates (declared evidence only until phase 2).",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "default": "plugin"}},
        },
    },
    {
        "name": "graph_overlaps",
        "description": "Plugin pairs sharing 2+ capability domains (description-keyword heuristic; phase 2 replaces with behavioral evidence).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "graph_suggested_connections",
        "description": "Plugin pairs with strong one-way references but no reverse edge — integration candidates.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "graph_drift",
        "description": "Declared vs observed coupling: 'references' edges never exercised in sessions, and session co-occurrence nobody declared.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
        },
    },
    {
        "name": "graph_export",
        "description": "interchart-compatible JSON dump ({generated, stats, nodes, edges, overlaps}).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _db_path() -> Path:
    env = os.environ.get("INTERGRAPH_DB")
    return Path(env) if env else store.DEFAULT_DB


def _connect():
    path = _db_path()
    if not path.exists():
        con = store.connect(path)
        ingest.ingest(ingest.INTERVERSE, con)
        behavior.ingest_behavior(con)
        code.ingest_code(con, ingest.INTERVERSE)
        return con
    return store.connect(path)


def _result(id_, payload) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_result(id_, data) -> dict:
    return _result(id_, {
        "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
        "isError": False,
    })


def _call_tool(con, name: str, args: dict):
    if name == "graph_stats":
        return store.stats(con)
    if name == "graph_neighbors":
        return store.neighbors(con, args["id"], args.get("direction", "both"))
    if name == "graph_path":
        return store.path(con, args["a"], args["b"], args.get("max_depth", 8))
    if name == "graph_impact":
        return store.impact(con, args["id"], args.get("max_depth", 4))
    if name == "graph_dead_nodes":
        return store.dead_nodes(con, args.get("kind", "plugin"))
    if name == "graph_overlaps":
        return export.compute_overlaps(con)
    if name == "graph_suggested_connections":
        return store.suggested_connections(con, args.get("limit", 20))
    if name == "graph_drift":
        return store.drift(con, args.get("limit", 50))
    if name == "graph_export":
        return export.export(con)
    raise KeyError(name)


def handle(con, msg: dict) -> dict | None:
    method = msg.get("method")
    id_ = msg.get("id")

    if method == "initialize":
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "intergraph", "version": __version__},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            data = _call_tool(con, params.get("name", ""), params.get("arguments") or {})
            return _tool_result(id_, data)
        except KeyError as e:
            return _error(id_, -32602, f"unknown tool: {e}")
        except (TypeError, KeyError) as e:
            return _error(id_, -32602, f"bad arguments: {e}")
        except Exception as e:  # noqa: BLE001 — surface query failures to the host
            return _error(id_, -32603, str(e))
    if id_ is not None:
        return _error(id_, -32601, f"method not found: {method}")
    return None


def main() -> int:
    con = _connect()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            out.flush()
            continue
        resp = handle(con, msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
