"""Tests for intergraph behavior ingestor, decay, drift, behavioral overlaps."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intergraph import behavior, export, ingest, store  # noqa: E402
from test_intergraph import make_fixture  # noqa: E402

NOW = time.time()
DAY = 86400.0


def make_sessions(base: Path) -> None:
    """Fake session dirs: recent file mentions alpha+beta, old file mentions alpha."""
    recent = base / "claude" / "proj"
    old = base / "codex" / "2026" / "01"
    recent.mkdir(parents=True)
    old.mkdir(parents=True)
    (recent / "s1.jsonl").write_text('{"text":"used alpha and beta together"}\n')
    (old / "s2.jsonl").write_text('{"text":"alpha only"}\n')
    os.utime(recent / "s1.jsonl", (NOW - 1 * DAY, NOW - 1 * DAY))
    os.utime(old / "s2.jsonl", (NOW - behavior.MAX_AGE_DAYS * DAY - DAY,
                                NOW - behavior.MAX_AGE_DAYS * DAY - DAY))


class BehaviorTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        root = tmp / "interverse"
        root.mkdir()
        make_fixture(root)
        self.con = store.connect(tmp / "g.db")
        ingest.ingest(root, self.con)
        self.sessions = tmp / "sessions"
        make_sessions(self.sessions)
        self.sources = [("claude", self.sessions / "claude"),
                        ("codex", self.sessions / "codex"),
                        ("kimi", self.sessions / "kimi")]  # missing dir: tolerated

    def test_decay_half_life(self):
        self.assertAlmostEqual(behavior.decay(0), 1.0)
        self.assertAlmostEqual(behavior.decay(behavior.HALF_LIFE_DAYS), 0.5)
        self.assertAlmostEqual(behavior.decay(2 * behavior.HALF_LIFE_DAYS), 0.25)

    def test_scan_sessions_recent_only(self):
        r = behavior.scan_sessions(["alpha", "beta"], now=NOW, sources=self.sources)
        # old file (beyond MAX_AGE_DAYS) is skipped
        self.assertEqual(r["files"], 1)
        self.assertEqual(set(r["sources_used"]), {"claude", "codex"})
        w = behavior.decay(1.0)  # 1 day old
        self.assertAlmostEqual(r["observed"]["alpha"], w, places=3)
        self.assertAlmostEqual(r["observed"]["beta"], w, places=3)
        self.assertAlmostEqual(r["pairs"][("alpha", "beta")], w, places=3)

    def test_ingest_behavior_writes_edges_and_meta(self):
        counts = behavior.ingest_behavior(self.con, now=NOW, sources=self.sources)
        self.assertEqual(counts["co_occurs_edges"], 1)
        nb = store.neighbors(self.con, "alpha", "out")
        co = [e for e in nb["outgoing"] if e["type"] == "co-occurs"]
        self.assertEqual(len(co), 1)
        self.assertEqual(co[0]["node"], "beta")
        meta = json.loads(store.get_node(self.con, "alpha")["meta"])
        self.assertIn("observed", meta)

    def test_drift_both_directions(self):
        behavior.ingest_behavior(self.con, now=NOW, sources=self.sources)
        d = store.drift(self.con)
        # alpha->beta is declared (references) AND observed -> in neither list
        self.assertEqual(d["declared_not_observed"], [])
        self.assertEqual(d["observed_not_declared"], [])
        # remove the declared edge: co-occurs becomes observed_not_declared
        self.con.execute("DELETE FROM edges WHERE type='references'")
        d = store.drift(self.con)
        self.assertEqual([(x["source"], x["target"]) for x in d["observed_not_declared"]],
                         [("alpha", "beta")])

    def test_overlaps_rank_behavioral_first(self):
        behavior.ingest_behavior(self.con, now=NOW, sources=self.sources)
        ov = export.compute_overlaps(self.con)
        self.assertEqual(ov[0]["evidence"], "behavioral")
        self.assertEqual((ov[0]["source"], ov[0]["target"]), ("alpha", "beta"))
        self.assertGreater(ov[0]["behavioral"], 0)

    def test_missing_sources_tolerated(self):
        r = behavior.scan_sessions(["alpha"], now=NOW,
                                   sources=[("nope", self.sessions / "nope")])
        self.assertEqual(r["files"], 0)
        self.assertEqual(r["sources_used"], [])


if __name__ == "__main__":
    unittest.main()
