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


def test_switching_leads_with_the_no_restart_fact():
    """This topic used to open with "You must restart Claude Desktop".

    That was the headline for as long as nobody checked, and it is what kept
    people quitting Desktop to change projects. The restart is real but it
    only moves the DEFAULT graph, so the first thing the topic says now is
    that you usually do not need one.
    """
    ctl = _make_ctl()
    htb.switching(ctl)
    prose = [c[1] for c in ctl._calls if c[0] in ("ok", "warn", "p")]
    assert "not need" in prose[0].lower(), prose[0]


def test_switching_names_graph_root():
    """Without the mechanism the reassurance is just a claim."""
    ctl = _make_ctl()
    htb.switching(ctl)
    body = " ".join(c[1] for c in ctl._calls if len(c) > 1)
    assert "graph_root" in body


def test_switching_still_explains_the_restart():
    """Dropping it would trade one wrong impression for another.

    Changing the default project genuinely does need a Desktop restart, and
    a user who reads only "you don't need to restart" and then pins a
    project would be left wondering why nothing changed.
    """
    ctl = _make_ctl()
    htb.switching(ctl)
    body = " ".join(c[1] for c in ctl._calls if len(c) > 1)
    assert "restart" in body.lower()
    assert "DEFAULT" in body


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


def test_switching_separates_desktop_chats_from_claude_code():
    """The topic explains pinning, so it has to say who pinning applies to.

    Claude Code runs inside the Desktop window; without this section the
    whole topic reads as advice for a client it does not actually govern.
    """
    ctl = _make_ctl()
    htb.switching(ctl)

    headings = [c[1] for c in ctl._calls if c[0] == "h2"]
    assert any("Claude Code" in h for h in headings), headings

    body = " ".join(c[1] for c in ctl._calls if len(c) > 1)
    assert "working directory" in body, "must say what Code binds to instead"
    assert "wrapper" in body, "must say how Desktop's chats reach the pin"
