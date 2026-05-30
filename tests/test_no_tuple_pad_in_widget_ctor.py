"""tests/test_no_tuple_pad_in_widget_ctor.py — pre-flight guard.

A tuple ``padx``/``pady`` (e.g. ``pady=(6, 0)``) is valid ONLY in geometry-
manager calls (``.pack()`` / ``.grid()`` / ``.place()`` and their
``*_configure`` forms). Passing a tuple to a **widget constructor or
``.configure()``** is invalid — strict Tk builds raise
``_tkinter.TclError: bad screen distance "6 0"``. Under pythonw.exe that
exception is written to a null stderr and vanishes, so a dialog that builds
halfway then throws just appears as a blank window (this is exactly the bug
that hid in the PR-draft dialog for a whole session).

This AST scan walks every module under ``src/`` and flags any call that
passes a tuple/list to ``padx``/``pady`` UNLESS the call is a geometry-manager
method. No Tk / display needed.

If this fails: move the padding off the widget constructor/``configure`` and
onto the widget's ``.pack()``/``.grid()`` call (where the 2-tuple form means
(leading, trailing)).
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

# Calls where a tuple padx/pady is legitimate.
_GEOMETRY_METHODS = {
    "pack", "grid", "place",
    "pack_configure", "grid_configure", "place_configure",
}


def _callee_name(func: ast.expr) -> str:
    """Return the trailing attribute/name of a call's callee, or ''."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _find_violations(tree: ast.AST, relpath: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name in _GEOMETRY_METHODS:
            continue  # tuple pad is valid here
        for kw in node.keywords:
            if kw.arg in ("padx", "pady") and isinstance(kw.value, (ast.Tuple, ast.List)):
                out.append(f"{relpath}:{node.lineno}  {name}(... {kw.arg}=<tuple>)")
    return out


def test_no_tuple_pad_in_widget_constructors():
    assert _SRC.is_dir(), f"src dir not found at {_SRC}"
    violations: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        violations.extend(_find_violations(tree, str(py.relative_to(_SRC))))

    assert not violations, (
        "Tuple padx/pady passed to a non-geometry call (widget constructor or "
        ".configure) — invalid on strict Tk, fails silently under pythonw. "
        "Move the padding onto the widget's .pack()/.grid() call:\n  "
        + "\n  ".join(violations)
    )
