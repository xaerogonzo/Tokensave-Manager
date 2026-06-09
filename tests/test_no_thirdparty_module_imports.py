"""tests/test_no_thirdparty_module_imports.py — import-hygiene guard.

The manager is zero-runtime-dependency by design (stdlib + Tk). A single
unconditional **module-level** ``import <optional-dep>`` in ``src/`` aborts
pytest COLLECTION in CI (Linux, dev-deps-only) for every test that
transitively imports that module — e.g. a top-level ``import pystray`` in
``helpers/tray_manager.py`` killed ``tests/test_app.py`` collection until it
was made lazy.

This guard AST-scans every module under ``src/`` and fails if any has a
module-level third-party import. The fix for a violation is to move the import
into the function that uses it (a "lazy import"), as
``helpers/tray_manager.setup()`` and ``helpers/runtime._make_tray_icon()`` do.

Imports nested inside a module-level ``try``/``if`` (i.e. *guarded* optional
imports) are intentionally NOT flagged — only bare, unconditional top-level
imports, which are the ones that abort collection.

**Dual-mode:** also runnable as a standalone, stdlib-only script —
``python tests/test_no_thirdparty_module_imports.py`` — so CI's import-free
``check`` job can run it even when a bad import would abort the whole pytest
run (the guard test itself would never execute in that case).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

# Runtime third-party deps allowed at module level. Empty by design — the
# manager has no runtime dependencies. If that ever changes, add the dep here
# AND to requirements-dev.txt (so CI can import it).
_ALLOWED: set[str] = set()


def _project_roots() -> set[str]:
    """Top-level importable names that live under src/ (packages + modules)."""
    roots: set[str] = set()
    for entry in _SRC.iterdir():
        if entry.is_dir() and not entry.name.startswith((".", "__")):
            roots.add(entry.name)
        elif entry.suffix == ".py":
            roots.add(entry.stem)
    return roots


def _module_level_import_roots(tree: ast.Module) -> set[str]:
    """Top-level package names imported at MODULE scope.

    Uses direct children of the Module node only, so imports nested inside a
    module-level ``try``/``if`` (guarded optional imports) are excluded — those
    don't abort collection.
    """
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:        # absolute imports only
                names.add(node.module.split(".")[0])
    return names


def find_violations() -> list[str]:
    """Return sorted 'src/rel/path.py: depname' for each module-level offender."""
    allowed = set(sys.stdlib_module_names) | _project_roots() | _ALLOWED | {"__future__"}
    violations: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        if ".tmp." in py.name:                          # atomic-write leftovers
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        rel = py.relative_to(_SRC.parent).as_posix()
        for root in sorted(_module_level_import_roots(tree)):
            if root not in allowed:
                violations.append(f"{rel}: {root}")
    return violations


def test_src_has_no_module_level_thirdparty_imports():
    violations = find_violations()
    assert not violations, (
        "Module-level third-party imports found in src/ (the manager is\n"
        "zero-runtime-dependency by design). A bare top-level import of an\n"
        "optional dependency aborts pytest COLLECTION in CI. Move each into\n"
        "the function that uses it (lazy import), like\n"
        "helpers/tray_manager.setup() or helpers/runtime._make_tray_icon():\n\n  "
        + "\n  ".join(violations)
        + "\n\n(If the manager genuinely took a new runtime dependency, add it\n"
          "to requirements-dev.txt AND to _ALLOWED in this file.)"
    )


def _main() -> int:
    violations = find_violations()
    if violations:
        sys.stderr.write(
            "Module-level third-party imports in src/ (must be lazy):\n")
        for v in violations:
            sys.stderr.write(f"  {v}\n")
        sys.stderr.write(
            "\nMove each import into the function that uses it. "
            "See helpers/tray_manager.setup().\n")
        return 1
    sys.stdout.write("OK: no module-level third-party imports in src/.\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
