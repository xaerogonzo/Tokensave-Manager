# Upstream issue — `tokensave daemon --stop` fails on Windows

> **STATUS: MOOT — daemon removed entirely in tokensave v6.0.0.**
> File-watching now lives inside the MCP server. Manager daemon_cost.py
> daemon functions removed; _poll_daemon_status loop and daemon UI deleted.

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `tokensave daemon --stop` errors out on Windows when the daemon was started ad-hoc (no service installed)
**Type:** Bug — platform regression
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

On Windows, `tokensave daemon --stop` fails with a Windows service API error whenever the daemon was started by simply running `tokensave daemon` (the documented, non-autostart way). The user has no functional way to stop the daemon from the CLI on Windows.

Observed against tokensave 5.x on Windows 11:

```
> tokensave.exe daemon --status
tokensave daemon is running (PID: 15344)

> tokensave.exe daemon --stop
Error: config error: service error: failed to open service 'tokensave-daemon': IO error in winapi call
```

The daemon process (PID 15344) is **still alive** after this call — `--stop` did nothing.

## Why it happens

`--stop` appears to assume the daemon was installed as a Windows service (the `tokensave-daemon` named service it tries to open via the SCM). But on Windows, the daemon is normally started by spawning `tokensave daemon` as a detached process — there is no registered Windows service unless `--enable-autostart` was explicitly run (and even then it installs a Scheduled Task, not a service, so the service lookup would still fail).

So on Windows the stop path:
- Tries the service-stop code path unconditionally
- Errors when no `tokensave-daemon` service exists
- Never falls back to the simpler "find the daemon PID and signal it" path

Net effect: there is **no working way** to stop the daemon from the CLI on Windows. Users have to find the PID manually (via `--status`) and `taskkill /F /PID <pid>`.

## Proposed fix

Make `--stop` on Windows do whatever the POSIX path does: find the daemon process (the PID is already known — it's printed in `--status`, presumably backed by a lockfile or registry entry), and terminate it via `TerminateProcess`. The service code path should only run when autostart is actually installed; otherwise fall back to PID-based termination.

Pseudocode of the desired flow:

```
if windows_service_exists("tokensave-daemon"):
    stop_service("tokensave-daemon")
else:
    pid = read_daemon_pidfile()
    if pid is not None:
        TerminateProcess(pid)
    else:
        report "daemon not running"
```

## Why it matters

Wrappers that expose a **Stop daemon** action (TokenSave Manager has one in its daemon-status right-click menu) currently get a confusing error string back from `--stop` and have to implement their own PID-kill fallback to give users a working stop button. That fallback duplicates logic tokensave already has (reading the PID from `--status` output and calling `TerminateProcess`), and it's the kind of cross-platform polish that's best owned upstream.

It also means **autostart isn't really opt-in** in practice — once the daemon is running, it survives the spawning process (which is correct, per the `DETACHED_PROCESS` design), and `--stop` doesn't work, so unless the user knows to manually `taskkill`, the daemon keeps running forever.

## Workaround in use today

TokenSave Manager's `_stop_daemon` helper (`src/helpers/daemon_cost.py`):

1. Calls `tokensave daemon --stop` first (so when this bug is fixed upstream, the workaround silently drops away).
2. If that returns non-zero, or if `daemon --status` still reports running afterwards, parses the PID out of `--status` and calls `taskkill /F /PID <pid>` (Windows) / `os.kill(pid, SIGTERM)` then `SIGKILL` (POSIX).
3. Confirms via a second `--status` poll.

## Related (but separate) issue

The bug above is purely about `--stop` not working. There's a **separate** Windows-specific bug — daemon-spawned re-index worker subprocesses don't pass `CREATE_NO_WINDOW`, so every file change flashes a cmd window. Filed separately as `tokensave-daemon-child-no-window.md`.

## Environment

- tokensave 5.x
- Windows 11
- Daemon started via `tokensave daemon` (no autostart installed)

## Author note

Filed by a TokenSave Manager user. Strip any proprietary code paths from repros before submitting.
