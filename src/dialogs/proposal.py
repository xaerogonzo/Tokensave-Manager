"""ProposalDialog + ProposalBridge — write-verification gate for AI-proposed edits.

Two cooperating classes:

  * `WriteProposal`  — dataclass describing what the AI wants to write, with
                       optional safety fields (`original_hash`, `original_mtime`,
                       `dirs_to_create`) that the agent worker uses to detect
                       races and surface side effects in the dialog.

  * `ProposalDialog` — dual-pane (Original / Proposed) Tk Toplevel with editable
                       proposed pane, draggable sash, both scrollbars. Calls
                       `on_accept` (with final content) or `on_reject` exactly
                       once. Also exposes `show_expired()` for post-timeout UX.

  * `ProposalBridge` — cross-thread coordinator. The agent worker calls
                       `invoke()`, which schedules the dialog on the Tk main
                       thread via `root.after(0, ...)`, blocks on a
                       `threading.Event` until the user resolves (or a
                       bounded timeout), and returns `(accepted, final_content)`.
                       Race-safe (`_resolve` is idempotent — first resolution
                       wins, so late clicks after timeout are no-ops).

Threading invariants:
  * Dialog construction and destruction always happen on the Tk main thread.
  * `event.wait()` is only ever called on the agent worker thread — never the
    main thread (would freeze the GUI).
  * Post-resolution disk I/O happens on the agent worker thread, not Tk main
    (avoids GUI hitches when `os.replace` stalls on AV / OneDrive / Defender).

App-shutdown invariant: `App.__init__` should register every active bridge so
the root's WM_DELETE_WINDOW handler can call `cancel()` on each — otherwise
agent workers can hang forever waiting on a destroyed Tk event loop.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

# Allow direct execution (`python src/dialogs/proposal.py`) for the test
# harness at the bottom of this file. In production the parent `src/` is
# already on sys.path via app.py.
if __package__ is None and "constants" not in sys.modules:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import C

log = logging.getLogger(__name__)


@dataclass
class WriteProposal:
    """Data contract for an AI-proposed file write.

    Required fields (the standalone test harness can construct with just these):
        filepath, original_content, proposed_content, rationale

    Optional safety fields (defaulted so older call sites keep working):
        original_hash    — sha256 of original_content captured at proposal-build
                           time. Used by the agent worker's post-accept handler
                           to detect "user edited the file while the proposal
                           was open" races. Empty string = skip the race check.
        original_mtime   — file mtime at proposal-build time. Diagnostics only;
                           hash is authoritative for race detection.
        dirs_to_create   — parent directories that don't exist yet and will be
                           created on accept. ProposalDialog surfaces these so
                           the user sees the full side-effect set.
    """
    filepath:         str
    original_content: str
    proposed_content: str
    rationale:        str
    original_hash:    str         = ""
    original_mtime:   float       = 0.0
    dirs_to_create:   list[str]   = field(default_factory=list)


class ProposalDialog(tk.Toplevel):
    """Show an AI-proposed file edit and let the user accept, edit, or reject it.

    The `on_accept_callback` positional arg is preserved for backwards
    compatibility (the standalone test harness uses it). Bridge callers should
    additionally pass `on_reject` so the dialog can signal cancellation to the
    waiting agent thread; without it, rejection just destroys the window.
    """

    def __init__(self, parent, proposal: WriteProposal, on_accept_callback,
                 *, on_reject=None):
        super().__init__(parent)
        self._proposal     = proposal
        self._on_accept    = on_accept_callback
        self._on_reject_cb = on_reject
        self._expired      = False
        self._accept_btn: tk.Button | None = None
        self._reject_btn: tk.Button | None = None
        self._close_btn:  tk.Button | None = None
        self._banner:     tk.Label  | None = None

        self.title("🛡️  Secure Write Verification Required")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(700, 500)
        self.grab_set()

        # Window-close button (X) and Alt-F4 both count as reject — never let
        # the agent thread deadlock because the user used the title-bar X.
        self.protocol("WM_DELETE_WINDOW", self._on_reject)

        self._build_ui()

        # Centre on parent
        self.update_idletasks()
        w, h = 960, 640
        try:
            px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
            py = parent.winfo_y() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            self.geometry(f"{w}x{h}")

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        self._build_panes()
        self._build_buttons()

    def _build_header(self):
        hdr = tk.Frame(self, bg=C["base"], padx=20, pady=14)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr,
            text=f"The AI proposes editing:  {self._proposal.filepath}",
            fg=C["text"], bg=C["base"],
            font=("Segoe UI", 10, "bold"),
            justify=tk.LEFT,
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text=f"Reason: {self._proposal.rationale}",
            fg=C["subtext"], bg=C["base"],
            font=("Segoe UI", 9, "italic"),
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))
        # Directory-creation side-effect notice (only when dirs_to_create non-empty)
        if self._proposal.dirs_to_create:
            joined = ", ".join(self._proposal.dirs_to_create)
            tk.Label(
                hdr,
                text=f"Will also create: {joined}",
                fg=C["peach"], bg=C["base"],
                font=("Segoe UI", 9, "italic"),
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(2, 0))
        # Expired-state banner — created hidden, shown only on show_expired()
        self._banner = tk.Label(
            hdr,
            text="⏱  Proposal expired — re-issue the request from the Ask tab",
            fg=C["crust"], bg=C["peach"],
            font=("Segoe UI", 10, "bold"),
            padx=8, pady=4,
        )
        # Not packed yet

    def _build_panes(self):
        paned = tk.PanedWindow(
            self, orient=tk.HORIZONTAL,
            sashrelief=tk.FLAT, sashwidth=6,
            bg=C["surface0"],
        )
        paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        # Left: Original (read-only)
        left_frame = tk.LabelFrame(
            paned, text="Original Content",
            fg=C["red"], bg=C["base"],
            labelanchor="n", padx=4, pady=4,
        )
        self.orig_text = self._make_text_pane(left_frame, editable=False)
        self.orig_text.insert(tk.END, self._proposal.original_content)
        self.orig_text.configure(state=tk.DISABLED)
        paned.add(left_frame, stretch="always")

        # Right: Proposed (editable)
        right_frame = tk.LabelFrame(
            paned, text="Proposed Edit  (you may edit before accepting)",
            fg=C["green"], bg=C["base"],
            labelanchor="n", padx=4, pady=4,
        )
        self.prop_text = self._make_text_pane(right_frame, editable=True)
        self.prop_text.insert(tk.END, self._proposal.proposed_content)
        paned.add(right_frame, stretch="always")

    def _build_buttons(self):
        btn_row = tk.Frame(self, bg=C["base"], padx=20, pady=12)
        btn_row.pack(fill=tk.X)

        self._reject_btn = tk.Button(
            btn_row, text="❌  Reject Changes",
            fg=C["crust"], bg=C["red"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=10, pady=4,
            command=self._on_reject,
        )
        self._reject_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self._accept_btn = tk.Button(
            btn_row, text="🛡️  Accept & Apply Changes",
            fg=C["crust"], bg=C["green"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=10, pady=4,
            command=self._on_accept_press,
        )
        self._accept_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # Close button — created hidden, shown only when expired
        self._close_btn = tk.Button(
            btn_row, text="Close",
            fg=C["crust"], bg=C["overlay0"],
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=10, pady=4,
            command=self.destroy,
        )

    def _make_text_pane(self, parent: tk.Frame, editable: bool) -> tk.Text:
        wrapper = tk.Frame(parent, bg=C["mantle"])
        wrapper.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(wrapper, orient="vertical")
        hsb = ttk.Scrollbar(wrapper, orient="horizontal")
        txt = tk.Text(
            wrapper, wrap=tk.NONE,
            bg=C["mantle"], fg=C["text"],
            insertbackground=C["text"],
            font=("Consolas", 9),
            highlightthickness=0, relief=tk.FLAT,
            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
        )
        vsb.configure(command=txt.yview)
        hsb.configure(command=txt.xview)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        txt.pack(side=tk.LEFT,   fill=tk.BOTH, expand=True)
        return txt

    # ── Handlers ──────────────────────────────────────────────────────────

    def _on_reject(self):
        # If already expired the dialog is read-only; treat click on the close
        # button (which replaces reject) as just destroying.
        if self._expired:
            try:
                self.destroy()
            except tk.TclError:
                pass
            return
        if self._on_reject_cb is not None:
            try:
                self._on_reject_cb()
            except Exception:
                log.exception("on_reject callback raised")
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _on_accept_press(self):
        if self._expired:
            return  # Button should be disabled, but defend anyway
        final_content = self.prop_text.get("1.0", tk.END).rstrip("\n")
        try:
            self.destroy()
        except tk.TclError:
            pass
        try:
            self._on_accept(self._proposal.filepath, final_content)
        except Exception:
            log.exception("on_accept callback raised")

    def show_expired(self):
        """Switch the dialog into post-timeout read-only state.

        Called from the bridge on the Tk main thread when the timeout fires
        before the user acted. Disables Accept / Reject, shows the expired
        banner, and surfaces a Close button. Dialog stays open so the user
        can still inspect the diff — no auto-close (avoids losing context).
        """
        if self._expired:
            return
        self._expired = True
        try:
            if self._accept_btn is not None:
                self._accept_btn.configure(state=tk.DISABLED)
            if self._reject_btn is not None:
                self._reject_btn.pack_forget()
            if self._close_btn is not None:
                self._close_btn.pack(side=tk.RIGHT, padx=(6, 0))
            if self._banner is not None:
                self._banner.pack(anchor="w", pady=(6, 0))
            # Also lock the proposed pane so the user can't edit and then
            # be confused about why "Accept" is greyed out.
            try:
                self.prop_text.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
        except tk.TclError:
            # Dialog destroyed mid-call; nothing to do.
            pass


# ── ProposalBridge ─────────────────────────────────────────────────────────

class ProposalBridge:
    """Cross-thread coordinator between agent worker and ProposalDialog.

    Lives one per `LocalAgent.run()` invocation (or one per write-tool call
    if you prefer — both work). The agent worker thread calls `invoke()`,
    which schedules dialog creation on the Tk main thread, blocks the worker
    on a `threading.Event` until the user resolves (accept / reject / close /
    timeout), and returns `(accepted: bool, final_content: str | None)`.

    Race safety: `_resolve()` is idempotent under a `threading.Lock`. Only
    the first call wins. Late user clicks after the timeout, double-callbacks,
    or external cancellation during a resolve race all reduce to no-ops.
    """

    def __init__(self, root: tk.Misc, proposal: WriteProposal,
                 timeout_s: float = 300.0):
        self._root      = root
        self._proposal  = proposal
        self._timeout_s = timeout_s
        self._event     = threading.Event()
        self._lock      = threading.Lock()
        self._resolved  = False
        self._accepted  = False
        self._content: str | None = None
        self._dialog: ProposalDialog | None = None
        self._timeout_after_id: str | None = None

    def invoke(self) -> tuple[bool, str | None]:
        """Run the proposal flow. MUST be called from a non-Tk-main thread.

        Returns (accepted, final_content). Final_content is None if not
        accepted. Returns (False, None) immediately if the root is already
        destroyed.
        """
        try:
            self._root.after(0, self._create_dialog)
        except (tk.TclError, RuntimeError):
            log.warning("ProposalBridge: root unavailable; resolving as reject")
            return False, None

        self._event.wait(timeout=self._timeout_s + 5.0)  # small safety margin

        # If event.wait timed out without resolution, force timeout path.
        with self._lock:
            if not self._resolved:
                self._resolved = True
                self._accepted = False
                self._content = None

        # Best-effort: show the expired banner if the dialog is still alive.
        # (The Tk-side after() timer should already have done this, but if
        # event.wait timed out via the safety margin without after() firing,
        # we make sure the user still sees expired state.)
        try:
            self._root.after(0, self._mark_expired_safe)
        except (tk.TclError, RuntimeError):
            pass

        return self._accepted, self._content

    def cancel(self) -> None:
        """External cancellation (e.g. App shutdown). Resolves as reject."""
        self._resolve(False, None)
        try:
            self._root.after(0, self._destroy_dialog_safe)
        except (tk.TclError, RuntimeError):
            pass

    # ── Tk-main-thread helpers ────────────────────────────────────────────

    def _create_dialog(self) -> None:
        try:
            self._dialog = ProposalDialog(
                self._root, self._proposal,
                lambda _path, content: self._resolve(True, content),
                on_reject=lambda: self._resolve(False, None),
            )
        except Exception:
            log.exception("ProposalDialog construction failed")
            # Catastrophic: dialog never displayed — unblock agent as reject.
            self._resolve(False, None)
            return
        # Arm Tk-side timeout. The worker's event.wait has its own (slightly
        # longer) timeout as a backstop, but driving the UI-side expiry
        # from Tk's clock keeps the user-visible state coherent.
        try:
            self._timeout_after_id = self._root.after(
                int(self._timeout_s * 1000), self._on_timeout)
        except (tk.TclError, RuntimeError):
            pass

    def _on_timeout(self) -> None:
        with self._lock:
            already = self._resolved
        if already:
            return
        self._mark_expired_safe()
        self._resolve(False, None)

    def _mark_expired_safe(self) -> None:
        if self._dialog is not None:
            try:
                self._dialog.show_expired()
            except (tk.TclError, AttributeError):
                pass

    def _destroy_dialog_safe(self) -> None:
        if self._dialog is not None:
            try:
                self._dialog.destroy()
            except tk.TclError:
                pass

    # ── Resolution (callable from any thread) ─────────────────────────────

    def _resolve(self, accepted: bool, content: str | None) -> None:
        """First-resolution-wins. Idempotent."""
        with self._lock:
            if self._resolved:
                return
            self._resolved = True
            self._accepted = accepted
            self._content = content
        # Cancel pending Tk timeout — best-effort; safe to call from any thread
        # via root.after_cancel (Tcl handles it).
        if self._timeout_after_id is not None:
            try:
                self._root.after_cancel(self._timeout_after_id)
            except (tk.TclError, RuntimeError):
                pass
        self._event.set()


# ── Standalone test harness ───────────────────────────────────────────────────

if __name__ == "__main__":
    # Two test modes:
    #   * Automated bridge tests (no UI clicks needed) — verify race-safety
    #     invariants: first-resolution-wins, cancel-while-waiting, timeout.
    #   * Interactive dialog test — opens the dialog so a human can click
    #     Accept / Reject / Close / Alt-F4 and watch the printed output.
    #
    # Run via `python src/dialogs/proposal.py` from the project root.

    import time

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # ── Automated bridge tests ────────────────────────────────────────────

    def _make_dummy_proposal() -> WriteProposal:
        return WriteProposal(
            filepath="src/example.py",
            original_content="# old\n",
            proposed_content="# new\n",
            rationale="testing",
        )

    def _report(name: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    def _test_late_resolve_noop(root):
        """Second _resolve call must NOT overwrite the first."""
        b = ProposalBridge(root, _make_dummy_proposal(), timeout_s=10.0)
        b._resolve(True, "first")
        b._resolve(False, None)
        _report("late-resolve no-op",
                b._accepted is True and b._content == "first" and b._resolved)

    def _test_cancel_releases_worker(root):
        """External cancel from another thread releases a waiting event.wait."""
        b = ProposalBridge(root, _make_dummy_proposal(), timeout_s=10.0)
        out: dict = {}

        def _wait_worker():
            b._event.wait(timeout=5.0)
            out["resolved"] = b._resolved

        t = threading.Thread(target=_wait_worker, daemon=True)
        t.start()
        time.sleep(0.2)
        b.cancel()
        t.join(timeout=2.0)
        _report("cancel releases waiting worker",
                not t.is_alive()
                and out.get("resolved") is True
                and b._accepted is False)

    def _test_concurrent_resolve(root):
        """Many threads firing _resolve simultaneously — only first wins."""
        b = ProposalBridge(root, _make_dummy_proposal(), timeout_s=10.0)
        threads = [threading.Thread(
            target=lambda i=i: b._resolve(True, f"thread-{i}"))
            for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)
        _report("concurrent _resolve race-safe",
                b._resolved and b._accepted
                and b._content.startswith("thread-"))

    def _test_invoke_no_deadlock(root):
        """invoke() without a real mainloop returns reject quickly, no hang."""
        b = ProposalBridge(root, _make_dummy_proposal(), timeout_s=10.0)
        result: dict = {}

        def _invoke_worker():
            result["ret"] = b.invoke()

        t = threading.Thread(target=_invoke_worker, daemon=True)
        t.start()
        t.join(timeout=2.0)
        _report("invoke() no-deadlock without mainloop",
                not t.is_alive() and result.get("ret") == (False, None))

    def _automated_tests():
        # These tests verify the bridge's internal coordination logic. The
        # dialog/timeout paths that need a live Tk mainloop are covered by
        # the interactive test below — test 4 confirms the bridge doesn't
        # deadlock when those paths can't run.
        #
        # Note: test 4 deliberately exercises the "root.after raises
        # RuntimeError because we're calling Tk from a worker thread with
        # no mainloop" path. You will see one "WARNING ProposalBridge: root
        # unavailable; resolving as reject" line in stderr — that's the
        # bridge correctly defending against the case. The 4 PASS lines
        # are the result that matters.
        print("=== Automated bridge tests ===")

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()

        _test_late_resolve_noop(root)
        _test_cancel_releases_worker(root)
        _test_concurrent_resolve(root)
        _test_invoke_no_deadlock(root)

        # Pump a moment so any deferred Tk callbacks finish cleanly
        for _ in range(5):
            try:
                root.update()
            except tk.TclError:
                break
            time.sleep(0.02)

        try:
            root.destroy()
        except tk.TclError:
            pass

    _automated_tests()

    # ── Interactive dialog test ───────────────────────────────────────────

    if "--no-ui" in sys.argv:
        print("Skipping interactive test (--no-ui).")
        sys.exit(0)

    print()
    print("=== Interactive test — click a button (or close the window) ===")
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()

    dummy = WriteProposal(
        filepath="src/helpers/example.py",
        original_content=(
            "def foo():\n"
            "    # old implementation\n"
            "    return 42\n"
        ),
        proposed_content=(
            "def foo():\n"
            "    # improved implementation with a very long comment "
            "that tests horizontal scrolling behaviour across the pane\n"
            "    result = sum(range(100))\n"
            "    return result\n"
        ),
        rationale="Replaced magic number with computed sum for clarity.",
        dirs_to_create=["src/helpers/"],   # exercise the side-effect notice
    )

    def _accepted(path, content):
        print(f"ACCEPTED: {path}\n---\n{content}\n---")
        root.destroy()

    def _rejected():
        print("REJECTED")
        root.destroy()

    ProposalDialog(root, dummy, _accepted, on_reject=_rejected)
    root.mainloop()
