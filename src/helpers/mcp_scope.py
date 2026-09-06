"""Which scope wins, and whether Claude Code is live to care.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from constants import CREATE_NO_WINDOW
from helpers.mcp_paths import (
    _claude_json_path,
)
from helpers.mcp_projects import (
    canonical_launch_dir,
)
from helpers.mcp_approval import (
    APPROVAL_APPROVED,
)




# ── which definition is Claude Code actually using? ───────────────────────

SCOPE_PROJECT = "project"

SCOPE_USER = "user"

SCOPE_LOCAL = "local"

SCOPE_ABSENT = "absent"

SCOPE_UNKNOWN = "unknown"


#: Claude Code resolves local > project > user and dedupes by server NAME, so a
#: project `.mcp.json` does not automatically win: a local definition for the
#: same name overrides it, and an unapproved project entry does not take effect
#: at all. Rather than re-implement that precedence — and eventually claim
#: "bound" while something else is serving — ask the client, which prints the
#: winner directly.


@dataclasses.dataclass(frozen=True)
class EffectiveScope:
    """What `claude mcp get <name>` reports for a project."""

    scope: str
    pending_approval: bool = False
    connected: bool = False
    detail: str = ""

    @property
    def is_known(self) -> bool:
        return self.scope != SCOPE_UNKNOWN

    @property
    def is_project(self) -> bool:
        return self.scope == SCOPE_PROJECT

    @property
    def is_shadowed(self) -> bool:
        """A project binding exists on disk but something else is serving.

        Only meaningful for a project the caller already knows is bound; this
        type cannot tell "shadowed" from "never bound" on its own.
        """
        return self.scope in (SCOPE_USER, SCOPE_LOCAL)



def _parse_mcp_get(text: str) -> "EffectiveScope":
    """Parse `claude mcp get` output. Pure, so the shapes can be tested.

    Keyed off the words the CLI actually prints, captured from live runs:

        Scope: Project config (shared via .mcp.json)
        Scope: User config (available in all your projects)
        Status: ⏸ Pending approval (run `claude` to approve)
        Status: ✔ Connected
    """
    low = (text or "").lower()
    if "no mcp server" in low or "not found" in low:
        return EffectiveScope(SCOPE_ABSENT, detail=text.strip()[:200])

    scope = SCOPE_UNKNOWN
    for line in (text or "").splitlines():
        stripped = line.strip().lower()
        if not stripped.startswith("scope:"):
            continue
        if "project config" in stripped:
            scope = SCOPE_PROJECT
        elif "local config" in stripped:
            scope = SCOPE_LOCAL
        elif "user config" in stripped:
            scope = SCOPE_USER
        break

    return EffectiveScope(
        scope,
        pending_approval="pending approval" in low,
        connected="connected" in low and "pending approval" not in low,
        detail=text.strip()[:200])



def describe_effective(got: "EffectiveScope", server: str = "tokensave",
                       approval: "str | None" = None,
                       trusted: "bool | None" = None) -> tuple:
    """`(state, label, issue)` for a row Claude Code has been asked about.

    Pure, so every verdict the dialog can display is testable without a CLI.
    Returns `None` when the answer carries no information — an unreachable
    `claude`, a timeout — because overwriting a row that already says
    something true with "could not verify" trades a correct badge for a
    complaint about our own tooling.

    `approval` is the cheap tier's verdict, and passing it is what stops this
    tier from overriding a true row with a stale one. **`claude mcp get` is not
    a reliable source for approval**: measured 2026-08-25, it reported
    `⏸ Pending approval` for a project whose server was demonstrably running
    and serving that project's own graph, because it does not read
    `.claude/settings.local.json`. It IS reliable about scope resolution —
    which definition wins — so those verdicts are kept.

    `trusted` is `helpers.mcp_projects.project_trust_state` as a tri-state
    (`True` / `False` / `None` for unknown), and it changes the *advice*, not
    just the wording. Claude Code does not load a project's `.mcp.json` in an
    untrusted folder, so such a project falls back to the user-scoped entry
    without any precedence contest having happened — and the old text told
    that user to retire the entry, which would have left them with no server
    at all. Measured on six projects with byte-identical `.mcp.json` files
    and approval granted in every one: trust alone separated the two that
    served from the four that did not.
    """
    if got is None or not got.is_known:
        return None
    if got.is_project:
        return ("ok", "✓ bound — verified serving", "")
    if got.pending_approval:
        if approval == APPROVAL_APPROVED:
            return None          # the client is behind; do not contradict it
        return ("project_unapproved", "⚠ written, not yet approved",
                ("Claude Code reports this binding as pending approval, so "
                 "sessions here still fall back to the user-scoped entry. Run "
                 "`claude` once in this project and approve it."))
    if got.is_shadowed:
        if trusted is False:
            # Trust gates BEFORE precedence: Claude Code does not load a
            # project's .mcp.json at all in a folder it has not been trusted
            # in, so the user-scoped entry answers by default rather than by
            # winning anything. Telling this user to retire that entry would
            # not bind the project — it would leave it with no server at all.
            return ("project_untrusted",
                    "⚠ folder not trusted — .mcp.json is not loaded",
                    ("Claude Code has not been trusted in this folder, so it "
                     "does not load this project's .mcp.json at all and "
                     "sessions fall back to the %s-scoped `%s`. Run `claude` "
                     "once here and accept \"Do you trust the files in this "
                     "folder?\" — that is the whole fix, and it cannot be "
                     "done by editing a file. Do NOT retire the %s-scoped "
                     "entry for this: trust is the blocker, not precedence, "
                     "and removing it would leave this project with no `%s`."
                     % (got.scope, server, got.scope, server)))
        return ("project_shadowed",
                "⚠ shadowed by the %s-scoped entry" % got.scope,
                ("This file is correct, but Claude Code reports it is serving "
                 "the %s-scoped `%s` instead. Two different things produce "
                 "that: the %s-scoped entry genuinely taking precedence, or "
                 "this folder not being trusted — in which case the project "
                 "entry is never loaded and the fallback is all there is. "
                 "Run `claude` once here first; it settles the trust question "
                 "and shows which server actually answers. Retire the "
                 "%s-scoped entry only once trust is granted, because doing "
                 "it while trust is the blocker leaves this project with no "
                 "`%s` at all."
                 % (got.scope, server, got.scope, got.scope, server)))
    if got.scope == SCOPE_ABSENT:
        return ("missing", "✗ Claude Code sees no %s at all" % server,
                ("The file is on disk but Claude Code reports no `%s` server "
                 "in this project. Check that `%s` resolves as a command."
                 % (server, server)))
    return None



def effective_scope(project_root: str, server: str = "tokensave",
                    timeout: int = 45) -> "EffectiveScope":
    """Ask Claude Code which `server` definition wins inside `project_root`.

    Run with cwd set to the project, because the answer is per-directory. Any
    failure — no `claude` on PATH, a timeout, unexpected output — comes back as
    UNKNOWN rather than a guess: reporting "shadowed" because a CLI call fell
    over would send the user hunting for a conflict that does not exist.

    The cwd goes through `canonical_launch_dir` rather than being passed
    through raw. Claude Code records per-project state under the spelling of
    the directory it was started in, so a status check run with a spelling the
    user never uses does not just read the wrong entry — it CREATES a second
    one, and this function was itself a source of the duplicate keys it now
    has to see past.
    """
    if not project_root or not os.path.isdir(project_root):
        return EffectiveScope(SCOPE_UNKNOWN, detail="no such project directory")

    # Resolve the launcher explicitly. On Windows `claude` is an npm shim
    # (`claude.CMD`), and CreateProcess does not apply PATHEXT the way a shell
    # does -- so a bare "claude" here fails with WinError 2 even though the
    # same command works in a terminal. shutil.which does apply it.
    import shutil
    exe = shutil.which("claude")
    if not exe:
        return EffectiveScope(SCOPE_UNKNOWN,
                              detail="the `claude` CLI is not on PATH")
    try:
        proc = subprocess.run(
            [exe, "mcp", "get", server],
            capture_output=True, text=True, timeout=timeout,
            cwd=canonical_launch_dir(project_root),
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EffectiveScope(SCOPE_UNKNOWN, detail=str(exc)[:200])
    return _parse_mcp_get((proc.stdout or "") + "\n" + (proc.stderr or ""))



#: How recently `~/.claude.json` must have been written to count as evidence
#: that a Claude Code session is live. Measured 2026-08-25 with one session
#: open: 30 seconds old. Generous enough to cover an idle session, short enough
#: that yesterday's file does not read as one.
_CLAUDE_JSON_ACTIVE_SECS = 300



def claude_code_active(claude_json_path: str = "") -> "tuple[bool, str]":
    """Is a Claude Code session live? Answered from the FILE, not the process list.

    Process-name matching already went stale here once, silently: this module
    looked for `claude-code.exe`, which has never existed, so `code` was
    permanently False and the one warning that mattered for `~/.claude.json`
    could never fire. Claude Code ships as an npm CLI (running as `node.exe`,
    far too generic to match on), as a native binary, and hosted inside the
    desktop app — where it is `claude.exe` and therefore indistinguishable from
    Desktop by name. No executable name settles this question.

    The file's own mtime does, and it is evidence rather than inference: a
    session rewrites `~/.claude.json` continuously, so a recent write means
    something is actively writing the file we are about to edit. That is
    precisely the risk the warning exists to describe, and it holds however
    Claude Code happens to be packaged.

    Returns `(active, detail)`; `detail` is for display, so it says how the
    answer was reached rather than making the user take it on trust.
    """
    path = claude_json_path or _claude_json_path()
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False, ""
    if age <= _CLAUDE_JSON_ACTIVE_SECS:
        if age < 90:
            return True, "%s was written %d seconds ago" % (
                os.path.basename(path), max(0, int(age)))
        return True, "%s was written %d minutes ago" % (
            os.path.basename(path), max(1, int(age // 60)))
    return False, ""



def _is_claude_running() -> dict:
    """Detect running Claude Desktop / Claude Code.

    Returns `{"desktop": bool, "code": bool, "pids": [int, ...],
    "code_detail": str}`.

    Why this matters: both apps rewrite their own MCP config from in-memory
    state, so an edit made while one is running is silently clobbered — Desktop
    within ~1-2 minutes for `claude_desktop_config.json`, and a Claude Code
    session for `~/.claude.json`. The dialog refuses to write a config whose
    owning app is live, so the user gets "quit it, then retry" instead of a fix
    that mysteriously reverts.

    `desktop` comes from the process list; `code` comes from the config's mtime
    for the reasons in :func:`claude_code_active`. Best-effort throughout —
    a false result must not block a write, because the alternative is a dialog
    that cannot be used when detection breaks.
    """
    code, code_detail = claude_code_active()
    result = {"desktop": False, "code": code, "pids": [],
              "code_detail": code_detail}
    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return result
    for line in (r.stdout or "").splitlines():
        # CSV: "claude.exe","12345","Console","1","123,456 K"
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0].lower()
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if name == "claude.exe":
            result["desktop"] = True
            result["pids"].append(pid)
    return result
