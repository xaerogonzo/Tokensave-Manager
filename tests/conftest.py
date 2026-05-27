"""tests/conftest.py — shared fixtures for the Token Save Manager pytest suite.

This file centralises the patterns Gemini-audited in v4.12. Every fixture
here has a lettered "G-" reference back to the cascade plan
(`~/.claude/plans/write-a-comprehensive-plan-elegant-cascade.md`)
documenting WHY the fixture exists in this shape.

Fixture summary:

* ``tk_root``        — withdrawn Tk root for headless dialog tests. Neutralises
                       ``grab_set`` (TclError on non-viewable windows) and
                       asserts no daemon threads leaked across the test
                       boundary (G-D).
* ``wait_for``       — polling helper that drives ``root.update()`` each
                       iteration so worker-scheduled ``after(0, ...)``
                       callbacks actually fire (G-G + G-M). The canonical
                       replacement for the rejected ``sync_threads`` pattern
                       — preserves real thread boundaries so missing
                       ``after``-wraps surface as ``TclError`` (which would
                       be silently passed under ``sync_threads``).
* ``patch_after``    — captures ``self.after(ms, cb, *args)`` into a queue
                       and exposes ``advance()``/``drain()`` for explicit
                       time control (G-A + G-H). Use this when you need
                       to step a recursive ``after()`` loop one tick at
                       a time (e.g. ``_draft_tick``'s 1-second poll).
* ``fake_home``      — redirects ``$HOME`` / ``$USERPROFILE`` / ``$APPDATA``
                       / ``$LOCALAPPDATA`` to a per-test tmp_path and
                       reloads modules that may have cached paths at
                       import (G-F + G-J Part 2).
* ``mock_config``    — minimal ``ManagerConfig`` stub for dialog tests,
                       with a ``_saved`` flag so tests can assert
                       ``cfg.save()`` was called (G-F symmetry check).

For dialog tests, the typical fixture combo is::

    def test_something(tk_root, mock_config, wait_for, fake_home, mocker):
        ...

For pure-logic tests, no fixtures from this file are needed — pytest
discovers the existing ``unittest.TestCase`` classes in
``tests/smoke_test.py`` and the new ``test_*.py`` modules automatically.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Add src/ to sys.path early so `from helpers.X import Y` and
# `from dialogs.X import Y` work in tests. pyproject.toml also sets
# `pythonpath = ["src"]` but conftest-time injection covers tests run
# via plain `python -m unittest` too.
_REPO_ROOT = Path(__file__).parent.parent
_SRC_DIR   = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ── Tk root (G-D) ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _tk_root_session():
    """ONE persistent Tk root for the whole test session.

    Rapidly creating + destroying ``tk.Tk()`` instances in sequence
    causes intermittent ``TclError: Can't find a usable init.tcl``
    failures on Windows (Tcl's interpreter state doesn't fully reset
    between roots). Sharing a single root across tests sidesteps this.

    The root stays withdrawn (never displayed). Per-test isolation is
    provided by the function-scoped ``tk_root`` wrapper below, which
    destroys any child Toplevels left over from a previous test.
    """
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"Tk not available in this environment: {e}")
    root.withdraw()

    # Patch Toplevel.grab_set globally for the whole session — every
    # dialog calls it and it raises on non-viewable windows.
    original_grab_set = tk.Toplevel.grab_set
    tk.Toplevel.grab_set = lambda self: None      # type: ignore[assignment]

    yield root

    tk.Toplevel.grab_set = original_grab_set      # type: ignore[assignment]
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def tk_root(_tk_root_session):
    """Yield the session Tk root + clean up child Toplevels on teardown.

    Three responsibilities:

    1. Hand back the shared withdrawn root (session-scoped above).
    2. After the test, destroy any Toplevel children it created so
       the next test sees a clean root.
    3. Assert (G-D) that no daemon worker threads have leaked across
       the test boundary — if a worker is still alive after teardown,
       it will eventually try to call ``self.after(0, ...)`` on the
       destroyed dialog and crash an UNRELATED later test with a
       confusing TclError. Catching the leak HERE produces an
       actionable error pointing at the actual offender.
    """
    import tkinter as tk

    baseline_threads = threading.active_count()

    try:
        yield _tk_root_session
    finally:
        # Destroy any Toplevel children the test created.
        for child in list(_tk_root_session.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        # Flush pending events from destroyed widgets so they don't
        # surface as TclErrors during the NEXT test.
        try:
            _tk_root_session.update_idletasks()
        except tk.TclError:
            pass

        # G-D: catch ghost threads BEFORE they crash a later test.
        leaked = threading.active_count() - baseline_threads
        if leaked > 0:
            time.sleep(0.1)
            leaked = threading.active_count() - baseline_threads
        assert leaked <= 0, (
            f"{leaked} worker thread(s) leaked from this test. "
            "Use wait_for(...) to block until workers complete, or "
            "ensure the test mocks the slow I/O so workers finish quickly."
        )


# ── wait_for (G-G + G-M) ──────────────────────────────────────────────────

@pytest.fixture
def wait_for(tk_root):
    """Polling helper that DRIVES the Tk event loop.

    Workers in our dialogs schedule UI updates via ``self.after(0, ...)``.
    Without ``root.update()`` calls, those scheduled callbacks NEVER FIRE
    in tests — the predicate stays False until timeout even when the
    worker has actually completed. G-M (audit refinement of G-G) embeds
    ``root.update()`` into each polling iteration to drain the
    ``after(0, ...)`` queue.

    G-G: this fixture deliberately preserves REAL THREADS. If a worker
    forgets to wrap a UI mutation in ``self.after(0, ...)``, production
    will crash with ``TclError: main thread is not in main loop`` — and
    THIS fixture exposes that crash in the test thread (via timeout),
    rather than masking it like the rejected ``sync_threads`` pattern
    would have.

    Example::

        wait_for(lambda: dialog._is_row_busy("codegraph"), timeout_s=1.0)
    """
    import tkinter as tk

    def _wait(predicate, timeout_s: float = 2.0, interval_s: float = 0.01):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                tk_root.update()
            except tk.TclError:
                # Root destroyed mid-test (e.g. by another fixture's
                # teardown racing this poller). Let the timeout fire.
                pass
            try:
                result = predicate()
            except Exception:
                result = False
            if result:
                return result
            time.sleep(interval_s)
        raise AssertionError(
            f"predicate did not become truthy within {timeout_s}s"
        )

    return _wait


# ── AfterHarness (G-A + G-H) ──────────────────────────────────────────────

class AfterHarness:
    """Captures ``self.after(ms, cb, *args)`` into a sorted queue.

    Tests advance time EXPLICITLY via ``harness.advance(ms)``. Only
    callbacks whose deadlines have passed fire — recursive ``after()``
    loops (e.g. ``_draft_tick``'s 1-second poll) can be ticked one
    iteration at a time without infinite recursion.

    G-H: ``advance()`` snapshots the ready queue BEFORE invoking ANY
    callback. If a callback schedules another ``after(0, cb)`` at the
    same deadline, that new callback waits for the NEXT ``advance()``
    call rather than firing in the same loop iteration. This mirrors
    real event-loop tick semantics and prevents synchronous hangs from
    mutually-recursive ``after(0, ...)`` chains.

    Use ``drain(max_rounds=1000)`` to repeatedly advance(0) until
    quiescent — useful when a chain of ``after(0, ...)`` callbacks
    should be processed without testing each tick individually.
    """

    def __init__(self):
        self._queue: list = []   # (deadline, seq, cb, args)
        self._now: int = 0
        self._seq: int = 0

    def schedule(self, ms, cb, *args):
        self._seq += 1
        # Defensive: tk.after accepts ms=0 and ms=None ("idle"); treat
        # both as immediate.
        try:
            ms_int = int(ms) if ms is not None else 0
        except (TypeError, ValueError):
            ms_int = 0
        self._queue.append((self._now + ms_int, self._seq, cb, args))
        self._queue.sort(key=lambda x: (x[0], x[1]))
        return f"after#{self._seq}"

    def cancel(self, after_id):
        # Tests that need to verify cancellation can override. We keep
        # this lenient by default so the simple "schedule + advance"
        # flow doesn't have to thread cancel IDs through assertions.
        pass

    def advance(self, ms: int) -> int:
        """Advance virtual time by ``ms`` and fire any callbacks now due.

        Returns the number of callbacks fired this call.

        G-H: ready callbacks are snapshotted BEFORE invocation. Callbacks
        that schedule new ones during execution must wait for the next
        ``advance()`` call to fire.
        """
        self._now += ms
        ready = []
        while self._queue and self._queue[0][0] <= self._now:
            ready.append(self._queue.pop(0))
        for _, _, cb, args in ready:
            cb(*args)
        return len(ready)

    def drain(self, max_rounds: int = 1000) -> int:
        """Repeatedly ``advance(0)`` until quiescent. Returns total fired.

        Cap prevents infinite outer loops if callbacks keep scheduling
        themselves with ``after(0, ...)`` forever (likely a bug).
        """
        total = 0
        for _ in range(max_rounds):
            n = self.advance(0)
            if n == 0:
                return total
            total += n
        raise AssertionError(
            f"AfterHarness.drain exceeded {max_rounds} rounds — "
            "likely an after(0) callback loop bug in the code under test."
        )


@pytest.fixture
def patch_after(mocker):
    """Factory that swaps a dialog's ``after`` for an ``AfterHarness``.

    Returns the harness so the test can call ``harness.advance(1000)``
    or ``harness.drain()`` to fire scheduled callbacks deterministically.

    Example::

        def test_draft_tick_lifecycle(tk_root, mock_config, patch_after):
            dialog = DocDrafterDialog(tk_root, cfg=mock_config)
            harness = patch_after(dialog)
            dialog._on_generate("changelog")
            assert harness.advance(1000) >= 1   # tick fires
    """
    def _patch(dialog):
        harness = AfterHarness()
        mocker.patch.object(dialog, "after",        side_effect=harness.schedule)
        mocker.patch.object(dialog, "after_cancel", side_effect=harness.cancel)
        return harness
    return _patch


# ── fake_home (G-F + G-J Part 2) ──────────────────────────────────────────

# Modules that MAY cache user-paths at import time. The G-J pre-flight
# test (`test_no_import_time_path_resolution.py`) enforces that they
# don't, but this list is kept here as a defence-in-depth measure: the
# fixture calls importlib.reload() on each so any future regression
# is at least covered when the cascade-uninstall tests run with
# fake_home active.
_RELOAD_AFTER_FAKE_HOME = (
    "helpers.mcp",
    "helpers.install_tokensave",
    "helpers.codegraph_freshness",
    "helpers.claude_tasks",
    "state",
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``$HOME`` / ``$USERPROFILE`` / ``$APPDATA`` / ``$LOCALAPPDATA``
    to per-test tmp_path subdirectories.

    All helpers that use ``os.path.expanduser('~')`` or
    ``os.environ.get('APPDATA')`` (e.g. ``_claude_code_mcp_has_codegraph``,
    ``is_manager_installed``, ``install_tokensave``) now resolve to the
    isolated fake home for the duration of the test.

    Returns the fake-home root path so tests can write fixture files
    into it (e.g. seed a ``~/.claude.json`` with a known MCP config).
    """
    home    = tmp_path / "home";    home.mkdir()
    appdata = home / "AppData" / "Roaming"; appdata.mkdir(parents=True)
    local   = home / "AppData" / "Local";   local.mkdir(parents=True)

    monkeypatch.setenv("HOME",         str(home))
    monkeypatch.setenv("USERPROFILE",  str(home))
    monkeypatch.setenv("APPDATA",      str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    # G-J Part 2: defence in depth. The pre-flight test should keep this
    # list a no-op in practice, but if someone adds a module-level path
    # constant tomorrow and skips the pre-flight, the reload here at
    # least keeps cascade-uninstall tests honest.
    for modname in _RELOAD_AFTER_FAKE_HOME:
        if modname in sys.modules:
            try:
                importlib.reload(sys.modules[modname])
            except Exception:
                pass  # Tk-coupled or otherwise unreloadable; skip silently.

    return home


# ── mock_config ───────────────────────────────────────────────────────────

class _MockConfig:
    """Minimal stand-in for ``state.ManagerConfig`` used by dialog tests.

    Real ``ManagerConfig`` reads from a JSON file and writes back via
    ``save()``. Tests need:

    * a ``raw`` dict that dialog code can mutate
    * a ``save()`` method that just records it was called
    * read-only properties matching the real interface so dialogs that
      read ``cfg.tokensave_exe`` etc. don't crash

    Dialog tests verify ``cfg._saved`` is True after operations that
    should persist (G-F: every cfg mutation must be followed by save).
    """

    def __init__(self, **overrides):
        # Same defaults as a fresh manager-config.json minus the
        # search_roots / window_geometry / per-machine paths.
        self.raw: dict = {
            "tokensave_exe":             "",
            "codegraph_exe":             "",
            "claude_cli_exe":            "",
            "python_exe":                sys.executable,
            "git_exe":                   "",
            "enable_llm_grounding":      True,
            "enable_commit_grounding":   True,
            "enable_pr_grounding":       True,
            "checks_enabled": {
                "syntax":   True,
                "pyflakes": True,
                "doctor":   False,
                "claude":   False,
            },
            "doctor_skip_paths": [],
            "commit_message_backend": "llm_first",
            "draft_pr_backend":       "auto",
            **overrides,
        }
        self._saved: bool = False

    # Property accessors mirror ManagerConfig's read interface so dialogs
    # that read attributes (not just .raw) don't break.
    @property
    def tokensave_exe(self):       return self.raw.get("tokensave_exe", "")
    @property
    def codegraph_exe(self):       return self.raw.get("codegraph_exe", "")
    @property
    def claude_cli_exe(self):      return self.raw.get("claude_cli_exe", "")
    @property
    def python_exe(self):          return self.raw.get("python_exe", sys.executable)
    @property
    def git_exe(self):             return self.raw.get("git_exe", "")
    @property
    def enable_llm_grounding(self):     return bool(self.raw.get("enable_llm_grounding", True))
    @property
    def enable_commit_grounding(self):  return bool(self.raw.get("enable_commit_grounding", True))
    @property
    def enable_pr_grounding(self):      return bool(self.raw.get("enable_pr_grounding", True))

    def save(self) -> None:
        self._saved = True


@pytest.fixture
def mock_config():
    """Yield a fresh ``_MockConfig``. Each test gets its own instance."""
    return _MockConfig()
