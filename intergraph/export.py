"""interchart-compatible JSON export.

Emits the same shape interchart's scan.js produces ({generated, stats, nodes,
edges, overlaps}) so interchart can render intergraph data directly. Overlaps
are domain-keyword pairs computed from plugin descriptions — the heuristic
lives here until phase 2 replaces it with behavioral evidence.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import store

DOMAIN_RULES = [
    ("analytics-observability", re.compile(r"\banalytics?\b|\btelemetry\b|\bmetrics?\b|\bbenchmark|\bmonitoring\b|\bstatusline\b|\bhealth\b", re.I)),
    ("quality-review", re.compile(r"\bquality\b|\breview\b|\baudit\b|\bverification\b|\bguards?\b|\btests?(?:ing)?\b", re.I)),
    ("docs-artifacts", re.compile(r"\bdocs?(?:umentation)?\b|\broadmap\b|\bprd\b|\bartifact\b|\bchangelog\b|\bfreshness\b|\bdrift\b|\bmemory\b", re.I)),
    ("discovery-research", re.compile(r"\bsearch\b|\bdiscovery\b|\bresearch\b|\bembedding\b|\bretrieval\b|\bsemantic\b", re.I)),
    ("phase-gates", re.compile(r"\bphases?\b|\bgates?(?:d|ing)?\b|\bsprint\b|\blifecycle\b", re.I)),
    ("coordination-dispatch", re.compile(r"\bcoordination\b|\bdispatch(?:es|ed)?\b|\breserv(?:e|ation|ing)\b|\bconflict\b|\bmessaging\b", re.I)),
    ("design-product", re.compile(r"\bdesign\b|\bvisual\b|\bui\b|\bproduct\b", re.I)),
    ("release-publish", re.compile(r"\brelease\b|\bpublish(?:ing)?\b|\bversion\b|\bmarketplace\b", re.I)),
]


def plugin_domains(description: str | None) -> set[str]:
    if not description:
        return set()
    return {name for name, rx in DOMAIN_RULES if rx.search(description)}


def compute_overlaps(con) -> list[dict]:
    plugins = con.execute(
        "SELECT id, label, description FROM nodes WHERE kind='plugin'").fetchall()
    doms = {p["id"]: plugin_domains(p["description"]) for p in plugins}
    labels = {p["id"]: p["label"] for p in plugins}
    out = []
    ids = sorted(doms)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            shared = doms[a] & doms[b]
            if len(shared) >= 2:
                out.append({
                    "source": a, "sourceLabel": labels[a],
                    "target": b, "targetLabel": labels[b],
                    "score": round(1.0 + 0.6 * len(shared), 2),
                    "domains": sorted(shared),
                })
    out.sort(key=lambda o: -o["score"])
    return out


def export(con) -> dict:
    s = store.stats(con)
    rows = con.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    # plugin domains first, so children can inherit their parent's
    pdoms = {r["id"]: plugin_domains(r["description"])
             for r in rows if r["kind"] == "plugin"}

    def node_domain(r) -> tuple[str, list[str]]:
        if r["kind"] == "plugin":
            doms = sorted(pdoms[r["id"]])
            return (doms[0] if doms else "other"), doms
        if r["kind"] == "hook-event":
            return "hooks", []
        if r["kind"] == "monorepo":
            return "core", []
        parent = r["id"].split(":", 1)[0]
        doms = sorted(pdoms.get(parent, set()))
        return (doms[0] if doms else "other"), doms

    nodes = []
    for r in rows:
        domain, doms = node_domain(r)
        nodes.append({
            "id": r["id"], "type": r["kind"], "label": r["label"],
            "description": r["description"], "source": r["source"],
            "domain": domain,
            "meta": {
                "version": (json.loads(r["meta"]) or {}).get("version"),
                "path": r["source"],
                "plugin": r["id"].split(":", 1)[0],
                "domains": doms,
            },
        })
    edges = [
        {"source": r["source"], "target": r["target"], "type": r["type"],
         "weight": r["weight"], "meta": {}}
        for r in con.execute("SELECT * FROM edges ORDER BY source, target, type")
    ]
    overlaps = compute_overlaps(con)
    return {
        "generated": s["generated"] or datetime.now(timezone.utc).isoformat(),
        "stats": {"nodes": s["nodes"], "edges": s["edges"],
                  "overlaps": len(overlaps), "byType": s["by_kind"]},
        "nodes": nodes,
        "edges": edges,
        "overlaps": overlaps,
    }
