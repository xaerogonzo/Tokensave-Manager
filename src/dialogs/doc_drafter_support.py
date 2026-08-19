"""DocDrafterDialog support objects — backend resolution, draft tick, tab factory.

Split out of dialogs/doc_drafter.py (Roadmap-8 oversize fix). Three pieces:

  * ``_BackendResolver`` — pure compute: which LLM config / labels a
    Generate click uses, honouring the per-session backend override.
  * ``_DraftTicker``    — per-second status tick during a long Generate,
    holding a ``weakref.proxy`` to the dialog.
  * ``build_tab``       — the per-tab widget factory; constructs one
    notebook tab and registers its widgets in ``dlg._tab_widgets``.

Plus the ``_BACKEND_*`` override labels and the ``_suppressed_modified``
context manager they travel with. The dialog imports all of these — the
behavior contract is unchanged.
"""

from __future__ import annotations

import contextlib
import os
import time
import tkinter as tk
import weakref
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C

if TYPE_CHECKING:
    from typing import Callable


# Per-session backend override values for the dialog header dropdown.
# Picked per-draft, never persisted to Settings.  The override applies only
# to the NEXT Generate click (_llm_cfg_resolved snapshots at Generate time;
# mid-flight workers continue with their captured config).
_BACKEND_DEFAULT     = "Default (ask_tab_llm)"
_BACKEND_OLLAMA      = "Force Ollama"
_BACKEND_CLAUDE_CLI  = "Force Claude CLI"
_BACKEND_OPTIONS     = [_BACKEND_DEFAULT, _BACKEND_OLLAMA, _BACKEND_CLAUDE_CLI]


@contextlib.contextmanager
def _suppressed_modified(state: dict):
    """v4.4 (Gemini #2): guarantee the suppress_modified flag clears.

    Tk's ``<<Modified>>`` virtual event fires on programmatic
    ``text.insert(...)`` too, which would otherwise clear the warning
    banner the moment a draft lands. The dialog uses
    ``state["suppress_modified"]`` to gate the handler. Without
    ``try``/``finally``, any exception mid-insert (a TclError, a Unicode
    glitch, a worker race) leaves the flag stuck True for the rest of
    the session — the banner can never auto-clear again.

    This context manager wraps every programmatic mutation site so the
    flag is always reset, regardless of what happens inside the block.
    """
    state["suppress_modified"] = True
    try:
        yield
    finally:
        state["suppress_modified"] = False


class _BackendResolver:
    """Resolves which LLM config / display labels to use for a Generate click.

    Extracted from :class:`DocDrafterDialog` (Phase C4). Pure compute — no Tk
    widgets — so it can be unit-tested in isolation. Reads the live config and
    the per-session backend-override label via the ``get_override`` callable.
    """

    def __init__(self, cfg, get_override: "Callable[[], str]") -> None:
        self._cfg = cfg
        self._get_override = get_override

    def llm_cfg_resolved(self) -> dict:
        """Resolve which LLM config dict to use for the next Generate click.

        Honours the per-session backend override.  When the override is active,
        builds a synthetic config by WHITELISTING only the fields the target
        provider's dispatcher reads — avoids leaking an Anthropic api_key_env
        into a forced Ollama call, etc.
        """
        raw = self._cfg.raw if isinstance(self._cfg.raw, dict) else {}
        base = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
        override = self._get_override()
        if override == _BACKEND_OLLAMA:
            base_provider = (base.get("provider") or "").lower()
            base_url_safe = base.get("base_url") if base_provider in (
                "ollama", "openai_compatible") else None
            return {
                "enabled":  True,
                "provider": "ollama",
                "model":    base.get("model") or "qwen2.5-coder:14b",
                "base_url": base_url_safe or "http://localhost:11434",
            }
        if override == _BACKEND_CLAUDE_CLI:
            return {
                "enabled":  True,
                "provider": "claude_cli",
                "model":    self._cfg.claude_cli_model or "",
            }
        return base

    def summary(self) -> str:
        """One-line header summary of the resolved backend."""
        cfg = self.llm_cfg_resolved()
        prov = cfg.get("provider", "(none configured)")
        model = cfg.get("model") or "(default)"
        override = self._get_override()
        if override != _BACKEND_DEFAULT:
            return f"⚡ OVERRIDE → {prov} / {model}"
        which = "ask_tab_llm" if (self._cfg.raw or {}).get("ask_tab_llm") \
                else "commit_message_llm"
        return f"Backend: {which} → {prov} / {model}"

    def label_for_status(self) -> str:
        """Short human-readable backend name for the 'Drafting on X' tick.

        Reads the resolved LLM config at call time so a backend-override change
        during generation reflects in the visible label.
        """
        try:
            cfg = self.llm_cfg_resolved() or {}
        except (AttributeError, RuntimeError):
            return "LLM"
        provider = (cfg.get("provider") or "").lower()
        if provider == "claude_cli":
            return "Claude CLI"
        if provider == "ollama":
            return "Ollama"
        if provider == "anthropic":
            return "Anthropic"
        if provider == "openai":
            return "OpenAI"
        if provider == "openai_compatible":
            base = (cfg.get("base_url") or "").lower()
            return "Ollama" if "localhost" in base else "OpenAI-compat"
        return provider or "LLM"


class _DraftTicker:
    """Per-second status-bar tick during a long Generate run.

    Extracted from :class:`DocDrafterDialog` (Phase C4). Holds a
    ``weakref.proxy`` to the owning dialog so the bidirectional
    dialog↔ticker reference doesn't delay Tk resource cleanup. Operates on
    the dialog's shared ``_tab_state`` dict and uses the dialog's Tk
    ``after`` / ``winfo_exists`` / ``_set_status``.
    """

    # G6 (v4.4): hard timeout beyond dispatch_llm's 300 s limit; +10 s grace
    # absorbs thread-scheduling lag. On trigger, the captured stop_event is
    # signalled so the worker can break its loop.
    _HARD_TIMEOUT = 310   # seconds; dispatch_llm timeout is 300

    def __init__(self, dialog, tab_state: dict,
                 backend: "_BackendResolver") -> None:
        self._dlg     = weakref.proxy(dialog)
        self._state   = tab_state
        self._backend = backend

    def start(self, key, stop_event) -> None:
        """Start the per-second draft tick. Called from _on_generate_impl."""
        state = self._state.setdefault(key, {})
        state["draft_start_ts"] = time.monotonic()
        state["draft_tick_stop_event"] = stop_event
        try:
            state["draft_tick_after"] = self._dlg.after(
                1000, lambda k=key: self._tick(k))
        except tk.TclError:
            pass

    def stop(self, key) -> None:
        """Cancel the tick. Called from _on_generate_done / _on_generate_error."""
        state = self._state.get(key) or {}
        state["draft_start_ts"] = None
        state["draft_tick_stop_event"] = None
        after_id = state.pop("draft_tick_after", None)
        if after_id is not None:
            try:
                self._dlg.after_cancel(after_id)
            except tk.TclError:
                pass

    def _tick(self, key) -> None:
        """Periodic status-bar tick; updates elapsed seconds, self-reschedules.

        Self-cancels when: dialog destroyed, draft_start_ts cleared (completion),
        stop_event identity changed (newer generation), or elapsed exceeds the
        hard timeout (G6 v4.4).
        """
        try:
            if not self._dlg.winfo_exists():
                return
        except (tk.TclError, ReferenceError):
            return
        state = self._state.get(key) or {}
        start = state.get("draft_start_ts")
        if not start:
            return   # completed / cancelled
        captured = state.get("draft_tick_stop_event")
        if captured is not None and captured is not state.get("stop"):
            return
        elapsed = int(time.monotonic() - start)
        if elapsed > self._HARD_TIMEOUT:
            if captured is not None:
                captured.set()
            state["draft_start_ts"] = None
            self._dlg._set_status(
                key,
                f"⚠ Drafting timed out after {elapsed}s — backend hung "
                "(no Python exception). Stop signalled. Try again or "
                "switch to a different backend.",
                C["red"],
            )
            return
        backend = self._backend.label_for_status()
        self._dlg._set_status(key, f"Drafting on {backend} ({elapsed}s)…",
                              C["overlay0"])
        try:
            state["draft_tick_after"] = self._dlg.after(
                1000, lambda k=key: self._tick(k))
        except tk.TclError:
            pass


# Display names for the notebook tab strip. Tk notebooks do not scroll their
# tabs — once the strip is wider than the dialog, the overflowing tabs are
# simply unreachable rather than merely cramped. With every key rendered as
# `key.upper()` the strip needed ~760px while the dialog's own minsize is 720,
# so TOKENSAVE_GUIDE could not be clicked at the smallest supported size.
# Only the two longest need shortening; the rest read better in full.
_TAB_LABELS = {
    "docs_generic":    "DOCS",
    "tokensave_guide": "GUIDE",
}


def build_tab(dlg, key, target_file, generate_label) -> None:
    """Construct one notebook tab for *dlg* and register its widgets.

    Moved verbatim from ``DocDrafterDialog._build_tab`` (self → dlg).
    Construction-only — all behavior stays on the dialog's handlers,
    which the buttons/bindings reference through ``dlg``.
    """
    frame = tk.Frame(dlg._notebook, bg=C["base"], padx=8, pady=8)
    dlg._notebook.add(frame, text=f"  {_TAB_LABELS.get(key, key.upper())}  ")

    is_file_picker = (target_file == "")
    target_var = None

    if is_file_picker:
        # File-picker row: combobox + refresh button
        picker_row = tk.Frame(frame, bg=C["base"])
        picker_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(picker_row, text="Target file:",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        target_var = tk.StringVar()
        file_list = dlg._list_picker_files(key)
        cb = ttk.Combobox(picker_row, textvariable=target_var,
                          values=file_list, width=40, state="normal")
        if file_list:
            target_var.set(file_list[0])
        cb.pack(side=tk.LEFT, padx=(6, 0))

        def _refresh_picker(k=key, c=cb, v=target_var):
            new_list = dlg._list_picker_files(k)
            c["values"] = new_list
            if new_list and not v.get():
                v.set(new_list[0])

        ttk.Button(picker_row, text="↻",
                   command=_refresh_picker).pack(side=tk.LEFT, padx=(4, 0))
    else:
        # Static target label
        target_path = os.path.join(dlg._project_path, target_file)
        tk.Label(frame,
                 text=f"Target: {target_file}    ({target_path})",
                 bg=C["base"], fg=C["overlay0"],
                 font=("Consolas", 8)).pack(anchor=tk.W, pady=(0, 4))

    # v4 Fix 2a: read-only warning banner above the text widget.
    # Surfaces simulate-time title-rejection / mismatch warnings
    # WITHOUT polluting the editable text payload — keeps the Apply
    # button's data clean. pack_forget'd by default; the
    # `_show_warning_banner` / `_hide_warning_banner` helpers
    # toggle visibility.
    warning_var = tk.StringVar(value="")
    warning_lbl = tk.Label(
        frame, textvariable=warning_var,
        bg=C["base"], fg=C["red"], font=("Segoe UI", 9, "bold"),
        wraplength=900, justify=tk.LEFT, anchor=tk.W,
    )
    # Not packed yet — visibility managed by _show_warning_banner.

    # Editable text area for the draft
    txt_wrap = tk.Frame(frame, bg=C["mantle"])
    txt_wrap.pack(fill=tk.BOTH, expand=True)
    txt = tk.Text(txt_wrap, font=("Consolas", 9),
                  bg=C["mantle"], fg=C["text"],
                  insertbackground=C["text"],
                  relief=tk.FLAT, padx=6, pady=4, wrap=tk.WORD,
                  height=20)
    vsb = ttk.Scrollbar(txt_wrap, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Empty-state placeholder. The "(no draft yet" prefix is load-bearing
    # — _on_copy/_on_apply/_on_text_modified match it via startswith.
    txt.insert("1.0",
               "(no draft yet — click Generate to draft from the "
               "selected commit range. You can edit the draft freely "
               "before clicking Apply.)")
    txt.tag_add("placeholder", "1.0", tk.END)
    txt.tag_configure("placeholder", foreground=C["overlay0"])

    # <<Modified>> binding — user-typed edits re-enable Apply if the
    # buffer holds non-placeholder content.  Apply may have been hard-
    # disabled by a previous "all filtered" generate result; without
    # this binding the user couldn't rescue the situation by typing
    # their own bullet.  Tk fires <<Modified>> once per flag toggle;
    # _on_text_modified resets the flag so subsequent edits also fire.
    txt.bind("<<Modified>>",
             lambda _e, k=key: dlg._on_text_modified(k))
    # Clear the modified flag set by the programmatic insert above so
    # the first USER edit (not our setup insert) fires the binding.
    try:
        txt.edit_modified(False)
    except tk.TclError:
        pass

    # Buttons + status
    btn_row = tk.Frame(frame, bg=C["base"])
    btn_row.pack(fill=tk.X, pady=(6, 0))
    gen_btn = ttk.Button(btn_row, text=f"🔄 {generate_label}",
                         command=lambda k=key: dlg._on_generate(k))
    gen_btn.pack(side=tk.LEFT)
    copy_btn = ttk.Button(btn_row, text="📋 Copy",
                          command=lambda k=key: dlg._on_copy(k))
    copy_btn.pack(side=tk.LEFT, padx=(6, 0))
    apply_btn = ttk.Button(btn_row, text="✓ Apply via Proposal",
                           command=lambda k=key: dlg._on_apply(k))
    apply_btn.pack(side=tk.LEFT, padx=(6, 0))

    # C3: retry-with-feedback button — hidden until apply is rejected.
    feedback_btn = ttk.Button(btn_row, text="🔁 Regenerate with feedback",
                              command=lambda k=key: dlg._on_regenerate_with_feedback(k))
    # NOT packed yet — revealed by _on_apply_result on failure.

    # B2: per-tab checkbox to enable agentic tokensave tool use.
    ts_tools_var = tk.BooleanVar(value=True)
    ts_tools_chk = ttk.Checkbutton(btn_row, text="🔍 Tokensave tools",
                                   variable=ts_tools_var)
    ts_tools_chk.pack(side=tk.RIGHT, padx=(6, 0))

    # v4.1: tooltip explaining the Ollama-only asymmetry. The checkbox
    # only routes to the agentic LocalAgent loop for ollama /
    # openai_compatible providers — Claude CLI and Anthropic use
    # single-shot completion regardless. The always-on tokensave
    # GROUNDING block (separate from this checkbox) runs for every
    # backend; the checkbox specifically enables runtime tool-use.
    from theme import _Tooltip
    _Tooltip(
        ts_tools_chk,
        "Enables an Ollama-only agentic loop where the local model "
        "can call tokensave_search and tokensave_context as tools "
        "mid-generation. Silently ignored for Claude CLI / Anthropic "
        "/ OpenAI — those backends use single-shot completion. "
        "Disable if Ollama drafts time out. "
        "(The always-on tokensave grounding block runs separately "
        "for all backends.)",
    )
    # Novice gotcha #4: the filled text area reads as an output pane —
    # spell out that it's the literal, editable patch payload.
    _Tooltip(
        apply_btn,
        "Patch the text above into the target file (via the Proposal "
        "diff dialog). The text area is fully editable — edit freely "
        "before applying; what you see is exactly what gets patched.",
    )

    status_var = tk.StringVar(value="")
    # wraplength keeps long filter messages from pushing buttons off-screen
    # on narrow window sizes; status text wraps inside the row instead.
    status_lbl = tk.Label(btn_row, textvariable=status_var,
                          bg=C["base"], fg=C["overlay0"],
                          font=("Segoe UI", 8),
                          wraplength=420, justify=tk.LEFT)
    status_lbl.pack(side=tk.LEFT, padx=(12, 0), fill=tk.X, expand=True)

    dlg._tab_widgets[key] = {
        "frame":          frame,
        "text":           txt,
        "txt_wrap":       txt_wrap,    # v4: pack-anchor for warning banner
        "gen_btn":        gen_btn,
        "apply_btn":      apply_btn,
        "feedback_btn":   feedback_btn,
        "ts_tools_var":   ts_tools_var,
        "btn_row":        btn_row,
        "status_var":     status_var,
        "status_lbl":     status_lbl,
        "warning_var":    warning_var,   # v4: simulate-time warnings
        "warning_lbl":    warning_lbl,
        "target":         target_file,
        "target_var":     target_var,   # None for fixed-target DocTypes
    }
