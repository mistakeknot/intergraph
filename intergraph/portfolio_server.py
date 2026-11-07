"""Read-only portfolio MCP entry point: python -m intergraph.portfolio_server.

No startup connection, schema creation, session scanning, or ingestion. The
legacy ecosystem server is intentionally independent of this entry point.
"""
from __future__ import annotations

import json
import sys

from . import __version__, portfolio

PROTOCOL_VERSION = '2024-11-05'
TOOLS = []


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
        return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': -32602, 'message': 'unknown tool'}}
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
