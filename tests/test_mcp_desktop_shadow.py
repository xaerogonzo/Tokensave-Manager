"""tests/test_mcp_desktop_shadow.py — the wrong tree, and the innocent one.

The bug these pin: a session in Token Save Manager Source asked
``tokensave_status`` over MCP and was answered from OpenChem Studio — 741 files
against 301, branch ``joback-thermophysical`` against ``Roadmap-11``, and a
``db_size_bytes`` that matched the other repo's database byte for byte. Both
projects' ``.mcp.json`` files were correct and both project servers were
running. Claude Desktop defines its own ``tokensave`` pointing at the manager's
wrapper, Claude Code dedupes MCP servers by NAME, and Desktop's won.

Two regression tests carry the weight, and the second matters as much as the
first:

  (a) Desktop serves A while B is inspected  -> B is shadowed, even though
      every individual config file looks correct.
  (b) Desktop serves A while A is inspected  -> NOT a shadow. A running
      Desktop wrapper is not a fault in itself, and a rule that says otherwise
      would fire against the one project that is working. This Doctor has
      already had to be fixed once for nagging.

The retirement tests exist because the migration removes a feature (Claude
Desktop chat loses tokensave) and edits a file Desktop rewrites from memory on
its own schedule. Every guard here corresponds to a way that could go wrong
silently: a stale preview, a half-applied write across the two physical views
of one UWP configuration, an Undo that reconstructs a canonical entry over the
user's custom one, or a Beta install caught in a blanket sweep.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers import mcp_desktop
from helpers.mcp_desktop import (
    LIFECYCLE_ABSENT,
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETIRED,
    LIFECYCLE_RETURNED,
    change_set,
    discover_desktop_configs,
    lifecycle_state,
    restore,
    retire,
)
from helpers.mcp_shadow import (
    SHADOW_ACTIVE,
    SHADOW_DORMANT,
    SHADOW_NONE,
    SHADOW_SERVING_THIS,
    SHADOW_UNCERTAIN,
    classify_shadow,
    structural_note,
    wrapper_servers,
)

PROJECT_A = r"D:\Random Projects\OpenChem Studio"
PROJECT_B = r"D:\Claude Co worker\Token Save Manager Source"

WRAPPER_ENTRY = {
    "command": r"C:\Users\pmpd\miniconda3\pythonw.exe",
    "args": [r"D:\Claude Co worker\Token Save Manager Source\src"
             r"\tokensave-wrapper.py"],
}


class FakeServer:
    """Enough of ``TokensaveServer`` for the pure classifier.

    Constructed rather than mocked: ``classify_shadow`` reads attributes only,
    and building the five states from real process output would mean starting
    five servers.
    """

    def __init__(self, pid, project, selection="", attribution="authoritative",
                 started_at=100.0, is_guess=False):
        self.pid = pid
        self.project = project
        self.selection = selection
        self.attribution = attribution
        self.started_at = started_at
        self.is_guess = is_guess


def _desktop_wrapper(project, pid=14796, started_at=100.0, **kw):
    """A server the manager's wrapper spawned — i.e. Claude Desktop's."""
    return FakeServer(pid, project, selection="pin", started_at=started_at,
                      **kw)


def _code_server(project, pid=33324, started_at=200.0):
    """A server a Claude Code session spawned from its own .mcp.json."""
    return FakeServer(pid, project, selection="", started_at=started_at)


# ── the two regressions ────────────────────────────────────────────────────

def test_desktop_serving_another_project_shadows_this_one():
    """(a) The original failure: correct files, healthy server, wrong tree."""
    servers = [_desktop_wrapper(PROJECT_A), _code_server(PROJECT_B)]

    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=servers)

    assert verdict.state == SHADOW_ACTIVE
    assert verdict.is_fault
    assert verdict.served_project == PROJECT_A
    # The project it is actually serving must be named -- "something is wrong"
    # is what cost four queries before the diagnosis was believed.
    assert PROJECT_A in verdict.detail
    # And it must say WHY that project was chosen, because "pin" is the fact
    # that makes it unfixable from inside the session.
    assert "pinned" in verdict.detail


def test_desktop_serving_this_project_is_not_a_shadow():
    """(b) The over-eager-nag guard: same server, inspected from its own project."""
    servers = [_desktop_wrapper(PROJECT_A), _code_server(PROJECT_B)]

    verdict = classify_shadow(PROJECT_A, desktop_entry_present=True,
                              servers=servers)

    assert verdict.state == SHADOW_SERVING_THIS
    assert not verdict.is_fault
    assert not verdict.needs_attention


# ── the rest of the state model ────────────────────────────────────────────

def test_no_desktop_entry_means_nothing_can_shadow():
    verdict = classify_shadow(PROJECT_B, desktop_entry_present=False,
                              servers=[_desktop_wrapper(PROJECT_A)])
    assert verdict.state == SHADOW_NONE
    assert not verdict.needs_attention


def test_entry_without_a_running_wrapper_is_dormant_not_a_fault():
    """Desktop closed. A config-level fact must not be reported as a runtime one."""
    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=[_code_server(PROJECT_B)])
    assert verdict.state == SHADOW_DORMANT
    assert not verdict.is_fault
    assert not verdict.is_runtime


def test_guessed_attribution_is_uncertain_never_a_confirmed_shadow():
    """A -shm correlation names a project as a guess. Wording must not outrun it."""
    guessed = _desktop_wrapper(PROJECT_A, attribution="heuristic",
                               is_guess=True)

    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=[guessed])

    assert verdict.state == SHADOW_UNCERTAIN
    assert not verdict.is_fault          # never accuse on a guess
    assert verdict.needs_attention       # but "cannot tell" is still actionable
    assert "cannot" in verdict.detail


def test_wrapper_with_no_project_is_uncertain():
    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=[_desktop_wrapper(None)])
    assert verdict.state == SHADOW_UNCERTAIN


def test_only_wrapper_servers_count_as_desktops():
    """A Claude Code server for another project is not a Desktop shadow.

    Both kinds appear in the same process list; only the wrapper's own run
    record distinguishes them, and mistaking one for the other would report a
    shadow whenever any other project had a session open.
    """
    servers = [_code_server(PROJECT_A, pid=1), _code_server(PROJECT_B, pid=2)]
    assert wrapper_servers(servers) == []
    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=servers)
    assert verdict.state == SHADOW_DORMANT


def test_newest_wrapper_server_is_the_one_reported():
    """Several wrappers can be alive; the newest is the one the user will see."""
    servers = [_desktop_wrapper(PROJECT_A, pid=1, started_at=10.0),
               _desktop_wrapper(PROJECT_A, pid=2, started_at=99.0)]
    verdict = classify_shadow(PROJECT_B, desktop_entry_present=True,
                              servers=servers)
    assert verdict.pid == 2


def test_path_spelling_variants_are_the_same_project():
    """`D:\\P\\Foo` and `D:/P/Foo/` are one directory, not a shadow."""
    servers = [_desktop_wrapper("D:/Random Projects/OpenChem Studio/")]
    verdict = classify_shadow(PROJECT_A, desktop_entry_present=True,
                              servers=servers)
    assert verdict.state == SHADOW_SERVING_THIS


def test_structural_note_only_when_several_projects():
    assert structural_note(1) == ""
    assert "only one project at a time" in structural_note(10)


# ── lifecycle: intent, not absence ─────────────────────────────────────────

@pytest.mark.parametrize("present,retired,expected", [
    (True,  False, LIFECYCLE_PRESENT),
    (False, True,  LIFECYCLE_RETIRED),
    (False, False, LIFECYCLE_ABSENT),
    (True,  True,  LIFECYCLE_RETURNED),
])
def test_lifecycle_state(present, retired, expected):
    assert lifecycle_state(present, retired) == expected


def test_returned_is_distinguishable_from_never_migrated():
    """The whole reason the flag records intent rather than absence."""
    assert lifecycle_state(True, True) != lifecycle_state(True, False)
    assert lifecycle_state(False, True) != lifecycle_state(False, False)


# ── discovery and the change set ───────────────────────────────────────────

def _write_cfg(path, entry=WRAPPER_ENTRY, extra=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"mcpServers": {}, "preferences": extra or {"sidebarMode": "x"}}
    if entry is not None:
        data["mcpServers"]["tokensave"] = entry
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def _uwp_path(fake_home, package="Claude_pzs8sxrjxfjjc"):
    return os.path.join(str(fake_home), "AppData", "Local", "Packages",
                        package, "LocalCache", "Roaming", "Claude",
                        "claude_desktop_config.json")


def _traditional_path(fake_home):
    return os.path.join(str(fake_home), "AppData", "Roaming", "Claude",
                        "claude_desktop_config.json")


def test_traditional_copy_is_part_of_the_active_uwp_configuration(fake_home):
    """Both physical files are one logical config, so both must change.

    This is the asymmetric-redirection case: a UWP Desktop reads the package
    copy, the non-UWP manager sees the `%APPDATA%` one, and both exist. An
    earlier version labelled the traditional file as its own UWP package —
    because the FILENAME starts with `claude_` — which dropped it from the
    change set and would have left half the migration unapplied.
    """
    _write_cfg(_uwp_path(fake_home))
    _write_cfg(_traditional_path(fake_home))

    configs = discover_desktop_configs()
    by_id = {c.install_id: c for c in configs}

    assert "traditional" in by_id, [c.install_id for c in configs]
    assert by_id["traditional"].is_active
    assert by_id["uwp:Claude_pzs8sxrjxfjjc"].is_active

    will, wont = change_set(configs)
    assert len(will) == 2
    assert wont == []


def test_inactive_package_is_listed_but_not_changed(fake_home):
    """A different installation is not another view of the active one.

    Which package counts as active is decided by
    ``helpers.mcp._resolve_desktop_cfg_path``, whose documented heuristic is
    "most recently modified" — this module must not invent a second answer to
    that question. The mtimes are therefore set explicitly rather than left to
    write order, so the test pins the exclusion rule and not the filesystem's
    timestamp resolution.
    """
    stable = _write_cfg(_uwp_path(fake_home))
    beta = _write_cfg(_uwp_path(fake_home, "Claude_beta00000000"))
    trad = _write_cfg(_traditional_path(fake_home))
    os.utime(stable, (2_000_000_000, 2_000_000_000))   # newest -> active
    os.utime(beta, (1_000_000_000, 1_000_000_000))

    will, wont = change_set(discover_desktop_configs())

    assert sorted(c.path for c in will) == sorted([stable, trad])
    assert [c.path for c in wont] == [beta]


def test_config_without_the_entry_is_not_in_the_change_set(fake_home):
    _write_cfg(_uwp_path(fake_home), entry=None)
    _write_cfg(_traditional_path(fake_home), entry=None)
    will, wont = change_set(discover_desktop_configs())
    assert will == [] and wont == []
    assert not mcp_desktop.desktop_entry_present()


def test_malformed_config_reports_its_error_and_is_left_alone(fake_home):
    path = _uwp_path(fake_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    configs = discover_desktop_configs()
    broken = [c for c in configs if c.path == path][0]

    assert broken.error
    assert not broken.has_entry
    assert change_set(configs)[0] == []


# ── retirement ─────────────────────────────────────────────────────────────

def test_retire_removes_from_every_active_view_and_records_the_entry(fake_home):
    uwp = _write_cfg(_uwp_path(fake_home))
    trad = _write_cfg(_traditional_path(fake_home))

    result = retire(discover_desktop_configs())

    assert result.ok, result.detail
    assert set(result.changed) == {uwp, trad}
    assert result.entry == WRAPPER_ENTRY
    for path in (uwp, trad):
        with open(path, encoding="utf-8") as fh:
            assert "tokensave" not in json.load(fh)["mcpServers"]
    # The restart caveat is not optional: the running server resolved its
    # project at startup and keeps serving it until Desktop restarts.
    assert "restart" in result.detail.lower()


def test_retire_preserves_everything_else_in_the_file(fake_home):
    """Desktop keeps its own preferences in this file. Do not eat them."""
    prefs = {"sidebarMode": "epitaxy", "coworkWebSearchEnabled": True}
    _write_cfg(_uwp_path(fake_home), extra=prefs)
    _write_cfg(_traditional_path(fake_home), extra=prefs)

    retire(discover_desktop_configs())

    with open(_uwp_path(fake_home), encoding="utf-8") as fh:
        assert json.load(fh)["preferences"] == prefs


def test_retire_refuses_when_a_file_changed_since_the_preview(fake_home):
    """Desktop rewrites this file on its own schedule, mid-preview."""
    uwp = _write_cfg(_uwp_path(fake_home))
    _write_cfg(_traditional_path(fake_home))
    configs = discover_desktop_configs()

    _write_cfg(uwp, extra={"sidebarMode": "changed-underneath"})

    result = retire(configs)

    assert not result.ok
    assert result.changed == []
    assert "changed since this preview" in result.detail
    # Refused entirely, not half-applied across the two views.
    with open(_traditional_path(fake_home), encoding="utf-8") as fh:
        assert "tokensave" in json.load(fh)["mcpServers"]


def test_retire_with_nothing_to_do_says_so(fake_home):
    _write_cfg(_uwp_path(fake_home), entry=None)
    result = retire(discover_desktop_configs())
    assert not result.ok
    assert "nothing to retire" in result.detail


def test_retire_writes_a_backup(fake_home):
    _write_cfg(_uwp_path(fake_home))
    _write_cfg(_traditional_path(fake_home))
    result = retire(discover_desktop_configs())
    directory = os.path.dirname(_uwp_path(fake_home))
    assert any(".backup." in n for n in os.listdir(directory))
    assert result.record["backups"]


# ── undo ───────────────────────────────────────────────────────────────────

def test_undo_restores_the_exact_custom_entry(fake_home):
    """Not a reconstructed canonical entry — the user's own command and args."""
    custom = {"command": "pythonw.exe",
              "args": ["wrapper.py", "--flag"], "env": {"X": "1"}}
    _write_cfg(_uwp_path(fake_home), entry=custom)
    _write_cfg(_traditional_path(fake_home), entry=custom)

    retired = retire(discover_desktop_configs())
    assert retired.ok

    restored = restore(retired.record)

    assert restored.ok, restored.detail
    with open(_uwp_path(fake_home), encoding="utf-8") as fh:
        assert json.load(fh)["mcpServers"]["tokensave"] == custom


def test_undo_leaves_a_file_that_changed_after_retirement_alone(fake_home):
    """Undo restores what the manager retired, not whatever is there now."""
    _write_cfg(_uwp_path(fake_home))
    _write_cfg(_traditional_path(fake_home))
    retired = retire(discover_desktop_configs())

    other = {"command": "somebody-elses-server", "args": []}
    with open(_uwp_path(fake_home), encoding="utf-8") as fh:
        data = json.load(fh)
    data["mcpServers"]["tokensave"] = other
    with open(_uwp_path(fake_home), "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    result = restore(retired.record)

    assert not result.ok
    assert any("changed since retirement" in why for _, why in result.failed)
    with open(_uwp_path(fake_home), encoding="utf-8") as fh:
        assert json.load(fh)["mcpServers"]["tokensave"] == other


def test_undo_without_a_record_does_nothing(fake_home):
    result = restore({})
    assert not result.ok
    assert "No recorded" in result.detail


# ── a chosen absence is not a defect ───────────────────────────────────────

def test_retired_desktop_config_is_not_reported_as_missing(fake_home):
    """The bug: the dialog offered "Apply this fix" to undo the migration.

    Straight after retiring, the Claude Desktop block went red with "No
    'tokensave' MCP server is configured" and an Apply button that would put
    the shadowing entry back — in the same dialog whose next panel reported
    the migration complete. Four surfaces read this verdict, so the fix has to
    live in the classifier, exactly as the user-scoped clause beside it does.
    """
    from helpers.mcp import _classify_mcp_entry

    path = _write_cfg(_uwp_path(fake_home), entry=None)

    plain = _classify_mcp_entry(path, {})
    retired = _classify_mcp_entry(
        path, {mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY: True})

    assert plain["state"] == "missing"          # never migrated: offer it
    assert retired["state"] == "ok"             # migrated: leave it alone
    assert "retired" in retired["label"]
    # The trade must be stated where the user is looking at the empty config.
    assert "Claude Desktop chat has no tokensave" in retired["issue"]


def test_the_desktop_flag_does_not_excuse_an_unbound_project(fake_home,
                                                             tmp_path):
    """Retiring Desktop is what makes project bindings matter, not optional.

    A project `.mcp.json` with no entry still needs binding; if the Desktop
    flag suppressed that too, the migration would silently turn every unbound
    project green while leaving it with no tokensave at all.
    """
    from helpers.mcp import _classify_mcp_entry

    project = tmp_path / "proj"
    (project / ".tokensave").mkdir(parents=True)
    mcp_json = project / ".mcp.json"
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")

    info = _classify_mcp_entry(
        str(mcp_json), {mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY: True})

    assert info["state"] == "missing"


def test_is_retired_reads_intent_not_absence():
    assert mcp_desktop.is_retired(
        {mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY: True}) is True
    assert mcp_desktop.is_retired({}) is False
    assert mcp_desktop.is_retired(None) is False


# ── the hard gate ──────────────────────────────────────────────────────────

class _GateHost:
    """The mixin with just the collaborators ``_desktop_gate`` reads.

    Built rather than mocked so the gate is exercised as the dialog calls it,
    without a Tk root: this is a pure decision about two facts.
    """

    def __init__(self, ready=True, cached=None):
        from dialogs.mcp_desktop_panel import DesktopMigrationMixin
        self._gate = DesktopMigrationMixin._desktop_gate.__get__(self)
        self._ready = ready
        self._desktop_running = cached

    def _migration_status(self, rows):
        return {"ready": self._ready, "bound": [("p", "r")]}

    def __call__(self, rows=(), live=True):
        return self._gate(rows, live=live)


def _desktop_running(mocker, value, detail="detail"):
    mocker.patch("helpers.mcp_desktop.desktop_app_running",
                 return_value=(value, detail))


def test_gate_blocks_while_claude_desktop_is_running(mocker):
    """Desktop rewrites this file from memory every 1-2 minutes.

    A removal applied while it runs is silently restored, which reads as the
    manager failing rather than as a race — so this is a hard gate, not a
    banner.
    """
    _desktop_running(mocker, True)
    allowed, reason = _GateHost()()
    assert not allowed
    assert "Quit Claude Desktop first" in reason


def test_gate_blocks_when_it_cannot_tell(mocker):
    """"Could not determine" must not be treated as a yes."""
    _desktop_running(mocker, None, "no process information was returned")
    allowed, reason = _GateHost()()
    assert not allowed
    assert "Could not determine" in reason


def test_gate_blocks_until_projects_are_bound_or_skipped(mocker):
    _desktop_running(mocker, False)
    allowed, reason = _GateHost(ready=False)()
    assert not allowed
    assert "Bind or skip" in reason


def test_gate_allows_when_desktop_is_closed_and_projects_are_ready(mocker):
    _desktop_running(mocker, False)
    allowed, reason = _GateHost()()
    assert allowed
    assert reason == ""


def test_gate_waits_rather_than_asking_on_the_render_path():
    """Rendering reads the cache; it must never spawn the check itself.

    Asking costs a subprocess, and `_render` runs on the Tk thread.
    """
    allowed, reason = _GateHost(cached=None)(live=False)
    assert not allowed
    assert "Checking" in reason


def test_gate_uses_the_cached_answer_when_not_live():
    allowed, _reason = _GateHost(cached=(False, "closed"))(live=False)
    assert allowed


def test_the_write_time_gate_re_asks_rather_than_trusting_the_cache(mocker):
    """Desktop may have been opened since the panel rendered.

    The cache says "closed"; the live check says otherwise and must win, or
    the edit is made into a file Desktop is about to rewrite from memory.
    """
    _desktop_running(mocker, True)
    allowed, reason = _GateHost(cached=(False, "stale: closed"))(live=True)
    assert not allowed
    assert "Quit Claude Desktop first" in reason


def test_claude_code_is_not_mistaken_for_claude_desktop(mocker):
    """Both ship as `claude.exe`; only the path separates them.

    Getting this wrong blocks the migration permanently, because the user is
    running Claude Code in order to perform it.
    """
    mocker.patch("sys.platform", "win32")
    mocker.patch(
        "helpers.tokensave_daemon._enumerate_processes",
        return_value=[{
            "pid": 33324,
            "exe": r"C:\Users\p\AppData\Roaming\Claude\claude-code\2.1\claude.exe",
            "cmdline": "claude", "started_at": 1.0}])

    running, detail = mcp_desktop.desktop_app_running()

    assert running is False
    assert "not running" in detail


def test_the_uwp_desktop_binary_is_recognised(mocker):
    mocker.patch("sys.platform", "win32")
    mocker.patch(
        "helpers.tokensave_daemon._enumerate_processes",
        return_value=[{
            "pid": 20724,
            "exe": (r"C:\Program Files\WindowsApps"
                    r"\Claude_1.37_x64__pzs8sxrjxfjjc\app\claude.exe"),
            "cmdline": "claude", "started_at": 1.0}])

    running, detail = mcp_desktop.desktop_app_running()

    assert running is True
    assert "20724" in detail


def test_an_unrecognised_claude_exe_counts_as_desktop(mocker):
    """Blocking is the safe error for an operation that removes a feature."""
    mocker.patch("sys.platform", "win32")
    mocker.patch(
        "helpers.tokensave_daemon._enumerate_processes",
        return_value=[{"pid": 5, "exe": r"C:\somewhere\odd\claude.exe",
                       "cmdline": "claude", "started_at": 1.0}])

    running, detail = mcp_desktop.desktop_app_running()

    assert running is True
    assert "unrecognised" in detail


def test_a_failed_enumeration_is_unknown_and_names_the_real_reason(mocker):
    """The bug this pins cost a whole debugging round.

    The manager ran with a PATH that had no PowerShell directory, so every
    enumeration returned `[]`. That was reported as "no process information
    was returned" — which reads as a fact about Claude Desktop rather than an
    admission that the question was never asked, and it sat next to a panel
    line confidently stating Desktop was closed.
    """
    from helpers.tokensave_daemon import EnumerationFailed

    mocker.patch("sys.platform", "win32")
    mocker.patch("helpers.tokensave_daemon._enumerate_processes",
                 side_effect=EnumerationFailed(
                     "PowerShell could not be located"))

    running, detail = mcp_desktop.desktop_app_running()

    assert running is None
    assert "PowerShell could not be located" in detail


def test_a_successful_enumeration_finding_nothing_is_an_answer(mocker):
    """Empty is not the same as failed, and must not block the migration."""
    mocker.patch("sys.platform", "win32")
    mocker.patch("helpers.tokensave_daemon._enumerate_processes",
                 return_value=[])

    running, detail = mcp_desktop.desktop_app_running()

    assert running is False
    assert "not running" in detail


def test_powershell_is_resolved_without_trusting_path(mocker, monkeypatch):
    """A bare "powershell" is not resolvable by CreateProcess off PATH."""
    from helpers import tokensave_daemon

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    mocker.patch("shutil.which", return_value=None)
    mocker.patch("os.path.isfile", return_value=True)

    assert tokensave_daemon._powershell_exe().lower().endswith(
        r"system32\windowspowershell\v1.0\powershell.exe")
