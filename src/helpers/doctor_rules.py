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
        "  %d edge(s) run from production code INTO the test tree, across "
        "%d source file(s) -- impossible by construction, since tests import "
        "production code and never the reverse. tokensave binds an "
        "unqualified name on an untracked receiver to the only symbol of "
        "that name in the project, and test doubles are named after what "
        "they stand in for."
        % (report.impossible_edges, report.source_files_affected),
        "  Examined %d edge(s) to find them."
        % report.edges_examined,
    ]

    # Per kind, never only the sum. The two contaminated kinds answer to
    # different upstream defects and move independently: 7.11.1 (#508) fixed
    # the call fallback and left the reference fallback alone, so a single
    # total renders a 94% clearance in one as a half-fix of the whole.
    # Clean kinds are named too -- "0 of 531 examined" is a result, and
    # omitting it leaves a reader unable to tell clean from unexamined.
    if report.by_kind:
        dirty = ", ".join("%s %d/%d" % (t.kind, t.impossible, t.examined)
                          for t in report.by_kind if t.impossible)
        clean = ", ".join("%s 0/%d" % (t.kind, t.examined)
                          for t in report.by_kind if not t.impossible)
        if dirty:
            notes.append("  By edge kind (impossible/examined): %s." % dirty)
        if clean:
            notes.append("  Clean kinds, measured not assumed: %s." % clean)

    calls = report.kind("calls")
    if calls is not None and calls.impossible:
        notes.append(
            "  %d of those are CALL edges -- upstream #503/#508. Treat "
            "acyclicity, and quality_signal which is the geometric mean "
            "over it, as unusable for trend comparison. See docs/"
            "upstream-issues/tokensave-python-bare-name-fallback.md."
            % calls.impossible)
    uses = report.kind("uses")
    if uses is not None and uses.impossible:
        notes.append(
            "  %d are USES edges -- the same bare-name binding in the "
            "reference resolver, which #508 did not reach. See docs/"
            "upstream-issues/tokensave-python-uses-bare-name.md."
            % uses.impossible)
    if report.collisions:
        top = ", ".join("%s x%d" % (c.target_name, c.count)
                        for c in report.collisions[:5])
        notes.append("  Most-bound test-double names: %s." % top)
    return notes


def _indexed_extractor_version(project_path: str) -> "tuple[str, str]":
    """What built this index, as ``(version, problem)`` -- exactly one set.

    ``.tokensave/config.json`` carries ``last_indexed_version``, and it means
    *the version that last performed a FULL index*. Measured, not assumed: a
    plain ``tokensave sync`` over a changed file leaves the field alone, so
    it cannot go green while the graph still holds an older extractor's
    edges. That is the whole reason this rule can be one string comparison
    rather than a comparison plus a timestamp.
    """
    import json

    cfg = os.path.join(project_path, ".tokensave", "config.json")
    if not os.path.isfile(cfg):
        return ("", "")           # not a tokensave project; nothing to say
    try:
        with open(cfg, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return ("", "cannot read .tokensave/config.json (%s)" % exc)
    if not isinstance(data, dict):
        return ("", ".tokensave/config.json is not a JSON object")
    version = str(data.get("last_indexed_version") or "").strip()
    if not version:
        return ("", "no last_indexed_version in .tokensave/config.json")
    return (version, "")


def _installed_extractor_version(tokensave_exe: str) -> "tuple[str, str]":
    """What is installed now, as ``(version, problem)`` -- exactly one set.

    ``tokensave --version`` prints ``tokensave X.Y.Z``. Subprocess and the
    Windows console-suppression flag are imported lazily, so this module's
    module-level import surface stays exactly ``ast``, ``os`` and ``re`` for
    the CI one-liner in ``helpers/ci_workflow.py``.
    """
    import subprocess

    from constants import CREATE_NO_WINDOW

    if not tokensave_exe or not os.path.isfile(tokensave_exe):
        return ("", "")           # not configured; the caller says nothing
    try:
        out = subprocess.run(
            [tokensave_exe, "--version"], capture_output=True, text=True,
            timeout=15, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ("", "could not run `tokensave --version` (%s)" % exc)
    text = ((out.stdout or "") + " " + (out.stderr or "")).strip()
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    if not m:
        return ("", "`tokensave --version` printed no version (%r)"
                % text[:60])
    return (m.group(1), "")


def audit_index_extractor_version(project_path: str,
                                  tokensave_exe: str = "") -> list:
    """Whether this project's graph was built by the tokensave now installed.

    The gap this exists to close: upgrading tokensave does not rebuild the
    graph, and an incremental sync does not revisit call sites it has already
    resolved. So a correctness fix in a new extractor -- 7.11.1's #508 is the
    worked example -- lands on disk and changes nothing anyone queries, for
    as long as nobody happens to run a full sync. On this repository that gap
    ran 20 days and hid 426 impossible edges that one `sync --force` removed.

    Nothing else in the Manager compares the binary that IS installed against
    the binary that BUILT the graph, and the integration check cannot: it
    reads local files and the graph is not one of them.

    Warn-only, and never a violation -- a stale index is a fact about the
    working tree, not a defect in the source, and counting it would move the
    number other things are measured against.

    Silent when the two agree. NOT silent when it could not ask: an
    unreadable config or an unrunnable binary is a different fact from a
    match, and letting them share the quiet path is how an unasked question
    starts reading as a clean answer.

    Imported lazily to keep this module's import surface exactly ``ast``,
    ``os`` and ``re`` for the CI one-liner in ``helpers/ci_workflow.py``.
    """
    if not project_path:
        return []

    indexed, indexed_problem = _indexed_extractor_version(project_path)
    if not indexed and not indexed_problem:
        return []                 # not a tokensave project
    installed, installed_problem = _installed_extractor_version(tokensave_exe)
    if not installed and not installed_problem:
        return []                 # tokensave not configured; not our business

    older = newer = False
    if indexed and installed:
        from helpers.detection import _version_lt
        older = _version_lt(indexed, installed)
        newer = _version_lt(installed, indexed)

    # "Could not ask" first, and never folded into the quiet path. An
    # unreadable config and an unrunnable binary are different facts from a
    # match, and they are reported as two lines rather than one because they
    # have different remedies -- a broken index directory is not a missing
    # tokensave. Reported even when the OTHER half answered: knowing one of
    # the two versions is not knowing whether they agree.
    unknown = []
    if indexed_problem:
        unknown.append("  Could not tell which tokensave built this index: "
                       "%s." % indexed_problem)
    if installed_problem:
        unknown.append("  Could not tell which tokensave is installed: %s."
                       % installed_problem)
    if unknown:
        unknown.append(
            "  Index freshness is unverified rather than confirmed -- a "
            "graph built by an older extractor looks exactly like this one.")
        return unknown

    if not older and not newer:
        return []                 # agreed; a healthy project gains no line

    if older:
        return [
            "  This project's graph was built by tokensave %s, and %s is "
            "installed. A correctness fix in a newer extractor only reaches "
            "edges that get re-resolved, and an incremental sync does not "
            "revisit call sites it has already resolved -- so a newer binary "
            "can sit installed and change nothing anyone queries."
            % (indexed, installed),
            "  Run `tokensave sync --force` to rebuild the graph under the "
            "installed extractor. Until then, treat every graph-derived "
            "answer here -- callers, impact, dead_code, the health "
            "dimensions -- as describing a tokensave you no longer run.",
        ]

    # A downgrade, and deliberately NOT the same advice. `sync --force` would
    # rebuild the graph with the OLDER extractor, reintroducing whatever the
    # newer one had fixed -- so recommending it here would be actively
    # harmful. The mismatch is real and worth a line; the remedy is the
    # binary, not the index.
    return [
        "  This project's graph was built by tokensave %s, which is NEWER "
        "than the installed %s. The index may hold edges this binary would "
        "not produce." % (indexed, installed),
        "  Do NOT run `sync --force` to reconcile it: that would rebuild "
        "the graph with the older extractor and discard whatever the newer "
        "one fixed. Upgrade tokensave instead, or point tokensave_exe at "
        "the newer install.",
    ]


def audit_pyscope_cache(project_path: str, pyscope_exe: str = "") -> list:
    """A recommendation about where PyScope keeps this project's analysis.

    A recommendation, not a warning, and deliberately not a violation. Nothing
    is broken: PyScope works perfectly with its cache in per-user app data. The
    only consequence is that the analysis does not live beside the source it
    describes, which is a preference with a performance side-effect rather than
    a defect.

    **Measures the outcome rather than inferring it.** The test is not "does
    .gitignore mention .pyscope/" -- git's matcher is what actually decides,
    and reading the file's text would be guessing at its answer. It is
    "PyScope analysed this project and did NOT leave a local cache", which is
    PyScope's own decision, already made, observed after the fact.

    Silent in every case where it would be speculating:

      * PyScope not configured -- nothing to say;
      * the registry could not be read -- "could not ask" is not "no";
      * the project is not registered -- no advice to give about a project
        PyScope does not know;
      * registered but never analysed -- PyScope has not chosen a cache
        location yet, so there is no outcome to report;
      * the local cache exists -- the good case says nothing, so a healthy
        project does not gain a permanent line.

    Imported lazily to keep this module's import surface exactly ``ast``,
    ``os`` and ``re`` for the CI one-liner in ``helpers/ci_workflow.py``.
    """
    if not pyscope_exe or not project_path:
        return []
    try:
        from helpers.pyscope import registered, same_path, canonical_project
        from helpers.detection import _is_pyscope_project
    except ImportError:
        return []

    entries = registered(pyscope_exe)
    if entries is None:
        return []          # could not ask; not the same as "no"

    canon = canonical_project(project_path)
    match = None
    for entry in entries:
        if same_path(str(entry.get("root", "")), canon):
            match = entry
            break
    if match is None:
        return []          # not registered with PyScope
    if not match.get("analyzed_at"):
        return []          # no cache decision has been made yet
    if _is_pyscope_project(project_path):
        return []          # already local; the good case is quiet

    return [
        "  PyScope has analysed this project but keeps its cache in per-user "
        "app data, because `.pyscope/` is not ignored here. PyScope writes "
        "`<project>/.pyscope/` only when git ignores it.",
        "  Optional: add `.pyscope/` to .gitignore (right-click -> Git -> "
        "Manage .gitignore... -> Baseline) to keep the analysis beside the "
        "source it describes. Nothing is broken either way.",
    ]


def audit_mcp_auto_approve() -> list:
    """Report blanket MCP auto-approval as posture, never as brokenness.

    `enableAllProjectMcpServers` in `~/.claude/settings.json` approves every
    MCP server in every repository on the machine, including ones not cloned
    yet. That is a legitimate choice for a single-user machine and a bad one
    on a shared box, and Doctor is not the place to enforce either -- so this
    states what is true and what it implies, and offers no fix.

    Silent when the setting is absent, which is the default.

    Imported lazily so this module's import surface stays exactly ``ast``,
    ``os`` and ``re`` for the CI one-liner in ``helpers/ci_workflow.py``.
    """
    try:
        from helpers.mcp_approval import AUTO_APPROVE_KEY, blanket_auto_approve
    except ImportError:
        return []

    enabled, path = blanket_auto_approve()
    if not enabled:
        return []
    return [
        "  %s is on in %s." % (AUTO_APPROVE_KEY, path),
        "  Every MCP server in every project on this machine is approved "
        "automatically, including repositories cloned in future. Measured, "
        "not inferred: a scratch project never opened before was approved by "
        "this flag alone.",
        "  Not a fault -- a trust setting. Remove the key to go back to "
        "approving each project's .mcp.json on its own merits.",
    ]
