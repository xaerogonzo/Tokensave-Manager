"""Detect that the manager's own source changed since it started.

Python does not reload modules, so after editing `src/` the running manager
keeps executing the code it imported at startup. The failure mode is not a
crash — it is worse: a feature behaves like its old self, and the obvious
conclusion is that the edit did not work. That footgun has cost real time here
more than once (a Draft-PR change appeared not to take effect; `prompts.py`
edits do not show in the Reference tab, which reads PROMPT_SNIPPETS once at
construction).

This module answers one question: has anything under `src/` changed since the
snapshot taken at startup?

Two details that decide whether the banner is trusted or ignored:

* **The fingerprint is `(mtime_ns, size)`, not mtime alone.** Filesystem
  timestamp granularity and editors that preserve timestamps both defeat a
  bare mtime compare, and a missed change is a banner that never appears when
  it should.
* **Editor noise is excluded in ONE place** — `is_source_change_candidate`.
  A banner that cries wolf over a `.swp` file gets dismissed permanently,
  which costs more than never having shipped it.

Pure module — stdlib only, no Tkinter, safe from any thread.
"""

from __future__ import annotations

import os

# Directories never worth watching: build artifacts and caches change for
# reasons that have nothing to do with the code the manager is running.
_SKIP_DIRS = {
    "__pycache__", ".git", ".tokensave", ".tokensave-manager", ".codegraph",
    ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules", "dist",
    "build", "logs",
}

# Editor scratch files. Vim writes `.swp`/`.swx`, Emacs `.#name` and `name~`,
# many editors drop `.tmp`/`.orig`, and this project's own helpers leave
# `*.backup.<ms>` snapshots beside files they rewrite.
_SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".swx", ".tmp", ".temp",
                  ".orig", ".rej", ".bak", "~")
_SKIP_PREFIXES = (".#", "#", "~$")


def is_source_change_candidate(path: str) -> bool:
    """True when *path* is a source file whose change should raise the banner.

    Centralised so the rule lives in one place rather than as a handful of
    suffix checks scattered through the UI layer.
    """
    name = os.path.basename(path)
    if not name:
        return False
    if name.startswith(_SKIP_PREFIXES):
        return False
    if name.endswith(_SKIP_SUFFIXES):
        return False
    if ".backup." in name:
        return False
    return name.endswith(".py")


def snapshot_sources(root: str) -> "dict[str, tuple[int, int]]":
    """Map each watched file under *root* to its ``(mtime_ns, size)``.

    Unreadable files are skipped rather than raising: this runs on a timer in
    a GUI, and one locked file must not take the check down.
    """
    snap: dict[str, tuple[int, int]] = {}
    if not os.path.isdir(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if not is_source_change_candidate(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            snap[full] = (st.st_mtime_ns, st.st_size)
    return snap


def changed_files(baseline: "dict[str, tuple[int, int]]",
                  current: "dict[str, tuple[int, int]]") -> "list[str]":
    """Sorted paths that were added, removed, or modified since *baseline*.

    Additions and removals count: a new module or a deleted one changes what
    the running process would import just as much as an edit does.
    """
    changed = set()
    for path, fingerprint in current.items():
        if baseline.get(path) != fingerprint:
            changed.add(path)
    changed.update(set(baseline) - set(current))
    return sorted(changed)


def describe_changes(paths: "list[str]", root: str, limit: int = 3) -> str:
    """Short human summary naming a few files — never a bare count.

    "3 files changed" invites "which?"; naming them lets the reader decide
    instantly whether a restart matters for what they are doing.
    """
    if not paths:
        return ""
    rel = []
    for p in paths[:limit]:
        try:
            rel.append(os.path.relpath(p, root).replace("\\", "/"))
        except ValueError:
            rel.append(os.path.basename(p))
    listed = ", ".join(rel)
    extra = len(paths) - len(rel)
    if extra > 0:
        listed += f" and {extra} more"
    return listed
