"""Turn a Markdown inventory into a private import proposal and identity sidecar."""

import hashlib
import re
from uuid import UUID, uuid4, uuid5

from . import portfolio

REPO_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)", re.ASCII)
BULLET = re.compile(r"^- \[([^]]+)\]\((https://github\.com/[^)]+)\)")
FIELD = re.compile(r"^- ([^:]+):\s*(.*)$")
LOCAL_FIELDS = {"Local path", "Configured origin", "Directory mtime"}
FORGE_FIELDS = {
    "GitHub description", "Visibility", "Last push", "Language",
    "Last metadata update", "Repository flags",
}
ACTOR = "local-bootstrap-converter"


def _url(text):
    match = REPO_URL.search(text)
    if match:
        return f"https://github.com/{match[1]}/{match[2].removesuffix('.git')}"
    return None


def parse(text):
    records, exclusions, rejected = [], [], []
    headings = []
    detailed = None

    def finish():
        if detailed is not None:
            if any(field in LOCAL_FIELDS for _, field, _ in detailed["fields"]):
                exclusions.append({"line": detailed["line"], "reason": "local-observation record"})
            else:
                records.append(detailed)

    for line_number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            finish()
            detailed = None
            depth = len(line) - len(line.lstrip("#"))
            headings[depth - 1:] = [line[depth:].strip()]
            continue
        heading = "/".join(headings)
        bullet = BULLET.match(line)
        if bullet:
            finish()
            detailed = None
            url = _url(bullet[2])
            records.append({
                "key": f"compact:{heading}:{(url or bullet[2]).casefold()}",
                "name": bullet[1], "line": line_number, "url": url, "compact": True,
                "fields": [(line_number, "Repository entry", line)],
            })
            continue
        if line.startswith("- [Z]"):
            rejected.append({"line": line_number, "reason": "local-observation field"})
            continue
        field = FIELD.match(line)
        if field and field[1] == "Name":
            finish()
            detailed = {"key": f"detailed:{heading}", "name": field[2], "line": line_number,
                        "compact": False, "url": None, "fields": []}
        if detailed is None or not line.strip():
            continue
        if field:
            detailed["fields"].append((line_number, field[1], field[2]))
            if field[1] == "Repo URL":
                detailed["url"] = _url(field[2])
        else:
            detailed["fields"].append((line_number, "Continuation", line))
    finish()
    if len({record["key"] for record in records}) != len(records):
        raise portfolio.PortfolioError("ambiguous duplicate carrier keys; review inventory headings")
    return records, exclusions, rejected


def _sidecar(prior):
    collections = ("entities", "claims", "key_redirects", "snapshots")
    if prior is None:
        return {"version": 1, "namespace": str(uuid4()), **{key: {} for key in collections}}
    if (not isinstance(prior, dict) or prior.keys() != {"version", "namespace", *collections}
            or type(prior["version"]) is not int or prior["version"] != 1
            or any(not isinstance(prior[key], dict) for key in collections)):
        raise portfolio.PortfolioError("unsupported identity sidecar")
    try:
        UUID(prior["namespace"])
        return portfolio.loads(portfolio.canonical(prior))
    except (ValueError, TypeError, AttributeError):
        raise portfolio.PortfolioError("invalid identity sidecar") from None


def _history(sidecar, source_id):
    """Recover accepted projections from the manifests retained in the sidecar."""
    latest_entities = {}
    source = None
    for saved in sidecar["snapshots"].values():
        manifest = saved["manifest"]
        for entity in manifest["entities"]:
            previous = latest_entities.get(entity["id"])
            if previous is None or entity.get("revision", 1) > previous.get("revision", 1):
                latest_entities[entity["id"]] = entity
        for record in manifest["sources"]:
            if record["id"] == source_id:
                source = record
    return latest_entities, source


def _reviewed_entity(record, latest):
    previous = latest.get(record["id"])
    if previous is None:
        return record
    if record["created_at"] != previous["created_at"]:
        raise portfolio.PortfolioError("reviewed identity must retain its creation timestamp")
    fields = ("kind", "label", "retired_at")
    amended = any(record.get(field) != previous.get(field) for field in fields)
    current = previous.get("revision", 1) + amended
    # A submitted sidecar is the last accepted one plus pending review edits.
    result = dict(record)
    if current > 1 or "revision" in previous:
        result["revision"] = current
    else:
        result.pop("revision", None)
    return result


def _bind(records, sidecar, latest, observed_at):
    identities = {}
    redirects = sidecar["key_redirects"]

    def entity(key, kind, label):
        key = redirects.get(key, key)
        prior = sidecar["entities"].get(key)
        if prior is None:
            prior = {"id": str(uuid4()), "kind": kind, "label": label, "created_at": observed_at}
        row = _reviewed_entity(prior, latest)
        if row["id"] in identities and identities[row["id"]] != row:
            raise portfolio.PortfolioError("conflicting records for a reviewed identity")
        sidecar["entities"][key] = row
        identities[row["id"]] = dict(row)
        return row["id"]

    bindings = []
    for record in records:
        key = redirects.get(record["key"], record["key"])
        url = record["url"]
        repo = entity(f"repository:{url.casefold()}", "repository", url.rsplit("/", 1)[1]) if url else None
        if record["compact"] and repo is None:
            raise portfolio.PortfolioError("compact inventory entry requires a repository URL")
        subject = repo if record["compact"] else entity(f"{key}:identity", "unclassified", record["name"])
        bindings.append((key, subject, repo))
    return identities, bindings


def _claim(claim_id, source_id, observed_at, subject, predicate, line, field, raw, verified, object_id):
    markers = set(re.findall(r"\[([GCF])\]", raw))
    exact_metadata = verified and markers == {"G"}
    origin = re.search(r"https?://[^\s)>`]+", raw)
    row = {
        "id": claim_id, "subject_id": subject, "predicate": predicate, "source_id": source_id,
        "source_locator": f"line:{line}", "observed_at": observed_at, "writer": ACTOR,
        "state": "confirmed" if exact_metadata else "reported",
        "origin_class": "forge-metadata" if exact_metadata else "reported",
        "origin_ref": origin[0] if origin else "unresolved:carrier-origin",
        "qualifiers_json": {"field": field, "raw": raw, "carrier_markers": sorted(markers)},
    }
    if object_id is None:
        row["value_json"] = raw
    else:
        row.update(object_id=object_id, state="disputed")
    return row


def convert(text, observed_at, sidecar=None):
    """Return a manifest, proposed sidecar and report. Retain the sidecar on import."""
    portfolio._time(observed_at)
    sidecar = _sidecar(sidecar)
    records, excluded, rejected = parse(text)
    if not records:
        raise portfolio.PortfolioError("no recognized inventory records")

    digest = hashlib.sha256(text.encode()).hexdigest()
    namespace = UUID(sidecar["namespace"])
    source_id = f"carrier:{digest}"
    latest, source = _history(sidecar, source_id)
    identities, bindings = _bind(records, sidecar, latest, observed_at)
    identity_hash = hashlib.sha256(portfolio.canonical([identities, bindings]).encode()).hexdigest()
    snapshot = f"{digest}:{identity_hash}"
    saved = sidecar["snapshots"].get(snapshot)
    if saved is None and not sidecar["key_redirects"]:
        # A text-only snapshot can be replayed when its entity projection agrees.
        legacy = sidecar["snapshots"].get(digest)
        if legacy and {row["id"]: row for row in legacy["manifest"]["entities"]} == identities:
            saved = legacy
    if saved is not None:
        return saved["manifest"], sidecar, saved["report"]

    if source is None:
        source = {"id": source_id, "uri": f"urn:intergraph:carrier:{digest}", "content_hash": digest,
                  "retrieved_at": observed_at, "source_class": "mixed-carrier"}
    manifest = {
        "version": 1, "batch": {"id": f"snapshot:{snapshot}", "source_id": source_id, "actor": ACTOR},
        "sources": [source], "entities": list(identities.values()), "claims": [], "gaps": [], "links": [],
    }
    proposals = []
    repository_ids = set()
    unresolved = 0
    lineage = sidecar["claims"]

    for record, (key, subject, repo) in zip(records, bindings):
        used_fields = set()

        def add_claim(target, predicate, line, field, raw, verified=False, object_id=None):
            logical = f"{key}:{target}:{field}"
            if field == "Continuation" or logical in used_fields:
                logical += f":{line}"
            used_fields.add(logical)
            claim_id = str(uuid5(namespace, f"{snapshot}:{logical}"))
            row = _claim(claim_id, source_id, observed_at, target, predicate, line, field, raw, verified, object_id)
            previous = lineage.get(logical)
            supersedes = previous["id"] if previous else None
            if supersedes is not None:
                row["supersedes"] = supersedes
            lineage[logical] = {"id": claim_id, "snapshot": snapshot, "supersedes": supersedes}
            manifest["claims"].append(row)

        def add_gap(field, reason):
            manifest["gaps"].append({
                "id": str(uuid5(namespace, f"{snapshot}:{key}:{field}-gap")),
                "subject_id": subject, "field": field, "reason": reason,
                "status": "open", "source_id": source_id,
            })

        conflict = None
        alias_gap = False
        for line, field, raw in record["fields"]:
            forge = repo is not None and field in FORGE_FIELDS
            predicate = "description" if field == "GitHub description" else "inventory-field"
            add_claim(repo if forge else subject, predicate, line, field, raw, verified=forge)
            if field == "Notes / gaps":
                if conflict is None and re.search(r"disputed candidate|name match only|conflicting[- ]identity", raw, re.I):
                    conflict = (line, raw)
                alias_gap |= bool(re.search(r"unverified alias|unresolved alias", raw, re.I))

        if repo is None:
            unresolved += 1
            add_gap("repository", "unknown")
        else:
            url = record["url"]
            repository_ids.add(url.casefold())
            add_claim(repo, "candidate-external-id", record["line"], "candidate repository URL", url)
            if not record["compact"]:
                if conflict:
                    line, raw = conflict
                    add_claim(subject, "candidate-same-as", line, "identity-candidate", raw, object_id=repo)
                    add_gap("identity", "conflicting-identity")
                else:
                    proposals.append({"subject_id": subject, "object_id": repo, "line": record["line"],
                                      "predicate": "implemented-by",
                                      "status": "requires identity and endpoint-kind review"})
        if alias_gap:
            add_gap("alias", "unverified-alias")

    portfolio.validate(manifest)
    report = {
        "visibility": "private", "source_hash": digest, "parsed_records": len(records),
        "distinct_urls": len(repository_ids), "independently_reconciled_repository_ids": 0,
        "unresolved_numeric_ids": len(repository_ids), "unresolved_records": unresolved,
        "excluded_records": excluded, "rejected_fields": rejected,
        "conflicts": sum(gap["reason"] == "conflicting-identity" for gap in manifest["gaps"]),
        "proposed_relationships": proposals, "retraction_candidates": [],
        "warnings": ["Review identity and relationship proposals before accepting this snapshot.",
                     "Omitted records remain in the store."],
    }
    sidecar["snapshots"][snapshot] = {"manifest": manifest, "report": report}
    return manifest, sidecar, report
