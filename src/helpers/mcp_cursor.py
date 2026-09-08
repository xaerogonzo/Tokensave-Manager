"""Cursor's MCP configuration — where it lives and what state it is in.

Cursor reads MCP servers from two files, and they use the same JSON shape as
Claude Code's: a top-level ``mcpServers`` object of ``command`` / ``args`` /
``env`` entries.

    <project>/.cursor/mcp.json     project scope
    ~/.cursor/mcp.json             global scope

Because the shape matches, the entry BODY is reused verbatim from
:func:`helpers.mcp_classify._canonical_project_entry` rather than rebuilt here.
Only the destination differs.

What is deliberately NOT reused
-------------------------------
The trust gate. Claude Code gates project ``.mcp.json`` loading behind
``hasTrustDialogAccepted`` in ``~/.claude.json``, and `helpers/mcp_projects.py`
carries a good deal of machinery for reasoning about it. **Cursor has no
equivalent**, so none of that vocabulary appears here. Reporting a Cursor
binding as "blocked by trust" would be inventing a verdict the product cannot
produce — the same class of mistake as the false-green MCP rows that reported a
file's contents as an effective binding.

What this module refuses to collapse
------------------------------------
`absent` and `malformed` are different answers. A missing file means "nothing
has been configured here"; a file that exists but does not parse means "a
configuration exists and is broken", which needs a human rather than a silent
re-write. Folding the second into the first would let the manager cheerfully
overwrite a file the user is halfway through editing, and would report a
damaged config as an empty one. Absence is never success.
"""

from __future__ import annotations

import json
import os

from helpers.mcp_paths import _same_project, _write_json_atomic

#: Verdicts from :func:`cursor_binding_state`. Five, not three.
STATE_ABSENT = "absent"
STATE_PRESENT_CORRECT = "present_correct"
STATE_PRESENT_WRONG_TARGET = "present_wrong_target"
STATE_MALFORMED = "malformed"
STATE_UNREADABLE = "unreadable"

#: Human-readable one-liners, so every caller words these the same way.
STATE_LABELS = {
    STATE_ABSENT: "No Cursor MCP entry",
    STATE_PRESENT_CORRECT: "Bound to this project",
    STATE_PRESENT_WRONG_TARGET: "Bound to a different project",
    STATE_MALFORMED: "Config file is not valid JSON",
    STATE_UNREADABLE: "Config file could not be read",
}

#: The server key the manager writes. Matches the Claude-side spelling so a
#: user reading both files sees one name for one thing.
SERVER_KEY = "tokensave"


def cursor_project_mcp_path(project_root: str) -> str:
    """Where Cursor looks for a project's own MCP config."""
    return os.path.join(project_root, ".cursor", "mcp.json")


def cursor_global_mcp_path() -> str:
    """Cursor's machine-wide MCP config.

    Resolved at call time, never at import: tests redirect ``$USERPROFILE`` via
    the ``fake_home`` fixture, and a module-level constant would capture the
    developer's real home before the fixture runs (G-L).
    """
    return os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")


def read_cursor_mcp(path: str) -> "tuple[dict | None, str]":
    """Return ``(config, state)`` for one Cursor MCP file.

    ``config`` is None for every state except a successful parse. The state is
    the caller's cue about what it is allowed to do next: only a parsed file may
    be rewritten, because rewriting a malformed one discards whatever the user
    was in the middle of writing.
    """
    if not os.path.isfile(path):
        return None, STATE_ABSENT
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, STATE_MALFORMED
    except OSError:
        return None, STATE_UNREADABLE
    if not isinstance(data, dict):
        # Valid JSON, wrong document: a list or a bare string parses fine and
        # would sail past a json.load-only check straight into a KeyError.
        return None, STATE_MALFORMED
    return data, STATE_PRESENT_CORRECT


def _entry_target(entry: dict) -> str:
    """The project path an entry points at, or "" for the portable form.

    The canonical entry uses a literal "." so the file stays shareable, which
    means "no explicit target" is the healthy case, not a defect.
    """
    if not isinstance(entry, dict):
        return ""
    args = entry.get("args")
    if not isinstance(args, list):
        return ""
    for i, arg in enumerate(args):
        if arg == "-p" and i + 1 < len(args):
            target = args[i + 1]
            return "" if target == "." else str(target)
    return ""


def cursor_binding_state(project_root: str,
                         server: str = SERVER_KEY) -> "tuple[str, str]":
    """Return ``(state, detail)`` for a project's Cursor MCP binding.

    ``detail`` is a short human-readable clause for the UI — the offending
    target for a wrong-target binding, the parse error for a malformed file —
    so the dialog can say what is wrong rather than only that something is.
    """
    path = cursor_project_mcp_path(project_root)
    data, state = read_cursor_mcp(path)
    if data is None:
        return state, path
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server not in servers:
        return STATE_ABSENT, path
    target = _entry_target(servers[server])
    if target and not _same_project(target, project_root):
        return STATE_PRESENT_WRONG_TARGET, target
    return STATE_PRESENT_CORRECT, path


def bind_cursor_project(project_root: str, entry: dict,
                        server: str = SERVER_KEY) -> "tuple[bool, str]":
    """Write *entry* into the project's ``.cursor/mcp.json``.

    Merges into whatever is already there: every other server in the file is
    preserved, because this file is shared with whatever else the user has
    wired into Cursor and clobbering it would be a data-loss bug wearing a
    feature's clothes.

    Refuses to touch a malformed or unreadable file. That is the whole reason
    the state is not collapsed into "absent" — an overwrite there destroys work.
    """
    path = cursor_project_mcp_path(project_root)
    data, state = read_cursor_mcp(path)
    if state in (STATE_MALFORMED, STATE_UNREADABLE):
        return False, (f"{STATE_LABELS[state]}: {path}. "
                       f"Fix or remove it, then try again.")
    if data is None:
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[server] = entry
    data["mcpServers"] = servers

    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return False, f"Could not create {directory}: {exc}"
    return _write_json_atomic(path, data)


def unbind_cursor_project(project_root: str,
                          server: str = SERVER_KEY) -> "tuple[bool, str]":
    """Remove the manager's server from a project's Cursor MCP config.

    Leaves the file in place when other servers remain; only the one key is
    removed. A file left holding an empty ``mcpServers`` is still written
    rather than deleted, because deleting a file the user created is a bigger
    action than the one they asked for.
    """
    path = cursor_project_mcp_path(project_root)
    data, state = read_cursor_mcp(path)
    if data is None:
        # Nothing to remove, and a broken file is not ours to rewrite.
        return (True, "") if state == STATE_ABSENT else (
            False, f"{STATE_LABELS[state]}: {path}")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server not in servers:
        return True, ""
    del servers[server]
    data["mcpServers"] = servers
    return _write_json_atomic(path, data)


# ── .gitignore treatment ─────────────────────────────────────────────────────

#: Config key: add `.cursor/mcp.json` to the project's .gitignore after binding.
#: Default ON, mirroring GITIGNORE_PROJECT_MCP_KEY and for the same reason — the
#: entry names a server that only resolves on a machine with tokensave on PATH,
#: so committing it opts collaborators into a binding that will not work for
#: them.
GITIGNORE_CURSOR_MCP_KEY = "gitignore_cursor_mcp"

#: The pattern written. Deliberately the FILE, never the `.cursor/` directory.
#:
#: Two different classes of thing live under `.cursor/`, and a directory-wide
#: ignore would sweep up both:
#:
#:     .cursor/rules/*.mdc   shared project configuration  -> COMMITTED
#:     .cursor/mcp.json      machine-local executable paths -> IGNORED
#:
#: Ignoring the directory would silently stop the rules files this manager
#: writes from ever being committed, which is the opposite of the point of
#: writing them. `tests/test_gitignore_cursor.py` asserts the split against
#: real `git check-ignore` output rather than against the pattern text.
CURSOR_MCP_IGNORE_PATTERN = ".cursor/mcp.json"


def ensure_cursor_mcp_ignored(project_root: str,
                              raw: "dict | None" = None) -> "tuple[bool, str]":
    """Add `.cursor/mcp.json` to the project's .gitignore, if enabled.

    Returns ``(added, detail)``. Never raises: failing to ignore is not a
    reason to report a successful binding as failed.
    """
    settings = raw if isinstance(raw, dict) else {}
    if not settings.get(GITIGNORE_CURSOR_MCP_KEY, True):
        return False, ""
    try:
        from helpers.gitignore import ensure_pattern
        return ensure_pattern(
            project_root, CURSOR_MCP_IGNORE_PATTERN,
            comment="# Cursor MCP binding (TokenSave Manager)")
    except Exception as exc:                                  # noqa: BLE001
        return False, f"could not update .gitignore: {exc}"
