"""tests/test_doctor_service.py — invoking doctor, and reading it honestly.

`scan_stale` is the leaf the whole purge/verify contract rests on, and it was
previously reachable only as a patch target in the controller's tests. The
headless CLI calls it directly, so its own behaviour is pinned here.

The distinction it exists to preserve: **a scan that failed is not a clean
bill of health.** An empty entry list from a crashed doctor run looks exactly
like "nothing to clean" unless `ok` is checked, which is why every failure
path returns `ok=False` with an explanation rather than an empty transcript.

Two measured facts about the tool are pinned as assertions rather than left in
prose, because both are invisible until they bite:

  * doctor writes its report to **stderr**, so a caller that does not merge the
    streams gets an empty transcript and reads it as "nothing to report";
  * doctor emits ANSI regardless of ``NO_COLOR``, so the transcript must be
    stripped by us rather than by the environment.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from helpers import doctor_service
from helpers.doctor_service import (
    PurgeResult,
    VERIFY_NO_CHANGE,
    VERIFY_PARTIAL,
    VERIFY_UNVERIFIED,
    VERIFY_VERIFIED,
    doctor_env,
    scan_stale,
    verification_label,
)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# ── the invocation contract ─────────────────────────────────────────────────

def test_doctor_env_asks_for_no_colour():
    env = doctor_env()
    assert env["NO_COLOR"] == "1"
    assert env["TERM"] == "dumb"


def test_doctor_env_does_not_mutate_the_process_environment():
    before = dict(os.environ)
    doctor_env()["NO_COLOR"] = "tampered"
    assert dict(os.environ) == before


def test_the_streams_are_merged_because_doctor_reports_on_stderr(tmp_path, mocker):
    """Not cosmetic: without this the transcript is empty and reads as clean."""
    run = mocker.patch("helpers.doctor_service.subprocess.run",
                       return_value=_FakeCompleted())
    scan_stale(str(tmp_path), "tokensave")
    assert run.call_args.kwargs["stderr"] is subprocess.STDOUT


def test_the_scan_runs_in_the_project_it_was_given(tmp_path, mocker):
    run = mocker.patch("helpers.doctor_service.subprocess.run",
                       return_value=_FakeCompleted())
    scan_stale(str(tmp_path), "tokensave")
    assert run.call_args.kwargs["cwd"] == str(tmp_path)
    assert run.call_args.args[0] == ["tokensave", "doctor"]


def test_ansi_is_stripped_by_us_not_trusted_to_the_environment(tmp_path, mocker):
    """doctor emits colour regardless of NO_COLOR (measured against v7.9.0)."""
    coloured = "\x1b[31m! stale entry\x1b[0m\n\x1b[1mdone\x1b[0m"
    mocker.patch("helpers.doctor_service.subprocess.run",
                 return_value=_FakeCompleted(0, coloured))
    result = scan_stale(str(tmp_path), "tokensave")
    assert result.ok is True
    assert "\x1b[" not in result.transcript
    assert "! stale entry" in result.transcript


def test_the_exit_code_is_reported(tmp_path, mocker):
    mocker.patch("helpers.doctor_service.subprocess.run",
                 return_value=_FakeCompleted(2, "problems"))
    result = scan_stale(str(tmp_path), "tokensave")
    assert result.ok is True, "a non-zero doctor exit is still a usable scan"
    assert result.exit_code == 2


# ── "we don't know" must never look like "nothing to clean" ─────────────────

@pytest.mark.parametrize("boom,expected_fragment", [
    (FileNotFoundError(),                              "not found"),
    (subprocess.TimeoutExpired("tokensave", 120),      "timed out"),
    (OSError("access is denied"),                      "access is denied"),
])
def test_every_failure_path_says_so_rather_than_returning_empty(
        tmp_path, mocker, boom, expected_fragment):
    mocker.patch("helpers.doctor_service.subprocess.run", side_effect=boom)
    result = scan_stale(str(tmp_path), "tokensave")
    assert result.ok is False
    assert expected_fragment in result.error
    assert result.transcript == ""


def test_the_timeout_message_names_the_limit(tmp_path, mocker):
    mocker.patch("helpers.doctor_service.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("tokensave", 5))
    result = scan_stale(str(tmp_path), "tokensave", timeout=5)
    assert "5s" in result.error


def test_a_failed_verification_scan_is_unverified_not_no_change(tmp_path, mocker):
    """The single most important distinction in the purge contract."""
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=doctor_service.DoctorScanResult(False, error="boom"))
    result = doctor_service.verify_purge(str(tmp_path), "tokensave", ["a", "b"])
    assert result.verification_status == VERIFY_UNVERIFIED
    assert result.verification_status != VERIFY_NO_CHANGE


# ── the baseline is reused, not re-scanned ──────────────────────────────────

def test_a_supplied_baseline_does_not_trigger_a_second_doctor_run(tmp_path, mocker):
    """One scan, two jobs — the baseline is also verify's comparison point."""
    scan = mocker.patch("helpers.doctor_service.scan_stale")
    result = doctor_service.purge_stale(str(tmp_path), "tokensave",
                                        baseline=["stale-1"])
    scan.assert_not_called()
    assert result.status == PurgeResult.HANDED_OFF


def test_handing_off_claims_nothing_about_the_outcome(tmp_path):
    result = doctor_service.purge_stale(str(tmp_path), "tokensave",
                                        baseline=["stale-1", "stale-2"])
    assert result.status == PurgeResult.HANDED_OFF
    assert result.succeeded is False, "handed_off is not success"
    assert result.stale_after == []
    assert result.verification_status == ""


# ── verdict rendering ───────────────────────────────────────────────────────

@pytest.mark.parametrize("status,after,fragment", [
    (VERIFY_VERIFIED,   [],           "no stale entries remain"),
    (VERIFY_PARTIAL,    ["a"],        "1 remaining"),
    (VERIFY_NO_CHANGE,  ["a", "b"],   "2 still reported"),
    (VERIFY_UNVERIFIED, [],           "could not be verified"),
])
def test_every_verdict_renders_a_distinct_sentence(status, after, fragment):
    label = verification_label(
        PurgeResult(PurgeResult.SUCCESS, verification_status=status,
                    stale_after=after))
    assert fragment in label


def test_an_unknown_status_renders_nothing_rather_than_a_template():
    assert verification_label(PurgeResult(PurgeResult.HANDED_OFF)) == ""
