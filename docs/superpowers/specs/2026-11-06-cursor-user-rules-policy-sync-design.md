# Cursor User Rules and TokenSave Agent Policy sync

**Date:** 2026-11-06
**Branch / worktree:** feat/cursor-rules-policy-sync at D:/_scratch/Token-Save-Manager-Source-cursor-rules-policy-sync
**Cursor measured:** 3.29.9 (user setup), commit 306d153d51d714882ab7b1bc49649cab624e8a30

## Goal

When TokenSave Manager enables Allow automatic commits / Allow automatic pushes, Cursor agents in wired projects must not keep refusing because of a conflicting "only create commits when requested" instruction. Manager owns the policy. Alex does not maintain two git policies by hand.

## Measured facts (this machine, this version)

1. Customize → Rules (Command Palette, not Settings → Agents) is text-only. Alex's User cards were: PR body, two Context.dev plugin rules, name. No git-commit card.
2. The sentence "Only create commits when requested by the user" is not in settings.json and is not a titled User Rule. It is generated in Cursor's agent runtime (`extensions/cursor-agent-exec/dist/main.js`, a function that builds a "Committing changes with git" section; also present in `cursor-agent-host` and `cursor-local-agent-runtime`). It is injected into the agent prompt as a synthetic user rule.
3. That generator's own wording allows some git limits to yield to "a different user rule".
4. Titled User Rules are visible via Cursor's rule API with cloud-like ids. They are not a documented file under `%APPDATA%\Cursor\User\`. `%APPDATA%\Cursor\User\globalStorage\state.vscdb` ItemTable has no dedicated rules key; the git-commit text there is only inside chat transcripts (`cursorDiskKV` agent blobs).
5. TokenSave already compiles `agent_may_commit` / `agent_may_push` into `templates/project-baseline.md` then `AGENTS.md` (`TOKENSAVE-MANAGED` blocks). `.cursor/rules/tokensave.mdc` is a short pointer at `AGENTS.md` so the baseline is not loaded twice.
6. A prior Cursor session treated the synthetic user rule as overriding workspace `AGENTS.md`.

Implication: Option A (project `.mdc` only) will not reliably win. Option B (edit the Customize list to delete the default) cannot, because the default is not in that list. We never patch Cursor.exe or workbench JS.

## Decision (approved)

Hybrid C:

- Keep Agent Policy → baseline → `AGENTS.md` as the source of truth.
- Upsert one titled User Rule card, stable title `TokenSave git policy`, whose body is compiled from the same policy and tells the agent to follow the `TOKENSAVE-MANAGED` git blocks in `AGENTS.md`.
- Leave `.cursor/rules/tokensave.mdc` as the existing `AGENTS.md` pointer (no second baseline copy).
- If the User Rule store cannot be written safely, fall back to a documented paste of that exact card (one-time migration), not a silent no-op that looks like success.

## Architecture

```
manager-config.json  (InstructionPolicy)
        |
        v
baseline_blocks.compile  ->  templates/project-baseline.md
        |
        +-- write_agents_md     ->  each wired project's AGENTS.md
        +-- cursor_user_rules   ->  one machine-wide User Rule card
                                    (or paste blob + skip reason)
```

Policy is per Manager installation, not per repo (existing `instruction_policy` contract). The User Rule is therefore machine-wide, applied once per Composer apply, not once per Retrofit project. Retrofit must not N-write the global card.

Direction of truth is unchanged: config is right; Markdown and the User Rule are compiled artifacts. Drift is detected by re-rendering and comparing, never by parsing intent out of Cursor's store.

## User Rule body (normative)

Title: `TokenSave git policy`

When commits are allowed (and optionally pushes), the body must:

- Point at `AGENTS.md` `TOKENSAVE-MANAGED` `agent_may_commit` / `agent_may_push` blocks as the git policy for TokenSave-wired projects.
- State that those blocks are an explicit user rule for this machine, so Cursor's built-in "only create commits when requested" does not block the allowed actions.
- Repeat existing hard limits: no force-push, no hook skip, no rewriting already-pushed history.

When commits are off, the same card stays (do not delete it). Its body says follow `AGENTS.md` (do not create commits / do not push). Toggling off must still win.

Exact English is owned by `baseline_blocks` or a sibling renderer in the new helper so tests can assert bytes, not tone.

## Components

| Piece | Role |
|---|---|
| `helpers/instruction_policy.py` | Unchanged model. |
| `helpers/baseline_blocks.py` | Unchanged AGENTS.md compiler. |
| `helpers/cursor_user_rules.py` (new) | Discover store for Cursor 3.29.x on Windows; read; backup; idempotent upsert of the one managed card; dry-run; structured skip. |
| Instruction Composer apply path | After a successful baseline write, call the upsert once. Preview already shows baseline diff; also show User Rule would-be title + body + store path or skip reason. |
| `helpers/agent_rules.py` | No baseline in `.mdc`. No new file required for v1. |
| Tests | Fake store fixtures only. |
| README | One paragraph under Git and Policy / Cursor. |

## Persistence and write safety

Probe order (implementation may stop at the first writable, understood store):

1. Cursor titled-rule API shape as observed (list/add/update by id) only if a local, documented-on-disk equivalent exists that Manager can read/write without attaching to a live Cursor process.
2. Any JSON/sqlite user-data file under `%APPDATA%\Cursor\` whose schema is identified as the titled-rule list (not `state.vscdb` chat blobs, not `cursorDiskKV` agentKv).
3. Else: skip and paste. Copy the card text to the Composer result / log. Do not invent a write into `state.vscdb`.

Rules for a write, when a store is found:

- Backup previous managed-card body (timestamped file under Manager's log/backup dir, or sibling `.bak` next to a plain file).
- Idempotent: same title + same compiled body → no write, `changed=False`.
- Unexpected schema / missing keys / lock → skip, do not truncate.
- Never write `%LOCALAPPDATA%\Programs\cursor\**`.
- Document: a live Cursor may need a reload (or a new agent chat) before the card is picked up; v1 does not force-quit Cursor.

If the store is cloud-authoritative and the local file is a cache, treat it as unwritable (paste fallback). Overwriting a stale cache would look like success and then get clobbered.

## Data flow (apply)

1. User toggles Agent Policy in the Composer and applies.
2. Manager validates `InstructionPolicy`, compiles baseline, writes `project-baseline.md`.
3. Wired-project `AGENTS.md` updates stay as they are today (fleet compile / existing apply).
4. `cursor_user_rules.apply(policy, dry_run=...)` runs once.
5. Log one line: `cursor user rule: updated|unchanged|skipped (reason) path=...`.
6. UI shows that line. Skip is visible, not folded into "baseline written".

## Error handling

- Missing Cursor install / missing AppData: skip, paste, continue (baseline still applied).
- Policy invalid: existing Composer refusal; no User Rule write.
- Partial failure (baseline written, User Rule skipped): report both outcomes. Do not roll back the baseline; they are different stores.
- Never emit force-push or `--no-verify` permission.

## Tests

`tests/test_cursor_user_rules.py` (names may match repo style):

- Missing store → skip + paste blob contains the compiled body.
- Unexpected schema → skip, fixture bytes unchanged.
- First apply creates the managed card; second apply is a no-op.
- Toggle commit off rewrites the card body (not delete).
- Backup file contains previous body.
- Dry-run does not write.
- Path filter: refuse any path under `Programs\cursor`.

No GUI drive, no live `%APPDATA%\Cursor` in CI.

## Docs

README, Git and Policy (or Cursor subsection): Cursor Rules in Customize are text-only; the "only commit when asked" line is a Cursor runtime default, not a card in that list; Manager upserts (or pastes) the `TokenSave git policy` User Rule from Agent Policy so the two do not fight. Point at this spec for the measurement.

No Agent Policy UI redesign.

## Non-goals

- Changing TokenSave's in-app Agent Policy controls beyond wiring this apply step.
- Force-push / hook-bypass.
- Patching Cursor.exe or `workbench*.js`.
- Syncing plugin rules, project `.mdc` besides the existing tokensave pointer, or Team rules.
- Auto-reload of a running Cursor instance.

## Success

Enabling commit/push in Manager either (a) leaves a titled User Rule that tells Cursor agents to follow `AGENTS.md` git blocks, without Alex editing Customize by hand, or (b) the apply result shows the exact paste and says why the store was not writable — a one-time migration, not a silent conflict.

## Open implementation detail (not a product question)

Exact on-disk format of titled User Rules for 3.29.9 was not a plain file at probe time. The helper's first task is to classify the store as writable local vs cloud cache vs absent, then take the skip path if unsure. Shipping paste-fallback is success; shipping a speculative sqlite write is not.
