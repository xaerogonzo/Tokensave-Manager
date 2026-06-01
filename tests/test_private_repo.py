"""Tests for helpers/private_repo.py — sync private local git repos."""

import os
import subprocess
import pytest
from types import SimpleNamespace

from constants import CREATE_NO_WINDOW
from helpers.private_repo import sync_private_repo


def test_returns_false_if_dest_missing(monkeypatch, tmp_path):
    """Returns False and logs error if dest doesn't exist."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "nonexistent")
    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert len(logs) == 1
    assert "Private repo missing" in logs[0][0]


def test_copies_file_from_src_to_dest(monkeypatch, tmp_path):
    """Copies a file that exists in src to dest."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    test_file = "subdir/file.txt"
    src_file = os.path.join(src_path, test_file)
    os.makedirs(os.path.dirname(src_file))
    with open(src_file, "w") as f:
        f.write("content")

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [test_file], on_log)

    assert os.path.isfile(os.path.join(dest, test_file))
    with open(os.path.join(dest, test_file)) as f:
        assert f.read() == "content"


def test_removes_file_not_in_src(monkeypatch, tmp_path):
    """Removes a file from dest if it doesn't exist in src."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    test_file = "file.txt"
    dst_file = os.path.join(dest, test_file)
    with open(dst_file, "w") as f:
        f.write("to be deleted")

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [test_file], on_log)

    assert not os.path.isfile(dst_file)


def test_prunes_empty_parent_dirs(monkeypatch, tmp_path):
    """Prunes empty parent directories but not repo root."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    test_file = "a/b/c/file.txt"
    dst_file = os.path.join(dest, test_file)
    os.makedirs(os.path.dirname(dst_file))
    with open(dst_file, "w") as f:
        f.write("to be deleted")

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [test_file], on_log)

    assert not os.path.isfile(dst_file)
    assert not os.path.isdir(os.path.join(dest, "a"))


def test_does_not_prune_repo_root(monkeypatch, tmp_path):
    """Never prunes the repo root itself."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert os.path.isdir(dest)


def test_returns_true_if_nothing_to_commit(monkeypatch, tmp_path):
    """Returns True if git status shows no changes."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is True
    assert any("already up to date" in msg for msg, _ in logs)


def test_returns_false_if_git_status_fails_file_not_found(monkeypatch, tmp_path):
    """Returns False if git status raises FileNotFoundError."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    def mock_run(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git status failed" in msg for msg, _ in logs)


def test_returns_false_if_git_status_timeout(monkeypatch, tmp_path):
    """Returns False if git status times out."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    def mock_run(*a, **k):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git status failed" in msg for msg, _ in logs)


def test_returns_false_if_git_add_fails(monkeypatch, tmp_path):
    """Returns False if git add returns non-zero."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        else:  # git add
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal error")

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git add:" in msg for msg, _ in logs)


def test_returns_false_if_git_add_timeout(monkeypatch, tmp_path):
    """Returns False if git add times out."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        else:  # git add
            raise subprocess.TimeoutExpired("git", 15)

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git add failed" in msg for msg, _ in logs)


def test_returns_false_if_git_commit_fails(monkeypatch, tmp_path):
    """Returns False if git commit returns non-zero."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(returncode=1, stdout="", stderr="nothing to commit")

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git commit:" in msg for msg, _ in logs)


def test_returns_false_if_git_commit_timeout(monkeypatch, tmp_path):
    """Returns False if git commit times out."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            raise subprocess.TimeoutExpired("git", 15)

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is False
    assert any("git commit failed" in msg for msg, _ in logs)


def test_successful_commit(monkeypatch, tmp_path):
    """Returns True on successful commit."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0,
                stdout="[main abc123] sync: test_proj (manual)\n 1 file changed\n 1 insertion(+)\n",
                stderr="",
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert result is True
    assert any("Private repo synced" in msg for msg, _ in logs)


def test_logs_last_three_commit_lines(monkeypatch, tmp_path):
    """Logs the last 3 lines of git commit output."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append(msg)

    commit_output = "line1\nline2\nline3\n"

    call_count = [0]

    def mock_run(*a, **k):
        call_count[0] += 1
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(returncode=0, stdout=commit_output, stderr="")

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    assert any("line1" in log for log in logs)
    assert any("line2" in log for log in logs)
    assert any("line3" in log for log in logs)


def test_uses_backup_git_env_for_add(monkeypatch, tmp_path):
    """Uses backup git environment for git add."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] sync: test (manual)\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    add_call = captured_calls[1]

    assert "env" in add_call[1]
    assert add_call[1]["env"]["GIT_AUTHOR_NAME"] == "Manager Backup Agent"
    assert add_call[1]["env"]["GIT_AUTHOR_EMAIL"] == "backup@local"
    assert add_call[1]["env"]["GIT_COMMITTER_NAME"] == "Manager Backup Agent"
    assert add_call[1]["env"]["GIT_COMMITTER_EMAIL"] == "backup@local"


def test_uses_backup_git_env_for_commit(monkeypatch, tmp_path):
    """Uses backup git environment for git commit."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] sync: test (manual)\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    commit_call = captured_calls[2]

    assert "env" in commit_call[1]
    assert commit_call[1]["env"]["GIT_AUTHOR_NAME"] == "Manager Backup Agent"
    assert commit_call[1]["env"]["GIT_AUTHOR_EMAIL"] == "backup@local"
    assert commit_call[1]["env"]["GIT_COMMITTER_NAME"] == "Manager Backup Agent"
    assert commit_call[1]["env"]["GIT_COMMITTER_EMAIL"] == "backup@local"


def test_uses_create_no_window_flag(monkeypatch, tmp_path):
    """Uses CREATE_NO_WINDOW flag in all subprocess calls."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] sync: test (manual)\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    for args, kwargs in captured_calls:
        assert kwargs.get("creationflags") == CREATE_NO_WINDOW


def test_commit_message_uses_project_name(monkeypatch, tmp_path):
    """Uses project name (basename of src_path) in commit message."""
    src_path = str(tmp_path / "my_project")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    commit_call = captured_calls[2]
    commit_args = commit_call[0][0]
    commit_msg = commit_args[5]

    assert "my_project" in commit_msg


def test_commit_message_includes_custom_msg(monkeypatch, tmp_path):
    """Includes custom commit message when provided."""
    src_path = str(tmp_path / "proj")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo(
        "git", src_path, dest, [], on_log, commit_msg="this is a test commit"
    )

    commit_call = captured_calls[2]
    commit_args = commit_call[0][0]
    commit_msg_arg = commit_args[5]

    assert "this is a test commit" in commit_msg_arg


def test_commit_message_truncated_at_60_chars(monkeypatch, tmp_path):
    """Truncates custom commit message at 60 characters."""
    src_path = str(tmp_path / "p")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    long_msg = "a" * 100
    result = sync_private_repo(
        "git", src_path, dest, [], on_log, commit_msg=long_msg
    )

    commit_call = captured_calls[2]
    commit_args = commit_call[0][0]
    commit_msg_arg = commit_args[5]

    assert long_msg[:60] in commit_msg_arg
    assert long_msg[:61] not in commit_msg_arg


def test_default_commit_message_when_empty(monkeypatch, tmp_path):
    """Uses default commit message when commit_msg is empty."""
    src_path = str(tmp_path / "proj")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log, commit_msg="")

    commit_call = captured_calls[2]
    commit_args = commit_call[0][0]
    commit_msg_arg = commit_args[5]

    assert "(manual)" in commit_msg_arg
    assert "proj" in commit_msg_arg


def test_uses_git_dash_c_flag(monkeypatch, tmp_path):
    """Uses -C flag to run git in dest directory."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    for args, kwargs in captured_calls:
        assert args[0][1] == "-C"
        assert args[0][2] == dest


def test_uses_status_porcelain_format(monkeypatch, tmp_path):
    """Uses --porcelain format for git status."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    def mock_run(*args, **kwargs):
        captured_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    status_call = captured_calls[0]
    status_args = status_call[0][0]

    assert "--porcelain" in status_args


def test_uses_add_dash_a_flag(monkeypatch, tmp_path):
    """Uses -A flag for git add."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    captured_calls = []

    call_count = [0]

    def mock_run(*args, **kwargs):
        call_count[0] += 1
        captured_calls.append((args, kwargs))
        if call_count[0] == 1:  # git status
            return SimpleNamespace(returncode=0, stdout="M file.txt\n", stderr="")
        elif call_count[0] == 2:  # git add
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        else:  # git commit
            return SimpleNamespace(
                returncode=0, stdout="[main abc123] commit message\n", stderr=""
            )

    monkeypatch.setattr("helpers.private_repo.subprocess.run", mock_run)

    result = sync_private_repo("git", src_path, dest, [], on_log)

    add_call = captured_calls[1]
    add_args = add_call[0][0]

    assert "-A" in add_args


def test_multiple_tracked_files(monkeypatch, tmp_path):
    """Handles multiple tracked files correctly."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    file1 = "file1.txt"
    file2 = "dir/file2.txt"
    os.makedirs(os.path.join(src_path, "dir"))

    with open(os.path.join(src_path, file1), "w") as f:
        f.write("content1")
    with open(os.path.join(src_path, file2), "w") as f:
        f.write("content2")

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [file1, file2], on_log)

    assert os.path.isfile(os.path.join(dest, file1))
    assert os.path.isfile(os.path.join(dest, file2))
    with open(os.path.join(dest, file1)) as f:
        assert f.read() == "content1"
    with open(os.path.join(dest, file2)) as f:
        assert f.read() == "content2"


def test_parent_dir_with_other_files_not_pruned(monkeypatch, tmp_path):
    """Does not prune parent directory if it contains other files."""
    src_path = str(tmp_path / "src")
    dest = str(tmp_path / "dest")
    os.makedirs(src_path)
    os.makedirs(dest)

    test_file = "a/b/file.txt"
    keep_file = "a/b/keep.txt"
    dst_file = os.path.join(dest, test_file)
    dst_keep = os.path.join(dest, keep_file)

    os.makedirs(os.path.dirname(dst_file))
    with open(dst_file, "w") as f:
        f.write("to be deleted")
    with open(dst_keep, "w") as f:
        f.write("keep this")

    logs = []

    def on_log(msg, color):
        logs.append((msg, color))

    monkeypatch.setattr(
        "helpers.private_repo.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = sync_private_repo("git", src_path, dest, [test_file], on_log)

    assert not os.path.isfile(dst_file)
    assert os.path.isfile(dst_keep)
    assert os.path.isdir(os.path.join(dest, "a", "b"))