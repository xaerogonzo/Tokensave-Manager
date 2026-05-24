"""
TokenSave Manager
A GUI for managing tokensave projects and controlling which project
Claude Desktop uses via the wrapper script.
"""

import os
import re
import json
import shlex
import shutil
import subprocess
import threading
import queue
import time
import sys
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
# simpledialog (used by SettingsDialog._add_root) moved with the dialog
# to src/dialogs/settings.py.
import pystray
# dataclasses (was used by _ReleaseCtx) + tempfile (was used by ReleaseWizardDialog)
# moved to dialogs/release_wizard.py in Phase C.2.

# Round 4 Phase A: shared immutable constants live in src/constants.py.
# Anything runtime-mutable belongs in src/state.py (ManagerConfig) instead.
from constants import (
    _ANSI,
    _TOKENSAVE_UPDATE_RE,
    CREATE_NO_WINDOW,
    AUTO_REFRESH_MS,
    _GIT_ENV_NO_PROMPT,
    C,
    _BASE_DIR,
    _CONFIG_PATH,
    LOG_FILE,
)
from helpers.detection import (
    _version_lt,
    _is_codegraph_project,
    _root_label,
)
# _detect_git, _detect_gh, _detect_npm, _detect_codegraph, _root_path also in
# helpers.detection — used by SettingsDialog (now in dialogs/settings.py) and
# the still-in-monolith ProjectsTabController. The controller will move in
# Phase D and re-import what it needs locally.
from helpers.config import _save_config
# _load_config and _migrate_config also live in helpers.config but are
# used only via ManagerConfig.load() internally — no monolith caller
# needs to import them.
from helpers.shadow_links import (
    generate_shadow_links, update_gitignore_for_shadows,
    DEFAULT_SHADOW_EXT_MAP,
)
# remove_shadow_links also lives in helpers.shadow_links — not imported here
# because the monolith doesn't call it (dead-code orphan from R3 cleanup; kept
# in the helper so a future controller can pick it back up if needed).
from helpers.gitignore import _BASELINE_GITIGNORE
# _read_gitignore_lines, _write_gitignore_lines, _GITIGNORE_TEMPLATES, _ensure_gitignore,
# _baseline_patterns also live in helpers.gitignore — used by the now-extracted
# dialogs/gitignore.py (Round 4 Phase C.1), not by the monolith anymore.
# _ensure_gitignore, _baseline_patterns also in helpers.gitignore (same
# orphaned-but-preserved rationale).
from helpers.llm import _is_auth_error
# _call_llm, _build_llm_prompt, _call_anthropic, _call_openai_compat,
# _iter_sse_events, _iter_json_lines also live in helpers.llm — used by
# extracted dialogs (ai_code_review.py, ollama_model_mgr.py) and
# helpers.commit_messages, not by the monolith.
# _call_anthropic, _call_openai_compat, _iter_sse_events live in helpers.llm
# but are internal to _call_llm — no external callers need them imported here.
from helpers.scaffold import _scaffold_git_hook
from helpers.runtime import (
    log, _acquire_instance_lock, _bring_existing_to_front, _make_tray_icon,
)
from helpers.project_discovery import (
    find_projects, get_pinned, set_pinned, clear_pinned,
    fmt_age, load_basic_instructions_template,
)
from helpers.git import (
    _is_git_repo, _find_tracked_but_ignored, _is_local_git_repo,
    _parse_git_status_v2, _format_git_status_cell,
)
# _fetch_tags, _git_tag, _git_push_with_tags also in helpers.git — used
# only by ReleaseWizardDialog (now in dialogs/release_wizard.py).
# helpers.release functions (_last_release_tag, _commits_since,
# _classify_commits_for_changelog, _bump_version, _suggest_bump_kind,
# _render_release_notes, _patch_changelog, _zip_dist, _release_basename,
# _fmt_size) are all consumed by ReleaseWizardDialog (now in
# dialogs/release_wizard.py) — no remaining monolith caller needs them.
# _CONVENTIONAL_RE / _TYPE_TO_SECTION / _SECTION_ORDER also live in
# helpers.release. Monolith doesn't reference them directly — only
# _classify_commits_for_changelog and _suggest_bump_kind do, and both
# moved to helpers/release.py with them.
from helpers.mcp import _MCP_CONFIGS, _classify_mcp_entry
# _apply_mcp_fix, _is_claude_running, plus the resolve/wrapper/canonical/
# checker helpers all live in helpers.mcp — used by MCPConfigDialog and
# SettingsDialog (now in dialogs/), no remaining monolith caller.
from helpers.commit_messages import _suggest_commit_message
# _pending_diff also lives in helpers.commit_messages — only used by the now-extracted
# AICodeReviewDialog, no remaining monolith caller needs it.
# All the other sanitizer / strategy / constant helpers live in
# helpers.commit_messages but no monolith code calls them directly — they
# are internal to _suggest_commit_message's orchestration. Future dialogs
# / controllers that need any of them should import from there directly
# (e.g. `from helpers.commit_messages import _sanitize_commit_message`).
# _resolve_desktop_cfg_path, _wrapper_path, _canonical_mcp_entry, _McpCtx,
# _chk_bundled_wrapper / _chk_python_wrapper / _chk_direct_serve, and
# _MCP_CMD_CHECKERS / _MCP_DESKTOP_CFG_PATH / _MCP_CODE_CFG_PATH also live
# in helpers.mcp — used only by _classify_mcp_entry / MCPConfigDialog
# internally, no external callers in the monolith need them imported here.


# _TOKENSAVE_UPDATE_RE moved to constants.py (Round 4 Phase A)

# _BASE_DIR / _CONFIG_PATH / LOG_DIR / LOG_FILE moved to constants.py
# _load_config / _save_config / _migrate_config moved to helpers/config.py
# (Round 4 Phase A)


# MCP-config helpers (_resolve_desktop_cfg_path, _wrapper_path,
# _canonical_mcp_entry, _McpCtx, _chk_*, _classify_mcp_entry, _apply_mcp_fix,
# _MCP_DESKTOP_CFG_PATH / _MCP_CODE_CFG_PATH / _MCP_CONFIGS / _MCP_CMD_CHECKERS)
# moved to helpers/mcp.py (Round 4 Phase A — 4 _classify_mcp_entry call sites
# updated to pass _cfg).



# Round 4 Phase A finale: `_state` is the new canonical settings holder
# (`state.ManagerConfig` instance). All future controllers and dialogs
# receive `_state` (or `App._cfg` which is the same instance) via
# `__init__(cfg: ManagerConfig)` and read live values through its
# property getters — `cfg.git_exe`, `cfg.tokensave_exe`, etc.
#
# During the Phase A transition window the legacy module globals
# (TOKENSAVE / GIT_EXE / etc.) and the legacy `_cfg` dict alias are
# REBOUND at module load AND in App._on_settings_saved to source their
# values from `_state` — so existing call sites that haven't migrated
# yet keep working unchanged. Phases B–E migrate each extracted file's
# call sites to `self._cfg.X` and the legacy globals get deleted in
# Phase E once nothing reads them.
from state import ManagerConfig
# Round 4 Phase B: dialog classes extracted to src/dialogs/.
from dialogs.new_branch import NewBranchDialog
from dialogs.set_remote import SetRemoteDialog
from dialogs.snippet_edit import SnippetEditDialog
from dialogs.assign_category import AssignCategoryDialog
from dialogs.switch_branch import SwitchBranchDialog
from dialogs.scaffold import ScaffoldDialog
from dialogs.shadow_links import ShadowLinksDialog
from dialogs.untrack_ignored import UntrackIgnoredDialog
from dialogs.retrofit import RetrofitDialog
from dialogs.merge_pr import MergePRDialog
# Round 4 Phase C.1 — big dialogs (no globals; receive `cfg: ManagerConfig` via __init__).
from dialogs.github_setup import GitHubSetupDialog
from dialogs.ai_code_review import AICodeReviewDialog
from dialogs.gitignore import GitignoreDialog
# Round 4 Phase C.2 — Ollama (no globals) + ReleaseWizard (cfg: ManagerConfig).
from dialogs.release_wizard import ReleaseWizardDialog
# _ReleaseCtx also lives in dialogs.release_wizard — internal to the dialog,
# no remaining monolith caller needs it.
# OllamaModelManagerDialog also in dialogs.ollama_model_mgr — only opened from
# SettingsDialog (now in dialogs/settings.py) via lazy import; no monolith caller.
# Round 4 Phase C.3 — final big-dialog batch. SettingsDialog last (depends on
# MCPConfig + Ollama from C.1/C.2 via lazy in-handler imports per Rule 6).
from dialogs.mcp_config import MCPConfigDialog
from dialogs.git_commit import GitCommitDialog
from dialogs.settings import SettingsDialog

_state = ManagerConfig.load()
_cfg   = _state.raw            # legacy dict alias — shared with _state.raw

TOKENSAVE     = _state.tokensave_exe
TEMPLATE_DIR  = _state.template_dir
SEARCH_ROOTS  = _state.search_roots
GIT_EXE: str  = _state.git_exe
CODEGRAPH_EXE: str = _state.codegraph_exe
BASIC_INSTRUCTIONS_TEMPLATE = _state.basic_instructions_template
BASELINE_INCLUDE_LINE       = _state.baseline_include_line


# _detect_git, _detect_gh, _detect_npm, _detect_codegraph, _is_codegraph_project
# moved to helpers/detection.py (Round 4 Phase A)
# _root_path and _root_label moved to helpers/detection.py (Round 4 Phase A)

# DESKTOP_PROJECT_FILE, SKIP_DIRS, MAX_DEPTH, CREATE_NO_WINDOW, AUTO_REFRESH_MS,
# and _GIT_ENV_NO_PROMPT moved to constants.py (Round 4 Phase A).


# _Tooltip moved to theme.py (Round 4 Phase A)
from theme import _Tooltip  # re-exported for in-file references


# _is_auth_error moved to helpers/llm.py (Round 4 Phase A)

# Commit-message helpers (_parse_commit_status / _strat_* / _suggest_commit_message /
# _strip_md / _escalate_commit_type / _normalize_commit_body / _sanitize_commit_message /
# _recent_commit_subjects / _find_changelog_file / _pending_diff /
# _extract_changelog_additions / _extract_scope / _dominant_directory /
# _message_from_changelog / _diff_added_python_symbols / _suggest_from_diff_content /
# _suggest_from_filenames / _call_llm_for_commit_message + 6 constants)
# moved to helpers/commit_messages.py (Round 4 Phase A —
# 5 external call sites updated: 3 _suggest_commit_message + 2 _pending_diff).



# _render_release_notes / _patch_changelog / _zip_dist / _release_basename /
# _fmt_size moved to helpers/release.py (Round 4 Phase A — 3 git-shell call
# sites in ReleaseWizardDialog updated to pass GIT_EXE).



# _fetch_tags / _git_tag / _git_push_with_tags moved to helpers/git.py
# (Round 4 Phase A — 3 sites in ReleaseWizardDialog updated).


# ── Prompt snippets (Reference tab) ─────────────────────────────────────────

PROMPT_SNIPPETS = [
    # ─────────────────────────── 🧭 EXPLORATION ───────────────────────────
    (
        "🧭  Codebase overview",
        "Give me a structural overview of this project using tokensave_context "
        "with the question 'overall architecture'. Then list the top-level "
        "modules with tokensave_files and run tokensave_outline on the entry "
        "point file. Summarise: what the project does, the main components, "
        "and the entry point flow."
    ),
    (
        "🧭  Find a symbol",
        "Use tokensave_search to find [symbol name]. Then use tokensave_context "
        "to explain what it does. Show its callers (tokensave_callers), what "
        "it calls (tokensave_callees), and where its fields are accessed if "
        "it's a class (tokensave_field_sites)."
    ),
    (
        "🧭  Understand a feature",
        "I want to understand how [feature/concept] works. Use tokensave_context "
        "to identify the relevant symbols, then read each one with tokensave_node "
        "and trace the data flow. Cite file:line for every claim. End with a "
        "one-paragraph summary of the feature's lifecycle."
    ),
    (
        "🧭  Onboarding tour",
        "Pretend I just joined this project and have 15 minutes. Use "
        "tokensave_outline on the main entry file, tokensave_module_api on the "
        "top 3 modules from tokensave_largest, and tokensave_files to list the "
        "directory layout. Produce a guided 5-step tour with file:line jumps."
    ),
    (
        "🧭  Module public API",
        "Use tokensave_module_api on [module or file name]. List its exports, "
        "their signatures (tokensave_signature for each), and how they're meant "
        "to be used (search for example call sites with tokensave_callers)."
    ),

    # ─────────────────────────── 🔬 ANALYSIS / TRACING ────────────────────
    (
        "🔬  Who calls this? Who does it call?",
        "Bidirectional call chain for [function name]: tokensave_callers shows "
        "what depends on it, tokensave_callees shows what it depends on. "
        "Render as a two-column tree with file:line on each node."
    ),
    (
        "🔬  Impact of changing X",
        "Run tokensave_impact on [function or class name]. Show me the full "
        "downstream impact chain. Then run tokensave_affected on the same "
        "symbol to see which tests would need re-running. Flag any high-risk "
        "ripples (cross-module, public API, etc.)."
    ),
    (
        "🔬  Trace a bug",
        "Bug symptom: [describe]. Workflow: (1) tokensave_search for the "
        "symbol you suspect, (2) tokensave_callers to see entry points, "
        "(3) tokensave_callees to see what it depends on, (4) tokensave_node "
        "to read the actual source, (5) tokensave_diagnose if you find "
        "anything anomalous. Walk me through your reasoning."
    ),
    (
        "🔬  Find similar code",
        "Use tokensave_similar on [function/class name] OR tokensave_signature_search "
        "for the signature pattern to find duplicates or near-duplicates. Group "
        "results by likely-extract-into-helper candidates."
    ),

    # ─────────────────────────── 📊 AUDITS ────────────────────────────────
    (
        "📊  Full health audit",
        "Run a comprehensive code health audit using ALL of these tokensave "
        "tools and produce one structured report:\n"
        "  • tokensave_health          (overall metrics)\n"
        "  • tokensave_complexity      (high-complexity functions)\n"
        "  • tokensave_god_class       (god classes / large files)\n"
        "  • tokensave_circular        (circular dependencies)\n"
        "  • tokensave_coupling        (high-coupling modules)\n"
        "  • tokensave_dead_code       (unused code)\n"
        "  • tokensave_unused_imports  (unused imports)\n"
        "  • tokensave_hotspots        (high-churn files)\n"
        "  • tokensave_recursion       (recursive call sites)\n"
        "  • tokensave_unsafe_patterns (risky patterns)\n"
        "Format: 🔴 Critical / 🟡 Warning / 🔵 Info, each with file:line and "
        "a one-sentence suggested fix. End with a top-5 priority list."
    ),
    (
        "📊  Architecture report",
        "Produce an architecture report for this project:\n"
        "  • tokensave_dsm              (dependency structure matrix)\n"
        "  • tokensave_dependency_depth (per-module)\n"
        "  • tokensave_inheritance_depth (per-class)\n"
        "  • tokensave_coupling          (high-coupling pairs)\n"
        "  • tokensave_module_api        (each top-level module)\n"
        "Format as a markdown ARCHITECTURE.md draft with sections: Module "
        "Map, Dependency Layers, Public APIs, Hot Spots. Cite file:line."
    ),
    (
        "📊  Test coverage audit",
        "Use tokensave_test_map to map tests to the code they cover. Use "
        "tokensave_test_risk to rank tests by risk. Use tokensave_doc_coverage "
        "for inline documentation gaps. Produce a 'Test & Docs Gap Report': "
        "which public symbols have no tests, which have low-risk tests, "
        "which lack docstrings. Cite file:line for each gap."
    ),
    (
        "📊  Security / unsafe-patterns scan",
        "Run tokensave_unsafe_patterns and tokensave_recursion across the "
        "whole project. Also run tokensave_diagnostics for general issues. "
        "Categorise findings: input validation, error handling, resource "
        "leaks, recursion depth, unsafe stdlib calls. Cite file:line and "
        "rate severity 🔴/🟡/🔵 each."
    ),
    (
        "📊  Hotspot risk scan",
        "Cross-reference tokensave_hotspots (high-churn files) with "
        "tokensave_complexity (high-complexity functions) and "
        "tokensave_coupling (high-coupling modules). Files high on multiple "
        "axes are most bug-prone. Output a ranked 'Risk Heatmap' table with "
        "the top 10 entries, churn / complexity / coupling scores, and a "
        "one-line refactor suggestion each."
    ),
    (
        "📊  Pre-release readiness check",
        "Check whether this project is ready to release:\n"
        "  • tokensave_diff_context     (recent changes summary)\n"
        "  • tokensave_changelog        (CHANGELOG draft from commits)\n"
        "  • tokensave_health           (overall metrics)\n"
        "  • tokensave_dead_code        (cruft that shouldn't ship)\n"
        "  • tokensave_unused_imports   (cruft that shouldn't ship)\n"
        "  • tokensave_circular         (architectural debt)\n"
        "  • tokensave_doc_coverage     (undocumented public API)\n"
        "Output a 'Release Readiness Checklist' with ✅/⚠/❌ per item. "
        "End with: 'Safe to release: yes/no, blockers:'."
    ),
    (
        "📊  Documentation coverage report",
        "Use tokensave_doc_coverage to find every public symbol without "
        "a docstring. Use tokensave_module_api to identify which of those "
        "are PART of a public module API (vs. internal helpers — those can "
        "be skipped). Output a prioritised 'Docstrings to add' list grouped "
        "by module, with the symbol's signature for context."
    ),

    # ─────────────────────────── 🪦 FINDINGS / CLEANUP ────────────────────
    (
        "🪦  Find dead code",
        "Use tokensave_dead_code and tokensave_unused_imports. List findings "
        "with file:line. For each, run tokensave_callers to double-check "
        "(some 'dead' code is actually called via reflection / dynamic "
        "dispatch and the index can miss it). Output: 'Safe to delete' vs "
        "'Verify before deleting' groups."
    ),
    (
        "🪦  List all TODOs / FIXMEs",
        "Use tokensave_todos to list every TODO/FIXME/HACK/XXX comment. "
        "Group by file, then by age (use git_log on the file to estimate "
        "when each was added). Flag any older than 6 months as stale."
    ),
    (
        "🪦  Circular dependencies",
        "Use tokensave_circular to find cycles. For each cycle, use "
        "tokensave_coupling on the involved modules to understand the "
        "binding strength. Propose a fix for each — typically: extract "
        "a third module, invert a dependency, or move a function."
    ),
    (
        "🪦  Largest / most complex files",
        "Cross-reference tokensave_largest (by LOC) with tokensave_complexity "
        "(by cyclomatic complexity) with tokensave_god_class (by method "
        "count). The intersection of all three is your top refactor "
        "priority. Output a ranked table with all three scores."
    ),

    # ─────────────────────────── 🛠 WORKFLOWS ─────────────────────────────
    (
        "🛠  Pre-commit checklist",
        "I'm about to commit. Run:\n"
        "  • tokensave_diff_context     (what's actually changed)\n"
        "  • tokensave_affected         (which tests would be affected)\n"
        "  • tokensave_impact           (downstream impact of changed symbols)\n"
        "  • tokensave_run_affected_tests (run only relevant tests)\n"
        "Then review the diff for: missing tests, missing docstrings, "
        "TODOs that should be resolved, secrets/keys. Output: 'Ready to "
        "commit: yes/no, issues:'."
    ),
    (
        "🛠  PR review prep",
        "I want to open a PR. Run tokensave_pr_context for context, "
        "tokensave_diff_context for the per-file diffs, tokensave_impact "
        "on the most-changed symbols, and tokensave_affected for test "
        "coverage. Draft a PR description with: Summary (2-3 sentences), "
        "Changes (bulleted by file), Testing (what was run), and Review "
        "Questions (what should a reviewer pay attention to)."
    ),
    (
        "🛠  Plan a refactor",
        "I want to refactor [target]. Workflow:\n"
        "  1. tokensave_coupling on the target to see what it's bound to\n"
        "  2. tokensave_dependency_depth to understand its position in the layer cake\n"
        "  3. tokensave_callers + tokensave_callees to map the blast radius\n"
        "  4. tokensave_rename_preview if any renames are involved\n"
        "  5. tokensave_test_map to see what tests cover it\n"
        "Output a step-by-step refactor plan with file:line for each step "
        "and the order to do them in (least-coupled first)."
    ),
    (
        "🛠  Refactor rename preview",
        "Use tokensave_rename_preview for [old name] → [new name]. List "
        "every affected file and line. Then use tokensave_callers to "
        "double-check that all callers are in the rename set (vs. e.g. "
        "string-based dynamic calls that the rename would miss)."
    ),
    (
        "🛠  Generate CHANGELOG entry",
        "Use tokensave_changelog to draft a CHANGELOG entry from recent "
        "commits. Group by ### Added / ### Changed / ### Fixed / ### Removed "
        "per Keep-a-Changelog format. Use the manager's existing CHANGELOG "
        "style as a tone reference (bolded lead-in followed by em-dash and "
        "prose description)."
    ),
    (
        "🛠  Generate ARCHITECTURE.md draft",
        "Produce a docs/ARCHITECTURE.md draft for this project. Use "
        "tokensave_outline on the main module(s), tokensave_module_api on "
        "each top-level module, tokensave_dependency_depth to identify "
        "layering, and tokensave_dsm to render module-pair dependencies. "
        "Format: Overview → Layer Diagram → Module Reference Table → "
        "Module Detail Sections. Cite file:line throughout."
    ),
]


class ProjectsTabController:
    """Owns the Projects tab UI and all per-project commands.

    No back-reference to App — all cross-App dependencies flow through the
    explicit callbacks passed at construction time.  At most 10 callbacks;
    if more are needed, revisit with an EventBus or shared-state bus.

    Thread safety rule:
      • Methods named *_worker run on a background daemon thread.
        They MUST NOT call Tkinter directly — use self._tab.after(0, ...).
      • All other methods run on the main thread and may call Tkinter freely.

    Log rule:
      • Worker threads call self._on_log(msg, color) — App._log already
        schedules self.after(0, ...) internally, so it is thread-safe.
      • Direct Tkinter widget calls (tree, menu) must be on the main thread only.
    """

    # ── Class-level constants ─────────────────────────────────────────────────

    # Set of tag names used as Git-status overrides on project rows.
    # _update_git_status_cell strips these before applying the new tag.
    _GIT_STATUS_TAGS = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(
        self,
        notebook: ttk.Notebook,
        cfg: dict,
        get_projects,          # () -> list[dict]
        on_run,                # (args, cwd, label) -> None  (App._run, threaded)
        on_run_capture,        # (args, cwd, label) -> (raw, rc, elapsed)  (sync, from thread)
        on_shell,              # (cmd, cwd, env=None) -> (out, rc)  (sync, from thread)
        on_log,                # (msg, colour) -> None  (thread-safe)
        on_commit,             # (path) -> None  (App._open_commit_dialog)
        on_refresh,            # () -> None  (App.refresh — full tree rebuild)
        on_project_select,     # (path) -> None  (fired on row click)
        on_set_running,        # (running: bool, label: str) -> None  (App._set_running)
        on_settings,           # () -> None  (App.cmd_settings)
    ):
        self._notebook       = notebook
        self._cfg            = cfg
        self._get_projects   = get_projects
        self._on_run         = on_run
        self._on_run_capture = on_run_capture
        self._on_shell       = on_shell
        self._on_log         = on_log
        self._on_commit      = on_commit
        self._on_refresh     = on_refresh
        self._on_project_select = on_project_select
        self._on_set_running = on_set_running
        self._on_settings    = on_settings

        # Controller-level subprocess tracking (parallel to App._current_proc)
        # Workers managed by THIS controller set/clear this attribute so that
        # App._auto_refresh can see whether the controller is busy.
        self.current_proc: object = None          # subprocess.Popen | None
        self._ctrl_stop_requested: bool = False   # checked by batch workers
        self._git_status_refresh_cancel: bool = False
        self._git_status_refresh_running: bool = False

        self._tree: ttk.Treeview | None = None
        self._ctx_menu: tk.Menu | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Projects  ")
        self._build_projects_tab()
        self._build_context_menu()

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def _root(self) -> tk.Tk:
        """The top-level App window — use for dialog parenting."""
        return self._tab.winfo_toplevel()

    def get_selected_path(self) -> str | None:
        """Return the path of the currently selected project row, or None."""
        if self._tree is None:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("proj:"):
            return iid[5:]
        return None

    def stop(self):
        """Cancel any in-flight controller worker (called by App._stop_current)."""
        self._ctrl_stop_requested = True
        proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # ── Tree population (called by App.refresh) ───────────────────────────────

    def rebuild_tree(self, projects: list, active_path: str | None,
                     pinned: str | None) -> None:
        """Clear and repopulate the Treeview from a fresh project list.

        Called by App.refresh() after it has computed projects / active_path.
        Does NOT kick off the git-status refresh — App.refresh() does that.
        """
        if self._tree is None:
            return
        for item in self._tree.get_children():
            self._tree.delete(item)

        proj_cats = self._cfg.get("project_categories", {})

        groups: dict = {}
        for p in projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self._tree.insert("", tk.END, iid=ciid, text=cat,
                                  open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self._tree.exists(siid):
                    self._tree.insert(parent, tk.END, iid=siid,
                                      text=f"  ↳ {subcat}",
                                      open=True, tags=("subcategory",))
                parent = siid

            for p in projs:
                is_active    = (p["path"] == active_path)
                has_scaffold = self._has_scaffold(p["path"])
                has_ts       = p.get("has_tokensave", True)
                has_git      = p.get("has_git", False)
                has_cg       = p.get("has_codegraph", False)
                if is_active:
                    base_tag = "active"
                elif not has_ts:
                    base_tag = "git_only"
                elif not has_scaffold:
                    base_tag = "scaffold"
                else:
                    base_tag = "normal"
                synced_str = fmt_age(p["mtime"]) if has_ts else "—"
                cg_text = "✓" if has_cg else "—"
                git_text, git_tag = _format_git_status_cell(
                    p.get("git_status"), has_git)
                tags = (base_tag, git_tag) if has_git else (base_tag, "git_none")
                piid = f"proj:{p['path']}"
                self._tree.insert(parent, tk.END, iid=piid,
                                  text=p["name"],
                                  values=("★" if is_active else "",
                                          p["path"],
                                          synced_str,
                                          cg_text,
                                          git_text,
                                          "✔" if has_scaffold else "—"),
                                  tags=tags)

    def refresh_git_status_column(self, projects: list) -> None:
        """Kick off background refresh of the Git status column.

        Called by App.refresh() after rebuild_tree().
        """
        self._kick_off_git_status_refresh(projects)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _has_scaffold(path: str) -> bool:
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def _selected_path(self) -> str | None:
        """Return the selected project path, or show a warning and return None."""
        sel = self._tree.selection() if self._tree else ()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.",
                                   parent=self._root)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).",
                parent=self._root)
            return None
        return iid[5:]

    def _require_tokensave(self, path: str) -> bool:
        if os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db")):
            return True
        name = os.path.basename(path)
        messagebox.showinfo(
            "Not indexed with tokensave",
            f"'{name}' is a git project but doesn't have a tokensave index yet.\n\n"
            "Tokensave builds a code-graph that lets Claude navigate your project "
            "efficiently without reading every file.\n\n"
            "To add it:  right-click → ⚙ Retrofit…  and tick "
            "'Add tokensave @include + init'.",
            parent=self._root)
        return False

    def _require_codegraph_installed(self) -> bool:
        if CODEGRAPH_EXE and os.path.isfile(CODEGRAPH_EXE):
            return True
        result = messagebox.askyesno(
            "CodeGraph is not installed",
            "CodeGraph is not installed on this machine.\n\n"
            "CodeGraph is an alternative code-graph tool that builds a "
            "per-project SQLite index for Claude Code to query. It complements "
            "tokensave — both can be enabled on the same project.\n\n"
            "Open Settings now to install it?",
            parent=self._root)
        if result:
            self._on_settings()
        return False

    def _offer_commit_after_change(self, path: str, summary_label: str) -> None:
        """After a manager action, check if tree is dirty and offer to commit."""
        if not _is_local_git_repo(path):
            return
        status_out, _ = self._on_shell(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._on_log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self._root):
            self._on_commit(path)
        else:
            self._on_log(f"  Working tree left dirty — commit when you're ready.",
                         C["yellow"])

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_projects_tab(self) -> None:
        tab = self._tab

        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self._on_refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self._on_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "cg", "git", "scaffold"),
            show="tree headings",
            selectmode="browse",
        )
        self._tree.heading("#0",       text="Project")
        self._tree.heading("active",   text="")
        self._tree.heading("path",     text="Path")
        self._tree.heading("synced",   text="Last Synced")
        self._tree.heading("cg",       text="CG")
        self._tree.heading("git",      text="Git")
        self._tree.heading("scaffold", text="Scaffold")

        self._tree.column("#0",       width=170, stretch=False)
        self._tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self._tree.column("path",     width=220)
        self._tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self._tree.column("cg",       width=36,  stretch=False, anchor=tk.CENTER)
        self._tree.column("git",      width=60,  stretch=False, anchor=tk.CENTER)
        self._tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.tag_configure("active",      foreground=C["green"])
        self._tree.tag_configure("normal",      foreground=C["text"])
        self._tree.tag_configure("scaffold",    foreground=C["peach"])
        self._tree.tag_configure("git_only",    foreground=C["overlay0"])
        self._tree.tag_configure("pending",     foreground=C["yellow"])
        self._tree.tag_configure("git_clean",   foreground=C["green"])
        self._tree.tag_configure("git_dirty",   foreground=C["yellow"])
        self._tree.tag_configure("git_ahead",   foreground=C["sky"])
        self._tree.tag_configure("git_behind",  foreground=C["red"])
        self._tree.tag_configure("git_mixed",   foreground=C["peach"])
        self._tree.tag_configure("git_pending", foreground=C["overlay0"])
        self._tree.tag_configure("git_none",    foreground=C["overlay0"])
        self._tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self._tree.tag_configure("subcategory", foreground=C["lavender"])

        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _build_context_menu(self) -> None:
        m = tk.Menu(self._root, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_separator()
        m.add_command(label="🧠  CodeGraph Init",          command=self.cmd_codegraph_init)
        m.add_command(label="🧠  CodeGraph Sync",          command=self.cmd_codegraph_sync)
        m.add_command(label="🧠  CodeGraph Status",        command=self.cmd_codegraph_status)
        m.add_command(label="🧠  Remove CodeGraph Index",  command=self.cmd_codegraph_remove)
        m.add_separator()
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",        command=self.cmd_git_commit)
        m.add_command(label="🔍  AI Code Review…",    command=self.cmd_ai_code_review)
        m.add_command(label="🔧  Git Init",           command=self.cmd_git_init)
        m.add_command(label="📋  Manage .gitignore…",      command=self.cmd_manage_gitignore)
        m.add_command(label="🧹  Untrack Ignored Files…",  command=self.cmd_untrack_ignored)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        if not row.startswith("proj:"):
            return
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_tree_select(self, event=None) -> None:
        """Internal handler for <<TreeviewSelect>> — fires the project-select callback."""
        path = self.get_selected_path()
        if path:
            self._on_project_select(path)

    def _insert_pending_row(self, path: str, name: str) -> None:
        """Add a placeholder row while tokensave init is running."""
        self._tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—", "—", "—"),
            tags=("pending",))

    # ── Git status column refresh ─────────────────────────────────────────────

    def _kick_off_git_status_refresh(self, projects: list) -> None:
        """Background-walk every git project and update its Git column cell."""
        if self._git_status_refresh_running:
            self._git_status_refresh_cancel = True
        self._git_status_refresh_cancel  = False
        self._git_status_refresh_running = True

        projects_snapshot = list(projects)

        def worker():
            try:
                for p in projects_snapshot:
                    if self._git_status_refresh_cancel:
                        return
                    if not p.get("has_git"):
                        continue
                    path = p["path"]
                    idx_path = os.path.join(path, ".git", "index")
                    try:
                        idx_mtime = os.path.getmtime(idx_path)
                    except OSError:
                        idx_mtime = 0
                    cached = p.get("git_status")
                    cached_mtime = p.get("_git_idx_mtime", -1)
                    if cached is not None and idx_mtime == cached_mtime:
                        continue
                    try:
                        out, _rc = self._on_shell(
                            [GIT_EXE, "-C", path,
                             "status", "--porcelain=v2", "--branch"],
                            path)
                        status = _parse_git_status_v2(out)
                    except Exception:
                        continue
                    p["git_status"]     = status
                    p["_git_idx_mtime"] = idx_mtime
                    piid = f"proj:{path}"
                    self._tab.after(0, self._update_git_status_cell, piid, status)
                    time.sleep(0.05)
            finally:
                self._git_status_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _update_git_status_cell(self, piid: str, status: dict) -> None:
        """Main-thread: update a single row's Git column value + override tag."""
        if not self._tree.exists(piid):
            return
        text, tag = _format_git_status_cell(status, has_git=True)
        try:
            self._tree.set(piid, "git", text)
        except tk.TclError:
            return
        existing = list(self._tree.item(piid, "tags") or ())
        existing = [t for t in existing if t not in self._GIT_STATUS_TAGS]
        existing.append(tag)
        self._tree.item(piid, tags=tuple(existing))

    # ── Scaffold helpers ──────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path: str) -> list:
        """Copy Nuitka build templates into path.  Returns list of action strings."""
        name     = os.path.basename(path)
        src_ps1  = os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template")
        src_bat  = os.path.join(TEMPLATE_DIR, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._on_log("  [WARN] Nuitka templates not found in template directory — skipped",
                         C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {TEMPLATE_DIR}")
            return actions

        if os.path.isfile(dst_ps1):
            self._on_log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._on_log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._on_log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        if os.path.isfile(dst_bat):
            self._on_log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._on_log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._on_log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._on_log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])
        return actions

    def _scaffold_project(self, path: str, create_bi: bool = True,
                          run_init: bool = True, scaffold_nuitka: bool = False,
                          add_git_hook: bool = False) -> None:
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template(
                    BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE)
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._on_log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._on_log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._on_log(f"  {action}", C["green"])

        if run_init:
            self._insert_pending_row(path, name)
            self._on_log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self._tab.after(0, self._on_set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._on_log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._on_log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._on_log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self.current_proc = None
                    self._tab.after(0, self._on_set_running, False, "")
                    self._tab.after(0, self._on_refresh)
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._on_refresh()
            self._offer_commit_after_change(path, "scaffold files")

    # ── Tokensave commands ────────────────────────────────────────────────────

    def cmd_set_active(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        set_pinned(path)
        self._on_log(f"Pinned → {path}", C["green"])
        try:
            states = [_classify_mcp_entry(p, _cfg)["state"] for _, p in _MCP_CONFIGS]
        except Exception:
            states = []
        if "ok" in states and not all(s == "ok" for s in states):
            bad = [lbl for (lbl, p), s in zip(_MCP_CONFIGS, states) if s != "ok"]
            self._on_log(
                f"  Pin will take effect at next Claude restart.  "
                f"Note: {', '.join(bad)} also still needs its MCP wiring "
                f"fixed (Settings → 🔌 Manage MCP wiring).",
                C["peach"])
        elif "ok" not in states:
            self._on_log(
                "  No MCP config currently routes through the wrapper — "
                "this pin won't take effect until you fix the MCP wiring "
                "AND restart Claude.  Settings → 🔌 Manage MCP wiring.",
                C["peach"])
        else:
            self._on_log(
                "  Pin will take effect at next Claude Desktop / Claude Code "
                "restart.  (Live in-session reload is deferred — see the "
                "wrapper script's docstring for context.)",
                C["overlay0"])
        self._on_refresh()

    def cmd_auto(self) -> None:
        clear_pinned()
        self._on_log("Auto-detect enabled — wrapper picks the most-recently-synced project at next launch.", C["sky"])
        self._on_log(
            "  Restart Claude Desktop / Claude Code to trigger a fresh "
            "auto-detect.",
            C["overlay0"])
        self._on_refresh()

    def cmd_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._on_run(["sync"], cwd=path, label=os.path.basename(path))

    def cmd_sync_all(self) -> None:
        projects = self._get_projects()
        if not projects:
            messagebox.showinfo("No Projects", "No projects found.", parent=self._root)
            return
        ts_projects = [p for p in projects if p.get("has_tokensave", True)]
        if not ts_projects:
            messagebox.showinfo(
                "No indexed projects",
                "None of your projects have a tokensave index yet.\n\n"
                "Right-click any project → ⚙ Retrofit… to add one.",
                parent=self._root)
            return
        count = len(ts_projects)
        skipped = len(projects) - count
        skip_note = (f"\n({skipped} git-only project{'s' if skipped != 1 else ''} "
                     f"will be skipped)") if skipped else ""
        if not messagebox.askyesno(
            "Sync All",
            f"Sync {count} indexed project{'s' if count != 1 else ''}?{skip_note}\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self._root,
        ):
            return

        projects_snapshot = list(ts_projects)

        def worker():
            self._ctrl_stop_requested = False
            self._on_log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self._tab.after(0, self._on_set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._ctrl_stop_requested:
                    self._on_log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._on_log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self.current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._on_log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._on_log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self.current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._on_log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self._tab.after(0, self._on_set_running, False, "")
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._on_run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._on_log(f"  Status OK — {data.get('node_count')} nodes, "
                                 f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self._tab.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._on_log(line)
                self._on_log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self._tab.after(0, self._on_refresh)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name: str, msg: str) -> None:
        win = tk.Toplevel(self._root)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()
        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self._root)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_force_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self._root,
        ):
            self._on_run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def cmd_doctor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_tokensave(path):
            return
        self._run_doctor_with_purge_offer(path)

    def _run_doctor_with_purge_offer(self, path: str) -> None:
        """Run `tokensave doctor`, stream output, and offer to purge stale entries."""
        label = os.path.basename(path)

        def worker():
            cmd_str = "tokensave doctor"
            self._on_log(f"$ {cmd_str}  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            output_lines: list = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.current_proc = proc
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._on_log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._on_log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                stale_paths = self._extract_doctor_stale_paths(output_lines)
                if stale_paths and proc.returncode == 0:
                    self._tab.after(0, self._offer_doctor_purge, path, stale_paths)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_doctor")
            finally:
                self.current_proc = None
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-worker").start()

    @staticmethod
    def _extract_doctor_stale_paths(output_lines: list) -> list:
        """Parse tokensave doctor's stdout for the stale-entries section."""
        bullet_re = re.compile(r"^\s*[•\*\-]\s+(.+?)\s*$")
        in_block = False
        paths: list = []
        for line in output_lines:
            if "stale project" in line and "global DB" in line:
                in_block = True
                continue
            if not in_block:
                continue
            if "Re-run" in line and "tokensave doctor" in line:
                break
            m = bullet_re.match(line)
            if m:
                paths.append(m.group(1).strip())
            elif paths and not line.startswith(" ") and not line.startswith("\t"):
                break
        return paths

    def _offer_doctor_purge(self, path: str, stale_paths: list) -> None:
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (f"tokensave doctor found {n} stale project entr"
               f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
               f"{bullets}\n\n"
               "These projects were registered but their `.tokensave/` "
               "folders are gone — most likely deleted folders.\n\n"
               "Purge them now?  The manager will re-run `tokensave "
               "doctor` with `y` piped to confirm the interactive "
               "purge prompt.")
        if not messagebox.askyesno(
                "Purge stale tokensave projects?",
                msg, parent=self._root):
            self._on_log("  (purge skipped — stale entries left in place)", C["overlay0"])
            return
        self._run_doctor_purge(path)

    def _run_doctor_purge(self, path: str) -> None:
        """Re-run `tokensave doctor` with `y` piped to confirm the purge prompt."""
        label = "doctor (purge)"

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            captured: list = []
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE, "doctor"],
                    cwd=path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.current_proc = proc
                try:
                    proc.stdin.write("y\ny\ny\ny\ny\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    captured.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                self._on_log(
                    "Done." if proc.returncode == 0
                    else f"Exited with code {proc.returncode}",
                    C["green"] if proc.returncode == 0 else C["red"])
                still_stale = self._extract_doctor_stale_paths(captured)
                if still_stale:
                    self._on_log(
                        f"  ⚠ Purge didn't take — tokensave still "
                        f"reports {len(still_stale)} stale entr"
                        f"{'y' if len(still_stale) == 1 else 'ies'}. "
                        f"tokensave doctor needs a real terminal "
                        f"(piped stdin doesn't trigger the prompt).",
                        C["peach"])
                    self._tab.after(0, self._offer_doctor_in_cmd, path, len(still_stale))
                else:
                    self._on_log("  ✓ Stale entries purged.", C["green"])
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in doctor purge")
            finally:
                self.current_proc = None
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-purge").start()

    def _offer_doctor_in_cmd(self, path: str, n_stale: int) -> None:
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The piped-stdin purge didn't work — tokensave needs "
                f"a real terminal for its interactive 'y/n' prompt.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self._root):
            self._on_log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return
        try:
            cmd_line = f'cmd.exe /k ""{TOKENSAVE}" doctor"'
            subprocess.Popen(
                cmd_line,
                cwd=path,
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self._on_log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
        except OSError as e:
            self._on_log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    # ── CodeGraph commands ────────────────────────────────────────────────────

    def cmd_codegraph_init(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if _is_codegraph_project(path):
            messagebox.showinfo("Already initialised",
                f"{os.path.basename(path)} already has CodeGraph initialised.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph init in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell(
                [CODEGRAPH_EXE, "init", "--index", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)
            self._tab.after(0, lambda: self._offer_commit_after_change(
                path, "CodeGraph init files"))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_sync(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph sync in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell([CODEGRAPH_EXE, "sync", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines()[-8:]:
                self._on_log(f"  {line}", col)
            self._tab.after(0, self._on_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_status(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not self._require_codegraph_installed():
            return
        if not _is_codegraph_project(path):
            messagebox.showinfo("Not initialised",
                f"{os.path.basename(path)} hasn't been initialised with "
                "CodeGraph yet.\n\nRight-click → 🧠 CodeGraph Init first.",
                parent=self._root)
            return
        name = os.path.basename(path)
        self._on_log(f"Running codegraph status in {name}…", C["peach"])

        def worker():
            out, rc = self._on_shell([CODEGRAPH_EXE, "status", path], path)
            col = C["green"] if rc == 0 else C["red"]
            for line in out.strip().splitlines():
                self._on_log(f"  {line}", col)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_codegraph_remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        cg_dir = os.path.join(path, ".codegraph")
        if not os.path.isdir(cg_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no CodeGraph index.", parent=self._root)
            return
        if not messagebox.askyesno(
                "Remove CodeGraph index?",
                f"Delete the CodeGraph index for {name}?\n\n"
                f"This removes .codegraph/ from the project folder. Your "
                "source files are not touched. You can re-create the index "
                "later via 🧠 CodeGraph Init.",
                parent=self._root):
            return
        try:
            shutil.rmtree(cg_dir)
            self._on_log(f"  Removed .codegraph/ from {name}", C["green"])
        except OSError as e:
            self._on_log(f"  Could not remove .codegraph/: {e}", C["red"])
        self._on_refresh()

    # ── Git commands (Projects-tab variants) ──────────────────────────────────

    def cmd_git_log(self) -> None:
        """Navigate to the Git tab and show live status/log for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Switch to Git tab first so is_visible() is True when on_project_select fires.
        try:
            for idx in range(self._notebook.index("end")):
                if self._notebook.tab(idx, "text").strip() == "Git":
                    self._notebook.select(idx)
                    break
        except tk.TclError:
            pass
        # Now fire the project-select callback — App._on_project_select wires
        # set_active_path + refresh on GitTabController.
        self._on_project_select(path)

    def cmd_git_commit(self) -> None:
        """Open the Git Commit dialog for the selected project."""
        path = self._selected_path()
        if path:
            self._on_commit(path)

    def cmd_ai_code_review(self) -> None:
        """Open the AI Code Review dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo(
                "Not a git repo",
                f"{os.path.basename(path)} doesn't have a .git folder, "
                "so there's no pending diff to review.",
                parent=self._root)
            return
        llm_cfg = self._cfg.get("commit_message_llm") or {}
        if not llm_cfg.get("enabled"):
            messagebox.showinfo(
                "AI is not enabled",
                "Open Settings → 'AI commit messages' and enable AI to use "
                "this feature.",
                parent=self._root)
            return
        AICodeReviewDialog(self._root, path, llm_cfg, _state)

    def cmd_git_init(self) -> None:
        """Initialise a git repository in the selected project folder."""
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        if _is_git_repo(path, GIT_EXE):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self._root,
            )
            return
        self._on_log(f"Running git init in {name}…", C["peach"])
        out, rc = self._on_shell([GIT_EXE, "-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._on_log(f"  {line}", col)
        if rc != 0:
            self._on_refresh()
            return
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._on_log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._on_log(f"  Warning: could not write .gitignore: {e}", C["yellow"])
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self._root,
        ):
            def do_commit():
                self._on_shell([GIT_EXE, "-C", path, "add", "-A"], path)
                cout, crc = self._on_shell(
                    [GIT_EXE, "-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self._tab.after(0, lambda l=line: self._on_log(f"  {l}", ccol))
                self._tab.after(0, self._on_refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self._on_refresh()

    def cmd_manage_gitignore(self) -> None:
        path = self._selected_path()
        if not path:
            return
        GitignoreDialog(self._root, path, _state)

    def cmd_untrack_ignored(self) -> None:
        path = self._selected_path()
        if not path:
            return
        if not _is_local_git_repo(path):
            messagebox.showinfo("Not a git repo",
                f"{os.path.basename(path)} is not a git repository — "
                "tracking isn't a concept here.",
                parent=self._root)
            return
        files = _find_tracked_but_ignored(path, GIT_EXE)
        if not files:
            messagebox.showinfo("Nothing to untrack",
                f"No tracked-but-ignored files found in "
                f"{os.path.basename(path)}.\n\n"
                "Either nothing is tracked yet, or all tracked files "
                "are consistent with your .gitignore — which is the "
                "healthy state.",
                parent=self._root)
            return
        UntrackIgnoredDialog(self._root, path, files,
                             on_confirm=self._do_untrack_ignored)

    def _do_untrack_ignored(self, path: str, files: list) -> None:
        """Worker: run `git rm --cached -- <files>` in a background thread."""
        if not files:
            return
        name = os.path.basename(path)
        self._on_log(f"Untracking {len(files)} file"
                     f"{'s' if len(files) != 1 else ''} in {name}…",
                     C["peach"])

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "rm", "-r", "--cached", "--"] + files,
                    path)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._on_log(f"  {line}", col)
                if rc != 0:
                    return
                self._on_log(
                    f"  ✓ Untracked {len(files)} file"
                    f"{'s' if len(files) != 1 else ''} — "
                    "local copies preserved",
                    C["green"])
            finally:
                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path,
                    f"untrack {len(files)} ignored file"
                    f"{'s' if len(files) != 1 else ''}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── File / editor / path commands ─────────────────────────────────────────

    def cmd_open_folder(self) -> None:
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self) -> None:
        path = self._selected_path()
        if not path:
            return
        editor_str = self._cfg.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self._root,
            )

    def cmd_copy_path(self) -> None:
        path = self._selected_path()
        if not path:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(path)
        self._on_log(f"Copied: {path}", C["sky"])

    def cmd_remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self._root)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self._root,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._on_log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self._on_refresh()
        except Exception as e:
            self._on_log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self._root)

    # ── Shadow Links ──────────────────────────────────────────────────────────

    def cmd_shadow_links(self) -> None:
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self._root, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict,
                         run_sync: bool = True) -> None:
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            rc = 0
            try:
                self._on_log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._on_log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and TOKENSAVE and created > 0:
                    self._on_log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._on_run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._on_log(f"    {line}", col)

                self._tab.after(0, self._on_refresh)
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}."
                       if run_sync and created > 0 else ""),
                    parent=self._root))
                self._tab.after(0, lambda: self._offer_commit_after_change(
                    path, "shadow links + .gitignore"))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()

    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_assign_category(self) -> None:
        path = self._selected_path()
        if not path:
            return
        all_cats: list = []
        all_subs: dict = {}
        for r in SEARCH_ROOTS:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in self._cfg.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = self._cfg.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self._root, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path: str, cat, subcat) -> None:
        proj_cats = self._cfg.setdefault("project_categories", {})
        if cat is None:
            proj_cats.pop(path, None)
            self._on_log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._on_log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        _save_config(self._cfg)
        self._on_refresh()

    # ── Scaffold / Retrofit ───────────────────────────────────────────────────

    def cmd_scaffold(self) -> None:
        folder = filedialog.askdirectory(title="Select folder to scaffold",
                                         parent=self._root)
        if not folder:
            return
        ScaffoldDialog(self._root, folder, self._scaffold_project)

    def cmd_retrofit(self) -> None:
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self._root)
        if not folder:
            return
        RetrofitDialog(self._root, folder, self._do_retrofit)

    def cmd_retrofit_selected(self) -> None:
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self._root, path, self._do_retrofit)

    def _do_retrofit(self, path: str, add_tokensave: bool,
                     add_basic_instructions: bool,
                     add_nuitka: bool = False,
                     add_shadow_links: bool = False,
                     shadow_ext_map: dict | None = None,
                     add_git_hook: bool = False) -> None:
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} "
                         f"nuitka={add_nuitka}")
                self._on_log(f"Retrofitting {name}…", C["peach"])
                actions_taken = []

                if add_tokensave:
                    claude_md = os.path.join(path, "CLAUDE.md")
                    include_line = BASELINE_INCLUDE_LINE
                    if os.path.isfile(claude_md):
                        content = open(claude_md, encoding="utf-8", errors="ignore").read()
                        if "project-baseline.md" in content:
                            log.info("  CLAUDE.md already has @include — skipped")
                            self._on_log("  Tokensave already integrated in CLAUDE.md — skipped",
                                         C["overlay0"])
                        else:
                            with open(claude_md, "r+", encoding="utf-8") as f:
                                existing = f.read()
                                f.seek(0)
                                f.write(include_line + "\n\n" + existing)
                            log.info("  prepended @include to CLAUDE.md")
                            self._on_log("  Added tokensave @include to CLAUDE.md", C["green"])
                            actions_taken.append("Added tokensave rules to CLAUDE.md")
                    else:
                        with open(claude_md, "w", encoding="utf-8") as f:
                            f.write(
                                f"# {name} — Claude Instructions\n\n"
                                f"{include_line}\n"
                            )
                        log.info("  created CLAUDE.md with @include")
                        self._on_log("  Created CLAUDE.md with tokensave @include", C["green"])
                        actions_taken.append("Created CLAUDE.md with tokensave rules")

                if add_basic_instructions:
                    basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
                    if os.path.isfile(basic_md):
                        log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
                        self._on_log("  BASIC_INSTRUCTIONS.md already exists — skipped",
                                     C["overlay0"])
                    else:
                        template = load_basic_instructions_template(
                            BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE)
                        with open(basic_md, "w", encoding="utf-8") as f:
                            f.write(template)
                        log.info("  created BASIC_INSTRUCTIONS.md")
                        self._on_log("  Created BASIC_INSTRUCTIONS.md", C["green"])
                        actions_taken.append("Created BASIC_INSTRUCTIONS.md")

                if add_nuitka:
                    nuitka_actions = self._scaffold_nuitka_build(path)
                    actions_taken.extend(nuitka_actions)

                if add_shadow_links:
                    ext_map = shadow_ext_map or DEFAULT_SHADOW_EXT_MAP
                    self._on_log("  Generating shadow extension links…", C["peach"])
                    created, skipped, failed = generate_shadow_links(path, ext_map)
                    update_gitignore_for_shadows(path, ext_map)
                    sl_msg = f"Shadow links: created {created}"
                    if skipped:
                        sl_msg += f", {skipped} already existed"
                    if failed:
                        sl_msg += f", {failed} failed"
                    self._on_log(f"  {sl_msg}", C["green"])
                    log.info(f"  {sl_msg}")
                    if created > 0:
                        actions_taken.append(sl_msg)

                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._on_log(f"  {action}", C["green"])
                    actions_taken.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions_taken or 'nothing changed'}")
                self._on_log(f"Retrofit complete: {path}", C["green"])
                self._tab.after(0, self._on_refresh)

                if actions_taken:
                    summary = "\n".join(f"  ✔ {a}" for a in actions_taken)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions_taken):
                        msg += ("\n\nNext step: open build.ps1 and replace "
                                "[ENTRY_SCRIPT] and [OUTPUT_NAME] before building.")
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self._tab.after(0, lambda: messagebox.showinfo(
                    "Retrofit complete", msg, parent=self._root))
                if actions_taken:
                    self._tab.after(0, lambda: self._offer_commit_after_change(
                        path, "retrofit additions"))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._on_log(f"  Error: {e}", C["red"])
                self._tab.after(0, lambda: messagebox.showerror(
                    "Retrofit failed", str(e), parent=self._root))

        threading.Thread(target=worker, daemon=True).start()


# ── Ask tab controller ─────────────────────────────────────────────────────────

class AskTabController:
    """Owns the Ask tab UI and the agent conversation loop.

    Decoupled from App: receives a get_project_path callback instead of
    holding a reference to the App instance.
    """

    _ASK_SYSTEM_PROMPT = (
        "You are a code-aware assistant for the user's current project. "
        "You have access to READ-ONLY tools that let you read files, list "
        "directories, view git history, view the pending diff, and search "
        "the project's tokensave code graph when present.\n\n"
        "How to use tools:\n"
        "- Use the API's tool_calls mechanism — emit calls via the "
        "tool_calls field of your response, NOT as JSON text inside the "
        "content field. After the tool result is returned to you as a "
        "role:'tool' message, continue your reasoning and either call "
        "another tool or give a final text answer.\n"
        "- Do NOT guess about file contents or code that you have not read.\n"
        "- Cite file:line locations when you reference code.\n\n"
        "Tool-selection guide (CRITICAL — wrong tool choice wastes "
        "iterations):\n"
        "- **read_file** is your primary tool. When the user names a "
        "specific file in their question, OR when you're asked about a "
        "specific symbol or behaviour you can locate, just read the file "
        "directly. Don't search first.\n"
        "- **tokensave_search** finds DEFINED SYMBOLS by name — "
        "functions, classes, methods, constants. It does NOT do "
        "full-text grep across source. Searching for 'Popen', 'import', "
        "or any keyword that isn't a symbol name returns nothing. Use "
        "tokensave_search to answer 'where is X defined?' for an X that "
        "is itself a function/class/constant name.  The result includes "
        "the exact line number where the symbol is *defined* — "
        "**chain it into a read_file call with start_line set to that "
        "line and end_line set ~150-200 lines later** so you read the "
        "FULL body, not just the signature.  Most Python functions / "
        "class methods are 20-200 lines; reading too narrow a window "
        "shows only the docstring and you'll miss the actual logic.  "
        "Never read a >50 KB file without a line range; you'll just "
        "get the first 50 KB which is almost certainly not what you "
        "want.\n"
        "- **tokensave_context** builds a focused subgraph for a "
        "natural-language task description (e.g. 'how does the commit "
        "message generator work'). Returns related symbols + their "
        "relationships. Use sparingly — it's expensive on a big project.\n"
        "- **list_directory** for path discovery when you don't know "
        "what's in a folder.\n"
        "- **git_log / git_diff** for change history and pending work.\n\n"
        "Error handling:\n"
        "- If a tool returns an error message starting with '[tool error]', "
        "DO NOT report the failure to the user. Instead, read the error "
        "carefully — it usually contains a concrete suggestion (e.g. "
        "'a file named X exists at src/X — retry with that path'). "
        "Apply the suggestion and call the tool again. Only report failure "
        "to the user as a last resort, after at least 2 retry attempts "
        "with different approaches.\n"
        "- If tokensave_search returns no results, that means the query "
        "isn't a symbol name. Switch to read_file (if you have a target "
        "file in mind) or list_directory (to discover one) — don't keep "
        "searching with variations.\n\n"
        "Style:\n"
        "- Keep answers concise. If a question is open-ended, ask a "
        "clarifying follow-up instead of writing a wall of text.\n"
        "- You CANNOT modify files, run commits, or change config. This is "
        "by design. If the user asks you to make changes, suggest the "
        "specific edits in your answer and let them apply them manually."
    )

    def __init__(self, notebook: ttk.Notebook, get_project_path):
        self._get_project_path = get_project_path
        self._ask_path: str | None = None
        self._ask_messages: list = []
        self._ask_stop_event: threading.Event | None = None
        self._ask_thread: threading.Thread | None = None
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  🤖 Ask  ")
        self._build()

    def _build(self):
        tab = self._tab

        # ── Header: project + model + clear ─────────────────────────────
        hdr = tk.Frame(tab, bg=C["base"], padx=14, pady=8)
        hdr.pack(fill=tk.X, side=tk.TOP)

        tk.Label(hdr, text="🤖  Ask",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        self._ask_project_lbl = tk.Label(
            hdr, text="(no project selected)",
            font=("Segoe UI", 10), bg=C["base"], fg=C["text"])
        self._ask_project_lbl.pack(side=tk.LEFT, padx=(10, 0))
        self._ask_model_lbl = tk.Label(
            hdr, text="", font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["overlay0"])
        self._ask_model_lbl.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Button(hdr, text="Clear history",
                   command=self._ask_clear).pack(side=tk.RIGHT)

        # ── Status line (under header) ──────────────────────────────────
        self._ask_status = tk.Label(
            tab, text="", font=("Segoe UI", 8, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W)
        self._ask_status.pack(fill=tk.X, padx=18, pady=(0, 4))

        # ── Input row (BOTTOM, packed before chat log so it stays put) ──
        # NOTE: the entry MUST have a visible border + contrasting bg or
        # it disappears against the parent frame.  Earlier version used
        # bg=mantle (#181825) on a base (#1e1e2e) parent with
        # relief=tk.FLAT — visually identical, so the field appeared
        # missing entirely.  Now uses surface0 (#313244) which is two
        # luminance steps lighter than base, plus a 1px SOLID border and
        # a 2px highlight ring that turns blue on focus.
        in_row = tk.Frame(tab, bg=C["base"], padx=14, pady=8)
        in_row.pack(fill=tk.X, side=tk.BOTTOM)

        self._ask_entry = tk.Entry(
            in_row, font=("Segoe UI", 10),
            bg=C["surface0"], fg=C["text"],
            insertbackground=C["text"],
            relief=tk.SOLID, bd=1,
            highlightthickness=2,
            highlightbackground=C["overlay0"],
            highlightcolor=C["blue"],
            width=40)
        self._ask_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                              ipady=6, padx=(0, 6))
        self._ask_entry.bind("<Return>", lambda e: self._ask_send())
        self._ask_entry.focus_set()

        self._ask_send_btn = ttk.Button(
            in_row, text="Send", style="Primary.TButton",
            command=self._ask_send)
        self._ask_send_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._ask_stop_btn = ttk.Button(
            in_row, text="■ Stop", style="Danger.TButton",
            command=self._ask_stop, state=tk.DISABLED)
        self._ask_stop_btn.pack(side=tk.LEFT)

        # ── Chat log (fills remaining space) ────────────────────────────
        log_outer = tk.Frame(tab, bg=C["base"])
        log_outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 4))
        log_inner = tk.Frame(log_outer, bg=C["mantle"])
        log_inner.pack(fill=tk.BOTH, expand=True)

        self._ask_log = tk.Text(
            log_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Segoe UI", 10),
            padx=10, pady=8, wrap=tk.WORD, state=tk.DISABLED)
        ask_vsb = ttk.Scrollbar(log_inner, orient="vertical",
                                 command=self._ask_log.yview)
        self._ask_log.configure(yscrollcommand=ask_vsb.set)
        self._ask_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ask_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._ask_log.tag_configure(
            "user",        foreground=C["blue"],
            font=("Segoe UI", 10, "bold"), spacing1=8, spacing3=2)
        self._ask_log.tag_configure(
            "assistant",   foreground=C["text"],
            spacing1=4, spacing3=4)
        self._ask_log.tag_configure(
            "tool_call",   foreground=C["peach"],
            font=("Consolas", 9), spacing1=4)
        self._ask_log.tag_configure(
            "tool_result", foreground=C["overlay0"],
            font=("Consolas", 9), lmargin1=20, lmargin2=20)
        self._ask_log.tag_configure(
            "error",       foreground=C["red"],
            font=("Segoe UI", 9, "italic"), spacing1=4)
        self._ask_log.tag_configure(
            "info",        foreground=C["overlay0"],
            font=("Segoe UI", 9, "italic"))

        self._ask_set_intro()

    def on_tab_selected(self):
        """Called by App._on_tab_changed when the Ask tab is focused."""
        path = self._get_project_path()
        if path:
            self._ask_path = path
        self._ask_refresh_header()
        try:
            self._ask_entry.focus_set()
        except tk.TclError:
            pass

    def _ask_set_intro(self):
        self._ask_log.configure(state=tk.NORMAL)
        self._ask_log.delete("1.0", tk.END)
        self._ask_log.insert(tk.END,
            "Ready. Ask anything about the selected project — I'll use "
            "read_file, list_directory, git_log, git_diff, and (when "
            "available) tokensave_search / tokensave_context to find "
            "answers. I cannot modify files.\n\n",
            "info")
        self._ask_log.configure(state=tk.DISABLED)

    def _ask_append(self, text: str, tag: str = "assistant"):
        if not text:
            return
        self._ask_log.configure(state=tk.NORMAL)
        self._ask_log.insert(tk.END, text, tag)
        self._ask_log.see(tk.END)
        self._ask_log.configure(state=tk.DISABLED)

    def _ask_refresh_header(self):
        if self._ask_path:
            self._ask_project_lbl.configure(
                text=os.path.basename(self._ask_path))
        else:
            self._ask_project_lbl.configure(text="(no project selected)")
        cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
        provider = cfg.get("provider") or "?"
        model = cfg.get("model") or "?"
        enabled = bool(cfg.get("enabled"))
        if enabled:
            self._ask_model_lbl.configure(
                text=f"[provider: {provider} / {model}]",
                fg=C["overlay0"])
        else:
            self._ask_model_lbl.configure(
                text="[AI is disabled — Settings → AI commit messages]",
                fg=C["red"])

    def _ask_clear(self):
        if self._ask_thread and self._ask_thread.is_alive():
            self._ask_stop()
        self._ask_messages = []
        self._ask_set_intro()
        self._ask_status.configure(text="")

    def _ask_stop(self):
        """Signal the agent thread to abort; in-flight HTTP request finishes
        but its result is discarded."""
        if self._ask_stop_event is not None:
            self._ask_stop_event.set()
        self._ask_status.configure(
            text="Cancelling — in-flight request will finish then stop.",
            fg=C["overlay0"])
        self._ask_stop_btn.configure(state=tk.DISABLED)

    def _ask_send(self):
        text = self._ask_entry.get().strip()
        if not text:
            return
        if self._ask_thread and self._ask_thread.is_alive():
            self._ask_status.configure(
                text="A request is already running — click Stop first.",
                fg=C["yellow"])
            return

        path = self._get_project_path()
        if path:
            self._ask_path = path
        if not self._ask_path:
            self._ask_append(
                "Select a project in the Projects tab first.\n\n", "error")
            return

        llm_cfg = (_cfg.get("commit_message_llm") or {}) if isinstance(_cfg, dict) else {}
        if not llm_cfg.get("enabled"):
            self._ask_append(
                "AI is disabled. Open Settings → AI commit messages and "
                "tick the enable box, then try again.\n\n", "error")
            return

        try:
            import agent as _agent_mod
            import agent_tools as _agent_tools_mod
        except ImportError as e:
            self._ask_append(
                f"Could not import agent module: {e}\n", "error")
            return

        self._ask_refresh_header()
        self._ask_append(f"\n👤  {text}\n\n", "user")
        self._ask_entry.delete(0, tk.END)
        self._ask_status.configure(
            text="⟳  Thinking…  (the model may call tools before answering)",
            fg=C["peach"])
        self._ask_send_btn.configure(state=tk.DISABLED)
        self._ask_stop_btn.configure(state=tk.NORMAL)

        if not self._ask_messages:
            self._ask_messages.append({
                "role": "system",
                "content": self._ASK_SYSTEM_PROMPT,
            })
        self._ask_messages.append({"role": "user", "content": text})

        stop_event = threading.Event()
        self._ask_stop_event = stop_event

        tokensave_exe = (_cfg.get("tokensave_exe") or "") if isinstance(_cfg, dict) else ""
        tools = _agent_tools_mod.build_tools(self._ask_path, tokensave_exe)
        agent_instance = _agent_mod.LocalAgent(llm_cfg, self._ask_path, tools)

        def _on_tool_call(name, args):
            short_args = json.dumps(args, ensure_ascii=False)
            if len(short_args) > 120:
                short_args = short_args[:120] + "…"
            self._tab.after(0, self._ask_append,
                            f"🔧  {name}({short_args})\n", "tool_call")

        def _on_tool_result(name, result):
            preview = result if len(result) <= 600 else (
                result[:600] + f"\n[... {len(result)-600} more chars ...]")
            self._tab.after(0, self._ask_append, preview + "\n\n", "tool_result")

        def _on_assistant_message(text):
            self._tab.after(0, self._ask_append, f"🤖  {text}\n\n", "assistant")

        def _on_done(final_text):
            def _ui():
                if final_text is None:
                    self._ask_status.configure(
                        text="✓  Done (no final answer text — model only "
                             "issued tool calls).",
                        fg=C["green"])
                else:
                    self._ask_status.configure(text="✓  Done.", fg=C["green"])
                self._ask_send_btn.configure(state=tk.NORMAL)
                self._ask_stop_btn.configure(state=tk.DISABLED)
                self._ask_stop_event = None
            self._tab.after(0, _ui)

        def _on_error(msg):
            def _ui():
                self._ask_append(f"⚠  {msg}\n\n", "error")
                self._ask_status.configure(text="✗  Error.", fg=C["red"])
                self._ask_send_btn.configure(state=tk.NORMAL)
                self._ask_stop_btn.configure(state=tk.DISABLED)
                self._ask_stop_event = None
            self._tab.after(0, _ui)

        def _worker():
            try:
                agent_instance.run(
                    self._ask_messages,
                    on_tool_call=_on_tool_call,
                    on_tool_result=_on_tool_result,
                    on_assistant_message=_on_assistant_message,
                    on_done=_on_done,
                    on_error=_on_error,
                    stop_event=stop_event,
                )
            except Exception as e:
                log.exception("Ask worker crashed")
                try:
                    self._tab.after(0, _on_error, f"{type(e).__name__}: {e}")
                except RuntimeError:
                    pass

        self._ask_thread = threading.Thread(
            target=_worker, daemon=True, name="ask-agent-worker")
        self._ask_thread.start()


# ── Reference / Snippets tab controller ────────────────────────────────────────

class SnippetsController:
    """Owns the Reference/Snippets tab UI."""

    def __init__(self, notebook: ttk.Notebook):
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Reference  ")
        self._build()

    def _build(self):
        tab = self._tab

        # ── Top: CLI cheatsheet ───────────────────────────────────────────────
        tk.Label(tab, text="CLI COMMANDS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=14, pady=(10, 4))

        cli_wrap = tk.Frame(tab, bg=C["mantle"])
        cli_wrap.pack(fill=tk.X, padx=14)

        cli_sb = ttk.Scrollbar(cli_wrap, orient="vertical")
        cli_txt = tk.Text(cli_wrap, height=9, font=("Consolas", 9),
                          bg=C["mantle"], fg=C["text"], relief=tk.FLAT,
                          padx=12, pady=8, wrap=tk.NONE,
                          cursor="arrow", state=tk.NORMAL,
                          yscrollcommand=cli_sb.set)
        cli_sb.configure(command=cli_txt.yview)
        cli_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cli_sb.pack(side=tk.RIGHT, fill=tk.Y)

        cli_txt.tag_configure("hd",  font=("Segoe UI", 9, "bold"), foreground=C["blue"],   spacing1=8, spacing3=2)
        cli_txt.tag_configure("cmd", font=("Consolas", 9),          foreground=C["peach"],  spacing3=1)
        cli_txt.tag_configure("dim", font=("Consolas", 9),          foreground=C["overlay0"])

        def cli_row(cmd, desc):
            cli_txt.insert(tk.END, f"  {cmd:<38}", "cmd")
            cli_txt.insert(tk.END, f"{desc}\n", "dim")

        def cli_h(t):
            cli_txt.insert(tk.END, f"\n  {t}\n", "hd")

        cli_h("Daily use")
        cli_row("tokensave sync",               "Incremental re-index (fast)")
        cli_row("tokensave sync --force",        "Full re-index from scratch")
        cli_row("tokensave sync --doctor",       "Sync and list what changed")
        cli_row("tokensave status",              "Stats + estimated token savings")
        cli_row("tokensave status --details",    "Stats with node-kind breakdown")
        cli_row("tokensave files",               "List all indexed files")
        cli_row("tokensave monitor",             "Live TUI of MCP tool calls")
        cli_h("Setup")
        cli_row("tokensave init",                "First-time index of a project")
        cli_row("tokensave install --agent claude", "Wire up Claude Code integration")
        cli_row("tokensave daemon",              "Auto-sync daemon (foreground)")
        cli_row("tokensave daemon --enable-autostart", "Install daemon as a service")
        cli_h("Troubleshooting")
        cli_row("tokensave doctor",              "Health check — diagnose issues")
        cli_row("tokensave upgrade",             "Self-update to latest version")
        cli_h("Cost & token tracking")
        cli_row("tokensave cost",                "7-day cost summary")
        cli_row("tokensave cost today",          "Today's spend only")
        cli_row("tokensave cost --by-model",     "Breakdown by Claude model")
        cli_h("Branches")
        cli_row("tokensave branch add",          "Track current git branch")
        cli_row("tokensave branch list",         "View tracked branches + DB sizes")
        cli_row("tokensave branch gc",           "Clean up deleted branches")

        cli_txt.configure(state=tk.DISABLED)

        # ── Bottom: Claude prompt snippets ────────────────────────────────────
        snippets_header = tk.Frame(tab, bg=C["base"])
        snippets_header.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(snippets_header, text="CLAUDE PROMPT SNIPPETS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(snippets_header, text="📖  Open Full Guide",
                   command=self._open_guide).pack(side=tk.RIGHT)

        snippets_frame = tk.Frame(tab, bg=C["base"])
        snippets_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        list_wrap = tk.Frame(snippets_frame, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self.snippet_lb = tk.Listbox(
            list_wrap, width=26, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        list_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                                command=self.snippet_lb.yview)
        self.snippet_lb.configure(yscrollcommand=list_sb.set)
        self.snippet_lb.pack(side=tk.LEFT, fill=tk.Y)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = tk.Frame(snippets_frame, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    def _refresh_snippet_list(self, reselect_index=None):
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        for title, text in PROMPT_SNIPPETS:
            self.snippet_lb.insert(tk.END, f"  {title}")
            self._active_snippets_map.append({"type": "builtin", "data": {"title": title, "text": text}})

        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        for idx, u in enumerate(_cfg.get("user_snippets", [])):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            self.snippet_lb.selection_clear(0, tk.END)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            return

        text = meta["data"]["text"]
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        is_user = (meta["type"] == "user")
        self._edit_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return
        self._tab.clipboard_clear()
        self._tab.clipboard_append(meta["data"]["text"])
        self._copy_status.configure(text="✔ Copied!")
        self._tab.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self._tab, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        SnippetEditDialog(self._tab, meta, self._on_snippet_saved)

    def _delete_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Delete snippet",
            f"Delete '{title}'?\n\nThis cannot be undone.",
            parent=self._tab,
        ):
            return
        user_snippets = _cfg.get("user_snippets", [])
        idx = meta["index"]
        del user_snippets[idx]
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog — save and refresh."""
        user_snippets = _cfg.get("user_snippets", [])
        if edit_meta is None:
            user_snippets.append({"title": title, "text": text})
            new_idx = len(PROMPT_SNIPPETS) + 1 + len(user_snippets) - 1
        else:
            idx = edit_meta["index"]
            user_snippets[idx] = {"title": title, "text": text}
            new_idx = len(PROMPT_SNIPPETS) + 1 + idx
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list(reselect_index=new_idx)

    def _open_guide(self):
        guide = os.path.join(_BASE_DIR, "TOKENSAVE_GUIDE.md")
        if os.path.isfile(guide):
            os.startfile(guide)
        else:
            messagebox.showerror("Not found",
                f"Guide not found at:\n{guide}", parent=self._tab)


# ── GitTabController ──────────────────────────────────────────────────────────

class GitTabController:
    """Owns the Git tab UI and all git operations.

    Decoupled from App via four explicit callbacks. No back-reference to App.
    Worker threads push log messages to _log_queue; _poll_log_queue drains it
    on the main thread every 100 ms so Tkinter is never touched from a thread.
    """

    def __init__(
        self,
        notebook: "ttk.Notebook",
        cfg: dict,
        get_path: "Callable[[], str | None]",
        on_log: "Callable[[str, str | None], None]",
        on_shell: "Callable[..., tuple[str, int]]",
        on_commit: "Callable[[str], None]",
    ):
        self._notebook           = notebook
        self._cfg                = cfg
        self._get_path           = get_path
        self._on_log             = on_log
        self._on_shell           = on_shell
        self._on_commit          = on_commit
        self._git_path: "str | None"   = None
        self._git_status_files: list   = []
        self._git_all_btns: list       = []
        self._git_push_pull_btns: list = []
        self._git_release_btns: list   = []
        self._git_op_in_flight: bool   = False
        self._log_queue                = queue.Queue()
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  Git  ")
        self._build_git_tab()
        self._poll_log_queue()

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_visible(self) -> bool:
        return self._git_tab_is_visible()

    def refresh(self) -> None:
        self._git_refresh()

    def set_active_path(self, path: str) -> None:
        self._git_path = path

    def has_path(self) -> bool:
        return bool(self._git_path)

    # ── Log queue ─────────────────────────────────────────────────────────────

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg, color = self._log_queue.get_nowait()
                try:
                    self._on_log(msg, color)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._tab.after(100, self._poll_log_queue)

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_git_tab(self):
        """Build the Git tab — shows live git state for the selected project."""
        self._build_git_header()
        mid = tk.Frame(self._tab, bg=C["base"], padx=14, pady=10)
        mid.pack(fill=tk.X)
        mid.columnconfigure(0, weight=1, minsize=200)
        mid.columnconfigure(1, weight=1, minsize=200)
        self._build_git_status_pane(mid)
        self._build_git_action_bar()
        self._build_git_diff_pane()

    def _build_git_header(self) -> None:
        hdr = tk.Frame(self._tab, bg=C["mantle"], padx=14, pady=8)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=C["mantle"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(left,
            text="OPERATING ON",
            font=("Segoe UI", 7, "bold"), bg=C["mantle"], fg=C["overlay0"]
            ).pack(anchor=tk.W)
        self._git_project_lbl = tk.Label(left,
            text="Select a project in the Projects tab",
            font=("Segoe UI", 13, "bold"), bg=C["mantle"], fg=C["blue"])
        self._git_project_lbl.pack(anchor=tk.W)

        info_row = tk.Frame(left, bg=C["mantle"])
        info_row.pack(anchor=tk.W, pady=(2, 0))

        self._git_branch_lbl = tk.Label(info_row,
            text="Branch: —", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"])
        self._git_branch_lbl.pack(side=tk.LEFT, padx=(0, 20))

        self._git_remote_lbl = tk.Label(info_row,
            text="Remote: No remote set", font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["overlay0"])
        self._git_remote_lbl.pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=C["mantle"])
        right.pack(side=tk.RIGHT, anchor=tk.N)

        self._btn_set_remote = ttk.Button(right, text="Set Remote",
                                          command=self.cmd_git_set_remote)
        self._btn_set_remote.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(self._btn_set_remote,
            "Connect this project to a GitHub repository.\n"
            "Paste the HTTPS URL from github.com/new.\n"
            "Required before you can Push or Pull.")

        btn_github = ttk.Button(right, text="🐙  GitHub…",
                                command=self.cmd_github_setup)
        btn_github.pack(side=tk.LEFT, padx=(0, 6))
        _Tooltip(btn_github,
            "Open the GitHub Setup wizard — walks you through creating\n"
            "a GitHub account, connecting this project, pushing your code,\n"
            "and publishing a Release with your built .exe file.")

        btn_refresh = ttk.Button(right, text="⟳  Refresh",
                                 command=self._git_refresh)
        btn_refresh.pack(side=tk.LEFT)
        _Tooltip(btn_refresh, "Re-check the project's current git state and update this tab.")

    def _build_git_status_pane(self, mid: tk.Frame) -> None:
        tk.Label(mid, text="WORKING TREE",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=0, sticky=tk.W, pady=(0, 4))

        tk.Label(mid, text="RECENT COMMITS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).grid(
                     row=0, column=1, sticky=tk.W, padx=(8, 0), pady=(0, 4))

        status_wrap = tk.Frame(mid, bg=C["mantle"])
        status_wrap.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 4))

        status_vsb = ttk.Scrollbar(status_wrap, orient="vertical")
        self._git_status_lb = tk.Listbox(
            status_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            selectbackground=C["surface1"],
            activestyle="none",
            relief=tk.FLAT, bd=0,
            yscrollcommand=status_vsb.set)
        status_vsb.configure(command=self._git_status_lb.yview)
        self._git_status_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                  padx=6, pady=4)
        status_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._git_status_lb.bind("<<ListboxSelect>>", self._on_git_status_select)

        log_wrap = tk.Frame(mid, bg=C["mantle"])
        log_wrap.grid(row=1, column=1, sticky=tk.NSEW, padx=(4, 0))

        log_vsb = ttk.Scrollbar(log_wrap, orient="vertical")
        self._git_log_txt = tk.Text(
            log_wrap, height=7,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=log_vsb.set)
        log_vsb.configure(command=self._git_log_txt.yview)
        self._git_log_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_git_action_bar(self) -> None:
        acts = tk.Frame(self._tab, bg=C["base"])
        acts.pack(fill=tk.X, padx=14, pady=(6, 4))

        row1 = tk.Frame(acts, bg=C["base"])
        row1.pack(anchor=tk.W, pady=(0, 4))
        row2 = tk.Frame(acts, bg=C["base"])
        row2.pack(anchor=tk.W)

        btn_push    = ttk.Button(row1, text="⬆  Push",
                                 command=self.cmd_git_push)
        btn_pull    = ttk.Button(row1, text="⬇  Pull",
                                 command=self.cmd_git_pull)
        btn_commit  = ttk.Button(row1, text="📝  Commit…",
                                 command=lambda: self._on_commit(self._git_path)
                                         if self._git_path else None)
        btn_undo    = ttk.Button(row1, text="↩  Undo Last Commit",
                                 command=self.cmd_git_undo_commit)
        btn_new     = ttk.Button(row2, text="🌿  New Branch",
                                 command=self.cmd_git_new_branch)
        btn_switch  = ttk.Button(row2, text="🔀  Switch Branch…",
                                 command=self.cmd_git_switch_branch)
        btn_merge   = ttk.Button(row2, text="⇄  Merge…",
                                 command=self.cmd_git_merge)
        btn_del     = ttk.Button(row2, text="🗑  Delete Branch…",
                                 command=self.cmd_git_delete_branch)
        btn_openpr  = ttk.Button(row2, text="🔗  Open PR",
                                 command=self.cmd_git_open_pr)
        btn_mergepr = ttk.Button(row2, text="🐙  Merge PR…",
                                 command=self.cmd_git_merge_pr)
        btn_release = ttk.Button(row2, text="📦  Release…",
                                 command=self.cmd_git_release)

        for btn in (btn_push, btn_pull, btn_commit, btn_undo,
                    btn_new, btn_switch, btn_merge, btn_del, btn_openpr,
                    btn_mergepr, btn_release):
            btn.pack(side=tk.LEFT, padx=(0, 6))

        _Tooltip(btn_push,
            "Send your saved commits to GitHub.\n"
            "Like uploading a backup — your work is now safe online\n"
            "and others can see it.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_pull,
            "Download any new commits from GitHub to this machine.\n"
            "Use this if you made changes on another computer,\n"
            "or if a collaborator pushed new work.\n\n"
            "Requires a remote (GitHub URL) to be set first.")
        _Tooltip(btn_commit,
            "Save a snapshot of your current changes.\n"
            "Like a save point in a game — you can always come back here.\n\n"
            "You'll write a short message describing what you changed.\n"
            "A suggestion is generated automatically from the file list.")
        _Tooltip(btn_undo,
            "Remove the most recent save point, but keep all your changes.\n"
            "Nothing is deleted — your edits stay exactly as they were.\n\n"
            "Useful if you committed too early or with the wrong message.")
        _Tooltip(btn_new,
            "Create a separate copy of the project to try out an idea.\n"
            "Changes on this branch won't touch your main code\n"
            "until you're ready to merge them in.")
        _Tooltip(btn_switch,
            "Jump to a different branch (version) of the project.\n"
            "For example: switch from an experiment back to 'master'.\n\n"
            "Tip: commit your changes first — switching with\n"
            "unsaved edits will fail.")
        _Tooltip(btn_merge,
            "Merge another branch INTO the branch you're currently on.\n"
            "Use this to bring a finished feature branch back into master.\n\n"
            "Typical workflow: switch to master → pull → merge your feature →\n"
            "push → delete the feature branch.\n\n"
            "Conflicts (if any) must be resolved manually in your editor.")
        _Tooltip(btn_del,
            "Delete a branch you no longer need.\n"
            "Safe by default — warns you if the branch has changes\n"
            "that haven't been saved back to the main branch yet.\n"
            "After local delete, offers to also delete it from GitHub\n"
            "if a remote copy exists.")
        _Tooltip(btn_openpr,
            "Open a Pull Request on GitHub for the current branch.\n\n"
            "A Pull Request is a way to say: 'I made some changes on a\n"
            "separate branch — please review them and merge into main.'\n\n"
            "On master/main: shows you how to create a branch first.\n"
            "On any other branch: opens GitHub's compare page directly.\n\n"
            "Requires a GitHub remote and the branch to be pushed first.")
        _Tooltip(btn_mergepr,
            "Merge an open Pull Request from GitHub.\n\n"
            "Lists every open PR on this repo with its +X/-Y diff size,\n"
            "then lets you pick one and choose a merge strategy:\n"
            "  • Merge commit  — preserves the PR's branch history\n"
            "  • Squash and merge — collapses to a single commit\n"
            "  • Rebase and merge — replays commits linearly\n\n"
            "Shows a confirmation with the title and strategy before\n"
            "doing anything. After a successful merge, the PR is closed\n"
            "and (optionally) its branch is deleted on GitHub.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")
        _Tooltip(btn_release,
            "One-button GitHub release.\n\n"
            "Opens a wizard that auto-drafts release notes from your\n"
            "commits, builds the project, zips dist/, tags locally,\n"
            "pushes, and publishes via `gh release create` — all in one\n"
            "threaded worker. Editable textarea so you can polish the\n"
            "notes before publishing.\n\n"
            "Requires: GitHub CLI (`gh`) installed AND a remote set.")

        self._git_all_btns       = [self._btn_set_remote, btn_push, btn_pull,
                                     btn_commit, btn_undo, btn_new,
                                     btn_switch, btn_merge, btn_del, btn_openpr,
                                     btn_mergepr, btn_release]
        self._git_push_pull_btns = [btn_push, btn_pull, btn_openpr]
        self._git_release_btns   = [btn_release, btn_mergepr]

        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _build_git_diff_pane(self) -> None:
        tk.Label(self._tab, text="DIFF  (click a file above to preview)",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(
                     anchor=tk.W, padx=14, pady=(4, 4))

        diff_wrap = tk.Frame(self._tab, bg=C["mantle"])
        diff_wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        diff_vsb = ttk.Scrollbar(diff_wrap, orient="vertical")
        diff_hsb = ttk.Scrollbar(diff_wrap, orient="horizontal")
        self._git_diff_txt = tk.Text(
            diff_wrap,
            font=("Consolas", 9),
            bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=6, pady=4,
            wrap=tk.NONE, cursor="arrow", state=tk.DISABLED,
            yscrollcommand=diff_vsb.set,
            xscrollcommand=diff_hsb.set)
        diff_vsb.configure(command=self._git_diff_txt.yview)
        diff_hsb.configure(command=self._git_diff_txt.xview)
        self._git_diff_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        diff_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        diff_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self._git_diff_txt.tag_configure("plus",   foreground=C["green"])
        self._git_diff_txt.tag_configure("minus",  foreground=C["red"])
        self._git_diff_txt.tag_configure("header", foreground=C["blue"])
        self._git_diff_txt.tag_configure("meta",   foreground=C["overlay0"])

    # ── Git tab data methods ──────────────────────────────────────────────────

    def _git_tab_is_visible(self) -> bool:
        try:
            return self._notebook.tab(self._notebook.select(), "text").strip() == "Git"
        except (tk.TclError, AttributeError):
            return False

    def _git_refresh(self):
        """Kick off a background thread that re-reads all git state."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            branch_out, brc = self._on_shell(
                [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
            is_repo = brc == 0
            branch  = branch_out.strip() if is_repo else "—"

            remote_out, rrc = self._on_shell(
                [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
            remote = remote_out.strip() if rrc == 0 else ""

            status_out, _ = self._on_shell(
                [GIT_EXE, "-C", path, "status", "--short"], path)

            log_out, lrc = self._on_shell(
                [GIT_EXE, "-C", path, "log", "--oneline", "-15"], path)
            log_text = log_out.strip() if lrc == 0 else ""

            self._tab.after(0, lambda: self._git_update_ui(
                path, name, is_repo, branch, remote, status_out, log_text))

        threading.Thread(target=worker, daemon=True).start()

    def _git_begin_op(self):
        """Mark a git operation as in flight and disable all Git tab buttons."""
        self._git_op_in_flight = True
        for btn in self._git_all_btns:
            btn.configure(state=tk.DISABLED)

    def _git_end_op(self):
        """Clear the in-flight flag and refresh the Git tab to re-enable buttons."""
        self._git_op_in_flight = False
        self._git_refresh()

    def _git_update_ui(self, path, name, is_repo, branch, remote,
                       status_raw, log_text):
        """Main-thread update of all Git tab widgets."""
        self._git_project_lbl.config(text=name)

        if is_repo:
            self._git_branch_lbl.config(text=f"Branch:  {branch}",
                                         fg=C["text"])
        else:
            self._git_branch_lbl.config(
                text="Not a git repository — right-click project → 🔧 Git Init",
                fg=C["peach"])

        if remote:
            disp = remote.replace("https://", "").replace("http://", "")
            disp = disp.rstrip("/")
            if disp.endswith(".git"):
                disp = disp[:-4]
            self._git_remote_lbl.config(text=f"Remote:  {disp}",
                                         fg=C["overlay0"])
        else:
            self._git_remote_lbl.config(text="Remote:  No remote set",
                                         fg=C["overlay0"])

        if self._git_op_in_flight:
            for btn in self._git_all_btns:
                btn.configure(state=tk.DISABLED)
        else:
            repo_state = tk.NORMAL if is_repo else tk.DISABLED
            for btn in self._git_all_btns:
                btn.configure(state=repo_state)
            push_pull_state = tk.NORMAL if (is_repo and remote) else tk.DISABLED
            for btn in self._git_push_pull_btns:
                btn.configure(state=push_pull_state)
            release_ok = bool(is_repo and remote and shutil.which("gh"))
            for btn in self._git_release_btns:
                btn.configure(state=tk.NORMAL if release_ok else tk.DISABLED)

        self._git_status_lb.configure(state=tk.NORMAL)
        self._git_status_lb.delete(0, tk.END)
        self._git_status_files = []
        if is_repo:
            # BUG FIX: do NOT call status_raw.strip() before splitlines() —
            # status lines for working-tree changes start with a leading space
            # (" M file.py"), and strip() eats that leading space from the
            # FIRST line only, shifting column 0..1 (status) and 3+ (path) so
            # the path silently loses its first character. Use splitlines()
            # directly and skip blanks individually.
            has_lines = False
            for line in status_raw.splitlines():
                if len(line) < 4:
                    continue
                has_lines = True
                xy    = line[:2]
                fname = line[3:]
                self._git_status_lb.insert(tk.END, f"  {xy}  {fname}")
                self._git_status_files.append((xy.strip(), fname))
            if not has_lines:
                self._git_status_lb.insert(tk.END, "  (working tree clean)")

        self._git_log_txt.configure(state=tk.NORMAL)
        self._git_log_txt.delete("1.0", tk.END)
        if is_repo:
            self._git_log_txt.insert(tk.END,
                log_text if log_text else "(no commits yet)")
        self._git_log_txt.configure(state=tk.DISABLED)

        self._git_diff_txt.configure(state=tk.NORMAL)
        self._git_diff_txt.delete("1.0", tk.END)
        if not is_repo:
            self._git_diff_txt.insert(tk.END,
                "This project has no git history.\n\n"
                "Right-click the project in the Projects tab\n"
                "→ 🔧 Git Init to set one up.")
        self._git_diff_txt.configure(state=tk.DISABLED)

    def _on_git_status_select(self, event=None):
        """Click a file in the status listbox → show its diff below."""
        sel = self._git_status_lb.curselection()
        if not sel or not self._git_status_files:
            return
        idx = sel[0]
        if idx >= len(self._git_status_files):
            return
        _, fname = self._git_status_files[idx]
        path = self._git_path
        if not path:
            return

        def worker():
            d1, _ = self._on_shell(
                [GIT_EXE, "-C", path, "diff", "--", fname], path)
            d2, _ = self._on_shell(
                [GIT_EXE, "-C", path, "diff", "--cached", "--", fname], path)
            combined = "\n".join(filter(None, [d1.strip(), d2.strip()]))
            if not combined:
                combined = f"(no diff available — {fname} may be untracked or binary)"
            self._tab.after(0, lambda d=combined: self._git_show_diff(d))

        threading.Thread(target=worker, daemon=True).start()

    def _git_show_diff(self, diff_text):
        """Render a diff string into the diff Text widget with colour tags."""
        txt = self._git_diff_txt
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)

        lines = diff_text.splitlines(keepends=True)
        CAP = 2000
        if len(lines) > CAP:
            lines = lines[:CAP]
            lines.append(f"\n… [diff capped at {CAP} lines — file is very large] …\n")

        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                txt.insert(tk.END, line, "plus")
            elif line.startswith("-") and not line.startswith("---"):
                txt.insert(tk.END, line, "minus")
            elif line.startswith("@@"):
                txt.insert(tk.END, line, "header")
            elif line.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file")):
                txt.insert(tk.END, line, "meta")
            else:
                txt.insert(tk.END, line)

        txt.configure(state=tk.DISABLED)

    # ── Git action commands ────────────────────────────────────────────────────

    def cmd_git_push(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pushing…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "push", "-u", "origin", "HEAD"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0 and _is_auth_error(out):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in this project folder and run:\n"
                        "    git push\n\n"
                        "A browser window will open asking you to log in to GitHub.\n"
                        "After that, this button will work normally.",
                        parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_pull(self):
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        name = os.path.basename(path)
        self._on_log(f"[{name}] Pulling…", C["peach"])
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "pull"], path,
                    env=_GIT_ENV_NO_PROMPT)
                col = C["green"] if rc == 0 else C["red"]
                for line in out.strip().splitlines()[-6:]:
                    self._log_queue.put((f"  {line}", col))
                if rc != 0:
                    if _is_auth_error(out):
                        self._tab.after(0, lambda: messagebox.showinfo(
                            "GitHub Authentication Required",
                            "GitHub needs to verify your identity.\n\n"
                            "Open a terminal in this project folder and run:\n"
                            "    git pull\n\n"
                            "A browser window will open asking you to log in to GitHub.\n"
                            "After that, this button will work normally.",
                            parent=self._root))
                    elif "conflict" in out.lower():
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            "Pull completed but there are merge conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<<).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.",
                            parent=self._root))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_open_pr(self):
        """Open a pull-request comparison page on GitHub for the current branch."""
        path = self._git_path
        if not path:
            return

        branch_out, brc = self._on_shell(
            [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)

        branch = branch_out.strip() if brc == 0 else ""
        remote = remote_out.strip() if rrc == 0 else ""

        if not remote:
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        base = remote.rstrip("/").removesuffix(".git")
        if base.startswith("git@github.com:"):
            base = "https://github.com/" + base[len("git@github.com:"):]

        is_main = branch in ("master", "main", "")

        if is_main:
            go = messagebox.askyesno(
                "You're on the main branch",
                f"You're on '{branch}' — the main/default branch.\n\n"
                "Pull Requests work like this:\n\n"
                "  1. 🌿 New Branch  →  give it a name (e.g. 'my-feature')\n"
                "  2. Make your changes, then 📝 Commit\n"
                "  3. ⬆ Push  →  sends the branch to GitHub\n"
                "  4. 🔗 Open PR  →  GitHub shows a 'Compare & pull request' button\n"
                "  5. Fill in the description and click 'Create pull request'\n\n"
                "Open the repository page on GitHub now?",
                parent=self._root)
            if go:
                os.startfile(base)
        else:
            pr_url = f"{base}/compare/{branch}"
            self._on_log(
                f"  [{os.path.basename(path)}] Opening PR page for branch '{branch}'…",
                C["peach"])
            os.startfile(pr_url)

    def cmd_git_merge_pr(self):
        """List open PRs on this repo's GitHub origin and let the user pick one to merge."""
        path = self._git_path
        if not path:
            return
        name = os.path.basename(path)

        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning(
                "No Remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header to add one first.",
                parent=self._root)
            return

        self._on_log(f"$ gh pr list  [{name}]", C["blue"])
        self._git_begin_op()

        def worker():
            try:
                r = subprocess.run(
                    ["gh", "pr", "list", "--state", "open",
                     "--json", "number,title,headRefName,baseRefName,"
                               "additions,deletions,author,url",
                     "--limit", "50"],
                    cwd=path, capture_output=True, text=True,
                    timeout=20, creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    err = (r.stderr or r.stdout or "").strip()
                    self._log_queue.put((f"  ✗ gh pr list failed: {err[:400]}", C["red"]))
                    return
                try:
                    prs = json.loads(r.stdout or "[]")
                except json.JSONDecodeError as e:
                    self._log_queue.put((f"  ✗ Could not parse gh output: {e}", C["red"]))
                    return
                if not prs:
                    self._log_queue.put(("  No open PRs on this repo.", C["overlay0"]))
                    return
                self._log_queue.put((
                    f"  Found {len(prs)} open PR(s).  Opening selection dialog…",
                    C["overlay0"]))
                self._tab.after(0, self._show_merge_pr_dialog, path, prs)
            except (OSError, subprocess.TimeoutExpired) as e:
                self._log_queue.put((f"  ✗ gh pr list error: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-list").start()

    def _show_merge_pr_dialog(self, path: str, prs: list):
        """Open the MergePRDialog with the fetched PR list."""
        MergePRDialog(self._root, path, prs, self._do_merge_pr)

    def _do_merge_pr(self, path: str, pr_number: int, strategy: str,
                     delete_branch: bool, pr_title: str):
        """Run `gh pr merge <N> --<strategy>` and stream output."""
        name = os.path.basename(path)
        flag = f"--{strategy}"
        cmd = ["gh", "pr", "merge", str(pr_number), flag]
        if delete_branch:
            cmd.append("--delete-branch")
        self._on_log(
            f"$ gh pr merge {pr_number} {flag}"
            f"{' --delete-branch' if delete_branch else ''}  [{name}]",
            C["blue"])
        self._git_begin_op()

        def worker():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if stripped:
                        self._log_queue.put((stripped, None))
                proc.wait()
                if proc.returncode == 0:
                    self._log_queue.put((
                        f"  ✓ PR #{pr_number} merged ({strategy}).  "
                        f"Pulling master to sync local…",
                        C["green"]))
                    self._tab.after(0, self._post_merge_pr_sync, path)
                else:
                    self._log_queue.put((
                        f"  ✗ gh pr merge exited with code {proc.returncode}",
                        C["red"]))
            except (OSError, FileNotFoundError) as e:
                self._log_queue.put((f"  ✗ Error running gh: {e}", C["red"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True, name="gh-pr-merge").start()

    def _post_merge_pr_sync(self, path: str):
        """After a successful PR merge, switch to master and pull."""
        name = os.path.basename(path)
        base = "master"
        try:
            r = subprocess.run(
                [GIT_EXE, "-C", path, "symbolic-ref",
                 "refs/remotes/origin/HEAD", "--short"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip():
                head = r.stdout.strip()
                if head.startswith("origin/"):
                    base = head[len("origin/"):]
        except (OSError, subprocess.TimeoutExpired):
            pass

        cur_out, cur_rc = self._on_shell(
            [GIT_EXE, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], path)
        cur = cur_out.strip() if cur_rc == 0 else ""

        def worker():
            self._git_begin_op()
            try:
                if cur and cur != base:
                    self._log_queue.put((f"  Switching to '{base}'…", C["overlay0"]))
                    out, rc = self._on_shell(
                        [GIT_EXE, "-C", path, "switch", base], path)
                    if rc != 0:
                        self._log_queue.put((
                            f"  ⚠ Could not switch to '{base}': "
                            f"{out.strip()[:200]}.  Pull manually after switching.",
                            C["peach"]))
                        return
                self._log_queue.put((f"  Pulling latest '{base}'…", C["overlay0"]))
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "pull", "--ff-only"], path)
                if rc == 0:
                    self._log_queue.put((f"  ✓ Local '{base}' synced.", C["green"]))
                else:
                    self._log_queue.put((
                        f"  ⚠ Pull failed: {out.strip()[:200]}", C["peach"]))
            finally:
                self._tab.after(0, self._git_end_op)
                self._tab.after(0, self._git_refresh)

        threading.Thread(target=worker, daemon=True, name="post-merge-sync").start()

    def cmd_git_release(self):
        """Open the Release Wizard for the currently loaded Git-tab project."""
        path = self._git_path
        if not path:
            return

        if not shutil.which("gh"):
            messagebox.showwarning("GitHub CLI required",
                "The Release Wizard runs `gh release create` under the hood.\n\n"
                "Install GitHub CLI from https://cli.github.com and re-open\n"
                "this dialog.",
                parent=self._root)
            return

        if not _is_local_git_repo(path):
            messagebox.showwarning("Not a git repo",
                f"{os.path.basename(path)} is not a git repository.\n\n"
                "Right-click the project → 🔧 Git Init first.",
                parent=self._root)
            return

        remote_out, rrc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        if rrc != 0 or not remote_out.strip():
            messagebox.showwarning("No remote",
                "This project has no GitHub remote set.\n\n"
                "Click 'Set Remote' in the Git tab header first.",
                parent=self._root)
            return

        status_out, src = self._on_shell(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if src == 0 and status_out.strip():
            dirty_files = []
            for line in status_out.splitlines():
                if len(line) < 4:
                    continue
                fname = line[3:].strip()
                if fname and fname != "CHANGELOG.md":
                    dirty_files.append(fname)
            if dirty_files:
                preview = "\n".join(f"  • {f}" for f in dirty_files[:6])
                if len(dirty_files) > 6:
                    preview += f"\n  • …and {len(dirty_files) - 6} more"
                choice = messagebox.askyesnocancel(
                    "Working tree has unrelated changes",
                    f"The Release Wizard needs a clean working tree so the\n"
                    f"release-prep commit only contains the version bump.\n\n"
                    f"Uncommitted files:\n{preview}\n\n"
                    f"Yes  → open the Git Commit dialog now\n"
                    f"No   → cancel, deal with it later\n"
                    f"(Stash flow is on the roadmap.)",
                    parent=self._root)
                if choice is None or choice is False:
                    return
                self._on_commit(path)
                return

        ReleaseWizardDialog(self._root, path, _state)

    def cmd_git_undo_commit(self):
        """Undo the last commit, keeping all changes staged."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return
        if not messagebox.askyesno(
                "Undo Last Commit",
                "Undo the last commit?\n\n"
                "Your changes will be kept and moved back to 'staged'.\n"
                "Nothing is deleted — you can re-commit at any time.",
                parent=self._root):
            return
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "reset", "--soft", "HEAD~1"], path)
                col = C["green"] if rc == 0 else C["red"]
                msg = "Last commit undone — changes are now staged." if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_set_remote(self):
        """Open the Set Remote dialog to connect this project to GitHub."""
        path = self._git_path
        if not path:
            return
        out, rc = self._on_shell(
            [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
        current_url = out.strip() if rc == 0 else ""
        SetRemoteDialog(self._root, path, current_url, self._do_git_set_remote)

    def _do_git_set_remote(self, path: str, url: str):
        """Callback from SetRemoteDialog — add or update the origin remote."""
        self._git_begin_op()

        def worker():
            try:
                _, rc_check = self._on_shell(
                    [GIT_EXE, "-C", path, "remote", "get-url", "origin"], path)
                if rc_check == 0:
                    cmd = [GIT_EXE, "-C", path, "remote", "set-url", "origin", url]
                else:
                    cmd = [GIT_EXE, "-C", path, "remote", "add", "origin", url]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = "updated" if rc_check == 0 else "added"
                msg = f"Remote {action}: {url}" if rc == 0 else out.strip()
                self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_github_setup(self):
        """Open the GitHub Setup wizard for the selected/current project."""
        path = self._git_path or self._get_path()
        if not path:
            messagebox.showwarning("No project selected",
                "Select a project first.", parent=self._root)
            return
        GitHubSetupDialog(self._root, path, _state)

    def cmd_git_new_branch(self):
        """Open New Branch dialog."""
        path = self._git_path
        if not path:
            return
        NewBranchDialog(self._root, path, self._do_git_new_branch)

    def _do_git_new_branch(self, path: str, name: str, switch: bool):
        self._git_begin_op()

        def worker():
            try:
                if switch:
                    cmd = [GIT_EXE, "-C", path, "checkout", "-b", name]
                else:
                    cmd = [GIT_EXE, "-C", path, "branch", name]
                out, rc = self._on_shell(cmd, path)
                col = C["green"] if rc == 0 else C["red"]
                action = f"Created and switched to '{name}'" if (switch and rc == 0) \
                         else (f"Created '{name}'" if rc == 0 else out.strip())
                self._log_queue.put((f"  [{os.path.basename(path)}] {action}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_switch_branch(self):
        """Open Switch Branch dialog."""
        path = self._git_path
        if not path:
            return
        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return
        branches = []
        current  = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                branches.append(line.strip())
        SwitchBranchDialog(self._root, path, branches, current,
                           self._do_git_switch_branch)

    def _do_git_switch_branch(self, path: str, name: str):
        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "checkout", name], path)
                if rc != 0:
                    self._tab.after(0, lambda: messagebox.showerror(
                        "Switch Failed",
                        "Could not switch branches.\n\n"
                        "You may have uncommitted changes that conflict with the target branch.\n\n"
                        "Please commit or undo your changes before switching.",
                        parent=self._root))
                else:
                    self._log_queue.put((
                        f"  [{os.path.basename(path)}] Switched to branch '{name}'",
                        C["green"]))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_merge(self):
        """Merge another branch INTO the current branch."""
        path = self._git_path
        if not path:
            return
        if self._git_op_in_flight:
            return

        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return
        non_current = []
        current = ""
        for line in out.strip().splitlines():
            if line.startswith("* "):
                current = line[2:].strip()
            else:
                non_current.append(line.strip())
        if not non_current:
            messagebox.showinfo("No Other Branches",
                "There are no other branches to merge from.", parent=self._root)
            return

        proj = os.path.basename(path)
        source = SwitchBranchDialog.pick(self._root,
            f"Merge into {current} — {proj}",
            non_current, parent_widget=self._root)
        if not source:
            return

        if not messagebox.askyesno(
                "Merge Branch",
                f"Merge '{source}' INTO '{current}'?\n\n"
                f"This brings commits from '{source}' into '{current}'.\n"
                "Your working tree must be clean.\n\n"
                "If conflicts occur, resolve them in your editor, then\n"
                "Commit the result.",
                parent=self._root):
            return

        self._git_begin_op()

        def worker():
            try:
                out, rc = self._on_shell(
                    [GIT_EXE, "-C", path, "merge", "--no-edit", source], path)
                col = C["green"] if rc == 0 else C["red"]
                if rc == 0:
                    self._log_queue.put((
                        f"  [{proj}] Merged '{source}' into '{current}'", col))
                    for line in out.strip().splitlines()[-4:]:
                        self._log_queue.put((f"    {line}", col))
                else:
                    out_l = out.lower()
                    if "conflict" in out_l:
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Merge Conflicts",
                            f"Merging '{source}' into '{current}' produced conflicts.\n\n"
                            "Open the project in your editor and look for files\n"
                            "marked with conflict markers (<<<<<< / >>>>>>).\n"
                            "Resolve them, then use 📝 Commit… to commit the result.\n\n"
                            "Or open a terminal in the project folder and run\n"
                            "    git merge --abort\n"
                            "to undo the merge attempt entirely.",
                            parent=self._root))
                    elif "unmerged" in out_l or "your local changes" in out_l:
                        self._tab.after(0, lambda: messagebox.showwarning(
                            "Working Tree Not Clean",
                            f"Cannot merge — '{current}' has uncommitted changes.\n\n"
                            "Commit or stash them first, then try again.",
                            parent=self._root))
                    self._log_queue.put((f"  [{proj}] Merge failed", col))
                    for line in out.strip().splitlines()[-4:]:
                        self._log_queue.put((f"    {line}", col))
            finally:
                self._tab.after(0, self._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_git_delete_branch(self):
        """Delete a non-current branch with safe/force-delete distinction."""
        path = self._git_path
        if not path:
            return
        branch = self._confirm_branch_delete(path)
        if branch is None:
            return
        self._do_delete_branch(path, branch)

    def _confirm_branch_delete(self, path: str) -> "str | None":
        """List non-current branches, prompt for selection, and confirm."""
        out, rc = self._on_shell([GIT_EXE, "-C", path, "branch"], path)
        if rc != 0:
            messagebox.showerror("Git Error", out.strip(), parent=self._root)
            return None
        non_current = [line.strip() for line in out.strip().splitlines()
                       if not line.startswith("* ")]
        if not non_current:
            messagebox.showinfo("No Branches",
                "There are no other branches to delete.", parent=self._root)
            return None
        branch = SwitchBranchDialog.pick(
            self._root, f"Delete Branch — {os.path.basename(path)}",
            non_current, parent_widget=self._root)
        if not branch:
            return None
        if not messagebox.askyesno(
                "Delete Branch",
                f"Delete branch '{branch}'?\n\n"
                "If this branch has been merged, it will be removed safely.",
                parent=self._root):
            return None
        return branch

    def _do_delete_branch(self, path: str, branch: str) -> None:
        """Start the local-branch-delete flow on a background thread."""
        self._git_begin_op()
        threading.Thread(
            target=self._del_branch_worker, args=(path, branch),
            daemon=True).start()

    # ── Branch-delete helpers (one per step; thread boundary in name) ────────
    # Methods named *_worker run on a background thread → use self._tab.after()
    # for any UI touch. Other methods run on the main thread → Tkinter-safe.

    def _del_branch_worker(self, path: str, branch: str) -> None:
        """Thread: attempt safe delete (`git branch -d`). Route to next step."""
        try:
            out, rc = self._on_shell(
                [GIT_EXE, "-C", path, "branch", "-d", branch], path)
            if rc == 0:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] Deleted branch '{branch}'",
                    C["green"]))
                self._tab.after(0, self._del_branch_offer_remote, path, branch)
                return
            out_l = out.lower()
            if "not fully merged" in out_l or "unmerged" in out_l:
                self._tab.after(0, self._del_branch_ask_force, path, branch)
            else:
                self._tab.after(0, lambda: messagebox.showerror(
                    "Delete Failed",
                    f"Could not delete branch '{branch}':\n\n{out.strip()}",
                    parent=self._root))
                self._tab.after(0, self._git_end_op)
        except Exception:
            self._tab.after(0, self._git_end_op)
            raise

    def _del_branch_ask_force(self, path: str, branch: str) -> None:
        """Main thread: ask user whether to force-delete an unmerged branch."""
        if not messagebox.askyesno(
                "Force Delete?",
                f"Branch '{branch}' has unmerged changes.\n\n"
                "Force-delete anyway?\n"
                "This permanently discards those commits.",
                parent=self._root):
            self._git_end_op()
            return
        threading.Thread(
            target=self._del_branch_force_worker, args=(path, branch),
            daemon=True).start()

    def _del_branch_force_worker(self, path: str, branch: str) -> None:
        """Thread: force-delete (`git branch -D`). Route to remote-offer on success."""
        try:
            o2, r2 = self._on_shell(
                [GIT_EXE, "-C", path, "branch", "-D", branch], path)
            col = C["green"] if r2 == 0 else C["red"]
            msg = f"Force-deleted '{branch}'" if r2 == 0 else o2.strip()
            self._log_queue.put((f"  [{os.path.basename(path)}] {msg}", col))
            if r2 == 0:
                self._tab.after(0, self._del_branch_offer_remote, path, branch)
                return
        finally:
            self._tab.after(0, self._git_end_op)

    def _del_branch_offer_remote(self, path: str, branch: str) -> None:
        """Main thread: check for a remote copy; ask user whether to delete it too."""
        rbo, rbrc = self._on_shell(
            [GIT_EXE, "-C", path, "branch", "-r"], path)
        has_remote = rbrc == 0 and any(
            line.strip().split(" ", 1)[0] == f"origin/{branch}"
            for line in rbo.strip().splitlines())
        if not has_remote:
            self._git_end_op()
            return
        if not messagebox.askyesno(
                "Delete from GitHub too?",
                f"'{branch}' is deleted locally, but a copy still\n"
                f"exists on GitHub (origin/{branch}).\n\n"
                "Also delete it from GitHub?\n"
                "(This is the same as running\n"
                f"  git push origin --delete {branch})",
                parent=self._root):
            self._git_end_op()
            return
        threading.Thread(
            target=self._del_branch_remote_worker, args=(path, branch),
            daemon=True).start()

    def _del_branch_remote_worker(self, path: str, branch: str) -> None:
        """Thread: `git push origin --delete <branch>`. Log result."""
        try:
            ro, rrc = self._on_shell(
                [GIT_EXE, "-C", path, "push", "origin", "--delete", branch],
                path, env=_GIT_ENV_NO_PROMPT)
            col = C["green"] if rrc == 0 else C["red"]
            if rrc == 0:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] "
                    f"Deleted 'origin/{branch}' from GitHub", col))
            else:
                self._log_queue.put((
                    f"  [{os.path.basename(path)}] Remote delete failed", col))
                for line in ro.strip().splitlines()[-4:]:
                    self._log_queue.put((f"    {line}", col))
                if _is_auth_error(ro):
                    self._tab.after(0, lambda: messagebox.showinfo(
                        "GitHub Authentication Required",
                        "GitHub needs to verify your identity.\n\n"
                        "Open a terminal in the project folder and run:\n"
                        f"    git push origin --delete {branch}\n\n"
                        "A browser window will open asking you to log in.",
                        parent=self._root))
        finally:
            self._tab.after(0, self._git_end_op)


# ── App ────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TokenSave Manager")
        self.geometry("760x600")
        self.minsize(600, 520)
        self.configure(bg=C["base"])
        # Hold the canonical ManagerConfig instance. Future controllers and
        # dialogs (extracted in Phases B–E) receive this via __init__ and
        # read live values through cfg.git_exe / cfg.tokensave_exe / etc.
        # During the Phase A transition window, legacy module globals
        # (TOKENSAVE, GIT_EXE, …) still exist and get re-bound by
        # _on_settings_saved alongside _state.refresh_derived().
        self._cfg = _state
        self._current_proc = None
        self._stop_requested = False
        # Cached tokensave version info.  Current version is populated at
        # App startup via `tokensave --version` (fast, no network).
        # Available-update version is populated by the output parser in
        # `_run` when tokensave emits "Update available: vA → vB" at the
        # end of a sync — that line is opportunistic (tokensave appears
        # to throttle update checks to once per day), so SettingsDialog
        # ALWAYS shows the Upgrade button with the current version, and
        # only labels it with the target version when one is known.
        self._tokensave_current_version: str | None = None
        self._tokensave_available_version: str | None = None
        self._probe_tokensave_version()
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {TOKENSAVE}")
        log.info(f"  templates: {TEMPLATE_DIR}")
        log.info(f"  log file : {LOG_FILE}")
        self._style()
        self._build()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray = None
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.bind("<Unmap>", self._on_unmap)
        self.after(300, self._check_config)

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.withdraw()
        log.debug("Window hidden to tray")

    def _on_unmap(self, event):
        if event.widget is self:
            self.after(100, self._maybe_hide)

    def _maybe_hide(self):
        if self.state() == "iconic":
            self.withdraw()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None):
        log.info("Quit requested from tray")
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)

    # ── Styles ─────────────────────────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C["base"], foreground=C["text"],
            font=("Segoe UI", 10), borderwidth=0)

        s.configure("Treeview",
            background=C["mantle"], foreground=C["text"],
            fieldbackground=C["mantle"], rowheight=30,
            font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["surface0"], foreground=C["subtext"],
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", C["surface1"])],
            foreground=[("selected", C["text"])])

        s.configure("TButton",
            background=C["surface0"], foreground=C["text"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("TButton",
            background=[("active", C["surface1"]), ("pressed", C["surface1"])])

        s.configure("Primary.TButton",
            background=C["blue"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Primary.TButton",
            background=[("active", C["lavender"]), ("pressed", C["lavender"])])

        s.configure("Action.TButton",
            background=C["peach"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Action.TButton",
            background=[("active", C["yellow"]), ("pressed", C["yellow"])])

        s.configure("Danger.TButton",
            background=C["surface0"], foreground=C["red"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("Danger.TButton",
            background=[("active", C["surface1"])])

        s.configure("TScrollbar",
            background=C["surface0"], troughcolor=C["mantle"],
            bordercolor=C["base"], arrowcolor=C["overlay0"],
            relief="flat")

        s.configure("TSeparator", background=C["surface0"])

        s.configure("TNotebook",
            background=C["base"], borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=C["surface0"], foreground=C["subtext"],
            padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
            background=[("selected", C["base"])],
            foreground=[("selected", C["blue"])])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=C["mantle"], pady=12, padx=16)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="TokenSave Manager",
                 font=("Segoe UI", 15, "bold"),
                 bg=C["mantle"], fg=C["blue"]).pack(side=tk.LEFT)

        self.active_badge = tk.Label(hdr, text="",
            font=("Segoe UI", 9), bg=C["surface0"],
            fg=C["green"], padx=8, pady=3)
        self.active_badge.pack(side=tk.RIGHT)

        # ── Credit bar ──
        tk.Label(self, text="TokenSave Manager  ·  Alexander L Corthell",
                 font=("Segoe UI", 7), bg=C["crust"], fg=C["overlay0"],
                 pady=2).pack(fill=tk.X, side=tk.BOTTOM)

        # ── Separator + Log — packed BEFORE notebook so expand=True doesn't eat it ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, side=tk.BOTTOM)

        log_frame = tk.Frame(self, bg=C["base"], padx=14, pady=8)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Notebook ──
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._projects = ProjectsTabController(
            self.nb, _cfg,
            get_projects=lambda: self.projects,
            on_run=self._run,
            on_run_capture=self._run_capture,
            on_shell=self._shell_capture,
            on_log=self._log,
            on_commit=self._open_commit_dialog,
            on_refresh=self.refresh,
            on_project_select=self._on_project_selected,
            on_set_running=self._set_running,
            on_settings=self.cmd_settings,
        )
        self._git = GitTabController(
            self.nb, _cfg,
            get_path=self._get_git_path,
            on_log=self._log,
            on_shell=self._shell_capture,
            on_commit=self._open_commit_dialog,
        )
        self._ask_ctrl = AskTabController(self.nb, self._get_ask_project_path)
        self._snippets_ctrl = SnippetsController(self.nb)
        self._build_help_tab()

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        self._stop_btn = ttk.Button(log_header, text="■  Stop",
                                    style="Danger.TButton",
                                    command=self._stop_current,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._running_label = tk.Label(log_header, text="",
                                       font=("Segoe UI", 8),
                                       bg=C["base"], fg=C["yellow"])
        self._running_label.pack(side=tk.RIGHT, padx=(0, 8))

        log_inner = tk.Frame(log_frame, bg=C["mantle"])
        log_inner.pack(fill=tk.X)

        self.log = tk.Text(log_inner, height=4,
            font=("Consolas", 9), bg=C["mantle"], fg=C["green"],
            insertbackground=C["green"], relief=tk.FLAT,
            padx=10, pady=6, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(log_inner, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Tab / project navigation ────────────────────────────────────────────

    def _on_project_selected(self, path: str) -> None:
        """Fired by ProjectsTabController when the user clicks a project row.

        Routes the new project path to GitTabController and AskTabController.
        This replaces the old event-handler _on_project_select which accessed
        self.tree directly.
        """
        self._git.set_active_path(path)
        if self._git.is_visible():
            self._git.refresh()

    def _on_tab_changed(self, event=None):
        """Fires when the user switches notebook tabs."""
        try:
            current_tab_text = self.nb.tab(self.nb.select(), "text").strip()
        except tk.TclError:
            current_tab_text = ""
        if "Ask" in current_tab_text:
            self._ask_ctrl.on_tab_selected()
            return

        if not self._git.is_visible():
            return
        sel_path = self._projects.get_selected_path()
        if sel_path:
            self._git.set_active_path(sel_path)
        elif not self._git.has_path() and self.active_path:
            self._git.set_active_path(self.active_path)
        if self._git.has_path():
            self._git.refresh()

    def _get_ask_project_path(self) -> str | None:
        """Return the currently focused project path for AskTabController."""
        if hasattr(self, "_projects"):
            path = self._projects.get_selected_path()
            if path:
                return path
        return getattr(self, "active_path", None)

    def _get_git_path(self) -> str | None:
        """Return the currently focused project path for GitTabController."""
        if hasattr(self, "_projects"):
            path = self._projects.get_selected_path()
            if path:
                return path
        return getattr(self, "active_path", None)

    # ═══════════════════════════════════════════════════════════════════
    # 🤖 Ask tab — handled by AskTabController (see above App class)
    # 📚 Reference tab — handled by SnippetsController (see above App class)
    # ═══════════════════════════════════════════════════════════════════

    def _build_help_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  Project Categories",  self._help_categories),
            ("  Git: What & Why",     self._help_git_concepts),
            ("  Git: Daily Workflow", self._help_git_workflow),
            ("  Git Tab Buttons",     self._help_git_tab),
            ("  GitHub Setup",        self._help_github_setup),
            ("  CodeGraph",           self._help_codegraph),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    def _help_show(self, fn):
        """Clear the help text widget, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ──────────────────────────────────────────────────────────

    def _help_switching(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_categories(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Project Categories")
            p("Projects are automatically grouped under the label of the search root "
              "folder they belong to. You can override any project's category — and add "
              "an optional sub-category — without moving any files.")
            br()
            h2("How root labels work")
            p("Each entry in Settings → Search Roots has a Label. That label becomes "
              "the category header for all projects found inside that folder. Edit the "
              "label in Settings to rename the whole group at once.")
            br()
            h2("Overriding a single project")
            ins("  1. Right-click the project row\n", "body")
            ins("  2. Choose  📁 Assign Category…\n", "body")
            ins("  3. Pick or type a Category (and optional Sub-category)\n", "body")
            ins("  4. Click OK — the project moves to the new group immediately\n", "body")
            br()
            p("To remove an override and return the project to its root's group, "
              "open Assign Category… and click Clear Override.")
            br()
            h2("Sub-categories")
            p("Sub-categories appear indented under their parent category (shown as "
              "↳ Sub-category). They work like folders-within-folders. Right-click "
              "any project at any time to move it between groups.")
            br()
            warn("⚠  Category headers and sub-category rows are not selectable — "
                 "right-click and action buttons only work on project rows.")
        self._help_show(_fill)

    def _help_git_concepts(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: What & Why")
            p("Git is a tool that remembers the history of every change you make to "
              "your project. Think of it like infinite undo — but smarter. You decide "
              "when to save a checkpoint, and you can always go back.")
            br()
            h2("Commit — a save point")
            p("A commit is a snapshot of your project at a moment in time. Each one "
              "has a short message you write, like 'fix: typo in README' or "
              "'feat: add dark mode'. Over time, these build up into a history "
              "you can scroll through.")
            br()
            h2("Repository (repo) — the project folder + its history")
            p("When you run Git Init on a project, git creates a hidden .git folder "
              "inside it. That folder stores every commit ever made. The whole thing "
              "— your files plus that history — is called a repository.")
            br()
            h2("Branch — a parallel version")
            p("Imagine photocopying your project so you can experiment on the copy "
              "without touching the original. That's a branch. When you're happy "
              "with the experiment, you can merge it back. The default branch is "
              "usually called 'master' or 'main'.")
            br()
            h2("Remote — a copy on GitHub")
            p("A remote is a second home for your repository, stored on GitHub's "
              "servers. It acts as a backup and lets others see your work. The "
              "remote is usually called 'origin'.")
            br()
            h2("Push — upload to GitHub")
            p("After making commits on your machine, Push sends them to GitHub. "
              "Nothing leaves your computer until you Push — commits are purely local "
              "until then.")
            br()
            h2("Pull — download from GitHub")
            p("Pull fetches any commits from GitHub that you don't have yet and "
              "adds them to your local history. Useful if you work on multiple "
              "machines, or if a collaborator pushed something new.")
            br()
            h2("Working tree — uncommitted changes")
            p("The working tree is the current state of your files right now, before "
              "you've committed them. The Git tab shows a list of files that have "
              "changed since your last commit. An 'M' means modified, '?' means "
              "a new file git hasn't seen before, 'D' means deleted.")
            br()
            h2("Staging — choosing what to commit")
            p("Git lets you pick exactly which changes to include in a commit. "
              "The 'Stage all changes' checkbox in the Commit dialog does this "
              "automatically — it stages everything in the working tree, which is "
              "almost always what you want.")
            br()
            ok("Bottom line: commit often, push when you're done for the day.")
        self._help_show(_fill)

    def _help_git_workflow(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git: Daily Workflow")
            p("Here's how a typical coding session looks when using the Git tab.")
            br()
            h2("Starting a session")
            ins("  1. Switch to the Git tab\n", "body")
            ins("  2. Click ⟳ Refresh to see the current state\n", "body")
            ins("  3. If there's a remote set, click ⬇ Pull first — picks up any\n"
                "     changes from GitHub before you start editing\n", "body")
            br()
            h2("While you're working")
            p("Edit your files normally. The Working Tree list updates whenever "
              "you Refresh. Click any file in the list to see exactly what changed "
              "(green = added, red = removed).")
            br()
            h2("Saving your work (committing)")
            ins("  1. Click  📝 Commit…\n", "body")
            ins("  2. The dialog shows what files changed and suggests a message\n", "body")
            ins("  3. Edit the message if you like — keep it short and descriptive\n", "body")
            ins("  4. Click Commit\n", "body")
            br()
            p("There's no rule for how often to commit. A good rule of thumb: "
              "commit whenever you finish one thing. Small commits are better than "
              "one huge commit at the end of the day.")
            br()
            h2("Uploading to GitHub (pushing)")
            ins("  1. Click  ⬆ Push\n", "body")
            ins("  2. The output log shows whether it succeeded\n", "body")
            ins("  3. Your commits are now on GitHub — backed up and shareable\n", "body")
            br()
            h2("Trying out an idea safely (branching)")
            ins("  1. Click  🌿 New Branch  and give it a name (e.g. 'try-new-ui')\n", "body")
            ins("  2. Check 'Switch to this branch immediately'\n", "body")
            ins("  3. Make your changes and commit as normal\n", "body")
            ins("  4. If you don't like it: 🔀 Switch Branch back to master — the\n"
                "     experiment branch stays there but your main code is untouched\n", "body")
            br()
            h2("Finishing a feature branch (merge & cleanup)")
            p("Once your branch is tested and ready to bring back into master:")
            ins("  1.  🔀 Switch Branch  → master\n", "body")
            ins("  2.  ⬇ Pull            — pick up any new master commits first\n", "body")
            ins("  3.  ⇄ Merge…          → pick your feature branch\n", "body")
            ins("                          Confirmation says 'Merge X INTO master?' — yes\n", "body")
            ins("  4.  ⬆ Push            — master with the merged commits goes to GitHub\n", "body")
            ins("  5.  🗑 Delete Branch  → pick your feature branch → Yes (local)\n", "body")
            ins("                          Then: 'Also delete from GitHub?' → Yes\n", "body")
            br()
            p("If the merge produces conflicts, the manager pops a dialog telling "
              "you what to do (resolve in editor + commit, or run "
              "'git merge --abort' to undo). Conflicts only happen when both "
              "branches changed the same lines.")
            br()
            h2("Undoing mistakes")
            p("Made a bad commit? Click  ↩ Undo Last Commit. Your changes come back "
              "as uncommitted edits — you can fix them and recommit, or just discard.")
            br()
            warn("⚠  Undo Last Commit only removes the last commit. To undo older "
                 "commits, use the terminal.")
            br()
            h2("Typical day in one line")
            dim("  Pull → Edit → Commit → Edit → Commit → Push")
        self._help_show(_fill)

    def _help_git_tab(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Git Tab")
            p("The Git tab shows live status for whichever project is selected in the "
              "Projects tab. It updates automatically when you switch projects or switch "
              "to this tab.")
            br()
            h2("Working Tree & Diff")
            p("The Working Tree panel lists every modified, added, or deleted file. "
              "Click any file to see its diff below — added lines are green, removed "
              "lines are red.")
            br()
            h2("Committing changes")
            ins("  1. Make your edits (in your editor, or via Claude)\n", "body")
            ins("  2. Click  📝 Commit… — the dialog opens with a suggested message\n", "body")
            ins("  3. Edit the message if you like, then click Commit\n", "body")
            br()
            p("The suggested message is generated from your staged changes, using "
              "a chain of strategies — highest-quality first:")
            ins("    1. CHANGELOG.md bullets (if you've added an entry)\n", "body")
            ins("    2. Diff content — added Python defs/classes, file kinds\n", "body")
            ins("    3. File-name patterns (legacy fallback)\n", "body")
            p("Each result is sanitised (subject ≤ 72 chars, imperative mood, "
              "no filename listings). When AI is enabled in Settings, an "
              "Anthropic / OpenAI / LM Studio / Ollama call runs first — silent "
              "fallback to heuristics on any failure. Click 💡 Suggest at any "
              "time to regenerate.")
            br()
            h2("Undo Last Commit")
            p("Removes the most recent commit but keeps all your changes staged — "
              "nothing is deleted. Safe to use if you committed too early or with "
              "the wrong message.")
            br()
            h2("Branches")
            ins("  🌿 New Branch    — create a branch and optionally switch to it\n", "body")
            ins("  🔀 Switch Branch — pick a branch from the list to check out\n", "body")
            ins("  ⇄ Merge…         — merge another branch INTO the current one\n", "body")
            ins("                     (use after switching to master to pull a finished feature back in)\n", "body")
            ins("  🗑 Delete Branch — safe-delete locally; then offers to also delete from GitHub\n", "body")
            ins("                     (only prompts about GitHub if a remote copy actually exists)\n", "body")
            br()
            warn("⚠  Switching branches with uncommitted changes will fail. "
                 "Commit or undo first.")
            br()
            warn("⚠  Merging with uncommitted changes also fails. Same fix — commit, "
                 "stash, or undo first.")
            br()
            h2("Push & Pull")
            p("Push and Pull are only enabled once a remote (GitHub URL) is set. "
              "Use  Set Remote  or the  🐙 GitHub…  wizard to connect to GitHub first.")
        self._help_show(_fill)

    def _help_github_setup(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("GitHub Setup")
            p("The  🐙 GitHub…  button in the Git tab header opens a step-by-step "
              "wizard for getting your project onto GitHub — even if you've never used "
              "GitHub before.")
            br()
            h2("Step 1 — Git identity")
            p("Every commit is stamped with your name and email. The wizard shows your "
              "current global settings and lets you update them. These are stored in "
              "your global git config and apply to every project on this machine.")
            br()
            h2("Step 2 — Create a GitHub account")
            p("Free at github.com. The wizard has a button to open the sign-up page.")
            br()
            h2("Step 3 — Create a repository")
            p("Go to github.com/new. Give it a name, leave it Public. "
              "Do NOT check 'Add README' or 'Add .gitignore' — you already have those. "
              "Copy the HTTPS URL shown after creation (e.g. "
              "https://github.com/you/my-project.git).")
            br()
            h2("Step 4 — Paste the URL")
            p("Paste the URL into the wizard and click Set. This tells git where to "
              "send your code. The Git tab's Remote label will update immediately.")
            br()
            h2("Step 5 — Push")
            p("Click ⬆ Push to GitHub. The first time, a browser window opens asking "
              "you to log in to GitHub — this is Git Credential Manager doing its job. "
              "Log in once and future pushes happen silently.")
            br()
            warn("⚠  If Push fails with an authentication error, open a terminal in "
                 "the project folder and run:  git push\n"
                 "This triggers the browser login. After that, the Push button works normally.")
            br()
            h2("📦 GitHub Releases")
            p("A Release lets anyone download your .exe without needing Python "
              "installed. To create one:")
            ins("  1. Run build.bat to compile dist\\tokensave-manager.exe\n", "body")
            ins("  2. Open  🐙 GitHub…  and scroll to the Releases section\n", "body")
            ins("  3. Enter a version tag (e.g. v1.0.0) and a title\n", "body")
            ins("  4. Click  📦 Create Release — the .exe files are uploaded automatically\n", "body")
            br()
            p("Releases require the GitHub CLI (gh). If it's not installed, "
              "the wizard shows a link to cli.github.com.")
        self._help_show(_fill)

    def _help_codegraph(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("CodeGraph (alternative code-graph tool)")
            p("CodeGraph is a separate MCP server that does what tokensave does — "
              "builds a per-project code-graph index and exposes it to Claude Code. "
              "The two don't conflict; a project can have both at once.")
            br()
            h2("When to use which")
            ins("  • tokensave — bundled with the manager; full-featured; manual sync\n", "body")
            ins("  • CodeGraph — auto-syncs while its MCP server is running; faster\n", "body")
            ins("                for very large codebases (e.g. 25k-file repos)\n", "body")
            br()
            warn("⚠  About CodeGraph's auto-sync: the file watcher only runs while "
                 "CodeGraph's MCP server is active inside an open Claude Code session. "
                 "If you edit code with Claude Code closed, those edits won't be "
                 "picked up automatically until the next session — at which point "
                 "the watcher catches up. You can also right-click → 🧠 CodeGraph Sync "
                 "to force an incremental update manually.")
            br()
            h2("Install")
            ins("  Settings → CodeGraph → Install via npm  ", "body")
            ins("(requires Node.js 18+)\n", "dim")
            br()
            h2("Use")
            ins("  Right-click any project → 🧠 CodeGraph Init  →  then 🧠 Sync / Status\n", "body")
            ins("  CG column in the Projects tab shows ✓ for initialised projects.\n", "body")
            br()
            h2("Why the manager doesn't run `codegraph install`")
            p("CodeGraph registers itself with Claude Code (and Cursor / Codex / "
              "opencode if you use them) via its own one-time installer: "
              "`npx @colbymchenry/codegraph`. The TokenSave Manager intentionally "
              "stays out of that flow — we handle per-project lifecycle only "
              "(init / sync / status / remove). This means tokensave and CodeGraph "
              "can both write their own sections into your global ~/.claude.json "
              "without fighting each other.")
        self._help_show(_fill)

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(TEMPLATE_DIR, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)

    def refresh(self):
        self.projects = find_projects(SEARCH_ROOTS)
        pinned = get_pinned()
        self.active_path = pinned or (self.projects[0]["path"] if self.projects else None)

        # Delegate tree population to the controller
        self._projects.rebuild_tree(self.projects, self.active_path, pinned)

        if self.active_path:
            name = os.path.basename(self.active_path)
            tag  = "pinned" if pinned else "auto"
            self.active_badge.config(text=f"  ★ {name}  ({tag})  ")
        else:
            self.active_badge.config(text="  No project  ")

        # Keep Git tab in sync when it's visible and a project is tracked
        if self._git.is_visible() and self._git.has_path():
            self._git.refresh()

        # Kick off background refresh of the Git status column via controller
        self._projects.refresh_git_status_column(self.projects)

    def _check_config(self):
        problems = []
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            problems.append("tokensave.exe path is missing or invalid")
        if not TEMPLATE_DIR or not os.path.isdir(TEMPLATE_DIR):
            problems.append("Template directory is missing or invalid")

        # MCP-config drift detection — opens the configurator instead of
        # Settings when there are no other problems, since that's the most
        # actionable thing the user can do.
        skips = (_cfg.get("mcp_skip_warnings") or []) \
                if isinstance(_cfg, dict) else []
        mcp_drift = []
        for label, path in _MCP_CONFIGS:
            if path in skips:
                continue
            try:
                info = _classify_mcp_entry(path, _cfg)
            except Exception:
                # Defensive — never crash startup just because we can't read
                # a Claude config file. The dialog can surface details.
                continue
            if info["state"] != "ok":
                mcp_drift.append((label, info))

        if not problems and not mcp_drift:
            return

        if problems:
            # Existing path: paths broken, open Settings as before.
            note = "Please set the correct paths before using the manager."
            self._log("Config problem: " + " | ".join(problems), C["red"])
            SettingsDialog(
                self, _state, _save_config, self._on_settings_saved,
                startup_note=(note + "\n\n"
                              + "\n".join(f"• {p}" for p in problems)))
            return

        # Pure MCP drift — log it, open the configurator dialog directly.
        # Don't auto-pop in a modal way; the user just launched the manager
        # and wants to see the project list. A log line + a non-modal dialog
        # gives them the choice.
        for label, info in mcp_drift:
            self._log(
                f"MCP: {label} {info['label']} ({info['cfg_path']}). "
                f"Open Settings → MCP integration to fix.",
                C["peach"] if info["state"] in
                ("direct_serve", "wrong_wrapper") else C["red"])

        # Open the configurator after a short delay so the main window has
        # finished laying out — feels less like an interruption.
        self.after(800, lambda: MCPConfigDialog(self, _state))

    def _auto_refresh(self):
        ctrl_idle = (not hasattr(self, "_projects") or self._projects.current_proc is None)
        if self._current_proc is None and ctrl_idle:
            self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _log(self, msg, colour=None):
        def _do():
            self.log.configure(state=tk.NORMAL)
            tag = f"col_{colour}"
            self.log.tag_configure(tag, foreground=colour or C["green"])
            self.log.insert(tk.END, msg + "\n", tag)
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _set_running(self, running, label=""):
        if running:
            self._stop_btn.configure(state=tk.NORMAL)
            self._running_label.configure(text=f"⏳ running: {label}")
        else:
            self._stop_btn.configure(state=tk.DISABLED)
            self._running_label.configure(text="")

    def _stop_current(self):
        self._stop_requested = True
        proc = self._current_proc
        if proc and proc.poll() is None:
            proc.kill()
            self._log("  ■ Stopped by user.", C["red"])
        # Also stop any controller-managed subprocess (e.g. cmd_sync_all, scaffold)
        if hasattr(self, "_projects"):
            self._projects.stop()

    def _open_log(self):
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("No log yet",
                "No log file exists yet — run an operation first.", parent=self)

    def _run(self, args, cwd, label):
        def worker():
            cmd_str = "tokensave " + " ".join(args)
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            log.debug(f"     cwd={cwd}")
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE] + args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                log.debug(f"     pid={proc.pid}")
                # Suppress tokensave's misleading "legacy .codegraph/" warning
                # when CodeGraph is actually an active alternate index in this
                # project — manager treats tokensave and CodeGraph as equal
                # citizens; following tokensave's "safely deleted" advice would
                # wipe the user's CodeGraph index. The warning is only correct
                # for genuinely orphaned .codegraph/ folders (no codegraph.db).
                codegraph_active = os.path.isfile(
                    os.path.join(cwd, ".codegraph", "codegraph.db"))
                _suppressed_codegraph_warning = False
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    if (codegraph_active
                            and "legacy .codegraph" in stripped
                            and "safely deleted" in stripped):
                        # Drop silently, but log once that we did so. The
                        # info-bar message lets the user know we filtered
                        # — never silent fakery without trace.
                        if not _suppressed_codegraph_warning:
                            self._log(
                                "  (suppressed tokensave's '.codegraph/ legacy' "
                                "warning — CodeGraph is active in this project)",
                                C["overlay0"])
                            _suppressed_codegraph_warning = True
                        log.debug(f"  SUPPRESSED {stripped}")
                        continue
                    # Detect tokensave's "Update available: v→v" line and
                    # remember the upgrade target. Settings shows an
                    # "Upgrade tokensave" button when this is set; the
                    # button just runs `tokensave upgrade` via _run.
                    m = _TOKENSAVE_UPDATE_RE.search(stripped)
                    if m:
                        cur_v, new_v = m.group(1), m.group(2)
                        self._tokensave_current_version = cur_v
                        self._tokensave_available_version = new_v
                        self._log(stripped, C["yellow"])
                        self._log(
                            f"  → tokensave {cur_v} → {new_v} ready to "
                            f"install.  Settings → 'Upgrade tokensave to "
                            f"v{new_v}' to apply, or run "
                            f"'tokensave upgrade' from a shell.",
                            C["peach"])
                        log.info(f"UPDATE-AVAILABLE  {cur_v} -> {new_v}")
                        continue
                    self._log(stripped)
                    log.debug(f"  OUT {stripped}")
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                    if (args and args[0] == "sync"
                            and _cfg.get("auto_commit_after_sync")
                            and _is_git_repo(cwd, GIT_EXE)):
                        self._auto_commit_after_sync(cwd)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)
        threading.Thread(target=worker, daemon=True).start()

    def _auto_commit_after_sync(self, cwd: str) -> None:
        """Commit staged changes after a successful sync, using LLM or amend-stacking."""
        self._log("  Auto-committing sync changes…", C["peach"])
        self._shell_capture([GIT_EXE, "-C", cwd, "add", "-A"], cwd)
        _, staged_rc = self._shell_capture(
            [GIT_EXE, "-C", cwd, "diff", "--cached", "--quiet"], cwd)
        if staged_rc == 0:
            return  # nothing staged

        llm_cfg = _cfg.get("commit_message_llm") or {}
        use_llm = bool(llm_cfg.get("enabled") and llm_cfg.get("use_for_sync_autocommit"))

        if use_llm:
            # LLM mode: each sync gets a unique message; no amend-stacking.
            self._log("  Composing AI commit message…", C["peach"])
            status_out, _ = self._shell_capture(
                [GIT_EXE, "-C", cwd, "status", "--short"], cwd)
            ai_msg = _suggest_commit_message(cwd, status_out, _cfg, GIT_EXE) or "chore: tokensave sync"
            commit_cmd = [GIT_EXE, "-C", cwd, "commit", "-m", ai_msg.split("\n", 1)[0]]
            if "\n\n" in ai_msg:
                commit_cmd.extend(["-m", ai_msg.split("\n\n", 1)[1]])
        else:
            # Default amend-stacking: repeated syncs collapse into one commit.
            last_out, _ = self._shell_capture(
                [GIT_EXE, "-C", cwd, "log", "-1", "--format=%s"], cwd)
            if last_out.strip() == "chore: tokensave sync":
                commit_cmd = [GIT_EXE, "-C", cwd, "commit", "--amend", "--no-edit"]
                self._log("  Amending previous sync commit…", C["peach"])
            else:
                commit_cmd = [GIT_EXE, "-C", cwd, "commit", "-m", "chore: tokensave sync"]

        cout, crc = self._shell_capture(commit_cmd, cwd)
        col = C["green"] if crc == 0 else C["red"]
        for line in cout.strip().splitlines()[-3:]:
            self._log(f"  {line}", col)

    def _run_capture(self, args, cwd, label) -> tuple:
        """Run a tokensave command and return (raw_output, returncode, elapsed_s).

        Synchronous — must be called from a background thread.
        Handles _current_proc tracking and _set_running for the duration.
        The caller is responsible for logging and scheduling UI updates.
        """
        cmd_str = "tokensave " + " ".join(args)
        self._log(f"$ {cmd_str}  [{label}]", C["blue"])
        self.after(0, self._set_running, True, label)
        log.info(f"RUN  {cmd_str}")
        log.debug(f"     cwd={cwd}")
        t0 = time.monotonic()
        try:
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            proc = subprocess.Popen(
                [TOKENSAVE] + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self._current_proc = proc
            log.debug(f"     pid={proc.pid}")
            raw = proc.stdout.read()
            proc.wait()
            elapsed = time.monotonic() - t0
            log.info(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
            log.debug(f"  OUT {raw[:500]}")
            return raw, proc.returncode, elapsed
        finally:
            self._current_proc = None
            self.after(0, self._set_running, False)

    def _shell_capture(self, cmd: list, cwd: str, env=None) -> tuple:
        """Run any shell command and return (stdout+stderr, returncode).

        Generic helper — cmd[0] is the executable (not tokensave-specific).
        Synchronous — must be called from a background thread.
        Returns ("Error: '<exe>' not found on system PATH.", 1) if the
        executable is missing so callers always get a displayable string.

        Pass env= to override the process environment (e.g. set
        GIT_TERMINAL_PROMPT=0 for network git operations so they fail
        immediately instead of hanging waiting for stdin auth prompts).
        """
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                env=env,
            )
            out = proc.stdout.read()
            proc.wait()
            return out, proc.returncode
        except FileNotFoundError:
            return (f"Error: '{cmd[0]}' not found on system PATH.", 1)

    def _probe_tokensave_version(self):
        """Best-effort read of the installed tokensave version.

        Runs `tokensave --version` once at App startup (in a background
        thread to avoid blocking the GUI). Output looks like
        "tokensave 5.1.1" — we extract the version string and cache it
        on the instance. Failures (binary missing, weird output) leave
        the cache as None, which the Settings UI handles gracefully.
        """
        def _worker():
            if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
                return
            try:
                r = subprocess.run(
                    [TOKENSAVE, "--version"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                    encoding="utf-8", errors="replace")
            except (OSError, subprocess.TimeoutExpired):
                return
            out = (r.stdout or "").strip()
            m = re.search(r'(\d+\.\d+\.\d+(?:\.\d+)?)', out)
            if m:
                self._tokensave_current_version = m.group(1)
                log.debug(f"tokensave installed version: "
                          f"{self._tokensave_current_version}")
                # Kick off a single update check right after we know the
                # local version. Subsequent checks fire from the hourly
                # poller below.
                self._check_tokensave_updates()
        threading.Thread(target=_worker, daemon=True,
                         name="tokensave-version-probe").start()
        # Hourly background poller. Cheap (one GitHub API call, no auth);
        # safe to run forever as a daemon thread.
        threading.Thread(target=self._tokensave_update_poll_loop,
                         daemon=True,
                         name="tokensave-update-poll").start()

    # GitHub releases API endpoint for tokensave. Hardcoded since the
    # tokensave repo URL is referenced in README.md and is unlikely to
    # change. If it does, this is a one-line update.
    _TOKENSAVE_RELEASES_API = (
        "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest")

    # Hourly poll cadence — GitHub allows 60 unauthenticated requests/hour
    # per IP, so once an hour is comfortably within the limit and keeps
    # the update notification fresh enough that users won't miss a release
    # for long. Tunable via _cfg["tokensave_update_poll_hours"] if a user
    # wants to be more or less aggressive.
    def _tokensave_update_poll_interval(self) -> float:
        hours = float(_cfg.get("tokensave_update_poll_hours", 1.0))
        return max(0.25, hours) * 3600.0  # never poll more than 4x/hour

    def _tokensave_update_poll_loop(self):
        """Daemon: re-check GitHub for new tokensave releases periodically.

        Doesn't trigger any UI prompts — just refreshes the cached
        `_tokensave_available_version` so the Settings dialog reflects
        the current state next time it's opened. The OUTPUT-pane hint
        line is only logged on FRESH discovery (transition from "no
        update known" → "update available"), not on every poll, to avoid
        spamming the log.
        """
        while True:
            time.sleep(self._tokensave_update_poll_interval())
            self._check_tokensave_updates()

    def _check_tokensave_updates(self):
        """Single-shot check against the tokensave releases API.

        Compares against `_tokensave_current_version` (set by the local
        --version probe). When a strictly-newer version is found AND it
        wasn't known before, logs a peach hint to the OUTPUT pane so
        users see the update offer without opening Settings.
        """
        import urllib.request, urllib.error, json as _json
        if not self._tokensave_current_version:
            return  # nothing to compare against yet
        try:
            req = urllib.request.Request(
                self._TOKENSAVE_RELEASES_API,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "tokensave-manager"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, _json.JSONDecodeError, OSError) as e:
            # Common when offline or rate-limited. Silent — try again
            # next interval.
            log.debug(f"tokensave update check failed: {type(e).__name__}: {e}")
            return
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        m = re.match(r'(\d+\.\d+\.\d+(?:\.\d+)?)', tag)
        if not m:
            return
        latest = m.group(1)
        cur = self._tokensave_current_version
        if not _version_lt(cur, latest):
            return  # current is up-to-date or ahead (unlikely but possible)
        prev_known = self._tokensave_available_version
        self._tokensave_available_version = latest
        if prev_known != latest:
            # Fresh discovery — surface it. (Skip the log line if the
            # poller is just re-confirming a version we already knew
            # about.)
            self._log(
                f"  → tokensave {cur} → {latest} ready to install.  "
                f"Settings → 'Upgrade tokensave to v{latest}' to apply, "
                f"or run 'tokensave upgrade' from a shell.",
                C["peach"])
            log.info(f"UPDATE-AVAILABLE  {cur} -> {latest}  (via GitHub API)")

    def cmd_upgrade_tokensave(self):
        """Run `tokensave upgrade` from the manager.

        Streams output to the OUTPUT pane via the existing _run path. cwd
        doesn't matter for upgrade (it operates on the installed binary,
        not any specific project) so we use the tokensave_exe's directory
        as a stable choice. On success, the upgrade replaces the binary
        on disk; future sync / MCP-server spawns pick up the new version
        automatically, but the currently-running MCP wrappers continue
        serving from the old binary until Claude is restarted.

        Clears the cached `_tokensave_available_version` on success so
        the Settings button auto-hides until the next sync re-reports an
        update.
        """
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            messagebox.showwarning(
                "tokensave not found",
                "Set the tokensave.exe path in Settings first.",
                parent=self)
            return
        # Confirm — upgrades replace a binary and we want the user fully
        # aware. Skip the prompt if no version metadata is known (still
        # useful — runs the upgrade command which itself shows what'll
        # happen).
        target = self._tokensave_available_version
        cur = self._tokensave_current_version
        if target:
            msg = (f"Upgrade tokensave from v{cur or '?'} to v{target}?\n\n")
        elif cur:
            msg = (f"Run `tokensave upgrade`?  (Currently installed: "
                   f"v{cur}.)\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        else:
            msg = ("Run `tokensave upgrade`?\n\n"
                   "tokensave will check GitHub for a newer release and "
                   "apply it.  No-op if you're already on the latest.\n\n")
        msg += (
            "This replaces the tokensave binary on disk.  Currently-running\n"
            "MCP wrappers continue serving from the old binary until you\n"
            "restart Claude Desktop / Claude Code.")
        if not messagebox.askyesno("Upgrade tokensave", msg, parent=self):
            return
        # Clear cache so the Settings button hides until the next sync
        # reports a fresh update.  If the upgrade fails, the next sync
        # will re-populate it anyway.
        self._tokensave_available_version = None
        # cwd is the tokensave.exe folder — works for any upgrade flow.
        self._run(["upgrade"], cwd=os.path.dirname(TOKENSAVE),
                  label="upgrade")

    # ── Commit dialog (shared by Projects context menu, Git tab, and
    #    offer-commit-after-change flow) ──────────────────────────────────────

    def _open_commit_dialog(self, path: str):
        """Open GitCommitDialog for a given project path. Reused by
        `cmd_git_commit` (Projects-tab right-click) AND by the
        offer-commit-after-change flow that runs after Ensure .gitignore,
        Shadow Links, Scaffold, and Retrofit.

        Pre-flight check: if the project has tracked-but-ignored files,
        offer to run Untrack Ignored Files FIRST. Otherwise the commit
        attempt would inevitably hit git's "paths are ignored" error and
        the user would have to come back and untrack anyway.
        """
        if _is_local_git_repo(path):
            stale = _find_tracked_but_ignored(path, GIT_EXE)
            if stale:
                n = len(stale)
                preview = "\n".join(f"  • {f}" for f in stale[:5])
                if n > 5:
                    preview += f"\n  • …and {n - 5} more"
                choice = messagebox.askyesnocancel(
                    "Tracked-but-ignored files detected",
                    f"{n} file{'s' if n != 1 else ''} in this project "
                    f"{'are' if n != 1 else 'is'} tracked by git BUT also "
                    "match a .gitignore rule. Committing in this state "
                    "usually surfaces git's 'paths are ignored' error and "
                    "blocks the commit.\n\n"
                    f"Affected:\n{preview}\n\n"
                    "Yes  → run 🧹 Untrack Ignored Files first (recommended)\n"
                    "No   → open the commit dialog anyway\n"
                    "Cancel → close, do nothing",
                    parent=self)
                if choice is None:   # Cancel
                    return
                if choice:           # Yes — untrack first, that flow then
                                     # offers a fresh commit prompt of its own
                    UntrackIgnoredDialog(self, path, stale,
                        reason="tracked but listed in .gitignore "
                               "(blocks commit until untracked)")
                    return
        # No conflicts, OR user chose to proceed anyway
        status_out, _ = self._shell_capture(
            [GIT_EXE, "-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path, GIT_EXE)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit, _state)

    def _offer_commit_after_change(self, path: str, summary_label: str) -> None:
        """After a manager action (Ensure .gitignore, Scaffold, Retrofit, etc.),
        check whether the working tree is dirty and offer a commit dialog if so.

        Called directly by dialogs that hold a reference to App (e.g.
        GitignoreDialog via self._app._offer_commit_after_change). The
        ProjectsTabController has its own copy for internal flows; this one
        serves external callers that go through App.
        """
        if not _is_local_git_repo(path):
            return
        status_out, _ = self._shell_capture(
            [GIT_EXE, "-C", path, "status", "--porcelain"], path)
        if not status_out.strip():
            self._log("  Working tree clean — nothing to commit.", C["overlay0"])
            return
        name = os.path.basename(path)
        if messagebox.askyesno(
                "Commit this change?",
                f"Manager updated {summary_label} in {name}.\n\n"
                "Commit this change now?\n\n"
                "Click 'Yes' to open the Commit dialog with the changed files "
                "ready to stage. Click 'No' to leave the working tree dirty.",
                parent=self):
            self._open_commit_dialog(path)
        else:
            self._log("  Working tree left dirty — commit when you're ready.",
                      C["yellow"])

    def _do_git_commit(self, path: str, message: str, selected: list):
        """Stage and commit the picked files. `selected` is a list of
        (filename, xy) tuples from the GitCommitDialog.
        """
        if not selected:
            return
        # Backward-compat: callers passing legacy list-of-strings still
        # work; treat unknown XY as needs-add.
        if selected and isinstance(selected[0], str):
            selected = [(fname, "??") for fname in selected]

        name = os.path.basename(path)
        all_paths = [fname for fname, _xy in selected]
        # xy[1] == ' ' means "no working-tree change" — file is fully
        # captured in the index already (staged D, A, M, R, etc.).
        files_to_add = [fname for fname, xy in selected
                        if len(xy) >= 2 and xy[1] != ' ']

        self._git._git_begin_op()

        def worker():
            try:
                if files_to_add:
                    out, rc = self._shell_capture(
                        [GIT_EXE, "-C", path, "add", "--"] + files_to_add, path)
                    if rc != 0:
                        if "ignored by one of your .gitignore files" in out:
                            offending = [
                                ln.strip() for ln in out.splitlines()
                                if ln.strip() and not ln.strip().startswith(
                                    ("hint:", "The following", "warning:"))]
                            self.after(0, lambda: messagebox.showwarning(
                                "Tracked-but-ignored files",
                                "Some of the files you selected are already "
                                "tracked by git AND match a .gitignore rule. "
                                "Git refuses to re-add them in this state.\n\n"
                                f"Affected paths:\n  "
                                + "\n  ".join(offending[:10])
                                + ("\n  …" if len(offending) > 10 else "")
                                + "\n\nFix: right-click → "
                                "🧹 Untrack Ignored Files… → untrack those "
                                "paths first. Then commit the result.",
                                parent=self))
                        else:
                            self.after(0, lambda: self._log(
                                f"git add failed: {out.strip()}", C["red"]))
                        return

                self._log(f"[{name}] Committing ({len(all_paths)} file"
                          f"{'s' if len(all_paths) != 1 else ''})…",
                          C["peach"])
                commit_cmd = ([GIT_EXE, "-C", path, "commit", "-m", message,
                               "--"] + all_paths)
                cout, crc = self._shell_capture(commit_cmd, path)
                col = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", col))
                self.after(0, self.refresh)
            finally:
                self.after(0, self._git._git_end_op)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_settings(self):
        SettingsDialog(self, _state, _save_config, self._on_settings_saved)

    def _on_settings_saved(self):
        global TOKENSAVE, TEMPLATE_DIR, SEARCH_ROOTS, GIT_EXE, CODEGRAPH_EXE
        global BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE
        # ManagerConfig.raw is the same dict object as _cfg, so SettingsDialog's
        # mutations are already visible — we just need to recompute the cached
        # derived fields (git_exe, codegraph_exe) and re-bind the legacy globals
        # so any caller still reading them gets the new values. Phases B–E
        # migrate readers to `self._cfg.X` and the global rebind goes away.
        _state.refresh_derived()
        TOKENSAVE     = _state.tokensave_exe
        TEMPLATE_DIR  = _state.template_dir
        SEARCH_ROOTS  = _state.search_roots
        GIT_EXE       = _state.git_exe
        CODEGRAPH_EXE = _state.codegraph_exe
        BASIC_INSTRUCTIONS_TEMPLATE = _state.basic_instructions_template
        BASELINE_INCLUDE_LINE       = _state.baseline_include_line
        self.refresh()
        self._log("Settings saved and applied.", C["green"])

# ── Retrofit dialog ────────────────────────────────────────────────────────────

# RetrofitDialog moved to src/dialogs/retrofit.py (Round 4 Phase B)



# ── Scaffold dialog ────────────────────────────────────────────────────────────

# ScaffoldDialog moved to src/dialogs/scaffold.py (Round 4 Phase B)



# ── Settings dialog ────────────────────────────────────────────────────────────

# SettingsDialog+_probe_loaded_model moved to src/dialogs/settings.py (Round 4 Phase C.3)




# ── Snippet edit dialog ────────────────────────────────────────────────────────

# SnippetEditDialog moved to src/dialogs/snippet_edit.py (Round 4 Phase B)



# ── Shadow Links dialog ────────────────────────────────────────────────────────

# ShadowLinksDialog moved to src/dialogs/shadow_links.py (Round 4 Phase B)



# ── Git Commit dialog ──────────────────────────────────────────────────────────

# ── Git helper dialogs ────────────────────────────────────────────────────────

# SetRemoteDialog moved to src/dialogs/set_remote.py (Round 4 Phase B)



# MergePRDialog moved to src/dialogs/merge_pr.py (Round 4 Phase B)



# NewBranchDialog moved to src/dialogs/new_branch.py (Round 4 Phase B)



# SwitchBranchDialog moved to src/dialogs/switch_branch.py (Round 4 Phase B)



# ── Assign Category dialog ────────────────────────────────────────────────────

# AssignCategoryDialog moved to src/dialogs/assign_category.py (Round 4 Phase B)



# GitHubSetupDialog moved to src/dialogs/github_setup.py (Round 4 Phase C.1 — cfg: ManagerConfig param added)



# ReleaseWizard+_ReleaseCtx moved to src/dialogs/release_wizard.py (Round 4 Phase C.2)




# UntrackIgnoredDialog moved to src/dialogs/untrack_ignored.py (Round 4 Phase B)
# GitignoreDialog moved to src/dialogs/gitignore.py (Round 4 Phase C.1 — cfg: ManagerConfig param added)
# _iter_json_lines moved to helpers/llm.py (Round 4 Phase A)

# OllamaModelManagerDialog moved to src/dialogs/ollama_model_mgr.py (Round 4 Phase C.2)



# _is_claude_running moved to helpers/mcp.py (Round 4 Phase A).




# MCPConfigDialog moved to src/dialogs/mcp_config.py (Round 4 Phase C.3)



# GitCommitDialog moved to src/dialogs/git_commit.py (Round 4 Phase C.3)



# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()
