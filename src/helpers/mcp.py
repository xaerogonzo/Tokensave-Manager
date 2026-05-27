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


@dataclasses.dataclass
class _McpCtx:
    cmd: str
    cmd_lower: str
    args: list
    is_claude_code: bool


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


_MCP_CMD_CHECKERS = [_chk_bundled_wrapper, _chk_python_wrapper, _chk_direct_serve]


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
    proposed = _canonical_mcp_entry(cfg)
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
                  args=args, is_claude_code=is_claude_code)

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

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except OSError as e:
        return False, f"Could not write config: {e}"
    return True, f"Wrote tokensave entry to {cfg_path}{backup_msg}"


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
