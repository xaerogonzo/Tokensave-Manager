# cursor_transcripts fixtures

Captured from real headless `agent -p` runs, CLI 2026.10.22-27dcf5f, 2026-10-31,
from `~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl`.

- `plain.jsonl`: one question, one answer.
- `tool_use.jsonl`: a run that called an MCP tool (`GetDynamicTools` then
  `CallDynamicTool` in assistant records) before answering.

Shape: `{"role": "user"|"assistant", "message": {"content": [blocks]}}` plus
`{"type": "turn_ended", "status": ...}`. The user's text is wrapped in
`<timestamp>...</timestamp>` and `<user_query>...</user_query>`. Only these two
transcripts were captured; a run with attachments, rules or subagents may carry
other shapes. Replace with a fresh capture if Cursor changes the format.
