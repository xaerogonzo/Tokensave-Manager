# Tokensave Upgrade Integration Guide

When a new tokensave version ships it may add MCP tools, remove commands, fix
upstream issues, or change tool schemas.  This document describes the repeatable
workflow the manager provides to surface those gaps quickly and without burning
LLM tokens on the routine parts.

---

## Why this matters

The manager's `src/prompts.py` snippet bodies call tokensave MCP tools by name.
If tokensave removes a tool (e.g. the daemon was removed in v6.0.0) and the
corresponding snippet still references it, Claude will get a tool-not-found error
mid-session.  Conversely, new tools ship without any manager snippet until someone
writes one.

The integration workflow below catches both classes of problem.

---

## The sequence (ORDER MATTERS)

The check script reads **local files only** — it does not fetch anything from the
network.  Running it before upgrading or pulling the latest repo will produce a
false-clean report because the local files won't reflect the new version yet.

```
1.  tokensave upgrade          ← update the binary
2.  git pull                   ← update this repo's CHANGELOG.md, docs/, src/
3.  🔍 Check integration       ← free deterministic checks (see below)
4.  🔄 Integration audit       ← LLM prompt for new-tool discovery (see below)
```

---

## Step 3 — Free deterministic check

### From the manager UI (recommended)

- **Settings → "🔍 Check integration"** button (next to the Upgrade button)
- **Right-click any project → "🔄 Integration check"**

Both open a scrollable report dialog showing:

| Section | What it checks |
|---------|----------------|
| **Installed version** | Reads `tokensave --version` via the configured `tokensave_exe` |
| **Upstream issues** | Scans `docs/upstream-issues/*.md` for `STATUS:` lines; flags any that aren't FIXED / SHIPPED / MOOT |
| **Stale snippet references** | Finds tool names in CHANGELOG `### Removed` blocks; checks whether any snippet body in `src/prompts.py` still calls them |

### From a terminal (source-only)

```
python scripts/check_tokensave_integration.py
```

Exit code is always 0 — this is an advisory report, not a blocking check.

### What it does NOT check

- **New tools without snippets** — detecting this reliably requires reading tokensave's
  own changelog (the manager's CHANGELOG records manager changes, not tokensave tool
  additions).  This gap is covered by the LLM audit in Step 4.

---

## Step 4 — LLM integration audit

Open the **Reference tab → copy "🔄  Integration audit (after upgrade)"** and paste
it into a **Claude Code CLI session** (`claude`) opened in this project's directory.

The prompt instructs Claude to:

1. Call `tokensave_changelog` — reads tokensave's own release notes directly
2. Cross-reference new/removed tool names against `src/prompts.py` snippet bodies
3. Read `docs/upstream-issues/*.md` and flag which issues the changelog resolves
4. Scan `helpers/daemon_cost.py`, `ai_tasks_ctrl.py`, `agent_tools.py` for wrappers
   of removed tools
5. Produce a structured action list: new snippets needed / issue docs to update /
   code changes / no-action confirmed

A companion snippet **"🔄  Generate snippet for [[new tool name]]"** then writes the
`src/prompts.py` entry for each new tool the audit surfaces.

---

## Writing a new snippet

After the audit identifies a new tool, use the "Generate snippet" prompt or write
one manually following these rules (enforced by the "Generate snippet" prompt body):

- **Title**: emoji prefix + concise name, e.g. `🔗  tokensave_call_chain`
- **Body**: 3–5 numbered steps; show the exact tool call and what to do with the result
- **Placeholders**: use `[[double brackets]]` for things the user fills in
- **Length**: 60–120 words (long enough to be self-contained, short enough to read in
  one pass)
- **Superseded tools**: if the new tool replaces an existing one, flag the old snippet
  in your PR description so it can be updated or removed

Add the new tuple to the `🔄 UPGRADE INTEGRATION` block at the bottom of
`src/prompts.py`, or create a new thematic section if several new tools arrived.

---

## Update upstream-issue docs

For each issue in `docs/upstream-issues/` that the new release resolves:

1. Change the `STATUS:` line to `STATUS: FIXED vX.Y.Z` (or `SHIPPED` / `MOOT`)
2. Add a one-line note under the status: `Resolved in tokensave vX.Y.Z — <release tag>.`

The check script (Step 3) will then show ✓ for that file on future runs.

---

## Hint in the OUTPUT pane

When the hourly GitHub poller detects a new tokensave release for the first time, the
OUTPUT pane shows two lines:

```
→ tokensave X.Y.Z → A.B.C ready to install.  Settings → 'Upgrade tokensave to vA.B.C'
  to apply, or run 'tokensave upgrade' from a shell.
→ Integration workflow: upgrade tokensave → git pull this repo →
  python scripts/check_tokensave_integration.py → '🔄 Integration audit' snippet
  in Reference tab.
```

This is the trigger to begin the workflow above.

---

## Files involved

| File | Role |
|------|------|
| `scripts/check_tokensave_integration.py` | Free deterministic audit script |
| `src/prompts.py` | Snippet bodies — updated when new tools need coverage |
| `docs/upstream-issues/*.md` | Per-issue tracking docs — updated as fixes ship |
| `CHANGELOG.md` | Manager's own release history (also parsed by the check script for `### Removed` tool names) |
| `src/controllers/update_poller.py` | `UpdatePollerController` — hosts `cmd_integration_check()`, version probe, poller, and upgrade command |
| `src/app.py` | `cmd_integration_check()` thin wrapper (mirrors `cmd_upgrade_tokensave`) |
| `src/dialogs/settings.py` | "🔍 Check integration" button next to the Upgrade button |
| `src/controllers/projects_tab.py` | "🔄 Integration check" right-click menu item |
