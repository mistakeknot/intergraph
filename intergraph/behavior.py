"""Behavioral ingestor (layer 3 signal, phase 2): observed plugin usage.

Scans local agent session histories (read-only) for plugin-name mentions and
turns them into time-decayed evidence:

  - per-plugin observed weight  -> node meta['observed']
  - per-pair co-occurrence      -> edges type='co-occurs' (undirected, stored
                                   once with source < target)

Decay model: a session file contributes 0.5 ** (age_days / HALF_LIFE_DAYS).
Files older than MAX_AGE_DAYS (4 half-lives, <6% weight) are skipped. File
mtime is the session timestamp proxy; a plugin counts at most once per file,
so repeat mentions inside one session don't inflate the signal.

Pure stdlib; session directories are opened read-only and never written.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import store

HALF_LIFE_DAYS = 14.0
MAX_AGE_DAYS = 56  # 4 half-lives
CHUNK = 8 * 1024 * 1024

SOURCES = [
    ("claude", Path.home() / ".claude" / "projects"),
    ("kimi", Path.home() / ".kimi-code" / "sessions"),
    ("codex", Path.home() / ".codex" / "sessions"),
]


def decay(age_days: float, half_life: float = HALF_LIFE_DAYS) -> float:
    return math.pow(0.5, age_days / half_life)


def _names_rx(plugin_names: list[str]) -> re.Pattern[bytes]:
    alt = b"|".join(re.escape(n.encode()) for n in sorted(plugin_names, key=len, reverse=True))
    return re.compile(rb"\b(" + alt + rb")\b")


def scan_file(path: Path, rx: re.Pattern[bytes], total_names: int) -> set[str]:
    """Plugin names mentioned in a file; stops early once every name is seen."""
    found: set[str] = set()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                for m in rx.findall(chunk):
                    found.add(m.decode())
                if len(found) >= total_names:
                    break
    except OSError:
        pass
    return found


def scan_sessions(plugin_names: list[str], now: float | None = None,
                  sources: list[tuple[str, Path]] | None = None) -> dict:
    """Returns {observed: {plugin: weight}, pairs: {(a,b): weight},
    files: n, sources_used: [names]}. `now` is epoch seconds (injectable)."""
    now = now if now is not None else time.time()
    cutoff = now - MAX_AGE_DAYS * 86400
    rx = _names_rx(plugin_names)
    observed: dict[str, float] = {}
    pairs: dict[tuple[str, str], float] = {}
    files = 0
    sources_used = []

    for source_name, base in (sources if sources is not None else SOURCES):
        if not base.is_dir():
            continue
        sources_used.append(source_name)
        for path in base.rglob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            names = scan_file(path, rx, len(plugin_names))
            if not names:
                continue
            files += 1
            w = decay((now - mtime) / 86400.0)
            for n in names:
                observed[n] = observed.get(n, 0.0) + w
            for i, a in enumerate(sorted(names)):
                for b in sorted(names)[i + 1:]:
                    pairs[(a, b)] = pairs.get((a, b), 0.0) + w

    return {"observed": observed, "pairs": pairs, "files": files,
            "sources_used": sources_used}


def ingest_behavior(con, now: float | None = None,
                    sources: list[tuple[str, Path]] | None = None) -> dict:
    """Scan sessions and write observed weights + co-occurs edges."""
    plugin_names = [r["id"] for r in
                    con.execute("SELECT id FROM nodes WHERE kind='plugin'")]
    result = scan_sessions(plugin_names, now=now, sources=sources)
    ts = datetime.now(timezone.utc).isoformat()

    for plugin, w in result["observed"].items():
        store.update_node_meta(con, plugin, observed=round(w, 4), observed_at=ts)
    for (a, b), w in result["pairs"].items():
        store.upsert_edge(con, a, b, "co-occurs", weight=round(w, 4),
                          evidence="session co-occurrence", last_seen=ts)

    store.set_meta(con, "behavior_files", str(result["files"]))
    store.set_meta(con, "behavior_sources", ",".join(result["sources_used"]))
    store.set_meta(con, "behavior_generated", ts)
    con.commit()
    return {"files": result["files"], "sources_used": result["sources_used"],
            "plugins_observed": len(result["observed"]),
            "co_occurs_edges": len(result["pairs"])}
