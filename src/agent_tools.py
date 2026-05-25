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

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from dialogs.proposal import WriteProposal

CREATE_NO_WINDOW = 0x08000000   # mirrors constants.CREATE_NO_WINDOW

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
        commits, change config). Write tools are routed through the
        agent's `on_write_proposal` bridge for ProposalDialog gating
        before `post_accept` runs. **Mis-labelling a write tool as read
        is a security defect.**
    proposal_builder : Callable[[dict], WriteProposal | str] | None
        For write tools (`is_write=True`): function that takes the JSON-
        decoded arguments dict and returns either a `WriteProposal` (which
        gets shown to the user via ProposalDialog) OR an error string
        starting with `[tool error]` (validation failure — no dialog, no
        write, error returned to the model directly). None for read tools.
    post_accept : Callable[[WriteProposal, str], str] | None
        For write tools: function that does the actual file mutation AFTER
        the user accepts. Takes the original WriteProposal plus the
        final (possibly user-edited) content; returns a result string the
        model sees (e.g. "wrote 24 lines to src/X.py"). Must perform the
        race-safe hash recheck against `proposal.original_hash` before
        writing. None for read tools.
    """
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    is_write: bool = False
    proposal_builder: Callable[[dict], "WriteProposal | str"] | None = None
    post_accept: Callable[["WriteProposal", str], str] | None = None

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
            # File not found — look for likely matches by basename so
            # the model gets an actionable hint instead of just "no".
            # qwen2.5-coder and similar local models frequently give up
            # after a single tool error; suggesting concrete retry
            # paths keeps the agent loop productive.
            return _suggest_paths_for_missing_file(project_path, path)

        # Line-range mode: when start_line / end_line are provided, read
        # only that window. Critical for files larger than 50 KB
        # (controllers/projects_tab.py and dialogs/settings.py are the
        # largest post-Round-4 files, ~70–80 KB each) —
        # without this, the model can't ever see anything past the
        # first 50 KB of byte-truncated output. tokensave_search hands
        # back precise line numbers for symbols; the model is expected
        # to use them here.
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        if start_line is not None or end_line is not None:
            try:
                start = max(1, int(start_line)) if start_line is not None else 1
            except (TypeError, ValueError):
                return "[tool error] start_line must be a positive integer"
            try:
                end = int(end_line) if end_line is not None else None
            except (TypeError, ValueError):
                return "[tool error] end_line must be a positive integer"
            if end is not None and end < start:
                return (f"[tool error] end_line ({end}) is less than "
                        f"start_line ({start}) — swap them or omit end_line")
            return _read_file_range(full, start, end)

        # Default: full-file read with byte cap.
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                # Read one byte over the cap so we can detect truncation.
                content = f.read(_READ_FILE_MAX_BYTES + 1)
        except OSError as e:
            return f"[tool error] {type(e).__name__}: {e}"
        truncated = len(content) > _READ_FILE_MAX_BYTES
        if truncated:
            content = (content[:_READ_FILE_MAX_BYTES]
                       + f"\n[... truncated at {_READ_FILE_MAX_BYTES} bytes. "
                         f"This file is larger than the cap.  Re-call "
                         f"read_file with start_line and end_line set "
                         f"to read a specific section.  Use "
                         f"tokensave_search to find the line number of "
                         f"a symbol, then pass those line numbers here.]")
        return content
    return _read_file


def _read_file_range(full_path: str, start: int, end: int | None) -> str:
    """Read a specific line range from a file, returning the result with
    line numbers prepended.

    Line numbers in the output make it trivial for the model to cite
    file:line in its final answer. Same byte cap applies — pass a
    smaller range if the result is still too big.
    """
    try:
        out_lines = []
        with open(full_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if end is not None and i > end:
                    break
                if i >= start:
                    out_lines.append(f"{i:>6}  {line.rstrip()}")
    except OSError as e:
        return f"[tool error] {type(e).__name__}: {e}"
    if not out_lines:
        end_str = str(end) if end is not None else "EOF"
        return (f"(no lines in range {start}..{end_str} — file may be "
                f"shorter than {start} lines)")
    result = "\n".join(out_lines)
    if len(result) > _READ_FILE_MAX_BYTES:
        result = (result[:_READ_FILE_MAX_BYTES]
                  + f"\n[... range still exceeds {_READ_FILE_MAX_BYTES} "
                    "bytes.  Narrow the range with a closer "
                    "start_line / end_line.]")
    return result


def _suggest_paths_for_missing_file(project_path: str, requested: str) -> str:
    """Build a `[tool error] file not found` message with concrete retry
    suggestions.

    When the model asks to read a file by bare basename or with a wrong
    leading directory (very common — local models often guess "foo.py"
    when the actual path is "src/foo.py"), we walk the project tree
    looking for files whose basename matches the request and surface
    up to 5 candidates. This converts a dead-end "file not found" into
    an actionable hint the next iteration can act on.
    """
    basename = os.path.basename(requested.replace("\\", "/")).strip()
    if not basename:
        return f"[tool error] file not found: {requested}"

    matches: list[str] = []
    basename_lower = basename.lower()
    try:
        for root, dirs, files in os.walk(project_path):
            # Skip hidden and noisy dirs to keep the search fast.
            dirs[:] = [d for d in dirs
                       if not d.startswith(".")
                       and d not in ("__pycache__", "node_modules",
                                     "venv", ".venv", "dist", "build")]
            for f in files:
                if f.lower() == basename_lower:
                    rel = os.path.relpath(
                        os.path.join(root, f), project_path)
                    matches.append(rel.replace("\\", "/"))
                    if len(matches) >= 5:
                        break
            if len(matches) >= 5:
                break
    except OSError:
        # Walking failed mid-stream; deliver whatever we got.
        pass

    if not matches:
        return (f"[tool error] file not found: {requested}.  "
                "Use list_directory to see what's available in the "
                "project root or its subdirectories.")
    return (f"[tool error] file not found at '{requested}', but a file "
            f"named '{basename}' exists at: {', '.join(matches)}.  "
            f"Retry read_file with one of those paths.")


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
    """Build a handler for one of tokensave's CLI subcommands.

    Note on naming mismatch: the MCP tools we expose to the model are
    `tokensave_search` and `tokensave_context`, but the tokensave CLI
    uses the subcommands `query` (not `search`) and `context`. The MCP-
    style names are preferred at the agent-facing layer because that's
    what users / the model already know from tokensave's own MCP server.
    Internally, `subcommand="search"` maps to the `query` CLI command.

    Also note: neither `query` nor `context` accept a `--json` flag.
    The `context` subcommand has `--format json|markdown` instead. The
    `query` subcommand has no structured output mode — we get plain
    text and pass it through.
    """
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

        # Map the agent-facing subcommand name to the actual CLI
        # subcommand, and build the argv with the correct flags.
        if subcommand == "search":
            # tokensave's CLI calls this `query` (not `search` — that
            # would clash with `search` in shell scripts maybe?).
            cli_subcommand = "query"
            cli_args = [tokensave_exe, "query", query]
            display_name = "tokensave query"
        elif subcommand == "context":
            cli_subcommand = "context"
            # Cap nodes lower than tokensave's default 20: subgraph JSON
            # is dense with metadata (IDs, columns, attrs_start_line,
            # etc.) and we slim it further in _slim_tokensave_context
            # below, but a smaller node count keeps both the wire size
            # and the model's reasoning load down.
            cli_args = [tokensave_exe, "context",
                        "--format", "json",
                        "--max-nodes", "10",
                        query]
            display_name = "tokensave context"
        else:
            return f"[tool error] unknown tokensave subcommand: {subcommand}"

        try:
            r = subprocess.run(
                cli_args,
                cwd=project_path,
                capture_output=True, text=True, timeout=20,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"[tool error] tokensave executable not found at {tokensave_exe}"
        except subprocess.TimeoutExpired:
            return f"[tool error] {display_name} timed out after 20s"
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            return (f"[tool error] {display_name} failed "
                    f"(rc={r.returncode}): {err[:500]}")
        if not out:
            return f"(no results for: {query})"
        if len(out) > _TOKENSAVE_MAX_CHARS:
            out = (out[:_TOKENSAVE_MAX_CHARS]
                   + f"\n[... output truncated at {_TOKENSAVE_MAX_CHARS} chars ...]")
        # context returns valid JSON when --format json is used. Slim
        # the node dicts down to just the fields the model actually
        # needs (name, kind, file_path, start_line, signature, parent)
        # — the raw tokensave output also includes IDs, column numbers,
        # docstrings, attrs_start_line, branches/loops/returns counts,
        # etc. that bloat the wire by ~3-5x without helping the model
        # answer code questions. query output is plain text and passes
        # through unchanged.
        if cli_subcommand == "context":
            try:
                parsed = json.loads(out)
                slimmed = _slim_tokensave_context(parsed)
                return json.dumps(slimmed, indent=2)[:_TOKENSAVE_MAX_CHARS]
            except json.JSONDecodeError:
                # Fallback if --format json wasn't honored for some
                # reason (older tokensave versions).
                return out
        return out
    return _runner


def _slim_tokensave_context(ctx: dict) -> dict:
    """Strip noise from `tokensave context --format json` output.

    The raw response includes per-node metadata that's useful for IDE
    integrations (column numbers, internal hash IDs, attrs vs start
    line, complexity metrics) but is dead weight for an LLM trying to
    answer a structural question. This function returns a slim version
    keeping only the fields a code-Q&A agent needs: name, kind,
    qualified_name, file_path, start_line, end_line, signature, parent_id.

    Edges and the top-level summary/query fields pass through unchanged.
    On any structural surprise (missing keys, wrong types) the function
    falls back to returning the input untouched — better to deliver
    something the model can use than to crash on a future schema change.
    """
    if not isinstance(ctx, dict):
        return ctx
    try:
        sub = ctx.get("subgraph") or {}
        nodes = sub.get("nodes")
        if isinstance(nodes, list):
            slim_nodes = []
            for n in nodes:
                if not isinstance(n, dict):
                    slim_nodes.append(n)
                    continue
                slim_nodes.append({
                    k: n[k] for k in
                    ("name", "kind", "qualified_name", "file_path",
                     "start_line", "end_line", "signature", "parent_id")
                    if k in n and n[k] is not None
                })
            sub = {**sub, "nodes": slim_nodes}
        return {
            "query":   ctx.get("query"),
            "summary": ctx.get("summary"),
            "subgraph": sub,
        }
    except Exception:
        # Schema drift defence: pass through if anything looks off.
        return ctx


# ───────────────────────────────────────────────────────────────────────
# Per-tool spec factories
# ───────────────────────────────────────────────────────────────────────

def _tool_read_file(project_path: str) -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description=(
            "Read a file's contents. Returns up to 50 KB; longer "
            "files are truncated with a notice. THIS IS THE PRIMARY "
            "TOOL FOR ANSWERING 'why does X behave like Y' or 'how "
            "does X work' questions — when the user names a file, "
            "read it directly instead of searching.\n\n"
            "FOR LARGE FILES (e.g. src/controllers/projects_tab.py or "
            "src/dialogs/settings.py which are over 50 KB): pass "
            "`start_line` and `end_line` to read a "
            "specific section. tokensave_search returns the exact "
            "line number where each symbol is DEFINED — pass that "
            "as `start_line` and set `end_line` 150-200 lines later "
            "so you read the full function body, not just the "
            "signature + docstring.\n\n"
            "If the path isn't found, the error message will "
            "suggest the correct location."),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": ("Path relative to the project root, "
                                    "e.g. 'src/main.py' or 'README.md'."),
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("Optional 1-based line number to "
                                    "start reading from.  Use with "
                                    "end_line to read just a section "
                                    "of a large file."),
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("Optional 1-based last line to "
                                    "read, inclusive.  Defaults to "
                                    "end of file."),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_make_read_file(project_path),
    )


def _tool_list_directory(project_path: str) -> ToolSpec:
    return ToolSpec(
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
    )


def _tool_git_log(project_path: str) -> ToolSpec:
    return ToolSpec(
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
                    "default": 20,
                    "description": "Number of commits to show (1-100). Default 20.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_make_git_log(project_path),
    )


def _tool_git_diff(project_path: str) -> ToolSpec:
    return ToolSpec(
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
    )


def _tool_tokensave_search(project_path: str, tokensave_exe: str) -> ToolSpec:
    return ToolSpec(
        name="tokensave_search",
        description=(
            "Find DEFINED SYMBOLS (functions, classes, methods, "
            "constants) in the project's tokensave code graph by "
            "name. Returns matched node names with file:line "
            "locations. NOT a full-text grep — searching for "
            "keywords like 'Popen', 'import', or arbitrary "
            "substrings will return nothing. Use this to answer "
            "'where is the symbol X defined?'. For reading actual "
            "file contents, use read_file."),
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
    )


def _tool_tokensave_context(project_path: str, tokensave_exe: str) -> ToolSpec:
    return ToolSpec(
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
    )


# ───────────────────────────────────────────────────────────────────────
# Write tool — Stage 3: gated by ProposalDialog via agent's bridge
# ───────────────────────────────────────────────────────────────────────

_WRITE_FILE_LARGE_LINES = 400  # threshold for the "prefer minimal edits" warning

_WRITE_FILE_DESCRIPTION = (
    "Write a file in the project. The user MUST review and accept "
    "your proposed content via a dialog before the write happens — "
    "this tool is gated by user approval, never autonomous.\n\n"
    "When to use:\n"
    "- The user has asked you to change a file (e.g. \"fix the bug "
    "in foo.py\", \"add a docstring to bar()\").\n"
    "- After reading the current file (use read_file FIRST so your "
    "`content` is built on top of the real existing file, not your "
    "guess of what's there).\n\n"
    "Arguments:\n"
    "- `path`: project-relative target path (forward slashes preferred).\n"
    f"- `content`: the complete new file contents (this is a FULL "
    f"REWRITE — for files over {_WRITE_FILE_LARGE_LINES} lines, "
    "PREFER minimal edits and preserve unrelated content verbatim. "
    "Full-file regeneration on large files frequently strips "
    "imports, reorders sections, and stomps comments).\n"
    "- `rationale`: one-sentence explanation of WHY this edit is "
    "needed. The user sees this in the approval dialog.\n\n"
    "What happens:\n"
    "1. The dialog opens with a side-by-side diff (original vs "
    "proposed). The user can edit your proposed content directly "
    "in the dialog before accepting.\n"
    "2. If the user accepts: the file is written atomically; you get "
    "back \"Wrote N lines to <path>.\".\n"
    "3. If the user rejects, closes, or doesn't act within 5 minutes: "
    "you get back a `[write rejected]` string. Do NOT retry "
    "automatically — explain to the user what you tried."
)

_WRITE_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Project-relative path to the file to write.",
        },
        "content": {
            "type": "string",
            "description": "Complete new file contents.",
        },
        "rationale": {
            "type": "string",
            "description": "One-sentence justification, shown to the user.",
        },
    },
    "required": ["path", "content", "rationale"],
    "additionalProperties": False,
}


def _validate_write_args(args: dict) -> tuple[str, str, str] | str:
    """Return (path, content, rationale) or an `[tool error]` string."""
    path = (args.get("path") or "").strip()
    content = args.get("content")
    rationale = (args.get("rationale") or "").strip()
    if not path:
        return "[tool error] missing required arg: path"
    if content is None:
        return "[tool error] missing required arg: content"
    if not rationale:
        return ("[tool error] missing required arg: rationale "
                "(explain WHY the edit is needed, in one sentence)")
    return path, str(content), rationale


def _capture_original_file(full: str) -> tuple[str, str, float] | str:
    """Read existing file or return defaults for a new file.

    Returns (content, sha256_hex, mtime) on success, or an error string.
    For a missing file: returns ("", "", 0.0). For existing-but-not-file:
    returns an error string. The empty hash is the signal that the file
    didn't exist at proposal-build time.
    """
    if not os.path.exists(full):
        return "", "", 0.0
    if not os.path.isfile(full):
        return f"[tool error] target '{full}' exists but is not a regular file"
    try:
        with open(full, encoding="utf-8") as f:
            original_content = f.read()
    except OSError as e:
        return f"[tool error] could not read existing file: {e}"
    original_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
    try:
        original_mtime = os.path.getmtime(full)
    except OSError:
        original_mtime = 0.0
    return original_content, original_hash, original_mtime


def _compute_dirs_to_create(project_path: str, full: str) -> list[str]:
    """Return project-relative directory paths (top-down) that don't exist
    yet and would be created by an `os.makedirs(parent, exist_ok=True)`
    call before writing `full`. Surfaced in the ProposalDialog so the user
    sees the full side-effect set.
    """
    parent = os.path.dirname(full)
    if not parent or os.path.isdir(parent):
        return []
    missing: list[str] = []
    walk = parent
    project_real = os.path.realpath(project_path)
    while walk and walk != project_real and not os.path.isdir(walk):
        missing.append(walk)
        new_walk = os.path.dirname(walk)
        if new_walk == walk:
            break
        walk = new_walk
    return [
        os.path.relpath(d, project_path).replace("\\", "/") + "/"
        for d in reversed(missing)
    ]


def _check_write_race(proposal: "WriteProposal") -> str | None:
    """Re-read the file at write-time; return error string if changed, else None.

    Hash is authoritative. The empty-hash + file-now-exists case is also
    a race (someone created the file between proposal-build and accept).
    """
    full = proposal.filepath
    current_hash = ""
    if os.path.exists(full):
        try:
            with open(full, encoding="utf-8") as f:
                current_hash = hashlib.sha256(
                    f.read().encode("utf-8")).hexdigest()
        except OSError as e:
            return f"[tool error] could not re-read target for race check: {e}"
    if proposal.original_hash and current_hash != proposal.original_hash:
        return ("[write rejected: file changed on disk while proposal was "
                "open. Re-issue the write_file call after re-reading.]")
    if not proposal.original_hash and current_hash:
        return ("[write rejected: target was created on disk while proposal "
                "was open. Re-read before issuing a new write_file call.]")
    return None


def _atomic_write_file(full: str, content: str) -> str | None:
    """Write `content` to `full` atomically via .tmp + os.replace.

    Returns None on success, `[tool error]` string on failure. Creates parent
    directories if missing (same set already surfaced in the proposal's
    dirs_to_create). Always ends file with newline.
    """
    parent = os.path.dirname(full)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"[tool error] could not create parent directory: {e}"
    tmp = full + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        os.replace(tmp, full)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return f"[tool error] write failed: {e}"
    return None


def _tool_write_file(project_path: str) -> ToolSpec:
    """Write-tool factory. Construction is split into proposal_builder
    (validates + builds the WriteProposal shown to the user) and
    post_accept (does the atomic write after the user accepts).

    The two-step shape is what `agent.py:_dispatch_tool` orchestrates: it
    calls `proposal_builder`, hands the result to the user-approval bridge,
    then calls `post_accept` only if the user accepted.
    """
    # Import lazily so module-load doesn't pull in Tk via dialogs/proposal.py
    # (agent_tools is imported by the wrapper and CLI tools too).
    from dialogs.proposal import WriteProposal

    def _builder(args: dict) -> "WriteProposal | str":
        validated = _validate_write_args(args)
        if isinstance(validated, str):
            return validated
        path, content, rationale = validated

        # Symlink-safe containment — `_under_project` uses os.path.realpath
        full = _under_project(project_path, path)
        if full is None:
            return f"[tool error] path '{path}' is outside the project root"
        if os.path.islink(full):
            return f"[tool error] target '{path}' is a symlink; refusing to follow"

        captured = _capture_original_file(full)
        if isinstance(captured, str):
            return captured
        original_content, original_hash, original_mtime = captured

        return WriteProposal(
            filepath=full,
            original_content=original_content,
            proposed_content=content,
            rationale=rationale,
            original_hash=original_hash,
            original_mtime=original_mtime,
            dirs_to_create=_compute_dirs_to_create(project_path, full),
        )

    def _post_accept(proposal: "WriteProposal", final_content: str) -> str:
        race_err = _check_write_race(proposal)
        if race_err is not None:
            return race_err
        write_err = _atomic_write_file(proposal.filepath, final_content)
        if write_err is not None:
            return write_err
        line_count = final_content.count("\n") + (
            0 if final_content.endswith("\n") else 1)
        rel = os.path.relpath(proposal.filepath, project_path).replace("\\", "/")
        return f"Wrote {line_count} lines to {rel}."

    def _handler_unused(_args: dict) -> str:
        # Defence-in-depth — should NEVER be called. agent._dispatch_tool
        # routes is_write=True tools through proposal_builder + post_accept
        # and never invokes spec.handler.
        return ("[tool error] write_file.handler called directly; this should "
                "never happen — agent dispatch must route through "
                "proposal_builder + post_accept.")

    return ToolSpec(
        name="write_file",
        description=_WRITE_FILE_DESCRIPTION,
        parameters=_WRITE_FILE_PARAMETERS,
        handler=_handler_unused,
        is_write=True,
        proposal_builder=_builder,
        post_accept=_post_accept,
    )


# ───────────────────────────────────────────────────────────────────────
# Registry builder
# ───────────────────────────────────────────────────────────────────────

def build_tools(project_path: str, tokensave_exe: str = "",
                with_write: bool = False) -> dict[str, ToolSpec]:
    """Construct the tool registry for a given project.

    `with_write=True` includes the gated `write_file` tool (Stage 3).
    Default is read-only for backwards compatibility — call sites that
    want the write tool must opt in explicitly.
    """
    tools = {
        "read_file":         _tool_read_file(project_path),
        "list_directory":    _tool_list_directory(project_path),
        "git_log":           _tool_git_log(project_path),
        "git_diff":          _tool_git_diff(project_path),
        "tokensave_search":  _tool_tokensave_search(project_path, tokensave_exe),
        "tokensave_context": _tool_tokensave_context(project_path, tokensave_exe),
    }
    if with_write:
        tools["write_file"] = _tool_write_file(project_path)
    return tools


def tools_as_openai_array(tools: dict[str, ToolSpec]) -> list[dict]:
    """Convenience: render the whole registry as the `tools` payload field
    in an OpenAI-compatible chat completion request."""
    return [spec.to_openai_tool() for spec in tools.values()]
