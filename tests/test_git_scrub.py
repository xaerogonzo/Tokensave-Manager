"""tests/test_git_scrub.py — git_scrub helpers.

The most safety-critical assertions in this file are the ARGV checks on
``run_scrub`` and ``force_push``: filter-repo and force-push are
destructive operations and any silent argv-construction bug could
delete user work irrecoverably. We never actually invoke filter-repo
or git push — every subprocess is mocked at the import site (G-E).
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from helpers import git_scrub
from helpers.git_scrub import (
    build_backup_branch_name,
    create_backup_branch,
    force_push,
    get_remote_url,
    has_filter_repo,
    is_tracked_in_head,
    list_affected_commits,
    run_scrub,
    working_tree_clean,
)


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── build_backup_branch_name ──────────────────────────────────────────────

def test_backup_branch_name_format():
    name = build_backup_branch_name()
    assert name.startswith("backup/before-scrub-")
    # Trailing integer (unix timestamp).
    suffix = name.rsplit("-", 1)[-1]
    assert suffix.isdigit()
    assert int(suffix) > 0


def test_backup_branch_name_respects_prefix():
    name = build_backup_branch_name(prefix="backup/test")
    assert name.startswith("backup/test-")


# ── create_backup_branch — name validation + argv ─────────────────────────

def test_create_backup_branch_rejects_malformed_name(tmp_path):
    """A name with shell metacharacters must be rejected before subprocess."""
    ok, msg = create_backup_branch(str(tmp_path), "git.exe",
                                    "backup; rm -rf /")
    assert ok is False
    assert "refusing branch name" in msg


def test_create_backup_branch_rejects_flag_smuggling(tmp_path):
    """A name starting with `-` could smuggle a CLI flag."""
    ok, msg = create_backup_branch(str(tmp_path), "git.exe", "--force")
    assert ok is False
    assert "refusing branch name" in msg


def test_create_backup_branch_argv(tmp_path, mocker):
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0, stdout="", stderr=""))
    ok, _msg = create_backup_branch(str(tmp_path), "git.exe",
                                     "backup/before-scrub-1234567890")
    assert ok
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "git.exe"
    assert cmd[1:3] == ["-C", str(tmp_path)]
    assert cmd[3] == "branch"
    assert cmd[4] == "backup/before-scrub-1234567890"
    assert cmd[5] == "HEAD"


# ── is_tracked_in_head ────────────────────────────────────────────────────

def test_is_tracked_in_head_true_on_rc_zero(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=0))
    assert is_tracked_in_head(str(tmp_path), "git.exe", "build.ps1") is True


def test_is_tracked_in_head_false_on_rc_nonzero(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=1, stderr="not in index"))
    assert is_tracked_in_head(str(tmp_path), "git.exe", "build.ps1") is False


def test_is_tracked_in_head_argv(tmp_path, mocker):
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0))
    is_tracked_in_head(str(tmp_path), "git.exe", "build.ps1")
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    # Must include --error-unmatch and -- separator (so a "-prefixed"
    # filename can't be interpreted as a flag).
    assert "ls-files" in cmd
    assert "--error-unmatch" in cmd
    assert "--" in cmd
    assert "build.ps1" in cmd


# ── working_tree_clean ────────────────────────────────────────────────────

def test_working_tree_clean_true_on_empty_porcelain(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=0, stdout=""))
    assert working_tree_clean(str(tmp_path), "git.exe") is True


def test_working_tree_clean_false_when_porcelain_nonempty(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=0, stdout=" M src/app.py\n"))
    assert working_tree_clean(str(tmp_path), "git.exe") is False


def test_working_tree_clean_false_on_rc_nonzero(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=128, stderr="not a git repo"))
    assert working_tree_clean(str(tmp_path), "git.exe") is False


# ── list_affected_commits ─────────────────────────────────────────────────

def test_list_affected_commits_parses_tab_separated_output(tmp_path, mocker):
    mocker.patch(
        "helpers.git_scrub.subprocess.run",
        return_value=_proc(rc=0, stdout=(
            "abc1234\tFirst commit\n"
            "def5678\tSecond commit\n"
            "deadbef\tThird commit with: punctuation\n"
        )),
    )
    out = list_affected_commits(str(tmp_path), "git.exe", "build.ps1")
    assert out == [
        ("abc1234", "First commit"),
        ("def5678", "Second commit"),
        ("deadbef", "Third commit with: punctuation"),
    ]


def test_list_affected_commits_empty_on_rc_nonzero(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=128, stderr="bad ref"))
    assert list_affected_commits(str(tmp_path), "git.exe", "x.py") == []


def test_list_affected_commits_respects_max_n(tmp_path, mocker):
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0, stdout=""))
    list_affected_commits(str(tmp_path), "git.exe", "x.py", max_n=42)
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    assert "--max-count=42" in cmd


def test_list_affected_commits_includes_all_refs(tmp_path, mocker):
    """``--all`` is critical — without it, file content in non-current branches
    would be missed and the 'consequences before scrub' display would lie."""
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0, stdout=""))
    list_affected_commits(str(tmp_path), "git.exe", "build.ps1")
    args, _kwargs = mock_run.call_args
    cmd = args[0]
    assert "--all" in cmd


# ── has_filter_repo ───────────────────────────────────────────────────────

def test_has_filter_repo_true_when_git_subcommand_succeeds(tmp_path, mocker):
    mocker.patch("helpers.git_scrub._user_scripts_dir", return_value="")
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=0, stdout="filter-repo 2.38\n"))
    # Make git_exe look like a real file so the isfile() check passes.
    fake_git = tmp_path / "git.exe"
    fake_git.write_bytes(b"")
    assert has_filter_repo(str(fake_git)) is True


def test_has_filter_repo_falls_back_to_script_probe(tmp_path, mocker):
    """When the git subcommand probe fails, the fallback file check
    is decisive — covers fresh pip --user installs where PATH is stale."""
    mocker.patch("helpers.git_scrub.subprocess.run",
                 side_effect=FileNotFoundError("git not found"))
    mocker.patch("helpers.git_scrub._find_filter_repo_script",
                 return_value="/fake/path/git-filter-repo")
    fake_git = tmp_path / "git.exe"
    fake_git.write_bytes(b"")
    assert has_filter_repo(str(fake_git)) is True


def test_has_filter_repo_false_when_neither_path_works(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=1, stderr="filter-repo is not a git command"))
    mocker.patch("helpers.git_scrub._find_filter_repo_script", return_value="")
    fake_git = tmp_path / "git.exe"
    fake_git.write_bytes(b"")
    assert has_filter_repo(str(fake_git)) is False


# ── get_remote_url ────────────────────────────────────────────────────────

def test_get_remote_url_returns_stripped_url(tmp_path, mocker):
    mocker.patch(
        "helpers.git_scrub.subprocess.run",
        return_value=_proc(rc=0, stdout="git@github.com:user/repo.git\n"),
    )
    url = get_remote_url(str(tmp_path), "git.exe")
    assert url == "git@github.com:user/repo.git"


def test_get_remote_url_empty_on_missing_remote(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=128, stderr="no such remote"))
    assert get_remote_url(str(tmp_path), "git.exe") == ""


def test_get_remote_url_argv_specifies_remote_name(tmp_path, mocker):
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0, stdout=""))
    get_remote_url(str(tmp_path), "git.exe", remote="upstream")
    cmd = mock_run.call_args[0][0]
    assert "remote" in cmd
    assert "get-url" in cmd
    assert "upstream" in cmd


# ── run_scrub (the safety-critical argv check) ────────────────────────────

def test_run_scrub_constructs_canonical_argv(tmp_path, mocker):
    """The scrub argv must be EXACTLY:
        git -C <repo> filter-repo --invert-paths --path <file> --force

    Any deviation could delete the wrong files or accidentally rewrite
    the whole repo. This is the SINGLE most safety-critical test in the
    suite — locks down the destructive command's shape forever.
    """
    # Make subprocess.Popen return a fake process that emits no output
    # and exits 0. The Popen mock needs an iterable stdout.
    fake_proc = SimpleNamespace(
        stdout=iter([]),
        wait=lambda timeout=None: 0,
        returncode=0,
    )
    mock_popen = mocker.patch("helpers.git_scrub.subprocess.Popen",
                              return_value=fake_proc)
    ok, _log = run_scrub(str(tmp_path), "git.exe", "secrets.json")
    assert ok is True

    args, _kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == "git.exe"
    assert cmd[1] == "-C"
    assert cmd[2] == str(tmp_path)
    assert cmd[3] == "filter-repo"
    assert cmd[4] == "--invert-paths"
    assert cmd[5] == "--path"
    assert cmd[6] == "secrets.json"
    assert cmd[7] == "--force"


def test_run_scrub_returns_false_on_nonzero_rc(tmp_path, mocker):
    fake_proc = SimpleNamespace(
        stdout=iter(["error: refusing\n"]),
        wait=lambda timeout=None: 1,
        returncode=1,
    )
    mocker.patch("helpers.git_scrub.subprocess.Popen", return_value=fake_proc)
    # Also stub the fallback paths so they don't try to re-invoke.
    mocker.patch("helpers.git_scrub._find_filter_repo_script", return_value="")
    import importlib.util as _ilu
    mocker.patch.object(_ilu, "find_spec", return_value=None)
    ok, log = run_scrub(str(tmp_path), "git.exe", "x.py")
    assert ok is False
    assert "error" in log.lower() or "refusing" in log.lower()


# ── force_push (also safety-critical) ─────────────────────────────────────

def test_force_push_argv(tmp_path, mocker):
    """Force-push argv must be exact: ``git push --force origin <branch>``."""
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run",
                            return_value=_proc(rc=0, stdout=""))
    ok, _log = force_push(str(tmp_path), "git.exe", "master")
    assert ok is True
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "git.exe"
    assert cmd[1] == "-C"
    assert cmd[2] == str(tmp_path)
    assert cmd[3] == "push"
    assert cmd[4] == "--force"
    assert cmd[5] == "origin"
    assert cmd[6] == "master"


def test_force_push_returns_failure_on_rc_nonzero(tmp_path, mocker):
    mocker.patch("helpers.git_scrub.subprocess.run",
                 return_value=_proc(rc=128, stderr="rejected"))
    ok, log = force_push(str(tmp_path), "git.exe", "master")
    assert ok is False
    assert "rejected" in log.lower()


# ── preflight (combined snapshot) ─────────────────────────────────────────

def test_preflight_returns_all_fields(tmp_path, mocker):
    git_exe = tmp_path / "git.exe"
    git_exe.write_bytes(b"")

    def fake_run(cmd, *args, **kwargs):
        # rev-parse --is-inside-work-tree
        if "--is-inside-work-tree" in cmd:
            return _proc(rc=0, stdout="true\n")
        # rev-parse --abbrev-ref HEAD
        if "--abbrev-ref" in cmd:
            return _proc(rc=0, stdout="Roadmap-7\n")
        # status --porcelain
        if "status" in cmd and "--porcelain" in cmd:
            return _proc(rc=0, stdout="")
        # filter-repo --version
        if "filter-repo" in cmd and "--version" in cmd:
            return _proc(rc=0, stdout="filter-repo 2.38\n")
        # remote get-url
        if "remote" in cmd and "get-url" in cmd:
            return _proc(rc=0, stdout="https://github.com/foo/bar.git\n")
        return _proc(rc=128)

    mocker.patch("helpers.git_scrub.subprocess.run", side_effect=fake_run)

    info = git_scrub.preflight(str(tmp_path), str(git_exe))
    assert info["git_exe_present"] is True
    assert info["filter_repo"] is True
    assert info["is_git_repo"] is True
    assert info["head_branch"] == "Roadmap-7"
    assert info["working_tree_clean"] is True
    assert info["remote_url"] == "https://github.com/foo/bar.git"


def test_preflight_short_circuits_on_missing_git_exe(tmp_path, mocker):
    """If git.exe doesn't exist, preflight must not invoke subprocess."""
    mock_run = mocker.patch("helpers.git_scrub.subprocess.run")
    info = git_scrub.preflight(str(tmp_path), "/nonexistent/git")
    assert info["git_exe_present"] is False
    assert info["is_git_repo"] is False
    mock_run.assert_not_called()
