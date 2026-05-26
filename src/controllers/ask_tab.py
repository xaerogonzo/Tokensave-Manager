"""AskTabController — owns the 🤖 Ask tab.

Decoupled from App: receives a `get_project_path` callback instead of
holding a reference to the App instance, plus a `cfg: ManagerConfig`
for live config reads.

The agent loop itself lives in `src/agent.py` (lazy-imported on first
Send click). This controller is just the UI layer + thread plumbing.

Per Round 4 plan rules:
  - `self._cfg.tokensave_exe` / `self._cfg.raw.get("commit_message_llm")`
    read at execution time (Rule 3) — settings change propagates without
    restart.
  - Lazy imports of `agent` / `agent_tools` keep the Ask tab off the
    import-graph hot path (and avoid pulling LocalAgent into apps that
    never open the tab).
"""

from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

import datetime
import subprocess
import sys

from constants import C, LOG_DIR
from helpers.runtime import log
from helpers.ui import append_text

if TYPE_CHECKING:
    from state import ManagerConfig


class AskTabController:
    """Owns the Ask tab UI and the agent conversation loop.

    Decoupled from App: receives a get_project_path callback instead of
    holding a reference to the App instance.
    """

    _ASK_LOG_FILE = os.path.join(LOG_DIR, "ask_sessions.md")

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
        "- Do NOT guess about THIS project's file contents or code unless you "
        "have already read it. For general knowledge questions (concepts, "
        "language syntax, tool documentation, best practices, what 'git "
        "rebase' does, etc.) answer directly from knowledge — no tool call "
        "needed.\n"
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
        "read it carefully — it sometimes contains a concrete suggestion "
        "(e.g. 'a file named X exists at src/X — retry with that path'). "
        "Follow that specific suggestion exactly once. Do NOT blindly retry "
        "the same call or try minor path/argument tweaks on your own "
        "(e.g. adding './', changing slashes, guessing alternate filenames). "
        "If it still fails after one targeted retry, report the error "
        "directly to the user and continue from what you already know.\n"
        "- If tokensave_search returns no results, that means the query "
        "isn't a symbol name. Switch to read_file (if you have a target "
        "file in mind) or list_directory (to discover one) — don't keep "
        "searching with variations.\n\n"
        "Code-health questions:\n"
        "- For code-health questions (\"is this a god class?\", \"what's "
        "the most complex function?\", \"what's the biggest file?\"), "
        "prefer the tokensave analytics tools: **tokensave_god_class**, "
        "**tokensave_complexity**, and **tokensave_largest**. They give "
        "the canonical answer for this project's caps (see "
        "BASIC_INSTRUCTIONS.md Rule A: file ≤800 lines, method ≤100 "
        "lines, class ≤40 direct methods, complexity ≤10).\n\n"
        "Style:\n"
        "- Keep answers concise. If a question is open-ended, ask a "
        "clarifying follow-up instead of writing a wall of text.\n"
        "- You CANNOT modify files, run commits, or change config. This is "
        "by design. If the user asks you to make changes, suggest the "
        "specific edits in your answer and let them apply them manually."
    )

    # Timeout for claude --print calls in the Ask tab.  Generous (2 min) to
    # accommodate Opus-class responses and slow local networks.
    _CLAUDE_CLI_TIMEOUT = 120

    def __init__(self, notebook: ttk.Notebook, get_project_path,
                 cfg: "ManagerConfig"):
        self._get_project_path = get_project_path
        self._cfg = cfg
        self._notebook = notebook  # kept so seed_question() can flip to this tab
        self._ask_path: str | None = None
        self._ask_messages: list = []
        self._ask_stop_event: threading.Event | None = None
        self._ask_thread: threading.Thread | None = None
        # Streaming support — buffer accumulates token chunks from the worker
        # thread; a 50 ms flush loop drains it on the Tk main thread so the
        # widget is toggled NORMAL/DISABLED at most 20× per second instead of
        # once per token (which would peg the UI on fast models).
        self._stream_buffer: list[str] = []
        self._flush_after_id: str | None = None  # Tk after() handle
        # Active proposal bridges for the in-flight agent run. App.cancel_all_proposals
        # iterates this set on root shutdown so worker threads never deadlock.
        self._active_bridges: set = set()
        self._bridges_lock = threading.Lock()
        self._tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(self._tab, text="  🤖 Ask  ")
        self._build()

    # ── External seeding (refactor scout → Investigate) ──────────────────────

    def seed_question(self, text: str, path: str) -> None:
        """Inject `text` as a user question and run the agent against `path`.

        Used by the refactor scout's Investigate button. The Ask entry is a
        single-line widget that can't display a multi-line investigate prompt
        legibly, so we put the text into the entry (so _ask_send reads it),
        switch the notebook to the Ask tab, and trigger _ask_send. The full
        prompt then appears in the chat log as the user message — that's the
        legible record. Refuses if an agent run is already in flight; the
        user can wait + retry.
        """
        if self._ask_thread and self._ask_thread.is_alive():
            from tkinter import messagebox
            messagebox.showinfo(
                "Ask tab busy",
                "An agent run is already in progress. Wait for it to finish "
                "(or click Stop) before launching a new investigation.",
                parent=self._tab.winfo_toplevel(),
            )
            return
        self._ask_path = path
        try:
            self._notebook.select(self._tab)
        except tk.TclError:
            pass
        try:
            self._ask_entry.delete(0, tk.END)
            self._ask_entry.insert(0, text)
        except tk.TclError:
            return
        self._ask_refresh_header()
        # Defer the send by one Tk tick so the notebook has time to switch
        # tabs first — feels less abrupt and avoids any half-rendered state.
        self._tab.after(50, self._ask_send)

    # ── Phase 1 (Roadmap-2): write-proposal bridge ────────────────────────

    def _ask_open_proposal(self, proposal):
        """Bridge callback passed to LocalAgent.on_write_proposal.

        Called from the agent worker thread. Creates a ProposalBridge,
        registers it for shutdown cancellation, invokes it (blocks the
        worker on a bounded event.wait), then deregisters.

        Returns (accepted: bool, final_content: str | None).
        """
        from dialogs.proposal import ProposalBridge   # lazy import (Tk surface)
        root = self._tab.winfo_toplevel()
        bridge = ProposalBridge(root, proposal, timeout_s=300.0)
        with self._bridges_lock:
            self._active_bridges.add(bridge)
        try:
            return bridge.invoke()
        finally:
            with self._bridges_lock:
                self._active_bridges.discard(bridge)

    def cancel_all_proposals(self):
        """Called by App's WM_DELETE_WINDOW handler. Releases all worker
        threads waiting on open proposals so the app can shut down cleanly."""
        with self._bridges_lock:
            bridges = list(self._active_bridges)
        for b in bridges:
            try:
                b.cancel()
            except Exception:
                log.exception("ProposalBridge.cancel raised on shutdown")

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
        self._session_log_btn = ttk.Button(
            hdr, text="📄 Session log",
            command=self._open_session_log,
            state=tk.DISABLED)
        self._session_log_btn.pack(side=tk.RIGHT, padx=(0, 6))

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

        # Enable session-log button immediately if a previous log exists
        if os.path.isfile(self._ASK_LOG_FILE):
            self._session_log_btn.configure(state=tk.NORMAL)

    def on_tab_selected(self):
        """Called by App._on_tab_changed when the Ask tab is focused."""
        path = self._get_project_path()
        if path and path != self._ask_path:
            # Project switched — stale conversation context would confuse the
            # agent (it would reference files from the old project while tools
            # now target the new one).
            self._ask_path = path
            if self._ask_messages:
                self._ask_messages = []
                self._ask_set_intro()
                append_text(
                    self._ask_log,
                    f"[ Switched to project: {os.path.basename(path)}"
                    f" — previous conversation cleared ]\n\n",
                    "info")
        elif path:
            self._ask_path = path
        self._ask_refresh_header()
        try:
            self._ask_entry.focus_set()
        except tk.TclError:
            pass

    def _ask_set_intro(self):
        self._ask_log.configure(state=tk.NORMAL)
        self._ask_log.delete("1.0", tk.END)
        # Show different intro based on configured provider.
        raw = self._cfg.raw
        cfg = ((raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {})
               if isinstance(raw, dict) else {})
        if cfg.get("provider") == "claude_cli":
            intro = (
                "Ready (Claude CLI mode). Ask anything — responses come from "
                "claude --print.\n"
                "⚠  No tool access: read_file / tokensave_search are unavailable. "
                "Great for general questions, explanations, and code advice.\n\n"
            )
        else:
            intro = (
                "Ready. Ask anything about the selected project — I'll use "
                "read_file, list_directory, git_log, git_diff, and (when "
                "available) tokensave_search / tokensave_context to find "
                "answers. I cannot modify files.\n\n"
            )
        self._ask_log.insert(tk.END, intro, "info")
        self._ask_log.configure(state=tk.DISABLED)

    def _ask_append(self, text: str, tag: str = "assistant"):
        append_text(self._ask_log, text, tag)

    def _ask_refresh_header(self):
        if self._ask_path:
            self._ask_project_lbl.configure(
                text=os.path.basename(self._ask_path))
        else:
            self._ask_project_lbl.configure(text="(no project selected)")
        raw = self._cfg.raw
        cfg = ((raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {})
               if isinstance(raw, dict) else {})
        provider = cfg.get("provider") or "?"
        model = cfg.get("model") or "?"
        enabled = bool(cfg.get("enabled"))
        if enabled:
            self._ask_model_lbl.configure(
                text=f"[provider: {provider} / {model}]",
                fg=C["overlay0"])
        else:
            self._ask_model_lbl.configure(
                text="[AI is disabled — Settings → Ask Tab AI]",
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
        """Top-level send handler — orchestrates validation + agent run."""
        text = self._ask_entry.get().strip()
        if not text:
            return
        llm_cfg = self._ask_preflight()
        if llm_cfg is None:
            return  # validation already showed the error

        # Claude CLI path: bypass LocalAgent, use call_claude_cli_print directly.
        if llm_cfg.get("provider") == "claude_cli":
            self._ask_send_claude_cli(llm_cfg)
            return

        try:
            import agent as _agent_mod
            import agent_tools as _agent_tools_mod
        except ImportError as e:
            self._ask_append(f"Could not import agent module: {e}\n", "error")
            return

        self._ask_prepare_ui(text)

        stop_event = threading.Event()
        self._ask_stop_event = stop_event

        # Phase 1 (Roadmap-2): build with write tool enabled, supply the
        # bridge callback. Each agent run gets its own bridge — never
        # shared, so an abandoned proposal can't starve concurrent runs.
        # The bridge is registered with the App's active-bridge set so the
        # WM_DELETE_WINDOW handler can cancel it on shutdown.
        tools = _agent_tools_mod.build_tools(
            self._ask_path, self._cfg.tokensave_exe, with_write=True)
        agent_instance = _agent_mod.LocalAgent(
            llm_cfg, self._ask_path, tools,
            on_write_proposal=self._ask_open_proposal,
        )
        self._ask_spawn_worker(agent_instance, stop_event)

    # ── _ask_send helpers ────────────────────────────────────────────────────

    def _ask_send_claude_cli(self, llm_cfg: dict) -> None:
        """Send via `claude --print` (no tool access; conversation as text context).

        Called from _ask_send when provider == "claude_cli". Bypasses LocalAgent
        entirely — claude --print is not tool-calling-aware so the agent loop
        isn't needed. The full conversation history is serialised as plain text
        context, with the current user message sent cleanly as the final turn.
        """
        from helpers.claude_cli import call_claude_cli_print

        claude_exe = self._cfg.claude_cli_exe or ""
        if not claude_exe:
            self._ask_append(
                "Claude CLI is not configured. Set the path in "
                "Settings → Claude Code CLI.\n\n", "error")
            return

        text = self._ask_entry.get().strip()
        self._ask_prepare_ui(text)

        model = llm_cfg.get("model") or ""
        stop_event = threading.Event()
        self._ask_stop_event = stop_event

        def _worker():
            # Build history from _ask_messages[:-1] (prior turns only).
            # _ask_prepare_ui already appended the current question as the
            # last entry, so include that as the clean terminal turn below.
            history_parts: list[str] = []
            for m in self._ask_messages[:-1]:
                role = m.get("role", "")
                content = (m.get("content") or "").strip()
                if role == "user" and content:
                    history_parts.append(f"User: {content}")
                elif role == "assistant" and content:
                    history_parts.append(f"Assistant: {content}")
            context_str = "\n\n".join(history_parts)
            current_query = (self._ask_messages[-1].get("content") or "").strip()
            if context_str:
                prompt = (
                    f"Here is our ongoing conversation context:\n\n"
                    f"{context_str}\n\n"
                    f"Now answer this question:\n{current_query}"
                )
            else:
                prompt = current_query

            result = call_claude_cli_print(
                claude_exe, prompt,
                system_prompt=self._ASK_SYSTEM_PROMPT,
                timeout=self._CLAUDE_CLI_TIMEOUT,
                model=model,
                cwd=self._ask_path,  # loads project CLAUDE.md as context
            )
            if stop_event.is_set():
                return
            if result:
                self._ask_messages.append({"role": "assistant", "content": result})
                self._tab.after(0, self._ask_finish, result, False, "")
            else:
                self._tab.after(0, self._ask_finish, None, True,
                                "Claude CLI call failed or timed out. "
                                "Check that the claude executable is configured "
                                "and authenticated (run 'claude --version' to test).")

        self._ask_thread = threading.Thread(target=_worker, daemon=True)
        self._ask_send_btn.configure(state=tk.DISABLED)
        self._ask_stop_btn.configure(state=tk.NORMAL)
        self._ask_status.configure(
            text="Asking Claude CLI… (no tool access in this mode)",
            fg=C["overlay0"])
        self._ask_thread.start()

    def _ask_preflight(self) -> "dict | None":
        """Validate preconditions for sending. Returns llm_cfg dict on success,
        None if any check fails (the appropriate error has already been shown).
        """
        if self._ask_thread and self._ask_thread.is_alive():
            self._ask_status.configure(
                text="A request is already running — click Stop first.",
                fg=C["yellow"])
            return None

        path = self._get_project_path()
        if path:
            self._ask_path = path
        if not self._ask_path:
            self._ask_append(
                "Select a project in the Projects tab first.\n\n", "error")
            return None

        raw = self._cfg.raw
        # Ask tab has its own config; fall back to commit_message_llm for
        # migration (users upgrading from a version that didn't have ask_tab_llm).
        llm_cfg = ((raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {})
                   if isinstance(raw, dict) else {})
        if not llm_cfg.get("enabled"):
            self._ask_append(
                "AI is disabled. Open Settings → Ask Tab AI and "
                "tick the enable box, then try again.\n\n", "error")
            return None
        return llm_cfg

    def _ask_prepare_ui(self, text: str) -> None:
        """Update the chat pane + button states + message history before sending."""
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
            if self._ask_path:
                basename = os.path.basename(self._ask_path)
                self._ask_messages.append({
                    "role": "system",
                    "content": (
                        f"Active project: {basename}\n"
                        f"Project root: {self._ask_path}\n\n"
                        "All file paths in tool calls are relative to this "
                        "root. Start there when the user asks about the "
                        "project."
                    ),
                })
        self._ask_messages.append({"role": "user", "content": text})

    def _ask_build_callbacks(self) -> dict:
        """Build the on_* callback dict passed into LocalAgent.run()."""
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
            self._tab.after(0, self._ask_finish, final_text, False, "")

        def _on_error(msg):
            self._tab.after(0, self._ask_finish, None, True, msg)

        # Streaming delta — worker thread appends to buffer; Tk flush loop
        # drains it at 20 fps to avoid toggling widget state per-token.
        _stream_started = [False]

        def _on_stream_delta(chunk):
            if not _stream_started[0]:
                _stream_started[0] = True
                self._stream_buffer.append("🤖  ")
            self._stream_buffer.append(chunk)

        return {
            "on_tool_call":         _on_tool_call,
            "on_tool_result":       _on_tool_result,
            "on_assistant_message": _on_assistant_message,
            "on_done":              _on_done,
            "on_error":             _on_error,
            "on_stream_delta":      _on_stream_delta,
            "_stream_started":      _stream_started,
        }

    def _start_stream_flush_loop(self) -> None:
        """Start the 50 ms flush loop that drains _stream_buffer to the UI."""
        self._stream_buffer = []

        def _flush():
            if self._stream_buffer:
                text = "".join(self._stream_buffer)
                self._stream_buffer.clear()
                self._ask_append(text, "assistant")
            # Reschedule while the worker thread is alive.
            if self._ask_thread and self._ask_thread.is_alive():
                self._flush_after_id = self._tab.after(50, _flush)
            else:
                self._flush_after_id = None

        self._flush_after_id = self._tab.after(50, _flush)

    def _stop_stream_flush_loop(self) -> None:
        """Cancel the flush loop and drain any remaining buffer."""
        if self._flush_after_id is not None:
            try:
                self._tab.after_cancel(self._flush_after_id)
            except Exception:
                pass
            self._flush_after_id = None
        if self._stream_buffer:
            text = "".join(self._stream_buffer)
            self._stream_buffer.clear()
            self._ask_append(text, "assistant")

    def _open_session_log(self) -> None:
        """Open ask_sessions.md in the system default viewer (cross-platform)."""
        path = self._ASK_LOG_FILE
        if not os.path.isfile(path):
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=True)
            else:
                subprocess.run(["xdg-open", path], check=True)
        except Exception as exc:
            log.warning("Could not open session log: %s", exc)

    def _persist_exchange(self, user_text: str, assistant_text: str) -> None:
        """Append the completed Q&A pair to logs/ask_sessions.md.

        Called after every successful non-error agent response so the
        conversation survives app restarts and can be reviewed in any editor.
        Creates the logs/ directory and the file on first use.
        """
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            project = (os.path.basename(self._ask_path)
                       if self._ask_path else "—")
            with open(self._ASK_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n---\n**{ts} | {project}**\n\n")
                f.write(f"**Q:** {user_text.strip()}\n\n")
                f.write(f"**A:** {assistant_text.strip()}\n")
            # Enable the button on first successful write
            try:
                self._session_log_btn.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        except OSError as exc:
            log.warning("Could not write ask session log: %s", exc)

    def _ask_finish(self, final_text, is_error: bool, error_msg: str,
                    stream_started: "list[bool] | None" = None) -> None:
        """Main-thread terminal state — called from both _on_done and _on_error."""
        self._stop_stream_flush_loop()
        if is_error:
            self._ask_append(f"⚠  {error_msg}\n\n", "error")
            self._ask_status.configure(text="✗  Error.", fg=C["red"])
        elif final_text is None:
            self._ask_status.configure(
                text="✓  Done (no final answer text — model only issued tool calls).",
                fg=C["green"])
        else:
            self._ask_status.configure(text="✓  Done.", fg=C["green"])
            # Append trailing newlines after streamed content if needed.
            if stream_started and stream_started[0]:
                tail = self._ask_log.get("end-3c", "end-1c")
                if not tail.endswith("\n\n"):
                    self._ask_append("\n\n", "assistant")
            # Persist the exchange to disk (survives app restarts)
            last_user = next(
                (m["content"] for m in reversed(self._ask_messages)
                 if m.get("role") == "user"),
                "")
            if last_user and final_text:
                self._persist_exchange(last_user, final_text)
        self._ask_send_btn.configure(state=tk.NORMAL)
        self._ask_stop_btn.configure(state=tk.DISABLED)
        self._ask_stop_event = None

    def _ask_spawn_worker(self, agent_instance, stop_event) -> None:
        """Spawn the daemon thread that runs agent.run() with our callbacks."""
        callbacks = self._ask_build_callbacks()
        stream_started = callbacks.pop("_stream_started")
        self._start_stream_flush_loop()

        # Wrap on_done / on_error to pass stream_started into _ask_finish.
        # (The original callbacks in `callbacks` are intentionally discarded —
        # the wrapped versions below replace them entirely via reassignment.)
        def _on_done_wrapped(final_text):
            self._tab.after(0, self._ask_finish, final_text, False, "",
                            stream_started)

        def _on_error_wrapped(msg):
            self._tab.after(0, self._ask_finish, None, True, msg,
                            stream_started)

        callbacks["on_done"]  = _on_done_wrapped
        callbacks["on_error"] = _on_error_wrapped

        def _worker():
            try:
                agent_instance.run(self._ask_messages,
                                   stop_event=stop_event, **callbacks)
            except Exception as e:
                log.exception("Ask worker crashed")
                try:
                    self._tab.after(0, self._ask_finish, None, True,
                                    f"{type(e).__name__}: {e}",
                                    stream_started)
                except RuntimeError:
                    pass

        self._ask_thread = threading.Thread(
            target=_worker, daemon=True, name="ask-agent-worker")
        self._ask_thread.start()
