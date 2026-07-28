"""Tests for helpers/claude_cli.py.

Covers:
  spawn_claude_cli       — empty-exe guard, newline stripping, argv construction,
                           model flag, Popen OSError
  call_claude_cli_print  — empty-exe guard, success, empty-stdout→None,
                           TimeoutExpired, OSError, non-zero exit, model flag,
                           system-prompt flag, cwd pass-through

Monkeypatching
--------------
`subprocess` is imported at module scope in claude_cli.py, so all patches
target `"helpers.claude_cli.subprocess.*"`. This scopes the patch to the
module under test and does not touch the global subprocess registry, making
the tests safe to run in parallel (if pytest-xdist is ever added).

`sys.platform` is patched to "linux" for spawn_claude_cli tests so the
non-Windows argv branch runs — the Windows cmd-string branch opens a real
console and is not testable in CI.

stdout as str (not bytes)
-------------------------
call_claude_cli_print passes text=True and encoding="utf-8" to subprocess.run,
so proc.stdout is always a str. The _make_proc helper returns str accordingly.
"""
import subprocess
from unittest.mock import MagicMock

from helpers.claude_cli import (
    call_claude_cli_print,
    get_last_cli_error,
    spawn_claude_cli,
    spawn_claude_cli_interactive,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_proc(stdout="", returncode=0, stderr=""):
    """Minimal subprocess.CompletedProcess stub.

    stdout/stderr are str because call_claude_cli_print uses text=True.
    """
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    m.stderr = stderr
    return m


# ── spawn_claude_cli ──────────────────────────────────────────────────────────

def test_spawn_empty_exe_returns_false():
    ok, err = spawn_claude_cli("", "/some/path", "do something")
    assert ok is False
    assert "not configured" in err


def test_spawn_strips_newlines_from_instruction(monkeypatch):
    """Stray \\n inside cmd.exe /k fires Enter prematurely — must be stripped."""
    captured = []
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured.append(argv) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    spawn_claude_cli("/usr/bin/claude", "/path", "line1\nline2\r\nline3")
    assert captured, "Popen was not called"
    instruction = captured[0][-1]   # last element of argv on the Linux path
    assert "\n" not in instruction
    assert "\r" not in instruction


def test_spawn_linux_path_builds_argv(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured.append(argv) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    ok, err = spawn_claude_cli("/usr/bin/claude", "/path", "run tests")
    assert ok is True
    assert err == ""
    assert captured[0] == ["/usr/bin/claude", "run tests"]


def test_spawn_linux_path_includes_model_flag(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured.append(argv) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    spawn_claude_cli("/usr/bin/claude", "/path", "do x", model="claude-haiku")
    assert captured[0] == ["/usr/bin/claude", "--model", "claude-haiku", "do x"]


def test_spawn_popen_oserror_returns_false(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("No such file")),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    ok, err = spawn_claude_cli("/bad/path/claude", "/path", "do x")
    assert ok is False
    assert "No such file" in err


# ── spawn_claude_cli_interactive ──────────────────────────────────────────────

def test_spawn_interactive_empty_exe_returns_false():
    ok, err = spawn_claude_cli_interactive("", "/some/path")
    assert ok is False
    assert "not configured" in err


def test_spawn_interactive_argv_has_no_instruction(monkeypatch):
    """The whole point of the variant: no trailing instruction argument —
    an empty "" arg would make claude run a blank one-shot prompt instead
    of entering interactive mode."""
    captured = []
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured.append(argv) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    ok, err = spawn_claude_cli_interactive("/usr/bin/claude", "/path")
    assert ok is True
    assert err == ""
    assert captured[0] == ["/usr/bin/claude"]


def test_spawn_interactive_includes_model_flag(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured.append(argv) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    spawn_claude_cli_interactive("/usr/bin/claude", "/path",
                                 model="claude-haiku")
    assert captured[0] == ["/usr/bin/claude", "--model", "claude-haiku"]


def test_spawn_interactive_uses_project_path_as_cwd(monkeypatch):
    captured_kwargs = {}
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda argv, **kw: captured_kwargs.update(kw) or MagicMock(),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    spawn_claude_cli_interactive("/usr/bin/claude", "/my/project")
    assert captured_kwargs.get("cwd") == "/my/project"


def test_spawn_interactive_popen_oserror_returns_false(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.Popen",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("No such file")),
    )
    monkeypatch.setattr("helpers.claude_cli.sys.platform", "linux")
    ok, err = spawn_claude_cli_interactive("/bad/path/claude", "/path")
    assert ok is False
    assert "No such file" in err


# ── call_claude_cli_print ─────────────────────────────────────────────────────

def test_print_empty_exe_returns_none():
    assert call_claude_cli_print("", "some prompt") is None


def test_print_success_returns_stripped_stdout(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: _make_proc(stdout="  hello world\n"),
    )
    assert call_claude_cli_print("/usr/bin/claude", "prompt") == "hello world"


def test_print_empty_stdout_returns_none(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: _make_proc(stdout="   "),
    )
    assert call_claude_cli_print("/usr/bin/claude", "prompt") is None


def test_print_timeout_returns_none(monkeypatch):
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=5)
    monkeypatch.setattr("helpers.claude_cli.subprocess.run", raise_timeout)
    assert call_claude_cli_print("/usr/bin/claude", "prompt", timeout=5) is None


def test_print_oserror_returns_none(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("not found")),
    )
    assert call_claude_cli_print("/usr/bin/claude", "prompt") is None


def test_print_nonzero_exit_returns_none(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: _make_proc(stdout="out", returncode=1, stderr="auth error"),
    )
    assert call_claude_cli_print("/usr/bin/claude", "prompt") is None


def test_print_model_flag_included(monkeypatch):
    captured = []
    def fake_run(cmd, **kw):
        captured.append(cmd)
        return _make_proc(stdout="ok")
    monkeypatch.setattr("helpers.claude_cli.subprocess.run", fake_run)
    call_claude_cli_print("/usr/bin/claude", "prompt", model="claude-haiku")
    assert "--model" in captured[0]
    assert "claude-haiku" in captured[0]


def test_print_system_prompt_flag_included(monkeypatch):
    captured = []
    def fake_run(cmd, **kw):
        captured.append(cmd)
        return _make_proc(stdout="ok")
    monkeypatch.setattr("helpers.claude_cli.subprocess.run", fake_run)
    call_claude_cli_print("/usr/bin/claude", "prompt", system_prompt="be concise")
    assert "--append-system-prompt" in captured[0]


def test_print_cwd_passed_to_subprocess(monkeypatch):
    captured_kw = {}
    def fake_run(cmd, **kw):
        captured_kw.update(kw)
        return _make_proc(stdout="ok")
    monkeypatch.setattr("helpers.claude_cli.subprocess.run", fake_run)
    call_claude_cli_print("/usr/bin/claude", "prompt", cwd="/my/project")
    assert captured_kw.get("cwd") == "/my/project"


# ── get_last_cli_error: the SPECIFIC failure cause (per-thread) ───────────────

def test_cli_error_success_is_none(monkeypatch):
    monkeypatch.setattr("helpers.claude_cli.subprocess.run",
                        lambda *a, **kw: _make_proc(stdout="ok"))
    assert call_claude_cli_print("/usr/bin/claude", "p") == "ok"
    assert get_last_cli_error() is None


def test_cli_error_empty_exe(monkeypatch):
    assert call_claude_cli_print("", "p") is None
    assert "no Claude CLI path" in (get_last_cli_error() or "")


def test_cli_error_timeout(monkeypatch):
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=7)
    monkeypatch.setattr("helpers.claude_cli.subprocess.run", raise_timeout)
    assert call_claude_cli_print("/usr/bin/claude", "p", timeout=7) is None
    err = get_last_cli_error() or ""
    assert "timed out" in err and "7" in err


def test_cli_error_oserror_mentions_not_runnable(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("No such file")),
    )
    assert call_claude_cli_print("/usr/bin/claude", "p") is None
    err = get_last_cli_error() or ""
    assert "not runnable" in err or "not found" in err
    assert "No such file" in err


def test_cli_error_nonzero_exit_includes_stderr(monkeypatch):
    monkeypatch.setattr(
        "helpers.claude_cli.subprocess.run",
        lambda *a, **kw: _make_proc(stdout="", returncode=1, stderr="not logged in"),
    )
    assert call_claude_cli_print("/usr/bin/claude", "p") is None
    err = get_last_cli_error() or ""
    assert "exited 1" in err and "not logged in" in err
