# Private portfolio

The portfolio stores curated entities, sourced claims, gaps, and links to
ecosystem nodes. It has its own SQLite file and stdio entry point. Ecosystem
ingest and reset operate on the ecosystem database.

Set `INTERGRAPH_PORTFOLIO_DB` or pass `--portfolio-db` to import and export.
The default is `$HOME/.local/share/intergraph/portfolio.sqlite`. Create the
parent directory first and restrict its permissions. New database files use
mode 0600, and imports refuse existing files accessible to group or other users.
Use `umask 077` when redirecting private output. Keep inventories, sidecars,
manifests, databases, and archives outside version control.

## Import and export

```sh
python3 bin/intergraph portfolio-import "$PRIVATE_DIR/reviewed.json" \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" --dry-run
python3 bin/intergraph portfolio-import "$PRIVATE_DIR/reviewed.json" \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" --expected-revision 0
python3 bin/intergraph portfolio-export \
  --portfolio-db "$PRIVATE_DIR/portfolio.sqlite" > "$PRIVATE_DIR/archive.json"
```

Use the revision of the store you reviewed for the committed import. A fresh
store starts at revision 0. The dry run reports the projected receipt and gaps
using an in-memory copy. An import validates and applies its batch in one
transaction. A validation failure leaves the existing revision intact. A
failed initial import can leave an empty file.

Importing the same manifest hash returns its original receipt, even if the
expected revision differs from the current one. A different manifest needs a
new batch ID and a matching expected revision. Receipts record the original
hash, the revision before and after import, and the inserted and updated counts.

### Manifest records

A version 1 manifest contains a `batch` object with `id`, `source_id`, and
`actor`, plus the arrays `sources`, `entities`, `claims`, `gaps`, and `links`.
Provide an empty array for any collection you omit. The field and predicate
registries live in `intergraph/portfolio.py`. The test fixtures provide examples.

| Record | Requirements |
| --- | --- |
| Source | Include an ID, URI, source class, retrieval time, and lowercase SHA256 content hash. |
| Entity | Use a persisted canonical UUID, registered kind, label, and creation time. New entities start at revision 1. |
| Claim | Specify the subject, predicate, state, and provenance. Supply one of `object_id` and `value_json`. Relationship endpoints must have registered kinds. Qualifiers are a JSON object. |
| Gap | State the subject, missing field, reason, and status. A resolved gap must cite a claim on the same subject. |
| Link | Supply a project entity, a legacy ecosystem node ID, and the relation `represents-project`. |

Timestamps require ISO 8601 with explicit timezones. SQLite stores the UTC
projection, and batch manifests preserve the original timestamps. Visibility
is private. The importer rejects unknown fields, repeated record IDs within a
collection, duplicate JSON keys, and non-finite numbers.

Claim provenance includes the source locator, observation time, writer, origin
class, and origin reference. Keep carrier provenance separate from the original
evidence. Candidate identity links require a disputed state. Dependency claims
require manifest, code, or manually verified evidence.

A correction appends a claim and names the prior claim with a nonempty string
in `supersedes`. Both claims must have the same subject and predicate. In a
single batch, put the prior claim first. A retraction also uses `state: retracted`
and repeats the original assertion. Omitted records remain in the store.
Repeated immutable records must agree with the existing records. Entity
corrections retain the UUID and creation time, advance the entity revision by
one, and must preserve the validity of recorded relationships and bridges.

### Archive recovery

Use `portfolio-import` to restore an exported archive into a scratch store at
expected revision 0. The importer replays the original manifests in memory and
compares the resulting receipts and revision before writing the destination.
Archives must contain one distinct batch per revision, in order. The final
revision must match the envelope. An identical archive can be replayed safely.
To move an archive between hosts, transport it as private data.

## Read-only MCP

Launch the portfolio endpoint with `python3 -m intergraph.portfolio_server`.
Initialization and tool discovery do not open a database. Query calls open an
existing store in SQLite read-only mode. This stdio endpoint runs under the
local operator account.

- `graph_portfolio_find` searches UUIDs, labels, and effective aliases. It
  accepts optional `query`, `kind`, and `state` filters and returns claim history
  and evidence.
- `graph_portfolio_neighbors` takes an entity `id` and returns direct object
  claims. Use `direction`, `predicates`, `states`, and `include_history` to
  customize results. The defaults exclude superseded and retracted claims and
  disputed candidate identity links.
- `graph_portfolio_gaps` accepts an optional `subject_id` and returns recorded
  gaps and bridges. If `INTERGRAPH_ECOSYSTEM_DB` points to a readable ecosystem
  database, bridge targets are reported as resolved or dangling. Otherwise,
  bridge status is unresolved.

## Prepare a Markdown inventory

```sh
python3 bin/intergraph portfolio-convert "$PRIVATE_DIR/inventory.md" \
  --observed-at 2026-01-01T00:00:00Z \
  --output-manifest "$PRIVATE_DIR/proposed.json" \
  --output-sidecar "$PRIVATE_DIR/proposed-identities.json"
```

The converter writes two distinct new files at mode 0600 and prints a review
report. Review the proposal before importing it. Keep the proposed sidecar
after the import succeeds and pass it as `--sidecar` on the next conversion.

The converter recognizes detailed `- Name:` and `- Repo URL:` records and compact
Markdown bullets linking to GitHub repositories. It retains quotes and line
locators, deduplicates repository URLs, and reports omitted local observations,
missing numeric repository IDs, and conflicting or unresolved identities.
A repository URL is a provisional ID. Exact forge metadata marked `[G]` can
be confirmed as a quotation. Other carrier observations are reported claims.
The converter reports proposed implementation relationships for review.
Explicit identity conflicts produce disputed candidate links and gaps.

Detailed entities start as `unclassified`. To apply a reviewed kind change,
edit the entity in the accepted sidecar and convert again. When the text is
identical, the converter retains the source record, creates a new batch for the
reviewed metadata, and increments the existing entity's revision. Repeating
conversion with that accepted metadata reuses the cached manifest. For renames
or transfers, review the sidecar's `key_redirects` so the appropriate UUID
follows the carrier record.

## Tests

```sh
python3 -m unittest discover -s tests
```

Public tests use synthetic inventories. The optional inventory recovery test
runs only when `INTERGRAPH_PRIVATE_INVENTORY` points to a local inventory. Its
temporary databases stay outside the repository, and failures omit input data.
