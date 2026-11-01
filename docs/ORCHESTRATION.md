# Orchestration: design (draft for review, no code yet)

Status: **proposal.** Nothing in this document is built. It exists so the
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
| Read-only enforcement | `AgentCLISpec.read_only_args` | Cursor: `--mode ask`. **Claude: empty, i.e. not established** |
| Propose, then apply | `ProposalBridge` (`dialogs/proposal.py`) | Built for one file edit; a branch diff is a different payload |
| Don't double-start | `AITasksController._running` | A UI guard only; lost on restart |
| Find stray worktrees | `worktree_health`, `worktree_cleanup`, Doctor stray-checkouts | Knows nothing of task ownership |
| See sessions | Tasks tab scanners for both agents | Reads transcripts, not Manager-launched tasks |

## 3. Facts that are NOT established (measure before coding)

Each is a prerequisite, not a footnote. The earlier phases kept paying for
guesses of this kind (`~/.local/bin`, `-p` can write files).

- **M1. What flags make a Cursor headless run WRITE?** Measured so far: `-p`
  alone "has access to all tools"; `--mode ask` blocks writes; `--force`
  approves everything. Not measured: whether a write run needs `--force`, what a
  refused shell command looks like in output, and the exit code on refusal.
- **M2. How does Claude Code write headless, and can it be made read-only?**
  `--print` plus a permission mode or `--allowedTools`. Unmeasured. Until
  measured, **Claude cannot be a reviewer** (section 7) and a write run for
  Claude cannot be launched.
- **M3. Does an agent started in a fresh worktree reach tokensave?** Cursor's
  `.cursor/mcp.json` is per-project and git-ignored, so a new worktree has none.
  Claude's `.mcp.json` may or may not be tracked. Measured only for the main
  checkout.
- **M4. What does a headless run leave behind?** Final text only, or also a
  transcript path? Needed to link a task to its session.
- **M5. Can a process tree be killed cleanly on Windows?** `agent.cmd` is a
  shim; the real process is a child. Needs `taskkill /T` behaviour measured
  against the real binaries.
- **Usage/cost: unknown by design.** No supported source was found. The record
  stores `usage: "unknown"`; nothing is estimated and no dollar figure appears.

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
usage          "unknown"
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
5. **Index it.** `tokensave init <worktree>`; then verify the agent's MCP config
   in the worktree points at the worktree (M3). If tokensave is unreachable the
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
  read-only (Claude today), the review is refused**, not run on trust. When M2
  is measured this may change. Because of that, the useful default pairing is
  *Claude implements, Cursor reviews* until Claude's read-only mode is shown.
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
7. Unknown usage is `"unknown"`. No figure is derived.
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
| D3 | Reviewer before M2 is measured | Cursor reviews, Claude implements; Claude-as-reviewer refused until its read-only mode is shown |
| D4 | Launch surface | Window only for now; no CLI/agent-initiated launch |
| D5 | Limits in section 9 | Accept as defaults, tune after use |
| D6 | Order | Measure M1-M5 first (one session, no product code), then stage A |
| D7 | Flag | New `enable_agent_orchestration`, separate from handoff, default false |

If any recommendation is wrong for how you actually work, say so before M1-M5:
the measurements are cheap, the module plan is not.
