"""test_discovery — Pure helpers backing the Test Manager dialog (v4.13).

Three concerns:

* **list_test_files**     — enumerate ``tests/test_*.py`` with test counts.
                            V-C: counts BOTH module-level ``def test_*``
                            and class-indented ``def test_*`` (the latter
                            is how ``tests/smoke_test.py``'s 14
                            ``unittest.TestCase`` classes expose tests).
* **scan_coverage_gaps**  — filename-heuristic source→test mapping.
                            Documented as approximate; pytest-cov line-
                            level coverage is a Roadmap-8 follow-up.
* **detect_stale_tests**  — flag test files that import modules/symbols
                            not present in src/. V-D: scans TOP-LEVEL
                            imports only (skips ``if TYPE_CHECKING:`` /
                            function-scoped / try-guarded imports).

Plus per-project cache helpers for the Tab 1 last-run results:

* ``load_last_run_results`` / ``save_last_run_results`` — keyed by test
  filename so the treeview can hydrate immediately on dialog open.

All functions are pure (no Tk). All filesystem ops use the
manager-owned ``.tokensave-manager/`` dir (V-B), never tokensave's
own ``.tokensave/``.
"""
from __future__ import annotations

import ast
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional


# ── Cache directory (V-B: .tokensave-manager/, NOT .tokensave/) ──────────

MANAGER_CACHE_DIRNAME = ".tokensave-manager"

_LAST_RUN_FILENAME    = "last_test_run.json"
_ALLOWLIST_FILENAME   = "test_allowlist.json"


def manager_cache_dir(project_root: str) -> str:
    """Return ``<project_root>/.tokensave-manager/`` (created if absent)."""
    p = os.path.join(project_root, MANAGER_CACHE_DIRNAME)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


# ── Dataclasses ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TestFileInfo:
    """One row in Test Manager Tab 1's treeview.

    Fields:
        path       — absolute path to ``tests/test_*.py``
        name       — basename (e.g. ``test_quality_checks.py``)
        test_count — number of ``def test_*`` definitions (module-level
                     + class-indented combined; see V-C).
    """
    path: str
    name: str
    test_count: int


@dataclass(frozen=True)
class CoverageRow:
    """One row in Test Manager Tab 2's treeview.

    Fields:
        source_path — absolute path to a ``src/`` .py file
        rel_path    — relative path from project root (e.g. ``src/helpers/foo.py``)
        test_path   — absolute path to the matching ``tests/test_*.py``
                      if one was found via the filename heuristic;
                      empty string otherwise
        has_tests   — True iff test_path is non-empty AND exists
    """
    source_path: str
    rel_path:    str
    test_path:   str
    has_tests:   bool


@dataclass(frozen=True)
class StaleSignal:
    """One row in Test Manager Tab 3's treeview.

    A test file can have MULTIPLE stale signals; each becomes a separate
    StaleSignal entry. The dialog renders them grouped by file.

    Fields:
        test_path  — absolute path to the test file
        test_name  — basename
        reason     — short human-readable signal description
        detail     — optional longer detail (e.g. the missing symbol name)
    """
    test_path: str
    test_name: str
    reason:    str
    detail:    str = ""


# ── list_test_files (V-C: count both module-level and class-indented) ────

def _count_tests_via_ast(source: str) -> int:
    """Return the number of test functions/methods in *source*.

    Walks the parsed AST counting any ``FunctionDef`` (or
    ``AsyncFunctionDef``) whose name starts with ``test_``. This
    catches BOTH:

      * module-level ``def test_foo(): ...``  (pytest-native)
      * class-indented ``def test_foo(self): ...``  (unittest.TestCase)

    Falls back to zero on syntax errors so a broken test file doesn't
    blow up the dialog's treeview population.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                count += 1
    return count


def list_test_files(project_root: str) -> list[TestFileInfo]:
    """Return every ``tests/test_*.py`` under *project_root*.

    Ordered by filename for stable display. Skips ``__init__.py`` and
    any ``*.tmp.*`` atomic-write artifacts. Empty list if no
    ``tests/`` directory exists.
    """
    tests_dir = os.path.join(project_root, "tests")
    if not os.path.isdir(tests_dir):
        return []
    out: list[TestFileInfo] = []
    for entry in sorted(os.listdir(tests_dir)):
        if not entry.startswith("test_") or not entry.endswith(".py"):
            # Also include the original smoke_test.py — single special case.
            if entry != "smoke_test.py":
                continue
        if ".tmp." in entry:
            continue
        path = os.path.join(tests_dir, entry)
        if not os.path.isfile(path):
            continue
        try:
            source = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        count = _count_tests_via_ast(source)
        out.append(TestFileInfo(path=path, name=entry, test_count=count))
    return out


# ── scan_coverage_gaps (filename heuristic) ──────────────────────────────

_COVERAGE_SKIP_DIRS = {"__pycache__", ".tokensave", ".tokensave-manager",
                       ".codegraph", ".git", "dist", "build", "logs"}


def _iter_src_files(project_root: str):
    """Yield every ``src/.../*.py`` worth considering for coverage.

    Skips ``__init__.py``, atomic-write tmps, and known cache dirs.
    """
    src_root = os.path.join(project_root, "src")
    if not os.path.isdir(src_root):
        return
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in _COVERAGE_SKIP_DIRS]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            if fname == "__init__.py":
                continue
            if ".tmp." in fname:
                continue
            yield os.path.join(root, fname)


def _candidate_test_paths(project_root: str, source_path: str) -> list[str]:
    """Return the candidate ``tests/test_*.py`` paths for *source_path*.

    Tries both shapes the test suite uses:

      * ``tests/test_<basename>.py``        (helpers/quality_checks.py
                                              → tests/test_quality_checks.py)
      * ``tests/test_<subpkg>_<basename>.py`` (dialogs/checks_dialog.py
                                                → tests/test_dialog_checks.py)

    Returned in order of preference (most-specific subpkg form first).
    """
    src_root  = os.path.join(project_root, "src")
    tests_dir = os.path.join(project_root, "tests")
    rel = os.path.relpath(source_path, src_root).replace("\\", "/")
    parts = rel.split("/")
    basename = parts[-1][:-len(".py")]    # strip .py
    if len(parts) >= 2:
        subpkg = parts[-2]
        # Convention observed in the v4.12 test suite:
        #   src/helpers/quality_checks.py     → tests/test_quality_checks.py
        #   src/dialogs/checks_dialog.py      → tests/test_dialog_checks.py
        #   src/dialogs/tool_manager.py       → tests/test_dialog_tool_manager.py
        #   src/controllers/help_tab.py       → tests/test_help_tab.py (anticipated)
        # → helpers/ keeps the plain ``test_<basename>`` form
        # → dialogs/ prepends "dialog_" AND strips a trailing ``_dialog`` suffix
        # → controllers/ keeps the plain form (with optional ``_ctrl`` strip)
        subpkg_short = {"dialogs": "dialog"}.get(subpkg, "")
        basename_short = basename
        for suffix in ("_dialog", "_ctrl"):
            if basename_short.endswith(suffix):
                basename_short = basename_short[:-len(suffix)]
        candidates: list[str] = []
        if subpkg_short:
            candidates.append(f"test_{subpkg_short}_{basename_short}.py")
            candidates.append(f"test_{subpkg_short}_{basename}.py")
        candidates.append(f"test_{basename}.py")
        candidates.append(f"test_{basename_short}.py")
    else:
        candidates = [f"test_{basename}.py"]
    return [os.path.join(tests_dir, c) for c in candidates]


def scan_coverage_gaps(project_root: str) -> list[CoverageRow]:
    """Return one CoverageRow per source file under ``src/``.

    Heuristic: a source file is considered "tested" iff at least one
    of its candidate ``test_*.py`` paths exists. Documented as
    approximate — the only way to be sure is line-level pytest-cov,
    deferred to a Roadmap-8 follow-up.
    """
    rows: list[CoverageRow] = []
    for src_path in _iter_src_files(project_root):
        rel = os.path.relpath(src_path, project_root).replace("\\", "/")
        candidates = _candidate_test_paths(project_root, src_path)
        found = ""
        for c in candidates:
            if os.path.isfile(c):
                found = c
                break
        rows.append(CoverageRow(
            source_path=src_path,
            rel_path=rel,
            test_path=found,
            has_tests=bool(found),
        ))
    return rows


# ── detect_stale_tests (V-D: top-level imports only) ─────────────────────

def _toplevel_imports(tree: ast.Module) -> list[tuple[str, str]]:
    """Return (module, name) for every import REACHABLE at module load.

    Skips imports nested inside:

      * ``if TYPE_CHECKING:`` / ``if False:`` blocks
      * Function or method bodies (lazy/conditional imports)
      * ``try:`` blocks (optional-dependency pattern)
      * Class bodies (rare but technically possible)

    V-D: critical that this is NOT ``ast.walk`` — that would find every
    import including TYPE_CHECKING-guarded ones, producing false
    positives on the standard typing idiom.
    """
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, ""))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                out.append((mod, alias.name))
        # Implicitly skips ast.If, ast.Try, ast.FunctionDef,
        # ast.AsyncFunctionDef, ast.ClassDef etc. — those are the cases
        # V-D specifically guards against.
    return out


def _resolve_src_module(project_root: str, module_path: str) -> Optional[str]:
    """Map a Python import string like ``helpers.foo`` → ``src/helpers/foo.py``.

    Returns the absolute path if the file exists, or ``None`` otherwise.
    Imports that don't start with one of our known subpackages
    (``helpers``, ``dialogs``, ``controllers``, etc.) are treated as
    "external dependency" — None is returned but the caller should
    NOT treat that as stale.
    """
    if not module_path:
        return None
    parts = module_path.split(".")
    src_root = os.path.join(project_root, "src")
    candidate = os.path.join(src_root, *parts) + ".py"
    # Note: src_root used twice below (candidate + pkg).
    if os.path.isfile(candidate):
        return candidate
    # Maybe it's a package (directory with __init__.py).
    pkg = os.path.join(src_root, *parts)
    if os.path.isdir(pkg):
        return pkg
    return None


def _is_project_module(project_root: str, module_path: str) -> bool:
    """True iff this import string refers to OUR project's namespace.

    Subpackages: state, helpers.*, dialogs.*, controllers.*, theme,
    constants, app, etc. — anything that exists as a sibling under
    ``src/``. Skip ``os``, ``re``, ``tkinter``, ``typing``, etc.
    """
    src_root = os.path.join(project_root, "src")
    if not os.path.isdir(src_root):
        return False
    parts = module_path.split(".")
    first = parts[0]
    return (
        os.path.isdir(os.path.join(src_root, first)) or
        os.path.isfile(os.path.join(src_root, first + ".py"))
    )


def detect_stale_tests(
    project_root: str,
    allowlist: Optional[set[str]] = None,
) -> list[StaleSignal]:
    """Scan every ``tests/test_*.py`` for stale-import signals.

    Returns one ``StaleSignal`` per offending import. A single test file
    can contribute multiple entries. The Tab 3 UI renders them grouped
    by ``test_path``.

    Signals (cheap; AST-only, no subprocess):
      * Imports a project module that no longer exists in ``src/``.
      * Imports a symbol from a project module where the module exists
        but the symbol is not defined in it.

    The ``allowlist`` is a set of relative test paths (e.g.
    ``tests/test_old.py``) the user has marked "still valid" via the
    Tab 3 button. Files in the allowlist are skipped entirely.

    V-D safety: uses ``_toplevel_imports`` (not ``ast.walk``) so
    ``if TYPE_CHECKING:`` and function-scoped imports are NEVER flagged.
    """
    if allowlist is None:
        allowlist = set()
    out: list[StaleSignal] = []

    for tfi in list_test_files(project_root):
        rel_test = os.path.relpath(tfi.path, project_root).replace("\\", "/")
        if rel_test in allowlist:
            continue
        try:
            source = open(tfi.path, "r", encoding="utf-8", errors="replace").read()
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue

        for mod, name in _toplevel_imports(tree):
            if not _is_project_module(project_root, mod):
                continue
            resolved = _resolve_src_module(project_root, mod)
            if resolved is None:
                out.append(StaleSignal(
                    test_path=tfi.path,
                    test_name=tfi.name,
                    reason="imports missing module",
                    detail=mod,
                ))
                continue
            # Module exists; if a specific name was imported, check that
            # the source file actually defines it. Only flag when the
            # source is a single .py file (not a package) — packages
            # re-export through __init__.py which we don't fully resolve.
            if name and os.path.isfile(resolved):
                try:
                    src_source = open(resolved, "r", encoding="utf-8",
                                        errors="replace").read()
                    src_tree = ast.parse(src_source)
                except (OSError, SyntaxError, ValueError):
                    continue
                if not _module_defines(src_tree, name):
                    out.append(StaleSignal(
                        test_path=tfi.path,
                        test_name=tfi.name,
                        reason="references missing symbol",
                        detail=f"{mod}.{name}",
                    ))
    return out


def _module_defines(tree: ast.Module, name: str) -> bool:
    """True iff *tree* defines a top-level symbol called *name*.

    Considers: function defs, class defs, module-level assignments,
    and module-level imports (``from X import Y as name`` defines name).
    """
    if name == "*":
        return True   # ``from X import *`` is opaque — don't false-flag
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return True
                if isinstance(tgt, ast.Tuple):
                    for elt in tgt.elts:
                        if isinstance(elt, ast.Name) and elt.id == name:
                            return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == name:
                    return True
    return False


# ── Per-project cache (last-run results, allowlists) ─────────────────────

def _atomic_write_json(path: str, data) -> None:
    """Write JSON via tmp-file + replace so a crash mid-write can't
    leave a half-written cache file behind."""
    dirpath = os.path.dirname(path)
    try:
        os.makedirs(dirpath, exist_ok=True)
    except OSError:
        return
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try: os.remove(tmp)
        except OSError: pass


def load_last_run_results(project_root: str) -> dict:
    """Return the persisted Tab 1 last-run dict, or ``{}`` if absent.

    Schema::

        {
            "ran_at": <unix_seconds>,
            "results": {
                "tests/test_foo.py": {"passed": 12, "total": 12, "status": "pass"},
                "tests/test_bar.py": {"passed": 5,  "total": 8,  "status": "fail"},
                ...
            }
        }
    """
    p = os.path.join(manager_cache_dir(project_root), _LAST_RUN_FILENAME)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_last_run_results(project_root: str, data: dict) -> None:
    """Persist the Tab 1 last-run dict to ``.tokensave-manager/``."""
    p = os.path.join(manager_cache_dir(project_root), _LAST_RUN_FILENAME)
    _atomic_write_json(p, data)


def load_stale_allowlist(project_root: str) -> set[str]:
    """Return the set of rel-test-paths the user marked 'still valid'."""
    p = os.path.join(manager_cache_dir(project_root), _ALLOWLIST_FILENAME)
    if not os.path.isfile(p):
        return set()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("allow"), list):
            return set(str(x) for x in data["allow"])
        if isinstance(data, list):
            return set(str(x) for x in data)
    except (OSError, json.JSONDecodeError):
        pass
    return set()


def save_stale_allowlist(project_root: str, allowlist: set[str]) -> None:
    """Persist the stale-tests 'still valid' allowlist."""
    p = os.path.join(manager_cache_dir(project_root), _ALLOWLIST_FILENAME)
    _atomic_write_json(p, {"allow": sorted(allowlist)})


# Suppress unused-import warning for `re` — kept in case future scanners
# (e.g. stale-test mtime check) need it inline.
_ = re
_ = field
