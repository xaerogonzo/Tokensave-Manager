"""Anti-monolith audit rules — the Doctor's checks, with no Tk in sight.

These were module-level functions in ``controllers/doctor_ctrl.py``, which
imports ``tkinter`` at module scope. That single import was the only reason
the audit could not run on a headless Linux CI runner, so the CI step that
would have enforced these caps sat commented out in ``helpers/ci_workflow.py``
while the rules ran on developer machines only.

Moved here verbatim. The controller and ``prepush_runner`` are now adapters
over this module; the caps, the exemption syntax, and every message string are
unchanged, so an audit run before and after the move produces byte-identical
output.

Per-directory overrides are the one addition — see ``resolve_caps``. Utility
scripts are not production modules, and the alternative to looser caps there
was the blanket skip that had been hiding ``scripts/`` from the audit
entirely.

Pure module — stdlib only (``ast``, ``os``, ``re``), safe from any thread.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, replace

_CAP_FILE_LINES         = 1500  # Doctor warning threshold (BASIC_INSTRUCTIONS aspires to 800)
_CAP_METHOD_LINES       = 100
_CAP_CLASS_METHODS      = 40
_CAP_COMPLEXITY         = 10
_CAP_LAYOUT_COMPLEXITY  = 3


@dataclass(frozen=True)
class Caps:
    """The four thresholds one file is judged against."""

    file_lines:    int = _CAP_FILE_LINES
    method_lines:  int = _CAP_METHOD_LINES
    class_methods: int = _CAP_CLASS_METHODS
    complexity:    int = _CAP_COMPLEXITY


DEFAULT_CAPS = Caps()


@dataclass(frozen=True)
class Violation:
    """One cap violation, with the position the AST already knew.

    These used to be plain formatted strings, which is why a Doctor finding
    could not be clicked to a line while a scout finding could: the auditors
    walk AST nodes and therefore always had ``node.lineno``, but threw it away
    at the point of formatting.

    **``__str__`` reproduces the old text exactly**, byte for byte, so every
    existing renderer — the Doctor tab, the Run Checks dialog, the pre-push
    hook and the generated CI workflow — keeps working untouched. That parity
    is a tested property, not an intention; see ``tests/test_doctor_rules.py``.

    ``file`` is empty until ``_audit_project_tree`` places it: the node-level
    auditors are given a node, not a path, and inventing one there would mean
    two sources of truth for which file a violation belongs to.
    """
    message: str
    symbol: str = ""
    line: int = 1
    file: str = ""

    def __str__(self) -> str:
        return f"  {self.file}: {self.message}" if self.file else self.message

# Keys accepted in a doctor_path_overrides entry, mapped onto Caps fields.
_OVERRIDE_KEYS = {
    "max_lines":         "file_lines",
    "max_method_lines":  "method_lines",
    "max_class_methods": "class_methods",
    "max_complexity":    "complexity",
}

SKIP = "skip"


def resolve_caps(rel_path: str,
                 overrides: "dict | None") -> "Caps | None":
    """Caps for *rel_path*, or None when it should be skipped entirely.

    ``overrides`` maps a directory prefix (or exact file path) to either the
    string ``"skip"`` or a dict of cap names::

        {"scripts": {"max_complexity": 20, "max_lines": 500},
         "dist":    "skip"}

    The LONGEST matching prefix wins, so a specific subdirectory can tighten
    or loosen what its parent set. Unknown keys are ignored rather than
    raising: this comes from hand-edited JSON, and one typo should not take
    the whole audit down.

    Why this exists: ``scripts/`` had been blanket-skipped because one-off
    utilities tripped production-grade caps and blocked pushes. Skipping is a
    blunt instrument — it hides real problems along with the noise — where a
    looser cap still audits the directory.
    """
    if not overrides or not isinstance(overrides, dict):
        return DEFAULT_CAPS
    rel = (rel_path or "").replace("\\", "/")

    best_key, best_val = None, None
    for key, val in overrides.items():
        k = str(key).replace("\\", "/").rstrip("/")
        if not k:
            continue
        if rel == k or rel.startswith(k + "/"):
            if best_key is None or len(k) > len(best_key):
                best_key, best_val = k, val
    if best_key is None:
        return DEFAULT_CAPS
    if isinstance(best_val, str) and best_val.strip().lower() == SKIP:
        return None
    if not isinstance(best_val, dict):
        return DEFAULT_CAPS

    fields = {}
    for name, field_name in _OVERRIDE_KEYS.items():
        raw = best_val.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue          # bools are ints in Python; reject them explicitly
        if raw > 0:
            fields[field_name] = raw
    return Caps(**{**DEFAULT_CAPS.__dict__, **fields})

# Layout-method name patterns — qualify for the 100-line carve-out IF cyclomatic
# complexity is also ≤ _CAP_LAYOUT_COMPLEXITY. Naming alone never grants immunity.
_LAYOUT_NAME_RE = re.compile(r"^_?(build|populate|render|layout)(_.*)?$")

# Top-of-file exemption: `# anti-monolith: exempt — <non-empty reason>` appearing
# in the file's comment block BEFORE the first class/def (robust to long docstrings,
# grouped imports, formatter reordering, __future__ blocks).
_EXEMPT_RE = re.compile(r"#\s*anti-monolith:\s*exempt\s*[—-]\s*(\S.*\S)")


def _parse_exempt_header(source: str) -> str | None:
    """Return the exemption rationale if present in the file's pre-class/def
    comment block. Returns None if no exemption (or if rationale is empty)."""
    for raw_line in source.splitlines():
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _EXEMPT_RE.search(stripped)
            if m:
                reason = m.group(1).strip()
                return reason if reason else None
            continue
        # Hit a non-comment, non-blank line — exemption can only appear before
        # the first class/def. Anything else (imports, docstrings, decorators,
        # module-level statements) ends the search window.
        return None
    return None


def _method_span_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Method body span — decorators do NOT count toward length.

    `node.lineno` points at the `def` line (after any decorators); `body[-1]`
    has the final statement's end_lineno. This matches the Rule A semantics.
    """
    if not node.body:
        return 1
    end = getattr(node.body[-1], "end_lineno", None) or node.lineno
    return end - node.lineno + 1


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Cyclomatic complexity per Rule A.

    Each if/elif/for/while/except adds 1; each and/or short-circuit adds 1;
    each match arm adds 1; comprehension `if` clauses add 1 each.
    Nested function/class definitions are NOT recursed into — they have
    their own scores.
    """
    score = 1  # base path

    def _visit(n: ast.AST, is_root: bool) -> None:
        nonlocal score
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not is_root:
            return  # nested scope — not our complexity
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            score += 1
        elif isinstance(n, ast.BoolOp):
            score += max(0, len(n.values) - 1)
        elif isinstance(n, ast.comprehension):
            score += len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            score += 1
        for child in ast.iter_child_nodes(n):
            _visit(child, is_root=False)

    _visit(node, is_root=True)
    return score


def _is_layout_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Layout-method carve-out: name pattern + complexity ≤ 3.

    Naming alone never grants immunity (Rule A).
    """
    if not _LAYOUT_NAME_RE.match(node.name):
        return False
    return _cyclomatic_complexity(node) <= _CAP_LAYOUT_COMPLEXITY


_AUDIT_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".tokensave", ".codegraph",
    ".venv", "venv", "node_modules", "dist", "build",
    ".build", ".onefile-build",
    # `.claude/worktrees/<name>/` holds full checkouts of this same repo, so
    # walking into it audits a second copy of every file and reports each
    # violation twice (442 files / 253 violations instead of ~221 / ~126).
    # Skipped as a whole for the same reason as .git and .tokensave: it is
    # agent-owned state, not project source.
    ".claude",
})

# Non-Python source / prose extensions that get a line-count-only audit.
# Data formats (.json, .xml, .yaml) are intentionally excluded — line count
# isn't meaningful for serialised data.
_AUDIT_TEXT_EXTS = frozenset({
    ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".lua",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".rs", ".go", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".swift", ".kt", ".php", ".sql",
    ".html", ".css", ".scss", ".md",
})


def _audit_project_tree(
    project_path: str,
    skip_rel_paths: set[str],
    overrides: "dict | None" = None,
) -> tuple[list[Violation], list[str], int]:
    """Walk audit-eligible files; return (violations, exempts, files_scanned).

    Python files (`*.py`) get the full AST audit (methods, classes,
    complexity). Non-Python source/prose files (see `_AUDIT_TEXT_EXTS`)
    get a line-count-only check against the same file cap.

    Violations are `Violation` records, not strings. Every existing consumer
    renders them with `str()` and gets exactly what it got before; a consumer
    that wants the position (the VS Code Problems panel) can now have it.
    """
    violations: list[Violation] = []
    exempt_notes: list[str] = []
    files_scanned = 0

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _AUDIT_SKIP_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, project_path).replace("\\", "/")
            # Support both exact-file matches ("scripts/foo.py") and
            # directory-prefix matches ("scripts" skips everything under it).
            if any(rel == sp or rel.startswith(sp.rstrip("/") + "/")
                   for sp in skip_rel_paths):
                continue
            caps = resolve_caps(rel, overrides)
            if caps is None:
                continue          # this path is configured "skip"
            if ext == ".py":
                result = _audit_python_file(full, caps)
            elif ext in _AUDIT_TEXT_EXTS:
                result = _audit_text_file(full, caps)
            else:
                continue
            files_scanned += 1
            if result is None:
                continue
            if result["exempt"]:
                exempt_notes.append(f"  (exempt: {rel} — {result['exempt_reason']})")
            else:
                # The node-level auditors know the symbol and the line but not
                # the path; this is the one place that knows `rel`, so it is
                # the one place that fills it in.
                violations.extend(replace(v, file=rel)
                                  for v in result["violations"])

    return violations, exempt_notes, files_scanned


def _audit_text_file(path: str, caps: Caps = DEFAULT_CAPS) -> dict | None:
    """Line-count-only audit for non-Python source / prose files.

    Honours the same `# anti-monolith: exempt — <reason>` rationale comment.
    The regex matches the bare phrase, so any comment syntax wrapping it
    works: `# ...` (sh/ps1/lua), `// ...` (js/c/rust), `<!-- ... -->` (md/html).
    Scans the first 20 lines for the exemption marker.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return None

    head = "\n".join(source.splitlines()[:20])
    m = _EXEMPT_RE.search(head)
    if m:
        reason = m.group(1).strip()
        if reason:
            return {"exempt": True, "exempt_reason": reason, "violations": []}

    line_count = source.count("\n") + 1
    if line_count > caps.file_lines:
        # Line 1: the file itself is the subject, so the top of it is where a
        # reader wants to land. There is no narrower position to offer.
        return {"exempt": False, "exempt_reason": None,
                "violations": [Violation(
                    f"file is {line_count} lines (cap {caps.file_lines})",
                    line=1)]}
    return {"exempt": False, "exempt_reason": None, "violations": []}


def _audit_method_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    caps: Caps = DEFAULT_CAPS,
) -> list[Violation]:
    """Return violations for a single method/function node.

    ``node.lineno`` is the line the ``def`` sits on, which is where a reader
    wants the cursor for both of these findings.
    """
    out: list[Violation] = []
    span = _method_span_lines(node)
    if span > caps.method_lines and not _is_layout_method(node):
        out.append(Violation(
            f"{node.name}() is {span} lines (cap {caps.method_lines})",
            symbol=node.name, line=node.lineno))
    cc = _cyclomatic_complexity(node)
    if cc > caps.complexity:
        out.append(Violation(
            f"{node.name}() complexity {cc} (cap {caps.complexity})",
            symbol=node.name, line=node.lineno))
    return out


def _audit_class_node(node: ast.ClassDef,
                      caps: Caps = DEFAULT_CAPS) -> list[Violation]:
    """Return violations for a class node."""
    method_count = sum(
        1 for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    if method_count > caps.class_methods:
        return [Violation(
            f"class {node.name} has {method_count} direct methods "
            f"(cap {caps.class_methods})",
            symbol=node.name, line=node.lineno)]
    return []


def _audit_python_file(path: str, caps: Caps = DEFAULT_CAPS) -> dict | None:
    """Audit a single .py file. Returns dict or None on parse failure.

    Returned dict:
        {"exempt": bool, "exempt_reason": str | None, "violations": list[str]}
    """
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return None

    reason = _parse_exempt_header(source)
    if reason:
        return {"exempt": True, "exempt_reason": reason, "violations": []}

    violations: list[Violation] = []
    line_count = source.count("\n") + 1
    if line_count > caps.file_lines:
        violations.append(Violation(
            f"file is {line_count} lines (cap {caps.file_lines})", line=1))

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"exempt": False, "exempt_reason": None, "violations": violations}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_audit_method_node(node, caps))
        elif isinstance(node, ast.ClassDef):
            violations.extend(_audit_class_node(node, caps))

    return {"exempt": False, "exempt_reason": None, "violations": violations}


def count_methods_in_class(file_path: str, class_name: str) -> int:
    """Return the AST-direct-children method count for a class.

    Public helper exported for verification scripts (e.g. god-class regression
    checks after extractions). Matches the same semantics the Doctor audit
    uses — never use `dir()` for this; it includes inherited attributes,
    descriptor magic, and runtime additions.
    """
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sum(
                1 for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    raise LookupError(f"class {class_name!r} not found in {file_path}")


# ── Shadow-link health (R9-SL3) ───────────────────────────────────────────

def audit_shadow_links(project_path: str) -> list:
    """Warn-only notes about shadow hardlinks. Returns [] when not in use.

    A hardlink outlives its source: delete or rename ``Blood.zsc`` and
    ``Blood.zsc.cpp`` remains, so the index keeps serving code with nothing
    behind it and git sees an untracked file the .gitignore pattern may no
    longer cover.

    Reports rather than fixes, and never counts a file it cannot attribute.
    A file matching the shadow naming pattern that the manager did not record
    creating might be the user's own work, and the difference between those
    two is not visible on disk — see ``helpers/shadow_links.py``.

    Silent for projects with no saved shadow map: they are not using the
    feature, and every project would otherwise gain a line about it.

    Imported lazily so this module's import surface stays exactly ``ast``,
    ``os`` and ``re`` for the CI one-liner in ``helpers/ci_workflow.py``.
    """
    try:
        from helpers.shadow_links import (
            SHADOW_CANDIDATE, SHADOW_STALE, SHADOW_SUSPICIOUS,
            load_shadow_config, scan_shadows,
        )
    except ImportError:
        return []

    config = load_shadow_config(project_path)
    if not config:
        return []

    counts = {}
    for finding in scan_shadows(project_path, config.ext_map,
                                config.generated):
        counts[finding.state] = counts.get(finding.state, 0) + 1

    notes = []
    if counts.get(SHADOW_STALE):
        notes.append(
            "  %d stale shadow link(s): the source file is gone but the "
            "hardlink remains, so the index still contains it. "
            "Right-click the project -> Shadow Links -> Clean up."
            % counts[SHADOW_STALE])
    if counts.get(SHADOW_SUSPICIOUS):
        notes.append(
            "  %d shadow file(s) share a name with a live source but are no "
            "longer the same file -- an editor that saves by replacing "
            "breaks the link. Re-generate to restore it."
            % counts[SHADOW_SUSPICIOUS])
    if counts.get(SHADOW_CANDIDATE):
        notes.append(
            "  %d file(s) match the shadow naming pattern with no source and "
            "no record of being created here. Origin cannot be proven, so "
            "nothing will touch them automatically."
            % counts[SHADOW_CANDIDATE])
    return notes


def audit_graph_trust(project_path: str) -> list:
    """Warn-only notes about how much of tokensave's call graph is real.

    Not a cap violation and deliberately not counted as one: this is a fact
    about the *index*, not about the code, and folding it into the violation
    count would move a number that other things are measured against. It
    must never block a push for the same reason -- the source is fine; the
    graph describing it is not.

    Says nothing when the graph is sound, so a healthy project does not gain
    a permanent line. But `unknown` and `insufficient` are NOT silent: an
    inspection that could not reach a population is a different fact from
    one that found nothing, and letting them share the quiet path is exactly
    how an unread graph starts reading as a clean bill of health.

    Imported lazily so this module's import surface stays exactly ``ast``,
    ``os`` and ``re`` for the CI one-liner in ``helpers/ci_workflow.py``.
    """
    try:
        from helpers.graph_trust import (
            STATE_INSUFFICIENT, STATE_TAINTED, STATE_UNKNOWN, inspect_graph,
        )
    except ImportError:
        return []

    report = inspect_graph(project_path)

    if report.state == STATE_UNKNOWN:
        if "no tokensave index" in report.detail:
            return []          # not a tokensave project; nothing to say
        return ["  Graph trust could not be established: %s. Health "
                "sub-scores that read call edges (acyclicity, and the "
                "quality_signal aggregate over it) are unverified."
                % report.detail]

    if report.state == STATE_INSUFFICIENT:
        return ["  Graph trust inconclusive: %s." % report.detail]

    if report.state != STATE_TAINTED:
        return []

    notes = [
        "  %d call edge(s) run from production code INTO the test tree, "
        "across %d source file(s) -- impossible by construction, since "
        "tests import production code and never the reverse. tokensave "
        "binds an unqualified call on an untracked receiver to the only "
        "symbol of that name in the project, and test doubles are named "
        "after what they stand in for."
        % (report.impossible_edges, report.source_files_affected),
        "  Examined %d edge(s) to find them."
        % report.edges_examined,
        "  Treat acyclicity -- and quality_signal, which is the geometric "
        "mean over it -- as unusable for trend comparison until upstream "
        "fixes this. See docs/upstream-issues/"
        "tokensave-python-bare-name-fallback.md.",
    ]
    if report.collisions:
        top = ", ".join("%s x%d" % (c.target_name, c.count)
                        for c in report.collisions[:5])
        notes.append("  Most-bound test-double names: %s." % top)
    return notes
