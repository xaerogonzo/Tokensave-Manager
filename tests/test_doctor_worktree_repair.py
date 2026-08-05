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
    """All three offer types firing in one run must be SEQUENCED.

    Order is purge -> worktrees -> agents: the agent picker is a persistent
    grab_set() Toplevel, so it goes last (an askyesno after it would render
    over a live modal). The purge branch releases the rest via its on_done
    continuation, since it can still prompt again from a worker thread.
    """
    ctl, _ = _ctrl(tk_root, mock_config)
    calls = []

    def _purge(path, stale, on_done=None):
        calls.append("purge")
        if on_done:
            on_done()

    mocker.patch.object(ctl, "_offer_purge", side_effect=_purge)
    mocker.patch.object(ctl, "_offer_agent_wiring",
                        side_effect=lambda *a: calls.append("agents"))
    mocker.patch.object(ctl, "_offer_worktree_repair",
                        side_effect=lambda *a: calls.append("worktrees"))
    ctl._offer_followups("/path", ["/stale"], ["claude"], 0,
                         orphans=[{"worktree_path": "/x"}])
    assert calls == ["purge", "worktrees", "agents"]


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


# ── Async purge chain must not stack dialogs ──────────────────────────────

def test_purge_branch_defers_later_offers_until_it_finishes(
        tk_root, mock_config, mocker):
    """REGRESSION: _offer_purge spawns a background worker, so simply
    calling the next offer after it returns lets that worker's own
    follow-up prompt land ON TOP of the later dialog. The later offers must
    wait for the purge continuation instead."""
    ctl, _ = _ctrl(tk_root, mock_config)
    captured = {}

    def _fake_offer_purge(path, stale, on_done=None):
        captured["on_done"] = on_done          # simulate async: don't call it

    mocker.patch.object(ctl, "_offer_purge", side_effect=_fake_offer_purge)
    wt = mocker.patch.object(ctl, "_offer_worktree_repair")
    agents = mocker.patch.object(ctl, "_offer_agent_wiring")

    ctl._offer_followups("/p", ["/stale"], ["claude"], 0,
                         orphans=[{"worktree_path": "/x"}])

    # Purge is in flight — nothing else may have prompted yet.
    wt.assert_not_called()
    agents.assert_not_called()

    captured["on_done"]()                       # purge chain completes
    wt.assert_called_once()
    agents.assert_called_once()


def test_agent_picker_is_opened_last(tk_root, mock_config, mocker):
    """The picker is a persistent grab_set() Toplevel — any askyesno opened
    after it would render over a live modal, so it must come last."""
    ctl, _ = _ctrl(tk_root, mock_config)
    order = []
    mocker.patch.object(ctl, "_offer_worktree_repair",
                        side_effect=lambda *a: order.append("worktrees"))
    mocker.patch.object(ctl, "_offer_agent_wiring",
                        side_effect=lambda *a: order.append("agents"))
    ctl._offer_followups("/p", [], ["claude"], 0,
                         orphans=[{"worktree_path": "/x"}])
    assert order == ["worktrees", "agents"]


def test_declining_purge_still_releases_the_chain(tk_root, mock_config, mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    mocker.patch("controllers.doctor_ctrl.messagebox.askyesno",
                return_value=False)
    run = mocker.patch.object(ctl, "_run_purge")
    called = []
    ctl._offer_purge("/p", ["/stale"], on_done=lambda: called.append(1))
    run.assert_not_called()
    assert called == [1]


def test_offer_in_cmd_releases_chain_even_when_declined(
        tk_root, mock_config, mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    mocker.patch("controllers.doctor_ctrl.messagebox.askyesno",
                return_value=False)
    called = []
    ctl._offer_in_cmd("/p", 2, on_done=lambda: called.append(1))
    assert called == [1]


def test_offer_in_cmd_releases_chain_even_when_launch_fails(
        tk_root, mock_config, mocker):
    """A cmd.exe launch failure must not strand the rest of the sequence."""
    ctl, _ = _ctrl(tk_root, mock_config)
    mocker.patch("controllers.doctor_ctrl.messagebox.askyesno",
                return_value=True)
    mocker.patch("controllers.doctor_ctrl.subprocess.Popen",
                side_effect=OSError("nope"))
    called = []
    ctl._offer_in_cmd("/p", 2, on_done=lambda: called.append(1))
    assert called == [1]


# ── other_n alone must never raise a modal ────────────────────────────────

def test_no_actionable_agents_means_no_agent_dialog(tk_root, mock_config,
                                                    mocker):
    """REGRESSION: `other_n` is informational (agents not installed here, or
    already wired). It was raising a modal on every doctor run with nothing
    the user could act on."""
    ctl, _ = _ctrl(tk_root, mock_config)
    agents = mocker.patch.object(ctl, "_offer_agent_wiring")
    ctl._offer_followups("/p", [], [], 18, orphans=[])
    agents.assert_not_called()


def test_actionable_agents_still_prompt(tk_root, mock_config, mocker):
    ctl, _ = _ctrl(tk_root, mock_config)
    agents = mocker.patch.object(ctl, "_offer_agent_wiring")
    ctl._offer_followups("/p", [], ["cursor"], 18, orphans=[])
    agents.assert_called_once_with(["cursor"], 18)
