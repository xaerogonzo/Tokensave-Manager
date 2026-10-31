"""`manager-cli handoff`: what an agent actually runs.

Everything goes through `main()` with a real config file, because the contract
that matters is the envelope an agent parses and the exit code it branches on:
refused-because-off is not the same as failed, and neither is a bad argument.
"""
import json
import os
import subprocess

import pytest

from cli import EXIT_FAILED, EXIT_OK, EXIT_PREREQUISITE, EXIT_USAGE, main
from helpers import handoff_ledger as hl


def _run(capsys, argv):
    code = main(argv)
    out, err = capsys.readouterr()
    return code, json.loads(out), err


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _config(tmp_path, **raw):
    path = tmp_path / "manager-config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return str(path)


def _go(capsys, project, config, *extra):
    return _run(capsys, ["handoff", "--project", project, "--config", config,
                         "--json", *extra])


def _repo(project):
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "t")):
        subprocess.run(["git", "-C", project, *args], check=True, capture_output=True)
    with open(os.path.join(project, ".gitignore"), "w") as fh:
        fh.write(".tokensave/\n")
    with open(os.path.join(project, "a.txt"), "w") as fh:
        fh.write("1")
    subprocess.run(["git", "-C", project, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", project, "commit", "-q", "-m", "first"],
                   check=True, capture_output=True)


PUBLISH = ["--action", "publish", "--agent", "cursor", "--session", "s1",
           "--goal", "wire the thing", "--decision", "use a ledger",
           "--decision", "keep it local", "--next-step", "write tests",
           "--question", "who clears it?", "--file", "src/a.py"]


# -- opt-in -------------------------------------------------------------------------

def test_publishing_is_refused_when_the_person_has_not_opted_in(capsys, project, tmp_path):
    cfg = _config(tmp_path)                                  # no flag at all
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_PREREQUISITE
    assert env["ok"] is False and env["data"]["enabled"] is False
    assert "off" in env["error"]
    assert hl.read(project).state == hl.STATE_ABSENT         # nothing was written


@pytest.mark.parametrize("flag", [False, 1, "true", "yes", None, 0])
def test_only_a_literal_true_counts_as_consent(capsys, project, tmp_path, flag):
    cfg = _config(tmp_path, enable_agent_handoff=flag)
    code, _, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_PREREQUISITE
    assert hl.read(project).state == hl.STATE_ABSENT


def test_the_config_property_agrees_with_the_cli():
    from state import ManagerConfig
    assert ManagerConfig(raw={}).enable_agent_handoff is False
    assert ManagerConfig(raw={"enable_agent_handoff": "true"}).enable_agent_handoff is False
    assert ManagerConfig(raw={"enable_agent_handoff": True}).enable_agent_handoff is True


# -- publishing -------------------------------------------------------------------------

def test_a_publish_records_the_agents_words_and_measures_the_rest(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK and env["ok"] is True
    assert env["data"]["written"] is True and env["data"]["duplicate"] is False
    entry = hl.read(project).entries[0]
    assert entry["agent"] == "cursor" and entry["goal"] == "wire the thing"
    assert entry["decisions"] == ["use a ledger", "keep it local"]
    assert entry["next_steps"] == ["write tests"] and entry["open_questions"] == ["who clears it?"]
    assert entry["agent_reported_files"] == ["src/a.py"]
    assert entry["branch"] == "main" and len(entry["base_commit"]) == 40


def test_a_retry_after_a_timeout_is_reported_as_a_duplicate_not_written_twice(
        capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, *PUBLISH)
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK
    assert env["data"]["duplicate"] is True and env["data"]["written"] is False
    assert len(hl.read(project).entries) == 1


def test_publishing_without_an_agent_is_a_usage_error(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, "--action", "publish", "--goal", "x")
    assert code == EXIT_USAGE and "--agent" in env["error"]


def test_an_empty_publish_is_refused_with_the_reason(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, "--action", "publish", "--agent", "claude")
    assert code == EXIT_USAGE and "nothing to record" in env["error"]


def test_a_bad_agent_id_is_a_usage_error_not_a_crash(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, "--action", "publish", "--agent",
                       "Not Valid!", "--goal", "x")
    assert code == EXIT_USAGE and "not a valid id" in env["error"]


def test_a_ledger_that_would_be_committed_produces_a_warning(capsys, project, tmp_path):
    _repo(project)
    os.remove(os.path.join(project, ".gitignore"))           # nothing ignores .tokensave now
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK and any("NOT git-ignored" in w for w in env["warnings"])


def test_a_secret_in_a_published_decision_never_reaches_the_file(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, "--action", "publish", "--agent", "claude",
        "--decision", "deploy with sk-abcdefghijklmnop1234")
    with open(hl.ledger_path(project), encoding="utf-8") as fh:
        assert "sk-abcdefghijklmnop1234" not in fh.read()


# -- reading works whether or not it is on ----------------------------------------------------

def test_status_of_a_missing_ledger_says_absent_and_whether_it_is_enabled(
        capsys, project, tmp_path):
    code, env, _ = _go(capsys, project, _config(tmp_path))
    assert code == EXIT_OK
    assert env["data"]["state"] == "absent" and env["data"]["enabled"] is False
    assert env["data"]["entries"] == 0


def test_list_still_works_after_the_person_turns_it_off(capsys, project, tmp_path):
    on = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, on, *PUBLISH)
    off = _config(tmp_path, enable_agent_handoff=False)
    code, env, _ = _go(capsys, project, off, "--action", "list")
    assert code == EXIT_OK and len(env["data"]["entries"]) == 1


def test_list_returns_the_newest_entries_and_honours_the_limit(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    for n in range(4):
        _go(capsys, project, cfg, "--action", "publish", "--agent", "claude",
            "--goal", f"goal {n}")
    _, env, _ = _go(capsys, project, cfg, "--action", "list", "--limit", "2")
    assert [e["goal"] for e in env["data"]["entries"]] == ["goal 2", "goal 3"]


def test_status_reports_unreadable_lines_instead_of_hiding_them(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, *PUBLISH)
    with open(hl.ledger_path(project), "ab") as fh:
        fh.write(b"not json\n")
    _, env, _ = _go(capsys, project, cfg, "--action", "status")
    assert env["data"]["bad_lines"] == 1
    assert any("could not be read" in w for w in env["warnings"])


def test_status_flags_a_ledger_that_git_would_not_ignore(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, *PUBLISH)
    os.remove(os.path.join(project, ".gitignore"))
    _, env, _ = _go(capsys, project, cfg, "--action", "status")
    assert env["data"]["ignored_by_git"] is False
    assert any("not git-ignored" in w for w in env["warnings"])


# -- clearing -------------------------------------------------------------------------------------

def test_clear_needs_an_explicit_yes(capsys, project, tmp_path):
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, *PUBLISH)
    code, env, _ = _go(capsys, project, cfg, "--action", "clear")
    assert code == EXIT_USAGE and "--yes" in env["error"]
    assert len(hl.read(project).entries) == 1                 # untouched


def test_clear_with_yes_deletes_the_ledger_even_when_handoff_is_off(capsys, project, tmp_path):
    on = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, on, *PUBLISH)
    off = _config(tmp_path, enable_agent_handoff=False)
    code, env, _ = _go(capsys, project, off, "--action", "clear", "--yes")
    assert code == EXIT_OK and env["data"]["cleared"] is True
    assert hl.read(project).state == hl.STATE_ABSENT


# -- the contract -----------------------------------------------------------------------------------

def test_the_default_action_is_the_read_only_one(capsys, project, tmp_path):
    code, env, _ = _go(capsys, project, _config(tmp_path))
    assert code == EXIT_OK and "state" in env["data"] and "entries" in env["data"]


def test_handoff_is_classified_as_mutating_and_needs_a_project():
    from helpers import commands
    cmd = commands.BY_ACTION["handoff"]
    assert cmd.side_effect == commands.MUTATING and cmd.requires_project
