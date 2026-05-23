# MCP Integration Gotchas

A field manual for anyone touching `tokensave-wrapper.py`, the manager's
MCP-config editing flow, or trying to reintroduce live pin reloading. Written
2026-05-23 after a multi-hour debugging session that produced 5+ failed fixes
before landing the correct one. Each section is structured as **what we
tried → what went wrong → the lesson**.

---

## TL;DR — three things to know before touching this code

1. **Claude Desktop's MCP config file is NOT `%APPDATA%\Claude\claude_desktop_config.json` on Microsoft Store / UWP installs.** It's actually `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`. Both paths exist on disk simultaneously; Windows asymmetric file-path redirection makes them resolve to different physical files depending on whether the caller is in UWP context. Edit only the package-internal path.

2. **`subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)` with default `stdin/stdout/stderr=None` doesn't reliably proxy stdio under pythonw.exe.** Tokensave (a console child) never sees the MCP messages Claude Desktop is piping in. Pass `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` explicitly.

3. **Don't add `import threading` or daemon threads to the wrapper script.** It interacts badly with Windows stdio handling under pythonw.exe in subtle ways. Any live-reload feature must be implemented as an **out-of-process** mechanism, not inside the wrapper.

---

## The original goal

`★ Set as Active` in the manager should switch which project tokensave's MCP
server is serving — and ideally do so live, without requiring a Claude
Desktop / Claude Code restart.

Before the fix attempts, this was already partly working: `set as active`
wrote a pin file (`~/.tokensave/desktop-project.txt`) that the wrapper
read **once** at startup. So the pin worked, but only for the next
Claude session — current sessions were stuck on whichever project they
launched with.

The goal was: pin changes propagate within ~2 seconds to all running MCP
clients, no restart needed.

---

## What we tried (chronological)

### Attempt 1 — Pin-watcher daemon thread in the wrapper

**The plan**: add a background thread to `tokensave-wrapper.py` that polls
`~/.tokensave/desktop-project.txt` every 2 seconds. When the mtime
changes to a different valid project, terminate the current
`tokensave serve` child and respawn with the new `-p`.

**What we built**: ~150 lines added to a previously-12-line wrapper. A
`threading.Lock` synchronizing state between watcher and main thread. A
restartable outer `while True:` loop with a `_swap_requested` flag and
retry-on-port-busy backoff. Looked elegant on paper.

**What went wrong**: Claude Desktop banner showed *"Could not attach to
MCP server tokensave"*. Per-project `mcp-logs-tokensave/*.jsonl` files
revealed the smoking gun:

```json
{"debug":"Starting connection with timeout of 30000ms","sessionId":"..."}
{"debug":"Connection timeout triggered after 30040ms (limit: 30000ms)"}
{"error":"Connection failed: MCP server \"tokensave\" connection timed out after 30000ms"}
```

The wrapper was being spawned, AND it was spawning tokensave (we saw the
child process via `Get-CimInstance Win32_Process`), but MCP's initial
handshake never completed. tokensave wasn't receiving the `initialize`
message at all.

**The wrong lesson we drew**: "Threading must be incompatible with MCP
stdio." We reverted to a near-original wrapper that still had `import
threading` and a daemon thread, but with a simpler one-shot structure.
It also failed. We reverted again to the literal 12-line original. It
ALSO failed.

**The correct lesson (only learned later)**: the threading was a red
herring. The real bug was in stdio inheritance under pythonw.exe — a
separate issue that happened to surface around the same time we added
threading. See Attempt 5.

---

### Attempt 2 — Edit Claude Desktop's MCP config to point at the wrapper

**The plan**: modify `%APPDATA%\Claude\claude_desktop_config.json` so
Claude Desktop runs the wrapper instead of `tokensave.exe` directly.
Wrapper-routed → live pin reloading.

**What we built**: a one-line `shutil.copy2(...) + json.dump(...)` Bash
edit, replacing the legacy direct-serve entry:

```json
{"command": "tokensave.exe", "args": ["serve", "-p", "...KicomAI_Project"]}
```

with:

```json
{"command": "pythonw.exe", "args": ["...tokensave-wrapper.py"]}
```

**What went wrong (the first time)**: 90 seconds after we wrote the new
config, Claude Desktop **silently overwrote it** with its own cached
in-memory copy (which still had the old direct-serve entry). The mtime
on disk advanced. The fix lasted only as long as Desktop didn't fire
its "preferences save" mechanism.

**Lesson 1**: Claude Desktop reads `claude_desktop_config.json` at
startup, caches the entire file (including `mcpServers`) in memory,
then writes it back periodically when its own preferences change. Any
edit you make while Desktop is running gets clobbered within minutes.

**Mitigation**: detect running `claude.exe` processes via `tasklist` and
refuse to apply config edits until they're gone. We built
`_is_claude_running()` in the manager and a `messagebox.showerror`
that explicitly says *"NO CHANGES WERE WRITTEN — fully quit Claude
Desktop first"*.

**What went wrong (the second time)**: Even after a clean quit-then-edit
cycle, the user reported MCP still timing out at next launch. This
turned out to be Attempt 3.

---

### Attempt 3 — Discover UWP path redirection

**The setup**: Claude Desktop was installed via Microsoft Store
(`C:\Program Files\WindowsApps\Claude_<id>\app\Claude.exe`). This is a
UWP / packaged install — fundamentally different from a traditional
installer.

**What we observed**: a fresh check from outside Claude's process tree
(Win+R `cmd`, `dir %APPDATA%\Claude\claude_desktop_config.json`)
showed our wrapper-routed entry, 231 bytes, recently modified.
**But** the same path queried from a python process spawned by Claude
Code (which itself is a child of Claude Desktop) showed a completely
different file: 2,219 bytes, with the old direct-serve entry, mtime
unchanged for hours.

```
External view (manager, Notepad++, Win+R cmd):
  C:\Users\pmpd\AppData\Roaming\Claude\claude_desktop_config.json
    → 231 bytes, wrapper-routed ✓

UWP-internal view (Desktop, anything spawned by Desktop):
  C:\Users\pmpd\AppData\Local\Packages\Claude_<id>\LocalCache\
    Roaming\Claude\claude_desktop_config.json
    → 2,219 bytes, direct-serve ✗
```

**Lesson 2**: On UWP / Microsoft Store-installed apps, Windows applies
**asymmetric file-system redirection** to `%APPDATA%` accesses. A UWP-
context process reading `%APPDATA%\<package>\<file>` gets transparently
redirected to the package's `LocalCache\Roaming\<package>\<file>`. A
non-UWP-context process reading the same path string sees a completely
separate "external" file. Both files exist; both have the same path
string; they have nothing to do with each other on disk.

**Fix**: the manager's `_resolve_desktop_cfg_path()` helper now globs
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\` at module
load. When the UWP install is detected, it targets the package-internal
path. Falls back to the traditional `%APPDATA%\Claude\` path otherwise.

**How to spot this in the future**: if a config edit "succeeds" (file
mtime updates) but the app doesn't see the change, suspect UWP path
redirection. The smoking gun: same-named files in both
`%APPDATA%\<app>\` and `%LOCALAPPDATA%\Packages\<app>_*\LocalCache\Roaming\<app>\`
with **different content**. Edit the one in `%LOCALAPPDATA%\Packages\…`.

---

### Attempt 4 — The "Could not attach" mystery (chasing the threading red herring)

After fixing the path issue, the config edit landed correctly in
Desktop's actual file. Restarted Desktop. MCP **still** timed out.
"Could not attach to MCP server tokensave."

**What we tried**:
1. Reverted the wrapper to a "simpler" shape (kept threading, removed
   retry loop). Still failed.
2. Reverted to the literal 12-line original wrapper from `git show
   master:src/tokensave-wrapper.py`. **Still failed.**
3. Pondered briefly whether Claude Desktop was blacklisting the server
   after too many failed attaches. Suggested wiping the package state.
   Didn't pull the trigger.

**The breakthrough**: ran the wrapper directly with `subprocess.PIPE`
stdio and fed it a real MCP `initialize` JSON-RPC message:

```python
init_msg = b'{"jsonrpc":"2.0","id":1,"method":"initialize",...}\n'
proc = subprocess.Popen([python, wrapper],
    stdin=PIPE, stdout=PIPE, stderr=PIPE)
stdout, stderr = proc.communicate(input=init_msg, timeout=10)
```

**Result**: zero bytes back from tokensave. The wrapper's child wasn't
receiving the MCP message.

Then ran `tokensave.exe serve` **directly** (no wrapper) with the same
stdio + same init message: **full valid MCP response in <100 ms.**

That isolated the bug: it was in the wrapper's child-spawn, NOT in
threading, NOT in tokensave, NOT in Desktop's MCP behaviour.

---

### Attempt 5 — The actual root cause (stdio inheritance)

**The bug**: in the original wrapper:

```python
proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
```

No `stdin/stdout/stderr` arguments specified. Python's documented
behaviour is "inherit the parent's standard handles." On Windows
under pythonw.exe (the windowless Python launcher), this default
inheritance **does not reliably propagate piped stdio handles to a
console subprocess** like tokensave.exe.

Specifically: when Claude Desktop launches pythonw.exe with stdin/stdout
piped (for MCP protocol exchange), pythonw's `sys.stdin/stdout/stderr`
ARE tied to those pipes. But when subprocess.Popen spawns a child
without explicit stdio args, Python's default-inheritance path uses
OS-level handles rather than the Python-level `sys.std*` objects.
Under pythonw.exe, those OS-level handles can be NULL/invalid even
when sys.std* are working — so the child gets no stdin/stdout.

**The fix** (~10 lines):

```python
proc = subprocess.Popen(args,
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
    creationflags=CREATE_NO_WINDOW)
```

Explicitly passing `sys.std*` forces Python to extract the underlying
file descriptors from those Python objects and supply them as the
child's stdio. This works under BOTH python.exe (console launch) AND
pythonw.exe (windowless launch).

**Verified post-fix**: same `subprocess.PIPE` + initialize test, both
`python.exe wrapper.py` and `pythonw.exe wrapper.py` returned a full
valid MCP response in <100 ms.

**This bug almost certainly existed in the original wrapper for
months**. Production users who never hit it were probably running
tokensave through Claude Desktop's **DXT (Desktop eXtension)**
mechanism, where `tokensave.exe` runs directly with no Python
indirection — bypassing the wrapper entirely.

---

## Other gotchas discovered along the way

### Claude Desktop has TWO config systems

- **Legacy**: `claude_desktop_config.json` with a top-level `mcpServers`
  object. This is what we've been editing. Still read at startup, still
  used for legacy MCP entries.

- **Modern**: `config.json` with `dxt:allowlistEnabled`, `dxt:allowlistCache`
  (encrypted), and other DXT-related keys. This is the registry behind
  the **Connectors UI**. DXT extensions register here.

Editing `claude_desktop_config.json` doesn't make your server appear in
the Connectors UI — that's a DXT-only surface. Your legacy entry still
works functionally, but won't show up under Settings → Connectors.

### The Apply-refused silent path

The first version of `MCPConfigDialog._apply` used `messagebox.showwarning`
when Claude Desktop was running. Warnings have a yellow icon that reads
as "FYI" — users dismissed them quickly and walked away thinking the
fix had landed, when in fact NOTHING was written. Three improvements:

1. Switched to `messagebox.showerror` (red icon reads as "rejection")
2. Added explicit `★ NO CHANGES WERE WRITTEN ★` line in the modal body
3. Log a red `MCP Apply REFUSED:` line to the persistent OUTPUT pane

Plus a forced `self._render()` after every Apply attempt (success OR
refusal) so the row's status badge can't silently lie about the result.

### Classify reported "ok ok" against a disk file that was actually broken

This one we never fully root-caused. The manager's `_classify_mcp_entry`
returned `state="ok"` for both Desktop and Code configs at one point
during the debugging, while a parallel `python` process reading the
same files reported `state="direct_serve"` for Desktop. Two possible
explanations: (a) Notepad++ had the file open with a stale buffer that
fooled the user, (b) some Windows file-system cache or filter served
different content to different processes. The classify code itself has
no internal cache; it reads the file fresh on every call.

**Workaround in place**: when in doubt, click **↻ Re-detect** in the
MCPConfigDialog. That forces a fresh read.

### `tokensave-wrapper.py` exits when `tokensave serve` exits

The wrapper's main loop is `sys.exit(proc.wait())`. If the tokensave
child dies (for any reason — MCP shutdown, crash, idle timeout), the
wrapper dies with it. Claude Desktop's MCP supervisor will respawn the
wrapper. In normal operation we see a new tokensave process roughly
every 60 seconds, paired with a wrapper that lives just long enough to
spawn it. This is fine; it's how MCP servers are supposed to work.
Don't try to "fix" it by keeping the wrapper alive across child deaths.

### "Last Synced" was reading the wrong file

tokensave uses SQLite in WAL mode. Incremental syncs only touch
`tokensave.db-wal`; the main `tokensave.db` file's mtime only advances
on checkpoint (server shutdown or `sync --force`). The manager was
reading `getmtime(tokensave.db)` exclusively, so "Last Synced" stayed
stuck on the last checkpoint time. Fixed by taking `max()` across the
three sibling files.

### `tokensave install` writes broken Claude Code hooks on path-with-spaces installs

Discovered while building the in-manager Doctor purge flow:
`tokensave install --agent claude` writes hook commands as a single
unquoted string like `"D:/Claude Co worker/Token Save/tokensave.exe hook-pre-tool-use"`.
Claude Code splits by whitespace and ends up with subcommand =
`Co` (or `With`, `Program`, etc., depending on the install path).
`tokensave doctor` detects `wrong subcommand: "Co"` and auto-removes
the hooks; the next install re-adds them broken. Infinite loop until
either tokensave fixes the quoting or the install path stops having
spaces.

**This is an upstream bug, not a manager bug.** The draft issue body
lives at `docs/upstream-issues/tokensave-hook-quoting.md`. When/if it
gets filed, replace the placeholder below with the actual issue URL
so future readers can follow the fix.

> **Upstream issue:** (file at https://github.com/aovestdipaperino/tokensave/issues — https://github.com/aovestdipaperino/tokensave/issues/81)

In the meantime, the hooks aren't critical (they're for tokensave's
session-event tracking — token counters get less rich data without
them) and the manager doesn't depend on them at all. Just dismiss
the doctor warnings about hooks until the upstream fix lands.

---

## What we DID NOT solve: live pin reloading

After all this, the only thing left on the table from the original goal
is **live in-session pin reloading**. The wrapper is back to its
single-threaded shape and pin changes still require a Claude restart.

If you want to reintroduce live reload, do NOT do it in the wrapper.
Options that should work:

### Option A: External watcher process

A small standalone Python script (or compiled binary) that:
1. Runs as a long-lived background process (started by the manager, perhaps)
2. Polls `~/.tokensave/desktop-project.txt` every 2s
3. When the pin changes to a different valid project, calls
   `taskkill /F /PID <tokensave-pid>` to kill the currently-running
   tokensave server
4. Claude Desktop's MCP supervisor sees its server die and respawns
   the wrapper, which reads the new pin

This keeps the wrapper completely untouched and isolates the live-reload
concern to its own process. The watcher needs a reliable way to discover
the tokensave PID — probably by scanning `Win32_Process` for
`tokensave.exe serve` instances whose parent is pythonw or claude.exe.

### Option B: Pre-spawn the tokensave child differently

A more radical refactor: have the wrapper detect the pin file mtime on
its tokensave process termination, and respawn with the new -p IN-
PROCESS. This is closer to Attempt 1 but requires very careful stdio
hygiene to not break MCP handshake on subsequent spawns. Probably not
worth it.

### Option C: Convert tokensave to a DXT extension

DXT extensions are Claude Desktop's modern integration mechanism. They
appear in the Connectors UI, can declare allowlists, and presumably
have their own switching APIs. Repackaging tokensave as a DXT might
provide native project-switching without the wrapper indirection at
all. Significant new work, but the right long-term shape.

---

## Diagnostic recipes (save these for future debugging)

### Per-project MCP server logs

```
%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Local\claude-cli-nodejs\
  Cache\<sanitized-project-path>\mcp-logs-<server-name>\<timestamp>.jsonl
```

Each line is a JSON event. Look for `Connection timeout triggered` to
confirm a handshake failure. The `sessionId` field correlates to a
specific Claude Code session.

### Test the wrapper end-to-end without Claude Desktop

```python
import subprocess

init_msg = b'{"jsonrpc":"2.0","id":1,"method":"initialize",'\
           b'"params":{"protocolVersion":"2024-11-05",'\
           b'"capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n'

for py in [r"C:\Users\pmpd\miniconda3\python.exe",
           r"C:\Users\pmpd\miniconda3\pythonw.exe"]:
    proc = subprocess.Popen([py, wrapper_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, _ = proc.communicate(input=init_msg, timeout=8)
    print(py, "→", "OK" if b'"result":' in stdout else "FAIL")
```

If either fails, MCP attach won't work from Claude Desktop either. Fix
the wrapper here first.

### Find Claude Desktop's actual config file

```python
import os, glob, time
for p in glob.glob(os.path.expandvars(
    r'%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json')):
    print(time.ctime(os.path.getmtime(p)), os.path.getsize(p), p)
```

The recently-touched, larger file is Desktop's actual config. The one
under `%APPDATA%\Claude\` may be an unrelated stale file.

### Confirm tokensave-the-child receives MCP messages

If the wrapper's MCP attach is failing, isolate the wrapper by running
`tokensave.exe serve -p <project>` directly with piped stdio (see the
test recipe above). If it responds, the wrapper's stdio handling is
broken. If it doesn't, tokensave itself or the project DB is the
problem.

---

## Tests we don't have but should

1. **MCP smoke test** — automated CI that spawns `pythonw wrapper.py`
   with piped stdio, sends an `initialize`, asserts a valid response
   within 5 seconds. Catches stdio-inheritance regressions immediately.

2. **UWP-aware path resolution test** — assert `_resolve_desktop_cfg_path()`
   returns the package-internal path when `%LOCALAPPDATA%\Packages\Claude_*\`
   exists, the traditional path otherwise.

3. **Config-file mtime monotonicity check** — when we write a config,
   read it back from the same path immediately afterwards and confirm
   the bytes match. Catches asymmetric-redirection silently writing to
   a different file than we expect.

---

## Glossary

- **MCP** — Model Context Protocol. JSON-RPC over stdio for Claude to
  talk to external "tool servers" like tokensave.
- **DXT** — Desktop eXtension. Anthropic's modern packaged format for
  Claude Desktop integrations. Shows in the Connectors UI.
- **UWP / MSIX** — Universal Windows Platform / Microsoft installer
  format used for Microsoft Store apps. Sandboxed, with asymmetric file
  redirection from `%APPDATA%` to package-local state.
- **The wrapper** — `src/tokensave-wrapper.py`. A thin Python script
  that picks which project tokensave should serve and `Popen`s the
  tokensave child with the right `-p` flag. Inherits stdio so MCP
  flows transparently.
- **The pin file** — `~/.tokensave/desktop-project.txt`. Plain-text
  file containing one path. Written by the manager's `★ Set as Active`,
  read by the wrapper at startup.

---

*Last updated: 2026-05-23 (after the stdio-inheritance fix landed).
Update this doc when a new failure mode is discovered or when the
deferred live-reload work resumes.*
