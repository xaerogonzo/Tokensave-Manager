"""Tests for helpers/handoff_rules.py -- the git-ignored rule that names the command.

The whole point of the file is that it holds THIS machine's paths and must never
reach a commit, so most of these are about git: it is ignored before it is
written, a tracked one is refused, a worktree resolves to the right exclude
file, and removing the block leaves a person's own text alone.
"""
import os
import subprocess

import pytest

from helpers import handoff_rules as hr
from helpers.handoff_rules import (
    RULE_FILE,
    STATE_ABSENT,
    STATE_CURRENT,
    STATE_STALE,
    STATE_TRACKED,
    install,
    remove,
    render_rule,
    rule_path,
    status,
)

RUNNER = ["python", "D:/Claude Co worker/Token Save Manager Source/src/cli.py"]
CONFIG = "C:/Users/me/manager-config.json"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "t")
    with open(os.path.join(root, "a.txt"), "w") as fh:
        fh.write("1")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _text(project):
    with open(rule_path(project), encoding="utf-8") as fh:
        return fh.read()


# -- the text ----------------------------------------------------------------------

def test_the_rule_names_the_command_with_both_paths_quoted_and_slashed():
    text = render_rule(RUNNER, "D:\\Random Projects\\Fortuna Lab", "C:\\Users\\me\\cfg.json")
    assert 'python "D:/Claude Co worker/Token Save Manager Source/src/cli.py" handoff' in text
    assert '"python"' not in text                    # a bare interpreter is left bare
    assert '--project "D:/Random Projects/Fortuna Lab"' in text
    assert '--config "C:/Users/me/cfg.json"' in text
    assert "\\" not in text.split("```")[1]          # no backslash inside the command


def test_the_rule_tells_the_agent_how_to_publish_read_and_when_to_stop():
    text = render_rule(RUNNER, "P", CONFIG)
    assert "--action publish --agent <your-name>" in text
    assert "--action list --limit 5" in text
    assert "If the command says handoff is off, stop" in text
    assert "Never put a secret" in text


def test_the_rule_is_deterministic():
    assert render_rule(RUNNER, "P", CONFIG) == render_rule(RUNNER, "P", CONFIG)


# -- installing -----------------------------------------------------------------------

def test_install_writes_the_block_and_git_sees_nothing_to_commit(repo):
    result = install(repo, RUNNER, CONFIG)
    assert result.ok and result.changed and result.state == STATE_CURRENT
    assert hr.BEGIN_MARKER in _text(repo) and "--action publish" in _text(repo)
    assert _git(repo, "status", "--short").strip() == ""      # ignored, not untracked
    assert hr.is_ignored(repo) is True


def test_the_file_is_already_ignored_at_the_moment_it_is_written(repo, monkeypatch):
    """Ignore first, write second: no window where `git add -A` could take it."""
    seen = {}
    real = hr._write_atomic

    def spy(path, text):
        seen["ignored_before_write"] = hr.is_ignored(repo)
        return real(path, text)
    monkeypatch.setattr(hr, "_write_atomic", spy)
    assert install(repo, RUNNER, CONFIG).ok
    assert seen["ignored_before_write"] is True


def test_nothing_is_written_when_it_cannot_be_ignored(repo, monkeypatch):
    monkeypatch.setattr(hr, "_exclude_path", lambda *a: "")
    result = install(repo, RUNNER, CONFIG)
    assert not result.ok and "not written" in result.error
    assert not os.path.exists(rule_path(repo))


def test_a_second_install_changes_nothing(repo):
    install(repo, RUNNER, CONFIG)
    before = _text(repo)
    again = install(repo, RUNNER, CONFIG)
    assert again.ok and again.changed is False and _text(repo) == before


def test_a_persons_own_text_around_the_block_survives_install(repo):
    with open(rule_path(repo), "w", encoding="utf-8") as fh:
        fh.write("# My private notes\n\nDo not delete me.\n")
    install(repo, RUNNER, CONFIG)
    text = _text(repo)
    assert "Do not delete me." in text and hr.BEGIN_MARKER in text
    assert text.startswith("# My private notes")


def test_moving_the_manager_makes_the_block_stale_and_a_reinstall_fixes_it(repo):
    install(repo, RUNNER, CONFIG)
    moved = ["python", "E:/elsewhere/src/cli.py"]
    assert status(repo, RUNNER, CONFIG) == STATE_CURRENT
    assert status(repo, moved, CONFIG) == STATE_STALE
    install(repo, moved, CONFIG)
    assert "E:/elsewhere/src/cli.py" in _text(repo) and "Token Save Manager" not in \
        _text(repo).split("```")[1]
    assert status(repo, moved, CONFIG) == STATE_CURRENT


def test_status_before_anything_is_absent(repo):
    assert status(repo, RUNNER, CONFIG) == STATE_ABSENT


# -- the file git would commit -------------------------------------------------------------

def test_a_committed_rule_file_is_refused_and_left_untouched(repo):
    with open(rule_path(repo), "w", encoding="utf-8") as fh:
        fh.write("shared team rules\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "someone committed it")
    result = install(repo, RUNNER, CONFIG)
    assert not result.ok and result.state == STATE_TRACKED and "committed" in result.error
    assert _text(repo) == "shared team rules\n"
    assert status(repo, RUNNER, CONFIG) == STATE_TRACKED


def test_a_file_already_ignored_by_gitignore_does_not_touch_the_exclude_file(repo):
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write("CLAUDE.local.md\n")
    exclude = os.path.join(repo, ".git", "info", "exclude")
    before = open(exclude).read() if os.path.exists(exclude) else ""
    assert install(repo, RUNNER, CONFIG).ok
    after = open(exclude).read() if os.path.exists(exclude) else ""
    assert after == before


def test_an_exclude_file_without_a_trailing_newline_is_not_glued_to(repo):
    exclude = os.path.join(repo, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "w") as fh:
        fh.write("*.log")                                   # no newline
    assert install(repo, RUNNER, CONFIG).ok
    assert open(exclude).read().splitlines()[-2:] == ["*.log", RULE_FILE]


def test_outside_a_repository_it_still_writes_there_is_nothing_to_commit_to(tmp_path):
    folder = str(tmp_path / "plain")
    os.makedirs(folder)
    result = install(folder, RUNNER, CONFIG)
    assert result.ok and os.path.isfile(rule_path(folder))


def test_a_missing_folder_is_an_error_not_a_created_directory(tmp_path):
    result = install(str(tmp_path / "nope"), RUNNER, CONFIG)
    assert not result.ok and "no such folder" in result.error
    assert not os.path.exists(str(tmp_path / "nope"))


def test_a_linked_worktree_is_ignored_through_the_shared_exclude_file(repo, tmp_path):
    wt = str(tmp_path / "wt")
    _git(repo, "worktree", "add", "-q", "-b", "feature", wt)
    result = install(wt, RUNNER, CONFIG)
    assert result.ok
    assert _git(wt, "status", "--short").strip() == ""
    # The exclude file is the clone's, shared by every worktree of it.
    assert RULE_FILE in open(os.path.join(repo, ".git", "info", "exclude")).read()
    assert hr.is_ignored(repo) is True


def test_only_one_file_is_written_cursor_reads_claude_local_md_too(repo):
    """Measured: Cursor loaded CLAUDE.local.md AND its own .mdc, so a second file
    would put the instruction in its context twice."""
    install(repo, RUNNER, CONFIG)
    assert not os.path.exists(os.path.join(repo, ".cursor"))
    assert os.listdir(repo).count(RULE_FILE) == 1


# -- removing ------------------------------------------------------------------------------------

def test_remove_keeps_a_persons_text_and_takes_only_the_block(repo):
    with open(rule_path(repo), "w", encoding="utf-8") as fh:
        fh.write("# Mine\n\nkeep this\n")
    install(repo, RUNNER, CONFIG)
    result = remove(repo)
    assert result.ok and result.changed
    text = _text(repo)
    assert "keep this" in text and hr.BEGIN_MARKER not in text


def test_remove_deletes_the_file_when_nothing_else_is_in_it(repo):
    install(repo, RUNNER, CONFIG)
    assert remove(repo).ok
    assert not os.path.exists(rule_path(repo))


def test_removing_when_there_is_no_block_changes_nothing(repo):
    with open(rule_path(repo), "w", encoding="utf-8") as fh:
        fh.write("only mine\n")
    result = remove(repo)
    assert result.ok and result.changed is False and _text(repo) == "only mine\n"
    assert remove(str(repo) + "-gone").ok                    # absent file, absent folder


def test_the_exclude_line_is_left_after_removal(repo):
    install(repo, RUNNER, CONFIG)
    remove(repo)
    assert RULE_FILE in open(os.path.join(repo, ".git", "info", "exclude")).read()
