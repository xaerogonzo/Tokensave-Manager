<!--
STATUS: FILED 2026-08-12 — https://github.com/aovestdipaperino/tokensave/issues/389
Found against tokensave v7.9.0. Still OPEN upstream; issue 389 is registered
in docs/tracked-issues.json, so the integration check reports its state.

This file is the source-of-truth draft the filed issue was built from. What
went out is this document minus this comment block, the `# ` title line
(which became the issue title), and the redundant **Repo:** line. Paths were
already generic, so no anonymisation pass was needed.

When it is fixed upstream, change the STATUS line above to
"STATUS: FIXED in tokensave vX.Y.Z" so the checker stops flagging it.
-->

# `hook-pre-tool-use` implements Glob coverage (#294) but the Claude Code installer writes a matcher that excludes `Glob`

**Repo:** https://github.com/aovestdipaperino/tokensave
**Type:** Bug — feature unreachable as installed
**Affects:** v7.9.0 (the release that added #294)

## Summary

v7.9.0 extended `hook-pre-tool-use` to cover path-shaped discovery, including
Claude Code's `Glob` tool (#294). The handler side landed: the binary carries
the classifier machinery. What did not land is the **matcher** — the regex of
tool names Claude Code uses to decide which calls to route through a
`PreToolUse` hook at all.

The `PreToolUse` entry in `~/.claude/settings.json` on a v7.9.0 install
reads:

```json
"PreToolUse": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "/path/to/tokensave",
        "args": ["hook-pre-tool-use"]
      }
    ],
    "matcher": "Agent|Grep|Bash"
  }
]
```

`Glob` is not in that alternation, so Claude Code never invokes the hook for a
`Glob` call, and the hook's Glob branch is unreachable. The `find -name` and
`fd --extension` halves of #294 are unaffected — those arrive as `Bash` calls,
which the matcher does cover. Only the `Glob` tool redirect is dead.

## Why it is easy to miss

Both of the commands a user would run to check report success:

- `tokensave doctor` prints `✔ PreToolUse hook installed`. That check verifies
  the hook entry exists, that the binary resolves, and that the subcommand is
  right — it never inspects whether the matcher covers the tools the hook now
  handles.
- `tokensave install --agent claude` "succeeds" and rewrites the same matcher,
  so reinstalling does not repair it.

There is no user-visible signal. A `Glob` call simply runs unredirected, with
no savings and no explanation — the same silent-no-op shape #294 was written
to eliminate for `find`/`fd`.

## Reproduction

On a machine with tokensave v7.9.0 and Claude Code:

1. Run `tokensave doctor`. It reports `✔ PreToolUse hook installed`.
2. Inspect `~/.claude/settings.json` → `hooks.PreToolUse[0].matcher`.
3. Observed: `Agent|Grep|Bash`, unchanged by doctor. Expected: a value
   including `Glob`.

The install path was deliberately not re-run here, to avoid clobbering the
local workaround below. The binary evidence indicates it writes the same
value — the only pipe-separated matcher literal naming these tools is
`Agent|Grep|Bash`.

Confirming the handler exists but the matcher does not reach it — the only
pipe-separated matcher literal in the binary that mentions these tools is
`Agent|Grep|Bash`, while Glob-handling symbols are clearly present:

```
$ grep -a -o -E "[A-Za-z]+(\|[A-Za-z]+){1,6}" tokensave | grep -E "Glob|Grep|Bash|Agent" | sort -u
Agent|Grep|Bash          # <- the matcher that gets written; no Glob
Execute|Grep|Task        # (other agents' matchers)
Grep|Shell

$ grep -a -o -E ".{25}Glob.{25}" tokensave | sort -u
...readgrepglobReadGrepGloboutline / read / nodesear...
...GlobOptionscase_insensitive...
...GlobUnmatchedIgnore...
```

## Suggested fix

1. Add `Glob` to the matcher the Claude Code installer writes
   (`Agent|Glob|Grep|Bash`).
2. **Repair existing installs**, not just new ones. Every user who installed
   before v7.9.0 has the old matcher, and neither `install` nor `doctor`
   currently updates it.
3. Consider having `doctor` validate matcher *coverage* rather than matcher
   *presence*, comparing the installed matcher against the set of tools the
   running binary's hook actually handles. This class of drift — the hook
   learns a new tool while existing settings keep the old matcher — will recur
   on every future coverage extension. It is the same shape as the v6.0.0
   hook-quoting repair (#81), which `doctor` does auto-fix today.

## Workaround

Hand-edit `hooks.PreToolUse[0].matcher` in `~/.claude/settings.json` to
`Agent|Glob|Grep|Bash`. Applied locally and confirmed to leave the rest of
the hook config untouched. `TOKENSAVE_DISABLE_GREP_HOOK=1` still opts out as
documented.

## Environment

- tokensave 7.9.0 (Windows x86_64)
- Windows 11
- Claude Code, global install, MCP server registered in `~/.claude.json`
- Hook entries in args-array shape (post-#81), binary path resolves correctly

## Author note

Found from TokenSave Manager while auditing the v7.8.1 → v7.9.0 upgrade for
snippet and integration coverage.
