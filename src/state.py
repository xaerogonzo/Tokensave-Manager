"""ManagerConfig — the runtime-mutable settings store.

App holds one instance; future controllers and dialogs receive it via
`__init__(cfg: ManagerConfig)` and read live values through the property
getters (`cfg.git_exe`, `cfg.tokensave_exe`, etc.). When the user saves
Settings, `App._on_settings_saved` mutates `cfg.raw` in place and calls
`cfg.refresh_derived()` — all holders of the instance see the new values
without anyone having to re-bind a global.

Round 4 Phase A locks in two rules from `docs/plans/...`:

  * **Rule 3 — no caching of derived values in caller __init__.**
    Read `self._cfg.git_exe` at execution time, never snapshot it at
    construction. Cached snapshots become stale after a Settings save.

  * **Rule 5 — derived fields are read-only `@property`.** Direct
    assignment (`cfg.git_exe = "..."`) raises AttributeError. The ONLY
    supported mutation path is:
        ``cfg.raw.update(new_values); cfg.save(); cfg.refresh_derived()``

The class is dataclass-style so callers can construct one cheaply in
tests with `ManagerConfig(raw={"git_exe": "/usr/bin/git", ...})`, but
real production usage goes through `ManagerConfig.load()` which reads
+ migrates `manager-config.json` from disk.

This module imports from `helpers.detection` and `helpers.config` *lazily*
inside `refresh_derived()` and `load()` / `save()` — never at module
load — so `state.py` stays free of circular-import risk. Other modules
can `from state import ManagerConfig` for `TYPE_CHECKING` type hints
without dragging the helper graph along with them.
"""

from __future__ import annotations

import dataclasses
import os


@dataclasses.dataclass
class ManagerConfig:
    """Runtime-mutable settings, backed by a JSON dict on disk.

    The only writable surface is `self.raw` (the underlying dict).
    All derived fields are read-only `@property` getters that read from
    `self.raw` plus a small cache for fields that require an expensive
    fallback (subprocess shells for `_detect_git` / `_detect_codegraph`).
    """

    raw: dict

    # ── Direct dict-lookup properties (cheap, no caching needed) ──────────

    @property
    def tokensave_exe(self) -> str:
        """Path to tokensave.exe (empty string if not configured)."""
        return self.raw.get("tokensave_exe", "")

    @property
    def template_dir(self) -> str:
        """Folder holding template files (BASIC_INSTRUCTIONS, baseline gitignore, etc.).

        Falls back to `<repo>/templates` when not explicitly configured.
        """
        from constants import _BASE_DIR
        return self.raw.get("template_dir", "") or os.path.join(_BASE_DIR, "templates")

    @property
    def search_roots(self) -> list:
        """List of search-root entries (each entry is str or {"path","label"})."""
        return self.raw.get("search_roots", [])

    @property
    def draft_pr_backend(self) -> str:
        """Backend for the Draft PR feature: 'auto' | 'claude_cli' | 'llm'.

        'auto' prefers Claude Code CLI if configured, falls back to API key.
        """
        return (self.raw.get("draft_pr_backend") or "auto").lower()

    @property
    def claude_cli_model(self) -> str:
        """Model passed to manager-spawned `claude --print` calls via --model.

        Empty string means: don't pass --model, let Claude CLI use its own
        default (from ~/.claude/settings.json — Opus 4.7 for Max subscribers).
        Defaults to Haiku 4.5 because the manager's automated calls (pre-commit
        review, commit-message Suggest, Draft PR via CLI) need to be fast.
        """
        # Defensive: raw.get(key, default) returns None (not the default) if
        # the user manually wrote `"claude_cli_model": null` in the JSON, and
        # tk.StringVar(value=None) coerces to the literal string "None".
        val = self.raw.get("claude_cli_model")
        return val if val is not None else "claude-haiku-4-5-20251001"

    @property
    def cursor_cli_model(self) -> str:
        """Model passed to manager-spawned Cursor Agent calls via --model.

        Defaults to empty — let Cursor choose. Deliberately NOT mirrored from
        `claude_cli_model`: Anthropic model ids are not valid Cursor model ids,
        so copying that default would send every Cursor call a name it rejects.
        """
        val = self.raw.get("cursor_cli_model")
        return val if val is not None else ""

    @property
    def agent_cli(self) -> str:
        """Which agent CLI the manager shells out to: "claude" | "cursor".

        Returned RAW, exactly as persisted, including values this build does
        not recognise. Validation belongs to `resolve_agent_cli`, which can
        report an unknown id as a configuration error; coercing it to the
        default here would silently run Claude while the settings file said
        otherwise. An absent key returns the default, which is what every
        pre-Cursor configuration implicitly had.
        """
        from helpers.agent_cli import DEFAULT_AGENT_ID
        val = self.raw.get("agent_cli")
        return DEFAULT_AGENT_ID if val is None else str(val)

    @property
    def enable_llm_grounding(self) -> bool:
        """Master switch for tokensave/codegraph grounding across LLM features.

        When True (default), commit-message draft, PR draft, AI code review,
        and the doc drafter all splice a tokensave + codegraph grounding
        block into the LLM prompt. When False, every grounding callsite
        short-circuits to empty before doing any subprocess work.

        Defaults to True; existing configs without the key upgrade
        seamlessly via the explicit None-check below.
        """
        val = self.raw.get("enable_llm_grounding")
        return True if val is None else bool(val)

    @property
    def enable_commit_grounding(self) -> bool:
        """Per-feature override for commit-message grounding (v4.6).

        Defaults to OFF, because live testing showed grounding ADDS prompt
        weight that small local models handle poorly on big multi-file
        commits (qwen2.5-coder copies recent commit subjects verbatim when
        overwhelmed; Haiku CLI truncates its output). The master toggle
        still gates every other LLM feature (PR draft / code review / Ask
        tab / doc drafter) — those benefit from grounding because they
        need codebase knowledge, not diff summarization.

        Explicitly stored value wins; None falls back to OFF (NOT to the
        master toggle) because commit messages are the one place where
        grounding is empirically counterproductive.
        """
        if not self.enable_llm_grounding:
            return False
        val = self.raw.get("enable_commit_grounding")
        return False if val is None else bool(val)

    @property
    def enable_pr_grounding(self) -> bool:
        """Per-feature override for Draft PR grounding (v4.6 — CLI + API paths).

        Defaults to ON when the master toggle is on. Unlike commit messages,
        PR descriptions GENUINELY benefit from codegraph + tokensave context
        — test-impact mapping (`codegraph affected --stdin`) and PR-scope
        symbol references are the strongest grounding wins, and Claude CLI
        / cloud APIs handle the extra prompt weight comfortably.

        Plumbed through both `_draft_pr_via_api` (`helpers/pr_draft.py`)
        AND `_draft_pr_via_cli` (manager pre-builds a context block and
        splices it into the CLI instruction; also nudges the CLI to use
        its own MCP tools if codegraph/tokensave are wired into Claude
        Code's MCP config).
        """
        if not self.enable_llm_grounding:
            return False
        val = self.raw.get("enable_pr_grounding")
        return True if val is None else bool(val)

    @property
    def commit_message_backend(self) -> str:
        """Strategy order for commit-message suggestion.

        'auto'      — Claude CLI first → LLM fallback (default)
        'llm_first' — LLM (Ollama/LM Studio) first → Claude CLI fallback
        'claude_cli'— Claude CLI only, no LLM fallback
        'llm'       — LLM only, Claude CLI never fires
        """
        return (self.raw.get("commit_message_backend") or "auto").lower()

    @property
    def basic_instructions_template(self) -> str:
        """Absolute path to the BASIC_INSTRUCTIONS.md template (derives from template_dir)."""
        return os.path.join(self.template_dir, "claude-md-template.md")

    @property
    def baseline_include_line(self) -> str:
        """The `@<path>\\project-baseline.md` include line written into BASIC_INSTRUCTIONS.md."""
        path = os.path.join(self.template_dir, "project-baseline.md")
        return f"@{os.path.normpath(path)}"

    # ── Cached properties (cache values from refresh_derived) ─────────────

    @property
    def git_exe(self) -> str:
        """Absolute path to git.exe (or bare 'git' fallback)."""
        return self._cached_git_exe

    @property
    def codegraph_exe(self) -> str:
        """Absolute path to codegraph CLI (empty string if not installed)."""
        return self._cached_codegraph_exe

    @property
    def claude_cli_exe(self) -> str:
        """Absolute path to the Claude Code CLI (empty string if not installed)."""
        return self._cached_claude_cli_exe

    @property
    def cursor_cli_exe(self) -> str:
        """Absolute path to the Cursor Agent CLI (empty string if not installed)."""
        return self._cached_cursor_cli_exe

    # ── Agent-CLI resolution ──────────────────────────────────────────────

    def resolve_agent_cli(self) -> "object":
        """Resolve the selected agent CLI to (spec, exe, state).

        The single entry point for every feature that shells out to a coding
        agent. Returns an `AgentResolution` (see helpers/agent_cli) whose three
        states callers must keep distinct: runnable, right-agent-but-missing,
        and unknown-agent-in-config. Collapsing the last two into one message
        told users to install something when the real fault was a typo.

        Read at execution time, never snapshotted — a Settings save rebinds
        both the selector and the paths (Rule 3).
        """
        from helpers.agent_cli import resolve
        return resolve(self.agent_cli, lambda spec: self._agent_exe_for(spec))

    def _agent_exe_for(self, spec) -> str:
        """Configured-or-detected path for one agent spec."""
        return {
            "claude_cli_exe": self.claude_cli_exe,
            "cursor_cli_exe": self.cursor_cli_exe,
        }.get(spec.config_key_exe, "")

    def agent_model_for(self, spec) -> str:
        """Pinned model for one agent spec, falling back to its own default."""
        val = self.raw.get(spec.config_key_model)
        return spec.default_model if val is None else str(val)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def __post_init__(self):
        # Populate the cached fields once so the property accessors are usable
        # immediately after construction.
        self.refresh_derived()

    def refresh_derived(self) -> None:
        """Recompute cached derived fields from self.raw.

        Called after disk reload (in `load`) and after any code path that
        mutates `self.raw` (most notably `App._on_settings_saved`).
        """
        # Lazy import — keeps state.py at the bottom of the import graph.
        from helpers.detection import (_detect_git, _detect_codegraph,
                                        _detect_claude_cli, _detect_cursor_cli)
        # An explicitly configured path always wins over detection — otherwise
        # a Settings save would be silently reverted by whatever is on PATH.
        self._cached_git_exe        = self.raw.get("git_exe")        or _detect_git()
        self._cached_codegraph_exe  = self.raw.get("codegraph_exe")  or _detect_codegraph()
        self._cached_claude_cli_exe = self.raw.get("claude_cli_exe") or _detect_claude_cli()
        self._cached_cursor_cli_exe = self.raw.get("cursor_cli_exe") or _detect_cursor_cli()

    # ── Disk I/O ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "ManagerConfig":
        """Read + migrate manager-config.json from disk, return a fresh instance."""
        # Lazy import — same isolation rationale as refresh_derived.
        from helpers.config import _load_config, _migrate_config
        return cls(raw=_migrate_config(_load_config()))

    def save(self) -> None:
        """Persist `self.raw` back to manager-config.json (no derived-field refresh)."""
        from helpers.config import _save_config
        _save_config(self.raw)
