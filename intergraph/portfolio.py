"""Durable private portfolio storage, independent of the disposable ecosystem.

Only explicit imports write. Readers never initialize or migrate a database.
Records and receipts are append-only; corrections are new sourced claims.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

VERSION = 1
KINDS = {'project', 'repository', 'organization', 'forge-account', 'unclassified',
         'person', 'steward-bot', 'infrastructure', 'knowledge-store', 'ecosystem', 'bead'}
STATES = {'reported', 'confirmed', 'disputed', 'retracted'}
REASONS = {'unknown', 'not-applicable', 'not-visible', 'conflicting-identity', 'unverified-alias'}
# An explicit registry, not a general-purpose inference engine.
RELATIONS = {
    'implemented-by': {('project', 'repository')},
    'part-of': {('project', 'project'), ('project', 'ecosystem')},
    'member-of': {('repository', 'ecosystem')},
    'stewards': {(a, b) for a in ('person', 'steward-bot')
                for b in ('project', 'infrastructure', 'knowledge-store')},
    'owns': {('organization', b) for b in ('project', 'repository')},
    'depends-on': {('project', 'project'), ('repository', 'repository')},
    'tracks': {('bead', 'project')},
    'runs-on': {('project', 'infrastructure')},
    'based-on': {('knowledge-store', 'project')},
    'hosted-under': {('repository', 'forge-account')},
    'supported-by': {('project', 'project')},
    'candidate-same-as': {(a, b) for a in KINDS for b in KINDS},
}
SCALARS = {'alias', 'description', 'classification', 'status', 'purpose',
           'candidate-external-id', 'planned-intent', 'archived', 'url', 'subtype', 'inventory-field'}
TABLES = ('sources', 'entities', 'claims', 'gaps', 'links')
FIELDS = {
    'sources': {'id', 'uri', 'content_hash', 'retrieved_at', 'visibility', 'source_class'},
    'entities': {'id', 'kind', 'label', 'revision', 'created_at', 'retired_at', 'visibility'},
    'claims': {'id', 'subject_id', 'predicate', 'object_id', 'value_json', 'state',
               'visibility', 'source_id', 'source_locator', 'observed_at', 'valid_from',
               'valid_to', 'supersedes', 'qualifiers_json', 'origin_class', 'origin_ref',
               'writer', 'batch_id'},
    'gaps': {'id', 'subject_id', 'field', 'reason', 'status', 'source_id',
             'resolution_claim_id', 'visibility'},
    'links': {'id', 'entity_id', 'ecosystem_node_id', 'relation', 'source_id', 'visibility'},
}
REQUIRED = {
    'sources': {'id', 'uri', 'content_hash', 'retrieved_at', 'source_class'},
    'entities': {'id', 'kind', 'label', 'created_at'},
    'claims': {'id', 'subject_id', 'predicate', 'state', 'source_id', 'source_locator',
               'observed_at', 'origin_class', 'origin_ref', 'writer'},
    'gaps': {'id', 'subject_id', 'field', 'reason', 'status', 'source_id'},
    'links': {'id', 'entity_id', 'ecosystem_node_id', 'relation', 'source_id'},
}


class PortfolioError(ValueError):
    """Safe, non-content-bearing validation or lifecycle error."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise PortfolioError('duplicate JSON key')
        result[k] = v
    return result


def loads(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              PortfolioError('non-finite JSON value')))
    except (ValueError, TypeError) as e:
        raise PortfolioError('invalid JSON document') from e


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise PortfolioError('expected nonempty text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise PortfolioError('text must be valid Unicode') from None


def _uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PortfolioError('IDs must be canonical persisted UUIDs') from None


def _time(value):
    _text(value)
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise PortfolioError('timestamp requires ISO 8601 and explicit timezone') from None
    # Preserve original zone in the immutable manifest; SQL projection is UTC.
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _visibility(value):
    if value != 'private':
        raise PortfolioError('only private visibility is supported; publication is unavailable')


def validate(manifest):
    """Validate syntax without a DB. Unknown fields fail closed (no silent losses)."""
    if not isinstance(manifest, dict) or set(manifest) != {'version', 'batch', *TABLES}:
        raise PortfolioError('expected complete versioned manifest envelope')
    if type(manifest['version']) is not int or manifest['version'] != VERSION:
        raise PortfolioError('unsupported manifest version')
    batch = manifest['batch']
    if not isinstance(batch, dict) or not {'id', 'source_id', 'actor'} <= set(batch) \
            or set(batch) - {'id', 'source_id', 'actor', 'visibility'}:
        raise PortfolioError('invalid batch metadata')
    for k in ('id', 'source_id', 'actor'):
        _text(batch[k])
    _visibility(batch.get('visibility', 'private'))
    for table in TABLES:
        rows = manifest[table]
        if not isinstance(rows, list):
            raise PortfolioError('record collection must be an array')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not REQUIRED[table] <= set(row) \
                    or set(row) - FIELDS[table]:
                raise PortfolioError('missing or unregistered record field')
            _text(row['id'])
            if row['id'] in seen:
                raise PortfolioError('duplicate record ID')
            seen.add(row['id'])
            _visibility(row.get('visibility', 'private'))
            for key in REQUIRED[table] - {'revision'}:
                _text(row[key])
            for key in ('retrieved_at', 'created_at', 'retired_at', 'observed_at',
                        'valid_from', 'valid_to'):
                if row.get(key) is not None:
                    _time(row[key])
            if row.get('valid_from') is not None and row.get('valid_to') is not None:
                if datetime.fromisoformat(row['valid_from'].replace('Z', '+00:00')) > datetime.fromisoformat(row['valid_to'].replace('Z', '+00:00')):
                    raise PortfolioError('invalid source-asserted validity interval')
            if table == 'sources':
                if len(row['content_hash']) != 64 or any(c not in '0123456789abcdef' for c in row['content_hash']):
                    raise PortfolioError('source hash must be lowercase SHA256')
            if table == 'entities':
                _uuid(row['id'])
                if row['kind'] not in KINDS:
                    raise PortfolioError('unregistered entity kind')
                if type(row.get('revision', 1)) is not int or row.get('revision', 1) < 1:
                    raise PortfolioError('entity revision must be a positive integer')
            if table == 'claims':
                if row['state'] not in STATES:
                    raise PortfolioError('unregistered claim state')
                if (row.get('object_id') is not None) == (row.get('value_json') is not None):
                    raise PortfolioError('claim requires exactly one object or scalar value')
                if row.get('object_id') is not None:
                    _text(row['object_id'])
                    if row['predicate'] not in RELATIONS:
                        raise PortfolioError('unregistered relationship')
                elif row['predicate'] not in SCALARS:
                    raise PortfolioError('unregistered scalar predicate')
                if not isinstance(row.get('qualifiers_json', {}), dict):
                    raise PortfolioError('qualifiers must be a JSON object')
                if row.get('batch_id', batch['id']) != batch['id']:
                    raise PortfolioError('claim batch mismatch')
                if row['predicate'] == 'candidate-same-as' and row['state'] not in {'disputed', 'retracted'}:
                    raise PortfolioError('candidate identity must remain disputed')
                if row['state'] == 'retracted' and not row.get('supersedes'):
                    raise PortfolioError('retraction must identify a prior claim')
                if row['predicate'] == 'depends-on' and row['origin_class'] not in {
                        'manifest', 'code', 'manual-authoritative'}:
                    raise PortfolioError('dependency requires authoritative evidence')
            if table == 'gaps':
                if row['reason'] not in REASONS or row['status'] not in {'open', 'resolved'}:
                    raise PortfolioError('invalid gap reason or status')
                if (row['status'] == 'resolved') != (row.get('resolution_claim_id') is not None):
                    raise PortfolioError('resolved gap requires a resolution claim')
            if table == 'links' and row['relation'] != 'represents-project':
                raise PortfolioError('unregistered ecosystem bridge')
    try:
        canonical(manifest)
    except (ValueError, TypeError):
        raise PortfolioError('manifest is not finite JSON') from None
    return manifest


SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 portfolio_schema_version INTEGER NOT NULL, revision INTEGER NOT NULL);
INSERT OR IGNORE INTO portfolio_state VALUES(1,1,0);
CREATE TABLE IF NOT EXISTS portfolio_sources (
 id TEXT PRIMARY KEY, uri TEXT NOT NULL, content_hash TEXT NOT NULL,
 retrieved_at TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility='private'),
 source_class TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_entities (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, retired_at TEXT, visibility TEXT NOT NULL CHECK(visibility='private'));
CREATE TABLE IF NOT EXISTS portfolio_batches (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
 actor TEXT NOT NULL, imported_at TEXT NOT NULL, base_revision INTEGER NOT NULL,
 manifest_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status='committed'),
 visibility TEXT NOT NULL CHECK(visibility='private'), manifest_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_claims (
 id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES portfolio_entities(id),
 predicate TEXT NOT NULL, object_id TEXT REFERENCES portfolio_entities(id), value_json TEXT,
 state TEXT NOT NULL CHECK(state IN ('reported','confirmed','disputed','retracted')),
 visibility TEXT NOT NULL CHECK(visibility='private'),
 source_id TEXT NOT NULL REFERENCES portfolio_sources(id), source_locator TEXT NOT NULL,
 observed_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
 supersedes TEXT REFERENCES portfolio_claims(id), qualifiers_json TEXT NOT NULL,
 origin_class TEXT NOT NULL, origin_ref TEXT NOT NULL, writer TEXT NOT NULL,
 batch_id TEXT NOT NULL REFERENCES portfolio_batches(id) DEFERRABLE INITIALLY DEFERRED,
 CHECK((object_id IS NULL) != (value_json IS NULL)));
CREATE TABLE IF NOT EXISTS portfolio_gaps (
 id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES portfolio_entities(id),
 field TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
 resolution_claim_id TEXT REFERENCES portfolio_claims(id),
 visibility TEXT NOT NULL CHECK(visibility='private'));
CREATE TABLE IF NOT EXISTS portfolio_links (
 id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES portfolio_entities(id),
 ecosystem_node_id TEXT NOT NULL, relation TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
 visibility TEXT NOT NULL CHECK(visibility='private'));
CREATE INDEX IF NOT EXISTS portfolio_claim_subject ON portfolio_claims(subject_id);
CREATE INDEX IF NOT EXISTS portfolio_claim_object ON portfolio_claims(object_id);
CREATE INDEX IF NOT EXISTS portfolio_claim_supersedes ON portfolio_claims(supersedes);
"""


def _schema(con):
    """Called only within an explicit write transaction, never by readers."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('nodes','edges','meta')").fetchone():
        raise PortfolioError('portfolio requires a separate database file')
    for statement in SCHEMA.split(';'):
        if statement.strip():
            con.execute(statement)
    if con.execute('SELECT portfolio_schema_version FROM portfolio_state').fetchone()[0] != VERSION:
        raise PortfolioError('unsupported portfolio schema version')
    con.execute('DROP TRIGGER IF EXISTS immutable_entities_UPDATE')
    for table in (*TABLES, 'batches'):
        for action in ('UPDATE', 'DELETE'):
            if table == 'entities' and action == 'UPDATE':
                continue
            con.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                BEFORE {action} ON portfolio_{table}
                BEGIN SELECT RAISE(ABORT, 'portfolio records are immutable'); END''')


def _check(con):
    try:
        row = con.execute('SELECT portfolio_schema_version FROM portfolio_state WHERE singleton=1').fetchone()
        if not row or row[0] != VERSION:
            raise PortfolioError('unsupported portfolio schema version')
    except sqlite3.DatabaseError:
        raise PortfolioError('portfolio not initialized') from None


def open_readonly(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PortfolioError('portfolio not initialized')
    try:
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA foreign_keys=ON')
        _check(con)
        return con
    except Exception:
        if 'con' in locals():
            con.close()
        raise


def default_path():
    return Path(os.environ.get('INTERGRAPH_PORTFOLIO_DB',
                str(Path.home() / '.local/share/intergraph/portfolio.sqlite')))


def revision(con):
    return con.execute('SELECT revision FROM portfolio_state WHERE singleton=1').fetchone()[0]


def _project(table, row, batch):
    row = dict(row)
    row.setdefault('visibility', 'private')
    if table == 'entities':
        row.setdefault('revision', 1)
        row.setdefault('retired_at', None)
    if table == 'claims':
        for key in ('object_id', 'value_json', 'valid_from', 'valid_to', 'supersedes'):
            row.setdefault(key, None)
        if row['value_json'] is not None:
            row['value_json'] = canonical(row['value_json'])
        row['qualifiers_json'] = canonical(row.get('qualifiers_json', {}))
        row['batch_id'] = batch['id']
    if table == 'gaps':
        row.setdefault('resolution_claim_id', None)
    for key in ('retrieved_at', 'created_at', 'retired_at', 'observed_at', 'valid_from', 'valid_to'):
        if row.get(key) is not None:
            row[key] = _time(row[key])
    return row


def _insert(con, table, row):
    keys = sorted(row)
    # Table and column names come only from this module's registry.
    con.execute(f"INSERT INTO portfolio_{table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [row[k] for k in keys])


def _apply(con, manifest, expected_revision, imported_at=None):
    digest = hashlib.sha256(canonical(manifest).encode()).hexdigest()
    batch = manifest['batch']
    existing = con.execute('SELECT receipt_json FROM portfolio_batches WHERE manifest_hash=?', (digest,)).fetchone()
    if existing:
        return loads(existing[0])
    if con.execute('SELECT 1 FROM portfolio_batches WHERE id=?', (batch['id'],)).fetchone():
        raise PortfolioError('batch ID reused with different content')
    current = revision(con)
    if type(expected_revision) is not int or expected_revision != current:
        raise PortfolioError('stale or missing expected revision')
    counts = {}
    updates = {'entities': 0}
    for table in TABLES:
        counts[table] = 0
        for raw in manifest[table]:
            row = _project(table, raw, batch)
            old = con.execute(f'SELECT * FROM portfolio_{table} WHERE id=?', (row['id'],)).fetchone()
            if old:
                if table == 'claims' and 'batch_id' not in raw:
                    row['batch_id'] = old['batch_id']
                if dict(old) != row:
                    if table != 'entities' or row['revision'] != old['revision'] + 1 or row['created_at'] != old['created_at']:
                        raise PortfolioError('immutable record change or stale entity revision')
                    keys = sorted(set(row) - {'id'})
                    con.execute(f"UPDATE portfolio_entities SET {','.join(k + '=?' for k in keys)} WHERE id=?",
                                [row[k] for k in keys] + [row['id']])
                    updates['entities'] += 1
                continue
            if table == 'entities' and row['revision'] != 1:
                raise PortfolioError('new entity revision must be one')
            if table == 'claims':
                subject = con.execute('SELECT kind FROM portfolio_entities WHERE id=?', (row['subject_id'],)).fetchone()
                obj = con.execute('SELECT kind FROM portfolio_entities WHERE id=?', (row['object_id'],)).fetchone()
                if not subject:
                    raise PortfolioError('unknown claim subject')
                if row['object_id'] is not None and (not obj or (subject[0], obj[0]) not in RELATIONS[row['predicate']]):
                    raise PortfolioError('invalid relationship endpoints')
                if row['supersedes']:
                    prior = con.execute('SELECT * FROM portfolio_claims WHERE id=?', (row['supersedes'],)).fetchone()
                    if not prior or prior['subject_id'] != row['subject_id'] or prior['predicate'] != row['predicate']:
                        raise PortfolioError('supersession must refer to prior claim on same subject and predicate')
                    if row['state'] == 'retracted' and (prior['object_id'], prior['value_json']) != (row['object_id'], row['value_json']):
                        raise PortfolioError('retraction must preserve original assertion')
            if table == 'gaps' and row['resolution_claim_id'] is not None:
                resolution = con.execute('SELECT subject_id FROM portfolio_claims WHERE id=?', (row['resolution_claim_id'],)).fetchone()
                if not resolution or resolution[0] != row['subject_id']:
                    raise PortfolioError('gap resolution must cite a claim on the same subject')
            if table == 'links':
                target = con.execute('SELECT kind FROM portfolio_entities WHERE id=?', (row['entity_id'],)).fetchone()
                if not target or target[0] != 'project':
                    raise PortfolioError('bridge must represent a project')
            _insert(con, table, row)
            counts[table] += 1
    # Entity-kind corrections must not invalidate any stored typed relationship.
    for claim in con.execute('''SELECT c.predicate, a.kind, b.kind FROM portfolio_claims c
            JOIN portfolio_entities a ON a.id=c.subject_id
            JOIN portfolio_entities b ON b.id=c.object_id'''):
        if (claim[1], claim[2]) not in RELATIONS[claim[0]]:
            raise PortfolioError('entity correction invalidates a typed relationship')
    if con.execute("SELECT 1 FROM portfolio_links l JOIN portfolio_entities e ON e.id=l.entity_id WHERE e.kind != 'project'").fetchone():
        raise PortfolioError('entity correction invalidates an ecosystem bridge')
    stamp = _time(imported_at) if imported_at else datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    receipt = {'id': batch['id'], 'source_id': batch['source_id'], 'actor': batch['actor'],
               'imported_at': stamp, 'base_revision': current, 'revision': current + 1,
               'manifest_hash': digest, 'status': 'committed', 'visibility': 'private',
               'inserted': counts, 'updated': updates}
    _insert(con, 'batches', {k: receipt[k] for k in ('id', 'source_id', 'actor', 'imported_at',
            'base_revision', 'manifest_hash', 'status', 'visibility')} |
            {'manifest_json': canonical(manifest), 'receipt_json': canonical(receipt)})
    if con.execute('PRAGMA foreign_key_check').fetchone():
        raise PortfolioError('unresolved record reference')
    con.execute('UPDATE portfolio_state SET revision=? WHERE singleton=1', (current + 1,))
    return receipt


def import_manifest(path, manifest, expected_revision=None, dry_run=False):
    """Atomic explicit import; dry runs operate entirely on an in-memory copy."""
    validate(manifest)
    path = Path(path).expanduser().resolve()
    if type(expected_revision) is not int and not dry_run:
        raise PortfolioError('expected revision is required')
    if dry_run:
        con = sqlite3.connect(':memory:')
        if path.exists():
            with closing(open_readonly(path)) as source:
                source.backup(con)
    else:
        # Schema creation is an import side effect only; never create parent trees.
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        elif path.stat().st_mode & 0o077:
            raise PortfolioError('portfolio file must be accessible only to its owner')
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        _schema(con)
        result = _apply(con, manifest, revision(con) if dry_run and expected_revision is None else expected_revision)
        if dry_run:
            result = dict(result, dry_run=True, gaps=_rows(con, 'gaps'))
            con.rollback()
        else:
            con.commit()
        return result
    except (sqlite3.DatabaseError, OverflowError):
        con.rollback()
        raise PortfolioError('portfolio import aborted; invalid data or database state') from None
    finally:
        con.close()


def _rows(con, table):
    rows = [dict(r) for r in con.execute(f'SELECT * FROM portfolio_{table} ORDER BY id')]
    for row in rows:
        for key in ('value_json', 'qualifiers_json'):
            if key in row and row[key] is not None:
                row[key] = loads(row[key])
    return rows


def export_archive(con):
    """Lossless private archive: original manifests, original receipts, and revision."""
    # One read snapshot, even if an explicit local importer is active.
    con.execute('BEGIN')
    try:
        return {'format': 'intergraph-portfolio-archive', 'version': VERSION,
                'visibility': 'private', 'revision': revision(con),
                'batches': [{'manifest': loads(r['manifest_json']), 'receipt': loads(r['receipt_json'])}
                            for r in con.execute('SELECT * FROM portfolio_batches ORDER BY base_revision')]}
    finally:
        con.rollback()


def validate_archive(archive):
    if not isinstance(archive, dict) or set(archive) != {
            'format', 'version', 'visibility', 'revision', 'batches'} \
            or archive['format'] != 'intergraph-portfolio-archive' \
            or type(archive['version']) is not int or archive['version'] != VERSION:
        raise PortfolioError('invalid archive envelope')
    _visibility(archive['visibility'])
    if type(archive['revision']) is not int or archive['revision'] < 0 \
            or not isinstance(archive['batches'], list) \
            or archive['revision'] != len(archive['batches']):
        raise PortfolioError('invalid archive revision')
    for batch in archive['batches']:
        if not isinstance(batch, dict) or set(batch) != {'manifest', 'receipt'}:
            raise PortfolioError('invalid archived batch')
        validate(batch['manifest'])
        if not isinstance(batch['receipt'], dict) or 'imported_at' not in batch['receipt']:
            raise PortfolioError('invalid archived receipt')
        _time(batch['receipt']['imported_at'])
    return archive


def import_archive(path, archive, expected_revision=None, dry_run=False):
    """Restore a lossless archive atomically to an empty store, or replay unchanged.

    Archives are recovery transports, not merge instructions. Each original batch
    is fully revalidated; its receipt and manifest hash must reproduce exactly.
    """
    validate_archive(archive)
    # Validate the entire replay before even opening a destination for writing.
    with closing(sqlite3.connect(':memory:')) as scratch:
        scratch.row_factory = sqlite3.Row
        scratch.execute('PRAGMA foreign_keys=ON')
        scratch.execute('BEGIN IMMEDIATE')
        _schema(scratch)
        for index, entry in enumerate(archive['batches']):
            receipt = _apply(scratch, entry['manifest'], index, entry['receipt']['imported_at'])
            if receipt != entry['receipt']:
                raise PortfolioError('archive receipt does not reproduce')
    path = Path(path).expanduser().resolve()
    if type(expected_revision) is not int and not dry_run:
        raise PortfolioError('expected revision is required')
    if dry_run:
        con = sqlite3.connect(':memory:')
        if path.exists():
            with closing(open_readonly(path)) as source:
                source.backup(con)
    else:
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        elif path.stat().st_mode & 0o077:
            raise PortfolioError('portfolio file must be accessible only to its owner')
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        _schema(con)
        current = revision(con)
        old = [{'manifest': loads(r['manifest_json']), 'receipt': loads(r['receipt_json'])}
               for r in con.execute('SELECT * FROM portfolio_batches ORDER BY base_revision')]
        if old == archive['batches']:
            result = {'revision': current, 'visibility': 'private', 'replayed': True}
        else:
            if current != 0 or (expected_revision is not None and expected_revision != current):
                raise PortfolioError('archive restore requires empty store at expected revision')
            for index, entry in enumerate(archive['batches']):
                _apply(con, entry['manifest'], index, entry['receipt']['imported_at'])
            result = {'revision': revision(con), 'visibility': 'private', 'replayed': False}
        if dry_run:
            result.update(dry_run=True, gaps=_rows(con, 'gaps'))
            con.rollback()
        else:
            con.commit()
        return result
    except sqlite3.DatabaseError:
        con.rollback()
        raise PortfolioError('portfolio restore aborted; invalid database state') from None
    finally:
        con.close()


def import_document(path, document, expected_revision=None, dry_run=False):
    if isinstance(document, dict) and document.get('format') == 'intergraph-portfolio-archive':
        return import_archive(path, document, expected_revision, dry_run)
    return import_manifest(path, document, expected_revision, dry_run)


def _claims(con):
    claims = _rows(con, 'claims')
    superseded = {c['supersedes'] for c in claims if c['supersedes']}
    sources = {s['id']: s for s in _rows(con, 'sources')}
    for c in claims:
        c['effective'] = c['id'] not in superseded and c['state'] != 'retracted'
        c['source'] = sources[c['source_id']]
    return claims


def _output(con, **data):
    return {'revision': revision(con), 'visibility': 'private', **data}


def find(con, query='', kind=None, state=None):
    if not isinstance(query, str) or kind is not None and kind not in KINDS \
            or state is not None and state not in STATES:
        raise PortfolioError('invalid find filter')
    claims = _claims(con)
    result = []
    for entity in _rows(con, 'entities'):
        own = [c for c in claims if c['subject_id'] == entity['id']]
        aliases = [c['value_json'] for c in own if c['predicate'] == 'alias' and c['effective']]
        if kind is not None and entity['kind'] != kind:
            continue
        if state is not None and not any(c['state'] == state for c in own):
            continue
        if query.casefold() not in entity['label'].casefold() \
                and query != entity['id'] \
                and not any(isinstance(a, str) and query.casefold() in a.casefold() for a in aliases):
            continue
        result.append(dict(entity, claims=own))
    return _output(con, entities=result)


def neighbors(con, id, direction='both', predicates=None, states=None, include_history=False):
    _text(id)
    if direction not in {'in', 'out', 'both'} or type(include_history) is not bool:
        raise PortfolioError('invalid neighbor options')
    for values, allowed in ((predicates, RELATIONS), (states, STATES)):
        if values is not None and (not isinstance(values, list) or
                any(not isinstance(x, str) or x not in allowed for x in values)):
            raise PortfolioError('invalid neighbor filter')
    predicates = set(RELATIONS) - {'candidate-same-as'} if predicates is None else set(predicates)
    states = {'reported', 'confirmed', 'disputed'} if states is None else set(states)
    claims = [c for c in _claims(con) if c['object_id'] is not None and
              c['predicate'] in predicates and c['state'] in states and
              (include_history or c['effective']) and
              ((direction in {'out', 'both'} and c['subject_id'] == id) or
               (direction in {'in', 'both'} and c['object_id'] == id))]
    return _output(con, claims=claims)


def gaps(con, subject_id=None, ecosystem_path=None):
    if subject_id is not None:
        _text(subject_id)
    rows = [r for r in _rows(con, 'gaps') if subject_id is None or r['subject_id'] == subject_id]
    for row in rows:
        row['source'] = dict(con.execute('SELECT * FROM portfolio_sources WHERE id=?', (row['source_id'],)).fetchone())
    links = [r for r in _rows(con, 'links') if subject_id is None or r['entity_id'] == subject_id]
    ecosystem = None
    try:
        if ecosystem_path is not None and Path(ecosystem_path).is_file():
            uri = Path(ecosystem_path).resolve().as_uri() + '?mode=ro'
            ecosystem = sqlite3.connect(uri, uri=True)
            ecosystem.execute('PRAGMA query_only=ON')
        for link in links:
            link['endpoint_status'] = 'unresolved'
            if ecosystem is not None:
                exists = ecosystem.execute('SELECT 1 FROM nodes WHERE id=?', (link['ecosystem_node_id'],)).fetchone()
                link['endpoint_status'] = 'resolved' if exists else 'dangling'
    except sqlite3.DatabaseError:
        for link in links:
            link['endpoint_status'] = 'unresolved'
    finally:
        if ecosystem is not None:
            ecosystem.close()
    return _output(con, gaps=rows, bridges=links)
