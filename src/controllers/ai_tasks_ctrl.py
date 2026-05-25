"""AITasksController — long-running AI project tasks for the Projects tab.

Owns the orchestration of AI-backed project commands:
  - cmd_draft_changelog(path)   Stage 3: draft [Unreleased] CHANGELOG bullets
  - cmd_refactor_scout(path)    Stage 4: surface code-health findings

This controller orchestrates tasks but does NOT own reusable infrastructure.
Shared primitives (LLM calling, ProposalBridge lifecycle, error formatting,
the scout's DB queries) stay in helpers/. Any method here that could be
useful to a different AI feature should be extracted to helpers/ instead.

Per-task, per-project locking:
  _running: set[tuple[str, str]] of (project_path, task_name). The right-click
  menu builder checks this set before rendering each entry. Project-wide lock
  is intentionally NOT used — unrelated tasks on other projects stay available.

Shutdown safety:
  cancel_all_proposals() is called by App._quit_app via the same pattern as
  AskTabController. Every ProposalBridge created here is registered in
  _active_bridges and released on shutdown.

Dependency contract:
  tab            — the Projects tk.Frame (after() + winfo_toplevel())
  cfg            — ManagerConfig (read at execution time, not snapshotted)
  on_log         — thread-safe log callback (msg: str, colour: str = "")
  on_commit_offer — (path: str, label: str) -> None
  on_seed_ask    — optional (text, path) -> None; routes scout's Investigate
                   action to the Ask tab. None disables Investigate.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Callable

from helpers.runtime import log

if TYPE_CHECKING:
    import tkinter as tk
    from state import ManagerConfig

# Task name constants (used as the second element of (path, task_name) lock keys)
TASK_DRAFT_CHANGELOG = "draft_changelog"
TASK_REFACTOR_SCOUT  = "refactor_scout"
TASK_RUN_CHECKS      = "run_checks"


def _build_changelog_prompt(classified: dict, existing: str) -> "tuple[str, str]":
    """Build (system_prompt, user_prompt) for the CHANGELOG drafter LLM call."""
    commit_lines = "\n".join(
        f"- [{section}] {msg}"
        for section, msgs in classified.items()
        for msg in msgs
    )
    existing_note = (
        f"\n\nThe [Unreleased] block already contains these notes "
        f"— integrate them VERBATIM into your output. "
        f"Do not drop any user-written content:\n\n{existing}"
        if existing else ""
    )
    system_prompt = (
        "You are a technical writer generating CHANGELOG.md content. "
        "Output ONLY valid markdown bullet points grouped under "
        "### Added, ### Changed, ### Fixed, ### Removed headings as appropriate. "
        "Rules:\n"
        "- Collapse trivial internal commits (typo, fmt, revert, chore) — omit them entirely.\n"
        "- Infer user-facing impact from commit messages; rewrite jargon into plain English.\n"
        "- De-duplicate near-identical points.\n"
        "- Preserve existing formatting/layout (bullet style, line wrapping) unless strictly necessary.\n"
        "- Do NOT include a ## [version] or ## [Unreleased] header — only the body content.\n"
        "- Do NOT add commentary, preamble, or explanation outside the bullet list."
    )
    user_prompt = (
        f"Here are the commits since the last release, pre-classified by type:\n\n"
        f"{commit_lines}"
        f"{existing_note}\n\n"
        "Draft the [Unreleased] CHANGELOG body."
    )
    return system_prompt, user_prompt


class AITasksController:
    """Orchestrates long-running AI project tasks for the Projects tab."""

    def __init__(
        self,
        tab: "tk.Frame",
        cfg: "ManagerConfig",
        on_log: Callable,
        on_commit_offer: Callable[[str, str], None],
        on_seed_ask: Callable[[str, str], None] | None = None,
    ) -> None:
        self._tab = tab
        self._cfg = cfg
        self._on_log = on_log
        self._on_commit_offer = on_commit_offer
        self._on_seed_ask = on_seed_ask

        # Per-task, per-project lock: set of (path, task_name) pairs.
        # Right-click menu builder checks before rendering each AI menu item.
        self._running: set[tuple[str, str]] = set()
        self._running_lock = threading.Lock()

        # Active ProposalBridges — iterated by cancel_all_proposals on shutdown.
        self._active_bridges: set = set()
        self._bridges_lock = threading.Lock()

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    # ── Public query ─────────────────────────────────────────────────────────

    def is_running(self, path: str, task_name: str) -> bool:
        """Return True if this task is currently running for this project."""
        with self._running_lock:
            return (path, task_name) in self._running

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def cancel_all_proposals(self) -> None:
        """Called by App._quit_app. Releases all worker threads waiting on
        open proposals so the app can shut down cleanly."""
        with self._bridges_lock:
            bridges = list(self._active_bridges)
        for b in bridges:
            try:
                b.cancel()
            except Exception:
                log.exception("AITasksController: ProposalBridge.cancel raised on shutdown")

    # ── Stage 3: CHANGELOG drafter ───────────────────────────────────────────

    def cmd_draft_changelog(self, path: str) -> None:
        """Right-click → 📝 Draft CHANGELOG entry…

        Reads commits since the last release tag, feeds them to the LLM with
        any existing [Unreleased] content, and proposes updated bullets via
        ProposalDialog. On accept, update_unreleased() writes the result.

        Per-task lock: if already running for this path, shows a warning and
        returns immediately.
        """
        with self._running_lock:
            if (path, TASK_DRAFT_CHANGELOG) in self._running:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Already running",
                    "A CHANGELOG draft is already in progress for this project.",
                    parent=self._root,
                )
                return
            self._running.add((path, TASK_DRAFT_CHANGELOG))

        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        llm_cfg = raw.get("commit_message_llm") or {}
        if not llm_cfg.get("enabled"):
            from tkinter import messagebox
            messagebox.showwarning(
                "AI disabled",
                "AI is disabled. Enable it in Settings → AI commit messages first.",
                parent=self._root,
            )
            with self._running_lock:
                self._running.discard((path, TASK_DRAFT_CHANGELOG))
            return

        self._on_log(f"[CHANGELOG] Drafting entry for {os.path.basename(path)}…")

        stop_event = threading.Event()
        t = threading.Thread(
            target=self._draft_changelog_worker,
            args=(path, llm_cfg, stop_event),
            daemon=True,
            name=f"draft-changelog:{os.path.basename(path)}",
        )
        t.start()

    def _draft_changelog_worker(
        self, path: str, llm_cfg: dict, stop_event: threading.Event
    ) -> None:
        """Background worker: gather commits, call LLM, propose via ProposalDialog."""
        try:
            self._do_draft_changelog(path, llm_cfg, stop_event)
        except Exception as e:
            log.exception("AITasksController: draft_changelog worker crashed")
            self._tab.after(
                0, self._on_log,
                f"[CHANGELOG] Error: {type(e).__name__}: {e}", "red",
            )
        finally:
            with self._running_lock:
                self._running.discard((path, TASK_DRAFT_CHANGELOG))

    def _open_changelog_proposal(
        self, changelog_path: str, drafted: str, existing: str,
        WriteProposal, ProposalBridge,
    ) -> "tuple[bool, str]":
        """Show a ProposalDialog for the drafted CHANGELOG content.

        Returns (accepted, final_content). Registers the bridge in
        _active_bridges so App._quit_app can cancel cleanly on shutdown.
        """
        old_content = existing if existing else "(empty)"
        rationale = (
            "Draft [Unreleased] CHANGELOG bullets generated from commits since "
            "last release tag."
            + (" Existing notes were preserved — review the diff to confirm."
               if existing else "")
        )
        proposal = WriteProposal(
            filepath=changelog_path,
            original_content=old_content,
            proposed_content=drafted,
            rationale=rationale,
        )
        bridge = ProposalBridge(self._root, proposal, timeout_s=300.0)
        with self._bridges_lock:
            self._active_bridges.add(bridge)
        try:
            return bridge.invoke()
        finally:
            with self._bridges_lock:
                self._active_bridges.discard(bridge)

    def _gather_changelog_data(
        self, path: str, changelog_path: str, stop_event: threading.Event
    ) -> "tuple[dict, str] | None":
        """Stages 1–3: fetch commits, classify, read existing unreleased block.

        Returns (classified, existing) or None on error / nothing-to-draft / cancel.
        """
        from helpers.release import (
            _commits_since, _classify_commits_for_changelog, _last_release_tag,
        )
        from helpers.changelog_patch import read_unreleased

        try:
            last_tag = _last_release_tag(path)
            commits  = _commits_since(path, last_tag)
        except Exception as e:
            self._tab.after(0, self._on_log,
                            f"[CHANGELOG] Could not read git history: {e}", "red")
            return None

        if not commits:
            self._tab.after(0, self._on_log,
                            "[CHANGELOG] No commits found since last tag — nothing to draft.")
            return None

        if stop_event.is_set():
            return None

        classified = _classify_commits_for_changelog(commits)
        existing   = read_unreleased(changelog_path) if os.path.exists(changelog_path) else ""
        return classified, existing

    def _call_changelog_llm(
        self, classified: dict, existing: str, llm_cfg: dict, stop_event: threading.Event
    ) -> "str | None":
        """Stages 4–5: build prompt, call LLM, return drafted text.

        Returns stripped draft string or None on error / empty result / cancel.
        """
        from helpers.llm import _call_llm

        system_prompt, user_prompt = _build_changelog_prompt(classified, existing)

        if stop_event.is_set():
            return None

        self._tab.after(0, self._on_log, "[CHANGELOG] Calling LLM…")

        try:
            drafted = _call_llm(llm_cfg, system_prompt, user_prompt)
        except Exception as e:
            self._tab.after(0, self._on_log,
                            f"[CHANGELOG] LLM call failed: {e}", "red")
            return None

        if not drafted or not drafted.strip():
            self._tab.after(0, self._on_log,
                            "[CHANGELOG] LLM returned empty result.", "red")
            return None

        return drafted.strip()

    def _do_draft_changelog(
        self, path: str, llm_cfg: dict, stop_event: threading.Event
    ) -> None:
        from helpers.changelog_patch import update_unreleased
        from dialogs.proposal import ProposalBridge, WriteProposal

        changelog_path = os.path.join(path, "CHANGELOG.md")

        result = self._gather_changelog_data(path, changelog_path, stop_event)
        if result is None:
            return
        classified, existing = result

        drafted = self._call_changelog_llm(classified, existing, llm_cfg, stop_event)
        if drafted is None:
            return

        if stop_event.is_set():
            return

        accepted, final_content = self._open_changelog_proposal(
            changelog_path, drafted, existing, WriteProposal, ProposalBridge
        )

        if not accepted or not final_content:
            self._tab.after(0, self._on_log, "[CHANGELOG] Draft rejected.")
            return

        ok, msg = update_unreleased(changelog_path, final_content)
        if ok:
            self._tab.after(0, self._on_log,
                            f"[CHANGELOG] {msg} — CHANGELOG.md updated.", "green")
            self._tab.after(0, self._on_commit_offer,
                            path, "CHANGELOG.md (unreleased draft)")
        else:
            self._tab.after(0, self._on_log,
                            f"[CHANGELOG] Write failed: {msg}", "red")

    # ── Stage 4: Refactor scout ──────────────────────────────────────────────

    def cmd_refactor_scout(self, path: str) -> None:
        """Right-click → 🔬 Refactor scout…

        Runs deterministic SQL queries against the project's tokensave DB
        to surface complexity / god-class / god-file / dead-code findings
        and opens RefactorScoutDialog. No LLM call happens at scout time —
        the LLM is only invoked if the user clicks Investigate on a card.

        Guarded: tokensave index must exist (`.tokensave/tokensave.db`).
        Per-task lock prevents two scouts overlapping on the same project.
        """
        with self._running_lock:
            if (path, TASK_REFACTOR_SCOUT) in self._running:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Already running",
                    "A refactor scout is already in progress for this project.",
                    parent=self._root,
                )
                return
            self._running.add((path, TASK_REFACTOR_SCOUT))

        self._on_log(f"[scout] Running refactor scout on {os.path.basename(path)}…")

        t = threading.Thread(
            target=self._refactor_scout_worker,
            args=(path,),
            daemon=True,
            name=f"refactor-scout:{os.path.basename(path)}",
        )
        t.start()

    def _refactor_scout_worker(self, path: str) -> None:
        """Background worker: run scout, marshal to UI thread."""
        try:
            from helpers.refactor_scout import run_scout
            ignored = set(self._cfg.raw.get("refactor_scout_ignored") or [])
            findings, suppressed = run_scout(path, ignored)
            self._tab.after(
                0, self._open_refactor_scout_dialog,
                path, findings, suppressed, ignored,
            )
        except FileNotFoundError as e:
            self._tab.after(
                0, self._on_log,
                f"[scout] {e}", "red",
            )
        except Exception as e:
            log.exception("AITasksController: refactor scout crashed")
            self._tab.after(
                0, self._on_log,
                f"[scout] Error: {type(e).__name__}: {e}", "red",
            )
        finally:
            with self._running_lock:
                self._running.discard((path, TASK_REFACTOR_SCOUT))

    def _open_refactor_scout_dialog(self, path: str, findings, suppressed: int,
                                      ignored: set) -> None:
        """Main-thread: construct the dialog with persistence + investigate hooks."""
        from dialogs.refactor_scout import RefactorScoutDialog

        def _save_ignored(updated: set[str]) -> None:
            self._cfg.raw["refactor_scout_ignored"] = sorted(updated)
            self._cfg.save()

        def _investigate(finding) -> None:
            if self._on_seed_ask is None:
                return
            from helpers.refactor_scout import format_investigate_context
            text = format_investigate_context(finding)
            self._on_seed_ask(text, path)

        def _investigate_cli(finding) -> None:
            self._launch_scout_briefing_in_cli(path, finding)

        def _export_all_cli() -> None:
            self._launch_scout_report_in_cli(path, findings, suppressed)

        cli_available = bool(self._cfg.claude_cli_exe)

        def _batch_clipboard(items) -> None:
            self._batch_scout_to_clipboard(path, items)

        def _batch_cli(items) -> None:
            self._batch_scout_to_cli(path, items)

        def _batch_ask(items) -> None:
            self._batch_scout_to_ask(path, items)

        RefactorScoutDialog(
            parent=self._root,
            project_path=path,
            findings=findings,
            suppressed_count=suppressed,
            on_investigate=_investigate if self._on_seed_ask else None,
            on_investigate_cli=_investigate_cli if cli_available else None,
            on_export_all_cli=_export_all_cli if cli_available else None,
            on_batch_clipboard=_batch_clipboard,
            on_batch_cli=_batch_cli if cli_available else None,
            on_batch_ask=_batch_ask if self._on_seed_ask else None,
            on_save_ignored=_save_ignored,
            currently_ignored=ignored,
        )

        total = sum(len(v) for v in findings.values())
        self._on_log(
            f"[scout] {total} findings ({suppressed} suppressed) — see dialog.",
            "green" if total == 0 else "",
        )

    def _launch_scout_briefing_in_cli(self, project_path: str, finding) -> None:
        """Write one finding to a temp .md and open a Claude CLI terminal."""
        from helpers.refactor_scout import write_finding_briefing
        from helpers.claude_cli import spawn_claude_cli
        try:
            briefing_path = write_finding_briefing(finding)
        except OSError as e:
            self._on_log(f"[scout] Could not write briefing: {e}", "red")
            return
        instruction = (
            f"Read the refactor scout briefing at \"{briefing_path}\" and "
            f"explain why this finding fired, then suggest a concrete, scoped "
            f"refactor for the named symbol only."
        )
        ok, err = spawn_claude_cli(self._cfg.claude_cli_exe, project_path,
                                    instruction)
        if not ok:
            self._on_log(f"[scout] Could not launch Claude CLI: {err}", "red")
        else:
            self._on_log(f"[scout] Opened Claude CLI with briefing → {briefing_path}")

    def _batch_scout_to_clipboard(self, project_path: str, items) -> None:
        """Format the selected findings as one markdown briefing and copy.

        The clipboard is the most universal handoff — works for the Claude
        Desktop app (paste into the chat), claude.ai web, ChatGPT, any
        editor scratchpad. The briefing is self-contained: paste-and-go.
        """
        from helpers.refactor_scout import format_batch_briefing
        text = format_batch_briefing(items, project_path)
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
            self._root.update()  # forces the clipboard buffer to flush
        except Exception as e:
            self._on_log(f"[scout] Clipboard copy failed: {e}", "red")
            return
        self._on_log(
            f"[scout] Copied {len(items)} findings to clipboard "
            f"({len(text)} chars). Paste into Claude Desktop / anywhere.",
            "green",
        )

    def _batch_scout_to_cli(self, project_path: str, items) -> None:
        """Write the selected findings to a temp briefing and open Claude CLI."""
        from helpers.refactor_scout import write_batch_briefing
        from helpers.claude_cli import spawn_claude_cli
        try:
            briefing_path = write_batch_briefing(items, project_path)
        except OSError as e:
            self._on_log(f"[scout] Could not write batch briefing: {e}", "red")
            return
        instruction = (
            f"Read the refactor scout batch briefing at \"{briefing_path}\" "
            f"and propose a prioritised refactoring plan. Group related "
            f"findings; suggest tackling order."
        )
        ok, err = spawn_claude_cli(self._cfg.claude_cli_exe, project_path,
                                    instruction)
        if not ok:
            self._on_log(f"[scout] Could not launch Claude CLI: {err}", "red")
        else:
            self._on_log(
                f"[scout] Opened Claude CLI with {len(items)} findings "
                f"→ {briefing_path}")

    def _batch_scout_to_ask(self, project_path: str, items) -> None:
        """Seed the Ask tab with the batch briefing. Routes to whatever LLM
        is configured (Ollama, Anthropic, OpenAI, LM Studio, etc.)."""
        if self._on_seed_ask is None:
            return
        from helpers.refactor_scout import format_batch_briefing
        text = format_batch_briefing(items, project_path)
        self._on_seed_ask(text, project_path)
        self._on_log(
            f"[scout] Seeded Ask tab with {len(items)} findings.")

    def _launch_scout_report_in_cli(self, project_path: str, findings,
                                     suppressed: int) -> None:
        """Write the full report to a temp .md and open a Claude CLI terminal."""
        from helpers.refactor_scout import write_full_report
        from helpers.claude_cli import spawn_claude_cli
        try:
            report_path = write_full_report(findings, project_path, suppressed)
        except OSError as e:
            self._on_log(f"[scout] Could not write report: {e}", "red")
            return
        instruction = (
            f"Read the refactor scout report at \"{report_path}\" and propose "
            f"a prioritised refactoring plan grouped by impact. Do not re-run "
            f"analytics tools — the briefing IS the analytics output."
        )
        ok, err = spawn_claude_cli(self._cfg.claude_cli_exe, project_path,
                                    instruction)
        if not ok:
            self._on_log(f"[scout] Could not launch Claude CLI: {err}", "red")
        else:
            self._on_log(f"[scout] Opened Claude CLI with full report → {report_path}")

    # ── Roadmap-3: pre-merge check runner ────────────────────────────────────

    def cmd_run_checks(self, path: str) -> None:
        """Right-click → ✓ Run checks…

        Opens ChecksDialog immediately on the main thread. The dialog owns its
        own ThreadPoolExecutor and runs all enabled checks concurrently inside
        itself — no background thread is needed here.

        Per-task lock prevents two check dialogs for the same project.
        """
        with self._running_lock:
            if (path, TASK_RUN_CHECKS) in self._running:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Already running",
                    "A checks dialog is already open for this project.",
                    parent=self._root,
                )
                return
            self._running.add((path, TASK_RUN_CHECKS))

        self._tab.after(0, self._open_checks_dialog, path)

    def _open_checks_dialog(self, path: str) -> None:
        """Main-thread: resolve base branch and open ChecksDialog."""
        try:
            from controllers.git_tab import _detect_base_branch
            from dialogs.checks_dialog import ChecksDialog

            git_exe = (self._cfg.raw or {}).get("git_exe") or "git"
            base = _detect_base_branch(path, git_exe)

            def _on_destroy(event=None) -> None:
                with self._running_lock:
                    self._running.discard((path, TASK_RUN_CHECKS))

            dlg = ChecksDialog(
                parent=self._root,
                path=path,
                cfg=self._cfg,
                base=base,
                on_log=self._on_log,
            )
            dlg.bind("<Destroy>", _on_destroy)
        except Exception as e:
            log.exception("AITasksController: could not open ChecksDialog")
            self._on_log(f"[checks] Error opening dialog: {e}", "red")
            with self._running_lock:
                self._running.discard((path, TASK_RUN_CHECKS))
