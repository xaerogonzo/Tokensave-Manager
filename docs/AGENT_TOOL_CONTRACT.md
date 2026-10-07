# Agent Tool Contract

How a project makes its own functionality callable by an agent session
(Claude Code first; Cursor, Codex and others reuse the same contract). Written for
people and agents **adding** a callable surface to a project. It is not a help
topic: the in-app Help corpus is deliberately limited to user-facing documents.

Status: **contract; phases 1-2 shipped.** The Manager's own surface (`src/cli.py`,
`helpers/commands.py`) already implements most of it; the gaps are listed at the
end. Live-session, MCP and cross-provider verification are deferred.

## The layering

```
project logic (helpers / domain code)
        |
   project CLI            <- the canonical agent-facing contract
        |
   one JSON envelope on stdout, semantic exit codes
        |
  +-----+-------------+
  |                   |
 Bash (any agent)    MCP adapter (optional, later; delegates to the CLI)
```

| Tier | What | Use it for | It is NOT |
|---|---|---|---|
| 0 | Import a helper directly | Exploring source; a pure internal calculation | A contract. It bypasses classification, validation and the envelope, and is coupled to module layout |
| 1 | **The project CLI** | Everything an agent should be able to ask of a project | -- |
| 2 | An MCP adapter over tier 1 | Typed tool schemas, when a plain CLI is not enough | A place for logic. It delegates; it never re-implements a command |
| 3 | Live app (drive / IPC) | What exists only in a running window: layout, dialogs, screenshots, actions on the open app | A substitute for a CLI command the project could expose headlessly |

**Rule of thumb.** Deterministic, project-local and headless belongs in tier 1
first. If it is frequently invoked by agents and wants a typed schema, tier 2
may wrap it later. If it needs the running application, it is tier 3.

## Two scopes -- do not conflate them

| | Target-scoped | Self-scoped |
|---|---|---|
| Question it answers | What does tool X say about project P? | What does project P say about itself? |
| Example | `manager-cli doctor --project D:/p` | A project's own `cli.py <command>` |
| Project root comes from | **`--project`, always explicit.** Never inferred from the working directory | The CLI's own location (`__file__`), so a moved or cloned checkout still works |
| Reference implementation | `src/cli.py` | Modelled on `src/cli.py` + `src/cli_support.py` |

`--project` is mandatory on target-scoped commands for a reason: silent cwd
inference is what produced the MCP scope collision. A self-scoped CLI has no
`--project` because the project *is* the tool.

**Never substitute another project's tool for this project's tool.** A session
working on project X uses X's callable surface. The Manager's CLI answers
questions *about* X; it does not give X a surface it lacks.

## The envelope

stdout is exactly one JSON object and nothing else; stderr is for humans. A new
project CLI should reuse the Manager's envelope rather than invent one
(`cli_support._envelope`):

```
schema_version   int, bumped only on a breaking change
cli_version      the tool's own version
command          the subcommand that ran
ok               true iff exit code is 0
data             per-command payload
findings         top-level, always present (empty list when none)
warnings         list
error            string or null
```

Convention for `data` in new CLIs (a convention, not a schema change): carry
**measured facts**, a **derived state**, and the **reason** separately, never a
bare verdict. `{"state": "STALE", "facts": {"last_full_sync": "..."}, "reason":
"..."}` rather than `"probably stale"`. Unknown is never false: a source that
could not be read is reported as unread, not as a pass.

Exit codes (`cli_support`): `0` ok, `1` the operation ran and reported problems,
`2` invalid invocation, `3` a required tool or path is missing, `4` an
operation ran but could not be verified.

## Side-effect classes (project-assigned, never inferred)

From `helpers/commands.py` (`SIDE_EFFECT_MEANING`):

| Class | Meaning |
|---|---|
| `pure_read` | No filesystem, project or database mutation. OS-level UI effects (window focus) are excluded |
| `observe_refresh` | Reads the project, but may refresh the tool's own bookkeeping |
| `mutating` | Changes project or tool state |

The *unattended-safe* set is derived, not a fourth class:
`pure_read | observe_refresh`. **A `mutating` command must not be run merely
because it looks useful**; it needs explicit user intent. The class comes from
the project's command table, not from the model's reading of the command name.

## Self-description

`commands --json` is the capability manifest. Beyond the table it carries `tool`
(name, `cli_version`, `schema_version`, `scope`) and `invocation`, keyed by
subcommand: `unattended_safe` plus an `args` list (flag, kind, required,
multiple, default, choices, help) read from the argument parser. In the Manager it is already
derived from the single command table (`helpers/commands.py`), which also feeds
the VS Code tasks and command ids, the generated `commands.ts` and the IPC
actions, so a new surface is a table row, not a new list. It is project-less:
it must work without a project argument.

A project CLI should answer, from that command alone: its name, its tool
version and schema version, and for each command its class, whether it needs a
project, and which flags it accepts.

## Discovery pointer (`.agent-tool.json`, self-scoped projects)

A discovery pointer, not the tool. Project-relative, no machine paths:

```json
{
  "protocol_version": 1,
  "launcher": ["python", "src/cli.py"],
  "working_directory": ".",
  "capability_command": ["commands", "--json"]
}
```

When present, an agent runs `launcher + capability_command` from
`working_directory` to learn what the project exposes. Implementation behind the
launcher is the project's own business. A project with no such file and no known
CLI has no callable surface yet; say so rather than guessing at `cli.py`.

## Health is three facts

`configured` (a launcher is declared), `executable` (it starts), `healthy` (it
answers its capability command with a known schema). A launcher that exists is
not a launcher that works; do not infer health from the file being there. A
missing or failing tool is reported as such, never as an empty result.

## Packaging caveats

A packaged console exe is a snapshot and goes stale when the source changes. It
must be built with a console subsystem (`--windows-console-mode=force`): a GUI
build has no stdout, which silently removes the whole contract. Commands that
shell out to `sys.executable` cannot work in a onefile build and must fail with
an explicit prerequisite exit, not `WinError 2`. See ARCHITECTURE, "Packaged
CLI".

## Where the Manager stands against this

Present: CLI with envelope and exit codes, three side-effect classes with a
derived unattended-safe set, single command table, `commands --json`,
mandatory `--project`, packaged console exe, live-app IPC and drive.

Also present (phase 2): the tool-identity block and per-command argument rows in
`commands --json`, derived from the parser.

Not yet: a `--plan` / `--apply` split for the mutating
commands (`sync`, `commit-request`, `request`), none of which has a preview mode
today; delivery of this contract and `.agent-tool.json` to the fleet (phase 3);
a CLI template for projects that have only a GUI drive (phase 4); an MCP adapter
generated from the manifest (later, and only delegating to the CLI).
