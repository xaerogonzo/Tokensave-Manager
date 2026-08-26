# MCP Integration Gotchas

A field manual for anyone touching `tokensave-wrapper.py`, the manager's
MCP-config editing flow, or trying to reintroduce live pin reloading. Written
2026-05-23 after a multi-hour debugging session that produced 5+ failed fixes
before landing the correct one. Each section is structured as **what we
tried → what went wrong → the lesson**.

---

## TL;DR — four things to know before touching this code

0. **Claude Desktop's `tokensave` entry shadows every project's own binding, machine-wide.** Desktop spawns the wrapper app-level, the wrapper picks one project from the pin, and Claude Code dedupes MCP servers by NAME — so a Desktop-hosted session in *any* repo is answered from that one project. Symptom: the index looks stale and a re-sync does not help. See [The scope collision](#the-scope-collision-desktops-tokensave-outranks-every-project-binding) below, and prove it with the two-command recipe there before re-indexing anything.


1. **Claude Desktop's MCP config file is NOT `%APPDATA%\Claude\claude_desktop_config.json` on Microsoft Store / UWP installs.** It's actually `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`. Both paths exist on disk simultaneously; Windows asymmetric file-path redirection makes them resolve to different physical files depending on whether the caller is in UWP context. Edit only the package-internal path.

2. **`subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)` with default `stdin/stdout/stderr=None` doesn't reliably proxy stdio under pythonw.exe.** Tokensave (a console child) never sees the MCP messages Claude Desktop is piping in. Pass `stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr` explicitly.

3. **Don't add `import threading` or daemon threads to the wrapper script.** It interacts badly with Windows stdio handling under pythonw.exe in subtle ways. Any live-reload feature must be implemented as an **out-of-process** mechanism, not inside the wrapper.

---

## The scope collision: Desktop's `tokensave` outranks every project binding

*Diagnosed 2026-08-26, after the same symptom had been misread three times.*

### The symptom, and why it is so convincing

A session reports that the tokensave index is stale. Re-syncing does not fix
it. Three lookups of symbols known to exist return `[]` — one written minutes
earlier, one four commits back, one shipped long ago. `tokensave_status`
reports a branch the repository does not have.

**Three misses in a row is not staleness. It is a different tree.**

The tell that settles it: after a re-sync, `status` comes back byte-identical
— same `last_sync_at`, same node count — while `uptime_secs` has reset. The
server restarted and reopened *the same wrong database*.

### The mechanism

1. Claude Desktop registers `tokensave` in its own `claude_desktop_config.json`,
   pointing at `src/tokensave-wrapper.py`.
2. Desktop spawns that wrapper **for the whole app, not per session**. Measured:
   two live wrappers, both with `ppid` = the Desktop process (PID 20724).
3. The wrapper therefore has no way to know which repository a session is in,
   and picks one project from the global pin
   (`~/.tokensave/desktop-project.txt`).
4. Every Desktop-hosted Claude Code session inherits that single server.
5. Claude Code **dedupes MCP servers by name**, so this entry beats the
   project's own `.mcp.json` — which is running correctly at the same moment.

Measured in Token Save Manager Source while its own session was being answered
from OpenChem Studio:

| Source | Files | Nodes | DB size | Branch |
|---|---|---|---|---|
| `tokensave_status` over MCP | 741 | 24,530 | 93,863,936 | `joback-thermophysical` |
| `tokensave status` over CLI | 301 | 9,568 | 33.0 MB | `Roadmap-11` |

`93,863,936` was a byte-exact match for OpenChem Studio's `tokensave.db`.

### Why none of the existing defences caught it

- **`strict_tree` cannot.** It was `true` in both projects. The server opened
  one project's index and reads *that project's* config, so it believes it is
  serving correctly. `strict_tree` guards `graph_root` redirection inside a
  server; it knows nothing about which repo the client is in.
- **`claude mcp get` cannot.** It reads `~/.claude.json` and never
  `claude_desktop_config.json`, so Desktop's entry is invisible to the one
  tier that asks the client which definition wins.
- **The earlier migration did not cover it.** Retiring the *user-scoped*
  `~/.claude.json` entry was correct and was done — the Desktop entry is a
  separate definition that outlived it.

### The proof recipe — run this before re-indexing anything

```bash
"D:/Claude Co worker/Token Save/tokensave.exe" status "<project>"
```

Compare `db_size_bytes` and `active_branch` against `tokensave_status` over
MCP. If they disagree, the MCP server is on another tree and no amount of
syncing will help. The CLI always reads the project you name, so it is the
tiebreak — and it is a usable workspace for the rest of the session:

```bash
"D:/Claude Co worker/Token Save/tokensave.exe" tool search <symbol> --project "<project>"
```

These are **diagnostic heuristics, not correctness predicates**. Repeated
empty results are a strong wrong-tree signal; the authoritative test is always
MCP status versus direct CLI status for the same project.

### The fix

Settings → MCP Integration → **Retire Desktop tokensave…**. Each project's
own `.mcp.json` (`tokensave serve -p .`) then wins, and every session serves
its own tree. The accepted trade is that **Claude Desktop chat loses tokensave
entirely** — deliberate, and stated in the confirmation.

Three things about that migration are load-bearing:

- **Quit Claude Desktop first.** It rewrites `claude_desktop_config.json` from
  its in-memory cache every 1–2 minutes, so an edit made while it runs is
  silently reverted. The manager enforces this as a hard gate rather than a
  banner.
- **Both physical config files must change** on a UWP install (see TL;DR #1) —
  they are one logical configuration seen from two process contexts.
- **A running server keeps its old project.** It resolves the project once, at
  startup. Restart Desktop, then verify.

### Two bugs the fix surfaced, both worth knowing on their own

**Process enumeration was silently returning nothing inside the manager.**
`tokensave_daemon._enumerate_windows` spawned a bare `"powershell"`, so
`CreateProcess` depended on PATH, and the resulting `OSError` was swallowed
into `[]` — indistinguishable from "found none". The manager runs as a
windowless `pythonw.exe` whose PATH did not contain
`System32\WindowsPowerShell\v1.0`, so every enumeration came back empty. It
became visible only because the new Desktop panel printed both halves at once:

```
No Desktop tokensave server is running right now (Claude Desktop is closed).
Could not determine whether Claude Desktop is running (no process information
was returned).
```

Two contradictory sentences, and two servers actually running. **The Tokensave
Daemon Manager dialog had been listing zero servers for the same reason.**

Reproduce it in one line — strip the PowerShell directory from PATH and call
`desktop_app_running()`; you get `(None, 'no process information was
returned')`, the exact string.

Fixed by `_powershell_exe()`: `shutil.which` for `pwsh` then `powershell`,
falling back to the absolute System32 path. Same lesson `effective_scope`
already recorded for `claude` — *what a shell resolves and what
`CreateProcess` resolves are not the same set*. Plus `stdin=DEVNULL`, and a
`strict=True` mode raising `EnumerationFailed` so a caller that must not guess
can tell "failed to ask" from "asked, found none".

Two plausible hypotheses were eliminated by measurement first, and both are
worth not re-chasing: pythonw stdio inheritance works fine even when fully
detached (`sys.stdin is None` and the CIM call still returns), and CIM takes
0.34s and is unaffected by four-way concurrency.

**`os.path.normcase` is a no-op on POSIX.** `discover_desktop_configs` tested
a normcased path for the lowercase markers `"packages"` and `"claude_"`. On
Windows `normcase` lowercases, so it worked; on Linux it does nothing, the
comparison was permanently False, and the `%APPDATA%` view was never marked
active — dropping it out of the change set and half-applying the migration
across the two physical views of one configuration. Caught only by Linux CI.

Recognising the *shape* of a Windows path must not depend on the host OS, so
that comparison lowercases explicitly now. Path *equality* still goes through
`normcase`, which is correct: it folds case exactly where the filesystem does.

### Verification is three layers, and all three must agree

The reason config-only checking was never enough here: the configuration
looked plausible, the server was healthy, and it was simply serving the wrong
repository.

```
config truth       the project's .mcp.json resolves to project scope
process truth      the live server belongs to that project (wrapper record / -p;
                   -shm mtime corroborates, never identifies)
behavioural truth  MCP tokensave_status == direct CLI status for that project
```

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

**Lesson 1b (2026-08-25): the same hazard applies to `~/.claude.json`, and
the detection for it was dead code for its entire life.** Claude Code
rewrites that file continuously while a session is open — measured at 30
seconds old with one session running — so removing the user-scoped
`tokensave` entry, which is exactly what the migration button does, can be
undone within a minute and read as the removal silently failing.

`_is_claude_running()` matched the process name `claude-code.exe`. **That
name has never existed.** `code` was therefore permanently `False`, so the
banner never mentioned Claude Code and the Apply guard on the Claude Code
row could never fire. Nothing failed loudly; the warning simply was not
there.

No executable name can answer this. Claude Code ships as an npm CLI (runs
as `node.exe`, far too generic to match), as a native binary, and hosted
inside the desktop app — where it is `claude.exe`, indistinguishable from
Desktop. `claude_code_active()` therefore uses the **mtime of
`~/.claude.json`** (a 5-minute window): packaging-independent, and direct
evidence of the actual risk rather than an inference from a process list.

Two deliberate asymmetries in the guards:

- **Desktop gets a hard refusal, Claude Code gets a confirmation.** Desktop's
  outcome is certain, so refusing helps. The mtime is probabilistic and its
  window is five minutes wide, so a hard block would strand someone for five
  minutes *after* they had closed every session as instructed.
- **The banner gives each app its own sentence.** The old wording was
  `"Claude Desktop / Claude Code is currently running. It rewrites its own
  config file"` — ungrammatical for two apps, and it never said which file
  was at risk. They own different files, and the migration button writes
  Claude Code's.

The banner also states that the desktop app hosts Claude Code sessions.
"Quit Claude Code" is not actionable advice for someone whose session is a
tab in `claude.exe`.

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

### `tokensave doctor` writes its report to STDERR, and colours regardless

Measured on v7.9.0. Two consequences for anything capturing it:

- `capture_output=True` gives an **empty stdout**, which reads as "nothing to
  report" rather than as an error. Merge with `stderr=subprocess.STDOUT`.
- `NO_COLOR=1` and `TERM=dumb` are ignored — strip ANSI yourself. The
  `_count_tokensave_files` helper in `helpers/doc_grounding.py` silently
  returned 0 for months because it regex-matched `Files:` against what had
  become an ANSI-styled box table; it now reads the SQLite file directly.

`DoctorController.doctor_env` + `scan_stale` are the one blessed way to invoke
it — don't add a second call site with its own subprocess shape.

### You cannot drive tokensave's interactive prompts from the Manager

It only prompts when `isatty()` is true. A Windows pseudoconsole (ConPTY) was
implemented to work around this and **abandoned** — the child attaches but its
std handles still resolve to the inherited console. Full diagnostic, and the
eight hypotheses ruled out by experiment, in
[`WINDOWS_CONPTY_FINDINGS.md`](WINDOWS_CONPTY_FINDINGS.md). Read that before
attempting it again. The shipped answer is to hand off to a real terminal and
then **verify by re-scanning**, which is a better contract anyway: a terminal
that opened, or even exited cleanly, proves nothing about what the user did in
it.

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

## Live pin reloading: attempted, measured, abandoned

**Do not build this. It was built. It does not work, and the reason is
structural rather than a bug that could be fixed.**

Option A below was implemented as `PinWatcherController` (Roadmap-10 phase
8) and removed again after measurement. The step it depends on — item 4,
"Claude Desktop's MCP supervisor sees its server die and respawns the
wrapper" — is the part that is false. **Desktop does not respawn a died
MCP server.** It surfaces "Server disconnected" and leaves it dead until
the app restarts.

Measured live, twice, with Desktop running throughout: after two pin
changes with the watcher enabled there were zero wrapper processes, zero
wrapper run records, and zero Desktop tokensave servers. The failure had
been invisible until respawn verification was added, because the watcher
logged "Claude Desktop will restart it" without ever checking — so the
feature reported success while leaving the user with no server at all,
which is strictly worse than the stale one they started with.

The deeper reason no variant works: **an MCP stdio server's lifetime is
owned by the client process that spawned it.** A wrapper the manager
starts has its stdio connected to the manager, so it cannot become
Desktop's server. There is no out-of-process way to hand a running Desktop
a replacement.

### What to do instead

The pin only selects a **default** graph. Every tokensave MCP tool accepts
`graph_root`, which opens any indexed project read-only — verified across
unrelated directory trees, not just siblings:

```
tokensave_context(task=..., graph_root="D:\\Random Projects\\Fortuna Lab")
→ tokensave_graph: root="D:\\Random Projects\\Fortuna Lab" read_only=true
```

So cross-project *reading* never needed a restart. The residual risk is an
agent that does not know to pass it and answers confidently from the wrong
graph — which is what `strict_tree` turns into an explicit refusal, and
why the manager now offers it per-project rather than only in bulk.

---

The original options are kept below for the record. Option A is the one
that was tried; its item 4 is the false premise.

### Option A: External watcher process — TRIED, DOES NOT WORK

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
