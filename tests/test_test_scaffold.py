"""tests/test_test_scaffold.py — helpers/test_scaffold.py (v4.13).

Argv-style assertions on the rendered templates: docstring shape,
fixture imports, ``@pytest.mark.tk`` marker on the dialog template,
filename mapping (helpers/ vs dialogs/), refuse-to-overwrite behaviour.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from helpers.test_scaffold import (
    TEMPLATES,
    _test_filename_for,
    generate_test_file,
    preview_test_file,
)


# ── Fixture: synthetic project ────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Build a tmp src/ layout with one helper + one dialog."""
    src = tmp_path / "src"
    (src / "helpers").mkdir(parents=True)
    (src / "dialogs").mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "helpers" / "__init__.py").write_text("")
    (src / "dialogs" / "__init__.py").write_text("")
    (src / "helpers" / "quality_checks.py").write_text(
        "def run_check(p): return True\n"
    )
    (src / "dialogs" / "checks_dialog.py").write_text(
        "class ChecksDialog: pass\n"
    )
    return tmp_path


# ── _test_filename_for ───────────────────────────────────────────────────

def test_filename_for_helper(proj):
    """Helpers use the plain ``test_<basename>.py`` form."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    assert _test_filename_for(source, str(proj)) == "test_quality_checks.py"


def test_filename_for_dialog_strips_dialog_suffix(proj):
    """src/dialogs/foo_dialog.py → test_dialog_foo.py (suffix stripped)."""
    source = str(proj / "src" / "dialogs" / "checks_dialog.py")
    assert _test_filename_for(source, str(proj)) == "test_dialog_checks.py"


def test_filename_for_dialog_without_dialog_suffix(proj):
    """src/dialogs/tool_manager.py → test_dialog_tool_manager.py
    (no suffix to strip)."""
    (proj / "src" / "dialogs" / "tool_manager.py").write_text("")
    source = str(proj / "src" / "dialogs" / "tool_manager.py")
    assert _test_filename_for(source, str(proj)) == "test_dialog_tool_manager.py"


# ── preview_test_file ────────────────────────────────────────────────────

def test_preview_unknown_template_raises(proj):
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    with pytest.raises(ValueError, match="unknown template"):
        preview_test_file(source, "bogus_template", str(proj))


def test_preview_pure_helper_renders_import_path(proj):
    """The ``from <import_path> import`` line names the helper module."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    content = preview_test_file(source, "pure_helper", str(proj))
    assert "from helpers.quality_checks import" in content
    assert "def test_TODO_basic_happy_path" in content
    # Pure helper template has NO @pytest.mark.tk marker.
    assert "@pytest.mark.tk" not in content
    assert "pytestmark = pytest.mark.tk" not in content


def test_preview_subprocess_helper_includes_import_site_warning(proj):
    """The subprocess template references mocking at the IMPORT SITE
    (G-E discipline) — the comment must appear so the user is reminded."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    content = preview_test_file(source, "subprocess_helper", str(proj))
    assert "IMPORT SITE" in content or "import site" in content.lower()
    # The patched module path is included in the example.
    assert '"helpers.quality_checks.subprocess.run"' in content


def test_preview_dialog_tk_marks_test_file(proj):
    """The dialog template MUST include pytestmark = pytest.mark.tk
    so Linux CI without xvfb will skip it correctly."""
    source = str(proj / "src" / "dialogs" / "checks_dialog.py")
    content = preview_test_file(source, "dialog_tk", str(proj))
    assert "pytestmark = pytest.mark.tk" in content
    assert "tk = pytest.importorskip" in content
    assert "from dialogs.checks_dialog import" in content


def test_preview_dialog_tk_references_tk_fixtures(proj):
    """Dialog template should mention the tk_root + mock_config fixtures."""
    source = str(proj / "src" / "dialogs" / "checks_dialog.py")
    content = preview_test_file(source, "dialog_tk", str(proj))
    assert "tk_root" in content
    assert "mock_config" in content


def test_preview_blank_template_minimal(proj):
    """Blank template should have NO mock-specific imports but a basic
    test stub the user can edit immediately."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    content = preview_test_file(source, "blank", str(proj))
    assert "import pytest" in content
    assert "def test_TODO_" in content
    # No subprocess-specific or Tk-specific scaffolding.
    assert "pytestmark = pytest.mark.tk" not in content
    assert "subprocess" not in content.lower()


def test_preview_each_template_has_placeholder_assertions(proj):
    """Every template must produce a file that compiles AND passes
    pytest immediately (placeholders are ``assert True`` so the user
    gets green feedback before customising)."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    for tmpl in TEMPLATES:
        content = preview_test_file(source, tmpl, str(proj))
        assert "assert" in content, f"{tmpl} template lacks any assert"


# ── generate_test_file ───────────────────────────────────────────────────

def test_generate_writes_file_with_expected_name(proj):
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    ok, msg = generate_test_file(str(proj), source, "pure_helper")
    assert ok, f"generate failed: {msg}"
    expected = proj / "tests" / "test_quality_checks.py"
    assert expected.is_file()


def test_generate_refuses_to_overwrite(proj):
    """If the test file already exists, refuse and surface the path."""
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    # Seed an existing test file.
    (proj / "tests").mkdir(exist_ok=True)
    pre = proj / "tests" / "test_quality_checks.py"
    pre.write_text("# user-written content — must NOT be clobbered\n")

    ok, msg = generate_test_file(str(proj), source, "pure_helper")
    assert ok is False
    assert "refusing to overwrite" in msg.lower()
    # Original content untouched.
    assert "user-written content" in pre.read_text()


def test_generate_refuses_unknown_template(proj):
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    ok, msg = generate_test_file(str(proj), source, "made_up_template")
    assert ok is False
    assert "unknown template" in msg.lower()


def test_generate_refuses_missing_source(proj):
    """A source path that doesn't exist on disk is rejected up front."""
    ok, msg = generate_test_file(
        str(proj), str(proj / "src" / "helpers" / "ghost.py"), "blank")
    assert ok is False
    assert "not found" in msg.lower()


def test_generated_file_compiles(proj):
    """The freshly-written test file must parse as valid Python."""
    import py_compile
    source = str(proj / "src" / "helpers" / "quality_checks.py")
    ok, msg = generate_test_file(str(proj), source, "pure_helper")
    assert ok
    py_compile.compile(msg, doraise=True)
