"""Read-only portfolio MCP entry point: python -m intergraph.portfolio_server.

No startup connection, schema creation, session scanning, or ingestion. The
legacy ecosystem server is intentionally independent of this entry point.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import sys

from . import __version__, portfolio

PROTOCOL_VERSION = '2024-11-05'
TOOLS = [
    {'name': 'graph_portfolio_find', 'description': 'Find private portfolio identities and sourced claims.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {
         'query': {'type': 'string'}, 'kind': {'type': 'string', 'enum': sorted(portfolio.KINDS)},
         'state': {'type': 'string', 'enum': sorted(portfolio.STATES)}}}},
    {'name': 'graph_portfolio_neighbors', 'description': 'Direct typed portfolio claims with full provenance; not transitive dependency impact.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['id'], 'properties': {
         'id': {'type': 'string'}, 'direction': {'type': 'string', 'enum': ['in', 'out', 'both']},
         'predicates': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(portfolio.RELATIONS)}},
         'states': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(portfolio.STATES)}},
         'include_history': {'type': 'boolean'}}}},
    {'name': 'graph_portfolio_gaps', 'description': 'Explicit portfolio gaps and bridge resolution warnings (no cross-layer traversal).',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {
         'subject_id': {'type': 'string'}}}},
]


def call_tool(path, name, args):
    tool = next((t for t in TOOLS if t['name'] == name), None)
    if not tool or not isinstance(args, dict):
        raise portfolio.PortfolioError('unknown tool or invalid arguments')
    schema = tool['inputSchema']
    if set(args) - set(schema['properties']) or not set(schema.get('required', [])) <= set(args):
        raise portfolio.PortfolioError('unsupported arguments; publication and mutations are unavailable')
    for key, value in args.items():
        expected = schema['properties'][key]['type']
        if (expected == 'string' and not isinstance(value, str) or
            expected == 'array' and not isinstance(value, list) or
            expected == 'boolean' and type(value) is not bool):
            raise portfolio.PortfolioError('invalid argument type')
    with closing(portfolio.open_readonly(path)) as con:
        con.execute('BEGIN')
        if name == 'graph_portfolio_find':
            return portfolio.find(con, **args)
        if name == 'graph_portfolio_neighbors':
            return portfolio.neighbors(con, **args)
        return portfolio.gaps(con, ecosystem_path=os.environ.get('INTERGRAPH_ECOSYSTEM_DB'), **args)


def handle(path, msg):
    id_ = msg.get('id')
    method = msg.get('method')
    result = None
    if method == 'initialize':
        result = {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'intergraph-portfolio', 'version': __version__}}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'ping':
        result = {}
    elif method == 'tools/call':
        params = msg.get('params') or {}
        try:
            if not isinstance(params, dict):
                raise portfolio.PortfolioError('invalid tool parameters')
            data = call_tool(path, params.get('name'), params.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(data)}], 'isError': False}
        except portfolio.PortfolioError as e:
            return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': -32602, 'message': str(e)}}
        except (sqlite3.DatabaseError, OSError):
            return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': -32603, 'message': 'portfolio query unavailable'}}
    elif id_ is not None:
        return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': -32601, 'message': 'method not found'}}
    if id_ is None:
        return None
    return {'jsonrpc': '2.0', 'id': id_, 'result': result}


def main():
    path = portfolio.default_path()
    for line in sys.stdin:
        try:
            msg = portfolio.loads(line)
            if not isinstance(msg, dict):
                raise portfolio.PortfolioError('invalid request')
            result = handle(path, msg)
        except portfolio.PortfolioError:
            result = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        if result is not None:
            print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
