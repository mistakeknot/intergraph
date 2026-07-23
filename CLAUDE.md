# intergraph

Self-tuning ecosystem graph for Sylveste — SQLite store + MCP query server. See `AGENTS.md` for architecture and development notes, `README.md` for usage.

## Quick Commands

```bash
python3 bin/intergraph ingest           # rebuild graph from interverse manifests
python3 bin/intergraph stats            # node/edge counts
python3 bin/intergraph export           # interchart-compatible JSON
python3 -m unittest discover -s tests   # tests
```
