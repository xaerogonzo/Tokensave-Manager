"""tests/test_install_tokensave.py — downloading and unpacking tokensave.

Split out of the former ``tests/smoke_test.py``. The Zip-Slip guard is the
one to read first: an archive entry escaping the extraction directory is the
difference between installing a tool and letting it write anywhere.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

# ── Make src/ importable from any working directory ───────────────────────────
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

from helpers.install_tokensave import (                     # noqa: E402
    _is_safe_zip_member,
    extract_tokensave_zip,
    is_manager_installed,
    latest_tokensave_release,
)


class TestIsSafeZipMember(unittest.TestCase):

    _EXTRACT = os.path.abspath("/tmp/extract_dir_smoke_test")

    def _safe(self, member):
        return _is_safe_zip_member(member, self._EXTRACT)

    # ── safe members ──────────────────────────────────────────────────────────
    def test_normal_subpath(self):        self.assertTrue(self._safe("src/helpers/foo.py"))
    def test_flat_filename(self):         self.assertTrue(self._safe("tokensave.exe"))
    def test_nested_subdir(self):         self.assertTrue(self._safe("bin/tokensave.exe"))
    def test_deeper_nesting(self):        self.assertTrue(self._safe("a/b/c/d.py"))

    # ── rejected members ──────────────────────────────────────────────────────
    def test_parent_traversal(self):
        self.assertFalse(self._safe("../etc/passwd"))

    def test_deep_parent_traversal(self):
        self.assertFalse(self._safe("a/../../etc/shadow"))

    def test_absolute_unix_path(self):
        self.assertFalse(self._safe("/absolute/path/file.txt"))

    def test_windows_drive_letter(self):
        # After replace("\\","/"), "C:\Windows" → "C:/Windows" → name[1] == ":"
        self.assertFalse(self._safe("C:\\Windows\\System32\\bad.dll"))

    def test_double_dot_only(self):
        self.assertFalse(self._safe(".."))

    def test_traversal_within_path(self):
        self.assertFalse(self._safe("safe/../../../escape"))


class TestExtractTokensaveZip(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="tsm_smoke_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_zip(self, members: dict) -> str:
        """Write a zip with ``{member_name: content_bytes}`` to a temp file."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        zip_path = os.path.join(self._tmpdir, "test.zip")
        with open(zip_path, "wb") as fh:
            fh.write(buf.getvalue())
        return zip_path

    def _write_malicious_zip(self, safe_name, evil_name) -> str:
        """Write a zip that contains both a safe member and a traversal member."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(safe_name, b"safe content")
            zf.writestr(evil_name, b"escape attempt")
        zip_path = os.path.join(self._tmpdir, "malicious.zip")
        with open(zip_path, "wb") as fh:
            fh.write(buf.getvalue())
        return zip_path

    def test_valid_flat_zip_extracts_exe(self):
        zip_path = self._write_zip({"tokensave.exe": b"fake binary content"})
        extract_dir = os.path.join(self._tmpdir, "out1")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "")
        self.assertTrue(exe_path.endswith("tokensave.exe"))
        self.assertTrue(os.path.isfile(exe_path))

    def test_valid_nested_zip_extracts_exe(self):
        zip_path = self._write_zip({
            "bin/tokensave.exe": b"binary",
            "bin/README.md": b"readme",
        })
        extract_dir = os.path.join(self._tmpdir, "out2")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "")
        self.assertTrue(exe_path.endswith("tokensave.exe"))

    def test_no_exe_in_archive_returns_error(self):
        zip_path = self._write_zip({"README.txt": b"docs", "LICENCE": b"MIT"})
        extract_dir = os.path.join(self._tmpdir, "out3")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(exe_path, "")
        self.assertEqual(err, "no_exe_in_archive")

    def test_zip_slip_traversal_aborts_cleanly(self):
        """Traversal member → zip_slip; safe member must NOT be extracted."""
        zip_path = self._write_malicious_zip("safe_file.txt", "../escape.txt")
        extract_dir = os.path.join(self._tmpdir, "out4")
        exe_path, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(exe_path, "")
        self.assertEqual(err, "zip_slip")
        # First-pass validation aborts before any write
        self.assertFalse(
            os.path.isfile(os.path.join(extract_dir, "safe_file.txt")),
            "No file must be extracted when zip_slip is detected",
        )

    def test_absolute_path_member_aborts(self):
        zip_path = self._write_malicious_zip("ok.txt", "/absolute/path/evil.txt")
        extract_dir = os.path.join(self._tmpdir, "out5")
        _, err = extract_tokensave_zip(zip_path, extract_dir)
        self.assertEqual(err, "zip_slip")


class TestIsManagerInstalled(unittest.TestCase):

    def _make_exe(self, directory, name="tokensave.exe"):
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write("fake binary")
        return path

    def test_exe_inside_manager_dir_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            exe = self._make_exe(d)
            with mock.patch("helpers.install_tokensave.manager_install_dir",
                            return_value=d):
                self.assertTrue(is_manager_installed(exe))

    def test_exe_outside_manager_dir_returns_false(self):
        with tempfile.TemporaryDirectory() as managed:
            with tempfile.TemporaryDirectory() as other:
                exe = self._make_exe(other)
                with mock.patch("helpers.install_tokensave.manager_install_dir",
                                return_value=managed):
                    self.assertFalse(is_manager_installed(exe))

    def test_lookalike_prefix_path_rejected(self):
        """C:\\TokenSaveManager-Lookalike\\exe must NOT pass via string prefix match.

        os.path.commonpath() correctly handles this — the common path would be
        the parent of both dirs, not the manager dir itself.
        """
        with tempfile.TemporaryDirectory() as d:
            sibling = d + "-Lookalike"
            os.makedirs(sibling, exist_ok=True)
            try:
                exe = self._make_exe(sibling)
                with mock.patch("helpers.install_tokensave.manager_install_dir",
                                return_value=d):
                    self.assertFalse(is_manager_installed(exe))
            finally:
                shutil.rmtree(sibling, ignore_errors=True)

    def test_empty_path_returns_false(self):
        self.assertFalse(is_manager_installed(""))

    def test_nonexistent_exe_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            ghost = os.path.join(d, "does_not_exist.exe")
            with mock.patch("helpers.install_tokensave.manager_install_dir",
                            return_value=d):
                self.assertFalse(is_manager_installed(ghost))


class TestLatestTokensaveRelease(unittest.TestCase):

    @staticmethod
    def _mock_urlopen(status=200, body=b""):
        """Return a context-manager-compatible mock for urllib.request.urlopen.

        MagicMock.__enter__() returns a NEW sub-mock by default, so resp.status
        would read the sub-mock's attribute, not ours. Fix: set
        __enter__.return_value = resp so `with urlopen(...) as r: r.status`
        reads the attributes we configured.
        """
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp   # critical: return self, not sub-mock
        return mock.patch("urllib.request.urlopen", return_value=resp)

    @staticmethod
    def _mock_urlopen_exc(exc):
        return mock.patch("urllib.request.urlopen", side_effect=exc)

    def test_rate_limit_403_returns_rate_limit_tag(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=403, msg="Forbidden",
                                     hdrs=None, fp=None)
        with self._mock_urlopen_exc(exc):
            version, url, err = latest_tokensave_release()
        self.assertIsNone(version)
        self.assertIsNone(url)
        self.assertEqual(err, "rate_limit",
                         "HTTP 403 must return the 'rate_limit' tag so caller "
                         "can show the actionable 'wait or install manually' message")

    def test_successful_response_parses_windows_asset(self):
        payload = json.dumps({
            "tag_name": "v6.1.0",
            "assets": [
                {"name": "tokensave-v6.1.0-x86_64-linux.tar.gz",
                 "browser_download_url": "https://example.com/linux.tar.gz"},
                {"name": "tokensave-v6.1.0-x86_64-windows.zip",
                 "browser_download_url": "https://example.com/windows.zip"},
            ],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            version, url, err = latest_tokensave_release()
        self.assertEqual(version, "6.1.0")
        self.assertIn("windows.zip", url)
        self.assertIsNone(err)

    def test_v_prefix_stripped_from_tag(self):
        payload = json.dumps({
            "tag_name": "v6.0.3",
            "assets": [{"name": "tokensave-v6.0.3-x86_64-windows.zip",
                        "browser_download_url": "https://example.com/w.zip"}],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            version, _, _ = latest_tokensave_release()
        self.assertEqual(version, "6.0.3", "'v' prefix must be stripped from tag_name")

    def test_no_windows_asset_returns_error(self):
        payload = json.dumps({
            "tag_name": "v6.1.0",
            "assets": [{"name": "tokensave-v6.1.0-x86_64-linux.tar.gz",
                        "browser_download_url": "https://example.com/l.tar.gz"}],
        }).encode()
        with self._mock_urlopen(status=200, body=payload):
            _, _, err = latest_tokensave_release()
        self.assertEqual(err, "no_windows_asset")

    def test_other_http_error_returns_http_tag(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=500, msg="Server Error",
                                     hdrs=None, fp=None)
        with self._mock_urlopen_exc(exc):
            _, _, err = latest_tokensave_release()
        self.assertEqual(err, "http_500")

    def test_malformed_json_returns_json_err(self):
        with self._mock_urlopen(status=200, body=b"{not valid json"):
            _, _, err = latest_tokensave_release()
        self.assertTrue(err.startswith("json_err"),
                        f"Expected 'json_err…' but got: {err!r}")

