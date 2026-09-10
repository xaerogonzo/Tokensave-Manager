"""tests/test_mcp_lifecycle_truth_table.py — one truth table, two callers.

``lifecycle_state`` began life inside ``helpers/mcp_desktop.py`` and served
only the Desktop migration. It was never Desktop-specific — it takes two
booleans — and the user-scope migration needs the identical answer, so it now
lives in ``helpers/mcp_paths.py`` beside both retirement keys.

That move is only safe while the four cells mean the same thing to both
callers. The failure it guards against is gradual: a Desktop-only assumption
creeping back in ("RETURNED means a Desktop update re-added it"), which would
be correct for one caller and wrong for the other. So the table is asserted
twice — once through the module that owns the Desktop migration and once
through the leaf the user-scope panel reads — and the identity check proves
they are one object rather than two copies that happen to agree today.

Why RETURNED matters at all, measured on the author's machine 2026-09-09:
``mcp_user_scope_retired`` was ``true`` while ``~/.claude.json`` again held a
``tokensave`` entry — re-added by a ``tokensave install`` or ``doctor`` run.
The user-scope panel had no lifecycle of its own and rendered that exactly
like "you never migrated", which is a different problem with a different fix.

Pure: two booleans in, a string out. No Tk, no subprocess, no filesystem.
"""
from __future__ import annotations

import pytest

from helpers import mcp, mcp_desktop, mcp_paths
from helpers.mcp_paths import (
    LIFECYCLE_ABSENT,
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETIRED,
    LIFECYCLE_RETURNED,
)

#: The whole contract. `entry_present` is FACT, `retired_flag` is INTENT.
TRUTH_TABLE = [
    (False, False, LIFECYCLE_ABSENT),
    (True,  False, LIFECYCLE_PRESENT),
    (False, True,  LIFECYCLE_RETIRED),
    (True,  True,  LIFECYCLE_RETURNED),
]

#: Both import paths a caller may reasonably use: the leaf that defines these,
#: and the `helpers.mcp` facade that re-exports every family name.
#:
#: `helpers.mcp_desktop` is deliberately NOT one of them. It defined these
#: originally, and a re-export shim was left behind during the move so its
#: callers would not have to change — which left five names imported and used
#: nowhere, and this project's own `run_pyflakes_check` fails on any warning.
#: The shim came out and the two callers (`doctor_ctrl`, `mcp_desktop_panel`)
#: now read the leaf. `test_the_shim_did_not_come_back` holds that line.
CALLERS = [
    pytest.param(mcp_paths, id="leaf"),
    pytest.param(mcp, id="facade"),
]


@pytest.mark.parametrize("module", CALLERS)
@pytest.mark.parametrize("present,retired,expected", TRUTH_TABLE)
def test_four_cells_hold_for_every_caller(module, present, retired, expected):
    assert module.lifecycle_state(present, retired) == expected


@pytest.mark.parametrize("module", CALLERS)
def test_the_four_constants_are_distinct(module):
    """A collapsed pair would make two rows of the table silently agree."""
    names = (module.LIFECYCLE_ABSENT, module.LIFECYCLE_PRESENT,
             module.LIFECYCLE_RETIRED, module.LIFECYCLE_RETURNED)
    assert len(set(names)) == 4, names


def test_both_callers_share_one_object_rather_than_two_copies():
    """Identity, not equality — the same rule `test_mcp_split.py` enforces.

    Two modules each defining a truth table that agrees today is exactly how
    they stop agreeing later.
    """
    assert mcp.lifecycle_state is mcp_paths.lifecycle_state
    for name in ("LIFECYCLE_ABSENT", "LIFECYCLE_PRESENT",
                 "LIFECYCLE_RETIRED", "LIFECYCLE_RETURNED"):
        assert getattr(mcp, name) is getattr(mcp_paths, name)


def test_the_shim_did_not_come_back():
    """`mcp_desktop` must not re-export these again.

    Re-adding the alias is the obvious, friendly thing to do the next time
    someone moves a caller back — and it reintroduces five imports the module
    does not use, which fails `run_pyflakes_check` and therefore the
    pre-commit review. One definition site, reachable through the leaf or the
    facade, and no third name for the same object.
    """
    for name in ("lifecycle_state", "LIFECYCLE_ABSENT", "LIFECYCLE_PRESENT",
                 "LIFECYCLE_RETIRED", "LIFECYCLE_RETURNED"):
        assert not hasattr(mcp_desktop, name), (
            "helpers.mcp_desktop.%s is back — import it from "
            "helpers.mcp_paths or helpers.mcp instead" % name)


def test_presence_is_read_from_fact_not_from_intent():
    """The distinction the user-scope drift banner depends on.

    A retired flag with the entry back on disk must NOT read as absent: the
    entry exists and is serving. Collapsing these is what let a stale flag
    describe a live fallback as gone.
    """
    assert mcp_paths.lifecycle_state(True, True) != LIFECYCLE_RETIRED
    assert mcp_paths.lifecycle_state(True, True) == LIFECYCLE_RETURNED
    assert mcp_paths.lifecycle_state(False, True) == LIFECYCLE_RETIRED
