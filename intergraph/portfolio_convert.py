"""Conservative Markdown bootstrap into a *reviewable*, private manifest.

This does not import a DB or interpret diagrams, grouping, names, or prose as
relationships. Detailed Name/Repo URL records and compact repository bullets
are recognized. Other fields survive as quoted, reported carrier claims. A
private sidecar persists UUIDs and snapshot lineage; review it alongside the
manifest. Never put the input, sidecar, manifest, or report in a public repo.
"""
from __future__ import annotations

import hashlib
import re
from uuid import uuid4, uuid5, UUID

from . import portfolio as p

URL = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)')
COMPACT = re.compile(r'^- \[([^]]+)\]\((https://github\.com/[^)]+)\)(.*)$')
FIELD = re.compile(r'^- ([^:]+):\s*(.*)$')
METADATA = {'GitHub description', 'Visibility', 'Last push', 'Language',
            'Last metadata update', 'Repository flags'}


def _url(value):
    match = URL.search(value)
    if not match:
        return None
    return f'https://github.com/{match[1]}/{match[2].removesuffix(".git")}'.rstrip('/')


def parse(text):
    """Return records, explicit exclusions, and unparsed structural warnings."""
    lines = text.splitlines()
    headings = []
    records, excluded, rejected = [], [], []
    current = None

    def finish():
        nonlocal current
        if current:
            # Local checkout observations are not a supported bootstrap source.
            if any(key in {'Local path', 'Configured origin', 'Directory mtime'} for _, key, _ in current['fields']):
                excluded.append({'line': current['line'], 'reason': 'local-observation record'})
            else:
                records.append(current)
            current = None

    for n, line in enumerate(lines, 1):
        if line.startswith('#'):
            finish()
            depth = len(line) - len(line.lstrip('#'))
            headings = headings[:depth - 1] + [line[depth:].strip()]
        compact = COMPACT.match(line)
        if compact:
            finish()
            records.append({'key': 'compact:' + '/'.join(headings) + ':' + (_url(compact[2]) or compact[2]).casefold(),
                            'line': n, 'name': compact[1], 'url': _url(compact[2]),
                            'compact': True, 'fields': [(n, 'Repository entry', line)]})
            continue
        if re.match(r'^- \[Z\]', line):
            rejected.append({'line': n, 'reason': 'local-observation field'})
            continue
        field = FIELD.match(line)
        if field and field[1] == 'Name':
            finish()
            current = {'key': 'detailed:' + '/'.join(headings), 'line': n,
                       'name': field[2], 'url': None, 'compact': False, 'fields': []}
        if field and current:
            current['fields'].append((n, field[1], field[2]))
            if field[1] == 'Repo URL':
                current['url'] = _url(field[2])
        elif current and line.strip():
            # Keep multiline fields rather than silently losing prose.
            current['fields'].append((n, 'Continuation', line))
    finish()
    keys = [r['key'] for r in records]
    if len(keys) != len(set(keys)):
        raise p.PortfolioError('ambiguous duplicate carrier keys; review headings before conversion')
    return records, excluded, rejected


def convert(text, observed_at, sidecar=None):
    """Pure conversion: returns (manifest, proposed_sidecar, validation_report).

    Sidecar is not overwritten; caller must retain the proposed copy only after
    accepting the import. Renames/transfers require reviewed key redirects.
    No GitHub numeric IDs, ownership, dependencies, or active status are inferred.
    """
    p._time(observed_at)
    if sidecar is not None and not isinstance(sidecar, dict):
        raise p.PortfolioError('invalid identity sidecar')
    sidecar = p.loads(p.canonical(sidecar)) if sidecar is not None else {
        'version': 1, 'namespace': str(uuid4()), 'entities': {}, 'claims': {}, 'key_redirects': {}, 'snapshots': {}}
    if set(sidecar) != {'version', 'namespace', 'entities', 'claims', 'key_redirects', 'snapshots'} or sidecar['version'] != 1:
        raise p.PortfolioError('unsupported identity sidecar')
    try:
        namespace = UUID(sidecar['namespace'])
    except (ValueError, TypeError, AttributeError):
        raise p.PortfolioError('invalid sidecar namespace') from None
    if any(not isinstance(sidecar[k], dict) for k in ('entities', 'claims', 'key_redirects', 'snapshots')):
        raise p.PortfolioError('invalid identity sidecar collections')
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest in sidecar['snapshots']:
        saved = sidecar['snapshots'][digest]
        return saved['manifest'], sidecar, saved['report']
    source_id = 'carrier:' + digest
    records, excluded, rejected = parse(text)
    if not records:
        raise p.PortfolioError('no recognized inventory records')
    manifest = {'version': 1, 'batch': {'id': 'snapshot:' + digest, 'source_id': source_id,
                                     'actor': 'local-bootstrap-converter'},
                'sources': [{'id': source_id, 'uri': 'urn:intergraph:carrier:' + digest,
                             'content_hash': digest, 'retrieved_at': observed_at,
                             'source_class': 'mixed-carrier'}],
                'entities': [], 'claims': [], 'gaps': [], 'links': []}
    entities = {}
    snapshots = sidecar['claims']
    unresolved = 0
    proposed_relations = []
    urls = set()

    def entity(key, kind, label):
        key = sidecar['key_redirects'].get(key, key)
        if key not in sidecar['entities']:
            sidecar['entities'][key] = {'id': str(uuid4()), 'kind': kind, 'label': label,
                                       'created_at': observed_at}
        record = sidecar['entities'][key]
        if record['id'] in entities and entities[record['id']] != record:
            raise p.PortfolioError('conflicting identity sidecar records')
        entities[record['id']] = dict(record)
        return record['id']

    def claim(key, subject, predicate, value, line, field, raw, metadata=False, object_id=None):
        logical = key + ':' + subject + ':' + field
        if field == 'Continuation':
            logical += ':' + str(line)
        old = snapshots.get(logical)
        cid = str(uuid5(namespace, digest + ':' + logical))
        supersedes = old['id'] if old and old['snapshot'] != digest else old.get('supersedes') if old else None
        markers = set(re.findall(r'\[([GCF])\]', raw))
        # Carrier G is confirmed only as exact quoted forge metadata, never meaning.
        state = 'confirmed' if metadata and markers == {'G'} else 'reported'
        origin = 'forge-metadata' if state == 'confirmed' else 'reported'
        origin_url = re.search(r'https?://[^\s)>`]+', raw)
        row = {'id': cid, 'subject_id': subject, 'predicate': predicate,
               'value_json': value, 'state': state, 'source_id': source_id,
               'source_locator': 'line:' + str(line), 'observed_at': observed_at,
               'origin_class': origin, 'origin_ref': origin_url[0] if origin_url else 'unresolved:carrier-origin',
               'writer': 'local-bootstrap-converter',
               'qualifiers_json': {'field': field, 'raw': raw, 'carrier_markers': sorted(markers)}}
        if object_id is not None:
            row.pop('value_json')
            row['object_id'] = object_id
            row['state'] = 'disputed'
        if supersedes:
            row['supersedes'] = supersedes
        manifest['claims'].append(row)
        snapshots[logical] = {'id': cid, 'snapshot': digest, 'supersedes': supersedes}

    for record in records:
        key = sidecar['key_redirects'].get(record['key'], record['key'])
        repo = None
        if record['url']:
            urls.add(record['url'].casefold())
            repo = entity('repository:' + record['url'].casefold(), 'repository', record['url'].rsplit('/', 1)[-1])
        subject = repo if record['compact'] else entity(key + ':identity', 'unclassified', record['name'])
        for line, field, raw in record['fields']:
            target = repo if repo and field in METADATA else subject
            predicate = 'description' if field == 'GitHub description' else 'inventory-field'
            claim(key, target, predicate, raw, line, field, raw, metadata=target == repo and field in METADATA)
        if repo:
            # URL is a provisional external identifier, not a fetched numeric ID.
            claim(key, repo, 'candidate-external-id', record['url'], record['line'],
                  'candidate repository URL', record['url'])
        else:
            unresolved += 1
            manifest['gaps'].append({'id': str(uuid5(namespace, digest + ':' + key + ':repository-gap')),
                'subject_id': subject, 'field': 'repository', 'reason': 'unknown',
                'status': 'open', 'source_id': source_id})
        conflict = next(((line, raw) for line, field, raw in record['fields']
                         if field == 'Notes / gaps' and re.search(
                             r'disputed candidate|name match only|conflicting[- ]identity', raw, re.I)), None)
        alias_gap = next(((line, raw) for line, field, raw in record['fields']
                          if field == 'Notes / gaps' and re.search(r'unverified alias|unresolved alias', raw, re.I)), None)
        if conflict and repo and not record['compact']:
            line, raw = conflict
            claim(key, subject, 'candidate-same-as', None, line, 'identity-candidate', raw, object_id=repo)
            manifest['gaps'].append({'id': str(uuid5(namespace, digest + ':' + key + ':identity-gap')),
                'subject_id': subject, 'field': 'identity', 'reason': 'conflicting-identity',
                'status': 'open', 'source_id': source_id})
        if alias_gap:
            manifest['gaps'].append({'id': str(uuid5(namespace, digest + ':' + key + ':alias-gap')),
                'subject_id': subject, 'field': 'alias', 'reason': 'unverified-alias',
                'status': 'open', 'source_id': source_id})
        if not record['compact'] and repo and not conflict:
            proposed_relations.append({'subject_id': subject, 'object_id': repo,
                                       'predicate': 'implemented-by', 'line': record['line'],
                                       'status': 'requires identity and endpoint-kind review'})
    manifest['entities'] = list(entities.values())
    # Field values are quoted facts about the carrier, not ontology assertions.
    p.validate(manifest)
    report = {'visibility': 'private', 'source_hash': digest, 'parsed_records': len(records),
              'distinct_urls': len(urls), 'independently_reconciled_repository_ids': 0,
              'unresolved_numeric_ids': len(urls), 'unresolved_records': unresolved,
              'excluded_records': excluded, 'rejected_fields': rejected,
              'conflicts': sum(g['reason'] == 'conflicting-identity' for g in manifest['gaps']),
              'proposed_relationships': proposed_relations,
              'retraction_candidates': [],
              'warnings': ['All semantic identity/relationship decisions require review.',
                           'Omission never retracts; partial sources never propose deletions.',
                           'Unparsed diagrams/headings are not authoritative relationships.']}
    sidecar['snapshots'][digest] = {'manifest': manifest, 'report': report}
    return manifest, sidecar, report
