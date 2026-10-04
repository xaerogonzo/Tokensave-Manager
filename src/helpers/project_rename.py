"""helpers/project_rename.py — rename a project folder as a coordinated migration.

A project's folder path is not one fact. It is referenced independently by a
running ``tokensave serve``, by ``~/.claude.json``'s per-project trust/MCP
record, by ``~/.claude/projects/<encoded-path>/``'s session transcripts, by
git's worktree metadata (for a linked worktree, in both directions), and by
this Manager's own ``manager-config.json``. No single transaction spans NTFS,
those two Claude Code stores and git — so a rename is treated here as a
migration with independently-reportable outcomes, never as one atomic op.

Every mutating function reports what it actually did (or why it did not),
rather than raising past the first failure — callers assemble those reports
into a ``VERIFIED`` / ``PARTIAL_FAILURE`` summary rather than a stack trace,
per this project's "unknown is never false" posture (see
`helpers/mcp_posture.py`).

``preflight()`` is meant to be called twice: once to populate the confirmation
dialog, and again, fresh, immediately before the mutation begins. Facts
gathered the first time do not authorize a mutation against a state that has
since changed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from constants import CREATE_NO_WINDOW
from helpers.claude_tasks import encode_project_path, scan_sessions, scan_worktrees
from helpers.mcp_paths import _claude_json_path, _write_json_atomic
from helpers.mcp_projects import matching_project_keys, normalize_project_key


# ── path comparison ─────────────────────────────────────────────────────────

def _same_dir(a: str, b: str) -> bool:
    """Do two paths name the same directory, ignoring case and `..`/`.` noise?"""
    def norm(p):
        try:
            return os.path.normcase(os.path.realpath(os.path.abspath(p)))
        except (OSError, ValueError):
            return os.path.normcase(p)
    return bool(a) and bool(b) and norm(a) == norm(b)


def is_case_only_rename(old_path: str, new_path: str) -> bool:
    """True when *new_path* differs from *old_path* only by letter case.

    Windows filesystems are case-insensitive but case-preserving, so a direct
    `os.rename` between two spellings of the same directory can no-op or
    raise depending on the filesystem driver. Callers route this case through
    a two-step rename via a temporary name instead of the ordinary path.
    """
    if old_path == new_path:
        return False
    try:
        a = os.path.normcase(os.path.normpath(old_path))
        b = os.path.normcase(os.path.normpath(new_path))
    except (OSError, ValueError):
        return False
    return a == b


# ── name / destination validation ───────────────────────────────────────────

@dataclass
class DestinationCheck:
    ok: bool
    reason: str = ""


_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_ILLEGAL_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def validate_new_name(name: str) -> DestinationCheck:
    """Reject a folder name Windows cannot create, before the OS call does.

    Helpers never rely on the dialog having validated input first — this is
    called from `validate_destination` too, not only from the UI layer.
    """
    if not name or name in (".", ".."):
        return DestinationCheck(False, "enter a folder name")
    if name != name.rstrip(" ."):
        return DestinationCheck(
            False, "Windows cannot create a name ending in a space or a period")
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        return DestinationCheck(False, f"'{name}' is a reserved Windows device name")
    bad = _ILLEGAL_CHARS & set(name)
    if bad:
        return DestinationCheck(
            False,
            "name contains a character Windows does not allow: "
            + " ".join(sorted(bad)))
    return DestinationCheck(True)


def validate_destination(old_path: str, new_path: str) -> DestinationCheck:
    """Full pre-flight validation of the target path a Rename dialog offers.

    Never trusts the caller to have already ruled out a collision, a nested
    destination, or a same-path no-op.
    """
    new_name = os.path.basename(os.path.normpath(new_path))
    name_check = validate_new_name(new_name)
    if not name_check.ok:
        return name_check

    if old_path == new_path:
        return DestinationCheck(False, "new path is identical to the current one")

    old_norm = os.path.normcase(os.path.normpath(old_path))
    new_norm = os.path.normcase(os.path.normpath(new_path))
    old_parts = old_norm.split(os.sep)
    new_parts = new_norm.split(os.sep)
    if len(new_parts) > len(old_parts) and new_parts[: len(old_parts)] == old_parts:
        return DestinationCheck(
            False, "the new location is inside the folder being renamed")

    if os.path.exists(new_path) and not is_case_only_rename(old_path, new_path):
        return DestinationCheck(False, "a file or folder already exists at the new path")

    return DestinationCheck(True)


# ── filesystem move ──────────────────────────────────────────────────────────

@dataclass
class MoveResult:
    ok: bool
    strategy: str = ""
    reason: str = ""
    rename_failed_reason: str = ""
    moved_children: list = field(default_factory=list)
    failed_child: str = ""
    rollback_attempted: bool = False
    rollback_succeeded: bool = False
    old_exists: "bool | None" = None
    new_exists: "bool | None" = None


def _error_reason(exc: OSError) -> str:
    """Classify an OSError into the narrow buckets the caller acts on.

    Deliberately not "any OSError means retry with the hazardous fallback" —
    a collision, a permissions problem and a cross-device move each need a
    different response, and only one of them is the "in use" case the
    move-children fallback exists for.
    """
    code = getattr(exc, "winerror", None)
    if code is None:
        code = exc.errno
    if code in (32, 33):        # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        return "in_use"
    if code in (5,):            # ERROR_ACCESS_DENIED
        return "access_denied"
    if code in (17, 183):       # EEXIST / ERROR_ALREADY_EXISTS
        return "collision"
    if code in (18,):           # EXDEV — cross-device rename
        return "cross_device"
    if code in (206, 36, 63):   # filename/path too long
        return "path_too_long"
    return "other"


def move_directory(old_path: str, new_path: str) -> MoveResult:
    """The only rename primitive attempted automatically.

    Uses the OS's own directory move (`os.rename`, which on Windows maps to
    `MoveFileEx` moving the directory — and everything under it — as one
    unit). No content-migration fallback runs from here: see
    `move_directory_contents_hazardous`, which is a separate, explicitly
    confirmed action the caller decides whether to offer.
    """
    if is_case_only_rename(old_path, new_path):
        tmp = old_path + ".tsm-rename-tmp"
        n = 0
        while os.path.exists(tmp):
            n += 1
            tmp = f"{old_path}.tsm-rename-tmp{n}"
        try:
            os.rename(old_path, tmp)
            os.rename(tmp, new_path)
        except OSError as exc:
            return MoveResult(False, strategy="case_only_rename", reason=str(exc),
                              rename_failed_reason=_error_reason(exc))
        return MoveResult(True, strategy="case_only_rename")

    try:
        os.rename(old_path, new_path)
    except OSError as exc:
        return MoveResult(False, strategy="rename", reason=str(exc),
                          rename_failed_reason=_error_reason(exc))
    return MoveResult(True, strategy="rename")


def is_in_use_failure(move_result: MoveResult) -> bool:
    """Whether *move_result* failed for the one reason the hazardous
    move-children fallback is offered for — never for a collision,
    permissions problem, or cross-device move, which it would not fix and
    would only make riskier to attempt."""
    return not move_result.ok and move_result.rename_failed_reason == "in_use"


def _rollback_children(new_path: str, old_path: str, moved: list) -> bool:
    ok = True
    for name in moved:
        try:
            os.rename(os.path.join(new_path, name), os.path.join(old_path, name))
        except OSError:
            ok = False
    return ok


def move_directory_contents_hazardous(old_path: str, new_path: str) -> MoveResult:
    """Last-resort fallback for the narrow 'directory in use' failure class.

    This is NOT a rename — it is a multi-file migration with no atomicity,
    per `docs/gotchas/windows-filesystem.md` #1's own recommended workaround.
    A failure partway through can leave real files split across both paths;
    every field on the returned `MoveResult` exists so that split state is a
    reportable fact rather than a swallowed exception. Callers must gate this
    behind its own explicit, separately-worded confirmation — never invoke it
    automatically from a generic `except OSError` around `move_directory`.
    """
    try:
        os.makedirs(new_path, exist_ok=False)
    except OSError as exc:
        return MoveResult(False, strategy="move_children", reason=str(exc))

    try:
        entries = list(os.scandir(old_path))
    except OSError as exc:
        return MoveResult(False, strategy="move_children", reason=str(exc))

    moved: list = []
    failed_child = ""
    for entry in entries:
        dest = os.path.join(new_path, entry.name)
        try:
            os.rename(entry.path, dest)
            moved.append(entry.name)
        except OSError:
            failed_child = entry.name
            break

    if failed_child:
        rollback_ok = _rollback_children(new_path, old_path, moved)
        return MoveResult(
            False, strategy="move_children",
            reason=f"could not move '{failed_child}'",
            moved_children=moved, failed_child=failed_child,
            rollback_attempted=True, rollback_succeeded=rollback_ok,
            old_exists=os.path.isdir(old_path), new_exists=os.path.isdir(new_path))

    try:
        os.rmdir(old_path)
    except OSError:
        pass  # a leftover empty shell, non-fatal — reported via old_exists below

    return MoveResult(
        True, strategy="move_children", moved_children=moved,
        old_exists=os.path.isdir(old_path), new_exists=os.path.isdir(new_path))


# ── git worktrees ────────────────────────────────────────────────────────────

def _run_git(git_exe: str, args: list, cwd: str, timeout: int = 20):
    """Return (ok, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            [git_exe, "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)


def is_linked_worktree(project_path: str) -> bool:
    """A linked worktree's `.git` is a FILE (a pointer), not a directory."""
    return os.path.isfile(os.path.join(project_path, ".git"))


def worktree_locked(project_path: str, git_exe: str) -> "bool | None":
    """Git's own `locked` declaration for this worktree, or `None` if unknown.

    A locked worktree is git's own statement that its location matters — a
    stronger signal than "the rename happened to fail" and never silently
    overridden by falling back to filesystem surgery.
    """
    ok, out, _ = _run_git(git_exe, ["worktree", "list", "--porcelain"], project_path)
    if not ok:
        return None
    target = os.path.normcase(os.path.realpath(project_path))
    for block in out.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        wt_path = lines[0][len("worktree "):].strip()
        try:
            same = os.path.normcase(os.path.realpath(wt_path)) == target
        except OSError:
            same = False
        if same:
            return any(line == "locked" or line.startswith("locked ") for line in lines)
    return None


def nested_worktree_conflict(project_path: str, linked_worktrees: list) -> bool:
    """True when a linked worktree of *project_path* lives inside it.

    Moving the main repo would then silently drag a second worktree along as
    a side effect of the move — reject the combination rather than manage it.
    """
    try:
        base = os.path.normcase(os.path.realpath(project_path)) + os.sep
    except OSError:
        return False
    for wt in linked_worktrees:
        wt_path = wt.get("path", "")
        if not wt_path:
            continue
        try:
            candidate = os.path.normcase(os.path.realpath(wt_path)) + os.sep
        except OSError:
            continue
        if candidate.startswith(base):
            return True
    return False


def move_linked_worktree(old_path: str, new_path: str, git_exe: str,
                         *, force: bool = False) -> MoveResult:
    """`git worktree move` — git owns its own metadata, both directions.

    Used instead of `move_directory` + `worktree repair` for a project that
    IS a linked worktree: git already has a first-class operation for this,
    and reimplementing its bookkeeping by hand is exactly the "manually move,
    then hope repair fixes it" pattern git's own docs warn against.
    """
    args = ["worktree", "move"]
    if force:
        args.append("--force")
    args += [old_path, new_path]
    ok, out, err = _run_git(git_exe, args, old_path)
    if not ok:
        return MoveResult(False, strategy="worktree_move", reason=err or out)
    return MoveResult(True, strategy="worktree_move")


def repair_main_worktree_links(new_path: str, git_exe: str) -> bool:
    """`git worktree repair` — for the case where the MAIN repo moved and its
    linked worktrees (which did not move) need their back-references fixed.
    Not used for the linked-worktree-moved case; `worktree move` needs no
    separate repair.
    """
    ok, _, _ = _run_git(git_exe, ["worktree", "repair"], new_path)
    return ok


# ── tokensave servers ────────────────────────────────────────────────────────

def servers_for_project(project_path: str, tokensave_exe: str) -> list:
    """Running tokensave servers this Manager can confidently attribute to
    *project_path* — never a guess offered up as certainty (see
    `helpers/tokensave_daemon.py`'s attribution contract)."""
    from helpers.tokensave_daemon import list_tokensave_servers
    try:
        target = os.path.normcase(os.path.realpath(project_path))
    except OSError:
        return []
    servers = list_tokensave_servers(tokensave_exe, known_projects=[project_path])
    out = []
    for srv in servers:
        if not srv.project:
            continue
        try:
            if os.path.normcase(os.path.realpath(srv.project)) == target:
                out.append(srv)
        except OSError:
            continue
    return out


# ── Claude Code sessions ─────────────────────────────────────────────────────

def sessions_for_project(project_path: str) -> list:
    """Session-transcript evidence for *project_path* — reported as evidence
    ("a session was observed, last activity Ns ago"), never as a verdict
    ("a session is active"). See `helpers/claude_tasks.py:scan_sessions`."""
    encoded = encode_project_path(os.path.normpath(project_path))
    return [s for s in scan_sessions([project_path]) if s["project_encoded"] == encoded]


def claude_projects_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def session_store_info(project_path: str) -> dict:
    """Facts about `~/.claude/projects/<encoded-path>/` — kept separate from
    `sessions_for_project`'s recency evidence: one is a directory that may or
    may not exist, the other is what's recently been written inside it."""
    encoded = encode_project_path(os.path.normpath(project_path))
    directory = os.path.join(claude_projects_root(), encoded)
    exists = os.path.isdir(directory)
    count = 0
    if exists:
        try:
            count = len([f for f in os.listdir(directory) if f.endswith(".jsonl")])
        except OSError:
            pass
    return {"dir": directory, "encoded": encoded, "exists": exists, "file_count": count}


# ── writability probes ───────────────────────────────────────────────────────

def _is_writable(path: str) -> "bool | None":
    """Best-effort: can this file be opened for append? `None` if the
    question doesn't apply (path unknown)."""
    if not path:
        return None
    try:
        if os.path.exists(path):
            with open(path, "a", encoding="utf-8"):
                pass
            return True
        directory = os.path.dirname(path) or "."
        return os.access(directory, os.W_OK)
    except OSError:
        return False


# ── preflight ────────────────────────────────────────────────────────────────

@dataclass
class RenamePreflight:
    project_path: str
    tokensave_servers: list = field(default_factory=list)
    is_linked_worktree: bool = False
    worktree_locked: "bool | None" = None
    linked_worktrees: list = field(default_factory=list)
    nested_worktree_conflict: bool = False
    active_sessions: list = field(default_factory=list)
    session_store_dir: str = ""
    session_store_exists: bool = False
    session_store_file_count: int = 0
    manager_cwd_inside_project: bool = False
    manager_config_writable: "bool | None" = None
    claude_json_writable: "bool | None" = None
    claude_process_running: "bool | None" = None  # always unknown — see note below

    @property
    def has_hazard(self) -> bool:
        """Any fact that should stop a silent, single-click rename."""
        return bool(
            self.tokensave_servers
            or self.active_sessions
            or self.worktree_locked
            or self.nested_worktree_conflict
            or self.manager_cwd_inside_project)


def preflight(project_path: str, cfg, git_exe: str) -> RenamePreflight:
    """Gather every independent fact a rename decision depends on.

    Called twice by design: once for the dialog, once again immediately
    before the mutation. Nothing here mutates anything.
    """
    from constants import _CONFIG_PATH

    pf = RenamePreflight(project_path=project_path)
    pf.tokensave_servers = servers_for_project(project_path, cfg.tokensave_exe)

    pf.is_linked_worktree = is_linked_worktree(project_path)
    if pf.is_linked_worktree:
        pf.worktree_locked = worktree_locked(project_path, git_exe)
    else:
        pf.linked_worktrees = scan_worktrees(project_path, git_exe)
    pf.nested_worktree_conflict = nested_worktree_conflict(
        project_path, pf.linked_worktrees)

    pf.active_sessions = sessions_for_project(project_path)
    store = session_store_info(project_path)
    pf.session_store_dir = store["dir"]
    pf.session_store_exists = store["exists"]
    pf.session_store_file_count = store["file_count"]

    pf.manager_cwd_inside_project = _same_dir(os.getcwd(), project_path)
    pf.manager_config_writable = _is_writable(_CONFIG_PATH)
    pf.claude_json_writable = _is_writable(_claude_json_path())

    # `claude_process_running` is deliberately left `None` (unknown) rather
    # than guessed: reliably tying a running process to THIS project would
    # need the agent's PID recorded at spawn time, which the Manager does
    # not currently track. A false "not running" here would be worse than an
    # honest "unknown" — see this project's "unknown is never false" rule.
    return pf


# ── manager-config.json repointing ──────────────────────────────────────────

def _repoint_project_categories(raw: dict, old_norm: str, new_path: str) -> list:
    cats = raw.get("project_categories")
    if not isinstance(cats, dict):
        return []
    touched = []
    for key in list(cats.keys()):
        if os.path.normcase(os.path.normpath(key)) == old_norm:
            cats[new_path] = cats.pop(key)
            touched.append(key)
    return touched


def _repoint_instructions_skip_paths(raw: dict, old_norm: str, new_path: str) -> list:
    skip_paths = raw.get("instructions_skip_paths")
    if not isinstance(skip_paths, list):
        return []
    touched = []
    for i, entry in enumerate(skip_paths):
        if isinstance(entry, str) and os.path.normcase(
                os.path.normpath(entry)) == old_norm:
            skip_paths[i] = new_path
            touched.append(entry)
    return touched


def _repoint_search_roots(raw: dict, old_norm: str, new_path: str) -> list:
    """Only an entry whose `.path` IS the project is rewritten — a root that
    merely CONTAINS the project must not be touched."""
    roots = raw.get("search_roots")
    if not isinstance(roots, list):
        return []
    touched = []
    for i, entry in enumerate(roots):
        path = entry if isinstance(entry, str) else (entry or {}).get("path", "")
        if not path or os.path.normcase(os.path.normpath(path)) != old_norm:
            continue
        if isinstance(entry, str):
            roots[i] = new_path
        else:
            entry["path"] = new_path
        touched.append(path)
    return touched


def _repoint_mcp_skip_warnings(raw: dict, old_path: str, new_path: str) -> list:
    """Entries are absolute *config file* paths UNDER a project root (e.g.
    `old_path/.mcp.json`), rewritten by replacing the `old_path` prefix and
    keeping the rest of the path — never treated as project-root entries."""
    warnings = raw.get("mcp_skip_warnings")
    if not isinstance(warnings, list):
        return []
    old_prefix = old_path.rstrip("\\/") + os.sep
    old_prefix_cf = os.path.normcase(old_prefix)
    touched = []
    for i, entry in enumerate(warnings):
        if isinstance(entry, str) and os.path.normcase(entry).startswith(old_prefix_cf):
            warnings[i] = os.path.join(new_path, entry[len(old_prefix):])
            touched.append(entry)
    return touched


def repoint_manager_config(cfg, old_path: str, new_path: str) -> "dict[str, list]":
    """Rewrite the path-keyed entries `manager-config.json` itself owns.

    Each field has its own match/rewrite semantics — this is a table, not a
    single normalized-string-equality pass over every field, because the
    fields don't mean the same thing (see the per-field helpers above).
    Returns which fields were actually touched, for the summary dialog —
    nothing is rewritten silently.
    """
    old_norm = os.path.normcase(os.path.normpath(old_path))
    fields = (
        ("project_categories", _repoint_project_categories(
            cfg.raw, old_norm, new_path)),
        ("instructions_skip_paths", _repoint_instructions_skip_paths(
            cfg.raw, old_norm, new_path)),
        ("search_roots", _repoint_search_roots(cfg.raw, old_norm, new_path)),
        ("mcp_skip_warnings", _repoint_mcp_skip_warnings(
            cfg.raw, old_path, new_path)),
    )
    touched = {name: entries for name, entries in fields if entries}
    if touched:
        cfg.save()
    return touched


# ── ~/.claude.json repointing ────────────────────────────────────────────────

@dataclass
class RepointResult:
    outcome: str  # "moved" | "collapsed" | "merged" | "blocked" | "no_match"
    old_keys: list = field(default_factory=list)
    new_key: str = ""
    conflicting_fields: list = field(default_factory=list)
    write_ok: "bool | None" = None
    verified: "bool | None" = None


def repoint_claude_json(old_path: str, new_path: str,
                        claude_json_path: str = "") -> RepointResult:
    """Rename `~/.claude.json`'s `projects` key for *old_path* to *new_path*.

    `new_path` — exactly as given, case preserved — becomes the stored key;
    normalisation is used only to MATCH and DEDUPLICATE, never to choose what
    gets written, since Windows is case-insensitive but case-preserving and
    the dialog's own spelling is the authoritative one.

    Merge policy when a record already exists at `new_path` (never invented
    ad hoc — each case is a deliberate, testable rule):
    * destination absent            -> move the old record under `new_path`.
    * destination identical         -> collapse to one record.
    * destination has disjoint keys -> deterministic union.
    * same field, different values  -> BLOCKED; both records are left in
      place for the user to resolve by hand rather than guessing a winner.
    """
    import json

    path = claude_json_path or _claude_json_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return RepointResult(outcome="no_match")

    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return RepointResult(outcome="no_match")

    old_keys = matching_project_keys(old_path, projects)
    if not old_keys:
        return RepointResult(outcome="no_match")

    new_norm = normalize_project_key(new_path)
    dest_key = next((k for k in projects if normalize_project_key(k) == new_norm), None)

    if dest_key is None:
        record = projects.pop(old_keys[0])
        for extra in old_keys[1:]:
            projects.pop(extra, None)
        projects[new_path] = record
        result = RepointResult(outcome="moved", old_keys=old_keys, new_key=new_path)
    else:
        source = projects.pop(old_keys[0])
        for extra in old_keys[1:]:
            projects.pop(extra, None)
        dest = projects[dest_key]
        if source == dest:
            del projects[dest_key]
            projects[new_path] = dest
            result = RepointResult(outcome="collapsed", old_keys=old_keys, new_key=new_path)
        else:
            conflicts = [k for k in source
                        if k in dest and dest[k] != source[k]]
            if conflicts:
                # Put the popped source record back — nothing is guessed.
                projects[old_keys[0]] = source
                return RepointResult(outcome="blocked", old_keys=old_keys,
                                     new_key=dest_key, conflicting_fields=conflicts)
            merged = {**source, **dest}
            del projects[dest_key]
            projects[new_path] = merged
            result = RepointResult(outcome="merged", old_keys=old_keys, new_key=new_path)

    data["projects"] = projects
    ok, _ = _write_json_atomic(path, data)
    result.write_ok = ok
    if ok:
        result.verified = _verify_claude_json_key(path, new_path)
    return result


def _verify_claude_json_key(claude_json_path: str, expected_key: str) -> bool:
    """Re-read the file after writing it. Claude Code can rewrite this file
    from its own in-memory state while running (the same reason the MCP
    config dialog already warns 'Claude is running'), so a write that
    reports success here is not proof it survived — this is."""
    import json

    try:
        with open(claude_json_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return False
    return any(normalize_project_key(k) == normalize_project_key(expected_key)
              for k in projects)


# ── ~/.claude/projects/<encoded>/ session store ─────────────────────────────

def repoint_session_store(old_path: str, new_path: str) -> str:
    """Move `~/.claude/projects/<encoded-old>/` to the new encoding, on
    request only — never automatic.

    This directory layout is an OBSERVED Claude Code behaviour, not a
    documented public contract, so a failure or an unrecognised layout here
    degrades to "left in place" rather than risking anything.

    Returns one of: "moved", "left_in_place", "not_present".
    """
    old_info = session_store_info(old_path)
    if not old_info["exists"]:
        return "not_present"
    new_encoded = encode_project_path(os.path.normpath(new_path))
    new_dir = os.path.join(claude_projects_root(), new_encoded)
    if os.path.exists(new_dir):
        return "left_in_place"
    try:
        os.rename(old_info["dir"], new_dir)
    except OSError:
        return "left_in_place"
    return "moved"


# ── post-migration verification ─────────────────────────────────────────────

@dataclass
class VerificationReport:
    servers_gone: "bool | None" = None
    tokensave_dir_present: "bool | None" = None
    worktree_registered: "bool | None" = None
    worktree_links_intact: "bool | None" = None
    git_dirs_resolve: "bool | None" = None


def verify(new_path: str, old_path: str, tokensave_exe: str, git_exe: str,
          *, was_linked_worktree: bool,
          expected_linked_worktrees: "list | None" = None) -> VerificationReport:
    """Establish facts about the post-migration state — never declares
    success merely because a directory exists where it's expected to."""
    report = VerificationReport()

    report.servers_gone = not servers_for_project(old_path, tokensave_exe)
    report.tokensave_dir_present = os.path.isdir(
        os.path.join(new_path, ".tokensave"))

    if was_linked_worktree:
        ok, out, _ = _run_git(git_exe, ["worktree", "list", "--porcelain"], new_path)
        target = os.path.normcase(os.path.realpath(new_path)) if ok else ""
        report.worktree_registered = ok and any(
            os.path.normcase(os.path.realpath(line[len("worktree "):].strip()))
            == target
            for line in out.splitlines() if line.startswith("worktree ")
        )
    elif expected_linked_worktrees:
        ok, out, _ = _run_git(git_exe, ["worktree", "list", "--porcelain"], new_path)
        registered = {
            os.path.normcase(os.path.realpath(line[len("worktree "):].strip()))
            for line in out.splitlines() if line.startswith("worktree ")
        } if ok else set()
        report.worktree_links_intact = ok and all(
            os.path.normcase(os.path.realpath(wt.get("path", ""))) in registered
            for wt in expected_linked_worktrees if wt.get("path"))

    ok_dir, _, _ = _run_git(git_exe, ["rev-parse", "--git-dir"], new_path)
    ok_common, _, _ = _run_git(git_exe, ["rev-parse", "--git-common-dir"], new_path)
    report.git_dirs_resolve = ok_dir and ok_common

    return report
