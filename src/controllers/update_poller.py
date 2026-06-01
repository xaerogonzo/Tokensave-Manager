"""UpdatePollerController — tokensave version probe + update-check loop.

Extracted from App (Round 5).

Dependency contract:
  • cfg      — read-only ManagerConfig (.tokensave_exe, .raw)
  • on_log   — thread-safe log callback  (msg: str, colour: str = "")
  • on_run   — run a tokensave sub-command (args: list, cwd: str, label: str)
  • root     — tk.Tk root window (messagebox parent)
"""

from __future__ import annotations

import functools
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from tkinter import messagebox, scrolledtext, ttk
import tkinter as tk
from typing import TYPE_CHECKING, Callable

from constants import C, CREATE_NO_WINDOW, _BASE_DIR
from helpers.detection import _version_lt
from helpers.runtime import log

if TYPE_CHECKING:
    from state import ManagerConfig


class UpdatePollerController:
    """Probes the installed tokensave version and polls GitHub for updates.

    Call start() once after App construction. Exposes cmd_upgrade() for the
    Settings dialog Upgrade button. The two version attributes are read by
    App._run() (to capture opportunistic "Update available: vA → vB" lines
    from sync output) and by SettingsDialog to decide whether to show the
    Upgrade button.
    """

    # GitHub releases API endpoint for tokensave.
    _RELEASES_API = (
        "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest"
    )

    def __init__(
        self,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_run: Callable,
        root,
    ) -> None:
        self._cfg    = cfg
        self._on_log = on_log
        self._on_run = on_run
        self._root   = root

        self._current_version:        str | None = None
        self._available_version:      str | None = None
        self._last_integration_report: str       = ""
        # The single live integration-check dialog. Re-runs (e.g. Apply Fixes)
        # reuse/replace this window instead of stacking new Toplevels.
        self._integration_dialog = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def current_version(self) -> str | None:
        return self._current_version

    @current_version.setter
    def current_version(self, value: str | None) -> None:
        """Allow App._run() to set the version when it parses sync output."""
        self._current_version = value

    @property
    def available_version(self) -> str | None:
        return self._available_version

    @available_version.setter
    def available_version(self, value: str | None) -> None:
        """Allow App._run() to set/clear the available version."""
        self._available_version = value

    def start(self) -> None:
        """Spawn the version-probe thread and the hourly update-poll loop.

        Called once from App.__init__ after the GUI is built.
        """
        threading.Thread(
            target=self._probe_worker,
            daemon=True,
            name="tokensave-version-probe",
        ).start()
        threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="tokensave-update-poll",
        ).start()

    def cmd_upgrade(self) -> None:
        """Run `tokensave upgrade` from the manager.

        Streams output to the OUTPUT pane via on_run. cwd is the
        tokensave_exe's directory — stable for any upgrade flow. Clears the
        cached available_version on entry so the Settings button auto-hides
        until the next sync re-reports an update.
        """
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            messagebox.showwarning(
                "tokensave not found",
                "Set the tokensave.exe path in Settings first.",
                parent=self._root,
            )
            return
        target = self._available_version
        cur    = self._current_version
        if target:
            msg = f"Upgrade tokensave from v{cur or '?'} to v{target}?\n\n"
        elif cur:
            msg = (
                f"Run `tokensave upgrade`?  (Currently installed: v{cur}.)\n\n"
                "tokensave will check GitHub for a newer release and apply it. "
                "No-op if you're already on the latest.\n\n"
            )
        else:
            msg = (
                "Run `tokensave upgrade`?\n\n"
                "tokensave will check GitHub for a newer release and apply it. "
                "No-op if you're already on the latest.\n\n"
            )
        msg += (
            "This replaces the tokensave binary on disk.  Currently-running\n"
            "MCP wrappers continue serving from the old binary until you\n"
            "restart Claude Desktop / Claude Code."
        )
        if not messagebox.askyesno("Upgrade tokensave", msg, parent=self._root):
            return
        # Clear cache so the Settings button hides until the next sync
        # reports a fresh update.  If the upgrade fails, the next sync
        # will re-populate it anyway.
        self._available_version = None
        self._on_run(
            ["upgrade"],
            cwd=os.path.dirname(self._cfg.tokensave_exe),
            label="upgrade",
        )

    def cmd_integration_check(self) -> None:
        """Run scripts/check_tokensave_integration.py and show the output.

        Source-only: the script lives in the repo's scripts/ directory and is
        not shipped in the compiled dist. When the script is absent (compiled
        exe mode), shows a friendly info dialog instead of an error.
        """
        script = os.path.join(_BASE_DIR, "scripts", "check_tokensave_integration.py")
        if not os.path.isfile(script):
            messagebox.showinfo(
                "Source-only tool",
                "check_tokensave_integration.py is only available when running\n"
                "TokenSave Manager from source (not from the compiled exe).\n\n"
                "Clone the repo and run  python src/app.py  to use this tool.",
                parent=self._root,
            )
            return
        threading.Thread(
            target=self._integration_check_worker,
            args=(script,),
            daemon=True,
            name="tokensave-integration-check",
        ).start()

    def _run_integration_fix(self) -> None:
        """Re-run the integration check with --fix to apply pending lifecycle actions.

        Creates stubs for new open issues and archives resolved issues. The result
        opens a fresh integration check dialog showing what was changed.
        """
        script = os.path.join(_BASE_DIR, "scripts", "check_tokensave_integration.py")
        if not os.path.isfile(script):
            messagebox.showinfo(
                "Source-only tool",
                "check_tokensave_integration.py is only available when running\n"
                "TokenSave Manager from source (not from the compiled exe).",
                parent=self._root,
            )
            return
        threading.Thread(
            target=self._integration_check_worker,
            args=(script, True),        # fix_mode=True
            daemon=True,
            name="tokensave-integration-fix",
        ).start()

    def _integration_check_worker(self, script: str, fix_mode: bool = False) -> None:
        """Background worker: run the integration check script and surface output."""
        args = [sys.executable, script]
        if self._available_version:
            args += ["--available", self._available_version]
        if fix_mode:
            args += ["--fix"]
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
                cwd=_BASE_DIR,
                creationflags=CREATE_NO_WINDOW,
            )
            output = (r.stdout or "").strip() or (r.stderr or "").strip() or "(no output)"
        except subprocess.TimeoutExpired:
            output = "⚠ Timed out after 30 s (GitHub API calls may be slow)."
        except Exception as exc:
            output = f"⚠ Error running script: {exc}"
        # In fix mode, also compute a human-readable summary of what (if anything)
        # changed so the user gets explicit feedback rather than a silent re-run.
        summary = self._summarize_fix_output(output) if fix_mode else None
        self._root.after(
            0, lambda o=output, s=summary: self._show_integration_output(o, fix_summary=s))

    @staticmethod
    def _summarize_fix_output(output: str) -> str:
        """Parse the --fix script output into a short 'what changed' summary.

        The script prints lifecycle actions under
        ``### Upstream issue lifecycle (--fix applied)`` as ``📄  Created …`` /
        ``📦  Archived …`` lines. Absent section or no action lines means
        nothing needed doing.
        """
        created, archived = [], []
        for line in output.splitlines():
            s = line.strip()
            if s.startswith("📄") and "Created" in s:
                created.append(s.lstrip("📄 ").strip())
            elif s.startswith("📦") and "Archived" in s:
                archived.append(s.lstrip("📦 ").strip())

        if not created and not archived:
            return (
                "✓  Nothing to apply — the integration docs are already in sync.\n\n"
                "No issue stubs were created and no resolved issues needed "
                "archiving. Your working tree is unchanged."
            )

        parts = [f"✓  Applied {len(created) + len(archived)} change"
                 f"{'s' if (len(created) + len(archived)) != 1 else ''}:\n"]
        if created:
            parts.append(f"\n📄  Created {len(created)} stub"
                         f"{'s' if len(created) != 1 else ''}:")
            parts += [f"   • {c}" for c in created]
        if archived:
            parts.append(f"\n📦  Archived {len(archived)} resolved issue"
                         f"{'s' if len(archived) != 1 else ''}:")
            parts += [f"   • {a}" for a in archived]
        parts.append("\n\nReview the changes in the Git tab and commit them "
                     "to keep the repo clean.")
        return "\n".join(parts)

    def _show_integration_output(self, text: str, fix_summary: "str | None" = None) -> None:
        """Main-thread: pop a scrollable read-only text dialog with the report.

        Features:
        - Single window: a prior integration dialog is destroyed first, so
          re-runs (Apply Fixes / Run Audit) replace it instead of stacking.
        - Clickable https:// URLs (unique tag per URL to avoid last-URL override)
        - "🔍 Run Audit ▼" Menubutton with Claude CLI / Local LLM / Copy for Chat
        - grab_set() overrides any open Settings modal
        - fix_summary: when set (Apply Fixes path), a small modal afterwards
          reports exactly what changed (or that nothing did).
        """
        self._last_integration_report = text

        # Single-window: destroy any prior integration dialog so re-runs replace
        # it rather than opening a second window on top.
        prior = self._integration_dialog
        if prior is not None:
            try:
                if prior.winfo_exists():
                    prior.destroy()
            except tk.TclError:
                pass
            self._integration_dialog = None

        dlg = tk.Toplevel(self._root)
        self._integration_dialog = dlg
        dlg.title("Tokensave integration check")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.geometry("720x480")
        dlg.transient(self._root)
        # Clear the tracked reference when the user closes the window.
        dlg.bind("<Destroy>", lambda e: setattr(self, "_integration_dialog", None)
                 if e.widget is dlg else None, add="+")

        st = scrolledtext.ScrolledText(
            dlg, wrap=tk.WORD, font=("Consolas", 10),
            bg=C["mantle"], fg=C["text"], insertbackground=C["text"],
            relief=tk.FLAT, padx=10, pady=10,
        )
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        st.insert(tk.END, text)

        # Apply unique clickable tags for each URL — must be done before DISABLED
        for i, match in enumerate(re.finditer(r"https://\S+", text)):
            url = match.group().rstrip(".,)")   # strip trailing punctuation
            tag = f"url_{i}"
            # Convert character offset to Tkinter "line.char" index
            start_idx = f"1.0 + {match.start()} chars"
            end_idx   = f"1.0 + {match.start() + len(url)} chars"
            st.tag_add(tag, start_idx, end_idx)
            st.tag_configure(tag, foreground=C.get("blue", "#89b4fa"), underline=True)
            st.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
            st.tag_bind(tag, "<Enter>", lambda e: st.configure(cursor="hand2"))
            st.tag_bind(tag, "<Leave>", lambda e: st.configure(cursor=""))

        st.configure(state=tk.DISABLED)

        # ── Button row ────────────────────────────────────────────────────────
        btn_row = tk.Frame(dlg, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))

        # Determine which backends are available
        has_cli = bool(self._cfg.claude_cli_exe)
        raw = self._cfg.raw or {}
        llm_cfg = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
        has_llm = bool(llm_cfg.get("enabled") and llm_cfg.get("provider"))

        audit_btn = ttk.Menubutton(btn_row, text="🔍  Run Audit ▼")
        audit_btn.pack(side=tk.LEFT)

        audit_menu = tk.Menu(audit_btn, tearoff=False, bg=C["base"], fg=C["text"])
        audit_btn["menu"] = audit_menu
        audit_menu.add_command(
            label="🤖  Via Claude CLI",
            state=tk.NORMAL if has_cli else tk.DISABLED,
            command=lambda: self._run_audit_backend("claude_cli", dlg, audit_btn),
        )
        audit_menu.add_command(
            label="🧠  Via Local LLM",
            state=tk.NORMAL if has_llm else tk.DISABLED,
            command=lambda: self._run_audit_backend("local_llm", dlg, audit_btn),
        )
        audit_menu.add_command(
            label="📋  Copy for Chat",
            command=lambda: self._run_audit_backend("clipboard", dlg, audit_btn),
        )

        ttk.Button(btn_row, text="🔧  Apply Fixes",
                   command=self._run_integration_fix).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        dlg.grab_set()
        dlg.focus_set()

        # Apply Fixes path: report exactly what changed in a small modal. Done
        # after the main dialog is up + grabbed so it layers on top and returns
        # focus to the report when dismissed.
        if fix_summary is not None:
            changed = fix_summary.startswith("✓  Applied")
            dlg.after(0, lambda: (
                messagebox.showinfo("Apply Fixes — result", fix_summary, parent=dlg)
                if changed else
                messagebox.showinfo("Apply Fixes — no changes", fix_summary, parent=dlg)
            ))

    # ── LLM audit helpers ─────────────────────────────────────────────────────

    _AUDIT_SYSTEM = (
        "You are a tokensave integration auditor. "
        "Analyze the provided report and GitHub context, then produce a "
        "structured action list: new snippets needed, stale snippets to "
        "update, upstream issues to update, and any manager code changes."
    )

    # Direct tool-use prompt for Claude CLI mode — replaces the PROMPT_SNIPPETS
    # audit body because the model has real tool access (MCP servers, file reads)
    # and can call tokensave_changelog directly instead of using pre-fetched text.
    _AUDIT_CLI_STEPS = (
        "You are a tokensave integration auditor with full tool access.\n"
        "The pre-fetched integration report above contains live GitHub issue "
        "statuses from the manager's GraphQL queries.\n\n"
        "STEP 1 — Call tokensave_changelog. List every new tool, removed tool,\n"
        "  and CLI/schema change. Extract tool names exactly.\n\n"
        "STEP 2 — Read src/prompts.py. For each NEW tool from Step 1, check\n"
        "  snippet coverage. List any new tools with no covering snippet.\n\n"
        "STEP 3 — For each REMOVED tool from Step 1, check if any snippet body\n"
        "  still references it. List stale snippet titles.\n\n"
        "STEP 4 — Read docs/upstream-issues/*.md. For each file lacking\n"
        "  STATUS: FIXED/SHIPPED/MOOT in first 8 lines, check if the changelog\n"
        "  change resolves it and propose the correct STATUS line.\n\n"
        "STEP 5 — Read src/helpers/daemon_cost.py and src/agent_tools.py.\n"
        "  Flag any function wrapping a tool removed or renamed in Step 1.\n"
        "  If codegraph MCP tools are available, use them for structural\n"
        "  cross-references alongside tokensave_context.\n\n"
        "STEP 6 — Output a structured action list:\n"
        "  ## Integration report — tokensave v[[new version]]\n"
        "  ### New snippets needed\n"
        "  ### Stale snippets to update\n"
        "  ### Upstream issues to update\n"
        "  ### Manager code changes\n"
        "  ### Confirmed no-action"
    )

    def _build_audit_user_prompt(self, mode: str) -> str:
        """Assemble the LLM audit prompt. Strategy differs by backend:

        claude_cli:  inject integration report (GitHub issue statuses) + direct
                     tool-use instructions. No releases block — CLI can call
                     tokensave_changelog directly.
        local_llm:   inject report only (no releases — ReAct loops compound
                     context, so we keep the initial prompt lean). Append tool
                     instructions conditionally when tokensave_exe is set.
        clipboard:   full context (report) + PROMPT_SNIPPETS audit body + MCP note.

        Uses a randomised boundary string to prevent injection via GitHub
        release body or issue titles containing markdown/XML characters.
        """
        report = self._last_integration_report or "(no report available)"
        boundary = f"AUDIT-CTX-{secrets.token_hex(8).upper()}"
        report_block = (
            f"---{boundary}-REPORT---\n{report}\n---{boundary}-END---"
        )

        if mode == "clipboard":
            from prompts import PROMPT_SNIPPETS  # lazy — avoids circular risk
            audit_body = next(
                (b for t, b in PROMPT_SNIPPETS if "Integration audit" in t), ""
            )
            return (
                f"{report_block}\n\n"
                f"{audit_body}\n\n"
                "If MCP tools are configured in your session, call "
                "tokensave_changelog directly as instructed in the prompt above."
            )

        if mode == "claude_cli":
            # CLI has full tool access — use the direct tool-use prompt.
            # No releases block needed; model calls tokensave_changelog itself.
            return f"{report_block}\n\n{self._AUDIT_CLI_STEPS}"

        # local_llm — lean prompt (no releases) + conditional tool instructions
        prompt = (
            f"{report_block}\n\n"
            "Analyze the integration report above and produce a structured "
            "audit:\n"
            "  1. Are there any tracked issues still OPEN that the report shows "
            "as resolved?\n"
            "  2. Are there any snippets in src/prompts.py referencing removed "
            "tools?\n"
            "  3. Are there any manager helper files that wrap tools now gone?\n\n"
            "Output:\n"
            "  ## Integration report\n"
            "  ### Upstream issues to update\n"
            "  ### Stale snippets to update\n"
            "  ### Manager code changes\n"
            "  ### Confirmed no-action"
        )
        if self._cfg.tokensave_exe:
            prompt += (
                "\n\nYou have two tools available:\n"
                "  tokensave_search — find symbols by keyword\n"
                "  tokensave_context — explore how a symbol is used\n"
                "Use them to verify prompt coverage and check manager code "
                "for removed tool references."
            )
        return prompt

    def _fetch_releases_for_prompt(self, repo: str, gh_exe: str,
                                   installed_version: str) -> str:
        """Fetch GitHub releases newer than installed_version via `gh api`.

        Returns a formatted text block, or "" if nothing newer / gh fails.
        No --paginate; fetches one page of 10 releases max.
        Inlined here to avoid cross-directory import from scripts/.
        """
        import json as _json
        import re as _re

        _verre = _re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

        def _pv(s: str):
            m = _verre.match(s or "")
            return tuple(map(int, m.groups())) if m else (0, 0, 0)

        args = [gh_exe, "api", f"repos/{repo}/releases?per_page=10"]
        try:
            r = subprocess.run(
                args, capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                return ""
            data = _json.loads(r.stdout)
        except Exception:
            return ""

        if not isinstance(data, list):
            return ""

        installed_tuple = _pv(installed_version)
        newer = [
            rel for rel in data
            if _pv(rel.get("tag_name", "")) > installed_tuple
        ]
        if not newer:
            return ""

        lines = [f"GitHub releases — {repo} (since v{installed_version})\n"]
        for rel in newer:
            tag = rel.get("tag_name", "?")
            published = (rel.get("published_at") or "")[:10]
            body = (rel.get("body") or "").strip()
            if len(body) > 1500:
                body = body[:1500] + "\n  … (truncated)"
            lines.append(f"\n#### {tag} — {published}")
            if body:
                lines.append(body)
        return "\n".join(lines)

    def _run_audit_backend(self, mode: str, parent_dlg, audit_btn) -> None:
        """Launch the chosen LLM audit backend.

        mode ∈ {"claude_cli", "local_llm", "clipboard"}

        Imports are done on the MAIN THREAD before spawning the worker to
        avoid Python's import-lock deadlock in background threads.
        """
        if mode == "clipboard":
            prompt = self._build_audit_user_prompt("clipboard")
            self._root.clipboard_clear()
            self._root.clipboard_append(prompt)
            messagebox.showinfo(
                "Copied!",
                "Paste into a Claude Code chat session.\n"
                "If MCP tools are configured in your session, Claude will call\n"
                "tokensave_changelog directly as instructed in the prompt.",
                parent=parent_dlg,
            )
            return

        # Import on main thread — prevents import-lock deadlock in worker
        if mode == "claude_cli":
            from helpers.claude_cli import call_claude_cli_print as _call_fn
            exe   = self._cfg.claude_cli_exe
            model = self._cfg.claude_cli_model
            llm_cfg_local = None
        else:  # local_llm
            from helpers.doc_drafter import dispatch_llm as _call_fn
            exe, model = "", ""
            raw = self._cfg.raw or {}
            llm_cfg_local = (
                raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
            )

        prompt = self._build_audit_user_prompt(mode)
        audit_btn.configure(state=tk.DISABLED)

        # Loading modal — grab_set so it blocks interaction
        loading_dlg = tk.Toplevel(parent_dlg)
        loading_dlg.title("Running LLM audit…")
        loading_dlg.configure(bg=C["base"])
        loading_dlg.geometry("420x90")
        loading_dlg.resizable(False, False)
        loading_dlg.grab_set()
        wait_msg = (
            "Please wait — calling LLM (CLI: up to 5 min, Local: up to 6 min)…"
        )
        tk.Label(
            loading_dlg, text=wait_msg,
            bg=C["base"], fg=C["text"], font=("Segoe UI", 10),
        ).pack(expand=True)

        audit_system = self._AUDIT_SYSTEM

        def _worker() -> None:
            result, err = None, None
            try:
                if mode == "claude_cli":
                    # 300 s — agentic tool-use loops (file reads + tokensave_changelog)
                    # take longer than the old 180 s plain-completion ceiling.
                    result = _call_fn(
                        exe, prompt, system_prompt=audit_system,
                        timeout=300, model=model, cwd=str(_BASE_DIR),
                    )
                    if result is None:
                        err = (
                            "Claude CLI returned no output.\n"
                            "Check claude_cli_exe in Settings → Claude Code CLI."
                        )
                else:
                    # enable_tokensave_tools routes to _dispatch_agentic (LocalAgent
                    # loop) which also fixes the Ollama num_ctx overflow by breaking
                    # the task into smaller turns instead of one huge completion.
                    # 360 s — LocalAgent with max_iterations=6 on Ollama hardware
                    # can take 30–60 s per turn; 180 s was consistently too tight.
                    result, err = _call_fn(
                        llm_cfg_local, audit_system, prompt,
                        self._cfg.claude_cli_exe, str(_BASE_DIR), timeout=360,
                        enable_tokensave_tools=True,
                        tokensave_exe=self._cfg.tokensave_exe or "",
                    )
            except Exception as exc:
                err = str(exc)
            finally:
                # winfo_exists guard — user may close the loading modal manually
                # during the extended 300 s window; guard prevents TclError.
                self._root.after(
                    0,
                    lambda: loading_dlg.destroy() if loading_dlg.winfo_exists() else None,
                )
                self._root.after(0, lambda: audit_btn.configure(state=tk.NORMAL))
                self._root.after(
                    0,
                    functools.partial(
                        self._show_audit_result, result, err, parent_dlg
                    ),
                )

        threading.Thread(
            target=_worker, daemon=True, name=f"ts-audit-{mode}"
        ).start()

    def _show_audit_result(self, text: str | None, err: str | None,
                           parent) -> None:
        """Show the LLM audit result in a new scrollable dialog."""
        result_text = text or err or "(no output)"
        dlg = tk.Toplevel(parent)
        dlg.title("LLM Integration Audit")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.geometry("700x520")
        dlg.grab_set()

        st = scrolledtext.ScrolledText(
            dlg, wrap=tk.WORD, font=("Consolas", 10),
            bg=C["mantle"], fg=C["text"], insertbackground=C["text"],
            relief=tk.FLAT, padx=10, pady=10,
        )
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        st.insert(tk.END, result_text)
        st.configure(state=tk.DISABLED)

        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 10))
        dlg.focus_set()

    # ── Internal workers ──────────────────────────────────────────────────────

    def _probe_worker(self) -> None:
        """Best-effort read of the installed tokensave version.

        Runs `tokensave --version` once at startup (background thread to avoid
        blocking the GUI). Caches the version string; failures leave it as None.
        After a successful probe, triggers a single update check immediately.
        """
        if not self._cfg.tokensave_exe or not os.path.isfile(self._cfg.tokensave_exe):
            return
        try:
            r = subprocess.run(
                [self._cfg.tokensave_exe, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        out = (r.stdout or "").strip()
        m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out)
        if m:
            self._current_version = m.group(1)
            log.debug(f"tokensave installed version: {self._current_version}")
            # Immediate one-shot check right after we know the local version.
            # Subsequent checks fire from the hourly poller.
            self._check_updates()

    def _poll_interval(self) -> float:
        """Return the poll interval in seconds (tunable via cfg)."""
        hours = float(self._cfg.raw.get("tokensave_update_poll_hours", 1.0))
        return max(0.25, hours) * 3600.0  # never poll more than 4×/hour

    def _poll_loop(self) -> None:
        """Daemon: re-check GitHub for new tokensave releases periodically.

        Doesn't prompt the user — just refreshes the cached available_version
        so Settings reflects current state next time it's opened. The OUTPUT-
        pane hint is only logged on fresh discovery (transition from "no update
        known" → "update available"), not on every poll.
        """
        while True:
            time.sleep(self._poll_interval())
            self._check_updates()

    def _check_updates(self) -> None:
        """Single-shot check against the tokensave releases API.

        Compares against current_version (set by _probe_worker). When a
        strictly-newer version is found AND it wasn't known before, logs a
        peach hint to the OUTPUT pane.
        """
        import json as _json
        import urllib.error
        import urllib.request

        if not self._current_version:
            return  # nothing to compare against yet
        try:
            req = urllib.request.Request(
                self._RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "tokensave-manager",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            _json.JSONDecodeError,
            OSError,
        ) as e:
            log.debug(f"tokensave update check failed: {type(e).__name__}: {e}")
            return
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        m = re.match(r"(\d+\.\d+\.\d+(?:\.\d+)?)", tag)
        if not m:
            return
        latest = m.group(1)
        cur = self._current_version
        if not _version_lt(cur, latest):
            return  # current is up-to-date or ahead
        prev_known = self._available_version
        self._available_version = latest
        if prev_known != latest:
            # Fresh discovery — surface to OUTPUT pane.
            self._on_log(
                f"  → tokensave {cur} → {latest} ready to install.  "
                f"Settings → 'Upgrade tokensave to v{latest}' to apply, "
                f"or run 'tokensave upgrade' from a shell.",
                C["peach"],
            )
            self._on_log(
                "  → Integration workflow: upgrade tokensave → git pull "
                "this repo → python scripts/check_tokensave_integration.py "
                "→ '🔄 Integration audit' snippet in Reference tab.",
                C["subtext"],
            )
            log.info(f"UPDATE-AVAILABLE  {cur} -> {latest}  (via GitHub API)")
