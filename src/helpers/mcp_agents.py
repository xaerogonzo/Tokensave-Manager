"""Per-agent wiring tables for codegraph and tokensave.

Split out of helpers/mcp.py (Roadmap-16 god-file split).
Importable via the ``helpers.mcp`` facade, which re-exports
every name, so existing call sites and tests are unchanged.
This module must never import that facade.
"""

from __future__ import annotations

import json
import os




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
