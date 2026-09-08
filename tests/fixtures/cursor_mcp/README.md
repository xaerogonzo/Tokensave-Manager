# Cursor MCP fixtures

Realistic `~/.cursor/mcp.json` and `<project>/.cursor/mcp.json` documents, used
by `tests/test_cursor_mcp.py`.

These are hand-written rather than captured from a live install: Cursor is not
installed on the development machine this support was built on, so every Cursor
path in the manager is **fixture-verified, not live-verified**. The shapes come
from Cursor's published MCP documentation (top-level `mcpServers`, per-server
`command` / `args` / `env` / `type`), which matches Claude Code's.

If you have Cursor installed, replacing these with real captured files — and
saying so here — is a strict improvement.
