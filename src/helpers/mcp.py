"""MCP-config introspection + canonical-shape helpers.

Two Claude apps each have their own MCP config; both need to point at
tokensave-wrapper for the manager's ★ Set as Active pin to drive which
project gets served. Without the wrapper, an MCP entry that runs
`tokensave.exe serve -p <hardcoded>` bypasses the pin file entirely —
the failure mode that produced this whole subsystem.

Pure helpers, no Tk. Easy to unit-test from the command line.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import os
import shutil
import subprocess
import sys
import time

from constants import _BASE_DIR, CREATE_NO_WINDOW


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


def _canonical_mcp_entry(cfg: dict) -> dict:
    """The MCP server entry the manager wants every Claude config to have.

    Source-mode shape: pythonw.exe → tokensave-wrapper.py
    Bundled-mode shape: tokensave-wrapper.exe directly (no python needed)

    The python_exe field in manager-config.json supplies pythonw — same one
    the .bat launcher uses, so users who already configured the launcher
    don't have to pick a python a second time.
    """
    wrapper = _wrapper_path()
    if wrapper.lower().endswith(".exe"):
        return {"command": wrapper, "args": []}
    # Source mode — need a python interpreter for the .py wrapper.
    py = (cfg.get("python_exe") if isinstance(cfg, dict) else "") or ""
    if not py:
        # Best-effort default: the same pythonw that's running this script.
        # User can override in Settings.
        py_candidate = sys.executable.replace("python.exe", "pythonw.exe")
        py = py_candidate if os.path.isfile(py_candidate) else sys.executable
    return {"command": py, "args": [wrapper]}


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


def _project_mcp_path(project_root: str) -> str:
    """Where Claude Code looks for a project's own MCP config."""
    return os.path.join(project_root, ".mcp.json")


def _canonical_project_entry(cfg: dict) -> dict:
    """The entry the manager wants in a project's `.mcp.json`.

    Takes no project root **on purpose**. It returns a template, not an
    interpolated path, which is what structurally prevents this machine's paths
    from reaching a file other people may check out. The project root is used
    for classifying an existing entry and for choosing a verification cwd —
    never for building this.

    Deliberately not the wrapper: the wrapper exists to read the Desktop pin,
    and a project binding must ignore the pin entirely. `cfg` is accepted for
    symmetry with :func:`_canonical_mcp_entry` and for future binding modes.
    """
    return {"command": "tokensave", "args": ["serve", "-p", PROJECT_PATH_ARG]}


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


@dataclasses.dataclass
class _McpCtx:
    cmd: str
    cmd_lower: str
    args: list
    is_claude_code: bool
    is_project_scoped: bool = False
    project_root: str = ""


def _chk_bundled_wrapper(ctx: "_McpCtx", base: dict) -> dict | None:
    if ctx.cmd_lower.endswith("tokensave-wrapper.exe"):
        return {**base, "state": "ok", "label": "✓ correct (bundled wrapper)", "issue": ""}
    return None


def _chk_python_wrapper(ctx: "_McpCtx", base: dict) -> dict | None:
    if not (ctx.cmd_lower.endswith("pythonw.exe") or ctx.cmd_lower.endswith("python.exe")):
        return None
    if not (ctx.args and isinstance(ctx.args[0], str)
            and ctx.args[0].lower().endswith("tokensave-wrapper.py")):
        return None
    if os.path.isfile(ctx.args[0]):
        return {**base, "state": "ok", "label": "✓ correct", "issue": ""}
    return {**base, "state": "wrong_wrapper", "label": "⚠ wrapper path missing",
            "issue": (f"Points at {ctx.args[0]} but that file doesn't exist. "
                      "Click Apply to update to the current wrapper location.")}


def _chk_project_scoped(ctx: "_McpCtx", base: dict) -> dict | None:
    """Verdicts for a project's own `.mcp.json`, where the rules invert.

    In a global config a hardcoded `-p` is a defect — it locks every session to
    one project. In a project-scoped file it is the entire point, and a bare
    `serve` is the defect: this is the one place with enough information to bind
    explicitly, and declining to use it falls back to cwd resolution.
    """
    if not ctx.is_project_scoped:
        return None

    args = ctx.args if isinstance(ctx.args, list) else []
    if "-p" not in args:
        return {**base, "state": "project_unbound",
                "label": "\u26a0 project entry is unbound",
                "issue": ("This project's .mcp.json runs `serve` without `-p`, so "
                          "it falls back to cwd resolution instead of binding "
                          "explicitly. Click Apply to bind it to this project.")}
    try:
        target = args[args.index("-p") + 1]
    except (IndexError, ValueError):
        target = ""

    # The template form is correct by construction: Claude Code spawns the
    # server at the project root, so "." IS this project.
    if target == PROJECT_PATH_ARG:
        return {**base, "state": "ok", "label": "\u2713 bound to this project",
                "issue": ""}
    if _same_project(target, ctx.project_root):
        return {**base, "state": "project_absolute",
                "label": "\u26a0 bound by absolute path",
                "issue": (f"Bound to the right project, but as \"{target}\" — a "
                          "path that only exists on this machine. A .mcp.json is "
                          "shared through version control; Apply rewrites it to "
                          "the portable form.")}
    return {**base, "state": "project_mismatch",
            "label": "\u26a0 bound to a DIFFERENT project",
            "issue": (f"This file binds tokensave to \"{target}\", which is not "
                      "this project. Every query here would be answered from "
                      "another codebase and look completely normal. Apply to "
                      "rebind.")}


def _chk_direct_serve(ctx: "_McpCtx", base: dict) -> dict | None:
    if not ctx.cmd_lower.endswith("tokensave.exe"):
        return None
    has_p = isinstance(ctx.args, list) and "-p" in ctx.args
    if has_p:
        try:
            target = ctx.args[ctx.args.index("-p") + 1]
        except (IndexError, ValueError):
            target = "(unknown)"
        return {**base, "state": "direct_serve", "label": "⚠ hardcoded project",
                "issue": (f"Runs tokensave.exe directly with -p \"{target}\". "
                          "This locks the MCP server to one project — switching "
                          "requires a config edit AND a Claude restart. Click Apply "
                          "to route through the wrapper or run `tokensave install "
                          "--agent claude` for the auto-detect default.")}
    if ctx.is_claude_code:
        return {**base, "state": "ok", "label": "✓ tokensave-install canonical", "issue": ""}
    return {**base, "state": "direct_serve", "label": "⚠ bypasses wrapper (Desktop needs wrapper)",
            "issue": ("Claude Desktop's MCP server is long-lived and must use the wrapper "
                      "to honour ★ Set as Active. Tokensave's auto-detect runs only at process "
                      "startup, so direct-serve here means project switches need a Claude "
                      "Desktop restart. Click Apply to route through the wrapper.")}


# Project scope runs FIRST: inside a `.mcp.json` its verdicts replace the
# global ones entirely, and it matches on any command shape (the binary may
# be named `tokensave` off PATH rather than `tokensave.exe`).
_MCP_CMD_CHECKERS = [_chk_project_scoped, _chk_bundled_wrapper,
                     _chk_python_wrapper, _chk_direct_serve]


def _classify_mcp_entry(cfg_path: str, cfg: dict) -> dict:
    """Inspect a Claude MCP config file and report what shape its tokensave
    entry is in.

    Returns a dict with keys:
      - "state":  one of "ok", "direct_serve", "wrong_wrapper", "missing",
                  "no_file", "unparseable"
      - "label":  short human-readable status (✓ / ⚠ / ✗ prefixed)
      - "issue":  longer explanation suitable for a tooltip / dialog body
      - "current": the current tokensave entry dict (if any) — for diff display
      - "proposed": the canonical entry the manager wants to write
      - "cfg_path": echoes the input, for callers that thread through many configs

    Two valid shapes are recognised as "ok":
      (a) Wrapper-routed:  pythonw[w].exe + tokensave-wrapper.py  (or the
          bundled tokensave-wrapper.exe).  Manager-blessed.  Required for
          Claude Desktop because Desktop spawns the MCP server once at
          startup and the wrapper is what reads ~/.tokensave/desktop-
          project.txt to pick the right project.
      (b) tokensave install canonical:  tokensave.exe serve  with NO
          hardcoded "-p" flag.  This is what `tokensave install --agent
          claude` writes to ~/.claude.json, and `tokensave doctor`
          considers it the correct shape.  Standalone Claude Code
          sessions then let tokensave auto-detect the project per
          invocation.  We ONLY accept this shape for Claude Code
          (cfg_path ending in .claude.json) — Desktop must use (a)
          because its MCP server is long-lived and can't re-auto-detect.

    Direct-serve WITH a hardcoded "-p" is always flagged — that's the
    KicomAI-style footgun where every Claude restart needs to know the
    project at install time.

    Pure function — no side effects. Safe to call on every startup.
    """
    # Scope is derived from the path, so no caller had to change. A
    # `.mcp.json` counts as project-scoped only when its directory actually
    # holds a tokensave index -- the filename alone would let a stray
    # `somewhere/.mcp.json` be judged by project rules it has nothing to do
    # with.
    project_root = ""
    if os.path.basename(cfg_path).lower() == ".mcp.json":
        candidate = os.path.dirname(cfg_path)
        if os.path.isdir(os.path.join(candidate, ".tokensave")):
            project_root = candidate
    is_project_scoped = bool(project_root)

    proposed = (_canonical_project_entry(cfg) if is_project_scoped
                else _canonical_mcp_entry(cfg))
    base = {"cfg_path": cfg_path, "current": None, "proposed": proposed}
    is_claude_code = cfg_path.lower().endswith(".claude.json")

    if not cfg_path or not os.path.isfile(cfg_path):
        return {**base, "state": "no_file", "label": "✗ no config file",
                "issue": (f"{cfg_path} doesn't exist yet. "
                          "If you use this Claude app, the manager can create "
                          "the file with just a tokensave entry.")}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {**base, "state": "unparseable", "label": "✗ unreadable",
                "issue": (f"Could not parse {cfg_path}: {type(e).__name__}: {e}. "
                          "Fix the JSON by hand before re-running the configurator.")}

    servers = data.get("mcpServers") or {}
    entry = servers.get("tokensave")
    if not isinstance(entry, dict):
        return {**base, "state": "missing", "label": "✗ no tokensave entry",
                "issue": ("No 'tokensave' MCP server is configured. "
                          "Click Apply to add the canonical wrapper-based "
                          "entry — other mcpServers entries (if any) stay untouched.")}

    base["current"] = entry
    cmd = (entry.get("command") or "").strip()
    args = entry.get("args") or []
    ctx = _McpCtx(cmd=cmd, cmd_lower=cmd.lower().replace("/", os.sep),
                  args=args, is_claude_code=is_claude_code,
                  is_project_scoped=is_project_scoped, project_root=project_root)

    for checker in _MCP_CMD_CHECKERS:
        result = checker(ctx, base)
        if result is not None:
            return result

    return {**base, "state": "wrong_wrapper", "label": "⚠ non-canonical",
            "issue": (f"command is {cmd!r}, which isn't a shape the manager "
                      "knows how to maintain. Click Apply to replace with "
                      "the canonical wrapper-based entry.")}


def _apply_mcp_fix(cfg_path: str, proposed_entry: dict) -> tuple[bool, str]:
    """Write `proposed_entry` into `cfg_path` under mcpServers.tokensave.

    Returns (success, message). Always writes a timestamped backup first
    via shutil.copy2 (skipped if the source file doesn't exist — in that
    case the function creates a fresh config with only tokensave in it).
    Other mcpServers entries are preserved verbatim.

    Idempotent: applying twice in a row is a no-op-equivalent (the second
    write writes the same bytes the first one did).
    """
    backup_msg = ""
    if os.path.isfile(cfg_path):
        try:
            backup = cfg_path + ".backup." + str(int(time.time() * 1000))
            shutil.copy2(cfg_path, backup)
            backup_msg = f" (backup: {os.path.basename(backup)})"
        except OSError as e:
            return False, f"Could not write backup: {e}"
        try:
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return False, f"Could not parse existing file: {e}"
    else:
        # Fresh file — make sure the parent dir exists.
        try:
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        except OSError as e:
            return False, f"Could not create parent dir: {e}"
        data = {}

    servers = data.setdefault("mcpServers", {})
    servers["tokensave"] = proposed_entry

    ok, err = _write_json_atomic(cfg_path, data)
    if not ok:
        return False, err
    return True, f"Wrote tokensave entry to {cfg_path}{backup_msg}"


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


def remove_mcp_entry(cfg_path: str, server: str = "tokensave") -> "tuple[bool, str]":
    """Delete one server from a Claude MCP config. Returns (changed, detail).

    Used by the migration off the user-scoped `tokensave`, which cannot be a
    side effect of binding a project: Claude Code dedupes by server name, so
    once projects are bound the user-scoped definition is what shadows them —
    but removing it also takes tokensave away from every project that is NOT
    bound. That is a decision, not a cleanup step.

    Refuses rather than guesses: an unparseable file is left alone, and an
    already-absent entry is reported as no change rather than as success.
    """
    if not cfg_path or not os.path.isfile(cfg_path):
        return False, f"No such config file: {cfg_path}"

    try:
        backup = cfg_path + ".backup." + str(int(time.time() * 1000))
        shutil.copy2(cfg_path, backup)
    except OSError as exc:
        return False, f"Could not write backup: {exc}"

    try:
        with open(cfg_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Could not parse existing file: {exc}"
    if not isinstance(data, dict):
        return False, "Config root is not a JSON object"

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server not in servers:
        return False, f"No '{server}' entry in {cfg_path} — nothing to remove."

    removed = servers.pop(server)
    ok, err = _write_json_atomic(cfg_path, data)
    if not ok:
        return False, err
    return True, ("Removed '%s' from %s (backup: %s)\n\nRemoved entry:\n%s"
                  % (server, cfg_path, os.path.basename(backup),
                     json.dumps(removed, indent=2)))


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


def effective_scope(project_root: str, server: str = "tokensave",
                    timeout: int = 45) -> "EffectiveScope":
    """Ask Claude Code which `server` definition wins inside `project_root`.

    Run with cwd set to the project, because the answer is per-directory. Any
    failure — no `claude` on PATH, a timeout, unexpected output — comes back as
    UNKNOWN rather than a guess: reporting "shadowed" because a CLI call fell
    over would send the user hunting for a conflict that does not exist.
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
            capture_output=True, text=True, timeout=timeout, cwd=project_root,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EffectiveScope(SCOPE_UNKNOWN, detail=str(exc)[:200])
    return _parse_mcp_get((proc.stdout or "") + "\n" + (proc.stderr or ""))


def _is_claude_running() -> dict:
    """Detect running Claude Desktop / Claude Code processes.

    Returns a dict: {"desktop": bool, "code": bool, "pids": [int, ...]}.

    Why this matters: Claude Desktop periodically rewrites
    `claude_desktop_config.json` with its in-memory state, including its
    cached copy of `mcpServers`. Any edit we make while Desktop is running
    is silently clobbered within ~1-2 minutes. The configurator dialog
    refuses to apply a fix to a config whose owning app is currently
    running — the user gets a clear "quit Claude Desktop, then retry"
    message instead of a fix that mysteriously reverts itself.

    Best-effort: uses tasklist on Windows. Empty/false results don't
    block the apply — the warning is advisory, not enforced.
    """
    result = {"desktop": False, "code": False, "pids": []}
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
        elif name in ("claude-code.exe",):  # currently bundled inside claude.exe; future-proof
            result["code"] = True
            result["pids"].append(pid)
    return result


# ── v4.7: CodeGraph MCP wiring detection ────────────────────────────────────

def _claude_code_mcp_has_codegraph(claude_json_path: str = "") -> tuple[bool, str]:
    """Return ``(wired, server_key_used)`` from Claude Code's MCP config.

    Reads ``~/.claude.json`` (or ``claude_json_path`` if provided), walks
    ``mcpServers``. Returns ``(True, key)`` when any entry meets any of:
      * dict key contains "codegraph" (case-insensitive); OR
      * entry["command"] contains "codegraph"; OR
      * any entry["args"] element contains "codegraph"

    Defensive matching covers the canonical shape (verified via
    ``codegraph install --print-config claude``)::

        "codegraph": {"type": "stdio", "command": "codegraph",
                       "args": ["serve", "--mcp"]}

    AND user-written variants (full binary path, namespaced key, etc.).

    Fail-open: any IO/JSON error or missing file returns ``(False, "")``.
    The Settings status row uses this to render the green ✓ / red ✗
    indicator beside the codegraph binary status.

    Codegraph does NOT use a wrapper like tokensave does — the MCP entry
    invokes ``codegraph serve --mcp`` directly via PATH, so this helper
    is much simpler than ``_classify_mcp_entry`` above (no UWP / wrapper
    resolution needed).
    """
    if not claude_json_path:
        claude_json_path = os.path.expanduser("~/.claude.json")
    if not os.path.isfile(claude_json_path):
        return False, ""
    try:
        with open(claude_json_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False, ""
    if not isinstance(cfg, dict):
        return False, ""
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        return False, ""
    for key, entry in servers.items():
        if not isinstance(key, str):
            continue
        if "codegraph" in key.lower():
            return True, key
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command")
        if isinstance(cmd, str) and "codegraph" in cmd.lower():
            return True, key
        args = entry.get("args")
        if isinstance(args, list):
            for a in args:
                if isinstance(a, str) and "codegraph" in a.lower():
                    return True, key
    return False, ""


# Codegraph install-target IDs (verified from `codegraph install --help`).
# Web docs incorrectly listed 8 agents; the binary supports only these 4.
_CODEGRAPH_AGENTS: tuple = (
    ("claude",   "Claude Code"),
    ("cursor",   "Cursor"),
    ("codex",    "Codex CLI"),
    ("opencode", "opencode"),
)


def _codegraph_agent_destination_path(agent_id: str) -> str:
    """Return the absolute path codegraph would write to for an agent.

    Verified via ``codegraph install --print-config <id>``:
      claude    → ~/.claude.json
      cursor    → ~/.cursor/mcp.json
      codex     → ~/.codex/config.toml
      opencode  → ~/AppData/Roaming/opencode/opencode.jsonc  (Windows)
                  ~/.config/opencode/opencode.jsonc          (XDG fallback)

    Returns empty string for unknown agents.  The Settings picker uses
    this to determine "is this agent installed?" via parent-directory
    existence — fast, no subprocess.
    """
    home = os.path.expanduser("~")
    if agent_id == "claude":
        return os.path.join(home, ".claude.json")
    if agent_id == "cursor":
        return os.path.join(home, ".cursor", "mcp.json")
    if agent_id == "codex":
        return os.path.join(home, ".codex", "config.toml")
    if agent_id == "opencode":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "opencode", "opencode.jsonc")
        return os.path.join(home, ".config", "opencode", "opencode.jsonc")
    return ""


def _codegraph_agent_installed(agent_id: str) -> bool:
    """Return True if the agent's config dir / file exists on this machine.

    Used by the MCP picker to grey-out checkboxes for agents the user
    doesn't actually have. Checks the destination path's existence
    (file OR parent dir for the file-write case).
    """
    path = _codegraph_agent_destination_path(agent_id)
    if not path:
        return False
    # Direct file or its parent directory — either indicates the agent
    # has been used on this machine (Claude Code creates ~/.claude.json
    # on first launch; the others create their config directories).
    if os.path.exists(path):
        return True
    parent = os.path.dirname(path)
    return os.path.isdir(parent) if parent else False


# ── tokensave agent integration ────────────────────────────────────────────────

# tokensave install --agent IDs, verified live from `tokensave install --help`
# (v7.8.1).  Unlike codegraph's `--target=<csv>`, tokensave accepts exactly ONE
# --agent per invocation — callers must loop.  Order here is the order the
# picker renders in; "claude" first because it's the common case.
_TOKENSAVE_AGENTS: tuple = (
    ("claude",      "Claude Code"),
    ("copilot",     "GitHub Copilot"),
    ("cursor",      "Cursor"),
    ("codex",       "Codex CLI"),
    ("gemini",      "Gemini CLI"),
    ("qwen",        "Qwen Code"),
    ("opencode",    "OpenCode"),
    ("droid",       "Factory Droid"),
    ("zed",         "Zed"),
    ("cline",       "Cline"),
    ("roo-code",    "Roo Code"),
    ("antigravity", "Antigravity"),
    ("kilo",        "Kilo CLI"),
    ("kiro",        "Kiro"),
    ("kimi",        "Kimi CLI"),
    ("vibe",        "Mistral Vibe"),
    ("grok",        "Grok Build"),
    ("pi",          "Pi"),
    ("plank",       "Plank"),
    ("auggie",      "AugmentCode"),
)

# Config-path recipes per agent: ((base, *segments), ...).
#
# `base` is "home" (→ os.path.expanduser("~")) or "appdata" (→ %APPDATA%).
# The first recipe is canonical (what we display); later ones are alternates
# consulted only for detection — e.g. Copilot registers in both VS Code's
# mcp.json and ~/.copilot/mcp-config.json.
#
# Paths transcribed from `tokensave doctor`'s own output, which prints the
# destination it checks for every integration — authoritative, and cheaper
# than shelling out per agent.
#
# Kept as a table rather than an if-chain: 20 branches would blow the
# cyclomatic-complexity cap in BASIC_INSTRUCTIONS Rule A.
_TOKENSAVE_AGENT_PATHS: dict = {
    "claude":      ((["home"], ".claude.json"),),
    "copilot":     ((["appdata"], "Code", "User", "mcp.json"),
                    (["home"], ".copilot", "mcp-config.json")),
    "cursor":      ((["home"], ".cursor", "mcp.json"),),
    "codex":       ((["home"], ".codex", "config.toml"),),
    "gemini":      ((["home"], ".gemini", "settings.json"),),
    "qwen":        ((["home"], ".qwen", "settings.json"),),
    "opencode":    ((["home"], ".config", "opencode", "opencode.json"),),
    "droid":       ((["home"], ".factory", "mcp.json"),),
    "zed":         ((["home"], ".config", "zed", "settings.json"),),
    "cline":       ((["appdata"], "Code", "User", "globalStorage",
                     "saoudrizwan.claude-dev", "settings",
                     "cline_mcp_settings.json"),),
    "roo-code":    ((["appdata"], "Code", "User", "globalStorage",
                     "rooveterinaryinc.roo-cline", "settings",
                     "cline_mcp_settings.json"),),
    "antigravity": ((["home"], ".gemini", "antigravity", "mcp_config.json"),),
    "kilo":        ((["home"], ".config", "kilo", "kilo.jsonc"),),
    "kiro":        ((["home"], ".kiro", "settings", "mcp.json"),),
    "kimi":        ((["home"], ".kimi", "mcp.json"),),
    "vibe":        ((["home"], ".vibe", "config.toml"),),
    "grok":        ((["home"], ".grok", "config.toml"),),
    "pi":          ((["home"], ".pi", "agent", "mcp.json"),),
    "plank":       ((["home"], ".plank", ".mcp.json"),),
    "auggie":      ((["home"], ".augment", "settings.json"),),
}


def _tokensave_agent_path_candidates(agent_id: str) -> list:
    """All config paths tokensave might use for an agent, canonical first.

    Roots are resolved *inside* the call, never at module scope, so the
    ``fake_home`` fixture's $USERPROFILE / $APPDATA redirects apply — see
    ``tests/test_no_import_time_path_resolution.py`` (G-L).
    """
    recipes = _TOKENSAVE_AGENT_PATHS.get(agent_id) or ()
    out: list = []
    for base, *segments in recipes:
        root = (os.environ.get("APPDATA", "") if base[0] == "appdata"
                else os.path.expanduser("~"))
        if root:
            out.append(os.path.join(root, *segments))
    return out


def _tokensave_agent_destination_path(agent_id: str) -> str:
    """Config path tokensave would write to for an agent ('' if unknown).

    Prefers whichever candidate already exists so the picker shows the file
    actually in play (Copilot has two); otherwise falls back to canonical.
    """
    candidates = _tokensave_agent_path_candidates(agent_id)
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0] if candidates else ""


def _tokensave_agent_installed(agent_id: str) -> bool:
    """Return True if the agent appears to be present on this machine.

    A config *directory* counts as present (the agent has run at least once
    and will pick up an MCP entry we add).  Deliberately stricter than
    ``_codegraph_agent_installed`` for configs that live directly in the home
    directory: ``~`` always exists, so parent-dir existence there would report
    every such agent as installed.  Those require the file itself.
    """
    home = os.path.expanduser("~")
    for path in _tokensave_agent_path_candidates(agent_id):
        if os.path.exists(path):
            return True
        parent = os.path.dirname(path)
        if parent and os.path.normcase(parent) != os.path.normcase(home):
            if os.path.isdir(parent):
                return True
    return False


def _tokensave_agent_wired(agent_id: str) -> bool:
    """Return True if the agent's config ALREADY references tokensave.

    "Installed" and "wired" are different questions, and conflating them
    produces a nag that can never be satisfied. Several agents ship multiple
    surfaces under one ``--agent`` id — Copilot covers VS Code, VS Code
    Insiders, the CLI, and JetBrains — and ``tokensave doctor`` emits a
    ``run `tokensave install --agent copilot``` line for EACH surface it
    can't find, including ones the user doesn't have installed. Filtering on
    "is this agent installed?" alone therefore keeps offering to wire an
    agent that is already fully wired everywhere it exists, on every single
    doctor run.

    Substring match on the raw file rather than per-format parsing: these
    configs are JSON, JSONC (comments break ``json.load``) and TOML, and the
    question is only ever "does tokensave appear here at all". Erring toward
    "wired" is the safe direction — the cost is a missed prompt the user can
    still reach from Tool Manager, versus a prompt that reappears forever.
    """
    for path in _tokensave_agent_path_candidates(agent_id):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if "tokensave" in fh.read().lower():
                    return True
        except OSError:
            continue
    return False
