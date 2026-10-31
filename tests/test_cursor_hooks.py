"""Tests for helpers/cursor_hooks.py -- the edit to Cursor's agent-control config.

`~/.cursor/hooks.json` decides what runs inside a person's Cursor, so the
properties worth testing are the cautious ones: the edit is a pure function that
can be shown first, a file that does not parse is never replaced, a backup exists
before any write, every other hook survives, and nothing is written when the
plan is "no change".
"""
import json
import os

import pytest

from helpers import cursor_hooks as ch
from helpers.cursor_hooks import (
    STATE_ABSENT,
    STATE_CURRENT,
    STATE_DUPLICATE,
    STATE_STALE,
    STATE_UNREADABLE,
    hook_command,
    hooks_json_path,
    install,
    plan_install,
    plan_uninstall,
    render_script,
    script_path,
    status,
    status_of,
    uninstall,
    unified_diff,
)

RUNNER = ["python", "D:/Claude Co worker/Token Save Manager Source/src/cli.py"]
CONFIG = "C:/Users/me/manager-config.json"
THEIRS = {"command": "node ./hooks/audit.js", "timeout": 5}


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "home")


def _write(home, text):
    os.makedirs(ch.cursor_home(home), exist_ok=True)
    with open(hooks_json_path(home), "w", encoding="utf-8") as fh:
        fh.write(text)


def _data(home):
    with open(hooks_json_path(home), encoding="utf-8") as fh:
        return json.load(fh)


# -- the plan is a pure function -------------------------------------------------------

def test_an_absent_file_plans_a_complete_new_document():
    plan = plan_install(None)
    assert plan.ok and plan.changed and plan.before == STATE_ABSENT
    assert json.loads(plan.new_text) == {
        "version": 1, "hooks": {"sessionEnd": [{"command": hook_command(), "timeout": 30}]}}
    assert plan.new_text.endswith("\n")


def test_a_blank_file_is_treated_like_an_absent_one():
    assert plan_install("  \n").changed


def test_planning_writes_nothing(home):
    plan_install(None)
    assert not os.path.exists(hooks_json_path(home))


def test_the_command_is_relative_to_the_cursor_folder_user_hooks_run_from():
    assert hook_command() == "python ./hooks/tokensave_session_marker.py"


def test_other_events_and_other_keys_survive_untouched():
    existing = json.dumps({"version": 1, "note": "mine", "hooks": {
        "beforeSubmitPrompt": [{"command": "./hooks/check.sh"}]}})
    new = json.loads(plan_install(existing).new_text)
    assert new["note"] == "mine"
    assert new["hooks"]["beforeSubmitPrompt"] == [{"command": "./hooks/check.sh"}]
    assert new["hooks"]["sessionEnd"] == [ch._our_entry()]


def test_a_persons_own_session_end_hook_is_kept_and_ours_goes_after_it():
    existing = json.dumps({"version": 1, "hooks": {"sessionEnd": [THEIRS]}})
    entries = json.loads(plan_install(existing).new_text)["hooks"]["sessionEnd"]
    assert entries == [THEIRS, ch._our_entry()]


def test_a_current_install_plans_no_change_and_returns_the_text_as_it_was():
    first = plan_install(None).new_text
    again = plan_install(first)
    assert again.ok and again.changed is False and again.new_text == first
    assert again.before == STATE_CURRENT


def test_a_stale_entry_is_replaced_not_added_beside():
    old = json.dumps({"version": 1, "hooks": {"sessionEnd": [
        {"command": "python ./hooks/tokensave_session_marker.py", "timeout": 5}]}})
    assert status_of(old) == STATE_STALE
    entries = json.loads(plan_install(old).new_text)["hooks"]["sessionEnd"]
    assert entries == [ch._our_entry()]


def test_a_duplicate_is_collapsed_to_one():
    two = json.dumps({"version": 1, "hooks": {"sessionEnd": [ch._our_entry(), ch._our_entry()]}})
    assert status_of(two) == STATE_DUPLICATE
    assert len(json.loads(plan_install(two).new_text)["hooks"]["sessionEnd"]) == 1


def test_our_entry_is_found_by_its_script_name_not_by_exact_text():
    odd = json.dumps({"version": 1, "hooks": {"sessionEnd": [
        {"command": 'python "C:/Users/x/.cursor/hooks/tokensave_session_marker.py"'}]}})
    assert status_of(odd) != STATE_ABSENT


def test_a_version_the_person_set_is_kept():
    existing = json.dumps({"version": 7, "hooks": {}})
    assert json.loads(plan_install(existing).new_text)["version"] == 7


# -- a file that does not parse is never replaced -------------------------------------------

@pytest.mark.parametrize("bad,why", [
    ("{ not json", "not valid JSON"),
    ("[1, 2]", "not a JSON object"),
    ('{"hooks": []}', "'hooks' in hooks.json is not an object"),
    ('{"hooks": {"sessionEnd": {}}}', "is not a list"),
])
def test_unreadable_files_are_refused_with_the_reason(bad, why):
    plan = plan_install(bad)
    assert not plan.ok and why in plan.error and "not touched" in plan.error
    assert status_of(bad) == STATE_UNREADABLE


def test_install_leaves_a_broken_file_byte_for_byte_alone(home):
    _write(home, "{ not json")
    outcome = install(RUNNER, CONFIG, home)
    assert not outcome.ok and "not valid JSON" in outcome.error
    assert open(hooks_json_path(home), encoding="utf-8").read() == "{ not json"
    assert not os.path.exists(script_path(home))             # nothing half-installed


# -- installing -------------------------------------------------------------------------------------

def test_a_first_install_creates_the_script_and_the_file_with_no_backup_to_make(home):
    outcome = install(RUNNER, CONFIG, home)
    assert outcome.ok and outcome.changed and outcome.backup == ""
    assert os.path.isfile(script_path(home))
    assert status(home) == STATE_CURRENT
    assert _data(home)["hooks"]["sessionEnd"] == [ch._our_entry()]


def test_installing_over_an_existing_file_backs_it_up_byte_for_byte_first(home):
    original = json.dumps({"version": 1, "hooks": {"sessionEnd": [THEIRS]}}, indent=4)
    _write(home, original)
    outcome = install(RUNNER, CONFIG, home, now_ms=1793000000000)
    assert outcome.ok
    assert outcome.backup == hooks_json_path(home) + ".backup.1793000000000"
    assert open(outcome.backup, encoding="utf-8").read() == original
    assert THEIRS in _data(home)["hooks"]["sessionEnd"]


def test_the_script_is_written_before_the_entry_that_names_it(home, monkeypatch):
    order = []
    real = ch._write_atomic

    def spy(path, text):
        order.append(os.path.basename(path))
        return real(path, text)
    monkeypatch.setattr(ch, "_write_atomic", spy)
    install(RUNNER, CONFIG, home)
    assert order == [ch.SCRIPT_BASENAME, ch.HOOKS_FILE]


def test_a_failed_script_write_leaves_hooks_json_untouched(home, monkeypatch):
    _write(home, json.dumps({"version": 1, "hooks": {}}))
    before = open(hooks_json_path(home), encoding="utf-8").read()
    monkeypatch.setattr(ch, "_write_atomic", lambda p, t: (False, "disk full"))
    outcome = install(RUNNER, CONFIG, home)
    assert not outcome.ok and "disk full" in outcome.error
    assert open(hooks_json_path(home), encoding="utf-8").read() == before


def test_a_second_install_changes_nothing_and_takes_no_backup(home):
    install(RUNNER, CONFIG, home)
    text = open(hooks_json_path(home), encoding="utf-8").read()
    again = install(RUNNER, CONFIG, home)
    assert again.ok and again.changed is False and again.backup == ""
    assert open(hooks_json_path(home), encoding="utf-8").read() == text
    assert [f for f in os.listdir(ch.cursor_home(home)) if "backup" in f] == []


def test_moving_the_manager_rewrites_only_the_script(home):
    install(RUNNER, CONFIG, home)
    moved = install(["python", "E:/new/src/cli.py"], CONFIG, home)
    assert moved.ok and moved.changed and moved.backup == ""     # hooks.json was current
    assert "E:/new/src/cli.py" in open(script_path(home), encoding="utf-8").read()


def test_a_failed_backup_stops_before_hooks_json_is_written(home, monkeypatch):
    original = json.dumps({"version": 1, "hooks": {"sessionEnd": [THEIRS]}})
    _write(home, original)
    monkeypatch.setattr(ch.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    outcome = install(RUNNER, CONFIG, home)
    assert not outcome.ok and "could not back up" in outcome.error
    assert open(hooks_json_path(home), encoding="utf-8").read() == original


# -- the script it writes -------------------------------------------------------------------------------

def test_the_script_is_valid_python_with_the_paths_embedded_as_data():
    text = render_script(["python", 'D:\\odd "name"\\cli.py'], 'C:\\a b\\cfg.json')
    compile(text, "hook.py", "exec")
    assert "Generated by TokenSave Manager" in text
    assert 'D:/odd \\"name\\"/cli.py' in text and "C:/a b/cfg.json" in text


def test_the_script_passes_paths_and_ids_never_prompt_text():
    text = render_script(RUNNER, CONFIG)
    assert '"--transcript", str(data.get("transcript_path")' in text
    assert "prompt" not in text.lower().replace("never any prompt", "")


def test_the_script_runs_end_to_end_against_a_fake_cli(tmp_path):
    """Run the generated script for real, with a stand-in CLI that records its argv."""
    import subprocess
    import sys
    fake = tmp_path / "fake_cli.py"
    fake.write_text("import json, sys\nopen(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))\n")
    out = tmp_path / "argv.json"
    script = tmp_path / "hook.py"
    script.write_text(render_script([sys.executable, str(fake), str(out)], CONFIG))
    payload = json.dumps({"workspace_roots": ["D:\\proj"], "session_id": "s1",
                          "reason": "completed", "final_status": "completed",
                          "transcript_path": "C:\\t\\s1.jsonl"})
    done = subprocess.run([sys.executable, str(script)], input=payload,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    argv = json.loads(out.read_text())
    assert argv[:2] == ["handoff", "--project"] and argv[2] == "D:\\proj"
    assert argv[argv.index("--action") + 1] == "marker"
    assert argv[argv.index("--agent") + 1] == "cursor"
    assert argv[argv.index("--session") + 1] == "s1"
    assert argv[argv.index("--transcript") + 1] == "C:\\t\\s1.jsonl"


@pytest.mark.parametrize("stdin", ["", "not json", "{}", '{"workspace_roots": []}',
                                   '{"workspace_roots": [5]}'])
def test_the_script_does_nothing_and_exits_zero_on_a_payload_it_cannot_use(tmp_path, stdin):
    import subprocess
    import sys
    boom = tmp_path / "boom.py"
    boom.write_text("raise SystemExit('must not be called')\n")
    script = tmp_path / "hook.py"
    script.write_text(render_script([sys.executable, str(boom)], CONFIG))
    done = subprocess.run([sys.executable, str(script)], input=stdin,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""


def test_the_script_exits_zero_even_when_the_manager_has_gone(tmp_path):
    import subprocess
    import sys
    script = tmp_path / "hook.py"
    script.write_text(render_script(["Z:/definitely/not/here.exe"], CONFIG))
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          input=json.dumps({"workspace_roots": ["D:\\p"]}), timeout=60)
    assert done.returncode == 0 and done.stderr == ""


# -- uninstalling --------------------------------------------------------------------------------------------

def test_uninstall_removes_only_our_entry_and_our_script(home):
    _write(home, json.dumps({"version": 1, "hooks": {"sessionEnd": [THEIRS],
                                                      "stop": [{"command": "x"}]}}))
    install(RUNNER, CONFIG, home)
    outcome = uninstall(home)
    assert outcome.ok and outcome.changed and outcome.backup
    data = _data(home)
    assert data["hooks"]["sessionEnd"] == [THEIRS] and data["hooks"]["stop"] == [{"command": "x"}]
    assert not os.path.exists(script_path(home))
    assert status(home) == STATE_ABSENT


def test_uninstall_drops_an_empty_session_end_list(home):
    install(RUNNER, CONFIG, home)
    uninstall(home)
    assert "sessionEnd" not in _data(home)["hooks"]


def test_uninstalling_when_nothing_is_there_is_a_no_op(home):
    outcome = uninstall(home)
    assert outcome.ok and outcome.changed is False and outcome.backup == ""


def test_uninstall_leaves_a_script_it_did_not_generate(home):
    os.makedirs(os.path.dirname(script_path(home)))
    with open(script_path(home), "w") as fh:
        fh.write("# written by hand, not by the Manager\n")
    uninstall(home)
    assert os.path.exists(script_path(home))


# -- the diff a person reads first --------------------------------------------------------------------------------

def test_an_absent_file_diffs_against_nothing_and_every_line_is_an_addition():
    diff = unified_diff(None, plan_install(None).new_text, "hooks.json")
    assert "(file does not exist)" in diff
    body = [ln for ln in diff.splitlines() if ln and not ln.startswith(("---", "+++", "@@"))]
    assert body and all(ln.startswith("+") for ln in body)


def test_a_change_to_an_existing_file_shows_only_what_moved():
    old = json.dumps({"version": 1, "hooks": {"sessionEnd": [THEIRS]}}, indent=2) + "\n"
    diff = unified_diff(old, plan_install(old).new_text, "hooks.json")
    assert '+        "command": "python ./hooks/tokensave_session_marker.py"' in diff
    removed = [ln for ln in diff.splitlines()[2:] if ln.startswith("-")]
    assert not any("audit.js" in ln for ln in removed)          # their hook is context, not removed
