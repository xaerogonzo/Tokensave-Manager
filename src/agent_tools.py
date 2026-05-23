"""
agent_tools.py — Tool registry for the Stage 2 agent chat (🤖 Ask tab).

Every tool the agent can call lives here as a `ToolSpec` entry. Adding a new
tool means adding ONE registry entry — no scattered definitions, no ad-hoc
shell-outs inside dialog code. See `docs/AGENT_ARCHITECTURE.md` rule #4.

All tools in this v1 are READ-ONLY (`is_write=False`). Stage 3+ will add
write tools, gated by ProposalDialog.

Tool handlers are pure functions: they take a dict of arguments (JSON-decoded
from the LLM's tool_call.arguments field) and return a string (whatever the
LLM should see as the tool's result). Errors are returned AS STRINGS prefixed
`[tool error]` rather than raised — this lets the model self-correct ("the
file doesn't exist, let me list the directory instead") rather than crash
the background agent thread.

Path-bearing arguments are validated by `_under_project` to prevent the model
from reading `C:\\Users\\...` or other locations outside the project root.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

CREATE_NO_WINDOW = 0x08000000   # mirrors tokensave-manager.py

# Per-tool output caps. Individual caps + the cumulative context budget in
# LocalAgent together prevent context-window saturation.
_READ_FILE_MAX_BYTES = 50 * 1024
_GIT_DIFF_MAX_CHARS  = 24 * 1024
_GIT_LOG_DEFAULT_N   = 20
_LIST_DIR_MAX_ENTRIES = 200
_TOKENSAVE_MAX_CHARS = 30 * 1024


# ───────────────────────────────────────────────────────────────────────
# ToolSpec
# ───────────────────────────────────────────────────────────────────────

@dataclass
class ToolSpec:
    """Specification for a single tool the agent can call.

    Attributes
    ----------
    name : str
        Tool identifier the LLM uses in its `tool_calls[].function.name`.
        Must be a valid Python identifier (Ollama / OpenAI tool-calling
        format requires this).
    description : str
        Plain-English description shown to the LLM as part of the tools
        array. Keep terse — local models pay attention to wording but
        have limited context budget.
    parameters : dict
        OpenAI-style JSON Schema for the tool arguments. The agent
        validates `tool_call.arguments` against this schema before
        dispatching to the handler. Always include `"additionalProperties":
        false` to stop the model from inventing extra fields.
    handler : Callable[[dict], str]
        Function taking the JSON-decoded arguments dict and returning the
        tool result as a string. Must not raise — errors are returned as
        `[tool error] ...` strings. The agent's master try/except in
        LocalAgent catches anything that does slip through.
    is_write : bool
        Mark `True` for tools that mutate state (write files, run
        commits, change config). v1 has no write tools — Stage 3+ will
        add them with ProposalDialog gating. **Mis-labelling a write
        tool as read is a security defect.**
    """
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    is_write: bool = False

    def to_openai_tool(self) -> dict:
        """Render this ToolSpec as an OpenAI-style tools[] entry.

        Ollama, LM Studio, and OpenAI all accept the same shape:
            {"type": "function", "function": {"name": ..., "description":
             ..., "parameters": {...}}}
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ───────────────────────────────────────────────────────────────────────
# Path-containment helper
# ───────────────────────────────────────────────────────────────────────

def _under_project(project_path: str, raw_path: str) -> str | None:
    """Resolve `raw_path` against `project_path` and return the absolute
    path if it sits inside the project root.

    Uses `os.path.realpath` on both sides to defeat symlink traversal,
    and `os.path.normcase` for the comparison so Windows case-insensitive
    paths don't slip through. Returns None if the result is outside the
    project root or if path resolution fails.

    A blank `raw_path` resolves to the project root itself, which IS
    inside the project — useful for list_directory(""). Absolute paths
    are tolerated as long as they end up inside the project.
    """
    if not project_path:
        return None
    try:
        if os.path.isabs(raw_path):
            full = os.path.realpath(raw_path)
        else:
            full = os.path.realpath(os.path.join(project_path, raw_path or "."))
        root = os.path.realpath(project_path)
    except (OSError, ValueError):
        return None
    full_n = os.path.normcase(full)
    root_n = os.path.normcase(root)
    # Require either equality OR a proper subpath (avoid /foo matching /foobar).
    if full_n == root_n:
        return full
    if full_n.startswith(root_n + os.sep):
        return full
    return None


def _run_git(project_path: str, *args: str, timeout: int = 10) -> tuple[int, str]:
    """Run a git command in `project_path` and return (returncode, output).

    Output is merged stderr+stdout so any error message also reaches the
    LLM. Encoding is utf-8 with `replace` for binary file diffs.
    """
    try:
        r = subprocess.run(
            ["git", "-C", project_path, *args],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "[git not found on PATH]"
    except subprocess.TimeoutExpired:
        return 124, f"[git command timed out after {timeout}s]"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


# ───────────────────────────────────────────────────────────────────────
# Tool handlers (closures over project_path / tokensave_exe via build_tools)
# ───────────────────────────────────────────────────────────────────────

def _make_read_file(project_path: str):
    def _read_file(args: dict) -> str:
        path = (args.get("path") or "").strip()
        if not path:
            return "[tool error] missing required arg: path"
        full = _under_project(project_path, path)
        if full is None:
            return f"[tool error] path '{path}' is outside the project root"
        if not os.path.isfile(full):
            return f"[tool error] file not found: {path}"
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                # Read one byte over the cap so we can detect truncation.
                content = f.read(_READ_FILE_MAX_BYTES + 1)
        except OSError as e:
            return f"[tool error] {type(e).__name__}: {e}"
        truncated = len(content) > _READ_FILE_MAX_BYTES
        if truncated:
            content = (content[:_READ_FILE_MAX_BYTES]
                       + f"\n[... truncated at {_READ_FILE_MAX_BYTES} bytes — "
                         "ask for a specific section if you need more ...]")
        return content
    return _read_file


def _make_list_directory(project_path: str):
    def _list_directory(args: dict) -> str:
        path = (args.get("path") or "").strip()
        full = _under_project(project_path, path)
        if full is None:
            return f"[tool error] path '{path}' is outside the project root"
        if not os.path.isdir(full):
            return f"[tool error] not a directory: {path or '.'}"
        try:
            entries = sorted(os.listdir(full))
        except OSError as e:
            return f"[tool error] {type(e).__name__}: {e}"
        if not entries:
            return "(empty directory)"
        truncated = len(entries) > _LIST_DIR_MAX_ENTRIES
        shown = entries[:_LIST_DIR_MAX_ENTRIES]
        lines = []
        for name in shown:
            tag = "/" if os.path.isdir(os.path.join(full, name)) else ""
            lines.append(name + tag)
        result = "\n".join(lines)
        if truncated:
            result += (f"\n[... {len(entries) - _LIST_DIR_MAX_ENTRIES} "
                       f"more entries truncated ...]")
        return result
    return _list_directory


def _make_git_log(project_path: str):
    def _git_log(args: dict) -> str:
        n = args.get("n", _GIT_LOG_DEFAULT_N)
        try:
            n = max(1, min(int(n), 100))
        except (TypeError, ValueError):
            n = _GIT_LOG_DEFAULT_N
        rc, out = _run_git(project_path, "log", "--oneline", f"-n{n}")
        if rc == 127:
            return out
        if rc != 0:
            return f"[tool error] git log failed (rc={rc}): {out.strip()}"
        return out.strip() or "(no commits)"
    return _git_log


def _make_git_diff(project_path: str):
    def _git_diff(args: dict) -> str:
        path = (args.get("path") or "").strip()
        cmd = ["diff", "HEAD"]
        if path:
            cmd += ["--", path]
        rc, out = _run_git(project_path, *cmd, timeout=15)
        if rc == 127:
            return out
        if rc != 0:
            return f"[tool error] git diff failed (rc={rc}): {out.strip()[:500]}"
        if not out.strip():
            return "(no pending diff)"
        if len(out) > _GIT_DIFF_MAX_CHARS:
            out = (out[:_GIT_DIFF_MAX_CHARS]
                   + f"\n[... diff truncated at {_GIT_DIFF_MAX_CHARS} chars ...]")
        return out
    return _git_diff


def _make_tokensave_runner(project_path: str, tokensave_exe: str,
                            subcommand: str):
    def _runner(args: dict) -> str:
        if not tokensave_exe or not os.path.isfile(tokensave_exe):
            return ("[tool error] tokensave executable not configured. "
                    "Set tokensave_exe in manager-config.json.")
        if not os.path.isdir(os.path.join(project_path, ".tokensave")):
            return ("[tool note] this project has no tokensave index "
                    "(.tokensave/ missing) — run 'tokensave init' first, "
                    "or use read_file / git_log / list_directory instead.")
        query = (args.get("query") or "").strip()
        if not query:
            return "[tool error] missing required arg: query"
        try:
            r = subprocess.run(
                [tokensave_exe, subcommand, "--json", query],
                cwd=project_path,
                capture_output=True, text=True, timeout=20,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"[tool error] tokensave executable not found at {tokensave_exe}"
        except subprocess.TimeoutExpired:
            return f"[tool error] tokensave {subcommand} timed out after 20s"
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            return (f"[tool error] tokensave {subcommand} failed "
                    f"(rc={r.returncode}): {err[:500]}")
        if not out:
            return f"(no results for: {query})"
        if len(out) > _TOKENSAVE_MAX_CHARS:
            out = (out[:_TOKENSAVE_MAX_CHARS]
                   + f"\n[... output truncated at {_TOKENSAVE_MAX_CHARS} chars ...]")
        # Try to pretty-print if it's valid JSON, otherwise pass through.
        try:
            parsed = json.loads(out)
            return json.dumps(parsed, indent=2)[:_TOKENSAVE_MAX_CHARS]
        except json.JSONDecodeError:
            return out
    return _runner


# ───────────────────────────────────────────────────────────────────────
# Registry builder
# ───────────────────────────────────────────────────────────────────────

def build_tools(project_path: str, tokensave_exe: str = "") -> dict[str, ToolSpec]:
    """Construct the tool registry for a given project.

    Handlers are closures over `project_path` and `tokensave_exe` so they
    don't need to be passed through every call. Return a new registry
    per project (cheap — these are dataclass instances).
    """
    return {
        "read_file": ToolSpec(
            name="read_file",
            description=(
                "Read the contents of a file inside the current project. "
                "Returns up to 50 KB; longer files are truncated with a "
                "notice. Use this when you need the literal text of a file. "
                "For finding WHERE something is defined, prefer "
                "tokensave_search."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": ("Path relative to the project root, "
                                        "e.g. 'src/main.py' or 'README.md'."),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_make_read_file(project_path),
        ),
        "list_directory": ToolSpec(
            name="list_directory",
            description=(
                "List entries in a directory inside the project. Entries "
                "ending with '/' are subdirectories. Use this to discover "
                "what files exist before reading them."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": ("Directory path relative to the "
                                        "project root. Pass '' or '.' for "
                                        "the project root."),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_make_list_directory(project_path),
        ),
        "git_log": ToolSpec(
            name="git_log",
            description=(
                "Show the last N commits as one line each (sha + subject). "
                "Default 20. Useful for understanding recent history."),
            parameters={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Number of commits to show (1-100).",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_make_git_log(project_path),
        ),
        "git_diff": ToolSpec(
            name="git_diff",
            description=(
                "Show the pending diff (git diff HEAD) for the project, "
                "or for a specific path. Returns up to 24 KB; longer diffs "
                "are truncated."),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": ("Optional path to limit the diff. "
                                        "Pass '' for the whole project."),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=_make_git_diff(project_path),
        ),
        "tokensave_search": ToolSpec(
            name="tokensave_search",
            description=(
                "Search the project's tokensave code graph for symbols / "
                "files matching a natural-language or substring query. "
                "Returns JSON with matched nodes (functions, classes, "
                "methods) and their file:line locations. Far cheaper than "
                "grepping. Skips with a notice if the project has no "
                "tokensave index."),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": ("What to search for, e.g. "
                                        "'commit message generator' or "
                                        "'_call_llm'."),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_make_tokensave_runner(project_path, tokensave_exe, "search"),
        ),
        "tokensave_context": ToolSpec(
            name="tokensave_context",
            description=(
                "Build an AI-ready context bundle from the project's "
                "tokensave code graph for a natural-language task. "
                "Returns related symbols, relationships, and code snippets. "
                "Prefer this over many read_file calls when answering "
                "questions about code structure."),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": ("Natural-language description of "
                                        "what you want context for."),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_make_tokensave_runner(project_path, tokensave_exe, "context"),
        ),
    }


def tools_as_openai_array(tools: dict[str, ToolSpec]) -> list[dict]:
    """Convenience: render the whole registry as the `tools` payload field
    in an OpenAI-compatible chat completion request."""
    return [spec.to_openai_tool() for spec in tools.values()]
