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


def _nested(project):
    """A plain folder inside someone else's repository, with nothing ignoring it."""
    _repo(project)
    os.remove(os.path.join(project, ".gitignore"))
    child = os.path.join(project, "child")
    os.makedirs(child)
    return child


def test_a_ledger_that_would_be_committed_produces_a_warning(capsys, project, tmp_path):
    child = _nested(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, child, cfg, *PUBLISH)
    assert code == EXIT_OK and any("NOT git-ignored" in w for w in env["warnings"])


def test_a_ledger_inside_a_repository_needs_no_ignore_rule_and_says_so(capsys, project, tmp_path):
    _repo(project)
    os.remove(os.path.join(project, ".gitignore"))
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    # The memory warning (no tokensave configured here) is a different matter.
    assert code == EXIT_OK and not [w for w in env["warnings"] if "ignored" in w or "retention" in w]
    assert env["data"]["path"].endswith(os.path.join(".git", "tokensave", "handoff.jsonl"))
    _, status, _ = _go(capsys, project, cfg)
    assert status["data"]["location"] == "git" and status["data"]["ignored_by_git"] is True


def test_a_worktree_publishes_and_the_main_checkout_lists_it(capsys, project, tmp_path):
    _repo(project)
    wt = str(tmp_path / "wt")
    subprocess.run(["git", "-C", project, "worktree", "add", "-q", "-b", "f", wt],
                   check=True, capture_output=True)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, wt, cfg, *PUBLISH)
    _, env, _ = _go(capsys, project, cfg, "--action", "list")
    assert [e["goal"] for e in env["data"]["entries"]] == ["wire the thing"]


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
    child = _nested(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, child, cfg, *PUBLISH)
    _, env, _ = _go(capsys, child, cfg, "--action", "status")
    assert env["data"]["ignored_by_git"] is False and env["data"]["location"] == "project"
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


# -- the local rule ----------------------------------------------------------------------------

def test_the_rule_is_refused_while_handoff_is_off(capsys, project, tmp_path):
    _repo(project)
    code, env, _ = _go(capsys, project, _config(tmp_path), "--action", "rule")
    assert code == EXIT_PREREQUISITE and env["data"]["enabled"] is False
    assert not os.path.exists(os.path.join(project, "CLAUDE.local.md"))


def test_the_rule_writes_one_ignored_file_naming_this_project_and_config(
        capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, "--action", "rule")
    assert code == EXIT_OK and env["data"]["installed"] and env["data"]["changed"]
    text = open(os.path.join(project, "CLAUDE.local.md"), encoding="utf-8").read()
    assert project.replace("\\", "/") in text
    assert cfg.replace("\\", "/") in text
    assert "handoff --project" in text and "--action publish" in text
    status = subprocess.run(["git", "-C", project, "status", "--short"],
                            capture_output=True, text=True).stdout
    assert status.strip() == ""                  # ignored: nothing for git to commit


def test_status_reports_the_rule_state_and_it_follows_the_install(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _, before, _ = _go(capsys, project, cfg)
    assert before["data"]["rule"] == "absent"
    _go(capsys, project, cfg, "--action", "rule")
    _, after, _ = _go(capsys, project, cfg)
    assert after["data"]["rule"] == "current"


def test_a_committed_rule_file_is_refused_with_a_failure_not_overwritten(
        capsys, project, tmp_path):
    _repo(project)
    with open(os.path.join(project, "CLAUDE.local.md"), "w") as fh:
        fh.write("team file\n")
    subprocess.run(["git", "-C", project, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", project, "commit", "-q", "-m", "x"], check=True,
                   capture_output=True)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    code, env, _ = _go(capsys, project, cfg, "--action", "rule")
    assert code == EXIT_FAILED and "committed" in env["error"]
    assert open(os.path.join(project, "CLAUDE.local.md")).read() == "team file\n"


def test_unrule_works_even_when_handoff_is_off(capsys, project, tmp_path):
    _repo(project)
    on = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, on, "--action", "rule")
    off = _config(tmp_path, enable_agent_handoff=False)
    code, env, _ = _go(capsys, project, off, "--action", "unrule")
    assert code == EXIT_OK and env["data"]["removed"] is True
    assert not os.path.exists(os.path.join(project, "CLAUDE.local.md"))


# -- the session marker (called by the Cursor hook for every session) -------------------------------------

MARKER = ["--action", "marker", "--agent", "cursor", "--session", "s1", "--reason", "completed",
          "--final-status", "completed", "--transcript", "C:/t/s1.jsonl"]


def _opt_in(capsys, project, cfg):
    _go(capsys, project, cfg, "--action", "rule")


def test_a_marker_is_not_written_while_handoff_is_off(capsys, project, tmp_path):
    _repo(project)
    code, env, _ = _go(capsys, project, _config(tmp_path), *MARKER)
    assert code == EXIT_PREREQUISITE and env["data"]["recorded"] is False
    assert hl.read(project).state == hl.STATE_ABSENT


def test_a_project_that_has_not_opted_in_gets_no_marker_and_no_failure(capsys, project, tmp_path):
    """The hook fires for every Cursor session on the machine; only opted-in
    projects may be written to, and the rest must be a quiet, non-error refusal."""
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)          # on, but no rule here
    code, env, _ = _go(capsys, project, cfg, *MARKER)
    assert code == EXIT_PREREQUISITE and env["data"]["recorded"] is False
    assert "not opted in" in env["error"]
    assert hl.read(project).state == hl.STATE_ABSENT


def test_an_opted_in_project_records_a_session_marker_with_paths_only(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _opt_in(capsys, project, cfg)
    code, env, _ = _go(capsys, project, cfg, *MARKER)
    assert code == EXIT_OK and env["data"]["recorded"] is True
    entry = hl.read(project).entries[0]
    assert entry["kind"] == "session" and entry["agent"] == "cursor"
    assert entry["session_id"] == "s1"
    assert entry["provenance"] == {"source": "hook", "reason": "completed",
                                   "final_status": "completed",
                                   "transcript_path": "C:/t/s1.jsonl"}
    assert entry["goal"] == "" and entry["decisions"] == []         # a hook has no summary


def test_the_same_session_ending_twice_is_recorded_once(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _opt_in(capsys, project, cfg)
    _go(capsys, project, cfg, *MARKER)
    _, again, _ = _go(capsys, project, cfg, *MARKER)
    assert again["data"]["duplicate"] is True and len(hl.read(project).entries) == 1


def test_a_marker_needs_an_agent(capsys, project, tmp_path):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _opt_in(capsys, project, cfg)
    code, env, _ = _go(capsys, project, cfg, "--action", "marker")
    assert code == EXIT_USAGE and "--agent" in env["error"]


# -- the tokensave memory mirror ----------------------------------------------------------

@pytest.fixture
def memory(monkeypatch):
    from helpers import handoff_memory
    rows = []

    def call(exe, tool, project, payload):
        if not exe:
            return None, "tokensave is not configured (no tokensave_exe)"
        if tool == "session_recall":
            needle = payload["query"].strip('"')
            return {"decisions": [r for r in rows if needle in (r["reason"] or "")]}, ""
        rows.append({"text": payload["text"], "reason": payload["reason"],
                     "tags": payload["tags"]})
        return {"status": "recorded"}, ""
    monkeypatch.setattr(handoff_memory, "_call", call)
    return rows


def test_publish_mirrors_its_decisions_and_a_repeat_adds_nothing(capsys, project, tmp_path, memory):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True, tokensave_exe="ts.exe")
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK and env["warnings"] == []
    assert env["data"]["memory"][0]["state"] == "mirrored" and len(memory) == 2
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK and env["data"]["duplicate"] is True
    assert env["data"]["memory"][0]["state"] == "already" and len(memory) == 2


def test_a_publish_survives_a_memory_that_cannot_be_reached(capsys, project, tmp_path, memory):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)       # no tokensave_exe
    code, env, _ = _go(capsys, project, cfg, *PUBLISH)
    assert code == EXIT_OK and env["data"]["written"] is True
    assert env["data"]["memory"][0]["state"] == "pending"
    assert any("not yet in tokensave memory" in w for w in env["warnings"])
    assert hl.read(project).entries                            # the ledger kept it


def test_mirror_repairs_a_pending_entry_later(capsys, project, tmp_path, memory):
    _repo(project)
    _go(capsys, project, _config(tmp_path, enable_agent_handoff=True), *PUBLISH)
    fixed = _config(tmp_path, enable_agent_handoff=True, tokensave_exe="ts.exe")
    code, env, _ = _go(capsys, project, fixed, "--action", "mirror")
    assert code == EXIT_OK and len(memory) == 2
    code, env, _ = _go(capsys, project, fixed, "--action", "mirror")
    assert code == EXIT_OK and len(memory) == 2


def test_mirror_with_no_tokensave_reports_failure_not_success(capsys, project, tmp_path, memory):
    _repo(project)
    cfg = _config(tmp_path, enable_agent_handoff=True)
    _go(capsys, project, cfg, *PUBLISH)
    code, env, _ = _go(capsys, project, cfg, "--action", "mirror")
    assert code == EXIT_FAILED and "not configured" in env["error"]


def test_mirror_is_refused_while_handoff_is_off(capsys, project, tmp_path, memory):
    code, env, _ = _go(capsys, project, _config(tmp_path), "--action", "mirror")
    assert code == EXIT_PREREQUISITE and memory == []
