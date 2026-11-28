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


class PortfolioTransportTest(PortfolioTest):
    def test_archive_round_trip_and_tamper(self):
        self.apply()
        m = fixture()
        m['batch']['id'] = 'synthetic-correction'
        m['claims'][0].update(id='synthetic-correction', state='retracted', supersedes='synthetic-claim')
        self.apply(m, 1)
        with closing(p.open_readonly(self.path)) as con:
            archive = p.export_archive(con)
        other = Path(self.tmp.name) / 'restored.sqlite'
        p.import_document(other, archive, 0)
        self.assertEqual(p.import_document(other, archive, 999)['revision'], 2)
        with closing(p.open_readonly(other)) as con:
            self.assertEqual(p.export_archive(con), archive)
        tampered = copy.deepcopy(archive)
        tampered['batches'][1]['manifest']['entities'][0]['label'] = 'Synthetic tamper'
        untouched = Path(self.tmp.name) / 'untouched.sqlite'
        with self.assertRaises(p.PortfolioError):
            p.import_archive(untouched, tampered, 0)
        self.assertFalse(untouched.exists())

    def test_queries_evidence_filters_and_bridges(self):
        self.apply()
        with closing(p.open_readonly(self.path)) as con:
            found = p.find(con, 'private')
            self.assertEqual(len(found['entities']), 1)
            self.assertEqual(found['visibility'], 'private')
            self.assertEqual(p.neighbors(con, uid(1))['claims'], [])
            result = p.neighbors(con, uid(1), predicates=['candidate-same-as'])
            self.assertEqual(result['claims'][0]['state'], 'disputed')
            self.assertEqual(result['claims'][0]['source']['content_hash'], 'a' * 64)
            missing = Path(self.tmp.name) / 'missing.sqlite'
            self.assertEqual(p.gaps(con, ecosystem_path=missing)['bridges'][0]['endpoint_status'], 'unresolved')
            self.assertFalse(missing.exists())
            ecosystem = Path(self.tmp.name) / 'ecosystem.sqlite'
            with closing(store.connect(ecosystem)) as legacy:
                self.assertEqual(p.gaps(con, ecosystem_path=ecosystem)['bridges'][0]['endpoint_status'], 'dangling')
                legacy.execute("INSERT INTO nodes(id,kind,label) VALUES ('synthetic-missing-plugin','plugin','Synthetic')")
                legacy.commit()
                self.assertEqual(p.gaps(con, ecosystem_path=ecosystem)['bridges'][0]['endpoint_status'], 'resolved')
                store.reset(legacy)
                self.assertEqual(p.gaps(con, ecosystem_path=ecosystem)['bridges'][0]['endpoint_status'], 'dangling')
            self.assertEqual(len(p._rows(con, 'links')), 1)

    def test_mcp_absent_bad_args_and_no_mutations(self):
        def call(name, args):
            return portfolio_server.handle(self.path, {'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': args}})
        for name in ('graph_portfolio_find', 'graph_portfolio_gaps', 'graph_portfolio_neighbors'):
            result = call(name, {'id': uid(1)} if name.endswith('neighbors') else {})
            self.assertEqual(result['error']['message'], 'portfolio not initialized')
        self.assertFalse(self.path.exists())
        self.apply()
        before = self.path.read_bytes()
        for args in ({'public': True}, {'query': 3}, {'visibility': 'public'}, {'filtered': True}):
            self.assertIn('error', call('graph_portfolio_find', args))
        self.assertIn('error', call('portfolio_import', {}))
        result = call('graph_portfolio_find', {})
        self.assertEqual(len(json.loads(result['result']['content'][0]['text'])['entities']), 4)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_and_no_legacy_database_side_effects(self):
        import subprocess
        manifest = Path(self.tmp.name) / 'manifest.json'
        manifest.write_text(json.dumps(fixture()))
        def run(*args):
            return subprocess.run(['python3', 'bin/intergraph', *args], text=True, capture_output=True)
        result = run('portfolio-import', str(manifest), '--portfolio-db', str(self.path), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(run('portfolio-import', str(manifest), '--portfolio-db', str(self.path)).returncode, 2)
        self.assertEqual(run('portfolio-import', str(manifest), '--portfolio-db', str(self.path), '--expected-revision', '0').returncode, 0)
        result = run('portfolio-export', '--portfolio-db', str(self.path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['revision'], 1)
        self.assertNotEqual(run('portfolio-export', '--portfolio-db', str(self.path), '--public').returncode, 0)
        from test_portfolio_convert import TEXT
        inventory = Path(self.tmp.name) / 'synthetic.md'
        inventory.write_text(TEXT)
        proposed = Path(self.tmp.name) / 'proposed.json'
        identities = Path(self.tmp.name) / 'identities.json'
        result = run('portfolio-convert', str(inventory), '--observed-at', STAMP,
                     '--output-manifest', str(proposed), '--output-sidecar', str(identities))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(proposed.stat().st_mode & 0o777, 0o600)
        self.assertEqual(identities.stat().st_mode & 0o777, 0o600)

    def test_interrupted_transaction_not_visible(self):
        self.apply()
        with closing(sqlite3.connect(self.path)) as writer:
            writer.row_factory = sqlite3.Row
            writer.execute('PRAGMA foreign_keys=ON')
            writer.execute('BEGIN IMMEDIATE')
            m = fixture()
            m['batch']['id'] = 'synthetic-interrupted'
            m['claims'][0].update(id='synthetic-new-claim')
            p._apply(writer, m, 1)
            self.assertEqual(self.count(), (1, 4, 1))
            writer.rollback()
        self.assertEqual(self.count(), (1, 4, 1))

    def test_legacy_file_is_not_a_portfolio_target(self):
        with closing(store.connect(self.path)):
            pass
        self.path.chmod(0o600)
        with self.assertRaisesRegex(p.PortfolioError, 'separate database'):
            self.apply()
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'portfolio_%'").fetchone()[0], 0)


    def test_revisioned_entity_correction_and_recovery(self):
        self.apply()
        m = fixture()
        m['batch']['id'] = 'synthetic-entity-correction'
        m['entities'][0].update(label='Synthetic revised private label', revision=2)
        result = self.apply(m, 1)
        self.assertEqual(result['updated'], {'entities': 1})
        with closing(p.open_readonly(self.path)) as con:
            self.assertEqual(p.find(con, 'revised')['entities'][0]['revision'], 2)
            archive = p.export_archive(con)
        other = Path(self.tmp.name) / 'replayed.sqlite'
        p.import_archive(other, archive, 0)
        with closing(p.open_readonly(other)) as con:
            self.assertEqual(p.export_archive(con), archive)
        m['batch']['id'] = 'synthetic-stale-entity-correction'
        m['entities'][0]['label'] = 'Synthetic another label'
        with self.assertRaises(p.PortfolioError):
            self.apply(m, 2)

    def test_schema_initialization_twice_and_typed_corrections(self):
        with closing(sqlite3.connect(':memory:')) as con:
            con.execute('BEGIN IMMEDIATE')
            p._schema(con)
            before = con.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
            p._schema(con)
            self.assertEqual(con.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall(), before)
        m = fixture()
        m['claims'][0].update(predicate='implemented-by', state='reported')
        self.apply(m)
        correction = fixture()
        correction['batch']['id'] = 'synthetic-bad-kind-correction'
        correction['claims'] = []
        correction['entities'][1].update(kind='organization', revision=2)
        with self.assertRaises(p.PortfolioError):
            self.apply(correction, 1)
        self.assertEqual(self.count(), (1, 4, 1))


if __name__ == '__main__':
    unittest.main()
