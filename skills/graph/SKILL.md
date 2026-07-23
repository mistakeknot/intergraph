---
name: graph
description: Use when working across Sylveste plugins — before changing a plugin check what depends on it (graph_impact), find integration candidates (graph_suggested_connections), redundancy candidates (graph_overlaps), or unreferenced plugins (graph_dead_nodes). Queries the intergraph MCP server over the ecosystem graph.
---

# intergraph

Query the Sylveste ecosystem graph: 270+ nodes (plugins, skills, commands,
agents, MCP servers, hook events) and the edges between them, built from
manifests and cross-plugin references. Backed by SQLite at
`interverse/intergraph/data/intergraph.db`.

## When to use

- **Before changing a plugin's interface** — `graph_impact(id)` lists everything
  that reaches it via inbound edges.
- **Looking for redundancy** — `graph_overlaps()` returns plugin pairs sharing
  2+ capability domains.
- **Looking for integration opportunities** — `graph_suggested_connections()`
  returns strong one-way reference pairs with no reverse edge.
- **Finding dead weight** — `graph_dead_nodes(kind)` lists nodes nothing
  references (declared evidence only — verify before acting).
- **Understanding how two things relate** — `graph_path(a, b)` shortest path,
  `graph_neighbors(id)` direct edges.

## Access

Prefer the MCP tools (`graph_*`) when the intergraph MCP server is registered.
Fallback CLI (always works, no MCP needed):

```bash
python3 ~/projects/Sylveste/interverse/intergraph/bin/intergraph <cmd>
# cmds: ingest | stats | export | neighbors <id> | path <a> <b> |
#       impact <id> | dead-nodes [--kind K] | suggested-connections | overlaps
```

## Refreshing the graph

The graph is rebuilt from interverse manifests on demand:

```bash
python3 ~/projects/Sylveste/interverse/intergraph/bin/intergraph ingest
```

Re-ingest after adding/renaming plugins or skills. The MCP server auto-ingests
on first run if the DB is missing, but does not watch for changes (phase 2).

## Caveats

- Edges are **declared** structure: containment, hooks, and text mentions of
  other plugin names. `references` does not mean "calls" — it means "mentions".
- `graph_overlaps` is a description-keyword heuristic until phase 2 lands
  behavioral evidence from cass session histories.
- `depends-on` is currently empty ecosystem-wide (no plugin declares
  peerDependencies) — treat reference edges as the de-facto dependency signal.
