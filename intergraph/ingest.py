"""Static + manifest ingestor for intergraph (layer 1: declared structure).

Scans interverse/*/.claude-plugin/plugin.json manifests and extracts:
  - plugin / skill / command / agent / mcp-server / hook-event nodes
  - containment edges (part-of, provides-skill/command/agent/mcp, fires-hook)
  - cross-plugin reference edges (skill/command/agent/hook body mentions)
  - declared depends-on edges (peerDependencies in plugin.json)

Pure stdlib. Rebuilds are idempotent: the store is reset per full ingest.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import store

INTERVERSE = Path(__file__).resolve().parent.parent.parent
TEXT_SUFFIXES = {".md", ".sh", ".py", ".js", ".mjs", ".json", ".toml", ".yaml", ".yml"}


def discover_plugins(root: Path) -> list[Path]:
    return sorted(
        child for child in root.iterdir()
        if child.is_dir() and (child / ".claude-plugin" / "plugin.json").exists()
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _skill_description(skill_dir: Path) -> str | None:
    """Pull description from SKILL.md YAML frontmatter, if present."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return None
    try:
        text = md.read_text(errors="replace")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    d = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
    return d.group(1).strip().strip('"') if d else None


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _entry_dir(root: Path, plugin_dir: Path, entry: str) -> Path | None:
    """Resolve a manifest entry like './skills/browse' to a directory."""
    p = (plugin_dir / entry).resolve()
    return p if p.is_dir() and root in p.parents else None


def ingest(root: Path, con) -> dict:
    root = root.resolve()  # normalize symlinks (/var -> /private/var on macOS)
    plugins = discover_plugins(root)
    names = [p.name for p in plugins]
    ref_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
    now = datetime.now(timezone.utc).isoformat()

    store.reset(con)
    counts = {"plugins": 0, "skills": 0, "commands": 0, "agents": 0,
              "mcp_servers": 0, "hook_events": 0, "references": 0, "depends_on": 0}

    # Monorepo root node
    store.upsert_node(con, root.name, "monorepo", root.name,
                      source=str(root))
    store.set_meta(con, "generated", now)

    for plugin_dir in plugins:
        name = plugin_dir.name
        manifest = _read_json(plugin_dir / ".claude-plugin" / "plugin.json")
        desc = manifest.get("description")
        store.upsert_node(con, name, "plugin", name, desc,
                          source=_rel(root, plugin_dir),
                          meta={"version": manifest.get("version")})
        store.upsert_edge(con, name, root.name, "part-of", last_seen=now)
        counts["plugins"] += 1

        # --- children: skills / commands / agents ---
        for key, kind, edge_type, counter in (
            ("skills", "skill", "provides-skill", "skills"),
            ("commands", "command", "provides-command", "commands"),
            ("agents", "agent", "provides-agent", "agents"),
        ):
            entries = manifest.get(key) or []
            if isinstance(entries, str):
                entries = [entries]
            for entry in entries:
                child = _entry_dir(root, plugin_dir, entry)
                if child is None:
                    # commands/agents may be single .md files
                    f = (plugin_dir / entry).resolve()
                    if not (f.is_file() and root in f.parents):
                        continue
                    child = f
                cid = f"{name}:{kind}:{child.stem}"
                cdesc = _skill_description(child) if child.is_dir() else None
                store.upsert_node(con, cid, kind, child.stem, cdesc,
                                  source=_rel(root, child))
                store.upsert_edge(con, name, cid, edge_type, last_seen=now)
                counts[counter] += 1

        # --- MCP servers ---
        mcp = manifest.get("mcpServers") or {}
        for server_name in mcp:
            sid = f"{name}:{server_name}"
            store.upsert_node(con, sid, "mcp-server", server_name,
                              source=_rel(root, plugin_dir))
            store.upsert_edge(con, name, sid, "provides-mcp", last_seen=now)
            counts["mcp_servers"] += 1

        # --- hook events ---
        hooks_file = plugin_dir / "hooks" / "hooks.json"
        hooks = _read_json(hooks_file) if hooks_file.exists() else {}
        for event in (hooks.get("hooks") or hooks or {}).keys():
            if not isinstance(event, str):
                continue
            eid = f"hook:{event}"
            store.upsert_node(con, eid, "hook-event", event)
            store.upsert_edge(con, name, eid, "fires-hook", last_seen=now)
            counts["hook_events"] += 1

        # --- declared dependencies ---
        for peer in (manifest.get("peerDependencies") or {}):
            if peer in names:
                store.upsert_edge(con, name, peer, "depends-on", last_seen=now)
                counts["depends_on"] += 1

    # --- cross-plugin references (text mentions in skill/command/agent/hook bodies) ---
    ref_counts: dict[tuple[str, str], int] = {}
    for plugin_dir in plugins:
        name = plugin_dir.name
        for sub in ("skills", "commands", "agents", "hooks"):
            base = plugin_dir / sub
            if not base.is_dir():
                continue
            for f in base.rglob("*"):
                if f.suffix not in TEXT_SUFFIXES or not f.is_file():
                    continue
                try:
                    text = f.read_text(errors="replace")
                except OSError:
                    continue
                for other in set(ref_re.findall(text)):
                    if other != name:
                        key = (name, other)
                        ref_counts[key] = ref_counts.get(key, 0) + 1
    for (src, tgt), n in sorted(ref_counts.items()):
        store.upsert_edge(con, src, tgt, "references", weight=float(n),
                          evidence=f"{n} file(s) mention", last_seen=now)
        counts["references"] += 1

    con.commit()
    return counts
