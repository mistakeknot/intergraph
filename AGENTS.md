# intergraph — Agent Notes

Ecosystem graph plugin. Pure Python stdlib; tests: `python3 -m unittest discover -s tests`.

- `intergraph/store.py` — SQLite schema + queries (MCP tools map 1:1)
- `intergraph/ingest.py` — layer-1 manifest/reference ingestor (idempotent full rebuild)
- `intergraph/export.py` — interchart-compatible JSON + keyword-overlap heuristic
- `intergraph/server.py` — hand-rolled MCP stdio JSON-RPC (NDJSON, no deps)
- `bin/intergraph` — CLI; `bin/serve-mcp.sh` — MCP launcher referenced by plugin.json

Conventions: node ids are namespaced (`plugin`, `plugin:skill:foo`,
`plugin:mcp-server:bar`, `hook:EventName`). Re-ingest after plugin/skill
renames. Don't commit `data/*.db` (generated).

Phase 2 (bead Sylveste-8sp follow-up): cass behavioral ingestor, edge-weight
decay, drift view. Phase 3: tldrs code edges.
