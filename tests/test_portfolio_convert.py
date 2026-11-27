"""Inventory conversion coverage. Public tests use made-up records."""

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from intergraph import portfolio
from intergraph.portfolio_convert import convert
from test_portfolio_regressions import LATER, STAMP

TEXT = """# Inventory
## Orchard
- Name: Orchard
- Repo URL: https://github.com/example-orchard/sapling
- GitHub description: [G] Catalog entry for Sapling.
- Related projects: [C] A mention of another project.
## Grove
- Name: Grove
- Notes / gaps: [F] Unverified alias.
## Mirrors
- [sapling](https://github.com/example-orchard/sapling.git)
## Local
- Name: A local observation
- Local path: omitted
"""


class ConversionTests(unittest.TestCase):
    def test_preserves_carrier_quotes_and_counts_repositories_once(self):
        manifest, _, report = convert(TEXT, STAMP)
        self.assertEqual(report["parsed_records"], 3)
        self.assertEqual(report["distinct_urls"], 1)
        self.assertEqual(report["unresolved_numeric_ids"], 1)
        self.assertEqual(len(report["excluded_records"]), 1)
        self.assertEqual(len(manifest["entities"]), 3)
        self.assertTrue(all("object_id" not in claim for claim in manifest["claims"]))
        metadata, = [claim for claim in manifest["claims"] if claim["predicate"] == "description"]
        self.assertEqual(metadata["state"], "confirmed")
        self.assertEqual(metadata["value_json"], "[G] Catalog entry for Sapling.")
        self.assertTrue(metadata["source_locator"].startswith("line:"))
        self.assertEqual(len(report["proposed_relationships"]), 1)

    def test_disputed_identity_is_retained_with_a_gap(self):
        text = TEXT.replace("- Related projects: [C] A mention of another project.",
                            "- Notes / gaps: [C] Name match only.\n- [Z] Local-only note.")
        manifest, _, report = convert(text, STAMP)
        candidate, = [claim for claim in manifest["claims"] if "object_id" in claim]
        self.assertEqual((candidate["predicate"], candidate["state"]), ("candidate-same-as", "disputed"))
        self.assertEqual(report["conflicts"], 1)
        self.assertEqual(len(report["rejected_fields"]), 1)
        self.assertFalse(report["proposed_relationships"])
        self.assertNotIn("Local-only note", portfolio.canonical(manifest))

    def test_new_snapshot_retains_identity_and_supersedes_observations(self):
        first, sidecar, _ = convert(TEXT, STAMP)
        changed = TEXT.replace("Catalog entry for Sapling.", "Updated catalog entry.")
        second, proposed, _ = convert(changed, LATER, sidecar)
        self.assertEqual(first["entities"], second["entities"])
        previous, = [claim for claim in first["claims"] if claim["predicate"] == "description"]
        current, = [claim for claim in second["claims"] if claim["predicate"] == "description"]
        self.assertEqual(current["supersedes"], previous["id"])
        self.assertEqual(convert(TEXT, LATER, proposed)[0], first)
        self.assertEqual(convert(changed, STAMP, proposed)[0], second)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.sqlite"
            portfolio.import_manifest(path, first, 0)
            portfolio.import_manifest(path, second, 1)

    def test_reviewed_redirect_preserves_uuid_after_url_change(self):
        first, accepted, _ = convert(TEXT, STAMP)
        old = "repository:https://github.com/example-orchard/sapling"
        new = "repository:https://github.com/example-orchard/seedling"
        accepted["key_redirects"][new] = old
        second, _, _ = convert(TEXT.replace("/sapling", "/seedling"), LATER, accepted)
        original, = [row["id"] for row in first["entities"] if row["kind"] == "repository"]
        current, = [row["id"] for row in second["entities"] if row["kind"] == "repository"]
        self.assertEqual(original, current)

    def test_omissions_do_not_create_retractions(self):
        _, accepted, _ = convert(TEXT, STAMP)
        partial, _, report = convert("# Inventory\n## Orchard\n- Name: Orchard\n", LATER, accepted)
        self.assertFalse(any(claim["state"] == "retracted" for claim in partial["claims"]))
        self.assertEqual(report["retraction_candidates"], [])
        with self.assertRaises(portfolio.PortfolioError):
            convert("unstructured paragraph", STAMP)


@unittest.skipUnless(os.environ.get("INTERGRAPH_PRIVATE_INVENTORY"), "private inventory path was not supplied")
class PrivateAcceptance(unittest.TestCase):
    def test_private_inventory_recovery(self):
        # Keep diagnostics inside the private environment.
        try:
            carrier = Path(os.environ["INTERGRAPH_PRIVATE_INVENTORY"]).read_text(encoding="utf-8")
            manifest, _, _ = convert(carrier, STAMP)
            with tempfile.TemporaryDirectory() as directory:
                original = Path(directory) / "original.sqlite"
                restored = Path(directory) / "restored.sqlite"
                receipt = portfolio.import_manifest(original, manifest, 0)
                self.assertEqual(portfolio.import_manifest(original, manifest, 99), receipt)
                with closing(portfolio.open_readonly(original)) as con:
                    archive = portfolio.export_archive(con)
                portfolio.import_archive(restored, archive, 0)
                with closing(portfolio.open_readonly(restored)) as con:
                    self.assertEqual(portfolio.export_archive(con), archive)
        except Exception:
            self.fail("private inventory recovery failed")
