"""The Claude-memory snapshot: a private, one-time copy for Cursor.

What must hold: it never lands in a commit, it is honest that it is a copy, it
copies only the index, and a project with no memory is a refusal and not an
empty file that looks configured.
"""
import json
import os
import subprocess

import pytest

from cli import EXIT_OK, EXIT_PREREQUISITE, main
from helpers import memory_snapshot as ms
from helpers.claude_tasks import encode_project_path

INDEX = ("# Memory Index\n\n"
         "- [Profile](user_profile.md) — who the user is\n"
         "- [Paywalled](reference_paywalled.md) — token=abc123secret99 lives here\n")


def _git(project, *args):
    return subprocess.run(["git", "-C", project, *args], capture_output=True, text=True)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "home")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _repo(project):
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@e.com")
    _git(project, "config", "user.name", "t")
    with open(os.path.join(project, "a.txt"), "w") as fh:
        fh.write("1")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "first")


def _memory(home, project, index=INDEX):
    folder = ms.memory_dir(project, home)
    os.makedirs(folder)
    with open(os.path.join(folder, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(index)
    return folder


def test_memory_dir_uses_claudes_own_encoding(home, project):
    want = os.path.join(home, ".claude", "projects",
                        encode_project_path(os.path.normpath(project)), "memory")
    assert ms.memory_dir(project, home) == want


def test_it_copies_the_index_and_names_the_folder(home, project):
    folder = _memory(home, project)
    done = ms.write(project, home)
    assert done.ok and done.entries == 2
    text = open(ms.rule_path(project), encoding="utf-8").read()
    assert "alwaysApply: true" in text
    assert folder in text
    assert "[Profile](user_profile.md)" in text


def test_it_says_it_is_a_copy_and_when_it_was_taken(home, project):
    _memory(home, project)
    ms.write(project, home)
    text = open(ms.rule_path(project), encoding="utf-8").read()
    assert "snapshot" in text and "not kept up to date" in text


def test_secret_shapes_in_the_index_are_masked(home, project):
    _memory(home, project)
    ms.write(project, home)
    assert "abc123secret99" not in open(ms.rule_path(project), encoding="utf-8").read()


def test_only_the_index_is_copied_never_a_note(home, project):
    folder = _memory(home, project)
    with open(os.path.join(folder, "user_profile.md"), "w") as fh:
        fh.write("PRIVATE-NOTE-BODY")
    ms.write(project, home)
    assert "PRIVATE-NOTE-BODY" not in open(ms.rule_path(project), encoding="utf-8").read()


def test_no_memory_is_a_refusal_not_an_empty_rule(home, project):
    done = ms.write(project, home)
    assert not done.ok and done.state == ms.STATE_NO_SOURCE
    assert not os.path.exists(ms.rule_path(project))


def test_an_oversized_index_is_cut_on_a_line_and_reported(home, project):
    big = "# Memory Index\n" + "".join(f"- [n{i}](n{i}.md) — {'x' * 80}\n" for i in range(400))
    _memory(home, project, big)
    done = ms.write(project, home)
    assert done.ok and done.truncated
    text = open(ms.rule_path(project), encoding="utf-8").read()
    assert "longer than this snapshot keeps" in text
    assert text.rstrip().splitlines()[-2].startswith("- [n") or "longer" in text


def test_in_a_repository_the_file_is_ignored_so_it_cannot_be_committed(home, project):
    _repo(project)
    _memory(home, project)
    assert ms.write(project, home).ok
    assert _git(project, "check-ignore", "-q", "--", ms.RULE_RELPATH).returncode == 0
    _git(project, "add", "-A")
    assert ms.RULE_RELPATH not in _git(project, "ls-files").stdout


def test_a_tracked_file_is_never_overwritten(home, project):
    _repo(project)
    _memory(home, project)
    os.makedirs(os.path.dirname(ms.rule_path(project)))
    with open(ms.rule_path(project), "w") as fh:
        fh.write("committed by someone")
    _git(project, "add", "-f", "-A")
    _git(project, "commit", "-q", "-m", "track it")
    done = ms.write(project, home)
    assert not done.ok and done.state == ms.STATE_TRACKED
    assert open(ms.rule_path(project)).read() == "committed by someone"


def test_status_is_derived_and_a_changed_index_reads_as_stale(home, project):
    folder = _memory(home, project)
    assert ms.status(project, home) == ms.STATE_ABSENT
    ms.write(project, home)
    assert ms.status(project, home) == ms.STATE_CURRENT
    with open(os.path.join(folder, "MEMORY.md"), "a", encoding="utf-8") as fh:
        fh.write("- [New](new.md) — added later\n")
    assert ms.status(project, home) == ms.STATE_STALE


def test_remove_deletes_only_what_the_manager_wrote(home, project):
    _memory(home, project)
    ms.write(project, home)
    assert ms.remove(project).changed and not os.path.exists(ms.rule_path(project))
    os.makedirs(os.path.dirname(ms.rule_path(project)), exist_ok=True)
    with open(ms.rule_path(project), "w") as fh:
        fh.write("someone else's rule")
    gone = ms.remove(project)
    assert not gone.ok
    assert open(ms.rule_path(project)).read() == "someone else's rule"


def test_the_shared_committed_cursor_rule_is_never_touched(home, project):
    _memory(home, project)
    shared = os.path.join(project, ".cursor", "rules", "tokensave.mdc")
    os.makedirs(os.path.dirname(shared))
    with open(shared, "w") as fh:
        fh.write("shared")
    ms.write(project, home)
    assert open(shared).read() == "shared"


# -- through the CLI ----------------------------------------------------------------

def _cli(capsys, tmp_path, project, action, monkeypatch, home):
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("HOME", home)
    cfg = tmp_path / "manager-config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    code = main(["handoff", "--project", project, "--config", str(cfg), "--json",
                 "--action", action])
    return code, json.loads(capsys.readouterr().out)


def test_cli_import_writes_and_reports(capsys, tmp_path, project, home, monkeypatch):
    _memory(home, project)
    code, env = _cli(capsys, tmp_path, project, "memory-import", monkeypatch, home)
    assert code == EXIT_OK and env["data"]["entries"] == 2
    assert os.path.exists(ms.rule_path(project))


def test_cli_import_without_memory_is_a_prerequisite_exit(capsys, tmp_path, project, home, monkeypatch):
    code, env = _cli(capsys, tmp_path, project, "memory-import", monkeypatch, home)
    assert code == EXIT_PREREQUISITE and not os.path.exists(ms.rule_path(project))


def test_cli_import_works_with_handoff_off(capsys, tmp_path, project, home, monkeypatch):
    # It is a named request for a copy, not a ledger write.
    _memory(home, project)
    code, _env = _cli(capsys, tmp_path, project, "memory-import", monkeypatch, home)
    assert code == EXIT_OK


def test_cli_remove(capsys, tmp_path, project, home, monkeypatch):
    _memory(home, project)
    _cli(capsys, tmp_path, project, "memory-import", monkeypatch, home)
    code, env = _cli(capsys, tmp_path, project, "memory-remove", monkeypatch, home)
    assert code == EXIT_OK and env["data"]["removed"] is True
