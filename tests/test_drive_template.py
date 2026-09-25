"""tests/test_drive_template.py — the starting point new projects get.

The template exists so a project's driver starts with the lessons three projects
paid for, instead of rediscovering each one. That only holds if it (a) does not
quietly fall behind the Manager's own ledger, (b) actually works when a project
renames one variable and runs it, and (c) is copied once and never touches what a
person has since edited.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

from helpers import drive_template

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(_ROOT, "templates")
DRIVE = os.path.join(TEMPLATES, "drive")


def _lf(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


# ── the template cannot quietly fall behind ───────────────────────────────

def test_the_templates_ledger_is_byte_identical_to_the_managers():
    """One ledger, two homes. A fix to the Manager's copy that is not carried
    into the template is a new project starting with a known defect."""
    assert _lf(os.path.join(DRIVE, "drive_ledger.py")) == \
        _lf(os.path.join(_ROOT, "src", "helpers", "drive_ledger.py"))


def test_the_shipped_files_are_exactly_the_scaffolded_files():
    on_disk = {n for n in os.listdir(DRIVE) if not n.startswith(("_", "."))}
    assert on_disk == set(drive_template.FILES)


def test_the_readme_states_the_rules_a_new_driver_would_otherwise_relearn():
    text = _lf(os.path.join(DRIVE, "README.md")).lower()
    for phrase in ("fail-open", "settle_ms", "os._exit", "step()", "startup",
                   "encoding-safe"):
        assert phrase in text, phrase


# ── the skeleton works, not just parses ───────────────────────────────────

@pytest.fixture
def skeleton(monkeypatch):
    """The template's driver, imported the way a project would: beside its ledger."""
    monkeypatch.syspath_prepend(DRIVE)
    for name in ("drive_ledger", "debug_drive_tk"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "debug_drive_tk", os.path.join(DRIVE, "debug_drive_tk.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in ("drive_ledger", "debug_drive_tk"):
        sys.modules.pop(name, None)


class _App:
    def __init__(self):
        self.scheduled, self.destroyed = [], False

    def after(self, delay, fn=None):
        self.scheduled.append((delay, fn))

    def destroy(self):
        self.destroyed = True

    def winfo_children(self):
        return []


def _driver(skeleton, steps, **kw):
    app = _App()
    ledger = skeleton.drive_ledger.Ledger()
    ledger.start_drive()
    return app, ledger, skeleton.Driver(app, steps, ledger=ledger, **kw)


def test_the_skeleton_is_inert_without_its_env_var(skeleton):
    assert skeleton.start_if_requested(_App()) is None
    skeleton.begin(_App())
    assert skeleton._LEDGER is None


def test_a_failing_expect_and_a_step_failure_fail_the_run(skeleton):
    _, ledger, driver = _driver(skeleton, [
        {"do": "expect", "id": "never", "check": "diagnostic_contains",
         "text": "absent", "within_ms": 0},
        {"do": "nonsense"}])
    driver.step()
    driver.step()
    assert ledger.failed
    assert [e["passed"] for e in ledger.expectations] == [False]


def test_expect_clean_needs_a_quiet_window_and_catches_a_late_exception(skeleton):
    _, ledger, driver = _driver(skeleton,
                                [{"do": "expect_clean", "settle_ms": 60000}])
    driver.step()
    assert driver._hold() is True                       # quiet so far
    ledger.record(skeleton.drive_ledger.UNCAUGHT_EXCEPTION, "tk.callback",
                  "CRITICAL", "f", "late")
    assert driver._hold() is False
    assert ledger.failed


def test_step_arms_no_timer_but_run_next_does(skeleton):
    app = _App()
    driver = skeleton.Driver(app, [{"do": "wait"}, {"do": "wait"}])
    driver.step()
    assert app.scheduled == []
    driver._run_next()
    assert len(app.scheduled) == 1


def test_finalize_writes_the_report_once_and_a_handbuilt_driver_never_exits(
        skeleton, tmp_path):
    script = tmp_path / "s.json"
    script.write_text("[]", encoding="utf-8")
    app = _App()
    ledger = skeleton.drive_ledger.Ledger()
    ledger.expect("e", False, expected=1, actual=2, elapsed_ms=1, reason="r")
    driver = skeleton.Driver(app, [], ledger=ledger, script=None)
    driver._script = str(script)

    report = driver._finalize("quit")
    written = tmp_path / "s.report.json"
    first = written.read_text(encoding="utf-8")
    driver._finalize("exit")

    assert report["passed"] is False
    assert json.loads(first)["finished_because"] == "quit"
    assert written.read_text(encoding="utf-8") == first
    # No script at construction: _do_quit must not exit the test runner.
    driver._script = None
    driver._do_quit({"do": "quit"})
    assert app.destroyed


def test_the_skeleton_and_the_managers_driver_agree_on_the_evidence_api(skeleton):
    """The names the skeleton calls must exist on the ledger it ships with."""
    led = skeleton.drive_ledger.Ledger()
    for name in ("install", "install_tk", "start_drive", "step_failed", "expect",
                 "drive_diagnostics", "contains", "summary", "finalize",
                 "finalized", "uninstall"):
        assert hasattr(led, name), name


# ── scaffolding: copied once, never overwriting ───────────────────────────

def test_scaffolding_creates_every_file_under_drive_template(tmp_path):
    lines = drive_template.scaffold_drive_template(TEMPLATES, str(tmp_path))
    assert lines == ["Created drive_template/%s" % n for n in drive_template.FILES]
    for name in drive_template.FILES:
        assert (tmp_path / "drive_template" / name).is_file()
    assert drive_template.wrote_anything(lines)


def test_an_existing_file_is_left_byte_for_byte_alone(tmp_path):
    target = tmp_path / "drive_template" / "debug_drive_tk.py"
    target.parent.mkdir()
    target.write_text("# my tuned driver\n", encoding="utf-8")

    lines = drive_template.scaffold_drive_template(TEMPLATES, str(tmp_path))

    assert target.read_text(encoding="utf-8") == "# my tuned driver\n"
    assert any("already exists - left alone" in l for l in lines)
    assert (tmp_path / "drive_template" / "README.md").is_file(), \
        "one existing file must not stop the others"


def test_a_second_scaffold_changes_nothing(tmp_path):
    drive_template.scaffold_drive_template(TEMPLATES, str(tmp_path))
    before = {n: (tmp_path / "drive_template" / n).read_bytes()
              for n in drive_template.FILES}
    again = drive_template.scaffold_drive_template(TEMPLATES, str(tmp_path))
    assert not drive_template.wrote_anything(again)
    assert {n: (tmp_path / "drive_template" / n).read_bytes()
            for n in drive_template.FILES} == before


def test_an_unreadable_template_file_is_reported_not_written_empty(tmp_path):
    lines = drive_template.scaffold_drive_template(str(tmp_path / "nowhere"),
                                                   str(tmp_path / "proj"))
    assert len(lines) == len(drive_template.FILES)
    assert all("could not be read" in l for l in lines)
    assert not (tmp_path / "proj").exists()


def test_the_scaffold_dialog_offers_it_and_forwards_the_choice():
    """The option must exist AND reach the controller under its own keyword."""
    src = open(os.path.join(_ROOT, "src", "dialogs", "scaffold.py"),
               encoding="utf-8").read()
    assert re.search(r"scaffold_drive\s*=\s*scaffold_drive", src)
    ctl = open(os.path.join(_ROOT, "src", "controllers", "scaffold_ctrl.py"),
               encoding="utf-8").read()
    assert "scaffold_drive: bool = False" in ctl
    assert "self._scaffold_drive_template(path)" in ctl
