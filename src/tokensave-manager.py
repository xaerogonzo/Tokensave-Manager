"""
TokenSave Manager
A GUI for managing tokensave projects and controlling which project
Claude Desktop uses via the wrapper script.
"""

import os
import re
import json
import glob
import shlex
import shutil
import subprocess
import threading
import queue
import time
import logging
import logging.handlers
import ctypes
import sys
import dataclasses
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont
import math
import tempfile
import textwrap
import pystray
from PIL import Image, ImageDraw

_ANSI = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])')

def _version_lt(a: str, b: str) -> bool:
    """Return True if version string `a` is strictly less than `b`.

    Compares dotted numeric versions tuple-wise after coercing missing
    components to 0. Non-numeric tags (alpha/beta/rc) are not handled —
    pure semver-style "1.2.3" comparisons only, which is what tokensave
    uses. Falls back to string compare on parse failure.
    """
    def _parts(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, AttributeError):
            return None
    pa, pb = _parts(a), _parts(b)
    if pa is None or pb is None:
        return a < b
    # Pad to equal length for fair tuple compare.
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return pa < pb


# tokensave emits this line at the end of any sync when a newer release is
# available on GitHub. Capture both versions so the manager can display
# "Upgrade v5.1.1 → v5.1.2" in Settings and decide when the button should
# show up. Accepts an arrow rendered as either Unicode → or ASCII -> /=>,
# and tolerates either the bare "5.1.2" or "v5.1.2" form.
_TOKENSAVE_UPDATE_RE = re.compile(
    r'Update available:\s*v?(\d+\.\d+\.\d+(?:\.\d+)?)\s*'
    r'(?:→|->|=>)\s*v?(\d+\.\d+\.\d+(?:\.\d+)?)')

# ── Paths ─────────────────────────────────────────────────────────────────────
# Under Nuitka --onefile, NUITKA_ONEFILE_PARENT is the actual .exe path.
# In dev mode, the script lives in src/ so go up one level.

if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_CONFIG_PATH = os.path.join(_BASE_DIR, "manager-config.json")
LOG_DIR      = os.path.join(_BASE_DIR, "logs")
LOG_FILE     = os.path.join(LOG_DIR, "manager.log")

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM if present
        return json.load(f)

def _save_config(cfg: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _migrate_config(cfg: dict) -> dict:
    """Bump old config defaults to current ones, on first load after upgrade.

    Why this exists: tk Settings dialog uses ``setdefault`` which only fills
    MISSING keys. Users upgrading from v1.0.x have ``timeout_seconds: 12``
    AND ``max_diff_chars: 8000`` already present in their saved config —
    those keys exist, so setdefault no-ops, and the user keeps the old slow
    defaults until they edit the JSON by hand. This migration treats values
    AT-OR-BELOW the previous default as 'never explicitly chosen by the user'
    and bumps them to the current default. Users who deliberately set
    intermediate values (e.g. timeout=60, max_diff_chars=16000) keep those.

    Migration is idempotent: re-running on already-migrated configs is a no-op.
    Saves back to disk only if anything changed.
    """
    changed = False
    llm = cfg.get("commit_message_llm")
    if isinstance(llm, dict):
        # timeout_seconds: old default was 12, current is 90; bump anything <30
        if int(llm.get("timeout_seconds", 90)) < 30:
            llm["timeout_seconds"] = 90
            changed = True
        # max_diff_chars: old default was 8000, current is 24000; bump anything <16000
        if int(llm.get("max_diff_chars", 24000)) < 16000:
            llm["max_diff_chars"] = 24000
            changed = True
    # MCP-config skip list — added [Unreleased]. Empty list means "warn me
    # whenever any Claude MCP config drifts from the canonical wrapper-based
    # shape". Each entry is an absolute path to a config file the user has
    # told us to stop warning about.
    if "mcp_skip_warnings" not in cfg:
        cfg["mcp_skip_warnings"] = []
        changed = True
    if changed:
        _save_config(cfg)
    return cfg


# ───────────────────────────────────────────────────────────────────────
# MCP-config introspection + canonical-shape helpers
# ───────────────────────────────────────────────────────────────────────
#
# Two Claude apps each have their own MCP config; both need to point at
# tokensave-wrapper.py for the manager's ★ Set as Active pin to drive which
# project gets served. Without the wrapper, an MCP entry that runs
# `tokensave.exe serve -p <hardcoded>` bypasses the pin file entirely —
# which is the exact failure mode that produced this whole subsystem.
#
# Pure helpers, no Tk. Easy to unit-test from the command line.

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
    it's the .py next to tokensave-manager.py. Matches the same lookup the
    Reference tab already does (~ line 5275).
    """
    if os.environ.get("NUITKA_ONEFILE_PARENT"):
        return os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
    return os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")


def _canonical_mcp_entry() -> dict:
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
    py = (_cfg.get("python_exe") if isinstance(_cfg, dict) else "") or ""
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


def _classify_mcp_entry(cfg_path: str) -> dict:
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
    proposed = _canonical_mcp_entry()
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


_cfg = _migrate_config(_load_config())

TOKENSAVE    = _cfg.get("tokensave_exe", "")
TEMPLATE_DIR = _cfg.get("template_dir", "") or os.path.join(_BASE_DIR, "templates")
SEARCH_ROOTS = _cfg.get("search_roots", [])


def _detect_git() -> str:
    """Return the best available path to git.exe.

    Priority:
      1. Explicit path in manager-config.json  (caller checks that first)
      2. shutil.which("git")  — works if Git is on PATH
      3. Common Git-for-Windows install locations
      4. Bare "git" fallback (will fail with a clear error if not found)
    """
    found = shutil.which("git")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "git"


def _detect_gh() -> str:
    """Return the path to gh.exe (GitHub CLI) if installed, else empty string.

    Checks PATH first, then common winget/scoop install locations.
    Returns "" when not found so callers can easily test with `if _detect_gh()`.
    """
    found = shutil.which("gh")
    if found:
        return found
    for candidate in [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\gh.exe"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_npm() -> str:
    """Return the path to npm, else empty string.

    On Windows npm is a `.cmd` shim, not a `.exe` — `subprocess.run` with
    a bare `.cmd` raises FileNotFoundError unless the absolute path
    (including the .cmd extension) is supplied. So we probe `.cmd` first.
    """
    for name in ("npm.cmd", "npm"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\npm.cmd"),
        os.path.expandvars(r"%ProgramFiles%\nodejs\npm.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _detect_codegraph() -> str:
    """Return the path to the codegraph CLI, else empty string.

    Same Windows-.cmd-first priority as _detect_npm because codegraph is
    installed by npm as a .cmd shim. Returns "" (not the bare command
    name) so callers can test `if CODEGRAPH_EXE:` cleanly without
    accidentally invoking a bare command via subprocess.
    """
    for name in ("codegraph.cmd", "codegraph"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in [
        os.path.expandvars(r"%APPDATA%\npm\codegraph.cmd"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\codegraph.cmd"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _is_codegraph_project(path: str) -> bool:
    """True iff `path` has been initialised by CodeGraph (the .codegraph/
    SQLite database exists)."""
    return os.path.isfile(os.path.join(path, ".codegraph", "codegraph.db"))


# Resolved at startup; updated whenever Settings are saved.
# All git subprocess calls use this variable so the user only has to
# configure the path in one place.
GIT_EXE: str = _cfg.get("git_exe") or _detect_git()

# Resolved at startup; rebuilt in _on_settings_saved. Empty string when not
# installed (codegraph is optional, distributed via npm — not bundled).
CODEGRAPH_EXE: str = _cfg.get("codegraph_exe") or _detect_codegraph()


# ── Search-root helpers (support both legacy str and new {"path":…,"label":…} format) ──

def _root_path(r):
    """Return the directory path from a search-root entry (str or dict)."""
    return r if isinstance(r, str) else r["path"]

def _root_label(r):
    """Return the display label for a search-root entry."""
    p = _root_path(r)
    if isinstance(r, str):
        return os.path.basename(p.rstrip("/\\"))
    return r.get("label", os.path.basename(p.rstrip("/\\"))) or os.path.basename(p.rstrip("/\\"))

BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"

DESKTOP_PROJECT_FILE = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".tokensave", "desktop-project.txt",
)
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "target", "build", "dist", "out", ".gradle", "bin", "obj",
}
MAX_DEPTH = 4
CREATE_NO_WINDOW = 0x08000000
AUTO_REFRESH_MS = 60_000  # auto-refresh project list every 60 s

# Git network operations — prevents infinite hang when credentials aren't cached.
# GIT_TERMINAL_PROMPT=0 tells git to fail immediately instead of waiting for
# stdin. Compatible with Git Credential Manager (GCM authenticates via browser,
# not stdin, so this env var doesn't interfere with it).
_GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class _Tooltip:
    """Hover tooltip for tkinter widgets.

    Shows a small popup after `delay` ms; hides on leave or click.
    Text wraps at ~300 px so multi-line tips stay readable.
    """
    _DELAY = 650   # ms before appearing

    def __init__(self, widget, text: str):
        self._widget  = widget
        self._text    = text
        self._job     = None
        self._tip_win = None
        widget.bind("<Enter>",       self._schedule,  add="+")
        widget.bind("<Leave>",       self._cancel,    add="+")
        widget.bind("<ButtonPress>", self._cancel,    add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self._widget.after(self._DELAY, self._show)

    def _cancel(self, _event=None):
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None

    def _show(self):
        self._job = None
        try:
            x = self._widget.winfo_rootx() + 12
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        except Exception:
            return
        self._tip_win = win = tk.Toplevel(self._widget)
        win.wm_overrideredirect(True)   # no title bar / border
        win.wm_attributes("-topmost", True)
        win.wm_geometry(f"+{x}+{y}")
        win.configure(bg=C["surface1"])
        # Thin border effect via a 1-px frame
        outer = tk.Frame(win, bg=C["overlay0"], padx=1, pady=1)
        outer.pack()
        tk.Label(outer, text=self._text,
                 font=("Segoe UI", 9),
                 bg=C["surface1"], fg=C["text"],
                 padx=10, pady=6,
                 wraplength=300,
                 justify=tk.LEFT).pack()


def _is_auth_error(text: str) -> bool:
    """Return True if git output looks like an authentication failure."""
    t = text.lower()
    return any(s in t for s in (
        "authentication failed",
        "could not read username",
        "permission denied",
        "fatal: authentication",
        "remote: repository not found",
        "invalid username or password",
    ))

def _parse_commit_status(status_text: str) -> "tuple[list[tuple[str,str]], bool]":
    """Parse `git status --short` output into (files, has_source) for the commit strategies."""
    _SOURCE_EXTS = {".py", ".js", ".ts", ".cs", ".cpp", ".c", ".h", ".rs",
                    ".go", ".java", ".rb", ".php", ".swift", ".kt", ".scala"}
    files = []
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        xy    = line[:2].strip()
        fname = line[3:]
        if " -> " in fname:
            fname = fname.split(" -> ")[-1]
        files.append((xy, fname))
    has_source = any(
        os.path.splitext(os.path.basename(f))[1].lower() in _SOURCE_EXTS
        for _xy, f in files
    )
    return files, has_source


def _strat_llm(repo_path: str, has_source: bool) -> "tuple[str, str] | None":
    """Strategy 0: LLM (opt-in). Returns (subject, body) or None."""
    if not repo_path:
        return None
    llm_cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
    if not llm_cfg.get("enabled"):
        return None
    raw = _call_llm_for_commit_message(llm_cfg, repo_path)
    if not raw:
        return None
    head, _, tail = raw.partition("\n\n")
    return head, tail


def _strat_changelog(repo_path: str, files: list) -> "tuple[str, str] | None":
    """Strategy 1: CHANGELOG bullets. Returns (subject, body) or None."""
    if not repo_path:
        return None
    try:
        additions = _extract_changelog_additions(repo_path)
    except Exception:
        return None
    if not additions:
        return None
    return _message_from_changelog(additions, files)


def _strat_diff(repo_path: str, files: list) -> "tuple[str, str] | None":
    """Strategy 2: Diff content (added defs/classes, file kinds). Returns (subject, body) or None."""
    if not repo_path or not files:
        return None
    subj, body = _suggest_from_diff_content(repo_path, files)
    return (subj, body) if subj else None


def _strat_filenames(status_text: str) -> "tuple[str, str] | None":
    """Strategy 3: File-name patterns (legacy fallback). Returns (subject, body) or None."""
    result = _suggest_from_filenames(status_text)
    return (result, "") if result else None


def _suggest_commit_message(repo_path: str = "", status_text: str = "") -> str:
    """Generate a conventional-commit-style message for the staged changes.

    Multi-strategy orchestrator — tries the highest-quality strategy first
    and falls through to weaker ones on empty results. Returns "" only if
    every strategy yields nothing AND there are no staged files at all.

    `repo_path` is optional for backwards compatibility — if empty, only
    the file-name strategy runs (it doesn't need shell access).
    """
    files, has_source = _parse_commit_status(status_text)

    strategies = [
        lambda: _strat_llm(repo_path, has_source),
        lambda: _strat_changelog(repo_path, files),
        lambda: _strat_diff(repo_path, files),
        lambda: _strat_filenames(status_text),
    ]
    for strategy in strategies:
        try:
            result = strategy()
        except Exception:
            continue
        if result is None:
            continue
        subj, body = _sanitize_commit_message(
            result[0], result[1], has_source_changes=has_source)
        if subj:
            return subj + (("\n\n" + body) if body else "")

    # Generic backstop — reached only when every strategy was empty or
    # produced a filename-listing that sanitization rejected.
    if files:
        if has_source:
            scope = _dominant_directory(files)
            return f"refactor({scope}): update sources" if scope else "refactor: update sources"
        return "chore: update files"
    return ""


# ── Shadow-link helpers ────────────────────────────────────────────────────────

# Default extension map: ZScript → C++, ACS → C, DECORATE lump → C++.
# Keys starting with '.' are matched against the file extension
#   (e.g. ".zsc" matches "Blood.zsc" → shadow "Blood.zsc.cpp").
# Keys WITHOUT a leading dot are matched by exact filename, case-insensitive
#   (e.g. "DECORATE" matches the extensionless lump → shadow "DECORATE.cpp").
DEFAULT_SHADOW_EXT_MAP = {
    ".zs":  ".cpp",
    ".zsc": ".cpp",
    ".acs": ".c",
    "DECORATE": ".cpp",   # extensionless lump — matched by exact filename
}

_SHADOW_SKIP_DIRS = {".tokensave", ".git", "node_modules", "__pycache__",
                     ".venv", "venv", "target", "build", "dist", "out"}


def generate_shadow_links(path: str, ext_map: dict) -> tuple:
    """
    Walk *path* and create NTFS hardlinks so tokensave can index
    non-standard extensions via an existing tree-sitter grammar.

    Two matching modes, determined by key format:
    - Dot-prefixed keys (".zsc") match by file extension → Blood.zsc → Blood.zsc.cpp
    - Non-dot keys ("DECORATE") match by exact filename, case-insensitive →
      DECORATE → DECORATE.cpp  (handles extensionless Doom lumps)

    Existing shadow files are left untouched.
    Returns (created, skipped, failed) counts.
    """
    created = skipped = failed = 0
    ext_keys  = {k: v for k, v in ext_map.items() if k.startswith(".")}
    name_keys = {k.upper(): v for k, v in ext_map.items() if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext in ext_keys:
                shadow_suffix = ext_keys[ext]
            elif fname.upper() in name_keys:
                shadow_suffix = name_keys[fname.upper()]
            else:
                continue
            src = os.path.join(root, fname)
            dst = src + shadow_suffix
            if os.path.exists(dst):
                skipped += 1
            else:
                try:
                    os.link(src, dst)
                    created += 1
                except OSError:
                    failed += 1
    return created, skipped, failed


def remove_shadow_links(path: str, ext_map: dict) -> int:
    """Delete all shadow hardlink files created by generate_shadow_links."""
    removed = 0
    suffixes  = set(ext_map.values())
    src_exts  = {k for k in ext_map if k.startswith(".")}
    src_names = {k.upper() for k in ext_map if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            for suf in suffixes:
                if fname.endswith(suf):
                    base = fname[:-len(suf)]
                    if (any(base.endswith(e) for e in src_exts) or
                            base.upper() in src_names):
                        try:
                            os.remove(os.path.join(root, fname))
                            removed += 1
                        except OSError:
                            pass
    return removed


def update_gitignore_for_shadows(path: str, ext_map: dict):
    """
    Append shadow-file patterns to .gitignore (if not already present).
    Creates .gitignore if it doesn't exist.
    Extension-based entries use a glob (*.zsc.cpp); exact-name entries use
    a literal filename (DECORATE.cpp) — no leading wildcard.
    """
    gi_path = os.path.join(path, ".gitignore")
    patterns = []
    for key, val in ext_map.items():
        if key.startswith("."):
            patterns.append(f"*{key}{val}")   # glob:  *.zsc.cpp
        else:
            patterns.append(f"{key}{val}")    # exact: DECORATE.cpp
    try:
        existing = open(gi_path, encoding="utf-8", errors="ignore").read() \
                   if os.path.isfile(gi_path) else ""
        to_add = [p for p in patterns if p not in existing]
        if to_add:
            header = "\n# tokensave shadow extension hardlinks\n"
            with open(gi_path, "a", encoding="utf-8") as f:
                f.write(header + "\n".join(to_add) + "\n")
    except OSError:
        pass


def _is_git_repo(path: str) -> bool:
    """Return True if *path* is inside an initialised git repository.

    NOTE: this walks UPWARD via `git rev-parse --git-dir` — so a project
    folder inside a parent git repo will also return True. For the strict
    'this folder IS a repo root' check, use _is_local_git_repo instead.
    """
    try:
        proc = subprocess.run(
            [GIT_EXE,"-C", path, "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def _find_tracked_but_ignored(path: str) -> list:
    """Return a list of paths that are TRACKED by git in `path` but ALSO
    match a pattern in `.gitignore`.

    Uses `git ls-files -ci --exclude-standard`:
      -c  show cached (tracked) files
      -i  filter to those that are ignored
      --exclude-standard  use the project's actual .gitignore rules

    Returns paths relative to the repo root, one per line, empty string
    filtered out. Returns [] if the call fails (not a repo, git missing,
    etc.) — caller can treat empty as "nothing to do".

    This is the canonical way to find the "stale tracking" problem: a
    file that was committed before being added to .gitignore. Git will
    keep tracking it until `git rm --cached <file>` is run, even though
    .gitignore implies the user no longer wants it in the repo.
    """
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path,
             "ls-files", "-ci", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _is_local_git_repo(path: str) -> bool:
    """Return True only if *path* itself is a git repo root.

    Strict local check — does NOT walk upward. Use this whenever the
    intent is 'should we treat this folder as its own version-controlled
    project?' (e.g. commit-prompt flows, .gitignore writes).

    Uses os.path.exists rather than os.path.isdir because git worktrees
    store `.git` as a flat text file pointing to the main repo's .git/.
    os.path.isdir would miss those; os.path.exists handles both.
    """
    return os.path.exists(os.path.join(path, ".git"))


def _parse_git_status_v2(text: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` output.

    Returns a dict with keys:
      dirty      — True if any working-tree or index changes exist
      ahead      — int, commits ahead of upstream (0 if no upstream)
      behind     — int, commits behind upstream
      has_remote — True if `# branch.upstream <name>` line is present

    Pure function — never raises; bad input returns the empty default.
    """
    result = {"dirty": False, "ahead": 0, "behind": 0, "has_remote": False}
    for line in text.splitlines():
        if line.startswith("# branch.upstream "):
            result["has_remote"] = True
        elif line.startswith("# branch.ab "):
            # Format: "# branch.ab +N -M"
            try:
                parts = line.split()
                # parts: ['#', 'branch.ab', '+N', '-M']
                result["ahead"]  = int(parts[2].lstrip("+"))
                result["behind"] = int(parts[3].lstrip("-"))
            except (ValueError, IndexError):
                pass
        elif line and line[0] in ("1", "2", "u", "?"):
            # Tracked-modified (1), renamed/copied (2), unmerged (u), untracked (?)
            result["dirty"] = True
    return result


def _format_git_status_cell(status: dict | None, has_git: bool) -> tuple:
    """Return (display_text, tag_name) for the Git column on the Projects tab.

    status: dict from _parse_git_status_v2, or None if not yet computed
    has_git: True if the project has a .git/ directory at all

    Tags map to colours in _build_projects_tab via tree.tag_configure.
    """
    if not has_git:
        return ("—", "git_none")
    if status is None:
        return ("…", "git_pending")
    if not status["has_remote"]:
        # Repo exists but no remote — can't be ahead/behind
        if status["dirty"]:
            return ("●", "git_dirty")
        return ("✓", "git_clean")
    dirty  = status["dirty"]
    ahead  = status["ahead"]
    behind = status["behind"]
    if not dirty and ahead == 0 and behind == 0:
        return ("✓", "git_clean")
    parts = []
    if dirty:
        parts.append("●")
    if ahead:
        parts.append(f"↑{ahead}")
    if behind:
        parts.append(f"↓{behind}")
    text = "".join(parts)
    # Tag priority: mixed (dirty + remote drift) > behind > ahead > dirty
    if dirty and (ahead or behind):
        tag = "git_mixed"
    elif behind:
        tag = "git_behind"
    elif ahead:
        tag = "git_ahead"
    else:
        tag = "git_dirty"
    return (text, tag)


# Baseline .gitignore written by cmd_git_init when none exists yet.
_BASELINE_GITIGNORE = """\
# Machine-specific config (if your project uses one)
*.local.json

# Claude Code local session settings
.claude/

# tokensave index (machine-specific binary database)
.tokensave/

# CodeGraph SQLite index — machine-specific binary database.
# IMPORTANT: do NOT blanket-ignore .codegraph/ — CodeGraph deliberately
# expects .codegraph/config.json to be TRACKED (per-project indexing
# configuration intended to be shared across machines). CodeGraph itself
# writes a .codegraph/.gitignore that handles binary-DB exclusion.
.codegraph/codegraph.db
.codegraph/codegraph.db-*

# Python cache
__pycache__/
*.pyc
*.pyo

# Nuitka build output
*.onefile-build/
*.build/
dist/
build/

# Virtual environments
.venv/
venv/

# Logs
logs/

# OS noise
Thumbs.db
.DS_Store
"""

# ---------------------------------------------------------------------------

def _ensure_gitignore(path: str) -> list:
    """Merge _BASELINE_GITIGNORE entries into the project's .gitignore.

    Non-destructive: reads existing content and only appends lines that are
    not already present (exact-match, ignoring blank lines and comments).
    Returns a list of human-readable result strings for logging.
    """
    gi_path = os.path.join(path, ".gitignore")

    existing_raw = ""
    if os.path.isfile(gi_path):
        try:
            with open(gi_path, encoding="utf-8", errors="replace") as f:
                existing_raw = f.read()
        except OSError as e:
            return [f"Could not read .gitignore: {e}"]

    # Build a set of non-blank, non-comment lines that are already present
    existing_lines = set()
    for ln in existing_raw.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            existing_lines.add(s)

    # Find baseline entries that are missing
    missing = []
    for ln in _BASELINE_GITIGNORE.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and s not in existing_lines:
            missing.append(ln)

    if not missing:
        return ["✔ .gitignore already contains all baseline entries — nothing to add"]

    # Append missing entries with a header comment
    addition = "\n\n# Added by TokenSave Manager (baseline entries)\n" + "\n".join(missing) + "\n"
    try:
        with open(gi_path, "a", encoding="utf-8") as f:
            f.write(addition)
    except OSError as e:
        return [f"Could not update .gitignore: {e}"]

    return [
        f"✔ .gitignore {'created' if not existing_raw else 'updated'} — "
        f"added {len(missing)} missing {'entry' if len(missing) == 1 else 'entries'}:",
        *[f"  + {ln}" for ln in missing if ln.strip()],
    ]


# ─── Gitignore management helpers (used by GitignoreDialog) ─────────────────

def _baseline_patterns() -> list:
    """Return _BASELINE_GITIGNORE as a flat list of pattern lines.

    Strips comments and blank lines so the result can be fed directly into
    _GITIGNORE_TEMPLATES as the canonical Baseline category. Keeps a single
    source of truth between cmd_git_init's auto-write and the dialog.
    """
    return [ln.strip() for ln in _BASELINE_GITIGNORE.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _read_gitignore_lines(path: str) -> list:
    """Read `<path>/.gitignore` and return its lines (no terminal newlines).

    Returns an empty list when the file doesn't exist. Uses utf-8-sig so any
    UTF-8 BOM (sometimes written by PowerShell) is silently stripped.
    """
    gi_path = os.path.join(path, ".gitignore")
    if not os.path.isfile(gi_path):
        return []
    try:
        with open(gi_path, encoding="utf-8-sig", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _write_gitignore_lines(path: str, lines: list) -> None:
    """Atomically write `lines` to `<path>/.gitignore`.

    Writes to `.gitignore.tmp` first then renames — protects against
    half-written files if the process is killed. Ensures a trailing newline.
    Raises OSError on failure; caller logs/displays.
    """
    gi_path  = os.path.join(path, ".gitignore")
    tmp_path = gi_path + ".tmp"
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, gi_path)


# Categories of gitignore patterns offered as one-click "inject" buttons in
# GitignoreDialog. The Baseline entry is derived from _BASELINE_GITIGNORE at
# module load so we never have to keep two lists in sync.
_GITIGNORE_TEMPLATES = {
    "Baseline (TokenSave standard)": _baseline_patterns(),
    "Python": [
        "__pycache__/", "*.pyc", "*.pyo", "*.egg-info/",
        ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
        ".tox/", ".coverage", "htmlcov/",
    ],
    "Node.js": [
        "node_modules/", "npm-debug.log*", "yarn-debug.log*",
        "yarn-error.log*", ".pnpm-debug.log*", ".next/", "out/",
    ],
    "Rust": [
        "target/", "**/*.rs.bk",
    ],
    "Java / JVM": [
        "*.class", "*.jar", ".gradle/", "build/", "target/",
    ],
    ".NET / Visual Studio": [
        "bin/", "obj/", "*.user", "*.suo", "*.userprefs", ".vs/",
    ],
    "VS Code": [
        ".vscode/",
    ],
    "JetBrains IDEs": [
        ".idea/", "*.iml",
    ],
    "macOS": [
        ".DS_Store", "._*", ".Spotlight-V100", ".Trashes",
    ],
    "Windows": [
        "Thumbs.db", "ehthumbs.db", "Desktop.ini",
    ],
    "Nuitka": [
        "*.build/", "*.dist/", "*.onefile-build/", "dist/",
        "nuitka-crash-report.xml",
    ],
}


# ─── Release-wizard helpers ─────────────────────────────────────────────────
# Pure functions used by ReleaseWizardDialog. Pull them out at module scope
# so they're unit-testable without instantiating any Tk widget.

# Conventional-commit subject parser. Matches the prefix (type), optional
# (scope), optional ! breaking-marker, then ': ' and the rest of the subject.
# Subject-only — never applied to body text (the body can contain unrelated
# matching strings that would cause false positives).
_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|perf|style|test|build|ci)"
    r"(?:\(([^)]+)\))?(!)?:\s*(.+)$"
)

# Map conventional prefix → section header in the changelog.
_TYPE_TO_SECTION = {
    "feat":     "Added",
    "fix":      "Fixed",
    "chore":    "Changed",
    "docs":     "Docs",
    "refactor": "Changed",
    "perf":     "Changed",
    "style":    "Changed",
    "test":     "Changed",
    "build":    "Changed",
    "ci":       "Changed",
}

# Order sections appear in the rendered notes. Breaking always wins.
_SECTION_ORDER = ["Breaking", "Added", "Fixed", "Changed", "Docs", "Other"]


def _last_release_tag(path: str) -> str | None:
    """Return the most recent annotated/lightweight tag, or None if no tags."""
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path, "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tag = proc.stdout.strip()
    return tag or None


def _commits_since(path: str, ref: str | None) -> list:
    """Return commits between ``ref`` (exclusive) and HEAD (inclusive).

    Uses a custom git-log format with three fields per commit separated by
    \\x09 (tab), and records separated by \\x1f (unit separator). This lets
    multi-line bodies coexist with the field separator without ambiguity.

    Returns list of dicts: ``{"hash": str, "subject": str, "body": str}``.
    If ``ref`` is None (no prior tag), returns ALL commits.
    """
    range_spec = f"{ref}..HEAD" if ref else "HEAD"
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path, "log", range_spec,
             "--pretty=format:%H%x09%s%x09%b%x1f"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits = []
    # Each record ends with \x1f. Split, drop empty trailing, parse fields.
    for record in proc.stdout.split("\x1f"):
        if not record.strip():
            continue
        # Strip leading newline that git inserts between records
        record = record.lstrip("\n")
        parts = record.split("\x09", 2)
        if len(parts) < 2:
            continue
        h = parts[0]
        s = parts[1] if len(parts) >= 2 else ""
        b = parts[2] if len(parts) >= 3 else ""
        commits.append({"hash": h, "subject": s, "body": b})
    return commits


def _classify_commits_for_changelog(commits: list) -> dict:
    """Group commits into changelog sections by conventional-commit prefix.

    Rules:
      * Subject parsed via ``_CONVENTIONAL_RE`` (subject-only — body is
        excluded from the regex match so unrelated body text never causes
        false positives).
      * Substring check on body for ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:``
        escalates to ``Breaking`` regardless of subject prefix.
      * The ``!`` marker in the subject also forces ``Breaking``.
      * ``auto:`` commits (from the smart auto-commit Stop hook) are skipped
        entirely — internal noise, not changelog material.
      * Unmatched subjects fall into ``Other`` so nothing silently disappears.
      * Scope (the parenthetical) is preserved in the rendered line:
        ``feat(ui): button`` → ``- (ui) button``.

    Returns dict of section → list[str], in the order of ``_SECTION_ORDER``.
    Empty sections are omitted from the returned dict.
    """
    buckets = {name: [] for name in _SECTION_ORDER}
    for c in commits:
        subject = (c.get("subject") or "").strip()
        body    = c.get("body") or ""

        if not subject:
            continue

        # Skip auto-commit noise from the Stop hook helper.
        if subject.startswith("auto:"):
            continue

        m = _CONVENTIONAL_RE.match(subject)
        breaking_body = ("BREAKING CHANGE:" in body
                         or "BREAKING-CHANGE:" in body)

        if m:
            ctype, scope, bang, desc = m.group(1), m.group(2), m.group(3), m.group(4)
            line = f"({scope}) {desc}" if scope else desc
            if bang or breaking_body:
                buckets["Breaking"].append(line)
            else:
                section = _TYPE_TO_SECTION.get(ctype, "Other")
                buckets[section].append(line)
        else:
            # Non-conventional subject. If the body still mentions a BREAKING
            # CHANGE, surface it; else dump in Other.
            if breaking_body:
                buckets["Breaking"].append(subject)
            else:
                buckets["Other"].append(subject)

    # Drop empty sections from the returned dict, preserving order.
    return {name: buckets[name] for name in _SECTION_ORDER if buckets[name]}


def _bump_version(tag: str, kind: str) -> str:
    """Return the next tag for ``kind`` ∈ {patch, minor, major, hotfix}.

    Accepts tags with or without a leading ``v``. Output preserves the ``v``
    prefix if present. Non-semver inputs fall back to a date-stamped tag.

    Prerelease tail (``-alpha.1``, ``-rc.2``) and build metadata (``+abc``)
    are stripped before parsing per semver — we only bump the MAJOR.MINOR.PATCH
    core. Without this, a tag like ``v1.0.0-alpha.1`` would fail the
    ``int()`` parse on ``"0-alpha"`` and fall back to a date-stamped tag,
    producing three identical radio values in the wizard.

    Hotfix bump produces a four-part version: ``v1.0.4`` → ``v1.0.4.1``,
    ``v1.0.4.1`` → ``v1.0.4.2``. This is intentionally not strict semver —
    it's the "small adjustment on top of an existing release without
    starting a new patch series" idiom common in enterprise / Windows
    versioning (assembly versions, NuGet 4-part). The patch / minor / major
    bumps always normalise back to three parts, so a hotfix branch
    eventually merges into a clean semver line on the next regular release.
    """
    raw  = tag.lstrip("v") if tag else ""
    core = raw.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    try:
        major  = int(parts[0])
        minor  = int(parts[1])
        patch  = int(parts[2])
        # Optional 4th segment (hotfix counter). Default 0 if absent.
        hotfix = int(parts[3]) if len(parts) >= 4 else 0
    except (ValueError, IndexError):
        # Fallback for non-semver — date-stamped tag.
        from datetime import datetime as _dt
        return _dt.now().strftime("v%Y.%m.%d")

    prefix = "v" if (tag or "").startswith("v") else ""

    if kind == "major":
        return f"{prefix}{major + 1}.0.0"
    if kind == "minor":
        return f"{prefix}{major}.{minor + 1}.0"
    if kind == "hotfix":
        # Bump or introduce the 4th segment, leaving MAJOR.MINOR.PATCH intact.
        return f"{prefix}{major}.{minor}.{patch}.{hotfix + 1}"
    # default: patch — drop any hotfix segment, bump patch
    return f"{prefix}{major}.{minor}.{patch + 1}"


def _suggest_bump_kind(commits: list) -> str:
    """Pick the appropriate bump kind based on commit content.

    Returns ``"major"`` if any commit is breaking, ``"minor"`` if any new
    feature, else ``"patch"``. Mirrors conventional-commits semantics.
    """
    any_feat = False
    for c in commits:
        subject = (c.get("subject") or "").strip()
        body    = c.get("body") or ""
        if not subject:
            continue
        m = _CONVENTIONAL_RE.match(subject)
        if m and m.group(3):    # ! marker
            return "major"
        if "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body:
            return "major"
        if m and m.group(1) == "feat":
            any_feat = True
    return "minor" if any_feat else "patch"


# ─── GitCommitDialog helpers ────────────────────────────────────────────────
# Smart commit-message generation. Pure functions co-located with the
# release-wizard helpers above because they share `_CONVENTIONAL_RE`,
# `_TYPE_TO_SECTION`, and `_SECTION_ORDER`. The public entry point
# `_suggest_commit_message` is defined far above (near the top of the file)
# for backwards-compatible discoverability — it forward-references the
# strategy helpers below.

# Inverse of `_TYPE_TO_SECTION`, used when reading a CHANGELOG bullet
# under (e.g.) `### Added` and producing a `feat:` prefix in the commit.
# Picks the SINGLE canonical type per section — `Changed` maps back to
# `refactor` by default; `_message_from_changelog` escalates to `feat` when
# the bullet text hints at user-visible UI changes.
_SECTION_TO_TYPE = {
    "Added":      "feat",
    "Fixed":      "fix",
    "Changed":    "refactor",
    "Removed":    "refactor",
    "Deprecated": "chore",
    "Security":   "fix",
    "Docs":       "docs",
    "Breaking":   "feat",   # surfaced with ! marker in `_message_from_changelog`
    "Other":      "chore",
}

# Vocabulary for inferring conventional-commit scope from CHANGELOG bullet
# lead-ins or commit-subject text. Patterns are case-insensitive regex; the
# first match wins. Order matters — put more-specific patterns first.
# Maintain in lockstep with the project's actual subsystem names.
_SCOPE_PATTERNS = [
    (r"release\s*wizard",           "release-wizard"),
    (r"git\s*commit\s*dialog",      "commit-dialog"),
    (r"git\s*(status|column)",      "git-status"),
    (r"auto[-\s]*commit",           "auto-commit"),
    (r"settings?\s*dialog",         "settings"),
    (r"project\s*tree",             "tree"),
    (r"scaffold(?:ing)?",           "scaffold"),
    (r"shadow[-\s]*link",           "shadow-link"),
    (r"sync\b",                     "sync"),
    (r"tokensave\b",                "tokensave"),
    (r"changelog\b",                "changelog"),
]

# Words in a bullet description that signal "user-visible feature" rather
# than internal refactor. Used to escalate `Changed` section bullets from
# `refactor:` to `feat:` in commit subjects.
_USER_VISIBLE_HINTS = re.compile(
    r"\b(button|dialog|wizard|menu|toolbar|hotkey|shortcut|"
    r"command|option|toggle|checkbox|tab|panel|label|tooltip|"
    r"radio|dropdown|preview|banner|popup)\b",
    re.IGNORECASE,
)

# Imperative-mood rewriter — applied to the FIRST word of the subject only.
# Catches the most common past-tense / -ing slip-ups.
_IMPERATIVE_REWRITES = {
    "added":   "add",
    "adds":    "add",
    "adding":  "add",
    "fixed":   "fix",
    "fixes":   "fix",
    "fixing":  "fix",
    "updated": "update",
    "updates": "update",
    "updating":"update",
    "removed": "remove",
    "removes": "remove",
    "removing":"remove",
    "changed": "change",
    "changes": "change",
    "changing":"change",
    "refactored": "refactor",
    "improved":   "improve",
    "improves":   "improve",
}

# Common locations for project changelog files. First hit wins.
_CHANGELOG_CANDIDATES = [
    "CHANGELOG.md", "CHANGELOG", "Changelog.md",
    "docs/CHANGELOG.md", "HISTORY.md", "RELEASES.md",
]

# Anti-pattern: filename listings in the subject. Catches three variants
# the LLM commonly produces despite the system prompt telling it not to:
#   "update X.md, Y.md"           — comma-separated (the original case)
#   "update X.md and Y.md"        — natural-language "and" connector
#   "update X.md, Y.md, and Z.md" — Oxford-comma variant
#   "update X.md; Y.md"           — semicolon-separated
# All match the same anti-pattern: a verb followed by a filename followed by
# any separator and another filename-shaped token.
_FILENAME_LISTING_RE = re.compile(
    r"\b(update|change|modify|edit|refactor|fix)\s+"
    r"[\w.-]+\.\w+\s+"                       # first filename + any whitespace
    r"(?:,\s*(?:and\s+)?|and\s+|;\s*|or\s+)" # connector: ',', ', and', 'and', ';', 'or'
    r"[\w.-]+\.\w+",                         # second filename
    re.IGNORECASE,
)


def _strip_md(s: str) -> str:
    """Strip bold, italic, inline code, and markdown links from a string."""
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)        # bold
    s = re.sub(r"\*([^*]+)\*",      r"\1", s)        # italic
    s = re.sub(r"`([^`]+)`",        r"\1", s)        # code
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # links
    return s


def _escalate_commit_type(subject: str, has_source_changes: bool) -> str:
    """Escalate generic `chore:` and `docs:` prefixes to `refactor:` when
    source files are among the changed files.

    Scoped variants like `chore(deps):` or `docs(api):` are left alone —
    they carry enough signal to be treated as intentional.
    """
    if not has_source_changes:
        return subject
    for prefix in ("chore", "docs"):
        if subject.lower().startswith(f"{prefix}:"):
            if not re.match(rf"^{prefix}\([^)]+\):", subject, re.IGNORECASE):
                subject = "refactor:" + subject[len(f"{prefix}:"):]
            break
    return subject


def _normalize_commit_body(body: str) -> str:
    """Wrap body text at 72 chars per paragraph.

    Also splits jammed bullets (`. - Foo` on one line) that small local
    LLMs (e.g. Qwen 2.5 14B Q4) produce when they don't respect newlines.
    """
    if not body:
        return body
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    normalised = []
    for p in paragraphs:
        # Split `. - text` → `.\n- text` (also `: - text`)
        p = re.sub(r"([.:])\s+-\s+", r"\1\n- ", p)
        if "\n- " in p:
            wrapped_lines = []
            for line in p.split("\n"):
                if line.startswith("- "):
                    wrapped_lines.append(textwrap.fill(
                        line, width=72,
                        initial_indent="", subsequent_indent="  ",
                        break_long_words=False, break_on_hyphens=False,
                    ))
                else:
                    wrapped_lines.append(textwrap.fill(
                        line, width=72,
                        break_long_words=False, break_on_hyphens=False,
                    ))
            normalised.append("\n".join(wrapped_lines))
        else:
            normalised.append(textwrap.fill(
                p, width=72,
                break_long_words=False, break_on_hyphens=False,
            ))
    return "\n\n".join(normalised)


def _sanitize_commit_message(subject: str, body: str = "",
                              has_source_changes: bool = False) -> tuple[str, str]:
    """Enforce subject/body invariants on any candidate message.

    Returns (subject, body). Empty subject means "give up; caller should
    fall through to a lower-priority strategy".

    Rules:
      * Strip markdown noise: ``**x**`` → ``x``, backticks → none,
        ``[text](url)`` → ``text``
      * Strip surrounding quotes / leading "Here's a commit message:" preambles
      * Imperative mood — rewrite first word if it matches a known past tense
      * Subject ≤ 72 chars (truncate at last word boundary that fits)
      * Escalate ``chore:`` / ``docs:`` → ``refactor:`` when source files changed
      * Reject filename-listing anti-pattern → empty subject (forces fallback)
      * Body wrapped at 72 chars per line
    """
    subject = _strip_md((subject or "").strip())
    body    = _strip_md((body or "").strip())

    for pre in ("Here's a commit message:", "Commit message:",
                "Suggested commit message:"):
        if subject.lower().startswith(pre.lower()):
            subject = subject[len(pre):].lstrip(":").strip()
    subject = subject.strip("`'\"")

    if "\n" in subject:
        head, _, tail = subject.partition("\n")
        subject = head.strip()
        if tail.strip():
            body = (tail.strip() + ("\n\n" + body if body else "")).strip()

    m = re.match(r"^(?:(\w+(?:\([^)]+\))?!?:\s+))?(\w+)(.*)$", subject)
    if m:
        prefix, first, rest = m.group(1) or "", m.group(2), m.group(3)
        canonical = _IMPERATIVE_REWRITES.get(first.lower())
        if canonical:
            subject = f"{prefix}{canonical}{rest}"

    if _FILENAME_LISTING_RE.search(subject):
        return "", ""

    subject = _escalate_commit_type(subject, has_source_changes)

    if len(subject) > 72:
        truncated = subject[:72]
        sp = truncated.rfind(" ")
        if sp > 40:
            truncated = truncated[:sp]
        subject = truncated.rstrip(",;:-")

    body = _normalize_commit_body(body)
    return subject, body


def _recent_commit_subjects(repo_path: str, n: int = 5) -> list:
    """Return the last `n` commit subject lines (most-recent first)."""
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", repo_path, "log", f"-n{n}", "--format=%s"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _find_changelog_file(repo_path: str) -> str | None:
    """Locate the first present CHANGELOG-style file in the repo."""
    for candidate in _CHANGELOG_CANDIDATES:
        full = os.path.join(repo_path, candidate)
        if os.path.isfile(full):
            return candidate
    return None


def _pending_diff(repo_path: str, *paths: str, lines_of_context: int = 0) -> str:
    """Return the diff between HEAD and the working tree (staged + unstaged).

    Used for commit-message suggestion BEFORE the GitCommitDialog actually
    stages files. ``git diff HEAD`` captures everything that would land in
    the commit if the user stages and commits all working-tree changes.
    """
    cmd = [GIT_EXE, "-C", repo_path, "diff", "HEAD", "--no-color",
           f"-U{lines_of_context}"]
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _extract_changelog_additions(repo_path: str) -> list:
    """Parse the staged CHANGELOG diff into structured bullet records.

    Returns a list of dicts: ``{"section": "Added"|"Fixed"|..., "lead_in": str,
    "description": str}``. Returns ``[]`` if no changelog or no additions.

    Handles Keep-a-Changelog format with bolded lead-ins:
        ### Added
        - **Hotfix bump option in the Release Wizard** — fourth radio option…
    Falls back gracefully to plain bullets without bold lead-ins.
    """
    cl_file = _find_changelog_file(repo_path)
    if not cl_file:
        return []
    # NOTE: use generous context (-U20) so the section header (### Added /
    # ### Changed) appears as a CONTEXT line above any newly-added bullets.
    # Without this, bullets added to EXISTING sections become invisible to
    # the parser because the section header isn't itself a `+` line.
    diff = _pending_diff(repo_path, cl_file, lines_of_context=20)
    if not diff:
        return []

    section = None
    bullets = []
    current_bullet = None

    for raw_line in diff.splitlines():
        # Skip diff metadata
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("@@"):
            # Hunk boundary — section context from a different hunk is no
            # longer reliable. Reset so we don't misattribute the next bullet.
            section = None
            current_bullet = None
            continue

        # Classify the line: + (added), - (removed), space (context)
        if raw_line.startswith("+"):
            line = raw_line[1:]
            is_added = True
        elif raw_line.startswith(" "):
            line = raw_line[1:]
            is_added = False
        elif raw_line.startswith("-"):
            continue   # ignore removed lines for section/bullet tracking
        else:
            # Should not happen for unified diff output
            continue

        # Section header: ### Added, ### Changed, etc.
        # Track from BOTH context and added lines so a bullet added under an
        # existing section header still gets the right section attribution.
        m_section = re.match(r"^#{2,4}\s+(\w+)\s*$", line)
        if m_section:
            section = m_section.group(1)
            current_bullet = None
            continue

        # Only PROCESS added lines for bullets — context lines just inform section.
        if not is_added:
            continue

        # New bullet starting with "- "
        m_bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m_bullet and section:
            text = m_bullet.group(1)
            # Bolded lead-in pattern: handles
            #   **lead** — desc       (en-dash, em-dash, hyphen, colon)
            #   **lead.** desc        (period INSIDE the bold; no separator after)
            #   **lead** desc         (no separator at all)
            m_bold = re.match(
                r"^\*\*(?P<lead>[^*]+?)\*\*\s*(?:[—\-–:]\s*)?(?P<desc>.+)$",
                text,
            )
            if m_bold:
                lead = m_bold.group("lead").strip().rstrip(".").strip()
                desc = m_bold.group("desc").strip()
            else:
                # Fall back to first 60 chars or first sentence as lead
                first_sent = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
                lead = (first_sent[:60].rstrip() if len(first_sent) > 60
                        else first_sent).rstrip(".")
                desc = text[len(first_sent):].strip() or text
            current_bullet = {"section": section, "lead_in": lead,
                              "description": desc}
            bullets.append(current_bullet)
            continue

        # Continuation of a previous bullet (no leading dash)
        if current_bullet and line.strip():
            current_bullet["description"] = (
                current_bullet["description"] + " " + line.strip()
            ).strip()

    return bullets


def _extract_scope(text: str) -> str | None:
    """Match `text` against the scope vocabulary; return scope or None."""
    for pat, scope in _SCOPE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return scope
    return None


def _dominant_directory(files: list) -> str | None:
    """Find the most-common meaningful directory among staged files.

    `files` is a list of (xy, fname) tuples (the format `_suggest_from_filenames`
    uses). Returns the last component of the deepest common directory, or
    None if all files are at repo root.
    """
    if not files:
        return None
    dirs = []
    for _xy, fname in files:
        parent = os.path.dirname(fname.replace("\\", "/"))
        if parent:
            dirs.append(parent)
    if not dirs:
        return None
    # Most common directory
    from collections import Counter
    most_common, _ = Counter(dirs).most_common(1)[0]
    # Return the LAST meaningful component (e.g. src/components/wizard → wizard)
    parts = [p for p in most_common.split("/") if p and p not in
             ("src", "lib", "app", "source")]
    return parts[-1] if parts else None


def _message_from_changelog(bullets: list, files: list) -> tuple:
    """Build (subject, body) from CHANGELOG bullet additions. Returns ("","")
    if `bullets` is empty.

    Strategy:
      * Pick the highest-impact section (Breaking > Added > Fixed > Changed > …)
      * Map section → conventional-commit type (`feat` / `fix` / `refactor` / …)
      * Escalate Changed → feat when bullet description hints at user-visible UI
      * Infer scope from bullet lead-ins (vocabulary), then file paths
      * Subject = `type(scope): lead-in` (or "+ N more" if multiple bullets)
      * Body = stripped descriptions, one paragraph per bullet, wrapped at 72
    """
    if not bullets:
        return "", ""

    # Group bullets by section
    by_section = {}
    for b in bullets:
        by_section.setdefault(b["section"], []).append(b)

    # Impact order: prefer Added > Fixed > Changed > Removed > Deprecated > Docs > Security
    impact_order = ["Breaking", "Added", "Fixed", "Changed", "Security",
                    "Removed", "Deprecated", "Docs", "Other"]
    dominant_section = next((s for s in impact_order if s in by_section), None)
    if not dominant_section:
        return "", ""

    dom_bullets = by_section[dominant_section]
    ctype = _SECTION_TO_TYPE.get(dominant_section, "chore")

    # Escalate Changed → feat when bullet text describes user-visible UI changes
    if dominant_section == "Changed":
        for b in dom_bullets:
            combined = b["lead_in"] + " " + b["description"]
            if _USER_VISIBLE_HINTS.search(combined):
                ctype = "feat"
                break

    # Breaking marker
    bang = "!" if dominant_section == "Breaking" else ""

    # Scope: try vocabulary on every lead-in, then fall back to directory
    scope = None
    for b in dom_bullets:
        s = _extract_scope(b["lead_in"])
        if s:
            scope = s
            break
    if not scope:
        scope = _dominant_directory(files)

    # Subject
    primary_lead = dom_bullets[0]["lead_in"]
    # Lowercase first character so it reads naturally after "feat(x): "
    if primary_lead:
        primary_lead = primary_lead[0].lower() + primary_lead[1:]
    # Count ALL other bullets across ALL sections (not just same-section)
    total_bullets = sum(len(v) for v in by_section.values())
    extras = total_bullets - 1
    if extras > 0:
        primary_lead += f" + {extras} more"

    scope_str = f"({scope})" if scope else ""
    subject = f"{ctype}{scope_str}{bang}: {primary_lead}"

    # Body: one paragraph per bullet across ALL sections, lead-in + description
    body_paragraphs = []
    for section_name in impact_order:
        section_bullets = by_section.get(section_name) or []
        for b in section_bullets:
            lead = b["lead_in"].strip()
            desc = b["description"].strip()
            # Take first ~2 sentences of description, capped at 240 chars
            sentences = re.split(r"(?<=[.!?])\s+", desc, maxsplit=2)
            short_desc = " ".join(sentences[:2])[:240].strip()
            if short_desc and short_desc != lead:
                body_paragraphs.append(f"{lead}: {short_desc}")
            else:
                body_paragraphs.append(lead)
    body = "\n\n".join(body_paragraphs)

    return subject, body


def _diff_added_python_symbols(repo_path: str) -> dict:
    """Parse `git diff --cached` for newly-added top-level def/class names."""
    diff = _pending_diff(repo_path, lines_of_context=0)
    if not diff:
        return {"functions": [], "classes": []}

    functions, classes = [], []
    for line in diff.splitlines():
        # Only consider added lines that start at column 0 (top-level definitions)
        # The leading + is followed immediately by the keyword.
        m_fn = re.match(r"^\+(?:async\s+)?def\s+(\w+)", line)
        m_cl = re.match(r"^\+class\s+(\w+)", line)
        if m_fn:
            functions.append(m_fn.group(1))
        elif m_cl:
            classes.append(m_cl.group(1))
    # Dedup, preserve order
    return {
        "functions": list(dict.fromkeys(functions)),
        "classes":   list(dict.fromkeys(classes)),
    }


def _suggest_from_diff_content(repo_path: str, files: list) -> tuple:
    """Strategy 2 — infer message from file kinds + added Python symbols.

    `files` is a list of (xy, fname) tuples. Returns ("","") if no signal.
    """
    if not files:
        return "", ""

    basenames = [os.path.basename(f) for _xy, f in files]
    exts = {os.path.splitext(b)[1].lower() for b in basenames}
    paths = [f.replace("\\", "/") for _xy, f in files]

    doc_exts    = {".md", ".rst", ".txt", ".adoc"}
    test_paths  = [p for p in paths if re.search(r"(?:^|/)tests?/", p)
                   or os.path.basename(p).startswith("test_")
                   or os.path.basename(p).endswith("_test.py")]
    config_files = {"requirements.txt", "pyproject.toml", "package.json",
                    "package-lock.json", "Pipfile", "Pipfile.lock",
                    "poetry.lock", "setup.py", "setup.cfg"}
    config_paths = [p for p in paths if os.path.basename(p) in config_files]
    ci_paths     = [p for p in paths if "/.github/workflows/" in "/" + p]
    scope        = _dominant_directory(files)

    # Docs only
    if exts and exts <= doc_exts:
        if len(files) == 1:
            return f"docs: update {basenames[0]}", ""
        return "docs: update documentation", ""

    # CI workflows
    if ci_paths and len(ci_paths) == len(files):
        return f"ci: update {os.path.basename(ci_paths[0])}", ""

    # Config / deps
    if config_paths and len(config_paths) == len(files):
        return "chore(deps): update dependencies", ""

    # Tests only
    if test_paths and len(test_paths) == len(files):
        if scope:
            return f"test({scope}): add tests", ""
        return f"test: add {basenames[0]}", ""

    # Source-code changes: look at added Python symbols
    has_py = any(p.endswith(".py") for p in paths)
    if has_py:
        syms = _diff_added_python_symbols(repo_path)
        scope_str = f"({scope})" if scope else ""
        if syms["classes"]:
            return f"feat{scope_str}: add {syms['classes'][0]}", ""
        if syms["functions"]:
            fn = syms["functions"][0]
            return f"feat{scope_str}: add {fn}", ""
        # No new top-level defs AND no inferable scope — there's nothing useful
        # left to say at this level of analysis. Return empty so the
        # orchestrator falls through to the generic backstop strategy. (Using
        # `basenames[0]` here produces misleading messages like
        # "refactor: update BASIC_INSTRUCTIONS.md" for multi-file commits that
        # touched many things — the first alphabetical filename is not a scope.)
        if scope:
            return f"refactor({scope}): update {scope}", ""
        return "", ""

    return "", ""


def _build_llm_prompt(diff: str, recent: list, max_diff_chars: int) -> tuple:
    """Construct (system, user) prompt text for the LLM call."""
    system = (
        "You write conventional-commit messages. Output ONE commit message:\n"
        "- Subject line MUST be 72 chars or less, imperative mood "
        "(use add/fix/update, NOT added/fixed/updated).\n"
        "- Start with a conventional-commit prefix: "
        "feat / fix / chore / docs / refactor / perf / test / build / ci.\n"
        "- Optionally include scope: feat(scope): subject.\n"
        "- Blank line, then a body wrapped at 72 chars per line.\n"
        "- Match the existing tone from the recent commit subjects.\n"
        "- Output ONLY the commit message. NO preamble, NO markdown, "
        "NO quotes, NO code fences, NO explanation."
    )
    recent_lines = "\n".join(f"- {s}" for s in recent[:5]) if recent else "(no prior commits)"
    user = (
        f"Recent commit subjects (tone reference):\n{recent_lines}\n\n"
        f"Staged diff (truncated to {max_diff_chars} chars):\n"
        f"```diff\n{diff[:max_diff_chars]}\n```"
    )
    return system, user


def _iter_sse_events(response):
    """Yield decoded `data:` payloads from an HTTPResponse byte stream.

    Both Anthropic and OpenAI-compatible streaming use SSE (`data: <json>\\n`
    lines, terminator `data: [DONE]` for OpenAI). Network buffering can split
    a JSON payload mid-line, so we accumulate raw bytes in a bytearray and
    only yield once we've seen a complete `\\n`-terminated line. CRLF is
    handled via `rstrip("\\r")`. Non-data lines (event:, id:, retry:, blank
    keep-alives, SSE comments starting with `:`) are skipped.

    The generator stops when the underlying socket closes — the caller does
    not need to handle StopIteration specially.
    """
    buf = bytearray()
    while True:
        try:
            chunk = response.read(4096)
        except (OSError, ConnectionError):
            return
        if not chunk:
            # Final partial line (rare for well-behaved servers).
            if buf:
                line = buf.decode("utf-8", errors="replace").rstrip("\r")
                if line.startswith("data: "):
                    yield line[6:]
            return
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(buf[:i])
            del buf[:i + 1]
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line.startswith("data: "):
                yield line[6:]


def _call_anthropic(api_key: str, model: str, system_prompt: str, user_prompt: str,
                    max_tokens: int, timeout: int, on_token) -> str | None:
    """Anthropic Messages API — streaming and non-streaming. Pure execution layer;
    validation and error handling live in the _call_llm dispatcher."""
    import urllib.request
    payload = {
        "model": model or "claude-haiku-4-5",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if on_token is not None:
        payload["stream"] = True
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    if on_token is not None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            pieces = []
            for event in _iter_sse_events(resp):
                try:
                    data = json.loads(event)
                except json.JSONDecodeError:
                    continue
                # Anthropic streams content_block_delta events with
                # {"delta": {"type":"text_delta","text":"..."}}
                if data.get("type") == "content_block_delta":
                    delta = (data.get("delta") or {}).get("text", "")
                    if delta:
                        pieces.append(delta)
                        try:
                            on_token(delta)
                        except Exception:
                            log.exception("on_token callback raised")
        return "".join(pieces).strip() or None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.strip() or None


def _call_openai_compat(url: str, api_key: str, model: str,
                        system_prompt: str, user_prompt: str,
                        max_tokens: int, timeout: int, on_token) -> str | None:
    """OpenAI Chat Completions — covers openai, openai_compatible, and ollama.
    Caller resolves the endpoint URL before dispatching here."""
    import urllib.request
    payload = {
        "model": model or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    if on_token is not None:
        payload["stream"] = True
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    if on_token is not None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            pieces = []
            for event in _iter_sse_events(resp):
                if event == "[DONE]":
                    break
                try:
                    data = json.loads(event)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta_obj = choices[0].get("delta") or {}
                delta = delta_obj.get("content") or ""
                if delta:
                    pieces.append(delta)
                    try:
                        on_token(delta)
                    except Exception:
                        log.exception("on_token callback raised")
        return "".join(pieces).strip() or None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip() or None


def _call_llm(cfg: dict, system_prompt: str, user_prompt: str,
              max_tokens: int = 1500, timeout: int | None = None,
              on_token=None) -> str | None:
    """General-purpose LLM call. Returns raw text or None on ANY failure.

    Used by the commit-message orchestrator AND the AI Code Review feature
    AND any future agentic stages. Stays propose-only by design — this
    function returns text; what the caller does with that text is their
    concern (e.g. displaying in a dialog, parsing tool calls, etc.).

    Supported providers (`cfg["provider"]`):
      * "anthropic"        — native Messages API at api.anthropic.com
      * "openai"           — OpenAI Chat Completions at api.openai.com
      * "openai_compatible" — any OpenAI-compatible endpoint
            (Ollama at http://localhost:11434, LM Studio at :1234,
             vLLM, llama-server, LocalAI, etc.)
      * "ollama"           — friendly alias for openai_compatible with the
            default Ollama base URL filled in if none was set.

    Returns None on any error: no key, missing model, network failure,
    timeout, provider error, empty response. Caller falls back appropriately.

    Streaming (when `on_token` is provided): the function sends
    `"stream": true` to the provider and calls `on_token(delta_text)` for
    each text chunk as it arrives. The full accumulated text is still
    returned at the end (so existing callers can continue to use the return
    value unchanged). If the provider doesn't support streaming for the
    given configuration, the function silently falls back to the blocking
    path — `on_token` simply doesn't get called.

    The `on_token` callback runs on whichever thread called `_call_llm`.
    Callers that need to push deltas to a Tk UI must wrap it in a
    `self.after(0, ...)` schedule (see AICodeReviewDialog._start_review).
    """
    import urllib.error

    if not cfg.get("enabled"):
        return None

    # Reasoning models on consumer GPUs can take 30-60+ seconds. Auto-promote
    # any timeout below 30 to 90 (users who explicitly picked 30/60 keep their value).
    if timeout is None:
        raw_timeout = int(cfg.get("timeout_seconds", 90))
        timeout = 90 if raw_timeout < 30 else raw_timeout

    provider    = (cfg.get("provider") or "anthropic").lower()
    model       = cfg.get("model") or ""
    base_url    = (cfg.get("base_url") or "").rstrip("/")
    api_key_env = cfg.get("api_key_env") or ""
    api_key     = os.environ.get(api_key_env, "") if api_key_env else ""

    # "ollama" is a friendly alias — falls through to OpenAI-compatible with
    # the default Ollama base URL if none was set.
    if provider == "ollama":
        provider = "openai_compatible"
        if not base_url:
            base_url = "http://localhost:11434"

    try:
        if provider == "anthropic":
            if not api_key:
                return None
            return _call_anthropic(api_key, model, system_prompt, user_prompt,
                                   max_tokens, timeout, on_token)
        if provider in ("openai", "openai_compatible"):
            if provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
            else:
                if not base_url:
                    return None
                url = base_url + "/v1/chat/completions"
            return _call_openai_compat(url, api_key, model, system_prompt, user_prompt,
                                       max_tokens, timeout, on_token)
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, json.JSONDecodeError, KeyError, OSError):
        return None

    return None


def _call_llm_for_commit_message(cfg: dict, repo_path: str) -> str | None:
    """Commit-message-specific LLM wrapper. Composes the prompt and calls _call_llm.

    Skipped when the diff is shorter than `min_diff_lines` — trivial commits
    don't justify an LLM call when the heuristic chain handles them fine.
    """
    if not cfg.get("enabled"):
        return None

    diff = _pending_diff(repo_path)
    if not diff:
        return None
    diff_lines = diff.count("\n")
    if diff_lines < int(cfg.get("min_diff_lines", 30)):
        return None

    max_chars = int(cfg.get("max_diff_chars", 24000))
    recent    = _recent_commit_subjects(repo_path, n=5)
    system, user = _build_llm_prompt(diff, recent, max_chars)
    return _call_llm(cfg, system, user, max_tokens=1500)


def _suggest_from_filenames(status_text: str) -> str:
    """Legacy file-pattern strategy. Last-resort fallback.

    Generates a conventional-commit-style message from `git status --short`
    output ONLY (no diff content, no CHANGELOG). The strategies above this
    in the chain are preferred when they have signal.
    """
    # NOTE: never strip() the full status_text before splitlines() — strip
    # eats the leading space from the first line, shifting columns and
    # silently dropping the first character of the filename when the first
    # entry is a working-tree modification (single leading space).
    lines = [l for l in status_text.splitlines() if len(l) >= 4]
    if not lines:
        return ""

    files = []
    for line in lines:
        xy   = line[:2].strip()
        fname = line[3:]
        if " -> " in fname:
            fname = fname.split(" -> ")[-1]
        files.append((xy, fname))

    if not files:
        return ""

    basenames = [os.path.basename(f) for _, f in files]
    exts      = {os.path.splitext(b)[1].lower() for b in basenames}
    has_del   = any("D" in xy for xy, _ in files)
    has_add   = any("A" in xy or "?" in xy for xy, _ in files)

    if len(files) == 1:
        xy, fname = files[0]
        bname = os.path.basename(fname)
        if "D" in xy:
            return f"chore: remove {bname}"
        if "A" in xy or "?" in xy:
            ext = os.path.splitext(bname)[1].lower()
            return f"docs: add {bname}" if ext in (".md", ".txt", ".rst") else f"feat: add {bname}"
        if bname.lower().endswith((".md", ".txt", ".rst")):
            return f"docs: update {bname}"
        return f"chore: update {bname}"

    doc_exts  = {".md", ".txt", ".rst", ".adoc"}
    code_exts = {".py", ".js", ".ts", ".cs", ".cpp", ".c", ".h", ".rs", ".go", ".java"}

    if exts <= doc_exts:
        return "docs: update documentation"
    if exts <= code_exts:
        if len(basenames) <= 3:
            return f"chore: update {', '.join(basenames)}"
        return f"chore: update {len(files)} source files"
    if has_del and not has_add:
        return f"chore: remove {len(files)} files"
    if len(basenames) <= 2:
        return f"chore: update {', '.join(basenames)}"
    return f"chore: update {basenames[0]}, {basenames[1]} + {len(basenames) - 2} more"


def _render_release_notes(version: str, date: str, sections: dict,
                          summary: str = "") -> str:
    """Render the canonical release-notes markdown.

    Single source of truth used by BOTH the wizard textarea pre-fill AND
    the CHANGELOG.md section body. The leading ``## [version] — date``
    header is included so the same text can be inserted into the changelog
    verbatim.

    ``version`` should be passed WITHOUT the leading ``v`` (we add the
    bracket notation here). ``sections`` is the dict returned by
    ``_classify_commits_for_changelog``.
    """
    clean_version = version.lstrip("v")
    lines = [f"## [{clean_version}] — {date}", ""]
    if summary:
        lines.append(summary.strip())
        lines.append("")
    for section in _SECTION_ORDER:
        items = sections.get(section)
        if not items:
            continue
        lines.append(f"### {section}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    # Trim trailing blank line
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _patch_changelog(changelog_path: str, version: str, date: str,
                     notes_md: str) -> tuple:
    """Insert or replace a version section in CHANGELOG.md.

    Idempotent: if a section with the same version header already exists,
    its block (header line through the next ``## [`` line or EOF) is
    REPLACED with ``notes_md``. Otherwise the new section is inserted
    directly below the ``## [Unreleased]`` anchor.

    If neither the version section nor the ``## [Unreleased]`` anchor is
    present, returns ``(False, "missing anchor")`` and writes nothing —
    the caller surfaces this rather than producing a malformed file.

    Atomic write: writes to ``.tmp`` then ``os.replace``.

    Returns ``(ok: bool, message: str)``.
    """
    clean_version = version.lstrip("v")
    try:
        with open(changelog_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return (False, f"Could not read {changelog_path}: {exc}")

    new_block = notes_md if notes_md.endswith("\n") else notes_md + "\n"
    new_block = new_block.rstrip("\n") + "\n\n"   # ensure one blank line after

    # Try to find an existing section for this version.
    section_re = re.compile(
        rf"(?ms)^## \[{re.escape(clean_version)}\][^\n]*\n.*?(?=^## \[|\Z)"
    )
    m = section_re.search(text)
    if m:
        # Replace existing section.
        updated = text[:m.start()] + new_block + text[m.end():]
    else:
        # Insert below ## [Unreleased].
        anchor_re = re.compile(r"(?m)^## \[Unreleased\][^\n]*\n")
        am = anchor_re.search(text)
        if not am:
            return (False, "CHANGELOG.md is missing the `## [Unreleased]` anchor")
        insert_at = am.end()
        # Skip any blank lines directly after the anchor so the new section
        # ends up flush against (Unreleased) but with one blank line padding.
        tail = text[insert_at:]
        leading_blanks = len(tail) - len(tail.lstrip("\n"))
        # Keep exactly one blank line between [Unreleased] and the new section.
        updated = (text[:insert_at]
                   + "\n"
                   + new_block
                   + tail[leading_blanks:])

    # Atomic write
    tmp_path = changelog_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        os.replace(tmp_path, changelog_path)
    except OSError as exc:
        return (False, f"Could not write {changelog_path}: {exc}")

    return (True, "replaced" if m else "inserted")


def _zip_dist(dist_path: str, zip_path: str) -> str | None:
    """Zip the contents of ``dist_path`` into ``zip_path`` (flat).

    Uses ``shutil.make_archive`` with ``root_dir=dist_path, base_dir="."``
    so the archive contains files at the root, NOT nested under ``dist/``.
    Strips a trailing ``.zip`` from ``zip_path`` before passing it to
    ``make_archive`` (which re-appends the format extension automatically).

    Returns the absolute path of the created zip, or None on failure.
    """
    if not os.path.isdir(dist_path):
        return None
    try:
        any_files = any(True for _ in os.scandir(dist_path))
    except OSError:
        return None
    if not any_files:
        return None

    # Strip trailing .zip so make_archive doesn't double it.
    base = zip_path[:-4] if zip_path.lower().endswith(".zip") else zip_path
    try:
        produced = shutil.make_archive(
            base_name=base,
            format="zip",
            root_dir=dist_path,
            base_dir=".",
        )
    except (OSError, shutil.Error):
        return None
    return os.path.abspath(produced)


def _release_basename(path: str) -> str:
    """Return a clean release-artefact prefix (no spaces) for ``path``.

    Tries the git remote first — ``https://github.com/user/Repo.git`` →
    ``Repo``, ``git@github.com:user/Repo.git`` → ``Repo``. This matches the
    GitHub repo name, which is what users expect to see in
    ``Repo-vX.Y.Z-windows.zip``. Falls back to the folder basename with
    spaces replaced by dashes when the remote can't be read (no repo yet,
    no remote set, git missing).

    Pure-ish — does a single fast git call, no network.
    """
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        proc = None

    if proc and proc.returncode == 0:
        url = proc.stdout.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        # Split on both '/' (https) and ':' (ssh), take the last segment.
        tail = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if tail:
            return tail

    # Fallback: sanitize the folder basename.
    base = os.path.basename(path.rstrip(os.sep))
    return base.replace(" ", "-") or "release"


def _fmt_size(byte_count: int) -> str:
    """Format ``byte_count`` as a human-friendly size string.

    - <1 KB → "<1 KB" (avoids "0.0 MB" / "0.0 KB" for tiny files)
    - <1 MB → "N KB" with no decimal
    - ≥1 MB → "N.N MB" with one decimal
    """
    if byte_count < 1024:
        return "<1 KB"
    if byte_count < 1024 * 1024:
        return f"{byte_count // 1024} KB"
    return f"{byte_count / 1024 / 1024:.1f} MB"


def _fetch_tags(path: str) -> None:
    """Pull tags from origin so ``_last_release_tag`` reflects releases that
    were created remotely without a local ``git tag`` step.

    Why this exists: pre-v1.0.4 releases of this manager (and any release
    created by ``gh release create`` directly) only tag remotely. The local
    tree has no record of those tags until ``git fetch --tags`` runs. If
    the wizard relies purely on ``git describe --tags --abbrev=0``, it'll
    pick up an old prerelease like ``v1.0.0-alpha.1`` and suggest bumps
    from THAT, not the real current version.

    Silent on failure — wizard still works with whatever local tags exist.
    Short timeout (5 s) so a flaky network doesn't block dialog open for
    long.
    """
    try:
        subprocess.run(
            [GIT_EXE, "-C", path, "fetch", "--tags", "--quiet", "origin"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _git_tag(path: str, tag: str, message: str) -> tuple:
    """Create an annotated local tag. Returns (stdout+stderr, rc)."""
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path, "tag", "-a", tag, "-m", message],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (f"Error invoking git: {exc}", 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    return (out, proc.returncode)


def _git_push_with_tags(path: str) -> tuple:
    """Push HEAD plus the new annotated tag in one network round-trip."""
    try:
        proc = subprocess.run(
            [GIT_EXE, "-C", path, "push", "origin", "HEAD", "--follow-tags"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (f"Error invoking git: {exc}", 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    return (out, proc.returncode)


# ─── End release-wizard helpers ─────────────────────────────────────────────


# Stop hook injected into .claude/settings.json by _scaffold_git_hook.
# Runs the smart helper at .claude/auto-commit-helper.py — see _AUTO_COMMIT_HELPER.
# The helper amends consecutive auto-commits (so a long session is one commit,
# not seven) and builds a useful message including file count + names.
_STOP_HOOK_CMD = 'python ".claude/auto-commit-helper.py"'

# Legacy command we previously wrote — kept here so the idempotency check still
# recognises older projects that have the dumb single-line version and so the
# Retrofit pass can upgrade them in place.
_LEGACY_STOP_HOOK_CMD = (
    'git add -A && git diff --cached --quiet || '
    'git commit -m "auto: Claude session"'
)

# Helper script written to <project>/.claude/auto-commit-helper.py. Idempotent
# stop-hook brain: stages changes, bails if clean, then either amends the
# previous auto-commit or creates a new one. Designed to be path-portable so
# it works on Windows (cmd / PowerShell) and Unix shells alike — the Stop hook
# command is just `python ".claude/auto-commit-helper.py"`.
_AUTO_COMMIT_HELPER = '''"""Smart auto-commit Stop hook for Claude Code.

Managed by TokenSave Manager — do not hand-edit; it gets overwritten on
the next Retrofit pass. To disable: remove the Stop hook from
`.claude/settings.json`.

Behavior:
- Stages all changes (`git add -A`)
- Exits 0 if the working tree was already clean
- Builds a useful message: `auto: N file(s) (HH:MM) - path1, path2, +K more`
- If HEAD is itself an `auto:`-prefixed commit, AMENDS it rather than
  creating a new commit. So a long Claude Code session collapses into a
  SINGLE auto-commit that gets updated each session-end, instead of a wall
  of identical "auto: Claude session" entries.
"""
import subprocess
import sys
from datetime import datetime


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# 1. Stage everything Claude touched this session.
_run(["git", "add", "-A"])

# 2. Anything to commit?  `--quiet` exits 0 when index == HEAD.
if _run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
    sys.exit(0)

# 3. Build a useful commit message.
names = _run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
names = [n for n in names if n.strip()]
count = len(names)
sample = ", ".join(names[:3])
if count > 3:
    sample += f", +{count - 3} more"
ts = datetime.now().strftime("%H:%M")
plural = "" if count == 1 else "s"
msg = f"auto: {count} file{plural} ({ts}) - {sample}" if sample else f"auto: Claude session ({ts})"

# 4. Amend if previous commit was also an auto-commit; otherwise fresh commit.
last_msg = _run(["git", "log", "-1", "--pretty=%s"]).stdout.strip()
if last_msg.startswith("auto:"):
    subprocess.run(["git", "commit", "--amend", "-m", msg])
else:
    subprocess.run(["git", "commit", "-m", msg])
'''


def _scaffold_git_hook(path: str) -> list:
    """Write/merge a Claude Code Stop hook into .claude/settings.json AND
    write the smart helper script to .claude/auto-commit-helper.py.

    Creates .claude/ if it doesn't exist. Merges settings.json
    non-destructively. The helper script is always rewritten so projects
    pick up newer logic on the next Retrofit pass. Returns a list of
    human-readable action strings (for retrofit summaries).
    """
    settings_dir   = os.path.join(path, ".claude")
    settings_path  = os.path.join(settings_dir, "settings.json")
    helper_path    = os.path.join(settings_dir, "auto-commit-helper.py")
    try:
        os.makedirs(settings_dir, exist_ok=True)
    except OSError:
        return ["Could not create .claude/ directory"]

    actions = []

    # ── 1. Always (re)write the helper script ──────────────────────────────
    try:
        with open(helper_path, "w", encoding="utf-8") as f:
            f.write(_AUTO_COMMIT_HELPER)
        actions.append("Wrote .claude/auto-commit-helper.py (smart auto-commit)")
    except OSError as exc:
        return [f"Could not write {helper_path}: {exc}"]

    # ── 2. Read existing settings ──────────────────────────────────────────
    existing = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8-sig") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # treat as empty rather than failing

    hooks = existing.setdefault("hooks", {})
    stop  = hooks.setdefault("Stop", [])

    # ── 3. Upgrade legacy hook OR insert fresh hook ────────────────────────
    # A "managed" hook is any Stop entry whose command is either the new
    # helper invocation OR the old legacy oneliner. We replace legacy with
    # the new one in place so old projects get the smart behavior without
    # accumulating duplicate entries.
    upgraded = False
    inserted = False
    for entry in stop:
        for e in entry.get("hooks", []):
            if e.get("type") != "command":
                continue
            cmd = e.get("command", "")
            if cmd == _STOP_HOOK_CMD:
                # already on the new helper; nothing to do
                actions.append("Stop hook already on smart helper — skipped")
                break
            if cmd == _LEGACY_STOP_HOOK_CMD or cmd.startswith("git add -A"):
                e["command"] = _STOP_HOOK_CMD
                upgraded = True
                actions.append(
                    "Upgraded legacy Stop hook to smart helper "
                    "(amends consecutive auto-commits)")
                break
        else:
            continue
        break
    else:
        stop.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": _STOP_HOOK_CMD}],
        })
        inserted = True
        actions.append("Added smart Stop hook to .claude/settings.json")

    # ── 4. Persist settings only if we changed something ───────────────────
    if upgraded or inserted:
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except OSError as exc:
            return actions + [f"Could not write .claude/settings.json: {exc}"]

    return actions


def _setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=500_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger = logging.getLogger("tsm")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger

log = _setup_logger()

# ── Prompt snippets ────────────────────────────────────────────────────────────

PROMPT_SNIPPETS = [
    # ─────────────────────────── 🧭 EXPLORATION ───────────────────────────
    (
        "🧭  Codebase overview",
        "Give me a structural overview of this project using tokensave_context "
        "with the question 'overall architecture'. Then list the top-level "
        "modules with tokensave_files and run tokensave_outline on the entry "
        "point file. Summarise: what the project does, the main components, "
        "and the entry point flow."
    ),
    (
        "🧭  Find a symbol",
        "Use tokensave_search to find [symbol name]. Then use tokensave_context "
        "to explain what it does. Show its callers (tokensave_callers), what "
        "it calls (tokensave_callees), and where its fields are accessed if "
        "it's a class (tokensave_field_sites)."
    ),
    (
        "🧭  Understand a feature",
        "I want to understand how [feature/concept] works. Use tokensave_context "
        "to identify the relevant symbols, then read each one with tokensave_node "
        "and trace the data flow. Cite file:line for every claim. End with a "
        "one-paragraph summary of the feature's lifecycle."
    ),
    (
        "🧭  Onboarding tour",
        "Pretend I just joined this project and have 15 minutes. Use "
        "tokensave_outline on the main entry file, tokensave_module_api on the "
        "top 3 modules from tokensave_largest, and tokensave_files to list the "
        "directory layout. Produce a guided 5-step tour with file:line jumps."
    ),
    (
        "🧭  Module public API",
        "Use tokensave_module_api on [module or file name]. List its exports, "
        "their signatures (tokensave_signature for each), and how they're meant "
        "to be used (search for example call sites with tokensave_callers)."
    ),

    # ─────────────────────────── 🔬 ANALYSIS / TRACING ────────────────────
    (
        "🔬  Who calls this? Who does it call?",
        "Bidirectional call chain for [function name]: tokensave_callers shows "
        "what depends on it, tokensave_callees shows what it depends on. "
        "Render as a two-column tree with file:line on each node."
    ),
    (
        "🔬  Impact of changing X",
        "Run tokensave_impact on [function or class name]. Show me the full "
        "downstream impact chain. Then run tokensave_affected on the same "
        "symbol to see which tests would need re-running. Flag any high-risk "
        "ripples (cross-module, public API, etc.)."
    ),
    (
        "🔬  Trace a bug",
        "Bug symptom: [describe]. Workflow: (1) tokensave_search for the "
        "symbol you suspect, (2) tokensave_callers to see entry points, "
        "(3) tokensave_callees to see what it depends on, (4) tokensave_node "
        "to read the actual source, (5) tokensave_diagnose if you find "
        "anything anomalous. Walk me through your reasoning."
    ),
    (
        "🔬  Find similar code",
        "Use tokensave_similar on [function/class name] OR tokensave_signature_search "
        "for the signature pattern to find duplicates or near-duplicates. Group "
        "results by likely-extract-into-helper candidates."
    ),

    # ─────────────────────────── 📊 AUDITS ────────────────────────────────
    (
        "📊  Full health audit",
        "Run a comprehensive code health audit using ALL of these tokensave "
        "tools and produce one structured report:\n"
        "  • tokensave_health          (overall metrics)\n"
        "  • tokensave_complexity      (high-complexity functions)\n"
        "  • tokensave_god_class       (god classes / large files)\n"
        "  • tokensave_circular        (circular dependencies)\n"
        "  • tokensave_coupling        (high-coupling modules)\n"
        "  • tokensave_dead_code       (unused code)\n"
        "  • tokensave_unused_imports  (unused imports)\n"
        "  • tokensave_hotspots        (high-churn files)\n"
        "  • tokensave_recursion       (recursive call sites)\n"
        "  • tokensave_unsafe_patterns (risky patterns)\n"
        "Format: 🔴 Critical / 🟡 Warning / 🔵 Info, each with file:line and "
        "a one-sentence suggested fix. End with a top-5 priority list."
    ),
    (
        "📊  Architecture report",
        "Produce an architecture report for this project:\n"
        "  • tokensave_dsm              (dependency structure matrix)\n"
        "  • tokensave_dependency_depth (per-module)\n"
        "  • tokensave_inheritance_depth (per-class)\n"
        "  • tokensave_coupling          (high-coupling pairs)\n"
        "  • tokensave_module_api        (each top-level module)\n"
        "Format as a markdown ARCHITECTURE.md draft with sections: Module "
        "Map, Dependency Layers, Public APIs, Hot Spots. Cite file:line."
    ),
    (
        "📊  Test coverage audit",
        "Use tokensave_test_map to map tests to the code they cover. Use "
        "tokensave_test_risk to rank tests by risk. Use tokensave_doc_coverage "
        "for inline documentation gaps. Produce a 'Test & Docs Gap Report': "
        "which public symbols have no tests, which have low-risk tests, "
        "which lack docstrings. Cite file:line for each gap."
    ),
    (
        "📊  Security / unsafe-patterns scan",
        "Run tokensave_unsafe_patterns and tokensave_recursion across the "
        "whole project. Also run tokensave_diagnostics for general issues. "
        "Categorise findings: input validation, error handling, resource "
        "leaks, recursion depth, unsafe stdlib calls. Cite file:line and "
        "rate severity 🔴/🟡/🔵 each."
    ),
    (
        "📊  Hotspot risk scan",
        "Cross-reference tokensave_hotspots (high-churn files) with "
        "tokensave_complexity (high-complexity functions) and "
        "tokensave_coupling (high-coupling modules). Files high on multiple "
        "axes are most bug-prone. Output a ranked 'Risk Heatmap' table with "
        "the top 10 entries, churn / complexity / coupling scores, and a "
        "one-line refactor suggestion each."
    ),
    (
        "📊  Pre-release readiness check",
        "Check whether this project is ready to release:\n"
        "  • tokensave_diff_context     (recent changes summary)\n"
        "  • tokensave_changelog        (CHANGELOG draft from commits)\n"
        "  • tokensave_health           (overall metrics)\n"
        "  • tokensave_dead_code        (cruft that shouldn't ship)\n"
        "  • tokensave_unused_imports   (cruft that shouldn't ship)\n"
        "  • tokensave_circular         (architectural debt)\n"
        "  • tokensave_doc_coverage     (undocumented public API)\n"
        "Output a 'Release Readiness Checklist' with ✅/⚠/❌ per item. "
        "End with: 'Safe to release: yes/no, blockers:'."
    ),
    (
        "📊  Documentation coverage report",
        "Use tokensave_doc_coverage to find every public symbol without "
        "a docstring. Use tokensave_module_api to identify which of those "
        "are PART of a public module API (vs. internal helpers — those can "
        "be skipped). Output a prioritised 'Docstrings to add' list grouped "
        "by module, with the symbol's signature for context."
    ),

    # ─────────────────────────── 🪦 FINDINGS / CLEANUP ────────────────────
    (
        "🪦  Find dead code",
        "Use tokensave_dead_code and tokensave_unused_imports. List findings "
        "with file:line. For each, run tokensave_callers to double-check "
        "(some 'dead' code is actually called via reflection / dynamic "
        "dispatch and the index can miss it). Output: 'Safe to delete' vs "
        "'Verify before deleting' groups."
    ),
    (
        "🪦  List all TODOs / FIXMEs",
        "Use tokensave_todos to list every TODO/FIXME/HACK/XXX comment. "
        "Group by file, then by age (use git_log on the file to estimate "
        "when each was added). Flag any older than 6 months as stale."
    ),
    (
        "🪦  Circular dependencies",
        "Use tokensave_circular to find cycles. For each cycle, use "
        "tokensave_coupling on the involved modules to understand the "
        "binding strength. Propose a fix for each — typically: extract "
        "a third module, invert a dependency, or move a function."
    ),
    (
        "🪦  Largest / most complex files",
        "Cross-reference tokensave_largest (by LOC) with tokensave_complexity "
        "(by cyclomatic complexity) with tokensave_god_class (by method "
        "count). The intersection of all three is your top refactor "
        "priority. Output a ranked table with all three scores."
    ),

    # ─────────────────────────── 🛠 WORKFLOWS ─────────────────────────────
    (
        "🛠  Pre-commit checklist",
        "I'm about to commit. Run:\n"
        "  • tokensave_diff_context     (what's actually changed)\n"
        "  • tokensave_affected         (which tests would be affected)\n"
        "  • tokensave_impact           (downstream impact of changed symbols)\n"
        "  • tokensave_run_affected_tests (run only relevant tests)\n"
        "Then review the diff for: missing tests, missing docstrings, "
        "TODOs that should be resolved, secrets/keys. Output: 'Ready to "
        "commit: yes/no, issues:'."
    ),
    (
        "🛠  PR review prep",
        "I want to open a PR. Run tokensave_pr_context for context, "
        "tokensave_diff_context for the per-file diffs, tokensave_impact "
        "on the most-changed symbols, and tokensave_affected for test "
        "coverage. Draft a PR description with: Summary (2-3 sentences), "
        "Changes (bulleted by file), Testing (what was run), and Review "
        "Questions (what should a reviewer pay attention to)."
    ),
    (
        "🛠  Plan a refactor",
        "I want to refactor [target]. Workflow:\n"
        "  1. tokensave_coupling on the target to see what it's bound to\n"
        "  2. tokensave_dependency_depth to understand its position in the layer cake\n"
        "  3. tokensave_callers + tokensave_callees to map the blast radius\n"
        "  4. tokensave_rename_preview if any renames are involved\n"
        "  5. tokensave_test_map to see what tests cover it\n"
        "Output a step-by-step refactor plan with file:line for each step "
        "and the order to do them in (least-coupled first)."
    ),
    (
        "🛠  Refactor rename preview",
        "Use tokensave_rename_preview for [old name] → [new name]. List "
        "every affected file and line. Then use tokensave_callers to "
        "double-check that all callers are in the rename set (vs. e.g. "
        "string-based dynamic calls that the rename would miss)."
    ),
    (
        "🛠  Generate CHANGELOG entry",
        "Use tokensave_changelog to draft a CHANGELOG entry from recent "
        "commits. Group by ### Added / ### Changed / ### Fixed / ### Removed "
        "per Keep-a-Changelog format. Use the manager's existing CHANGELOG "
        "style as a tone reference (bolded lead-in followed by em-dash and "
        "prose description)."
    ),
    (
        "🛠  Generate ARCHITECTURE.md draft",
        "Produce a docs/ARCHITECTURE.md draft for this project. Use "
        "tokensave_outline on the main module(s), tokensave_module_api on "
        "each top-level module, tokensave_dependency_depth to identify "
        "layering, and tokensave_dsm to render module-pair dependencies. "
        "Format: Overview → Layer Diagram → Module Reference Table → "
        "Module Detail Sections. Cite file:line throughout."
    ),
]

# ── Colours (Catppuccin Mocha) ─────────────────────────────────────────────────

C = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "overlay0": "#6c7086",
    "text":     "#cdd6f4",
    "subtext":  "#bac2de",
    "blue":     "#89b4fa",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "red":      "#f38ba8",
    "lavender": "#b4befe",
    "sky":      "#89dceb",
    "peach":    "#fab387",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_projects():
    """Discover projects under every configured search root.

    A folder qualifies if it contains any of:
      - a tokensave index  (.tokensave/tokensave.db)  — full tokensave features
      - a CodeGraph index  (.codegraph/codegraph.db)  — full CodeGraph features
      - a git repository   (.git/)                    — git features only

    All three types are shown in the Projects tab. Each tool's commands show a
    friendly 'not initialised yet' prompt when called on a project that doesn't
    have its index — keeping tokensave and CodeGraph as equal citizens.
    """
    projects = []
    seen: set = set()
    for root in SEARCH_ROOTS:
        rpath  = _root_path(root)
        rlabel = _root_label(root)
        if not os.path.isdir(rpath):
            continue
        for dirpath, dirnames, _ in os.walk(rpath):
            rel = os.path.relpath(dirpath, rpath)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= MAX_DEPTH:
                dirnames.clear()
                continue

            # Check for markers BEFORE filtering dirnames
            has_ts  = ".tokensave" in dirnames
            has_git = ".git"       in dirnames
            has_cg  = ".codegraph" in dirnames

            # Strip hidden dirs and known noise from further traversal
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                           and not d.startswith(".")]

            if not (has_ts or has_git or has_cg):
                continue
            if dirpath in seen:
                continue
            seen.add(dirpath)

            # mtime: tokensave db age if available, else last git commit,
            # else codegraph db mtime (last full-build), else dirpath.
            #
            # tokensave uses SQLite in WAL mode. Incremental syncs write to
            # tokensave.db-wal; the main tokensave.db file's mtime only
            # advances when SQLite checkpoints (typically on close, or after
            # `sync --force`). So `getmtime(tokensave.db)` shows stale "6h
            # ago" even right after an incremental sync that's clearly
            # touched the index. Take the MAX mtime across the .db + WAL +
            # SHM sibling files so the "Last Synced" column tracks actual
            # sync activity, not just the last full checkpoint.
            if has_ts:
                db = os.path.join(dirpath, ".tokensave", "tokensave.db")
                ts_dir = os.path.join(dirpath, ".tokensave")
                mtime_candidates = []
                for fname in ("tokensave.db", "tokensave.db-wal",
                              "tokensave.db-shm"):
                    full = os.path.join(ts_dir, fname)
                    try:
                        mtime_candidates.append(os.path.getmtime(full))
                    except OSError:
                        pass
                if mtime_candidates:
                    mtime = max(mtime_candidates)
                else:
                    mtime = os.path.getmtime(dirpath)
            elif has_git:
                db    = None
                git_dir    = os.path.join(dirpath, ".git")
                commit_msg = os.path.join(git_dir, "COMMIT_EDITMSG")
                mtime = (os.path.getmtime(commit_msg)
                         if os.path.isfile(commit_msg)
                         else os.path.getmtime(git_dir))
            else:   # codegraph-only
                db    = None
                cg_db = os.path.join(dirpath, ".codegraph", "codegraph.db")
                mtime = os.path.getmtime(cg_db) if os.path.isfile(cg_db) else os.path.getmtime(dirpath)

            projects.append({
                "path":          dirpath,
                "name":          os.path.basename(dirpath),
                "db":            db,
                "mtime":         mtime,
                "root_label":    rlabel,
                "has_tokensave": has_ts,
                "has_git":       has_git,
                "has_codegraph": has_cg,
            })
    return sorted(projects, key=lambda p: p["mtime"], reverse=True)


def get_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        p = open(DESKTOP_PROJECT_FILE, encoding="utf-8").read().strip()
        if p and os.path.isfile(os.path.join(p, ".tokensave", "tokensave.db")):
            return p
    return None


def set_pinned(path):
    os.makedirs(os.path.dirname(DESKTOP_PROJECT_FILE), exist_ok=True)
    with open(DESKTOP_PROJECT_FILE, "w", encoding="utf-8") as f:
        f.write(path)


def clear_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        os.remove(DESKTOP_PROJECT_FILE)


def fmt_age(mtime):
    diff = datetime.now().timestamp() - mtime
    if diff < 60:         return "just now"
    if diff < 3600:       return f"{int(diff / 60)}m ago"
    if diff < 86400:      return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 7:  return f"{int(diff / 86400)}d ago"
    return datetime.fromtimestamp(mtime).strftime("%b %d")


def load_basic_instructions_template():
    """Load the BASIC_INSTRUCTIONS.md template text, or return a minimal fallback."""
    if os.path.isfile(BASIC_INSTRUCTIONS_TEMPLATE):
        raw = open(BASIC_INSTRUCTIONS_TEMPLATE, encoding="utf-8").read()
        # Replace any @<path>/project-baseline.md line with the current computed path
        # so the written BASIC_INSTRUCTIONS.md always points to the right location.
        # Use a lambda so BASELINE_INCLUDE_LINE is never parsed as a regex
        # replacement string — Windows paths contain backslashes that re.sub
        # would misinterpret as escape sequences (e.g. \p → bad escape error).
        raw = re.sub(r"^@[^\n]*project-baseline\.md",
                     lambda _: BASELINE_INCLUDE_LINE, raw, flags=re.MULTILINE)
        return raw
    # Minimal inline fallback if template file is missing
    return (
        "# [PROJECT NAME] — Basic Instructions\n\n"
        "<!-- CLAUDE: Replace all [PLACEHOLDER] sections on first use. -->\n\n"
        f"{BASELINE_INCLUDE_LINE}\n\n"
        "---\n\n"
        "## Project Overview\n\n"
        "**Name:** [PROJECT NAME]\n"
        "**Stack:** [Languages and frameworks]\n"
        "**Entry point:** [Main file or command]\n"
        "**Purpose:** [One sentence]\n\n"
        "---\n\n"
        "## Architecture\n\n[Replace with high-level structure.]\n\n"
        "---\n\n"
        "## Key Files\n\n[Replace with important files and their roles.]\n\n"
        "---\n\n"
        "## Project-Specific Rules\n\n[Replace or delete this section.]\n"
    )

# ── Single-instance ───────────────────────────────────────────────────────────

_MUTEX_NAME = "TokenSaveManager_SingleInstance"
_mutex_handle = None

def _acquire_instance_lock():
    """Return True if this is the first instance, False if another is running."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS

def _bring_existing_to_front():
    """Find the existing window by title and restore it."""
    hwnd = ctypes.windll.user32.FindWindowW(None, "TokenSave Manager")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)

# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_tray_icon():
    """Generate a 64×64 tray icon: dark circle with a white star."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (30, 30, 46, 255)   # Catppuccin Mocha base
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)
    # Simple 5-point star approximation using a polygon
    cx, cy, r_out, r_in = size / 2, size / 2, 26, 11
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=(137, 180, 250, 255))  # Catppuccin blue
    return img

# ── Projects tab controller ────────────────────────────────────────────────────

class ProjectsTabController:
    """Owns the Projects tab UI and all per-project commands.

    No back-reference to App — all cross-App dependencies flow through the
    explicit callbacks passed at construction time.  At most 10 callbacks;
    if more are needed, revisit with an EventBus or shared-state bus.

    Thread safety rule:
      • Methods named *_worker run on a background daemon thread.
        They MUST NOT call Tkinter directly — use self._tab.after(0, ...).
      • All other methods run on the main thread and may call Tkinter freely.

    Log rule:
      • Worker threads call self._on_log(msg, color) — App._log already
        schedules self.after(0, ...) internally, so it is thread-safe.
      • Direct Tkinter widget calls (tree, menu) must be on the main thread only.
    """

    # ── Class-level constants ─────────────────────────────────────────────────

    # Set of tag names used as Git-status overrides on project rows.
    # _update_git_status_cell strips these before applying the new tag.
    _GIT_STATUS_TAGS = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(
        self,
        notebook: ttk.Notebook,
        cfg: dict,
        get_projects,          # () -> list[dict]
        on_run,                # (args, cwd, label) -> None  (App._run, threaded)
        on_run_capture,        # (args, cwd, label) -> (raw, rc, elapsed)  (sync, from thread)
        on_shell,              # (cmd, cwd, env=None) -> (out, rc)  (sync, from thread)
        on_log,                # (msg, colour) -> None  (thread-safe)
        on_commit,             # (path) -> None  (App._open_commit_dialog)
        on_refresh,            # () -> None  (App.refresh — full tree rebuild)
        on_project_select,     # (path) -> None  (fired on row click)
        on_set_running,        # (running: bool, label: str) -> None  (App._set_running)
        on_settings,           # () -> None  (App.cmd_settings)
    ):
        self._notebook       = notebook
        self._cfg            = cfg
        self._get_projects   = get_projects
        self._on_run         = on_run
        self._on_run_capture = on_run_capture
        self._on_shell       = on_shell
        self._on_log         = on_log
        self._on_commit      = on_commit
        self._on_refresh     = on_refresh
        self._on_project_select = on_project_select
        self._on_set_running = on_set_running
        self._on_settings    = on_settings

        # Controller-level subprocess tracking (parallel to App._current_proc)
        # Workers managed by THIS controller set/clear this attribute so that
        # App._auto_refresh can see whether the controller is busy.
        self.current_proc: object = None          # subprocess.Popen | None
        self._ctrl_stop_requested: bool = False   # checked by batch workers
        self._git_status_refresh_cancel: bool = False
        self._git_status_refresh_running: bool = False

        self._tree: ttk.Treeview | None = None
        self._ctx_menu: tk.Menu | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Projects  ")
        self._build_projects_tab()
        self._build_context_menu()

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def _root(self) -> tk.Tk:
        """The top-level App window — use for dialog parenting."""
        return self._tab.winfo_toplevel()

    def get_selected_path(self) -> str | None:
        """Return the path of the currently selected project row, or None."""
        if self._tree is None:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("proj:"):
            return iid[5:]
        return None

    def stop(self):
        """Cancel any in-flight controller worker (called by App._stop_current)."""
        self._ctrl_stop_requested = True
        proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # ── Tree population (called by App.refresh) ───────────────────────────────

    def rebuild_tree(self, projects: list, active_path: str | None,
                     pinned: str | None) -> None:
        """Clear and repopulate the Treeview from a fresh project list.

        Called by App.refresh() after it has computed projects / active_path.
        Does NOT kick off the git-status refresh — App.refresh() does that.
        """
        if self._tree is None:
            return
        for item in self._tree.get_children():
            self._tree.delete(item)

        proj_cats = self._cfg.get("project_categories", {})

        groups: dict = {}
        for p in projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self._tree.insert("", tk.END, iid=ciid, text=cat,
                                  open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self._tree.exists(siid):
                    self._tree.insert(parent, tk.END, iid=siid,
                                      text=f"  ↳ {subcat}",
                                      open=True, tags=("subcategory",))
                parent = siid

            for p in projs:
                is_active    = (p["path"] == active_path)
                has_scaffold = self._has_scaffold(p["path"])
                has_ts       = p.get("has_tokensave", True)
                has_git      = p.get("has_git", False)
                has_cg       = p.get("has_codegraph", False)
                if is_active:
                    base_tag = "active"
                elif not has_ts:
                    base_tag = "git_only"
                elif not has_scaffold:
                    base_tag = "scaffold"
                else:
                    base_tag = "normal"
                synced_str = fmt_age(p["mtime"]) if has_ts else "—"
                cg_text = "✓" if has_cg else "—"
                git_text, git_tag = _format_git_status_cell(
                    p.get("git_status"), has_git)
                tags = (base_tag, git_tag) if has_git else (base_tag, "git_none")
                piid = f"proj:{p['path']}"
                self._tree.insert(parent, tk.END, iid=piid,
                                  text=p["name"],
                                  values=("★" if is_active else "",
                                          p["path"],
                                          synced_str,
                                          cg_text,
                                          git_text,
                                          "✔" if has_scaffold else "—"),
                                  tags=tags)

    def refresh_git_status_column(self, projects: list) -> None:
        """Kick off background refresh of the Git status column.

        Called by App.refresh() after rebuild_tree().
        """
        self._kick_off_git_status_refresh(projects)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _has_scaffold(path: str) -> bool:
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def _selected_path(self) -> str | None:
        """Return the selected project path, or show a warning and return None."""
        sel = self._tree.selection() if self._tree else ()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.",
                                   parent=self._root)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).",
                parent=self._root)
            return None
        return iid[5:]

    def _require_tokensave(self, path: str) -> bool:
        if os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db")):
            return True
        name = os.path.basename(path)
        messagebox.showinfo(
            "Not indexed with tokensave",
            f"'{name}' is a git project but doesn't have a tokensave index yet.\n\n"
            "Tokensave builds a code-graph that lets Claude navigate your project "
            "efficiently without reading every file.\n\n"
            "To add it:  right-click → ⚙ Retrofit…  and tick "
            "'Add tokensave @include + init'.",
            parent=self._root)
        return False

    def _require_codegraph_installed(self) -> bool:
        if CODEGRAPH_EXE and os.path.isfile(CODEGRAPH_EXE):
            return True
        result = messagebox.askyesno(
            "CodeGraph is not installed",
            "CodeGraph is not installed on this machine.\n\n"
            "CodeGraph is an alternative code-graph tool that builds a "
            "per-project SQLite index for Claude Code to query. It complements "
            "tokensave — both can be enabled on the same project.\n\n"
            "Open Settings now to install it?",
            parent=self._root)
        if result:
            self._on_settings()
        return False

    def _offer_commit_after_change(self, path: str, summary_label: str) -> None:
        """After a manager action, check if tree is dirty and offer to commit."""
        if not _is_local_git_repo(path):
            return
        status_out, _ = self._on_shell(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._on_log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self._root):
            self._on_commit(path)
        else:
            self._on_log(f"  Working tree left dirty — commit when you're ready.",
                         C["yellow"])

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_projects_tab(self) -> None:
        tab = self._tab

        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self._on_refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self._on_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "cg", "git", "scaffold"),
            show="tree headings",
            selectmode="browse",
        )
        self._tree.heading("#0",       text="Project")
        self._tree.heading("active",   text="")
        self._tree.heading("path",     text="Path")
        self._tree.heading("synced",   text="Last Synced")
        self._tree.heading("cg",       text="CG")
        self._tree.heading("git",      text="Git")
        self._tree.heading("scaffold", text="Scaffold")

        self._tree.column("#0",       width=170, stretch=False)
        self._tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self._tree.column("path",     width=220)
        self._tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self._tree.column("cg",       width=36,  stretch=False, anchor=tk.CENTER)
        self._tree.column("git",      width=60,  stretch=False, anchor=tk.CENTER)
        self._tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.tag_configure("active",      foreground=C["green"])
        self._tree.tag_configure("normal",      foreground=C["text"])
        self._tree.tag_configure("scaffold",    foreground=C["peach"])
        self._tree.tag_configure("git_only",    foreground=C["overlay0"])
        self._tree.tag_configure("pending",     foreground=C["yellow"])
        self._tree.tag_configure("git_clean",   foreground=C["green"])
        self._tree.tag_configure("git_dirty",   foreground=C["yellow"])
        self._tree.tag_configure("git_ahead",   foreground=C["sky"])
        self._tree.tag_configure("git_behind",  foreground=C["red"])
        self._tree.tag_configure("git_mixed",   foreground=C["peach"])
        self._tree.tag_configure("git_pending", foreground=C["overlay0"])
        self._tree.tag_configure("git_none",    foreground=C["overlay0"])
        self._tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("subcategory", foreground=C["lavender"])

        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _build_context_menu(self) -> None:
        m = tk.Menu(self._root, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_separator()
        m.add_command(label="🧠  CodeGraph Init",          command=self.cmd_codegraph_init)
        m.add_command(label="🧠  CodeGraph Sync",          command=self.cmd_codegraph_sync)
        m.add_command(label="🧠  CodeGraph Status",        command=self.cmd_codegraph_status)
        m.add_command(label="🧠  Remove CodeGraph Index",  command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔍  AI Code Review…",    command=self.cmd_ai_code_review)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        if not row.startswith("proj:"):
            return
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_tree_select(self, event=None) -> None:
        """Internal handler for <<TreeviewSelect>> — fires the project-select callback."""
        path = self.get_selected_path()
        if path:
            self._on_project_select(path)

    def _insert_pending_row(self, path: str, name: str) -> None:
        """Add a placeholder row while tokensave init is running."""
        self._tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))

    # ── Git status column refresh ─────────────────────────────────────────────

    def _kick_off_git_status_refresh(self, projects: list) -> None:
        """Background-walk every git project and update its Git column cell."""
        if self._git_status_refresh_running:
            self._git_status_refresh_cancel = True
        self._git_status_refresh_cancel  = False
        self._git_status_refresh_running = True

        projects_snapshot = list(projects)

        def worker():
            try:
                for p in projects_snapshot:
                    if self._git_status_refresh_cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue
                    try:
                        out, _rc = self._on_shell(
                            [GIT_EXE, "-C", path,
                             "status", "--porcelain=v2", "--branch"],
                            path)
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]     = status
                    p["_git_idx_mtime"] = idx_mtime
                    piid = f"proj:{path}"
                    self._tab.after(0, self._update_git_status_cell, piid, status)
                    time.sleep(0.05)
            finally:
                self._git_status_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _update_git_status_cell(self, piid: str, status: dict) -> None:
        """Main-thread: update a single row's Git column value + override tag."""
        if not self._tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            self._tree.set(piid, "git", text)
        except tk.TclError:
            return
        existing = list(self._tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in self._GIT_STATUS_TAGS]
        existing.append(tag)
        self._tree.item(piid, tags=tuple(existing))

    # ── Scaffold helpers ──────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path: str) -> list:
        """Copy Nuitka build templates into path.  Returns list of action strings."""
        name     = os.path.basename(path)
        src_ps1  = os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template")
        src_bat  = os.path.join(TEMPLATE_DIR, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._on_log("  [WARN] Nuitka templates not found in template directory — skipped",
                         C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {TEMPLATE_DIR}")
            return actions

        if os.path.isfile(dst_ps1):
            self._on_log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._on_log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._on_log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        if os.path.isfile(dst_bat):
            self._on_log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._on_log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._on_log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._on_log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])
        return actions

    def _scaffold_project(self, path: str, create_bi: bool = True,
                          run_init: bool = True, scaffold_nuitka: bool = False,
                          add_git_hook: bool = False) -> None:
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template()
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._on_log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._on_log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._on_log(f"  {action}", C["green"])

        if run_init:
            self._insert_pending_row(path, name)
            self._on_log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self._tab.after(0, self._on_set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._on_log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._on_log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._on_log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self.current_proc = None
                    self._tab.after(0, self._on_set_running, False, "")
                    self._tab.after(0, self._on_refresh)
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._on_refresh()
            self._offer_commit_after_change(path, "scaffold files")

    # ── Tokensave commands ────────────────────────────────────────────────────

    def cmd_set_active(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        set_pinned(path)
        self._on_log(f"Pinned → {path}", C["green"])
        try:
            states = [_classify_mcp_entry(p)["state"] for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if "ok" in states and not all(s == "ok" for s in states):
            bad = [lbl for (lbl, p), s in zip(_MCP_CONFIGS, states) if s != "ok"]
            self._on_log(
                f"  Pin will take effect at next Claude restart.  "
                f"Note: {', '.join(bad)} also still needs its MCP wiring "
                f"fixed (Settings → 🔌 Manage MCP wiring).",
                C["peach"])
        elif "ok" not in states:
            self._on_log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
        else:
            self._on_log(
                "  Pin will take effect at next Claude Desktop / Claude Code "
                "restart.  (Live in-session reload is deferred — see the "
                "wrapper script's docstring for context.)",
                C["overlay0"])
        self._on_refresh()

    def cmd_auto(self) -> None:
        clear_pinned()
        self._on_log("Auto-detect enabled — wrapper picks the most-recently-synced project at next launch.", C["sky"])
        self._on_log(
            "  Restart Claude Desktop / Claude Code to trigger a fresh "
            "auto-detect.",
            C["overlay0"])
        self._on_refresh()

    def cmd_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._on_run(["sync"], cwd=path, label=os.path.basename(path))

    def cmd_sync_all(self) -> None:
        projects = self._get_projects()
        if not projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self._root)
            return
        ts_projects = [p for p in projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self._root)
            return
        count = len(ts_projects)
        skipped = len(projects) - count
        skip_note = (f"\n({skipped} git-only project{'s' if skipped != 1 else ''} "
                     f"will be skipped)") if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self._root,
        ):
            return

        projects_snapshot = list(ts_projects)

        def worker():
            self._ctrl_stop_requested = False
            self._on_log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self._tab.after(0, self._on_set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._ctrl_stop_requested:
                    self._on_log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._on_log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._on_log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._on_log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self.current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._on_log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self._tab.after(0, self._on_set_running, False, "")
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._on_run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._on_log(f"  Status OK — {data.get('node_count')} nodes, "
                                 f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self._tab.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._on_log(line)
                self._on_log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self._tab.after(0, self._on_refresh)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name: str, msg: str) -> None:
        win = tk.Toplevel(self._root)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self._root)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_force_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self._root,
        ):
            self._on_run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def cmd_doctor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._run_doctor_with_purge_offer(path)

    def _run_doctor_with_purge_offer(self, path: str) -> None:
        """Run `tokensave doctor`, stream output, and offer to purge stale entries."""
        label = os.path.basename(path)

        def worker():
            cmd_str = "tokensave doctor"
            self._on_log(f"$ {cmd_str}  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            output_lines: list = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.current_proc = proc
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._on_log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._on_log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                stale_paths = self._extract_doctor_stale_paths(output_lines)
                if stale_paths and proc.returncode == 0:
                    self._tab.after(0, self._offer_doctor_purge, path, stale_paths)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_doctor")
            finally:
                self.current_proc = None
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-worker").start()

    @staticmethod
    def _extract_doctor_stale_paths(output_lines: list) -> list:
        """Parse tokensave doctor's stdout for the stale-entries section."""
        bullet_re = re.compile(r"^\s*[•\*\-]\s+(.+?)\s*$")
        in_block = False
        paths: list = []
        for line in output_lines:
            if "stale project" in line and "global DB" in line:
                in_block = True
                continue
            if not in_block:
                continue
            if "Re-run" in line and "tokensave doctor" in line:
                break
            m = bullet_re.match(line)
            if m:
                paths.append(m.group(1).strip())
            elif paths and not line.startswith(" ") and not line.startswith("\t"):
                break
        return paths

    def _offer_doctor_purge(self, path: str, stale_paths: list) -> None:
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (f"tokensave doctor found {n} stale project entr"
               f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
               f"{bullets}\n\n"
               "These projects were registered but their `.tokensave/` "
               "folders are gone — most likely deleted folders.\n\n"
               "Purge them now?  The manager will re-run `tokensave "
               "doctor` with `y` piped to confirm the interactive "
               "purge prompt.")
        if not messagebox.askyesno(
                "Purge stale tokensave projects?",
                msg, parent=self._root):
            self._on_log("  (purge skipped — stale entries left in place)", C["overlay0"])
            return
        self._run_doctor_purge(path)

    def _run_doctor_purge(self, path: str) -> None:
        """Re-run `tokensave doctor` with `y` piped to confirm the purge prompt."""
        label = "doctor (purge)"

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            captured: list = []
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.current_proc = proc
                try:
                    proc.stdin.write("y\ny\ny\ny\ny\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    captured.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                self._on_log(
                    "Done." if proc.returncode == 0
                    else f"Exited with code {proc.returncode}",
                    C["green"] if proc.returncode == 0 else C["red"])
                still_stale = self._extract_doctor_stale_paths(captured)
                if still_stale:
                    self._on_log(
                        f"  ⚠ Purge didn't take — tokensave still "
                        f"reports {len(still_stale)} stale entr"
                        f"{'y' if len(still_stale) == 1 else 'ies'}. "
                        f"tokensave doctor needs a real terminal "
                        f"(piped stdin doesn't trigger the prompt).",
                        C["peach"])
                    self._tab.after(0, self._offer_doctor_in_cmd, path, len(still_stale))
                else:
                    self._on_log("  ✓ Stale entries purged.", C["green"])
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in doctor purge")
            finally:
                self.current_proc = None
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-purge").start()

    def _offer_doctor_in_cmd(self, path: str, n_stale: int) -> None:
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The piped-stdin purge didn't work — tokensave needs "
                f"a real terminal for its interactive 'y/n' prompt.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self._root):
            self._on_log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return
        try:
            cmd_line = f'cmd.exe /k ""{TOKENSAVE}" doctor"'
            subprocess.Popen(
                cmd_line,
                cwd=path,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self._on_log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
        except OSError as e:
            self._on_log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    # ── CodeGraph commands ────────────────────────────────────────────────────

    def cmd_codegraph_init(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if _is_codegraph_project(path):
            messagebox.showinfo("Already initialised",
                f"{os.path.basename(path)} already has CodeGraph initialised.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph init in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell(
                [CODEGRAPH_EXE, "init", "--index", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)
            self._tab.after(0, lambda: self._offer_commit_after_change(
                path, "CodeGraph init files"))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph sync in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell([CODEGRAPH_EXE, "sync", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_status(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph status in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell([CODEGRAPH_EXE, "status", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines():
                self._on_log(f"  {line}", col)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        cg_dir = os.path.join(path, ".codegraph")
        if not os.path.isdir(cg_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no CodeGraph index.", parent=self._root)
            return
        if not messagebox.askyesno(
                "Remove CodeGraph index?",
                f"Delete the CodeGraph index for {name}?\n\n"
                f"This removes .codegraph/ from the project folder. Your "
                "source files are not touched. You can re-create the index "
                "later via 🧠 CodeGraph Init.",
                parent=self._root):
            return
        try:
            shutil.rmtree(cg_dir)
            self._on_log(f"  Removed .codegraph/ from {name}", C["green"])
        except OSError as e:
            self._on_log(f"  Could not remove .codegraph/: {e}", C["red"])
        self._on_refresh()

    # ── Git commands (Projects-tab variants) ──────────────────────────────────

    def cmd_git_log(self) -> None:
        """Navigate to the Git tab and show live status/log for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Switch to Git tab first so is_visible() is True when on_project_select fires.
        try:
            for idx in range(self._notebook.index("end")):
                if self._notebook.tab(idx, "text").strip() == "Git":
                    self._notebook.select(idx)
                    break
        except tk.TclError:
            pass
        # Now fire the project-select callback — App._on_project_select wires
        # set_active_path + refresh on GitTabController.
        self._on_project_select(path)

    def cmd_git_commit(self) -> None:
        """Open the Git Commit dialog for the selected project."""
        path = self._selected_path()
        if path:
            self._on_commit(path)

    def cmd_ai_code_review(self) -> None:
        """Open the AI Code Review dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} doesn't have a .git folder, "
                "so there's no pending diff to review.",
                parent=self._root)
            return
        llm_cfg = self._cfg.get("commit_message_llm") or {}
        if not llm_cfg.get("enabled"):
            messagebox.showinfo(
                "AI is not enabled",
                "Open Settings → 'AI commit messages' and enable AI to use "
                "this feature.",
                parent=self._root)
            return
        AICodeReviewDialog(self._root, path, llm_cfg)

    def cmd_git_init(self) -> None:
        """Initialise a git repository in the selected project folder."""
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        if _is_git_repo(path):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self._root,
            )
            return
        self._on_log(f"Running git init in {name}…", C["peach"])
        out, rc = self._on_shell([GIT_EXE, "-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._on_log(f"  {line}", col)
        if rc != 0:
            self._on_refresh()
            return
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._on_log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._on_log(f"  Warning: could not write .gitignore: {e}", C["yellow"])
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self._root,
        ):
            def do_commit():
                self._on_shell([GIT_EXE, "-C", path, "add", "-A"], path)
                cout, crc = self._on_shell(
                    [GIT_EXE, "-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self._tab.after(0, lambda l=line: self._on_log(f"  {l}", ccol))
                self._tab.after(0, self._on_refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self._on_refresh()

    def cmd_manage_gitignore(self) -> None:
        path = self._selected_path()
        if not path:
            return
        GitignoreDialog(self._root, path)

    def cmd_untrack_ignored(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo("Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self._root)
            return
        files = _find_tracked_but_ignored(path)
        if not files:
            messagebox.showinfo("Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self._root)
            return
        UntrackIgnoredDialog(self._root, path, files,
                             on_confirm=self._do_untrack_ignored)

    def _do_untrack_ignored(self, path: str, files: list) -> None:
        """Worker: run `git rm --cached -- <files>` in a background thread."""
        if not files:
            return
        name = os.path.basename(path)
        self._on_log(f"Untracking {len(files)} file"
                     f"{'s' if len(files) != 1 else ''} in {name}…",
                     C["peach"])

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "rm", "-r", "--cached", "--"] + files,
                    path)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._on_log(f"  {line}", col)
                if rc != 0:
                    return
                self._on_log(
                    f"  ✓ Untracked {len(files)} file"
                    f"{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path,
                    f"untrack {len(files)} ignored file"
                    f"{'s' if len(files) != 1 else ''}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── File / editor / path commands ─────────────────────────────────────────

    def cmd_open_folder(self) -> None:
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        editor_str = self._cfg.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self._root,
            )

    def cmd_copy_path(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(path)
        self._on_log(f"Copied: {path}", C["sky"])

    def cmd_remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self._root)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self._root,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._on_log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self._on_refresh()
        except Exception as e:
            self._on_log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self._root)

    # ── Shadow Links ──────────────────────────────────────────────────────────

    def cmd_shadow_links(self) -> None:
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self._root, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict,
                         run_sync: bool = True) -> None:
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            rc = 0
            try:
                self._on_log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._on_log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and TOKENSAVE and created > 0:
                    self._on_log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._on_run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._on_log(f"    {line}", col)

                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}."
                       if run_sync and created > 0 else ""),
                    parent=self._root))
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()

    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_assign_category(self) -> None:
        path = self._selected_path()
        if not path:
            return
        all_cats: list = []
        all_subs: dict = {}
        for r in SEARCH_ROOTS:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in self._cfg.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = self._cfg.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self._root, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path: str, cat, subcat) -> None:
        proj_cats = self._cfg.setdefault("project_categories", {})
        if cat is None:
            proj_cats.pop(path, None)
            self._on_log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._on_log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        _save_config(self._cfg)
        self._on_refresh()

    # ── Scaffold / Retrofit ───────────────────────────────────────────────────

    def cmd_scaffold(self) -> None:
        folder = filedialog.askdirectory(title="Select folder to scaffold",
                                         parent=self._root)
        if not folder:
            return
        ScaffoldDialog(self._root, folder, self._scaffold_project)

    def cmd_retrofit(self) -> None:
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self._root)
        if not folder:
            return
        RetrofitDialog(self._root, folder, self._do_retrofit)

    def cmd_retrofit_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self._root, path, self._do_retrofit)

    def _do_retrofit(self, path: str, add_tokensave: bool,
                     add_basic_instructions: bool,
                     add_nuitka: bool = False,
                     add_shadow_links: bool = False,
                     shadow_ext_map: dict | None = None,
                     add_git_hook: bool = False) -> None:
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} "
                         f"nuitka={add_nuitka}")
                self._on_log(f"Retrofitting {name}…", C["peach"])
                actions_taken = []

                if add_tokensave:
                    claude_md = os.path.join(path, "CLAUDE.md")
                    include_line = BASELINE_INCLUDE_LINE
                    if os.path.isfile(claude_md):
                        content = open(claude_md, encoding="utf-8", errors="ignore").read()
                        if "project-baseline.md" in content:
                            log.info("  CLAUDE.md already has @include — skipped")
                            self._on_log("  Tokensave already integrated in CLAUDE.md — skipped",
                                         C["overlay0"])
                        else:
                            with open(claude_md, "r+", encoding="utf-8") as f:
                                existing = f.read()
                                f.seek(0)
                                f.write(include_line + "\n\n" + existing)
                            log.info("  prepended @include to CLAUDE.md")
                            self._on_log("  Added tokensave @include to CLAUDE.md", C["green"])
                            actions_taken.append("Added tokensave rules to CLAUDE.md")
                    else:
                        with open(claude_md, "w", encoding="utf-8") as f:
                            f.write(
                                f"# {name} — Claude Instructions\n\n"
                                f"{include_line}\n"
                            )
                        log.info("  created CLAUDE.md with @include")
                        self._on_log("  Created CLAUDE.md with tokensave @include", C["green"])
                        actions_taken.append("Created CLAUDE.md with tokensave rules")

                if add_basic_instructions:
                    basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
                    if os.path.isfile(basic_md):
                        log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
                        self._on_log("  BASIC_INSTRUCTIONS.md already exists — skipped",
                                     C["overlay0"])
                    else:
                        template = load_basic_instructions_template()
                        with open(basic_md, "w", encoding="utf-8") as f:
                            f.write(template)
                        log.info("  created BASIC_INSTRUCTIONS.md")
                        self._on_log("  Created BASIC_INSTRUCTIONS.md", C["green"])
                        actions_taken.append("Created BASIC_INSTRUCTIONS.md")

                if add_nuitka:
                    nuitka_actions = self._scaffold_nuitka_build(path)
                    actions_taken.extend(nuitka_actions)

                if add_shadow_links:
                    ext_map = shadow_ext_map or DEFAULT_SHADOW_EXT_MAP
                    self._on_log("  Generating shadow extension links…", C["peach"])
                    created, skipped, failed = generate_shadow_links(path, ext_map)
                    update_gitignore_for_shadows(path, ext_map)
                    sl_msg = f"Shadow links: created {created}"
                    if skipped:
                        sl_msg += f", {skipped} already existed"
                    if failed:
                        sl_msg += f", {failed} failed"
                    self._on_log(f"  {sl_msg}", C["green"])
                    log.info(f"  {sl_msg}")
                    if created > 0:
                        actions_taken.append(sl_msg)

                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._on_log(f"  {action}", C["green"])
                    actions_taken.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions_taken or 'nothing changed'}")
                self._on_log(f"Retrofit complete: {path}", C["green"])
                self._tab.after(0, self._on_refresh)

                if actions_taken:
                    summary = "\n".join(f"  ✔ {a}" for a in actions_taken)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions_taken):
                        msg += ("\n\nNext step: open build.ps1 and replace "
                                "[ENTRY_SCRIPT] and [OUTPUT_NAME] before building.")
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Retrofit complete", msg, parent=self._root))
                if actions_taken:
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "retrofit additions"))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Retrofit failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()


# ── Ask tab controller ─────────────────────────────────────────────────────────

class AskTabController:
    """Owns the Ask tab UI and the agent conversation loop.

    Decoupled from App: receives a get_project_path callback instead of
    holding a reference to the App instance.
    """

    _ASK_SYSTEM_PROMPT = (
        "You are a code-aware assistant for the user's current project. "
        "You have access to READ-ONLY tools that let you read files, list "
        "directories, view git history, view the pending diff, and search "
        "the project's tokensave code graph when present.\n\n"
        "How to use tools:\n"
        "- Use the API's tool_calls mechanism — emit calls via the "
        "tool_calls field of your response, NOT as JSON text inside the "
        "content field. After the tool result is returned to you as a "
        "role:'tool' message, continue your reasoning and either call "
        "another tool or give a final text answer.\n"
        "- Do NOT guess about file contents or code that you have not read.\n"
        "- Cite file:line locations when you reference code.\n\n"
        "Tool-selection guide (CRITICAL — wrong tool choice wastes "
        "iterations):\n"
        "- **read_file** is your primary tool. When the user names a "
        "specific file in their question, OR when you're asked about a "
        "specific symbol or behaviour you can locate, just read the file "
        "directly. Don't search first.\n"
        "- **tokensave_search** finds DEFINED SYMBOLS by name — "
        "functions, classes, methods, constants. It does NOT do "
        "full-text grep across source. Searching for 'Popen', 'import', "
        "or any keyword that isn't a symbol name returns nothing. Use "
        "tokensave_search to answer 'where is X defined?' for an X that "
        "is itself a function/class/constant name.  The result includes "
        "the exact line number where the symbol is *defined* — "
        "**chain it into a read_file call with start_line set to that "
        "line and end_line set ~150-200 lines later** so you read the "
        "FULL body, not just the signature.  Most Python functions / "
        "class methods are 20-200 lines; reading too narrow a window "
        "shows only the docstring and you'll miss the actual logic.  "
        "Never read a >50 KB file without a line range; you'll just "
        "get the first 50 KB which is almost certainly not what you "
        "want.\n"
        "- **tokensave_context** builds a focused subgraph for a "
        "natural-language task description (e.g. 'how does the commit "
        "message generator work'). Returns related symbols + their "
        "relationships. Use sparingly — it's expensive on a big project.\n"
        "- **list_directory** for path discovery when you don't know "
        "what's in a folder.\n"
        "- **git_log / git_diff** for change history and pending work.\n\n"
        "Error handling:\n"
        "- If a tool returns an error message starting with '[tool error]', "
        "DO NOT report the failure to the user. Instead, read the error "
        "carefully — it usually contains a concrete suggestion (e.g. "
        "'a file named X exists at src/X — retry with that path'). "
        "Apply the suggestion and call the tool again. Only report failure "
        "to the user as a last resort, after at least 2 retry attempts "
        "with different approaches.\n"
        "- If tokensave_search returns no results, that means the query "
        "isn't a symbol name. Switch to read_file (if you have a target "
        "file in mind) or list_directory (to discover one) — don't keep "
        "searching with variations.\n\n"
        "Style:\n"
        "- Keep answers concise. If a question is open-ended, ask a "
        "clarifying follow-up instead of writing a wall of text.\n"
        "- You CANNOT modify files, run commits, or change config. This is "
        "by design. If the user asks you to make changes, suggest the "
        "specific edits in your answer and let them apply them manually."
    )

    def __init__(self, notebook: ttk.Notebook, get_project_path):
        self._get_project_path = get_project_path
        self._ask_path: str | None = None
        self._ask_messages: list = []
        self._ask_stop_event: threading.Event | None = None
        self._ask_thread: threading.Thread | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  🤖 Ask  ")
        self._build()

    def _build(self):
        tab = self._tab

        # ── Header: project + model + clear ─────────────────────────────
        hdr = tk.Frame(tab, bg=C["base"], padx=14, pady=8)
        hdr.pack(fill=tk.X, side=tk.TOP)

        tk.Label(hdr, text="🤖  Ask",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        self._ask_project_lbl = tk.Label(
            hdr, text="(no project selected)",
            font=("Segoe UI", 10), bg=C["base"], fg=C["text"])
        self._ask_project_lbl.pack(side=tk.LEFT, padx=(10, 0))
        self._ask_model_lbl = tk.Label(
            hdr, text="", font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["overlay0"])
        self._ask_model_lbl.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Button(hdr, text="Clear history",
                   command=self._ask_clear).pack(side=tk.RIGHT)

        # ── Status line (under header) ──────────────────────────────────
        self._ask_status = tk.Label(
            tab, text="", font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W)
        self._ask_status.pack(fill=tk.X, padx=18, pady=(0, 4))

        # ── Input row (BOTTOM, packed before chat log so it stays put) ──
        # NOTE: the entry MUST have a visible border + contrasting bg or
        # it disappears against the parent frame.  Earlier version used
        # bg=mantle (#181825) on a base (#1e1e2e) parent with
        # relief=tk.FLAT — visually identical, so the field appeared
        # missing entirely.  Now uses surface0 (#313244) which is two
        # luminance steps lighter than base, plus a 1px SOLID border and
        # a 2px highlight ring that turns blue on focus.
        in_row = tk.Frame(tab, bg=C["base"], padx=14, pady=8)
        in_row.pack(fill=tk.X, side=tk.BOTTOM)

        self._ask_entry = tk.Entry(
            in_row, font=("Segoe UI", 10),
            bg=C["surface0"], fg=C["text"],
            insertbackground=C["text"],
            relief=tk.SOLID, bd=1,
            highlightthickness=2,
            highlightbackground=C["overlay0"],
            highlightcolor=C["blue"],
            width=40)
        self._ask_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                              ipady=6, padx=(0, 6))
        self._ask_entry.bind("<Return>", lambda e: self._ask_send())
        self._ask_entry.focus_set()

        self._ask_send_btn = ttk.Button(
            in_row, text="Send", style="Primary.TButton",
            command=self._ask_send)
        self._ask_send_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._ask_stop_btn = ttk.Button(
            in_row, text="■ Stop", style="Danger.TButton",
            command=self._ask_stop, state=tk.DISABLED)
        self._ask_stop_btn.pack(side=tk.LEFT)

        # ── Chat log (fills remaining space) ────────────────────────────
        log_outer = tk.Frame(tab, bg=C["base"])
        log_outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 4))
        log_inner = tk.Frame(log_outer, bg=C["mantle"])
        log_inner.pack(fill=tk.BOTH, expand=True)

        self._ask_log = tk.Text(
            log_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Segoe UI", 10),
            padx=10, pady=8, wrap=tk.WORD, state=tk.DISABLED)
        ask_vsb = ttk.Scrollbar(log_inner, orient="vertical",
                                 command=self._ask_log.yview)
        self._ask_log.configure(yscrollcommand=ask_vsb.set)
        self._ask_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ask_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._ask_log.tag_configure(
            "user",        foreground=C["blue"],
            font=("Segoe UI", 10, "bold"), spacing1=8, spacing3=2)
        self._ask_log.tag_configure(
            "assistant",   foreground=C["text"],
            spacing1=4, spacing3=4)
        self._ask_log.tag_configure(
            "tool_call",   foreground=C["peach"],
            font=("Consolas", 9), spacing1=4)
        self._ask_log.tag_configure(
            "tool_result", foreground=C["overlay0"],
            font=("Consolas", 9), lmargin1=20, lmargin2=20)
        self._ask_log.tag_configure(
            "error",       foreground=C["red"],
            font=("Segoe UI", 9, "italic"), spacing1=4)
        self._ask_log.tag_configure(
            "info",        foreground=C["overlay0"],
            font=("Segoe UI", 9, "italic"))

        self._ask_set_intro()

    def on_tab_selected(self):
        """Called by App._on_tab_changed when the Ask tab is focused."""
        path = self._get_project_path()
        if path:
            self._ask_path = path
        self._ask_refresh_header()
        try:
            self._ask_entry.focus_set()
        except tk.TclError:
            pass

    def _ask_set_intro(self):
        self._ask_log.configure(state=tk.NORMAL)
        self._ask_log.delete("1.0", tk.END)
        self._ask_log.insert(tk.END,
            "Ready. Ask anything about the selected project — I'll use "
            "read_file, list_directory, git_log, git_diff, and (when "
            "available) tokensave_search / tokensave_context to find "
            "answers. I cannot modify files.\n\n",
            "info")
        self._ask_log.configure(state=tk.DISABLED)

    def _ask_append(self, text: str, tag: str = "assistant"):
        if not text:
            return
        self._ask_log.configure(state=tk.NORMAL)
        self._ask_log.insert(tk.END, text, tag)
        self._ask_log.see(tk.END)
        self._ask_log.configure(state=tk.DISABLED)

    def _ask_refresh_header(self):
        if self._ask_path:
            self._ask_project_lbl.configure(
                text=os.path.basename(self._ask_path))
        else:
            self._ask_project_lbl.configure(text="(no project selected)")
        cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
        provider = cfg.get("provider") or "?"
        model = cfg.get("model") or "?"
        enabled = bool(cfg.get("enabled"))
        if enabled:
            self._ask_model_lbl.configure(
                text=f"[provider: {provider} / {model}]",
                fg=C["overlay0"])
        else:
            self._ask_model_lbl.configure(
                text="[AI is disabled — Settings → AI commit messages]",
                fg=C["red"])

    def _ask_clear(self):
        if self._ask_thread and self._ask_thread.is_alive():
            self._ask_stop()
        self._ask_messages = []
        self._ask_set_intro()
        self._ask_status.configure(text="")

    def _ask_stop(self):
        """Signal the agent thread to abort; in-flight HTTP request finishes
        but its result is discarded."""
        if self._ask_stop_event is not None:
            self._ask_stop_event.set()
        self._ask_status.configure(
            text="Cancelling — in-flight request will finish then stop.",
            fg=C["overlay0"])
        self._ask_stop_btn.configure(state=tk.DISABLED)

    def _ask_send(self):
        text = self._ask_entry.get().strip()
        if not text:
            return
        if self._ask_thread and self._ask_thread.is_alive():
            self._ask_status.configure(
                text="A request is already running — click Stop first.",
                fg=C["yellow"])
            return

        path = self._get_project_path()
        if path:
            self._ask_path = path
        if not self._ask_path:
            self._ask_append(
                "Select a project in the Projects tab first.\n\n", "error")
            return

        llm_cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
        if not llm_cfg.get("enabled"):
            self._ask_append(
                "AI is disabled. Open Settings → AI commit messages and "
                "tick the enable box, then try again.\n\n", "error")
            return

        try:
            import agent as _agent_mod
            import agent_tools as _agent_tools_mod
        except ImportError as e:
            self._ask_append(
                f"Could not import agent module: {e}\n", "error")
            return

        self._ask_refresh_header()
        self._ask_append(f"\n👤  {text}\n\n", "user")
        self._ask_entry.delete(0, tk.END)
        self._ask_status.configure(
            text="⟳  Thinking…  (the model may call tools before answering)",
            fg=C["peach"])
        self._ask_send_btn.configure(state=tk.DISABLED)
        self._ask_stop_btn.configure(state=tk.NORMAL)

        if not self._ask_messages:
            self._ask_messages.append({
                "role": "system",
                "content": self._ASK_SYSTEM_PROMPT,
            })
        self._ask_messages.append({"role": "user", "content": text})

        stop_event = threading.Event()
        self._ask_stop_event = stop_event

        tokensave_exe = (_cfg.get("tokensave_exe") or "") if isinstance(_cfg, dict) else ""
        tools = _agent_tools_mod.build_tools(self._ask_path, tokensave_exe)
        agent_instance = _agent_mod.LocalAgent(llm_cfg, self._ask_path, tools)

        def _on_tool_call(name, args):
            short_args = json.dumps(args, ensure_ascii=False)
            if len(short_args) > 120:
                short_args = short_args[:120] + "…"
            self._tab.after(0, self._ask_append,
                            f"🔧  {name}({short_args})\n", "tool_call")

        def _on_tool_result(name, result):
            preview = result if len(result) <= 600 else (
                result[:600] + f"\n[... {len(result)-600} more chars ...]")
            self._tab.after(0, self._ask_append, preview + "\n\n", "tool_result")

        def _on_assistant_message(text):
            self._tab.after(0, self._ask_append, f"🤖  {text}\n\n", "assistant")

        def _on_done(final_text):
            def _ui():
                if final_text is None:
                    self._ask_status.configure(
                        text="✓  Done (no final answer text — model only "
                             "issued tool calls).",
                        fg=C["green"])
                else:
                    self._ask_status.configure(text="✓  Done.", fg=C["green"])
                self._ask_send_btn.configure(state=tk.NORMAL)
                self._ask_stop_btn.configure(state=tk.DISABLED)
                self._ask_stop_event = None
            self._tab.after(0, _ui)

        def _on_error(msg):
            def _ui():
                self._ask_append(f"⚠  {msg}\n\n", "error")
                self._ask_status.configure(text="✗  Error.", fg=C["red"])
                self._ask_send_btn.configure(state=tk.NORMAL)
                self._ask_stop_btn.configure(state=tk.DISABLED)
                self._ask_stop_event = None
            self._tab.after(0, _ui)

        def _worker():
            try:
                agent_instance.run(
                    self._ask_messages,
                    on_tool_call=_on_tool_call,
                    on_tool_result=_on_tool_result,
                    on_assistant_message=_on_assistant_message,
                    on_done=_on_done,
                    on_error=_on_error,
                    stop_event=stop_event,
                )
            except Exception as e:
                log.exception("Ask worker crashed")
                try:
                    self._tab.after(0, _on_error, f"{type(e).__name__}: {e}")
                except RuntimeError:
                    pass

        self._ask_thread = threading.Thread(
            target=_worker, daemon=True, name="ask-agent-worker")
        self._ask_thread.start()


# ── Reference / Snippets tab controller ────────────────────────────────────────

class SnippetsController:
    """Owns the Reference/Snippets tab UI."""

    def __init__(self, notebook: ttk.Notebook):
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Reference  ")
        self._build()

    def _build(self):
        tab = self._tab

        # ── Top: CLI cheatsheet ───────────────────────────────────────────────
        tk.Label(tab, text="CLI COMMANDS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=14, pady=(10, 4))

        cli_wrap = tk.Frame(tab, bg=C["mantle"])
        cli_wrap.pack(fill=tk.X, padx=14)

        cli_sb = ttk.Scrollbar(cli_wrap, orient="vertical")
        cli_txt = tk.Text(cli_wrap, height=9, font=("Consolas", 9),
                          bg=C["mantle"], fg=C["text"], relief=tk.FLAT,
                          padx=12, pady=8, wrap=tk.NONE,
                          cursor="arrow", state=tk.NORMAL,
                          yscrollcommand=cli_sb.set)
        cli_sb.configure(command=cli_txt.yview)
        cli_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cli_sb.pack(side=tk.RIGHT, fill=tk.Y)

        cli_txt.tag_configure("hd",  font=("Segoe UI", 9, "bold"), foreground=C["blue"],   spacing1=8, spacing3=2)
        cli_txt.tag_configure("cmd", font=("Consolas", 9),          foreground=C["peach"],  spacing3=1)
        cli_txt.tag_configure("dim", font=("Consolas", 9),          foreground=C["overlay0"])

        def cli_row(cmd, desc):
            cli_txt.insert(tk.END, f"  {cmd:<38}", "cmd")
            cli_txt.insert(tk.END, f"{desc}\n", "dim")

        def cli_h(t):
            cli_txt.insert(tk.END, f"\n  {t}\n", "hd")

        cli_h("Daily use")
        cli_row("tokensave sync",               "Incremental re-index (fast)")
        cli_row("tokensave sync --force",        "Full re-index from scratch")
        cli_row("tokensave sync --doctor",       "Sync and list what changed")
        cli_row("tokensave status",              "Stats + estimated token savings")
        cli_row("tokensave status --details",    "Stats with node-kind breakdown")
        cli_row("tokensave files",               "List all indexed files")
        cli_row("tokensave monitor",             "Live TUI of MCP tool calls")
        cli_h("Setup")
        cli_row("tokensave init",                "First-time index of a project")
        cli_row("tokensave install --agent claude", "Wire up Claude Code integration")
        cli_row("tokensave daemon",              "Auto-sync daemon (foreground)")
        cli_row("tokensave daemon --enable-autostart", "Install daemon as a service")
        cli_h("Troubleshooting")
        cli_row("tokensave doctor",              "Health check — diagnose issues")
        cli_row("tokensave upgrade",             "Self-update to latest version")
        cli_h("Cost & token tracking")
        cli_row("tokensave cost",                "7-day cost summary")
        cli_row("tokensave cost today",          "Today's spend only")
        cli_row("tokensave cost --by-model",     "Breakdown by Claude model")
        cli_h("Branches")
        cli_row("tokensave branch add",          "Track current git branch")
        cli_row("tokensave branch list",         "View tracked branches + DB sizes")
        cli_row("tokensave branch gc",           "Clean up deleted branches")

        cli_txt.configure(state=tk.DISABLED)

        # ── Bottom: Claude prompt snippets ────────────────────────────────────
        snippets_header = tk.Frame(tab, bg=C["base"])
        snippets_header.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(snippets_header, text="CLAUDE PROMPT SNIPPETS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(snippets_header, text="📖  Open Full Guide",
                   command=self._open_guide).pack(side=tk.RIGHT)

        snippets_frame = tk.Frame(tab, bg=C["base"])
        snippets_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        list_wrap = tk.Frame(snippets_frame, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self.snippet_lb = tk.Listbox(
            list_wrap, width=26, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        list_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                                command=self.snippet_lb.yview)
        self.snippet_lb.configure(yscrollcommand=list_sb.set)
        self.snippet_lb.pack(side=tk.LEFT, fill=tk.Y)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = tk.Frame(snippets_frame, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    def _refresh_snippet_list(self, reselect_index=None):
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        for title, text in PROMPT_SNIPPETS:
            self.snippet_lb.insert(tk.END, f"  {title}")
            self._active_snippets_map.append({"type": "builtin", "data": {"title": title, "text": text}})

        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        for idx, u in enumerate(_cfg.get("user_snippets", [])):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            self.snippet_lb.selection_clear(0, tk.END)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            return

        text = meta["data"]["text"]
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        is_user = (meta["type"] == "user")
        self._edit_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return
        self._tab.clipboard_clear()
        self._tab.clipboard_append(meta["data"]["text"])
        self._copy_status.configure(text="✔ Copied!")
        self._tab.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self._tab, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        SnippetEditDialog(self._tab, meta, self._on_snippet_saved)

    def _delete_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Delete snippet",
            f"Delete '{title}'?\n\nThis cannot be undone.",
            parent=self._tab,
        ):
            return
        user_snippets = _cfg.get("user_snippets", [])
        idx = meta["index"]
        del user_snippets[idx]
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog — save and refresh."""
        user_snippets = _cfg.get("user_snippets", [])
        if edit_meta is None:
            user_snippets.append({"title": title, "text": text})
            new_idx = len(PROMPT_SNIPPETS) + 1 + len(user_snippets) - 1
        else:
            idx = edit_meta["index"]
            user_snippets[idx] = {"title": title, "text": text}
            new_idx = len(PROMPT_SNIPPETS) + 1 + idx
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list(reselect_index=new_idx)

    def _open_guide(self):
        guide = os.path.join(_BASE_DIR, "TOKENSAVE_GUIDE.md")
        if os.path.isfile(guide):
            os.startfile(guide)
        else:
            messagebox.showerror("Not found",
                f"Guide not found at:\n{guide}", parent=self._tab)


# ── GitTabController ──────────────────────────────────────────────────────────

class GitTabController:
    """Owns the Git tab UI and all git operations.

    Decoupled from App via four explicit callbacks. No back-reference to App.
    Worker threads push log messages to _log_queue; _poll_log_queue drains it
    on the main thread every 100 ms so Tkinter is never touched from a thread.
    """

    def __init__(
        self,
        notebook: "ttk.Notebook",
        cfg: dict,
        get_path: "Callable[[], str | None]",
        on_log: "Callable[[str, str | None], None]",
        on_shell: "Callable[..., tuple[str, int]]",
        on_commit: "Callable[[str], None]",
    ):
        self._notebook           = notebook
        self._cfg                = cfg
        self._get_path           = get_path
        self._on_log             = on_log
        self._on_shell           = on_shell
        self._on_commit          = on_commit
        self._git_path: "str | None"   = None
        self._git_status_files: list   = []
        self._git_all_btns: list       = []
        self._git_push_pull_btns: list = []
        self._git_release_btns: list   = []
        self._git_op_in_flight: bool   = False
        self._log_queue                = queue.Queue()
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Git  ")
        self._build_git_tab()
        self._poll_log_queue()

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_visible(self) -> bool:
        return self._git_tab_is_visible()

    def refresh(self) -> None:
        self._git_refresh()

    def set_active_path(self, path: str) -> None:
        self._git_path = path

    def has_path(self) -> bool:
        return bool(self._git_path)

    # ── Log queue ─────────────────────────────────────────────────────────────

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg, color = self._log_queue.get_nowait()
                try:
                    self._on_log(msg, color)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._tab.after(100, self._poll_log_queue)

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_git_tab(self):
        """Build the Git tab — shows live git state for the selected project."""
        self._build_git_header()
        mid = tk.Frame(self._tab, bg=C["base"], padx=14, pady=10)
        mid.pack(fill=tk.X)
        mid.columnconfigure(0, weight=1, minsize=200)
        mid.columnconfigure(1, weight=1, minsize=200)
        self._build_git_status_pane(mid)
        self._build_git_action_bar()
        self._build_git_diff_pane()

    def _build_git_header(self) -> None:
        hdr = tk.Frame(self._tab, bg=C["mantle"], padx=14, pady=8)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=C["mantle"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(left,
            text="OPERATING ON",
            font=("Segoe UI", 7, "bold"), bg=C["mantle"], fg=C["overlay0"]
            ).pack(anchor=tk.W)
        self._git_project_lbl = tk.Label(left,
            text="Select a project in the Projects tab",
            font=("Segoe UI", 13, "bold"), bg=C["mantle"], fg=C["blue"])
        self._git_project_lbl.pack(anchor=tk.W)

        info_row = tk.Frame(left, bg=C["mantle"])
        info_row.pack(anchor=tk.W, pady=(2, 0))

        self._git_branch_lbl = tk.Label(info_row,
            text="Branch: —", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"])
        self._git_branch_lbl.pack(side=tk.LEFT, padx=(0, 20))

        self._git_remote_lbl = tk.Label(info_row,
            text="Remote: No remote set", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["overlay0"])
        self._git_remote_lbl.pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=C["mantle"])
        right.pack(side=tk.RIGHT, anchor=tk.N)

        self._btn_set_remote = ttk.Button(right, text="Set Remote",
                                          command=self.cmd_git_set_remote)
        self._btn_set_remote.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(self._btn_set_remote,
            "Connect this project to a GitHub repository.\n"
            "Paste the HTTPS URL from github.com/new.\n"
            "Required before you can Push or Pull.")

        btn_github = ttk.Button(right, text="🐙  GitHub…",
                                command=self.cmd_github_setup)
        btn_github.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_github,
            "Open the GitHub Setup wizard — walks you through creating\n"
            "a GitHub account, connecting this project, pushing your code,\n"
            "and publishing a Release with your built .exe file.")

        btn_refresh = ttk.Button(right, text="⟳  Refresh",
                                 command=self._git_refresh)
        btn_refresh.pack(side=tk.LEFT)
        _Tooltip(btn_refresh, "Re-check the project's current git state and update this tab.")

    def _build_git_status_pane(self, mid: tk.Frame) -> None:
        tk.Label(mid, text="WORKING TREE",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=0, sticky=tk.W, pady=(0, 4))

        tk.Label(mid, text="RECENT COMMITS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))

        status_wrap = tk.Frame(mid, bg=C["mantle"])
        status_wrap.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 4))

        status_vsb = ttk.Scrollbar(status_wrap, orient="vertical")
        self._git_status_lb = tk.Listbox(
            status_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            selectbackground=C["surface1"],
            activestyle="none",
            relief=tk.FLAT, bd=0,
            yscrollcommand=status_vsb.set)
        status_vsb.configure(command=self._git_status_lb.yview)
        self._git_status_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                  padx=6, pady=4)
        status_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._git_status_lb.bind("<<ListboxSelect>>", self._on_git_status_select)

        log_wrap = tk.Frame(mid, bg=C["mantle"])
        log_wrap.grid(row=1, column=1, sticky=tk.NSEW, padx=(4, 0))

        log_vsb = ttk.Scrollbar(log_wrap, orient="vertical")
        self._git_log_txt = tk.Text(
            log_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=log_vsb.set)
        log_vsb.configure(command=self._git_log_txt.yview)
        self._git_log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_git_action_bar(self) -> None:
        acts = tk.Frame(self._tab, bg=C["base"])
        acts.pack(fill=tk.X, padx=14, pady=(6, 4))

        row1 = tk.Frame(acts, bg=C["base"])
        row1.pack(anchor=tk.W, pady=(0, 4))
        row2 = tk.Frame(acts, bg=C["base"])
        row2.pack(anchor=tk.W)

        btn_push    = ttk.Button(row1, text="⬆  Push",
                                 command=self.cmd_git_push)
        btn_pull    = ttk.Button(row1, text="⬇  Pull",
                                 command=self.cmd_git_pull)
        btn_commit  = ttk.Button(row1, text="📝  Commit…",
                                 command=lambda: self._on_commit(self._git_path)
                                         if self._git_path else None)
        btn_undo    = ttk.Button(row1, text="↩  Undo Last Commit",
                                 command=self.cmd_git_undo_commit)
        btn_new     = ttk.Button(row2, text="🌿  New Branch",
                                 command=self.cmd_git_new_branch)
        btn_switch  = ttk.Button(row2, text="🔀  Switch Branch…",
                                 command=self.cmd_git_switch_branch)
        btn_merge   = ttk.Button(row2, text="⇄  Merge…",
                                 command=self.cmd_git_merge)
        btn_del     = ttk.Button(row2, text="🗑  Delete Branch…",
                                 command=self.cmd_git_delete_branch)
        btn_openpr  = ttk.Button(row2, text="🔗  Open PR",
                                 command=self.cmd_git_open_pr)
        btn_mergepr = ttk.Button(row2, text="🐙  Merge PR…",
                                 command=self.cmd_git_merge_pr)
        btn_release = ttk.Button(row2, text="📦  Release…",
                                 command=self.cmd_git_release)

        for btn in (btn_push, btn_pull, btn_commit, btn_undo,
                    btn_new, btn_switch, btn_merge, btn_del, btn_openpr,
                    btn_mergepr, btn_release):
            btn.pack(side=tk.LEFT, padx=(0, 6))

        _Tooltip(btn_push,
            "Send your saved commits to GitHub.\n"
            "Like uploading a backup — your work is now safe online\n"
            "and others can see it.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_pull,
            "Download any new commits from GitHub to this machine.\n"
            "Use this if you made changes on another computer,\n"
            "or if a collaborator pushed new work.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_commit,
            "Save a snapshot of your current changes.\n"
            "Like a save point in a game — you can always come back here.\n\n"
            "You'll write a short message describing what you changed.\n"
            "A suggestion is generated automatically from the file list.")
        _Tooltip(btn_undo,
            "Remove the most recent save point, but keep all your changes.\n"
            "Nothing is deleted — your edits stay exactly as they were.\n\n"
            "Useful if you committed too early or with the wrong message.")
        _Tooltip(btn_new,
            "Create a separate copy of the project to try out an idea.\n"
            "Changes on this branch won't touch your main code\n"
            "until you're ready to merge them in.")
        _Tooltip(btn_switch,
            "Jump to a different branch (version) of the project.\n"
            "For example: switch from an experiment back to 'master'.\n\n"
            "Tip: commit your changes first — switching with\n"
            "unsaved edits will fail.")
        _Tooltip(btn_merge,
            "Merge another branch INTO the branch you're currently on.\n"
            "Use this to bring a finished feature branch back into master.\n\n"
            "Typical workflow: switch to master → pull → merge your feature →\n"
            "push → delete the feature branch.\n\n"
            "Conflicts (if any) must be resolved manually in your editor.")
        _Tooltip(btn_del,
            "Delete a branch you no longer need.\n"
            "Safe by default — warns you if the branch has changes\n"
            "that haven't been saved back to the main branch yet.\n"
            "After local delete, offers to also delete it from GitHub\n"
            "if a remote copy exists.")
        _Tooltip(btn_openpr,
            "Open a Pull Request on GitHub for the current branch.\n\n"
            "A Pull Request is a way to say: 'I made some changes on a\n"
            "separate branch — please review them and merge into main.'\n\n"
            "On master/main: shows you how to create a branch first.\n"
            "On any other branch: opens GitHub's compare page directly.\n\n"
            "Requires a GitHub remote and the branch to be pushed first.")
        _Tooltip(btn_mergepr,
            "Merge an open Pull Request from GitHub.\n\n"
            "Lists every open PR on this repo with its +X/-Y diff size,\n"
            "then lets you pick one and choose a merge strategy:\n"
            "  • Merge commit  — preserves the PR's branch history\n"
            "  • Squash and merge — collapses to a single commit\n"
            "  • Rebase and merge — replays commits linearly\n\n"
            "Shows a confirmation with the title and strategy before\n"
            "doing anything. After a successful merge, the PR is closed\n"
            "and (optionally) its branch is deleted on GitHub.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")
        _Tooltip(btn_release,
            "One-button GitHub release.\n\n"
            "Opens a wizard that auto-drafts release notes from your\n"
            "commits, builds the project, zips dist/, tags locally,\n"
            "pushes, and publishes via `gh release create` — all in one\n"
            "threaded worker. Editable textarea so you can polish the\n"
            "notes before publishing.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")

        self._git_all_btns       = [self._btn_set_remote, btn_push, btn_pull,
                                     btn_commit, btn_undo, btn_new,
                                     btn_switch, btn_merge, btn_del, btn_openpr,
                                     btn_mergepr, btn_release]
        self._git_push_pull_btns = [btn_push, btn_pull, btn_openpr]
        self._git_release_btns   = [btn_release, btn_mergepr]

        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _build_git_diff_pane(self) -> None:
        tk.Label(self._tab, text="DIFF  (click a file above to preview)",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(
                     anchor=tk.W, padx=14, pady=(4, 4))

        diff_wrap = tk.Frame(self._tab, bg=C["mantle"])
        diff_wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        diff_vsb = ttk.Scrollbar(diff_wrap, orient="vertical")
        diff_hsb = ttk.Scrollbar(diff_wrap, orient="horizontal")
        self._git_diff_txt = tk.Text(
            diff_wrap,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=diff_vsb.set,
            xscrollcommand=diff_hsb.set)
        diff_vsb.configure(command=self._git_diff_txt.yview)
        diff_hsb.configure(command=self._git_diff_txt.xview)
        self._git_diff_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        diff_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        diff_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self._git_diff_txt.tag_configure("plus",   foreground=C["green"])
        self._git_diff_txt.tag_configure("minus",  foreground=C["red"])
        self._git_diff_txt.tag_configure("header", foreground=C["blue"])
        self._git_diff_txt.tag_configure("meta",   foreground=C["overlay0"])

    # ── Git tab data methods ──────────────────────────────────────────────────

    def _git_tab_is_visible(self) -> bool:
        try:
            return self._notebook.tab(self._notebook.select(), "text").strip() == "Git"
        except (tk.TclError, AttributeError):
            return False

    def _git_refresh(self):
        """Kick off a background thread that re-reads all git state."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            branch_out, brc = self._on_shell(
                [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
            is_repo = brc == 0
            branch  = branch_out.strip() if is_repo else "—"

            remote_out, rrc = self._on_shell(
                [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
            remote = remote_out.strip() if rrc == 0 else ""

            status_out, _ = self._on_shell(
                [GIT_EXE, "-C", path, "status", "--short"], path)

            log_out, lrc = self._on_shell(
                [GIT_EXE, "-C", path, "log", "--oneline", "-15"], path)
            log_text = log_out.strip() if lrc == 0 else ""

            self._tab.after(0, lambda: self._git_update_ui(
                path, name, is_repo, branch, remote, status_out, log_text))

        threading.Thread(target=worker, daemon=True).start()

    def _git_begin_op(self):
        """Mark a git operation as in flight and disable all Git tab buttons."""
        self._git_op_in_flight = True
        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _git_end_op(self):
        """Clear the in-flight flag and refresh the Git tab to re-enable buttons."""
        self._git_op_in_flight = False
        self._git_refresh()

    def _git_update_ui(self, path, name, is_repo, branch, remote,
                       status_raw, log_text):
        """Main-thread update of all Git tab widgets."""
        self._git_project_lbl.config(text=name)

        if is_repo:
            self._git_branch_lbl.config(text=f"Branch:  {branch}",
                                         fg=C["text"])
        else:
            self._git_branch_lbl.config(
                text="Not a git repository — right-click project → 🔧 Git Init",
                fg=C["peach"])

        if remote:
            disp = remote.replace("https://", "").replace("http://", "")
            disp = disp.rstrip("/")
            if disp.endswith(".git"):
                disp = disp[:-4]
            self._git_remote_lbl.config(text=f"Remote:  {disp}",
                                         fg=C["overlay0"])
        else:
            self._git_remote_lbl.config(text="Remote:  No remote set",
                                         fg=C["overlay0"])

        if self._git_op_in_flight:
            for btn in self._git_all_btns:
                btn.configure(state=tk.DISABLED)
        else:
            repo_state = tk.NORMAL if is_repo else tk.DISABLED
            for btn in self._git_all_btns:
                btn.configure(state=repo_state)
            push_pull_state = tk.NORMAL if (is_repo and remote) else tk.DISABLED
            for btn in self._git_push_pull_btns:
                btn.configure(state=push_pull_state)
            release_ok = bool(is_repo and remote and shutil.which("gh"))
            for btn in self._git_release_btns:
                btn.configure(state=tk.NORMAL if release_ok else tk.DISABLED)

        self._git_status_lb.configure(state=tk.NORMAL)
        self._git_status_lb.delete(0, tk.END)
        self._git_status_files = []
        if is_repo:
            # BUG FIX: do NOT call status_raw.strip() before splitlines() —
            # status lines for working-tree changes start with a leading space
            # (" M file.py"), and strip() eats that leading space from the
            # FIRST line only, shifting column 0..1 (status) and 3+ (path) so
            # the path silently loses its first character. Use splitlines()
            # directly and skip blanks individually.
            has_lines = False
            for line in status_raw.splitlines():
                if len(line) < 4:
                    continue
                has_lines = True
                xy    = line[:2]
                fname = line[3:]
                self._git_status_lb.insert(tk.END, f"  {xy}  {fname}")
                self._git_status_files.append((xy.strip(), fname))
            if not has_lines:
                self._git_status_lb.insert(tk.END, "  (working tree clean)")

        self._git_log_txt.configure(state=tk.NORMAL)
        self._git_log_txt.delete("1.0", tk.END)
        if is_repo:
            self._git_log_txt.insert(tk.END,
                log_text if log_text else "(no commits yet)")
        self._git_log_txt.configure(state=tk.DISABLED)

        self._git_diff_txt.configure(state=tk.NORMAL)
        self._git_diff_txt.delete("1.0", tk.END)
        if not is_repo:
            self._git_diff_txt.insert(tk.END,
                "This project has no git history.\n\n"
                "Right-click the project in the Projects tab\n"
                "→ 🔧 Git Init to set one up.")
        self._git_diff_txt.configure(state=tk.DISABLED)

    def _on_git_status_select(self, event=None):
        """Click a file in the status listbox → show its diff below."""
        sel = self._git_status_lb.curselection()
        if not sel or not self._git_status_files:
            return
        idx = sel[0]
        if idx >= len(self._git_status_files):
            return
        _, fname = self._git_status_files[idx]
        path = self._git_path
        if not path:
            return

        def worker():
            d1, _ = self._on_shell(
                [GIT_EXE, "-C", path, "diff", "--", fname], path)
            d2, _ = self._on_shell(
                [GIT_EXE, "-C", path, "diff", "--cached", "--", fname], path)
            combined = "\n".join(filter(None, [d1.strip(), d2.strip()]))
            if not combined:
                combined = f"(no diff available — {fname} may be untracked or binary)"
            self._tab.after(0, lambda d=combined: self._git_show_diff(d))

        threading.Thread(target=worker, daemon=True).start()

    def _git_show_diff(self, diff_text):
        """Render a diff string into the diff Text widget with colour tags."""
        txt = self._git_diff_txt
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        lines = diff_text.splitlines(keepends=True)
        CAP = 2000
        if len(lines) > CAP:
            lines = lines[:CAP]
            lines.append(f"\n… [diff capped at {CAP} lines — file is very large] …\n")

        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                txt.insert(tk.END, line, "plus")
            elif line.startswith("-") and not line.startswith("---"):
                txt.insert(tk.END, line, "minus")
            elif line.startswith("@@"):
                txt.insert(tk.END, line, "header")
            elif line.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file")):
                txt.insert(tk.END, line, "meta")
            else:
                txt.insert(tk.END, line)

        txt.configure(state=tk.DISABLED)

    # ── Git action commands ────────────────────────────────────────────────────

    def cmd_git_push(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pushing…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "push", "-u", "origin", "HEAD"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0 and _is_auth_error(out):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git push\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_pull(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pulling…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "pull"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0:
                    if _is_auth_error(out):
                        self._tab.after(0, lambda: messagebox.showinfo(
                            "GitHub Authentication Required",
                            "GitHub needs to verify your identity.\n\n"
                            "Open a terminal in this project folder and run:\n"
                            "    git pull\n\n"
                            "A browser window will open asking you to log in to GitHub.\n"
                            "After that, this button will work normally.",
                            parent=self._root))
                    elif "conflict" in out.lower():
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            "Pull completed but there are merge conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<<).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.",
                            parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_open_pr(self):
        """Open a pull-request comparison page on GitHub for the current branch."""
        path = self._git_path
        if not path:
            return

        branch_out, brc = self._on_shell(
            [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)

        branch = branch_out.strip() if brc == 0 else ""
        remote = remote_out.strip() if rrc == 0 else ""

        if not remote:
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        base = remote.rstrip("/").removesuffix(".git")
        if base.startswith("git@github.com:"):
            base = "https://github.com/" + base[len("git@github.com:"):]

        is_main = branch in ("master", "main", "")

        if is_main:
            go = messagebox.askyesno(
                "You're on the main branch",
                f"You're on '{branch}' — the main/default branch.\n\n"
                "Pull Requests work like this:\n\n"
                "  1. 🌿 New Branch  →  give it a name (e.g. 'my-feature')\n"
                "  2. Make your changes, then 📝 Commit\n"
                "  3. ⬆ Push  →  sends the branch to GitHub\n"
                "  4. 🔗 Open PR  →  GitHub shows a 'Compare & pull request' button\n"
                "  5. Fill in the description and click 'Create pull request'\n\n"
                "Open the repository page on GitHub now?",
                parent=self._root)
            if go:
                os.startfile(base)
        else:
            pr_url = f"{base}/compare/{branch}"
            self._on_log(
                f"  [{os.path.basename(path)}] Opening PR page for branch '{branch}'…",
                C["peach"])
            os.startfile(pr_url)

    def cmd_git_merge_pr(self):
        """List open PRs on this repo's GitHub origin and let the user pick one to merge."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        self._on_log(f"$ gh pr list  [{name}]", C["blue"])
        self._git_begin_op()

        def worker():
            try:
                r = subprocess.run(
                    ["gh", "pr", "list", "--state", "open",
                     "--json", "number,title,headRefName,baseRefName,"
                               "additions,deletions,author,url",
                     "--limit", "50"],
                    cwd=path, capture_output=True, text=True,
                    timeout=20, creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    err = (r.stderr or r.stdout or "").strip()
                    self._log_queue.put((f"  ✗ gh pr list failed: {err[:400]}", C["red"]))
                    return
                try:
                    prs = json.loads(r.stdout or "[]")
                except json.JSONDecodeError as e:
                    self._log_queue.put((f"  ✗ Could not parse gh output: {e}", C["red"]))
                    return
                if not prs:
                    self._log_queue.put(("  No open PRs on this repo.", C["overlay0"]))
                    return
                self._log_queue.put((
                    f"  Found {len(prs)} open PR(s).  Opening selection dialog…",
                    C["overlay0"]))
                self._tab.after(0, self._show_merge_pr_dialog, path, prs)
            except (OSError, subprocess.TimeoutExpired) as e:
                self._log_queue.put((f"  ✗ gh pr list error: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-list").start()

    def _show_merge_pr_dialog(self, path: str, prs: list):
        """Open the MergePRDialog with the fetched PR list."""
        MergePRDialog(self._root, path, prs, self._do_merge_pr)

    def _do_merge_pr(self, path: str, pr_number: int, strategy: str,
                     delete_branch: bool, pr_title: str):
        """Run `gh pr merge <N> --<strategy>` and stream output."""
        name = os.path.basename(path)
        flag = f"--{strategy}"
        cmd = ["gh", "pr", "merge", str(pr_number), flag]
        if delete_branch:
            cmd.append("--delete-branch")
        self._on_log(
            f"$ gh pr merge {pr_number} {flag}"
            f"{' --delete-branch' if delete_branch else ''}  [{name}]",
            C["blue"])
        self._git_begin_op()

        def worker():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if stripped:
                        self._log_queue.put((stripped, None))
                proc.wait()
                if proc.returncode == 0:
                    self._log_queue.put((
                        f"  ✓ PR #{pr_number} merged ({strategy}).  "
                        f"Pulling master to sync local…",
                        C["green"]))
                    self._tab.after(0, self._post_merge_pr_sync, path)
                else:
                    self._log_queue.put((
                        f"  ✗ gh pr merge exited with code {proc.returncode}",
                        C["red"]))
            except (OSError, FileNotFoundError) as e:
                self._log_queue.put((f"  ✗ Error running gh: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-merge").start()

    def _post_merge_pr_sync(self, path: str):
        """After a successful PR merge, switch to master and pull."""
        name = os.path.basename(path)
        base = "master"
        try:
            r = subprocess.run(
                [GIT_EXE, "-C", path, "symbolic-ref",
                 "refs/remotes/origin/HEAD", "--short"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                head = r.stdout.strip()
                if head.startswith("origin/"):
                    base = head[len("origin/"):]
        except (OSError, subprocess.TimeoutExpired):
            pass

        cur_out, cur_rc = self._on_shell(
            [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        cur = cur_out.strip() if cur_rc == 0 else ""

        def worker():
            self._git_begin_op()
            try:
                if cur and cur != base:
                    self._log_queue.put((f"  Switching to '{base}'…", C["overlay0"]))
                    out, rc = self._on_shell(
                        [GIT_EXE, "-C", path, "switch", base], path)
                    if rc != 0:
                        self._log_queue.put((
                            f"  ⚠ Could not switch to '{base}': "
                            f"{out.strip()[:200]}.  Pull manually after switching.",
                            C["peach"]))
                        return
                self._log_queue.put((f"  Pulling latest '{base}'…", C["overlay0"]))
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "pull", "--ff-only"], path)
                if rc == 0:
                    self._log_queue.put((f"  ✓ Local '{base}' synced.", C["green"]))
                else:
                    self._log_queue.put((
                        f"  ⚠ Pull failed: {out.strip()[:200]}", C["peach"]))
            finally:
                self._tab.after(0, self._git_end_op)
                self._tab.after(0, self._git_refresh)

        threading.Thread(target=worker, daemon=True, name="post-merge-sync").start()

    def cmd_git_release(self):
        """Open the Release Wizard for the currently loaded Git-tab project."""
        path = self._git_path
        if not path:
            return

        if not shutil.which("gh"):
            messagebox.showwarning("GitHub CLI required",
                "The Release Wizard runs `gh release create` under the hood.\n\n"
                "Install GitHub CLI from https://cli.github.com and re-open\n"
                "this dialog.",
                parent=self._root)
            return

        if not _is_local_git_repo(path):
            messagebox.showwarning("Not a git repo",
                f"{os.path.basename(path)} is not a git repository.\n\n"
                "Right-click the project → 🔧 Git Init first.",
                parent=self._root)
            return

        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning("No remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header first.",
                parent=self._root)
            return

        status_out, src = self._on_shell(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if src == 0 and status_out.strip():
            dirty_files = []
            for line in status_out.splitlines():
                if len(line) < 4:
                    continue
                fname = line[3:].strip()
                if fname and fname != "CHANGELOG.md":
                    dirty_files.append(fname)
            if dirty_files:
                preview = "\n".join(f"  • {f}" for f in dirty_files[:6])
                if len(dirty_files) > 6:
                    preview += f"\n  • …and {len(dirty_files) - 6} more"
                choice = messagebox.askyesnocancel(
                    "Working tree has unrelated changes",
                    f"The Release Wizard needs a clean working tree so the\n"
                    f"release-prep commit only contains the version bump.\n\n"
                    f"Uncommitted files:\n{preview}\n\n"
                    f"Yes  → open the Git Commit dialog now\n"
                    f"No   → cancel, deal with it later\n"
                    f"(Stash flow is on the roadmap.)",
                    parent=self._root)
                if choice is None or choice is False:
                    return
                self._on_commit(path)
                return

        ReleaseWizardDialog(self._root, path)

    def cmd_git_undo_commit(self):
        """Undo the last commit, keeping all changes staged."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        if not messagebox.askyesno(
                "Undo Last Commit",
                "Undo the last commit?\n\n"
                "Your changes will be kept and moved back to 'staged'.\n"
                "Nothing is deleted — you can re-commit at any time.",
                parent=self._root):
            return
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "reset", "--soft", "HEAD~1"], path)
                col = C["green"] if rc == 0 else C["red"]
                msg = "Last commit undone — changes are now staged." if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_set_remote(self):
        """Open the Set Remote dialog to connect this project to GitHub."""
        path = self._git_path
        if not path:
            return
        out, rc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        current_url = out.strip() if rc == 0 else ""
        SetRemoteDialog(self._root, path, current_url, self._do_git_set_remote)

    def _do_git_set_remote(self, path: str, url: str):
        """Callback from SetRemoteDialog — add or update the origin remote."""
        self._git_begin_op()

        def worker():
            try:
                _, rc_check = self._on_shell(
                    [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
                if rc_check == 0:
                    cmd = [GIT_EXE, "-C", path, "remote", "set-url", "origin", url]
                else:
                    cmd = [GIT_EXE, "-C", path, "remote", "add", "origin", url]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = "updated" if rc_check == 0 else "added"
                msg = f"Remote {action}: {url}" if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_github_setup(self):
        """Open the GitHub Setup wizard for the selected/current project."""
        path = self._git_path or self._get_path()
        if not path:
            messagebox.showwarning("No project selected",
                "Select a project first.", parent=self._root)
            return
        GitHubSetupDialog(self._root, path)

    def cmd_git_new_branch(self):
        """Open New Branch dialog."""
        path = self._git_path
        if not path:
            return
        NewBranchDialog(self._root, path, self._do_git_new_branch)

    def _do_git_new_branch(self, path: str, name: str, switch: bool):
        self._git_begin_op()

        def worker():
            try:
                if switch:
                    cmd = [GIT_EXE, "-C", path, "checkout", "-b", name]
                else:
                    cmd = [GIT_EXE, "-C", path, "branch", name]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = f"Created and switched to '{name}'" if (switch and rc == 0) \
                         else (f"Created '{name}'" if rc == 0 else out.strip())
                self._log_queue.put((f"  [{os.path.basename(path)}] {action}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_switch_branch(self):
        """Open Switch Branch dialog."""
        path = self._git_path
        if not path:
            return
        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return
        branches = []
        current  = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                branches.append(line.strip())
        SwitchBranchDialog(self._root, path, branches, current,
                           self._do_git_switch_branch)

    def _do_git_switch_branch(self, path: str, name: str):
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "checkout", name], path)
                if rc != 0:
                    self._tab.after(0, lambda: messagebox.showerror(
                        "Switch Failed",
                        "Could not switch branches.\n\n"
                        "You may have uncommitted changes that conflict with the target branch.\n\n"
                        "Please commit or undo your changes before switching.",
                        parent=self._root))
                else:
                    self._log_queue.put((
                        f"  [{os.path.basename(path)}] Switched to branch '{name}'",
                        C["green"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_merge(self):
        """Merge another branch INTO the current branch."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return

        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return
        non_current = []
        current = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                non_current.append(line.strip())
        if not non_current:
            messagebox.showinfo("No Other Branches",
                "There are no other branches to merge from.", parent=self._root)
            return

        proj = os.path.basename(path)
        source = SwitchBranchDialog.pick(self._root,
            f"Merge into {current} — {proj}",
            non_current, parent_widget=self._root)
        if not source:
            return

        if not messagebox.askyesno(
                "Merge Branch",
                f"Merge '{source}' INTO '{current}'?\n\n"
                f"This brings commits from '{source}' into '{current}'.\n"
                "Your working tree must be clean.\n\n"
                "If conflicts occur, resolve them in your editor, then\n"
                "Commit the result.",
                parent=self._root):
            return

        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "merge", "--no-edit", source], path)
                col = C["green"] if rc == 0 else C["red"]
                if rc == 0:
                    self._log_queue.put((
                        f"  [{proj}] Merged '{source}' into '{current}'", col))
                    for line in out.strip().splitlines()[-4:]:
                        self._log_queue.put((f"    {line}", col))
                else:
                    out_l = out.lower()
                    if "conflict" in out_l:
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            f"Merging '{source}' into '{current}' produced conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<< / >>>>>>).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.\n\n"
                            "Or open a terminal in the project folder and run\n"
                            "    git merge --abort\n"
                            "to undo the merge attempt entirely.",
                            parent=self._root))
                    elif "unmerged" in out_l or "your local changes" in out_l:
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Working Tree Not Clean",
                            f"Cannot merge — '{current}' has uncommitted changes.\n\n"
                            "Commit or stash them first, then try again.",
                            parent=self._root))
                    self._log_queue.put((f"  [{proj}] Merge failed", col))
                    for line in out.strip().splitlines()[-4:]:
                        self._log_queue.put((f"    {line}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_delete_branch(self):
        """Delete a non-current branch with safe/force-delete distinction."""
        path = self._git_path
        if not path:
            return
        branch = self._confirm_branch_delete(path)
        if branch is None:
            return
        self._do_delete_branch(path, branch)

    def _confirm_branch_delete(self, path: str) -> "str | None":
        """List non-current branches, prompt for selection, and confirm."""
        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return None
        non_current = [line.strip() for line in out.strip().splitlines()
                       if not line.startswith("* ")]
        if not non_current:
            messagebox.showinfo("No Branches",
                "There are no other branches to delete.", parent=self._root)
            return None
        branch = SwitchBranchDialog.pick(
            self._root, f"Delete Branch — {os.path.basename(path)}",
            non_current, parent_widget=self._root)
        if not branch:
            return None
        if not messagebox.askyesno(
                "Delete Branch",
                f"Delete branch '{branch}'?\n\n"
                "If this branch has been merged, it will be removed safely.",
                parent=self._root):
            return None
        return branch

    def _do_delete_branch(self, path: str, branch: str) -> None:
        """Execute local (and optionally remote) branch deletion on background threads."""
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "branch", "-d", branch], path)
                if rc == 0:
                    self._log_queue.put((
                        f"  [{os.path.basename(path)}] Deleted branch '{branch}'",
                        C["green"]))
                    self._tab.after(0, lambda: offer_remote_delete(branch))
                    return
                out_l = out.lower()
                if "not fully merged" in out_l or "unmerged" in out_l:
                    self._tab.after(0, ask_force)
                else:
                    self._tab.after(0, lambda: messagebox.showerror(
                        "Delete Failed",
                        f"Could not delete branch '{branch}':\n\n{out.strip()}",
                        parent=self._root))
                    self._tab.after(0, self._git_end_op)
            except Exception:
                self._tab.after(0, self._git_end_op)
                raise

        def ask_force():
            if not messagebox.askyesno(
                    "Force Delete?",
                    f"Branch '{branch}' has unmerged changes.\n\n"
                    "Force-delete anyway?\n"
                    "This permanently discards those commits.",
                    parent=self._root):
                self._git_end_op()
                return

            def force_worker():
                try:
                    o2, r2 = self._on_shell(
                        [GIT_EXE, "-C", path, "branch", "-D", branch], path)
                    col = C["green"] if r2 == 0 else C["red"]
                    msg = f"Force-deleted '{branch}'" if r2 == 0 else o2.strip()
                    self._log_queue.put((
                        f"  [{os.path.basename(path)}] {msg}", col))
                    if r2 == 0:
                        self._tab.after(0, lambda: offer_remote_delete(branch))
                        return
                finally:
                    self._tab.after(0, self._git_end_op)

            threading.Thread(target=force_worker, daemon=True).start()

        def offer_remote_delete(deleted_branch: str):
            rbo, rbrc = self._on_shell(
                [GIT_EXE, "-C", path, "branch", "-r"], path)
            has_remote = False
            if rbrc == 0:
                target = f"origin/{deleted_branch}"
                for line in rbo.strip().splitlines():
                    if line.strip().split(" ", 1)[0] == target:
                        has_remote = True
                        break
            if not has_remote:
                self._git_end_op()
                return

            if not messagebox.askyesno(
                    "Delete from GitHub too?",
                    f"'{deleted_branch}' is deleted locally, but a copy still\n"
                    f"exists on GitHub (origin/{deleted_branch}).\n\n"
                    "Also delete it from GitHub?\n"
                    "(This is the same as running\n"
                    f"  git push origin --delete {deleted_branch})",
                    parent=self._root):
                self._git_end_op()
                return

            def remote_worker():
                try:
                    ro, rrc = self._on_shell(
                        [GIT_EXE, "-C", path, "push", "origin", "--delete",
                         deleted_branch],
                        path, env=_GIT_ENV_NO_PROMPT)
                    col = C["green"] if rrc == 0 else C["red"]
                    if rrc == 0:
                        self._log_queue.put((
                            f"  [{os.path.basename(path)}] "
                            f"Deleted 'origin/{deleted_branch}' from GitHub",
                            col))
                    else:
                        self._log_queue.put((
                            f"  [{os.path.basename(path)}] Remote delete failed",
                            col))
                        for line in ro.strip().splitlines()[-4:]:
                            self._log_queue.put((f"    {line}", col))
                        if _is_auth_error(ro):
                            self._tab.after(0, lambda: messagebox.showinfo(
                                "GitHub Authentication Required",
                                "GitHub needs to verify your identity.\n\n"
                                "Open a terminal in the project folder and run:\n"
                                f"    git push origin --delete {deleted_branch}\n\n"
                                "A browser window will open asking you to log in.",
                                parent=self._root))
                finally:
                    self._tab.after(0, self._git_end_op)

            threading.Thread(target=remote_worker, daemon=True).start()

        threading.Thread(target=worker, daemon=True).start()


# ── App ────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TokenSave Manager")
        self.geometry("760x600")
        self.minsize(600, 520)
        self.configure(bg=C["base"])
        self._current_proc = None
        self._stop_requested = False
        # Cached tokensave version info.  Current version is populated at
        # App startup via `tokensave --version` (fast, no network).
        # Available-update version is populated by the output parser in
        # `_run` when tokensave emits "Update available: vA → vB" at the
        # end of a sync — that line is opportunistic (tokensave appears
        # to throttle update checks to once per day), so SettingsDialog
        # ALWAYS shows the Upgrade button with the current version, and
        # only labels it with the target version when one is known.
        self._tokensave_current_version: str | None = None
        self._tokensave_available_version: str | None = None
        self._probe_tokensave_version()
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {TOKENSAVE}")
        log.info(f"  templates: {TEMPLATE_DIR}")
        log.info(f"  log file : {LOG_FILE}")
        self._style()
        self._build()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray = None
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.bind("<Unmap>", self._on_unmap)
        self.after(300, self._check_config)

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.withdraw()
        log.debug("Window hidden to tray")

    def _on_unmap(self, event):
        if event.widget is self:
            self.after(100, self._maybe_hide)

    def _maybe_hide(self):
        if self.state() == "iconic":
            self.withdraw()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None):
        log.info("Quit requested from tray")
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)

    # ── Styles ─────────────────────────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C["base"], foreground=C["text"],
            font=("Segoe UI", 10), borderwidth=0)

        s.configure("Treeview",
            background=C["mantle"], foreground=C["text"],
            fieldbackground=C["mantle"], rowheight=30,
            font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["surface0"], foreground=C["subtext"],
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", C["surface1"])],
            foreground=[("selected", C["text"])])

        s.configure("TButton",
            background=C["surface0"], foreground=C["text"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("TButton",
            background=[("active", C["surface1"]), ("pressed", C["surface1"])])

        s.configure("Primary.TButton",
            background=C["blue"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Primary.TButton",
            background=[("active", C["lavender"]), ("pressed", C["lavender"])])

        s.configure("Action.TButton",
            background=C["peach"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Action.TButton",
            background=[("active", C["yellow"]), ("pressed", C["yellow"])])

        s.configure("Danger.TButton",
            background=C["surface0"], foreground=C["red"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("Danger.TButton",
            background=[("active", C["surface1"])])

        s.configure("TScrollbar",
            background=C["surface0"], troughcolor=C["mantle"],
            bordercolor=C["base"], arrowcolor=C["overlay0"],
            relief="flat")

        s.configure("TSeparator", background=C["surface0"])

        s.configure("TNotebook",
            background=C["base"], borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=C["surface0"], foreground=C["subtext"],
            padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
            background=[("selected", C["base"])],
            foreground=[("selected", C["blue"])])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=C["mantle"], pady=12, padx=16)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="TokenSave Manager",
                 font=("Segoe UI", 15, "bold"),
                 bg=C["mantle"], fg=C["blue"]).pack(side=tk.LEFT)

        self.active_badge = tk.Label(hdr, text="",
            font=("Segoe UI", 9), bg=C["surface0"],
            fg=C["green"], padx=8, pady=3)
        self.active_badge.pack(side=tk.RIGHT)

        # ── Credit bar ──
        tk.Label(self, text="TokenSave Manager  ·  Alexander L Corthell",
                 font=("Segoe UI", 7), bg=C["crust"], fg=C["overlay0"],
                 pady=2).pack(fill=tk.X, side=tk.BOTTOM)

        # ── Separator + Log — packed BEFORE notebook so expand=True doesn't eat it ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, side=tk.BOTTOM)

        log_frame = tk.Frame(self, bg=C["base"], padx=14, pady=8)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Notebook ──
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._build_projects_tab()
        self._git = GitTabController(
            self.nb, _cfg,
            get_path=self._get_git_path,
            on_log=self._log,
            on_shell=self._shell_capture,
            on_commit=self._open_commit_dialog,
        )
        self._ask_ctrl = AskTabController(self.nb, self._get_ask_project_path)
        self._snippets_ctrl = SnippetsController(self.nb)
        self._build_help_tab()

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        self._stop_btn = ttk.Button(log_header, text="■  Stop",
                                    style="Danger.TButton",
                                    command=self._stop_current,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._running_label = tk.Label(log_header, text="",
                                       font=("Segoe UI", 8),
                                       bg=C["base"], fg=C["yellow"])
        self._running_label.pack(side=tk.RIGHT, padx=(0, 8))

        log_inner = tk.Frame(log_frame, bg=C["mantle"])
        log_inner.pack(fill=tk.X)

        self.log = tk.Text(log_inner, height=4,
            font=("Consolas", 9), bg=C["mantle"], fg=C["green"],
            insertbackground=C["green"], relief=tk.FLAT,
            padx=10, pady=6, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(log_inner, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_projects_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Projects  ")

        # ── Toolbar + hint packed first with side=BOTTOM so they are always
        #    visible — the treeview (expand=True) fills whatever space remains.
        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self.refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self.cmd_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

        # ── Project list — fills remaining space ──
        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "cg", "git", "scaffold"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0",       text="Project")
        self.tree.heading("active",   text="")
        self.tree.heading("path",     text="Path")
        self.tree.heading("synced",   text="Last Synced")
        self.tree.heading("cg",       text="CG")
        self.tree.heading("git",      text="Git")
        self.tree.heading("scaffold", text="Scaffold")

        self.tree.column("#0",       width=170, stretch=False)
        self.tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self.tree.column("path",     width=220)
        self.tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self.tree.column("cg",       width=36,  stretch=False, anchor=tk.CENTER)
        self.tree.column("git",      width=60,  stretch=False, anchor=tk.CENTER)
        self.tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("active",      foreground=C["green"])
        self.tree.tag_configure("normal",      foreground=C["text"])
        self.tree.tag_configure("scaffold",    foreground=C["peach"])
        self.tree.tag_configure("git_only",    foreground=C["overlay0"])
        self.tree.tag_configure("pending",     foreground=C["yellow"])
        # Git status override tags — applied AFTER the baseline tag so they
        # take precedence (tkinter Treeview resolves tag properties from the
        # last matching tag in the tuple). When a row has e.g. tags=("normal",
        # "git_dirty"), the foreground comes from git_dirty.
        self.tree.tag_configure("git_clean",   foreground=C["green"])
        self.tree.tag_configure("git_dirty",   foreground=C["yellow"])
        self.tree.tag_configure("git_ahead",   foreground=C["sky"])
        self.tree.tag_configure("git_behind",  foreground=C["red"])
        self.tree.tag_configure("git_mixed",   foreground=C["peach"])
        self.tree.tag_configure("git_pending", foreground=C["overlay0"])
        self.tree.tag_configure("git_none",    foreground=C["overlay0"])
        self.tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("subcategory", foreground=C["lavender"])

        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_project_select)
        self._build_context_menu()

    # ── Tab / project navigation ────────────────────────────────────────────

    def _on_project_select(self, event=None):
        """Fires when the user clicks a row in the Projects Treeview."""
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("proj:"):
            return
        path = iid[5:]
        self._git.set_active_path(path)
        if self._git.is_visible():
            self._git.refresh()

    def _on_tab_changed(self, event=None):
        """Fires when the user switches notebook tabs."""
        try:
            current_tab_text = self.nb.tab(self.nb.select(), "text").strip()
        except tk.TclError:
            current_tab_text = ""
        if "Ask" in current_tab_text:
            self._ask_ctrl.on_tab_selected()
            return

        if not self._git.is_visible():
            return
        sel = self.tree.selection()
        if sel and sel[0].startswith("proj:"):
            self._git.set_active_path(sel[0][5:])
        elif not self._git.has_path() and self.active_path:
            self._git.set_active_path(self.active_path)
        if self._git.has_path():
            self._git.refresh()

    def _get_ask_project_path(self) -> str | None:
        """Return the currently focused project path for AskTabController."""
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        if sel and sel[0].startswith("proj:"):
            return sel[0][5:]
        return getattr(self, "active_path", None)

    def _get_git_path(self) -> str | None:
        """Return the currently focused project path for GitTabController."""
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        if sel and sel[0].startswith("proj:"):
            return sel[0][5:]
        return getattr(self, "active_path", None)

    # ═══════════════════════════════════════════════════════════════════
    # 🤖 Ask tab — handled by AskTabController (see above App class)
    # 📚 Reference tab — handled by SnippetsController (see above App class)
    # ═══════════════════════════════════════════════════════════════════

    def _build_help_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  Project Categories",  self._help_categories),
            ("  Git: What & Why",     self._help_git_concepts),
            ("  Git: Daily Workflow", self._help_git_workflow),
            ("  Git Tab Buttons",     self._help_git_tab),
            ("  GitHub Setup",        self._help_github_setup),
            ("  CodeGraph",           self._help_codegraph),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    def _help_show(self, fn):
        """Clear the help text widget, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ──────────────────────────────────────────────────────────

    def _help_switching(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_categories(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Project Categories")
            p("Projects are automatically grouped under the label of the search root "
              "folder they belong to. You can override any project's category — and add "
              "an optional sub-category — without moving any files.")
            br()
            h2("How root labels work")
            p("Each entry in Settings → Search Roots has a Label. That label becomes "
              "the category header for all projects found inside that folder. Edit the "
              "label in Settings to rename the whole group at once.")
            br()
            h2("Overriding a single project")
            ins("  1. Right-click the project row\n", "body")
            ins("  2. Choose  📁 Assign Category…\n", "body")
            ins("  3. Pick or type a Category (and optional Sub-category)\n", "body")
            ins("  4. Click OK — the project moves to the new group immediately\n", "body")
            br()
            p("To remove an override and return the project to its root's group, "
              "open Assign Category… and click Clear Override.")
            br()
            h2("Sub-categories")
            p("Sub-categories appear indented under their parent category (shown as "
              "↳ Sub-category). They work like folders-within-folders. Right-click "
              "any project at any time to move it between groups.")
            br()
            warn("⚠  Category headers and sub-category rows are not selectable — "
                 "right-click and action buttons only work on project rows.")
        self._help_show(_fill)

    def _help_git_concepts(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: What & Why")
            p("Git is a tool that remembers the history of every change you make to "
              "your project. Think of it like infinite undo — but smarter. You decide "
              "when to save a checkpoint, and you can always go back.")
            br()
            h2("Commit — a save point")
            p("A commit is a snapshot of your project at a moment in time. Each one "
              "has a short message you write, like 'fix: typo in README' or "
              "'feat: add dark mode'. Over time, these build up into a history "
              "you can scroll through.")
            br()
            h2("Repository (repo) — the project folder + its history")
            p("When you run Git Init on a project, git creates a hidden .git folder "
              "inside it. That folder stores every commit ever made. The whole thing "
              "— your files plus that history — is called a repository.")
            br()
            h2("Branch — a parallel version")
            p("Imagine photocopying your project so you can experiment on the copy "
              "without touching the original. That's a branch. When you're happy "
              "with the experiment, you can merge it back. The default branch is "
              "usually called 'master' or 'main'.")
            br()
            h2("Remote — a copy on GitHub")
            p("A remote is a second home for your repository, stored on GitHub's "
              "servers. It acts as a backup and lets others see your work. The "
              "remote is usually called 'origin'.")
            br()
            h2("Push — upload to GitHub")
            p("After making commits on your machine, Push sends them to GitHub. "
              "Nothing leaves your computer until you Push — commits are purely local "
              "until then.")
            br()
            h2("Pull — download from GitHub")
            p("Pull fetches any commits from GitHub that you don't have yet and "
              "adds them to your local history. Useful if you work on multiple "
              "machines, or if a collaborator pushed something new.")
            br()
            h2("Working tree — uncommitted changes")
            p("The working tree is the current state of your files right now, before "
              "you've committed them. The Git tab shows a list of files that have "
              "changed since your last commit. An 'M' means modified, '?' means "
              "a new file git hasn't seen before, 'D' means deleted.")
            br()
            h2("Staging — choosing what to commit")
            p("Git lets you pick exactly which changes to include in a commit. "
              "The 'Stage all changes' checkbox in the Commit dialog does this "
              "automatically — it stages everything in the working tree, which is "
              "almost always what you want.")
            br()
            ok("Bottom line: commit often, push when you're done for the day.")
        self._help_show(_fill)

    def _help_git_workflow(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: Daily Workflow")
            p("Here's how a typical coding session looks when using the Git tab.")
            br()
            h2("Starting a session")
            ins("  1. Switch to the Git tab\n", "body")
            ins("  2. Click ⟳ Refresh to see the current state\n", "body")
            ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
                "     changes from GitHub before you start editing\n", "body")
            br()
            h2("While you're working")
            p("Edit your files normally. The Working Tree list updates whenever "
              "you Refresh. Click any file in the list to see exactly what changed "
              "(green = added, red = removed).")
            br()
            h2("Saving your work (committing)")
            ins("  1. Click  📝 Commit…\n", "body")
            ins("  2. The dialog shows what files changed and suggests a message\n", "body")
            ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
            ins("  4. Click Commit\n", "body")
            br()
            p("There's no rule for how often to commit. A good rule of thumb: "
              "commit whenever you finish one thing. Small commits are better than "
              "one huge commit at the end of the day.")
            br()
            h2("Uploading to GitHub (pushing)")
            ins("  1. Click  ⬆ Push\n", "body")
            ins("  2. The output log shows whether it succeeded\n", "body")
            ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
            br()
            h2("Trying out an idea safely (branching)")
            ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
            ins("  2. Check 'Switch to this branch immediately'\n", "body")
            ins("  3. Make your changes and commit as normal\n", "body")
            ins("  4. If you don't like it: 🔀 Switch Branch back to master — the\n"
                "     experiment branch stays there but your main code is untouched\n", "body")
            br()
            h2("Finishing a feature branch (merge & cleanup)")
            p("Once your branch is tested and ready to bring back into master:")
            ins("  1.  🔀 Switch Branch  → master\n", "body")
            ins("  2.  ⬇ Pull            — pick up any new master commits first\n", "body")
            ins("  3.  ⇄ Merge…          → pick your feature branch\n", "body")
            ins("                          Confirmation says 'Merge X INTO master?' — yes\n", "body")
            ins("  4.  ⬆ Push            — master with the merged commits goes to GitHub\n", "body")
            ins("  5.  🗑 Delete Branch  → pick your feature branch → Yes (local)\n", "body")
            ins("                          Then: 'Also delete from GitHub?' → Yes\n", "body")
            br()
            p("If the merge produces conflicts, the manager pops a dialog telling "
              "you what to do (resolve in editor + commit, or run "
              "'git merge --abort' to undo). Conflicts only happen when both "
              "branches changed the same lines.")
            br()
            h2("Undoing mistakes")
            p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
              "as uncommitted edits — you can fix them and recommit, or just discard.")
            br()
            warn("⚠  Undo Last Commit only removes the last commit. To undo older "
                 "commits, use the terminal.")
            br()
            h2("Typical day in one line")
            dim("  Pull → Edit → Commit → Edit → Commit → Push")
        self._help_show(_fill)

    def _help_git_tab(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git Tab")
            p("The Git tab shows live status for whichever project is selected in the "
              "Projects tab. It updates automatically when you switch projects or switch "
              "to this tab.")
            br()
            h2("Working Tree & Diff")
            p("The Working Tree panel lists every modified, added, or deleted file. "
              "Click any file to see its diff below — added lines are green, removed "
              "lines are red.")
            br()
            h2("Committing changes")
            ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
            ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
            ins("  3. Edit the message if you like, then click Commit\n", "body")
            br()
            p("The suggested message is generated from your staged changes, using "
              "a chain of strategies — highest-quality first:")
            ins("    1. CHANGELOG.md bullets (if you've added an entry)\n", "body")
            ins("    2. Diff content — added Python defs/classes, file kinds\n", "body")
            ins("    3. File-name patterns (legacy fallback)\n", "body")
            p("Each result is sanitised (subject ≤ 72 chars, imperative mood, "
              "no filename listings). When AI is enabled in Settings, an "
              "Anthropic / OpenAI / LM Studio / Ollama call runs first — silent "
              "fallback to heuristics on any failure. Click 💡 Suggest at any "
              "time to regenerate.")
            br()
            h2("Undo Last Commit")
            p("Removes the most recent commit but keeps all your changes staged — "
              "nothing is deleted. Safe to use if you committed too early or with "
              "the wrong message.")
            br()
            h2("Branches")
            ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
            ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
            ins("  ⇄ Merge…         — merge another branch INTO the current one\n", "body")
            ins("                     (use after switching to master to pull a finished feature back in)\n", "body")
            ins("  🗑 Delete Branch — safe-delete locally; then offers to also delete from GitHub\n", "body")
            ins("                     (only prompts about GitHub if a remote copy actually exists)\n", "body")
            br()
            warn("⚠  Switching branches with uncommitted changes will fail. "
                 "Commit or undo first.")
            br()
            warn("⚠  Merging with uncommitted changes also fails. Same fix — commit, "
                 "stash, or undo first.")
            br()
            h2("Push & Pull")
            p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
              "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
        self._help_show(_fill)

    def _help_github_setup(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("GitHub Setup")
            p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
              "wizard for getting your project onto GitHub — even if you've never used "
              "GitHub before.")
            br()
            h2("Step 1 — Git identity")
            p("Every commit is stamped with your name and email. The wizard shows your "
              "current global settings and lets you update them. These are stored in "
              "your global git config and apply to every project on this machine.")
            br()
            h2("Step 2 — Create a GitHub account")
            p("Free at github.com. The wizard has a button to open the sign-up page.")
            br()
            h2("Step 3 — Create a repository")
            p("Go to github.com/new. Give it a name, leave it Public. "
              "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
              "Copy the HTTPS URL shown after creation (e.g. "
              "https://github.com/you/my-project.git).")
            br()
            h2("Step 4 — Paste the URL")
            p("Paste the URL into the wizard and click Set. This tells git where to "
              "send your code. The Git tab's Remote label will update immediately.")
            br()
            h2("Step 5 — Push")
            p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
              "you to log in to GitHub — this is Git Credential Manager doing its job. "
              "Log in once and future pushes happen silently.")
            br()
            warn("⚠  If Push fails with an authentication error, open a terminal in "
                 "the project folder and run:  git push\n"
                 "This triggers the browser login. After that, the Push button works normally.")
            br()
            h2("📦 GitHub Releases")
            p("A Release lets anyone download your .exe without needing Python "
              "installed. To create one:")
            ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
            ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
            ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
            ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
            br()
            p("Releases require the GitHub CLI (gh). If it's not installed, "
              "the wizard shows a link to cli.github.com.")
        self._help_show(_fill)

    def _help_codegraph(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("CodeGraph (alternative code-graph tool)")
            p("CodeGraph is a separate MCP server that does what tokensave does — "
              "builds a per-project code-graph index and exposes it to Claude Code. "
              "The two don't conflict; a project can have both at once.")
            br()
            h2("When to use which")
            ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
            ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
            ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
            br()
            warn("⚠  About CodeGraph's auto-sync: the file watcher only runs while "
                 "CodeGraph's MCP server is active inside an open Claude Code session. "
                 "If you edit code with Claude Code closed, those edits won't be "
                 "picked up automatically until the next session — at which point "
                 "the watcher catches up. You can also right-click → 🧠 CodeGraph Sync "
                 "to force an incremental update manually.")
            br()
            h2("Install")
            ins("  Settings → CodeGraph → Install via npm  ", "body")
            ins("(requires Node.js 18+)\n", "dim")
            br()
            h2("Use")
            ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
            ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
            br()
            h2("Why the manager doesn't run `codegraph install`")
            p("CodeGraph registers itself with Claude Code (and Cursor / Codex / "
              "opencode if you use them) via its own one-time installer: "
              "`npx @colbymchenry/codegraph`. The TokenSave Manager intentionally "
              "stays out of that flow — we handle per-project lifecycle only "
              "(init / sync / status / remove). This means tokensave and CodeGraph "
              "can both write their own sections into your global ~/.claude.json "
              "without fighting each other.")
        self._help_show(_fill)

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(TEMPLATE_DIR, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)

    # ── Data ───────────────────────────────────────────────────────────────────

    def _has_scaffold(self, path):
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def refresh(self):
        self.projects = find_projects()
        pinned = get_pinned()
        self.active_path = pinned or (self.projects[0]["path"] if self.projects else None)

        for item in self.tree.get_children():
            self.tree.delete(item)

        proj_cats = _cfg.get("project_categories", {})

        # Group projects by (category, subcategory)
        groups: dict = {}
        for p in self.projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            # Insert category header row if not yet present
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self.tree.insert("", tk.END, iid=ciid, text=cat,
                                 open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]

            # Insert sub-category header row if specified
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self.tree.exists(siid):
                    self.tree.insert(parent, tk.END, iid=siid, text=f"  ↳ {subcat}",
                                     open=True, tags=("subcategory",))
                parent = siid

            # Insert project rows
            for p in projs:
                is_active    = (p["path"] == self.active_path)
                has_scaffold = self._has_scaffold(p["path"])
                has_ts       = p.get("has_tokensave", True)
                has_git      = p.get("has_git", False)
                has_cg       = p.get("has_codegraph", False)
                if is_active:
                    base_tag = "active"
                elif not has_ts:
                    base_tag = "git_only"
                elif not has_scaffold:
                    base_tag = "scaffold"
                else:
                    base_tag = "normal"
                # synced column: show tokensave index age, or "—" for git-only projects
                synced_str = fmt_age(p["mtime"]) if has_ts else "—"
                # CG column: simple ✓/— marker (codegraph auto-syncs; no age shown)
                cg_text = "✓" if has_cg else "—"
                # Git column: placeholder "…" while the async refresh runs.
                # Non-git projects get "—" immediately and need no async update.
                git_text, git_tag = _format_git_status_cell(
                    p.get("git_status"), has_git)
                # Combine baseline + git tag (later tag wins for foreground)
                tags = (base_tag, git_tag) if has_git else (base_tag, "git_none")
                piid = f"proj:{p['path']}"
                self.tree.insert(parent, tk.END, iid=piid,
                                 text=p["name"],
                                 values=("★" if is_active else "",
                                         p["path"],
                                         synced_str,
                                         cg_text,
                                         git_text,
                                         "✔" if has_scaffold else "—"),
                                 tags=tags)

        if self.active_path:
            name = os.path.basename(self.active_path)
            tag  = "pinned" if pinned else "auto"
            self.active_badge.config(text=f"  ★ {name}  ({tag})  ")
        else:
            self.active_badge.config(text="  No project  ")

        # Keep Git tab in sync when it's visible and a project is tracked
        if self._git.is_visible() and self._git.has_path():
            self._git.refresh()

        # Kick off background refresh of the Git status column so the
        # "…" placeholders get replaced with real ✓ / ● / ↑N / ↓N indicators.
        self._kick_off_git_status_refresh()

    def _kick_off_git_status_refresh(self):
        """Background-walk every git project and update its Git column cell.

        Cheap mtime-of-.git/index check first: if the index hasn't changed
        since the last computed status, reuse the cached result.
        """
        # Cancel any in-flight refresh; only one at a time
        if getattr(self, "_git_status_refresh_running", False):
            self._git_status_refresh_cancel = True
        self._git_status_refresh_cancel  = False
        self._git_status_refresh_running = True

        projects_snapshot = list(self.projects)

        def worker():
            try:
                for p in projects_snapshot:
                    if self._git_status_refresh_cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    # mtime cache: skip if .git/index hasn't changed since last
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue  # nothing new
                    try:
                        out, _rc = self._shell_capture(
                            [GIT_EXE, "-C", path,
                             "status", "--porcelain=v2", "--branch"],
                            path)
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]    = status
                    p["_git_idx_mtime"] = idx_mtime
                    piid = f"proj:{path}"
                    self.after(0, self._update_git_status_cell, piid, status)
                    # Yield to UI thread between checks
                    time.sleep(0.05)
            finally:
                self._git_status_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    # Set of tag names used as Git-status overrides on project rows.
    # _update_git_status_cell strips these (but never the baseline "git_only"
    # tag which uses a different meaning — git project with no tokensave).
    _GIT_STATUS_TAGS = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }

    def _update_git_status_cell(self, piid: str, status: dict):
        """Main-thread: update a single row's Git column value + override tag."""
        if not self.tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            self.tree.set(piid, "git", text)
        except tk.TclError:
            return
        # Preserve baseline tag (e.g. "active", "git_only", "scaffold", "normal"),
        # swap the prior status-override tag for the new one.
        existing = list(self.tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in self._GIT_STATUS_TAGS]
        existing.append(tag)
        self.tree.item(piid, tags=tuple(existing))

    def _build_context_menu(self):
        m = tk.Menu(self, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_separator()
        m.add_command(label="🧠  CodeGraph Init",          command=self.cmd_codegraph_init)
        m.add_command(label="🧠  CodeGraph Sync",          command=self.cmd_codegraph_sync)
        m.add_command(label="🧠  CodeGraph Status",        command=self.cmd_codegraph_status)
        m.add_command(label="🧠  Remove CodeGraph Index",  command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔍  AI Code Review…",    command=self.cmd_ai_code_review)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        if not row.startswith("proj:"):
            return   # no context menu on category/subcategory header rows
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _insert_pending_row(self, path, name):
        """Add a placeholder row while tokensave init is running.

        Six columns: active, path, synced, cg, git, scaffold — must match
        the Treeview definition in _build_projects_tab.
        """
        self.tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))

    def _check_config(self):
        problems = []
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            problems.append("tokensave.exe path is missing or invalid")
        if not TEMPLATE_DIR or not os.path.isdir(TEMPLATE_DIR):
            problems.append("Template directory is missing or invalid")

        # MCP-config drift detection — opens the configurator instead of
        # Settings when there are no other problems, since that's the most
        # actionable thing the user can do.
        skips = (_cfg.get("mcp_skip_warnings") or []) \
                if isinstance(_cfg, dict) else []
        mcp_drift = []
        for label, path in _MCP_CONFIGS:
            if path in skips:
                continue
            try:
                info = _classify_mcp_entry(path)
            except Exception:
                # Defensive — never crash startup just because we can't read
                # a Claude config file. The dialog can surface details.
                continue
            if info["state"] != "ok":
                mcp_drift.append((label, info))

        if not problems and not mcp_drift:
            return

        if problems:
            # Existing path: paths broken, open Settings as before.
            note = "Please set the correct paths before using the manager."
            self._log("Config problem: " + " | ".join(problems), C["red"])
            SettingsDialog(
                self, _cfg, _save_config, self._on_settings_saved,
                startup_note=(note + "\n\n"
                              + "\n".join(f"• {p}" for p in problems)))
            return

        # Pure MCP drift — log it, open the configurator dialog directly.
        # Don't auto-pop in a modal way; the user just launched the manager
        # and wants to see the project list. A log line + a non-modal dialog
        # gives them the choice.
        for label, info in mcp_drift:
            self._log(
                f"MCP: {label} {info['label']} ({info['cfg_path']}). "
                f"Open Settings → MCP integration to fix.",
                C["peach"] if info["state"] in
                ("direct_serve", "wrong_wrapper") else C["red"])

        # Open the configurator after a short delay so the main window has
        # finished laying out — feels less like an interruption.
        self.after(800, lambda: MCPConfigDialog(self))

    def _auto_refresh(self):
        if self._current_proc is None:
            self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _log(self, msg, colour=None):
        def _do():
            self.log.configure(state=tk.NORMAL)
            tag = f"col_{colour}"
            self.log.tag_configure(tag, foreground=colour or C["green"])
            self.log.insert(tk.END, msg + "\n", tag)
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.", parent=self)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).", parent=self)
            return None
        return iid[5:]   # strip "proj:" prefix

    # ── Scaffold ────────────────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path) -> list:
        """Copy Nuitka build templates into path, auto-filling [PROJECT_NAME].

        Returns a list of action strings describing what was created (empty if
        both files already existed or if the templates couldn't be found).
        """
        name     = os.path.basename(path)
        src_ps1  = os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template")
        src_bat  = os.path.join(TEMPLATE_DIR, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._log("  [WARN] Nuitka templates not found in template directory — skipped",
                      C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {TEMPLATE_DIR}")
            return actions

        # build.ps1 — fill [PROJECT_NAME]; leave [ENTRY_SCRIPT] / [OUTPUT_NAME] for the user
        if os.path.isfile(dst_ps1):
            self._log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        # build.bat — copy as-is (ASCII, no customisation needed)
        if os.path.isfile(dst_bat):
            self._log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])

        return actions

    def _scaffold_project(self, path, create_bi=True, run_init=True,
                          scaffold_nuitka=False, add_git_hook=False):
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        # Write BASIC_INSTRUCTIONS.md synchronously (it's instant)
        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template()
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        # Copy Nuitka build templates synchronously (instant file copy)
        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        # Write/merge auto-commit Stop hook
        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._log(f"  {action}", C["green"])

        if run_init:
            # Show a pending row immediately so the user sees the project appearing
            self._insert_pending_row(path, name)
            self._log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self.after(0, self._set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self._current_proc = None
                    self.after(0, self._set_running, False)
                    self.after(0, self.refresh)
                    # Offer to commit the new BASIC_INSTRUCTIONS.md /
                    # Nuitka templates / hook files (if this is a git repo)
                    self.after(0, lambda: self._offer_commit_after_change(
                        path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self.refresh()
            # Even without tokensave init, file writes still happened
            self._offer_commit_after_change(path, "scaffold files")

    # ── Commands ───────────────────────────────────────────────────────────────

    def _require_tokensave(self, path: str) -> bool:
        """Return True if the project has a tokensave index.

        If not, shows a friendly info dialog explaining how to add one and
        returns False so the caller can bail out gracefully.
        """
        if os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db")):
            return True
        name = os.path.basename(path)
        messagebox.showinfo(
            "Not indexed with tokensave",
            f"'{name}' is a git project but doesn't have a tokensave index yet.\n\n"
            "Tokensave builds a code-graph that lets Claude navigate your project "
            "efficiently without reading every file.\n\n"
            "To add it:  right-click → ⚙ Retrofit…  and tick "
            "'Add tokensave @include + init'.",
            parent=self)
        return False

    def _require_codegraph_installed(self) -> bool:
        """Return True if the CodeGraph CLI is installed and resolvable.

        If not, shows a friendly install nudge dialog with an 'Open Settings'
        button that scrolls Settings to the CodeGraph section. Returns False
        so callers can bail out gracefully.
        """
        if CODEGRAPH_EXE and os.path.isfile(CODEGRAPH_EXE):
            return True
        # Custom yes/no dialog so we can label the buttons "Open Settings" / "Cancel"
        result = messagebox.askyesno(
            "CodeGraph is not installed",
            "CodeGraph is not installed on this machine.\n\n"
            "CodeGraph is an alternative code-graph tool that builds a "
            "per-project SQLite index for Claude Code to query. It complements "
            "tokensave — both can be enabled on the same project.\n\n"
            "Open Settings now to install it?",
            parent=self)
        if result:
            dlg = SettingsDialog(self, _cfg, _save_config, self._on_settings_saved)
            # Defer the scroll so the dialog has been mapped and sized first
            if hasattr(dlg, "_scroll_to_codegraph"):
                dlg.after(50, dlg._scroll_to_codegraph)
        return False

    def cmd_set_active(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        set_pinned(path)
        self._log(f"Pinned → {path}", C["green"])
        # Live-reload of the pin via a wrapper-side file watcher was
        # attempted earlier (2026-05-23) but broke MCP handshake with
        # Claude Desktop on UWP installs — Desktop's MCP attach would
        # time out at 30s with the wrapper running a daemon thread. The
        # wrapper has been reverted to the literal original single-
        # threaded shape, so pin changes once again require a Claude
        # restart to take effect.
        try:
            states = [_classify_mcp_entry(p)["state"] for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if "ok" in states and not all(s == "ok" for s in states):
            bad = [lbl for (lbl, p), s in zip(_MCP_CONFIGS, states) if s != "ok"]
            self._log(
                f"  Pin will take effect at next Claude restart.  "
                f"Note: {', '.join(bad)} also still needs its MCP wiring "
                f"fixed (Settings → 🔌 Manage MCP wiring).",
                C["peach"])
        elif "ok" not in states:
            self._log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
        else:
            self._log(
                "  Pin will take effect at next Claude Desktop / Claude Code "
                "restart.  (Live in-session reload is deferred — see the "
                "wrapper script's docstring for context.)",
                C["overlay0"])
        self.refresh()

    def cmd_auto(self):
        clear_pinned()
        self._log("Auto-detect enabled — wrapper picks the most-recently-synced project at next launch.", C["sky"])
        self._log(
            "  Restart Claude Desktop / Claude Code to trigger a fresh "
            "auto-detect.",
            C["overlay0"])
        self.refresh()

    def _set_running(self, running, label=""):
        if running:
            self._stop_btn.configure(state=tk.NORMAL)
            self._running_label.configure(text=f"⏳ running: {label}")
        else:
            self._stop_btn.configure(state=tk.DISABLED)
            self._running_label.configure(text="")

    def _stop_current(self):
        self._stop_requested = True
        proc = self._current_proc
        if proc and proc.poll() is None:
            proc.kill()
            self._log("  ■ Stopped by user.", C["red"])

    def _open_log(self):
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("No log yet",
                "No log file exists yet — run an operation first.", parent=self)

    def _run(self, args, cwd, label):
        def worker():
            cmd_str = "tokensave " + " ".join(args)
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            log.debug(f"     cwd={cwd}")
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE] + args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                log.debug(f"     pid={proc.pid}")
                # Suppress tokensave's misleading "legacy .codegraph/" warning
                # when CodeGraph is actually an active alternate index in this
                # project — manager treats tokensave and CodeGraph as equal
                # citizens; following tokensave's "safely deleted" advice would
                # wipe the user's CodeGraph index. The warning is only correct
                # for genuinely orphaned .codegraph/ folders (no codegraph.db).
                codegraph_active = os.path.isfile(
                    os.path.join(cwd, ".codegraph", "codegraph.db"))
                _suppressed_codegraph_warning = False
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    if (codegraph_active
                            and "legacy .codegraph" in stripped
                            and "safely deleted" in stripped):
                        # Drop silently, but log once that we did so. The
                        # info-bar message lets the user know we filtered
                        # — never silent fakery without trace.
                        if not _suppressed_codegraph_warning:
                            self._log(
                                "  (suppressed tokensave's '.codegraph/ legacy' "
                                "warning — CodeGraph is active in this project)",
                                C["overlay0"])
                            _suppressed_codegraph_warning = True
                        log.debug(f"  SUPPRESSED {stripped}")
                        continue
                    # Detect tokensave's "Update available: v→v" line and
                    # remember the upgrade target. Settings shows an
                    # "Upgrade tokensave" button when this is set; the
                    # button just runs `tokensave upgrade` via _run.
                    m = _TOKENSAVE_UPDATE_RE.search(stripped)
                    if m:
                        cur_v, new_v = m.group(1), m.group(2)
                        self._tokensave_current_version = cur_v
                        self._tokensave_available_version = new_v
                        self._log(stripped, C["yellow"])
                        self._log(
                            f"  → tokensave {cur_v} → {new_v} ready to "
                            f"install.  Settings → 'Upgrade tokensave to "
                            f"v{new_v}' to apply, or run "
                            f"'tokensave upgrade' from a shell.",
                            C["peach"])
                        log.info(f"UPDATE-AVAILABLE  {cur_v} -> {new_v}")
                        continue
                    self._log(stripped)
                    log.debug(f"  OUT {stripped}")
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                    if (args and args[0] == "sync"
                            and _cfg.get("auto_commit_after_sync")
                            and _is_git_repo(cwd)):
                        self._auto_commit_after_sync(cwd)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)
        threading.Thread(target=worker, daemon=True).start()

    def _auto_commit_after_sync(self, cwd: str) -> None:
        """Commit staged changes after a successful sync, using LLM or amend-stacking."""
        self._log("  Auto-committing sync changes…", C["peach"])
        self._shell_capture([GIT_EXE, "-C", cwd, "add", "-A"], cwd)
        _, staged_rc = self._shell_capture(
            [GIT_EXE, "-C", cwd, "diff", "--cached", "--quiet"], cwd)
        if staged_rc == 0:
            return  # nothing staged

        llm_cfg = _cfg.get("commit_message_llm") or {}
        use_llm = bool(llm_cfg.get("enabled") and llm_cfg.get("use_for_sync_autocommit"))

        if use_llm:
            # LLM mode: each sync gets a unique message; no amend-stacking.
            self._log("  Composing AI commit message…", C["peach"])
            status_out, _ = self._shell_capture(
                [GIT_EXE, "-C", cwd, "status", "--short"], cwd)
            ai_msg = _suggest_commit_message(cwd, status_out) or "chore: tokensave sync"
            commit_cmd = [GIT_EXE, "-C", cwd, "commit", "-m", ai_msg.split("\n", 1)[0]]
            if "\n\n" in ai_msg:
                commit_cmd.extend(["-m", ai_msg.split("\n\n", 1)[1]])
        else:
            # Default amend-stacking: repeated syncs collapse into one commit.
            last_out, _ = self._shell_capture(
                [GIT_EXE, "-C", cwd, "log", "-1", "--format=%s"], cwd)
            if last_out.strip() == "chore: tokensave sync":
                commit_cmd = [GIT_EXE, "-C", cwd, "commit", "--amend", "--no-edit"]
                self._log("  Amending previous sync commit…", C["peach"])
            else:
                commit_cmd = [GIT_EXE, "-C", cwd, "commit", "-m", "chore: tokensave sync"]

        cout, crc = self._shell_capture(commit_cmd, cwd)
        col = C["green"] if crc == 0 else C["red"]
        for line in cout.strip().splitlines()[-3:]:
            self._log(f"  {line}", col)

    def _run_capture(self, args, cwd, label) -> tuple:
        """Run a tokensave command and return (raw_output, returncode, elapsed_s).

        Synchronous — must be called from a background thread.
        Handles _current_proc tracking and _set_running for the duration.
        The caller is responsible for logging and scheduling UI updates.
        """
        cmd_str = "tokensave " + " ".join(args)
        self._log(f"$ {cmd_str}  [{label}]", C["blue"])
        self.after(0, self._set_running, True, label)
        log.info(f"RUN  {cmd_str}")
        log.debug(f"     cwd={cwd}")
        t0 = time.monotonic()
        try:
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            proc = subprocess.Popen(
                [TOKENSAVE] + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self._current_proc = proc
            log.debug(f"     pid={proc.pid}")
            raw = proc.stdout.read()
            proc.wait()
            elapsed = time.monotonic() - t0
            log.info(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
            log.debug(f"  OUT {raw[:500]}")
            return raw, proc.returncode, elapsed
        finally:
            self._current_proc = None
            self.after(0, self._set_running, False)

    def _shell_capture(self, cmd: list, cwd: str, env=None) -> tuple:
        """Run any shell command and return (stdout+stderr, returncode).

        Generic helper — cmd[0] is the executable (not tokensave-specific).
        Synchronous — must be called from a background thread.
        Returns ("Error: '<exe>' not found on system PATH.", 1) if the
        executable is missing so callers always get a displayable string.

        Pass env= to override the process environment (e.g. set
        GIT_TERMINAL_PROMPT=0 for network git operations so they fail
        immediately instead of hanging waiting for stdin auth prompts).
        """
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
            out = proc.stdout.read()
            proc.wait()
            return out, proc.returncode
        except FileNotFoundError:
            return (f"Error: '{cmd[0]}' not found on system PATH.", 1)

    def cmd_sync(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._run(["sync"], cwd=path, label=os.path.basename(path))

    def _probe_tokensave_version(self):
        """Best-effort read of the installed tokensave version.

        Runs `tokensave --version` once at App startup (in a background
        thread to avoid blocking the GUI). Output looks like
        "tokensave 5.1.1" — we extract the version string and cache it
        on the instance. Failures (binary missing, weird output) leave
        the cache as None, which the Settings UI handles gracefully.
        """
        def _worker():
            if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
                return
            try:
                r = subprocess.run(
                    [TOKENSAVE, "--version"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired):
                return
            out = (r.stdout or "").strip()
            m = re.search(r'(\d+\.\d+\.\d+(?:\.\d+)?)', out)
            if m:
                self._tokensave_current_version = m.group(1)
                log.debug(f"tokensave installed version: "
                          f"{self._tokensave_current_version}")
                # Kick off a single update check right after we know the
                # local version. Subsequent checks fire from the hourly
                # poller below.
                self._check_tokensave_updates()
        threading.Thread(target=_worker, daemon=True,
                         name="tokensave-version-probe").start()
        # Hourly background poller. Cheap (one GitHub API call, no auth);
        # safe to run forever as a daemon thread.
        threading.Thread(target=self._tokensave_update_poll_loop,
                         daemon=True,
                         name="tokensave-update-poll").start()

    # GitHub releases API endpoint for tokensave. Hardcoded since the
    # tokensave repo URL is referenced in README.md and is unlikely to
    # change. If it does, this is a one-line update.
    _TOKENSAVE_RELEASES_API = (
        "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest")

    # Hourly poll cadence — GitHub allows 60 unauthenticated requests/hour
    # per IP, so once an hour is comfortably within the limit and keeps
    # the update notification fresh enough that users won't miss a release
    # for long. Tunable via _cfg["tokensave_update_poll_hours"] if a user
    # wants to be more or less aggressive.
    def _tokensave_update_poll_interval(self) -> float:
        hours = float(_cfg.get("tokensave_update_poll_hours", 1.0))
        return max(0.25, hours) * 3600.0  # never poll more than 4x/hour

    def _tokensave_update_poll_loop(self):
        """Daemon: re-check GitHub for new tokensave releases periodically.

        Doesn't trigger any UI prompts — just refreshes the cached
        `_tokensave_available_version` so the Settings dialog reflects
        the current state next time it's opened. The OUTPUT-pane hint
        line is only logged on FRESH discovery (transition from "no
        update known" → "update available"), not on every poll, to avoid
        spamming the log.
        """
        while True:
            time.sleep(self._tokensave_update_poll_interval())
            self._check_tokensave_updates()

    def _check_tokensave_updates(self):
        """Single-shot check against the tokensave releases API.

        Compares against `_tokensave_current_version` (set by the local
        --version probe). When a strictly-newer version is found AND it
        wasn't known before, logs a peach hint to the OUTPUT pane so
        users see the update offer without opening Settings.
        """
        import urllib.request, urllib.error, json as _json
        if not self._tokensave_current_version:
            return  # nothing to compare against yet
        try:
            req = urllib.request.Request(
                self._TOKENSAVE_RELEASES_API,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "tokensave-manager"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, _json.JSONDecodeError, OSError) as e:
            # Common when offline or rate-limited. Silent — try again
            # next interval.
            log.debug(f"tokensave update check failed: {type(e).__name__}: {e}")
            return
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        m = re.match(r'(\d+\.\d+\.\d+(?:\.\d+)?)', tag)
        if not m:
            return
        latest = m.group(1)
        cur = self._tokensave_current_version
        if not _version_lt(cur, latest):
            return  # current is up-to-date or ahead (unlikely but possible)
        prev_known = self._tokensave_available_version
        self._tokensave_available_version = latest
        if prev_known != latest:
            # Fresh discovery — surface it. (Skip the log line if the
            # poller is just re-confirming a version we already knew
            # about.)
            self._log(
                f"  → tokensave {cur} → {latest} ready to install.  "
                f"Settings → 'Upgrade tokensave to v{latest}' to apply, "
                f"or run 'tokensave upgrade' from a shell.",
                C["peach"])
            log.info(f"UPDATE-AVAILABLE  {cur} -> {latest}  (via GitHub API)")

    def cmd_upgrade_tokensave(self):
        """Run `tokensave upgrade` from the manager.

        Streams output to the OUTPUT pane via the existing _run path. cwd
        doesn't matter for upgrade (it operates on the installed binary,
        not any specific project) so we use the tokensave_exe's directory
        as a stable choice. On success, the upgrade replaces the binary
        on disk; future sync / MCP-server spawns pick up the new version
        automatically, but the currently-running MCP wrappers continue
        serving from the old binary until Claude is restarted.

        Clears the cached `_tokensave_available_version` on success so
        the Settings button auto-hides until the next sync re-reports an
        update.
        """
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            messagebox.showwarning(
                "tokensave not found",
                "Set the tokensave.exe path in Settings first.",
                parent=self)
            return
        # Confirm — upgrades replace a binary and we want the user fully
        # aware. Skip the prompt if no version metadata is known (still
        # useful — runs the upgrade command which itself shows what'll
        # happen).
        target = self._tokensave_available_version
        cur = self._tokensave_current_version
        if target:
            msg = (f"Upgrade tokensave from v{cur or '?'} to v{target}?\n\n")
        elif cur:
            msg = (f"Run `tokensave upgrade`?  (Currently installed: "
                   f"v{cur}.)\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        else:
            msg = ("Run `tokensave upgrade`?\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        msg += (
            "This replaces the tokensave binary on disk.  Currently-running\n"
            "MCP wrappers continue serving from the old binary until you\n"
            "restart Claude Desktop / Claude Code.")
        if not messagebox.askyesno("Upgrade tokensave", msg, parent=self):
            return
        # Clear cache so the Settings button hides until the next sync
        # reports a fresh update.  If the upgrade fails, the next sync
        # will re-populate it anyway.
        self._tokensave_available_version = None
        # cwd is the tokensave.exe folder — works for any upgrade flow.
        self._run(["upgrade"], cwd=os.path.dirname(TOKENSAVE),
                  label="upgrade")

    def cmd_sync_all(self):
        if not self.projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self)
            return
        ts_projects = [p for p in self.projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self)
            return
        count = len(ts_projects)
        skipped = len(self.projects) - count
        skip_note = f"\n({skipped} git-only project{'s' if skipped != 1 else ''} will be skipped)" if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self,
        ):
            return

        projects_snapshot = list(ts_projects)

        def worker():
            self._stop_requested = False
            self._log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self.after(0, self._set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._stop_requested:
                    self._log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self._current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self.after(0, self._set_running, False)
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._log(f"  Status OK — {data.get('node_count')} nodes, "
                              f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._log(line)
                self._log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name, msg):
        win = tk.Toplevel(self)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_git_log(self):
        """Navigate to the Git tab and show live status/log for the selected project.

        Replaces the old popup approach — all git information is now displayed
        inline in the Git tab, which avoids dialog clutter.
        """
        path = self._selected_path()
        if not path:
            return

        # Point the Git tab at this project and switch to it
        self._git.set_active_path(path)
        try:
            for idx in range(self.nb.index("end")):
                if self.nb.tab(idx, "text").strip() == "Git":
                    self.nb.select(idx)
                    break
        except tk.TclError:
            pass

        self._git.refresh()

    # ── Git Init ───────────────────────────────────────────────────────────────

    def cmd_git_init(self):
        """Right-click: initialise a git repository in the selected project folder.

        git init is near-instantaneous (~5ms) so we run it synchronously on the
        main thread — no need for a background thread, and it keeps the
        messagebox flow simple and race-condition-free.
        """
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)

        # Guard: already a repo?
        if _is_git_repo(path):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self,
            )
            return

        # Run git init on the main thread (instantaneous)
        self._log(f"Running git init in {name}…", C["peach"])
        out, rc = self._shell_capture([GIT_EXE,"-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._log(f"  {line}", col)

        if rc != 0:
            self.refresh()
            return

        # Write a baseline .gitignore if none exists (protects git add -A)
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._log(f"  Warning: could not write .gitignore: {e}", C["yellow"])

        # Ask about initial commit — natural main-thread messagebox, no scheduling
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self,
        ):
            def do_commit():
                self._shell_capture([GIT_EXE,"-C", path, "add", "-A"], path)
                cout, crc = self._shell_capture(
                    [GIT_EXE,"-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", ccol))
                self.after(0, self.refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self.refresh()

    # ── Ensure .gitignore ──────────────────────────────────────────────────────

    def cmd_manage_gitignore(self):
        """Right-click: open the .gitignore management dialog for the selected project.

        Replaces the old cmd_ensure_gitignore command. The baseline-only
        quick-add is still available as the first button inside the dialog
        ('+ Baseline'). The module-level `_ensure_gitignore` function is
        unchanged and still called by `cmd_git_init` for first-time setup.
        """
        path = self._selected_path()
        if not path:
            return
        GitignoreDialog(self, path)

    def cmd_untrack_ignored(self):
        """Right-click: open the Untrack Ignored Files dialog.

        Finds every file that's tracked by git AND matches a pattern in
        .gitignore (the 'stale tracking' problem) and offers a checklist
        for selective untracking via `git rm --cached`.
        """
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo("Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self)
            return
        files = _find_tracked_but_ignored(path)
        if not files:
            messagebox.showinfo("Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self)
            return
        UntrackIgnoredDialog(self, path, files)

    def _do_untrack_ignored(self, path: str, files: list):
        """Worker: run `git rm --cached -- <files>` in a background thread.

        Files are removed from the index (`git rm --cached`) but the
        working-tree copies are preserved — this is the safe "stop
        tracking but keep the file" operation. After completion, offers
        a commit prompt so the user can record the untracking in history.
        """
        if not files:
            return
        name = os.path.basename(path)
        self._log(f"Untracking {len(files)} file"
                  f"{'s' if len(files) != 1 else ''} in {name}…",
                  C["peach"])

        def worker():
            try:
                # `git rm --cached` accepts multiple paths after the `--` arg
                # separator. We always pass --cached so the working tree is
                # never touched (only the index).
                out, rc = self._shell_capture(
                    [GIT_EXE, "-C", path, "rm", "-r", "--cached", "--"] + files,
                    path)
                col = C["green"] if rc == 0 else C["red"]
                # Log a few output lines for visibility
                for line in out.strip().splitlines()[-6:]:
                    self._log(f"  {line}", col)
                if rc != 0:
                    return
                self._log(
                    f"  ✓ Untracked {len(files)} file"
                    f"{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self.after(0, self.refresh)
                # Offer to commit the untracking in the same flow.
                # Builds on the existing post-action commit prompt.
                self.after(0, lambda: self._offer_commit_after_change(
                    path,
                    f"untrack {len(files)} ignored file"
                    f"{'s' if len(files) != 1 else ''}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Git Commit ─────────────────────────────────────────────────────────────

    def cmd_git_commit(self):
        """Open the Git Commit dialog.

        Reused from BOTH the Projects-tab right-click menu AND the Git-tab
        Commit button. Prefer the Git tab's active path (set whenever the
        Git tab loads a project — via tab-switch or row-click) so the
        Git-tab button works without first round-tripping to the Projects tab.
        Falls back to the Projects-tab selection when no git path is set.
        """
        path = self._git._git_path or self._selected_path()
        if not path:
            return
        self._open_commit_dialog(path)

    def cmd_ai_code_review(self):
        """Open the AI Code Review dialog for the selected project.

        Stage 1 of the agentic-AI roadmap (see docs/ROADMAP.md). Shows the
        pending diff alongside an AI-generated structured review. Pure
        read-only — no tools, no autonomy, just a one-shot LLM call.
        Requires the LLM to be enabled in Settings → "AI commit messages".
        """
        path = self._git._git_path or self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} doesn't have a .git folder, "
                "so there's no pending diff to review.",
                parent=self)
            return
        llm_cfg = _cfg.get("commit_message_llm") or {}
        if not llm_cfg.get("enabled"):
            messagebox.showinfo(
                "AI is not enabled",
                "Open Settings → 'AI commit messages' and enable AI to use "
                "this feature.",
                parent=self)
            return
        AICodeReviewDialog(self, path, llm_cfg)

    def _open_commit_dialog(self, path: str):
        """Open GitCommitDialog for a given project path. Reused by
        `cmd_git_commit` (Projects-tab right-click) AND by the
        offer-commit-after-change flow that runs after Ensure .gitignore,
        Shadow Links, Scaffold, and Retrofit.

        Pre-flight check: if the project has tracked-but-ignored files,
        offer to run Untrack Ignored Files FIRST. Otherwise the commit
        attempt would inevitably hit git's "paths are ignored" error and
        the user would have to come back and untrack anyway.
        """
        if _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path)
            if stale:
                n = len(stale)
                preview = "\n".join(f"  • {f}" for f in stale[:5])
                if n > 5:
                    preview += f"\n  • …and {n - 5} more"
                choice = messagebox.askyesnocancel(
                    "Tracked-but-ignored files detected",
                    f"{n} file{'s' if n != 1 else ''} in this project "
                    f"{'are' if n != 1 else 'is'} tracked by git BUT also "
                    "match a .gitignore rule. Committing in this state "
                    "usually surfaces git's 'paths are ignored' error and "
                    "blocks the commit.\n\n"
                    f"Affected:\n{preview}\n\n"
                    "Yes  → run 🧹 Untrack Ignored Files first (recommended)\n"
                    "No   → open the commit dialog anyway\n"
                    "Cancel → close, do nothing",
                    parent=self)
                if choice is None:   # Cancel
                    return
                if choice:           # Yes — untrack first, that flow then
                                     # offers a fresh commit prompt of its own
                    UntrackIgnoredDialog(self, path, stale,
                        reason="tracked but listed in .gitignore "
                               "(blocks commit until untracked)")
                    return
        # No conflicts, OR user chose to proceed anyway
        status_out, _ = self._shell_capture(
            [GIT_EXE,"-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit)

    def _offer_commit_after_change(self, path: str, summary_label: str):
        """After a destructive manager action (Ensure .gitignore, Shadow Links,
        Scaffold, Retrofit, CodeGraph Init), check whether the working tree is
        now dirty and offer the user a commit dialog if so.

        Uses _is_local_git_repo (strict — does NOT walk upward) so a project
        nested inside an unrelated parent git repo never triggers a ghost
        commit-prompt against the wrong repository.

        Silent no-ops:
          - Path is not a git repo root locally (nothing to commit here)
          - Working tree is still clean (the operation didn't change anything)
        """
        if not _is_local_git_repo(path):
            return
        # Check whether the working tree is actually dirty
        status_out, _ = self._shell_capture(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self):
            self._open_commit_dialog(path)
        else:
            self._log(f"  Working tree left dirty — commit when you're ready.",
                      C["yellow"])

    def _do_git_commit(self, path: str, message: str, selected: list):
        """Stage and commit the picked files. `selected` is a list of
        (filename, xy) tuples from the GitCommitDialog.

        Key design points learned the hard way:

        1. NO `git reset` at start. The original implementation reset the
           index "to prevent stale staging from sneaking in", but that
           UNDOES any intentional staging — most importantly the
           `git rm --cached` queued by 🧹 Untrack Ignored Files. Result
           was a cycle where untracked-but-ignored files re-staged
           themselves every commit attempt.

        2. Don't blindly `git add` every selected file. Files that are
           already staged (xy column 1 == ' ', meaning no working-tree
           change) don't need re-adding. For staged DELETIONS (xy = "D ")
           specifically, calling git add UN-DOES the deletion and tries
           to re-add the file — which fails if the file matches a
           .gitignore rule. Filter to only git-add files with actual
           working-tree changes (xy[1] != ' ').

        3. Use `git commit -m <msg> -- <paths>` (path-specific commit)
           instead of plain `git commit -m <msg>`. The plain form
           commits the entire index; path-specific commits only the
           listed paths regardless of other staged work. This is what
           makes (1) work cleanly.
        """
        if not selected:
            return
        # Backward-compat: callers passing legacy list-of-strings still
        # work; treat unknown XY as needs-add (worst case is a redundant
        # git add, which is idempotent).
        if selected and isinstance(selected[0], str):
            selected = [(fname, "??") for fname in selected]

        name = os.path.basename(path)
        all_paths = [fname for fname, _xy in selected]
        # xy[1] == ' ' means "no working-tree change" — file is fully
        # captured in the index already (could be a staged D, A, M, R,
        # etc.). Those don't need git add. Everything else does.
        files_to_add = [fname for fname, xy in selected
                        if len(xy) >= 2 and xy[1] != ' ']

        # Lock Git tab buttons during the commit (prevents double-click races).
        self._git._git_begin_op()

        def worker():
            try:
                # 1. Stage only the files that have working-tree changes.
                # Already-staged files (including queued `git rm --cached`
                # deletions) are left alone.
                if files_to_add:
                    out, rc = self._shell_capture(
                        [GIT_EXE,"-C", path, "add", "--"] + files_to_add, path)
                    if rc != 0:
                        # Detect the "tracked-but-ignored" case (a file is
                        # in the index AND matches .gitignore) and surface
                        # an actionable recovery message.
                        if "ignored by one of your .gitignore files" in out:
                            offending = []
                            for ln in out.splitlines():
                                ln = ln.strip()
                                if (ln and not ln.startswith("hint:")
                                        and not ln.startswith("The following")
                                        and not ln.startswith("warning:")):
                                    offending.append(ln)
                            self.after(0, lambda: messagebox.showwarning(
                                "Tracked-but-ignored files",
                                "Some of the files you selected are already "
                                "tracked by git AND match a .gitignore rule. "
                                "Git refuses to re-add them in this state.\n\n"
                                f"Affected paths:\n  " + "\n  ".join(offending[:10])
                                + ("\n  …" if len(offending) > 10 else "")
                                + "\n\nFix: right-click the project → "
                                "🧹 Untrack Ignored Files… → untrack those "
                                "paths first. Then commit the result.",
                                parent=self))
                        else:
                            self.after(0, lambda: self._log(
                                f"git add failed: {out.strip()}", C["red"]))
                        return

                # 2. Commit ONLY the selected paths (includes any
                # already-staged ones that we didn't re-add — like
                # untracking-deletions queued via `git rm --cached`).
                self._log(f"[{name}] Committing ({len(all_paths)} file"
                          f"{'s' if len(all_paths) != 1 else ''})…",
                          C["peach"])
                commit_cmd = [GIT_EXE,"-C", path, "commit", "-m", message,
                              "--"] + all_paths
                cout, crc = self._shell_capture(commit_cmd, path)
                col = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", col))
                self.after(0, self.refresh)
            finally:
                self.after(0, self._git._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_force_sync(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self,
        ):
            self._run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def cmd_doctor(self):
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        # Stream output through the normal _log path AND accumulate so we
        # can scan for the "N stale project(s) in global DB" warning
        # tokensave emits when registered projects have their .tokensave/
        # folders deleted. When detected, offer to re-invoke doctor with
        # `y` piped to stdin (which tokensave reads to confirm the purge
        # when its interactive prompt fires — the same prompt the user
        # would see by running `tokensave doctor` from a terminal).
        self._run_doctor_with_purge_offer(path)

    def _run_doctor_with_purge_offer(self, path: str):
        """Run `tokensave doctor`, stream output, and after completion
        offer to purge any stale global-DB entries the warning surfaced.

        Variant of self._run that also captures the output text for
        post-completion parsing. Kept separate from _run rather than
        adding an on_complete callback to avoid disturbing the
        commit/sync paths that already depend on _run's exact shape.
        """
        label = os.path.basename(path)

        def worker():
            cmd_str = "tokensave doctor"
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            output_lines: list[str] = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")

                # Parse for stale-entry warning.
                stale_paths = self._extract_doctor_stale_paths(output_lines)
                if stale_paths and proc.returncode == 0:
                    self.after(0, self._offer_doctor_purge,
                               path, stale_paths)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in cmd_doctor")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)

        threading.Thread(target=worker, daemon=True,
                         name="doctor-worker").start()

    @staticmethod
    def _extract_doctor_stale_paths(output_lines: list[str]) -> list[str]:
        """Parse tokensave doctor's stdout for the stale-entries section.

        The relevant chunk looks like:

            ! 2 stale project(s) in global DB (registered but `.tokensave/` is gone):
                • D:\\Claude Co worker\\Token Save
                • D:\\Games\\Doom wads\\My Projects\\Doom RPG MOD\\X
                  Re-run `tokensave doctor` interactively to purge them.

        Returns the list of paths after the bullets, or [] if no stale
        warning was found. Bullet detection is permissive — accepts `•`,
        `*`, or `-` prefixes after optional leading whitespace.
        """
        bullet_re = re.compile(r"^\s*[•\*\-]\s+(.+?)\s*$")
        in_block = False
        paths: list[str] = []
        for line in output_lines:
            if "stale project" in line and "global DB" in line:
                in_block = True
                continue
            if not in_block:
                continue
            # End the block when we hit the "Re-run" suggestion line OR
            # a section header (anything starting with bold-ANSI-stripped
            # text in a known section).
            if "Re-run" in line and "tokensave doctor" in line:
                break
            m = bullet_re.match(line)
            if m:
                paths.append(m.group(1).strip())
            elif paths and not line.startswith(" ") and not line.startswith("\t"):
                # Hit a new section after collecting some bullets.
                break
        return paths

    def _offer_doctor_purge(self, path: str, stale_paths: list[str]):
        """Prompt the user with the list of stale entries and re-run
        doctor with stdin piped 'y' if they confirm."""
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (f"tokensave doctor found {n} stale project entr"
               f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
               f"{bullets}\n\n"
               "These projects were registered but their `.tokensave/` "
               "folders are gone — most likely deleted folders.\n\n"
               "Purge them now?  The manager will re-run `tokensave "
               "doctor` with `y` piped to confirm the interactive "
               "purge prompt.")
        if not messagebox.askyesno(
                "Purge stale tokensave projects?",
                msg, parent=self):
            self._log("  (purge skipped — stale entries left in place)",
                     C["overlay0"])
            return
        self._run_doctor_purge(path)

    def _run_doctor_purge(self, path: str):
        """Run `tokensave doctor` with `y\\n` piped repeatedly to stdin
        so tokensave's interactive purge prompt fires and is confirmed.

        Caveat: tokensave's prompt may use `is_terminal()` to decide
        whether to ask at all. If that's the case under our piped
        stdin, the second doctor run will just print the same stale
        warning again and exit without purging. We surface that
        outcome to the user via the log so they know the manager-side
        purge isn't possible — fall back to running `tokensave doctor`
        from a real terminal."""
        label = "doctor (purge)"

        def worker():
            self._log(f"$ tokensave doctor  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            captured: list[str] = []
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                # Send several yes-newlines in case doctor asks more than
                # one yes/no question. Closing stdin after to signal EOF.
                try:
                    proc.stdin.write("y\ny\ny\ny\ny\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    captured.append(stripped)
                    self._log(stripped)
                proc.wait()
                self._log(
                    "Done." if proc.returncode == 0
                    else f"Exited with code {proc.returncode}",
                    C["green"] if proc.returncode == 0 else C["red"])

                # Did the purge actually work?  If the second run STILL
                # reports stale entries, tokensave's prompt requires a
                # real TTY and stdin-piping isn't enough — offer to
                # open cmd.exe with the doctor command pre-typed so the
                # user can answer 'y' themselves.
                still_stale = self._extract_doctor_stale_paths(captured)
                if still_stale:
                    self._log(
                        f"  ⚠ Purge didn't take — tokensave still "
                        f"reports {len(still_stale)} stale entr"
                        f"{'y' if len(still_stale) == 1 else 'ies'}. "
                        f"tokensave doctor needs a real terminal "
                        f"(piped stdin doesn't trigger the prompt).",
                        C["peach"])
                    self.after(0, self._offer_doctor_in_cmd, path,
                               len(still_stale))
                else:
                    self._log("  ✓ Stale entries purged.", C["green"])
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in doctor purge")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)

        threading.Thread(target=worker, daemon=True,
                         name="doctor-purge").start()

    def _offer_doctor_in_cmd(self, path: str, n_stale: int):
        """Pop a follow-up dialog: when the piped-stdin purge fails,
        offer to spawn cmd.exe with `tokensave doctor` already running
        so the user has a real TTY to answer the interactive 'y' prompt.

        This is the cleanest fallback for tokensave's TTY-gated purge
        without us reaching into the global.db directly. The new cmd
        window stays open (cmd /k) so the user can see the output and
        type their answer.
        """
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The piped-stdin purge didn't work — tokensave needs "
                f"a real terminal for its interactive 'y/n' prompt.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self):
            self._log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return

        # Launch cmd.exe in a new console window with tokensave doctor
        # already running. /k keeps the window open after the command
        # finishes so the user has time to read + close manually.
        #
        # Critical Windows-quoting note: we pass the command as a SINGLE
        # STRING (not a list) so Python's subprocess doesn't apply its
        # own list2cmdline quoting on top of cmd.exe's. The earlier
        # list-form `["cmd.exe", "/k", f'"{TOKENSAVE}" doctor']`
        # produced `\"D:/path/tokensave.exe\" doctor` (backslash-escaped
        # quotes) — cmd doesn't recognise those as quote pairs and
        # errors with `'\"D:/...\" is not recognized as an internal or
        # external command`.
        #
        # The pattern `cmd /k ""path with spaces" doctor"` is cmd.exe's
        # idiom for running quoted paths via /k: the OUTER pair of `"`s
        # marks the start/end of the /k command region, the INNER pair
        # quotes the path. Cmd strips the outermost pair on /k entry,
        # leaving `"path with spaces" doctor` to execute.
        try:
            cmd_line = f'cmd.exe /k ""{TOKENSAVE}" doctor"'
            subprocess.Popen(
                cmd_line,
                cwd=path,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self._log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
        except OSError as e:
            self._log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    # ── CodeGraph commands ─────────────────────────────────────────────────

    def cmd_codegraph_init(self):
        """Right-click: initialise CodeGraph in the selected project.

        Uses `--index` so init also builds the initial graph in one step
        (matches the experience of tokensave init).
        """
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if _is_codegraph_project(path):
            messagebox.showinfo("Already initialised",
                f"{os.path.basename(path)} already has CodeGraph initialised.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph init in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "init", "--index", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._log(f"  {line}", col)
            self.after(0, self.refresh)
            # Offer to commit the new .codegraph/config.json if this is a
            # local git repo — uses _is_local_git_repo so a parent-directory
            # repo doesn't ghost-trigger the prompt.
            self.after(0, lambda: self._offer_commit_after_change(
                path, "CodeGraph init files"))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_sync(self):
        """Right-click: incrementally sync CodeGraph's index.

        Note: CodeGraph auto-syncs via its native file watcher while its
        MCP server is running inside a Claude Code session. This manual
        sync is useful for catching up after editing files with Claude
        Code closed.
        """
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph sync in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "sync", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._log(f"  {line}", col)
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_status(self):
        """Right-click: show CodeGraph stats for the selected project."""
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self)
            return
        name = os.path.basename(path)
        self._log(f"Running codegraph status in {name}…", C["peach"])

        def worker():
            out, rc = self._shell_capture(
                [CODEGRAPH_EXE, "status", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines():
                self._log(f"  {line}", col)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_remove(self):
        """Right-click: delete the .codegraph/ directory after confirmation."""
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        cg_dir = os.path.join(path, ".codegraph")
        if not os.path.isdir(cg_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no CodeGraph index.", parent=self)
            return
        if not messagebox.askyesno(
                "Remove CodeGraph index?",
                f"Delete the CodeGraph index for {name}?\n\n"
                f"This removes .codegraph/ from the project folder. Your "
                "source files are not touched. You can re-create the index "
                "later via 🧠 CodeGraph Init.",
                parent=self):
            return
        try:
            shutil.rmtree(cg_dir)
            self._log(f"  Removed .codegraph/ from {name}", C["green"])
        except OSError as e:
            self._log(f"  Could not remove .codegraph/: {e}", C["red"])
        self.refresh()

    def cmd_open_folder(self):
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self):
        path = self._selected_path()
        if not path:
            return
        editor_str = _cfg.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self,
            )

    def cmd_copy_path(self):
        path = self._selected_path()
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self._log(f"Copied: {path}", C["sky"])

    def cmd_remove(self):
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self.refresh()
        except Exception as e:
            self._log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self)

    def cmd_settings(self):
        SettingsDialog(self, _cfg, _save_config, self._on_settings_saved)

    def _on_settings_saved(self):
        global TOKENSAVE, TEMPLATE_DIR, SEARCH_ROOTS, GIT_EXE, CODEGRAPH_EXE
        global BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE
        TOKENSAVE     = _cfg.get("tokensave_exe", "")
        TEMPLATE_DIR  = _cfg.get("template_dir", "")
        SEARCH_ROOTS  = _cfg.get("search_roots", [])
        GIT_EXE       = _cfg.get("git_exe") or _detect_git()
        CODEGRAPH_EXE = _cfg.get("codegraph_exe") or _detect_codegraph()
        BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
        BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"
        self.refresh()
        self._log("Settings saved and applied.", C["green"])

    # ── Shadow Links ───────────────────────────────────────────────────────────

    def cmd_shadow_links(self):
        """Right-click: open the Shadow Links dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict, run_sync: bool = True):
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            try:
                self._log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and TOKENSAVE and created > 0:
                    self._log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._log(f"    {line}", col)

                self.after(0, self.refresh)
                self.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}." if run_sync and created > 0 else ""),
                    parent=self))
                # Shadow Links wrote hardlinks + .gitignore entries — offer commit
                self.after(0, lambda: self._offer_commit_after_change(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()

    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_assign_category(self):
        """Right-click: open the Assign Category dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Build sorted list of all categories and subcategories currently in use
        all_cats: list = []
        all_subs: dict = {}  # cat -> set of subcategories
        for r in SEARCH_ROOTS:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in _cfg.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = _cfg.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path, cat, subcat):
        """Callback from AssignCategoryDialog — update config and refresh."""
        proj_cats = _cfg.setdefault("project_categories", {})
        if cat is None:
            # Clear override
            proj_cats.pop(path, None)
            self._log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        _save_config(_cfg)
        self.refresh()

    # ── Scaffold / Retrofit ────────────────────────────────────────────────────

    def cmd_scaffold(self):
        folder = filedialog.askdirectory(title="Select folder to scaffold", parent=self)
        if not folder:
            return
        ScaffoldDialog(self, folder, self._scaffold_project)

    def cmd_retrofit(self):
        """Toolbar button — pick any folder then open the Retrofit dialog."""
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self)
        if not folder:
            return
        RetrofitDialog(self, folder, self._do_retrofit)

    def cmd_retrofit_selected(self):
        """Right-click menu — open the Retrofit dialog for the selected project directly."""
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self, path, self._do_retrofit)

    def _do_retrofit(self, path, add_tokensave, add_basic_instructions,
                     add_nuitka=False, add_shadow_links=False, shadow_ext_map=None,
                     add_git_hook=False):
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} nuitka={add_nuitka}")
                self._log(f"Retrofitting {name}…", C["peach"])
                actions_taken = []

                # ── Tokensave integration ──────────────────────────────────────
                if add_tokensave:
                    claude_md = os.path.join(path, "CLAUDE.md")
                    include_line = BASELINE_INCLUDE_LINE

                    if os.path.isfile(claude_md):
                        content = open(claude_md, encoding="utf-8", errors="ignore").read()
                        if "project-baseline.md" in content:
                            log.info("  CLAUDE.md already has @include — skipped")
                            self._log("  Tokensave already integrated in CLAUDE.md — skipped",
                                      C["overlay0"])
                        else:
                            with open(claude_md, "r+", encoding="utf-8") as f:
                                existing = f.read()
                                f.seek(0)
                                f.write(include_line + "\n\n" + existing)
                            log.info("  prepended @include to CLAUDE.md")
                            self._log("  Added tokensave @include to CLAUDE.md", C["green"])
                            actions_taken.append("Added tokensave rules to CLAUDE.md")
                    else:
                        with open(claude_md, "w", encoding="utf-8") as f:
                            f.write(
                                f"# {name} — Claude Instructions\n\n"
                                f"{include_line}\n"
                            )
                        log.info("  created CLAUDE.md with @include")
                        self._log("  Created CLAUDE.md with tokensave @include", C["green"])
                        actions_taken.append("Created CLAUDE.md with tokensave rules")

                # ── BASIC_INSTRUCTIONS.md ─────────────────────────────────────
                if add_basic_instructions:
                    basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
                    if os.path.isfile(basic_md):
                        log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
                        self._log("  BASIC_INSTRUCTIONS.md already exists — skipped",
                                  C["overlay0"])
                    else:
                        template = load_basic_instructions_template()
                        with open(basic_md, "w", encoding="utf-8") as f:
                            f.write(template)
                        log.info("  created BASIC_INSTRUCTIONS.md")
                        self._log("  Created BASIC_INSTRUCTIONS.md", C["green"])
                        actions_taken.append("Created BASIC_INSTRUCTIONS.md")

                # ── Nuitka build files ─────────────────────────────────────────
                if add_nuitka:
                    nuitka_actions = self._scaffold_nuitka_build(path)
                    actions_taken.extend(nuitka_actions)

                # ── Shadow extension links ─────────────────────────────────────
                if add_shadow_links:
                    ext_map = shadow_ext_map or DEFAULT_SHADOW_EXT_MAP
                    self._log("  Generating shadow extension links…", C["peach"])
                    created, skipped, failed = generate_shadow_links(path, ext_map)
                    update_gitignore_for_shadows(path, ext_map)
                    sl_msg = f"Shadow links: created {created}"
                    if skipped:
                        sl_msg += f", {skipped} already existed"
                    if failed:
                        sl_msg += f", {failed} failed"
                    self._log(f"  {sl_msg}", C["green"])
                    log.info(f"  {sl_msg}")
                    if created > 0:
                        actions_taken.append(sl_msg)

                # ── Auto-commit Stop hook ──────────────────────────────────────
                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._log(f"  {action}", C["green"])
                    actions_taken.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions_taken or 'nothing changed'}")
                self._log(f"Retrofit complete: {path}", C["green"])
                self.after(0, self.refresh)

                if actions_taken:
                    summary = "\n".join(f"  ✔ {a}" for a in actions_taken)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions_taken):
                        msg += "\n\nNext step: open build.ps1 and replace [ENTRY_SCRIPT] and [OUTPUT_NAME] before building."
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self.after(0, lambda: messagebox.showinfo("Retrofit complete", msg, parent=self))
                # If anything actually changed, offer to commit
                if actions_taken:
                    self.after(0, lambda: self._offer_commit_after_change(
                        path, "retrofit additions"))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror("Retrofit failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()


# ── Retrofit dialog ────────────────────────────────────────────────────────────

class RetrofitDialog(tk.Toplevel):
    """Small dialog with two checkboxes for the retrofit options."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Retrofit Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.callback = callback
        self.path = path

        pad = {"padx": 20, "pady": 8}

        tk.Label(self, text="Retrofit options",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 4))

        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(0, 10))

        # Checkbox: tokensave integration
        self.var_ts = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Add tokensave rules to CLAUDE.md",
            variable=self.var_ts,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Prepends an @include line so Claude always loads the\n"
                 "  tokensave lookup table. Non-destructive — existing content kept.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self.var_bi = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Also create BASIC_INSTRUCTIONS.md",
            variable=self.var_bi,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Drops a full project template (overview, architecture,\n"
                 "  key files, rules) for Claude to fill in on first use.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self.var_nuitka = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add Nuitka build files",
            variable=self.var_nuitka,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text=f"  Copies build templates from the templates folder.{chr(10)}"
                 "  Edit [ENTRY_SCRIPT] and [OUTPUT_NAME] in build.ps1 before building.\n"
                 f"  {nuitka_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Shadow extension links
        self.var_shadow = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Generate shadow extension links",
            variable=self.var_shadow,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # Count existing shadow files
        existing_shadows = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        shadow_note = (f"  {existing_shadows} shadow file(s) already exist."
                       if existing_shadows else
                       "  None exist yet — click Apply to create them.")
        tk.Label(self,
            text="  Creates NTFS hardlinks (.zs→.cpp, .zsc→.cpp, .acs→.c, DECORATE→.cpp)\n"
                 "  so tokensave can parse ZScript/ACS/DECORATE as C++/C. Zero disk cost.\n"
                 "  Adds gitignore patterns. Use 🔗 Shadow Links… for custom mappings.\n"
                 f"  {shadow_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Auto-commit Stop hook
        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_note = "  (already present)" if os.path.isfile(hook_settings) else "  (.claude/settings.json)"
        self.var_hook = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add auto-commit Stop hook",
            variable=self.var_hook,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Auto-commits when Claude finishes a session in this project.\n"
                 "  Only commits when the working tree has changes. Safe on clean repos.\n"
                 f"  {hook_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 12))

        # Buttons
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 16))

        ttk.Button(btn_frame, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        # Centre over parent
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        ts     = self.var_ts.get()
        bi     = self.var_bi.get()
        nuitka = self.var_nuitka.get()
        shadow = self.var_shadow.get()
        hook   = self.var_hook.get()
        self.destroy()
        if ts or bi or nuitka or shadow or hook:
            self.callback(self.path, ts, bi, nuitka, shadow, add_git_hook=hook)


# ── Scaffold dialog ────────────────────────────────────────────────────────────

class ScaffoldDialog(tk.Toplevel):
    """Options dialog shown before scaffolding a new project."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Scaffold Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self.path = path
        self.callback = callback

        name = os.path.basename(path)
        has_bi = os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))
        has_db = os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db"))

        pad = dict(padx=20, pady=6)

        # Folder display
        tk.Label(self, text="Folder", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=path, bg=C["surface0"], fg=C["text"],
                 font=("Consolas", 9), padx=10, pady=6,
                 wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=20, pady=(2, 10))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self._bi_var = tk.BooleanVar(value=not has_bi)
        bi_text = "Create BASIC_INSTRUCTIONS.md"
        bi_note = "  (already exists — will overwrite)" if has_bi else "  (Claude instruction template)"
        bi_frame = tk.Frame(self, bg=C["base"])
        bi_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(bi_frame, text=bi_text, variable=self._bi_var).pack(side=tk.LEFT)
        tk.Label(bi_frame, text=bi_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Checkbox: tokensave init
        self._init_var = tk.BooleanVar(value=not has_db)
        init_text = "Run tokensave init"
        init_note = "  (already indexed)" if has_db else "  (builds the code graph — ~10–30s)"
        init_frame = tk.Frame(self, bg=C["base"])
        init_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(init_frame, text=init_text, variable=self._init_var).pack(side=tk.LEFT)
        tk.Label(init_frame, text=init_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Info note
        tk.Label(self,
                 text="Project appears in the list immediately while indexing runs in the background.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420).pack(padx=20, pady=(4, 8))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self._nuitka_var = tk.BooleanVar(value=False)
        nuitka_frame = tk.Frame(self, bg=C["base"])
        nuitka_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(nuitka_frame, text="Add Nuitka build files",
                        variable=self._nuitka_var).pack(side=tk.LEFT)
        tk.Label(nuitka_frame, text=nuitka_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        tk.Label(self,
                 text="Copies build.ps1 + build.bat from templates. Edit [ENTRY_SCRIPT] and\n"
                      "[OUTPUT_NAME] in build.ps1 before running your first build.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 8))

        # Checkbox: auto-commit Stop hook
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_exists = os.path.isfile(hook_settings)
        hook_note = "  (already present)" if hook_exists else "  (.claude/settings.json)"
        self._hook_var = tk.BooleanVar(value=False)
        hook_frame = tk.Frame(self, bg=C["base"])
        hook_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(hook_frame, text="Add auto-commit Stop hook",
                        variable=self._hook_var).pack(side=tk.LEFT)
        tk.Label(hook_frame, text=hook_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Label(self,
                 text="  Auto-commits when Claude finishes a session in this project.\n"
                      "  Safe: only commits if the working tree has changes.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 12))

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _apply(self):
        create_bi       = self._bi_var.get()
        run_init        = self._init_var.get()
        scaffold_nuitka = self._nuitka_var.get()
        add_git_hook    = self._hook_var.get()
        self.destroy()
        self.callback(self.path, create_bi=create_bi, run_init=run_init,
                      scaffold_nuitka=scaffold_nuitka, add_git_hook=add_git_hook)


# ── Settings dialog ────────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    """Edit manager-config.json through the GUI."""

    def __init__(self, parent, cfg: dict, save_fn, callback, startup_note=""):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 500)
        self.geometry("760x700")
        self.grab_set()
        self.transient(parent)
        self._cfg = cfg
        self._save_fn = save_fn
        self._callback = callback

        # ── Scrollable content area ───────────────────────────────────────
        # Save/Cancel buttons stay anchored at the bottom (packed on `self`,
        # NOT on the scrollable body). All section content goes on `body`.
        _scroll_wrap = tk.Frame(self, bg=C["base"])
        _scroll_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        _canvas = tk.Canvas(_scroll_wrap, bg=C["base"], highlightthickness=0, bd=0)
        _vsb = ttk.Scrollbar(_scroll_wrap, orient="vertical", command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(_canvas, bg=C["base"])
        _body_window = _canvas.create_window((0, 0), window=body, anchor="nw")
        def _on_body_configure(event):
            _canvas.configure(scrollregion=_canvas.bbox("all"))
        def _on_canvas_configure(event):
            _canvas.itemconfigure(_body_window, width=event.width)
        body.bind("<Configure>", _on_body_configure)
        _canvas.bind("<Configure>", _on_canvas_configure)
        def _on_mousewheel(event):
            _canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        _canvas.bind("<MouseWheel>", _on_mousewheel)
        body.bind("<MouseWheel>", _on_mousewheel)

        if startup_note:
            tk.Label(body, text=startup_note,
                     bg=C["red"], fg=C["mantle"],
                     font=("Segoe UI", 9, "bold"),
                     justify=tk.LEFT, padx=14, pady=8,
                     wraplength=440).pack(fill=tk.X, pady=(0, 4))

        self._build_paths_section(body, cfg)
        self._build_git_tools_section(body, cfg)
        self._build_codegraph_section(body, cfg)
        self._build_roots_section(body, cfg)
        self._build_behavior_section(body, cfg)
        self._build_ai_section(body, cfg)

        # ── Save/Cancel — anchored outside the scroll area ────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(8, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _build_paths_section(self, body, cfg):
        """Tokensave exe, upgrade row, template dir, editor command."""
        def field_row(label, key, is_file=False, is_dir=False, note=""):
            tk.Label(body, text=label, bg=C["base"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(10, 0))
            row = tk.Frame(body, bg=C["base"])
            row.pack(fill=tk.X, padx=20, pady=(2, 0))
            var = tk.StringVar(value=cfg.get(key, ""))
            entry = ttk.Entry(row, textvariable=var, width=52)
            entry.pack(side=tk.LEFT, padx=(0, 6))
            def browse(v=var, f=is_file, d=is_dir):
                if f:
                    p = filedialog.askopenfilename(
                        title=f"Select {label}", filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                        initialfile=v.get(), parent=self)
                elif d:
                    p = filedialog.askdirectory(title=f"Select {label}", parent=self)
                else:
                    return
                if p:
                    v.set(p)
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT)
            if note:
                tk.Label(row, text=note, bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 0))
            return var

        self._exe_var = field_row("tokensave.exe  —  path to the tokensave binary",
                                  "tokensave_exe", is_file=True)

        # Upgrade tokensave row — ALWAYS shown (idempotent; reports "already
        # on latest" when no update is available). Promoted style when the app
        # has cached an available version from an "Update available" sync line.
        upgrade_row = tk.Frame(body, bg=C["base"])
        upgrade_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        host = self.master
        cur_ver = getattr(host, "_tokensave_current_version", None)
        new_ver = getattr(host, "_tokensave_available_version", None)
        cur_str = f"v{cur_ver}" if cur_ver else "version unknown"
        if new_ver:
            btn_label = f"🔄  Upgrade tokensave to v{new_ver}"
            btn_style = "Primary.TButton"
            hint = (f"  Current: {cur_str} → available: v{new_ver}.  "
                    "Replaces the binary; restart Claude after upgrading.")
            hint_fg = C["green"]
        else:
            btn_label = "🔄  Upgrade tokensave"
            btn_style = "TButton"
            hint = (f"  Current: {cur_str}.  Runs `tokensave upgrade` — "
                    "no-op if you're already on the latest release. "
                    "Restart Claude after a successful upgrade.")
            hint_fg = C["overlay0"]
        ttk.Button(upgrade_row, text=btn_label, style=btn_style,
                   command=host.cmd_upgrade_tokensave).pack(side=tk.LEFT)
        tk.Label(body, text=hint, bg=C["base"], fg=hint_fg,
                 font=("Segoe UI", 8), justify=tk.LEFT,
                 anchor=tk.W).pack(fill=tk.X, padx=20, pady=(0, 4))

        self._tmpl_var = field_row(
            "Template directory  —  folder containing claude-md-template.md and project-baseline.md",
            "template_dir", is_dir=True, note="(leave blank to auto-detect)")
        self._editor_var = field_row(
            "Editor command  —  launched by 'Open in Editor' (e.g. code, code --new-window, notepad)",
            "editor_cmd", note="(flags supported)")

    def _build_git_tools_section(self, body, cfg):
        """Git executable path + GitHub CLI install/detect."""
        # ── Git executable ────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(body, text="Git executable  —  path to git.exe",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        git_row = tk.Frame(body, bg=C["base"])
        git_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._git_exe_var = tk.StringVar(value=cfg.get("git_exe", ""))
        ttk.Entry(git_row, textvariable=self._git_exe_var, width=44).pack(side=tk.LEFT, padx=(0, 6))
        def _browse_git():
            p = filedialog.askopenfilename(
                title="Select git.exe",
                filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                initialdir=r"C:\Program Files\Git\cmd", parent=self)
            if p:
                self._git_exe_var.set(p)
                self._verify_git(p)
        def _autodetect_git():
            found = _detect_git()
            self._git_exe_var.set(found)
            self._verify_git(found)
        ttk.Button(git_row, text="Browse…", command=_browse_git).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(git_row, text="Auto-detect", command=_autodetect_git).pack(side=tk.LEFT, padx=(0, 6))
        self._git_status_lbl = tk.Label(git_row, text="", bg=C["base"],
                                        font=("Segoe UI", 8), fg=C["overlay0"])
        self._git_status_lbl.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(body, text="  Leave blank to auto-detect from PATH or common install locations.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        self.after(100, lambda: self._verify_git(cfg.get("git_exe") or GIT_EXE))

        # ── GitHub CLI (gh) ───────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        tk.Label(body, text="GitHub CLI (gh)  —  enables 'Open PR on GitHub' and release creation",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        gh_row = tk.Frame(body, bg=C["base"])
        gh_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._gh_status_lbl = tk.Label(gh_row, text="Checking…",
                                       bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8))
        self._gh_status_lbl.pack(side=tk.LEFT, padx=(0, 12))

        def _check_gh_status():
            found = _detect_gh()
            if found:
                self._gh_status_lbl.config(text=f"✓  {found}", fg=C["green"])
                self._gh_install_btn.configure(state=tk.DISABLED)
            else:
                self._gh_status_lbl.config(text="✗  not installed", fg=C["red"])
                self._gh_install_btn.configure(state=tk.NORMAL)

        def _install_gh():
            self._gh_status_lbl.config(
                text="Installing…  (this may take a minute, a UAC prompt may appear)",
                fg=C["peach"])
            self._gh_install_btn.configure(state=tk.DISABLED)
            def worker():
                try:
                    result = subprocess.run(
                        ["winget", "install", "--id", "GitHub.cli",
                         "--silent", "--accept-package-agreements",
                         "--accept-source-agreements"],
                        capture_output=True, text=True, timeout=180,
                        creationflags=CREATE_NO_WINDOW)
                    if result.returncode == 0:
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text="✓  Installed!  Restart TokenSave Manager to use gh features.",
                            fg=C["green"]))
                    else:
                        err = (result.stdout + result.stderr).strip()[-120:]
                        self.after(0, lambda: self._gh_status_lbl.config(
                            text=f"✗  Install failed (code {result.returncode}): {err}",
                            fg=C["red"]))
                        self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
                except Exception as ex:
                    self.after(0, lambda: self._gh_status_lbl.config(
                        text=f"✗  Error: {ex}", fg=C["red"]))
                    self.after(0, lambda: self._gh_install_btn.configure(state=tk.NORMAL))
            threading.Thread(target=worker, daemon=True).start()

        self._gh_install_btn = ttk.Button(gh_row, text="Install via winget", command=_install_gh)
        self._gh_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(gh_row, text="Check again", command=_check_gh_status).pack(side=tk.LEFT)
        tk.Label(body,
                 text="  Once installed, use the Git tab's '🔗 Open PR' button to create pull requests on GitHub.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(2, 0))
        self.after(150, _check_gh_status)

    def _build_codegraph_section(self, body, cfg):
        """CodeGraph executable path, install via npm, status check."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._cg_section = tk.Frame(body, bg=C["base"])
        self._cg_section.pack(fill=tk.X)
        tk.Label(self._cg_section,
                 text="CodeGraph (codegraph)  —  optional alternative code-graph tool",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)

        cg_path_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_path_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._cg_exe_var = tk.StringVar(value=cfg.get("codegraph_exe", ""))
        self._cg_exe_entry = ttk.Entry(cg_path_row, textvariable=self._cg_exe_var, width=44)
        self._cg_exe_entry.pack(side=tk.LEFT, padx=(0, 6))

        def _browse_cg():
            p = filedialog.askopenfilename(
                title="Select codegraph executable",
                filetypes=[("Executable", "*.cmd;*.exe;*.bat"), ("All", "*.*")],
                initialdir=os.path.expandvars(r"%APPDATA%\npm"), parent=self)
            if p:
                self._cg_exe_var.set(p)
                self._verify_codegraph(p)

        def _autodetect_cg():
            found = _detect_codegraph()
            if found:
                self._cg_exe_var.set(found)
                self._verify_codegraph(found)
            else:
                self._cg_status_lbl.config(text="✗  not installed", fg=C["red"])

        ttk.Button(cg_path_row, text="Browse…", command=_browse_cg).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_path_row, text="Auto-detect", command=_autodetect_cg).pack(side=tk.LEFT, padx=(0, 6))

        cg_install_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_install_row.pack(fill=tk.X, padx=20, pady=(6, 0))
        self._cg_status_lbl = tk.Label(cg_install_row, text="Checking…",
                                       bg=C["base"], fg=C["overlay0"],
                                       font=("Segoe UI", 8), justify=tk.LEFT,
                                       wraplength=420, anchor=tk.W)
        self._cg_status_lbl.pack(side=tk.LEFT, padx=(0, 12), fill=tk.X, expand=True)

        cg_btn_row = tk.Frame(self._cg_section, bg=C["base"])
        cg_btn_row.pack(fill=tk.X, padx=20, pady=(4, 0))
        npm_path = _detect_npm()

        def _check_cg_status():
            found = _detect_codegraph()
            if found:
                self._cg_status_lbl.config(text=f"✓  {found}", fg=C["green"])
                self._cg_install_btn.configure(state=tk.DISABLED)
                if not self._cg_exe_var.get():
                    self._cg_exe_var.set(found)
            else:
                self._cg_status_lbl.config(text="✗  not installed", fg=C["red"])
                state = tk.NORMAL if _detect_npm() else tk.DISABLED
                self._cg_install_btn.configure(state=state)

        def _cg_finish_install(ok: bool, msg: str):
            if ok:
                path = _detect_codegraph()
                if path:
                    self._cg_exe_var.set(path)
                    self._cg_status_lbl.config(text=f"✓  Installed — {path}", fg=C["green"])
                else:
                    self._cg_status_lbl.config(
                        text="✓  Installed.  Click 'Check again' to confirm.", fg=C["green"])
                self._cg_install_btn.configure(state=tk.NORMAL)
            else:
                self._cg_status_lbl.config(text=msg, fg=C["red"])
                self._cg_install_btn.configure(state=tk.NORMAL)
                if "\n" in msg:
                    messagebox.showerror("CodeGraph install failed", msg, parent=self)

        def _install_cg():
            npm = _detect_npm()
            if not npm:
                self._cg_status_lbl.config(
                    text="✗  npm not found — install Node.js 18+ first (https://nodejs.org)",
                    fg=C["red"])
                return
            self._cg_install_btn.configure(state=tk.DISABLED)
            self._cg_status_lbl.config(
                text="Installing…  (this may take a couple of minutes)", fg=C["yellow"])
            def worker():
                try:
                    result = subprocess.run(
                        [npm, "install", "-g", "@colbymchenry/codegraph"],
                        capture_output=True, text=True, timeout=300,
                        creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    self.after(0, lambda: _cg_finish_install(
                        ok=False, msg="Install timed out after 5 minutes."))
                    return
                except FileNotFoundError as e:
                    self.after(0, lambda: _cg_finish_install(ok=False, msg=f"npm not found: {e}"))
                    return
                if result.returncode == 0:
                    self.after(0, lambda: _cg_finish_install(ok=True, msg="✓ Installed successfully."))
                else:
                    err_text = (result.stderr or result.stdout or "").strip()
                    hint = ""
                    if "EPERM" in err_text or "EACCES" in err_text:
                        hint = ("\n\nThis usually happens when Node.js was "
                                "installed system-wide. Either run TokenSave "
                                "Manager as administrator OR reinstall "
                                "Node.js as a per-user install (the Node "
                                "installer offers this option).")
                    tail = "\n".join(err_text.splitlines()[-8:]) or "(no output)"
                    self.after(0, lambda: _cg_finish_install(
                        ok=False, msg=f"✗  Install failed (exit {result.returncode}):\n\n{tail}{hint}"))
            threading.Thread(target=worker, daemon=True).start()

        self._cg_install_btn = ttk.Button(cg_btn_row, text="Install via npm", command=_install_cg)
        self._cg_install_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(cg_btn_row, text="Check again", command=_check_cg_status).pack(side=tk.LEFT, padx=(0, 6))
        if not npm_path:
            self._cg_install_btn.configure(state=tk.DISABLED)
        tk.Label(self._cg_section,
                 text="  npm install -g @colbymchenry/codegraph  —  requires Node.js 18+ on PATH.\n"
                      "  Per-project actions live in the right-click menu (🧠 CodeGraph …).",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(4, 0))
        self.after(200, _check_cg_status)

    def _build_roots_section(self, body, cfg):
        """Search roots two-column Treeview (label + path)."""
        tk.Label(body,
                 text="Search roots  —  each root's label becomes a category in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))
        roots_frame = tk.Frame(body, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))
        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._roots_tv = ttk.Treeview(tv_wrap, columns=("label", "path"),
                                      show="headings", height=5, selectmode="browse")
        self._roots_tv.heading("label", text="Label")
        self._roots_tv.heading("path",  text="Path")
        self._roots_tv.column("label", width=130, stretch=False)
        self._roots_tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical", command=self._roots_tv.yview)
        self._roots_tv.configure(yscrollcommand=roots_vsb.set)
        self._roots_tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        for r in cfg.get("search_roots", []):
            self._roots_tv.insert("", tk.END, values=(_root_label(r), _root_path(r)))
        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",      command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label", command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",     command=self._remove_root).pack(fill=tk.X)

    def _build_behavior_section(self, body, cfg):
        """Auto-commit toggle, MCP integration status, Ollama shortcut."""
        # ── Auto-commit ───────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))
        self._var_autocommit = tk.BooleanVar(value=bool(cfg.get("auto_commit_after_sync", False)))
        tk.Checkbutton(body,
            text="Auto-commit after sync  (git add -A + git commit)",
            variable=self._var_autocommit,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
            text="  Only fires when the project is a git repo and the working tree has changes.\n"
                 "  Commit message: \"chore: tokensave sync\"  (or AI-generated if enabled below)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── MCP integration ───────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="MCP integration",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        mcp_row = tk.Frame(body, bg=C["base"])
        mcp_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(mcp_row, text="🔌  Manage MCP wiring…",
                   command=lambda: MCPConfigDialog(self)).pack(side=tk.LEFT)
        try:
            states = [_classify_mcp_entry(p)["state"] for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if states and all(s == "ok" for s in states):
            summary = "✓  Both Claude Desktop and Claude Code route through the wrapper."
            summary_fg = C["green"]
        elif "no_file" in states or "missing" in states:
            summary = "✗  One or more Claude configs need a tokensave entry."
            summary_fg = C["red"]
        elif any(s in ("direct_serve", "wrong_wrapper", "unparseable") for s in states):
            summary = "⚠  One or more Claude configs bypass the wrapper (★ pin won't work for them)."
            summary_fg = C["peach"]
        else:
            summary = ""
            summary_fg = C["overlay0"]
        if summary:
            tk.Label(body, text="  " + summary,
                     font=("Segoe UI", 9), bg=C["base"], fg=summary_fg,
                     justify=tk.LEFT, anchor=tk.W,
                     wraplength=620).pack(anchor=tk.W, padx=36, pady=(0, 2))
        tk.Label(body,
            text="  Routes tokensave through the manager's pin-aware wrapper so\n"
                 "  ★ Set as Active swaps projects live, without restarting Claude.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── Ollama ────────────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="Ollama", font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        ollama_row = tk.Frame(body, bg=C["base"])
        ollama_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(ollama_row, text="🦙  Manage Ollama Models…",
                   command=self._open_ollama_manager).pack(side=tk.LEFT)
        tk.Label(body,
            text="  Browse installed models, pull new ones, see context windows.\n"
                 "  Uses Ollama's native REST API at the base URL configured below.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _build_ai_section(self, body, cfg):
        """AI commit messages — provider, model, key env, presets, options."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="AI commit messages",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))

        llm_cfg = cfg.get("commit_message_llm") or {}
        self._var_llm_enabled = tk.BooleanVar(value=bool(llm_cfg.get("enabled", False)))
        tk.Checkbutton(body,
            text="Use AI to generate commit message suggestions",
            variable=self._var_llm_enabled,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 4))

        # Provider / model / key / base URL grid
        llm_grid = tk.Frame(body, bg=C["base"])
        llm_grid.pack(fill=tk.X, padx=36, pady=(0, 6))

        def _row(parent, label_txt, widget):
            row = tk.Frame(parent, bg=C["base"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label_txt, width=18, anchor=tk.W,
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["subtext"]).pack(side=tk.LEFT)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            return row

        self._var_llm_provider = tk.StringVar(value=llm_cfg.get("provider", "anthropic"))
        provider_box = ttk.Combobox(llm_grid, textvariable=self._var_llm_provider,
            values=["ollama", "anthropic", "openai", "openai_compatible"],
            state="readonly", width=22)
        _row(llm_grid, "Provider:", provider_box)

        self._var_llm_model = tk.StringVar(value=llm_cfg.get("model", "claude-haiku-4-5"))
        _row(llm_grid, "Model:", ttk.Entry(llm_grid, textvariable=self._var_llm_model))

        self._var_llm_keyenv = tk.StringVar(value=llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        _row(llm_grid, "API key env var:", ttk.Entry(llm_grid, textvariable=self._var_llm_keyenv))

        self._var_llm_base_url = tk.StringVar(value=llm_cfg.get("base_url", ""))
        _row(llm_grid, "Base URL:", ttk.Entry(llm_grid, textvariable=self._var_llm_base_url))

        # Quick presets
        preset_row = tk.Frame(body, bg=C["base"])
        preset_row.pack(anchor=tk.W, padx=36, pady=(0, 4))
        tk.Label(preset_row, text="Quick presets:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 8))

        def _probe_loaded_model(base_url: str) -> str:
            import urllib.request, urllib.error, json as _json
            try:
                req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError, _json.JSONDecodeError):
                return ""
            for m in (data.get("data") or []):
                mid = m.get("id", "")
                lid = mid.lower()
                if mid and "embed" not in lid and "rerank" not in lid and "whisper" not in lid:
                    return mid
            return ""

        def _apply_lm_studio():
            self._var_llm_provider.set("openai_compatible")
            base = "http://localhost:1234"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            detected = _probe_loaded_model(base)
            if detected:
                self._var_llm_model.set(detected)
                self._llm_preset_hint.configure(text=f"✓  Using loaded model: {detected}", fg=C["green"])
            else:
                self._llm_preset_hint.configure(
                    text="⚠  LM Studio server not reachable at http://localhost:1234 — "
                         "start the Local Server in LM Studio's '</>' panel and load a model, "
                         "then click this preset again.", fg=C["peach"])

        def _apply_ollama():
            self._var_llm_provider.set("ollama")
            base = "http://localhost:11434"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            detected = _probe_loaded_model(base)
            if detected:
                self._var_llm_model.set(detected)
                self._llm_preset_hint.configure(text=f"✓  Using Ollama model: {detected}", fg=C["green"])
            else:
                if not self._var_llm_model.get() or "claude" in self._var_llm_model.get():
                    self._var_llm_model.set("qwen2.5-coder:14b")
                self._llm_preset_hint.configure(
                    text="⚠  Ollama not reachable at http://localhost:11434 — "
                         "make sure the Ollama service is running and run "
                         "`ollama pull qwen2.5-coder:14b` (or any chat model), "
                         "then click this preset again.", fg=C["peach"])

        def _apply_anthropic():
            self._var_llm_provider.set("anthropic")
            self._var_llm_base_url.set("")
            self._var_llm_keyenv.set("ANTHROPIC_API_KEY")
            if not self._var_llm_model.get() or "/" in self._var_llm_model.get():
                self._var_llm_model.set("claude-haiku-4-5")
            self._llm_preset_hint.configure(
                text="ℹ  Set the ANTHROPIC_API_KEY environment variable (get a "
                     "key at console.anthropic.com).  Haiku is cheapest "
                     "(~$0.0005/commit); Sonnet/Opus are higher-fidelity.", fg=C["blue"])

        ttk.Button(preset_row, text="Anthropic", command=_apply_anthropic).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="LM Studio", command=_apply_lm_studio).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Ollama",    command=_apply_ollama).pack(side=tk.LEFT)

        # Preset feedback line — stays blank until user clicks a preset
        self._llm_preset_hint = tk.Label(body, text="", font=("Segoe UI", 8),
                                         bg=C["base"], fg=C["overlay0"],
                                         justify=tk.LEFT, wraplength=620, anchor=tk.W)
        self._llm_preset_hint.pack(anchor=tk.W, padx=36, pady=(2, 0), fill=tk.X)

        # Min diff lines
        min_row = tk.Frame(body, bg=C["base"])
        min_row.pack(anchor=tk.W, padx=36, pady=(2, 0))
        self._var_llm_min_diff = tk.StringVar(value=str(llm_cfg.get("min_diff_lines", 30)))
        tk.Label(min_row, text="Min diff lines (smaller commits skip AI):",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(min_row, textvariable=self._var_llm_min_diff, width=6).pack(side=tk.LEFT)

        self._var_llm_for_sync = tk.BooleanVar(value=bool(llm_cfg.get("use_for_sync_autocommit", False)))
        tk.Checkbutton(body,
            text="Also use AI for sync auto-commit messages (disables amend-stacking)",
            variable=self._var_llm_for_sync,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(6, 2))
        tk.Label(body,
            text="  AI runs only when toggled ON. Silent fallback on any error\n"
                 "  (missing key, network failure, timeout). Anthropic Claude Haiku\n"
                 "  costs ~$0.0005 per commit.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    def _open_ollama_manager(self):
        """Launch the Ollama Model Manager dialog.

        Uses whatever base URL is currently typed in the AI commit messages
        section (so editing the URL takes effect without saving Settings
        first). Falls back to http://localhost:11434 if blank. When the
        user clicks "Use for AI features" on a model in the dialog, the
        callback updates the provider/model/base-url fields in this very
        Settings dialog — they still have to click Save to persist.
        """
        base_url = self._var_llm_base_url.get().strip() \
                   or "http://localhost:11434"

        def _on_use(model_name: str, server_url: str):
            self._var_llm_provider.set("ollama")
            self._var_llm_model.set(model_name)
            self._var_llm_base_url.set(server_url)
            self._var_llm_keyenv.set("")
            # Auto-enable AI features when the user explicitly picks a model.
            self._var_llm_enabled.set(True)
            if hasattr(self, "_llm_preset_hint"):
                self._llm_preset_hint.configure(
                    text=f"✓  Using Ollama model: {model_name}.  "
                         f"Click Save to persist.",
                    fg=C["green"])

        OllamaModelManagerDialog(
            self, base_url=base_url, on_use_for_ai=_on_use)

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            f"Label for this category:\n(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self,
        )
        if lbl is None:
            return   # user cancelled
        self._roots_tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))

    def _edit_root_label(self):
        sel = self._roots_tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl  = self._roots_tv.set(iid, "label")
        new_lbl  = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self)
        if new_lbl is not None:
            self._roots_tv.set(iid, "label", new_lbl.strip() or cur_lbl)

    def _remove_root(self):
        sel = self._roots_tv.selection()
        if sel:
            self._roots_tv.delete(sel[0])

    def _verify_git(self, exe_path: str):
        """Run 'git --version' with the given path and update the status label."""
        if not exe_path:
            self._git_status_lbl.config(text="(will auto-detect on save)", fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW)
            version = result.stdout.strip() or result.stderr.strip()
            if result.returncode == 0:
                self._git_status_lbl.config(text=f"✓  {version}", fg=C["green"])
            else:
                self._git_status_lbl.config(text="✗  not found", fg=C["red"])
        except Exception:
            self._git_status_lbl.config(text="✗  not found", fg=C["red"])

    def _verify_codegraph(self, exe_path: str):
        """Run 'codegraph --version' with the given path; update status label."""
        if not exe_path:
            self._cg_status_lbl.config(text="(will auto-detect on save)",
                                        fg=C["overlay0"])
            return
        try:
            result = subprocess.run(
                [exe_path, "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW)
            version = (result.stdout or result.stderr).strip()
            if result.returncode == 0:
                self._cg_status_lbl.config(text=f"✓  {version or 'OK'}",
                                            fg=C["green"])
                self._cg_install_btn.configure(state=tk.DISABLED)
            else:
                self._cg_status_lbl.config(text="✗  not found at that path",
                                            fg=C["red"])
        except Exception:
            self._cg_status_lbl.config(text="✗  not found at that path",
                                        fg=C["red"])

    def _scroll_to_codegraph(self):
        """Pull the CodeGraph section into view + focus its path entry.

        SettingsDialog is non-scrollable (resizable=False, no canvas wrapper),
        so all sections are always rendered. focus_set on the path entry is
        enough — no yview math needed. Wrapped in try/except so any future
        layout change that introduces scrolling fails gracefully rather than
        crashing the install-nudge flow.
        """
        try:
            self._cg_exe_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _save(self):
        exe = self._exe_var.get().strip()
        if exe and not os.path.isfile(exe):
            messagebox.showwarning("Not found",
                f"tokensave.exe not found at:\n{exe}", parent=self)
            return
        self._cfg["tokensave_exe"] = exe
        self._cfg["template_dir"]  = self._tmpl_var.get().strip()
        self._cfg["editor_cmd"]    = self._editor_var.get().strip() or "code"
        # python_exe is intentionally not exposed in the UI (used by the .bat
        # launcher only); preserve whatever value is already in the config.
        self._cfg["search_roots"] = [
            {"path": self._roots_tv.set(iid, "path"),
             "label": self._roots_tv.set(iid, "label")}
            for iid in self._roots_tv.get_children()
        ]
        self._cfg["auto_commit_after_sync"] = self._var_autocommit.get()
        # Persist AI commit-message settings (preserves any unknown keys
        # the user may have added manually via JSON edit).
        existing_llm = self._cfg.get("commit_message_llm") or {}
        try:
            min_diff_lines = int(self._var_llm_min_diff.get())
        except ValueError:
            min_diff_lines = 30
        existing_llm.update({
            "enabled":     self._var_llm_enabled.get(),
            "provider":    self._var_llm_provider.get().strip() or "anthropic",
            "model":       self._var_llm_model.get().strip(),
            "api_key_env": self._var_llm_keyenv.get().strip(),
            "base_url":    self._var_llm_base_url.get().strip(),
            "min_diff_lines": max(0, min_diff_lines),
            "use_for_sync_autocommit": self._var_llm_for_sync.get(),
        })
        # Fill in defaults that other helpers expect
        existing_llm.setdefault("max_diff_chars", 24000)
        existing_llm.setdefault("timeout_seconds", 90)
        self._cfg["commit_message_llm"] = existing_llm
        self._cfg["git_exe"]       = self._git_exe_var.get().strip()
        self._cfg["codegraph_exe"] = self._cg_exe_var.get().strip()
        self._save_fn(self._cfg)
        self.destroy()
        self._callback()


# ── Snippet edit dialog ────────────────────────────────────────────────────────

class SnippetEditDialog(tk.Toplevel):
    """Add or edit a user-defined prompt snippet."""

    def __init__(self, parent, edit_meta, callback):
        """
        edit_meta: None for new snippet, or the _active_snippets_map entry for editing.
        callback(title, text, edit_meta): called on save; edit_meta is passed back.
        """
        super().__init__(parent)
        self.title("Add Snippet" if edit_meta is None else "Edit Snippet")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._callback = callback
        self._edit_meta = edit_meta

        pad = dict(padx=20, pady=4)

        tk.Label(self,
                 text="Add Snippet" if edit_meta is None else "Edit Snippet",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 8))

        # Title field
        tk.Label(self, text="Title", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._title_var = tk.StringVar(
            value=edit_meta["data"]["title"] if edit_meta else "")
        ttk.Entry(self, textvariable=self._title_var, width=52).pack(
            anchor=tk.W, padx=20, pady=(2, 6))

        # Body field
        tk.Label(self, text="Prompt text", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)

        body_wrap = tk.Frame(self, bg=C["mantle"])
        body_wrap.pack(fill=tk.X, padx=20, pady=(2, 12))
        vsb = ttk.Scrollbar(body_wrap, orient="vertical")
        self._body_txt = tk.Text(
            body_wrap, height=8, width=52,
            font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD,
            yscrollcommand=vsb.set,
        )
        vsb.configure(command=self._body_txt.yview)
        self._body_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        if edit_meta:
            self._body_txt.insert(tk.END, edit_meta["data"]["text"])

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        title = self._title_var.get().strip().replace("\n", " ")
        text  = self._body_txt.get("1.0", tk.END).strip()

        if not title:
            messagebox.showwarning("Empty title",
                "Please enter a title for this snippet.", parent=self)
            return
        if not text:
            messagebox.showwarning("Empty text",
                "Please enter the prompt text.", parent=self)
            return

        self.destroy()
        self._callback(title, text, self._edit_meta)


# ── Shadow Links dialog ────────────────────────────────────────────────────────

class ShadowLinksDialog(tk.Toplevel):
    """
    Configure and run shadow extension link generation for a project.
    Lets the user review/edit the extension map before applying.
    """

    def __init__(self, parent, path, callback):
        """
        callback(path, ext_map, run_sync): called on Apply.
        ext_map: dict mapping source extension → shadow suffix (e.g. {'.zsc': '.cpp'})
        """
        super().__init__(parent)
        self.title("Shadow Extension Links")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path = path
        self._callback = callback

        pad = dict(padx=20, pady=6)

        tk.Label(self,
                 text="🔗  Shadow Extension Links",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))

        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        tk.Label(self,
            text="Creates NTFS hardlinks with an appended extension so tokensave's\n"
                 "tree-sitter parsers can index non-standard file types. Hardlinks\n"
                 "cost zero extra disk space and update instantly with the source.",
            font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── Extension map editor ──
        tk.Label(self,
                 text="Mapping  (one per line:  .ext = .suffix  or  FILENAME = .suffix)",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        map_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        map_frame.pack(fill=tk.X, padx=20, pady=(0, 4))
        self._map_text = tk.Text(map_frame, height=6, width=36,
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, font=("Consolas", 10),
                                  padx=8, pady=6)
        self._map_text.pack(fill=tk.X)

        # Populate with DEFAULT_SHADOW_EXT_MAP
        for src_ext, tgt_suf in DEFAULT_SHADOW_EXT_MAP.items():
            self._map_text.insert(tk.END, f"{src_ext} = {tgt_suf}\n")

        tk.Label(self,
            text="  .ext = .suffix  →  extension match  (e.g. .txt = .cpp for HyperV files)\n"
                 "  NAME = .suffix  →  exact filename, case-insensitive  (e.g. DECORATE = .cpp)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        # ── Status summary ──
        existing = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        status_col = C["green"] if existing else C["overlay0"]
        status_txt = (f"✔  {existing} shadow file(s) already exist in this project."
                      if existing else "No shadow files found — none created yet.")
        tk.Label(self, text=status_txt,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=status_col).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))

        # ── Options ──
        self._var_sync = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Run tokensave sync after generating links",
                       variable=self._var_sync,
                       bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
                       activebackground=C["base"], activeforeground=C["text"],
                       font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(8, 16))

        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _parse_ext_map(self) -> dict:
        """Parse the text widget content into an ext_map dict.

        Two valid line formats:
          .ext = .suffix   → extension-based match (dot-prefixed key)
          NAME = .suffix   → exact filename match, case-insensitive (e.g. DECORATE)
        Lines starting with '#' and blank lines are ignored.
        """
        ext_map = {}
        for line in self._map_text.get("1.0", tk.END).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            src, _, tgt = line.partition("=")
            src = src.strip()
            tgt = tgt.strip()
            # tgt must be a dot-suffix; src can be a dot-extension OR a bare filename
            if tgt.startswith(".") and src:
                ext_map[src] = tgt
        return ext_map

    def _apply(self):
        ext_map = self._parse_ext_map()
        if not ext_map:
            messagebox.showwarning("No mappings",
                "Please define at least one extension mapping.", parent=self)
            return
        run_sync = self._var_sync.get()
        self.destroy()
        self._callback(self._path, ext_map, run_sync)


# ── Git Commit dialog ──────────────────────────────────────────────────────────

# ── Git helper dialogs ────────────────────────────────────────────────────────

class SetRemoteDialog(tk.Toplevel):
    """Connect a project to a GitHub repository by entering its HTTPS URL.

    Guides beginners through the three-step process: create repo on GitHub,
    copy the URL, paste here.
    Callback: callback(path, url)
    """

    def __init__(self, parent, path: str, current_url: str, callback):
        super().__init__(parent)
        self.title(f"Set Remote — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🔗  Connect to GitHub",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # Instructions
        instr = (
            "Steps:\n"
            "  1.  Go to  github.com/new  and create a new repository\n"
            "  2.  Copy the HTTPS URL from the repository page\n"
            "        (looks like: https://github.com/you/repo-name.git)\n"
            "  3.  Paste it in the box below and click Save"
        )
        tk.Label(self, text=instr,
                 font=("Segoe UI", 9), bg=C["base"], fg=C["text"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Remote URL:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._url_var = tk.StringVar(value=current_url)
        ttk.Entry(self, textvariable=self._url_var,
                  width=52).pack(anchor=tk.W, padx=20, pady=(4, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _save(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("URL required",
                "Please enter the GitHub repository URL.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@\n\n"
                "Example:  https://github.com/username/repo.git",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, url)


class MergePRDialog(tk.Toplevel):
    """Pick an open Pull Request and a merge strategy, then confirm.

    Callback: callback(path, pr_number, strategy, delete_branch, title)
      • path           — project root (used to scope the gh invocation)
      • pr_number      — int, the PR number to merge
      • strategy       — 'merge' | 'squash' | 'rebase' (matches gh flags)
      • delete_branch  — bool, whether to pass --delete-branch
      • title          — the PR title (just for logging)

    The list view shows: #N, title (truncated), source → base, +X/-Y.
    Selecting a row enables the action buttons. Three confirm buttons
    correspond to the three gh merge strategies; clicking one pops a
    final "Merge PR #N — <title> — into <base> using <strategy>?"
    confirmation before the callback fires.
    """

    def __init__(self, parent, path: str, prs: list[dict], callback):
        super().__init__(parent)
        self.title("Merge Pull Request")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(720, 380)
        self.geometry("840x460")
        self.grab_set()
        self.transient(parent)

        self._path = path
        self._prs = prs
        self._callback = callback
        # Tracks whether to also delete the source branch on GitHub
        # after a successful merge (the same toggle gh's web UI offers).
        self._var_delete_branch = tk.BooleanVar(value=True)

        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🐙  Merge Pull Request",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text=os.path.basename(path),
                 font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(
            side=tk.LEFT, padx=(10, 0))

        tk.Label(self,
            text=("Choose an open PR and a merge strategy.  After a "
                  "successful merge, your local clone is auto-switched "
                  "to the base branch and pulled."),
            font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W,
            wraplength=780).pack(fill=tk.X, padx=18, pady=(0, 8))

        # ── PR list (Treeview) ──────────────────────────────────────────
        list_frame = tk.LabelFrame(
            self, text=f" Open PRs ({len(prs)}) ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))

        tv_wrap = tk.Frame(list_frame, bg=C["mantle"])
        tv_wrap.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._tv = ttk.Treeview(
            tv_wrap,
            columns=("title", "branch", "diff"),
            show="tree headings", height=8)
        self._tv.heading("#0",      text="#")
        self._tv.heading("title",   text="Title")
        self._tv.heading("branch",  text="Source → Base")
        self._tv.heading("diff",    text="+/-")
        self._tv.column("#0",       width=60,  anchor=tk.E)
        self._tv.column("title",    width=380, anchor=tk.W)
        self._tv.column("branch",   width=210, anchor=tk.W)
        self._tv.column("diff",     width=110, anchor=tk.E)
        tv_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                command=self._tv.yview)
        self._tv.configure(yscrollcommand=tv_vsb.set)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv.bind("<<TreeviewSelect>>", self._on_select)

        for pr in prs:
            n      = pr.get("number", "?")
            title  = pr.get("title", "(no title)")
            if len(title) > 60:
                title = title[:57] + "…"
            head   = pr.get("headRefName", "?")
            base   = pr.get("baseRefName", "?")
            add    = pr.get("additions", 0)
            rem    = pr.get("deletions", 0)
            self._tv.insert("", tk.END, iid=str(n),
                             text=f"#{n}",
                             values=(title,
                                     f"{head} → {base}",
                                     f"+{add} -{rem}"))

        # ── Delete-branch toggle ────────────────────────────────────────
        opts_row = tk.Frame(self, bg=C["base"])
        opts_row.pack(fill=tk.X, padx=18, pady=(4, 2))
        tk.Checkbutton(opts_row,
            text="Also delete the source branch on GitHub after merge",
            variable=self._var_delete_branch,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # ── Strategy buttons + Close ────────────────────────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(2, 14))
        self._btn_merge = ttk.Button(
            btn_row, text="Merge commit",
            style="Primary.TButton",
            command=lambda: self._confirm("merge"),
            state=tk.DISABLED)
        self._btn_merge.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_squash = ttk.Button(
            btn_row, text="Squash and merge",
            command=lambda: self._confirm("squash"),
            state=tk.DISABLED)
        self._btn_squash.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_rebase = ttk.Button(
            btn_row, text="Rebase and merge",
            command=lambda: self._confirm("rebase"),
            state=tk.DISABLED)
        self._btn_rebase.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _on_select(self, _evt=None):
        sel = self._tv.selection()
        state = tk.NORMAL if sel else tk.DISABLED
        for b in (self._btn_merge, self._btn_squash, self._btn_rebase):
            b.configure(state=state)

    def _confirm(self, strategy: str):
        sel = self._tv.selection()
        if not sel:
            return
        try:
            pr_number = int(sel[0])
        except ValueError:
            return
        pr = next((p for p in self._prs if p.get("number") == pr_number),
                  None)
        if pr is None:
            return
        title = pr.get("title", "(no title)")
        head  = pr.get("headRefName", "?")
        base  = pr.get("baseRefName", "?")
        add   = pr.get("additions", 0)
        rem   = pr.get("deletions", 0)
        delete_branch = bool(self._var_delete_branch.get())

        strategy_name = {
            "merge":  "Merge commit",
            "squash": "Squash and merge",
            "rebase": "Rebase and merge",
        }.get(strategy, strategy)

        msg = (f"Merge PR #{pr_number} into {base}?\n\n"
               f"  Title:    {title}\n"
               f"  Source:   {head}\n"
               f"  Target:   {base}\n"
               f"  Changes:  +{add} / -{rem}\n"
               f"  Strategy: {strategy_name}\n"
               f"  Delete source branch: "
               f"{'yes' if delete_branch else 'no'}\n\n"
               "This runs `gh pr merge` and pushes the result to "
               "GitHub.  Your local master will then be switched-to "
               "and pulled so it reflects the merged state.")
        if not messagebox.askyesno(
                "Merge Pull Request?",
                msg, parent=self):
            return
        self.destroy()
        self._callback(self._path, pr_number, strategy, delete_branch,
                       title)


class NewBranchDialog(tk.Toplevel):
    """Create a new git branch, with an option to switch to it immediately.

    Callback: callback(path, branch_name, switch_immediately)
    """

    def __init__(self, parent, path: str, callback):
        super().__init__(parent)
        self.title(f"New Branch — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback

        tk.Label(self, text="🌿  New Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["green"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))

        tk.Label(self, text="Branch name:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)
        self._name_var = tk.StringVar()
        name_entry = ttk.Entry(self, textvariable=self._name_var, width=38)
        name_entry.pack(anchor=tk.W, padx=20, pady=(4, 10))
        name_entry.focus_set()

        self._switch_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Switch to this branch immediately",
            variable=self._switch_var,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Create", style="Primary.TButton",
                   command=self._create).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.bind("<Return>", lambda _: self._create())

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _create(self):
        name = self._name_var.get().strip()
        if not name:
            messagebox.showwarning("Name required",
                "Enter a branch name.", parent=self)
            return
        if " " in name:
            messagebox.showwarning("Invalid name",
                "Branch names cannot contain spaces.\n"
                "Try using a hyphen instead, e.g. my-feature",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, name, self._switch_var.get())


class SwitchBranchDialog(tk.Toplevel):
    """Select a branch to switch to from a list of local branches.

    Also used by cmd_git_delete_branch via the static pick() helper.
    Callback: callback(path, branch_name)
    """

    def __init__(self, parent, path: str, branches: list, current: str,
                 callback):
        super().__init__(parent)
        self.title(f"Switch Branch — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback
        self._result   = None

        tk.Label(self, text="🔀  Switch Branch",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["lavender"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        if current:
            tk.Label(self, text=f"Current: {current}",
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        lb_wrap = tk.Frame(self, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        self._lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                               bg=C["mantle"], fg=C["text"],
                               selectbackground=C["surface1"],
                               activestyle="none",
                               relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            self._lb.insert(tk.END, f"  {b}")
        self._lb.pack(padx=6, pady=6)
        self._lb.bind("<Double-Button-1>", lambda _: self._switch())

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Switch", style="Primary.TButton",
                   command=self._switch).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _switch(self):
        sel = self._lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected",
                "Select a branch first.", parent=self)
            return
        name = self._lb.get(sel[0]).strip()
        self.destroy()
        if self._callback:
            self._callback(self._path, name)

    @staticmethod
    def pick(parent, title: str, branches: list, parent_widget=None) -> str:
        """Synchronous branch picker — returns chosen branch name or ''."""
        result = [""]
        pw = parent_widget or parent

        def cb(path, name):
            result[0] = name

        dlg = SwitchBranchDialog.__new__(SwitchBranchDialog)
        tk.Toplevel.__init__(dlg, parent)
        dlg.title(title)
        dlg.configure(bg=C["base"])
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(parent)
        dlg._path     = ""
        dlg._callback = None
        dlg._result   = result

        tk.Label(dlg, text=f"Select a branch:",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(16, 8))
        lb_wrap = tk.Frame(dlg, bg=C["mantle"])
        lb_wrap.pack(padx=20, pady=(0, 14), fill=tk.X)
        lb = tk.Listbox(lb_wrap, font=("Consolas", 10),
                        bg=C["mantle"], fg=C["text"],
                        selectbackground=C["surface1"],
                        activestyle="none",
                        relief=tk.FLAT, bd=0, height=8, width=36)
        for b in branches:
            lb.insert(tk.END, f"  {b}")
        lb.pack(padx=6, pady=6)

        def confirm():
            sel = lb.curselection()
            if sel:
                result[0] = lb.get(sel[0]).strip()
            dlg.destroy()

        lb.bind("<Double-Button-1>", lambda _: confirm())
        btn_row = tk.Frame(dlg, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text=title.split()[0], style="Primary.TButton",
                   command=confirm).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        px, py = pw.winfo_x(), pw.winfo_y()
        pw2, ph = pw.winfo_width(), pw.winfo_height()
        dlg.geometry(f"{w}x{h}+{px + (pw2 - w) // 2}+{py + (ph - h) // 2}")

        parent.wait_window(dlg)
        return result[0]


# ── Assign Category dialog ────────────────────────────────────────────────────

class AssignCategoryDialog(tk.Toplevel):
    """Assign or override the category (and optional sub-category) for a project.

    Categories are sourced from search-root labels and existing overrides.
    Both comboboxes are editable so the user can type a new category/sub-category
    without any prior setup.

    Callback signature: callback(path, cat_or_None, subcat_str)
    Passing cat=None means "clear override" (restore root default).
    """

    def __init__(self, parent, path: str,
                 all_cats: list, subs_by_cat: dict,
                 current: dict, callback):
        super().__init__(parent)
        self.title("Assign Category")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._subs_map = subs_by_cat
        self._callback = callback

        pad = dict(padx=20, pady=4)

        # ── Title ──
        tk.Label(self, text="📁  Assign Category",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Category ──
        tk.Label(self, text="Category:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._cat_var = tk.StringVar(value=current.get("category", ""))
        self._cat_cb  = ttk.Combobox(self, textvariable=self._cat_var,
                                     values=all_cats, width=36)
        self._cat_cb.pack(anchor=tk.W, padx=20, pady=(0, 8))
        self._cat_cb.bind("<<ComboboxSelected>>", self._on_cat_changed)
        self._cat_var.trace_add("write", lambda *_: self._on_cat_changed())

        # ── Sub-category ──
        tk.Label(self, text="Sub-category:  (optional)",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._sub_var = tk.StringVar(value=current.get("subcategory", ""))
        self._sub_cb  = ttk.Combobox(self, textvariable=self._sub_var,
                                     values=subs_by_cat.get(self._cat_var.get(), []),
                                     width=36)
        self._sub_cb.pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Clear Override",
                   command=self._clear).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="OK", style="Primary.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _on_cat_changed(self, *_):
        cat  = self._cat_var.get()
        subs = self._subs_map.get(cat, [])
        self._sub_cb.configure(values=subs)

    def _ok(self):
        cat    = self._cat_var.get().strip()
        subcat = self._sub_var.get().strip()
        if not cat:
            messagebox.showwarning("Category required",
                "Enter a category name, or click 'Clear Override' to restore the default.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, cat, subcat)

    def _clear(self):
        self.destroy()
        self._callback(self._path, None, "")


class GitHubSetupDialog(tk.Toplevel):
    """Step-by-step GitHub setup wizard.

    Walks first-time users through: git identity → GitHub account →
    create repo → set remote URL → first push → optional GitHub Release.
    Each step shows a live status indicator (✅ / ⬜ / ℹ️) based on the
    current git state of the project.
    """

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self._app  = parent   # App instance — gives access to _shell_capture, _log, etc.
        self._path = path
        self.title("GitHub Setup")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(480, 500)
        self.grab_set()
        self.transient(parent)

        self._name_var      = tk.StringVar()
        self._email_var     = tk.StringVar()
        self._remote_var    = tk.StringVar()
        self._tag_var       = tk.StringVar(value="v1.0.0")
        self._rel_title_var = tk.StringVar(value="Release")

        # Scrollable area: canvas + scrollbar wrap the body Frame.
        # body is a child of self (not canvas) — keeps Windows rendering happy.
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        _vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = tk.Frame(self, bg=C["base"])
        self._body_id = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._body_id, width=e.width))
        self._body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        try:
            self._build()
            self._refresh()
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            messagebox.showerror(
                "GitHub Setup — build error",
                f"The wizard failed to render:\n\n{ex}\n\n{tb[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent window.
        content_h = self._body.winfo_reqheight() + 20
        max_h = max(400, parent.winfo_height() - 60)
        w, h = 520, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── shell helper (fast config/remote queries — main-thread OK) ───────────

    def _sh(self, cmd) -> tuple:
        return self._app._shell_capture(cmd, self._path)

    # ── build ────────────────────────────────────────────────────────────────

    def _build(self):
        body = self._body   # all widgets pack into the scrollable canvas child frame
        P    = dict(padx=20)

        # ── Header ───────────────────────────────────────────────────────────
        tk.Label(body, text="🐙  GitHub Setup",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 2), **P)
        tk.Label(body, text=os.path.basename(self._path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6), **P)
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Step 1: Git identity ─────────────────────────────────────────────
        self._s1_icon = self._step_header(body, "1",
            "Your name & email  (shown on every commit)")

        id_frame = tk.Frame(body, bg=C["surface0"], padx=10, pady=8)
        id_frame.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))

        for lbl_text, var in (("Name:", self._name_var), ("Email:", self._email_var)):
            row = tk.Frame(id_frame, bg=C["surface0"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=lbl_text, width=7, anchor=tk.W,
                     bg=C["surface0"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=30,
                      font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(id_frame, text="Save Identity",
                   command=self._save_identity).pack(anchor=tk.W, pady=(6, 0))

        # ── Step 2: Sign in / create GitHub account ──────────────────────────
        self._s2_icon = self._step_header(body, "2",
            "Sign in to GitHub  (or create a free account)")
        s2 = tk.Frame(body, bg=C["base"])
        s2.pack(anchor=tk.W, padx=(44, 20), pady=(2, 4))
        ttk.Button(s2, text="Sign in to GitHub →",
                   command=lambda: os.startfile("https://github.com/login")).pack(
                   side=tk.LEFT, padx=(0, 8))
        ttk.Button(s2, text="Create free account →",
                   command=lambda: os.startfile("https://github.com/signup")).pack(side=tk.LEFT)
        tk.Label(body,
                 text="If you already have an account, just sign in — no need to create one.",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=(44, 20), pady=(0, 10))

        # ── Step 3: Create repository ────────────────────────────────────────
        self._s3_icon = self._step_header(body, "3",
            "Create a new repository on GitHub")
        s3 = tk.Frame(body, bg=C["base"])
        s3.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s3,
                 text="Go to github.com/new, fill in the repo name, leave it Public.\n"
                      "Do NOT check 'Add README' or 'Add .gitignore' — you already\n"
                      "have those. Then copy the HTTPS URL it shows you.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        ttk.Button(s3, text="Open github.com/new →",
                   command=lambda: os.startfile("https://github.com/new")).pack(anchor=tk.W)

        # ── Step 4: Set remote URL ───────────────────────────────────────────
        self._s4_icon = self._step_header(body, "4",
            "Paste your repository URL here")
        s4 = tk.Frame(body, bg=C["base"])
        s4.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        url_row = tk.Frame(s4, bg=C["base"])
        url_row.pack(fill=tk.X)
        ttk.Entry(url_row, textvariable=self._remote_var, width=34,
                  font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Set", command=self._set_remote).pack(side=tk.LEFT)
        tk.Label(s4,
                 text="e.g. https://github.com/you/my-project.git",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(4, 0))

        # ── Step 5: Push ─────────────────────────────────────────────────────
        self._s5_icon = self._step_header(body, "5",
            "Upload your code to GitHub")
        s5 = tk.Frame(body, bg=C["base"])
        s5.pack(fill=tk.X, padx=(44, 20), pady=(2, 10))
        tk.Label(s5,
                 text="This sends all your commits to GitHub. The first time, a\n"
                      "browser window will open asking you to log in — that's normal.\n"
                      "After that, pushes happen silently.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        self._push_btn = ttk.Button(s5, text="⬆  Push to GitHub",
                                    command=self._do_push)
        self._push_btn.pack(anchor=tk.W)

        # ── Releases section ─────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
        tk.Label(body, text="📦  GitHub Releases  (share your built .exe)",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["peach"]).pack(anchor=tk.W, padx=20, pady=(0, 4))
        tk.Label(body,
                 text="A Release lets anyone download your .exe without needing Python.\n"
                      "Build dist\\ first (run build.bat), then tag a release here.",
                 font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        gh_on_path = bool(shutil.which("gh"))
        if gh_on_path:
            rel_grid = tk.Frame(body, bg=C["base"])
            rel_grid.pack(anchor=tk.W, padx=20, pady=(0, 6))
            for col, (lbl, var, w) in enumerate([
                    ("Tag:", self._tag_var, 9),
                    ("Title:", self._rel_title_var, 22)]):
                tk.Label(rel_grid, text=lbl, bg=C["base"], fg=C["text"],
                         font=("Segoe UI", 9)).grid(
                         row=0, column=col*2, sticky=tk.W,
                         padx=(0 if col == 0 else 12, 4))
                ttk.Entry(rel_grid, textvariable=var, width=w,
                          font=("Segoe UI", 9)).grid(row=0, column=col*2+1, sticky=tk.W)
            ttk.Button(body, text="📦  Create Release",
                       command=self._create_release).pack(anchor=tk.W, padx=20, pady=(0, 4))
        else:
            tk.Label(body,
                     text="Install GitHub CLI to enable one-click releases from here:",
                     font=("Segoe UI", 9), bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20)
            ttk.Button(body, text="Get GitHub CLI  (cli.github.com) →",
                       command=lambda: os.startfile("https://cli.github.com")).pack(
                       anchor=tk.W, padx=20, pady=(4, 4))
            tk.Label(body,
                     text="After installing, re-open this dialog to enable releases.",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(10, 10))
        ttk.Button(body, text="Close", command=self.destroy).pack(
            anchor=tk.E, padx=20, pady=(0, 16))

    def _step_header(self, parent, num: str, text: str) -> tk.Label:
        """Numbered step row — returns the icon label so caller can update it."""
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(0, 2))
        icon = tk.Label(row, text="⬜", bg=C["base"], font=("Segoe UI", 10))
        icon.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=f"Step {num} — {text}",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        return icon

    # ── Refresh: query git and update step icons ─────────────────────────────

    def _refresh(self):
        # Step 1 — git identity
        name_out,  _ = self._sh([GIT_EXE,"config", "--global", "user.name"])
        email_out, _ = self._sh([GIT_EXE,"config", "--global", "user.email"])
        name  = name_out.strip()
        email = email_out.strip()
        if not self._name_var.get():
            self._name_var.set(name)
        if not self._email_var.get():
            self._email_var.set(email)
        self._s1_icon.config(text="✅" if (name and email) else "⚠️")

        # Steps 2 & 3 — can't detect automatically; show info marker
        self._s2_icon.config(text="ℹ️")
        self._s3_icon.config(text="ℹ️")

        # Step 4 — remote
        remote_out, rrc = self._sh(
            [GIT_EXE,"-C", self._path, "remote", "get-url", "origin"])
        remote = remote_out.strip() if rrc == 0 else ""
        self._remote_var.set(remote)
        self._s4_icon.config(text="✅" if remote else "⬜")

        # Step 5 — push (only enabled when remote exists)
        self._s5_icon.config(text="⬜")
        self._push_btn.config(state=tk.NORMAL if remote else tk.DISABLED)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _save_identity(self):
        name  = self._name_var.get().strip()
        email = self._email_var.get().strip()
        if not name or not email:
            messagebox.showwarning("Incomplete",
                "Please enter both a name and an email address.", parent=self)
            return
        self._sh([GIT_EXE,"config", "--global", "user.name",  name])
        self._sh([GIT_EXE,"config", "--global", "user.email", email])
        self._refresh()
        messagebox.showinfo("Identity saved",
            f"Git will now sign commits as:\n{name} <{email}>", parent=self)

    def _set_remote(self):
        url = self._remote_var.get().strip()
        if not url:
            messagebox.showwarning("No URL",
                "Paste the HTTPS URL from your new GitHub repository.", parent=self)
            return
        if not (url.startswith("http") or url.startswith("git@")):
            messagebox.showwarning("Invalid URL",
                "The URL should start with https:// or git@", parent=self)
            return
        _, rrc = self._sh([GIT_EXE,"-C", self._path, "remote", "get-url", "origin"])
        if rrc == 0:
            self._sh([GIT_EXE,"-C", self._path, "remote", "set-url", "origin", url])
            self._app._log(f"  Remote updated: {url}", C["green"])
        else:
            self._sh([GIT_EXE,"-C", self._path, "remote", "add", "origin", url])
            self._app._log(f"  Remote added: {url}", C["green"])
        self._refresh()
        self._app.after(0, self._app._git.refresh)

    def _do_push(self):
        self.destroy()
        self._app._git.set_active_path(self._path)
        self._app._git.cmd_git_push()

    def _create_release(self):
        tag   = self._tag_var.get().strip()
        title = self._rel_title_var.get().strip() or tag
        if not tag:
            messagebox.showwarning("No tag",
                "Enter a version tag, e.g. v1.0.0", parent=self)
            return
        # Collect .exe files from dist\
        dist_dir  = os.path.join(self._path, "dist")
        exe_files = []
        if os.path.isdir(dist_dir):
            exe_files = [os.path.join(dist_dir, f)
                         for f in os.listdir(dist_dir) if f.endswith(".exe")]
        if not exe_files:
            if not messagebox.askyesno(
                    "No .exe files found",
                    "No .exe files found in dist\\\n\n"
                    "Run build.bat first to compile them.\n\n"
                    "Create a release without uploading any files anyway?",
                    parent=self):
                return
        cmd = ["gh", "release", "create", tag,
               "--title", title, "--generate-notes"] + exe_files
        self.destroy()
        self._app._log(f"Creating GitHub release {tag}…", C["peach"])
        def worker():
            out, rc = self._app._shell_capture(cmd, self._path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-6:]:
                self._app._log(f"  {line}", col)
            if rc == 0:
                self._app._log(f"  ✓ Release {tag} created — check GitHub!", C["green"])
        threading.Thread(target=worker, daemon=True).start()


@dataclasses.dataclass
class _ReleaseCtx:
    """Shared state bus passed between _pub_* step methods in ReleaseWizardDialog."""
    tag: str
    title: str
    notes: str
    zip_path: str | None = None
    notes_file: str | None = None
    staged_files: list = dataclasses.field(default_factory=list)


class ReleaseWizardDialog(tk.Toplevel):
    """One-button release wizard for tagged GitHub releases.

    Six stages stacked top-to-bottom in a scrollable canvas:

      1. Version          — auto-detected last tag + Patch/Minor/Major radio
                            with intelligent default + free-text override
      2. Title            — auto-filled from highest-priority commit
      3. Release notes    — auto-drafted from `<last_tag>..HEAD` via the
                            classifier; editable textarea
      4. Build step       — checkbox + auto-detect build.ps1 or build.bat
      5. Artefact         — read-only preview of dist/ contents + zip name
      6. CHANGELOG sync   — checkbox (only if CHANGELOG.md exists with the
                            `## [Unreleased]` anchor)

    Publish runs everything locally in one threaded worker — zero LLM calls.
    Errors short-circuit with copy-pasteable recovery commands so any
    partial state can be cleaned up by hand.

    Pre-flight (in `cmd_git_release`, before constructing this dialog):
      • gh on PATH
      • _is_local_git_repo(path)
      • has remote
      • working tree clean (CHANGELOG.md is fine to be dirty — we own it)
    """

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self._app  = parent
        self._path = path
        # _repo_name is shown in the dialog title / header — keep the
        # human-readable folder name. _release_basename is used for the
        # release zip filename and is derived from the git remote so it
        # matches the GitHub repo name (no spaces).
        self._repo_name        = os.path.basename(path)
        self._release_basename = _release_basename(path)

        self.title(f"Release Wizard — {self._repo_name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 600)
        self.grab_set()
        self.transient(parent)

        # ── Discover state up front ─────────────────────────────────────────
        # Sync tags from origin first so the detected "last tag" reflects any
        # releases created remotely (e.g. legacy `gh release create` calls
        # that never tagged locally). Silent + 5s timeout — never blocks
        # dialog open on flaky networks.
        _fetch_tags(path)
        self._last_tag       = _last_release_tag(path)
        self._commits        = _commits_since(path, self._last_tag)
        self._suggested_kind = _suggest_bump_kind(self._commits)
        self._build_script   = self._detect_build_script()
        self._changelog_path = os.path.join(path, "CHANGELOG.md")
        self._has_changelog  = (os.path.isfile(self._changelog_path)
                                and self._changelog_has_unreleased())

        # ── State vars ──────────────────────────────────────────────────────
        self._bump_var       = tk.StringVar(value=self._suggested_kind)
        self._override_var   = tk.StringVar(value="")
        self._title_var      = tk.StringVar(value="")
        self._run_build_var  = tk.BooleanVar(value=bool(self._build_script))
        self._sync_cl_var    = tk.BooleanVar(value=self._has_changelog)
        self._publishing     = False    # guard against double-clicks

        # ── Scrollable canvas (mirrors GitHubSetupDialog pattern) ───────────
        self._canvas = tk.Canvas(self, bg=C["base"], highlightthickness=0)
        _vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._body = tk.Frame(self, bg=C["base"])
        self._body_id = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._body_id, width=e.width))
        self._body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>",
            lambda e: self._canvas.unbind_all("<MouseWheel>"))

        try:
            self._build_ui()
            self._regenerate_notes()
        except Exception as ex:
            import traceback
            messagebox.showerror(
                "Release Wizard — build error",
                f"The wizard failed to render:\n\n{ex}\n\n"
                f"{traceback.format_exc()[-800:]}",
                parent=self)

        self.update_idletasks()
        # Open at content height, but never taller than parent.
        content_h = self._body.winfo_reqheight() + 30
        max_h = max(500, parent.winfo_height() - 40)
        w, h = 680, min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── Discovery helpers ──────────────────────────────────────────────────

    def _detect_build_script(self) -> str | None:
        """Return ``build.ps1`` or ``build.bat`` if either exists in the repo
        root, preferring .ps1. Returns None if no script is found."""
        for name in ("build.ps1", "build.bat"):
            if os.path.isfile(os.path.join(self._path, name)):
                return name
        return None

    def _changelog_has_unreleased(self) -> bool:
        try:
            with open(self._changelog_path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return False
        return re.search(r"(?m)^## \[Unreleased\]", text) is not None

    def _next_tag(self) -> str:
        """Compute the version tag from the override box or the bump radio."""
        override = self._override_var.get().strip()
        if override:
            # Ensure leading 'v' for consistency with existing tags.
            return override if override.startswith("v") else f"v{override}"
        prior = self._last_tag or "v0.0.0"
        bumped = _bump_version(prior, self._bump_var.get())
        # On a fresh repo with no tags, the first release should default to
        # v0.1.0 rather than v0.0.1 (which a patch-bump of v0.0.0 would give).
        if self._last_tag is None:
            return "v0.1.0"
        return bumped

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        body = self._body
        P = dict(padx=20)

        # Header ─────────────────────────────────────────────────────────────
        tk.Label(body, text="📦  Release Wizard",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, pady=(16, 0), **P)
        tk.Label(body, text=self._repo_name,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 4), **P)
        n = len(self._commits)
        prior = self._last_tag or "(none — first release)"
        tk.Label(body,
                 text=f"{n} commit{'s' if n != 1 else ''} since {prior}",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 10), **P)

        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 12))

        # ── 1. Version ──────────────────────────────────────────────────────
        self._section_header(body, "1", "Version")
        ver_frame = tk.Frame(body, bg=C["base"])
        ver_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 4))

        base_for_bump = self._last_tag or "v0.0.0"
        suggestions = [
            ("patch",  _bump_version(base_for_bump, "patch")),
            ("minor",  _bump_version(base_for_bump, "minor")),
            ("major",  _bump_version(base_for_bump, "major")),
            ("hotfix", _bump_version(base_for_bump, "hotfix")),
        ]
        hotfix_labels = {
            "patch":  "Patch",
            "minor":  "Minor",
            "major":  "Major",
            "hotfix": "Hotfix",
        }
        hotfix_blurb = {
            "patch":  "(bug fixes — new patch series)",
            "minor":  "(new feature, no breaking changes)",
            "major":  "(breaking changes)",
            "hotfix": "(small tweak on top of the current release — 4-part)",
        }
        for kind, candidate in suggestions:
            text = f"{hotfix_labels[kind]}  ({candidate})  {hotfix_blurb[kind]}"
            ttk.Radiobutton(ver_frame, text=text,
                            variable=self._bump_var, value=kind,
                            command=self._refresh_resolved_tag).pack(anchor=tk.W)

        ov_row = tk.Frame(body, bg=C["base"])
        ov_row.pack(fill=tk.X, padx=(44, 20), pady=(6, 2))
        tk.Label(ov_row, text="Custom tag:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        custom_entry = ttk.Entry(ov_row, textvariable=self._override_var,
                                 width=18, font=("Consolas", 9))
        custom_entry.pack(side=tk.LEFT)
        tk.Label(ov_row, text="e.g. v1.0.5 or 1.0.5 or v1.0.4.1",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(side=tk.LEFT, padx=(8, 0))

        # Live "Will publish as" preview — updates as the user types in the
        # custom field OR switches radios. Removes the "do I add the v?"
        # ambiguity entirely because the resolved tag is right there.
        self._resolved_lbl = tk.Label(body, text="",
                                       font=("Segoe UI", 9, "bold"),
                                       bg=C["base"], fg=C["green"])
        self._resolved_lbl.pack(anchor=tk.W, padx=(44, 20), pady=(2, 12))

        # Watch the override entry — every keystroke refreshes the preview.
        self._override_var.trace_add("write",
            lambda *_: self._refresh_resolved_tag())

        # ── 2. Title ────────────────────────────────────────────────────────
        self._section_header(body, "2", "Title")
        ttk.Entry(body, textvariable=self._title_var, font=("Segoe UI", 10)
                  ).pack(fill=tk.X, padx=(44, 20), pady=(0, 12))

        # ── 3. Release notes ────────────────────────────────────────────────
        self._section_header(body, "3", "Release notes  (editable)")

        # Helper text BELOW the section header but ABOVE the textarea, so
        # the instruction never leaks INTO the textarea (and from there into
        # the published release on GitHub).
        tk.Label(body,
                 text="The textarea is your release body verbatim. Edit before "
                      "publishing — add a one-line summary at the top, tweak "
                      "bullets, drop sections you don't want shipped.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 wraplength=600, justify=tk.LEFT
                 ).pack(anchor=tk.W, padx=(44, 20), pady=(0, 4))

        notes_row = tk.Frame(body, bg=C["base"])
        notes_row.pack(fill=tk.X, padx=(44, 20), pady=(0, 4))
        ttk.Button(notes_row, text="🔄  Regenerate from commits",
                   command=self._regenerate_notes).pack(side=tk.LEFT)
        ttk.Button(notes_row, text="📋  Copy to clipboard",
                   command=self._copy_notes).pack(side=tk.LEFT, padx=(6, 0))

        nt_wrap = tk.Frame(body, bg=C["mantle"])
        nt_wrap.pack(fill=tk.BOTH, expand=False, padx=(44, 20), pady=(4, 12))
        self._notes_txt = tk.Text(nt_wrap, height=14, font=("Consolas", 9),
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD)
        nt_vsb = ttk.Scrollbar(nt_wrap, orient="vertical",
                               command=self._notes_txt.yview)
        self._notes_txt.configure(yscrollcommand=nt_vsb.set)
        self._notes_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        nt_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── 4. Build ────────────────────────────────────────────────────────
        self._section_header(body, "4", "Build step")
        build_frame = tk.Frame(body, bg=C["base"])
        build_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 12))

        if self._build_script:
            cmd_preview = (
                f"powershell -ExecutionPolicy Bypass -File {self._build_script}"
                if self._build_script.endswith(".ps1")
                else f"cmd.exe /c {self._build_script}"
            )
            ttk.Checkbutton(build_frame,
                            text=f"Run build before release  ({self._build_script})",
                            variable=self._run_build_var).pack(anchor=tk.W)
            tk.Label(build_frame, text=f"Will invoke: {cmd_preview}",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W, pady=(2, 0))
        else:
            self._run_build_var.set(False)
            tk.Label(build_frame,
                     text="No build.ps1 / build.bat found in repo root.",
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W)
            tk.Label(build_frame,
                     text="Release will package whatever is in dist/ as-is.",
                     font=("Segoe UI", 8), bg=C["base"],
                     fg=C["overlay0"]).pack(anchor=tk.W)

        # ── 5. Artefact ─────────────────────────────────────────────────────
        self._section_header(body, "5", "Artefact")
        self._artefact_lbl = tk.Label(body, text="(will be computed at publish time)",
                                       font=("Segoe UI", 9), bg=C["base"],
                                       fg=C["subtext"], justify=tk.LEFT)
        self._artefact_lbl.pack(anchor=tk.W, padx=(44, 20), pady=(0, 12))
        self._refresh_artefact_preview()

        # ── 6. CHANGELOG sync ───────────────────────────────────────────────
        self._section_header(body, "6", "CHANGELOG.md sync")
        cl_frame = tk.Frame(body, bg=C["base"])
        cl_frame.pack(fill=tk.X, padx=(44, 20), pady=(0, 12))
        if self._has_changelog:
            ttk.Checkbutton(cl_frame,
                            text="Update CHANGELOG.md with the new version section",
                            variable=self._sync_cl_var).pack(anchor=tk.W)
            tk.Label(cl_frame,
                     text="Inserts (or replaces) the [<version>] section directly "
                          "below [Unreleased], then commits as `chore: release "
                          "prep for <tag>`.",
                     font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=520).pack(anchor=tk.W, pady=(2, 0))
        else:
            self._sync_cl_var.set(False)
            tk.Label(cl_frame,
                     text="CHANGELOG.md not found or missing the `## [Unreleased]` "
                          "anchor — sync disabled.",
                     font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"],
                     justify=tk.LEFT, wraplength=520).pack(anchor=tk.W)

        # ── Publish row ─────────────────────────────────────────────────────
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 12))
        btn_row = tk.Frame(body, bg=C["base"])
        btn_row.pack(anchor=tk.W, padx=20, pady=(0, 18))
        self._publish_btn = ttk.Button(btn_row, text="🚀  Publish",
                                       style="Primary.TButton",
                                       command=self._on_publish)
        self._publish_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Status label below buttons — updated as the worker progresses.
        self._status_lbl = tk.Label(body, text="",
                                     font=("Segoe UI", 9), bg=C["base"],
                                     fg=C["overlay0"], justify=tk.LEFT,
                                     wraplength=600)
        self._status_lbl.pack(anchor=tk.W, padx=20, pady=(0, 18))

    def _section_header(self, parent, num: str, text: str):
        row = tk.Frame(parent, bg=C["base"])
        row.pack(fill=tk.X, padx=20, pady=(4, 4))
        tk.Label(row, text=f"{num}.", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=text, bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)

    # ── Auto-fill / regenerate ─────────────────────────────────────────────

    def _regenerate_notes(self):
        """(Re)populate the notes textarea from the commit classifier.

        Does NOT inject any placeholder summary text — the helper label
        above the textarea is the user-facing instruction, and a published
        release should contain only the user's content. (Earlier versions
        injected an "Edit this summary…" line that some users shipped to
        GitHub by accident.)
        """
        sections = _classify_commits_for_changelog(self._commits)
        from datetime import datetime as _dt
        date_str = _dt.now().strftime("%Y-%m-%d")
        tag_clean = self._next_tag().lstrip("v")
        notes = _render_release_notes(tag_clean, date_str, sections)
        self._notes_txt.delete("1.0", tk.END)
        self._notes_txt.insert("1.0", notes)
        # Cascade — title, resolved-tag preview, and artefact label all
        # depend on the resolved tag. _refresh_resolved_tag handles them all.
        self._refresh_resolved_tag()

    def _refresh_title(self):
        # Default title: "v1.0.4 — <first non-summary heading or top item>".
        tag = self._next_tag()
        first = ""
        sections = _classify_commits_for_changelog(self._commits)
        for sec in ("Breaking", "Added", "Fixed", "Changed", "Docs", "Other"):
            if sections.get(sec):
                first = sections[sec][0]
                break
        if first:
            # Take the first 60 chars of the first commit subject as a hint.
            self._title_var.set(f"{tag} — {first[:60]}")
        else:
            self._title_var.set(f"{tag} — release")

    def _refresh_resolved_tag(self):
        """Update the live "Will publish as" preview + title + artefact name.

        Called whenever the user changes a version control (radio or custom
        entry). Centralises the "what tag are we actually publishing?"
        question — no matter how they pick the version, the resolved tag
        is visible in one place and propagates downstream automatically.
        """
        tag = self._next_tag()
        # Show the resolved tag prominently in green.
        if self._override_var.get().strip():
            self._resolved_lbl.config(
                text=f"  → Will publish as:  {tag}   (custom)",
                fg=C["green"])
        else:
            kind = self._bump_var.get()
            self._resolved_lbl.config(
                text=f"  → Will publish as:  {tag}   ({kind} bump)",
                fg=C["green"])
        # Cascade — title and artefact preview depend on the resolved tag.
        self._refresh_title()
        if hasattr(self, "_artefact_lbl"):   # guard during __init__ build order
            self._refresh_artefact_preview()

    def _refresh_artefact_preview(self):
        """Refresh the artefact preview label.

        Layout intent: surface the IMPORTANT files first — `.exe` artefacts
        are what users actually download — then directories, then everything
        else. Plain alphabetical sort buried the exes after `templates/` in
        the manager's own dist/ which was misleading. Sizes use `_fmt_size`
        so a 12 KB file shows as `12 KB`, not `0.0 MB`.
        """
        dist_dir = os.path.join(self._path, "dist")
        tag = self._next_tag()
        zip_name  = f"{self._release_basename}-{tag}-windows.zip"
        if not os.path.isdir(dist_dir):
            self._artefact_lbl.config(
                text=f"dist/ not found yet — will be created by the build step.\n"
                     f"Zip: {zip_name}")
            return
        try:
            all_entries = sorted(os.listdir(dist_dir))
        except OSError:
            all_entries = []
        if not all_entries:
            self._artefact_lbl.config(
                text=f"dist/ is empty (will be populated by build).\n"
                     f"Zip: {zip_name}")
            return

        # Priority bucket order: .exe → other files → directories.
        # Within each bucket, alphabetical.
        exes, files, dirs = [], [], []
        for name in all_entries:
            full = os.path.join(dist_dir, name)
            if os.path.isdir(full):
                dirs.append(name)
            elif name.lower().endswith(".exe"):
                exes.append(name)
            else:
                files.append(name)
        ordered = exes + files + dirs

        # Show first 8 entries; if more, append "+ N more".
        SHOW = 8
        bits = []
        for name in ordered[:SHOW]:
            full = os.path.join(dist_dir, name)
            if os.path.isdir(full):
                bits.append(f"{name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                bits.append(f"{name} ({_fmt_size(size)})")
        more = f"  + {len(ordered) - SHOW} more" if len(ordered) > SHOW else ""

        # Two lines for legibility — exes get their own line so they stand out.
        if exes:
            exe_bits  = [b for b in bits if any(b.startswith(e) for e in exes)]
            rest_bits = [b for b in bits if b not in exe_bits]
            lines = ["Files:"]
            lines.append(f"  {', '.join(exe_bits)}")
            if rest_bits:
                lines.append(f"  {', '.join(rest_bits)}{more}")
            elif more:
                lines.append(f"  {more.strip()}")
            lines.append(f"Zip: {zip_name}")
            self._artefact_lbl.config(text="\n".join(lines))
        else:
            # No exes found in dist/ yet (probably pre-build) — keep the
            # original single-line layout, but the build step will produce them.
            self._artefact_lbl.config(
                text=f"Files: {', '.join(bits)}{more}\n"
                     f"(no .exe yet — will be produced by the build step)\n"
                     f"Zip: {zip_name}")

    def _copy_notes(self):
        text = self._notes_txt.get("1.0", "end-1c")
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status_lbl.config(text="✔ Release notes copied to clipboard.",
                                     fg=C["green"])
        except tk.TclError as exc:
            self._status_lbl.config(text=f"Could not copy: {exc}", fg=C["red"])

    # ── Publish pipeline ───────────────────────────────────────────────────

    def _on_publish(self):
        if self._publishing:
            return
        tag = self._next_tag()
        title = self._title_var.get().strip() or tag
        notes = self._notes_txt.get("1.0", "end-1c").strip()
        if not tag.lstrip("v"):
            messagebox.showwarning("Tag required",
                "Pick a bump radio or enter a custom tag.", parent=self)
            return
        if not notes:
            messagebox.showwarning("Notes required",
                "Release notes cannot be empty. Click 🔄 Regenerate or edit "
                "the textarea.", parent=self)
            return

        # Confirm
        confirm = messagebox.askyesno(
            "Publish release",
            f"Publish {tag} to GitHub?\n\n"
            f"This will:\n"
            f"  • Run the build  ({'yes' if self._run_build_var.get() else 'no'})\n"
            f"  • Zip dist/ as the release artefact\n"
            f"  • {'Update CHANGELOG.md, commit, ' if self._sync_cl_var.get() else ''}"
            f"create local tag {tag}, push, then call gh release create.",
            parent=self)
        if not confirm:
            return

        self._publishing = True
        self._publish_btn.configure(state=tk.DISABLED)
        self._app._git._git_begin_op()

        threading.Thread(
            target=self._publish_worker,
            args=(tag, title, notes),
            daemon=True,
        ).start()

    def _set_status(self, text: str, fg: str = None):
        """Thread-safe status update."""
        def _do():
            self._status_lbl.config(text=text, fg=fg or C["subtext"])
        try:
            self.after(0, _do)
        except tk.TclError:
            pass  # dialog already destroyed

    def _fail(self, message: str, *, keep_temp: bool = False,
              temp_path: str | None = None):
        """Surface a copy-pasteable recovery message and end the op."""
        self._set_status(message, fg=C["red"])
        self._app._log(f"  ✗ Release aborted", C["red"])
        for line in message.splitlines():
            if line.strip():
                self._app._log(f"    {line}", C["red"])
        if keep_temp and temp_path:
            self._app._log(f"    Notes preserved at: {temp_path}", C["red"])
        # Re-enable buttons
        self._publishing = False
        def _reenable():
            try:
                self._publish_btn.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
            self._app._git._git_end_op()
        try:
            self.after(0, _reenable)
        except tk.TclError:
            pass

    # ── Publish pipeline step methods ─────────────────────────────────────────

    def _pub_build(self, ctx: "_ReleaseCtx") -> bool:
        """Step 1: run the optional build script. Returns False on failure."""
        if not (self._run_build_var.get() and self._build_script):
            return True
        log = self._app._log
        sh  = self._app._shell_capture
        self._set_status(f"Building via {self._build_script}…  "
                         "(this can take 3–8 minutes)", fg=C["peach"])
        log(f"Release {ctx.tag}: running {self._build_script}…", C["peach"])
        if self._build_script.endswith(".ps1"):
            build_cmd = ["powershell", "-ExecutionPolicy", "Bypass",
                         "-File", self._build_script]
        else:
            # .bat needs cmd.exe /c — running the .bat directly raises
            # WinError 193 ("not a valid Win32 application") on Windows.
            build_cmd = ["cmd.exe", "/c", self._build_script]
        out, rc = sh(build_cmd, self._path)
        for line in out.strip().splitlines()[-12:]:
            log(f"  {line}", C["overlay0"])
        if rc != 0:
            self._fail("Build failed — release aborted, no state changed.\n"
                       "See log panel for build tail.")
            return False
        return True

    def _pub_zip(self, ctx: "_ReleaseCtx") -> bool:
        """Step 2: zip dist/. Populates ctx.zip_path on success."""
        self._set_status("Zipping dist/…", fg=C["peach"])
        dist_dir = os.path.join(self._path, "dist")
        zip_name = f"{self._release_basename}-{ctx.tag}-windows.zip"
        zip_path = os.path.join(self._path, zip_name)
        zip_out  = _zip_dist(dist_dir, zip_path)
        if not zip_out:
            self._fail("Zip step failed — dist/ missing or empty.\n"
                       "Re-run with 'Run build' enabled, or build manually first.")
            return False
        try:
            mb = os.path.getsize(zip_out) / 1024 / 1024
        except OSError:
            mb = 0.0
        self._app._log(f"  zipped: {os.path.basename(zip_out)} ({mb:.1f} MB)",
                       C["green"])
        ctx.zip_path = zip_out
        return True

    def _pub_write_notes(self, ctx: "_ReleaseCtx") -> bool:
        """Step 3: write release notes to a temp file. Populates ctx.notes_file."""
        notes_fd, notes_file = tempfile.mkstemp(
            prefix=f"release-notes-{ctx.tag}-", suffix=".md", text=True)
        try:
            with os.fdopen(notes_fd, "w", encoding="utf-8") as f:
                f.write(ctx.notes)
        except OSError as exc:
            self._fail(f"Could not write temporary notes file: {exc}")
            return False
        ctx.notes_file = notes_file
        return True

    def _pub_patch_changelog(self, ctx: "_ReleaseCtx") -> bool:
        """Step 4: optionally stamp CHANGELOG.md with the release version."""
        if not (self._sync_cl_var.get() and self._has_changelog):
            return True
        self._set_status("Patching CHANGELOG.md…", fg=C["peach"])
        ok, msg = _patch_changelog(
            self._changelog_path,
            ctx.tag.lstrip("v"),
            datetime.now().strftime("%Y-%m-%d"),
            ctx.notes,
        )
        if not ok:
            self._fail(f"CHANGELOG patch failed: {msg}",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  CHANGELOG.md {msg}", C["green"])
        ctx.staged_files.append("CHANGELOG.md")
        return True

    def _pub_stage_commit(self, ctx: "_ReleaseCtx") -> bool:
        """Step 5: stage and commit only the files we touched (e.g. CHANGELOG)."""
        if not ctx.staged_files:
            return True
        sh   = self._app._shell_capture
        path = self._path
        self._set_status("Committing release-prep changes…", fg=C["peach"])
        out, rc = sh([GIT_EXE, "-C", path, "add", "--"] + ctx.staged_files, path)
        if rc != 0:
            self._fail(f"git add CHANGELOG.md failed (rc={rc}).\n{out[-300:]}",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        commit_cmd = ([GIT_EXE, "-C", path, "commit",
                       "-m", f"chore: release prep for {ctx.tag}", "--"]
                      + ctx.staged_files)
        out, rc = sh(commit_cmd, path)
        if rc != 0:
            self._fail(f"Commit failed (rc={rc}).\n{out[-300:]}\n\n"
                       "Recover: git restore --staged CHANGELOG.md",
                       keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log("  committed release-prep changes", C["green"])
        return True

    def _pub_tag(self, ctx: "_ReleaseCtx") -> bool:
        """Step 6: create a local annotated tag."""
        self._set_status(f"Creating local tag {ctx.tag}…", fg=C["peach"])
        out, rc = _git_tag(self._path, ctx.tag, ctx.title)
        if rc != 0:
            self._fail(
                f"git tag {ctx.tag} failed — possibly already exists locally.\n"
                f"Recover: git tag -d {ctx.tag}\n\n"
                f"git output:\n{out[-300:]}",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  created annotated tag {ctx.tag}", C["green"])
        return True

    def _pub_push(self, ctx: "_ReleaseCtx") -> bool:
        """Step 7: push commits + tag to origin in one round-trip."""
        self._set_status(f"Pushing {ctx.tag} to origin…", fg=C["peach"])
        out, rc = _git_push_with_tags(self._path)
        if rc != 0:
            self._fail(
                f"Push failed (rc={rc}).\n{out[-300:]}\n\n"
                f"Recover local state with:\n"
                f"  git tag -d {ctx.tag}                # remove local tag\n"
                f"  git reset --soft HEAD~1         # if release-prep commit was made",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        self._app._log(f"  pushed HEAD + {ctx.tag} to origin", C["green"])
        return True

    def _pub_gh_release(self, ctx: "_ReleaseCtx") -> bool:
        """Step 8: create the GitHub Release via `gh release create`."""
        # After this point, tag + commit are already on the remote, so a
        # failure here means manual recovery is needed.
        self._set_status(f"Creating GitHub release {ctx.tag}…", fg=C["peach"])
        gh_cmd = ["gh", "release", "create", ctx.tag,
                  "--title", ctx.title,
                  "--notes-file", ctx.notes_file,
                  ctx.zip_path]
        out, rc = self._app._shell_capture(gh_cmd, self._path)
        for line in out.strip().splitlines()[-8:]:
            self._app._log(f"  {line}", C["overlay0"])
        if rc != 0:
            cmd_str = (f"gh release create {ctx.tag} --title \"{ctx.title}\" "
                       f"--notes-file \"{ctx.notes_file}\" \"{ctx.zip_path}\"")
            self._fail(
                f"GitHub release creation failed AFTER the tag was pushed.\n\n"
                f"Local repo and remote ARE in sync — only the GitHub Release\n"
                f"page wasn't created. Recover with:\n\n"
                f"  {cmd_str}\n\n"
                f"Notes file preserved at: {ctx.notes_file}",
                keep_temp=True, temp_path=ctx.notes_file)
            return False
        return True

    def _publish_worker(self, tag: str, title: str, notes: str):
        """Sequenced publish pipeline. Runs entirely on a background thread."""
        ctx = _ReleaseCtx(tag=tag, title=title, notes=notes)
        steps = [
            self._pub_build,
            self._pub_zip,
            self._pub_write_notes,
            self._pub_patch_changelog,
            self._pub_stage_commit,
            self._pub_tag,
            self._pub_push,
            self._pub_gh_release,
        ]
        for step in steps:
            if not step(ctx):
                return

        # Step 9: cleanup notes temp file
        if ctx.notes_file:
            try:
                os.unlink(ctx.notes_file)
            except OSError:
                pass

        # Step 10: done — refresh git tab and close wizard
        self._app._log(f"  ✓ Release {ctx.tag} published — check GitHub!", C["green"])
        self._set_status(f"✔ Release {ctx.tag} published.", fg=C["green"])
        def _close():
            self._publishing = False
            self._app._git._git_end_op()
            self.destroy()
        try:
            self.after(2000, _close)
        except tk.TclError:
            pass


class UntrackIgnoredDialog(tk.Toplevel):
    """Checklist dialog for untracking files that are tracked-but-ignored.

    Shows every file returned by `git ls-files -ci --exclude-standard`
    (i.e. files in git's index whose path matches the project's .gitignore)
    with a checkbox. Untrack Selected runs `git rm --cached -- <files>`
    which removes them from the index without touching the working tree,
    so the local copies stay where they are.

    Triggered:
      - manually via right-click → 🧹 Untrack Ignored Files…
      - automatically via GitignoreDialog._on_save when new ignore rules
        match files that were already tracked

    On success, calls _offer_commit_after_change on the parent App so the
    user can immediately commit the untracking as one atomic change.
    """

    def __init__(self, parent, path: str, files: list, *,
                 reason: str = "tracked but listed in .gitignore",
                 on_confirm=None):
        super().__init__(parent)
        self._app      = parent
        self._path     = path
        self._files    = files
        self._on_confirm = on_confirm  # callable(path, files) or None → falls back to self._app._do_untrack_ignored
        name = os.path.basename(path)
        self.title(f"Untrack Ignored Files — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 380)
        self.grab_set()
        self.transient(parent)

        # ── Header ──
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🧹  Untrack Ignored Files", bg=C["base"],
                 fg=C["blue"], font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(hdr, text=name, bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, pady=(2, 0))

        # ── Explanation ──
        expl = tk.Frame(self, bg=C["base"])
        expl.pack(fill=tk.X, padx=18, pady=(0, 6))
        tk.Label(expl,
                 text=(
                   f"The following files are {reason}.\n\n"
                   "Untracking removes them from git's index (i.e. git stops "
                   "treating them as part of the project) but leaves the "
                   "files on your disk. Future modifications won't appear "
                   "in git status — which is what your .gitignore intends.\n\n"
                   "This is the standard fix for the 'I added a path to "
                   ".gitignore but git keeps showing it as modified' problem."
                 ),
                 bg=C["base"], fg=C["text"], font=("Segoe UI", 9),
                 justify=tk.LEFT, anchor=tk.W,
                 wraplength=500).pack(anchor=tk.W)

        # ── File checklist (scrollable) ──
        tk.Label(self, text="FILES TO UNTRACK",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Segoe UI", 8, "bold")).pack(anchor=tk.W, padx=18, pady=(8, 2))
        list_outer = tk.Frame(self, bg=C["mantle"])
        list_outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=180)
        _vsb = ttk.Scrollbar(list_outer, orient="vertical",
                              command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))

        self._file_vars: list = []
        if not files:
            tk.Label(self._list_body,
                     text="  (no tracked-but-ignored files found)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Consolas", 9, "italic"),
                     padx=10, pady=10).pack(anchor=tk.W)
        else:
            for fname in files:
                row = tk.Frame(self._list_body, bg=C["mantle"])
                row.pack(fill=tk.X, padx=4, pady=1)
                var = tk.BooleanVar(value=True)
                self._file_vars.append((var, fname))
                cb = tk.Checkbutton(row, variable=var, bg=C["mantle"],
                                     activebackground=C["mantle"],
                                     selectcolor=C["surface0"])
                cb.pack(side=tk.LEFT)
                tk.Label(row, text=fname, anchor=tk.W,
                         font=("Consolas", 9),
                         bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(4, 6))

        # ── Quick-select buttons ──
        if files:
            sel_row = tk.Frame(self, bg=C["base"])
            sel_row.pack(fill=tk.X, padx=18, pady=(0, 8))
            ttk.Button(sel_row, text="Select All",
                       command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Select None",
                       command=lambda: self._set_all(False)).pack(side=tk.LEFT)

        # ── Action buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._action_btn = ttk.Button(btn_row, text="Untrack Selected",
                                       command=self._apply,
                                       state=tk.NORMAL if files else tk.DISABLED)
        self._action_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Centre
        self.update_idletasks()
        w, h = 580, 520
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    def _set_all(self, value: bool):
        for var, _f in self._file_vars:
            var.set(value)

    def _apply(self):
        selected = [fname for var, fname in self._file_vars if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to untrack.", parent=self)
            return
        path = self._path
        self.destroy()
        if self._on_confirm is not None:
            self._on_confirm(path, selected)
        else:
            self._app._do_untrack_ignored(path, selected)


class GitignoreDialog(tk.Toplevel):
    """View and edit a project's .gitignore through a structured dialog.

    Layout:
      1. Header: project name + file path + entry count
      2. Scrollable "Current entries" frame — one widget row per non-blank line
         of .gitignore; each pattern row has a × button to mark for removal
         (rendered with strikethrough font); comment rows are visible but
         require a confirm dialog before removal; blank lines are tracked
         by index for layout preservation but not displayed
      3. "Inject template patterns" row — push buttons (not checkboxes; see
         the Gemini critique note in the plan file). One click adds that
         category's missing patterns to the pending additions
      4. Custom entry field + Add button
      5. Pending changes panel (Text widget, read-only) showing + / − diff
      6. Save / Cancel buttons. Save calls _write_gitignore_lines (atomic) and
         then triggers _offer_commit_after_change on the parent App.
    """

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self._app  = parent
        self._path = path
        name = os.path.basename(path)
        self.title(f"Manage .gitignore — {name}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(560, 520)
        self.grab_set()
        self.transient(parent)

        # ── State ──────────────────────────────────────────────────────────
        self._original_lines: list  = _read_gitignore_lines(path)
        self._removed_indices: set  = set()
        self._additions:      list  = []
        self._row_widgets:    dict  = {}   # idx -> {label, btn, frame}

        self._normal_font = tkfont.Font(family="Consolas", size=9)
        self._strike_font = tkfont.Font(family="Consolas", size=9, overstrike=1)

        # ── Header ─────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["base"], padx=18, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📋  .gitignore", bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)
        gi_path = os.path.join(path, ".gitignore")
        sub = tk.Frame(hdr, bg=C["base"])
        sub.pack(fill=tk.X, pady=(4, 0))
        tk.Label(sub, text=gi_path, bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        functional_count = sum(
            1 for ln in self._original_lines
            if ln.strip() and not ln.strip().startswith("#"))
        self._count_lbl = tk.Label(sub,
            text=f"  ({functional_count} pattern"
                 f"{'s' if functional_count != 1 else ''})",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8))
        self._count_lbl.pack(side=tk.LEFT)

        # ── Current entries: scrollable canvas + body Frame ────────────────
        cur_label = tk.Label(self, text="CURRENT ENTRIES",
                             bg=C["base"], fg=C["overlay0"],
                             font=("Segoe UI", 8, "bold"))
        cur_label.pack(anchor=tk.W, padx=18, pady=(0, 4))
        cur_wrap = tk.Frame(self, bg=C["mantle"])
        cur_wrap.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))

        self._cur_canvas = tk.Canvas(cur_wrap, bg=C["mantle"],
                                     highlightthickness=0, height=180)
        cur_vsb = ttk.Scrollbar(cur_wrap, orient="vertical",
                                command=self._cur_canvas.yview)
        self._cur_canvas.configure(yscrollcommand=cur_vsb.set)
        cur_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cur_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._cur_body = tk.Frame(self._cur_canvas, bg=C["mantle"])
        self._cur_body_id = self._cur_canvas.create_window(
            (0, 0), window=self._cur_body, anchor="nw")
        self._cur_canvas.bind("<Configure>",
            lambda e: self._cur_canvas.itemconfigure(
                self._cur_body_id, width=e.width))
        self._cur_body.bind("<Configure>",
            lambda e: self._cur_canvas.configure(
                scrollregion=self._cur_canvas.bbox("all")))

        self._populate_current_entries()

        # ── Template Inject buttons (action, not state — see plan) ────────
        tmpl_label = tk.Label(self,
            text="INJECT TEMPLATE PATTERNS  (one-click — hover to see what each adds)",
            bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 8, "bold"))
        tmpl_label.pack(anchor=tk.W, padx=18, pady=(4, 4))
        tmpl_wrap = tk.Frame(self, bg=C["base"])
        tmpl_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))

        # Two-row grid of buttons; up to 6 per row
        per_row = 6
        for i, cat_name in enumerate(_GITIGNORE_TEMPLATES.keys()):
            row, col = divmod(i, per_row)
            btn = ttk.Button(tmpl_wrap, text=f"+ {cat_name}",
                             command=lambda n=cat_name: self._inject_template(n))
            btn.grid(row=row, column=col, padx=(0, 4), pady=(0, 4),
                     sticky=tk.W)
            _Tooltip(btn, self._template_tooltip_text(cat_name))

        # ── Custom entry ──────────────────────────────────────────────────
        custom_wrap = tk.Frame(self, bg=C["base"])
        custom_wrap.pack(fill=tk.X, padx=18, pady=(4, 0))
        tk.Label(custom_wrap, text="Custom entry:", bg=C["base"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self._custom_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_wrap, textvariable=self._custom_var,
                                  font=("Consolas", 9), width=40)
        custom_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                          padx=(0, 6))
        custom_entry.bind("<Return>", lambda e: self._add_custom())
        ttk.Button(custom_wrap, text="+ Add",
                   command=self._add_custom).pack(side=tk.LEFT)

        self._custom_hint = tk.Label(self, text="", bg=C["base"],
                                     fg=C["overlay0"], font=("Segoe UI", 8))
        self._custom_hint.pack(anchor=tk.W, padx=18)

        # ── Pending changes panel ────────────────────────────────────────
        pend_label = tk.Label(self, text="PENDING CHANGES",
                              bg=C["base"], fg=C["overlay0"],
                              font=("Segoe UI", 8, "bold"))
        pend_label.pack(anchor=tk.W, padx=18, pady=(10, 2))
        pend_wrap = tk.Frame(self, bg=C["mantle"])
        pend_wrap.pack(fill=tk.X, padx=18, pady=(0, 8))
        self._pend_txt = tk.Text(pend_wrap, height=4,
                                  font=("Consolas", 9),
                                  bg=C["mantle"], fg=C["text"],
                                  relief=tk.FLAT, padx=6, pady=4,
                                  wrap=tk.NONE, state=tk.DISABLED)
        self._pend_txt.pack(fill=tk.X, expand=True)
        self._pend_txt.tag_configure("add",  foreground=C["green"])
        self._pend_txt.tag_configure("rem",  foreground=C["red"])
        self._pend_txt.tag_configure("dim",  foreground=C["overlay0"])

        # ── Save / Cancel ────────────────────────────────────────────────
        btns = tk.Frame(self, bg=C["base"], padx=18, pady=10)
        btns.pack(fill=tk.X)
        self._save_btn = ttk.Button(btns, text="Save changes",
                                     command=self._on_save)
        self._save_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

        self._update_pending_panel()

        # Centre on parent
        self.update_idletasks()
        w, h = 640, 620
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Row population ─────────────────────────────────────────────────────

    def _populate_current_entries(self):
        """Build a row widget for every non-blank line in _original_lines.

        Blank lines are tracked by index (in _original_lines) but not
        displayed — they get preserved on save by iterating original_lines
        on write and skipping anything in _removed_indices.
        """
        if not self._original_lines:
            empty_lbl = tk.Label(self._cur_body,
                text="(no .gitignore yet — inject a template or add a custom entry below)",
                bg=C["mantle"], fg=C["overlay0"],
                font=("Segoe UI", 9, "italic"), padx=8, pady=12)
            empty_lbl.pack(anchor=tk.W)
            return

        for idx, raw in enumerate(self._original_lines):
            stripped = raw.strip()
            if not stripped:
                continue   # blank, preserved by index but invisible
            row = tk.Frame(self._cur_body, bg=C["mantle"])
            row.pack(fill=tk.X, padx=4, pady=1)

            is_comment = stripped.startswith("#")
            pattern_text = raw  # show raw (keeps any leading indentation)

            lbl = tk.Label(row, text=pattern_text, bg=C["mantle"],
                           fg=(C["peach"] if is_comment else C["text"]),
                           font=self._normal_font, anchor=tk.W)
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))

            if is_comment:
                marker = tk.Label(row, text="(comment)",
                                   bg=C["mantle"], fg=C["overlay0"],
                                   font=("Segoe UI", 8, "italic"))
                marker.pack(side=tk.LEFT, padx=(0, 4))

            btn = tk.Label(row, text="×", bg=C["mantle"],
                            fg=C["red"], font=("Segoe UI", 11, "bold"),
                            cursor="hand2", padx=8)
            btn.pack(side=tk.RIGHT)
            btn.bind("<Button-1>",
                lambda _e, i=idx: self._toggle_removal(i))

            self._row_widgets[idx] = {
                "frame":      row,
                "label":      lbl,
                "btn":        btn,
                "is_comment": is_comment,
            }

    # ── Removal toggle ─────────────────────────────────────────────────────

    def _toggle_removal(self, idx: int):
        """Mark or un-mark a row for removal. Confirms before removing comments."""
        widgets = self._row_widgets.get(idx)
        if not widgets:
            return
        if idx in self._removed_indices:
            # Undo removal
            self._removed_indices.discard(idx)
            widgets["label"].configure(font=self._normal_font,
                fg=(C["peach"] if widgets["is_comment"] else C["text"]))
            widgets["btn"].configure(text="×", fg=C["red"])
        else:
            # Adding removal — confirm if it's a comment
            if widgets["is_comment"]:
                ok = messagebox.askyesno(
                    "Remove comment line?",
                    f"Remove this comment line?\n\n  {self._original_lines[idx]}",
                    parent=self)
                if not ok:
                    return
            self._removed_indices.add(idx)
            widgets["label"].configure(font=self._strike_font,
                fg=C["overlay0"])
            widgets["btn"].configure(text="↺", fg=C["green"])
        self._update_pending_panel()

    # ── Template injection (smart-clear conflict aware) ───────────────────

    def _current_pattern_state(self) -> set:
        """Return the set of patterns currently in 'final state' = original
        minus pending-removed plus pending-added. Used to decide what a
        template injection actually needs to add."""
        in_state = set()
        for idx, raw in enumerate(self._original_lines):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if idx in self._removed_indices:
                continue
            in_state.add(s)
        for a in self._additions:
            in_state.add(a.strip())
        return in_state

    def _inject_template(self, cat_name: str):
        """Apply a category's patterns. Smart-resolves conflicts with
        pending removals: if the category contains a pattern currently
        marked for removal, un-remove it instead of appending a duplicate."""
        patterns = _GITIGNORE_TEMPLATES.get(cat_name, [])
        if not patterns:
            return
        already = self._current_pattern_state()
        added_any = False
        # First pass: un-remove any pattern that's currently in _removed_indices
        # AND the category wants it (revert the removal instead of duplicating)
        for idx, raw in enumerate(self._original_lines):
            if idx not in self._removed_indices:
                continue
            s = raw.strip()
            if s in patterns:
                # Revert this row's removal
                self._toggle_removal(idx)   # already updates pending panel
                already.add(s)
                added_any = True
        # Second pass: append patterns that genuinely aren't present
        for p in patterns:
            if p not in already:
                self._additions.append(p)
                already.add(p)
                added_any = True
        if added_any:
            self._update_pending_panel()
        # Don't bother flashing on no-op clicks

    def _template_tooltip_text(self, cat_name: str) -> str:
        """Tooltip text listing the patterns this category contributes."""
        pats = _GITIGNORE_TEMPLATES.get(cat_name, [])
        return f"Click to add to .gitignore:\n" + "\n".join(f"  {p}" for p in pats)

    # ── Custom entry ─────────────────────────────────────────────────────

    def _add_custom(self):
        text = self._custom_var.get().strip()
        if not text:
            return
        # Dedup
        if text in self._current_pattern_state():
            self._custom_hint.configure(text="(already present)", fg=C["overlay0"])
            self.after(2000,
                lambda: self._custom_hint.configure(text=""))
            return
        # Suspicious-looking pattern? Confirm before adding.
        if " " in text or text.startswith("/"):
            # Note: leading slash is actually valid gitignore (anchors to root),
            # but mixed with whitespace it's almost always a typo
            if " " in text:
                if not messagebox.askyesno(
                    "Suspicious pattern",
                    f"This doesn't look like a typical gitignore pattern "
                    f"(contains whitespace):\n\n  {text}\n\nAdd anyway?",
                    parent=self):
                    return
        self._additions.append(text)
        self._custom_var.set("")
        self._custom_hint.configure(text="")
        self._update_pending_panel()

    # ── Pending changes panel ────────────────────────────────────────────

    def _update_pending_panel(self):
        """Refresh the diff display and the Save button's enabled state."""
        self._pend_txt.configure(state=tk.NORMAL)
        self._pend_txt.delete("1.0", tk.END)
        any_changes = False
        for idx in sorted(self._removed_indices):
            line = self._original_lines[idx].strip()
            self._pend_txt.insert(tk.END, f"− {line}\n", "rem")
            any_changes = True
        for line in self._additions:
            self._pend_txt.insert(tk.END, f"+ {line}\n", "add")
            any_changes = True
        if not any_changes:
            self._pend_txt.insert(tk.END,
                "No changes — inject a template, add a custom entry, "
                "or click × on a row to begin.", "dim")
            self._save_btn.configure(state=tk.DISABLED)
        else:
            self._save_btn.configure(state=tk.NORMAL)
        self._pend_txt.configure(state=tk.DISABLED)

    # ── Save ──────────────────────────────────────────────────────────────

    def _on_save(self):
        # Build the final line list: original minus removed, plus additions
        kept_lines = [ln for i, ln in enumerate(self._original_lines)
                      if i not in self._removed_indices]
        final_lines = list(kept_lines)
        if self._additions:
            # Add a blank separator + header before new entries (only if the
            # last existing line isn't already blank — avoids double-blank)
            if final_lines and final_lines[-1].strip() != "":
                final_lines.append("")
            final_lines.append("# Added by TokenSave Manager")
            final_lines.extend(self._additions)
        try:
            _write_gitignore_lines(self._path, final_lines)
        except OSError as e:
            messagebox.showerror("Save failed",
                f"Could not write .gitignore:\n\n{e}", parent=self)
            return
        # Log via the App's log panel
        added_n   = len(self._additions)
        removed_n = len(self._removed_indices)
        bits = []
        if added_n:   bits.append(f"+{added_n}")
        if removed_n: bits.append(f"-{removed_n}")
        change_str = "  ".join(bits) if bits else "(no diff)"
        self._app._log(
            f"  Saved .gitignore  ({change_str})", C["green"])
        path = self._path
        self.destroy()
        # After the .gitignore write, check whether the new rules now match
        # files that were ALREADY tracked. If so, offer to untrack them in
        # the same flow — otherwise the user hits the confusing 'git keeps
        # showing this as modified after I added it to gitignore' problem.
        # Only relevant when at least one addition was made AND this is a
        # local git repo; pure removals or non-git projects skip this.
        if added_n > 0 and _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path)
            if stale:
                ask = messagebox.askyesno(
                    "Untrack files that match your new rules?",
                    f"Your .gitignore now matches "
                    f"{len(stale)} file{'s' if len(stale) != 1 else ''} "
                    "that {} already tracked by git:\n\n".format(
                        "are" if len(stale) != 1 else "is")
                    + "\n".join(f"  • {f}" for f in stale[:10])
                    + ("\n  ..." if len(stale) > 10 else "")
                    + "\n\nUntracking removes them from git's index but "
                    "keeps the local files. This is the standard fix for "
                    "'I added it to .gitignore but git keeps showing it.'\n\n"
                    "Open the Untrack Ignored Files dialog now?",
                    parent=self._app)
                if ask:
                    UntrackIgnoredDialog(self._app, path, stale,
                        reason="now matched by your updated .gitignore")
                    return  # untrack flow handles commit prompt itself
        # Otherwise: trigger the existing commit-after-change prompt
        self._app._offer_commit_after_change(path, ".gitignore")


def _iter_json_lines(response):
    """Yield decoded JSON objects from a newline-delimited JSON byte stream.

    Used for Ollama's /api/pull progress stream — each line is a complete
    JSON object terminated by `\\n`. Same byte-aligned accumulator pattern
    as `_iter_sse_events` (network buffering can split a line in half).
    Decode errors on individual lines are silently skipped — the next valid
    line usually has the same status info we missed.
    """
    import json as _json
    buf = bytearray()
    while True:
        try:
            chunk = response.read(4096)
        except (OSError, ConnectionError):
            return
        if not chunk:
            if buf:
                line = buf.decode("utf-8", errors="replace").strip()
                if line:
                    try:
                        yield _json.loads(line)
                    except _json.JSONDecodeError:
                        pass
            return
        buf.extend(chunk)
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            raw = bytes(buf[:i])
            del buf[:i + 1]
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield _json.loads(line)
            except _json.JSONDecodeError:
                continue


class OllamaModelManagerDialog(tk.Toplevel):
    """Browse, pull, and delete Ollama models without leaving the manager.

    Uses Ollama's native REST API (not the OpenAI-compatible /v1 surface):
      - `GET  /api/version`  — connection check
      - `GET  /api/tags`     — list installed models (name, size)
      - `POST /api/show`     — per-model details (context length)
      - `POST /api/pull`     — download a new model, with streaming progress
      - `DELETE /api/delete` — remove a model

    The Pull operation streams newline-delimited JSON. Cancellation works by
    holding a reference to the open HTTPResponse and calling `.close()` on
    it from the main thread — that unblocks the worker thread's `read()`
    immediately. A `threading.Event` alone would not (the worker is
    syscall-blocked inside the network stack).

    Pure read-only with respect to the project — no project-level state is
    touched. The user's saved `commit_message_llm.model` is only updated if
    they explicitly click "Use for AI features".
    """

    PRESET_MODELS = [
        # Coder-tuned (top of the roadmap recommendations)
        "qwen2.5-coder:14b",
        "qwen2.5-coder:7b",
        "deepseek-coder-v2:16b",
        # General instruction-tuned
        "qwen2.5:14b",
        "qwen2.5:7b",
        "mistral-nemo:12b",
        # Smaller / fast
        "llama3.1:8b",
        "llama3.2",
        "llama3.2:3b",
    ]

    def __init__(self, parent, base_url: str = "http://localhost:11434",
                 on_use_for_ai=None):
        super().__init__(parent)
        self.title("Ollama Model Manager")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 500)
        self.geometry("780x620")
        self.grab_set()
        self.transient(parent)

        self._base_url = base_url.rstrip("/") if base_url else "http://localhost:11434"
        self._on_use_for_ai = on_use_for_ai
        self._current_response = None      # type: ignore[assignment]
        self._pull_cancelled = False
        self._pull_active = False

        # ── Header: server URL + check ──────────────────────────────────
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🦙  Ollama Model Manager",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)

        url_row = tk.Frame(self, bg=C["base"])
        url_row.pack(fill=tk.X, padx=18, pady=(2, 2))
        tk.Label(url_row, text="Server:", width=8, anchor=tk.W,
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._var_base_url = tk.StringVar(value=self._base_url)
        ttk.Entry(url_row, textvariable=self._var_base_url,
                  width=42).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Check connection",
                   command=self._check_connection).pack(side=tk.LEFT)

        self._conn_lbl = tk.Label(
            self, text="(click 'Check connection' to verify)",
            font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W)
        self._conn_lbl.pack(fill=tk.X, padx=18, pady=(0, 8))

        # ── Installed models list ───────────────────────────────────────
        list_frame = tk.LabelFrame(
            self, text=" Installed models ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))

        tv_wrap = tk.Frame(list_frame, bg=C["mantle"])
        tv_wrap.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._tv = ttk.Treeview(
            tv_wrap, columns=("size", "context"),
            show="tree headings", height=8)
        self._tv.heading("#0",      text="Model")
        self._tv.heading("size",    text="Size")
        self._tv.heading("context", text="Context window")
        self._tv.column("#0",      width=320, anchor=tk.W)
        self._tv.column("size",    width=100, anchor=tk.E)
        self._tv.column("context", width=140, anchor=tk.E)
        tv_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                command=self._tv.yview)
        self._tv.configure(yscrollcommand=tv_vsb.set)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv.bind("<<TreeviewSelect>>", self._on_select)

        list_btns = tk.Frame(list_frame, bg=C["base"])
        list_btns.pack(fill=tk.X, padx=4, pady=(4, 4))
        ttk.Button(list_btns, text="↻ Refresh",
                   command=self._refresh_models).pack(side=tk.LEFT)
        self._use_btn = ttk.Button(
            list_btns, text="Use for AI features",
            command=self._use_for_ai, state=tk.DISABLED)
        self._use_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._del_btn = ttk.Button(
            list_btns, text="🗑 Delete",
            command=self._delete_selected, state=tk.DISABLED)
        self._del_btn.pack(side=tk.LEFT, padx=(8, 0))

        # ── Pull section ────────────────────────────────────────────────
        pull_frame = tk.LabelFrame(
            self, text=" Pull a new model ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        pull_frame.pack(fill=tk.X, padx=18, pady=(0, 6))

        pull_row = tk.Frame(pull_frame, bg=C["base"])
        pull_row.pack(fill=tk.X, padx=4, pady=(6, 4))
        tk.Label(pull_row, text="Model:", width=8, anchor=tk.W,
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._var_pull = tk.StringVar(value=self.PRESET_MODELS[0])
        self._pull_combo = ttk.Combobox(
            pull_row, textvariable=self._var_pull,
            values=self.PRESET_MODELS, width=28)
        self._pull_combo.pack(side=tk.LEFT, padx=(0, 6))
        self._pull_btn = ttk.Button(
            pull_row, text="Pull", command=self._start_pull)
        self._pull_btn.pack(side=tk.LEFT)

        self._progress = ttk.Progressbar(
            pull_frame, orient="horizontal", mode="determinate",
            maximum=100, value=0)
        self._progress.pack(fill=tk.X, padx=6, pady=(4, 2))
        self._pull_status = tk.Label(
            pull_frame, text="(idle)", font=("Segoe UI", 8),
            bg=C["base"], fg=C["overlay0"],
            anchor=tk.W, justify=tk.LEFT)
        self._pull_status.pack(fill=tk.X, padx=8, pady=(0, 6))

        # ── Close ───────────────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        ttk.Button(btn_row, text="Close",
                   command=self._on_close).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial population.
        self.after(80, self._check_connection)
        self.after(160, self._refresh_models)

    # ── Networking helpers ──────────────────────────────────────────────

    def _server(self) -> str:
        v = self._var_base_url.get().strip().rstrip("/")
        return v or "http://localhost:11434"

    def _check_connection(self):
        url = self._server() + "/api/version"
        self._conn_lbl.configure(
            text="⟳  Checking connection…", fg=C["peach"])

        def _worker():
            import urllib.request, urllib.error, json as _json
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                ver = data.get("version", "?")
                self.after(0, self._conn_lbl.configure,
                    {"text": f"✓  Connected — Ollama {ver}", "fg": C["green"]})
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, _json.JSONDecodeError, OSError) as e:
                self.after(0, self._conn_lbl.configure,
                    {"text": f"✗  Not reachable at {self._server()} — "
                             f"is the Ollama service running? ({type(e).__name__})",
                     "fg": C["red"]})

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-version").start()

    def _refresh_models(self):
        url = self._server() + "/api/tags"

        def _worker():
            import urllib.request, urllib.error, json as _json
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, _json.JSONDecodeError, OSError):
                self.after(0, self._populate_models, [])
                return
            models = data.get("models") or []
            # Enrich each with context length via /api/show. This is N HTTP
            # calls — fine for typical (< 20) installed model counts, and
            # we cap at 25 to avoid pathological cases.
            enriched = []
            for m in models[:25]:
                name = m.get("name") or m.get("model") or ""
                size = int(m.get("size") or 0)
                ctx = self._fetch_context_length(name)
                enriched.append({"name": name, "size": size, "context": ctx})
            self.after(0, self._populate_models, enriched)

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-tags").start()

    def _fetch_context_length(self, name: str) -> int | None:
        import urllib.request, urllib.error, json as _json
        url = self._server() + "/api/show"
        payload = _json.dumps({"name": name}).encode("utf-8")
        req = urllib.request.Request(
            url, method="POST", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, _json.JSONDecodeError, OSError):
            return None
        # Ollama 0.3+ exposes model_info.<arch>.context_length; older
        # versions use parameters.num_ctx. Try both, fall back to None.
        mi = data.get("model_info") or {}
        for k, v in mi.items():
            if k.endswith(".context_length") and isinstance(v, int):
                return v
        params = data.get("parameters") or ""
        if isinstance(params, str):
            for line in params.splitlines():
                if line.lower().startswith("num_ctx"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1].isdigit():
                        return int(parts[-1])
        return None

    def _populate_models(self, rows: list[dict]):
        self._tv.delete(*self._tv.get_children())
        if not rows:
            self._tv.insert("", tk.END, text="(no models found — pull one below)",
                            values=("", ""))
            return
        for r in rows:
            size_h = self._human_bytes(r["size"])
            ctx_h = "—" if r["context"] is None else f"{r['context']:,}"
            self._tv.insert("", tk.END, text=r["name"], values=(size_h, ctx_h))

    @staticmethod
    def _human_bytes(n: int) -> str:
        if n <= 0:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024.0:
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Selection-driven actions ────────────────────────────────────────

    def _on_select(self, _evt=None):
        sel = self._tv.selection()
        state = tk.NORMAL if sel and self._tv.item(sel[0], "text") else tk.DISABLED
        self._use_btn.configure(state=state)
        self._del_btn.configure(state=state)

    def _selected_model(self) -> str:
        sel = self._tv.selection()
        if not sel:
            return ""
        name = self._tv.item(sel[0], "text") or ""
        return "" if name.startswith("(no models") else name

    def _use_for_ai(self):
        name = self._selected_model()
        if not name:
            return
        if self._on_use_for_ai:
            self._on_use_for_ai(name, self._server())
            messagebox.showinfo(
                "Set as AI model",
                f"'{name}' will be used for AI features.\n\n"
                "Provider set to 'ollama'.",
                parent=self)
        else:
            # No callback wired — copy to clipboard as a fallback.
            self.clipboard_clear()
            self.clipboard_append(name)
            messagebox.showinfo(
                "Copied", f"'{name}' copied to clipboard.\n"
                "Paste it into Settings → AI commit messages → Model.",
                parent=self)

    def _delete_selected(self):
        name = self._selected_model()
        if not name:
            return
        if not messagebox.askyesno(
                "Delete model",
                f"Delete '{name}' from Ollama?\n\n"
                "This frees disk space. You can re-pull it later.",
                parent=self):
            return

        def _worker(model=name):
            import urllib.request, urllib.error, json as _json
            url = self._server() + "/api/delete"
            payload = _json.dumps({"name": model}).encode("utf-8")
            req = urllib.request.Request(
                url, method="DELETE", data=payload,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                ok = True
                err = ""
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError) as e:
                ok = False
                err = f"{type(e).__name__}: {e}"
            self.after(0, self._after_delete, model, ok, err)

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-delete").start()

    def _after_delete(self, name: str, ok: bool, err: str):
        if ok:
            self._refresh_models()
        else:
            messagebox.showerror(
                "Delete failed",
                f"Could not delete '{name}'.\n\n{err}",
                parent=self)

    # ── Pull (streaming) ────────────────────────────────────────────────

    def _start_pull(self):
        if self._pull_active:
            # Button is in Cancel mode — second click cancels.
            self._cancel_pull()
            return
        name = self._var_pull.get().strip()
        if not name:
            return
        self._pull_active = True
        self._pull_cancelled = False
        self._progress.configure(value=0, maximum=100)
        self._pull_status.configure(
            text=f"⟳  Pulling {name}…", fg=C["peach"])
        self._pull_btn.configure(text="Cancel")
        self._pull_combo.configure(state=tk.DISABLED)

        def _worker(model=name):
            import urllib.request, urllib.error, json as _json
            url = self._server() + "/api/pull"
            payload = _json.dumps({"name": model, "stream": True}).encode("utf-8")
            req = urllib.request.Request(
                url, method="POST", data=payload,
                headers={"Content-Type": "application/json"})
            try:
                # NOTE: no `with` block — we need the response object to
                # remain accessible from the main thread so a Cancel click
                # can call .close() on it to break this worker out of read().
                response = urllib.request.urlopen(req, timeout=30)
                self._current_response = response
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError) as e:
                self.after(0, self._after_pull, model, False,
                           f"{type(e).__name__}: {e}")
                return
            try:
                for event in _iter_json_lines(response):
                    if self._pull_cancelled:
                        break
                    status = event.get("status") or ""
                    total = event.get("total")
                    completed = event.get("completed")
                    if isinstance(total, int) and total > 0 and isinstance(completed, int):
                        pct = max(0.0, min(100.0, 100.0 * completed / total))
                        msg = (f"{status} — "
                               f"{self._human_bytes(completed)} / "
                               f"{self._human_bytes(total)}  ({pct:.0f}%)")
                        self.after(0, self._update_pull_progress, pct, msg)
                    elif status:
                        self.after(0, self._update_pull_status, status)
                    if status == "success":
                        self.after(0, self._after_pull, model, True, "")
                        return
            finally:
                try:
                    response.close()
                except OSError:
                    pass
                self._current_response = None
            # Stream ended without an explicit "success" — could be cancel,
            # network drop, or an error event. Cancelled path takes priority.
            if self._pull_cancelled:
                self.after(0, self._after_pull, model, False, "cancelled")
            else:
                self.after(0, self._after_pull, model, False,
                           "Stream ended without success")

        self._pull_thread = threading.Thread(
            target=_worker, daemon=True, name="ollama-pull")
        self._pull_thread.start()

    def _cancel_pull(self):
        self._pull_cancelled = True
        self._pull_status.configure(text="⏹  Cancelling…", fg=C["overlay0"])
        # Sever the socket immediately so the worker thread's read() unblocks.
        resp = self._current_response
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass

    def _update_pull_progress(self, pct: float, msg: str):
        self._progress.configure(value=pct)
        self._pull_status.configure(text=msg, fg=C["peach"])

    def _update_pull_status(self, status: str):
        self._pull_status.configure(text=status, fg=C["overlay0"])

    def _after_pull(self, name: str, ok: bool, err: str):
        self._pull_active = False
        self._pull_btn.configure(text="Pull")
        self._pull_combo.configure(state=tk.NORMAL)
        if ok:
            self._progress.configure(value=100)
            self._pull_status.configure(
                text=f"✓  Pulled {name} successfully.", fg=C["green"])
            self._refresh_models()
        else:
            self._progress.configure(value=0)
            if err == "cancelled":
                self._pull_status.configure(
                    text=f"⏹  Cancelled. Partial download for {name} is kept "
                         f"by Ollama — re-run Pull to resume (layers are "
                         f"deduplicated).",
                    fg=C["overlay0"])
            else:
                self._pull_status.configure(
                    text=f"✗  Pull failed: {err}", fg=C["red"])

    # ── Close ───────────────────────────────────────────────────────────

    def _on_close(self):
        if self._pull_active:
            # Cancel any in-flight pull so we don't leave the thread blocked.
            self._cancel_pull()
        try:
            self.destroy()
        except tk.TclError:
            pass


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


class MCPConfigDialog(tk.Toplevel):
    """Manage tokensave entries in Claude Desktop's and Claude Code's MCP
    config files.

    Shows one block per config file with:
      - Path of the file
      - Current state badge (✓ correct / ⚠ drift / ✗ missing)
      - Diff between current and proposed entries
      - Apply / Skip / Open file actions
      - Big warning if the owning Claude app is currently running

    Per the show-diff-and-ask protocol in CLAUDE.md: each config gets its
    own Apply button (no "Fix all"), each Apply writes a timestamped
    backup before mutating, and the diff is rendered in plain text so the
    user can read it without leaving the dialog.

    The dialog is the one place in the manager that touches Claude's MCP
    configs. Other callers (startup banner, Settings button) only LAUNCH
    this dialog — they never edit the JSON themselves.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("MCP Integration")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(680, 520)
        self.geometry("820x680")
        self.grab_set()
        self.transient(parent)

        # Header
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🔌  MCP Integration",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text="Manage tokensave's wiring into Claude's MCP system.",
                 font=("Segoe UI", 9, "italic"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT, padx=(10, 0))

        # Running-Claude warning banner (populated on detect)
        self._warn_lbl = tk.Label(
            self, text="", font=("Segoe UI", 9),
            bg=C["base"], fg=C["red"],
            justify=tk.LEFT, anchor=tk.W, wraplength=760)
        self._warn_lbl.pack(fill=tk.X, padx=18, pady=(2, 4))

        # Scrollable body — same Canvas+Frame pattern as SettingsDialog
        wrap = tk.Frame(self, bg=C["base"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(2, 4))
        self._canvas = tk.Canvas(wrap, bg=C["base"], highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._body = tk.Frame(self._canvas, bg=C["base"])
        self._body_win = self._canvas.create_window(
            (0, 0), window=self._body, anchor="nw")
        self._body.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(
                self._body_win, width=e.width))
        for w in (self._canvas, self._body):
            w.bind(
                "<MouseWheel>",
                lambda e: self._canvas.yview_scroll(
                    int(-1 * (e.delta / 120)), "units"))

        # Footer
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        ttk.Button(btn_row, text="↻ Re-detect",
                   command=self._render).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

        # Per-config-path state for the renderer
        self._config_state: dict[str, dict] = {}
        self._render()

    # ── Rendering ───────────────────────────────────────────────────────

    def _render(self):
        """(Re-)build the per-config blocks. Called once at open and from
        Re-detect / after each Apply so the state badges stay fresh."""
        for child in self._body.winfo_children():
            child.destroy()

        # Warning banner — running Claude apps
        running = _is_claude_running()
        if running["desktop"] or running["code"]:
            apps = []
            if running["desktop"]:
                apps.append("Claude Desktop")
            if running["code"]:
                apps.append("Claude Code")
            self._warn_lbl.configure(
                text=("⚠  " + " / ".join(apps) + " is currently running. "
                      "It rewrites its own config file every 1–2 minutes, "
                      "which will silently revert any fix you apply here. "
                      "Fully quit the app before clicking Apply on its row."))
        else:
            self._warn_lbl.configure(text="")

        for label, path in _MCP_CONFIGS:
            self._render_block(label, path)

    def _render_block(self, label: str, path: str):
        info = _classify_mcp_entry(path)
        self._config_state[path] = info

        # Section frame
        frame = tk.LabelFrame(
            self._body, text=f"  {label}  ",
            bg=C["base"], fg=C["text"],
            font=("Segoe UI", 10, "bold"),
            bd=1, relief=tk.GROOVE)
        frame.pack(fill=tk.X, padx=4, pady=(8, 4), ipady=4)

        # Path + status row
        head = tk.Frame(frame, bg=C["base"])
        head.pack(fill=tk.X, padx=8, pady=(4, 2))
        tk.Label(head, text=path, font=("Consolas", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)

        # UWP / traditional install indicator — only meaningful for the
        # Desktop row.  Users hitting the UWP path-redirection footgun
        # need to SEE that the manager is targeting the package-internal
        # config, not %APPDATA%\Claude\.  Knowing this prevents a whole
        # category of "I edited the file but Desktop ignores my fix"
        # confusion.
        if label == "Claude Desktop":
            is_uwp = "\\Packages\\Claude_" in path
            install_tag = ("(UWP / Store install)" if is_uwp
                           else "(Traditional install)")
            tag_colour = C["blue"] if is_uwp else C["overlay0"]
            tk.Label(head, text="  " + install_tag,
                     font=("Segoe UI", 8, "italic"),
                     bg=C["base"], fg=tag_colour).pack(side=tk.LEFT)

        state = info["state"]
        if state == "ok":
            badge_colour = C["green"]
        elif state in ("direct_serve", "wrong_wrapper"):
            badge_colour = C["peach"]
        else:
            badge_colour = C["red"]
        tk.Label(head, text=info["label"],
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=badge_colour).pack(side=tk.RIGHT)

        # Issue text (always shown for clarity, even on ok)
        issue_text = info["issue"] or "No action needed — already routes through the wrapper."
        tk.Label(frame, text=issue_text,
                 font=("Segoe UI", 9),
                 bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=720, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))

        # Diff (only if there's something to change)
        if state != "ok":
            diff_box = tk.Text(
                frame, height=8, font=("Consolas", 9),
                bg=C["mantle"], fg=C["text"],
                relief=tk.FLAT, padx=8, pady=6, wrap=tk.NONE,
                state=tk.NORMAL)
            diff_box.tag_configure("old", foreground="#f38ba8")
            diff_box.tag_configure("new", foreground="#a6e3a1")
            diff_box.tag_configure("hdr", foreground=C["overlay0"],
                                    font=("Consolas", 9, "italic"))

            if info["current"] is None:
                diff_box.insert(tk.END, "  (no current entry — will be added)\n", "hdr")
            else:
                diff_box.insert(tk.END, "  --- current ---\n", "hdr")
                for line in json.dumps(info["current"], indent=2).splitlines():
                    diff_box.insert(tk.END, "  - " + line + "\n", "old")
            diff_box.insert(tk.END, "  +++ proposed +++\n", "hdr")
            for line in json.dumps(info["proposed"], indent=2).splitlines():
                diff_box.insert(tk.END, "  + " + line + "\n", "new")

            # Auto-size height to content, capped
            line_count = int(diff_box.index("end-1c").split(".")[0])
            diff_box.configure(height=min(max(line_count + 1, 6), 18),
                               state=tk.DISABLED)
            diff_box.pack(fill=tk.X, padx=8, pady=(2, 4))

        # Action row
        actions = tk.Frame(frame, bg=C["base"])
        actions.pack(fill=tk.X, padx=8, pady=(2, 4))

        if state == "ok":
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(side=tk.LEFT)
        else:
            apply_btn = ttk.Button(
                actions, text="Apply this fix",
                style="Primary.TButton",
                command=lambda p=path, l=label: self._apply(p, l))
            apply_btn.pack(side=tk.LEFT)

            ttk.Button(actions, text="Skip (don't warn again)",
                       command=lambda p=path: self._skip(p)).pack(
                side=tk.LEFT, padx=(8, 0))
            ttk.Button(actions, text="Open file",
                       command=lambda p=path: self._open_file(p)).pack(
                side=tk.LEFT, padx=(8, 0))

        # Backup-notice strip
        tk.Label(frame,
            text=("  A timestamped backup is written before any change. "
                  "Other mcpServers entries in this file are preserved verbatim."),
            font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=8, pady=(0, 4))

    # ── Actions ─────────────────────────────────────────────────────────

    def _apply(self, cfg_path: str, label: str):
        # Pre-flight: don't apply over a running Claude.  The previous
        # version of this method used a showwarning that was easy to
        # dismiss without realising the Apply was a no-op — the row state
        # didn't change visibly, no main-log entry appeared, and users
        # walked away thinking the fix had landed.  Now: showerror (which
        # uses the red-X icon and reads as a rejection, not a hint), a
        # main-log line in red so the OUTPUT pane records it persistently,
        # and a forced re-render so any state-change (or lack thereof)
        # is visible immediately.
        running = _is_claude_running()
        if (label == "Claude Desktop" and running["desktop"]) or \
           (label == "Claude Code" and running["code"]):
            try:
                # The parent App is the manager window; reach back to log()
                # so the message lands in the persistent OUTPUT pane.
                self.master._log(
                    f"MCP Apply REFUSED: {label} is still running. "
                    f"No changes were written. Quit {label} (verify zero "
                    f"rows in Task Manager) then click Apply again.",
                    C["red"])
            except (AttributeError, tk.TclError):
                pass
            messagebox.showerror(
                f"Apply refused — {label} is running",
                f"{label} is currently running and is reading the MCP "
                "config from its in-memory cache.  Writing to the file now "
                "would either be ignored (Desktop only reloads at startup) "
                "or silently reverted (Desktop writes its cache back to "
                "disk every 1–2 minutes).\n\n"
                "★  NO CHANGES WERE WRITTEN  ★\n\n"
                f"To fix:\n"
                f"1. Fully quit {label} (tray icon → Quit).\n"
                f"2. Verify ZERO rows for 'claude' in Task Manager.\n"
                f"3. Wait ~5 seconds for stragglers (crashpad, renderer).\n"
                "4. Click Re-detect, then Apply this fix.",
                parent=self)
            self._render()
            return

        proposed = self._config_state[cfg_path]["proposed"]
        ok, msg = _apply_mcp_fix(cfg_path, proposed)
        if ok:
            try:
                self.master._log(
                    f"MCP Apply OK: wrote canonical tokensave entry to "
                    f"{label} config.  {msg}",
                    C["green"])
            except (AttributeError, tk.TclError):
                pass
            messagebox.showinfo(
                "Fix applied", f"{msg}\n\n"
                "Status row below has been refreshed.",
                parent=self)
            # Take the path off the skip list if it was on it.
            skips = (_cfg.get("mcp_skip_warnings") or []) \
                    if isinstance(_cfg, dict) else []
            if cfg_path in skips:
                skips.remove(cfg_path)
                _cfg["mcp_skip_warnings"] = skips
                _save_config(_cfg)
            self._render()
        else:
            try:
                self.master._log(
                    f"MCP Apply FAILED: {label} — {msg}", C["red"])
            except (AttributeError, tk.TclError):
                pass
            messagebox.showerror("Fix failed", msg, parent=self)
            self._render()

    def _skip(self, cfg_path: str):
        skips = (_cfg.get("mcp_skip_warnings") or []) \
                if isinstance(_cfg, dict) else []
        if cfg_path not in skips:
            skips.append(cfg_path)
            _cfg["mcp_skip_warnings"] = skips
            _save_config(_cfg)
        messagebox.showinfo(
            "Skipped",
            f"Won't warn about {cfg_path} on startup anymore.\n\n"
            "Open this dialog from Settings → MCP integration to revisit.",
            parent=self)

    def _open_file(self, cfg_path: str):
        """Open the config file in the user's default editor, or its parent
        folder if the file doesn't exist yet."""
        try:
            if os.path.isfile(cfg_path):
                os.startfile(cfg_path)
            else:
                parent_dir = os.path.dirname(cfg_path)
                if os.path.isdir(parent_dir):
                    os.startfile(parent_dir)
                else:
                    messagebox.showwarning(
                        "Not found",
                        f"Neither {cfg_path} nor its parent directory exists.",
                        parent=self)
        except OSError as e:
            messagebox.showerror("Could not open", str(e), parent=self)


class AICodeReviewDialog(tk.Toplevel):
    """Stage 1 of the agentic-AI roadmap: AI Code Review on the pending diff.

    Pure read-only. Calls _call_llm once with a "you are a code reviewer"
    system prompt and the project's `git diff HEAD` output. Result is
    displayed as Markdown-style structured text. No tool calls, no file
    writes, no autonomy concerns — just a one-shot review.

    Async pattern matches GitCommitDialog: daemon thread runs the LLM call,
    spinner shows progress, self.after(0, ...) pushes the result back to
    the main thread. Stop button aborts the in-flight request.
    """

    _SYSTEM_PROMPT = (
        "You are a senior code reviewer reviewing a pending git diff. "
        "Produce a structured Markdown report.\n\n"
        "Output format:\n\n"
        "## ⚠ High severity\n"
        "- <finding> (file:line)\n\n"
        "## ⚡ Medium severity\n"
        "- <finding> (file:line)\n\n"
        "## 💡 Low severity / nits\n"
        "- <finding> (file:line)\n\n"
        "## ℹ Observations\n"
        "- <design note>\n\n"
        "Rules:\n"
        "- One bullet per finding. Cite file:line when possible.\n"
        "- Omit a section entirely if it has nothing — don't write empty sections.\n"
        "- Do NOT repeat the diff back at me.\n"
        "- Focus on correctness, security, and maintainability — not formatting.\n"
        "- Match the project's existing style (judge by surrounding context).\n"
        "- Output ONLY the report. No preamble, no closing remarks."
    )

    def __init__(self, parent, path: str, llm_cfg: dict):
        super().__init__(parent)
        self.title(f"AI Code Review — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(700, 500)
        self.geometry("900x720")
        self.grab_set()
        self.transient(parent)

        self._path = path
        self._llm_cfg = llm_cfg
        self._review_token = 0
        self._cancelled = False

        # ── Header ───────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=20, pady=(14, 6))
        tk.Label(hdr, text="🔍  AI Code Review",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text=os.path.basename(path),
                 font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT, padx=(10, 0))

        # Live status (spinner + provider/model info)
        self._status_lbl = tk.Label(
            self, text="", font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["peach"],
            justify=tk.LEFT, anchor=tk.W)
        self._status_lbl.pack(fill=tk.X, padx=20, pady=(0, 6))

        # ── Split: diff (top) + review output (bottom) ───────────────────
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=C["base"],
                                sashwidth=6, sashrelief=tk.FLAT)
        paned.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

        # Diff view
        diff_frame = tk.LabelFrame(
            paned, text=" Pending diff (git diff HEAD) ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        diff_inner = tk.Frame(diff_frame, bg=C["mantle"])
        diff_inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._diff_txt = tk.Text(
            diff_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Consolas", 9),
            padx=8, pady=6, wrap=tk.NONE, height=14)
        diff_vsb = ttk.Scrollbar(diff_inner, orient="vertical",
                                  command=self._diff_txt.yview)
        diff_hsb = ttk.Scrollbar(diff_inner, orient="horizontal",
                                  command=self._diff_txt.xview)
        self._diff_txt.configure(
            yscrollcommand=diff_vsb.set, xscrollcommand=diff_hsb.set)
        self._diff_txt.grid(row=0, column=0, sticky="nsew")
        diff_vsb.grid(row=0, column=1, sticky="ns")
        diff_hsb.grid(row=1, column=0, sticky="ew")
        diff_inner.grid_rowconfigure(0, weight=1)
        diff_inner.grid_columnconfigure(0, weight=1)
        # Catppuccin Mocha-ish diff colours
        self._diff_txt.tag_configure("add",      foreground="#a6e3a1")
        self._diff_txt.tag_configure("del",      foreground="#f38ba8")
        self._diff_txt.tag_configure("hunk",     foreground="#89b4fa")
        self._diff_txt.tag_configure("filename", foreground="#cba6f7",
                                      font=("Consolas", 9, "bold"))
        paned.add(diff_frame, minsize=120, stretch="always")

        # Review output
        rev_frame = tk.LabelFrame(
            paned, text=" AI review ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        rev_inner = tk.Frame(rev_frame, bg=C["mantle"])
        rev_inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._rev_txt = tk.Text(
            rev_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Segoe UI", 10),
            padx=10, pady=8, wrap=tk.WORD, height=14)
        rev_vsb = ttk.Scrollbar(rev_inner, orient="vertical",
                                 command=self._rev_txt.yview)
        self._rev_txt.configure(yscrollcommand=rev_vsb.set)
        self._rev_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rev_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # Section header colours for the markdown-ish review text
        self._rev_txt.tag_configure(
            "h_high",   foreground="#f38ba8",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_medium", foreground="#fab387",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_low",    foreground="#f9e2af",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_info",   foreground="#89b4fa",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        paned.add(rev_frame, minsize=150, stretch="always")

        # ── Action buttons ──────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 14))
        self._copy_btn = ttk.Button(
            btn_row, text="Copy review to clipboard",
            command=self._copy_review, state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._regen_btn = ttk.Button(
            btn_row, text="Regenerate",
            command=self._start_review, state=tk.DISABLED)
        self._regen_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._stop_btn = ttk.Button(
            btn_row, text="Stop", command=self._cancel)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

        # ── Load diff & kick off review ─────────────────────────────────
        self._load_diff()
        self._start_review()

    # ─────────────────────────────────────────────────────────────────────

    def _load_diff(self):
        """Read the pending diff and render it into the diff pane."""
        diff = _pending_diff(self._path, lines_of_context=3)
        self._diff_txt.configure(state=tk.NORMAL)
        self._diff_txt.delete("1.0", tk.END)
        if not diff:
            self._diff_txt.insert(tk.END, "(no pending changes)")
            self._diff_txt.configure(state=tk.DISABLED)
            self._show_status("No pending diff to review.", colour=C["overlay0"])
            self._stop_btn.configure(state=tk.DISABLED)
            self._regen_btn.configure(state=tk.DISABLED)
            return
        for line in diff.splitlines():
            tag = ""
            if line.startswith("+++") or line.startswith("---"):
                tag = "filename"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            elif line.startswith("@@"):
                tag = "hunk"
            self._diff_txt.insert(tk.END, line + "\n", tag)
        self._diff_txt.configure(state=tk.DISABLED)

    def _start_review(self):
        """Kick off (or restart) the LLM review on a background thread.

        Streams tokens from the LLM into the review pane as they arrive, so
        the user sees output building up in real time instead of staring at
        a spinner for 30+ seconds. Tokens are batched on the worker side
        (every ~8 deltas or 50 ms, whichever first) before being pushed to
        the Tk main thread via `self.after` — a fast local model can emit
        80+ tokens/s and 1:1 self.after calls would saturate Tk's event
        loop. The accumulated full text is still returned at end-of-stream
        so the existing `_render_review` (which applies severity-section
        colour tags) can do its final pass.
        """
        diff = _pending_diff(self._path, lines_of_context=3)
        if not diff:
            return
        self._review_token += 1
        token = self._review_token
        self._cancelled = False
        self._streaming_started = False  # placeholder cleared on first token

        provider = self._llm_cfg.get("provider", "?")
        model    = self._llm_cfg.get("model", "?")
        self._show_status(
            f"⟳  Streaming review with {provider} / {model}…  "
            f"(can take 30–60s on local models)",
            colour=C["peach"])
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        self._rev_txt.insert(tk.END, "(waiting for first token…)")
        self._rev_txt.configure(state=tk.DISABLED)

        self._copy_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)

        max_chars = int(self._llm_cfg.get("max_diff_chars", 24000))
        # Construct the user prompt — just the diff with context.
        user_prompt = (
            f"Review the following git diff. Project: "
            f"{os.path.basename(self._path)}.\n\n"
            f"```diff\n{diff[:max_chars]}\n```"
            + ("\n\n[diff truncated for length]" if len(diff) > max_chars else "")
        )

        # Worker-thread-only batching buffer. No lock needed because both
        # `_on_token` and the final-flush call run on the worker thread, and
        # the captured snapshot is passed by value into `self.after`.
        batch_text: list[str] = []
        batch_chars = [0]
        last_flush = [time.monotonic()]

        def _flush(snapshot: str, tok=token):
            # Runs on the Tk main thread.
            if tok != self._review_token or self._cancelled:
                return
            self._stream_append(snapshot)

        def _on_token(delta: str):
            # Runs on the worker thread.
            batch_text.append(delta)
            batch_chars[0] += len(delta)
            now = time.monotonic()
            if batch_chars[0] >= 32 or (now - last_flush[0]) >= 0.05:
                snapshot = "".join(batch_text)
                batch_text.clear()
                batch_chars[0] = 0
                last_flush[0] = now
                try:
                    self.after(0, _flush, snapshot)
                except RuntimeError:
                    # Dialog destroyed mid-stream — stop trying to push.
                    pass

        def _worker(tok=token):
            try:
                result = _call_llm(
                    self._llm_cfg,
                    self._SYSTEM_PROMPT,
                    user_prompt,
                    max_tokens=2000,
                    on_token=_on_token,
                )
            except Exception:
                log.exception("AI code review worker failed")
                result = None
            # Final flush — any tokens still buffered when the stream ended.
            if batch_text:
                snapshot = "".join(batch_text)
                batch_text.clear()
                try:
                    self.after(0, _flush, snapshot)
                except RuntimeError:
                    pass
            try:
                self.after(0, self._on_review_ready, tok, result)
            except RuntimeError:
                # Dialog destroyed before result arrived — silent drop.
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="ai-code-review-worker").start()

    def _stream_append(self, text: str):
        """Append a batch of streamed tokens to the review pane.

        On the first call after `_start_review`, clears the placeholder
        ('(waiting for first token…)'). Auto-scrolls so the latest content
        stays visible. Section-header colour tags are NOT applied here —
        they get a clean re-render in `_on_review_ready` once the full text
        is available, which keeps the streaming path simple (no need to
        re-tag partial lines as they grow).
        """
        if not text:
            return
        self._rev_txt.configure(state=tk.NORMAL)
        if not getattr(self, "_streaming_started", False):
            self._rev_txt.delete("1.0", tk.END)
            self._streaming_started = True
        self._rev_txt.insert(tk.END, text)
        self._rev_txt.see(tk.END)
        self._rev_txt.configure(state=tk.DISABLED)

    def _on_review_ready(self, token: int, result):
        """Main-thread callback: receive the LLM result."""
        if token != self._review_token or self._cancelled:
            return  # stale or cancelled
        self._stop_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.NORMAL)
        if not result:
            self._show_status(
                "⚠  LLM call failed or returned empty. Check Settings → "
                "AI commit messages (provider / model / server running).",
                colour=C["red"])
            self._rev_txt.configure(state=tk.NORMAL)
            self._rev_txt.delete("1.0", tk.END)
            self._rev_txt.insert(
                tk.END,
                "No review produced. Common causes:\n\n"
                "• Ollama / LM Studio server isn't running\n"
                "• Model name in Settings doesn't match a loaded model\n"
                "• Timeout exceeded (try a smaller / non-reasoning model)\n"
                "• Diff exceeds context window — try a model with more context")
            self._rev_txt.configure(state=tk.DISABLED)
            return
        self._render_review(result)
        self._show_status(
            f"✓  Review complete. {self._llm_cfg.get('provider')} / "
            f"{self._llm_cfg.get('model')}",
            colour=C["green"])
        self._copy_btn.configure(state=tk.NORMAL)
        self._last_review = result

    def _render_review(self, text: str):
        """Insert the review text with section-header colour tags."""
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        for line in text.splitlines():
            stripped = line.lstrip()
            tag = ""
            if stripped.startswith("## ⚠") or stripped.lower().startswith("## high"):
                tag = "h_high"
            elif stripped.startswith("## ⚡") or stripped.lower().startswith("## medium"):
                tag = "h_medium"
            elif stripped.startswith("## 💡") or stripped.lower().startswith("## low"):
                tag = "h_low"
            elif stripped.startswith("## ℹ") or stripped.lower().startswith("## observ"):
                tag = "h_info"
            if tag:
                self._rev_txt.insert(tk.END, line + "\n", tag)
            else:
                self._rev_txt.insert(tk.END, line + "\n")
        self._rev_txt.configure(state=tk.DISABLED)

    def _show_status(self, text: str, colour: str):
        self._status_lbl.configure(text=text, fg=colour)

    def _cancel(self):
        """User clicked Stop. The worker thread is daemon and will exit when
        the LLM responds (we can't actually kill urllib mid-call from another
        thread), but the result will be discarded by token mismatch."""
        self._cancelled = True
        self._review_token += 1   # invalidate the in-flight token
        self._show_status(
            "Cancelled. The background request may still finish but its "
            "result will be discarded.",
            colour=C["overlay0"])
        self._stop_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.NORMAL)
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        self._rev_txt.insert(tk.END, "(cancelled)")
        self._rev_txt.configure(state=tk.DISABLED)

    def _copy_review(self):
        text = getattr(self, "_last_review", "")
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._show_status("✓  Review copied to clipboard.", colour=C["green"])
        except tk.TclError:
            pass


class GitCommitDialog(tk.Toplevel):
    """
    Stage and commit changes in a project's git repository.
    Shows the working-tree files as a checklist so the user can pick which
    files to include in this commit. On commit, only checked files are staged
    and committed; un-checked files stay as un-committed changes.
    """

    # status-char meanings shown next to filenames
    _STATUS_DESC = {
        "M":  "modified",
        "A":  "added",
        "D":  "deleted",
        "R":  "renamed",
        "C":  "copied",
        "U":  "conflict",
        "?":  "untracked",
        "!":  "ignored",
    }

    def __init__(self, parent, path, status_text: str, is_repo: bool, callback):
        """
        callback(path, message, selected_files): called on Commit.
        selected_files is a list of paths (relative to repo root) to stage+commit.
        status_text: output of `git status --short` (may be empty).
        is_repo: False disables the Commit button and shows a warning.
        """
        super().__init__(parent)
        self.title(f"Git Commit — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(540, 480)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._callback = callback
        self._is_repo  = is_repo
        self._status_raw = status_text

        # Parse status into [(xy, fname), ...] — same logic as Git tab.
        # CRITICAL: do NOT call .strip() on status_text BEFORE splitlines().
        # `git status --short` lines have a 2-char status (XY) + 1 space +
        # path. For working-tree-modified files the first char is a space
        # (" M file.py"). Calling strip() on the whole multi-line string
        # eats that leading space from the FIRST line only, shifting its
        # columns by 1, which caused `line[3:]` to drop the first character
        # of the path — e.g. ".claude/settings.json" became
        # "claude/settings.json" and git add failed with pathspec error.
        self._files = []
        if is_repo:
            for line in status_text.splitlines():
                if len(line) >= 4:
                    self._files.append((line[:2], line[3:]))

        # ── Header ──
        tk.Label(self,
                 text="📝  Git Commit",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))
        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── File checklist ──
        hdr_row = tk.Frame(self, bg=C["base"])
        hdr_row.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(hdr_row,
                 text="Pick files to include in this commit:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        if self._files:
            tk.Label(hdr_row,
                     text=f"  ({len(self._files)} changed)",
                     font=("Segoe UI", 9),
                     bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        # Scrollable checklist via canvas + frame (body parented to self so
        # children render reliably on Windows — same pattern as the wizard).
        list_outer = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        list_outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))
        self._canvas = tk.Canvas(list_outer, bg=C["mantle"],
                                 highlightthickness=0, height=160)
        _vsb = ttk.Scrollbar(list_outer, orient="vertical",
                             command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._list_body = tk.Frame(self, bg=C["mantle"])
        self._list_body_id = self._canvas.create_window(
            (0, 0), window=self._list_body, anchor="nw")
        self._canvas.bind("<Configure>",
            lambda e: self._canvas.itemconfigure(self._list_body_id, width=e.width))
        self._list_body.bind("<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))

        def _mw(e):
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _mw)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

        # Populate the checklist
        self._file_vars: list = []   # parallel to self._files — list of (BooleanVar, fname)
        if not is_repo:
            tk.Label(self._list_body,
                     text="⚠  Not a git repository — run 🔧 Git Init first.",
                     bg=C["mantle"], fg=C["red"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
        elif not self._files:
            tk.Label(self._list_body,
                     text="(nothing to commit — working tree clean)",
                     bg=C["mantle"], fg=C["overlay0"],
                     font=("Segoe UI", 9), padx=10, pady=10).pack(anchor=tk.W)
        else:
            for xy, fname in self._files:
                var = tk.BooleanVar(value=True)
                self._file_vars.append((var, fname, xy))
                row = tk.Frame(self._list_body, bg=C["mantle"])
                row.pack(fill=tk.X, padx=4, pady=1)
                # Status badge — colored char in a fixed-width slot
                xy_clean = xy.strip() or "?"
                status_char = xy_clean[0]
                color = {
                    "M": C["yellow"], "A": C["green"], "D": C["red"],
                    "R": C["sky"], "C": C["sky"], "U": C["red"],
                    "?": C["blue"], "!": C["overlay0"],
                }.get(status_char, C["text"])
                desc = self._STATUS_DESC.get(status_char, status_char)
                cb = tk.Checkbutton(row, variable=var,
                                    bg=C["mantle"], activebackground=C["mantle"],
                                    selectcolor=C["surface0"])
                cb.pack(side=tk.LEFT)
                tk.Label(row, text=xy_clean, width=3, anchor=tk.W,
                         font=("Consolas", 9, "bold"),
                         bg=C["mantle"], fg=color).pack(side=tk.LEFT)
                tk.Label(row, text=fname, anchor=tk.W,
                         font=("Consolas", 9),
                         bg=C["mantle"], fg=C["text"]).pack(side=tk.LEFT, padx=(2, 6))
                tk.Label(row, text=f"({desc})", anchor=tk.W,
                         font=("Segoe UI", 8, "italic"),
                         bg=C["mantle"], fg=C["overlay0"]).pack(side=tk.LEFT)

        # ── Quick-select buttons ──
        if is_repo and self._files:
            sel_row = tk.Frame(self, bg=C["base"])
            sel_row.pack(fill=tk.X, padx=20, pady=(0, 8))
            ttk.Button(sel_row, text="Select All",
                       command=lambda: self._set_all(True)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Select None",
                       command=lambda: self._set_all(False)).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(sel_row, text="Modified Only  (skip untracked)",
                       command=self._select_modified).pack(side=tk.LEFT)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(2, 8))

        # ── Commit message ──
        # Async-suggestion infrastructure:
        # `_suggestion_token` is bumped every time a new request is issued
        # (initial fill or 💡 Suggest click). The background worker stamps its
        # result with the token it captured at launch — if a newer request has
        # since started, the stale result is discarded.
        # `_user_has_edited` flips to True the moment the user touches the
        # field, after which background results are silently dropped so we
        # never overwrite their typing.
        self._suggestion_token = 0
        self._user_has_edited = False

        msg_hdr = tk.Frame(self, bg=C["base"])
        msg_hdr.pack(fill=tk.X, padx=20, pady=(0, 4))
        tk.Label(msg_hdr,
                 text="Commit message:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(side=tk.LEFT)
        # Spinner / status label between header and Suggest button — empty
        # when idle, "⟳ Generating with AI…" while the worker runs.
        self._suggest_status_lbl = tk.Label(
            msg_hdr, text="", font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["peach"])
        self._suggest_status_lbl.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(msg_hdr, text="💡 Suggest",
                   command=self._fill_suggestion).pack(side=tk.RIGHT)

        msg_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        msg_frame.pack(fill=tk.X, padx=20, pady=(0, 12))
        self._msg_txt = tk.Text(msg_frame, height=3,
                                bg=C["mantle"], fg=C["text"],
                                insertbackground=C["text"],
                                relief=tk.FLAT, font=("Segoe UI", 10),
                                padx=8, pady=6, wrap=tk.WORD)
        self._msg_txt.pack(fill=tk.X)

        # Mark the field as user-edited on any keypress (other than navigation).
        # This is how the async result knows to NOT overwrite the user's input.
        def _on_key(event):
            # Skip pure navigation / modifier keys so clicking around doesn't
            # falsely lock the field.
            if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                                 "Prior", "Next", "Shift_L", "Shift_R",
                                 "Control_L", "Control_R", "Alt_L", "Alt_R",
                                 "Tab", "Escape", "Caps_Lock"):
                return
            self._user_has_edited = True
        self._msg_txt.bind("<KeyPress>", _on_key, add="+")

        # Kick off the initial suggestion (sync if heuristic-only, async if LLM
        # might be involved — see `_populate_suggestion`).
        if is_repo:
            self._populate_suggestion(status_text, source="initial")
        self._msg_txt.focus_set()

        # ── Action buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 16))
        self._commit_btn = ttk.Button(btn_row, text="Commit Selected",
                                      style="Primary.TButton",
                                      command=self._apply,
                                      state=tk.NORMAL if is_repo and self._files else tk.DISABLED)
        self._commit_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Ctrl+Enter = commit
        self._msg_txt.bind("<Control-Return>", lambda e: self._apply())

        self.update_idletasks()
        content_h = self.winfo_reqheight() + 20
        max_h = max(420, parent.winfo_height() - 60)
        w = 580
        h = min(content_h, max_h)
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")

    # ── Selection helpers ────────────────────────────────────────────────────

    def _set_all(self, value: bool):
        for var, _f, _xy in self._file_vars:
            var.set(value)

    def _select_modified(self):
        """Select all tracked changes; uncheck untracked (??) files."""
        for var, _f, xy in self._file_vars:
            var.set(xy.strip() != "??")

    def _fill_suggestion(self):
        """Replace the commit message field with a fresh suggestion based on
        currently *selected* files only. Resets the user-edited flag so the
        async result is allowed to land — clicking 💡 Suggest is an explicit
        opt-in to overwrite whatever is in the field."""
        selected_lines = []
        for var, fname, xy in self._file_vars:
            if var.get():
                selected_lines.append(f"{xy} {fname}")
        sub_status = "\n".join(selected_lines) if selected_lines else self._status_raw
        # User pressed Suggest — they want to overwrite. Re-arm.
        self._user_has_edited = False
        self._populate_suggestion(sub_status, source="suggest_button")

    def _populate_suggestion(self, status_text: str, source: str = "initial"):
        """Run the commit-message orchestrator and populate the message field.

        Synchronous when LLM is disabled (heuristics are fast).
        Asynchronous when LLM is enabled — a daemon thread calls the
        orchestrator while the dialog stays responsive; the spinner label
        shows progress; the result lands via `self.after(0, …)`.

        `source` is informational ("initial" / "suggest_button") for logging.
        """
        llm_cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
        llm_active = bool(llm_cfg.get("enabled"))

        if not llm_active:
            # Heuristics only — instant, no spinner needed.
            result = _suggest_commit_message(self._path, status_text) or "chore: update files"
            self._apply_suggestion(result)
            return

        # Async path. Bump token so an in-flight earlier request loses the race.
        self._suggestion_token += 1
        my_token = self._suggestion_token

        # Visible spinner
        self._suggest_status_lbl.configure(
            text="⟳  Generating with AI…  (can take 30–60s on local models)",
            fg=C["peach"])
        # Optional: drop a placeholder in the field if it's currently empty,
        # so the user sees "something is happening" without confusing focus.
        if not self._msg_txt.get("1.0", tk.END).strip() and not self._user_has_edited:
            self._msg_txt.delete("1.0", tk.END)
            self._msg_txt.insert(tk.END, "(generating commit message…)")
            self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)
            # The placeholder counts as "not user-edited"; rebind so any
            # actual keystroke flips the flag.
            self._user_has_edited = False

        def _worker(path=self._path, st=status_text, tok=my_token):
            try:
                result = _suggest_commit_message(path, st)
            except Exception as exc:
                log.exception("Suggestion worker failed")
                result = ""
            # Always come back to the main thread to touch widgets.
            try:
                self.after(0, self._on_suggestion_ready, tok, result)
            except RuntimeError:
                # Dialog was destroyed before result arrived — silent drop.
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="commit-suggestion-worker").start()

    def _on_suggestion_ready(self, token: int, result: str):
        """Main-thread callback: receive the orchestrator's result."""
        # Clear spinner regardless of outcome.
        self._suggest_status_lbl.configure(text="")
        # Stale request (a newer Suggest click already ran) — discard.
        if token != self._suggestion_token:
            return
        # User started editing after we kicked off — respect their input.
        if self._user_has_edited:
            return
        # Empty result → keep placeholder cleared and offer a generic fallback
        if not result:
            result = "chore: update files"
        self._apply_suggestion(result)

    def _apply_suggestion(self, suggestion: str):
        """Insert `suggestion` into the message field, select first line."""
        self._msg_txt.delete("1.0", tk.END)
        self._msg_txt.insert(tk.END, suggestion)
        first_line_end = self._msg_txt.search("\n", "1.0", tk.END)
        if first_line_end:
            self._msg_txt.tag_add(tk.SEL, "1.0", first_line_end)
        else:
            self._msg_txt.tag_add(tk.SEL, "1.0", tk.END)
        # Inserting from code shouldn't count as user editing — keep the flag
        # consistent (it's already False; explicit reset for clarity).
        self._user_has_edited = False
        self._msg_txt.focus_set()

    def _apply(self):
        message = self._msg_txt.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Empty message",
                "Please enter a commit message.", parent=self)
            return
        # Pass xy alongside fname so the worker can distinguish
        # already-staged files (e.g. `git rm --cached` deletions from the
        # 🧹 Untrack flow) from files that still need `git add`. Without
        # this, calling git add on a staged deletion un-does the deletion
        # and re-encounters any matching .gitignore rule, blocking the
        # commit.
        selected = [(fname, xy) for var, fname, xy in self._file_vars
                    if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                "Tick at least one file to include in this commit.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, message, selected)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()
