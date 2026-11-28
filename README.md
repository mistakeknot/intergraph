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

## Three signal layers

| Phase | Signal | Status |
|-------|--------|--------|
| 1 | Declared structure — manifests, SKILL.md frontmatter, hooks, MCP configs, cross-plugin text references | shipped |
| 2 | Observed behavior — session-history co-occurrence across Claude/Kimi/Codex with 14-day half-life decay; `graph_drift` declared-vs-observed view; behavioral overlaps | shipped |
| 3 | Code edges — cross-plugin `calls` edges from tldrs import analysis (Go) + regex over code files | shipped |

## Behavioral model

`intergraph/behavior.py` scans `~/.claude/projects`, `~/.kimi-code/sessions`,
and `~/.codex/sessions` (read-only, 56-day window) for plugin-name mentions.
Each session file contributes `0.5 ** (age_days / 14)` to every plugin it
mentions and every pair that co-occurs in it. That yields `co-occurs` edges
and per-plugin `observed` weights in node meta — the autotune signal.
Deliberately excluded: beads deps and git co-change (work-graph ≠ code-graph).

## Code edges

`intergraph/code.py` adds `calls` edges between plugins from tldrs import
analysis (Go module imports, high confidence) plus a regex pass over code
files (exec'd binaries, socket paths, endpoints). `impact()`/`path()` span
them. Skip with `--no-code`; skip behavior with `--no-behavior`.

## Caveats

`references` edges mean "mentions in a body of text", not "calls"; `calls`
edges mean "mentions in code" unless marked `tldrs-import`. `co-occurs` means
"mentioned in the same session" — strong coupling signal, not causation.
`depends-on` is empty ecosystem-wide (no plugin declares peerDependencies).

## Development

```bash
python3 -m unittest discover -s tests
```

Pure Python stdlib — no dependencies, no build step.

## Private portfolio (additive)

A separate durable portfolio file and read-only MCP entry point are available
without changing ecosystem ingest/reset or existing tools. Explicit local CLI
commands prepare, validate, import and losslessly export private manifests.
See [the portfolio contract](docs/portfolio.md) for lifecycle, schema, privacy,
bootstrap review and recovery instructions. No public publication or deployment
is included.
