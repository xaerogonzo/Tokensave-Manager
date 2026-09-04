"""tests/test_worktree_health.py — orphaned-worktree detection + repair.

Pure-function tests (no Tk). `.tokensave/` existence checks use real `tmp_path`
directories rather than mocked `os.path.isdir` — that's the one thing this
module has to get right for real, per the G-E discipline used elsewhere in
this suite (never patch `<module>.os.path.*` globally).

Patches use `mocker.patch.object(wh, "scan_worktrees", ...)` against a
directly-imported module reference, NOT the string form
`mocker.patch("helpers.worktree_health.scan_worktrees", ...)`. The string
form resolves the module via `sys.modules` at patch time — and
`tests/test_no_import_time_path_resolution.py` (the G-L pre-flight test)
pops and re-imports every `src/` module as part of its own check, which
replaces that sys.modules entry with a NEW module object. If G-L happens to
run first (it does — collected before this file), a string-path patch lands
on that new object while `find_orphaned_worktrees_for_project` (imported here
at THIS file's own collection time, before G-L ran) still calls through the
OLD object's globals — the patch silently never takes effect. `patch.object`
against a same-collection-time module reference shares that exact globals
dict and is immune regardless of what G-L does to sys.modules.
"""
from __future__ import annotations

from types import SimpleNamespace

import helpers.worktree_health as wh
from helpers.worktree_health import (
    find_orphaned_worktrees,
    find_orphaned_worktrees_for_project,
    repair_worktree_index,
)


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── find_orphaned_worktrees_for_project ───────────────────────────────────

def test_flags_worktree_missing_tokensave_dir(mocker, tmp_path):
    wt = tmp_path / "wt1"
    wt.mkdir()
    mocker.patch.object(
        wh, "scan_worktrees",
        return_value=[{"path": str(wt), "branch": "feature1", "head": "abc12345"}])
    orphans = find_orphaned_worktrees_for_project(str(tmp_path), "git")
    assert orphans == [
        {"worktree_path": str(wt), "branch": "feature1", "head": "abc12345"}]


def test_worktree_with_own_index_is_not_flagged(mocker, tmp_path):
    wt = tmp_path / "wt1"
    (wt / ".tokensave").mkdir(parents=True)
    mocker.patch.object(
        wh, "scan_worktrees",
        return_value=[{"path": str(wt), "branch": "feature1", "head": "abc12345"}])
    assert find_orphaned_worktrees_for_project(str(tmp_path), "git") == []


def test_no_worktrees_returns_empty(mocker, tmp_path):
    mocker.patch.object(wh, "scan_worktrees", return_value=[])
    assert find_orphaned_worktrees_for_project(str(tmp_path), "git") == []


def test_mixed_worktrees_only_flags_orphans(mocker, tmp_path):
    healthy = tmp_path / "healthy"
    (healthy / ".tokensave").mkdir(parents=True)
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    mocker.patch.object(
        wh, "scan_worktrees",
        return_value=[
            {"path": str(healthy), "branch": "b1", "head": "1111"},
            {"path": str(orphan), "branch": "b2", "head": "2222"},
        ])
    orphans = find_orphaned_worktrees_for_project(str(tmp_path), "git")
    assert len(orphans) == 1
    assert orphans[0]["worktree_path"] == str(orphan)


# ── find_orphaned_worktrees (app-wide) ────────────────────────────────────

def test_app_wide_skips_projects_without_git(mocker, tmp_path):
    scan = mocker.patch.object(wh, "scan_worktrees")
    projects = [{"path": str(tmp_path), "name": "p1", "has_git": False}]
    assert find_orphaned_worktrees(projects, "git") == []
    scan.assert_not_called()


def test_app_wide_tags_project_name_and_path(mocker, tmp_path):
    wt = tmp_path / "wt1"
    wt.mkdir()
    mocker.patch.object(
        wh, "scan_worktrees",
        return_value=[{"path": str(wt), "branch": "b1", "head": "1111"}])
    projects = [{"path": str(tmp_path), "name": "myproj", "has_git": True}]
    out = find_orphaned_worktrees(projects, "git")
    assert out == [{
        "worktree_path": str(wt), "branch": "b1", "head": "1111",
        "project_path": str(tmp_path), "project_name": "myproj",
    }]


def test_app_wide_aggregates_across_multiple_projects(mocker, tmp_path):
    p1 = tmp_path / "p1"; p1.mkdir()
    p2 = tmp_path / "p2"; p2.mkdir()
    wt1 = p1 / "wt"; wt1.mkdir()
    wt2 = p2 / "wt"; wt2.mkdir()

    def _scan(project_path, git_exe):
        if project_path == str(p1):
            return [{"path": str(wt1), "branch": "b1", "head": "1111"}]
        return [{"path": str(wt2), "branch": "b2", "head": "2222"}]

    mocker.patch.object(wh, "scan_worktrees", side_effect=_scan)
    projects = [
        {"path": str(p1), "name": "p1", "has_git": True},
        {"path": str(p2), "name": "p2", "has_git": True},
    ]
    out = find_orphaned_worktrees(projects, "git")
    assert len(out) == 2
    assert {o["project_name"] for o in out} == {"p1", "p2"}


# ── repair_worktree_index ──────────────────────────────────────────────────

def test_fresh_worktree_calls_init(mocker, tmp_path):
    wt = tmp_path / "wt1"; wt.mkdir()
    exe = tmp_path / "tokensave.exe"; exe.write_bytes(b"")
    run = mocker.patch("helpers.worktree_health.subprocess.run",
                       return_value=_proc(0))
    ok, action, detail = repair_worktree_index(str(exe), str(wt))
    assert ok is True
    assert action == "init"
    assert run.call_args.args[0] == [str(exe), "init", str(wt)]


def test_existing_index_calls_sync_force_never_init(mocker, tmp_path):
    """REGRESSION GUARD (edge case 1): tokensave init on an already-
    initialized project can no-op without that being obvious from the exit
    code alone. This function must decide the verb from directory existence
    BEFORE running anything — never call init a second time."""
    wt = tmp_path / "wt1"
    (wt / ".tokensave").mkdir(parents=True)
    exe = tmp_path / "tokensave.exe"; exe.write_bytes(b"")
    run = mocker.patch("helpers.worktree_health.subprocess.run",
                       return_value=_proc(0))
    ok, action, detail = repair_worktree_index(str(exe), str(wt))
    assert ok is True
    assert action == "sync --force"
    argv = run.call_args.args[0]
    assert argv == [str(exe), "sync", "--force", str(wt)]
    assert "init" not in argv


def test_run_twice_never_reports_false_repair(mocker, tmp_path):
    """The exact scenario from the bug report, exercised end-to-end against
    the module's own state transitions (no mocking of the directory check)."""
    wt = tmp_path / "wt1"; wt.mkdir()
    exe = tmp_path / "tokensave.exe"; exe.write_bytes(b"")
    run = mocker.patch("helpers.worktree_health.subprocess.run",
                       return_value=_proc(0))

    def _first_call_creates_index(*args, **kwargs):
        (wt / ".tokensave").mkdir(parents=True, exist_ok=True)
        return _proc(0)
    run.side_effect = _first_call_creates_index

    ok1, action1, _ = repair_worktree_index(str(exe), str(wt))
    assert ok1 is True and action1 == "init"

    ok2, action2, _ = repair_worktree_index(str(exe), str(wt))
    assert ok2 is True and action2 == "sync --force"


def test_subprocess_failure_reports_detail_not_exception(mocker, tmp_path):
    wt = tmp_path / "wt1"; wt.mkdir()
    exe = tmp_path / "tokensave.exe"; exe.write_bytes(b"")
    mocker.patch("helpers.worktree_health.subprocess.run",
                return_value=_proc(1, stderr="disk full"))
    ok, action, detail = repair_worktree_index(str(exe), str(wt))
    assert ok is False
    assert action == "init"
    assert "disk full" in detail


def test_missing_binary_no_subprocess(mocker, tmp_path):
    wt = tmp_path / "wt1"; wt.mkdir()
    run = mocker.patch("helpers.worktree_health.subprocess.run")
    ok, action, detail = repair_worktree_index("", str(wt))
    assert ok is False
    run.assert_not_called()


def test_timeout_reported_not_raised(mocker, tmp_path):
    import subprocess
    wt = tmp_path / "wt1"; wt.mkdir()
    exe = tmp_path / "tokensave.exe"; exe.write_bytes(b"")
    mocker.patch("helpers.worktree_health.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=300))
    ok, action, detail = repair_worktree_index(str(exe), str(wt))
    assert ok is False
    assert "timed out" in detail


# ── _tokensave_agent_wired (lives in helpers.mcp; covered here alongside
#    the other agent-state predicates this feature depends on) ────────────

def test_agent_wired_true_when_config_mentions_tokensave(mocker, tmp_path):
    # The owning module, not the facade -- see test_mcp_split.py.
    import helpers.mcp_agents as mcp
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {"tokensave": {"command": "ts.exe"}}}',
                   encoding="utf-8")
    mocker.patch.object(mcp, "_tokensave_agent_path_candidates",
                        return_value=[str(cfg)])
    assert mcp._tokensave_agent_wired("copilot") is True


def test_agent_wired_false_when_config_lacks_tokensave(mocker, tmp_path):
    # The owning module, not the facade -- see test_mcp_split.py.
    import helpers.mcp_agents as mcp
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {"other": {"command": "x"}}}',
                   encoding="utf-8")
    mocker.patch.object(mcp, "_tokensave_agent_path_candidates",
                        return_value=[str(cfg)])
    assert mcp._tokensave_agent_wired("copilot") is False


def test_agent_wired_false_when_no_config_exists(mocker, tmp_path):
    # The owning module, not the facade -- see test_mcp_split.py.
    import helpers.mcp_agents as mcp
    mocker.patch.object(mcp, "_tokensave_agent_path_candidates",
                        return_value=[str(tmp_path / "nope.json")])
    assert mcp._tokensave_agent_wired("copilot") is False


def test_agent_wired_handles_jsonc_comments(mocker, tmp_path):
    """These configs are JSON, JSONC and TOML — json.load would raise on
    comments, which is why the check is a raw substring match."""
    # The owning module, not the facade -- see test_mcp_split.py.
    import helpers.mcp_agents as mcp
    cfg = tmp_path / "kilo.jsonc"
    cfg.write_text('// a comment\n{"servers": {"tokensave": {}}}',
                   encoding="utf-8")
    mocker.patch.object(mcp, "_tokensave_agent_path_candidates",
                        return_value=[str(cfg)])
    assert mcp._tokensave_agent_wired("kilo") is True


def test_agent_wired_true_if_any_candidate_is_wired(mocker, tmp_path):
    """Copilot has multiple config surfaces; one wired is enough."""
    # The owning module, not the facade -- see test_mcp_split.py.
    import helpers.mcp_agents as mcp
    a = tmp_path / "a.json"; a.write_text("{}", encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text('{"tokensave": {}}', encoding="utf-8")
    mocker.patch.object(mcp, "_tokensave_agent_path_candidates",
                        return_value=[str(a), str(b)])
    assert mcp._tokensave_agent_wired("copilot") is True
