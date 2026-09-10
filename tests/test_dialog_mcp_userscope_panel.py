"""tests/test_dialog_mcp_userscope_panel.py — the user-scope panel has a lifecycle.

The Desktop migration has had one since it shipped; this side did not. It
computed ``still_there = info["current"] is not None`` and branched on that,
which gives ONE rendering to two situations that need different fixes:

  * a user who never migrated, and
  * a user whose migration was undone.

Measured on the author's machine 2026-09-09: ``mcp_user_scope_retired`` was
``true`` in ``manager-config.json`` while ``~/.claude.json`` again held
``{"command": ".../tokensave.exe", "args": ["serve"]}``. The panel said
"⚠ User-scoped fallback is still active" — indistinguishable from a migration
that was never started, and it offered the retire button as though nothing had
happened. The thing worth telling the user is precisely what it could not say:
*this came back*.

Rendered against a stub host, following ``test_dialog_mcp_desktop_panel.py``:
the methods under test need only ``_body``, ``_cfg`` and ``_migration_status``,
and building a real ``MCPConfigDialog`` would drag in project discovery and a
modal-bearing constructor to read four labels.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from dialogs.mcp_migration_panel import UserScopeMigrationMixin
from helpers.mcp import USER_SCOPE_RETIRED_KEY

#: What `tokensave install --agent claude` writes, and what was found back in
#: `~/.claude.json` after the retirement. Verbatim rather than a placeholder:
#: the drift banner prints it, and the point of printing it is that the user
#: recognises the shape as something a tool they ran would produce.
INSTALLED_ENTRY = {"command": r"D:/Claude Co worker/Token Save/tokensave.exe",
                   "args": ["serve"]}


class _Cfg:
    def __init__(self, retired: bool):
        self.raw = {USER_SCOPE_RETIRED_KEY: retired, "mcp_skip_warnings": []}
        self.saved = 0

    def save(self):
        self.saved += 1


class _Host(UserScopeMigrationMixin):
    """The collaborators the panel reads, and nothing else."""

    def __init__(self, tk_root, cfg):
        self._body = tk.Frame(tk_root)
        self._cfg = cfg
        self.rendered = 0
        self.logged = []

    def _migration_status(self, rows):
        return {"bound": [("a", "/a")], "skipped": [], "remaining": [],
                "approved": [("a", "/a")], "blocked": [], "ready": True}

    def _render(self):
        self.rendered += 1

    def _log_to_app(self, text, colour):
        self.logged.append(text)

    def texts(self) -> str:
        """Every label string in the panel, joined — for substring asserts."""
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    out.append(child.cget("text"))
                walk(child)

        walk(self._body)
        return "\n".join(out)

    def buttons(self) -> list:
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                text = ""
                try:
                    text = child.cget("text")
                except tk.TclError:
                    pass
                if text and not isinstance(child, tk.Label):
                    out.append(str(text))
                walk(child)

        walk(self._body)
        return out


def _host(tk_root, mocker, *, entry, retired):
    """A panel whose two facts — the file and the flag — are set explicitly."""
    mocker.patch("dialogs.mcp_migration_panel._mcp_code_cfg_path",
                 return_value=r"C:\Users\pmpd\.claude.json")
    mocker.patch("dialogs.mcp_migration_panel._classify_mcp_entry",
                 return_value={"state": "ok", "label": "✓ canonical",
                               "current": entry, "issue": ""})
    return _Host(tk_root, _Cfg(retired))


# ── the four cells, as the panel renders them ────────────────────────────

def test_returned_states_the_fact_without_the_bookkeeping_vocabulary(
        tk_root, mocker):
    """The banner this replaced was headed "This machine recorded retiring the
    user-scoped entry — but it is back", with buttons offering to "retire it
    again" or "clear the retirement flag".

    Every noun in that was the manager's own bookkeeping. It announced a
    disagreement between a config key and a file — true, and not something a
    user has any reason to act on — while never saying what the entry DOES.
    Shown the running app, the user's reaction was that they could not tell
    what it was for and neither would anyone else.

    The decision now lives on the Overview's "Automatic serving" switch. What
    stays here is what a details panel is for: the file, the entry, and why it
    is probably back.
    """
    host = _host(tk_root, mocker, entry=INSTALLED_ENTRY, retired=True)
    host._render_user_scope_migration([])

    text = host.texts()
    assert ".claude.json" in text
    assert "tokensave install" in text
    assert "Automatic serving switch" in text
    for jargon in ("retirement flag", "user-scoped entry — but it is back",
                   "recorded decision"):
        assert jargon not in text, "bookkeeping vocabulary is back: %r" % jargon


def test_returned_offers_no_competing_decision(tk_root, mocker):
    """Two surfaces offering the same choice in different words is one too
    many, and the details panel is the one whose words were unreadable."""
    host = _host(tk_root, mocker, entry=INSTALLED_ENTRY, retired=True)
    host._render_user_scope_migration([])

    labels = " | ".join(host.buttons())
    assert "Retire it again" not in labels
    assert "retirement flag" not in labels


def test_returned_does_not_call_the_fallback_a_shadow(tk_root, mocker):
    """A bare `serve` spawned per session resolves to that session's own
    project. Calling it a shadow is what sent a user retiring the only thing
    still answering."""
    host = _host(tk_root, mocker, entry=INSTALLED_ENTRY, retired=True)
    host._render_user_scope_migration([])

    text = host.texts()
    assert "Nothing is broken by it" in text
    assert "resolves to that session's project" in text


def test_retired_is_the_completed_migration(tk_root, mocker):
    host = _host(tk_root, mocker, entry=None, retired=True)
    host._render_user_scope_migration([])

    assert "Migration complete" in host.texts()


def test_absent_does_not_congratulate_a_migration_that_never_happened(
        tk_root, mocker):
    """Same on-disk fact as RETIRED, opposite intent. The old code showed both
    as "Migration complete", crediting a user who never ran it — and hiding
    the half that matters to them: with no fallback, an unbound project gets
    nothing."""
    host = _host(tk_root, mocker, entry=None, retired=False)
    host._render_user_scope_migration([])

    text = host.texts()
    assert "Migration complete" not in text
    assert "No user-scoped tokensave entry" in text
    assert "gets no tokensave either" in text


def test_present_is_unchanged(tk_root, mocker):
    """The pre-existing cell must keep saying what it said."""
    host = _host(tk_root, mocker, entry=INSTALLED_ENTRY, retired=False)
    host._render_user_scope_migration([])

    text = host.texts()
    assert "User-scoped fallback is still active" in text
    assert "but it is back" not in text


# ── the flag-only resolution ─────────────────────────────────────────────

def test_the_flag_only_action_is_gone_with_its_button():
    """`_accept_userscope_return` asked the user about a config key. The
    Overview switch clears the same flag as a side effect of them saying "on",
    which is the same outcome without the vocabulary lesson — so the method
    went with the button rather than lingering as an unreachable branch."""
    from dialogs.mcp_migration_panel import UserScopeMigrationMixin

    assert not hasattr(UserScopeMigrationMixin, "_accept_userscope_return")
