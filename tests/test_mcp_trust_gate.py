"""tests/test_mcp_trust_gate.py — trust gates MCP binding before approval does.

Found by diagnosing a live MCP Integration dialog showing two projects
"✓ bound — verified serving" and four "⚠ shadowed by the user-scoped entry".
All six had **byte-identical `.mcp.json`** and `enabledMcpjsonServers:
['tokensave']` in their own `.claude/settings.local.json`, so approval was not
the discriminator. `hasTrustDialogAccepted` on the forward-slash key was, 6/6.

Claude Code does not load a project's `.mcp.json` at all in a folder it has
not been trusted in. Such a project falls back to the user-scoped entry
without any precedence contest happening — and the dialog's advice for a
shadowed row was *"retire the user-scoped entry"*, which for these four would
have removed the only thing still answering.

The reason it went unnoticed for so long is that the fallback **works**: the
surviving user-scoped entry is a bare `serve`, and tokensave resolves by
searching upward from the spawn directory, which is the project. Every tool
call succeeded. Only the scope label was wrong.

No Tk and no subprocess here: `project_trust_state` reads a dict and
`describe_effective` is pure, so this runs in the blocking gate.
"""
from __future__ import annotations

import pytest

from helpers.mcp_approval import ADVISORY_STATES
from helpers.mcp_projects import (
    TRUST_TRUSTED,
    TRUST_UNKNOWN,
    TRUST_UNTRUSTED,
    project_trust_state,
)
from helpers.mcp_scope import SCOPE_PROJECT, SCOPE_USER, EffectiveScope, describe_effective

_ROOT = r"D:\Random Projects\LexForge"
_FWD = "D:/Random Projects/LexForge"
_BACK = "D:\\Random Projects\\LexForge"


def test_a_trusted_forward_key_is_trusted():
    assert project_trust_state(
        _ROOT, projects={_FWD: {"hasTrustDialogAccepted": True}}
    ) == TRUST_TRUSTED


def test_an_explicit_false_is_untrusted():
    assert project_trust_state(
        _ROOT, projects={_FWD: {"hasTrustDialogAccepted": False}}
    ) == TRUST_UNTRUSTED


def test_no_record_at_all_is_untrusted_not_unknown():
    """Trust is granted, never assumed.

    A directory Claude Code has never seen is one it will ask about, and
    until it does that project's `.mcp.json` is not loaded — the same
    consequence as an explicit false. Uplift Messenger was exactly this case:
    no forward-slash key, and reported shadowed.
    """
    assert project_trust_state(_ROOT, projects={}) == TRUST_UNTRUSTED


def test_a_backslash_key_does_not_confer_trust():
    """The load-bearing case, and the one that made the diagnosis hard.

    Measured on a real `~/.claude.json` with 49 project keys: all 8 carrying
    session history are forward-slash, and **none of the 34 backslash keys
    carry any**. Claude Code writes forward slashes; the backslash keys are
    artifacts of tools running `claude` in a directory, this manager's own
    status checks included. A trust flag on a leftover governs nothing.

    File Converter and LexForge both read `true` on their backslash key and
    `false` on their forward one, and both were not loading their .mcp.json.
    """
    state = project_trust_state(
        _ROOT, projects={_BACK: {"hasTrustDialogAccepted": True}})
    assert state == TRUST_UNTRUSTED, (
        "a backslash key granted trust; that key has no session history and "
        "is not the record Claude Code consults"
    )


def test_a_trusted_forward_key_wins_over_an_untrusted_backslash_one():
    assert project_trust_state(_ROOT, projects={
        _BACK: {"hasTrustDialogAccepted": False},
        _FWD: {"hasTrustDialogAccepted": True},
    }) == TRUST_TRUSTED


def test_an_unreadable_config_is_unknown_not_untrusted():
    """A fact about this tool must not be reported as a fact about the user.

    UNKNOWN keeps the row's existing verdict; UNTRUSTED would rewrite it with
    a diagnosis drawn from our own failure to read a file.
    """
    assert project_trust_state(_ROOT, projects=None,
                               claude_json_path=r"Z:\nope\.claude.json") \
        == TRUST_UNKNOWN


# ── the verdict, which is where the harm was ──────────────────────────────

def test_an_untrusted_folder_is_not_reported_as_shadowed():
    state, label, issue = describe_effective(
        EffectiveScope(SCOPE_USER, connected=True), trusted=False)
    assert state == "project_untrusted"
    assert "not trusted" in label
    assert "not loaded" in label


def test_the_untrusted_remedy_warns_against_retiring_the_user_entry():
    """The whole point of the fix.

    The old text told every shadowed row to retire the user-scoped entry. For
    a trust-blocked project that is not a fix — trust is the blocker, not
    precedence — it is the removal of the only server still answering.
    """
    _state, _label, issue = describe_effective(
        EffectiveScope(SCOPE_USER, connected=True), trusted=False)
    assert "Do NOT retire" in issue, issue
    assert "trust is the blocker, not precedence" in issue, issue
    assert "no `tokensave`" in issue, (
        "the consequence has to be stated; 'do not do X' without 'because it "
        "would leave you with nothing' reads as a style preference"
    )
    assert "cannot be done by editing a file" in issue, (
        "trust is granted interactively; a user who goes looking for the "
        "config key to set will not find one"
    )


def test_trust_does_not_override_a_row_that_is_actually_serving():
    """A project-scope answer means trust cannot be blocking, whatever we read."""
    state, _label, _issue = describe_effective(
        EffectiveScope(SCOPE_PROJECT), trusted=False)
    assert state == "ok"


def test_unknown_trust_leaves_the_shadowed_verdict_but_hedges_the_advice():
    _state, _label, issue = describe_effective(
        EffectiveScope(SCOPE_USER, connected=True), trusted=None)
    assert "only once trust is granted" in issue


def test_the_untrusted_state_offers_no_button():
    """Trust cannot be written to a file, so an Apply here could not work."""
    assert "project_untrusted" in ADVISORY_STATES


@pytest.mark.parametrize("trusted", [True, None])
def test_trust_that_is_not_false_keeps_the_precedence_verdict(trusted):
    state, _label, _issue = describe_effective(
        EffectiveScope(SCOPE_USER, connected=True), trusted=trusted)
    assert state == "project_shadowed"


# ── the retirement guard must not count an untrusted project as bound ─────
# "13 bound / 13 approved / 0 still to bind" was rendered on a machine where
# three of the thirteen had never had the trust prompt accepted. The counter
# read as a finished migration, and the button that would have broken all
# three was one click away.


def _status(rows, skips=()):
    import types as _t
    from dialogs.mcp_migration_panel import UserScopeMigrationMixin

    host = UserScopeMigrationMixin()
    host._cfg = _t.SimpleNamespace(raw={"mcp_skip_warnings": list(skips)})
    return host._migration_status(rows)


def _row(name, state):
    return (name, "D:/Random Projects/" + name, {"state": state})


def test_an_untrusted_project_is_not_counted_as_bound():
    st = _status([_row("Good", "ok"), _row("Untrusted", "project_untrusted")])
    assert [n for n, _ in st["blocked"]] == ["Untrusted"]
    assert [n for n, _ in st["bound"]] == ["Good"]
    assert st["ready"] is False, (
        "the retirement was offered while a project depended on the very "
        "entry it removes"
    )


def test_a_genuinely_shadowed_project_still_counts_as_bound():
    """The existing rule stays: those ARE blocked by the entry being removed.

    Withholding the button for them would withhold it exactly when it is the
    fix, which is the trap the readiness rule already refuses. Trust is the
    single exception, and it is an exception because it runs the other way.
    """
    st = _status([_row("Shadowed", "project_shadowed")])
    assert [n for n, _ in st["bound"]] == ["Shadowed"]
    assert st["blocked"] == []
    assert st["ready"] is True


def test_the_measured_case_reports_not_ready():
    rows = [_row("P%d" % i, "ok") for i in range(10)]
    rows += [_row("CleanForge", "project_untrusted"),
             _row("ICO File Manager", "project_untrusted"),
             _row("Uplift Messenger", "project_untrusted")]
    st = _status(rows)
    assert len(st["bound"]) == 10
    assert len(st["blocked"]) == 3
    assert st["ready"] is False


def test_untrusted_is_the_only_state_that_blocks_the_migration():
    """A second state added here silently would change the button's meaning."""
    from helpers.mcp import ADVISORY_STATES, MIGRATION_BLOCKED_STATES

    assert MIGRATION_BLOCKED_STATES == {"project_untrusted"}
    assert MIGRATION_BLOCKED_STATES <= ADVISORY_STATES, (
        "a blocking state must also be advisory, or the row would offer an "
        "Apply button for something no file can fix"
    )
