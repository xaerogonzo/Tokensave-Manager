"""helpers/frozen_smoke.py: the judges fail closed, and the launchers can be fooled by nothing.

The judges are pure and are tested with hand-made reports, one reason per case. The
launchers are tested with stand-in programs that speak the same protocol (the drive report,
the CLI envelope), including the ones that never answer. One test runs the REAL Manager from
source through the same judge; it opens a window, so it is opt-in (`TSM_LIVE=1`).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from helpers import drive_ledger
from helpers import frozen_smoke as fs

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_BODY = '[{"do": "quit"}]'


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "drive.json"
    path.write_text(SCRIPT_BODY, encoding="utf-8")
    return path


def _report(script: Path, launched: float, **over) -> dict:
    report = {
        "report_schema": drive_ledger.REPORT_SCHEMA, "run_id": "r", "pid": 1, "script": str(script),
        "script_hash": drive_ledger.script_hash(str(script)), "commit_sha": None,
        "commit_sha_state": "UNKNOWN", "python": "3", "started_at": launched + 1.0,
        "finished_at": launched + 5.0, "finished_because": "quit", "passed": True,
        "expectations": [
            {"id": "on_projects", "passed": True, "elapsed_ms": 5, "expected": "x", "actual": "x", "failure_reason": ""},
            {"id": "expect_clean", "passed": True, "elapsed_ms": 5, "expected": "x", "actual": "x", "failure_reason": ""},
        ],
        "steps_failed": [], "startup_diagnostics": [], "drive_diagnostics": [], "shots": [],
    }
    report.update(over)
    return report


def _judge(tmp_path, report, *, exit_code=0, timed_out=False, launched=None, **kw):
    script = _script(tmp_path)
    launched = time.time() - 10 if launched is None else launched
    path = tmp_path / "drive.report.json"
    if report is not None:
        data = report(script, launched) if callable(report) else report
        path.write_text(json.dumps(data), encoding="utf-8")
    return fs.judge_drive_report(str(path), exit_code=exit_code, timed_out=timed_out,
                                 launched_at=launched, script=str(script), **kw)


def test_a_complete_fresh_report_for_this_script_passes(tmp_path):
    assert _judge(tmp_path, _report).ok


@pytest.mark.parametrize("label, report, kwargs, expect", [
    ("no report", None, {}, "no report"),
    ("exit status", _report, {"exit_code": 1}, "exit status"),
    ("exit unknown", _report, {"exit_code": None}, "exit status"),
    ("timeout", _report, {"timed_out": True}, "in time"),
    ("schema", lambda s, t: _report(s, t, report_schema=999), {}, "schema"),
    ("did not quit", lambda s, t: _report(s, t, finished_because="exit"), {}, "not by reaching quit"),
    ("passed false", lambda s, t: _report(s, t, passed=False), {}, "passed=False"),
    ("stale", lambda s, t: _report(s, t, started_at=t - 600, finished_at=t - 590), {}, "stale"),
    ("no times", lambda s, t: _report(s, t, started_at=None), {}, "started and finished"),
    ("finished before started", lambda s, t: _report(s, t, started_at=t + 9, finished_at=t + 1), {}, "before it started"),
    ("other script", lambda s, t: _report(s, t, script_hash="0" * 16), {}, "different version of the script"),
    ("no expectations", lambda s, t: _report(s, t, expectations=[]), {}, "no expectations"),
    ("required id gone", lambda s, t: _report(s, t, expectations=[{"id": "expect_clean", "passed": True}]), {}, "'on_projects' never ran"),
    ("expectation failed", lambda s, t: _report(s, t, expectations=[
        {"id": "on_projects", "passed": False, "failure_reason": "boom"}, {"id": "expect_clean", "passed": True}]), {}, "boom"),
    ("step failed", lambda s, t: _report(s, t, steps_failed=[{"kind": "STEP_FAILURE"}]), {}, "failed steps"),
    ("diagnostics", lambda s, t: _report(s, t, drive_diagnostics=[{"kind": "LOG"}]), {}, "diagnostics during the run"),
    ("startup diagnostics", lambda s, t: _report(s, t, startup_diagnostics=[{"kind": "WARNING"}]), {}, "startup diagnostics"),
    ("steps_failed missing", lambda s, t: {k: v for k, v in _report(s, t).items() if k != "steps_failed"}, {}, "failed steps"),
    ("not an object", lambda s, t: ["x"], {}, "not an object"),
])
def test_every_way_a_driven_run_can_be_wrong_is_its_own_failure(tmp_path, label, report, kwargs, expect):
    judgement = _judge(tmp_path, report, **kwargs)
    assert not judgement.ok and any(expect in f for f in judgement.failures), (label, judgement.failures)


def test_unreadable_json_is_a_failure_not_an_exception(tmp_path):
    script = _script(tmp_path)
    (tmp_path / "drive.report.json").write_text("{not json", encoding="utf-8")
    j = fs.judge_drive_report(str(tmp_path / "drive.report.json"), exit_code=0, timed_out=False,
                              launched_at=time.time(), script=str(script))
    assert not j.ok and "could not be read" in j.failures[0]


def test_startup_diagnostics_can_be_allowed_but_nothing_else_is_relaxed(tmp_path):
    ok = _judge(tmp_path, lambda s, t: _report(s, t, startup_diagnostics=[{"kind": "W"}]), allow_startup_diagnostics=True)
    assert ok.ok
    bad = _judge(tmp_path, lambda s, t: _report(s, t, startup_diagnostics=[{"kind": "W"}], passed=False),
                 allow_startup_diagnostics=True)
    assert not bad.ok


# ── the CLI envelope ────────────────────────────────────────────────────

def _envelope(**over):
    base = {"schema_version": 1, "cli_version": "2.6.0", "command": "commands", "ok": True, "data": {},
            "findings": [], "warnings": [], "error": None}
    base.update(over)
    return json.dumps(base)


def test_a_well_formed_envelope_of_the_expected_version_passes():
    assert fs.judge_cli_envelope(_envelope(), 0, command="commands", expected_version="2.6.0").ok


@pytest.mark.parametrize("stdout, code, expect", [
    ("", 0, "not JSON"),
    ("[]", 0, "not an object"),
    (_envelope(cli_version="2.5.0"), 0, "cli_version"),
    (_envelope(command="status"), 0, "command is"),
    (_envelope(ok=False, error="x"), 1, "ok is"),
    (_envelope(), 1, "exit status"),
    (_envelope(), None, "did not finish"),
])
def test_each_cli_failure_is_named(stdout, code, expect):
    j = fs.judge_cli_envelope(stdout, code, command="commands", expected_version="2.6.0")
    assert not j.ok and any(expect in f for f in j.failures), j.failures


def test_status_may_be_not_ok_but_must_still_be_a_correct_envelope():
    j = fs.judge_cli_envelope(_envelope(command="status", ok=False, error="no tokensave"), 1,
                              command="status", expected_version="2.6.0", require_ok=False)
    assert j.ok
    wrong = fs.judge_cli_envelope(_envelope(command="status", ok=False, cli_version="1.0.0"), 1,
                                  command="status", expected_version="2.6.0", require_ok=False)
    assert not wrong.ok


# ── the version resource ────────────────────────────────────────────────

def test_the_version_resource_must_match_in_both_fields_and_the_fourth_part_is_zero():
    good = fs.VersionInfo((2, 6, 0, 0), (2, 6, 0, 0), "TokenSave Manager")
    assert fs.judge_version_info(good, "2.6.0", expected_product="TokenSave Manager").ok
    assert not fs.judge_version_info(fs.VersionInfo((2, 5, 0, 0), (2, 6, 0, 0)), "2.6.0").ok
    assert not fs.judge_version_info(fs.VersionInfo((2, 6, 0, 0), (2, 6, 0, 1)), "2.6.0").ok
    assert not fs.judge_version_info(good, "2.6.0", expected_product="Other").ok


def test_an_unreadable_version_resource_is_never_a_match():
    assert not fs.judge_version_info(None, "2.6.0").ok


def test_the_fixed_file_info_maps_file_and_product_version_to_their_own_fields():
    """File and product version are different DWORDs, and they differ in real executables."""
    dwords = [0xFEEF04BD, 0x10000, (2 << 16) | 6, (0 << 16) | 0, (3 << 16) | 1, (4 << 16) | 5, 0x3F, 0x4, 0, 0, 0, 0, 0]
    assert fs.parse_fixed_info(dwords) == ((2, 6, 0, 0), (3, 1, 4, 5))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version resources")
def test_the_reader_reads_a_real_executable_and_says_none_for_nothing():
    info = fs.read_version_info(sys.executable)
    assert info is not None and info.file_version[:2] == sys.version_info[:2]
    assert fs.read_version_info(str(Path(sys.executable).with_name("no-such-file.exe"))) is None


# ── the launchers, with stand-in programs ───────────────────────────────

STAND_IN_GUI = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["TSM_SRC"])
from helpers import drive_ledger
mode = os.environ.get("STAND_IN_MODE", "pass")
script = os.environ["TOKENSAVE_MANAGER_DRIVE"]
config = os.environ["TOKENSAVE_MANAGER_CONFIG"]
# isolation: the config is a scratch copy beside the script, never an install's own file
assert os.path.dirname(config) == os.path.dirname(script), (config, script)
assert json.load(open(config))["search_roots"] == []
if mode == "hang":
    time.sleep(60)
if mode == "silent":
    sys.exit(0)
now = time.time()
report = {"report_schema": drive_ledger.REPORT_SCHEMA, "script_hash": drive_ledger.script_hash(script),
          "started_at": now, "finished_at": now + 0.1, "finished_because": "quit",
          "passed": mode != "fail",
          "expectations": [{"id": "on_projects", "passed": True}, {"id": "expect_clean", "passed": True}],
          "steps_failed": [], "startup_diagnostics": [], "drive_diagnostics": []}
open(os.path.splitext(script)[0] + ".report.json", "w").write(json.dumps(report))
sys.exit(0 if mode != "fail" else 1)
'''

STAND_IN_CLI = r'''
import json, os, sys
mode = os.environ.get("STAND_IN_MODE", "pass")
command = sys.argv[1]
version = "9.9.9" if mode == "wrong_version" else "2.6.0"
print(json.dumps({"cli_version": version, "command": command, "ok": command == "commands", "error": None}))
sys.exit(0 if command == "commands" else 1)
'''


def _app(tmp_path: Path, body: str) -> list:
    path = tmp_path / "stand_in.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


@pytest.mark.parametrize("mode, ok", [("pass", True), ("fail", False), ("silent", False), ("hang", False)])
def test_the_gui_launcher_judges_a_stand_in_through_the_isolation_contract(tmp_path, monkeypatch, mode, ok):
    monkeypatch.setenv("STAND_IN_MODE", mode)
    monkeypatch.setenv("TSM_SRC", str(ROOT / "src"))
    result = fs.run_gui(_app(tmp_path, STAND_IN_GUI), str(_script(tmp_path)),
                        timeout=4 if mode == "hang" else 60, workdir=str(tmp_path))
    assert result.judgement.ok is ok, result.judgement.failures
    assert result.layer == fs.LAYER_GUI


def test_the_launcher_does_not_write_into_the_folder_the_script_came_from(tmp_path, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "pass")
    monkeypatch.setenv("TSM_SRC", str(ROOT / "src"))
    script = _script(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    fs.run_gui(_app(tmp_path, STAND_IN_GUI), str(script), workdir=str(tmp_path))
    created = set(p.name for p in tmp_path.iterdir()) - set(before)
    assert not any(n.endswith(".report.json") for n in created)  # the report went to the scratch copy


def test_the_cli_launcher_runs_both_probes_from_a_foreign_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "pass")
    results = fs.run_cli(_app(tmp_path, STAND_IN_CLI), "2.6.0", workdir=str(tmp_path))
    assert [r.target.split()[-1] for r in results] == ["commands", "status"]
    assert all(r.judgement.ok for r in results), [r.judgement.failures for r in results]


def test_a_cli_that_reports_another_version_fails_both_probes(tmp_path, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "wrong_version")
    results = fs.run_cli(_app(tmp_path, STAND_IN_CLI), "2.6.0", workdir=str(tmp_path))
    assert not any(r.judgement.ok for r in results)


def test_a_dist_with_missing_executables_fails_every_layer_instead_of_running_fewer(tmp_path):
    results = fs.smoke_dist(str(tmp_path), "2.6.0", str(_script(tmp_path)), workdir=str(tmp_path))
    assert {r.layer for r in results} == {fs.LAYER_VERSION, fs.LAYER_CLI, fs.LAYER_GUI}
    assert not any(r.judgement.ok for r in results)
    assert len([r for r in results if r.layer == fs.LAYER_VERSION]) == len(fs.ALL_EXES)


def test_the_shipped_drive_script_carries_every_required_expectation_id():
    steps = json.loads((ROOT / "scripts" / "drive" / "frozen-smoke.json").read_text(encoding="utf-8"))
    ids = {s.get("id") for s in steps if s.get("do") == "expect"} | ({"expect_clean"} if any(s.get("do") == "expect_clean" for s in steps) else set())
    assert set(fs.GUI_REQUIRED_IDS) <= ids
    assert steps[-1] == {"do": "quit"}


@pytest.mark.skipif(os.environ.get("TSM_LIVE") != "1", reason="opens the real window; opt in with TSM_LIVE=1")
def test_the_real_manager_from_source_passes_the_same_judge():
    result = fs.run_gui([sys.executable, str(ROOT / "src" / "app.py")], str(ROOT / "scripts" / "drive" / "frozen-smoke.json"),
                        timeout=120)
    assert result.judgement.ok, result.judgement.failures


# ── the CONFIGURED layers: the program must be seen to use tokensave and git ──

def _status_envelope(indexed, branch, **over):
    data = {"git": {"branch": branch, "dirty": False, "ahead": 0, "behind": 0, "has_remote": False,
                    "changed_files": [], "changed_truncated": False},
            "tokensave": {"indexed": indexed}, "mcp": {"configured": False, "probed": False},
            "commit_request": {"pending": False}}
    return _envelope(command="status", data=data, **over)


def test_a_status_that_shows_tokensave_and_git_passes():
    assert fs.judge_configured_status(_status_envelope(True, "main"), 0, expected_version="2.6.0").ok


@pytest.mark.parametrize("stdout, expect", [
    (_status_envelope(False, "main"), "tokensave.indexed is False"),
    (_status_envelope(None, "main"), "tokensave.indexed is None"),
    (_status_envelope(True, None), "git.branch is None"),
    (_status_envelope(True, ""), "git.branch"),
    (_envelope(command="status", data={}), "tokensave.indexed"),
    (_status_envelope(True, "main", cli_version="1.0.0"), "cli_version"),
    ("not json", "not JSON"),
])
def test_a_well_formed_but_empty_status_is_not_enough_when_tools_were_given(stdout, expect):
    """The first-run envelope is well-formed and says nothing; here that is the failure."""
    j = fs.judge_configured_status(stdout, 0, expected_version="2.6.0")
    assert not j.ok and any(expect in f for f in j.failures), j.failures


def test_the_first_run_status_would_pass_the_loose_judge_and_fail_the_configured_one():
    """The weakness this exists for, pinned: the same envelope passes one judge and not the other."""
    first_run = _status_envelope(False, None, ok=True)
    assert fs.judge_cli_envelope(first_run, 0, command="status", expected_version="2.6.0").ok
    assert not fs.judge_configured_status(first_run, 0, expected_version="2.6.0").ok


def test_a_skipped_layer_is_still_not_ok_so_a_caller_that_ignores_the_flag_fails_closed():
    skipped = fs.LayerResult(fs.LAYER_CLI_CONFIGURED, "x", fs.Judgement(False, ["no tokensave"]), skipped=True)
    assert not skipped.judgement.ok and fs.layer_ok(skipped)
    assert "SKIPPED" in skipped.line() and "PASS" not in skipped.line()
    failed = fs.LayerResult(fs.LAYER_CLI_CONFIGURED, "x", fs.Judgement(False, ["no tokensave"]))
    assert not fs.layer_ok(failed)


def test_missing_tools_fail_both_configured_layers_and_only_an_explicit_skip_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "find_tools", lambda *_: fs.Tools("", ""))
    failed = fs.configured_layers(str(tmp_path), "2.6.0", str(_script(tmp_path)), workdir=str(tmp_path))
    assert {r.layer for r in failed} == {fs.LAYER_CLI_CONFIGURED, fs.LAYER_GUI_CONFIGURED}
    assert not any(fs.layer_ok(r) for r in failed) and "tokensave and git not found" in failed[0].judgement.failures[0]
    skipped = fs.configured_layers(str(tmp_path), "2.6.0", str(_script(tmp_path)), workdir=str(tmp_path), skip=True)
    assert all(r.skipped and fs.layer_ok(r) and not r.judgement.ok for r in skipped)


def test_a_project_that_cannot_be_made_is_a_failure_even_when_skipping_was_asked_for(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "find_tools", lambda *_: fs.Tools("ts.exe", "git.exe"))
    monkeypatch.setattr(fs, "make_project", lambda *a: (_ for _ in ()).throw(fs.SetupError("init failed")))
    results = fs.configured_layers(str(tmp_path), "2.6.0", str(_script(tmp_path)), workdir=str(tmp_path), skip=True)
    assert not any(fs.layer_ok(r) for r in results) and "init failed" in results[0].judgement.failures[0]


def test_find_tools_prefers_an_explicit_path_then_the_environment(monkeypatch, tmp_path):
    exe = tmp_path / "tokensave.exe"
    exe.write_bytes(b"x")
    other = tmp_path / "other.exe"
    other.write_bytes(b"x")
    monkeypatch.setenv("TOKENSAVE_EXE", str(other))
    assert fs.find_tools(str(exe)).tokensave_exe == str(exe)
    assert fs.find_tools().tokensave_exe == str(other)
    monkeypatch.setenv("TOKENSAVE_EXE", str(tmp_path / "gone.exe"))
    monkeypatch.setattr(fs.shutil, "which", lambda name: None)
    assert fs.find_tools().missing() == ["tokensave", "git"]  # a path that does not exist is not a tool


STAND_IN_CONFIGURED_GUI = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["TSM_SRC"])
from helpers import drive_ledger
script = os.environ["TOKENSAVE_MANAGER_DRIVE"]
config = json.load(open(os.environ["TOKENSAVE_MANAGER_CONFIG"]))
text = open(script, encoding="utf-8").read()
problems = []
if not config.get("tokensave_exe"): problems.append("tokensave_exe not configured")
if not config.get("git_exe"): problems.append("git_exe not configured")
if not str(config.get("template_dir", "")).endswith("templates"): problems.append("template_dir not the build's")
if not config.get("search_roots"): problems.append("no search root")
if "<<PROJECT>>" in text: problems.append("the project variable was not substituted")
if not json.loads(text)[0].get("project", "x"): problems.append("empty project")
now = time.time()
ids = os.environ["STAND_IN_IDS"].split(",")
report = {"report_schema": drive_ledger.REPORT_SCHEMA, "script_hash": drive_ledger.script_hash(script),
          "started_at": now, "finished_at": now + 0.1, "finished_because": "quit", "passed": not problems,
          "expectations": [{"id": i, "passed": not problems, "failure_reason": "; ".join(problems)} for i in ids],
          "steps_failed": [], "startup_diagnostics": [], "drive_diagnostics": []}
open(os.path.splitext(script)[0] + ".report.json", "w").write(json.dumps(report))
sys.exit(1 if problems else 0)
'''


def test_the_configured_gui_run_is_given_the_real_tools_the_builds_templates_and_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_SRC", str(ROOT / "src"))
    monkeypatch.setenv("STAND_IN_IDS", ",".join(fs.GUI_CONFIGURED_REQUIRED_IDS))
    script = tmp_path / "configured.json"
    script.write_text(json.dumps([{"do": "expect", "id": "project_listed", "check": "project_row", "project": "<<PROJECT>>"}]),
                      encoding="utf-8")
    result = fs.run_gui(_app(tmp_path, STAND_IN_CONFIGURED_GUI), str(script), workdir=str(tmp_path),
                        layer=fs.LAYER_GUI_CONFIGURED, required_ids=fs.GUI_CONFIGURED_REQUIRED_IDS,
                        config={"tokensave_exe": "C:/t/tokensave.exe", "git_exe": "C:/g/git.exe",
                                "template_dir": "C:/dist/templates", "search_roots": ["C:/roots"]},
                        script_vars={"PROJECT": "C:/roots/project"})
    assert result.layer == fs.LAYER_GUI_CONFIGURED and result.judgement.ok, result.judgement.failures


def test_the_same_run_fails_when_a_tool_is_not_in_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_SRC", str(ROOT / "src"))
    monkeypatch.setenv("STAND_IN_IDS", ",".join(fs.GUI_CONFIGURED_REQUIRED_IDS))
    script = tmp_path / "configured.json"
    script.write_text(json.dumps([{"do": "expect", "project": "<<PROJECT>>"}]), encoding="utf-8")
    result = fs.run_gui(_app(tmp_path, STAND_IN_CONFIGURED_GUI), str(script), workdir=str(tmp_path),
                        required_ids=fs.GUI_CONFIGURED_REQUIRED_IDS, config={"git_exe": "g"},
                        script_vars={"PROJECT": "p"})
    assert not result.judgement.ok


def test_a_windows_path_survives_substitution_into_the_json_script(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_SRC", str(ROOT / "src"))
    monkeypatch.setenv("STAND_IN_IDS", ",".join(fs.GUI_CONFIGURED_REQUIRED_IDS))
    script = tmp_path / "configured.json"
    script.write_text(json.dumps([{"project": "<<PROJECT>>"}]), encoding="utf-8")
    nasty = 'C:\\Users\\me\\a "b"\\project'
    result = fs.run_gui(_app(tmp_path, STAND_IN_CONFIGURED_GUI), str(script), workdir=str(tmp_path),
                        required_ids=fs.GUI_CONFIGURED_REQUIRED_IDS,
                        config={"tokensave_exe": "t", "git_exe": "g", "template_dir": "x/templates", "search_roots": ["r"]},
                        script_vars={"PROJECT": nasty})
    assert result.judgement.ok, result.judgement.failures
    written = json.loads((Path(result.work_dir) / "drive.json").read_text(encoding="utf-8"))
    assert written[0]["project"] == nasty


STAND_IN_CONFIGURED_CLI = r'''
import json, os, sys
config = json.load(open(os.environ["TOKENSAVE_MANAGER_CONFIG"]))
project = sys.argv[sys.argv.index("--project") + 1]
configured = bool(config.get("tokensave_exe")) and bool(config.get("git_exe"))
indexed = configured and os.path.isdir(os.path.join(project, ".tokensave"))
data = {"git": {"branch": "main" if configured else None}, "tokensave": {"indexed": indexed}}
print(json.dumps({"cli_version": "2.6.0", "command": "status", "ok": True, "error": None, "data": data}))
'''


def test_the_configured_cli_probe_passes_only_when_the_config_it_was_given_is_used(tmp_path):
    project = tmp_path / "p"
    (project / ".tokensave").mkdir(parents=True)
    argv = _app(tmp_path, STAND_IN_CONFIGURED_CLI)
    good = fs._configured_cli(argv, "2.6.0", str(project), {"tokensave_exe": "t", "git_exe": "g"}, str(tmp_path))
    assert good.judgement.ok, good.judgement.failures
    unconfigured = fs._configured_cli(argv, "2.6.0", str(project), {}, str(tmp_path))
    assert not unconfigured.judgement.ok  # first-run config: well-formed, empty, and rightly refused


@pytest.mark.skipif(not (fs.find_tools().tokensave_exe and fs.find_tools().git_exe),
                    reason="needs the real tokensave and git on this machine")
def test_make_project_really_indexes_a_git_repository(tmp_path):
    tools = fs.find_tools()
    project = fs.make_project(str(tmp_path / "roots"), tools)
    assert (Path(project) / ".tokensave").is_dir() and (Path(project) / ".git").is_dir()


def test_the_configured_drive_script_names_a_project_variable_and_every_required_id():
    steps = json.loads((ROOT / "scripts" / "drive" / "frozen-smoke-configured.json").read_text(encoding="utf-8"))
    text = json.dumps(steps)
    assert "<<PROJECT>>" in text and steps[-1] == {"do": "quit"}
    ids = {s.get("id") for s in steps if s.get("do") == "expect"} | {"expect_clean" for s in steps if s.get("do") == "expect_clean"}
    assert set(fs.GUI_CONFIGURED_REQUIRED_IDS) <= ids
    listed = next(s for s in steps if s.get("id") == "project_listed")
    assert listed["check"] == "project_row" and listed["project"] == "<<PROJECT>>"
