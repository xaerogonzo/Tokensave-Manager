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


_MCP_DESKTOP_CFG_PATH = _resolve_desktop_cfg_path()
_MCP_CODE_CFG_PATH    = os.path.join(
    os.environ.get("USERPROFILE", ""), ".claude.json")

# Friendly labels — kept short so they fit in the dialog headers.
_MCP_CONFIGS = [
    ("Claude Desktop", _MCP_DESKTOP_CFG_PATH),
    ("Claude Code",    _MCP_CODE_CFG_PATH),
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
