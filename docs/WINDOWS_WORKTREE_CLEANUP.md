# Windows Worktree Cleanup — field manual

Why deleting a Claude Code worktree on Windows half-works, and what to do
about it. Written 2026-08-19 after a cleanup that took several rounds of
trial and error, none of which was written down anywhere.

The short version: **this is not one problem, it is four stacked**, and each
one masks the next. Knowing that up front is most of the fix.

---

## TL;DR

1. `git worktree remove` **deregisters the worktree even when the file
   delete fails.** The half-state is the normal outcome. Do not retry it.
2. There are **two different lock holders** with two different fixes. Read
   the path in the error message to tell them apart.
3. A session **cannot delete the worktree it is running in**, no matter what
   it tries. That one only clears on exit.
4. `tokensave serve` holds the index DB and **does not say which project it
   serves.** Correlate `tokensave.db-shm` mtime against process start time.
5. MCP servers **respawn**. "Kill them all" never converges. Kill the one.

---

## 1. `git worktree remove` half-succeeds

```
> git worktree remove ".claude/worktrees/<name>"
error: failed to delete '<path>': Permission denied
```

That looks like a total failure. It is not. Check afterwards:

```bash
git worktree list          # the worktree is GONE from the list
ls .git/worktrees          # metadata already pruned
```

The git side completed; only the directory remains. **Do not re-run the
command** — it has nothing left to do. What you have now is an orphan
directory whose `.git` file points at pruned metadata, so git commands run
*inside* it will fail in confusing ways. Treat it as a plain folder.

## 2. Two lock holders, two fixes

Read the path in the error, not just the message:

| Error names | Holder | Fix |
|---|---|---|
| `...\.tokensave\tokensave.db` | a `tokensave serve` process | stop that one process (§3) |
| the bare directory | a process whose **cwd** is that directory | exit that process |

The most common instance of the second case is the session doing the
cleanup. A Claude Code session re-anchors its shell working directory into
its own worktree after every command, so it can never delete its own
worktree — not even by `cd`-ing to `C:\` first. Nothing to debug; it clears
when the session ends.

**Useful signal:** after you stop the right tokensave process, the error
*changes* from naming `tokensave.db` to naming the directory. That shift is
how you know the DB lock actually released and you have moved on to a
different holder — not that nothing happened.

## 3. Finding which `tokensave serve` holds an index

`serve` does not report its project, and most instances carry none in argv:

```powershell
Get-CimInstance Win32_Process -Filter "Name LIKE '%tokensave%'" |
  ForEach-Object { "PID $($_.ProcessId): $($_.CommandLine)" }
# PID 44224: tokensave.exe serve -p "D:\Random Projects\Fortuna Lab"
# PID 18012: tokensave.exe serve      <- which index?
# PID 46000: tokensave.exe serve      <- which index?
```

SQLite creates the `-shm` sidecar when it opens the DB in WAL mode, so the
holder's **start time matches the `-shm` mtime**, in practice to the second:

```powershell
Get-Item '<project>\.tokensave\tokensave.db-shm' | Select-Object LastWriteTime
Get-Process tokensave | Select-Object Id, StartTime
```

Match the two, stop that PID, and the whole index (`db` + `-wal` + `-shm`)
deletes. This is inference from an implementation detail, not a contract —
it breaks if two servers start in the same second. The proper tool is
Sysinternals `handle.exe -a tokensave.db`, which names the owner directly;
PowerShell cannot enumerate file handles natively.

Filed upstream as
[`tokensave-serve-db-lock-unidentifiable.md`](upstream-issues/tokensave-serve-db-lock-unidentifiable.md).

## 4. The servers respawn

PIDs churn between two checks minutes apart — Claude Code restarts its MCP
servers. So any "stop every tokensave process" plan disrupts live sessions
*and* does not terminate. Identify the single holder and stop only that.

## 5. `ExitWorktree` probably does not apply

Claude Code's `ExitWorktree` tool only handles worktrees created by
`EnterWorktree` **in the same session**. A session that *started* inside a
worktree gets a clean no-op:

> No-op: there is no active EnterWorktree session to exit.

Don't reach for it first and conclude something is broken.

---

## Recommended order

1. `git worktree remove <path>` — expect it to deregister and maybe not delete.
2. `git worktree prune`, then `git worktree list` to confirm the git side.
3. Delete the branch if it is merged: `git branch -d <branch>` (use `-d`, never
   `-D` — you want git to refuse if something is unmerged).
4. Try deleting the directory. If it fails, read which path the error names.
5. If it named the index DB, find the holder (§3) and stop it.
6. If it named the directory, find whose cwd it is — often your own session.
   Leave it; it clears on exit.

## Before you start: check the base

Worth doing at the *start* of worktree work, since it is the other way this
bites. A worktree may be branched from a different base than the task
assumes — the tell is quoted line numbers not matching what is on disk.

```bash
git merge-base --is-ancestor <expected-commit> HEAD && echo "in history"
git rev-list --left-right --count HEAD...origin/<branch>
```

And re-run the second one **immediately before committing**, not just before
pushing: `origin/<branch>` can advance while you work, and anything you
*counted* from the tree (file totals, coverage figures) must then be
re-measured rather than adjusted by arithmetic.

## Planned Manager support

`docs/ROADMAP.md` Theme I — Worktree + daemon lifecycle proposes a tokensave
daemon list/stop mirroring the existing `CodegraphDaemonManagerDialog`
(Tool Manager → CodeGraph → "🔌 Manage daemons…"), plus a worktree cleanup
surface that reports which worktrees are lock-blocked and by what. Until
that ships, this document is the procedure.
