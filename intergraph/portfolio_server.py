"""Read-only portfolio MCP over newline-delimited JSON on stdio."""

import json
import os
import sqlite3
import sys
from contextlib import closing

from . import __version__, portfolio

PROTOCOL_VERSION = "2024-11-05"


def _tool(name, description, properties, required=()):
    return {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": properties, "required": list(required)},
    }


STRING = {"type": "string"}
TOOLS = [
    _tool("graph_portfolio_find", "Find private identities and sourced claims.", {
        "query": STRING,
        "kind": dict(STRING, enum=sorted(portfolio.KINDS)),
        "state": dict(STRING, enum=sorted(portfolio.STATES)),
    }),
    _tool("graph_portfolio_neighbors", "List direct relationships with their supporting claims.", {
        "id": STRING,
        "direction": dict(STRING, enum=["in", "out", "both"]),
        "predicates": {"type": "array", "items": dict(STRING, enum=sorted(portfolio.RELATIONS))},
        "states": {"type": "array", "items": dict(STRING, enum=sorted(portfolio.STATES))},
        "include_history": {"type": "boolean"},
    }, required=("id",)),
    _tool("graph_portfolio_gaps", "List stored gaps and ecosystem bridge status.", {
        "subject_id": STRING,
    }),
]
QUERY = {
    "graph_portfolio_find": portfolio.find,
    "graph_portfolio_neighbors": portfolio.neighbors,
    "graph_portfolio_gaps": portfolio.gaps,
}
TYPES = {"string": str, "boolean": bool, "array": list}


def call_tool(path, name, args):
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if tool is None or not isinstance(args, dict):
        raise portfolio.PortfolioError("unknown tool or invalid arguments")
    schema = tool["inputSchema"]
    properties = schema["properties"]
    if not set(schema["required"]) <= args.keys() <= properties.keys():
        raise portfolio.PortfolioError("invalid query arguments")
    for key, value in args.items():
        if not isinstance(value, TYPES[properties[key]["type"]]):
            raise portfolio.PortfolioError("invalid argument type")

    if name == "graph_portfolio_gaps":
        args = dict(args, ecosystem_path=os.environ.get("INTERGRAPH_ECOSYSTEM_DB"))
    with closing(portfolio.open_readonly(path)) as con:
        con.execute("BEGIN")
        return QUERY[name](con, **args)


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(path, msg):
    request_id = msg.get("id")
    if request_id is None:
        return None
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "intergraph-portfolio", "version": __version__}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        try:
            params = msg.get("params", {})
            if not isinstance(params, dict):
                raise portfolio.PortfolioError("invalid tool parameters")
            data = call_tool(path, params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data, allow_nan=False)}],
                      "isError": False}
        except portfolio.PortfolioError as error:
            return _error(request_id, -32602, str(error))
        except (sqlite3.DatabaseError, OSError):
            return _error(request_id, -32603, "portfolio query unavailable")
    else:
        return _error(request_id, -32601, "method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    path = portfolio.default_path()
    for line in sys.stdin:
        try:
            msg = portfolio.loads(line)
            if not isinstance(msg, dict):
                raise portfolio.PortfolioError("expected a request object")
        except portfolio.PortfolioError:
            result = _error(None, -32700, "parse error")
        else:
            result = handle(path, msg)
        if result is not None:
            print(json.dumps(result, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
