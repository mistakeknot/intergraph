"""Tests for intergraph code ingestor (phase 3: 'calls' edges)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intergraph import code, ingest, store  # noqa: E402
from test_intergraph import make_fixture  # noqa: E402


def fake_tldrs(modules_by_name: dict[str, list[str]]):
    """Return a tldrs_runner stand-in: file -> imported module paths."""
    def runner(path: Path) -> list[str]:
        return modules_by_name.get(path.name, [])
    return runner


class CodeTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "interverse"
        self.root.mkdir()
        make_fixture(self.root)
        # alpha ships Go code that imports beta's module and execs beta's binary
        cmd = self.root / "alpha" / "cmd"
        cmd.mkdir()
        (cmd / "main.go").write_text(
            'package main\nimport "github.com/example/beta/sdk"\n'
            '// also shells out to interverse/beta/bin/serve.sh\n')
        # noise dir that must be skipped
        nm = self.root / "alpha" / "node_modules" / "junk"
        nm.mkdir(parents=True)
        (nm / "x.go").write_text('import "github.com/example/beta/should-not-count"')
        self.con = store.connect(Path(self.tmp.name) / "g.db")
        ingest.ingest(self.root, self.con)

    def test_regex_calls_edge(self):
        counts = code.ingest_code(self.con, self.root, use_tldrs=False)
        self.assertEqual(counts["calls_edges"], 1)
        self.assertFalse(counts["tldrs"])
        nb = store.neighbors(self.con, "alpha", "out")
        calls = [e for e in nb["outgoing"] if e["type"] == "calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["node"], "beta")
        self.assertEqual(calls[0]["weight"], 1.0)  # node_modules file skipped

    def test_tldrs_import_marks_evidence(self):
        runner = fake_tldrs({"main.go": ["github.com/example/beta/sdk"]})
        code.ingest_code(self.con, self.root, use_tldrs=True, tldrs_runner=runner)
        row = self.con.execute(
            "SELECT evidence FROM edges WHERE type='calls' AND source='alpha'").fetchone()
        self.assertIn("tldrs-import", row["evidence"])

    def test_tldrs_failure_falls_back(self):
        # a tldrs runner that yields nothing exercises the fallback path
        counts = code.ingest_code(self.con, self.root, use_tldrs=True,
                                  tldrs_runner=lambda p: [])
        self.assertEqual(counts["calls_edges"], 1)  # regex still finds it

    def test_impact_spans_calls(self):
        code.ingest_code(self.con, self.root, use_tldrs=False)
        hits = store.impact(self.con, "beta")
        nodes = {h["node"] for h in hits}
        self.assertIn("alpha", nodes)

    def test_skips_skip_dirs(self):
        files = code.code_files(self.root / "alpha")
        self.assertTrue(all("node_modules" not in f.parts for f in files))


if __name__ == "__main__":
    unittest.main()
