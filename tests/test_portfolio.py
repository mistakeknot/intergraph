"""Portfolio lifecycle and transport tests with synthetic data only."""

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from intergraph import portfolio, portfolio_server, store
from test_portfolio_regressions import PROJECT, REPO, STAMP, manifest


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portfolio.sqlite"

    def apply(self, data=None, expected=0, dry_run=False):
        return portfolio.import_manifest(self.path, manifest() if data is None else data, expected, dry_run)

    def export(self, path=None):
        with closing(portfolio.open_readonly(path or self.path)) as con:
            return portfolio.export_archive(con)

    def next_batch(self):
        data = manifest()
        data["batch"]["id"] = "second"
        return data

    def cli(self, *args):
        return subprocess.run([sys.executable, "bin/intergraph", *map(str, args)],
                              text=True, capture_output=True)

    def test_same_manifest_replays_before_revision_comparison(self):
        first = self.apply()
        self.assertEqual(self.apply(expected=99), first)
        self.assertEqual(self.export()["revision"], 1)
        self.assertEqual(first["inserted"]["claims"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_leaves_no_file_and_no_committed_changes(self):
        preview = self.apply(expected=None, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["revision"], 1)
        self.assertFalse(self.path.exists())
        self.apply()
        before = self.path.read_bytes()
        preview = self.apply(self.next_batch(), expected=None, dry_run=True)
        self.assertEqual(preview["revision"], 2)
        self.assertEqual(before, self.path.read_bytes())

    def test_requires_expected_revision_for_new_import(self):
        with self.assertRaises(portfolio.PortfolioError):
            self.apply(expected=None)
        self.assertFalse(self.path.exists())

    def test_distinct_batch_has_to_match_expected_revision(self):
        self.apply()
        before = self.export()
        with self.assertRaisesRegex(portfolio.PortfolioError, "stale"):
            self.apply(self.next_batch())
        self.assertEqual(self.export(), before)

    def test_reused_batch_id_with_changed_content_is_rejected(self):
        self.apply()
        different = manifest()
        different["batch"]["actor"] = "different actor"
        with self.assertRaisesRegex(portfolio.PortfolioError, "batch ID reused"):
            self.apply(different, expected=1)
        self.assertEqual(self.export()["revision"], 1)

    def test_optional_fields_and_records_are_validated_before_open(self):
        for field, replacement in (
            ("version", True), ("links", None), ("gaps", [{"unexpected": "field"}]),
        ):
            with self.subTest(field=field), self.assertRaises(portfolio.PortfolioError):
                self.apply(manifest() | {field: replacement})
        variants = (
            ("entities", "id", PROJECT.upper() + "x"),
            ("entities", "visibility", "public"),
            ("entities", "created_at", "2026-01-01T00:00:00"),
            ("sources", "content_hash", "z" * 64),
            ("claims", "qualifiers_json", {"invalid": float("nan")}),
            ("claims", "object_id", 1),
            ("claims", "predicate", "invented-relationship"),
        )
        for table, key, value in variants:
            with self.subTest(table=table, key=key), self.assertRaises(portfolio.PortfolioError):
                data = manifest()
                data[table][0][key] = value
                self.apply(data)
        repeated = manifest()
        repeated["claims"].extend(copy.deepcopy(repeated["claims"]))
        with self.assertRaisesRegex(portfolio.PortfolioError, "duplicate record ID"):
            self.apply(repeated)
        self.assertFalse(self.path.exists())

    def test_json_rejects_duplicate_keys_and_nonfinite_constants(self):
        for invalid in ('{"id":1,"id":2}', "NaN", "Infinity", "-Infinity"):
            with self.subTest(invalid=invalid), self.assertRaises(portfolio.PortfolioError):
                portfolio.loads(invalid)
        self.assertEqual(portfolio.loads('{"n":1e30}'), {"n": 1e30})

    def test_invalid_foreign_key_rolls_back_earlier_changes(self):
        self.apply()
        before = self.export()
        data = self.next_batch()
        data["entities"][0].update(label="Changed before failure", revision=2)
        data["gaps"] = [{"id": "broken", "subject_id": PROJECT, "field": "scope",
                         "reason": "unknown", "status": "open", "source_id": "missing"}]
        with self.assertRaises(portfolio.PortfolioError):
            self.apply(data, expected=1)
        self.assertEqual(self.export(), before)
        with closing(portfolio.open_readonly(self.path)) as con:
            self.assertEqual(portfolio.find(con)["entities"][0]["label"], "Orchard")

    def test_invalid_initial_import_leaves_no_committed_schema(self):
        data = manifest()
        data["claims"][0]["object_id"] = PROJECT
        with self.assertRaisesRegex(portfolio.PortfolioError, "relationship endpoints"):
            self.apply(data)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master").fetchall(), [])
        self.assertEqual(self.apply()["revision"], 1)

    def test_existing_store_with_loose_permissions_is_rejected(self):
        self.apply()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(portfolio.PortfolioError, "only to its owner"):
            self.apply(self.next_batch(), expected=1)
        self.assertEqual(self.export()["revision"], 1)

    def test_import_does_not_create_parent_directories(self):
        missing = self.path.parent / "missing"
        with self.assertRaises(FileNotFoundError):
            portfolio.import_manifest(missing / "portfolio.sqlite", manifest(), 0)
        self.assertFalse(missing.exists())

    def test_retraction_keeps_evidence_and_requires_original_assertion(self):
        self.apply()
        data = self.next_batch()
        data["claims"][0].update(id="withdrawal", state="retracted", supersedes="0")
        different = copy.deepcopy(data)
        different["claims"][0]["object_id"] = PROJECT
        with self.assertRaisesRegex(portfolio.PortfolioError, "original assertion"):
            self.apply(different, expected=1)
        self.apply(data, expected=1)
        with closing(portfolio.open_readonly(self.path)) as con:
            self.assertEqual(portfolio.neighbors(con, PROJECT)["claims"], [])
            history = portfolio.neighbors(con, PROJECT, states=["confirmed", "retracted"],
                                          include_history=True)["claims"]
        self.assertEqual([item["id"] for item in history], ["0", "withdrawal"])
        self.assertTrue(all(not item["effective"] for item in history))
        self.assertEqual(history[-1]["source"]["uri"], "urn:test:inventory")

    def test_candidate_and_dependency_require_suitable_provenance(self):
        data = manifest()
        for changes in ({"predicate": "candidate-same-as"}, {"predicate": "depends-on", "object_id": PROJECT}):
            modified = copy.deepcopy(data)
            modified["claims"][0].update(changes)
            with self.assertRaises(portfolio.PortfolioError):
                self.apply(modified)
        data["claims"][0].update(predicate="candidate-same-as", state="disputed")
        self.apply(data)
        with closing(portfolio.open_readonly(self.path)) as con:
            self.assertEqual(portfolio.neighbors(con, REPO)["claims"], [])
            claims = portfolio.neighbors(con, REPO, direction="in", predicates=["candidate-same-as"])["claims"]
            self.assertEqual([c["id"] for c in claims], ["0"])

    def test_immutable_claims_and_receipts_have_database_guards(self):
        self.apply()
        updates = ("UPDATE portfolio_claims SET writer = 'different'", "DELETE FROM portfolio_batches")
        with closing(sqlite3.connect(self.path)) as con:
            for mutation in updates:
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(mutation)

    def test_entity_correction_preserves_historical_manifest_and_endpoints(self):
        self.apply()
        second = self.next_batch()
        second["claims"] = []
        second["entities"][0].update(label="New Orchard", revision=2)
        receipt = self.apply(second, expected=1)
        self.assertEqual(receipt["updated"], {"entities": 1})
        before = self.export()
        self.assertEqual(before["batches"][0]["manifest"]["entities"][0]["label"], "Orchard")
        stale = copy.deepcopy(second)
        stale["batch"]["id"] = "stale-entity"
        stale["entities"][0]["label"] = "Stale label"
        with self.assertRaisesRegex(portfolio.PortfolioError, "stale entity revision"):
            self.apply(stale, expected=2)
        stale["entities"][0].update(kind="organization", revision=3)
        with self.assertRaisesRegex(portfolio.PortfolioError, "relationship endpoints"):
            self.apply(stale, expected=2)
        self.assertEqual(self.export(), before)

    def test_archive_round_trip_preserves_every_manifest_and_receipt(self):
        first = manifest()
        first["claims"][0]["observed_at"] = "2026-01-01T02:00:00+02:00"
        self.apply(first)
        second = self.next_batch()
        second["claims"][0].update(id="withdrawal", state="retracted", supersedes="0")
        self.apply(second, expected=1)
        archive = self.export()
        self.assertEqual(archive["batches"][0]["manifest"], first)
        restored = self.path.with_name("restored.sqlite")
        preview = portfolio.import_archive(restored, archive, dry_run=True)
        self.assertEqual(preview["revision"], 2)
        self.assertFalse(restored.exists())
        self.assertFalse(portfolio.import_document(restored, archive, 0)["replayed"])
        self.assertTrue(portfolio.import_document(restored, archive, 99)["replayed"])
        self.assertEqual(self.export(restored), archive)

    def test_archive_refuses_tampering_and_nonempty_merge(self):
        self.apply()
        first = self.export()
        second = self.next_batch()
        self.apply(second, expected=1)
        archive = self.export()
        for changes in ({"base_revision": 0}, {"revision": True}, {"manifest_hash": "b" * 64}):
            tampered = copy.deepcopy(archive)
            tampered["batches"][1]["receipt"].update(changes)
            target = self.path.with_name("target.sqlite")
            with self.assertRaises(portfolio.PortfolioError):
                portfolio.import_archive(target, tampered, 0)
            self.assertFalse(target.exists())
        with self.assertRaisesRegex(portfolio.PortfolioError, "empty store"):
            portfolio.import_archive(self.path, first, 2)
        self.assertEqual(self.export(), archive)

    def test_legacy_reset_and_portfolio_import_use_distinct_files(self):
        ecosystem = self.path.with_name("ecosystem.sqlite")
        with closing(store.connect(ecosystem)) as con:
            store.reset(con)
        ecosystem.chmod(0o600)
        with self.assertRaisesRegex(portfolio.PortfolioError, "separate database"):
            portfolio.import_manifest(ecosystem, manifest(), 0)
        self.apply()
        with closing(store.connect(ecosystem)) as con:
            store.reset(con)
            self.assertFalse(con.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'portfolio_%'"
            ).fetchone())
        self.assertEqual(self.export()["revision"], 1)

    def test_gap_provenance_resolution_and_bridge_recheck(self):
        data = manifest()
        data["gaps"] = [{"id": "gap", "subject_id": PROJECT, "field": "implementation",
                         "reason": "unknown", "status": "resolved", "source_id": "inventory",
                         "resolution_claim_id": "0"}]
        data["links"] = [{"id": "link", "entity_id": PROJECT, "ecosystem_node_id": "sample-plugin",
                          "relation": "represents-project", "source_id": "inventory"}]
        self.apply(data)
        ecosystem = self.path.with_name("ecosystem.sqlite")
        with closing(portfolio.open_readonly(self.path)) as con:
            result = portfolio.gaps(con, ecosystem_path=ecosystem)
            self.assertEqual(result["gaps"][0]["source"]["id"], "inventory")
            self.assertEqual(result["bridges"][0]["endpoint_status"], "unresolved")
            self.assertFalse(ecosystem.exists())
            with closing(store.connect(ecosystem)) as legacy:
                self.assertEqual(portfolio.gaps(con, ecosystem_path=ecosystem)["bridges"][0]["endpoint_status"], "dangling")
                legacy.execute("INSERT INTO nodes(id, kind, label) VALUES (?, ?, ?)",
                               ("sample-plugin", "plugin", "Sample"))
                legacy.commit()
                self.assertEqual(portfolio.gaps(con, ecosystem_path=ecosystem)["bridges"][0]["endpoint_status"], "resolved")
                store.reset(legacy)
                self.assertEqual(portfolio.gaps(con, ecosystem_path=ecosystem)["bridges"][0]["endpoint_status"], "dangling")

    def test_mcp_exposes_queries_and_leaves_the_store_read_only(self):
        def request(method, **params):
            return portfolio_server.handle(self.path, {"id": 17, "method": method, "params": params})

        names = {tool["name"] for tool in request("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"graph_portfolio_find", "graph_portfolio_neighbors", "graph_portfolio_gaps"})
        request("initialize")
        self.assertIn("error", request("tools/call", name="graph_portfolio_find", arguments={}))
        self.assertFalse(self.path.exists())
        self.apply()
        before = self.path.read_bytes()
        self.assertIn("error", request("tools/call", name="portfolio-import", arguments={}))
        self.assertIn("error", request("tools/call", name="graph_portfolio_find", arguments={"public": True}))
        self.assertIn("error", request("tools/call", name="graph_portfolio_neighbors", arguments={}))
        result = request("tools/call", name="graph_portfolio_find", arguments={"query": "orchard"})
        found = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(found["entities"][0]["id"], PROJECT)
        self.assertEqual(before, self.path.read_bytes())
        with closing(portfolio.open_readonly(self.path)) as con, self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM portfolio_entities")

    def test_cli_private_import_export_and_option_isolation(self):
        source = self.path.with_name("manifest.json")
        source.write_text(json.dumps(manifest()), encoding="utf-8")
        args = ("portfolio-import", source, "--portfolio-db", self.path)
        self.assertEqual(self.cli(*args).returncode, 2)
        preview = self.cli(*args, "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(json.loads(preview.stdout)["dry_run"])
        self.assertFalse(self.path.exists())
        applied = self.cli(*args, "--expected-revision", "0")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        exported = self.cli("portfolio-export", "--portfolio-db", self.path)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads(exported.stdout), self.export())
        self.assertEqual(self.cli("portfolio-export", "--public").returncode, 2)
        self.assertEqual(self.cli("--db", self.path, "portfolio-export").returncode, 2)

    def test_cli_conversion_writes_private_outputs_and_keeps_existing_files(self):
        text = self.path.with_name("inventory.md")
        text.write_text("# Inventory\n## Orchard\n- Name: Orchard\n", encoding="utf-8")
        proposal = self.path.with_name("proposed.json")
        sidecar = self.path.with_name("identities.json")
        sidecar.write_text("keep", encoding="utf-8")
        args = ("portfolio-convert", text, "--observed-at", STAMP,
                "--output-manifest", proposal, "--output-sidecar")
        self.assertEqual(self.cli(*args, sidecar).returncode, 2)
        self.assertFalse(proposal.exists())
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "keep")
        sidecar = self.path.with_name("new-identities.json")
        run = self.cli(*args, sidecar)
        self.assertEqual(run.returncode, 0, run.stderr)
        for output in (proposal, sidecar):
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
