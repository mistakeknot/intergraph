"""Regressions exercised at the import, conversion, and stdio boundaries."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from intergraph import portfolio
from intergraph.portfolio_convert import convert

STAMP = "2026-01-01T00:00:00Z"
LATER = "2026-02-01T00:00:00Z"
PROJECT = "00000000-0000-0000-0000-000000000001"
REPO = "00000000-0000-0000-0000-000000000002"


def manifest():
    return {
        "version": 1,
        "batch": {"id": "first", "source_id": "inventory", "actor": "test"},
        "sources": [{"id": "inventory", "uri": "urn:test:inventory", "content_hash": "a" * 64,
                     "retrieved_at": STAMP, "source_class": "manual"}],
        "entities": [
            {"id": PROJECT, "kind": "project", "label": "Orchard", "created_at": STAMP},
            {"id": REPO, "kind": "repository", "label": "Sapling", "created_at": STAMP},
        ],
        "claims": [{
            "id": "0", "subject_id": PROJECT, "predicate": "implemented-by",
            "object_id": REPO, "state": "confirmed", "source_id": "inventory",
            "source_locator": "line:1", "observed_at": STAMP, "origin_class": "manual",
            "origin_ref": "urn:test:statement", "writer": "test",
        }],
        "gaps": [],
        "links": [],
    }


class PortfolioRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "portfolio.sqlite"

    def test_falsey_supersedes_cannot_hide_an_unrelated_claim(self):
        first = manifest()
        portfolio.import_manifest(self.db, first, 0)
        second = copy.deepcopy(first)
        second["batch"]["id"] = "second"
        claim = second["claims"][0]
        del claim["object_id"]
        claim.update(id="alias", subject_id=REPO, predicate="alias", value_json="Sapling")
        for invalid in (0, False, "", "0"):
            with self.subTest(supersedes=invalid), self.assertRaises(portfolio.PortfolioError):
                portfolio.import_manifest(self.db, second | {"claims": [
                    claim | {"supersedes": invalid},
                ]}, 1)
        with closing(portfolio.open_readonly(self.db)) as reader:
            result = portfolio.neighbors(reader, PROJECT)
            self.assertEqual(result["revision"], 1)
            self.assertEqual([row["id"] for row in result["claims"]], ["0"])

    def test_archive_cannot_pad_revision_with_duplicate_batches(self):
        portfolio.import_manifest(self.db, manifest(), 0)
        with closing(portfolio.open_readonly(self.db)) as reader:
            archive = portfolio.export_archive(reader)
        archive["batches"].append(copy.deepcopy(archive["batches"][0]))
        archive["revision"] = 2
        target = self.db.with_name("restore.sqlite")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaises(portfolio.PortfolioError):
                portfolio.import_archive(target, archive, 0, dry_run=dry_run)
            self.assertFalse(target.exists())

    def test_stdio_rejects_an_overflowing_request_id_and_continues(self):
        run = subprocess.run(
            [sys.executable, "-m", "intergraph.portfolio_server"],
            input='{"jsonrpc":"2.0","id":1e400,"method":"initialize"}\n'
                  '{"jsonrpc":"2.0","id":7,"method":"ping"}\n',
            text=True, capture_output=True, check=True,
            env=os.environ | {"INTERGRAPH_PORTFOLIO_DB": str(self.db)},
        )
        malformed, ping = map(json.loads, run.stdout.splitlines())
        self.assertIsNone(malformed["id"])
        self.assertEqual(malformed["error"]["code"], -32700)
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertFalse(self.db.exists())

    def test_same_inventory_applies_and_caches_reviewed_kind_correction(self):
        text = "# Inventory\n## Orchard\n- Name: Orchard\n- Repo URL: unknown\n"
        first, accepted, _ = convert(text, STAMP)
        before = first["entities"][0]
        portfolio.import_manifest(self.db, first, 0)
        key, = accepted["entities"]
        accepted["entities"][key]["kind"] = "project"
        second, proposed, _ = convert(text, LATER, accepted)
        after = second["entities"][0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["kind"], "project")
        self.assertEqual(after["revision"], 2)
        self.assertNotEqual(second["batch"]["id"], first["batch"]["id"])
        self.assertEqual(second["sources"], first["sources"])
        self.assertEqual(portfolio.import_manifest(self.db, second, 1)["updated"], {"entities": 1})
        self.assertEqual(convert(text, STAMP, proposed)[0], second)
        changed, _, _ = convert(text.replace("Orchard\n- Repo", "Orchard project\n- Repo"),
                                LATER, proposed)
        self.assertEqual(changed["entities"][0], after)
        with closing(portfolio.open_readonly(self.db)) as reader:
            self.assertEqual(portfolio.find(reader, kind="project")["entities"][0]["id"], before["id"])
