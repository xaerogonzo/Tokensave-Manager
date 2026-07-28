"""test_gap_ctrl.py — TestGapCtrl sub-controller.

Extracted from :class:`~controllers.git_tab.GitTabController` (Phase C2).
Owns all test-gap panel logic: building the panel UI (via :class:`_GapPanelCtx`),
generating stubs / AI-verified tests, and showing AI generation results.

The module-level :func:`_apply_gap_progress_to_body` is importable by
:mod:`pr_draft_ctrl` without creating a circular dependency.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from theme import _Tooltip, themed_checkbutton

if TYPE_CHECKING:
    from typing import Any, Callable
    from state import ManagerConfig


def _apply_gap_progress_body(ctx: dict, rel_paths: list) -> None:
    """Flip the Draft PR body's coverage-gap lines to [x] for *rel_paths*.

    Pure module-level function — does not use ``self``. Imported by
    :mod:`pr_draft_ctrl` to avoid a circular sub-controller dependency.
    Named ``_apply_gap_progress_body`` (not ``…_to_body``) to avoid a
    name collision with the same-named instance method on :class:`TestGapCtrl`
    that delegates to this function — the collision caused tokensave to report
    a false self-recursion and a false dead-code finding.

    Called when the embedded panel writes a passing test / stub. Edits only the
    matching ``- [ ]`` gap lines (see pr_draft._mark_gaps_addressed), guarded so
    it counts as a manager insert (not a user edit → no dirty flag) and can never
    disrupt the generation flow: a closed dialog, missing widget, or any glitch
    is a silent no-op.
    """
    try:
        txt = ctx.get("txt")
        if txt is None or not txt.winfo_exists():
            return
        from helpers.pr_draft import _mark_gaps_addressed
        current = txt.get("1.0", tk.END).rstrip("\n")
        updated = _mark_gaps_addressed(current, rel_paths)
        if updated == current:
            return
        yview = txt.yview()
        ctx["prog"][0] = True          # mark as our insert, not a user edit
        try:
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.insert("1.0", updated)
            txt.configure(state=tk.DISABLED)
            txt.edit_modified(False)
            txt.yview_moveto(yview[0])
        finally:
            ctx["prog"][0] = False
    except Exception:
        pass                            # never let a body refresh break generation


class _GapPanelCtx:
    """Transient state shared between the test-gap panel sub-methods.

    Stores all mutable widget references and data lists so the sub-methods
    (``_gap_panel_header``, ``_gap_panel_suggestions``, ``_gap_panel_actions``)
    remain portable — they can be moved to a ``TestGapCtrl`` sub-controller
    in Phase C2 without hunting down scattered ``self.*`` attribute look-ups
    on the parent controller.
    """

    __slots__ = (
        # Immutable wiring — set at construction, never reassigned
        "panel", "dlg", "path", "base", "on_tests_written",
        # Data lists — grown by _add_rows inside _gap_panel_suggestions
        "check_vars", "status_vars", "panel_suggestions",
        # Widget vars created by _gap_panel_header
        "ai_enabled_var", "backend_var", "_ai_available",
        # Shared state created by _gap_panel_actions
        "status_var", "cancel_event",
        # Action button refs (for bulk disable/enable in _do_rescan)
        "stub_btn", "ai_btn", "fail_btn",
        "cancel_btn", "copy_cc_btn", "rescan_btn",
    )

    def __init__(self, panel, dlg, path: str, base: str,
                 on_tests_written) -> None:
        self.panel = panel
        self.dlg = dlg
        self.path = path
        self.base = base
        self.on_tests_written = on_tests_written
        # mutable data
        self.check_vars: list = []
        self.status_vars: list = []
        self.panel_suggestions: list = []
        # widget vars/buttons — set by sub-methods
        self.ai_enabled_var = None
        self.backend_var = None
        self._ai_available = True   # set by _gap_panel_header; read by _gap_panel_actions
        self.status_var = None
        self.cancel_event = None
        self.stub_btn = None
        self.ai_btn = None
        self.fail_btn = None
        self.cancel_btn = None
        self.copy_cc_btn = None
        self.rescan_btn = None

    def reset_for_rebuild(self) -> None:
        """Clear mutable lists and button refs before a Re-scan rebuild."""
        self.check_vars.clear()
        self.status_vars.clear()
        self.panel_suggestions.clear()
        self.ai_enabled_var = None
        self.backend_var = None
        self.status_var = None
        self.cancel_event = None
        self.stub_btn = self.ai_btn = self.fail_btn = None
        self.cancel_btn = self.copy_cc_btn = self.rescan_btn = None



@dataclass(frozen=True)
class _GapAiRun:
    """Context for one AI-generate run.

    Lifted out of ``_gap_generate_ai`` — inner closures fold their branches
    into the parent's cyclomatic complexity (the ``generate_verified_test``
    / ``_VerifyCtx`` lesson), so the worker pipeline lives at module level
    and threads this frozen bundle instead of 12 loose parameters.
    """
    ctl:              "TestGapCtrl"
    selected:         list                  # [(row_idx, suggestion), ...]
    backend:          str
    root:             str                   # project root at button-press time
    cancel_event:     "threading.Event"
    dlg:              tk.Toplevel
    status_vars:      list
    status_var:       tk.StringVar
    stub_btn:         "Any"
    ai_btn:           "Any"
    fail_btn:         "Any"
    on_tests_written: "Callable | None"

    def set_row(self, idx: int, glyph: str) -> None:
        if self.dlg.winfo_exists():
            self.dlg.after(0, lambda: self.status_vars[idx].set(glyph))

    def set_status(self, text: str) -> None:
        if self.dlg.winfo_exists():
            self.dlg.after(0, lambda: self.status_var.set(text))


def _gap_ai_token_cb(run: _GapAiRun, idx: int):
    """Per-row liveness: ⏳ (prefill, no tokens) → ✍ N (generating).

    on_token fires on the worker thread, so marshal to Tk via dlg.after.
    Throttled (every 5th token) so a long local run doesn't flood the
    event loop. The final ✓/✗ glyph is set by set_row after completion.
    """
    counter = {"n": 0}

    def _cb(_delta: str) -> None:
        counter["n"] += 1
        n_tok = counter["n"]
        if (n_tok == 1 or n_tok % 5 == 0) and run.dlg.winfo_exists():
            run.dlg.after(0, lambda n=n_tok: run.status_vars[idx].set(f"✍ {n}"))

    return _cb


def _gap_ai_generate_one(run: _GapAiRun, k: int, n: int, idx: int, sg):
    """Generate + verify one suggestion; returns (result, is_update)."""
    from helpers.test_gen_llm import generate_verified_test

    is_update = bool(getattr(sg, "test_exists", False))
    run.set_row(idx, "⏳")
    run.set_status(f"Verifying {k}/{n}…  (generate → run → repair)")
    res = generate_verified_test(
        sg.source_path, run.root,
        backend=run.backend, cfg=run.ctl._cfg,
        cancel_event=run.cancel_event,
        template=getattr(sg, "template", None),
        allow_overwrite=is_update,
        target_path=(getattr(sg, "test_path", "") or None),
        on_token=_gap_ai_token_cb(run, idx),
    )
    return res, is_update


def _gap_ai_record_result(run: _GapAiRun, idx: int, sg, res,
                          is_update: bool, acc: dict) -> None:
    """Fold one VerifiedResult into the run accumulator + row glyph + log."""
    if res.status == "pass":
        acc["passed"] += 1
        acc["written_paths"].append(sg.rel_path)
        acc["written"].append((idx, sg, res.written_path, res.is_tk))
        # Partial pass: per-test pruning kept some, dropped the rest.
        glyph = (f"✓ ({res.kept}/{res.total})"
                 if res.kept and res.total and res.kept < res.total
                 else "✓")
        run.set_row(idx, glyph)
        if res.kept and res.kept < res.total:
            acc["partials"][sg.rel_path] = res.report   # written; what was dropped
        run.ctl._on_log(
            f"  ✓ AI test {'updated' if is_update else 'written'} + "
            f"passing ({glyph}): {sg.rel_path}", C["green"])
    else:
        acc["failed"] += 1
        # Distinguish a failed regenerate (original preserved) from a
        # failed new-file generate (nothing written).
        if res.preserved_existing:
            run.set_row(idx, "↻✗")
            acc["failures"][sg.rel_path] = (
                "[Update failed] the regenerated test failed the runtime "
                "gate (or dropped coverage); the original test file was "
                "preserved on disk.\n\n" + (res.report or res.status))
        else:
            run.set_row(idx, "✗")
            acc["failures"][sg.rel_path] = res.report or res.status
        run.ctl._on_log(
            f"  ✗ AI test discarded ({res.status}): {sg.rel_path}",
            C["yellow"])


def _gap_ai_worker(run: _GapAiRun) -> None:
    """Background pipeline: generate each selection, re-verify, hand off to done."""
    n = len(run.selected)
    acc = {"passed": 0, "failed": 0,
           "partials": {},          # WRITTEN but some tests dropped (✓ N/M)
           "failures": {},          # DISCARDED (isolation fail / gate / error)
           "written_paths": [],     # source rel_paths that now have a passing test
           "written": []}           # (idx, sg, test_relpath, is_tk) for re-verify
    for k, (idx, sg) in enumerate(run.selected, 1):
        if run.cancel_event.is_set():
            break
        res, is_update = _gap_ai_generate_one(run, k, n, idx, sg)
        if res.status == "cancelled":
            break
        _gap_ai_record_result(run, idx, sg, res, is_update, acc)

    # Re-verify the written NON-tk files against the FULL -m "not tk" gate and
    # roll back any that pass alone but fail in the real suite. (tk files are
    # deselected by that gate; they were hang-checked by the 20s isolation run.)
    gate_relevant = [w for w in acc["written"] if w[2] and not w[3]]
    if gate_relevant and not run.cancel_event.is_set():
        run.set_status("Re-verifying generated tests against the full suite…")
        dp, df = run.ctl._gap_reverify_and_rollback(
            gate_relevant, run.cancel_event, run.root,
            acc["written_paths"], acc["partials"], acc["failures"],
            run.set_row)
        acc["passed"] += dp
        acc["failed"] += df

    run.ctl._last_ai_partials = acc["partials"]
    run.ctl._last_ai_fail_reports = acc["failures"]
    if run.dlg.winfo_exists():
        run.dlg.after(0, lambda: _gap_ai_done(
            run, acc["passed"], acc["failed"], acc["written_paths"]))


def _gap_ai_done(run: _GapAiRun, passed: int, failed: int,
                 written_paths: list) -> None:
    """Main-thread completion: re-enable buttons, summarise, notify."""
    if not run.dlg.winfo_exists():
        return
    run.stub_btn.configure(state=tk.NORMAL)
    run.ai_btn.configure(state=tk.NORMAL)
    # Enable "View failures…" if anything failed OR a partial pass dropped
    # some tests (both are stashed for the viewer).
    has_reports = bool(getattr(run.ctl, "_last_ai_fail_reports", None)
                       or getattr(run.ctl, "_last_ai_partials", None))
    run.fail_btn.configure(state=(tk.NORMAL if has_reports else tk.DISABLED))
    if run.cancel_event.is_set():
        run.status_var.set(f"Cancelled — {passed} ✓ / {failed} ✗ so far.")
    else:
        run.status_var.set(
            f"Done: {passed} ✓ / {failed} ✗ — passing (incl. pruned-partial) "
            "tests were written.")
    # Reflect closed gaps in the PR-body checklist (Draft PR dialog only).
    if run.on_tests_written and written_paths:
        run.on_tests_written(written_paths)
    # Refresh Test Manager if still open and same project
    ctl = run.ctl
    if (run.root == ctl._git_path
            and ctl._test_manager_ref is not None
            and ctl._test_manager_ref.winfo_exists()):
        try:
            ctl._test_manager_ref.refresh_coverage()
        except Exception:
            pass


class TestGapCtrl:
    __test__ = False  # prevent pytest from collecting this class
    """Owns all test-gap panel logic, extracted from GitTabController.

    Constructor callbacks decouple it from the parent controller:
    ``get_path`` returns the current project path (mirrors
    ``GitTabController._git_path``), and ``get_test_manager_ref`` returns
    the live :class:`~dialogs.test_manager.TestManagerDialog` (or None).

    Call :meth:`disconnect` during teardown to break reference cycles.
    """

    def __init__(
        self,
        tab: "tk.Frame",
        cfg: "ManagerConfig",
        on_log: "Callable",
        get_path: "Callable[[], str | None]",
        get_test_manager_ref: "Callable[[], Any]",
    ) -> None:
        self._tab = tab
        self._cfg = cfg
        self._on_log = on_log
        self.get_path = get_path
        self.get_test_manager_ref = get_test_manager_ref
        # AI failure tracking — populated by _gap_generate_ai
        self._last_ai_partials: dict = {}
        self._last_ai_fail_reports: dict = {}

    @property
    def _root(self):
        return self._tab.winfo_toplevel()

    @property
    def _git_path(self) -> "str | None":
        """Live project path — mirrors GitTabController._git_path via callback."""
        return self.get_path()

    @property
    def _test_manager_ref(self):
        """Live TestManagerDialog ref — read-only mirror via callback."""
        return self.get_test_manager_ref()

    def disconnect(self) -> None:
        """Clear callback references to prevent reference cycles on teardown."""
        self.get_path = None
        self.get_test_manager_ref = None

    def _apply_gap_progress_to_body(self, ctx: dict, rel_paths: list) -> None:
        """Instance delegate to the module-level helper; ``self`` is unused.

        Keeping this as an instance method preserves the unbound-call pattern
        ``TestGapCtrl._apply_gap_progress_to_body(object(), ctx, paths)``
        used in :mod:`tests.test_pr_gap_body_refresh`.
        """
        _apply_gap_progress_body(ctx, rel_paths)

    # ── Public aliases used by GitTabController thin delegates ────────────────

    def build_gap_panel(self, dlg, path: str, base: str,
                        suggestions=None, on_tests_written=None) -> None:
        """Public entry point → :meth:`_build_test_gap_panel`."""
        self._build_test_gap_panel(dlg, path, base, suggestions, on_tests_written)

    def open_test_gaps_window(self, path: str, base: str,
                              suggestions=None) -> None:
        """Public entry point → :meth:`_open_test_gaps_window`."""
        self._open_test_gaps_window(path, base, suggestions)

    def _open_test_gaps_window(self, path: str, base: str,
                               suggestions: "list | None" = None) -> None:
        """Open the standalone 🧪 Test Gaps window for *path* vs *base*.

        Shared by `cmd_show_test_gaps` (async scan) and the Claude CLI draft
        path (passes pre-computed *suggestions* so there's no second scan).
        """
        dlg = tk.Toplevel(self._root)
        dlg.title(f"🧪 Test Gaps — {os.path.basename(path)} vs {base.split('/')[-1]}")
        dlg.configure(bg=C["base"])
        dlg.resizable(True, True)
        dlg.minsize(560, 200)
        # NO transient() → standalone window with native minimize/maximize buttons
        # and its own taskbar entry (transient windows get only a close button on
        # Windows). Mirrors the PR-draft dialog.

        # Header showing what we're diffing
        hdr_row = tk.Frame(dlg, bg=C["base"])
        hdr_row.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(
            hdr_row,
            text=f"Changed files on this branch vs  {base.split('/')[-1]}",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT)

        # The test gap panel fills the rest of the dialog
        self._build_test_gap_panel(dlg, path, base, suggestions=suggestions)

        ttk.Button(dlg, text="Close",
                   command=dlg.destroy).pack(side=tk.BOTTOM, anchor=tk.E,
                                             padx=12, pady=(4, 10))

        dlg.update_idletasks()
        w, h = 620, 300
        try:
            px = self._root.winfo_x() + (self._root.winfo_width()  - w) // 2
            py = self._root.winfo_y() + (self._root.winfo_height() - h) // 2
            dlg.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            dlg.geometry(f"{w}x{h}")


    def _build_test_gap_panel(self, dlg, path: str, base: str,
                              suggestions: "list | None" = None,
                              on_tests_written=None) -> None:
        """Attach a 🧪 test-gap panel to *dlg* (Toplevel or Frame).

        If *suggestions* is provided (already computed by the caller), the panel
        fills synchronously with no extra coverage scan. Otherwise it runs
        ``suggest_tests_for_diff`` on a background thread and reveals the panel
        only if untested changed files are found.

        *on_tests_written* — optional callback ``(rel_paths: list[str]) -> None``
        invoked with the files that got a test written (AI ✓ or a fresh stub).
        The Draft PR dialog passes this to flip the body's coverage-gaps checklist
        to ``[x]``; the standalone window passes nothing (no-op).

        All widget-building is delegated to ``_gap_fill_panel`` and its three
        sub-methods; shared mutable state lives in a :class:`_GapPanelCtx`.
        """
        import threading

        panel = tk.Frame(dlg, bg=C["surface0"], relief=tk.FLAT, bd=1)
        ctx = _GapPanelCtx(panel=panel, dlg=dlg, path=path, base=base,
                           on_tests_written=on_tests_written)

        def _populate(suggs: list) -> None:
            if not dlg.winfo_exists() or not suggs:
                return
            panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
            self._gap_fill_panel(panel, ctx, suggs)

        def _fetch() -> None:
            from helpers.test_gap_report import suggest_tests_for_diff
            try:
                suggs = suggest_tests_for_diff(path, self._cfg.git_exe, base)
            except Exception:
                suggs = []
            if dlg.winfo_exists():
                dlg.after(0, lambda s=suggs: _populate(s))

        if suggestions is not None:
            _populate(suggestions)
        else:
            threading.Thread(target=_fetch, daemon=True).start()

    def _gap_fill_panel(self, parent: tk.Frame, ctx: "_GapPanelCtx",
                        suggestions: list) -> None:
        """Build all gap-panel widgets inside *parent* using the three sub-builders.

        Separated from ``_build_test_gap_panel`` so ``_do_rescan`` can call it
        again on the same panel frame after clearing it, passing a fresh
        suggestions list without recreating the ``_GapPanelCtx``.
        """
        self._gap_panel_header(parent, ctx, len(suggestions))
        self._gap_panel_suggestions(parent, ctx, suggestions)
        self._gap_panel_actions(parent, ctx)

    def _gap_panel_header(self, parent: tk.Frame, ctx: "_GapPanelCtx",
                          n_suggestions: int) -> None:
        """Build the header row: count label, AI toggle, backend selector, nudge.

        Stores ``ctx.ai_enabled_var`` and ``ctx.backend_var`` so the action
        sub-method can read them.  Owns its own pack calls — nothing in the
        coordinator post-configures layout details here.
        """
        # Header row
        hdr = tk.Frame(parent, bg=C["surface0"])
        hdr.pack(fill=tk.X, padx=8, pady=(6, 2))
        tk.Label(
            hdr,
            text=f"🧪  {n_suggestions} changed file(s) have no tests",
            font=("Segoe UI", 9, "bold"),
            bg=C["surface0"], fg=C["yellow"],
        ).pack(side=tk.LEFT)

        # AI master switch
        ai_available = bool(
            getattr(self._cfg, "claude_cli_exe", "") or
            self._cfg.raw.get("commit_message_llm", {}).get("provider")
        )
        ctx.ai_enabled_var = tk.BooleanVar(value=ai_available)
        ai_chk = themed_checkbutton(
            hdr,
            text="Enable AI generation",
            variable=ctx.ai_enabled_var,
            bg=C["surface0"], fg=C["subtext"],
            activebackground=C["surface0"],
            font=("Segoe UI", 9),
            state=tk.NORMAL if ai_available else tk.DISABLED,
        )
        ai_chk.pack(side=tk.RIGHT)

        # AI backend selector (persisted) — Auto / Claude CLI / Ollama.
        _cli_ok = bool(getattr(self._cfg, "claude_cli_exe", ""))
        _llm_ok = bool(self._cfg.raw.get("commit_message_llm", {}).get("provider"))
        ctx.backend_var = tk.StringVar(
            value=(self._cfg.raw.get("test_gen_backend") or "auto"))
        if ctx.backend_var.get() == "claude_cli" and not _cli_ok:
            ctx.backend_var.set("auto")
        elif ctx.backend_var.get() == "llm" and not _llm_ok:
            ctx.backend_var.set("auto")
        ctx.backend_var.trace_add(
            "write", lambda *_a: self._cfg.raw.__setitem__(
                "test_gen_backend", ctx.backend_var.get()))
        be_row = tk.Frame(parent, bg=C["surface0"])
        be_row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Label(be_row, text="AI backend:", bg=C["surface0"], fg=C["subtext"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
        for _bval, _blabel, _ok in (
                ("auto", "Auto", _cli_ok or _llm_ok),
                ("claude_cli", "Claude CLI", _cli_ok),
                ("llm", "Ollama", _llm_ok)):
            ttk.Radiobutton(
                be_row, text=_blabel, value=_bval, variable=ctx.backend_var,
                state=(tk.NORMAL if _ok else tk.DISABLED),
            ).pack(side=tk.LEFT, padx=(0, 6))

        # Nudge label
        tk.Label(
            parent,
            text=(f"{n_suggestions} changed file(s) lack tests — click "
                  "✓ Recommend (or pick files), then 📝 Generate stubs / "
                  "✨ AI generate to close the gap."),
            font=("Segoe UI", 8), bg=C["surface0"], fg=C["subtext"],
            anchor="w", justify=tk.LEFT, wraplength=560,
        ).pack(fill=tk.X, padx=10, pady=(0, 2))

        # Store ai_available so _gap_panel_actions can apply the tooltip.
        ctx._ai_available = ai_available

    def _gap_panel_suggestions(self, parent: tk.Frame, ctx: "_GapPanelCtx",
                               suggestions: list) -> None:
        """Build the scrollable file-checkbox list and start the update fetch thread.

        Populates ``ctx.check_vars``, ``ctx.status_vars``, and
        ``ctx.panel_suggestions`` via the inner ``_add_rows`` helper.
        The canvas + scrollbar bindings are fully self-contained here.
        """
        import threading

        # Scrollable checkbox list
        scroll_outer = tk.Frame(parent, bg=C["surface0"])
        scroll_outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=2)

        canvas = tk.Canvas(scroll_outer, bg=C["surface0"],
                           highlightthickness=0, height=160)
        vsb = ttk.Scrollbar(scroll_outer, orient="vertical",
                            command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        list_frame = tk.Frame(canvas, bg=C["surface0"])
        _cw = canvas.create_window((0, 0), window=list_frame, anchor="nw")

        # Self-contained scroll bindings.
        list_frame.bind("<Configure>",
                        lambda e, _c=canvas: _c.configure(
                            scrollregion=_c.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e, _c=canvas, _id=_cw: _c.itemconfig(
                        _id, width=e.width))
        for _w in (canvas, list_frame):
            _w.bind("<MouseWheel>",
                    lambda e, _c=canvas: _c.yview_scroll(
                        int(-1 * (e.delta / 120)), "units"))

        # _add_rows appends to ctx lists so action buttons (built later)
        # always see the live state, including rows added by the update thread.
        def _add_rows(sugg_list: list, is_update: bool = False) -> None:
            for sg in sugg_list:
                ctx.panel_suggestions.append(sg)
                var = tk.BooleanVar(value=False)
                ctx.check_vars.append(var)
                svar = tk.StringVar(value="")  # per-row status glyph
                ctx.status_vars.append(svar)
                row = tk.Frame(list_frame, bg=C["surface0"])
                row.pack(fill=tk.X, pady=1)
                themed_checkbutton(
                    row, variable=var, text=sg.rel_path,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface0"],
                    font=("Consolas", 8), anchor="w",
                ).pack(side=tk.LEFT)
                _tag = (f"↻ regenerate ({sg.template})" if is_update
                        else f"→ {sg.template}")
                tk.Label(row, text=_tag, bg=C["surface0"],
                         fg=(C["sky"] if is_update else C["overlay0"]),
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 0))
                tk.Label(row, textvariable=svar, bg=C["surface0"],
                         fg=C["subtext"], font=("Segoe UI", 9)).pack(
                             side=tk.LEFT, padx=(6, 0))

        _add_rows(suggestions, is_update=False)

        # Fetch changed-but-tested files on a thread (regenerate candidates).
        def _fetch_updates() -> None:
            try:
                from helpers.test_gap_report import suggest_test_updates_for_diff
                ups = suggest_test_updates_for_diff(
                    ctx.path, self._cfg.git_exe, ctx.base)
            except Exception:
                ups = []
            if ups and ctx.dlg.winfo_exists():
                ctx.dlg.after(0, lambda u=ups: _add_rows(u, is_update=True))

        threading.Thread(target=_fetch_updates, daemon=True).start()

    def _gap_panel_actions(self, parent: tk.Frame,
                           ctx: "_GapPanelCtx") -> None:
        """Build the status line, quick-select row, and all action buttons.

        Reads ctx.ai_enabled_var, ctx.backend_var, ctx.check_vars,
        ctx.status_vars, and ctx.panel_suggestions (set by the preceding
        sub-methods).  Stores every button reference into ctx so _do_rescan
        can bulk-disable them without name look-ups.
        """
        import threading

        # Shared status StringVar
        ctx.status_var = tk.StringVar()

        # Quick-select row
        def _select_recommended() -> None:
            rec = [bool(sg.requires_automation and not sg.test_exists)
                   for sg in ctx.panel_suggestions]
            for v, r in zip(ctx.check_vars, rec):
                v.set(r)
            ctx.status_var.set(
                f"Recommended {sum(rec)} new high-ROI helper(s). "
                "↻ existing-test regenerations are opt-in — check them by hand.")

        def _select_all() -> None:
            for v in ctx.check_vars:
                v.set(True)
            ctx.status_var.set(f"Selected all {len(ctx.check_vars)}.")

        def _select_none() -> None:
            for v in ctx.check_vars:
                v.set(False)
            ctx.status_var.set("Selection cleared.")

        qs_row = tk.Frame(parent, bg=C["surface0"])
        qs_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        tk.Label(qs_row, text="Select:", bg=C["surface0"], fg=C["subtext"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(2, 4))
        ttk.Button(qs_row, text="✓ Recommend",
                   command=_select_recommended).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(qs_row, text="All",
                   command=_select_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(qs_row, text="None",
                   command=_select_none).pack(side=tk.LEFT, padx=(0, 4))

        # Action buttons
        act_row = tk.Frame(parent, bg=C["surface0"])
        act_row.pack(fill=tk.X, padx=8, pady=(4, 6))

        tk.Label(act_row, textvariable=ctx.status_var,
                 bg=C["surface0"], fg=C["subtext"],
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)

        ctx.cancel_event = threading.Event()

        ctx.stub_btn = ttk.Button(
            act_row, text="📝 Generate stubs",
            command=lambda: self._gap_generate_stubs(
                ctx.panel_suggestions, ctx.check_vars, ctx.path,
                ctx.status_var, ctx.stub_btn, ctx.ai_btn,
                ctx.cancel_event, ctx.dlg,
                on_tests_written=ctx.on_tests_written),
        )
        ctx.stub_btn.pack(side=tk.RIGHT, padx=(4, 0))

        ctx.fail_btn = ttk.Button(
            act_row, text="View failures…", state=tk.DISABLED,
            command=lambda: self._show_ai_failures(ctx.dlg),
        )
        ctx.fail_btn.pack(side=tk.RIGHT, padx=(4, 0))

        ctx.ai_btn = ttk.Button(
            act_row, text="✨ AI generate selected",
            command=lambda: self._gap_generate_ai(
                ctx.panel_suggestions, ctx.check_vars, ctx.status_vars,
                ctx.path, ctx.status_var, ctx.stub_btn, ctx.ai_btn,
                ctx.fail_btn, ctx.ai_enabled_var, ctx.cancel_event,
                ctx.dlg, ctx.backend_var,
                on_tests_written=ctx.on_tests_written),
        )
        ctx.ai_btn.pack(side=tk.RIGHT, padx=(4, 0))

        ctx.cancel_btn = ttk.Button(
            act_row, text="Cancel",
            command=ctx.cancel_event.set,
        )
        ctx.cancel_btn.pack(side=tk.RIGHT, padx=(4, 0))

        ctx.copy_cc_btn = ttk.Button(
            act_row, text="📋 Copy Claude Code prompt",
            command=lambda: self._gap_copy_claude_prompt(
                ctx.panel_suggestions, ctx.check_vars, ctx.path,
                ctx.status_var, ctx.dlg),
        )
        ctx.copy_cc_btn.pack(side=tk.LEFT, padx=(0, 4))

        # Re-scan: clears panel, resets ctx, rebuilds via _gap_fill_panel.
        def _do_rescan() -> None:
            for _b in (ctx.stub_btn, ctx.ai_btn, ctx.fail_btn,
                       ctx.cancel_btn, ctx.copy_cc_btn, ctx.rescan_btn):
                if _b is not None:
                    _b.configure(state=tk.DISABLED)
            ctx.status_var.set("Re-scanning gaps…")

            def _work() -> None:
                from helpers.test_gap_report import suggest_tests_for_diff
                try:
                    fresh = suggest_tests_for_diff(
                        ctx.path, self._cfg.git_exe, ctx.base)
                except Exception:
                    fresh = []

                def _rebuild() -> None:
                    if not ctx.panel.winfo_exists():
                        return
                    for w in ctx.panel.winfo_children():
                        w.destroy()
                    ctx.reset_for_rebuild()
                    if fresh:
                        self._gap_fill_panel(ctx.panel, ctx, fresh)
                    else:
                        tk.Label(
                            ctx.panel,
                            text="✓ No remaining coverage gaps on this branch.",
                            bg=C["surface0"], fg=C["green"],
                            font=("Segoe UI", 9),
                        ).pack(padx=10, pady=10)

                if ctx.dlg.winfo_exists():
                    ctx.dlg.after(0, _rebuild)

            threading.Thread(target=_work, daemon=True).start()

        ctx.rescan_btn = ttk.Button(
            act_row, text="↻ Re-scan gaps", command=_do_rescan)
        ctx.rescan_btn.pack(side=tk.LEFT, padx=(4, 0))

        # Apply AI-unavailable state now that ai_btn exists.
        if not getattr(ctx, "_ai_available", True):
            ctx.ai_btn.configure(state=tk.DISABLED)
            _Tooltip(ctx.ai_btn,
                     "Configure Claude Code CLI or an LLM provider in Settings "
                     "to enable AI test generation.")

    def _gap_copy_claude_prompt(self, suggestions: list, check_vars: list,
                                path: str, status_var: tk.StringVar, dlg) -> None:
        """Copy a paste-into-Claude-Code prompt for the checked gaps.

        Claude Code is agentic (writes → runs pytest → fixes to green), unlike the
        manager's one-shot `--print` path — so we hand off and let the user re-scan.
        """
        from helpers.test_gen_llm import build_claude_code_handoff_prompt
        selected = [sg for sg, v in zip(suggestions, check_vars) if v.get()]
        if not selected:
            status_var.set("Nothing selected — check the files you want first.")
            return
        prompt = build_claude_code_handoff_prompt(selected, path)
        try:
            dlg.clipboard_clear()
            dlg.clipboard_append(prompt)
        except tk.TclError:
            status_var.set("Clipboard unavailable.")
            return
        status_var.set(
            f"Copied a Claude Code prompt for {len(selected)} file(s) — paste it into "
            "Claude Code, let it write + verify the tests, then click ↻ Re-scan gaps.")

    def _gap_generate_stubs(
        self, suggestions: list, check_vars: list, path: str,
        status_var: tk.StringVar, stub_btn, ai_btn,
        cancel_event: "threading.Event", dlg: tk.Toplevel,
        on_tests_written=None,
    ) -> None:
        """Generate template stubs for all checked entries."""
        import threading
        from helpers.test_scaffold import generate_test_file

        selected = [sg for sg, v in zip(suggestions, check_vars) if v.get()]
        if not selected:
            status_var.set("Nothing selected.")
            return

        stub_btn.configure(state=tk.DISABLED)
        ai_btn.configure(state=tk.DISABLED)
        status_var.set("Generating…")
        captured_root = path   # snapshot project root at button-press time

        def _run():
            ok, skipped = [], []
            for sg in selected:
                if cancel_event.is_set():
                    break
                try:
                    generate_test_file(captured_root, sg.source_path, sg.template)
                    ok.append(sg.rel_path)
                except FileExistsError:
                    skipped.append(sg.rel_path)
                except Exception as exc:
                    skipped.append(f"{sg.rel_path} ({exc})")
            if dlg.winfo_exists():
                dlg.after(0, lambda: _done(ok, skipped))

        def _done(ok: list, skipped: list) -> None:
            if not dlg.winfo_exists():
                return
            stub_btn.configure(state=tk.NORMAL)
            ai_btn.configure(state=tk.NORMAL)
            msg_parts = []
            if ok:
                msg_parts.append(f"Created: {', '.join(ok)}")
            if skipped:
                msg_parts.append(f"Skipped (already exist): {', '.join(skipped)}")
            status_var.set(" | ".join(msg_parts) or "Done.")
            # A fresh stub file also closes the "no test file" gap.
            if on_tests_written and ok:
                on_tests_written(ok)
            # Refresh Test Manager coverage view if open and same project
            if (self._test_manager_ref is not None
                    and self._test_manager_ref.winfo_exists()):
                try:
                    self._test_manager_ref.refresh_coverage()
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()


    def _gap_generate_ai(
        self, suggestions: list, check_vars: list, status_vars: list, path: str,
        status_var: tk.StringVar, stub_btn, ai_btn, fail_btn,
        ai_enabled_var: tk.BooleanVar,
        cancel_event: "threading.Event", dlg: tk.Toplevel,
        backend_var: "tk.StringVar | None" = None,
        on_tests_written=None,
    ) -> None:
        """Generate + VERIFY a test per checked entry; keep only passing tests.

        Each selected file goes through generate_verified_test: generate (valid
        Python guaranteed) → run under pytest → repair once on failure → write
        only if it passes, else discard. Per-row ⏳/✓/✗ + a summary; discarded
        files' pytest output is stashed for the "View failures…" button.
        """
        if not ai_enabled_var.get():
            status_var.set("AI generation is disabled.")
            return

        selected = [(i, sg) for i, (sg, v) in enumerate(zip(suggestions, check_vars))
                    if v.get()]
        if not selected:
            status_var.set("Nothing selected.")
            return

        cancel_event.clear()           # fresh run (Event is reused across clicks)
        for btn in (stub_btn, ai_btn, fail_btn):
            btn.configure(state=tk.DISABLED)
        run = _GapAiRun(
            ctl=self,
            selected=selected,
            backend=(backend_var.get() if backend_var is not None else "auto"),
            root=path,                 # snapshot project root at button-press time
            cancel_event=cancel_event,
            dlg=dlg,
            status_vars=status_vars,
            status_var=status_var,
            stub_btn=stub_btn,
            ai_btn=ai_btn,
            fail_btn=fail_btn,
            on_tests_written=on_tests_written,
        )
        threading.Thread(target=lambda: _gap_ai_worker(run),
                         daemon=True).start()

    def _gap_reverify_and_rollback(
        self,
        gate_relevant: list,
        cancel_event: "threading.Event",
        captured_root: str,
        written_paths: list,
        partials: dict,
        failures: dict,
        set_row_cb,
    ) -> "tuple[int, int]":
        """Re-run written non-tk tests against the full pytest suite; roll back failures.

        Called from the ``_run`` worker inside :meth:`_gap_generate_ai` after the
        per-file generation loop completes.  Extracted to reduce that method's CC.

        Args:
            gate_relevant:  List of ``(idx, sg, test_relpath, is_tk)`` tuples — only
                            non-tk files that have a written path.
            cancel_event:   The shared cancellation event; checked before each verdict.
            captured_root:  Absolute project root path (snapshot at button-press time).
            written_paths:  Mutable list of source rel-paths — rolled-back entries removed.
            partials:       Mutable dict of partial-pass reports — rolled-back entry cleared.
            failures:       Mutable dict of failure reports — rolled-back entries added.
            set_row_cb:     ``_set_row(idx, glyph)`` closure from the outer worker.

        Returns:
            ``(delta_passed, delta_failed)`` — caller adds these to its running totals.
        """
        from helpers.test_gen_llm import reverify_against_suite
        verdicts = reverify_against_suite(captured_root,
                                          [w[2] for w in gate_relevant])
        delta_passed = 0
        delta_failed = 0
        for (idx, sg, _wpath, _tk) in gate_relevant:
            if verdicts.get(_wpath) != "rolled_back":
                continue
            delta_passed -= 1
            delta_failed += 1
            set_row_cb(idx, "✗")
            partials.pop(sg.rel_path, None)
            failures[sg.rel_path] = (
                'This test PASSED alone but FAILED in the full `pytest -m '
                '"not tk"` suite (context-dependent) — discarded to keep the '
                'gate green. See manager.log for the failing ids.')
            if sg.rel_path in written_paths:
                written_paths.remove(sg.rel_path)
            try:                       # reset row model → no stale deleted path
                sg.test_exists = False
                sg.test_path = ""
            except Exception:
                pass
            self._on_log(
                f"  ✗ rolled back (failed full suite): {sg.rel_path}",
                C["yellow"])
        return delta_passed, delta_failed

    def _show_ai_failures(self, parent) -> None:
        """Read-only window: written-with-drops (informational) + discarded (failed)."""
        from tkinter import scrolledtext
        partials = getattr(self, "_last_ai_partials", {}) or {}
        failures = getattr(self, "_last_ai_fail_reports", {}) or {}
        win = tk.Toplevel(parent)
        win.title("AI test generation — results")
        win.configure(bg=C["base"])
        win.geometry("800x540")
        st = scrolledtext.ScrolledText(
            win, wrap=tk.NONE, bg=C["mantle"], fg=C["text"],
            font=("Consolas", 9), relief=tk.FLAT, padx=8, pady=6)
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
        if not partials and not failures:
            st.insert(tk.END, "No failures or dropped tests recorded for the last run.")
        else:
            if partials:
                st.insert(tk.END,
                          f"WRITTEN — {len(partials)} file(s) saved with some tests "
                          "dropped (the file passed; only the listed tests were "
                          "removed). Informational — no action needed.\n\n")
                for rel, rep in partials.items():
                    st.insert(tk.END, f"{'=' * 72}\n✓ {rel}\n{'=' * 72}\n{rep}\n\n")
            if failures:
                st.insert(tk.END,
                          f"\nDISCARDED — {len(failures)} file(s) failed and were NOT "
                          "written. Fix the source or the test, then re-run AI "
                          "generate.\n\n")
                for rel, rep in failures.items():
                    st.insert(tk.END, f"{'=' * 72}\n✗ {rel}\n{'=' * 72}\n{rep}\n\n")
        st.configure(state=tk.DISABLED)            # read-only
        ttk.Button(win, text="Close", command=win.destroy).pack(
            side=tk.BOTTOM, anchor=tk.E, padx=10, pady=(0, 10))


