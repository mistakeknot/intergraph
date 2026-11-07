"""Synthetic, local-only portfolio tests. No real inventory is loaded."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import UUID

from intergraph import portfolio as p, portfolio_server, server, store

STAMP = '2026-01-01T00:00:00Z'


def uid(n):
    return str(UUID(int=n))


def fixture():
    return {'version': 1, 'batch': {'id': 'synthetic-batch', 'source_id': 'synthetic-source',
                                  'actor': 'synthetic-importer'},
            'sources': [{'id': 'synthetic-source', 'uri': 'urn:synthetic:inventory',
                         'content_hash': 'a' * 64, 'retrieved_at': STAMP, 'source_class': 'reported'}],
            'entities': [{'id': uid(1), 'kind': 'project', 'label': 'Synthetic private label', 'created_at': STAMP},
                         {'id': uid(2), 'kind': 'repository', 'label': 'Synthetic unknown remote', 'created_at': STAMP},
                         {'id': uid(3), 'kind': 'organization', 'label': 'Synthetic LLC', 'created_at': STAMP},
                         {'id': uid(4), 'kind': 'knowledge-store', 'label': 'Synthetic store', 'created_at': STAMP}],
            'claims': [{'id': 'synthetic-claim', 'subject_id': uid(1), 'predicate': 'candidate-same-as',
                        'object_id': uid(2), 'state': 'disputed', 'source_id': 'synthetic-source',
                        'source_locator': 'line:1', 'observed_at': STAMP, 'origin_class': 'reported',
                        'origin_ref': 'urn:synthetic:unverified', 'writer': 'synthetic-importer'}],
            'gaps': [{'id': 'synthetic-gap', 'subject_id': uid(1), 'field': 'repository',
                      'reason': 'conflicting-identity', 'status': 'open', 'source_id': 'synthetic-source'}],
            'links': [{'id': 'synthetic-bridge', 'entity_id': uid(1),
                       'ecosystem_node_id': 'synthetic-missing-plugin', 'relation': 'represents-project',
                       'source_id': 'synthetic-source'}]}


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'portfolio.sqlite'

    def apply(self, manifest=None, expected=0, **kw):
        return p.import_manifest(self.path, manifest or fixture(), expected, **kw)

    def count(self):
        with closing(p.open_readonly(self.path)) as con:
            return p.revision(con), len(p._rows(con, 'entities')), len(p._rows(con, 'claims'))

    def test_idempotent_and_immutable(self):
        first = self.apply()
        self.assertEqual(self.apply(expected=999), first)
        self.assertEqual(self.count(), (1, 4, 1))
        with closing(sqlite3.connect(self.path)) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE portfolio_claims SET state='confirmed'")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_no_files_or_writes(self):
        result = self.apply(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(len(result['gaps']), 1)
        self.assertFalse(self.path.exists())
        self.apply()
        before = self.path.read_bytes()
        self.apply(expected=123, dry_run=True)  # replay before revision check
        self.assertEqual(before, self.path.read_bytes())

    def test_atomic_stale_invalid_and_duplicate(self):
        self.apply()
        for mutate in ('stale', 'endpoint', 'duplicate', 'batch', 'immutable', 'partial'):
            m = fixture()
            m['batch']['id'] = 'synthetic-next'
            m['claims'][0]['id'] = 'synthetic-next-claim'
            expected = 1
            if mutate == 'stale':
                expected = 0
            elif mutate == 'endpoint':
                m['claims'][0]['predicate'] = 'implemented-by'
                m['claims'][0]['object_id'] = uid(3)
            elif mutate == 'duplicate':
                m['entities'].append(copy.deepcopy(m['entities'][0]))
            elif mutate == 'batch':
                m['batch']['id'] = 'synthetic-batch'
            elif mutate == 'immutable':
                m['entities'][0]['label'] = 'Synthetic changed label'
            elif mutate == 'partial':
                del m['sources']
            with self.subTest(mutate=mutate), self.assertRaises(p.PortfolioError):
                self.apply(m, expected)
            self.assertEqual(self.count(), (1, 4, 1))

    def test_retraction_and_missing_records(self):
        self.apply()
        m = fixture()
        m['batch']['id'] = 'synthetic-retract'
        m['claims'][0].update(id='synthetic-retraction', state='retracted', supersedes='synthetic-claim')
        m['gaps'] = []
        m['links'] = []
        self.apply(m, 1)
        self.assertEqual(self.count(), (2, 4, 2))
        with closing(p.open_readonly(self.path)) as con:
            self.assertEqual(len(p._rows(con, 'gaps')), 1)

    def test_fail_closed_publication_and_registry(self):
        for mutate in ('visibility', 'predicate', 'kind', 'dependency', 'identity'):
            m = fixture()
            if mutate == 'visibility':
                m['entities'][0]['visibility'] = 'public'
            elif mutate == 'predicate':
                m['claims'][0]['predicate'] = 'inferred-from-heading'
            elif mutate == 'kind':
                m['entities'][0]['kind'] = 'plugin'
            elif mutate == 'dependency':
                m['claims'][0]['predicate'] = 'depends-on'
            else:
                m['claims'][0]['state'] = 'confirmed'
            with self.subTest(mutate=mutate), self.assertRaises(p.PortfolioError):
                self.apply(m)
        self.assertFalse(self.path.exists())

    def test_foreign_keys_and_rollback(self):
        self.apply()
        m = fixture()
        m['batch']['id'] = 'synthetic-bad-reference'
        m['gaps'][0]['id'] = 'synthetic-new-gap'
        m['gaps'][0]['source_id'] = 'synthetic-missing-source'
        with self.assertRaises(p.PortfolioError):
            self.apply(m, 1)
        self.assertEqual(self.count(), (1, 4, 1))

    def test_reader_lifecycle_and_legacy_isolation(self):
        for method in ('initialize', 'tools/list'):
            portfolio_server.handle(self.path, {'id': 1, 'method': method})
        with self.assertRaisesRegex(p.PortfolioError, 'portfolio not initialized'):
            p.open_readonly(self.path)
        self.assertFalse(self.path.exists())
        legacy = Path(self.tmp.name) / 'ecosystem.sqlite'
        with closing(store.connect(legacy)) as con:
            server.handle(con, {'id': 1, 'method': 'tools/list'})
            self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'portfolio_%'").fetchone()[0], 0)
        self.assertFalse(self.path.exists())
        self.apply()
        with closing(store.connect(legacy)) as con:
            store.reset(con)
        self.assertEqual(self.count(), (1, 4, 1))
        with closing(p.open_readonly(self.path)) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM portfolio_entities')

    def test_duplicate_json_keys(self):
        with self.assertRaises(p.PortfolioError):
            p.loads('{"version":1,"version":1}')


if __name__ == '__main__':
    unittest.main()
