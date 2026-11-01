"""Delegated agent tasks (docs/ORCHESTRATION.md, stage A).

Every lifecycle path runs against a FAKE agent (tests/fixtures/fake_task_agent.py)
so nothing needs a model, a network or a token. What these pin down is what the
design says must never go quietly wrong: the person's checkout is never touched,
the diff comes from git and not from the agent, a task that did not finish
cleanly is never acceptable, a cancel leaves no process behind, and a task whose
process vanished is `lost`, not guessed at.
"""
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from helpers import agent_cli
from helpers import orchestration_limits as lim
from helpers import orchestration_run as orun
from helpers import orchestration_task as ot
from helpers import orchestration_worktree as wt

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_task_agent.py")
GIT = "git"


def _git(cwd, *args):
    return subprocess.run([GIT, "-C", cwd, "-c", "user.email=t@e.com", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    project = str(root)
    _git(project, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".tokensave/\n")
    (root / "a.txt").write_text("one\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "base")
    return project


@pytest.fixture
def spec():
    return dataclasses.replace(agent_cli.CLAUDE, id="fake", label="Fake agent",
                               task_print_args=(FAKE,), trust_args=(), write_args=())


def _index(_exe, path):
    os.makedirs(os.path.join(path, ".tokensave"), exist_ok=True)
    return True, ""


def _run(repo, spec, prompt, tmp_path, **kw):
    seen = []
    kw.setdefault("limits", lim.Limits(wall_clock_seconds=60))
    record = orun.run_task(
        repo, spec, sys.executable, prompt, cfg_raw={}, git_exe=GIT, tokensave_exe="ts",
        index_fn=kw.pop("index_fn", _index), poll_seconds=0.05,
        scratch_root=str(tmp_path / "scratch"), on_change=seen.append, **kw)
    return record, seen


# -- the happy path and what it measures ---------------------------------------------------

def test_a_finished_task_is_result_ready_with_a_diff_measured_by_git(repo, spec, tmp_path):
    record, seen = _run(repo, spec, "WRITE out.txt please", tmp_path)
    assert record["status"] == ot.ST_RESULT_READY and record["exit_code"] == 0
    assert record["diff"]["files"] == ["out.txt"] and record["diff"]["file_count"] == 1
    assert record["session_id"] == "fake-session-1"
    assert record["usage"] == {"source": "agent-reported", "figures": {
        "input_tokens": 11, "output_tokens": 7, "total_cost_usd": 0.0123}}
    assert record["branch"] == "tsm/" + record["task_id"]
    assert record["base_commit"] == _git(repo, "rev-parse", "HEAD").strip()
    assert [s["status"] for s in seen][-1] == ot.ST_RESULT_READY
    assert ot.ST_RUNNING in [s["status"] for s in seen]
    assert "WRITE out.txt" in record["prompt"]


def test_the_persons_own_checkout_is_never_touched(repo, spec, tmp_path):
    before = _git(repo, "status", "--porcelain")
    head = _git(repo, "rev-parse", "HEAD")
    record, _ = _run(repo, spec, "WRITE out.txt", tmp_path)
    assert os.path.isfile(os.path.join(record["worktree_root"], "out.txt"))
    assert not os.path.exists(os.path.join(repo, "out.txt"))
    assert _git(repo, "status", "--porcelain") == before
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "branch", "--show-current").strip() == "main"


def test_a_committed_change_is_counted_with_its_lines_and_commits(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "COMMIT made.txt", tmp_path)
    assert record["status"] == ot.ST_RESULT_READY
    assert record["diff"]["commits"] == 1 and "made.txt" in record["diff"]["files"]
    assert record["diff"]["insertions"] == 1


def test_what_the_agent_claims_is_not_what_changed(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "LIE one.txt", tmp_path)
    assert record["diff"]["file_count"] == 1                 # not 99


def test_a_task_that_changes_nothing_says_so(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "NOTHING", tmp_path)
    assert record["status"] == ot.ST_RESULT_READY and record["diff"]["file_count"] == 0


def test_the_prompt_stored_in_the_record_has_secrets_masked(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "NOTHING use key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", tmp_path)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in record["prompt"]


# -- tasks that did not finish cleanly are never acceptable --------------------------------------

def test_a_nonzero_exit_is_failed_with_its_diff_kept(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "EXIT3 partial.txt", tmp_path)
    assert record["status"] == ot.ST_FAILED and record["exit_code"] == 3
    assert "code 3" in record["error"] and record["diff"]["files"] == ["partial.txt"]
    assert os.path.isdir(record["worktree_root"])            # kept: it holds the evidence


def test_an_agent_that_reports_an_error_with_exit_zero_is_still_failed(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "ERROR", tmp_path)
    assert record["status"] == ot.ST_FAILED and "reported an error" in record["error"]


def test_a_failed_task_cannot_be_accepted(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "EXIT3 x.txt", tmp_path)
    got, why = ot.transition(repo, record["task_id"], ot.ST_ACCEPTED, GIT)
    assert got is None and "cannot become accepted" in why


# -- refusals happen before anything exists ---------------------------------------------------------

def test_uncommitted_tracked_changes_refuse_and_name_the_file(repo, spec, tmp_path):
    with open(os.path.join(repo, "a.txt"), "a") as fh:
        fh.write("local edit\n")
    ready = orun.check_launch(repo, spec, sys.executable, git_exe=GIT,
                              scratch_root=str(tmp_path / "scratch"))
    assert not ready.ok and "a.txt" in " ".join(ready.refusals)
    record, _ = _run(repo, spec, "WRITE out.txt", tmp_path)
    assert record is None
    assert not os.path.exists(tmp_path / "scratch")           # no worktree was made
    assert ot.list_tasks(repo, GIT).tasks == ()               # and no record


def test_untracked_files_are_reported_not_refused(repo, spec, tmp_path):
    (tmp_path / "proj" / "scratch.txt").write_text("x")
    ready = orun.check_launch(repo, spec, sys.executable, git_exe=GIT,
                              scratch_root=str(tmp_path / "scratch"))
    assert ready.ok and ready.untracked == 1


def test_an_agent_with_no_measured_write_mode_is_refused_by_name(repo, spec, tmp_path):
    spec = dataclasses.replace(spec, write=agent_cli.WRITE_UNSUPPORTED)
    ready = orun.check_launch(repo, spec, sys.executable, git_exe=GIT,
                              scratch_root=str(tmp_path / "scratch"))
    assert not ready.ok and "Fake agent" in " ".join(ready.refusals)
    assert agent_cli.build_task_argv(spec, "x", "p", mcp_config="c")[2]


def test_not_a_repository_and_no_commit_are_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not wt.preflight(str(plain), GIT).ok
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(str(empty), "init", "-q", "-b", "main")
    got = wt.preflight(str(empty), GIT)
    assert not got.ok and "no commit" in got.error


def test_a_failed_index_stops_the_task_before_the_agent_starts(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "WRITE out.txt", tmp_path,
                     index_fn=lambda exe, path: (False, "tokensave init failed: boom"))
    assert record["status"] == ot.ST_FAILED and "boom" in record["error"]
    assert record["pid"] == 0 and record["started_at"] == ""


# -- limits ---------------------------------------------------------------------------------------------

def test_limits_refuse_with_the_reason_and_unknown_disk_is_not_enough_room():
    L = lim.Limits()
    ok = lim.check_launch(L, running_here=0, running_total=0, open_worktrees=0,
                          free_bytes=5 * lim.GIB)
    assert ok == []
    why = lim.check_launch(L, running_here=1, running_total=2, open_worktrees=6,
                           free_bytes=lim.GIB // 2)
    assert len(why) == 4 and "limit 1" in why[0]
    assert "could not measure" in lim.check_launch(
        L, running_here=0, running_total=0, open_worktrees=0, free_bytes=None)[0]


def test_a_bad_limit_is_reported_and_its_default_kept_never_unlimited():
    got, problems = lim.from_config({"max_running_total": 0, "typo_key": 3,
                                     "max_open_worktrees": "many", "min_free_bytes": 5})
    assert got.max_running_total == 2 and got.max_open_worktrees == 6
    assert got.min_free_bytes == 5
    assert len(problems) == 3
    assert lim.from_config("nope")[1]


def test_a_running_task_in_this_project_blocks_a_second_one(repo, spec, tmp_path):
    rec = ot.new_task(project=repo, agent="fake", prompt="x")
    rec["status"] = ot.ST_RUNNING
    ot.save(repo, rec, GIT)
    ready = orun.check_launch(repo, spec, sys.executable, git_exe=GIT,
                              scratch_root=str(tmp_path / "scratch"))
    assert not ready.ok and "already has 1 running" in " ".join(ready.refusals)


def test_open_worktrees_count_until_discarded(repo, spec, tmp_path):
    for _ in range(2):
        rec = ot.new_task(project=repo, agent="fake", prompt="x")
        rec.update(status=ot.ST_RESULT_READY, worktree_root="/some/where")
        ot.save(repo, rec, GIT)
    small = lim.Limits(max_open_worktrees=2)
    ready = orun.check_launch(repo, spec, sys.executable, git_exe=GIT, limits=small,
                              scratch_root=str(tmp_path / "scratch"))
    assert "2 task worktrees are still open" in " ".join(ready.refusals)


# -- cancelling and timing out leave nothing running ----------------------------------------------------

def _alive(pid):
    return orun.process_start_time(pid) != ""


def _start_hanging(repo, spec, tmp_path, limits=None):
    result = {}
    kw = {"limits": limits} if limits else {}
    thread = threading.Thread(
        target=lambda: result.update(rec=_run(repo, spec, "HANG", tmp_path, **kw)[0]))
    thread.start()
    return thread, result


def _wait_until(pred, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_cancel_kills_the_root_and_its_children_and_keeps_the_worktree(repo, spec, tmp_path):
    thread, result = _start_hanging(repo, spec, tmp_path)
    listed = lambda: ot.list_tasks(repo, GIT).tasks                      # noqa: E731
    assert _wait_until(lambda: listed() and listed()[0]["status"] == ot.ST_RUNNING
                       and listed()[0]["pid"])
    task = listed()[0]
    pidfile = os.path.join(os.path.dirname(task["worktree_root"]), "hang-child.pid")
    assert _wait_until(lambda: os.path.exists(pidfile) and open(pidfile).read().strip())
    child = int(open(pidfile).read())
    assert _alive(task["pid"]) and _alive(child)
    ok, _ = orun.request_cancel(repo, task["task_id"], GIT)
    thread.join(60)
    assert ok and not thread.is_alive()
    final = result["rec"]
    assert final["status"] == ot.ST_CANCELLED and final["cancel_state"] == ot.CANCEL_KILLED
    assert not _alive(task["pid"]) and not _alive(child)      # the tree, not just the root
    assert os.path.isdir(final["worktree_root"])


def test_a_task_over_its_wall_clock_is_killed_and_failed(repo, spec, tmp_path):
    thread, result = _start_hanging(repo, spec, tmp_path, lim.Limits(wall_clock_seconds=1))
    thread.join(60)
    final = result["rec"]
    assert final["status"] == ot.ST_FAILED and "limit" in final["error"]
    assert not _alive(final["pid"])


def test_cancelling_a_task_that_is_not_running_says_so(repo):
    ok, why = orun.request_cancel(repo, "tdoesnotexist", GIT)
    assert not ok and "not running" in why


# -- records, transitions and being lost -------------------------------------------------------------------

def test_illegal_transitions_are_refused_and_terminal_states_stay_put(repo):
    rec = ot.new_task(project=repo, agent="fake", prompt="x")
    ot.save(repo, rec, GIT)
    tid = rec["task_id"]
    assert ot.transition(repo, tid, ot.ST_RESULT_READY, GIT)[0] is None      # skipped steps
    assert ot.transition(repo, tid, ot.ST_FAILED, GIT)[0]["ended_at"]
    assert ot.transition(repo, tid, ot.ST_RUNNING, GIT)[0] is None
    assert ot.update(repo, tid, GIT, status="running")[0] is None            # no side door


def test_a_damaged_record_is_counted_not_hidden(repo):
    rec = ot.new_task(project=repo, agent="fake", prompt="x")
    ot.save(repo, rec, GIT)
    with open(os.path.join(ot.tasks_dir(repo, GIT), "tbroken.json"), "w") as fh:
        fh.write("{nope")
    seen = ot.list_tasks(repo, GIT)
    assert len(seen.tasks) == 1 and seen.bad_files == 1


def _running(repo, pid, started):
    rec = ot.new_task(project=repo, agent="fake", prompt="x")
    rec.update(status=ot.ST_RUNNING, pid=pid, pid_started_at=started)
    ot.save(repo, rec, GIT)
    return rec["task_id"]


def test_reconcile_loses_only_what_it_can_prove_is_gone(repo):
    gone = _running(repo, 99999999, "123")
    unknown = _running(repo, 4242, "")
    mine = _running(repo, os.getpid(), orun.process_start_time(os.getpid()))
    recycled = _running(repo, os.getpid(), "1")                  # same pid, other process
    lost = ot.reconcile(repo, orun.is_alive, GIT)
    assert sorted(lost) == sorted([gone, recycled])
    status = {t["task_id"]: t["status"] for t in ot.list_tasks(repo, GIT).tasks}
    assert status[unknown] == ot.ST_RUNNING and status[mine] == ot.ST_RUNNING
    assert status[gone] == ot.ST_LOST and "never recorded" in ot.load(repo, gone, GIT)[0]["error"]


def test_is_alive_is_three_valued():
    assert orun.is_alive(os.getpid(), orun.process_start_time(os.getpid())) is True
    assert orun.is_alive(os.getpid(), "1") is False
    assert orun.is_alive(0, "1") is None and orun.is_alive(os.getpid(), "") is None


# -- worktrees ----------------------------------------------------------------------------------------------------

def test_discard_removes_the_worktree_and_keeps_the_branch(repo, spec, tmp_path):
    record, _ = _run(repo, spec, "COMMIT made.txt", tmp_path)
    ok, why = wt.discard(repo, record["worktree_root"], GIT)
    assert ok, why
    assert not os.path.exists(record["worktree_root"])
    assert record["branch"] in _git(repo, "branch", "--list", record["branch"])


def test_the_worktree_lives_in_the_scratch_convention():
    got = wt.worktree_path(r"D:\Random Projects\Fortuna Lab", "tabc123")
    assert got == os.path.join("D:" + os.sep, "_scratch", "Fortuna Lab-tabc123")
    assert wt.worktree_path("D:/p/x", "t1", "E:/s") == os.path.join("E:/s", "x-t1")


def test_files_the_manager_wrote_into_the_worktree_are_not_the_agents_changes(repo):
    os.makedirs(os.path.join(repo, ".cursor"))
    open(os.path.join(repo, ".cursor", "mcp.json"), "w").write("{}")
    open(os.path.join(repo, "real.txt"), "w").write("x")
    base = _git(repo, "rev-parse", "HEAD").strip()
    got = wt.measure(repo, base, GIT, ignore=(".cursor/mcp.json",))
    assert got["files"] == ["real.txt"]


def test_measure_reports_its_own_failure_instead_of_an_empty_diff(repo):
    got = wt.measure(repo, "0" * 40, GIT)
    assert got["error"] and got["file_count"] == 0


def test_a_cursor_style_agent_gets_its_mcp_config_inside_the_worktree(repo, tmp_path):
    cursor = dataclasses.replace(agent_cli.CURSOR)
    config, written, err = wt.prepare_mcp(cursor, repo, "t1", repo, {}, GIT)
    assert err == "" and config == "" and written == [".cursor/mcp.json"]
    data = json.load(open(os.path.join(repo, ".cursor", "mcp.json")))
    assert data["mcpServers"]["tokensave"]["args"][-1] == "."          # follows the cwd


def test_a_claude_style_agent_gets_its_config_outside_the_worktree(repo):
    config, written, err = wt.prepare_mcp(agent_cli.CLAUDE, repo, "t1", repo, {}, GIT)
    assert err == "" and written == [] and os.path.isfile(config)
    assert not config.startswith(repo + os.sep) or ".git" in config      # not in the tree


# -- the argv each row builds ---------------------------------------------------------------------------------

def test_claude_task_argv_allows_edits_and_pins_one_mcp_config():
    argv, stdin, err = agent_cli.build_task_argv(
        agent_cli.CLAUDE, "claude.cmd", "do it", model="m", mcp_config="C:/c.json")
    assert err == "" and stdin == "do it"
    assert argv[:3] == ["claude.cmd", "--print", "--output-format"]
    assert ["--permission-mode", "acceptEdits"] == argv[argv.index("--permission-mode"):][:2]
    i = argv.index("--strict-mcp-config")
    assert argv[i + 1:i + 3] == ["--mcp-config", "C:/c.json"]
    assert "--mode" not in argv and "--disallowedTools" not in argv


def test_cursor_task_argv_writes_without_the_read_only_flag_and_approves_mcp():
    argv, stdin, err = agent_cli.build_task_argv(agent_cli.CURSOR, "agent.cmd", "do it")
    assert err == "" and "--trust" in argv and "stream-json" in argv
    assert "--mode" not in argv and "ask" not in argv            # the refusal flag is left off
    assert "--approve-mcps" in argv and "--force" in argv


def test_a_row_that_names_a_config_by_flag_refuses_without_one():
    argv, _stdin, err = agent_cli.build_task_argv(agent_cli.CLAUDE, "c", "p")
    assert argv == [] and "MCP config" in err


# -- reading what an agent returned ----------------------------------------------------------------------------------

def test_claude_json_and_cursor_stream_json_both_parse():
    claude = json.dumps({"session_id": "s", "result": "ok", "is_error": False,
                         "total_cost_usd": 0.5, "usage": {"input_tokens": 3, "note": "x"}})
    got = orun.parse_output(agent_cli.OUTPUT_JSON, "banner\n" + claude)
    assert got["session_id"] == "s" and got["usage"]["figures"] == {
        "input_tokens": 3, "total_cost_usd": 0.5}
    stream = "\n".join(json.dumps(o) for o in (
        {"type": "system", "session_id": "c1"},
        {"type": "assistant", "text": "hi"},
        {"type": "result", "session_id": "c1", "result": "done", "is_error": False,
         "usage": {"inputTokens": 9, "outputTokens": 2}}))
    got = orun.parse_output(agent_cli.OUTPUT_STREAM_JSON, stream)
    assert got["session_id"] == "c1" and got["usage"]["figures"]["inputTokens"] == 9


def test_output_that_does_not_parse_is_unknown_never_invented():
    for kind in (agent_cli.OUTPUT_JSON, agent_cli.OUTPUT_STREAM_JSON):
        got = orun.parse_output(kind, "Traceback: nothing here")
        assert got["usage"] == "unknown" and got["session_id"] == "" and got["is_error"] is None


# -- context from other agents ---------------------------------------------------------------------------------------------

def test_earlier_handoffs_reach_the_task_labelled_as_claims(repo):
    from helpers import handoff_ledger as hl
    hl.append(repo, hl.build_entry(agent="cursor", goal="wire the ledger",
                                   decisions=["keep it local"], next_steps=["add tests"]))
    brief = orun.context_brief(repo, GIT)
    assert "claims, not verified facts" in brief and "keep it local" in brief
    assert orun.context_brief(repo + "-none", GIT) == ""


# -- the Tasks-tab panel (real Tk, fake agent) -------------------------------------------------

@pytest.fixture
def panel(tk_root, repo, spec, tmp_path, monkeypatch):
    import types
    from controllers.orchestration_ctrl import AgentTasksPanel
    from tkinter import ttk
    monkeypatch.setitem(agent_cli.AGENT_CLIS, "fake", spec)
    cfg = types.SimpleNamespace(
        raw={"orchestration_limits": {"wall_clock_seconds": 60}}, git_exe=GIT,
        tokensave_exe="ts", enable_agent_orchestration=True,
        claude_cli_exe=sys.executable)
    scratch = str(tmp_path / "scratch")
    monkeypatch.setattr(wt, "index", _index)
    real_path = wt.worktree_path
    monkeypatch.setattr(wt, "worktree_path",
                        lambda project, tid, root="": real_path(project, tid, scratch))
    p = AgentTasksPanel(ttk.LabelFrame(tk_root), cfg, get_project_path=lambda: repo)
    p.cfg = cfg
    return p


@pytest.mark.tk
def test_the_panel_says_off_and_disables_new_when_the_switch_is_off(panel, wait_for):
    panel.cfg.enable_agent_orchestration = False
    panel.refresh()
    wait_for(lambda: "OFF" in panel._note.cget("text"))
    assert "disabled" in panel._buttons["new"].state()


@pytest.mark.tk
def test_starting_from_the_panel_with_the_switch_off_starts_nothing(panel, repo):
    panel.cfg.enable_agent_orchestration = False
    panel._start("fake", "WRITE out.txt", "")
    time.sleep(0.3)
    assert ot.list_tasks(repo, GIT).tasks == ()


@pytest.mark.tk
def test_a_task_started_from_the_panel_runs_and_can_be_accepted_without_merging(
        panel, repo, wait_for, mocker):
    mocker.patch("controllers.orchestration_ctrl.messagebox.showinfo")
    head = _git(repo, "rev-parse", "HEAD")
    panel._start("fake", "WRITE out.txt", "")
    wait_for(lambda: any(r["status"] == ot.ST_RESULT_READY for r in panel._rows.values()),
             timeout_s=40)
    tid = next(iter(panel._rows))
    panel._tree.selection_set(tid)
    panel._show_detail()
    assert "disabled" not in panel._buttons["accept"].state()
    assert "disabled" in panel._buttons["cancel"].state()
    panel.accept()
    wait_for(lambda: panel._rows.get(tid, {}).get("status") == ot.ST_ACCEPTED, timeout_s=10)
    assert _git(repo, "rev-parse", "HEAD") == head            # nothing merged
    assert not os.path.exists(os.path.join(repo, "out.txt"))


@pytest.mark.tk
def test_buttons_follow_the_selected_tasks_status(panel, repo, wait_for):
    rec = ot.new_task(project=repo, agent="fake", prompt="x")
    rec.update(status=ot.ST_FAILED, worktree_root="/w", branch="tsm/x")
    ot.save(repo, rec, GIT)
    panel.refresh()
    wait_for(lambda: rec["task_id"] in panel._rows)
    panel._tree.selection_set(rec["task_id"])
    panel._show_detail()
    state = {k: "disabled" in b.state() for k, b in panel._buttons.items()}
    assert state["accept"] and state["reject"] and state["cancel"]      # a failed task is not acceptable
    assert not state["discard"] and not state["diff"]


# -- the Settings switch ---------------------------------------------------------------------------------

@pytest.mark.tk
def test_an_install_that_never_had_the_switch_does_not_gain_the_key_on_save(
        tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    assert "enable_agent_orchestration" not in mock_config.raw


@pytest.mark.tk
def test_turning_it_on_saves_a_literal_true_and_turning_it_off_a_literal_false(
        tk_root, mock_config, mocker):
    from tests.test_settings_tab import _build
    ctl = _build(tk_root, mock_config, mocker)
    ctl._ai._var_enable_agent_orchestration.set(True)
    ctl._save()
    assert mock_config.raw["enable_agent_orchestration"] is True
    ctl._ai._var_enable_agent_orchestration.set(False)
    ctl._save()
    assert mock_config.raw["enable_agent_orchestration"] is False


@pytest.mark.tk
@pytest.mark.parametrize("stored", ["true", 1, "yes"])
def test_a_truthy_non_boolean_is_not_consent_and_shows_as_off(
        tk_root, mock_config, mocker, stored):
    from tests.test_settings_tab import _build
    mock_config.raw["enable_agent_orchestration"] = stored
    ctl = _build(tk_root, mock_config, mocker)
    assert ctl._ai._var_enable_agent_orchestration.get() is False
    ctl._save()
    assert mock_config.raw["enable_agent_orchestration"] is False


def test_the_config_property_is_strictly_true():
    from state import ManagerConfig
    for value, want in ((True, True), (False, False), ("true", False), (1, False), (None, False)):
        cfg = ManagerConfig.__new__(ManagerConfig)
        cfg.raw = {"enable_agent_orchestration": value}
        assert cfg.enable_agent_orchestration is want
    cfg = ManagerConfig.__new__(ManagerConfig)
    cfg.raw = {}
    assert cfg.enable_agent_orchestration is False


@pytest.mark.tk
def test_new_task_with_no_project_selected_says_so_instead_of_doing_nothing(
        tk_root, mocker):
    import types
    from controllers.orchestration_ctrl import AgentTasksPanel
    from tkinter import ttk
    cfg = types.SimpleNamespace(raw={}, git_exe=GIT, tokensave_exe="ts",
                                enable_agent_orchestration=True)
    panel = AgentTasksPanel(ttk.LabelFrame(tk_root), cfg, get_project_path=lambda: None)
    info = mocker.patch("controllers.orchestration_ctrl.messagebox.showinfo")
    panel.new_task()
    assert info.call_count == 1 and "Select a project" in info.call_args[0][1]


@pytest.mark.tk
def test_new_task_with_the_switch_off_says_so_instead_of_doing_nothing(tk_root, mocker):
    import types
    from controllers.orchestration_ctrl import AgentTasksPanel
    from tkinter import ttk
    cfg = types.SimpleNamespace(raw={}, git_exe=GIT, tokensave_exe="ts",
                                enable_agent_orchestration=False)
    panel = AgentTasksPanel(ttk.LabelFrame(tk_root), cfg, get_project_path=lambda: "x")
    info = mocker.patch("controllers.orchestration_ctrl.messagebox.showinfo")
    panel.new_task()
    assert info.call_count == 1 and "off" in info.call_args[0][1]
