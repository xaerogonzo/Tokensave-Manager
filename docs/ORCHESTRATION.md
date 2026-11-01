# Orchestration: design (draft for review, no code yet)

Status: **D1-D7 approved 2026-10-31; M1-M5 measured (section 3); stage A is built (section 14); B and C are not.** It exists so the
decisions below are made by a person before any process-spawning code is
written, because this is the first feature in the Manager that lets an agent
change files without the person watching.

Written 2026-10-31 against the `claude/token-save-cursor` branch, after Phases
1-3 (live Cursor, routing, handoff ledger) shipped.

## 1. What this is, and is not

**Is:** the Manager launching a coding agent (Claude Code or Cursor) on a
bounded task, in its own git worktree, recording what happened, and handing the
result back as a branch for a person to review. Later: a second agent reviewing
that branch. Later still, and off by default: a trigger starting a task.

**Is not:** an agent loop (the Manager does not decide the next step), a merge
bot, or a scheduler of unattended work. Nothing here merges, pushes, tags or
commits to a branch the person is on. Completion is not acceptance
(`docs/AGENT_ARCHITECTURE.md`: propose-only).

## 2. What already exists and gets reused

| Need | Existing piece | Gap |
|---|---|---|
| Pick the agent per job | `agent_cli.resolve_for_feature` + `agent_overrides` | Needs task kinds beside features, not a second config key |
| Run an agent headless and capture text | `agent_cli.call_print(cwd=, timeout=)` | Uses `subprocess.run`: cannot be cancelled, cannot kill a process tree, no pid recorded |
| Run an agent visibly | `agent_cli.spawn` (new console, `/k`) | Output is not capturable; no exit code |
| Tell the agent what the situation is | `agent_handoff.prepare` (the brief) and the ledger | Brief is written for a person-to-agent handover, not a task spec |
| Read-only enforcement | `AgentCLISpec.read_only_args` | Cursor: `--mode ask`. Claude row is empty, but read-only IS enforceable (section 3, M2): add the measured flags |
| Propose, then apply | `ProposalBridge` (`dialogs/proposal.py`) | Built for one file edit; a branch diff is a different payload |
| Don't double-start | `AITasksController._running` | A UI guard only; lost on restart |
| Find stray worktrees | `worktree_health`, `worktree_cleanup`, Doctor stray-checkouts | Knows nothing of task ownership |
| See sessions | Tasks tab scanners for both agents | Reads transcripts, not Manager-launched tasks |

## 3. Measurements (2026-10-31, Cursor agent 2026.10.22, Claude Code 2.1.261)

Run in a scratch repo, one run per cell: **evidence, not proof**. Each was
planned as an unknown (M1-M5) and is now a recorded fact or a named gap.

- **M1. Cursor headless write.** `-p --trust` alone **creates files**; `--force`
  is not needed for file edits. **Shell commands need `--force`**: without it
  the call is rejected by permissions (the allowlist is `~/.cursor/cli-config.json`;
  a headless run cannot ask). With `--force`, `echo x > file` ran (exit 0, file
  created). A first round of runs saw every shell call refused with "Hook
  blocked ... eval: syntax error": **the cause was the environment the runs were
  started from.** The harness's `SHELL=/bin/bash.exe` (Git Bash) made Cursor
  `eval` its PowerShell hook wrapper in bash. Started with `SHELL`/`MSYSTEM`/
  `BASH*` removed, the hook runs. **The orchestrator must strip those variables
  when it spawns Cursor.** Separately, tokensave's `PreToolUse` hook in
  `~/.claude/settings.json` had an unquoted spaced path and no `args` as Cursor
  sees it; it was rewritten to one quoted string with the user's approval
  (backup `settings.json.backup.1793504149747`). Whether that rewrite was
  *necessary* was not isolated (the original was not re-tested with a clean
  environment).
- **M2. Claude headless.** Default `--print` **denies writes** and reports them in
  `permission_denials`. `--permission-mode acceptEdits` allows file edits
  (not shell). `--permission-mode plan` and
  `--disallowedTools Write Edit NotebookEdit` each prevented the write, and in
  that default mode a Bash redirect was also denied. **So Claude *can* be made
  read-only.** Gap: only a write was probed, not every tool.
- **M3. Fresh worktree and MCP.** A new worktree has **no** `.cursor/mcp.json`
  (git-ignored, so it is not checked out) and this machine has no global
  `~/.cursor/mcp.json`: `agent mcp list` reports "No MCP servers configured".
  Even in the bound checkout the server reads "not loaded (needs approval)".
  The orchestrator must therefore **write the worktree's MCP config itself**
  and pass `--approve-mcps`. Note `mcp_args` also carries `--force`
  ("run everything"), so a Cursor WRITE run that uses tokensave is
  approve-everything inside the worktree; the worktree is the containment.
- **M4. What a run leaves behind.** Both agents return a `session_id`
  (`--output-format json` for Claude, `stream-json` for Cursor), and Cursor's
  `session_id` matches a folder under `~/.cursor/chats`, so a task can be linked
  to its session. Both also report **usage**: Claude `total_cost_usd` and token
  counts; Cursor token counts (`inputTokens`, `outputTokens`, `cacheReadTokens`,
  no cost). Cursor's `stream-json` lists every tool call, including refused ones.
- **M5. Killing a run.** `Popen.kill()` on the root left **7 descendant processes
  alive** (node, powershell, cmd, conhost). `taskkill /PID <pid> /T /F` removed
  them. Rule: kill the tree with `taskkill /T` **while the root still exists**;
  a plain kill orphans the agent.

### What the measurements change in this design

- **Usage is no longer "unknown".** It is *reported by the agent when it says so*
  (Claude: tokens and cost; Cursor: tokens), stored as `usage: {source:
  "agent-reported", ...}`, never summed across agents and never converted to
  money by the Manager. When absent, `"unknown"`.
- **D3 loosens.** Claude read-only is enforceable (`--disallowedTools` /
  `plan`), so Claude can review; the registry row gains `read_only_args` for
  Claude as a measured fact. The "Cursor reviews" pairing stays a default, not a
  requirement.
- **Execution modes become flag sets, per agent, from the registry:**
  Claude WRITE = `--permission-mode acceptEdits` (edits only); Cursor WRITE =
  `-p --trust` (+ `--approve-mcps --force` when tokensave is used, with the
  worktree as the boundary). Shell-capable runs need `--force` (measured),
  so they are a separate, explicitly chosen mode inside the worktree boundary.
- **Launch gains a step:** write the worktree's MCP config (M3) before the
  agent starts, and verify it with `agent mcp list`.
- **Cancel is `taskkill /T` on the live root** (M5), recorded in the task.
- **The spawn environment is part of the contract:** remove `SHELL`, `MSYSTEM`
  and `BASH*` before starting Cursor; a Manager launched from Git Bash would
  otherwise break every shell call in a way that looks like a hook bug.

## 4. The task record

One JSON file per task, **outside the working tree**:
`<git common dir>/tokensave/orchestration/<task_id>.json` (same rule as the
ledger: shared by every worktree, untrackable). Written atomically (tmp +
replace). The record is the source of truth; no in-memory state is authoritative.

```
task_id        t + 12 hex of (project, kind, created_at, nonce)
schema_version 1
kind           implement | review                (stage C adds more)
parent_task_id review points at the implement task it reviews
project_root   the checkout the person launched it from
worktree_root  the task's own checkout (never project_root)
branch         tsm/<task_id>
base_commit    exact sha the worktree was created from
agent          claude | cursor        model: ""|id
execution_mode READ_ONLY | WRITE                 (section 5)
status         see below
created_at / started_at / ended_at   ISO, local zone
pid            + pid_started_at (to tell a recycled pid from ours)
exit_code      null until known
result_path    captured stdout
diff           {files, insertions, deletions} measured by git, not by the agent
review_status  none | pending | done
cancel_state   none | requested | killed | failed
usage          {source: "agent-reported", ...} or "unknown"
error          string, or ""
```

**Status is a lifecycle, and the words are not interchangeable:**

```
created -> worktree_ready -> running -> exited -> result_ready -> accepted | rejected
                                  \-> cancelled | failed | lost
```

- `exited` means the process ended; says nothing about whether it did the work.
- `result_ready` means the Manager measured a diff and captured output.
- `accepted` / `rejected` are a person's click. Nothing else sets them.
- `lost` = the record says `running` but the pid is gone (Manager crashed,
  machine restarted). **Never assumed finished or failed.** The worktree is kept
  and the person is told what is in it.

On startup the Manager reconciles every `running` record against live
processes (pid AND start time) before showing the Tasks tab.

## 5. Stage A: delegated execution

### Execution modes

`READ_ONLY` and `WRITE` live in the registry row, as capabilities, never as an
`if agent == ...`. A row that cannot enforce a mode **refuses it** with a
message; the prompt asking an agent "please don't edit files" is not
enforcement. This extends `read_only_args`; it adds `write_args` (what a
headless write run needs: unmeasured, M1/M2).

### The launch, in order (each step can stop the task, none is skipped)

1. **Capability check.** The resolved agent must support the requested mode.
   Unmet capability is reported by name, not worked around.
2. **Preflight on the project checkout.** Working tree **clean** (porcelain
   empty) or the launch is refused. Never silently branch from HEAD over
   uncommitted work; never auto-commit the person's changes. The refusal names
   the dirty files.
3. **Limits.** Concurrency and disk (section 9). Over a limit: refused, not queued.
4. **Create the worktree** from the recorded `base_commit` on `tsm/<task_id>`,
   at `<drive>:\_scratch\<repo>-<task_id>` (the repo's existing convention for
   extra checkouts, so Doctor's stray-checkout scan already lists it).
5. **Index it.** `tokensave init <worktree>`; write the worktree's own MCP config
   (a new worktree has none, M3); then verify it points at the worktree. If tokensave is unreachable the
   task is **refused**, not run blind: an agent without the graph reads whole
   files, which is the cost this project exists to avoid.
6. **Brief.** The prompt is the task text plus a brief built from the ledger
   (what other agents decided) via `agent_handoff`. No "files you may not touch"
   list is attempted: scope is enforced by review of the measured diff, not by prose.
7. **Launch** with `Popen` (not `subprocess.run`), stdout/stderr to files in the
   orchestration directory, `cwd = worktree_root`, pid and start time recorded
   **before** anything else can fail.
8. **Wait on a worker thread** with a timeout. On exit record exit code, then
   measure: `git diff --stat base..HEAD` plus working-tree status, in the
   worktree. The diff is **observed**, never taken from the agent's report.
9. **Hand back.** Status `result_ready`. The Tasks tab shows branch, diff stat,
   exit code, captured output, and buttons: *Open diff*, *Open worktree in
   editor*, *Accept*, *Reject*, *Discard worktree*.

### What Accept means (decision D2)

Recommended: **Accept marks the task accepted and offers the next step; it
merges nothing.** The branch stays; the person merges it with the Git tab like
any branch. Rationale: the Manager never became the thing that merges code, and
the existing Git tab already has the guarded paths.

### Cancellation

Cancel kills the whole process tree (M5), keeps the worktree and whatever diff
exists, records `cancel_state`. A cancel that could not kill is `failed`, shown
as such. Quitting the Manager with a running task asks first.

### Concurrency inside a worktree

One task owns a worktree exclusively; a second launch into the same worktree is
refused. Two writers to one file set is the failure this prevents. Different
tasks get different worktrees, so parallel tasks do not share files, but they
can conflict at merge time; the Manager reports overlapping file sets between
open tasks as a fact, and does not try to resolve them.

## 6. Why not run in the person's own checkout

Rejected for stage A. The person is usually editing there; an agent writing
beside them is exactly the dirty-tree problem in section 5, step 2, without the
protection. The cost of a worktree is a second index and some disk, which the
limits bound.

## 7. Stage B: cross-agent review

Starts only after A has been used on real tasks.

- The reviewer receives the **task text, the base commit, the diff and the
  acceptance criteria first**, and the implementer's own narrative **last and
  labelled as a claim**. Handing over the narrative first measurably anchors a
  reviewer on the author's framing.
- The reviewer runs `READ_ONLY`. **If the chosen agent cannot enforce
  read-only, the review is refused**, not run on trust. Both current agents can
  (section 3, M2). The default pairing is still *one implements, the other
  reviews*, because that is the independent case.
- Output is a fixed structure, parsed:
  `confirmed` (defect with file:line and why), `potential` (needs a check),
  `unverified` (reviewer could not tell), `not reviewed` (reviewer did not look).
  Unparseable output is recorded as `not reviewed`, never as "no findings".
- A review is advice attached to the task; it never changes `status`.
- Reviewer and implementer being the same agent is allowed but labelled
  *same-agent review*, since it is not independent.

## 8. Stage C: event-driven (off by default, designed later)

Only constraints are fixed now, so that A and B do not close doors:

- A trigger is **policy**, separate from manual launch: its own setting, its
  own limits, default off, and it can only launch `READ_ONLY` review tasks
  until a person has accepted an unattended write path explicitly.
- Every automatic launch is recorded as automatic and names its trigger.
- No trigger is designed before stages A and B are in daily use.

## 9. Limits (conservative defaults, all in config)

| Limit | Default | When hit |
|---|---|---|
| Running tasks, all projects | 2 | refuse |
| Running tasks per project | 1 | refuse |
| Task wall-clock | 30 min | kill tree, status `failed`, keep worktree |
| Open (unresolved) worktrees | 6 | refuse until one is discarded |
| Free disk on the worktree drive | 2 GB | refuse |
| Review cycles per task | 2 | stop offering |
| Captured output kept | 1 MB per task | truncated and marked |

A refusal always says which limit and what to do. Nothing queues silently.

## 10. Safety invariants (each gets a test)

1. No code path merges, pushes, tags, or commits to a branch other than the
   task's own `tsm/<task_id>`.
2. A task never starts on a dirty project checkout.
3. A task never starts without a verified tokensave index in its worktree.
4. `running` is never inferred from a record alone; it is pid + start time.
5. A lost task keeps its worktree. Discarding a worktree is only ever a click.
6. The diff and file list are measured by git; agent reports are labelled claims.
7. Usage is what the agent reported, labelled so, or `"unknown"`. The Manager derives no figure.
8. A mode the agent's row cannot enforce is refused, not approximated.
9. Quitting never leaves an unreported running process.
10. Nothing here runs unless `enable_agent_orchestration` is literally `true`
    (default false), independent of `enable_agent_handoff`.

## 11. Module plan

Pure first, process-touching last, so most of it is testable without an agent:

- `helpers/orchestration_task.py`: record schema, lifecycle transitions
  (illegal transition = error), atomic read/write, reconcile. No subprocess.
- `helpers/orchestration_limits.py`: the table in section 9 as pure checks.
- `helpers/orchestration_worktree.py`: preflight, create, index, verify MCP,
  measure diff, discard. Git only.
- `helpers/orchestration_run.py`: `Popen`, wait, kill tree, capture.
- `helpers/orchestration_review.py` (stage B): prompt assembly, output parser.
- `controllers/orchestration_ctrl.py`: the Tasks-tab panel, thread-to-Tk bridge.
  Not added to `ai_tasks_ctrl.py`, whose job is single-shot proposals.
- `agent_cli.py`: `write_args`, task-kind routing through `agent_overrides`.
- `cli.py`: `orchestrate --action status|list` (read-only). Launching stays a
  window action until the model is proven; an agent launching agents is stage C
  territory.

Net: roughly five new helpers, one controller. `docs/ARCHITECTURE.md` counts,
the module-layout guard, `BASIC_INSTRUCTIONS.md` (the new config keys),
`ROADMAP.md` and a changelog fragment move in the same commits.

## 12. How it will be tested

- **A fake agent**: a small Python script standing in for `agent`/`claude`,
  with scripted behaviour (writes a file, exits 0; exits 3; hangs; ignores
  SIGTERM; writes to stderr). Lets every lifecycle path run deterministically
  without a model.
- **Mutation checks** on the guards in section 10: remove the clean-tree check,
  trust the agent's file list, treat a stale pid as running, skip the tokensave
  verification; each must fail a test.
- **Real checks** on a scratch repo with real Cursor (write run, cancel, lost
  task after killing the Manager, a dirty tree refusal), reported as
  observations with the date, like the Phase 1 measurements.
- **Restart recovery** tested by writing a `running` record with a dead pid.

## 13. Decisions needed from you

| # | Question | Recommendation |
|---|---|---|
| D1 | Worktree location | `<drive>:\_scratch\<repo>-<task_id>`: matches the existing extra-checkout rule and Doctor already lists them |
| D2 | What *Accept* does | Marks accepted, merges nothing; you merge in the Git tab |
| D3 | Reviewer | Approved; loosened by M2: either agent may review, default is the other one |
| D4 | Launch surface | Window only for now; no CLI/agent-initiated launch |
| D5 | Limits in section 9 | Accept as defaults, tune after use |
| D6 | Order | Done: M1-M5 measured (section 3). Stage A next |
| D7 | Flag | New `enable_agent_orchestration`, separate from handoff, default false |

If any recommendation is wrong for how you actually work, say so before M1-M5:
the measurements are cheap, the module plan is not.

## 14. Stage A as built (2026-11-01), and where it differs from this document

Built: `helpers/orchestration_task|limits|worktree|run.py`, the registry fields
(`write`, `write_args`, `task_print_args`, `task_output`, `task_mcp_flag`,
`task_mcp_file`, `agent_cli.build_task_argv`), `controllers/orchestration_ctrl.py`
(the Tasks tab's "Agent tasks" view), `dialogs/agent_task_dialog.py`, the
`enable_agent_orchestration` switch and the optional `orchestration_limits` dict.
Tested with a fake agent for every lifecycle path (including a cancel that must
leave no process, and a task recorded as running whose process is gone), and run
live against the real Cursor and Claude CLIs on a scratch repo.

Differences, each from something measured while building it:

- **Preflight refuses TRACKED changes only.** Untracked files are counted and
  reported, not refused: a worktree is made from a commit, so they are simply not
  in the task's copy, and a scratch file should not forbid delegating. (Section 5
  step 2 said "porcelain empty".)
- **Claude reaches tokensave by `--strict-mcp-config --mcp-config <file>`, never by
  a project `.mcp.json`.** Measured: with no project file at all Claude still had
  tokensave tools, from a user-scope entry, which could serve the wrong project;
  `--strict-mcp-config` with an empty config had none. So the task's config is a
  file OUTSIDE the worktree naming one server with `-p .`, and nothing else can
  serve it. Cursor reads `.cursor/mcp.json` inside the worktree, which the
  Manager writes and excludes from the measured diff.
- **The brief is a short list of recent handoff entries**, labelled as other
  agents' claims, appended to the task text. The full `agent_handoff` brief is
  written for a person handing a session over, not for a task.
- **A non-zero exit, or an agent that reports an error, is `failed`** (its diff is
  kept and its copy retained), never `result_ready`, so a half-done task cannot be
  accepted by accident.
- **Usage** is recorded exactly as the agent reported it, labelled
  `agent-reported`; nothing is summed or converted to money.
- **Quitting** with a task running asks, then cancels every task (whole tree) and
  lets each worker record how it ended. Closing from the tray path is covered too.
- **The Cursor task run is approve-everything inside its worktree** (`--approve-mcps
  --force` are what lets it use tokensave headlessly). The worktree and the
  edit-only scope of the prompt are the boundary; shell use is not offered.
- **Model blank means the agent's own default**, not the Manager's cheap default,
  because a delegated coding task is the wrong place to inherit "fast and cheap".
