"""tests/test_mcp_posture.py — tier is local; service is derived; unknown is not false.

Three properties hold this aggregate together, and each one fails silently.

**The fallback must not leak into the tier.** `EXPLICIT_INERT` means "this
project's own binding exists and does not load". Whether that matters at all
depends on a machine-wide fact — does a user-scoped `tokensave` exist — and the
same project is fine or broken depending on it. If the fallback were folded
into the tier, an inert project would carry a green "served" badge on a machine
where nothing serves it. So the cross-product below tests both fallback states
against both tiers that depend on it; the two `EXPLICIT_INERT` cases are the
load-bearing ones.

**`independent` and `covered` are orthogonal.** A machine deliberately running
explicit bindings only — fallback retired, every project bound — is independent
AND covered. A machine that retired the fallback with projects still unbound is
independent but NOT covered. One boolean would have to lie about one of them.

**An unreadable source is not a `False`.** The project has spent real effort
teaching `StrictTreeState`, `EffectiveScope` and `project_trust_state` not to
render "could not determine" as "off"; an aggregate is the easiest place to
throw that away, because a `bool` field invites it.

Pure throughout: `classify_posture` takes states and tiers, never files. No Tk,
no subprocess, no filesystem — this runs in the blocking gate.
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
    SERVICE_AUTOMATIC,
    SERVICE_SELF,
    SERVICE_UNKNOWN,
    SERVICE_UNSERVED,
    SERVICE_WRONG,
    TIER_EXPLICIT,
    TIER_EXPLICIT_INERT,
    TIER_MISBOUND,
    TIER_NONE,
    TIER_UNKNOWN,
    VERDICT_NO,
    VERDICT_UNKNOWN,
    VERDICT_YES,
    ProjectTier,
    classify_posture,
    service_of,
    tier_of,
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


# ── the cross-product: the fallback never reaches the tier ───────────────

@pytest.mark.parametrize("tier,fallback,expected", [
    # The two rows that prove the separation. Same tier, opposite outcome.
    (TIER_EXPLICIT_INERT, True,  SERVICE_AUTOMATIC),
    (TIER_EXPLICIT_INERT, False, SERVICE_UNSERVED),
    (TIER_NONE,           True,  SERVICE_AUTOMATIC),
    (TIER_NONE,           False, SERVICE_UNSERVED),
    # These do not depend on the fallback at all, and must not start to.
    (TIER_EXPLICIT,       True,  SERVICE_SELF),
    (TIER_EXPLICIT,       False, SERVICE_SELF),
    (TIER_MISBOUND,       True,  SERVICE_WRONG),
    (TIER_MISBOUND,       False, SERVICE_WRONG),
    (TIER_UNKNOWN,        True,  SERVICE_UNKNOWN),
    (TIER_UNKNOWN,        False, SERVICE_UNKNOWN),
])
def test_service_is_derived_from_tier_plus_fallback(tier, fallback, expected):
    assert service_of(tier, fallback) == expected


def test_an_inert_project_is_unserved_only_when_nothing_falls_back():
    """The false-advertising case, stated as posture rather than as a table.

    An untrusted project with a perfectly correct `.mcp.json` is running ON
    the user-scoped entry, not being shadowed by it — which is why retiring
    that entry breaks it. Measured on six projects with byte-identical files:
    trust alone separated the two that served from the four that did not.
    """
    with_fallback = _posture(userscope=LIFECYCLE_PRESENT,
                             tiers=[_p(TIER_EXPLICIT_INERT, "doom")])
    assert with_fallback.service(with_fallback.projects[0]) == SERVICE_AUTOMATIC
    assert with_fallback.covered == VERDICT_YES
    assert with_fallback.unserved == ()

    without = _posture(userscope=LIFECYCLE_RETIRED,
                       tiers=[_p(TIER_EXPLICIT_INERT, "doom")])
    assert without.service(without.projects[0]) == SERVICE_UNSERVED
    assert without.covered == VERDICT_NO
    assert [p.name for p in without.unserved] == ["doom"]


# ── fallback is read off the fact, never off the retirement flag ─────────

@pytest.mark.parametrize("state,expected", [
    (LIFECYCLE_PRESENT,  True),
    # The entry came back after being retired. It is physically there and it
    # serves; the flag being stale is a separate problem with its own banner.
    # Measured on the author's machine 2026-09-09 — reading this as "no
    # fallback" would report the very entry serving this repository as gone.
    (LIFECYCLE_RETURNED, True),
    (LIFECYCLE_RETIRED,  False),
    (LIFECYCLE_ABSENT,   False),
])
def test_automatic_fallback_follows_the_file_not_the_intent(state, expected):
    assert _posture(userscope=state).automatic_fallback is expected


def test_returned_userscope_still_covers_an_unbound_project():
    got = _posture(userscope=LIFECYCLE_RETURNED, tiers=[_p(TIER_NONE)])
    assert got.covered == VERDICT_YES
    assert got.unserved == ()


# ── independent and covered are orthogonal ───────────────────────────────

def test_explicit_only_machine_is_independent_and_covered():
    """Fallback deliberately retired, every project bound. Telling this user
    their machine is misconfigured because no fallback exists would be exactly
    backwards — it is the strictest posture available."""
    got = _posture(desktop=LIFECYCLE_RETIRED, userscope=LIFECYCLE_RETIRED,
                   tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_EXPLICIT, "b")])
    assert got.independent == VERDICT_YES
    assert got.covered == VERDICT_YES
    assert got.headline_ok is True


def test_retired_fallback_with_unbound_projects_is_independent_but_not_covered():
    got = _posture(desktop=LIFECYCLE_RETIRED, userscope=LIFECYCLE_RETIRED,
                   tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "b")])
    assert got.independent == VERDICT_YES
    assert got.covered == VERDICT_NO
    assert got.headline_ok is False, "green headline while `b` is served by nothing"


@pytest.mark.parametrize("desktop", [LIFECYCLE_PRESENT, LIFECYCLE_RETURNED])
def test_a_desktop_entry_costs_independence_however_it_got_there(desktop):
    """Desktop spawns its `tokensave` app-level, so its cwd is the app and
    every Desktop-hosted session inherits one server whatever repo it is in.
    A RETURNED entry does that just as thoroughly as one never retired."""
    got = _posture(desktop=desktop, tiers=[_p(TIER_EXPLICIT)])
    assert got.independent == VERDICT_NO
    assert got.headline_ok is False
    # Still covered — every project is served, just possibly from the wrong tree.
    assert got.covered == VERDICT_YES


def test_a_misbound_project_costs_independence_too():
    """The wrong-codebase case a Desktop-only predicate would miss entirely.

    `.mcp.json` with `-p` naming a different project answers every query from
    another repo and looks completely normal doing it. The headline asks "can
    any project be answered from the wrong codebase" — this is one of the two
    ways, so it has to count.
    """
    got = _posture(desktop=LIFECYCLE_RETIRED,
                   tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_MISBOUND, "b")])
    assert got.independent == VERDICT_NO
    assert [p.name for p in got.misbound] == ["b"]
    # It IS served — wrongly. Coverage asks a different question.
    assert got.covered == VERDICT_YES


# ── unknown is never false ───────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"desktop_read": READ_UNREADABLE},
    {"desktop_read": READ_MALFORMED},
    {"userscope_read": READ_UNREADABLE},
    {"userscope_read": READ_MALFORMED},
])
def test_an_unreadable_source_never_yields_a_confident_verdict(kwargs):
    got = _posture(tiers=[_p(TIER_EXPLICIT)], **kwargs)
    assert got.independent == VERDICT_UNKNOWN
    assert got.reads_ok is False
    assert got.headline_ok is False


def test_an_unknown_project_makes_both_verdicts_unknown():
    """One project we could not inspect is enough. Reporting the other fifteen
    as fine while silently dropping the sixteenth is how a check stops
    complaining without becoming satisfied."""
    got = _posture(tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_UNKNOWN, "b")])
    assert got.independent == VERDICT_UNKNOWN
    assert got.covered == VERDICT_UNKNOWN
    assert got.reads_ok is False


def test_headline_needs_all_three_and_not_two_of_them():
    """`headline_ok` is the only thing the green banner may consult."""
    assert _posture(tiers=[_p(TIER_EXPLICIT)]).headline_ok is True
    assert _posture(desktop=LIFECYCLE_PRESENT,
                    tiers=[_p(TIER_EXPLICIT)]).headline_ok is False
    assert _posture(userscope=LIFECYCLE_RETIRED,
                    tiers=[_p(TIER_NONE)]).headline_ok is False
    assert _posture(tiers=[_p(TIER_EXPLICIT)],
                    userscope_read=READ_MALFORMED).headline_ok is False


def test_no_projects_is_not_a_failure():
    """A machine with nothing indexed yet. Vacuously covered, and the headline
    is about routing, which is still answerable."""
    got = _posture(tiers=[])
    assert got.covered == VERDICT_YES
    assert got.independent == VERDICT_YES


# ── the classifier-state -> tier mapping ─────────────────────────────────

@pytest.mark.parametrize("state,trusted,expected", [
    ("ok",                True,  TIER_EXPLICIT),
    ("ok",                False, TIER_EXPLICIT_INERT),
    ("ok",                None,  TIER_UNKNOWN),
    # Bind this project, just not in the portable form. Hygiene notes the
    # details panel already makes — not serving faults.
    ("project_absolute",  True,  TIER_EXPLICIT),
    ("project_unbound",   True,  TIER_EXPLICIT),
    # Binds somewhere else entirely.
    ("project_mismatch",  True,  TIER_MISBOUND),
    ("project_mismatch",  False, TIER_MISBOUND),
    # Correct file, blocked from outside.
    ("project_unapproved", True, TIER_EXPLICIT_INERT),
    ("project_untrusted",  True, TIER_EXPLICIT_INERT),
    ("project_shadowed",   True, TIER_EXPLICIT_INERT),
    ("project_local_shadow", True, TIER_EXPLICIT_INERT),
    # No binding of its own. `missing` is a .mcp.json naming other servers.
    ("no_file",           True,  TIER_NONE),
    ("missing",           True,  TIER_NONE),
    ("unparseable",       True,  TIER_UNKNOWN),
    # A shape with no rule here yet: grey, never a confident badge.
    ("some_future_state", True,  TIER_UNKNOWN),
])
def test_tier_of(state, trusted, expected):
    assert tier_of(state, trusted) == expected


def test_trust_only_decides_between_explicit_and_inert():
    """It must not turn a mismatch into a non-mismatch, or invent a binding
    where there is no file. Trust gates LOADING; it does not edit content."""
    assert tier_of("project_mismatch", False) == TIER_MISBOUND
    assert tier_of("no_file", False) == TIER_NONE
    assert tier_of("no_file", None) == TIER_NONE
