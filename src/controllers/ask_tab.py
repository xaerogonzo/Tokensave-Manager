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

from constants import C
from helpers.runtime import log
from helpers.ui import append_text

if TYPE_CHECKING:
    from state import ManagerConfig


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

    def __init__(self, notebook: ttk.Notebook, get_project_path,
                 cfg: "ManagerConfig"):
        self._get_project_path = get_project_path
        self._cfg = cfg
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
        append_text(self._ask_log, text, tag)

    def _ask_refresh_header(self):
        if self._ask_path:
            self._ask_project_lbl.configure(
                text=os.path.basename(self._ask_path))
        else:
            self._ask_project_lbl.configure(text="(no project selected)")
        raw = self._cfg.raw
        cfg = (raw.get("commit_message_llm") or {}) if isinstance(raw, dict) else {}
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
        """Top-level send handler — orchestrates validation + agent run."""
        text = self._ask_entry.get().strip()
        if not text:
            return
        llm_cfg = self._ask_preflight()
        if llm_cfg is None:
            return  # validation already showed the error

        try:
            import agent as _agent_mod
            import agent_tools as _agent_tools_mod
        except ImportError as e:
            self._ask_append(f"Could not import agent module: {e}\n", "error")
            return

        self._ask_prepare_ui(text)

        stop_event = threading.Event()
        self._ask_stop_event = stop_event

        tools = _agent_tools_mod.build_tools(self._ask_path, self._cfg.tokensave_exe)
        agent_instance = _agent_mod.LocalAgent(llm_cfg, self._ask_path, tools)
        self._ask_spawn_worker(agent_instance, stop_event)

    # ── _ask_send helpers ────────────────────────────────────────────────────

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
        llm_cfg = (raw.get("commit_message_llm") or {}) if isinstance(raw, dict) else {}
        if not llm_cfg.get("enabled"):
            self._ask_append(
                "AI is disabled. Open Settings → AI commit messages and "
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

        return {
            "on_tool_call":         _on_tool_call,
            "on_tool_result":       _on_tool_result,
            "on_assistant_message": _on_assistant_message,
            "on_done":              _on_done,
            "on_error":             _on_error,
        }

    def _ask_finish(self, final_text, is_error: bool, error_msg: str) -> None:
        """Main-thread terminal state — called from both _on_done and _on_error."""
        if is_error:
            self._ask_append(f"⚠  {error_msg}\n\n", "error")
            self._ask_status.configure(text="✗  Error.", fg=C["red"])
        elif final_text is None:
            self._ask_status.configure(
                text="✓  Done (no final answer text — model only issued tool calls).",
                fg=C["green"])
        else:
            self._ask_status.configure(text="✓  Done.", fg=C["green"])
        self._ask_send_btn.configure(state=tk.NORMAL)
        self._ask_stop_btn.configure(state=tk.DISABLED)
        self._ask_stop_event = None

    def _ask_spawn_worker(self, agent_instance, stop_event) -> None:
        """Spawn the daemon thread that runs agent.run() with our callbacks."""
        callbacks = self._ask_build_callbacks()

        def _worker():
            try:
                agent_instance.run(self._ask_messages,
                                   stop_event=stop_event, **callbacks)
            except Exception as e:
                log.exception("Ask worker crashed")
                try:
                    self._tab.after(0, self._ask_finish, None, True,
                                    f"{type(e).__name__}: {e}")
                except RuntimeError:
                    pass

        self._ask_thread = threading.Thread(
            target=_worker, daemon=True, name="ask-agent-worker")
        self._ask_thread.start()
