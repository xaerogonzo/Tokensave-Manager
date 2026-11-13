"""The History list: which rows to show, under which project, and how to put them in a tree.

Measured: 212 session rows in one flat table, mixed between two agents and a dozen projects, with
the same repository spelled as a dozen worktree names. This module owns the three decisions that
make it readable, and none of them is Tk:

  * `project_of(row)` says which PROJECT a row belongs to: a stable identity (`key`) and a label.
  * `build_plan(...)` filters by agent and project and lays the rows out, flat or under group rows.
  * `apply_plan(tree, ...)` puts a plan in a Treeview without throwing away the person's place.

IDENTITY IS NOT THE NAME. Two different repositories can both be called `app`; a group id derived
from the label would merge them. The key is the canonical repository root, and the label is only
what is shown (disambiguated with the parent folder when two keys share a name).

A WORKTREE IS GROUPED UNDER ITS REPOSITORY ONLY BY EVIDENCE. A live worktree's `.git` file says
`gitdir: <repo>/.git/worktrees/<name>`, so the repository is read from there. A Claude worktree
whose checkout was removed counts only while its repository is still a git root (the rule
`claude_tasks.resolve_session_folder` already applies). A Cursor chat uses what the resolver
proved. A directory NAME is never evidence of a parent: when a relationship cannot be established
the row stays in a group of its own.

Group ids are `grp:<hash of key>`; row ids are the caller's own (`sess:<agent>:<id>`) and never
change, so selection survives a regroup.

No Tk imports: `apply_plan` drives anything shaped like a ttk.Treeview.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os

from helpers import claude_tasks

AGENT_ALL, AGENT_CLAUDE, AGENT_CURSOR = "All agents", "Claude", "Cursor"
AGENTS = (AGENT_ALL, AGENT_CLAUDE, AGENT_CURSOR)
PROJECT_ALL = "All projects"

#: Newest sessions read per agent. Grouping is only honest if the list is not cut short by a cap
#: that a quiet project falls behind, so this is well above the old 50 (measured: 50 sessions scan
#: in 0.25 s; unchanged transcripts are cached, so a refresh does not pay for the rest again).
SCAN_LIMIT = 200

UNRESOLVED_KEY = "unresolved:"
UNRESOLVED_LABEL = "(folder not identified)"

_GIT_WALK_LEVELS = 12


@dataclasses.dataclass(frozen=True)
class Entry:
    """One Treeview row: a session, or a group header."""
    iid: str
    parent: str            # "" at the top level
    kind: str              # "session" | "group"
    values: tuple
    text: str = ""         # a group's label (the tree column)


@dataclasses.dataclass(frozen=True)
class Plan:
    entries: tuple
    projects: tuple        # labels offered by the Project filter, for the agent filter applied
    shown: int
    total: int             # rows before any filter
    capped: tuple          # agents whose scan hit SCAN_LIMIT


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def worktree_repository(folder: str) -> str:
    """The repository root of a git worktree at `folder`, read from its `.git` FILE, or "".

    A clone has a `.git` directory (not a worktree); a worktree has a file saying
    `gitdir: <repo>/.git/worktrees/<name>`. Anything else (a submodule's `.git/modules/...`, an
    unreadable file, a path that is not shaped like that) is "" and the folder stays its own
    project: a relationship that cannot be established is not guessed.
    """
    dotgit = os.path.join(folder, ".git")
    if not os.path.isfile(dotgit):
        return ""
    try:
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return ""
    if not line.startswith("gitdir:"):
        return ""
    gitdir = line[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(folder, gitdir)
    gitdir = os.path.normpath(gitdir)
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) == "worktrees" and os.path.basename(os.path.dirname(parent)) == ".git":
        return os.path.dirname(os.path.dirname(parent))
    return ""


def _checkout_root(folder: str) -> str:
    """The top of the checkout containing `folder`: the nearest ancestor with a `.git`
    (resolved to the repository for a worktree), else `folder` itself."""
    cur = os.path.normpath(folder)
    for _ in range(_GIT_WALK_LEVELS):
        dotgit = os.path.join(cur, ".git")
        if os.path.isdir(dotgit):
            return cur
        if os.path.isfile(dotgit):
            return worktree_repository(cur) or cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.normpath(folder)


def project_of(row: dict) -> "tuple[str, str]":
    """`(key, label)` for a session row. The key is the identity, the label only what is shown."""
    proven = row.get("project_path") or ""
    if proven:                                         # a Cursor chat the resolver tied to a project
        return _norm(proven), os.path.basename(os.path.normpath(proven)) or proven
    cwd = row.get("cwd") or ""
    if not cwd:
        return UNRESOLVED_KEY, UNRESOLVED_LABEL
    found = claude_tasks.resolve_session_folder(cwd)
    if found.state == claude_tasks.FOLDER_REMOVED:     # a removed Claude worktree: its repository
        root = found.path
    elif found.state == claude_tasks.FOLDER_OK:
        root = _checkout_root(found.path)
    else:                                              # gone, and nothing says where it moved
        root = cwd
    return _norm(root), os.path.basename(os.path.normpath(root)) or root


def _labels(keys_to_names: dict) -> dict:
    """`{key: label}` where two keys sharing a name are told apart by their parent folder."""
    by_name: dict = {}
    for key, name in keys_to_names.items():
        by_name.setdefault(name, []).append(key)
    out = {}
    for name, keys in by_name.items():
        if len(keys) == 1:
            out[keys[0]] = name
            continue
        for key in keys:
            parent = os.path.basename(os.path.dirname(key.rstrip("\\/"))) or key
            out[key] = f"{name} ({parent})"
    return out


def _group_iid(key: str) -> str:
    return "grp:" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def agent_of(row: dict) -> str:
    return AGENT_CURSOR if row.get("agent") == "cursor" else AGENT_CLAUDE


def build_plan(rows: list, agent: str, project: str, grouped: bool, iid_of, values_of) -> Plan:
    """Filter by agent and project, then lay the rows out (newest first, groups by their newest).

    `iid_of(row)` and `values_of(row)` are the caller's, so the row identity and its columns stay
    where they were. `project` is a label from `Plan.projects`; one that is no longer offered
    (its sessions aged out) means "all".
    """
    capped = tuple(a for a in (AGENT_CLAUDE, AGENT_CURSOR)
                   if sum(1 for r in rows if agent_of(r) == a) >= SCAN_LIMIT)
    by_agent = [r for r in rows if agent in (AGENT_ALL, "", None) or agent_of(r) == agent]
    identity = {id(r): project_of(r) for r in by_agent}
    labels = _labels({key: name for key, name in identity.values()})
    named = [(r, identity[id(r)][0]) for r in by_agent]
    offered = tuple(sorted({labels[k] for _r, k in named}, key=str.casefold))
    if project in offered:
        named = [(r, k) for r, k in named if labels[k] == project]
    entries: list = []
    if not grouped:
        entries = [Entry(iid_of(r), "", "session", values_of(r)) for r, _k in named]
        return Plan(tuple(entries), offered, len(entries), len(rows), capped)
    groups: dict = {}
    for r, k in named:
        groups.setdefault(k, []).append(r)
    order = sorted(groups, key=lambda k: max((r.get("last_activity") or 0) for r in groups[k]),
                   reverse=True)
    for key in order:
        gid = _group_iid(key)
        members = groups[key]
        entries.append(Entry(gid, "", "group", (), f"{labels[key]}  ({len(members)})"))
        entries += [Entry(iid_of(r), gid, "session", values_of(r)) for r in members]
    return Plan(tuple(entries), offered, len(named), len(rows), capped)


def _is_open(value) -> bool:
    return str(value).lower() in ("1", "true", "yes")


def apply_plan(tree, entries: tuple, previous: tuple) -> tuple:
    """Put `entries` in `tree` and return their structure for the next call.

    When the same rows sit in the same places as last time only their text is updated, so a
    60-second refresh does not collapse a group, move the scroll position or drop the selection.
    When the structure changed, everything is rebuilt, and what a person set is put back:
    which groups were open (new groups start open), the selection (rows that still exist) and
    the scroll position.
    """
    structure = tuple((e.iid, e.parent) for e in entries)
    if structure == previous:
        for e in entries:
            if e.kind == "group":
                tree.item(e.iid, text=e.text, values=e.values)
            else:
                tree.item(e.iid, values=e.values)
        return structure
    open_before = {}
    for e in entries:
        if e.kind == "group":
            try:
                open_before[e.iid] = _is_open(tree.item(e.iid, "open"))
            except Exception:                                  # noqa: BLE001 - not there yet
                pass
    selected = tuple(getattr(tree, "selection", lambda: ())())
    top = None
    if hasattr(tree, "yview"):
        try:
            top = tree.yview()[0]
        except Exception:                                      # noqa: BLE001
            top = None
    for child in list(tree.get_children()):
        tree.delete(child)
    for e in entries:
        if e.kind == "group":
            tree.insert("", "end", iid=e.iid, text=e.text, values=e.values,
                        open=open_before.get(e.iid, True))
        else:
            tree.insert(e.parent, "end", iid=e.iid, values=e.values)
    keep = tuple(i for i in selected if any(e.iid == i for e in entries))
    if keep and hasattr(tree, "selection_set"):
        tree.selection_set(keep)
    if top and hasattr(tree, "yview_moveto"):
        tree.yview_moveto(top)
    return structure


def summary(plan: Plan) -> str:
    """The line under the filters: what is shown, and what is not complete."""
    if plan.total and not plan.shown:
        return "No sessions match these filters."
    note = f"Showing {plan.shown} of {plan.total} sessions." if plan.shown != plan.total else \
        f"{plan.total} sessions."
    if plan.capped:
        note += (f" The newest {SCAN_LIMIT} per agent are read, and {' and '.join(plan.capped)} "
                 "reached that, so older ones are not listed.")
    return note
