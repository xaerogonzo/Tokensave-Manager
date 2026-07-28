"""tests/test_help_topics_git.py — controllers/help_topics_git.py."""
from __future__ import annotations

from types import SimpleNamespace

import controllers.help_topics_git as htg


def _make_ctl():
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

    return SimpleNamespace(_hw=_hw, _help_show=_help_show,
                           _calls=calls, _shown=shown)


# ── git_concepts ─────────────────────────────────────────────────────────────

def test_git_concepts_calls_help_show():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    assert len(ctl._shown) == 1


def test_git_concepts_passes_doc_path():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    assert "doc_path" in ctl._shown[0]
    assert "GITHUB_GUIDE.md" in ctl._shown[0]["doc_path"]


def test_git_concepts_has_commit_section():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("commit" in t.lower() for t in h2_titles)


def test_git_concepts_has_branch_section():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("branch" in t.lower() for t in h2_titles)


def test_git_concepts_has_gitignore_section():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("gitignore" in t.lower() for t in h2_titles)


def test_git_concepts_mentions_push_and_pull():
    ctl = _make_ctl()
    htg.git_concepts(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("push" in t.lower() for t in h2_titles)
    assert any("pull" in t.lower() for t in h2_titles)


# ── git_workflow ──────────────────────────────────────────────────────────────

def test_git_workflow_calls_help_show():
    ctl = _make_ctl()
    htg.git_workflow(ctl)
    assert len(ctl._shown) == 1


def test_git_workflow_passes_doc_path():
    ctl = _make_ctl()
    htg.git_workflow(ctl)
    assert "doc_path" in ctl._shown[0]
    assert "GITHUB_GUIDE.md" in ctl._shown[0]["doc_path"]


def test_git_workflow_has_committing_section():
    ctl = _make_ctl()
    htg.git_workflow(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("commit" in t.lower() for t in h2_titles)


def test_git_workflow_has_branching_section():
    ctl = _make_ctl()
    htg.git_workflow(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("branch" in t.lower() for t in h2_titles)


def test_git_workflow_undo_warning():
    ctl = _make_ctl()
    htg.git_workflow(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert any("undo" in t.lower() for t in warn_texts)


# ── git_tab ───────────────────────────────────────────────────────────────────

def test_git_tab_calls_help_show():
    ctl = _make_ctl()
    htg.git_tab(ctl)
    assert len(ctl._shown) == 1


def test_git_tab_no_doc_path():
    ctl = _make_ctl()
    htg.git_tab(ctl)
    assert "doc_path" not in ctl._shown[0] or ctl._shown[0].get("doc_path") is None


def test_git_tab_has_commit_section():
    ctl = _make_ctl()
    htg.git_tab(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("commit" in t.lower() for t in h2_titles)


def test_git_tab_warns_about_uncommitted_changes():
    ctl = _make_ctl()
    htg.git_tab(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert any("uncommitted" in t.lower() for t in warn_texts)


def test_git_tab_mentions_push_pull():
    ctl = _make_ctl()
    htg.git_tab(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("push" in t.lower() or "pull" in t.lower() for t in h2_titles)


# ── github_setup ──────────────────────────────────────────────────────────────

def test_github_setup_calls_help_show():
    ctl = _make_ctl()
    htg.github_setup(ctl)
    assert len(ctl._shown) == 1


def test_github_setup_passes_doc_path():
    ctl = _make_ctl()
    htg.github_setup(ctl)
    assert "doc_path" in ctl._shown[0]
    assert "GITHUB_GUIDE.md" in ctl._shown[0]["doc_path"]


def test_github_setup_has_five_steps():
    ctl = _make_ctl()
    htg.github_setup(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    step_headers = [t for t in h2_titles if "Step" in t]
    assert len(step_headers) >= 5


def test_github_setup_has_releases_section():
    ctl = _make_ctl()
    htg.github_setup(ctl)
    h2_titles = [t[1] for t in ctl._calls if t[0] == "h2"]
    assert any("release" in t.lower() for t in h2_titles)


def test_github_setup_warns_about_auth():
    ctl = _make_ctl()
    htg.github_setup(ctl)
    warn_texts = [t[1] for t in ctl._calls if t[0] == "warn"]
    assert any("auth" in t.lower() or "push" in t.lower() for t in warn_texts)
