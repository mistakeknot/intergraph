"""Code-level ingestor (layer 2 signal, phase 3): cross-plugin 'calls' edges.

Two passes over each code-bearing plugin:

  1. tldrs import analysis (when the `tldrs` binary is available): Go source
     files' module imports that name another plugin are high-confidence edges
     (evidence marked 'import'). Capped per plugin; falls back silently.
  2. Regex mention scan over code files (*.py, *.go, *.js, *.mjs, *.ts, *.sh):
     any mention of another plugin's name in code — exec'd binaries, socket
     paths, endpoint URLs, module paths — becomes a 'calls' edge.

Edge weight = number of distinct code files referencing the target. impact()
and path() span 'calls' edges automatically (they are type-agnostic).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import store

CODE_SUFFIXES = {".py", ".go", ".js", ".mjs", ".ts", ".sh"}
SKIP_DIRS = {"node_modules", ".venv", "venv", "dist", "build",
             "__pycache__", ".git", ".turbo"}
TLDRS_SUFFIXES = {".go"}  # cross-plugin module imports are the Go pattern here
TLDRS_FILE_CAP = 100      # per plugin, bounds subprocess fan-out
TLDRS_TIMEOUT = 5


def code_files(plugin_dir: Path) -> list[Path]:
    out = []
    for p in plugin_dir.rglob("*"):
        if not p.is_file() or p.suffix not in CODE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return sorted(out)


def _tldrs_imports(path: Path, timeout: int = TLDRS_TIMEOUT) -> list[str]:
    """Module paths imported by `path`, via tldrs. [] on any failure."""
    try:
        r = subprocess.run(
            ["tldrs", "imports", str(path)],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return []
        return [m.get("module", "") for m in json.loads(r.stdout)]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []


def ingest_code(con, root: Path, use_tldrs: bool | None = None,
                tldrs_runner=_tldrs_imports) -> dict:
    """Write 'calls' edges between plugins. `use_tldrs=None` auto-detects."""
    root = root.resolve()
    if use_tldrs is None:
        use_tldrs = shutil.which("tldrs") is not None
    plugins = [r["id"] for r in con.execute("SELECT id FROM nodes WHERE kind='plugin'")]
    rx = re.compile(rb"\b(" + b"|".join(
        re.escape(n.encode()) for n in sorted(plugins, key=len, reverse=True)) + rb")\b")
    ts = datetime.now(timezone.utc).isoformat()

    # (source, target) -> {"files": set, "import": bool}
    hits: dict[tuple[str, str], dict] = {}
    files_scanned = 0
    tldrs_used = 0

    for name in plugins:
        plugin_dir = root / name
        if not plugin_dir.is_dir():
            continue
        files = code_files(plugin_dir)
        files_scanned += len(files)
        others = [p for p in plugins if p != name]

        for f in files:
            # pass 1: tldrs import analysis (Go), high confidence
            if use_tldrs and f.suffix in TLDRS_SUFFIXES and tldrs_used < TLDRS_FILE_CAP:
                tldrs_used += 1
                for module in tldrs_runner(f):
                    for other in others:
                        if other in module:
                            h = hits.setdefault((name, other), {"files": set(), "import": False})
                            h["files"].add(f)
                            h["import"] = True
            # pass 2: regex mention scan
            try:
                text = f.read_bytes()
            except OSError:
                continue
            for m in set(rx.findall(text)):
                other = m.decode()
                if other == name:
                    continue
                h = hits.setdefault((name, other), {"files": set(), "import": False})
                h["files"].add(f)

    edges = 0
    for (src, tgt), h in sorted(hits.items()):
        files = sorted(h["files"])
        shown = ", ".join(str(f.relative_to(root)) for f in files[:5])
        if len(files) > 5:
            shown += f", +{len(files) - 5} more"
        evidence = f"code: {shown}" + ("; tldrs-import" if h["import"] else "")
        store.upsert_edge(con, src, tgt, "calls", weight=float(len(files)),
                          evidence=evidence, last_seen=ts)
        edges += 1

    store.set_meta(con, "code_files_scanned", str(files_scanned))
    store.set_meta(con, "code_tldrs", "on" if use_tldrs else "off")
    con.commit()
    return {"files_scanned": files_scanned, "calls_edges": edges,
            "tldrs": use_tldrs, "tldrs_files": tldrs_used}
