"""Unmistakably synthetic bootstrap carrier; no network or real inventory."""
import copy
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from intergraph import portfolio as p
from intergraph.portfolio_convert import convert

STAMP = '2026-01-01T00:00:00Z'
TEXT = '''# Synthetic inventory
## Synthetic products
### Synthetic ambiguous label
- Name: Synthetic ambiguous label
- Purpose: [C] A synthetic reported purpose.
- Repo URL: unknown
- Notes / gaps: Synthetic disputed identity; do not guess.
### Synthetic private label
- Name: Synthetic private label
- Purpose: [C] Synthetic private tool.
- Repo URL: https://github.com/synthetic-owner/synthetic-private
- GitHub description: [G] Synthetic quoted metadata.
- Related projects: [C] Synthetic mention, not a dependency.
## Synthetic organization inventory
- [synthetic-org/synthetic-repository](https://github.com/synthetic-org/synthetic-repository) — Synthetic compact metadata
- [synthetic-owner/synthetic-private](https://github.com/synthetic-owner/synthetic-private) — Synthetic duplicate URL
### Synthetic local observation
- Name: Synthetic checkout
- Repo URL: unknown
- Local path: /synthetic/not-a-real-path
'''


class ConverterTest(unittest.TestCase):
    def test_formats_counts_provenance_and_no_inferences(self):
        manifest, sidecar, report = convert(TEXT, STAMP)
        self.assertEqual(report['parsed_records'], 4)
        self.assertEqual(report['distinct_urls'], 2)
        self.assertEqual(report['independently_reconciled_repository_ids'], 0)
        self.assertEqual(len(report['excluded_records']), 1)
        self.assertEqual(len(manifest['entities']), 4)
        self.assertFalse(any('object_id' in c for c in manifest['claims']))
        self.assertTrue(all(c['source_locator'].startswith('line:') for c in manifest['claims']))
        self.assertTrue(all('raw' in c['qualifiers_json'] for c in manifest['claims']))
        self.assertTrue(all(c['state'] == 'reported' for c in manifest['claims']
                            if 'C' in c['qualifiers_json']['carrier_markers']))
        self.assertEqual(convert(TEXT, '2026-02-01T00:00:00Z', sidecar)[0], manifest)
        self.assertEqual(len(manifest['gaps']), 1)
        self.assertTrue(sidecar['entities'])

    def test_snapshot_lineage_and_lossless_recovery(self):
        a, sidecar, _ = convert(TEXT, STAMP)
        b, new_sidecar, _ = convert(TEXT.replace('synthetic reported purpose', 'synthetic revised purpose'),
                                  '2026-02-01T00:00:00Z', sidecar)
        self.assertEqual(a['entities'], b['entities'])
        self.assertTrue(any(c.get('supersedes') for c in b['claims']))
        self.assertEqual(convert(TEXT, STAMP, new_sidecar)[0], a)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'portfolio.sqlite'
            p.import_manifest(path, a, 0)
            p.import_manifest(path, b, 1)
            with closing(p.open_readonly(path)) as con:
                archive = p.export_archive(con)
            other = Path(tmp) / 'restore.sqlite'
            p.import_archive(other, archive, 0)
            with closing(p.open_readonly(other)) as con:
                self.assertEqual(p.export_archive(con), archive)
                self.assertTrue(all(c['source']['content_hash'] for c in p.find(con)['entities'][0]['claims']))

    def test_partial_or_empty_source_never_infers_retractions(self):
        _, sidecar, _ = convert(TEXT, STAMP)
        partial, _, report = convert('# Synthetic\n### Synthetic partial\n- Name: Synthetic partial\n- Repo URL: unknown\n', STAMP, sidecar)
        self.assertFalse(any(c['state'] == 'retracted' for c in partial['claims']))
        self.assertEqual(report['retraction_candidates'], [])
        with self.assertRaises(p.PortfolioError):
            convert('arbitrary prose, not records', STAMP)


    def test_explicit_conflict_and_local_field_exclusion(self):
        text = TEXT.replace(
            '- Related projects: [C] Synthetic mention, not a dependency.',
            '- Related projects: [C] Synthetic mention, not a dependency.\n'
            '- Notes / gaps: [C] Name match only; disputed candidate, not identity.\n'
            '- [Z] Synthetic local finding, excluded from bootstrap.')
        manifest, _, report = convert(text, STAMP)
        candidates = [c for c in manifest['claims'] if c['predicate'] == 'candidate-same-as']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['state'], 'disputed')
        self.assertEqual(report['conflicts'], 1)
        self.assertEqual(len(report['rejected_fields']), 1)
        self.assertNotIn('Synthetic local finding', p.canonical(manifest))
        self.assertFalse(report['proposed_relationships'])
        with tempfile.TemporaryDirectory() as tmp:
            p.import_manifest(Path(tmp) / 'private.sqlite', manifest, 0)


@unittest.skipUnless(os.environ.get('INTERGRAPH_PRIVATE_INVENTORY'), 'explicit private acceptance path not supplied')
class PrivateAcceptanceTest(unittest.TestCase):
    def test_explicit_private_inventory_only(self):
        # Do not emit labels, paths, counts, or input fragments on failure in CI.
        try:
            text = Path(os.environ['INTERGRAPH_PRIVATE_INVENTORY']).read_text()
            manifest, _, _ = convert(text, STAMP)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'portfolio.sqlite'
                receipt = p.import_manifest(path, manifest, 0)
                if p.import_manifest(path, manifest, 99) != receipt:
                    raise RuntimeError()
                with closing(p.open_readonly(path)) as con:
                    archive = p.export_archive(con)
                other = Path(tmp) / 'restored.sqlite'
                p.import_archive(other, archive, 0)
                with closing(p.open_readonly(other)) as con:
                    if p.export_archive(con) != archive:
                        raise RuntimeError()
        except Exception:
            raise AssertionError('private acceptance failed; inspect locally without publishing source data') from None
