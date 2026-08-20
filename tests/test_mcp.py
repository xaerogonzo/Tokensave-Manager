"""tests/test_mcp.py — detecting how agents are wired to codegraph.

Split out of the former ``tests/smoke_test.py``. Reads the agent's own MCP
config to answer "is this already wired?", which is what stops the manager
nagging about an integration that is already in place.
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

from helpers.mcp import _claude_code_mcp_has_codegraph      # noqa: E402


class TestClaudeCodeMcpHasCodegraph(unittest.TestCase):

    def _write_cfg(self, data: dict, tmpdir: str) -> str:
        path = os.path.join(tmpdir, ".claude.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_canonical_entry_detected_by_key(self):
        cfg = {"mcpServers": {"codegraph": {
            "type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, key = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)
        self.assertEqual(key, "codegraph")

    def test_namespaced_key_detected(self):
        cfg = {"mcpServers": {"mcp-codegraph": {
            "type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)

    def test_detected_by_command_field(self):
        cfg = {"mcpServers": {"my-custom-key": {
            "command": "codegraph", "args": ["serve", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)

    def test_no_codegraph_entry_returns_false(self):
        cfg = {"mcpServers": {"tokensave": {
            "type": "stdio", "command": "tokensave", "args": ["server"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertFalse(wired)

    def test_empty_mcp_servers_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(
                self._write_cfg({"mcpServers": {}}, d))
        self.assertFalse(wired)

    def test_missing_file_returns_false(self):
        wired, _ = _claude_code_mcp_has_codegraph("/nonexistent/dir/.claude.json")
        self.assertFalse(wired)

    def test_malformed_json_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            wired, _ = _claude_code_mcp_has_codegraph(path)
        self.assertFalse(wired)

    def test_codegraph_in_args_detected(self):
        # Variant: key is generic but args include "codegraph"
        cfg = {"mcpServers": {"generic-mcp": {
            "command": "node", "args": ["run", "codegraph", "--mcp"],
        }}}
        with tempfile.TemporaryDirectory() as d:
            wired, _ = _claude_code_mcp_has_codegraph(self._write_cfg(cfg, d))
        self.assertTrue(wired)

