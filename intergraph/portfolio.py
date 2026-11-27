"""Versioned private portfolio data and its explicit SQLite write path."""

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

VERSION = 1
ARCHIVE_FORMAT = "intergraph-portfolio-archive"
KINDS = {
    "project", "repository", "organization", "forge-account", "unclassified",
    "person", "steward-bot", "infrastructure", "knowledge-store", "ecosystem", "bead",
}
STATES = {"reported", "confirmed", "disputed", "retracted"}
REASONS = {"unknown", "not-applicable", "not-visible", "conflicting-identity", "unverified-alias"}
RELATIONS = {
    "implemented-by": {("project", "repository")},
    "part-of": {("project", "project"), ("project", "ecosystem")},
    "member-of": {("repository", "ecosystem")},
    "stewards": {(actor, target) for actor in ("person", "steward-bot")
                 for target in ("project", "infrastructure", "knowledge-store")},
    "owns": {("organization", "project"), ("organization", "repository")},
    "depends-on": {("project", "project"), ("repository", "repository")},
    "tracks": {("bead", "project")},
    "runs-on": {("project", "infrastructure")},
    "based-on": {("knowledge-store", "project")},
    "hosted-under": {("repository", "forge-account")},
    "supported-by": {("project", "project")},
    "candidate-same-as": {(left, right) for left in KINDS for right in KINDS},
}
SCALARS = {
    "alias", "description", "classification", "status", "purpose", "candidate-external-id",
    "planned-intent", "archived", "url", "subtype", "inventory-field",
}
REQUIRED = {
    "sources": {"id", "uri", "content_hash", "retrieved_at", "source_class"},
    "entities": {"id", "kind", "label", "created_at"},
    "claims": {"id", "subject_id", "predicate", "state", "source_id", "source_locator",
               "observed_at", "origin_class", "origin_ref", "writer"},
    "gaps": {"id", "subject_id", "field", "reason", "status", "source_id"},
    "links": {"id", "entity_id", "ecosystem_node_id", "relation", "source_id"},
}
DEFAULTS = {
    "sources": {},
    "entities": {"revision": 1, "retired_at": None},
    "claims": {"object_id": None, "value_json": None, "valid_from": None, "valid_to": None,
               "supersedes": None, "qualifiers_json": {}},
    "gaps": {"resolution_claim_id": None},
    "links": {},
}
FIELDS = {table: required | DEFAULTS[table].keys() | {"visibility"}
          for table, required in REQUIRED.items()}
FIELDS["claims"].add("batch_id")
TABLES = tuple(REQUIRED)
TIMESTAMPS = ("created_at", "retired_at", "retrieved_at", "observed_at", "valid_from", "valid_to")


class PortfolioError(ValueError):
    """Validation and lifecycle messages safe to print without exposing portfolio data."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PortfolioError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise PortfolioError("non-finite JSON number")


def _finite_float(literal):
    number = float(literal)
    if not math.isfinite(number):
        _reject_constant(literal)
    return number


def loads(text):
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise PortfolioError("invalid JSON document") from None


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise PortfolioError("expected nonempty text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PortfolioError("expected valid Unicode text") from None


def _time(value):
    _text(value)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise PortfolioError("timestamp requires ISO 8601 and an explicit timezone") from None


def _private(mapping):
    if mapping.get("visibility", "private") != "private":
        raise PortfolioError("only private visibility is supported")


def _validate_claim(row, batch):
    for field in ("object_id", "supersedes"):
        if row.get(field) is not None:
            _text(row[field])
    relationship = row.get("object_id") is not None
    if relationship == (row.get("value_json") is not None):
        raise PortfolioError("claim requires exactly one object or scalar value")
    predicate = row["predicate"]
    if predicate not in (RELATIONS if relationship else SCALARS):
        raise PortfolioError("unregistered claim predicate")
    state = row["state"]
    if state not in STATES or predicate == "candidate-same-as" and state not in {"disputed", "retracted"}:
        raise PortfolioError("invalid state for claim")
    if state == "retracted" and row.get("supersedes") is None:
        raise PortfolioError("retraction requires a prior claim")
    if predicate == "depends-on" and row["origin_class"] not in {"manifest", "code", "manual-authoritative"}:
        raise PortfolioError("dependency requires authoritative evidence")
    if not isinstance(row.get("qualifiers_json", {}), dict):
        raise PortfolioError("qualifiers must be a JSON object")
    if row.get("batch_id", batch["id"]) != batch["id"]:
        raise PortfolioError("claim batch mismatch")
    if row.get("valid_from") is not None and row.get("valid_to") is not None:
        start = datetime.fromisoformat(_time(row["valid_from"]))
        end = datetime.fromisoformat(_time(row["valid_to"]))
        if start > end:
            raise PortfolioError("invalid source-asserted validity interval")


def _validate_record(table, row, batch):
    for field in REQUIRED[table]:
        _text(row[field])
    _private(row)
    for field in TIMESTAMPS:
        if row.get(field) is not None:
            _time(row[field])

    if table == "sources":
        if not re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]):
            raise PortfolioError("source hash must be lowercase SHA256")
    elif table == "entities":
        try:
            canonical_id = str(UUID(row["id"]))
        except ValueError:
            canonical_id = None
        if canonical_id != row["id"]:
            raise PortfolioError("entity ID must be a canonical UUID")
        if row["kind"] not in KINDS:
            raise PortfolioError("unregistered entity kind")
        entity_revision = row.get("revision", 1)
        if type(entity_revision) is not int or entity_revision < 1:
            raise PortfolioError("entity revision must be a positive integer")
    elif table == "claims":
        _validate_claim(row, batch)
    elif table == "gaps":
        resolution = row.get("resolution_claim_id")
        if resolution is not None:
            _text(resolution)
        if row["reason"] not in REASONS or row["status"] not in {"open", "resolved"}:
            raise PortfolioError("invalid gap reason or status")
        if (row["status"] == "resolved") != (resolution is not None):
            raise PortfolioError("only a resolved gap requires a resolution claim")
    elif row["relation"] != "represents-project":
        raise PortfolioError("unregistered ecosystem bridge")


def validate(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {"version", "batch", *TABLES}:
        raise PortfolioError("expected complete versioned manifest")
    if type(manifest["version"]) is not int or manifest["version"] != VERSION:
        raise PortfolioError("unsupported manifest version")
    batch = manifest["batch"]
    required = {"id", "source_id", "actor"}
    if not isinstance(batch, dict) or not required <= batch.keys() <= required | {"visibility"}:
        raise PortfolioError("invalid batch metadata")
    for field in required:
        _text(batch[field])
    _private(batch)
    for table in TABLES:
        if not isinstance(manifest[table], list):
            raise PortfolioError("record collection must be an array")
        seen = set()
        for row in manifest[table]:
            if not isinstance(row, dict) or not REQUIRED[table] <= row.keys() <= FIELDS[table]:
                raise PortfolioError("missing or unregistered record field")
            _validate_record(table, row, batch)
            if row["id"] in seen:
                raise PortfolioError("duplicate record ID")
            seen.add(row["id"])
    try:
        canonical(manifest)
    except (ValueError, TypeError, RecursionError):
        raise PortfolioError("manifest is not finite JSON") from None
    return manifest


# Run each statement inside the caller's transaction. executescript would commit it.
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS portfolio_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        portfolio_schema_version INTEGER NOT NULL, revision INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_sources (
        id TEXT PRIMARY KEY, uri TEXT NOT NULL, content_hash TEXT NOT NULL,
        retrieved_at TEXT NOT NULL, source_class TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility = 'private')
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_entities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, revision INTEGER NOT NULL,
        created_at TEXT NOT NULL, retired_at TEXT,
        visibility TEXT NOT NULL CHECK (visibility = 'private')
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_batches (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
        actor TEXT NOT NULL, imported_at TEXT NOT NULL, base_revision INTEGER NOT NULL,
        manifest_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK (status = 'committed'),
        visibility TEXT NOT NULL CHECK (visibility = 'private'),
        manifest_json TEXT NOT NULL, receipt_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_claims (
        id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES portfolio_entities(id),
        predicate TEXT NOT NULL, object_id TEXT REFERENCES portfolio_entities(id), value_json TEXT,
        state TEXT NOT NULL CHECK (state IN ('reported', 'confirmed', 'disputed', 'retracted')),
        source_id TEXT NOT NULL REFERENCES portfolio_sources(id), source_locator TEXT NOT NULL,
        observed_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
        supersedes TEXT REFERENCES portfolio_claims(id), qualifiers_json TEXT NOT NULL,
        origin_class TEXT NOT NULL, origin_ref TEXT NOT NULL, writer TEXT NOT NULL,
        batch_id TEXT NOT NULL REFERENCES portfolio_batches(id) DEFERRABLE INITIALLY DEFERRED,
        visibility TEXT NOT NULL CHECK (visibility = 'private'),
        CHECK ((object_id IS NULL) != (value_json IS NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_gaps (
        id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES portfolio_entities(id),
        field TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
        source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
        resolution_claim_id TEXT REFERENCES portfolio_claims(id),
        visibility TEXT NOT NULL CHECK (visibility = 'private')
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_links (
        id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES portfolio_entities(id),
        ecosystem_node_id TEXT NOT NULL, relation TEXT NOT NULL,
        source_id TEXT NOT NULL REFERENCES portfolio_sources(id),
        visibility TEXT NOT NULL CHECK (visibility = 'private')
    )""",
    "CREATE INDEX IF NOT EXISTS portfolio_claim_subject ON portfolio_claims(subject_id)",
    "CREATE INDEX IF NOT EXISTS portfolio_claim_object ON portfolio_claims(object_id)",
    "CREATE INDEX IF NOT EXISTS portfolio_claim_supersedes ON portfolio_claims(supersedes)",
)


def _schema(con):
    legacy = con.execute("""
        SELECT 1 FROM sqlite_master WHERE type = 'table' AND name IN ('nodes', 'edges', 'meta')
    """).fetchone()
    if legacy:
        raise PortfolioError("portfolio requires a separate database file")
    for statement in SCHEMA:
        con.execute(statement)
    con.execute("INSERT OR IGNORE INTO portfolio_state VALUES (1, ?, 0)", (VERSION,))
    _check_schema(con)
    con.execute("DROP TRIGGER IF EXISTS immutable_entities_UPDATE")
    for table in (*TABLES, "batches"):
        for action in ("UPDATE", "DELETE"):
            if table == "entities" and action == "UPDATE":
                continue
            con.execute(f"""
                CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                BEFORE {action} ON portfolio_{table}
                BEGIN SELECT RAISE(ABORT, 'portfolio records are immutable'); END
            """)


def _check_schema(con):
    try:
        state = con.execute("""
            SELECT portfolio_schema_version FROM portfolio_state WHERE singleton = 1
        """).fetchone()
    except sqlite3.DatabaseError:
        raise PortfolioError("portfolio not initialized") from None
    if state is None or state[0] != VERSION:
        raise PortfolioError("unsupported portfolio schema version")


def default_path():
    configured = os.environ.get("INTERGRAPH_PORTFOLIO_DB")
    return Path(configured) if configured is not None else Path.home() / ".local/share/intergraph/portfolio.sqlite"


def _read_connection(path):
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
    except sqlite3.DatabaseError:
        con.close()
        raise
    return con


def open_readonly(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PortfolioError("portfolio not initialized")
    con = _read_connection(path)
    try:
        _check_schema(con)
    except PortfolioError:
        con.close()
        raise
    return con


def revision(con):
    return con.execute("SELECT revision FROM portfolio_state WHERE singleton = 1").fetchone()[0]


@contextmanager
def _transaction(con, dry_run=False):
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("BEGIN IMMEDIATE")
    with con:
        _schema(con)
        yield con
        if dry_run:
            con.rollback()


@contextmanager
def _destination(path, dry_run, expected_revision):
    if expected_revision is None and not dry_run:
        raise PortfolioError("expected revision is required")
    if expected_revision is not None and type(expected_revision) is not int:
        raise PortfolioError("invalid expected revision")
    path = Path(path).expanduser().resolve()
    try:
        if dry_run:
            with closing(sqlite3.connect(":memory:")) as con:
                if path.exists():
                    with closing(open_readonly(path)) as source:
                        source.backup(con)
                with _transaction(con, dry_run=True):
                    yield con
        else:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.stat().st_mode & 0o077:
                    raise PortfolioError("portfolio file must be accessible only to its owner")
            else:
                os.close(fd)
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as con, _transaction(con):
                yield con
    except (sqlite3.DatabaseError, OverflowError):
        raise PortfolioError("portfolio import aborted; invalid data or database state") from None


def _record(table, raw, batch_id):
    row = {"visibility": "private", **DEFAULTS[table], **raw}
    for field in TIMESTAMPS:
        if row.get(field) is not None:
            row[field] = _time(row[field])
    if table == "claims":
        row["batch_id"] = batch_id
        row["qualifiers_json"] = canonical(row["qualifiers_json"])
        if row["value_json"] is not None:
            row["value_json"] = canonical(row["value_json"])
    return row


def _lookup(con, table, record_id):
    return con.execute(f"SELECT * FROM portfolio_{table} WHERE id = ?", (record_id,)).fetchone()


def _insert(con, table, values):
    # Identifiers come exclusively from the validated schema registry.
    names = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    con.execute(f"INSERT INTO portfolio_{table} ({names}) VALUES ({placeholders})", tuple(values.values()))


def _check_new_record(con, table, row):
    if table == "entities" and row["revision"] != 1:
        raise PortfolioError("new entity revision must be one")
    if table == "claims" and row["supersedes"] is not None:
        prior = _lookup(con, "claims", row["supersedes"])
        if prior is None or any(prior[field] != row[field] for field in ("subject_id", "predicate")):
            raise PortfolioError("supersession must cite a prior claim on the same subject and predicate")
        if row["state"] == "retracted" and any(
            prior[field] != row[field] for field in ("object_id", "value_json")
        ):
            raise PortfolioError("retraction must preserve the original assertion")
    if table == "gaps" and row["resolution_claim_id"] is not None:
        resolution = _lookup(con, "claims", row["resolution_claim_id"])
        if resolution is None or resolution["subject_id"] != row["subject_id"]:
            raise PortfolioError("gap resolution must cite a claim on the same subject")


def _check_endpoints(con):
    relationships = con.execute("""
        SELECT claim.predicate, subject.kind, object.kind FROM portfolio_claims AS claim
        JOIN portfolio_entities AS subject ON claim.subject_id = subject.id
        JOIN portfolio_entities AS object ON claim.object_id = object.id
    """)
    for predicate, subject_kind, object_kind in relationships:
        if (subject_kind, object_kind) not in RELATIONS[predicate]:
            raise PortfolioError("invalid relationship endpoints")
    bad_link = con.execute("""
        SELECT 1 FROM portfolio_links JOIN portfolio_entities AS entity
          ON portfolio_links.entity_id = entity.id
        WHERE entity.kind != 'project' LIMIT 1
    """).fetchone()
    if bad_link:
        raise PortfolioError("bridge must represent a project")


def _apply(con, manifest, expected_revision, imported_at=None, *, allow_replay=True):
    batch = manifest["batch"]
    content = canonical(manifest)
    digest = hashlib.sha256(content.encode()).hexdigest()
    duplicate = con.execute("SELECT receipt_json FROM portfolio_batches WHERE manifest_hash = ?",
                            (digest,)).fetchone()
    if duplicate is not None:
        if allow_replay:
            return loads(duplicate["receipt_json"])
        raise PortfolioError("duplicate archive batch hash")
    if _lookup(con, "batches", batch["id"]) is not None:
        raise PortfolioError("batch ID reused with different content")

    current = revision(con)
    if type(expected_revision) is not int or expected_revision != current:
        raise PortfolioError("stale or missing expected revision")
    inserted = dict.fromkeys(TABLES, 0)
    updated = {"entities": 0}

    for table in TABLES:
        for raw in manifest[table]:
            row = _record(table, raw, batch["id"])
            previous = _lookup(con, table, row["id"])
            if previous is None:
                _check_new_record(con, table, row)
                _insert(con, table, row)
                inserted[table] += 1
                continue
            if table == "claims" and "batch_id" not in raw:
                row["batch_id"] = previous["batch_id"]
            if row == dict(previous):
                continue
            if (table != "entities" or row["revision"] != previous["revision"] + 1
                    or row["created_at"] != previous["created_at"]):
                raise PortfolioError("immutable record change or stale entity revision")
            con.execute("""
                UPDATE portfolio_entities SET kind = :kind, label = :label,
                    revision = :revision, retired_at = :retired_at WHERE id = :id
            """, row)
            updated["entities"] += 1

    _check_endpoints(con)
    metadata = {
        "id": batch["id"], "source_id": batch["source_id"], "actor": batch["actor"],
        "imported_at": _time(imported_at or datetime.now(timezone.utc).isoformat()),
        "base_revision": current, "manifest_hash": digest, "status": "committed", "visibility": "private",
    }
    receipt = dict(metadata, revision=current + 1, inserted=inserted, updated=updated)
    _insert(con, "batches", dict(metadata, manifest_json=content, receipt_json=canonical(receipt)))
    if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise PortfolioError("unresolved record reference")
    con.execute("UPDATE portfolio_state SET revision = ? WHERE singleton = 1", (current + 1,))
    return receipt


def import_manifest(path, manifest, expected_revision=None, dry_run=False):
    validate(manifest)
    with _destination(path, dry_run, expected_revision) as con:
        expected = revision(con) if expected_revision is None else expected_revision
        receipt = _apply(con, manifest, expected)
        return dict(receipt, dry_run=True, gaps=_rows(con, "gaps")) if dry_run else receipt


def _batches(con):
    records = con.execute("SELECT manifest_json, receipt_json FROM portfolio_batches ORDER BY base_revision")
    return [{"manifest": loads(manifest), "receipt": loads(receipt)} for manifest, receipt in records]


def export_archive(con):
    con.execute("BEGIN")
    try:
        return {"format": ARCHIVE_FORMAT, "version": VERSION, "visibility": "private",
                "revision": revision(con), "batches": _batches(con)}
    finally:
        con.rollback()


def validate_archive(archive):
    if (not isinstance(archive, dict)
            or set(archive) != {"format", "version", "visibility", "revision", "batches"}
            or archive["format"] != ARCHIVE_FORMAT or type(archive["version"]) is not int
            or archive["version"] != VERSION or archive["visibility"] != "private"):
        raise PortfolioError("invalid archive envelope")
    entries = archive["batches"]
    if (not isinstance(entries, list) or type(archive["revision"]) is not int
            or archive["revision"] != len(entries)):
        raise PortfolioError("invalid archive revision")
    batch_ids = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"manifest", "receipt"}:
            raise PortfolioError("invalid archived batch")
        manifest = validate(entry["manifest"])
        batch_id = manifest["batch"]["id"]
        if batch_id in batch_ids:
            raise PortfolioError("duplicate archive batch ID")
        batch_ids.add(batch_id)
        receipt = entry["receipt"]
        if not isinstance(receipt, dict) or any(
            type(receipt.get(key)) is not int or receipt[key] != expected
            for key, expected in (("base_revision", index), ("revision", index + 1))
        ):
            raise PortfolioError("invalid archived receipt revision")
        _time(receipt.get("imported_at"))
    return archive


def _restore(con, archive):
    for index, entry in enumerate(archive["batches"]):
        recorded = entry["receipt"]
        recreated = _apply(con, entry["manifest"], index, recorded["imported_at"], allow_replay=False)
        if canonical(recreated) != canonical(recorded):
            raise PortfolioError("archive receipt does not reproduce")
    if revision(con) != archive["revision"]:
        raise PortfolioError("archive revision does not reproduce")


def import_archive(path, archive, expected_revision=None, dry_run=False):
    validate_archive(archive)
    # Validate recovery before opening a destination, including for a dry run.
    try:
        with closing(sqlite3.connect(":memory:")) as scratch, _transaction(scratch):
            _restore(scratch, archive)
    except (sqlite3.DatabaseError, OverflowError, ValueError, TypeError) as error:
        if isinstance(error, PortfolioError):
            raise
        raise PortfolioError("archive replay failed") from None

    with _destination(path, dry_run, expected_revision) as con:
        current = revision(con)
        replayed = current == archive["revision"] and _batches(con) == archive["batches"]
        if not replayed:
            if current != 0 or expected_revision not in (None, 0):
                raise PortfolioError("archive restore requires an empty store at the expected revision")
            _restore(con, archive)
        result = _output(con, replayed=replayed)
        if dry_run:
            result.update(dry_run=True, gaps=_rows(con, "gaps"))
        return result


def import_document(path, document, expected_revision=None, dry_run=False):
    importer = import_manifest
    if isinstance(document, dict) and document.get("format") == ARCHIVE_FORMAT:
        importer = import_archive
    return importer(path, document, expected_revision, dry_run)


def _rows(con, table):
    rows = [dict(row) for row in con.execute(f"SELECT * FROM portfolio_{table} ORDER BY id")]
    for row in rows:
        for key in ("value_json", "qualifiers_json"):
            if row.get(key) is not None:
                row[key] = loads(row[key])
    return rows


def _claims(con):
    claims = _rows(con, "claims")
    replaced = {claim["supersedes"] for claim in claims if claim["supersedes"] is not None}
    sources = {source["id"]: source for source in _rows(con, "sources")}
    for claim in claims:
        claim["effective"] = claim["id"] not in replaced and claim["state"] != "retracted"
        claim["source"] = sources[claim["source_id"]]
    return claims


def _output(con, **data):
    return {"revision": revision(con), "visibility": "private", **data}


def find(con, query="", kind=None, state=None):
    if (not isinstance(query, str)
            or (kind is not None and (not isinstance(kind, str) or kind not in KINDS))
            or (state is not None and (not isinstance(state, str) or state not in STATES))):
        raise PortfolioError("invalid find filter")
    needle = query.casefold()
    by_subject = defaultdict(list)
    for claim in _claims(con):
        by_subject[claim["subject_id"]].append(claim)
    matches = []
    for entity in _rows(con, "entities"):
        if kind is not None and kind != entity["kind"]:
            continue
        own = by_subject[entity["id"]]
        if state is not None and not any(claim["state"] == state for claim in own):
            continue
        aliases = [claim["value_json"] for claim in own
                   if claim["effective"] and claim["predicate"] == "alias"
                   and isinstance(claim["value_json"], str)]
        if query == entity["id"] or any(needle in name.casefold() for name in (entity["label"], *aliases)):
            matches.append(dict(entity, claims=own))
    return _output(con, entities=matches)


def neighbors(con, id, direction="both", predicates=None, states=None, include_history=False):
    _text(id)
    if not isinstance(direction, str) or direction not in ("in", "out", "both") or type(include_history) is not bool:
        raise PortfolioError("invalid neighbor options")
    for values, allowed in ((predicates, RELATIONS), (states, STATES)):
        if values is not None and (not isinstance(values, list)
                                   or any(not isinstance(value, str) or value not in allowed for value in values)):
            raise PortfolioError("invalid neighbor filter")
    selected_predicates = set(RELATIONS) - {"candidate-same-as"} if predicates is None else set(predicates)
    selected_states = STATES - {"retracted"} if states is None else set(states)
    claims = []
    for claim in _claims(con):
        if (claim["object_id"] is None or claim["predicate"] not in selected_predicates
                or claim["state"] not in selected_states or not (include_history or claim["effective"])):
            continue
        if ((direction != "in" and claim["subject_id"] == id)
                or (direction != "out" and claim["object_id"] == id)):
            claims.append(claim)
    return _output(con, claims=claims)


def gaps(con, subject_id=None, ecosystem_path=None):
    if subject_id is not None:
        _text(subject_id)
    rows = [row for row in _rows(con, "gaps") if subject_id is None or row["subject_id"] == subject_id]
    for row in rows:
        row["source"] = dict(_lookup(con, "sources", row["source_id"]))
    bridges = [row for row in _rows(con, "links") if subject_id is None or row["entity_id"] == subject_id]

    statuses = {}
    try:
        if bridges and ecosystem_path:
            path = Path(ecosystem_path).expanduser().resolve()
            if path.is_file():
                with closing(_read_connection(path)) as legacy, legacy:
                    legacy.execute("BEGIN")
                    for node_id in {row["ecosystem_node_id"] for row in bridges}:
                        hit = legacy.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
                        statuses[node_id] = "resolved" if hit else "dangling"
    except (sqlite3.DatabaseError, OSError):
        statuses.clear()
    for row in bridges:
        row["endpoint_status"] = statuses.get(row["ecosystem_node_id"], "unresolved")
    return _output(con, gaps=rows, bridges=bridges)
