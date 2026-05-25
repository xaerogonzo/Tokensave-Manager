"""Refactor scout — deterministic code-health findings from the tokensave DB.

Reads `.tokensave/tokensave.db` directly with read-only SQL and produces a
list of `Finding` records. NO LLM call happens here — findings are grounded
in measurable thresholds from BASIC_INSTRUCTIONS.md so the dialog can't
hallucinate "consider using X pattern" style advice.

The LLM enters the picture only when the user clicks **Investigate** in
the RefactorScoutDialog — that pre-seeds the Ask tab with the finding's
file/symbol/evidence so the agent has structured context to explain WHY
the finding fired, not to invent it.

Thresholds (canonical per BASIC_INSTRUCTIONS.md, frozen here):
- Cyclomatic complexity:  CC > 10        (CC = branches + loops + 1)
- God class:              > 40 direct methods
- God file:               > 1500 lines
- Dead code:              no incoming call edge AND not an entry-point pattern

Stable finding IDs: `md5(kind + file + symbol)`. A rename or extraction will
change the hash and the suppression "moves with the bug" — documented in the
dialog so the user doesn't expect permanent identity guarantees.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field

# ── Thresholds (canonical) ────────────────────────────────────────────────
CC_THRESHOLD = 10           # CC > 10 fires the finding
GOD_CLASS_METHODS = 40      # > 40 direct methods on a class
GOD_FILE_LINES = 1500       # > 1500 LOC in one file (Doctor warning level)

# Per-kind result caps so the dialog and the Investigate prompt don't get
# saturated. The dialog header still reports the suppressed count.
TOP_N_PER_KIND = 15

# Evidence snippet cap (lines). The dialog renders a "show more" if the
# symbol body is longer.
EVIDENCE_MAX_LINES = 10


# ── Entry-point heuristics for dead-code filtering ────────────────────────
#
# Tokensave's call-graph extractor (and Python's dynamic dispatch generally)
# can miss Tk widget callbacks, conventional test discovery, and the manager's
# right-click cmd_* convention. We use a conservative allowlist of names that
# should NEVER be flagged as dead, even when no caller edges exist. False
# negatives (a truly-dead `cmd_*` slipping through) are better than false
# positives that erode trust in the scout.
_ENTRYPOINT_NAME_RE = re.compile(
    r"^(?:"
    r"__\w+__"                           # dunder methods (__init__, __str__, …)
    r"|test_\w+"                         # pytest/unittest tests
    r"|_?on_\w+|_?cmd_\w+"               # Tk callbacks + manager cmd_* convention
    r"|_build_\w*|_render_\w*"           # layout builders (always Tk-dispatched)
    r"|_populate_\w*|_layout_\w*|_make_\w+"
    r"|main|run|setup|teardown"
    r")$"
)
_ENTRYPOINT_FILE_PATTERNS = (
    "tests/", "test_", "_test.", "conftest.py", "setup.py", "__main__.py",
)


@dataclass
class Finding:
    """One actionable scout result.

    `evidence` is the literal slice of source that triggered the finding
    (e.g., the function body for a complexity hit, the class signature for
    a god-class hit). The dialog renders it verbatim — never paraphrased.
    """
    id: str                  # stable hash for suppression
    kind: str                # "complexity" | "god_class" | "god_file" | "dead_code"
    file: str                # repo-relative path
    symbol: str              # function/class/file basename
    line: int                # 1-indexed start line in `file`
    message: str             # human-readable summary
    evidence: str = ""       # verbatim source snippet (capped EVIDENCE_MAX_LINES)
    metric: str = ""         # numeric detail ("CC=14", "methods=47", "lines=1812")
    metadata: dict = field(default_factory=dict)


def _finding_id(kind: str, file: str, symbol: str) -> str:
    """Stable ID: md5 of `kind|file|symbol`. Changes on rename or move."""
    raw = f"{kind}|{file}|{symbol}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


def _read_evidence(project_path: str, file_rel: str, start: int,
                    end: int) -> str:
    """Read up to EVIDENCE_MAX_LINES from the source around `start..end`.

    Returns "" if the file isn't readable (deleted since indexing, perms
    error, binary). Callers should treat that as a soft signal — the
    finding is still real, the dialog just shows no snippet."""
    full = os.path.join(project_path, file_rel.replace("/", os.sep))
    if not os.path.isfile(full):
        return ""
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    if not lines:
        return ""
    s = max(0, start - 1)
    e = min(len(lines), s + EVIDENCE_MAX_LINES)
    if end and end > start:
        e = min(len(lines), s + min(EVIDENCE_MAX_LINES, end - start + 1))
    return "".join(lines[s:e]).rstrip()


def _is_entrypoint(name: str, file_rel: str) -> bool:
    """Conservative allowlist — see module docstring for the rationale."""
    if _ENTRYPOINT_NAME_RE.match(name or ""):
        return True
    return any(p in file_rel for p in _ENTRYPOINT_FILE_PATTERNS)


def _scout_complexity(con: sqlite3.Connection, project_path: str
                       ) -> list[Finding]:
    """Functions / methods whose cyclomatic complexity exceeds the cap.

    CC = branches + loops + 1 per the formula frozen in BASIC_INSTRUCTIONS.md.
    """
    out: list[Finding] = []
    rows = con.execute(
        """
        SELECT name, qualified_name, file_path, start_line, end_line,
               branches, loops, kind
          FROM nodes
         WHERE kind IN ('function', 'method')
           AND (branches + loops + 1) > ?
         ORDER BY (branches + loops + 1) DESC
         LIMIT ?
        """,
        (CC_THRESHOLD, TOP_N_PER_KIND),
    ).fetchall()
    for name, qname, file_rel, start, end, branches, loops, _kind in rows:
        cc = (branches or 0) + (loops or 0) + 1
        symbol = qname or name or "(unknown)"
        out.append(Finding(
            id=_finding_id("complexity", file_rel, symbol),
            kind="complexity",
            file=file_rel,
            symbol=symbol,
            line=start or 1,
            message=f"Cyclomatic complexity {cc} (cap: {CC_THRESHOLD})",
            metric=f"CC={cc}",
            evidence=_read_evidence(project_path, file_rel, start or 1,
                                     end or 0),
            metadata={"cc": cc, "branches": branches, "loops": loops},
        ))
    return out


def _scout_god_class(con: sqlite3.Connection, project_path: str
                      ) -> list[Finding]:
    """Classes with more direct methods than the cap.

    Uses parent_id linkage: count `function`/`method` children per `class`.
    """
    out: list[Finding] = []
    rows = con.execute(
        """
        SELECT cls.name, cls.qualified_name, cls.file_path, cls.start_line,
               cls.end_line, COUNT(child.id) AS m
          FROM nodes AS cls
          JOIN nodes AS child ON child.parent_id = cls.id
         WHERE cls.kind IN ('class', 'struct', 'interface')
           AND child.kind IN ('function', 'method')
         GROUP BY cls.id
        HAVING m > ?
         ORDER BY m DESC
         LIMIT ?
        """,
        (GOD_CLASS_METHODS, TOP_N_PER_KIND),
    ).fetchall()
    for name, qname, file_rel, start, end, m in rows:
        symbol = qname or name or "(unknown)"
        out.append(Finding(
            id=_finding_id("god_class", file_rel, symbol),
            kind="god_class",
            file=file_rel,
            symbol=symbol,
            line=start or 1,
            message=f"{m} direct methods (cap: {GOD_CLASS_METHODS})",
            metric=f"methods={m}",
            evidence=_read_evidence(project_path, file_rel, start or 1,
                                     min((start or 1) + EVIDENCE_MAX_LINES,
                                         end or 0)),
            metadata={"methods": m},
        ))
    return out


def _scout_god_file(con: sqlite3.Connection, project_path: str
                     ) -> list[Finding]:
    """Files whose total LOC exceeds the Doctor warning threshold."""
    out: list[Finding] = []
    rows = con.execute(
        """
        SELECT files.path,
               (SELECT MAX(end_line) FROM nodes WHERE nodes.file_path = files.path) AS loc
          FROM files
         WHERE loc > ?
         ORDER BY loc DESC
         LIMIT ?
        """,
        (GOD_FILE_LINES, TOP_N_PER_KIND),
    ).fetchall()
    for file_rel, loc in rows:
        symbol = os.path.basename(file_rel)
        # Evidence for files: show the first EVIDENCE_MAX_LINES so the user
        # can see at least the module docstring / top-level layout.
        out.append(Finding(
            id=_finding_id("god_file", file_rel, symbol),
            kind="god_file",
            file=file_rel,
            symbol=symbol,
            line=1,
            message=f"{loc} lines (Doctor warns at {GOD_FILE_LINES})",
            metric=f"lines={loc}",
            evidence=_read_evidence(project_path, file_rel, 1,
                                     EVIDENCE_MAX_LINES),
            metadata={"lines": loc},
        ))
    return out


def _scout_dead_code(con: sqlite3.Connection, project_path: str
                      ) -> list[Finding]:
    """Functions/methods with no incoming call edges and not an entry-point.

    Conservative: applies _is_entrypoint() to drop dunder methods, Tk
    callback conventions, layout builders, tests, and the manager's
    `cmd_*` convention. Dead-code findings still carry an Ignore button
    because tokensave's call-graph misses dynamic dispatch — the user has
    the final word.
    """
    out: list[Finding] = []
    rows = con.execute(
        """
        SELECT name, qualified_name, file_path, start_line, end_line
          FROM nodes
         WHERE kind IN ('function', 'method')
           AND visibility != 'private_module'
           AND id NOT IN (SELECT target FROM edges WHERE kind = 'call')
         ORDER BY file_path, start_line
        """,
    ).fetchall()
    for name, qname, file_rel, start, end in rows:
        if _is_entrypoint(name or "", file_rel or ""):
            continue
        symbol = qname or name or "(unknown)"
        out.append(Finding(
            id=_finding_id("dead_code", file_rel, symbol),
            kind="dead_code",
            file=file_rel,
            symbol=symbol,
            line=start or 1,
            message="No callers found in tokensave's call graph",
            metric="callers=0",
            evidence=_read_evidence(project_path, file_rel, start or 1,
                                     end or 0),
            metadata={},
        ))
        if len(out) >= TOP_N_PER_KIND:
            break
    return out


_KIND_ORDER = ("complexity", "god_class", "god_file", "dead_code")
_KIND_LABELS = {
    "complexity": "High cyclomatic complexity",
    "god_class":  "God class (too many methods)",
    "god_file":   "Oversized file",
    "dead_code":  "Possibly unused (no callers in call graph)",
}


def kind_label(kind: str) -> str:
    """Human label for a finding kind — used by the dialog."""
    return _KIND_LABELS.get(kind, kind)


def run_scout(project_path: str,
              ignored_ids: set[str] | None = None
              ) -> tuple[dict[str, list[Finding]], int]:
    """Run all scout checks. Returns ({kind: [findings...]}, suppressed_count).

    `ignored_ids` filters out user-suppressed findings; their count is still
    returned as the second tuple element so the dialog can show
    "X findings (Y suppressed)".

    Raises FileNotFoundError if `.tokensave/tokensave.db` doesn't exist —
    the caller should surface a "run `tokensave init` first" message.
    """
    db_path = os.path.join(project_path, ".tokensave", "tokensave.db")
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"No tokensave index at {db_path} — run `tokensave init` first."
        )

    ignored = ignored_ids or set()
    # Read-only connection. uri=True lets us pass query-string flags.
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        raw = {
            "complexity": _scout_complexity(con, project_path),
            "god_class":  _scout_god_class(con, project_path),
            "god_file":   _scout_god_file(con, project_path),
            "dead_code":  _scout_dead_code(con, project_path),
        }
    finally:
        con.close()

    suppressed = 0
    filtered: dict[str, list[Finding]] = {}
    for kind in _KIND_ORDER:
        keep = []
        for f in raw[kind]:
            if f.id in ignored:
                suppressed += 1
                continue
            keep.append(f)
        filtered[kind] = keep
    return filtered, suppressed


def format_investigate_context(finding: Finding) -> str:
    """Format a Finding into a structured Ask-tab seed prompt.

    The prompt explicitly bounds the question (don't re-run the analytics)
    and provides the file/symbol/evidence verbatim so the LLM doesn't
    have to spend tool calls rediscovering what the scout already knows.
    """
    evidence = finding.evidence or "(snippet unavailable — file may have moved)"
    return (
        f"Refactor scout flagged {kind_label(finding.kind)}:\n\n"
        f"- File: {finding.file}\n"
        f"- Symbol: {finding.symbol}\n"
        f"- Line: {finding.line}\n"
        f"- Metric: {finding.metric}\n"
        f"- Detail: {finding.message}\n\n"
        f"Evidence (verbatim from source):\n"
        f"```\n{evidence}\n```\n\n"
        f"Please explain WHY this fired and suggest a concrete, scoped "
        f"refactor. Do NOT re-run the analytics tools (tokensave_*); the "
        f"scout already gathered the evidence above. Focus on the named "
        f"symbol only — do not propose architecture-wide rewrites."
    )


def write_finding_briefing(finding: Finding, dest_dir: str | None = None) -> str:
    """Write a Finding to a temp `.md` file for the Claude CLI to read.

    The CLI launcher (`helpers/claude_cli.spawn_claude_cli`) strips newlines
    from its instruction arg because `cmd.exe /k` would interpret them as
    Enter — so we can't hand it the multi-line prompt directly. Instead we
    drop the briefing to disk and tell Claude to read it; Claude's Read
    tool handles multi-line + code blocks natively.

    Returns the absolute path of the file written. Caller is responsible
    for cleanup (or leaving it for the OS temp sweeper — files are small
    and unique-named).

    `dest_dir` defaults to the OS temp dir. A custom dir is useful for the
    "export all" variant so the user can find the report later.
    """
    import tempfile
    body = format_investigate_context(finding)
    suffix = f"-{finding.kind}-{finding.id}.md"
    fd, path = tempfile.mkstemp(prefix="tokensave-scout", suffix=suffix,
                                 dir=dest_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"# Refactor scout finding\n\n{body}\n")
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def format_batch_briefing(findings: list[Finding],
                           project_path: str) -> str:
    """Format a user-picked subset of findings into one markdown briefing.

    Used by the batch actions (clipboard, CLI, Ask tab) — gives the LLM
    a coherent multi-finding view it can prioritise across, instead of
    treating each finding as an isolated puzzle. Grouped by kind so
    related items (god class + complexity in the same file) cluster.

    The instruction footer explicitly tells the model NOT to re-run
    analytics — the briefing IS the analytics output — and to propose
    refactors per-symbol rather than architecture-wide rewrites.
    """
    if not findings:
        return ""

    # Group by kind, preserve _KIND_ORDER for consistent reading order.
    grouped: dict[str, list[Finding]] = {k: [] for k in _KIND_ORDER}
    for f in findings:
        grouped.setdefault(f.kind, []).append(f)

    lines: list[str] = [
        f"# Refactor scout — batch ({len(findings)} findings)",
        "",
        f"_Project: `{os.path.basename(project_path)}`. All findings below "
        f"come from deterministic SQL queries against the tokensave index, "
        f"grounded in canonical thresholds from BASIC_INSTRUCTIONS.md._",
        "",
    ]
    for kind in _KIND_ORDER:
        items = grouped.get(kind) or []
        if not items:
            continue
        lines.append(f"\n## {kind_label(kind)} ({len(items)})\n")
        for f in items:
            ev = f.evidence or "(snippet unavailable)"
            lines.append(f"### `{f.symbol}`  —  {f.metric}\n")
            lines.append(f"- **File:** {f.file}:{f.line}")
            lines.append(f"- **Detail:** {f.message}\n")
            lines.append(f"```\n{ev}\n```\n")

    lines.append("\n---\n")
    lines.append(
        "Please review these findings and propose a refactoring plan. "
        "Group related findings (a god class may also be a complexity "
        "hotspot in the same file). Suggest the order to tackle them in. "
        "Do NOT re-run analytics tools — the briefing above IS the analytics "
        "output. Focus on the named symbols only; do not propose "
        "architecture-wide rewrites."
    )
    return "\n".join(lines) + "\n"


def write_batch_briefing(findings: list[Finding],
                          project_path: str,
                          dest_dir: str | None = None) -> str:
    """Write the batch briefing to a temp `.md` file. Returns the path."""
    import tempfile
    body = format_batch_briefing(findings, project_path)
    fd, path = tempfile.mkstemp(prefix="tokensave-scout-batch-",
                                 suffix=".md", dir=dest_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def write_full_report(findings: dict[str, list[Finding]],
                      project_path: str,
                      suppressed_count: int = 0,
                      dest_dir: str | None = None) -> str:
    """Write every finding into one combined markdown briefing.

    Used by the "Export all to Claude Code" footer button — gives Claude
    the full project-health picture so it can prioritise across kinds
    rather than reasoning about one finding in isolation.
    """
    import tempfile

    total = sum(len(v) for v in findings.values())
    lines: list[str] = [
        f"# Refactor scout report — {os.path.basename(project_path)}",
        "",
        f"_{total} findings ({suppressed_count} suppressed). "
        f"All findings are deterministic — produced by SQL queries against "
        f"the tokensave index, grounded in canonical thresholds from "
        f"BASIC_INSTRUCTIONS.md._",
        "",
        "Please review the findings below and propose a refactoring plan, "
        "prioritised by impact. Group related findings (e.g. a god class "
        "may also be a complexity hotspot). Do NOT re-run analytics tools "
        "— the data below IS the analytics output.",
        "",
    ]
    for kind, items in findings.items():
        if not items:
            continue
        lines.append(f"\n## {kind_label(kind)} ({len(items)})\n")
        for f in items:
            ev = f.evidence or "(snippet unavailable)"
            lines.append(f"### `{f.symbol}`  —  {f.metric}\n")
            lines.append(f"- **File:** {f.file}:{f.line}")
            lines.append(f"- **Detail:** {f.message}\n")
            lines.append(f"```\n{ev}\n```\n")

    fd, path = tempfile.mkstemp(prefix="tokensave-scout-report-",
                                 suffix=".md", dir=dest_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path
