#!/usr/bin/env bash
# Launch the intergraph MCP stdio server (plugin manifest entry point).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m intergraph.server
