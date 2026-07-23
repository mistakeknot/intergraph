# intergraph

Autonomous self-tuning ecosystem graph for Sylveste. Answers "how does the
ecosystem *actually* fit together" — as a queryable graph any agent can use,
not a static diagram.

**Bead:** Sylveste-8sp · **Brainstorm:** `docs/brainstorms/2026-07-22-intergraph-autonomous-ecosystem-graph-brainstorm.md`

## What it is

- **SQLite graph store** (`data/intergraph.db`) — nodes for plugins, skills,
  commands, agents, MCP servers, hook events; typed edges (`part-of`,
  `provides-*`, `fires-hook`, `references`, `depends-on`)
- **MCP stdio server** — 8 `graph_*` tools queryable from any MCP host
  (Claude Code, Codex, Kimi Code)
- **CLI** (`bin/intergraph`) — same queries without MCP
- **interchart-compatible export** — emits the scan JSON interchart renders;
  interchart becomes intergraph's frontend

## Usage

```bash
# Rebuild from interverse manifests (idempotent)
python3 bin/intergraph ingest

# Query
python3 bin/intergraph stats
python3 bin/intergraph impact interflux
python3 bin/intergraph path interflux interlens
python3 bin/intergraph dead-nodes
python3 bin/intergraph suggested-connections
python3 bin/intergraph overlaps
python3 bin/intergraph export > /tmp/scan.json
```

MCP tools (same names with `graph_` prefix): `graph_stats`,
`graph_neighbors`, `graph_path`, `graph_impact`, `graph_dead_nodes`,
`graph_overlaps`, `graph_suggested_connections`, `graph_export`.

The server auto-ingests on first run if the DB is missing. DB path override:
`INTERGRAPH_DB`.

## Three signal layers (roadmap)

| Phase | Signal | Status |
|-------|--------|--------|
| 1 | Declared structure — manifests, SKILL.md frontmatter, hooks, MCP configs, cross-plugin text references | **shipped (this MVP)** |
| 2 | Observed behavior — cass session co-occurrence, beads deps, git co-change; time-decayed edge weights; drift view | planned |
| 3 | Code edges — `tldrs calls`/`impact` call graphs per repo | planned |

## Caveats

`references` edges mean "mentions in a body of text", not "calls".
`graph_overlaps` is a description-keyword heuristic until phase 2. `depends-on`
is empty ecosystem-wide (no plugin declares peerDependencies yet).

## Development

```bash
python3 -m unittest discover -s tests
```

Pure Python stdlib — no dependencies, no build step.
