<!--
STATUS: FILED 2026-08-19 — https://github.com/aovestdipaperino/tokensave/issues/421
Found against tokensave 7.x on Windows 11 while cleaning up the git worktrees
Claude Code creates per task.

This file is the source-of-truth draft. What goes out is this document minus
this comment block, the `# ` title line (which becomes the issue title), and
the redundant **Repo:** line.

Anonymisation pass applied: the one real third-party project path in the
process listing was genericised to `D:\Projects\<other-project>`. Remaining
paths are this repo's own worktree names; PIDs are real but identify nothing.
Re-check before filing.
-->

# Upstream issue — `tokensave serve` locks `tokensave.db` with no way to identify or stop it

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `tokensave serve` holds an exclusive lock on `.tokensave/tokensave.db` and provides no way to tell which project a running server serves
**Type:** Bug / missing capability — platform (Windows), regression in scope from v6.0.0
**Status:** Filed 2026-08-19 as issue 421.

---

## Summary

Deleting a directory that contains a `.tokensave/` index fails on Windows
while any `tokensave serve` process has that index open:

```
Remove-Item: The process cannot access the file
'...\worktrees\strange-shaw-0b829e\.tokensave\tokensave.db'
because it is being used by another process.
```

The obvious next step — work out which server to stop — has no supported
answer. `serve` does not report the project it serves, and most instances
carry no project in their command line:

```
PID 44224: tokensave.exe serve -p "D:\Projects\<other-project>"
PID 39308: tokensave.exe serve -p "D:\Projects\<other-project>"
PID 18012: tokensave.exe serve
PID 46000: tokensave.exe serve
PID 35836: tokensave.exe serve
PID 24472: tokensave.exe serve
PID 46640: tokensave.exe serve
```

Seven servers were running; five had no project in argv. Nothing in the CLI
enumerates them or maps one to an index.

## Why it happens

This is a scope regression rather than a new bug. Before v6.0.0 the
long-lived, index-holding process was the daemon, and the daemon at least
had `tokensave daemon --status`, which printed its PID (see the archived
`tokensave-daemon-stop-windows.md`, which is marked MOOT on the grounds that
"the daemon was removed entirely in v6.0.0; file-watching now lives inside
the MCP server").

File-watching did move into the MCP server — and the lock-holding moved with
it. But `serve` inherited none of the daemon's introspection: no status, no
PID file, no stop, and no project in argv when launched by an MCP client
that supplies the project some other way. So the v6.0.0 change removed a
partly-broken stop path and replaced it with no path at all.

The lock itself looks like ordinary SQLite WAL behaviour — the index
directory carries `tokensave.db`, `tokensave.db-wal` and `tokensave.db-shm`
while a server has it open — so the handle is presumably held for the life of
the process regardless of idleness.

## Proposed fix

Any one of these would resolve it; the first is the cheapest:

1. **Write a PID/lock file into `.tokensave/`** naming the serving process
   (this is what the daemon effectively offered via `--status`). It makes the
   index → process direction answerable by reading a file, which is what a
   wrapper or a user deleting a checkout actually needs.
2. **Enumerate running servers from the CLI** — e.g. `tokensave serve --list`
   or a `servers` section in `tokensave status`, printing PID and project
   path per instance. Comparable to `codegraph daemon`'s listing output.
3. **Release the DB handle when idle**, or open it in a mode that does not
   block deletion of the containing directory.

## Why it matters

Deleting a checkout is routine, not exotic: Claude Code creates a git
worktree per task, each worktree gets indexed, and cleaning one up afterwards
runs straight into this. `git worktree remove` deregisters the worktree even
when the file delete fails, so the user is left with a half-state — pruned
git metadata, directory still on disk — and no supported way to find the
holder.

It also blocks wrappers from offering a fix. TokenSave Manager shipped a
daemon list/stop UI for exactly this class of problem, and deleted it in
v6.0.0 when the daemon went away (~162 lines). It cannot rebuild the
equivalent for `serve`, because there is no supported way to enumerate
servers or learn which index each one holds. Its sibling feature for
CodeGraph works precisely because `codegraph daemon` lists PID and path.

One aggravating factor worth knowing: MCP clients restart their servers, so
"stop all tokensave processes" does not converge — new PIDs appear while old
ones are being killed. Identifying the single holder is not a nicety; it is
the only approach that terminates.

## Workaround in use today

Correlate the SQLite sidecar's timestamp with process start times, since
SQLite creates `-shm` when it opens the DB in WAL mode:

```powershell
# the holder's StartTime matches the -shm LastWriteTime, typically to the second
Get-Item '<project>\.tokensave\tokensave.db-shm' | Select-Object LastWriteTime
Get-Process tokensave | Select-Object Id, StartTime
```

This identified the correct PID on the first try in the case above, and
killing it released the lock. It is inference from an implementation detail,
not a contract, and it fails outright when two servers start in the same
second. Sysinternals `handle.exe -a tokensave.db` answers it properly, but
requires a separate download and elevation.

## Environment

- tokensave 7.x, Windows 11
- Servers launched as MCP servers by Claude Code (not started by hand)
- Index directories inside git worktrees under a parent repo

## Author note

Filed by a TokenSave Manager user. Strip any proprietary code paths from
repros before submitting.
