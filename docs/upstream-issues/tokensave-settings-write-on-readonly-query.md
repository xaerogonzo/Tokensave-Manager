<!--
STATUS: FILED 2026-08-19 — https://github.com/aovestdipaperino/tokensave/issues/419
Found against tokensave v7.10.0 on Windows 11, first CLI invocation after an
in-place 7.9.0 → 7.10.0 upgrade.

This file is the source-of-truth draft the filed issue was built from. What
goes out is this document minus this comment block, the `# ` title line
(which becomes the issue title), and the redundant **Repo:** line.

Anonymisation pass applied: real paths were `C:\Users\<user>\.claude\` and a
binary under a directory with spaces on `D:`; both genericised below. No other
machine-identifying content is present.

IMPORTANT — honesty note for future readers: this did NOT reproduce on
subsequent invocations of the same command. The report says so explicitly.
Do not let a later summary of this file drop that caveat.

When it is fixed upstream, change the STATUS line above to
"STATUS: FIXED in tokensave vX.Y.Z" so the checker stops flagging it.
-->

# `tokensave gitignore` in its read-only "show current setting" form rewrites `~/.claude/settings.json`

**Repo:** https://github.com/aovestdipaperino/tokensave
**Type:** Bug — read-only query performs a write to the agent's settings file
**Affects:** observed on v7.10.0 (Windows 11); trigger not fully isolated, see Reproduction

## Summary

`tokensave gitignore` with no ACTION argument is documented as a query:

```
Arguments:
  [ACTION]  "on" to enable, "off" to disable, omit to show current setting
```

Run in that form, it emitted

```
✔ Wrote C:\Users\<user>\.claude\settings.json
gitignore: off
```

and did in fact rewrite `~/.claude/settings.json`, creating a
`settings.json.bak` beside it. The written content was **byte-identical** to
what was already there, so nothing was lost or changed — but a command whose
documented job is to print one line of state should not be writing the host
agent's settings file at all.

## Why this matters even though the content was identical

`~/.claude/settings.json` is agent-control configuration: permissions, hooks,
and the MCP wiring that governs what the agent is allowed to do. Writing it is
not a neutral act.

- A user running a **query** has no reason to expect a write, so they will not
  have quiesced concurrent editors, and will not think to check afterwards.
- The write races anything else touching the file — the agent itself, an
  editor with the file open, another `tokensave` process.
- An interruption mid-write (crash, power loss, full disk) turns a no-op into a
  truncated settings file. The `.bak` mitigates but does not remove this.
- Tooling that watches this file for tampering sees an unexplained
  modification, attributable to a command that reads.

## Reproduction — and the honest limits of it

Observed once, on the **first CLI invocation of the freshly-upgraded 7.10.0
binary** in a new shell:

```
$ tokensave gitignore
✔ Wrote C:\Users\<user>\.claude\settings.json
gitignore: off
```

Verified at the time:

- `settings.json` mtime advanced;
- `settings.json.bak` was newly created alongside it;
- `diff` and `md5sum` between the two: **identical** (`f9f7e5eb…`).

It has **not** reproduced since. A later run of the identical command in the
same session left mtime and md5 untouched and printed only `gitignore: on`,
with no `✔ Wrote` line.

So the trigger is conditional, not per-invocation. The most likely shape is a
one-time post-upgrade settings refresh that any subcommand can trip, and which
happened to be tripped by `gitignore`. I could not confirm that from the
outside: neither `~/.tokensave/config.toml` nor `~/.tokensave/state.toml`
carries a visible "settings written for version X" stamp, and I did not want to
mutate the installed binary to force the condition.

If that guess is right, the reproduction from a clean state would be:

1. have a working Claude Code integration installed from version *N*;
2. upgrade the binary in place to version *N+1*;
3. run any read-only subcommand — e.g. `tokensave gitignore`.

## Expected

A read-only subcommand does not write `~/.claude/settings.json`.

If a post-upgrade settings refresh is genuinely needed, it belongs in the
commands whose job is to change installation state — `install`, `reinstall`,
`doctor` — where the user has asked for repair and a write is unsurprising. If
it must be able to happen from anywhere, it should say what it is doing and
why, e.g. `✔ Refreshed settings for v7.10.0 (was v7.9.0)`, rather than a bare
`✔ Wrote <path>` from a query.

A cheap partial fix independent of where the refresh lives: **skip the write
when the rendered content matches what is already on disk.** In this instance
the bytes were identical, so the write, the `.bak`, and the message were all
avoidable.

## Environment

- tokensave 7.10.0 (upgraded in place from 7.9.0)
- Windows 11, Git Bash
- Claude Code integration installed; hooks and MCP server registered
- Encountered while investigating an unrelated indexing question, so the
  settings file was not otherwise in play
