"""tests/test_sync_ctrl_messages.py — what "★ Set as Active" tells the user.

Rescued from `test_pin_watcher.py`, which was deleted along with the feature it
covered. These never tested the watcher; they test `cmd_set_active`'s log, and
that log has now been wrong twice in two different directions:

* it claimed live reload was "deferred" and pointed at a wrapper docstring,
  months after a pin watcher had shipped to do exactly that;
* then it claimed the watcher would apply the change to a running Desktop,
  which measurement disproved — killing Desktop's server does not make Desktop
  start another one.

Both times the code was fine and the sentence was the defect, which is why the
sentence is under test. The invariant that matters now is that the message
tells the user the restart is only about the DEFAULT graph, and names
`graph_root` as the way to read another project without one. A message that
says only "restart Claude" is technically true and still leaves the user
believing the restart is unavoidable.
"""
from __future__ import annotations

import controllers.sync_ctrl as sync_ctrl


class _Cfg:
    """Minimal stand-in — cmd_set_active only reads `raw`."""

    def __init__(self):
        self.raw = {}


def _sync_ctrl(logs, mocker, states=("ok",)):
    """A controller whose MCP-wiring probe returns the given states."""
    mocker.patch("controllers.sync_ctrl.set_pinned")
    mocker.patch("controllers.sync_ctrl._mcp_configs",
                 return_value=[("Claude Desktop", "cfg%d.json" % i)
                               for i in range(len(states))])
    mocker.patch("controllers.sync_ctrl._classify_mcp_entry",
                 side_effect=[{"state": st} for st in states])
    return sync_ctrl.SyncStatusController(
        tab=None, cfg=_Cfg(),
        on_log=lambda msg, colour="": logs.append(msg),
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
        on_refresh=lambda: None,
        on_run=lambda *a, **k: None,
        on_run_capture=lambda *a, **k: ("", 0, 0.0),
        get_projects=lambda: [],
    )


PROJ = "D:/work/beta"


def test_pinning_scopes_the_restart_to_the_default_project(mocker):
    """"Restart Claude" on its own overstates what the restart is for."""
    logs = []
    _sync_ctrl(logs, mocker).cmd_set_active(PROJ)

    joined = " ".join(logs)
    assert "DEFAULT" in joined
    assert "restart" in joined.lower()


def test_pinning_names_graph_root_as_the_way_around_a_restart(mocker):
    """The whole point of the rewrite.

    A user who reads only that a restart is required will keep restarting
    Claude Desktop to change projects — which is the workflow this line now
    exists to stop.
    """
    logs = []
    _sync_ctrl(logs, mocker).cmd_set_active(PROJ)

    joined = " ".join(logs)
    assert "graph_root" in joined
    assert "Reference tab" in joined


def test_broken_wiring_on_one_client_still_reports_the_pin_effect(mocker):
    """This branch used to carry its own hardcoded copy of the advice.

    Fixing only the all-healthy path would leave anyone with a half-wired
    install reading whatever the stale sentence happened to be.
    """
    logs = []
    _sync_ctrl(logs, mocker, states=("ok", "missing")).cmd_set_active(PROJ)

    joined = " ".join(logs)
    assert "MCP wiring" in joined            # the warning survives
    assert "graph_root" in joined            # and the effect is still reported


def test_no_mcp_wiring_at_all_does_not_promise_anything(mocker):
    """Nothing routes through the wrapper, so there is nothing to pin for.

    Deliberately NOT given the graph_root advice: with no working MCP entry
    the user's problem is one step earlier, and offering a cross-project tip
    to someone whose tools do not run at all is noise.
    """
    logs = []
    _sync_ctrl(logs, mocker, states=("missing",)).cmd_set_active(PROJ)

    joined = " ".join(logs)
    assert "MCP wiring" in joined
    assert "graph_root" not in joined


def test_the_retired_claims_are_gone_from_the_source():
    """Two dead promises, asserted against the text the user actually reads.

    A rewrite that reinstated either sentence would otherwise pass every
    other test in this file.
    """
    with open(sync_ctrl.__file__.replace(".pyc", ".py"),
              encoding="utf-8") as fh:
        src = fh.read()

    assert "Live in-session reload is deferred" not in src
    assert "pin_watcher" not in src
