"""tests/test_runner_io.py — the git hooks' GUI fallback.

A hook that blocks an operation has to explain why. GUI git clients swallow
stderr, so the explanation never reaches someone using GitHub Desktop or the
VS Code git panel — they get "hook rejected" and nothing else.

The rules being pinned:

  * the box appears only when stderr is NOT a terminal, since on a terminal
    the same text is already on screen;
  * every failure path in raising it is swallowed. A hook must never fail
    because it could not draw a window — the exit code and the stderr output
    are the real contract, and the dialog is a courtesy.
"""
from __future__ import annotations

import io
import sys

import pytest

from helpers import runner_io
from helpers.runner_io import (
    maybe_show_hook_dialog,
    show_hook_dialog,
    stderr_is_tty,
)


class _FakeStderr(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


# ── stderr_is_tty ─────────────────────────────────────────────────────────────

def test_reports_a_real_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stderr", _FakeStderr(tty=True))
    assert stderr_is_tty() is True


def test_reports_a_pipe(monkeypatch):
    monkeypatch.setattr(sys, "stderr", _FakeStderr(tty=False))
    assert stderr_is_tty() is False


@pytest.mark.parametrize("exc", [AttributeError, OSError, ValueError])
def test_an_exotic_stderr_counts_as_not_a_terminal(monkeypatch, exc):
    """Erring this way shows a box to someone who may not need one; erring
    the other way hides why their commit was refused."""
    class Broken:
        def isatty(self):
            raise exc("boom")

    monkeypatch.setattr(sys, "stderr", Broken())
    assert stderr_is_tty() is False


def test_stderr_without_isatty_at_all(monkeypatch):
    monkeypatch.setattr(sys, "stderr", object())
    assert stderr_is_tty() is False


# ── the gate ──────────────────────────────────────────────────────────────────

def test_no_dialog_on_a_terminal(monkeypatch):
    monkeypatch.setattr(runner_io, "stderr_is_tty", lambda: True)
    monkeypatch.setattr(runner_io, "show_hook_dialog",
                        lambda *a: pytest.fail("must not raise a box on a TTY"))
    assert maybe_show_hook_dialog("t", "m") is False


def test_dialog_when_stderr_is_swallowed(monkeypatch):
    shown = []
    monkeypatch.setattr(runner_io, "stderr_is_tty", lambda: False)
    monkeypatch.setattr(runner_io, "show_hook_dialog",
                        lambda t, m: (shown.append((t, m)), True)[1])
    assert maybe_show_hook_dialog("Commit paused", "why") is True
    assert shown == [("Commit paused", "why")]


# ── failing open ──────────────────────────────────────────────────────────────

def test_missing_tk_is_not_an_error(monkeypatch):
    """Headless Linux, WSL without a display, a broken Tk install."""
    import builtins
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.split(".")[0] in ("tkinter", "_tkinter"):
            raise ImportError("no display")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert show_hook_dialog("t", "m") is False


def test_a_tk_that_fails_to_start_is_not_an_error(monkeypatch):
    import tkinter as tk

    def boom():
        raise tk.TclError("no display name and no $DISPLAY")

    monkeypatch.setattr(tk, "Tk", boom)
    assert show_hook_dialog("t", "m") is False


def test_a_dialog_that_raises_is_not_an_error(monkeypatch):
    """The box is a courtesy; the exit code is the contract."""
    import tkinter as tk
    from tkinter import messagebox

    class FakeRoot:
        def withdraw(self):
            pass

        def destroy(self):
            pass

    monkeypatch.setattr(tk, "Tk", FakeRoot)
    monkeypatch.setattr(messagebox, "showwarning",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert show_hook_dialog("t", "m") is False


def test_the_root_window_is_destroyed_even_when_the_box_raises(monkeypatch):
    """A leaked hidden Tk root would keep the hook process alive."""
    import tkinter as tk
    from tkinter import messagebox
    destroyed = []

    class FakeRoot:
        def withdraw(self):
            pass

        def destroy(self):
            destroyed.append(True)

    monkeypatch.setattr(tk, "Tk", FakeRoot)
    monkeypatch.setattr(messagebox, "showwarning",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    show_hook_dialog("t", "m")
    assert destroyed == [True]
