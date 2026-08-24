"""tests/test_doctor_project_binding.py — what Doctor says about MCP bindings.

Three states, and the value is in keeping them apart:

    bound              silent
    unbound            advise, but only when there is more than one project
    bound + shadowed   a DIFFERENT diagnostic, because "fix the .mcp.json" is
                       useless advice when the file is already correct and
                       something higher-precedence is serving

Two properties matter more than the wording:

* The user-scoped entry is never called broken. It is upstream's canonical
  shape and it was measured resolving correctly, so "unbound" is honest and
  "wrong" is not.
* Shadowing is asked, never derived. Claude Code resolves local > project >
  user and dedupes by server name, so a correct binding can still be overridden
  — and a manager that computed this itself would eventually report "bound"
  while something else served, which is the exact class of confident-wrong
  answer this feature exists to remove.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from controllers.doctor_ctrl import DoctorController
from helpers.mcp import (
    SCOPE_LOCAL,
    SCOPE_PROJECT,
    SCOPE_UNKNOWN,
    SCOPE_USER,
    EffectiveScope,
)


def _ctrl(tk_root, mock_config):
    tab = tk.Frame(tk_root)
    logs = []
    ctl = DoctorController(
        tab, mock_config,
        on_log=lambda m, c="": logs.append(m),
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
    )
    return ctl, logs


def _classify_as(mocker, state, issue=""):
    mocker.patch("helpers.mcp._classify_mcp_entry",
                 return_value={"state": state, "issue": issue})


def _scope_is(mocker, scope, pending=False):
    mocker.patch("helpers.mcp.effective_scope",
                 return_value=EffectiveScope(scope, pending_approval=pending))


# ── unbound ───────────────────────────────────────────────────────────────

def test_an_unbound_project_is_advised_when_there_are_several(
        tk_root, mock_config, mocker):
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "no_file")
    mocker.patch.object(ctl, "_several_projects", return_value=True)

    ctl._report_project_binding("/proj")

    assert any("no project MCP binding" in m for m in logs), logs


def test_a_lone_project_is_not_nagged(tk_root, mock_config, mocker):
    """With one project there is no other graph to be wrong about.

    Same evidence-not-nagging rule `should_recommend_enabling` documents: widen
    what counts as risk, never drop the requirement to have any.
    """
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "no_file")
    mocker.patch.object(ctl, "_several_projects", return_value=False)

    ctl._report_project_binding("/proj")

    assert logs == []


def test_the_advice_never_calls_the_user_entry_broken(
        tk_root, mock_config, mocker):
    """It is upstream's canonical shape and was measured resolving correctly.

    "Unbound" is honest; "wrong" or "broken" would not be, and would push
    people to remove something that is working for them.
    """
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "no_file")
    mocker.patch.object(ctl, "_several_projects", return_value=True)

    ctl._report_project_binding("/proj")

    joined = " ".join(logs).lower()
    assert "broken" not in joined
    assert "wrong" not in joined


# ── bound ─────────────────────────────────────────────────────────────────

def test_a_binding_that_is_actually_serving_is_silent(
        tk_root, mock_config, mocker):
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "ok")
    _scope_is(mocker, SCOPE_PROJECT)

    ctl._report_project_binding("/proj")

    assert logs == []


def test_a_shadowed_binding_gets_its_own_diagnostic(
        tk_root, mock_config, mocker):
    """The case that makes "fix your .mcp.json" actively misleading."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "ok")
    _scope_is(mocker, SCOPE_USER)

    ctl._report_project_binding("/proj")

    joined = " ".join(logs)
    assert "precedence" in joined
    assert "will not change which server runs" in joined


@pytest.mark.parametrize("scope", [SCOPE_USER, SCOPE_LOCAL])
def test_both_shadowing_scopes_are_reported(tk_root, mock_config, mocker, scope):
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "ok")
    _scope_is(mocker, scope)

    ctl._report_project_binding("/proj")

    assert any(scope in m for m in logs), logs


def test_a_written_but_unapproved_binding_says_so(tk_root, mock_config, mocker):
    """Pending approval is a first-class state, not a failure: the file is
    right and the user simply has not said yes yet."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "ok")
    # Not yet approved, so the user-scoped definition is still the one
    # serving: the file is right and simply inert until the user says yes.
    _scope_is(mocker, SCOPE_USER, pending=True)

    ctl._report_project_binding("/proj")

    assert any("not yet approved" in m for m in logs), logs


def test_an_unknown_scope_says_nothing(tk_root, mock_config, mocker):
    """A CLI that could not answer must not produce a diagnostic.

    Reporting "shadowed" because the lookup failed would send someone hunting
    for a conflicting definition that does not exist.
    """
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, "ok")
    _scope_is(mocker, SCOPE_UNKNOWN)

    ctl._report_project_binding("/proj")

    assert logs == []


# ── bound to the wrong project ────────────────────────────────────────────

def test_a_mismatched_binding_is_reported_even_for_a_lone_project(
        tk_root, mock_config, mocker):
    """Not gated on project count: this file actively points elsewhere, so
    every answer here comes from another codebase and looks normal."""
    ctl, logs = _ctrl(tk_root, mock_config)
    _classify_as(mocker, 'project_mismatch',
                 issue='This file binds tokensave to "D:/Other". Apply to rebind.')
    mocker.patch.object(ctl, "_several_projects", return_value=False)

    ctl._report_project_binding("/proj")

    assert any("D:/Other" in m for m in logs), logs


def test_a_failed_classification_is_survivable(tk_root, mock_config, mocker):
    """Doctor must finish its run even if this one check explodes."""
    ctl, logs = _ctrl(tk_root, mock_config)
    mocker.patch("helpers.mcp._classify_mcp_entry",
                 side_effect=OSError("boom"))

    ctl._report_project_binding("/proj")        # must not raise

    assert logs == []
