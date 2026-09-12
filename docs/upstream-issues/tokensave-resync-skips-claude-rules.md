<!--
STATUS: DRAFT 2026-09-12 — not filed. Review before posting; nothing inside
  this comment is part of the issue body.

DUPLICATE SEARCH (gh search issues, repo-scoped, 2026-09-12):
  "resync installed_agents rules" (0), "rules file not refreshed upgrade" (0),
  "migrate_installed_agents" (0), "installed_agents" (#491 OMP profile
  ownership, #191 CLAUDE_CONFIG_DIR — both unrelated), "silent reinstall"
  (#236 Droid hook, #84 timeout — unrelated).

  #540 (filed from this repo, CLOSED, shipped in 7.12.0) is the rules-text
  fix this report says never arrived. Not a re-ask: #540's change is correct
  and is present in the binary; the delivery path is the defect.

LOCAL EVIDENCE (not for the issue body):
  ~/.tokensave/state.toml before any post-upgrade command:
    last_installed_version = "7.11.1", previous_version = "7.11.1",
    installed_agents = ["copilot"]
  First interactive command after upgrading to 7.12.1 printed on stderr:
    "Refreshed agent config for tokensave 7.12.1 (was 7.11.1) — 1 agent"
  ~/.claude/rules/tokensave.md afterwards: unchanged (Aug 30 text, zero
  occurrences of tokensave_read). `install --agent claude --local` in a temp
  project with HOME redirected writes the 7.12.1 text, so the binary has it.
  Why claude is untracked here: the Manager retired the user-scope
  ~/.claude.json tokensave entry in favour of per-project .mcp.json.
-->

# Upgrade resync never refreshes the Claude rules file when `claude` is not in `installed_agents`, so rules-text fixes (#540) do not arrive

**tokensave 7.11.1 → 7.12.1** · Windows 11 · Claude Code, with tokensave registered per project in `.mcp.json` rather than in `~/.claude.json`

## Summary

#540 made the managed rules file name `tokensave_read`, and 7.12.0 ships it. On a machine that upgraded from 7.11.1, `~/.claude/rules/tokensave.md` still has the old text after the minor-version resync ran, and it will stay that way through every future upgrade.

The resync re-runs `install` only for the agents in `state.toml` `installed_agents`. On this machine that list is `["copilot"]`, even though `~/.claude/rules/tokensave.md` exists and is tokensave's own file.

## Why `claude` is missing from the list

- `resync_installed_agents` (`src/agents/mod.rs`) iterates `config.installed_agents` and nothing else. Startup maintenance never calls `migrate_installed_agents`; only `install`, `reinstall` and `uninstall` do.
- `migrate_installed_agents` would not add Claude here anyway. Claude's `has_tokensave` is true only when `~/.claude.json` has `mcpServers.tokensave`. A user who registers tokensave per project in `.mcp.json`, or removed the user-scope entry, is never detected.
- A tokensave-owned rules file on disk counts for nothing in either check.

## Measured

- Before the first post-upgrade command, `state.toml` held `last_installed_version = "7.11.1"`, `previous_version = "7.11.1"` and `installed_agents = ["copilot"]`.
- That command printed: `Refreshed agent config for tokensave 7.12.1 (was 7.11.1) — 1 agent`.
- Afterwards, `~/.claude/rules/tokensave.md` was unchanged, with zero occurrences of `tokensave_read`.
- `tokensave install --agent claude --local` in a scratch project writes the 7.12.1 text. So the binary has the fix, and only delivery is missing.

## The ask

Refresh an **existing** tokensave-managed rules file on upgrade whether or not its agent is in `installed_agents`, **without** re-running the full `install`.

The full install would re-add a user-scope MCP entry that the user deliberately does not have. The rules file is already exclusively tokensave's (`write_managed_rules_file` overwrites it whenever the content changes), so rewriting just that file is inside the contract.

The obvious alternative, having Claude's `has_tokensave` count the managed rules file as an installed integration, is worse. `installed_agents` feeds a full install on every minor bump, so it would re-add the MCP entry. The narrower refresh avoids that.

## What this is not

- **Not a re-ask for #540.** That text change is correct and is in the binary.
- **Not a request to track project-scoped installs.** `--local` is rightly excluded from `installed_agents`, because `reinstall` replays entries as global installs.
