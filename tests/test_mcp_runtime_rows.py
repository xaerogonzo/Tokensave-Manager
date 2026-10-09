"""mcp_runtime_rows: what is running, with one honest status per source.

Both listers are injected, so nothing here depends on what happens to be
running on the machine. The property under test is the D1b one: a source that
could not be asked is reported as such, never as an empty list.
"""
from helpers.codegraph_daemon import ListingFailed
from helpers.mcp_runtime_rows import (
    STATUS_FAILED, STATUS_NOT_INSTALLED, STATUS_OK, collect,
)
from helpers.tokensave_daemon import (
    AMBIGUOUS, AUTHORITATIVE, HEURISTIC, UNATTRIBUTED,
    EnumerationFailed, TokensaveServer,
)


def _ts(pid, project, attribution=AUTHORITATIVE):
    return TokensaveServer(pid=pid, command_line="", started_at=1_700_000_000.0,
                           project=project, attribution=attribution,
                           version="7.14.0")


def _cg(pid, path):
    return {"pid": pid, "version": "1.5.0", "uptime": "5m 7s", "path": path}


def _collect(ts=(), cg=(), cg_exe="cg.exe", ts_error=None, cg_error=None):
    def list_ts(exe, projects, strict=False):
        assert strict, "the view must ask strictly, or a failure reads as none"
        if ts_error:
            raise ts_error
        return list(ts)

    def list_cg(exe, strict=False):
        assert strict
        if cg_error:
            raise cg_error
        return list(cg)

    return collect("ts.exe", cg_exe, [], list_ts=list_ts, list_cg=list_cg)


def _source(snap, name):
    return next(s for s in snap.sources if s.name == name)


def test_confirmed_servers_from_both_sources_become_rows():
    snap = _collect(ts=[_ts(10, "D:/a")], cg=[_cg(20, "D:/b")])
    assert [(r.server, r.pid, r.project) for r in snap.rows] == [
        ("tokensave", 10, "D:/a"), ("codegraph", 20, "D:/b")]
    assert all(r.attribution == "confirmed" and not r.is_guess
               for r in snap.rows)


def test_a_failed_tokensave_scan_is_not_an_empty_list():
    snap = _collect(cg=[_cg(20, "D:/b")],
                    ts_error=EnumerationFailed("powershell missing"))
    status = _source(snap, "tokensave")
    assert status.status == STATUS_FAILED
    assert "could not check" in snap.summary
    assert "tokensave: 0 running" not in snap.summary
    # The other source is unaffected and still reported.
    assert [r.server for r in snap.rows] == ["codegraph"]


def test_a_failed_codegraph_listing_is_not_an_empty_list():
    snap = _collect(ts=[_ts(10, "D:/a")], cg_error=ListingFailed("timed out"))
    assert _source(snap, "codegraph").status == STATUS_FAILED
    assert "codegraph: could not check (timed out)" in snap.summary


def test_no_servers_running_is_a_real_zero_not_a_failure():
    snap = _collect()
    assert _source(snap, "tokensave").status == STATUS_OK
    assert "tokensave: 0 running" in snap.summary
    assert snap.rows == ()


def test_codegraph_not_installed_is_said_not_zero():
    snap = _collect(ts=[_ts(10, "D:/a")], cg_exe="")
    assert _source(snap, "codegraph").status == STATUS_NOT_INSTALLED
    assert "codegraph: not installed" in snap.summary


def test_guesses_unknowns_and_ambiguous_are_never_shown_as_confirmed():
    snap = _collect(ts=[_ts(1, "D:/a", HEURISTIC),
                        _ts(2, None, UNATTRIBUTED),
                        _ts(3, None, AMBIGUOUS)])
    by_pid = {r.pid: r for r in snap.rows}
    assert by_pid[1].attribution == "guess" and by_pid[1].is_guess
    assert by_pid[2].attribution == "unknown project"
    assert by_pid[3].attribution == "ambiguous" and by_pid[3].is_guess
    assert all(r.attribution != "confirmed" for r in snap.rows)
    assert "3 not confirmed" in snap.summary


def test_summary_names_each_source_and_never_gives_a_combined_total():
    snap = _collect(ts=[_ts(10, "D:/a"), _ts(11, "D:/a")],
                    cg=[_cg(20, "D:/b")])
    assert snap.summary == "tokensave: 2 running  \u00b7  codegraph: 1 running"


def test_rows_group_by_project_then_server():
    snap = _collect(ts=[_ts(10, "D:/b"), _ts(11, "D:/a")],
                    cg=[_cg(20, "D:/a")])
    assert [(r.project, r.server) for r in snap.rows] == [
        ("D:/a", "codegraph"), ("D:/a", "tokensave"), ("D:/b", "tokensave")]


# -- the Tasks tab view switch -------------------------------------------------

def test_dropdown_switches_between_the_two_views(tk_root, mocker):
    from tkinter import ttk
    from types import SimpleNamespace
    from controllers.tasks_tab import TasksController, VIEW_MCP, VIEW_SESSIONS

    nb = ttk.Notebook(tk_root)
    cfg = SimpleNamespace(git_exe="git", search_roots=[], tokensave_exe="",
                          codegraph_exe="")
    ctrl = TasksController(nb, cfg, get_project_path=lambda: None,
                           get_known_paths=lambda: [])
    refresh = mocker.patch.object(ctrl, "_refresh")

    assert ctrl._sessions_view.winfo_manager() == "pack"
    assert ctrl._mcp_view.winfo_manager() == ""

    ctrl._view_var.set(VIEW_MCP)
    ctrl._on_view_changed()
    assert ctrl._sessions_view.winfo_manager() == ""
    assert ctrl._mcp_view.winfo_manager() == "pack"
    assert ctrl._manage_btn.winfo_manager() == "pack"

    ctrl._view_var.set(VIEW_SESSIONS)
    ctrl._on_view_changed()
    assert ctrl._sessions_view.winfo_manager() == "pack"
    assert ctrl._mcp_view.winfo_manager() == ""
    assert ctrl._manage_btn.winfo_manager() == ""
    assert refresh.call_count == 2
