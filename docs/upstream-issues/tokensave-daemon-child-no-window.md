# Upstream issue draft — Daemon-spawned re-index workers flash cmd windows on Windows

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** Daemon's re-index worker subprocesses don't pass `CREATE_NO_WINDOW` on Windows — every file save flashes a console
**Type:** Bug — Windows polish
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

On Windows, every time a file changes in a project the `tokensave daemon` is watching, the daemon spawns a short-lived child process (presumably to re-extract the changed file into the graph DB). That child process opens a **visible console window** for the few hundred milliseconds it lives, then closes.

In an active editing session this means a cmd window flashes onto the desktop on **every save**. With autosave on, that can be hundreds of window flashes per hour. It's a serious flow-breaker — the windows steal focus, briefly tab over the active editor, and pile up in the taskbar.

The user cannot meaningfully use the daemon on Windows without this happening, which leads to people killing the daemon and losing live-watch incremental indexing (falling back to whole-project re-indexes when the MCP server queries the graph).

## Reproduction

1. Windows 10 / 11
2. `tokensave init` in any reasonably-sized project
3. `tokensave daemon` (started ad-hoc, no autostart)
4. Open any tracked source file in an editor and save it (or `echo // > some_file.rs && touch some_file.rs` from a loop)
5. Observe a cmd window flash on every save

## Root cause (suspected)

The daemon's worker spawn path likely uses `std::process::Command::new(...).spawn()` or `tokio::process::Command::new(...).spawn()` without setting Windows-specific creation flags. On Windows, child processes created from a console-attached parent inherit a console; child processes created from a GUI-attached parent get a new one allocated. Either way, without `CREATE_NO_WINDOW` (`0x08000000`) in the creation flags, the child gets a visible console window.

The MCP `serve` path doesn't have this problem because the manager spawns it with `DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP + DEVNULL` on all standard streams, suppressing the console. The daemon's *own* console is also suppressed (presumably the same flags). But the workers the daemon spawns under the hood don't inherit that suppression.

## Proposed fix

On the Windows-target codepath that spawns daemon worker subprocesses, set:

```rust
#[cfg(windows)]
use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

let mut cmd = Command::new(worker_exe);
#[cfg(windows)]
cmd.creation_flags(CREATE_NO_WINDOW);
```

Or, if the workers are in-process tasks rather than separate processes, identify whatever IS being shelled out per file change (anti-virus? sandboxing? secondary parser?) and apply the same flag there.

## Why it matters

- The cmd-window flashes are by far the most-cited Windows UX complaint from TokenSave Manager users. It dominates first impressions of the tool on Windows.
- It makes the daemon effectively unusable for users with autosave editors (VS Code's default, JetBrains, etc.).
- The MCP-`serve` path already gets this right (no window), so the fix should be a one-line creation-flag addition in the daemon's worker-spawn site.

## Workaround in use today

There isn't one from the manager side — this has to be fixed in tokensave. The manager's only mitigation is to make the **Stop daemon** menu action work reliably (see the separate `tokensave-daemon-stop-windows.md` upstream issue) so users who can't tolerate the flashing can at least turn the daemon off cleanly.

## Environment

- tokensave 5.x
- Windows 11 (also believed to affect Windows 10)
- Any editor with autosave

## Author note

Filed by a TokenSave Manager user. Strip any proprietary code paths from repros before submitting.
