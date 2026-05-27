"""tests/test_prepush_hook.py — install/remove/detect git pre-push hook.

Verifies the pre-push hook helpers in ``src/helpers/prepush_hook.py``:
marker-based detection, install/remove with refusal-to-overwrite for
user-installed hooks, pythonw→python swap on Windows, and stdin-drain
in the rendered shell script.

All filesystem operations happen under ``tmp_path``. No actual git is
invoked.
"""
from __future__ import annotations

import os
import sys

import pytest

from helpers.prepush_hook import (
    _HOOK_MARKER,
    _prefer_console_python,
    _render_hook_script,
    hook_path,
    install_pre_push_hook,
    is_pre_push_hook_installed,
    prepush_runner_script_path,
    remove_pre_push_hook,
)


# ── Test repo fixture ─────────────────────────────────────────────────────

@pytest.fixture
def fake_repo(tmp_path):
    """A tmp_path with a .git/hooks/ subdirectory — minimum for hook ops."""
    git_dir = tmp_path / ".git"
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def non_repo(tmp_path):
    """A tmp_path WITHOUT a .git/ directory."""
    return tmp_path


# ── hook_path ─────────────────────────────────────────────────────────────

def test_hook_path_constructs_correct_relative_location(tmp_path):
    p = hook_path(str(tmp_path))
    assert p == os.path.join(str(tmp_path), ".git", "hooks", "pre-push")


# ── is_pre_push_hook_installed ────────────────────────────────────────────

def test_detection_false_when_no_file(fake_repo):
    assert is_pre_push_hook_installed(str(fake_repo)) is False


def test_detection_false_when_no_marker(fake_repo):
    p = hook_path(str(fake_repo))
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho hello\n")
    assert is_pre_push_hook_installed(str(fake_repo)) is False


def test_detection_true_when_marker_present(fake_repo):
    p = hook_path(str(fake_repo))
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\n{_HOOK_MARKER}\n")
    assert is_pre_push_hook_installed(str(fake_repo)) is True


def test_detection_false_on_unreadable_file(fake_repo, mocker):
    """If the hook file exists but can't be read, detection returns False
    (fail-safe — better to wrongly re-install than wrongly leave alone)."""
    p = hook_path(str(fake_repo))
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"{_HOOK_MARKER}\n")
    mocker.patch("builtins.open", side_effect=OSError("permission denied"))
    assert is_pre_push_hook_installed(str(fake_repo)) is False


# ── install_pre_push_hook ─────────────────────────────────────────────────

def test_install_writes_marker_and_paths(fake_repo):
    ok, msg = install_pre_push_hook(
        str(fake_repo),
        python_exe="/path/to/python.exe",
        runner_script="/path/to/prepush_runner.py",
    )
    assert ok, f"install failed: {msg}"
    p = hook_path(str(fake_repo))
    assert os.path.isfile(p)
    content = open(p, encoding="utf-8").read()
    assert _HOOK_MARKER in content
    assert "python.exe" in content
    assert "prepush_runner.py" in content


def test_install_fails_when_no_git_directory(non_repo):
    ok, msg = install_pre_push_hook(
        str(non_repo),
        python_exe=sys.executable,
        runner_script="runner.py",
    )
    assert ok is False
    assert "not a git repo" in msg.lower()


def test_install_refuses_to_overwrite_user_hook(fake_repo):
    """If pre-push exists WITHOUT our marker, refuse — it's user-installed."""
    p = hook_path(str(fake_repo))
    user_content = "#!/bin/sh\n# User's custom hook\necho 'custom'\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(user_content)

    ok, msg = install_pre_push_hook(
        str(fake_repo),
        python_exe=sys.executable,
        runner_script="runner.py",
    )
    assert ok is False
    assert "already exists" in msg.lower()
    # User's hook untouched.
    assert open(p, encoding="utf-8").read() == user_content


def test_install_overwrites_existing_marker_hook(fake_repo):
    """Re-installing OUR hook (one with the marker) is fine — handles
    re-install / version upgrade."""
    p = hook_path(str(fake_repo))
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"{_HOOK_MARKER}\n# old version\n")

    ok, _msg = install_pre_push_hook(
        str(fake_repo),
        python_exe="/new/python.exe",
        runner_script="/new/runner.py",
    )
    assert ok
    new_content = open(p, encoding="utf-8").read()
    assert "/new/python.exe" in new_content or "new/python.exe" in new_content
    assert "old version" not in new_content


# ── remove_pre_push_hook ──────────────────────────────────────────────────

def test_remove_succeeds_on_marker_hook(fake_repo):
    p = hook_path(str(fake_repo))
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"{_HOOK_MARKER}\necho hello\n")
    ok, _msg = remove_pre_push_hook(str(fake_repo))
    assert ok
    assert not os.path.isfile(p)


def test_remove_fails_on_absent_hook(fake_repo):
    ok, msg = remove_pre_push_hook(str(fake_repo))
    assert ok is False
    assert "no pre-push" in msg.lower()


def test_remove_refuses_user_installed_hook(fake_repo):
    p = hook_path(str(fake_repo))
    user_content = "#!/bin/sh\n# user's custom hook\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(user_content)

    ok, msg = remove_pre_push_hook(str(fake_repo))
    assert ok is False
    assert "refusing to delete" in msg.lower() or "not written by" in msg.lower()
    assert os.path.isfile(p), "user's hook must NOT be deleted"
    assert open(p, encoding="utf-8").read() == user_content


# ── _prefer_console_python ────────────────────────────────────────────────

def test_prefer_console_python_swaps_pythonw_when_python_exists(tmp_path):
    """``pythonw.exe`` is silent — for hooks we want the console version
    so stderr surfaces. If python.exe lives next to pythonw.exe, prefer it.
    """
    pythonw = tmp_path / "pythonw.exe"
    python  = tmp_path / "python.exe"
    pythonw.write_bytes(b"")
    python.write_bytes(b"")

    result = _prefer_console_python(str(pythonw))
    assert os.path.basename(result).lower() == "python.exe"


def test_prefer_console_python_passes_through_python_exe(tmp_path):
    """Already python.exe — return unchanged."""
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    assert _prefer_console_python(str(python)) == str(python)


def test_prefer_console_python_passes_through_when_sibling_missing(tmp_path):
    """No python.exe next to pythonw.exe — keep pythonw.exe as a fallback."""
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"")
    assert _prefer_console_python(str(pythonw)) == str(pythonw)


# ── _render_hook_script ───────────────────────────────────────────────────

def test_render_includes_stdin_drain():
    """Pre-push hooks receive remote info via stdin from git. The script
    MUST drain it (``cat > /dev/null``) or git hangs waiting for the pipe
    to close.
    """
    script = _render_hook_script("python.exe", "runner.py", "/proj")
    assert "cat > /dev/null" in script


def test_render_includes_marker():
    script = _render_hook_script("python.exe", "runner.py", "/proj")
    assert _HOOK_MARKER in script


def test_render_passes_project_path_as_argv():
    """The runner needs the project path as argv[1] (hardcoded at install
    time — same pattern as the pre-commit hook's reviewer_script)."""
    script = _render_hook_script("python.exe", "runner.py", "/some/project")
    # exec "$PY" "$RUN" "$PRJ" — project path is the third argument
    assert "/some/project" in script
    assert 'exec "$PY" "$RUN" "$PRJ"' in script


def test_render_forward_slashes_paths():
    """The script targets MSYS sh (which Git for Windows bundles).
    Backslashes are an escaping minefield; we use forward slashes throughout.
    """
    script = _render_hook_script(
        "C:\\Python\\python.exe",
        "C:\\manager\\src\\prepush_runner.py",
        "C:\\proj",
    )
    # No raw backslashes in the path declarations.
    assert "PY='C:/Python/python.exe'" in script
    assert "RUN='C:/manager/src/prepush_runner.py'" in script
    assert "PRJ='C:/proj'" in script


def test_render_fail_open_paths():
    """If python or the runner script go missing, the hook should
    fail-open (exit 0) rather than block all future pushes."""
    script = _render_hook_script("python.exe", "runner.py", "/proj")
    assert "skipping checks" in script
    # Both fallback branches must `exit 0`.
    assert "exit 0" in script


# ── prepush_runner_script_path ────────────────────────────────────────────

def test_prepush_runner_script_path_resolves_relative_to_helpers():
    """Path must be ``src/prepush_runner.py`` relative to the manager
    install. Resolving relative to this module's location lets the
    manager move without breaking installed hooks (re-install picks up
    the new path)."""
    p = prepush_runner_script_path()
    assert os.path.basename(p) == "prepush_runner.py"
    # parent dir should be `src/`
    assert os.path.basename(os.path.dirname(p)) == "src"


# ── G-K — pre-push runner messagebox fallback ─────────────────────────────

def test_stderr_is_tty_returns_false_for_non_tty(mocker):
    """When sys.stderr.isatty() returns False, helper returns False."""
    import prepush_runner
    mock_stderr = mocker.MagicMock()
    mock_stderr.isatty.return_value = False
    mocker.patch.object(prepush_runner.sys, "stderr", mock_stderr)
    assert prepush_runner._stderr_is_tty() is False


def test_stderr_is_tty_handles_exception(mocker):
    """Some stderr replacements lack isatty (e.g. io.StringIO under capture).
    Helper must treat the exception as non-TTY rather than raising."""
    import prepush_runner

    class BogusStderr:
        def isatty(self):
            raise AttributeError("not a real stream")

    mocker.patch.object(prepush_runner.sys, "stderr", BogusStderr())
    assert prepush_runner._stderr_is_tty() is False


def test_runner_messagebox_invoked_when_stderr_not_tty(mocker, tmp_path):
    """G-K: failures + non-TTY stderr → messagebox.showerror is called
    AND main() returns 1.
    """
    import prepush_runner

    mocker.patch.object(prepush_runner, "_stderr_is_tty", return_value=False)
    # Stub everything else main() touches.
    project = tmp_path / "proj"; project.mkdir()
    mocker.patch.object(prepush_runner.sys, "argv",
                         ["prepush_runner.py", str(project)])
    cfg = type("C", (), {"raw": {}})()
    mocker.patch.object(prepush_runner, "_load_config",
                         return_value=(cfg, ""))
    # Force a failing check.
    mocker.patch.object(prepush_runner, "_run_checks",
                         return_value=[("syntax", False, "SyntaxError")])

    # Stub the actual messagebox so we don't pop a real window.
    import tkinter as _tk
    import tkinter.messagebox as _mb
    mock_root = mocker.MagicMock()
    mocker.patch.object(_tk, "Tk", return_value=mock_root)
    mock_show = mocker.patch.object(_mb, "showerror")

    rc = prepush_runner.main()
    assert rc == 1
    mock_show.assert_called_once()
    # The summary must mention which check failed.
    args, _kwargs = mock_show.call_args
    body = args[1] if len(args) >= 2 else _kwargs.get("message", "")
    assert "syntax" in body
    assert "SyntaxError" in body


def test_runner_messagebox_failure_does_not_block_exit_1(mocker, tmp_path):
    """If the messagebox attempt itself raises (e.g. no DISPLAY on WSL),
    main() must STILL return 1 rather than masking the exit code."""
    import prepush_runner

    mocker.patch.object(prepush_runner, "_stderr_is_tty", return_value=False)
    project = tmp_path / "proj"; project.mkdir()
    mocker.patch.object(prepush_runner.sys, "argv",
                         ["prepush_runner.py", str(project)])
    cfg = type("C", (), {"raw": {}})()
    mocker.patch.object(prepush_runner, "_load_config",
                         return_value=(cfg, ""))
    mocker.patch.object(prepush_runner, "_run_checks",
                         return_value=[("syntax", False, "boom")])

    # Make Tk import fail (simulates no DISPLAY on WSL/SSH).
    import tkinter as _tk
    mocker.patch.object(_tk, "Tk",
                         side_effect=_tk.TclError("no display name"))

    rc = prepush_runner.main()
    assert rc == 1   # exit code must NOT be masked by the GUI fallback


def test_runner_skips_messagebox_when_stderr_is_tty(mocker, tmp_path):
    """G-K: stderr IS a TTY (real terminal) → user sees stderr directly,
    no need for the messagebox fallback. Tk must NOT be imported."""
    import prepush_runner

    mocker.patch.object(prepush_runner, "_stderr_is_tty", return_value=True)
    project = tmp_path / "proj"; project.mkdir()
    mocker.patch.object(prepush_runner.sys, "argv",
                         ["prepush_runner.py", str(project)])
    cfg = type("C", (), {"raw": {}})()
    mocker.patch.object(prepush_runner, "_load_config",
                         return_value=(cfg, ""))
    mocker.patch.object(prepush_runner, "_run_checks",
                         return_value=[("syntax", False, "err")])

    import tkinter as _tk
    mock_tk = mocker.patch.object(_tk, "Tk")

    rc = prepush_runner.main()
    assert rc == 1
    mock_tk.assert_not_called()
