"""tests/test_ask_tab.py — controllers/ask_tab.py.

Tests Tk-safe behaviors of AskTabController that don't need a running
event loop: preflight validation, header refresh, intro text, clear,
cancel_all_proposals, _claude_md_note file reading.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import tkinter as tk
from tkinter import ttk

from controllers.ask_tab import AskTabController

pytestmark = pytest.mark.tk


# ── test fixture factory ──────────────────────────────────────────────────────

def _make_ctl(tk_root, project_path=None, llm_cfg=None, enable_grounding=False):
    """Build a minimal AskTabController attached to a real Notebook."""
    nb = ttk.Notebook(tk_root)
    nb.pack()

    if llm_cfg is None:
        llm_cfg = {"provider": "openai", "model": "gpt-4", "enabled": True}

    cfg = SimpleNamespace(
        raw={"ask_tab_llm": llm_cfg},
        tokensave_exe="",
        claude_cli_exe="",
        enable_llm_grounding=enable_grounding,
        codegraph_exe="",
    )

    get_path = lambda: project_path  # noqa: E731

    ctl = AskTabController(notebook=nb, get_project_path=get_path, cfg=cfg)
    return ctl


# ── _ask_preflight ────────────────────────────────────────────────────────────

def test_preflight_no_project_returns_none(tk_root):
    ctl = _make_ctl(tk_root, project_path=None)
    result = ctl._ask_preflight()
    assert result is None


def test_preflight_ai_disabled_returns_none(tk_root, tmp_path):
    ctl = _make_ctl(tk_root, project_path=str(tmp_path),
                    llm_cfg={"provider": "openai", "enabled": False})
    result = ctl._ask_preflight()
    assert result is None


def test_preflight_ai_enabled_returns_cfg(tk_root, tmp_path):
    ctl = _make_ctl(tk_root, project_path=str(tmp_path),
                    llm_cfg={"provider": "openai", "model": "gpt-4", "enabled": True})
    result = ctl._ask_preflight()
    assert result is not None
    assert result["provider"] == "openai"


def test_preflight_with_ask_path_set_already(tk_root, tmp_path):
    """Preflight should accept a manually set _ask_path when get_project_path returns None."""
    ctl = _make_ctl(tk_root, project_path=None,
                    llm_cfg={"provider": "openai", "enabled": True})
    ctl._ask_path = str(tmp_path)
    result = ctl._ask_preflight()
    assert result is not None


def test_preflight_thread_alive_returns_none(tk_root, tmp_path):
    """Preflight returns None if a thread is already running."""
    import threading
    ctl = _make_ctl(tk_root, project_path=str(tmp_path),
                    llm_cfg={"provider": "openai", "enabled": True})
    done = threading.Event()
    t = threading.Thread(target=done.wait, daemon=True)
    t.start()
    ctl._ask_thread = t
    result = ctl._ask_preflight()
    done.set()
    t.join(timeout=1)
    assert result is None


# ── _ask_refresh_header ───────────────────────────────────────────────────────

def test_refresh_header_no_project(tk_root):
    ctl = _make_ctl(tk_root, project_path=None)
    ctl._ask_refresh_header()
    assert "no project" in ctl._ask_project_lbl.cget("text").lower()


def test_refresh_header_with_project(tk_root, tmp_path):
    ctl = _make_ctl(tk_root, project_path=str(tmp_path))
    ctl._ask_path = str(tmp_path)
    ctl._ask_refresh_header()
    label_text = ctl._ask_project_lbl.cget("text")
    assert tmp_path.name in label_text


def test_refresh_header_ai_enabled_shows_provider(tk_root, tmp_path):
    ctl = _make_ctl(tk_root, project_path=str(tmp_path),
                    llm_cfg={"provider": "ollama", "model": "llama3", "enabled": True})
    ctl._ask_refresh_header()
    model_text = ctl._ask_model_lbl.cget("text")
    assert "ollama" in model_text.lower()


def test_refresh_header_ai_disabled_shows_warning(tk_root, tmp_path):
    ctl = _make_ctl(tk_root, project_path=str(tmp_path),
                    llm_cfg={"provider": "openai", "enabled": False})
    ctl._ask_refresh_header()
    model_text = ctl._ask_model_lbl.cget("text")
    assert "disabled" in model_text.lower() or "AI" in model_text


# ── _ask_set_intro ────────────────────────────────────────────────────────────

def test_set_intro_default_provider_mentions_tools(tk_root):
    ctl = _make_ctl(tk_root,
                    llm_cfg={"provider": "openai", "model": "gpt-4", "enabled": True})
    ctl._ask_set_intro()
    text = ctl._ask_log.get("1.0", tk.END)
    assert "read_file" in text


def test_set_intro_claude_cli_provider_mentions_no_tool_access(tk_root):
    ctl = _make_ctl(tk_root,
                    llm_cfg={"provider": "claude_cli", "enabled": True})
    ctl._ask_set_intro()
    text = ctl._ask_log.get("1.0", tk.END)
    assert "Claude CLI" in text
    assert "unavailable" in text.lower() or "No tool" in text


def test_set_intro_clears_previous_content(tk_root):
    ctl = _make_ctl(tk_root)
    ctl._ask_set_intro()
    first_text = ctl._ask_log.get("1.0", tk.END)
    ctl._ask_set_intro()
    second_text = ctl._ask_log.get("1.0", tk.END)
    # Should be the same content after a second call (cleared + rewritten)
    assert first_text == second_text


# ── _ask_clear ────────────────────────────────────────────────────────────────

def test_ask_clear_resets_messages(tk_root):
    ctl = _make_ctl(tk_root)
    ctl._ask_messages = [{"role": "user", "content": "hello"}]
    ctl._ask_clear()
    assert ctl._ask_messages == []


def test_ask_clear_resets_status(tk_root):
    ctl = _make_ctl(tk_root)
    ctl._ask_status.configure(text="some old status")
    ctl._ask_clear()
    assert ctl._ask_status.cget("text") == ""


# ── _ask_stop ─────────────────────────────────────────────────────────────────

def test_ask_stop_signals_stop_event(tk_root):
    import threading
    ctl = _make_ctl(tk_root)
    stop = threading.Event()
    ctl._ask_stop_event = stop
    ctl._ask_stop()
    assert stop.is_set()


# ── cancel_all_proposals ──────────────────────────────────────────────────────

def test_cancel_all_proposals_cancels_bridges(tk_root):
    ctl = _make_ctl(tk_root)
    cancelled = []

    class FakeBridge:
        def cancel(self):
            cancelled.append(True)

    b1, b2 = FakeBridge(), FakeBridge()
    ctl._active_bridges = {b1, b2}
    ctl.cancel_all_proposals()
    assert len(cancelled) == 2


def test_cancel_all_proposals_empty_set_no_error(tk_root):
    ctl = _make_ctl(tk_root)
    ctl._active_bridges = set()
    ctl.cancel_all_proposals()  # must not raise


def test_cancel_all_proposals_exception_swallowed(tk_root):
    ctl = _make_ctl(tk_root)

    class ExplodingBridge:
        def cancel(self):
            raise RuntimeError("exploded")

    ctl._active_bridges = {ExplodingBridge()}
    ctl.cancel_all_proposals()  # must not raise


# ── _claude_md_note ───────────────────────────────────────────────────────────

def test_claude_md_note_no_path_returns_empty(tk_root):
    ctl = _make_ctl(tk_root)
    ctl._ask_path = None
    assert ctl._claude_md_note() == ""


def test_claude_md_note_no_claude_md_returns_empty(tk_root, tmp_path):
    ctl = _make_ctl(tk_root)
    ctl._ask_path = str(tmp_path)
    assert ctl._claude_md_note() == ""


def test_claude_md_note_counts_sections(tk_root, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Title\n## Section one\ntext\n## Section two\nmore text\n"
    )
    ctl = _make_ctl(tk_root)
    ctl._ask_path = str(tmp_path)
    note = ctl._claude_md_note()
    assert "2 sections" in note or "2" in note


def test_claude_md_note_zero_sections(tk_root, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Title\nno sections here.\n")
    ctl = _make_ctl(tk_root)
    ctl._ask_path = str(tmp_path)
    note = ctl._claude_md_note()
    assert "0 sections" in note or note == ""


# ── _ask_build_callbacks ──────────────────────────────────────────────────────

def test_ask_build_callbacks_returns_dict_with_keys(tk_root):
    ctl = _make_ctl(tk_root)
    cb = ctl._ask_build_callbacks()
    # Must return a dict with the four LocalAgent callback keys
    assert isinstance(cb, dict)
    for key in ("on_tool_call", "on_tool_result", "on_assistant_message", "on_done"):
        assert key in cb, f"missing key: {key}"
        assert callable(cb[key]), f"value for {key!r} is not callable"
