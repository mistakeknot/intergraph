"""Tests for intergraph store, ingest, export, and MCP dispatch."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intergraph import export, ingest, server, store  # noqa: E402


def make_fixture(root: Path) -> None:
    """Two-plugin fake interverse: alpha (skill, mcp, hooks, refs beta) + beta."""
    for name in ("alpha", "beta"):
        (root / name / ".claude-plugin").mkdir(parents=True)
    (root / "alpha" / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "alpha", "version": "1.0.0", "description": "quality review tooling",
        "skills": ["./skills/review"],
        "mcpServers": {"alpha-mcp": {"type": "stdio", "command": "x"}},
    }))
    (root / "beta" / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "beta", "version": "0.1.0", "description": "docs and roadmap artifacts",
    }))
    skill = root / "alpha" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: reviews code, mentions beta for docs\n---\nUse beta.\n")
    hooks = root / "alpha" / "hooks"
    hooks.mkdir()
    (hooks / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": []}}))


class IntergraphTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "interverse"
        root.mkdir()
        make_fixture(root)
        self.con = store.connect(Path(self.tmp.name) / "g.db")
        self.counts = ingest.ingest(root, self.con)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_counts(self):
        self.assertEqual(self.counts["plugins"], 2)
        self.assertEqual(self.counts["skills"], 1)
        self.assertEqual(self.counts["mcp_servers"], 1)
        self.assertEqual(self.counts["hook_events"], 1)

    def test_skill_node_namespaced_by_kind(self):
        self.assertIsNotNone(store.get_node(self.con, "alpha:skill:review"))

    def test_reference_edge(self):
        nb = store.neighbors(self.con, "alpha", "out")
        targets = {(e["node"], e["type"]) for e in nb["outgoing"]}
        self.assertIn(("beta", "references"), targets)

    def test_path(self):
        p = store.path(self.con, "alpha:skill:review", "interverse")
        self.assertEqual(p, ["alpha:skill:review", "alpha", "interverse"])

    def test_impact(self):
        hits = store.impact(self.con, "beta")
        self.assertEqual([h["node"] for h in hits], ["alpha"])

    def test_dead_nodes(self):
        dead = [d["id"] for d in store.dead_nodes(self.con)]
        self.assertEqual(dead, ["alpha"])  # nothing references alpha

    def test_suggested_connections(self):
        sugg = store.suggested_connections(self.con)
        self.assertEqual([(s["source"], s["target"]) for s in sugg], [("alpha", "beta")])

    def test_export_shape(self):
        d = export.export(self.con)
        self.assertEqual(set(d), {"generated", "stats", "nodes", "edges", "overlaps"})
        self.assertEqual(d["stats"]["nodes"], len(d["nodes"]))
        self.assertEqual(d["stats"]["edges"], len(d["edges"]))
        self.assertIn("byType", d["stats"])

    def test_server_initialize_and_tools(self):
        resp = server.handle(self.con, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "intergraph")
        resp = server.handle(self.con, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(resp["result"]["tools"]), 8)

    def test_server_tool_call(self):
        resp = server.handle(self.con, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "graph_impact", "arguments": {"id": "beta"}}})
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(data[0]["node"], "alpha")

    def test_server_unknown_tool(self):
        resp = server.handle(self.con, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", resp)


if __name__ == "__main__":
    unittest.main()
