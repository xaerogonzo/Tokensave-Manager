"""Remember which projects the Manager has listed, and say which ones vanished.

`find_projects` only reports what it can walk to *now*. A repo moved to another
drive (or outside every search root) simply stops being returned, and the list
shrinks with no explanation. This module keeps a small memory of every project
ever listed (`cfg.raw["known_projects"]`) so the Projects tab can show the ones
that are no longer found, and why.

Nothing here ever forgets on its own: a drive that is merely unplugged must not
erase the memory of its projects. Only `forget` removes an entry, and only on a
person's say-so ("unknown is never false").
"""

from __future__ import annotations

import os

from helpers.mcp_projects import normalize_project_key

KEY = "known_projects"

#: Reasons, ordered by how certain we are. Displayed by the Projects tab.
GONE = "folder no longer exists - moved or deleted"
OFFLINE = "its search root is unavailable (drive disconnected?)"
UNLISTED = "folder exists but is not found by any search root"


def _store(raw: dict) -> dict:
    s = raw.get(KEY)
    if not isinstance(s, dict):
        s = {}
        raw[KEY] = s
    return s


def remember(raw: dict, projects: list) -> bool:
    """Record every listed project. Returns True when the memory changed."""
    store = _store(raw)
    changed = False
    for p in projects:
        entry = {"path": p["path"], "name": p["name"],
                 "root": p.get("root", ""),
                 "root_label": p.get("root_label", "")}
        key = normalize_project_key(p["path"])
        if store.get(key) != entry:
            store[key] = entry
            changed = True
    return changed


def find_missing(raw: dict, projects: list, isdir=os.path.isdir) -> list:
    """Remembered projects that the current scan did not return.

    Each row is `{"path", "name", "reason"}`, sorted by name. `isdir` is a seam
    for tests; it is only ever asked about paths, never written to.
    """
    store = raw.get(KEY)
    if not isinstance(store, dict):
        return []
    found = {normalize_project_key(p["path"]) for p in projects}
    rows = []
    for key, e in store.items():
        if key in found or not isinstance(e, dict) or not e.get("path"):
            continue
        if isdir(e["path"]):
            reason = UNLISTED
        elif e.get("root") and not isdir(e["root"]):
            reason = OFFLINE
        else:
            reason = GONE
        rows.append({"path": e["path"], "name": e.get("name", ""),
                     "reason": reason})
    return sorted(rows, key=lambda r: r["name"].casefold())


def forget(raw: dict, path: str) -> bool:
    """Drop one remembered project. True if it was there."""
    store = raw.get(KEY)
    if not isinstance(store, dict):
        return False
    return store.pop(normalize_project_key(path), None) is not None
