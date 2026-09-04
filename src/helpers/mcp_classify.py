"""What an MCP entry is, what is wrong with it, and how to repair it.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import time
from helpers.mcp_paths import (
    DESKTOP_SCOPE_RETIRED_KEY,
    PROJECT_PATH_ARG,
    USER_SCOPE_RETIRED_KEY,
    _same_project,
    _wrapper_path,
    _write_json_atomic,
)




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



def _retired_absence(base: dict, cfg: dict, *, is_claude_code: bool,
                     is_project_scoped: bool) -> "dict | None":
    """An ABSENCE that was chosen is not a defect. ``None`` when it wasn't.

    Both global migrations end with an empty `mcpServers`, and that is the
    point of them: each project serves its own graph and the global fallback
    is deliberately gone. Reporting it as "✗ no tokensave entry — click Apply"
    told the user to undo the migration they had just completed, in the same
    dialog whose next panel congratulated them for finishing it. Measured
    twice, once per migration.

    **Four surfaces read this verdict** — startup banner, Settings summary,
    pin note and the MCP dialog — so the correction belongs here rather than
    in each of them.

    A project `.mcp.json` is never excused: retiring the global entries is
    what makes project bindings load-bearing, so an unbound project still
    needs binding and must keep saying so.
    """
    if is_project_scoped:
        return None
    if is_claude_code and cfg.get(USER_SCOPE_RETIRED_KEY):
        return {**base, "state": "ok",
                "label": "✓ retired — projects serve themselves",
                "issue": ("Deliberately empty. The user-scoped entry was "
                          "retired, so each project's own .mcp.json is "
                          "authoritative and a project with no binding gets "
                          "no tokensave at all — which is the point. Nothing "
                          "to do here.")}
    if not is_claude_code and cfg.get(DESKTOP_SCOPE_RETIRED_KEY):
        return {**base, "state": "ok",
                "label": "✓ retired — projects serve themselves",
                "issue": ("Deliberately empty. Claude Desktop's entry ran the "
                          "pin-aware wrapper, which serves one project for the "
                          "whole machine and outranks every project's own "
                          ".mcp.json. Retiring it is what makes Claude Code "
                          "sessions serve their own project. Claude Desktop "
                          "chat has no tokensave as a result — the deliberate "
                          "trade. Nothing to do here.")}
    return None



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
        chosen = _retired_absence(base, cfg, is_claude_code=is_claude_code,
                                  is_project_scoped=is_project_scoped)
        if chosen is not None:
            return chosen
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
