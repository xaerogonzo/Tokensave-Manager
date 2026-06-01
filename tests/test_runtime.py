"""Tests for helpers/runtime.py.

The `log` object is a module-level singleton created at import time by
_setup_logger(). Calling _setup_logger() again in a test would add a SECOND
handler to the same 'tsm' logger name (logging uses a global registry). We
therefore test the live `log` object directly rather than constructing a
fresh one.

Windows-only functions (_acquire_instance_lock, _bring_existing_to_front)
use ctypes.windll which does not exist on Linux — guarded with skipif.
The mutex test is smoke-only: it confirms the function runs without crashing
and returns a bool. We do not assert the True/False value because it depends
on external state (whether a live instance of the app is already running on
the test machine).

_make_tray_icon is skipped entirely — it requires PIL, which is not
installed in the CI environment.
"""
import logging
import logging.handlers
import sys

import pytest

import helpers.runtime as runtime_mod


def test_log_is_logger_named_tsm():
    assert isinstance(runtime_mod.log, logging.Logger)
    assert runtime_mod.log.name == "tsm"


def test_log_level_is_debug():
    assert runtime_mod.log.level == logging.DEBUG


def test_log_has_rotating_file_handler():
    handler_types = [type(h) for h in runtime_mod.log.handlers]
    assert logging.handlers.RotatingFileHandler in handler_types, (
        f"Expected RotatingFileHandler in {handler_types}"
    )


def test_log_handler_formatter_includes_asctime():
    """The formatter uses %(asctime)s so every log line has a timestamp."""
    for h in runtime_mod.log.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            # _fmt is a protected attribute but stable across all CPython 3.x
            # versions — acceptable for an internal test-suite check.
            assert "%(asctime)s" in h.formatter._fmt
            return
    pytest.fail("No RotatingFileHandler found on the 'tsm' logger")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only kernel32 mutex")
def test_acquire_instance_lock_smoke():
    """Smoke-only: confirms the function runs and returns a bool.

    Does NOT assert True or False — the value depends on whether a live
    instance of the manager is already running on the test machine.
    """
    result = runtime_mod._acquire_instance_lock()
    assert isinstance(result, bool)
