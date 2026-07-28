"""tests/test_help_topics_tools.py — controllers/help_topics_tools.py."""
from __future__ import annotations

import os
from types import SimpleNamespace

import controllers.help_topics_tools as htt


def _make_ctl(template_dir="C:\\fake\\templates"):
    calls: list = []

    def _hw():
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

    cfg = SimpleNamespace(template_dir=template_dir)
    return SimpleNamespace(_hw=_hw, _help_show=_help_show,
                           _calls=calls, _shown=shown, _cfg=cfg)


# ── codegraph ─────────────────────────────────────────────────────────────────

def test_codegraph_calls_help_show():
    ctl = _make_ctl()
    htt.codegraph(ctl)
    assert len(ctl._shown) == 1


def test_codegraph_passes_doc_path():
    ctl = _make_ctl()
    htt.codegraph(ctl)
    assert "doc_path" in ctl._shown[0]


def test_codegraph_has_init_vs_sync_section():
    ctl = _make_ctl()
    htt.codegraph(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("init" in t.lower() and "sync" in t.lower() for t in h2_titles)


def test_codegraph_warns_about_restart():
    ctl = _make_ctl()
    htt.codegraph(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert any("restart" in t.lower() for t in warn_texts)


def test_codegraph_has_codegraph_section():
    ctl = _make_ctl()
    htt.codegraph(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("codegraph" in t.lower() for t in h2_titles)


# ── ai_features ───────────────────────────────────────────────────────────────

def test_ai_features_calls_help_show():
    ctl = _make_ctl()
    htt.ai_features(ctl)
    assert len(ctl._shown) == 1


def test_ai_features_has_ask_tab_section():
    ctl = _make_ctl()
    htt.ai_features(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("ask" in t.lower() for t in h2_titles)


def test_ai_features_has_commit_section():
    ctl = _make_ctl()
    htt.ai_features(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("commit" in t.lower() for t in h2_titles)


def test_ai_features_has_code_review_section():
    ctl = _make_ctl()
    htt.ai_features(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("review" in t.lower() for t in h2_titles)


# ── precommit_hook ────────────────────────────────────────────────────────────

def test_precommit_hook_calls_help_show():
    ctl = _make_ctl()
    htt.precommit_hook(ctl)
    assert len(ctl._shown) == 1


def test_precommit_hook_has_no_verify_info():
    ctl = _make_ctl()
    htt.precommit_hook(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("no-verify" in t for t in ins_texts)


def test_precommit_hook_has_fail_open_section():
    ctl = _make_ctl()
    htt.precommit_hook(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("offline" in t.lower() or "fail" in t.lower() for t in h2_titles)


def test_precommit_hook_warns_per_project():
    ctl = _make_ctl()
    htt.precommit_hook(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert len(warn_texts) >= 1


# ── run_checks ────────────────────────────────────────────────────────────────

def test_run_checks_calls_help_show():
    ctl = _make_ctl()
    htt.run_checks(ctl)
    assert len(ctl._shown) == 1


def test_run_checks_mentions_four_checks():
    ctl = _make_ctl()
    htt.run_checks(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("four" in t.lower() or "4" in t for t in h2_titles)


def test_run_checks_mentions_pyflakes():
    ctl = _make_ctl()
    htt.run_checks(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("pyflakes" in t.lower() for t in ins_texts)


# ── integration_check ─────────────────────────────────────────────────────────

def test_integration_check_calls_help_show():
    ctl = _make_ctl()
    htt.integration_check(ctl)
    assert len(ctl._shown) == 1


def test_integration_check_passes_doc_path():
    ctl = _make_ctl()
    htt.integration_check(ctl)
    assert "doc_path" in ctl._shown[0]
    assert "UPGRADE_INTEGRATION.md" in ctl._shown[0]["doc_path"]


def test_integration_check_has_four_step_section():
    ctl = _make_ctl()
    htt.integration_check(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("4" in t or "four" in t.lower() for t in h2_titles)


# ── settings_reference ────────────────────────────────────────────────────────

def test_settings_reference_calls_help_show():
    ctl = _make_ctl()
    htt.settings_reference(ctl)
    assert len(ctl._shown) == 1


def test_settings_reference_has_core_paths_section():
    ctl = _make_ctl()
    htt.settings_reference(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("path" in t.lower() or "core" in t.lower() for t in h2_titles)


def test_settings_reference_mentions_commit_backend():
    ctl = _make_ctl()
    htt.settings_reference(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any("commit_message_backend" in t for t in ins_texts)


# ── file_locations ────────────────────────────────────────────────────────────

def test_file_locations_calls_help_show():
    ctl = _make_ctl()
    htt.file_locations(ctl)
    assert len(ctl._shown) == 1


def test_file_locations_shows_log_file():
    ctl = _make_ctl()
    htt.file_locations(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    # LOG_FILE path should appear
    from constants import LOG_FILE
    assert any(LOG_FILE in t for t in ins_texts)


def test_file_locations_shows_config_path():
    ctl = _make_ctl()
    htt.file_locations(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    from constants import _CONFIG_PATH
    assert any(_CONFIG_PATH in t for t in ins_texts)


def test_file_locations_uses_cfg_template_dir():
    template_dir = "C:\\my\\custom\\templates"
    ctl = _make_ctl(template_dir=template_dir)
    htt.file_locations(ctl)
    ins_texts = [t[1] for t in ctl._calls if t[0] == "ins"]
    assert any(template_dir in t for t in ins_texts)


# ── about ─────────────────────────────────────────────────────────────────────

def test_about_calls_help_show():
    ctl = _make_ctl()
    htt.about(ctl)
    assert len(ctl._shown) == 1


def test_about_passes_doc_path():
    ctl = _make_ctl()
    htt.about(ctl)
    assert "doc_path" in ctl._shown[0]


def test_about_mentions_changelog():
    ctl = _make_ctl()
    htt.about(ctl)
    p_texts = [t[1] for t in ctl._calls if t[0] == "p"]
    assert any("changelog" in t.lower() for t in p_texts)


def test_about_has_version_history_section():
    ctl = _make_ctl()
    htt.about(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("version" in t.lower() or "history" in t.lower() for t in h2_titles)
