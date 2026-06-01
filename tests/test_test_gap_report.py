"""tests/test_test_gap_report.py — new-vs-update diff suggestion split.

`suggest_tests_for_diff` lists changed src files with NO test (create);
`suggest_test_updates_for_diff` lists changed src files that ALREADY have a
test (regenerate), carrying the real existing ``test_path``. Real temp repo.
Skips if git is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from helpers.test_gap_report import (
    suggest_test_updates_for_diff,
    suggest_tests_for_diff,
)

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git not available on PATH")


def _run(repo, *args):
    subprocess.run([_GIT, "-C", str(repo)] + list(args),
                   check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def _repo(tmp_path):
    _run(tmp_path, "init")
    _run(tmp_path, "config", "user.email", "t@e.com")
    _run(tmp_path, "config", "user.name", "t")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    _run(tmp_path, "branch", "-M", "main")
    (tmp_path / "src" / "helpers").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    # foo HAS a test; bar does NOT.
    (tmp_path / "src" / "helpers" / "foo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_foo.py").write_text(
        "from helpers.foo import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8")
    (tmp_path / "src" / "helpers" / "bar.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-m", "base")
    # feature branch changes BOTH files.
    _run(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "src" / "helpers" / "foo.py").write_text("def f():\n    return 11\n", encoding="utf-8")
    (tmp_path / "src" / "helpers" / "bar.py").write_text("def g():\n    return 22\n", encoding="utf-8")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-m", "change both")
    return str(tmp_path)


def test_updates_returns_changed_files_with_tests(tmp_path):
    ups = suggest_test_updates_for_diff(_repo(tmp_path), _GIT, "main")
    rels = {u.rel_path for u in ups}
    assert "src/helpers/foo.py" in rels        # changed AND has a test
    assert "src/helpers/bar.py" not in rels     # no test → not an update
    foo = next(u for u in ups if u.rel_path == "src/helpers/foo.py")
    assert foo.test_exists is True
    assert foo.test_path.replace("\\", "/").endswith("tests/test_foo.py")


def test_new_returns_changed_files_without_tests(tmp_path):
    new = suggest_tests_for_diff(_repo(tmp_path), _GIT, "main")
    rels = {u.rel_path for u in new}
    assert "src/helpers/bar.py" in rels         # changed AND untested
    assert "src/helpers/foo.py" not in rels      # already tested → not "new"
    assert all(u.test_exists is False for u in new)
