# Roadmap-5: Pre-merge quality checks, upstream detection, tokensave integration audit, and expanded tooling

## Summary
Major feature release adding pre-merge AI code review, upstream branch detection, tokensave integration health checks, GitHub PR direct creation, clause CLI model selection, and comprehensive governance documentation. Includes new controllers (branch management, AI tasks, doctor), dialogs (proposal, checks, cost viewer, refactor scout), and helper infrastructure (changelog patch, precommit hook, commit-message orchestrator, pr-draft, refactor-scout).

## Key Changes

### Core Infrastructure
- **CI/CD**: Add GitHub Actions workflow for syntax check + pyflakes audit on push/PR
- **Documentation overhaul**: 
  - `BASIC_INSTRUCTIONS.md` rewritten with operational working rules for Claude (anti-monolith caps, documentation discipline, tokensave-first exploration, project guard-rails)
  - `docs/ARCHITECTURE.md` expanded with Stage 0–5 shipping status and architectural decisions
  - `docs/ROADMAP.md` comprehensive completion sweep with Roadmap 1–3 shipped backlog, Roadmap 4–5 planning
  - New `docs/UPGRADE_INTEGRATION.md` workflow for tokensave version compatibility checks
- **Integration health check**: `scripts/check_tokensave_integration.py` validates tokensave MCP version compatibility, Python path, and CLI availability

### Agent & Tool Layer
- **Agent write-tool dispatch**: `src/agent.py` Stage 3 write-tool support via injected `on_write_proposal` bridge; `src/dialogs/proposal.py` new ProposalDialog for user approval of AI-suggested code changes
- **Enhanced tool registry**: `src/agent_tools.py` adds optional `write_file` tool; read-only tools now include `tokensave_search` and `tokensave_context`
- **Precommit review hook**: `src/precommit_review.py` + `src/helpers/precommit_hook.py` integration — AI code review at git pre-commit time with diagnostic mapping and severity filtering

### Controllers & Dialogs
- **Branch management extraction**: `src/controllers/branch_mgmt_ctrl.py` extracted from `git_tab.py` (upstream detection, branch switching, PR workflow orchestration)
- **AI tasks controller**: `src/controllers/ai_tasks_ctrl.py` new controller for background AI operations with task queue, progress reporting, and cancellation
- **Upstream detection**: `src/controllers/git_tab.py` adds upstream branch detection logic; helps identify tracking branch for PR creation
- **Doctor controller**: `src/controllers/doctor_ctrl.py` expanded with pre-merge checks (syntax, pyflakes, Doctor audit) via new `src/dialogs/checks_dialog.py`
- **New dialogs**:
  - `src/dialogs/proposal.py`: ProposalDialog for AI-suggested code write operations (bridge between agent and user approval)
  - `src/dialogs/checks_dialog.py`: Pre-merge quality checks runner (syntax, pyflakes, codebase health audit)
  - `src/dialogs/cost_viewer.py`: Real-time API token cost tracker
  - `src/dialogs/refactor_scout.py`: Refactoring opportunity discovery UI

### Helpers & Utilities
- **Claude CLI integration**: 
  - `src/helpers/claude_cli.py` spawns Claude CLI subprocess with model parameter (`--model` override)
  - `src/controllers/help_tab.py` + `src/controllers/ask_tab.py` add Claude CLI installation button + model selector
  - `src/controllers/git_tab.py` offers direct GitHub PR creation via `gh pr create` with title field
- **Changelog & release tooling**:
  - `src/helpers/changelog_patch.py`: Parse/mutate CHANGELOG.md (insert unreleased entries, bump version, render release notes)
  - `src/helpers/pr_draft.py`: Draft PR summary generator
  - `src/helpers/release.py` enhanced with `_last_release_tag`, `_commits_since`, `_classify_commits_for_changelog`, version bumping
- **Commit message orchestrator**: `src/helpers/commit_messages.py` multi-strategy (auto/llm_first/claude_cli/llm) backend dispatcher with min-diff threshold and local provider fallback
- **Refactor scout**: `src/helpers/refactor_scout.py` identifies code-smell clusters (duplication, cyclomatic complexity, coupling, dead code) for manual refactoring campaigns
- **Built-in prompts**: `src/prompts.py` ROM defaults for Claude instruction snippets (overridable in settings)

### Documentation & Guides
- **Upstream issues tracker**: 
  - `docs/upstream-issues/tokensave-daemon-*.md` (3 new): daemon lifecycle gotchas (no-window flag, status polling, graceful stop on Windows)
  - Updated health-details, hook-quoting, redundancy-tool documentation
- **MCP integration gotchas**: Explicit guide for `tokensave-wrapper.py` thread safety and stdio inheritance
- **Roadmap decisions**: Logged architectural choices (no LangChain, propose-only AI, no RAG, stages 0–3 shipped, 4–5 planned)

### Code Quality
- **Pyflakes baseline** cleared across `src/` (no unused imports, no dead code)
- **Anti-monolith governance** enforced: file length caps (1500 soft limit), method length (100 lines for logic; layout methods exempt if CC ≤ 3), class method count (40), cyclomatic complexity (10)
- **Doctor audit** refactored with non-Python text-file audit, file-size cap softened
- **Settings dialog** layout refactored: new sections must pack on `body` Frame (canvas wrapper), not `self`

## Testing Checklist

- [ ] **CI/CD**: Trigger GitHub Actions on a test push; verify syntax check and pyflakes pass
- [ ] **Tokensave integration**: 
  - Run `python scripts/check_tokensave_integration.py` with both compatible and incompatible tokensave versions
  - Verify Reference tab shows "🔄 Integration audit" snippet for manual CLI check
- [ ] **Agent write proposals**: 
  - Open Ask tab, request a code change (e.g., "refactor this function")
  - Verify ProposalDialog appears with user-approval-required flow
  - Confirm write-file tool in proposal is read-only until approved
- [ ] **Pre-merge checks**:
  - Open Git tab, trigger "Check before merge" dialog
  - Verify syntax, pyflakes, and Doctor audit run; failures block merge suggestion
- [ ] **Branch management**:
  - Switch projects and verify upstream detection identifies tracking branch
  - Test PR creation via "Create PR" button; verify title field and `gh pr create` invocation
- [ ] **Claude CLI integration**:
  - Test model selector in Ask tab; verify selected model is passed to CLI
  - Verify commit message suggestion via Claude CLI (when configured)
- [ ] **Cost viewer**: 
  - Enable cost tracking in settings
  - Run an Ask operation; verify cost viewer shows token usage
- [ ] **Refactor scout**:
  - Open refactor scout in Doctor tab
  - Verify duplication, cyclomatic complexity, coupling clusters are discovered correctly
- [ ] **Help & documentation**:
  - Verify Help tab links to updated BASIC_INSTRUCTIONS.md, ROADMAP, ARCHITECTURE
  - Confirm Claude CLI install button appears when CLI not detected

## Migration Notes

- **Configuration schema stable**: No breaking changes to `manager-config.json`; new optional keys auto-hydrated with defaults
- **Settings migration**: If upgrading from Roadmap-4, settings will auto-migrate; no manual intervention needed
- **Tokensave compatibility**: Check `scripts/check_tokensave_integration.py` output after any tokensave version upgrade (see `docs/UPGRADE_INTEGRATION.md`)
