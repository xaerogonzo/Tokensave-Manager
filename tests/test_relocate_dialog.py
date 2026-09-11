"""tests/test_relocate_dialog.py — the relocation offer (Tk-marked).

The load-bearing one is the ORDER. `baseline_include_line` is derived from
`template_dir`, so a relocation that repointed projects before updating the
config would write the OLD baseline path into every one of them — convincingly,
and with every project reporting success.

The rest guard the refusals: a downgrade must be acknowledged, and a blocked
plan must not be applicable at all.
"""
from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter")

from dialogs import relocate as relocate_mod
from dialogs.relocate import RelocateDialog
from helpers import install_identity as ii

pytestmark = pytest.mark.tk


def _install(tmp_path, name, baseline_text="# baseline\n"):
    d = tmp_path / name
    (d / "templates").mkdir(parents=True)
    (d / "templates" / "project-baseline.md").write_text(
        baseline_text, encoding="utf-8")
    return d


def _proj(name, reached=""):
    import types
    return types.SimpleNamespace(name=name, reached_baseline=reached)


def _moved_plan(tmp_path, baseline_text="# baseline\n"):
    """This install moved; two projects still point at where it was."""
    old = _install(tmp_path, "old")
    new = _install(tmp_path, "new", baseline_text)
    here = str(new / "templates")
    there = str(old / "templates")
    raw = {"install_dir": str(old), "template_dir": there}
    identity = ii.read_identity(raw, str(new))
    fleet = [_proj("alpha", os.path.join(there, "project-baseline.md")),
             _proj("beta", os.path.join(there, "project-baseline.md"))]
    ownership = ii.read_ownership(fleet, here)
    return ii.relocation_plan(identity, ownership, raw, here), raw, here


class _Cfg:
    """Minimal ManagerConfig stand-in: raw dict + the derived values used."""

    def __init__(self, raw, template_dir):
        self.raw = dict(raw)
        self._template_dir = template_dir
        self.saved = 0
        self.refreshed = 0
        self.basic_instructions_template = ""

    @property
    def template_dir(self):
        return self.raw.get("template_dir", "") or self._template_dir

    @property
    def baseline_include_line(self):
        return "@" + os.path.join(self.template_dir, "project-baseline.md")

    def save(self):
        self.saved += 1

    def refresh_derived(self):
        self.refreshed += 1


def test_the_config_is_written_before_any_project_is_repointed(
        tk_root, tmp_path, mocker, wait_for):
    """The order is not cosmetic.

    `baseline_include_line` is DERIVED from `template_dir`. Repointing first
    writes the OLD baseline path into every project, and every project reports
    success while doing it.

    Note where the patches go. `read_posture` is imported INSIDE the worker, so
    it has to be patched on its own module; `apply_to_project` is a module-level
    import here, so it is patched on this one. The first draft of this test
    patched both on `dialogs.relocate`, which left the real `read_posture`
    running against an empty root list -- so no project was ever visited, and
    `order[0] == "config"` passed against a list of length one.
    """
    from helpers import instructions_posture as ip_mod

    plan, raw, here = _moved_plan(tmp_path)
    cfg = _Cfg(raw, here)
    order = []

    real_save = cfg.save

    def spy_save():
        order.append("config")
        real_save()

    mocker.patch.object(cfg, "save", side_effect=spy_save)

    import types
    fleet = types.SimpleNamespace(projects=[_proj("alpha"), _proj("beta")])
    mocker.patch.object(ip_mod, "read_posture", return_value=fleet)

    from helpers.instructions_wiring import OUTCOME_WIRED, ApplyOutcome

    def spy_apply(*_a, **_k):
        order.append("project")
        return ApplyOutcome(OUTCOME_WIRED, changed_files=("CLAUDE.md",))

    mocker.patch.object(relocate_mod, "apply_to_project",
                        side_effect=spy_apply)
    mocker.patch.object(relocate_mod.messagebox, "askyesno", return_value=True)
    mocker.patch.object(relocate_mod.messagebox, "showinfo")

    dialog = RelocateDialog(tk_root, cfg, plan)
    dialog._apply()
    wait_for(lambda: order.count("project") == 2, timeout_s=3.0)

    assert order == ["config", "project", "project"], order
    assert cfg.refreshed >= 1, "refresh_derived was not called"
    assert cfg.raw["template_dir"] == here
    assert cfg.raw["install_dir"] == plan.identity.current_display


def test_a_matching_baseline_needs_no_acknowledgement(tk_root, tmp_path):
    """Same content on both sides: nothing to compare, Apply is live."""
    plan, raw, here = _moved_plan(tmp_path)
    assert plan.downgrades is False
    dialog = RelocateDialog(tk_root, _Cfg(raw, here), plan)
    assert str(dialog._apply_btn["state"]) == "normal"


def test_a_differing_baseline_must_be_acknowledged_first(tk_root, tmp_path):
    """The measured case: a release shipping a baseline four months older.

    Shown, never decided — there is no version order here, so the dialog puts
    both files on screen and requires a deliberate tick.
    """
    plan, raw, here = _moved_plan(tmp_path, baseline_text="# a different one\n")
    assert plan.downgrades is True

    dialog = RelocateDialog(tk_root, _Cfg(raw, here), plan)

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert dialog._warning["text"], "no evidence was shown"
    dialog._acknowledged.set(True)
    dialog._sync_apply()
    assert str(dialog._apply_btn["state"]) == "normal"


def test_both_baselines_are_shown_with_hash_not_just_size(tk_root, tmp_path):
    """Size and mtime can coincide; the hash is why both are carried."""
    plan, _raw, _here = _moved_plan(tmp_path, baseline_text="# other\n")
    assert plan.shipped_baseline.content_hash
    assert plan.reached_baseline.content_hash
    assert plan.shipped_baseline.content_hash != \
        plan.reached_baseline.content_hash


def test_a_blocked_plan_cannot_be_applied(tk_root, tmp_path):
    """A split fleet has no single thing to repoint."""
    old = _install(tmp_path, "old")
    new = _install(tmp_path, "new")
    here, there = str(new / "templates"), str(old / "templates")
    raw = {"install_dir": str(old), "template_dir": there}
    identity = ii.read_identity(raw, str(new))
    fleet = [_proj("a", os.path.join(there, "project-baseline.md")),
             _proj("b", os.path.join(here, "project-baseline.md"))]
    plan = ii.relocation_plan(identity, ii.read_ownership(fleet, here), raw,
                              here)

    assert plan.blocked
    cfg = _Cfg(raw, here)
    dialog = RelocateDialog(tk_root, cfg, plan)
    assert str(dialog._apply_btn["state"]) == "disabled"


def test_a_blocked_plan_writes_nothing_even_if_apply_is_called(
        tk_root, tmp_path, mocker):
    """The guard lives in `_apply`, not only on the button.

    `plan_wiring` records the same reasoning about its own exclusion check: a
    caller reaching the method directly walks straight past the widget. An
    earlier version of the test above asserted only the button state, and
    survived removing this guard entirely.
    """
    old = _install(tmp_path, "old")
    new = _install(tmp_path, "new")
    here, there = str(new / "templates"), str(old / "templates")
    raw = {"install_dir": str(old), "template_dir": there}
    identity = ii.read_identity(raw, str(new))
    fleet = [_proj("a", os.path.join(there, "project-baseline.md")),
             _proj("b", os.path.join(here, "project-baseline.md"))]
    plan = ii.relocation_plan(identity, ii.read_ownership(fleet, here), raw,
                              here)
    cfg = _Cfg(raw, here)
    asked = mocker.patch.object(relocate_mod.messagebox, "askyesno",
                                return_value=True)

    RelocateDialog(tk_root, cfg, plan)._apply()

    assert asked.called is False, "a blocked plan reached the confirmation"
    assert cfg.saved == 0
    assert cfg.raw["template_dir"] == there


def test_mixed_outcomes_are_reported_per_project(tk_root, tmp_path, mocker):
    """Never "N/N". A project skipped because it moved is its own line."""
    from helpers.instructions_wiring import (
        OUTCOME_SKIPPED_STATE_CHANGED, OUTCOME_WIRED, ApplyOutcome,
    )
    plan, raw, here = _moved_plan(tmp_path)
    logged = []
    dialog = RelocateDialog(tk_root, _Cfg(raw, here), plan,
                            on_log=lambda m, c="": logged.append(m))
    shown = {}
    mocker.patch.object(relocate_mod.messagebox, "showinfo",
                        side_effect=lambda *a, **k: shown.update(body=a[1]))

    dialog._finish([
        ("alpha", ApplyOutcome(OUTCOME_WIRED, changed_files=("CLAUDE.md",))),
        ("beta", ApplyOutcome(OUTCOME_SKIPPED_STATE_CHANGED, "was stale")),
    ])

    assert "1 repointed." in shown["body"]
    assert "1 not repointed:" in shown["body"]
    assert "beta" in shown["body"]
    assert any("alpha" in m for m in logged)
    assert any("beta" in m for m in logged)
