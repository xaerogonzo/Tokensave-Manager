"""pr_draft_ctrl.py — PRDraftCtrl sub-controller.

Extracted from :class:`~controllers.git_tab.GitTabController` (Phase C1).
Owns all Draft PR logic: base-branch resolution, CLI and API draft paths,
the streaming dialog, and the GitHub PR creation helpers.

The parent (:class:`~controllers.git_tab.GitTabController`) keeps
``_pr_draft_dialog`` and ``_pr_draft_dirty`` as properties that delegate to
:attr:`PRDraftCtrl._dialog` and :attr:`PRDraftCtrl._dirty` so existing tests
can still access them as ``controller._pr_draft_dialog``.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from constants import C, CREATE_NO_WINDOW
from theme import _Tooltip
from helpers.runtime import log

# Module-level helpers from git_tab — imported to avoid duplication
from controllers.git_tab import _detect_base_branch, _extract_pr_title

if TYPE_CHECKING:
    from typing import Callable
    from state import ManagerConfig



class PRDraftCtrl:
    """Owns all Draft PR logic, extracted from GitTabController.

    Constructor callbacks decouple it from the parent:

    * ``get_path`` — returns the current git project path.
    * ``build_gap_panel_cb(dlg, path, base, suggestions, on_tests_written)``
      — calls :meth:`~TestGapCtrl.build_gap_panel`.
    * ``open_test_gaps_cb(path, base, suggestions)``
      — calls :meth:`~TestGapCtrl.open_test_gaps_window`.
    * ``apply_gap_progress_cb(ctx, rel_paths)``
      — calls :func:`~test_gap_ctrl._apply_gap_progress_to_body`.

    The parent retains ``_pr_draft_dialog`` and ``_pr_draft_dirty`` as
    ``@property`` delegates to keep test assertions working unchanged.
    """

    def __init__(
        self,
        tab: "tk.Frame",
        cfg: "ManagerConfig",
        on_log: "Callable",
        get_path: "Callable[[], str | None]",
        build_gap_panel_cb: "Callable",
        open_test_gaps_cb: "Callable",
        apply_gap_progress_cb: "Callable",
    ) -> None:
        self._tab = tab
        self._cfg = cfg
        self._on_log = on_log
        self.get_path = get_path
        self._build_gap_panel_cb = build_gap_panel_cb
        self._open_test_gaps_cb = open_test_gaps_cb
        self._apply_gap_progress_cb = apply_gap_progress_cb
        # PR draft state (exposed to parent via properties)
        self._dialog = None
        self._dirty: bool = False

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    def disconnect(self) -> None:
        """Clear callback references to prevent reference cycles on teardown."""
        self.get_path = None
        self._build_gap_panel_cb = None
        self._open_test_gaps_cb = None
        self._apply_gap_progress_cb = None

    def resolve_pr_base(self, path: str) -> "str | None":
        """Public alias for :meth:`_resolve_pr_base` used by GitTabController."""
        return self._resolve_pr_base(path)

    def show_draft_pr_menu(self, event, btn) -> None:
        """Public alias for :meth:`_show_draft_pr_menu`."""
        self._show_draft_pr_menu(event, btn)

    def draft_pr_via_cli(self, path: str) -> None:
        """Public alias for :meth:`_draft_pr_via_cli`."""
        self._draft_pr_via_cli(path)

    def draft_pr_via_api(self, path: str) -> None:
        """Public alias for :meth:`_draft_pr_via_api`."""
        self._draft_pr_via_api(path)

    def _resolve_pr_base(self, path: str) -> "str | None":
        """Return the base branch for a PR, honouring any per-project override.

        Checks ``raw["pr_base_branch_override"][path]`` first; falls back to
        the automatic 7-step ``_detect_base_branch`` chain when no override is
        stored.
        """
        override = (self._cfg.raw
                    .get("pr_base_branch_override", {})
                    .get(path, ""))
        if override:
            return override
        return _detect_base_branch(path, self._cfg.git_exe)

    def _cmd_set_pr_base(self, path: str) -> None:
        """Prompt the user for a PR base branch override and persist it."""
        from tkinter import simpledialog
        current = (self._cfg.raw
                   .get("pr_base_branch_override", {})
                   .get(path, ""))
        new_val = simpledialog.askstring(
            "Set PR base branch",
            "Enter the base branch for Draft PR (leave blank to reset to auto-detect):\n\n"
            f"Current: {current or '(auto)'}",
            initialvalue=current,
            parent=self._root,
        )
        if new_val is None:   # user cancelled the dialog
            return
        overrides = self._cfg.raw.setdefault("pr_base_branch_override", {})
        if new_val.strip():
            overrides[path] = new_val.strip()
            self._on_log(f"  Draft PR base branch override set to '{new_val.strip()}' "
                         f"for {os.path.basename(path)}", C["green"])
        else:
            overrides.pop(path, None)
            self._on_log(f"  Draft PR base branch override cleared for "
                         f"{os.path.basename(path)} (back to auto-detect)", C["overlay0"])
        self._cfg.save()

    def _show_draft_pr_menu(self, event, btn):
        """Show an override menu for right-click / Shift+click on Draft PR."""
        path = self._git_path
        if not path:
            return
        menu = tk.Menu(self._tab, tearoff=0)
        menu.add_command(label="Use Claude Code CLI",
                         command=lambda: self._draft_pr_via_cli(path))
        menu.add_command(label="Use Ollama / API (inline dialog)",
                         command=lambda: self._draft_pr_via_api(path))
        # Base branch override
        current_base = (self._cfg.raw
                        .get("pr_base_branch_override", {})
                        .get(path, ""))
        base_label = f"Set PR base branch…  (now: {current_base or 'auto'})"
        menu.add_separator()
        menu.add_command(label=base_label,
                         command=lambda: self._cmd_set_pr_base(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _draft_pr_via_cli(self, path: str):
        from helpers.claude_cli import spawn_claude_cli   # lazy import
        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Draft PR — base branch not found",
                "Could not detect the base branch for this PR.\n\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push to a remote and set a tracking branch with:\n"
                "  git branch --set-upstream-to=origin/<base> <branch>",
                parent=self._root)
            return
        # triple-dot `git diff base...HEAD` computes diff(merge-base(base,HEAD), HEAD)
        # — this isolates only this branch's commits, excluding upstream changes that
        # landed on base after branching. Do not change to double-dot.
        gh_available = bool(shutil.which("gh"))
        gh_base = base.split("/")[-1]   # strip "origin/" prefix; gh expects bare branch name
        gh_step = (
            f"Then create the PR on GitHub: push the branch first with "
            f"`git push -u origin HEAD` if it has no remote tracking, then run "
            f"`gh pr create --title <one-line-title> --body-file PR_DRAFT.md "
            f"--base {gh_base}` (replace <one-line-title> with a short descriptive title) "
            f"and print the resulting PR URL. "
            f"If the PR is created successfully, delete PR_DRAFT.md from the project root."
            if gh_available else
            "Note: gh CLI is not on PATH, so the PR_DRAFT.md file is your deliverable — "
            "skip any gh commands."
        )

        # v4.6: pre-build tokensave + codegraph grounding when enabled, AND
        # nudge the CLI to use its own MCP tools if they're wired. Two
        # complementary mechanisms: the grounding block gives the CLI
        # ready-made facts (works regardless of MCP config), the MCP nudge
        # exercises tools the user has already configured for Claude Code.
        #
        # The grounding block CANNOT be inlined into the instruction string
        # because spawn_claude_cli passes it as a cmd.exe command-line
        # argument and cmd.exe interprets `|`, `&`, `(`, `)`, `"` in the
        # markdown content as shell metacharacters — the spawned process
        # crashes immediately.  Write the grounding to a sibling file
        # (`.pr_context.tmp.md` next to PR_DRAFT.md) and tell the CLI to
        # read it as step 1.  The CLI removes the temp file when done.
        grounding_block, grounded = self._build_pr_grounding(path, base)
        if grounded:
            self._on_log("  Draft PR: built grounding from tokensave + codegraph",
                          C["green"])
        else:
            reason = "off in Settings" if not self._cfg.enable_pr_grounding \
                     else "neither tool indexed for this project"
            self._on_log(f"  Draft PR: no grounding attached ({reason})",
                          C["overlay0"])

        # Pre-render the automated checklist block from the test-run cache so
        # Claude can copy it verbatim into PR_DRAFT.md — shell metacharacters in
        # the markdown ([, ], `, |) prevent inlining it into the instruction string,
        # so it lives in the context file alongside the grounding data.
        from helpers.pr_draft import (  # lazy — avoids circular import
            _render_automated_for_pr, _render_coverage_gaps)
        automated_block = _render_automated_for_pr(path)
        # Coverage gaps (changed files lacking tests) — same filtered list the
        # Ollama path injects, so both PR bodies carry it. Reused below to open
        # the test-gap window after the CLI launches.
        try:
            from helpers.test_gap_report import suggest_tests_for_diff
            _gap_suggestions = suggest_tests_for_diff(path, self._cfg.git_exe, base)
        except Exception:
            _gap_suggestions = []
        _gaps_block = _render_coverage_gaps(_gap_suggestions)
        _checklist_tmpl = (
            "## Testing checklist\n"
            "<!-- tokensave-manager:testing-checklist v1 -->\n"
            + automated_block
            + (("\n" + _gaps_block) if _gaps_block else "")
            + "\n### Manual (please verify before merge)\n"
            "- [ ] <one smoke check per meaningful UI flow or changed behaviour>\n"
            "- [ ] <2-5 bullets total>\n"
        )

        context_step = ""
        context_path = ""
        try:
            context_path = os.path.join(path, ".pr_context.tmp.md")
            parts = [
                "# PR context (pre-fetched by manager)\n\n"
                "_Delete this file after PR_DRAFT.md is written._\n",
            ]
            if grounded:
                parts.append(grounding_block + "\n")
            parts.append(
                "\n---\n\n"
                "## Required Testing Checklist Format\n\n"
                "The `## Testing checklist` section in PR_DRAFT.md MUST use this exact "
                "format, including the HTML comment marker — the manager's "
                "'Sync PR Checklist' button depends on it:\n\n"
                + _checklist_tmpl
            )
            with open(context_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("".join(parts))
            _ctx_hint = "project context and " if grounded else ""
            context_step = (
                f"First, read `.pr_context.tmp.md` — it contains {_ctx_hint}"
                f"the required PR checklist format (copy it verbatim into PR_DRAFT.md). "
                f"After PR_DRAFT.md is written, delete `.pr_context.tmp.md`. Then: "
            )
        except OSError as exc:
            self._on_log(
                f"  Draft PR: could not write context file ({exc}); proceeding without it.",
                C["peach"])
            context_step = ""

        mcp_nudge = (
            " Note: if `mcp__tokensave__*` tools are available in this "
            "session, prefer `mcp__tokensave__tokensave_pr_context` and "
            "`mcp__tokensave__tokensave_diff_context` over additional "
            "`git diff` calls — they return branch-scoped structural facts. "
            "Fall back to bash git when those MCP tools are not registered."
            if (grounded or self._cfg.enable_pr_grounding) else ""
        )

        instruction = (
            f"{context_step}"
            f"Draft a PR description for this branch against `{base}`. "
            f"Run `git log {base}..HEAD --oneline` to see the commits, then "
            f"`git diff {base}...HEAD` to see the full diff (triple-dot gives the "
            f"merge-base diff, isolating only this branch's changes — not upstream). "
            f"Write the PR description to PR_DRAFT.md in the current working directory "
            f"(use a relative path — just PR_DRAFT.md, not an absolute path). "
            f"Include: a one-line summary, bullet list of key changes, and a testing checklist "
            f"copied verbatim from the format in .pr_context.tmp.md. "
            f"{gh_step}"
            f"{mcp_nudge}"
        )
        ok, err = spawn_claude_cli(
            self._cfg.claude_cli_exe, path, instruction,
            model=self._cfg.claude_cli_model,
        )
        if not ok:
            # Primary action failed — surface the error and STOP; don't pop the
            # secondary test-gap window into a disjoint "failed but soliciting" state.
            messagebox.showerror("Claude Code CLI error", err, parent=self._root)
            return
        # CLI launched (non-blocking Popen) — open the manager's test-gap window
        # so CLI users get the same coverage visibility the Ollama dialog has.
        if base and _gap_suggestions:
            self._open_test_gaps_cb(path, base, suggestions=_gap_suggestions)

    def _build_pr_grounding(self, path: str, base: str) -> "tuple[str, bool]":
        """Pre-build the tokensave + codegraph grounding block for a CLI PR draft.

        Returns ``(block_text, grounded)``. ``grounded`` is True iff at least
        one source returned non-empty content AND the per-feature setting
        is on.  Fail-open: any exception returns ``("", False)`` and the
        Draft PR proceeds without grounding.
        """
        if not self._cfg.enable_pr_grounding:
            return "", False
        try:
            from helpers.doc_grounding import (
                build_grounding_block,
                build_codegraph_block,
                build_combined_grounding,
            )
            # Best-effort changed-files snapshot (triple-dot merge-base diff).
            # If git_exe is missing or the diff fails, the codegraph block
            # falls back to project-scoped queries — still useful.
            changed_files: list = []
            try:
                import subprocess
                from constants import CREATE_NO_WINDOW
                proc = subprocess.run(
                    [self._cfg.git_exe, "-C", path, "diff",
                     "--name-only", f"{base}...HEAD"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                if proc.returncode == 0 and proc.stdout:
                    changed_files = [ln.strip() for ln in
                                     proc.stdout.splitlines() if ln.strip()]
            except Exception:
                pass
            try:
                ts_block = build_grounding_block(
                    path, "roadmap_evidence",
                    tokensave_exe=self._cfg.tokensave_exe,
                )
            except Exception:
                ts_block = ""
            try:
                if self._cfg.codegraph_exe:
                    try:
                        from helpers.codegraph_freshness import ensure_fresh
                        ensure_fresh(path, self._cfg.codegraph_exe)
                    except Exception:
                        pass
                cg_block = build_codegraph_block(
                    path, "roadmap_evidence",
                    changed_files=changed_files,
                    codegraph_exe=self._cfg.codegraph_exe or "",
                )
            except Exception:
                cg_block = ""
            combined = build_combined_grounding(ts_block, cg_block)
            return combined, bool(combined.strip())
        except Exception:
            return "", False

    def _draft_pr_via_api(self, path: str):
        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Draft PR — base branch not found",
                "Could not detect the base branch for this PR.\n\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push to a remote and set a tracking branch with:\n"
                "  git branch --set-upstream-to=origin/<base> <branch>",
                parent=self._root)
            return

        llm_cfg = self._cfg.raw.get("commit_message_llm", {})
        if not llm_cfg.get("enabled"):
            messagebox.showerror(
                "Draft PR — LLM not enabled",
                "The Ollama / API provider is not enabled.\n\n"
                "Go to Settings → Commit Message LLM and enable it,\n"
                "then set a provider and model.",
                parent=self._root)
            return
        if not llm_cfg.get("model"):
            messagebox.showerror(
                "Draft PR — no model configured",
                "No model name is set for the LLM provider.\n\n"
                "Go to Settings → Commit Message LLM and enter\n"
                "a model name (e.g. 'llama3.2' for Ollama).",
                parent=self._root)
            return

        provider = llm_cfg.get("provider", "ollama")
        self._on_log(f"  Drafting PR description via {provider}…", C["blue"])

        # Open the streaming dialog immediately (standalone window). Returns the
        # context dict, or None if the user declined to discard a dirty draft.
        ctx = self._open_pr_draft_dialog(path, base or "", provider)
        if ctx is None:
            return

        _start_time = time.monotonic()
        ctx["start"] = _start_time
        q = ctx["queue"]

        def _fetch():
            from helpers.pr_draft import generate_pr_draft, _render_coverage_gaps
            from helpers.llm import get_last_llm_error
            # Outer guard: ALWAYS enqueue ("done", …) so the poll loop can never
            # spin forever, and log progress so a silent hang is diagnosable from
            # manager.log (Tk-callback / thread exceptions otherwise vanish under
            # pythonw with no console).
            result = None
            err = None
            suggestions: list = []
            try:
                log.info("PR draft worker: start (provider=%s, base=%s)",
                         provider, base or "")
                # Compute test-gap suggestions ONCE — reused for the body
                # checklist AND the panel (no duplicate whole-tree scan).
                try:
                    from helpers.test_gap_report import suggest_tests_for_diff
                    suggestions = suggest_tests_for_diff(path, self._cfg.git_exe, base or "")
                except Exception:
                    log.exception("PR draft: suggest_tests_for_diff failed")
                    suggestions = []
                try:
                    gaps_md = _render_coverage_gaps(suggestions)
                except Exception:
                    gaps_md = ""
                log.info("PR draft worker: %d gap(s); calling generate_pr_draft",
                         len(suggestions))
                result = generate_pr_draft(
                    self._cfg, path, base=base or "",
                    on_token=lambda d: q.put(("token", d)),
                    on_status=lambda p: q.put(("status", p)),
                    coverage_gaps_md=gaps_md,
                )
                log.info("PR draft worker: generate_pr_draft returned %d chars",
                         len(result) if result else 0)
            except Exception as exc:
                log.exception("PR draft worker failed")
                result, err = None, str(exc)
            # Capture on the worker thread — _tls.last_error is thread-local.
            diag = get_last_llm_error() if (result is None and err is None) else None
            q.put(("done", {
                "result": result, "err": err, "diag": diag,
                "suggestions": suggestions,
                "elapsed": int(time.monotonic() - _start_time),
            }))

        threading.Thread(target=_fetch, daemon=True).start()
        self._poll_pr_stream(ctx)

    @staticmethod
    def _pr_status_label(phase: str) -> str:
        """Map a generate_pr_draft on_status phase to a human status line."""
        return {
            "grounding":  "Grounding with tokensave + codegraph…",
            "generating": "Generating draft… (streaming)",
        }.get(phase, phase)

    def _open_pr_draft_dialog(self, path: str, base: str, provider: str = ""):
        """Open the standalone streaming PR-draft window; return its context dict.

        Standalone (no `transient`) → native min/max + its own taskbar entry, so
        it's alt-tab-able and never lost behind the main window. Singleton: a
        prior dialog is brought to front and — if dirty (streaming or user edits)
        — the user is asked before it's discarded. Returns the ctx dict, or None
        if the user declined to discard a dirty draft.
        """
        existing = self._dialog
        if existing is not None:
            try:
                alive = bool(existing.winfo_exists())
            except tk.TclError:
                alive = False
            if alive:
                existing.lift()
                try:
                    existing.focus_force()
                except tk.TclError:
                    pass
                if self._dirty and not messagebox.askyesno(
                        "Unsaved PR draft",
                        "The current PR draft is still generating or has unsaved "
                        "edits.\n\nDiscard it and start a new draft?",
                        parent=existing):
                    return None
                existing.destroy()
            self._dialog = None

        dlg = tk.Toplevel(self._root)
        self._dialog = dlg
        self._dirty = True            # streaming in progress
        dlg.title("PR Description Draft")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.minsize(620, 460)
        # NO transient() → standalone window with native min/max + a taskbar entry.
        dlg.lift()
        try:
            dlg.focus_force()
        except tk.TclError:
            pass

        def _on_destroy(e, _d=dlg):
            if e.widget is _d:
                self._dialog = None
                self._dirty = False
        dlg.bind("<Destroy>", _on_destroy, add="+")

        prog = [False]        # programmatic-insert guard (mutable for closures)
        streamed = [False]    # first real token clears the placeholder
        gh_exe = shutil.which("gh")

        # ── Header: status + grounding badge ──
        hdr = tk.Frame(dlg, bg=C["base"])
        status_var = tk.StringVar(value="Preparing…")
        tk.Label(hdr, textvariable=status_var, bg=C["base"], fg=C["blue"],
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=12, pady=(8, 0))
        grounded = bool(self._cfg.enable_pr_grounding and
                        (self._cfg.tokensave_exe or self._cfg.codegraph_exe))
        badge_var = tk.StringVar(
            value="✓ Grounded: tokensave + codegraph" if grounded else "not grounded")
        tk.Label(hdr, textvariable=badge_var, bg=C["base"],
                 fg=(C["green"] if grounded else C["overlay0"]),
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=12, pady=(8, 0))

        # ── Body: text + scrollbars in their own grid frame (corner-to-corner) ──
        body = tk.Frame(dlg, bg=C["base"])
        txt = tk.Text(body, wrap=tk.NONE, bg=C["mantle"], fg=C["text"],
                      font=("Consolas", 9), relief=tk.FLAT, padx=8, pady=6)
        vsb = ttk.Scrollbar(body, orient="vertical",   command=txt.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        txt.insert(
            tk.END,
            "⏳  Preparing your PR draft…\n\n"
            "Reading your branch diff and grounding context. On local models the "
            "first tokens can take 30–90s — the draft will stream in here as it "
            "writes. Watch the status line above for progress.\n")
        txt.configure(state=tk.DISABLED)

        # Dirty tracking — genuine user edits only (our inserts set prog[0]).
        def _on_modified(_e=None):
            if prog[0]:
                txt.edit_modified(False)
                return
            self._dirty = True
            txt.edit_modified(False)   # re-arm: <<Modified>> fires only on False→True
        txt.bind("<<Modified>>", _on_modified, add="+")

        # ── Title field ──
        # NB: padding goes on the .pack() call below, NOT here — a tuple pady in
        # a widget constructor raises TclError ("bad screen distance") on strict
        # Tk builds; pack/grid accept the 2-tuple form.
        title_row = tk.Frame(dlg, bg=C["base"])
        tk.Label(title_row, text="PR title:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT)
        title_var = tk.StringVar(value="")
        ttk.Entry(title_row, textvariable=title_var, width=60).pack(
            side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

        # ── Buttons (disabled until the draft completes) ──
        def _live_body():
            return txt.get("1.0", tk.END).rstrip()

        btn_row = tk.Frame(dlg, bg=C["base"], padx=12, pady=8)
        copy_btn = ttk.Button(btn_row, text="Copy to clipboard", state=tk.DISABLED,
                              command=lambda: (dlg.clipboard_clear(),
                                               dlg.clipboard_append(_live_body())))
        copy_btn.pack(side=tk.LEFT)
        create_btn = ttk.Button(
            btn_row, text="Create PR on GitHub", state=tk.DISABLED,
            command=lambda: self._create_pr_via_gh(
                gh_exe, path, title_var.get(), _live_body(), dlg))
        create_btn.pack(side=tk.LEFT, padx=(6, 0))
        open_btn = ttk.Button(
            btn_row, text="Open in Browser", state=tk.DISABLED,
            command=lambda: self._open_pr_via_gh(gh_exe, path, _live_body(), dlg))
        open_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

        # ── Test-gap panel mount point (filled on completion) ──
        gap_frame = tk.Frame(dlg, bg=C["base"])

        # Pack order: buttons + title pinned to bottom (always visible), gap panel
        # above them, header on top, body fills the remaining space.
        btn_row.pack(side=tk.BOTTOM, fill=tk.X)
        title_row.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 6))
        gap_frame.pack(side=tk.BOTTOM, fill=tk.X)
        hdr.pack(side=tk.TOP, fill=tk.X)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(6, 4))

        dlg.update_idletasks()
        w, h = 760, 560
        try:
            px = self._root.winfo_x() + (self._root.winfo_width()  - w) // 2
            py = self._root.winfo_y() + (self._root.winfo_height() - h) // 2
            dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            dlg.geometry(f"{w}x{h}")

        return {
            "dlg": dlg, "queue": queue.Queue(), "txt": txt, "vsb": vsb,
            "status_var": status_var, "badge_var": badge_var,
            "title_var": title_var, "gap_frame": gap_frame,
            "copy_btn": copy_btn, "create_btn": create_btn, "open_btn": open_btn,
            "gh_exe": gh_exe, "prog": prog, "streamed": streamed,
            "path": path, "base": base, "provider": provider,
            "phase": "Preparing…", "start": None,
        }

    def _poll_pr_stream(self, ctx: dict) -> None:
        """Drain streamed tokens from the queue into the dialog (Tk main thread).

        Single recursive after(50) loop (mirrors _poll_log_queue) with a bounded
        drain per tick so a token burst can't lock the event loop. Auto-scroll
        only when the user is already at the bottom.
        """
        dlg = ctx["dlg"]
        try:
            if not dlg.winfo_exists():
                return
        except tk.TclError:
            return
        q = ctx["queue"]
        txt = ctx["txt"]
        chunks: list = []
        budget = 0
        done = None
        try:
            while budget < 8000:        # bounded drain
                kind, data = q.get_nowait()
                if kind == "token":
                    chunks.append(data)
                    budget += len(data)
                elif kind == "status":
                    ctx["phase"] = self._pr_status_label(data)
                elif kind == "done":
                    done = data
                    break
        except queue.Empty:
            pass

        if chunks:
            try:
                at_bottom = ctx["vsb"].get()[1] >= 0.98
            except Exception:
                at_bottom = True
            ctx["prog"][0] = True
            txt.configure(state=tk.NORMAL)
            if not ctx["streamed"][0]:
                txt.delete("1.0", tk.END)        # clear the placeholder
                ctx["streamed"][0] = True
            txt.insert(tk.END, "".join(chunks))
            txt.configure(state=tk.DISABLED)
            txt.edit_modified(False)
            ctx["prog"][0] = False
            if at_bottom:
                txt.see(tk.END)

        # Live elapsed ticker until the first token arrives — local models spend
        # a long "prefill" reading the diff with NO output, which otherwise looks
        # frozen. Show the phase + seconds so the user can see it's alive.
        if done is None and not ctx["streamed"][0]:
            elapsed = int(time.monotonic() - ctx.get("start", time.monotonic()))
            hint = (" — the model is reading your diff; first tokens can take "
                    "30–90s on local models" if elapsed >= 8 else "")
            ctx["status_var"].set(f"{ctx['phase']}  ({elapsed}s){hint}")
        elif done is None:
            ctx["status_var"].set(ctx["phase"])

        if done is not None:
            self._finalize_pr_draft(ctx, done)
            return
        dlg.after(50, lambda: self._poll_pr_stream(ctx))

    def _finalize_pr_draft(self, ctx: dict, payload: dict) -> None:
        """Render the final body, enable actions, and attach the test-gap panel."""
        dlg = ctx["dlg"]
        try:
            if not dlg.winfo_exists():
                return
        except tk.TclError:
            return
        result, elapsed = payload["result"], payload["elapsed"]
        provider, base, path = ctx["provider"], ctx["base"], ctx["path"]

        # Failure modes — log, inform, and close the now-useless window.
        if payload["err"]:
            self._on_log(f"  ✗ PR draft failed ({elapsed}s): {payload['err']}", C["red"])
            messagebox.showerror("Draft PR — error", payload["err"], parent=dlg)
            dlg.destroy()
            return
        if result is None:
            reason = payload["diag"] or "LLM returned no output"
            self._on_log(f"  ✗ PR draft: no LLM response ({elapsed}s)", C["red"])
            messagebox.showerror(
                "Draft PR — no response from LLM",
                f"{reason}\n\nProvider: {provider}\nElapsed: {elapsed}s\n\n"
                "Check Settings → Commit Message LLM and verify:\n"
                "• The service is running\n"
                "• The model name is correct\n"
                "• The model is downloaded (ollama pull <model>)",
                parent=dlg)
            dlg.destroy()
            return
        if result.startswith("Empty diff"):
            self._on_log(f"  ✗ PR draft: empty diff ({elapsed}s)", C["yellow"])
            messagebox.showwarning(
                "Draft PR — no diff found",
                f"{result}\n\nBase branch used: {base!r}\n\n"
                "If this is wrong, right-click → Set PR base branch…\n"
                "to configure a different merge target.",
                parent=dlg)
            dlg.destroy()
            return

        # Success — replace the streamed raw text with the final processed body.
        txt = ctx["txt"]
        ctx["prog"][0] = True
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)
        txt.insert(tk.END, result)
        txt.edit_modified(False)
        ctx["prog"][0] = False
        # Editable now so the user can tweak before Create PR; dirty starts clean.
        self._dirty = False

        ctx["status_var"].set(
            f"✓ Draft ready ({elapsed}s) — review, edit, then Create PR")
        ctx["title_var"].set(_extract_pr_title(result))
        ctx["copy_btn"].configure(state=tk.NORMAL)
        if ctx["gh_exe"]:
            ctx["create_btn"].configure(state=tk.NORMAL)
            ctx["open_btn"].configure(state=tk.NORMAL)
            _Tooltip(ctx["create_btn"],
                     "Create the PR on GitHub directly. Edit the title above first.")
            _Tooltip(ctx["open_btn"],
                     "Open github.com's New PR page with this body pre-filled.")
        else:
            _Tooltip(ctx["create_btn"],
                     "GitHub CLI not on PATH. Install gh (cli.github.com) to enable.")
            _Tooltip(ctx["open_btn"],
                     "GitHub CLI not on PATH. Install gh (cli.github.com) to enable.")
        self._on_log(f"  ✓ PR draft ready ({elapsed}s)", C["green"])

        # Test-gap panel from the already-computed suggestions (no re-scan).
        # Closing a gap in the panel flips its body checklist line to [x].
        if base and payload.get("suggestions"):
            self._build_gap_panel_cb(
                ctx["gap_frame"], path, base,
                suggestions=payload["suggestions"],
                on_tests_written=lambda paths: self._apply_gap_progress_to_body(
                    ctx, paths))

    # ------------------------------------------------------------------

    def _open_pr_via_gh(self, gh_exe: str, path: str, body_text: str, dlg) -> None:
        """Write body to a temp file and spawn `gh pr create --web --body-file`.

        Using a temp file (rather than `--body` with the literal string) avoids
        Windows command-line length limits AND multi-line / quote escaping
        problems entirely. `--web` opens the GitHub New-PR page in the user's
        default browser with the body pre-filled; gh itself exits immediately
        after spawning the browser, so we don't need to capture output.

        Failures (missing remote, no commits to PR, gh auth not set up, etc.)
        surface as a messagebox so the user isn't left wondering why nothing
        happened.
        """
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".md",
                    prefix="pr-body-", delete=False) as f:
                f.write(body_text)
                tmp_path = f.name
        except OSError as e:
            messagebox.showerror(
                "Open PR on GitHub failed",
                f"Could not write temp body file: {e}",
                parent=dlg)
            return
        try:
            # cwd=path so `gh` picks up the project's repo / remote
            subprocess.Popen(
                [gh_exe, "pr", "create", "--web", "--body-file", tmp_path],
                cwd=path, creationflags=CREATE_NO_WINDOW)
            self._on_log(
                "  Opening GitHub New-PR page in your browser…", C["sky"])
        except OSError as e:
            messagebox.showerror(
                "Open PR on GitHub failed",
                f"Could not spawn gh: {e}",
                parent=dlg)
            return
        # NB: tmp_path is left on disk intentionally. gh reads it lazily after
        # the browser opens, so deleting it immediately would race. The OS will
        # clean it up from %TEMP% eventually.

    def _create_pr_via_gh(self, gh_exe: "str | None", path: str, title: str,
                          body_text: str, dlg) -> None:
        """Run `gh pr create` directly — no browser, PR is created immediately.

        Runs on a background thread; all UI callbacks are scheduled via dlg.after().
        """
        import tempfile, webbrowser

        title = title.strip()
        if not title:
            messagebox.showwarning("Create PR", "Enter a PR title first.", parent=dlg)
            return

        base = self._resolve_pr_base(path)
        if base is None:
            messagebox.showerror(
                "Create PR",
                "Could not detect base branch.\n"
                "Right-click the Draft PR button and choose\n"
                "'Set PR base branch…' to specify one manually, or\n"
                "push the branch and set a tracking upstream first.",
                parent=dlg)
            return

        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".md",
                    prefix="pr-body-", delete=False) as f:
                f.write(body_text)
                tmp_path = f.name
        except OSError as e:
            messagebox.showerror("Create PR", f"Could not write temp file: {e}", parent=dlg)
            return

        gh_base = base.split("/")[-1]  # strip "origin/" prefix; gh needs bare branch name

        def _run():
            try:
                result = subprocess.run(
                    [gh_exe, "pr", "create",
                     "--title", title,
                     "--body-file", tmp_path,
                     "--base", gh_base],
                    capture_output=True, text=True, encoding="utf-8",
                    cwd=path, creationflags=CREATE_NO_WINDOW, timeout=30)
            except subprocess.TimeoutExpired:
                dlg.after(0, lambda: messagebox.showerror(
                    "Create PR", "gh timed out. Check your network / gh auth status.",
                    parent=dlg))
                return
            except OSError as e:
                dlg.after(0, lambda msg=str(e): messagebox.showerror("Create PR", msg, parent=dlg))
                return

            if result.returncode != 0:
                err = (result.stderr or result.stdout or "unknown error").strip()
                dlg.after(0, lambda: messagebox.showerror(
                    "Create PR failed",
                    f"gh pr create exited {result.returncode}:\n\n{err[:600]}",
                    parent=dlg))
                return

            url = (result.stdout or "").strip()
            self._on_log(f"  PR created: {url}", C["green"])
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            dlg.after(0, lambda: _on_success(url))

        def _on_success(url: str):
            if messagebox.askyesno(
                    "PR Created",
                    f"Pull request created:\n{url}\n\nOpen in browser?",
                    parent=dlg):
                webbrowser.open(url)
            dlg.destroy()

        threading.Thread(target=_run, daemon=True).start()

