# Windows ConPTY — why the Manager doesn't drive interactive prompts

**Status:** investigated and abandoned 2026-08-13. Implementation removed; this
is the record so nobody re-derives it.

## Why we wanted it

`tokensave doctor` only offers its stale-entry purge prompt when `isatty()` is
true. Piping `y` into a pipe-backed subprocess does nothing — the prompt never
appears. A Windows pseudoconsole (ConPTY, Win10 1809+) *should* let the Manager
give tokensave a real TTY and answer the prompt itself, with no dependency
(the API is reachable straight from `ctypes.WinDLL("kernel32")`).

A full implementation was written and is preserved in git at commit `0536f89`
(`src/helpers/conpty.py`, 517 lines) if anyone wants to revisit it.

## Why it was abandoned

**The child attaches to the pseudoconsole but its standard handles still
resolve to the inherited console**, so its output never reaches the PTY pipe.

Every call reports success:

```
CreatePipe(in)                      rc=1  GetLastError=0
CreatePipe(out)                     rc=1  GetLastError=0
CreatePseudoConsole                 hr=0x00000000, valid HPCON
InitializeProcThreadAttributeList   rc=1  (size 48)
UpdateProcThreadAttribute(PSEUDO)   rc=1
CreateProcessW(EXTENDED_STARTUPINFO_PRESENT)
                                    rc=1  GetLastError=0
captured: 86 bytes — the child's stdout ABSENT
```

Those 86 bytes are the host's init frame, and they include
`ESC]0;C:\windows\SYSTEM32\cmd.exe BEL`. **That title sequence proves the attach
itself worked** — the console association happens; only the std-handle
resolution doesn't follow. The failure is narrower than "the attribute was
ignored".

Measured on Windows 11 build 26200, CPython 3.13.12.

### Ruled out by direct experiment

| Hypothesis | Result |
|---|---|
| Struct layout wrong | `STARTUPINFOW`=104, `STARTUPINFOEXW`=112 — correct |
| Attribute-list misalignment | address % 8 == 0 |
| `lpAttributeList` via `cast` vs `addressof` | identical failure |
| `bInheritHandles` TRUE/FALSE | no difference |
| Pipe handles marked inheritable | no difference |
| Renderer hadn't flushed | 0 / 0.3 / 1.0 / 2.5 s waits — all 86 bytes |
| Parent console being inherited | tested with `GetConsoleWindow()`==0 — still fails |
| `COORD` marshalling (4-byte struct by value) | struct *and* packed-DWORD forms both fail |

### The one configuration that captures output — and why it's useless

`STARTF_USESTDHANDLES` aimed at the pipes, with `bInheritHandles=TRUE`, does
capture the child's output. But std handles are then pipes, so `isatty()` is
**false** — measured against real tokensave, it lists the stale entries and
never prompts. That defeats the entire purpose.

## What we do instead

The purge hands off to a real terminal (`cmd.exe /k tokensave doctor`) and the
Manager **verifies the result** with a follow-up scan rather than trusting the
mechanism: `verified` / `partial` / `no_change` / `unverified`. See
`DoctorController.purge_stale` and `verify_purge`.

This is arguably the better design regardless of ConPTY. A launched terminal —
even one that exits cleanly — proves nothing about what the user did in it, so
the post-operation scan is the only honest signal. ConPTY would have removed a
manual step; it would not have made the result any more trustworthy.

## If you revisit this

Start from the title-sequence finding above: the attach works, so the question
is specifically why the child's `GetStdHandle(STD_OUTPUT_HANDLE)` doesn't
resolve to the new console. Compare against a known-good C implementation
(Microsoft's `EchoCon` sample) before rewriting the ctypes layer — the bug is
unlikely to be in the marshalling, which was checked exhaustively.
