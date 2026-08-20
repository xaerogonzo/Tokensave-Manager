"""AICodeReviewDialog — Stage 1 of the agentic-AI roadmap.

One-shot AI code review on `git diff HEAD`. Pure read-only: no tools,
no file writes, no autonomy concerns — just a streaming LLM call with
a locked-in "code reviewer" system prompt.

Async pattern matches GitCommitDialog: daemon thread runs the LLM call,
spinner shows progress, tokens stream into the output pane via
`self._post(...)` with worker-side batching (~50 ms / 32 chars) so
fast local models don't saturate Tk's event loop.

Takes both an `llm_cfg: dict` (the AI-features sub-config from
manager-config.json) AND a `cfg: ManagerConfig` (the full settings
instance). `llm_cfg` drives the LLM call; `cfg.git_exe` is the live
git path for the underlying `git diff HEAD`. Reading `self._cfg.git_exe`
at execution time (Rule 3) means a Settings → Save propagates without
restarting the dialog.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from helpers.commit_messages import _pending_diff, _files_from_diff
from helpers.llm import _call_llm
from helpers.runtime import log
from helpers.ui import append_text
from theme import UiPumpMixin

if TYPE_CHECKING:
    from state import ManagerConfig


class AICodeReviewDialog(UiPumpMixin, tk.Toplevel):
    """Stage 1 of the agentic-AI roadmap: AI Code Review on the pending diff.

    Pure read-only. Calls _call_llm once with a "you are a code reviewer"
    system prompt and the project's `git diff HEAD` output. Result is
    displayed as Markdown-style structured text. No tool calls, no file
    writes, no autonomy concerns — just a one-shot review.

    Async pattern matches GitCommitDialog: daemon thread runs the LLM call,
    spinner shows progress, self._post(...) pushes the result back to
    the main thread. Stop button aborts the in-flight request.
    """

    _SYSTEM_PROMPT = (
        "You are a senior code reviewer reviewing a pending git diff. "
        "Produce a structured Markdown report.\n\n"
        "Output format:\n\n"
        "## ⚠ High severity\n"
        "- <finding> (file:line)\n\n"
        "## ⚡ Medium severity\n"
        "- <finding> (file:line)\n\n"
        "## 💡 Low severity / nits\n"
        "- <finding> (file:line)\n\n"
        "## ℹ Observations\n"
        "- <design note>\n\n"
        "Rules:\n"
        "- One bullet per finding. Cite file:line when possible.\n"
        "- Omit a section entirely if it has nothing — don't write empty sections.\n"
        "- Do NOT repeat the diff back at me.\n"
        "- Focus on correctness, security, and maintainability — not formatting.\n"
        "- Match the project's existing style (judge by surrounding context).\n"
        "- Output ONLY the report. No preamble, no closing remarks."
    )

    def __init__(self, parent, path: str, llm_cfg: dict, cfg: "ManagerConfig"):
        super().__init__(parent)
        # Start the worker -> UI channel before anything can post to it.
        self._start_ui_pump()
        self.title(f"AI Code Review — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(700, 500)
        self.geometry("900x720")
        self.grab_set()

        self._path = path
        self._llm_cfg = llm_cfg
        self._cfg = cfg
        self._review_token = 0
        self._cancelled = False

        self._build_header_section(path)
        paned = self._build_paned_split()
        self._build_diff_pane(paned)
        self._build_review_pane(paned)
        self._build_buttons_section()

        # Load diff & kick off review
        self._load_diff()
        self._start_review()

    def _build_header_section(self, path: str):
        """Title row + live status label (spinner / provider info)."""
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=20, pady=(14, 6))
        tk.Label(hdr, text="🔍  AI Code Review",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)
        tk.Label(hdr, text=os.path.basename(path),
                 font=("Segoe UI", 10),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT, padx=(10, 0))
        self._status_lbl = tk.Label(
            self, text="", font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["peach"],
            justify=tk.LEFT, anchor=tk.W)
        self._status_lbl.pack(fill=tk.X, padx=20, pady=(0, 6))

    def _build_paned_split(self):
        """The vertical PanedWindow that hosts the diff pane (top) and
        the review pane (bottom). Returns the paned widget so the two
        pane-builders can `paned.add(...)` into it."""
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=C["base"],
                                sashwidth=6, sashrelief=tk.FLAT)
        paned.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))
        return paned

    def _build_diff_pane(self, paned: tk.PanedWindow):
        """Top pane: git-diff Text widget with both scrollbars + diff colour tags."""
        diff_frame = tk.LabelFrame(
            paned, text=" Pending diff (git diff HEAD) ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        diff_inner = tk.Frame(diff_frame, bg=C["mantle"])
        diff_inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._diff_txt = tk.Text(
            diff_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Consolas", 9),
            padx=8, pady=6, wrap=tk.NONE, height=14)
        diff_vsb = ttk.Scrollbar(diff_inner, orient="vertical",
                                  command=self._diff_txt.yview)
        diff_hsb = ttk.Scrollbar(diff_inner, orient="horizontal",
                                  command=self._diff_txt.xview)
        self._diff_txt.configure(
            yscrollcommand=diff_vsb.set, xscrollcommand=diff_hsb.set)
        self._diff_txt.grid(row=0, column=0, sticky="nsew")
        diff_vsb.grid(row=0, column=1, sticky="ns")
        diff_hsb.grid(row=1, column=0, sticky="ew")
        diff_inner.grid_rowconfigure(0, weight=1)
        diff_inner.grid_columnconfigure(0, weight=1)
        # Catppuccin Mocha-ish diff colours
        self._diff_txt.tag_configure("add",      foreground="#a6e3a1")
        self._diff_txt.tag_configure("del",      foreground="#f38ba8")
        self._diff_txt.tag_configure("hunk",     foreground="#89b4fa")
        self._diff_txt.tag_configure("filename", foreground="#cba6f7",
                                      font=("Consolas", 9, "bold"))
        paned.add(diff_frame, minsize=120, stretch="always")

    def _build_review_pane(self, paned: tk.PanedWindow):
        """Bottom pane: review Text widget + vertical scrollbar + severity-header tags."""
        rev_frame = tk.LabelFrame(
            paned, text=" AI review ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        rev_inner = tk.Frame(rev_frame, bg=C["mantle"])
        rev_inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._rev_txt = tk.Text(
            rev_inner, bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, font=("Segoe UI", 10),
            padx=10, pady=8, wrap=tk.WORD, height=14)
        rev_vsb = ttk.Scrollbar(rev_inner, orient="vertical",
                                 command=self._rev_txt.yview)
        self._rev_txt.configure(yscrollcommand=rev_vsb.set)
        self._rev_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rev_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        # Section header colours for the markdown-ish review text
        self._rev_txt.tag_configure(
            "h_high",   foreground="#f38ba8",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_medium", foreground="#fab387",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_low",    foreground="#f9e2af",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self._rev_txt.tag_configure(
            "h_info",   foreground="#89b4fa",
            font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        paned.add(rev_frame, minsize=150, stretch="always")

    def _build_buttons_section(self):
        """Action button row: Copy / Regenerate / Stop / Close."""
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 14))
        self._copy_btn = ttk.Button(
            btn_row, text="Copy review to clipboard",
            command=self._copy_review, state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._regen_btn = ttk.Button(
            btn_row, text="Regenerate",
            command=self._start_review, state=tk.DISABLED)
        self._regen_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._stop_btn = ttk.Button(
            btn_row, text="Stop", command=self._cancel)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Close",
                   command=self.destroy).pack(side=tk.RIGHT)

    # ─────────────────────────────────────────────────────────────────────

    def _load_diff(self):
        """Read the pending diff and render it into the diff pane."""
        diff = _pending_diff(self._path, git_exe=self._cfg.git_exe, lines_of_context=3)
        self._diff_txt.configure(state=tk.NORMAL)
        self._diff_txt.delete("1.0", tk.END)
        if not diff:
            self._diff_txt.insert(tk.END, "(no pending changes)")
            self._diff_txt.configure(state=tk.DISABLED)
            self._show_status("No pending diff to review.", colour=C["overlay0"])
            self._stop_btn.configure(state=tk.DISABLED)
            self._regen_btn.configure(state=tk.DISABLED)
            return
        for line in diff.splitlines():
            tag = ""
            if line.startswith("+++") or line.startswith("---"):
                tag = "filename"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            elif line.startswith("@@"):
                tag = "hunk"
            self._diff_txt.insert(tk.END, line + "\n", tag)
        self._diff_txt.configure(state=tk.DISABLED)

    def _start_review(self):
        """Kick off (or restart) the LLM review on a background thread.

        Orchestrator: takes a fresh review-token, resets UI state to the
        "waiting" view, builds the user prompt, then hands off to
        `_spawn_review_worker` for the streaming worker construction. Keeps
        the state-reset and worker-thread setup as two readable steps.
        """
        diff = _pending_diff(self._path, git_exe=self._cfg.git_exe, lines_of_context=3)
        if not diff:
            return
        self._review_token += 1
        token = self._review_token
        self._cancelled = False
        self._streaming_started = False  # placeholder cleared on first token

        provider = self._llm_cfg.get("provider", "?")
        model    = self._llm_cfg.get("model", "?")
        self._show_status(
            f"⟳  Streaming review with {provider} / {model}…  "
            f"(can take 30–60s on local models)",
            colour=C["peach"])
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        self._rev_txt.insert(tk.END, "(waiting for first token…)")
        self._rev_txt.configure(state=tk.DISABLED)

        self._copy_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)

        max_chars = int(self._llm_cfg.get("max_diff_chars", 24000))

        # v4.2: tokensave+codegraph grounding (module_deep_dive recipe —
        # surfaces caller/callee relationships for the changed symbols,
        # so the reviewer can reason about impact instead of just hunks).
        grounding = ""
        if self._cfg.enable_llm_grounding:
            try:
                from helpers.doc_grounding import (
                    build_grounding_block,
                    build_codegraph_block,
                    build_combined_grounding,
                )
                changed_files = _files_from_diff(diff)
                try:
                    ts_block = build_grounding_block(
                        self._path, "module_deep_dive",
                        tokensave_exe=self._cfg.tokensave_exe,
                    )
                except Exception:
                    ts_block = ""
                try:
                    # v4.3: ensure fresh before grounding call.
                    if self._cfg.codegraph_exe:
                        try:
                            from helpers.codegraph_freshness import ensure_fresh
                            ensure_fresh(self._path, self._cfg.codegraph_exe)
                        except Exception:
                            pass
                    cg_block = build_codegraph_block(
                        self._path, "module_deep_dive",
                        changed_files=changed_files,
                        codegraph_exe=self._cfg.codegraph_exe or "",
                    )
                except Exception:
                    cg_block = ""
                grounding = build_combined_grounding(ts_block, cg_block)
            except Exception:
                grounding = ""

        grounding_section = (
            f"## Repository context (auto-attached)\n\n{grounding}\n\n"
            if grounding else ""
        )
        user_prompt = (
            f"Review the following git diff. Project: "
            f"{os.path.basename(self._path)}.\n\n"
            + grounding_section
            + f"```diff\n{diff[:max_chars]}\n```"
            + ("\n\n[diff truncated for length]" if len(diff) > max_chars else "")
        )
        self._spawn_review_worker(token, user_prompt)

    def _spawn_review_worker(self, token: int, user_prompt: str):
        """Set up the token-batched streaming closures and spawn the worker.

        Streams tokens from the LLM into the review pane as they arrive, so
        the user sees output building up in real time instead of staring at
        a spinner for 30+ seconds. Tokens are batched on the worker side
        (every ~32 chars or 50 ms, whichever first) before being pushed to
        the Tk main thread via `self.after` — a fast local model can emit
        80+ tokens/s and 1:1 self.after calls would saturate Tk's event
        loop. The accumulated full text is still returned at end-of-stream
        so the existing `_render_review` (which applies severity-section
        colour tags) can do its final pass.

        Worker-thread-only batching buffer: no lock needed because both
        `_on_token` and the final-flush call run on the worker thread, and
        each snapshot is passed by value into `self.after`.
        """
        batch_text: list[str] = []
        batch_chars = [0]
        last_flush = [time.monotonic()]

        def _flush(snapshot: str, tok=token):
            # Runs on the Tk main thread.
            if tok != self._review_token or self._cancelled:
                return
            self._stream_append(snapshot)

        def _on_token(delta: str):
            # Runs on the worker thread.
            batch_text.append(delta)
            batch_chars[0] += len(delta)
            now = time.monotonic()
            if batch_chars[0] >= 32 or (now - last_flush[0]) >= 0.05:
                snapshot = "".join(batch_text)
                batch_text.clear()
                batch_chars[0] = 0
                last_flush[0] = now
                try:
                    self._post(_flush, snapshot)
                except RuntimeError:
                    # Dialog destroyed mid-stream — stop trying to push.
                    pass

        def _worker(tok=token):
            try:
                result = _call_llm(
                    self._llm_cfg,
                    self._SYSTEM_PROMPT,
                    user_prompt,
                    max_tokens=2000,
                    on_token=_on_token,
                )
            except Exception:
                log.exception("AI code review worker failed")
                result = None
            # Final flush — any tokens still buffered when the stream ended.
            if batch_text:
                snapshot = "".join(batch_text)
                batch_text.clear()
                try:
                    self._post(_flush, snapshot)
                except RuntimeError:
                    pass
            try:
                self._post(self._on_review_ready, tok, result)
            except RuntimeError:
                # Dialog destroyed before result arrived — silent drop.
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="ai-code-review-worker").start()

    def _stream_append(self, text: str):
        """Append a batch of streamed tokens to the review pane.

        On the first call after `_start_review`, clears the placeholder
        ('(waiting for first token…)'). Auto-scrolls so the latest content
        stays visible. Section-header colour tags are NOT applied here —
        they get a clean re-render in `_on_review_ready` once the full text
        is available, which keeps the streaming path simple (no need to
        re-tag partial lines as they grow).
        """
        if not text:
            return
        # First-token placeholder clear (specific to this widget — keep inline).
        if not getattr(self, "_streaming_started", False):
            self._rev_txt.configure(state=tk.NORMAL)
            self._rev_txt.delete("1.0", tk.END)
            self._rev_txt.configure(state=tk.DISABLED)
            self._streaming_started = True
        append_text(self._rev_txt, text)

    def _on_review_ready(self, token: int, result):
        """Main-thread callback: receive the LLM result."""
        if token != self._review_token or self._cancelled:
            return  # stale or cancelled
        self._stop_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.NORMAL)
        if not result:
            self._show_status(
                "⚠  LLM call failed or returned empty. Check Settings → "
                "AI commit messages (provider / model / server running).",
                colour=C["red"])
            self._rev_txt.configure(state=tk.NORMAL)
            self._rev_txt.delete("1.0", tk.END)
            self._rev_txt.insert(
                tk.END,
                "No review produced. Common causes:\n\n"
                "• Ollama / LM Studio server isn't running\n"
                "• Model name in Settings doesn't match a loaded model\n"
                "• Timeout exceeded (try a smaller / non-reasoning model)\n"
                "• Diff exceeds context window — try a model with more context")
            self._rev_txt.configure(state=tk.DISABLED)
            return
        self._render_review(result)
        self._show_status(
            f"✓  Review complete. {self._llm_cfg.get('provider')} / "
            f"{self._llm_cfg.get('model')}",
            colour=C["green"])
        self._copy_btn.configure(state=tk.NORMAL)
        self._last_review = result

    def _render_review(self, text: str):
        """Insert the review text with section-header colour tags."""
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        for line in text.splitlines():
            stripped = line.lstrip()
            tag = ""
            if stripped.startswith("## ⚠") or stripped.lower().startswith("## high"):
                tag = "h_high"
            elif stripped.startswith("## ⚡") or stripped.lower().startswith("## medium"):
                tag = "h_medium"
            elif stripped.startswith("## 💡") or stripped.lower().startswith("## low"):
                tag = "h_low"
            elif stripped.startswith("## ℹ") or stripped.lower().startswith("## observ"):
                tag = "h_info"
            if tag:
                self._rev_txt.insert(tk.END, line + "\n", tag)
            else:
                self._rev_txt.insert(tk.END, line + "\n")
        self._rev_txt.configure(state=tk.DISABLED)

    def _show_status(self, text: str, colour: str):
        self._status_lbl.configure(text=text, fg=colour)

    def _cancel(self):
        """User clicked Stop. The worker thread is daemon and will exit when
        the LLM responds (we can't actually kill urllib mid-call from another
        thread), but the result will be discarded by token mismatch."""
        self._cancelled = True
        self._review_token += 1   # invalidate the in-flight token
        self._show_status(
            "Cancelled. The background request may still finish but its "
            "result will be discarded.",
            colour=C["overlay0"])
        self._stop_btn.configure(state=tk.DISABLED)
        self._regen_btn.configure(state=tk.NORMAL)
        self._rev_txt.configure(state=tk.NORMAL)
        self._rev_txt.delete("1.0", tk.END)
        self._rev_txt.insert(tk.END, "(cancelled)")
        self._rev_txt.configure(state=tk.DISABLED)

    def _copy_review(self):
        text = getattr(self, "_last_review", "")
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._show_status("✓  Review copied to clipboard.", colour=C["green"])
        except tk.TclError:
            pass
