"""tests/test_doctor_worktree_repair.py — Doctor's worktree-repair offer.

Tk-marked. Covers the third leg of `_offer_followups` (worktree repair,
alongside the existing stale-purge and agent-wiring offers) and the repair
worker's verb-accurate logging.

Directory-existence checks (init vs sync --force) use real `tmp_path`
directories rather than mocked `os.path.isdir`, per the G-E discipline
established in test_dialog_tokensave_mcp_picker.py.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from controllers.doctor_ctrl import DoctorController


def _ctrl(tk_root, mock_config, on_log=None):
    tab = tk.Frame(tk_root)
    logs = []
    ctl = DoctorController(
        tab, mock_config,
        on_log=on_log or (lambda m, c="": logs.append(m)),
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
    )
    return ctl, logs


# ── _offer_followups dispatch ─────────────────────────────────────────────

def test_followups_offers_worktree_repair_when_orphans_present(
        tk_root, mock_config, mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    offer = mocker.patch.object(ctl, "_offer_worktree_repair")
    ctl._offer_followups("/path", [], [], 0, orphans=[{"worktree_path": "/x"}])
    offer.assert_called_once_with("/path", [{"worktree_path": "/x"}])


def test_followups_skips_worktree_offer_when_no_orphans(
        tk_root, mock_config, mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    offer = mocker.patch.object(ctl, "_offer_worktree_repair")
    ctl._offer_followups("/path", [], [], 0, orphans=[])
    offer.assert_not_called()


def test_followups_backward_compatible_without_orphans_arg(
        tk_root, mock_config, mocker):
    """orphans defaults to None — existing 4-arg callers must not break."""
    ctl, _ = _ctrl(tk_root, mock_config)
    offer = mocker.patch.object(ctl, "_offer_worktree_repair")
    ctl._offer_followups("/path", [], [], 0)
    offer.assert_not_called()


def test_followups_never_stacks_all_three_offers(tk_root, mock_config, mocker):
    """All three offer types firing in one run must still be SEQUENCED
    (each is a blocking askyesno), never scheduled independently."""
    ctl, _ = _ctrl(tk_root, mock_config)
    calls = []
    mocker.patch.object(ctl, "_offer_purge",
                        side_effect=lambda *a: calls.append("purge"))
    mocker.patch.object(ctl, "_offer_agent_wiring",
                        side_effect=lambda *a: calls.append("agents"))
    mocker.patch.object(ctl, "_offer_worktree_repair",
                        side_effect=lambda *a: calls.append("worktrees"))
    ctl._offer_followups("/path", ["/stale"], ["claude"], 0,
                         orphans=[{"worktree_path": "/x"}])
    assert calls == ["purge", "agents", "worktrees"]


# ── _offer_worktree_repair ────────────────────────────────────────────────

def test_declining_confirmation_runs_no_repair(tk_root, mock_config, mocker):
    ctl, logs = _ctrl(tk_root, mock_config)
    mocker.patch("controllers.doctor_ctrl.messagebox.askyesno",
                return_value=False)
    run = mocker.patch.object(ctl, "_run_worktree_repair")
    ctl._offer_worktree_repair("/proj", [{"worktree_path": "/wt", "branch": "b",
                                         "head": "1234"}])
    run.assert_not_called()
    assert any("skipped" in m for m in logs)


def test_confirmed_dispatches_to_run_worktree_repair(tk_root, mock_config,
                                                     mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    mocker.patch("controllers.doctor_ctrl.messagebox.askyesno",
                return_value=True)
    run = mocker.patch.object(ctl, "_run_worktree_repair")
    orphans = [{"worktree_path": "/wt", "branch": "b", "head": "1234"}]
    ctl._offer_worktree_repair("/proj", orphans)
    run.assert_called_once_with(orphans)


# ── _run_worktree_repair — verb-accurate logging ──────────────────────────

def test_repair_logs_init_for_fresh_worktree(tk_root, mock_config, mocker,
                                             wait_for, tmp_path):
    # No patch_after needed here — _run_worktree_repair calls the
    # thread-safe on_log callback directly, never self._tab.after(...).
    ctl, logs = _ctrl(tk_root, mock_config)
    wt = tmp_path / "wt1"; wt.mkdir()
    ts_exe = tmp_path / "tokensave.exe"; ts_exe.write_bytes(b"")
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    mocker.patch("controllers.doctor_ctrl.subprocess.run",
                return_value=__import__("types").SimpleNamespace(
                    returncode=0, stdout="", stderr=""))

    ctl._run_worktree_repair([{"worktree_path": str(wt), "branch": "b",
                              "head": "1234"}])
    wait_for(lambda: any("initialized" in m for m in logs), timeout_s=5)
    assert any("initialized" in m for m in logs)
    assert any("restarted" in m for m in logs)


def test_repair_logs_sync_force_for_existing_index(tk_root, mock_config,
                                                   mocker, wait_for, tmp_path):
    ctl, logs = _ctrl(tk_root, mock_config)
    wt = tmp_path / "wt1"
    (wt / ".tokensave").mkdir(parents=True)
    ts_exe = tmp_path / "tokensave.exe"; ts_exe.write_bytes(b"")
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    mocker.patch("controllers.doctor_ctrl.subprocess.run",
                return_value=__import__("types").SimpleNamespace(
                    returncode=0, stdout="", stderr=""))

    ctl._run_worktree_repair([{"worktree_path": str(wt), "branch": "b",
                              "head": "1234"}])
    wait_for(lambda: any("rebuilt" in m for m in logs), timeout_s=5)
    assert any("sync --force" in m for m in logs)
    # Edge case 1: an already-indexed worktree must never be logged as a
    # fresh "initialized" — that would be exactly the false-repair footgun.
    assert not any("✓ initialized" in m for m in logs)


def test_repair_failure_does_not_show_restart_banner(tk_root, mock_config,
                                                     mocker, wait_for,
                                                     tmp_path):
    """The restart banner implies a repair took effect — must not appear
    when every repair failed."""
    ctl, logs = _ctrl(tk_root, mock_config)
    wt = tmp_path / "wt1"; wt.mkdir()
    ts_exe = tmp_path / "tokensave.exe"; ts_exe.write_bytes(b"")
    mock_config.raw["tokensave_exe"] = str(ts_exe)
    mocker.patch("controllers.doctor_ctrl.subprocess.run",
                return_value=__import__("types").SimpleNamespace(
                    returncode=1, stdout="", stderr="disk full"))

    ctl._run_worktree_repair([{"worktree_path": str(wt), "branch": "b",
                              "head": "1234"}])
    wait_for(lambda: any("failed" in m for m in logs), timeout_s=5)
    assert not any("restarted" in m for m in logs)
