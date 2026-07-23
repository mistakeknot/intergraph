# intergraph — Agent Notes

Ecosystem graph plugin. Pure Python stdlib; tests: `python3 -m unittest discover -s tests`.

- `intergraph/store.py` — SQLite schema + queries (MCP tools map 1:1)
- `intergraph/ingest.py` — layer-1 manifest/reference ingestor (idempotent full rebuild)
- `intergraph/behavior.py` — layer-3 session-history ingestor (co-occurs edges, 14d half-life, 56d window)
- `intergraph/code.py` — layer-2 code ingestor (tldrs Go imports + regex, 'calls' edges)
- `intergraph/export.py` — interchart-compatible JSON + behavioral overlap ranking
- `intergraph/server.py` — hand-rolled MCP stdio JSON-RPC (NDJSON, no deps)
- `bin/intergraph` — CLI; `bin/serve-mcp.sh` — MCP launcher referenced by plugin.json

Conventions: node ids are namespaced (`plugin`, `plugin:skill:foo`,
`plugin:mcp-server:bar`, `hook:EventName`). Re-ingest after plugin/skill
renames. Don't commit `data/*.db` (generated). Full ingest order: static →
behavior → code (behavior/code layers need plugin nodes to exist).
