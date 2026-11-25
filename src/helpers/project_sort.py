"""Sort order for the Projects tab.

Pure functions: no Tk, no config. `rebuild_tree` sorts the projects inside each
category group with `sort_projects`, and the header click only changes which
column and direction are asked for.

Columns are named by the Treeview column id ("#0" is the project name).
"""

from __future__ import annotations

from typing import Callable

#: Treeview column id -> sort key name. The ★ column has no key on purpose:
#: there is one active project, so sorting by it would just move one row.
SORTABLE = {
    "#0":       "name",
    "path":     "path",
    "synced":   "synced",
    "cg":       "codegraph",
    "git":      "git",
    "scaffold": "scaffold",
}

DEFAULT_KEY = "name"

#: Which direction a first click on a column should give. Dates and "has it"
#: columns are most useful newest / present first; text is A-Z.
FIRST_CLICK_DESCENDING = {"synced", "codegraph", "scaffold"}

# Lower rank sorts first in ascending order: things that need attention first.
_GIT_RANK = {"behind": 0, "dirty": 1, "ahead": 2, "mixed": 3, "clean": 4}


def _git_rank(p: dict) -> int:
    """Rank a project's cached git status. No repo / not yet checked sort last."""
    if not p.get("has_git"):
        return 9
    st = p.get("git_status")
    if not isinstance(st, dict):
        return 8                                   # still checking
    dirty = bool(st.get("dirty"))
    ahead, behind = st.get("ahead", 0), st.get("behind", 0)
    if behind:
        return _GIT_RANK["behind"]
    if dirty and ahead:
        return _GIT_RANK["mixed"]
    if dirty:
        return _GIT_RANK["dirty"]
    if ahead:
        return _GIT_RANK["ahead"]
    return _GIT_RANK["clean"]


def sort_projects(projects: list, key: str, descending: bool,
                  has_scaffold: Callable[[str], bool]) -> list:
    """Return `projects` ordered by `key`; ties fall back to name A-Z.

    `descending` reverses the primary key only, so equal rows stay in
    alphabetical order in both directions.
    """
    def name(p):  return p["name"].casefold()

    primary: dict[str, Callable[[dict], object]] = {
        "name":      name,
        "path":      lambda p: p["path"].casefold(),
        # A project with no tokensave index shows "—", so it has no sync age.
        "synced":    lambda p: p["mtime"] if p.get("has_tokensave", True) else 0,
        "codegraph": lambda p: 1 if p.get("has_codegraph") else 0,
        "git":       _git_rank,
        "scaffold":  lambda p: 1 if has_scaffold(p["path"]) else 0,
    }.get(key, name)

    by_name = sorted(projects, key=name)
    return sorted(by_name, key=primary, reverse=descending)


def next_direction(current_key: str, current_desc: bool, clicked: str) -> tuple:
    """(key, descending) after a header click: same column flips, a new one resets."""
    if clicked == current_key:
        return clicked, not current_desc
    return clicked, clicked in FIRST_CLICK_DESCENDING
