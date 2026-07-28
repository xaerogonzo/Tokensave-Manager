"""tests/test_shadowlinks.py — controllers/shadowlinks_ctrl.py."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import tkinter as tk

from controllers.shadowlinks_ctrl import ShadowLinksController

pytestmark = pytest.mark.tk


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ctrl(tk_root, on_log=None, on_refresh=None,
               on_run_capture=None, on_commit_offer=None,
               tokensave_exe="fake_tokensave.exe"):
    tab = tk.Frame(tk_root)
    cfg = SimpleNamespace(tokensave_exe=tokensave_exe)
    calls: list = []

    def _after_immed(delay, fn, *args):
        fn(*args)

    tab.after = _after_immed  # type: ignore[assignment]

    return ShadowLinksController(
        tab=tab,
        cfg=cfg,
        on_log=on_log or (lambda msg, col="": calls.append(("log", msg))),
        on_refresh=on_refresh or (lambda: calls.append(("refresh",))),
        on_run_capture=on_run_capture or (lambda args, cwd, lbl: ("", 0, 0.0)),
        on_commit_offer=on_commit_offer or (lambda p, l: calls.append(("commit", p))),
    ), calls


# ── cmd_shadow_links: NTFS preflight ─────────────────────────────────────────

def test_cmd_shadow_links_error_when_no_hardlink_support(tk_root, monkeypatch, tmp_path):
    errors = []
    monkeypatch.setattr("controllers.shadowlinks_ctrl.supports_hardlinks",
                        lambda path: False)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(
                            showerror=lambda *a, **k: errors.append(a),
                            showinfo=lambda *a, **k: None,
                        ))
    dialog_shown = []
    monkeypatch.setattr("controllers.shadowlinks_ctrl.ShadowLinksDialog",
                        lambda *a, **k: dialog_shown.append(True))

    ctl, _ = _make_ctrl(tk_root)
    ctl.cmd_shadow_links(str(tmp_path))

    assert len(errors) == 1
    assert "NTFS" in errors[0][1] or "hardlink" in errors[0][1].lower()
    assert not dialog_shown


def test_cmd_shadow_links_opens_dialog_when_supported(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.supports_hardlinks",
                        lambda path: True)
    dialog_shown = []
    monkeypatch.setattr("controllers.shadowlinks_ctrl.ShadowLinksDialog",
                        lambda *a, **k: dialog_shown.append(True))

    ctl, _ = _make_ctrl(tk_root)
    ctl.cmd_shadow_links(str(tmp_path))

    assert len(dialog_shown) == 1


# ── _do_shadow_links: worker thread logic ─────────────────────────────────────

def _patch_threading_sync(monkeypatch):
    """Patch threading.Thread so worker runs synchronously in the calling thread."""
    import threading as _threading

    class _SyncThread:
        def __init__(self, target=None, daemon=False, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr("controllers.shadowlinks_ctrl.threading.Thread", _SyncThread)


def test_do_shadow_links_logs_created_count(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (5, 2, 0))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: None))
    _patch_threading_sync(monkeypatch)

    log_msgs: list = []
    ctl, _ = _make_ctrl(tk_root, on_log=lambda msg, col="": log_msgs.append(msg))
    ctl._do_shadow_links(str(tmp_path), {".gd": ".py"}, run_sync=False)

    assert any("Created: 5" in m for m in log_msgs)


def test_do_shadow_links_calls_on_refresh(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (1, 0, 0))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: None))
    _patch_threading_sync(monkeypatch)

    refreshed: list = []
    ctl, _ = _make_ctrl(tk_root, on_refresh=lambda: refreshed.append(1))
    ctl._do_shadow_links(str(tmp_path), {}, run_sync=False)

    assert len(refreshed) == 1


def test_do_shadow_links_offers_commit(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (2, 0, 0))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: None))
    _patch_threading_sync(monkeypatch)

    commit_calls: list = []
    ctl, _ = _make_ctrl(tk_root,
                        on_commit_offer=lambda p, l: commit_calls.append((p, l)))
    ctl._do_shadow_links(str(tmp_path), {}, run_sync=False)

    assert len(commit_calls) == 1
    assert "shadow" in commit_calls[0][1].lower()


def test_do_shadow_links_runs_sync_when_created(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (3, 0, 0))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: None))
    _patch_threading_sync(monkeypatch)

    sync_calls: list = []

    def _capture(args, cwd, label):
        sync_calls.append((args, label))
        return ("", 0, 0.1)

    ctl, _ = _make_ctrl(tk_root, on_run_capture=_capture)
    ctl._do_shadow_links(str(tmp_path), {}, run_sync=True)

    assert len(sync_calls) == 1
    assert "sync" in sync_calls[0][0] or "sync" in sync_calls[0][1].lower()


def test_do_shadow_links_no_sync_when_zero_created(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (0, 5, 0))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: None))
    _patch_threading_sync(monkeypatch)

    sync_calls: list = []
    ctl, _ = _make_ctrl(tk_root,
                        on_run_capture=lambda a, c, l: sync_calls.append(l) or ("", 0, 0))
    ctl._do_shadow_links(str(tmp_path), {}, run_sync=True)

    assert not sync_calls


def test_do_shadow_links_handles_exception(tk_root, monkeypatch, tmp_path):
    monkeypatch.setattr("controllers.shadowlinks_ctrl.generate_shadow_links",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr("controllers.shadowlinks_ctrl.update_gitignore_for_shadows",
                        lambda p, m: None)
    errors_shown: list = []
    monkeypatch.setattr("controllers.shadowlinks_ctrl.messagebox",
                        SimpleNamespace(showinfo=lambda *a, **k: None,
                                        showerror=lambda *a, **k: errors_shown.append(a)))
    _patch_threading_sync(monkeypatch)

    log_msgs: list = []
    ctl, _ = _make_ctrl(tk_root, on_log=lambda msg, col="": log_msgs.append(msg))
    ctl._do_shadow_links(str(tmp_path), {}, run_sync=False)

    assert any("disk full" in m or "Error" in m for m in log_msgs)
    assert len(errors_shown) == 1
