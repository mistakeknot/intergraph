# Private portfolio store (slice 1)

This is an additive part of intergraph, **not** a replacement for the ecosystem
store. There are two SQLite files and two entry points:

- Existing `python3 -m intergraph.server`: unchanged ecosystem behavior.
- `python3 -m intergraph.portfolio_server`: read-only portfolio queries.

The portfolio defaults to `$HOME/.local/share/intergraph/portfolio.sqlite`,
overridable with `INTERGRAPH_PORTFOLIO_DB` or CLI `--portfolio-db`. Create a
private parent directory outside the plugin checkout/cache/reinstall tree first
(`umask 077`). New DB files are mode 0600; imports reject existing files with
any group/other permissions. Choose the authoritative host and backup procedure
separately. No deployment, refresh job, network lookup, or cutover is included.

Never commit an inventory, identity sidecar, manifest, report, archive, or DB.
Visibility is private throughout this slice. Visibility labels are not a
multi-tenant authorization mechanism: MCP is local stdio under the operator's
account. Public/filtered publication options are deliberately unavailable and
rejected. Exported stdout is private too; shell redirection permissions are the
operator's responsibility.

## Explicit local import

```sh
python3 bin/intergraph portfolio-import "$PRIVATE_DIR/reviewed.json" \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" --dry-run
python3 bin/intergraph portfolio-import "$PRIVATE_DIR/reviewed.json" \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" --expected-revision 0
python3 bin/intergraph portfolio-export \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" > "$PRIVATE_DIR/archive.json"
```

`--db` remains ecosystem-only and is rejected for portfolio commands. A portfolio
cannot be initialized into a legacy ecosystem file. Parent directories are never
created implicitly. No MCP mutation tools exist.

A manifest is a JSON object with **all** of these keys:

- `version`: integer `1`.
- `batch`: `{id, source_id, actor}`, optional `visibility` (private only).
- `sources`, `entities`, `claims`, `gaps`, `links`: arrays (empty is explicit).

Unknown fields, duplicate IDs/JSON keys, non-finite numbers, missing collections,
invalid references, invalid endpoints, and naive timestamps are errors. See
`FIELDS`, `REQUIRED`, `RELATIONS`, `SCALARS` in `intergraph/portfolio.py` for the
versioned registry and `tests/test_portfolio.py::fixture` for a synthetic example.
In particular:

- Entities use persisted canonical UUIDs, never labels or guessed numeric IDs.
  Repository URLs are provisional, sourced `candidate-external-id` scalars;
  numeric forge reconciliation is deferred.
- Source hashes are lowercase SHA256. Source retrieval, entity creation and
  claim observation times require ISO 8601 with explicit zones. The SQL
  projection is UTC; immutable manifests preserve the original zone spelling.
- Claims have exactly one of `object_id` and `value_json` (a native JSON value,
  not a JSON-encoded string). `qualifiers_json` is a native JSON object.
  Relationship endpoint pairs must be registered. Description values are
  quotations, not proof of product meaning.
- Claims require `source_id`, `source_locator`, `origin_class`, `origin_ref`,
  `observed_at`, and `writer`. Carrier provenance is separate from original
  evidence. `confirmed` only supports the precise assertion, not global truth.
- A candidate identity relationship remains disputed. Dependencies require
  `manifest`, `code`, or `manual-authoritative` evidence, never lexical grouping.
- Corrections append claims with `supersedes`; retractions also require
  `state: retracted` and the unchanged original assertion. No UPDATE of old
  claims, including their source-asserted validity bounds, is allowed.
- Separate sources may assert competing claims. Aliases are non-unique claims.
- Gaps explicitly retain unknown/not-applicable/not-visible/conflicting-identity/
  unverified-alias reasons. A resolved gap needs a resolution claim reference.
- Links use `entity_id` for a project and `ecosystem_node_id` for a legacy
  plugin, with relation `represents-project`. This is not identity equivalence.

Imports acquire `BEGIN IMMEDIATE`, validate all cross-references, insert immutable
records and a receipt, and advance the portfolio-owned revision once. Claims
must be ordered after claims they supersede in the same manifest. Existing source
and entity records can be repeated unchanged. Existing claim records may be
omitted or repeated unchanged (their original batch attribution is retained).
Source/claim/gap/link IDs are immutable. Entity label/kind/retirement corrections
require an explicit entity `revision` increment by one (new entities start at 1),
retain the original creation timestamp, and must preserve validity of stored typed
relationships. Original entity versions remain auditable in batch manifests and
are reproduced on archive replay. A reused record ID cannot silently change data.
Retain historical records;
missing input does **not** delete or retract anything. Resolving an immutable gap
can be recorded as a new resolved gap row with its resolution claim; queries
retain both historical and new rows rather than guessing that an old gap vanished.

A repeated manifest hash returns its **original** receipt before checking the
expected revision; it does not advance revision. A batch ID reused with different
content is an error. Receipts distinguish inserted records from entity updates. Dry runs operate on an in-memory snapshot (or a fresh
in-memory DB), report inserted counts and gaps, then roll back; they never create
a file or mutate the existing DB. A new failed import can leave an empty,
uninitialized file, but never committed schema, partial records, or a receipt.

### Lossless recovery

`portfolio-export` produces a versioned private archive of the original manifests
and receipts in revision order. This retains provenance, competing claims,
retractions, original timestamps, stable IDs, gaps, and source hashes. Import the
archive with the same `portfolio-import` command into an empty scratch DB at
revision 0. It is replay-validated in memory, then restored atomically; hashes,
counts, receipt timestamps and revisions must reproduce exactly. An identical
archive reapply is a no-op. An archive is not a merge instruction for a different
nonempty DB. Markdown is not a lossless recovery format.

## Read-only queries

Initialize and tools/list never open or create a DB. Query calls open an existing
DB using SQLite `mode=ro` plus `query_only`; they never migrate, ingest, or scan
sessions. Absent/uninitialized stores return `portfolio not initialized`.

- `graph_portfolio_find`: optional `query`, `kind`, `state`; searches labels,
  UUIDs and effective alias claims. Includes full sourced claims and history.
- `graph_portfolio_neighbors`: required `id`; optional `direction`, `predicates`,
  `states`, `include_history`. Direct object claims only, with full evidence,
  state, qualifiers and private visibility. Defaults exclude retracted/superseded
  and candidate-same-as claims. Explicit filters can inspect them. This is not
  transitive reachability or dependency impact.
- `graph_portfolio_gaps`: optional `subject_id`; returns stored gaps and bridge
  endpoint status. Without an available ecosystem file, status is `unresolved`.
  Optional `INTERGRAPH_ECOSYSTEM_DB` is opened read-only and never created;
  endpoint absence is `dangling`, existence is `resolved`. Reset/reingest does
  not delete or relabel durable links. No cross-layer traversal is implemented.

All derived responses include a revision and private visibility. Legacy tools,
IDs, exports, ingest/reset, behavior and code passes are unchanged.

## Conservative bootstrap converter

```sh
python3 bin/intergraph portfolio-convert "$PRIVATE_DIR/inventory.md" \
  --observed-at 2026-01-01T00:00:00Z \
  --output-manifest "$PRIVATE_DIR/proposed.json" \
  --output-sidecar "$PRIVATE_DIR/proposed-identities.json"
```

On a later snapshot pass the previously **accepted** sidecar with `--sidecar`.
Output paths must be distinct new files, created mode 0600. Review the manifest,
sidecar and private stdout report before importing; retain the proposed sidecar
only after accepting the import. No automatic inventory edits occur.

Detailed `- Name:` / `- Repo URL:` records and compact Markdown GitHub repository
bullets are supported. URLs are normalized/deduplicated while each carrier field
and line locator is retained. Counts distinguish parsed records, URL strings,
unknown numeric repository IDs, unresolved records, and excluded local-observation
records. Reported descriptions are quoted claims, not invented semantic edges. Detailed
identities default to `unclassified`; the reviewed sidecar can assign their
coarse kind rather than guessing it from headings, labels, or prose.
G markers confirm only exact quoted repository metadata; C remains reported;
F without first-hand corroboration remains reported. Unresolved original evidence
gets an explicit unresolved origin reference.

The sidecar assigns stable UUIDs by carrier key and role, preserves first entity
labels as identity history, stores snapshots for repeatability and uses immutable
claim supersession for newer snapshots in the same lineage. Renames/transfers
require reviewed `key_redirects`, not automatic label matching. Repository IDs
are shared across detailed and compact URL occurrences; source locators are not
lost. Local checkout records and explicitly marked local-observation fields are
excluded and reported. Explicit carrier uncertainty phrases (disputed candidate,
name match only, conflicting identity) create a disputed candidate claim and
identity gap, never an implements edge. Explicit unresolved-alias phrases create
an alias gap, never confirmed aliases. No dependency, ownership, game identity,
book-title expansion or alias equivalence is inferred. Explicit repository fields
produce **proposed** implemented-by relationships in the report, not silently
accepted edges. The operator must add reviewed semantic relationships/gaps to the
manifest as needed; arbitrary prose and diagrams are not an ontology parser.
Missing records never automatically propose or apply retractions in this slice.

## Tests

```sh
python3 -m unittest discover -s tests
```

All public fixtures are synthetic. An additional acceptance test reads only an
explicit `INTERGRAPH_PRIVATE_INVENTORY` path and otherwise skips. Run it locally,
never in public CI; it imports and losslessly restores temporary private stores
outside the repo without printing inventory content. No credentials are needed.
