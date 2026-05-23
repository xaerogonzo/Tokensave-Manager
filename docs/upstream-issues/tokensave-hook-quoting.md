<!--
Draft issue body for tokensave upstream — file at:
  https://github.com/aovestdipaperino/tokensave/issues/new

This file is not the issue itself — it's the source-of-truth draft so we
can iterate before publishing. Once filed, paste the resulting issue URL
into docs/MCP_INTEGRATION_GOTCHAS.md (search for "Upstream issue:") so
future readers can find both.

Before filing: replace any "/Your Path Here/" examples below with the
exact path you want public if you'd like to use a real one. The
generic example given here reproduces the bug equally well.
-->

# `tokensave install --agent claude` writes settings.json hook commands without quoting paths-with-spaces; doctor → install loop ensues

## Summary

When `tokensave install --agent claude` runs against an installation path
that contains spaces, the hook entries it writes to
`~/.claude/settings.json` use a single-string `command` field with the
exe path and the subcommand concatenated by a space — e.g.

```json
{
  "type": "command",
  "command": "C:/Path With Spaces/tokensave.exe hook-pre-tool-use"
}
```

Claude Code splits this string by whitespace before exec, so:

- token 0 (executable): `C:/Path` (or `D:/Claude` if the path starts that way)
- token 1 (first arg, treated as the subcommand): `With` (or `Co` etc.)
- everything else is silently lost.

`tokensave doctor` correctly detects the mismatch:

```
✘ PreToolUse hook has wrong subcommand: "With" (expected "hook-pre-tool-use")
✘ UserPromptSubmit hook has wrong subcommand: "With" (expected "hook-prompt-submit")
✘ Stop hook has wrong subcommand: "With" (expected "hook-stop")
✔ Removed PreToolUse hook
✔ Removed UserPromptSubmit hook
✔ Removed Stop hook
✔ Auto-repaired hook(s)
```

…but `Auto-repaired` only **removes** the broken hooks, so the very
next `tokensave install` (or doctor's own auto-repair loop) re-adds
them in the same broken shape. Three observed doctor runs in
succession alternated:

1. Doctor sees `NOT installed` → auto-repair re-installs them (broken).
2. Doctor sees `wrong subcommand: "<token1>"` → auto-repair removes them.
3. Doctor sees `NOT installed` → auto-repair re-installs them (broken).
4. … and so on, indefinitely.

## Reproduction

1. Install tokensave to a path containing a space:
   `C:\Path With Spaces\tokensave.exe`
2. Run `tokensave install --agent claude`
3. Open `~/.claude/settings.json` — every tokensave hook entry's
   `command` field is the literal string `"C:/Path With Spaces/tokensave.exe hook-<event>"`.
4. Run `tokensave doctor`. It reports `wrong subcommand: "With"` for
   each hook and removes them.
5. Run `tokensave install --agent claude` again. The hooks return,
   broken in exactly the same way.

Observed on tokensave **v5.1.2** on Windows. The install location
itself doesn't need to be unusual — common Windows install dirs like
`C:\Program Files\Tools\tokensave.exe` trigger it identically.

## Proposed fix

Two options. **(b) is cleaner** because it avoids the shell-quoting
problem at the source instead of papering over it.

### (a) Quote the exe path inside the command string

Write the command field with the exe path explicitly quoted, e.g.

```json
{
  "type": "command",
  "command": "\"C:/Path With Spaces/tokensave.exe\" hook-pre-tool-use"
}
```

Claude Code's whitespace-splitter would then recognise the quoted
region as a single token.

### (b) Use Claude Code's `args` array shape (recommended)

Claude Code's hook schema accepts an `args` array, which sidesteps
quoting entirely:

```json
{
  "type": "command",
  "command": "C:/Path With Spaces/tokensave.exe",
  "args": ["hook-pre-tool-use"]
}
```

This is independent of shell rules and works regardless of where
tokensave is installed. It's also closer to how Claude Code's MCP
config already represents commands with args (see the modern
`{"command": "...", "args": [...]}` shape used by `tokensave install`
for the MCP server itself in the same file).

## Why this matters

Beyond the cosmetic doctor-warning cycle, the hooks themselves don't
fire — so any tokensave feature that relies on session-event tracking
(per-tool-call metrics, the global token-savings counter, etc.) gets
zero data from this user's sessions. Doctor's auto-removal then makes
the loss invisible — the user sees `✔ Auto-repaired hook(s)` and
assumes everything's fine.

## Suggested test

After fixing, a `tokensave install --agent claude` followed by `tokensave
doctor` on a path-with-spaces install should produce:

```
✔ PreToolUse hook installed
✔ UserPromptSubmit hook installed
✔ Stop hook installed
```

Stable across repeated doctor runs.

## Environment

- tokensave 5.1.2
- Windows (NTFS, paths permitted to contain spaces)
- Claude Code (the version Claude Desktop bundles —
  `C:\Users\<user>\AppData\Roaming\Claude\claude-code\2.1.149\claude.exe`
  was the relevant binary in the observed case)
