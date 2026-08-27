# VS Code MCP client matrix

Which client, running through which host, reads which config, and therefore
which tokensave ends up serving which project.

This exists because the Manager's MCP subsystem has been wrong three times in a
row by generalising one client's behaviour to another. See
[MCP_INTEGRATION_GOTCHAS.md](MCP_INTEGRATION_GOTCHAS.md) for the Claude Desktop
scope collision that produced that rule; this document applies it to VS Code,
where **three different MCP clients** can be running at once.

> **Status: A0.2 and A0.3 both CONFIRMED behaviourally.** The headline is that
> **one root `.mcp.json` serves both clients**, so Phase C's adapter is not
> needed. Duplicate-name arbitration is still `pending` and must not be treated
> as answered — see "What is still open".

## The model

Every row is a chain, not a config layer:

```
client / harness → host → consulted source(s) → effective source → server process → served project
```

Four states are recorded **separately** and never collapsed into one status:

| State | Question |
|---|---|
| `configured` | does a definition exist anywhere this client reads? |
| `started` | did a server process actually launch? |
| `connected` | did the client complete a session with it (trust granted)? |
| `serving_project` | which tree is that server actually answering from? |

A server can be configured but pending trust, started but disconnected, or
connected and serving the **wrong** project. Collapsing these is the original
defect this subsystem exists to correct.

## Evidence rules

- **Configuration truth is primary** — the `-p` value, the wrapper record, the
  process argv.
- **`.tokensave/tokensave.db-shm` mtime ↔ process start time is corroboration
  only.** Under VS Code several servers may start in the same window, so this
  heuristic must never become the identity mechanism.
- **`db_size_bytes` byte-matching the project's `tokensave.db` is the final
  behavioural acceptance test.**

## Experiment hygiene — mandatory

A careless MCP experiment can register the wrong project: the Manager's own
`effective_scope` created duplicate `~/.claude.json` keys exactly that way. So
every experiment uses

- a **scratch project**, never a real one,
- a **disposable tokensave index**,
- a **uniquely named server** — `tsprobe-a` / `tsprobe-b`, never `tokensave`,
- **cleanup and unregistration** afterwards.

Run the real-name collision only once the behaviour is already understood.

Within each experiment, in this order: (1) which sources are consulted *at all*,
(2) which one *wins*, (3) only then duplicate-name arbitration. Testing
duplicates first gives a misleading answer whenever a client never reads one of
the two locations.

## The matrix

| Client | Host | Consulted sources | Effective source | Duplicate-name | Var expansion | Trust/approval | 4-state | Manager action |
|---|---|---|---|---|---|---|---|---|
| **Claude Code VS Code extension** | ext host → spawned Claude Code CLI | `.mcp.json` + `~/.claude.json` — **measured** | **project `.mcp.json`** ✓ | n/a — VS Code's file is never read | pending | no prompt seen (see caveat) | all four ✓ | **`no action required`** |
| Claude Code CLI | CLI | same chain (control case) | pending | pending | pending | same | pending | pending |
| Copilot Agent Host | Agent Host | **`<ws>/.mcp.json` AND `<APPDATA>/Code/User/mcp.json`** — measured | both, in different categories | **pending** (names differed) | pending | discovered ≠ enabled | configured ✓, connected ✓ | **`detect-only`** |
| VS Code user MCP | VS Code native | `<APPDATA>/Code/User/mcp.json` | feeds Copilot only | pending | pending | unchecked by default | **invisible to Claude Code**; visible to Copilot | **`detect-only`** |
| Workspace root `.mcp.json` | both hosts | `<ws>/.mcp.json` | **serves BOTH clients** | pending | pending | Claude Code: approval chain; Copilot: tool picker | configured ✓, serving ✓ | **`managed` (already)** |

Action vocabulary: `managed` · `detect-only` · `unsupported` ·
`no action required`. Not every observed config has to become a repair path.

## A0.3 — Claude Code VS Code extension (priority client)

**Config-truth conclusion: the extension uses Claude Code's own MCP chain and
has no knowledge of VS Code's native `mcp.json`.** Four independent observations,
all from `anthropic.claude-code-2.1.246-win32-x64` — full record in
[`tests/fixtures/vscode_mcp/a0_3_claude_code_extension.json`](../tests/fixtures/vscode_mcp/a0_3_claude_code_extension.json):

1. It contributes **no MCP settings of its own** — 15 `claudeCode.*` properties,
   none MCP-related.
2. Its `jsonValidation` claims `**/.claude/settings.json`,
   `**/.claude/settings.local.json` and the two `managed-settings.json` paths —
   Claude Code's files, not VS Code's.
3. Its bundled `claude-code-settings.schema.json` speaks Claude Code's MCP
   vocabulary, explicitly keyed on `.mcp.json`: `enabledMcpjsonServers`
   ("List of approved MCP servers from .mcp.json"), `disabledMcpjsonServers`,
   `enableAllProjectMcpServers`, `allowedMcpServers`, `deniedMcpServers`.
4. A grep over the 3.0 MB `extension.js` finds `mcpServers` ×34, `mcp.json` ×8
   (every one of them Claude Code's `.mcp.json` or `managed-mcp.json`) and
   **zero** hits for `.vscode/mcp` or `Code/User/mcp`. The bundle also states
   that a setting "does not gate other MCP entry points (SDK `setMcpServers`,
   `claude mcp add`, `.mcp.json`)" and "blocks surfaces that **spawn the CLI**".

### CONFIRMED live, 2026-08-26

A scratch harness at `D:\vscode-mcp-probe` with two deliberately mismatched
projects — projA (7 files / 14 nodes) and projB (23 files / 46 nodes) — and
probe-named servers, never `tokensave`:

| Server | Defined in | Points at |
|---|---|---|
| `tsprobe-a` | `projA/.mcp.json` (Claude Code's chain) | projA |
| `tsprobe-b` | `%APPDATA%\Code\User\mcp.json` (VS Code's own) | projB |

A Claude Code session opened on projA reported **2 servers, 2 connected** and
invoked `mcp__tsprobe-a__tokensave_status`. The two were `tsprobe-a` (project
`.mcp.json`) and `codegraph` (user `~/.claude.json`). **`tsprobe-b` never
appeared**, though it sat in VS Code's file throughout.

The answer came from the right tree: `node_count` 14, `file_count` 7,
`db_size_bytes` **475136** — byte-exact against the pre-run measurement of
projA, with `sibling_projects` listing projB (i.e. standing *in* projA looking
outward). Process truth agreed: three live servers, every one
`tokensave serve -p .`, none bare.

**So the priority client needs no VS Code-specific binding.** The Manager's
existing `.mcp.json` machinery already covers Claude Code inside VS Code —
no `mcp_vscode.py` for this client. Phase C reduces to the Copilot surfaces,
if they turn out to need anything. That is a successful Phase A outcome, not
a failure to find work.

**Caveat, deliberately not overclaimed:** no approval prompt appears in the
transcript, but that is not proof approval was skipped — the folder trust
dialog may have covered it, and the transcript may not render such prompts.
The trust layer still needs its own measurement.

## A0.2 — Copilot Agent Host, CONFIRMED live 2026-08-26

Same harness, same window. VS Code's **MCP Servers** view listed 7 servers in
three categories:

| Category | Servers | Source |
|---|---|---|
| User (2) | `tokensave`, `tsprobe-b` | `%APPDATA%\Code\User\mcp.json` |
| Extensions (4) | GitKraken, Azure MCP, Foundry MCP, pylance | installed extensions |
| **Built-In (1)** | **`tsprobe-a`** | **`<ws>/.mcp.json`** |

`tsprobe-a` exists nowhere on this machine except `projA/.mcp.json`, so
**Copilot reads the workspace root `.mcp.json` natively** — and labels it
"Built-In", a piece of vocabulary any Manager UI describing this surface will
have to match. Copilot's **Configure Tools** picker listed both `tsprobe-a` and
`tsprobe-b`.

### The headline: one file serves both clients

Combining A0.2 and A0.3:

```
<project>/.mcp.json  ──▶ Claude Code extension   (proven: served projA, 14 nodes)
                     └─▶ Copilot Agent Host      (proven: listed as Built-In)

<APPDATA>/Code/User/mcp.json ──▶ Copilot only    (proven: Claude Code never saw it)
```

So **the Manager's existing project `.mcp.json` binding already covers both
clients**, and no `.vscode/mcp.json` needs writing. Phase C's `mcp_vscode.py`
is not required; the VS Code user file becomes `detect-only`. The gitignore
negation (C5) is also unnecessary — `.mcp.json` sits at the repo root, outside
`.vscode/`.

### Discovery is not enablement

Every MCP server in the Configure Tools picker was **unchecked** while built-in
tools and extension tool-sets were checked. A server can be configured,
discovered and listed and still contribute nothing. This is the
`configured → started → connected → serving` distinction showing up as a real
UI state, and any Manager verdict about Copilot must not read "present in the
file" as "in use".

## The collision run — Claude Code immune, Copilot inconclusive

A second run staged a real name collision: `tsprobe` defined in **both**
`<ws>/.mcp.json` (→ projA) and `<APPDATA>` user scope (→ projB), same name,
provably different trees.

**Claude Code: immune, as predicted.** The panel returned `node_count` 14,
`file_count` 7, `db_size_bytes` 475136 — projA, byte-exact. An identically
named user-scope server pointing at projB changed nothing, because that file
is never read. A0.3 now holds adversarially, not just in the clean case.

**Copilot: no winner observed.** Two findings, neither of them the answer:

- VS Code **did not collapse** the duplicates — Configure Tools listed *two*
  separate `tsprobe` entries, each with its own "Update Tools", neither marked
  disabled. So there is no name-based dedupe at the listing level.
- Copilot **never started either server**. It offered *"The MCP servers
  tsprobe, tokensave, Foundry MCP may have new tools and require interaction
  to start. Start them now?"*, then said the entry was *"not exposed in the
  available tool catalog"* and fell back to a shell command that does not
  exist. No MCP call happened, so nothing arbitrated.

## The correction: the bare `serve` does not shadow — it crashes

This investigation opened by calling the two bare-`serve` user entries
"structurally identical to the pathology retired from Claude Desktop", with the
verdict explicitly deferred to measurement. **The measurement says otherwise,
and the difference matters.**

VS Code's own MCP log for the user-scope entry:

```
[info]    Starting server tokensave
[info]    Connection state: Running
[warning] [server stderr] Multiple tokensave projects found — pass -p <path> to select one:
[warning] [server stderr]   D:\\Claude Co worker\\Token Save Manager Source
                          … 15 projects …
[warning] [server stderr] Error: config error: no TokenSave index found at 'C:\\Users\\<user>'
[info]    Connection state: Error Process exited with code 1
```

**VS Code spawns MCP servers with `cwd` = the user's HOME directory**, not the
open workspace. tokensave finds no index there, refuses to guess among the
registered projects, and exits 1.

| | Claude Desktop (PR #18) | VS Code user scope |
|---|---|---|
| Resolution | global pin file | `cwd` = HOME |
| Outcome | served the **wrong project**, silently | **exits 1**, serves nothing |
| Risk | confident wrong answers | a dead entry, and no tools |
| Right fix | retire the entry | "this has never worked — fix or remove" |

So the Manager must **not** reuse the Desktop retirement copy here. The honest
message is that the entry is broken, not that it is shadowing.

### Why the checkbox would not stick

A server that never connects enumerates no tools. In Configure Tools the
`tokensave` and `tsprobe` entries expand to nothing but "Update Tools", while a
working server (Pylance) expands to six named tools. Ticking an empty server
moved the counter 204 → 202 and did not persist — there was nothing to select.
This is the `configured → started → connected` distinction rendered as UI.

### VS Code names its sources, and we can read them

Log filenames are machine-readable identifiers for the surface:

| Source | Log filename |
|---|---|
| user scope | `mcpServer.mcp.config.usrlocal.<name>.log` |
| workspace root `.mcp.json` | `mcpServer.workspace-dot-mcp.0.<name>.log` |

`workspace-dot-mcp` is VS Code's own name for the root `.mcp.json` surface —
independent confirmation that it reads that file, and a cheap detection hook
for a future Doctor rule.

## What is still open

**Duplicate-name arbitration** is formally unmeasured, but largely moot for
this surface: a user-scope bare `serve` cannot win an arbitration it crashes
out of. It would only matter for a user-scope entry carrying a valid `-p`.

The surface stays **`detect-only`** — but for a better reason than before. Not
"we haven't measured the shadow" but "there is no shadow; there is a dead entry
worth reporting".

### Still outstanding for the Copilot surfaces
- `${workspaceFolder}` semantic value: single-root, multi-root, and
  workspace-opened-from-a-subdirectory.
- Subfolder session inheritance: workspace root `repo/`, active folder
  `repo/src/`, and separately a session launched with `cwd=repo/src` — does the
  server see the workspace root, the active folder, or the process cwd?
- Multi-root cardinality: two folders, one entry → one server or one per folder?
- File lifecycle: edit each config externally with VS Code open, wait, re-read.
  Claude Desktop rewrote from memory every 1–2 minutes; **do not assume VS Code
  does.** This alone decides whether an app-closed gate is warranted.

## Incidental finding — `enableAllProjectMcpServers`

`memory/roadmap11_binding_inert.md` records that "there is NO CLI to approve —
approval is interactive, or you write `settings.local.json` yourself". The
extension's bundled schema documents a first-party
`enableAllProjectMcpServers` ("Whether to automatically approve all MCP servers
in the project"), which would be a third option and possibly a cleaner approval
path than writing `enabledMcpjsonServers` per project.

**Unverified — do not act on it until tested.** Noted here so it is not lost.
