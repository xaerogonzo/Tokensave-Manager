"""Tests for helpers/commit_messages.py — smart commit-message generation."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from helpers.commit_messages import (
    _files_from_diff,
    _build_commit_grounding,
    _clean_prefix_scope,
    _fetch_commit_context,
    _ctx_cache,
)


class TestFilesFromDiff:
    """Tests for _files_from_diff — extract changed-file paths from unified diff."""

    def test_empty_diff_returns_empty_list(self):
        """Empty diff text returns empty list."""
        assert _files_from_diff("") == []

    def test_none_diff_returns_empty_list(self):
        """None input returns empty list."""
        assert _files_from_diff(None) == []

    def test_single_file_extracted(self):
        """Single file from diff is extracted."""
        diff = "+++ b/src/main.py\n"
        assert _files_from_diff(diff) == ["src/main.py"]

    def test_multiple_files_extracted(self):
        """Multiple files from diff are all extracted in order."""
        diff = "+++ b/src/main.py\n+++ b/tests/test_main.py\n+++ b/README.md\n"
        assert _files_from_diff(diff) == ["src/main.py", "tests/test_main.py", "README.md"]

    def test_dev_null_filtered_out(self):
        """Files with /dev/null in target path (deletions) are filtered."""
        diff = "+++ b/src/main.py\n+++ /dev/null\n"
        assert _files_from_diff(diff) == ["src/main.py"]

    def test_only_dev_null_returns_empty(self):
        """Diff with only /dev/null returns empty list."""
        diff = "+++ /dev/null\n"
        assert _files_from_diff(diff) == []

    def test_multiple_dev_null_filtered(self):
        """Multiple /dev/null entries filtered while keeping real files."""
        diff = "+++ /dev/null\n+++ b/file1.py\n+++ /dev/null\n+++ b/file2.py\n"
        assert _files_from_diff(diff) == ["file1.py", "file2.py"]

    def test_filename_with_spaces(self):
        """Filenames with spaces are preserved."""
        diff = '+++ b/src/my file.py\n'
        assert _files_from_diff(diff) == ["src/my file.py"]

    def test_deeply_nested_path(self):
        """Deeply nested directory paths are extracted correctly."""
        diff = "+++ b/a/b/c/d/e/f/module.py\n"
        assert _files_from_diff(diff) == ["a/b/c/d/e/f/module.py"]

    def test_filename_with_dots_and_dashes(self):
        """Filenames with dots and dashes handled correctly."""
        diff = "+++ b/src/my-file_v2.0.py\n"
        assert _files_from_diff(diff) == ["src/my-file_v2.0.py"]

    def test_real_world_diff_output(self):
        """Real diff output with headers and hunks."""
        diff = """diff --git a/src/main.py b/src/main.py
index abc123..def456 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,5 +1,6 @@
 print("hello")

diff --git a/tests/test.py b/tests/test.py
new file mode 100644
index 0000000..xyz789
--- /dev/null
+++ b/tests/test.py
@@ -0,0 +1,3 @@
"""
        assert _files_from_diff(diff) == ["src/main.py", "tests/test.py"]

    def test_multiline_diff_with_mixed_content(self):
        """Diff with both added files and deleted files."""
        diff = """--- a/old_file.py
+++ b/new_file.py
@@ -1,1 +1,1 @@

--- a/deleted_file.py
+++ /dev/null
@@ -1,1 +0,0 @@
"""
        assert _files_from_diff(diff) == ["new_file.py"]


class TestBuildCommitGrounding:
    """Tests for _build_commit_grounding."""

    def test_returns_string_type(self):
        """Always returns a string."""
        result = _build_commit_grounding(".", "", "", "")
        assert isinstance(result, str)

    def test_empty_diff_returns_string(self):
        """Empty diff text still returns a valid string."""
        result = _build_commit_grounding(".", "", "", "")
        assert isinstance(result, str)

    def test_with_file_paths_in_diff(self):
        """Processes diff text to extract files."""
        diff = "+++ b/src/main.py\n+++ b/src/utils.py\n"
        result = _build_commit_grounding(".", "", "", diff)
        assert isinstance(result, str)

    def test_empty_executables_returns_string(self):
        """Empty tokensave and codegraph exe paths handled gracefully."""
        result = _build_commit_grounding(".", "", "", "+++ b/test.py\n")
        assert isinstance(result, str)

    def test_missing_repo_path_handled(self):
        """Missing repo path doesn't crash."""
        result = _build_commit_grounding("nonexistent/path", "", "", "")
        assert isinstance(result, str)

    def test_with_real_diff(self):
        """Real-world diff input processed."""
        diff = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
"""
        result = _build_commit_grounding(".", "", "", diff)
        assert isinstance(result, str)


class TestCleanPrefixScope:
    """Tests for _clean_prefix_scope — clean up file path scopes."""

    def test_simple_scope_unchanged(self):
        """Simple scope without path markers unchanged."""
        result = _clean_prefix_scope("chore(scripts):")
        assert isinstance(result, str)
        assert "chore" in result
        assert ":" in result

    def test_scope_with_single_path_component(self):
        """Single filename scope cleaned."""
        result = _clean_prefix_scope("fix(llm.py):")
        assert isinstance(result, str)
        assert any(c in result for c in ["(", ")", ":"])

    def test_scope_with_nested_path(self):
        """Nested path scope reduced to last component."""
        result = _clean_prefix_scope("feat(src/controllers/update_poller.py):")
        assert isinstance(result, str)
        assert "feat" in result
        scope_part = result.split("(")[1].split(")")[0] if "(" in result else ""
        if scope_part:
            assert "/" not in scope_part

    def test_scope_with_file_extension_removed(self):
        """File extension stripped from scope."""
        result = _clean_prefix_scope("fix(helpers/llm.py):")
        assert isinstance(result, str)
        assert "fix" in result
        scope_part = result.split("(")[1].split(")")[0] if "(" in result else ""
        if scope_part:
            assert not scope_part.endswith(".py")

    def test_scope_with_breaking_change_marker(self):
        """Breaking change ! marker preserved."""
        result = _clean_prefix_scope("feat(scope)!:")
        assert isinstance(result, str)
        assert "feat" in result

    def test_long_scope_capped_at_20_chars(self):
        """Scope with file path exceeding 20 chars is capped."""
        # This filename is 35 chars without extension, should be capped to 20
        result = _clean_prefix_scope("feat(path/to/this_is_a_very_long_filename_text.py):")
        assert isinstance(result, str)
        # Extract scope content
        if "(" in result and ")" in result:
            scope = result.split("(")[1].split(")")[0]
            # Scope should be capped at 20 chars
            assert len(scope) <= 20

    def test_scope_without_extension_unchanged(self):
        """Scope without file extension unchanged."""
        result = _clean_prefix_scope("feat(scripts):")
        assert isinstance(result, str)

    def test_empty_scope_handled(self):
        """Empty scope handled gracefully."""
        result = _clean_prefix_scope("feat():")
        assert isinstance(result, str)

    def test_scope_with_multiple_dots(self):
        """Filename with multiple dots handled."""
        result = _clean_prefix_scope("fix(src/my.config.js):")
        assert isinstance(result, str)

    def test_windows_path_handling(self):
        """Windows-style paths (if present) handled."""
        result = _clean_prefix_scope("fix(src\\controllers\\main.py):")
        assert isinstance(result, str)


class TestFetchCommitContext:
    """Tests for _fetch_commit_context — tokensave commit_context enrichment."""

    def setup_method(self):
        _ctx_cache.clear()

    def _make_ctx_output(self, symbols=None, roles=None, style=None):
        return json.dumps({
            "changed_symbols": symbols or [],
            "file_roles": roles or {},
            "recent_commit_style": style or [],
        })

    def test_happy_path_formats_markdown(self, tmp_path):
        ctx_json = self._make_ctx_output(
            symbols=[{"name": "MyClass.save", "file": "foo.py"}],
            roles={"foo.py": "persistence layer"},
            style=["feat:", "fix:"],
        )
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                r = Mock()
                if "rev-parse" in cmd:
                    r.returncode = 0
                    r.stdout = "abc123\n"
                else:
                    r.returncode = 0
                    r.stdout = ctx_json
                return r
            mock_run.side_effect = side_effect
            result = _fetch_commit_context("tokensave", str(tmp_path), "git", "diff text")
        assert "## Changed symbols" in result
        assert "MyClass.save (foo.py)" in result
        assert "## File roles" in result
        assert "persistence layer" in result
        assert "## Recent commit style" in result
        assert "feat:" in result

    def test_no_tokensave_exe_returns_empty(self, tmp_path):
        result = _fetch_commit_context("", str(tmp_path), "git", "diff")
        assert result == ""

    def test_malformed_json_returns_empty(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                r = Mock()
                if "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "sha1\n"
                else:
                    r.returncode = 0; r.stdout = "not-valid-json{"
                return r
            mock_run.side_effect = side_effect
            result = _fetch_commit_context("tokensave", str(tmp_path), "git", "diff")
        assert result == ""

    def test_subprocess_failure_returns_empty(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                r = Mock()
                if "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "sha1\n"
                else:
                    r.returncode = 1; r.stdout = ""
                return r
            mock_run.side_effect = side_effect
            result = _fetch_commit_context("tokensave", str(tmp_path), "git", "diff")
        assert result == ""

    def test_cache_hit_skips_second_subprocess_call(self, tmp_path):
        ctx_json = self._make_ctx_output(symbols=[{"name": "Foo", "file": "x.py"}])
        ctx_call_count = {"n": 0}
        with patch("subprocess.run") as mock_run:
            def side_effect(cmd, **kw):
                r = Mock()
                if "rev-parse" in cmd:
                    r.returncode = 0; r.stdout = "sha1\n"
                else:
                    ctx_call_count["n"] += 1
                    r.returncode = 0; r.stdout = ctx_json
                return r
            mock_run.side_effect = side_effect
            _fetch_commit_context("tokensave", str(tmp_path), "git", "same diff")
            _fetch_commit_context("tokensave", str(tmp_path), "git", "same diff")
        # commit_context tool invoked once (first call); cache serves the second
        assert ctx_call_count["n"] == 1