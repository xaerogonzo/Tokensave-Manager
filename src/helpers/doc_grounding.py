"""Tokensave grounding block builder — Theme B1.

Runs ``tokensave tool context <query>`` subcommands and packages the output
as a markdown block that can be spliced into any DocType's user prompt.  The
model receives structured, citation-ready code-graph facts derived from the
local ``.tokensave/`` index rather than having to infer them from commit
messages alone.

Key design decisions:
- Returns ``""`` silently on any failure (missing exe, missing index,
  timeout, bad recipe key).  The doc-drafter dialog's generate flow always
  works — grounding is purely additive.
- Uses ``tokensave tool context`` (the only stable CLI pass-through for
  prose/JSON output).  The MCP-only subcommands (dsm, diff_context, etc.)
  are deferred to Theme B2 / Roadmap-8 agentic path.
- Output cap: ``_MAX_GROUNDING_CHARS`` (≈ 8 000 chars ≈ 2 000 tokens).
  Truncates at last complete line to avoid mid-sentence cuts.

Pure-function module (no Tkinter).  Safe to call from any thread.
"""

from __future__ import annotations

import json
import os
import subprocess

from constants import CREATE_NO_WINDOW


_MAX_GROUNDING_CHARS = 8_000

# Per-recipe query lists.  Each query is run as:
#   tokensave tool context --format json --max-nodes 8 <query>
# Queries containing ``{range}`` are skipped when no commit_range is given.
#
# Rationale for query choices:
#   commit_range_context  — "what changed" + "what does that affect": gives
#     the model function-level signal about scope, not just commit subjects.
#   architecture_overview — module coupling + public API: the two dimensions
#     an architecture maintainer needs when writing a module-reference update.
#   roadmap_evidence      — shipped + in-progress feature clusters: gives the
#     model facts to cite when promoting 🟡 → ✅ or flagging 💤 stale items.
#   module_deep_dive      — callers + callees: helps the memory-file author
#     describe a symbol's role in the system accurately.
_RECIPE_QUERIES: dict[str, list[str]] = {
    "commit_range_context": [
        "recently modified functions and helpers",
        "most active modules and their public exports",
    ],
    "architecture_overview": [
        "module dependencies coupling and import relationships",
        "public API surface exports and key helper names",
    ],
    "roadmap_evidence": [
        "recently shipped features and completed functionality",
        "in-progress work TODO items and partially implemented features",
    ],
    "module_deep_dive": [
        "symbol callers and callee relationships",
        "key classes functions and their roles in the system",
    ],
}


def build_grounding_block(
    project_path: str,
    recipe: str,
    commit_range: str | None = None,
    tokensave_exe: str = "",
) -> str:
    """Return a markdown grounding block for ``recipe``.

    Returns ``""`` silently when:
    - ``recipe`` is falsy or unknown
    - ``tokensave_exe`` is not a valid file
    - ``.tokensave/`` is absent in ``project_path``
    - all queries timeout or fail

    Args:
        project_path:  Absolute path to the project root.
        recipe:        Key from ``_RECIPE_QUERIES`` (e.g. "commit_range_context").
        commit_range:  Optional git range string (e.g. "HEAD~5..HEAD") for
                       ``{range}``-parameterised queries.
        tokensave_exe: Path to the tokensave binary.  If empty, attempts
                       auto-detection from the project's ``.tokensave/`` dir.
    """
    if not recipe:
        return ""

    queries = _RECIPE_QUERIES.get(recipe)
    if not queries:
        return ""

    exe = (tokensave_exe or "").strip()
    if not exe:
        exe = _auto_detect_exe(project_path)
    if not exe or not os.path.isfile(exe):
        return ""

    if not os.path.isdir(os.path.join(project_path, ".tokensave")):
        return ""

    parts: list[str] = []
    for query in queries:
        if "{range}" in query:
            if not commit_range:
                continue
            query = query.replace("{range}", commit_range)
        result = _run_context_query(exe, project_path, query)
        if result:
            parts.append(f"### {query}\n{result}")

    if not parts:
        return ""

    body = "\n\n".join(parts)
    body = _truncate_at_line(body, _MAX_GROUNDING_CHARS)

    return (
        "## Code-graph context (from tokensave — facts you can cite verbatim)\n\n"
        + body
        + "\n"
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_context_query(exe: str, project_path: str, query: str) -> str:
    """Run ``tokensave tool context`` and return trimmed text output."""
    try:
        r = subprocess.run(
            [exe, "tool", "context", "--format", "json",
             "--max-nodes", "8", query],
            cwd=project_path,
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""

    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return ""

    # Attempt to slim the JSON to key fields (same approach as agent_tools).
    try:
        parsed = json.loads(out)
        slimmed = _slim_context(parsed)
        text = _context_to_markdown(slimmed)
    except (json.JSONDecodeError, TypeError, KeyError):
        # Fallback: return raw text
        text = out

    return text


def _slim_context(ctx: dict) -> dict:
    """Keep only citation-useful fields from a tokensave context response."""
    keep_node_fields = {"name", "kind", "file_path", "start_line", "signature"}
    slimmed = {}
    if "nodes" in ctx:
        slimmed["nodes"] = [
            {k: v for k, v in node.items() if k in keep_node_fields}
            for node in (ctx["nodes"] or [])
        ]
    if "relationships" in ctx:
        slimmed["relationships"] = ctx["relationships"]
    if "summary" in ctx:
        slimmed["summary"] = ctx["summary"]
    return slimmed


def _context_to_markdown(ctx: dict) -> str:
    """Convert a slimmed context dict to a readable markdown snippet."""
    lines: list[str] = []

    if ctx.get("summary"):
        lines.append(ctx["summary"])
        lines.append("")

    for node in ctx.get("nodes") or []:
        name = node.get("name", "?")
        kind = node.get("kind", "")
        fpath = node.get("file_path", "")
        lineno = node.get("start_line", "")
        sig = node.get("signature", "")
        loc = f"{fpath}:{lineno}" if fpath and lineno else fpath
        sig_part = f" — `{sig}`" if sig else ""
        lines.append(f"- **{name}** ({kind}) `{loc}`{sig_part}")

    for rel in ctx.get("relationships") or []:
        lines.append(f"  → {rel}")

    return "\n".join(lines)


def _truncate_at_line(text: str, max_chars: int) -> str:
    """Truncate to ``max_chars`` at the last complete line boundary."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl]
    return truncated + "\n[... context truncated at output cap ...]"


def _auto_detect_exe(project_path: str) -> str:
    """Attempt to locate tokensave.exe alongside the project's binary root."""
    candidates = [
        # Sibling to the .tokensave dir (common layout when tokensave is
        # checked out next to the project)
        os.path.join(os.path.dirname(project_path), "tokensave.exe"),
        # Windows PATH
        "tokensave.exe",
        "tokensave",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""
