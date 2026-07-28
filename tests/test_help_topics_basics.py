"""tests/test_help_topics_basics.py — controllers/help_topics_basics.py.

All functions here are pure rendering routines that call ctl._hw() +
ctl._help_show(_fill). Tests verify that each runs without error and
that _help_show (and therefore the fill function) was invoked.
"""
from __future__ import annotations

from types import SimpleNamespace

import controllers.help_topics_basics as htb


# ── stub controller factory ──────────────────────────────────────────────────

def _make_ctl():
    """Return a minimal stub for HelpTabController."""
    calls: list = []

    def _hw():
        noop = lambda *a, **k: None
        h1   = lambda t, *a, **k: calls.append(("h1", t))
        h2   = lambda t, *a, **k: calls.append(("h2", t))
        p    = lambda t, *a, **k: calls.append(("p", t))
        warn = lambda t, *a, **k: calls.append(("warn", t))
        ok   = lambda t, *a, **k: calls.append(("ok", t))
        dim  = lambda t, *a, **k: calls.append(("dim", t))
        br   = lambda *a, **k: calls.append(("br",))
        ins  = lambda t, *a, **k: calls.append(("ins", t))
        return h1, h2, p, warn, ok, dim, br, ins

    shown: list = []

    def _help_show(fill, **kwargs):
        fill()
        shown.append(kwargs)

    return SimpleNamespace(_hw=_hw, _help_show=_help_show,
                           _calls=calls, _shown=shown)


# ── switching ────────────────────────────────────────────────────────────────

def test_switching_calls_help_show():
    ctl = _make_ctl()
    htb.switching(ctl)
    assert len(ctl._shown) == 1


def test_switching_emits_h1():
    ctl = _make_ctl()
    htb.switching(ctl)
    h1_titles = [t[1] for t in ctl._calls if t[0] == "h1"]
    assert any("Switch" in t for t in h1_titles)


def test_switching_contains_restart_warning():
    ctl = _make_ctl()
    htb.switching(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert any("restart" in t.lower() for t in warn_texts)


# ── window_tray ──────────────────────────────────────────────────────────────

def test_window_tray_calls_help_show():
    ctl = _make_ctl()
    htb.window_tray(ctl)
    assert len(ctl._shown) == 1


def test_window_tray_has_model_section():
    ctl = _make_ctl()
    htb.window_tray(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("model" in t.lower() for t in h2_titles)


def test_window_tray_has_controls_section():
    ctl = _make_ctl()
    htb.window_tray(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("control" in t.lower() for t in h2_titles)


# ── context_menu ─────────────────────────────────────────────────────────────

def test_context_menu_calls_help_show():
    ctl = _make_ctl()
    htb.context_menu(ctl)
    assert len(ctl._shown) == 1


def test_context_menu_has_git_section():
    ctl = _make_ctl()
    htb.context_menu(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("git" in t.lower() for t in h2_titles)


def test_context_menu_has_shadow_links_entry():
    ctl = _make_ctl()
    htb.context_menu(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("Shadow" in t for t in ins_texts)


def test_context_menu_shadow_links_has_v72_note():
    """Verify the v7.2+ project.json note is present (R9-TS7 integration)."""
    ctl = _make_ctl()
    htb.context_menu(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("v7.2" in t for t in ins_texts)


# ── scaffold ─────────────────────────────────────────────────────────────────

def test_scaffold_calls_help_show():
    ctl = _make_ctl()
    htb.scaffold(ctl)
    assert len(ctl._shown) == 1


def test_scaffold_mentions_tokensave_init():
    ctl = _make_ctl()
    htb.scaffold(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("tokensave init" in t for t in ins_texts)


# ── retrofit ─────────────────────────────────────────────────────────────────

def test_retrofit_calls_help_show():
    ctl = _make_ctl()
    htb.retrofit(ctl)
    assert len(ctl._shown) == 1


def test_retrofit_mentions_claude_md():
    ctl = _make_ctl()
    htb.retrofit(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("CLAUDE.md" in t for t in ins_texts)


# ── nuitka ───────────────────────────────────────────────────────────────────

def test_nuitka_calls_help_show():
    ctl = _make_ctl()
    htb.nuitka(ctl)
    assert len(ctl._shown) == 1


def test_nuitka_mentions_build_ps1():
    ctl = _make_ctl()
    htb.nuitka(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("build.ps1" in t for t in ins_texts)


# ── scaffold_column ──────────────────────────────────────────────────────────

def test_scaffold_column_calls_help_show():
    ctl = _make_ctl()
    htb.scaffold_column(ctl)
    assert len(ctl._shown) == 1


def test_scaffold_column_mentions_basic_instructions():
    ctl = _make_ctl()
    htb.scaffold_column(ctl)
    assert any("BASIC_INSTRUCTIONS" in t[1]
               for t in ctl._calls if t[0] in ("p", "ok", "ins"))


# ── autodetect ───────────────────────────────────────────────────────────────

def test_autodetect_calls_help_show():
    ctl = _make_ctl()
    htb.autodetect(ctl)
    assert len(ctl._shown) == 1


def test_autodetect_mentions_wrapper():
    ctl = _make_ctl()
    htb.autodetect(ctl)
    p_texts = [t[1] for t in ctl._calls if t[0] == "p"]
    assert any("wrapper" in t.lower() for t in p_texts)


# ── init_vs_sync ─────────────────────────────────────────────────────────────

def test_init_vs_sync_calls_help_show():
    ctl = _make_ctl()
    htb.init_vs_sync(ctl)
    assert len(ctl._shown) == 1


def test_init_vs_sync_has_two_sections():
    ctl = _make_ctl()
    htb.init_vs_sync(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("init" in t.lower() for t in h2_titles)
    assert any("sync" in t.lower() for t in h2_titles)


# ── categories ───────────────────────────────────────────────────────────────

def test_categories_calls_help_show():
    ctl = _make_ctl()
    htb.categories(ctl)
    assert len(ctl._shown) == 1


def test_categories_has_override_section():
    ctl = _make_ctl()
    htb.categories(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("override" in t.lower() or "single" in t.lower()
               for t in h2_titles)
