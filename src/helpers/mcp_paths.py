"""Where MCP config lives, and the primitives everything else needs.

The leaf of the family: it imports no sibling. `helpers/mcp_shadow.py` and `helpers/mcp_desktop.py` import from HERE rather than from the facade, so the facade can import this module without closing a cycle.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import glob
import json
import os
import re
from constants import _BASE_DIR




def _resolve_desktop_cfg_path() -> str:
    """Locate Claude Desktop's MCP config file — UWP-aware.

    Claude Desktop ships in two install flavours that store the same
    `claude_desktop_config.json` at different physical paths:

      1. Microsoft Store / UWP install (Claude_pzs8sxrjxfjjc package family):
         %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\
             claude_desktop_config.json

      2. Traditional installer:
         %APPDATA%\\Claude\\claude_desktop_config.json

    The UWP case has a brutal gotcha: Windows file-path redirection is
    ASYMMETRIC across UWP-context and non-UWP-context processes. A UWP
    process opening `%APPDATA%\\Claude\\<file>` gets redirected to the
    package's LocalCache. A non-UWP process opening the same path string
    sees a DIFFERENT physical file (the user's normal-view file) — both
    files exist on disk simultaneously, with the same path, and Windows
    silently picks one based on caller context.

    The manager is a non-UWP process. If we point it at `%APPDATA%\\Claude\\`
    on a UWP-installed Desktop, the manager will read/write the wrong
    file — Desktop never sees those changes. Diagnosed when a user's
    Apply through the configurator wrote 231 bytes of canonical content
    to the external `%APPDATA%\\Claude\\` file while Desktop continued
    serving `tokensave.exe -p <hardcoded>` from its 2,219-byte UWP-internal
    file.

    Detection: glob for any `Claude_*` package directory. When found,
    target the UWP-internal path — that's the one Desktop actually reads.
    Otherwise fall back to the traditional path.

    Multiple Claude packages can in principle co-exist (Stable + Beta);
    we pick the most-recently-modified config file as a heuristic for
    "the one Desktop is currently running from".
    """
    local = os.environ.get("LOCALAPPDATA", "")
    traditional = os.path.join(
        os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json")
    if local:
        try:
            candidates = glob.glob(os.path.join(
                local, "Packages", "Claude_*",
                "LocalCache", "Roaming", "Claude",
                "claude_desktop_config.json"))
        except OSError:
            candidates = []
        # Most-recently-touched first.
        candidates = sorted(
            candidates,
            key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)
        if candidates:
            return candidates[0]
    return traditional



def _mcp_desktop_cfg_path() -> str:
    """Claude Desktop config path, resolved lazily (re-evaluates per call).

    Lazy resolution matters because tests redirect ``$USERPROFILE`` /
    ``$LOCALAPPDATA`` / ``$APPDATA`` via the ``fake_home`` fixture
    (G-F + G-J); a module-level constant would capture the developer's
    real path at import time and silently bypass the fixture. See
    ``tests/test_no_import_time_path_resolution.py`` (G-L) for the
    pre-flight test that enforces this invariant.
    """
    return _resolve_desktop_cfg_path()



def _mcp_code_cfg_path() -> str:
    """Claude Code config path (``~/.claude.json``), resolved lazily."""
    return os.path.join(os.environ.get("USERPROFILE", ""), ".claude.json")



def _mcp_configs() -> list[tuple[str, str]]:
    """Friendly-label + path pairs for every MCP config the manager touches.

    Returns a fresh list each call so test-environment redirects apply.
    Callers iterate ``for label, path in _mcp_configs(): ...`` — same
    shape as the old module-level constant, just lazily evaluated.
    """
    return [
        ("Claude Desktop", _mcp_desktop_cfg_path()),
        ("Claude Code",    _mcp_code_cfg_path()),
    ]



def _wrapper_path() -> str:
    """Where tokensave-wrapper lives for this installation.

    In a Nuitka onefile build the wrapper is a sibling .exe; in source mode
    it's the .py next to app.py (under src/). Matches the same lookup the
    Reference tab already does.
    """
    if os.environ.get("NUITKA_ONEFILE_PARENT"):
        return os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
    return os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")



#: What a project-scoped entry passes as `-p`. A literal "." rather than an
#: absolute path or ${CLAUDE_PROJECT_DIR}, and both halves of that were
#: measured rather than assumed (Roadmap-11 Phase 0):
#:
#:   * Claude Code spawns a project-scoped MCP server with cwd = the project
#:     root, even when the session was launched from a subdirectory. Verified by
#:     elimination: an explicit path does not search upward (upstream #372), so
#:     a cwd of `<root>/src` would have errored instead of answering.
#:   * `${CLAUDE_PROJECT_DIR:-X}` resolves to X — the variable is not set at
#:     config-resolution time, and does not appear in a Claude Code session's
#:     environment at all. The documented-looking form silently degrades to its
#:     default, so it buys nothing over ".".
#:
#: "." is what keeps the file portable: a `.mcp.json` is project-scoped config
#: meant to be shared through version control, and an absolute path would make
#: it a machine-local file wearing a shared file's name.
PROJECT_PATH_ARG = "."


#: Config key: add `.mcp.json` to the project's .gitignore after binding.
#: Default ON, and the default is a judgement call worth stating. The file
#: is deliberately portable so it CAN be committed — but committing it
#: hands every collaborator an MCP server definition that only works if
#: they happen to have tokensave on PATH. Opting them in silently is the
#: ruder default, so the manager ignores by default and lets anyone who
#: wants it shared turn this off.
GITIGNORE_PROJECT_MCP_KEY = "gitignore_project_mcp"


#: Set when the user-scoped `tokensave` entry is retired, so its later ABSENCE
#: reads as a completed decision rather than as a missing entry to re-add.
#: Recorded rather than inferred: "no entry and some project is bound" would
#: also match a user who never had one, and the difference matters because one
#: of them should be offered the entry and the other must not be.
USER_SCOPE_RETIRED_KEY = "mcp_user_scope_retired"


#: The same decision for Claude Desktop's own entry — see
#: :mod:`helpers.mcp_desktop`, which owns that migration. The constant lives
#: HERE because `_classify_mcp_entry` must read it to stop reporting a
#: deliberate absence as a defect, and `mcp_desktop` imports from this module;
#: defining it there and importing it back would be a cycle.
DESKTOP_SCOPE_RETIRED_KEY = "mcp_desktop_scope_retired"



def _project_mcp_path(project_root: str) -> str:
    """Where Claude Code looks for a project's own MCP config."""
    return os.path.join(project_root, ".mcp.json")



def _same_project(a: str, b: str) -> bool:
    """Do two paths name the same checkout?

    `D:\\P\\Foo`, `D:\\P\\.\\Foo` and `D:\\P\\Foo\\` are one directory, and on
    Windows so are case variants and junction aliases. Comparing raw strings
    would report "bound to a different project" for a project bound to itself.
    """
    def norm(p):
        try:
            return os.path.normcase(os.path.realpath(os.path.abspath(p)))
        except (OSError, ValueError):
            return os.path.normcase(p)
    return bool(a) and bool(b) and norm(a) == norm(b)



def _write_json_atomic(cfg_path: str, data: dict) -> "tuple[bool, str]":
    """Temp file + os.replace, never a truncating in-place write.

    A half-written Claude config does not read as damaged, it reads as
    *absent* — the classifier would call it `unparseable` at best, and Claude
    itself would lose every server in the file. The failure would be far worse
    than the failed write it came from. Same reasoning as
    `tokensave_config.set_strict_tree`.
    """
    import tempfile

    directory = os.path.dirname(cfg_path) or "."
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcp_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
    except OSError as exc:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, f"Could not write config: {exc}"
    return True, ""



# ── canonical `~/.claude.json` project keys ───────────────────────────────

#: Claude Code keys per-project state by the directory a session was launched
#: in, spelled however the launcher spelled it. `D:\Random Projects\Foo` and
#: `D:/Random Projects/Foo` are one directory and routinely end up as two keys
#: with divergent state, so an approval recorded under one is invisible to a
#: reader matching the other. Exact string matching against these keys is
#: therefore always a bug.
_DRIVE_RE = re.compile(r"^([A-Za-z]):")



def _claude_json_path() -> str:
    """Claude Code's own config, which holds per-project MCP approval state."""
    return os.path.join(os.path.expanduser("~"), ".claude.json")
