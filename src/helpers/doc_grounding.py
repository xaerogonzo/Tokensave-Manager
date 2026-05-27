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
import re
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


# ─────────────────────────────────────────────────────────────────────────────
# v4.1 — Codegraph grounding (parallel to tokensave grounding above).
# ─────────────────────────────────────────────────────────────────────────────
#
# Codegraph (npm package, ships separately from tokensave) provides:
#   * `codegraph context <task>` — markdown structured-output, mirrors
#     tokensave's `tool context`. Returns Code Context / Entry Points /
#     Related Symbols. Used as the structural grounding source for
#     architecture/roadmap/memory tabs alongside tokensave's call-graph
#     focus.
#   * `codegraph affected --stdin` — UNIQUE capability: piped a list of
#     changed source files, returns the test files those changes affect.
#     Goldmine for roadmap_evidence (test impact is one of the strongest
#     "this actually shipped" signals).
#
# Health gate: codegraph has no auto-sync daemon (unlike tokensave). If
# the user hasn't manually run `codegraph index --force` recently, the
# index may reflect an obsolete file layout. Live observation on this
# project (2026-05-27): 2 files indexed pointing at a pre-modular
# `src/tokensave-manager.py` that no longer exists. Feeding that into
# the drafter would actively mislead — so the health gate refuses to
# inject the block unless the index passes basic sanity checks.

_CODEGRAPH_RECIPE_QUERIES: dict = {
    "commit_range_context": [
        "recently modified functions and their callers",
    ],
    "architecture_overview": [
        "module dependencies and import structure",
        "public API surface and exported helpers",
    ],
    "roadmap_evidence": [
        "shipped features and completed work",
    ],
    "module_deep_dive": [
        "symbol callers and callee relationships",
    ],
}

_MAX_CODEGRAPH_CHARS = 4000   # half the tokensave cap; see _build_combined_grounding
_CODEGRAPH_STALE_TOLERANCE_S = 200   # v4.3: bumped 60→200 s; covers edit-then-commit rhythm


def build_codegraph_block(
    project_path: str,
    recipe: str,
    commit_range: str | None = None,
    changed_files: "list[str] | None" = None,
    codegraph_exe: str = "",
) -> str:
    """Return a markdown grounding block from codegraph queries.

    Silently returns "" when codegraph isn't installed, `.codegraph/` is
    absent, or the index health gate fails (under-indexed / stale). The
    drafter falls back to tokensave-only grounding in those cases — same
    fail-open contract as ``build_grounding_block``.

    Bonus capability for `roadmap_evidence`: when `changed_files` is
    provided, also runs `codegraph affected --stdin` on those paths and
    appends the test-impact block. Tokensave has no equivalent — this
    is the most unique value-add the integration provides.
    """
    if not recipe:
        return ""

    queries = _CODEGRAPH_RECIPE_QUERIES.get(recipe)
    if not queries:
        return ""

    exe = (codegraph_exe or "").strip()
    if not exe or not os.path.isfile(exe):
        return ""

    health, detail = _codegraph_index_health(project_path, exe)
    if health != "healthy":
        # Skip silently. The dialog's worker is expected to surface a
        # one-time-per-session diagnostic to stderr.
        import sys
        print(f"[doc-drafter] codegraph grounding skipped: "
              f"{health} ({detail})", file=sys.stderr, flush=True)
        return ""

    parts: list = []
    for query in queries:
        text = _run_codegraph_context(exe, project_path, query)
        if text:
            parts.append(f"### {query}\n{text}")

    # Bonus: codegraph affected for roadmap_evidence
    if recipe == "roadmap_evidence" and changed_files:
        affected = _run_codegraph_affected(exe, project_path, changed_files)
        if affected:
            parts.append(f"### Tests affected by this commit range\n{affected}")

    if not parts:
        return ""

    body = "\n\n".join(parts)
    body = _truncate_at_line(body, _MAX_CODEGRAPH_CHARS)

    return (
        "## Codegraph context (from codegraph CLI — structural & test-impact facts)\n\n"
        + body
        + "\n"
    )


def _codegraph_index_health(project_path: str, codegraph_exe: str):
    """Return ``(status, detail)`` tuple. Status is one of:

      * ``'missing'`` — no .codegraph/ directory (codegraph not enabled)
      * ``'broken'`` — DB exists but file count is suspiciously low
        (under-indexed or pointing at obsolete layout)
      * ``'stale'`` — file count plausible but DB mtime drifted behind
        source files by more than ``_CODEGRAPH_STALE_TOLERANCE_S``
      * ``'healthy'`` — passes all gates
    """
    cg_dir = os.path.join(project_path, ".codegraph")
    if not os.path.isdir(cg_dir):
        return "missing", "no .codegraph/ directory"
    db_path = os.path.join(cg_dir, "codegraph.db")
    if not os.path.isfile(db_path):
        return "missing", "no codegraph.db file"

    # Parse `codegraph status` for file count. ANSI escape codes are
    # interspersed in the output but the "Files: N" pair is clean.
    try:
        r = subprocess.run(
            [codegraph_exe, "status", project_path],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "broken", "could not run codegraph status"
    if r.returncode != 0:
        return "broken", f"codegraph status exited {r.returncode}"

    m = re.search(r"Files:\s*(\d+)", r.stdout or "")
    if not m:
        return "broken", "could not parse file count from status"
    file_count = int(m.group(1))

    # Cheap broken-state detection: absolute minimum threshold.
    if file_count < 5:
        return "broken", f"only {file_count} files indexed"

    # Cross-tool sanity: if tokensave is also indexed on this project,
    # codegraph should be in the same order of magnitude. < 30% suggests
    # codegraph is severely behind.
    ts_count = _count_tokensave_files(project_path)
    if ts_count and file_count < ts_count * 0.3:
        return "broken", (
            f"only {file_count} files indexed vs tokensave's {ts_count} "
            f"(< 30%); codegraph likely on an obsolete layout"
        )

    # Staleness: DB mtime drift vs most-recent source file.
    try:
        db_mtime = os.path.getmtime(db_path)
        newest_src_mtime = _newest_source_file_mtime(project_path)
        if newest_src_mtime > db_mtime + _CODEGRAPH_STALE_TOLERANCE_S:
            drift = int(newest_src_mtime - db_mtime)
            return "stale", f"index older than newest source by {drift}s"
    except OSError:
        pass   # fail-open

    return "healthy", f"{file_count} files indexed"


def build_combined_grounding(tokensave_block: str, codegraph_block: str,
                              per_source_cap: int = 4000) -> str:
    """Combine tokensave + codegraph grounding into one block.

    v4.4 (Gemini #4): **dedup first, truncate after.** The previous
    v4.1 order (truncate-then-dedup) would chop the bottom 60% of
    both blocks to meet a per-source cap, then dedup the surviving
    overlap — losing unique tail content to preserve duplicated head
    content. Dedup-first keeps every byte under the combined cap
    unique.

    Combined cap is ``per_source_cap * 2`` (default 8000), matching
    the v3 single-source ceiling — same prompt-size budget, just
    every byte is now meaningful.

    Returns "" when both inputs are empty (caller passes through to a
    bodiless prompt — same as v3 behaviour without grounding).
    """
    tk_block = tokensave_block or ""
    cg_block = codegraph_block or ""
    if not tk_block and not cg_block:
        return ""
    # 1. Line-level dedup across both blocks, preserve first-seen order.
    seen: set = set()
    merged: list = []
    for block in (tk_block, cg_block):
        for line in block.splitlines():
            key = line.strip()
            if not key:
                merged.append(line)   # preserve blanks for readability
                continue
            if key in seen:
                continue
            seen.add(key)
            merged.append(line)
    # 2. Single truncation pass on the dedup'd content at the combined cap.
    combined = "\n".join(merged)
    return _truncate_at_line(combined, per_source_cap * 2)


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


# ── v4.1 codegraph helpers ────────────────────────────────────────────────────

def _run_codegraph_context(exe: str, project_path: str, query: str) -> str:
    """Run ``codegraph context`` and return trimmed markdown output."""
    try:
        r = subprocess.run(
            [exe, "context", query, "--max-nodes", "8",
             "--format", "markdown", "--path", project_path],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return ""
    # Strip codegraph's H1 "## Code Context" line — we'll wrap the whole
    # result under our own "## Codegraph context" heading.
    out = re.sub(r"^##\s+Code Context\s*\n", "", out, count=1)
    return out


def _run_codegraph_affected(exe: str, project_path: str,
                              changed_files: "list[str]") -> str:
    """Run ``codegraph affected --stdin`` with changed_files piped in.

    Returns a markdown-friendly summary of test files impacted by the
    given source changes. Unique to codegraph — tokensave has no
    equivalent.
    """
    if not changed_files:
        return ""
    try:
        r = subprocess.run(
            [exe, "affected", "--stdin", "--quiet", "--path", project_path],
            input="\n".join(changed_files),
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return ""
    # Format as a small markdown list. --quiet emits one path per line.
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return ""
    bullets = "\n".join(f"- `{p}`" for p in lines[:25])
    return bullets


def _count_tokensave_files(project_path: str) -> int:
    """Return the file count from the tokensave index, or 0 if unavailable.

    Used by the codegraph health check to detect cross-tool divergence
    (codegraph indexed < 30% of what tokensave sees → likely stale).
    Cheap — reads the SQLite file count via the tokensave CLI status.
    """
    ts_dir = os.path.join(project_path, ".tokensave")
    if not os.path.isdir(ts_dir):
        return 0
    # Glob common tokensave-binary names; fall back to PATH.
    candidates = [
        os.path.join(os.path.dirname(project_path), "tokensave.exe"),
        "tokensave.exe",
        "tokensave",
    ]
    exe = next((c for c in candidates if os.path.isfile(c)), "")
    if not exe:
        return 0
    try:
        r = subprocess.run(
            [exe, "status"],
            cwd=project_path,
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    if r.returncode != 0:
        return 0
    # tokensave status output includes "Files: N" or "File count: N".
    m = re.search(r"(?:[Ff]ile.*[Cc]ount|Files?)\s*:?\s*(\d+)", r.stdout or "")
    return int(m.group(1)) if m else 0


def _newest_source_file_mtime(project_path: str) -> float:
    """Walk the project tree (skipping VCS/index/cache dirs) and return
    the most recent .py/.js/.ts/.go/.rs file mtime.

    Used by the staleness check — if codegraph's DB is older than the
    newest source file by more than _CODEGRAPH_STALE_TOLERANCE_S, the
    index can't reflect recent changes.
    """
    skip_dirs = {
        ".codegraph", ".tokensave", ".git", ".venv", "venv",
        "node_modules", "__pycache__", "build", "dist", ".mypy_cache",
        ".pytest_cache", ".cache",
    }
    src_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                ".java", ".rb", ".php")
    newest = 0.0
    try:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                if fname.endswith(src_exts):
                    try:
                        mt = os.path.getmtime(os.path.join(root, fname))
                    except OSError:
                        continue
                    if mt > newest:
                        newest = mt
    except OSError:
        return 0.0
    return newest
