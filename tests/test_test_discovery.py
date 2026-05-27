"""tests/test_test_discovery.py — helpers/test_discovery.py (v4.13).

Tests list_test_files (V-C: count both module-level + class-indented),
scan_coverage_gaps (filename heuristic), detect_stale_tests (V-D:
top-level imports only), and the per-project cache helpers.

Uses a tmp_path-based synthetic layout (src/ + tests/) so we don't
depend on the real repo state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from helpers.test_discovery import (
    MANAGER_CACHE_DIRNAME,
    detect_stale_tests,
    list_test_files,
    load_last_run_results,
    load_stale_allowlist,
    save_last_run_results,
    save_stale_allowlist,
    scan_coverage_gaps,
)


# ── Synthetic project fixture ────────────────────────────────────────────

def _make_project(tmp_path: Path, *, src_files: dict, test_files: dict) -> Path:
    """Build a tmp src/+tests/ layout with the given files.

    Both dicts map relative path → file content (e.g.
    ``{"helpers/foo.py": "def bar(): pass"}``).
    """
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    # Empty __init__.py so the import-path looks real.
    (src / "__init__.py").write_text("")
    for rel, content in src_files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure each subpkg has an __init__.py.
        for part in path.parent.parts:
            pass
        cursor = src
        for part in path.parent.relative_to(src).parts:
            cursor = cursor / part
            cursor.mkdir(exist_ok=True)
            init = cursor / "__init__.py"
            if not init.exists():
                init.write_text("")
        path.write_text(content)
    for rel, content in test_files.items():
        path = tests / rel
        path.write_text(content)
    return tmp_path


# ── list_test_files (V-C) ────────────────────────────────────────────────

def test_list_test_files_empty_when_no_tests_dir(tmp_path):
    """No tests/ directory → empty list."""
    assert list_test_files(str(tmp_path)) == []


def test_list_test_files_counts_module_level_def_test(tmp_path):
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_foo.py": "def test_one(): assert True\n"
                       "def test_two(): assert True\n"
                       "def test_three(): pass\n",
    })
    files = list_test_files(str(proj))
    assert len(files) == 1
    assert files[0].name == "test_foo.py"
    assert files[0].test_count == 3


def test_list_test_files_counts_class_indented_tests_via_ast(tmp_path):
    """V-C: smoke_test.py uses ``class Test_X`` with indented ``def test_*``;
    the AST-based count must catch BOTH module-level and class-method tests."""
    proj = _make_project(tmp_path, src_files={}, test_files={
        "smoke_test.py":
            "import unittest\n\n"
            "class TestThing(unittest.TestCase):\n"
            "    def test_a(self): self.assertTrue(True)\n"
            "    def test_b(self): self.assertTrue(True)\n"
            "    def helper(self): pass\n\n"
            "def test_module_level(): assert True\n",
    })
    files = list_test_files(str(proj))
    assert len(files) == 1
    # 2 class-method tests + 1 module-level test = 3
    assert files[0].test_count == 3


def test_list_test_files_handles_syntax_error_files(tmp_path):
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_bad.py": "def test_oops( SYNTAX ERROR\n",
    })
    files = list_test_files(str(proj))
    # File is listed; count is 0 (graceful AST fallback).
    assert len(files) == 1
    assert files[0].test_count == 0


def test_list_test_files_skips_tmp_files(tmp_path):
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_foo.py":              "def test_x(): assert True\n",
        "test_foo.py.tmp.123.abc":   "junk",
    })
    files = list_test_files(str(proj))
    names = [f.name for f in files]
    assert "test_foo.py" in names
    assert all(".tmp." not in n for n in names)


# ── scan_coverage_gaps ──────────────────────────────────────────────────

def test_coverage_gaps_no_src_dir(tmp_path):
    assert scan_coverage_gaps(str(tmp_path)) == []


def test_coverage_gaps_finds_helper_match(tmp_path):
    proj = _make_project(tmp_path, src_files={
        "helpers/quality.py": "def run(): pass\n",
    }, test_files={
        "test_quality.py": "def test_run(): assert True\n",
    })
    gaps = scan_coverage_gaps(str(proj))
    helper = next((g for g in gaps if g.rel_path.endswith("quality.py")), None)
    assert helper is not None
    assert helper.has_tests is True


def test_coverage_gaps_finds_dialog_match(tmp_path):
    """src/dialogs/foo_dialog.py → tests/test_dialog_foo.py."""
    proj = _make_project(tmp_path, src_files={
        "dialogs/foo_dialog.py": "class FooDialog: pass\n",
    }, test_files={
        "test_dialog_foo.py": "def test_construct(): assert True\n",
    })
    gaps = scan_coverage_gaps(str(proj))
    dialog = next((g for g in gaps if g.rel_path.endswith("foo_dialog.py")), None)
    assert dialog is not None
    assert dialog.has_tests is True


def test_coverage_gaps_flags_untested(tmp_path):
    proj = _make_project(tmp_path, src_files={
        "helpers/lonely.py": "def lonely(): pass\n",
    }, test_files={})
    gaps = scan_coverage_gaps(str(proj))
    lonely = next((g for g in gaps if g.rel_path.endswith("lonely.py")), None)
    assert lonely is not None
    assert lonely.has_tests is False


def test_coverage_gaps_excludes_init_files(tmp_path):
    """__init__.py shouldn't appear in the coverage report."""
    proj = _make_project(tmp_path, src_files={
        "helpers/foo.py": "def foo(): pass\n",
    }, test_files={})
    gaps = scan_coverage_gaps(str(proj))
    assert all(not g.rel_path.endswith("__init__.py") for g in gaps)


# ── detect_stale_tests (V-D) ─────────────────────────────────────────────

def test_stale_detection_no_signals_when_clean(tmp_path):
    proj = _make_project(tmp_path, src_files={
        "helpers/real.py": "def real_fn(): pass\n",
    }, test_files={
        "test_real.py": "from helpers.real import real_fn\n"
                         "def test_x(): assert real_fn is not None\n",
    })
    assert detect_stale_tests(str(proj)) == []


def test_stale_detection_flags_missing_module(tmp_path):
    # The `helpers/` namespace exists (sentinel.py keeps it alive), but
    # `helpers.deleted_module.py` doesn't — exactly the post-refactor
    # scenario the stale detection is designed to catch.
    proj = _make_project(tmp_path, src_files={
        "helpers/sentinel.py": "# keeps the helpers/ namespace alive\n",
    }, test_files={
        "test_gone.py": "from helpers.deleted_module import x\n"
                         "def test_x(): pass\n",
    })
    stale = detect_stale_tests(str(proj))
    assert len(stale) == 1
    assert "missing module" in stale[0].reason
    assert "helpers.deleted_module" in stale[0].detail


def test_stale_detection_flags_missing_symbol(tmp_path):
    proj = _make_project(tmp_path, src_files={
        "helpers/exists.py": "def other_fn(): pass\n",
    }, test_files={
        "test_exists.py": "from helpers.exists import gone_fn\n"
                           "def test_x(): pass\n",
    })
    stale = detect_stale_tests(str(proj))
    assert len(stale) == 1
    assert "missing symbol" in stale[0].reason
    assert "exists.gone_fn" in stale[0].detail


def test_stale_detection_skips_type_checking_block(tmp_path):
    """V-D: TYPE_CHECKING imports must NOT be flagged even if the
    module they reference doesn't exist (it's only used by type checkers)."""
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_type_only.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from helpers.does_not_exist import GoneClass\n"
            "def test_x(): assert True\n"
        ),
    })
    stale = detect_stale_tests(str(proj))
    # No false positive: top-level scan skips `if TYPE_CHECKING:` body.
    assert stale == []


def test_stale_detection_skips_function_scoped_imports(tmp_path):
    """Lazy/function-scoped imports must not be flagged either."""
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_lazy.py": (
            "def test_x():\n"
            "    from helpers.does_not_exist import GoneClass\n"
            "    assert True\n"
        ),
    })
    assert detect_stale_tests(str(proj)) == []


def test_stale_detection_skips_external_imports(tmp_path):
    """Non-project imports (os, re, tkinter) must not be flagged."""
    proj = _make_project(tmp_path, src_files={}, test_files={
        "test_external.py": (
            "import os\n"
            "import re\n"
            "from tkinter import messagebox\n"
            "def test_x(): assert True\n"
        ),
    })
    assert detect_stale_tests(str(proj)) == []


def test_stale_detection_respects_allowlist(tmp_path):
    proj = _make_project(tmp_path, src_files={
        "helpers/sentinel.py": "# keeps the helpers/ namespace alive\n",
    }, test_files={
        "test_gone.py": "from helpers.deleted_module import x\n"
                         "def test_x(): pass\n",
    })
    # Without allowlist: 1 signal.
    assert len(detect_stale_tests(str(proj))) == 1
    # With allowlist: 0 signals.
    allow = {"tests/test_gone.py"}
    assert detect_stale_tests(str(proj), allowlist=allow) == []


# ── Cache helpers (V-B: .tokensave-manager/ not .tokensave/) ─────────────

def test_cache_dir_constant_is_tokensave_manager():
    """Critical: the manager-owned cache dir must NOT collide with
    tokensave's own ``.tokensave/`` directory."""
    assert MANAGER_CACHE_DIRNAME == ".tokensave-manager"


def test_load_last_run_returns_empty_when_absent(tmp_path):
    assert load_last_run_results(str(tmp_path)) == {}


def test_save_and_reload_last_run_roundtrip(tmp_path):
    data = {
        "ran_at": "2026-05-27 22:00",
        "results": {
            "tests/test_foo.py": {"passed": 5, "total": 5, "status": "pass"},
        },
    }
    save_last_run_results(str(tmp_path), data)
    loaded = load_last_run_results(str(tmp_path))
    assert loaded == data


def test_save_creates_manager_cache_dir(tmp_path):
    """V-B: writes go under ``.tokensave-manager/``, NOT ``.tokensave/``."""
    save_last_run_results(str(tmp_path), {"results": {}})
    assert (tmp_path / ".tokensave-manager").is_dir()
    assert not (tmp_path / ".tokensave").exists()


def test_allowlist_save_and_load_roundtrip(tmp_path):
    allow = {"tests/test_one.py", "tests/test_two.py"}
    save_stale_allowlist(str(tmp_path), allow)
    assert load_stale_allowlist(str(tmp_path)) == allow


def test_allowlist_empty_when_absent(tmp_path):
    assert load_stale_allowlist(str(tmp_path)) == set()
