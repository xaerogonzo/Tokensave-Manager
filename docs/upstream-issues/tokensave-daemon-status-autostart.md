# Upstream issue draft — `tokensave daemon --status` should surface autostart state

**Repo:** https://github.com/aovestdipaperino/tokensave
**Suggested title:** `tokensave daemon --status: include autostart enabled/disabled in output`
**Type:** Feature request / CLI enhancement
**Status:** Draft — review and strip any proprietary code before filing.

---

## Summary

`tokensave daemon --status` currently reports only the running state and PID:

```
$ tokensave daemon --status
tokensave daemon is running (PID: 15344)
```

It does NOT report whether autostart is installed. Callers (GUI wrappers, scripts, monitoring) have no way to tell from one CLI call whether `tokensave daemon --enable-autostart` has been run, which means:

- A wrapper UI cannot accurately label its right-click menu item as **Install autostart** vs **Disable autostart** — it has to guess (usually defaulting to "Install autostart", which is misleading once autostart is already installed).
- Idempotent re-installs work fine but produce no signal to the user that the action was a no-op.
- Scripted setup flows can't verify "did my install-autostart step actually take effect?" without poking at platform-specific implementation (Windows scheduled-task XML, macOS launchd plist, Linux systemd unit list).

## Proposed shape

Add a second line to `--status` output:

```
$ tokensave daemon --status
tokensave daemon is running (PID: 15344)
autostart: enabled
```

Or when not installed:

```
$ tokensave daemon --status
tokensave daemon is not running
autostart: disabled
```

The literal token `autostart: enabled` / `autostart: disabled` is convenient because it's trivial to grep, language-neutral, and matches the casing of the existing line. Bonus points for surfacing the source on each platform (e.g. `autostart: enabled (scheduled task: TokenSaveDaemon)` on Windows) but the basic boolean is the part that matters.

## Why this matters

1. **GUI wrappers can render the right menu state.** TokenSave Manager (and presumably any future wrapper) has a daemon-status footer with a context menu offering install / start / stop / disable. Without autostart in `--status`, the menu cannot toggle between **Install autostart** and **Disable autostart** based on actual state — it has to either always show one (misleading) or maintain its own side-channel cache (fragile across reinstalls).

2. **One source of truth.** Today, to answer "is autostart installed?", a wrapper would have to shell out to platform-specific tooling:
   - Windows: `schtasks /Query /TN <task-name>`
   - macOS: `launchctl list | grep <label>`
   - Linux: `systemctl --user is-enabled <unit>`
   That replicates logic tokensave already has internally.

3. **Idempotent flows want confirmation.** `--enable-autostart` is already idempotent (re-running it doesn't break anything), but callers currently can't verify "yes, autostart is now installed" without trusting the exit code blindly.

## Output channel note

While filing this, please also consider whether the existing status line should go to **stdout** rather than stderr.

Today (verified against the Windows build of tokensave 5.x), `tokensave daemon --status` writes its status string to stderr even on exit code 0, which is unusual for a `--status` command. Many subprocess wrappers (including the one shipped in TokenSave Manager) initially read only stdout and silently report "daemon stopped" when the daemon is in fact running. Moving the status line to stdout (and reserving stderr for errors / diagnostics) would match POSIX convention and avoid this pitfall.

If changing the stream is too invasive for backward compatibility, at minimum documenting it explicitly in `--help` / the manpage would help.

## Workaround in use today

TokenSave Manager parses both stdout AND stderr when reading `--status`, treats any "running"/"not running" tokens as authoritative, and assumes autostart is always disabled (so the menu always shows **Install autostart**). Clicking re-install when autostart is already installed is a harmless no-op on Windows.

## Environment

- tokensave 5.1.2
- Windows 11 (also relevant for macOS/Linux per the same `--status` API)

## Author note

Filed by a TokenSave Manager user. Project: https://github.com/(your-fork-or-source)
