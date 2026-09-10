"""tests/test_sync_ctrl_pin.py — the pin says what it actually does.

## The bug

`★ Set as Active` writes `~/.tokensave/desktop-project.txt`. That file has
exactly ONE reader: `src/tokensave-wrapper.py`, which is installed only as
Claude Desktop's MCP command. Retire that entry — which is the recommended
posture, and what this manager's own Desktop migration exists to do — and
nothing reads the pin at all.

`cmd_set_active` was supposed to notice. It gathered
`_classify_mcp_entry(...)["state"]` for both global configs and warned when
none was `"ok"`. But a **retired** Desktop config classifies as `"ok"`,
deliberately: `_retired_absence` exists so a chosen absence stops being
reported as a defect, and it names four surfaces that depend on that reading.
So on a completed migration the guard's `"ok" not in states` branch could never
be reached, and the reassuring Desktop text printed instead.

Measured on the author's machine 2026-09-09: `mcpServers` empty in
`claude_desktop_config.json`, the pin naming `D:\\Random Projects\\OpenChem
Studio`, and the OUTPUT pane reporting that the pin sets the default project
for Claude Desktop's own chats. Nothing was reading it.

## What these assert

The fix is in the CALLER, not the classifier, and that distinction is itself
under test: `test_the_classifier_still_calls_a_retired_config_ok` fails if
someone "fixes" this by changing what `_classify_mcp_entry` returns, which
would break the startup banner, the Settings summary and the MCP dialog at
once.

Also here: the pin's behavioural contract. It is easy to repair the wording
and then quietly re-attach an MCP side effect later.
"""
from __future__ import annotations


from controllers.sync_ctrl import SyncStatusController
from helpers.mcp import DESKTOP_SCOPE_RETIRED_KEY, _classify_mcp_entry

#: Phrases only true while Claude Desktop actually defines the wrapper entry.
DESKTOP_CLAIMS = ("Claude Desktop's own chats", "Desktop restart")


class _Cfg:
    def __init__(self, raw=None):
        self.raw = raw if raw is not None else {DESKTOP_SCOPE_RETIRED_KEY: True}
        self.search_roots = []
        self.tokensave_exe = "tokensave.exe"


def _ctl(mocker, *, wrapper_present, cfg=None):
    """A controller with the pin write and every callback stubbed."""
    ctl = object.__new__(SyncStatusController)
    ctl._cfg = cfg or _Cfg()
    ctl.logged = []
    ctl._on_log = lambda text, colour="": ctl.logged.append(text)
    ctl._on_refresh = lambda: None
    mocker.patch("controllers.sync_ctrl.set_pinned")
    mocker.patch("controllers.sync_ctrl.clear_pinned")
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 return_value=wrapper_present)
    return ctl


def _log(ctl) -> str:
    return "\n".join(ctl.logged)


# ── the regression ───────────────────────────────────────────────────────

def test_no_desktop_entry_means_no_claim_about_desktop(mocker):
    """The exact live state that produced the false line."""
    ctl = _ctl(mocker, wrapper_present=False)

    ctl.cmd_set_active(r"D:\Random Projects\OpenChem Studio")

    text = _log(ctl)
    for claim in DESKTOP_CLAIMS:
        assert claim not in text, "claimed %r with no Desktop entry" % claim
    assert "manager's own default project" in text
    assert "decides NOTHING about MCP" in text


def test_the_classifier_still_calls_a_retired_config_ok(tmp_path):
    """The fix must be in the caller.

    `_retired_absence` returning "ok" for a deliberately empty config is
    correct and load-bearing: the startup banner, the Settings summary, the
    pin note and the MCP dialog all read it, and reporting a completed
    migration as a defect told users to undo the thing they had just done.

    If this ever fails, someone repaired `cmd_set_active` by changing what an
    MCP entry MEANS, and four surfaces changed with it.
    """
    cfg_path = tmp_path / "claude_desktop_config.json"
    cfg_path.write_text('{"mcpServers": {}}', encoding="utf-8")

    info = _classify_mcp_entry(str(cfg_path),
                               {DESKTOP_SCOPE_RETIRED_KEY: True})

    assert info["state"] == "ok"
    assert "retired" in info["label"]


def test_a_live_desktop_entry_still_gets_the_desktop_text(mocker):
    """The other half of the branch. Someone running the pin-based posture
    must keep the explanation that is true for them."""
    ctl = _ctl(mocker, wrapper_present=True)

    ctl.cmd_set_active(r"D:\Random Projects\OpenChem Studio")

    text = _log(ctl)
    assert "Claude Desktop's own chats" in text
    assert "manager's own default project" not in text


def test_an_undetectable_desktop_state_keeps_the_louder_message(mocker):
    """Same rule `App._pin_tag` already follows: if we cannot tell whether the
    pin matters, say it might. Silently downgrading to "this decides nothing"
    would be the more dangerous half of the error."""
    ctl = _ctl(mocker, wrapper_present=True)
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 side_effect=OSError("no config readable"))

    ctl.cmd_set_active(r"D:\p")

    assert "Claude Desktop's own chats" in _log(ctl)


# ── the strictest posture names what it broke ────────────────────────────

def test_with_both_globals_retired_it_names_the_unserved_projects(mocker):
    """The one posture where an unbound project genuinely has no tokensave.

    A count would send the user looking; a name tells them whether they care.
    """
    ctl = _ctl(mocker, wrapper_present=False)

    class _T:
        def __init__(self, name):
            self.name = name

    class _P:
        automatic_fallback = False
        unserved = (_T("CleanForge"), _T("Doom RPG MOD"))

    mocker.patch("helpers.mcp_posture.read_posture", return_value=_P())

    ctl.cmd_set_active(r"D:\p")

    text = _log(ctl)
    assert "CleanForge" in text and "Doom RPG MOD" in text
    assert "no tokensave at all" in text


def test_with_a_fallback_present_it_does_not_cry_wolf(mocker):
    """An unbound project served by the user-scoped entry is fine. Warning
    about it is what made the MCP dialog render working projects as broken."""
    ctl = _ctl(mocker, wrapper_present=False)

    class _P:
        automatic_fallback = True
        unserved = ()

    mocker.patch("helpers.mcp_posture.read_posture", return_value=_P())

    ctl.cmd_set_active(r"D:\p")

    assert "no tokensave at all" not in _log(ctl)


# ── the pin's behavioural contract ───────────────────────────────────────

def test_setting_the_pin_writes_the_pin_and_touches_no_mcp_config(mocker):
    """`★ Manager default` DOES set the pin. It must never acquire an MCP side
    effect — routing is decided by config files this command does not own, and
    a pin that quietly edited them would resurrect the coupling being removed.
    """
    ctl = _ctl(mocker, wrapper_present=False)
    pin = mocker.patch("controllers.sync_ctrl.set_pinned")
    apply_fix = mocker.patch("helpers.mcp_classify._apply_mcp_fix")
    remove = mocker.patch("helpers.mcp_classify.remove_mcp_entry")
    approve = mocker.patch("helpers.mcp_approval.approve_project_binding")

    ctl.cmd_set_active(r"D:\Random Projects\Fortuna Lab")

    pin.assert_called_once_with(r"D:\Random Projects\Fortuna Lab")
    assert apply_fix.call_count == 0
    assert remove.call_count == 0
    assert approve.call_count == 0


def test_auto_detect_makes_no_wrapper_promise_without_a_wrapper(mocker):
    """`cmd_auto` described the wrapper's behaviour unconditionally — the same
    error, in the command right next to it."""
    ctl = _ctl(mocker, wrapper_present=False)

    ctl.cmd_auto()

    text = _log(ctl)
    assert "wrapper picks" not in text
    assert "Restart Claude Desktop" not in text
    assert "Pin cleared" in text


def test_auto_detect_keeps_its_promise_when_a_wrapper_exists(mocker):
    ctl = _ctl(mocker, wrapper_present=True)

    ctl.cmd_auto()

    assert "wrapper picks" in _log(ctl)
