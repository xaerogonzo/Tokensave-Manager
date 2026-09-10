"""tests/test_mcp_setup_plan.py — the plan is a function of the posture, one row at a time.

`plan_independence` exists so a user can see what their machine needs before
anything touches it. Two properties make that worth having, and one row of the
mapping table below covers each.

**Deterministic, and from the posture only.** No probing inside the planner: a
plan that consults the filesystem cannot be reviewed, because the thing shown
to the user and the thing that runs are computed at different moments against
possibly different machines.

**Zero steps is a real answer.** The explicit-only posture — fallback
deliberately retired, every project carrying its own binding — is the
strictest setup available. A planner that proposed re-adding a fallback there
would be undoing the user's decision and calling it a fix. Same for the
already-independent machine, which is the common case and must produce silence
rather than busywork.

Two things must never be quietly dropped from a plan. The trust step is not
automatable — Claude Code grants trust through a prompt a person answers, and
no file can confer it — and omitting it makes a plan look complete while
leaving the binding inert. And alternatives must stay alternatives: a group is
a choice, so a renderer that iterates the list and shows every step as an
available action offers "retire this" beside "keep this".

Pure: states in, steps out. No Tk, no subprocess, no filesystem.
"""
from __future__ import annotations

import pytest

from helpers.mcp_paths import (
    LIFECYCLE_ABSENT,
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETIRED,
    LIFECYCLE_RETURNED,
)
from helpers.mcp_posture import (
    READ_MALFORMED,
    READ_OK,
    READ_UNREADABLE,
    TIER_EXPLICIT,
    TIER_EXPLICIT_INERT,
    TIER_MISBOUND,
    TIER_NONE,
    TIER_UNKNOWN,
    ProjectTier,
    classify_posture,
)
from helpers.mcp_setup import (
    GROUP_COVERAGE,
    groups,
    plan_independence,
    plan_pin_down,
    ungrouped,
)


def _p(tier, name="proj"):
    return ProjectTier(root="/" + name, display_root="/" + name, name=name,
                       tier=tier)


def _posture(*, desktop=LIFECYCLE_RETIRED, userscope=LIFECYCLE_PRESENT,
             tiers=(), desktop_read=READ_OK, userscope_read=READ_OK):
    return classify_posture(desktop_state=desktop, desktop_read=desktop_read,
                            userscope_state=userscope,
                            userscope_read=userscope_read,
                            project_tiers=tiers)


def _kinds(steps) -> list:
    return [s.kind for s in steps]


# ── the mapping table, one test per row ──────────────────────────────────

@pytest.mark.parametrize("desktop", [LIFECYCLE_ABSENT, LIFECYCLE_RETIRED])
def test_independent_and_covered_needs_nothing(desktop):
    """The common case, and the one the old dialog could not recognise."""
    got = plan_independence(_posture(
        desktop=desktop, userscope=LIFECYCLE_PRESENT,
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "b")]))

    assert got == []


def test_the_explicit_only_posture_needs_nothing_either():
    """Fallback retired ON PURPOSE, every project bound. Proposing to restore
    a fallback here would undo a deliberate decision and call it a repair."""
    got = plan_independence(_posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_EXPLICIT, "b")]))

    assert got == []


def test_a_desktop_entry_proposes_exactly_one_thing():
    got = plan_independence(_posture(desktop=LIFECYCLE_PRESENT,
                                      tiers=[_p(TIER_EXPLICIT, "a")]))

    assert _kinds(got) == ["retire_desktop"]
    assert got[0].group == "", "a single valid answer needs no choice"
    # The trade is stated in the step, not discovered afterwards.
    assert "no tokensave at all" in got[0].detail


def test_a_returned_desktop_entry_proposes_the_same_single_step():
    """One step, not a fork, and the same one whichever way the entry got back.

    RETURNED briefly offered a second option — "keep it, clear the retirement
    flag" — which is not a step toward independence at all but a decision NOT
    to be independent, phrased in terms of a config key. Shown the running
    app, a user could not tell what that button did, and said so. The Claude
    Desktop chat switch expresses that decision properly in both directions,
    so a plan answering "what stands between this machine and independent
    projects" has exactly one thing to say.
    """
    got = plan_independence(_posture(desktop=LIFECYCLE_RETURNED,
                                      tiers=[_p(TIER_EXPLICIT, "a")]))

    assert _kinds(got) == ["retire_desktop"]
    assert got[0].group == ""
    assert "accept_desktop" not in _kinds(got)


def test_no_step_names_a_config_key():
    """Every step is read by someone deciding whether to click it. "clear the
    retirement flag" told them about our bookkeeping and nothing about their
    machine."""
    for posture in (_posture(desktop=LIFECYCLE_RETURNED,
                             tiers=[_p(TIER_EXPLICIT, "a")]),
                    _posture(desktop=LIFECYCLE_PRESENT,
                             tiers=[_p(TIER_EXPLICIT, "a")]),
                    _posture(userscope=LIFECYCLE_RETIRED,
                             tiers=[_p(TIER_NONE, "b")])):
        for step in plan_independence(posture):
            blob = (step.label + " " + step.detail).lower()
            for jargon in ("retirement flag", "mcp_user_scope_retired",
                           "mcp_desktop_scope_retired", "user-scoped entry"):
                assert jargon not in blob, (step.kind, jargon)


def test_an_unserved_project_proposes_two_alternatives():
    """Restore the fallback and everything is served automatically, or bind
    each one and they serve themselves. Same coverage, different posture."""
    got = plan_independence(_posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "CleanForge")]))

    assert set(_kinds(got)) == {"restore_userscope", "bind_project"}
    assert all(s.group == GROUP_COVERAGE for s in got)
    assert any("CleanForge" in s.label for s in got)


def test_an_inert_project_with_no_fallback_is_unserved_and_planned_for():
    """The tier says its file is fine. The service says nothing serves it.
    The plan must follow the second."""
    got = plan_independence(_posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_EXPLICIT_INERT, "doom")]))

    assert "bind_project" in _kinds(got)
    assert any("doom" in s.label for s in got)


def test_a_misbound_project_is_planned_for_even_when_everything_else_is_fine():
    """Independence has two failure modes and the Desktop entry is only one.
    A `.mcp.json` naming another project answers from that tree and looks
    entirely normal doing it."""
    got = plan_independence(_posture(
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_MISBOUND, "Fortuna Lab")]))

    assert _kinds(got) == ["rebind_project"]
    assert "Fortuna Lab" in got[0].label


def test_an_unreadable_posture_proposes_no_action_at_all():
    """Acting here would be acting on a gap in our own reading. The step
    returned explains rather than does — `automatable=False`, kind `explain`."""
    for kwargs in ({"desktop_read": READ_UNREADABLE},
                   {"userscope_read": READ_MALFORMED}):
        got = plan_independence(_posture(tiers=[_p(TIER_EXPLICIT, "a")],
                                          **kwargs))
        assert _kinds(got) == ["explain"]
        assert got[0].automatable is False


def test_an_uninspectable_project_also_stops_the_planner():
    got = plan_independence(_posture(tiers=[_p(TIER_UNKNOWN, "mystery")]))

    assert _kinds(got) == ["explain"]


def test_the_plan_is_a_pure_function_of_the_posture():
    """Same posture twice, same plan. A planner that probed would drift
    between the list shown to the user and the list that runs."""
    posture = _posture(desktop=LIFECYCLE_PRESENT,
                       userscope=LIFECYCLE_RETIRED,
                       tiers=[_p(TIER_NONE, "a")])

    assert plan_independence(posture) == plan_independence(posture)


# ── pinning one project down ─────────────────────────────────────────────

def test_pin_down_includes_the_step_nothing_can_automate():
    """Dropping it makes the plan look complete while the binding stays inert
    — the failure that let "13 bound · 13 approved" read as finished on a
    machine where three had never been trusted."""
    project = _p(TIER_NONE, "Token Save Manager")
    got = plan_pin_down(_posture(tiers=[project]), project)

    assert _kinds(got) == ["bind_project", "approve_project",
                           "trust_project", "strict_tree"]
    trust = [s for s in got if s.kind == "trust_project"][0]
    assert trust.automatable is False
    assert "cannot be written to a file" in trust.detail


def test_pin_down_skips_writing_a_file_that_already_exists():
    """An inert project has a correct .mcp.json already; rewriting it would
    be a no-op reported as a fix."""
    project = _p(TIER_EXPLICIT_INERT, "doom")
    got = plan_pin_down(_posture(tiers=[project]), project)

    assert "bind_project" not in _kinds(got)
    assert "trust_project" in _kinds(got)


def test_strict_tree_is_offered_as_hardening_not_as_the_mechanism():
    """It was `true` in both projects during the desktop-collision incident
    and did nothing. Wording it as a requirement is how it got mistaken for
    one."""
    project = _p(TIER_NONE, "a")
    step = [s for s in plan_pin_down(_posture(tiers=[project]), project)
            if s.kind == "strict_tree"][0]

    assert "Optional" in step.detail
    assert "NOT what makes the binding work" in step.detail


def test_every_pin_down_step_names_its_target():
    """The planner is rendered next to a project list; a step with no target
    cannot be routed to a writer."""
    project = _p(TIER_NONE, "a")
    for step in plan_pin_down(_posture(tiers=[project]), project):
        assert step.target == project.display_root, step


# ── the group contract ───────────────────────────────────────────────────

def test_groups_and_ungrouped_partition_the_plan():
    """A renderer that used one and forgot the other would silently drop
    steps — or show a choice as a checklist."""
    got = plan_independence(_posture(
        desktop=LIFECYCLE_RETURNED, userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_NONE, "a")]))

    grouped = [s for members in groups(got).values() for s in members]
    assert len(grouped) + len(ungrouped(got)) == len(got)
    assert set(groups(got)) == {GROUP_COVERAGE}
    assert _kinds(ungrouped(got)) == ["retire_desktop"]


def test_contradictory_steps_never_share_the_ungrouped_list():
    """The specific harm: two actions that are each individually available and
    together self-contradictory. Coverage is the surviving fork — restoring the
    shared entry and binding each project reach the same place by routes that
    must not both be taken."""
    got = plan_independence(_posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_NONE, "a"), _p(TIER_NONE, "b")]))

    kinds = {s.kind for s in ungrouped(got)}
    assert not {"restore_userscope", "bind_project"} & kinds
    assert set(groups(got)) == {GROUP_COVERAGE}
