"""dialogs/release_ci_panel.py and the Wizard's choice of body.

The panel is drawn for real on the session Tk root; git and gh are the scripted `World` from
test_release_flow, so nothing here touches a repository or the network. What is asserted is what a
person would rely on: the verdict and commands appear, Run is locked until the previous step is
done, a refused step runs nothing, and there is no way to publish from here.
"""
from __future__ import annotations

import json
import types

import pytest

from dialogs import release_ci_panel as panel_mod
from dialogs import release_wizard as rw
from helpers import release_flow as rfl
from helpers import release_profile as rp
from test_release_flow import TAG, World, project  # noqa: F401  (project is a fixture)


def _labels(widget):
    out = []
    for child in widget.winfo_children():
        try:
            out.append(str(child.cget("text")))
        except Exception:
            pass
        out += _labels(child)
    return out


def _entries(widget):
    out = []
    for child in widget.winfo_children():
        if child.winfo_class() == "Entry":
            out.append(child.get())
        out += _entries(child)
    return out


@pytest.fixture()
def made(tk_root, project, wait_for):
    (project / "release-flow.json").write_text(json.dumps({"assets": ["a.zip"]}), encoding="utf-8")
    world = World()
    cfg = types.SimpleNamespace(git_exe="git", raw={})
    opened = []
    panel = panel_mod.ReleaseCiPanel(tk_root, str(project), cfg, rp.detect(str(project)),
                                     opener=opened.append, env=world.env())
    wait_for(lambda: panel._pre is not None)
    yield panel, world, opened
    panel.destroy()


def test_shows_the_verdict_the_commands_and_the_checklist(made):
    panel, world, _ = made
    assert panel._pre.verdict == rfl.READY
    text = " ".join(_labels(panel))
    assert "Ready" in text and "ticked" in text
    assert _entries(panel) == [s.display for s in panel._plan.steps]
    assert any("Controlled Folder Access" in t for t in _labels(panel))


def test_only_the_next_step_can_run_and_nothing_is_run_without_a_yes(made, mocker):
    panel, world, _ = made
    states = [str(b.cget("state")) for b in panel._run_buttons]
    assert states == ["normal", "disabled"]
    mocker.patch.object(panel_mod.messagebox, "askyesno", return_value=False)
    panel._run(0)
    assert not world.ran("git", "tag")


def test_a_confirmed_step_runs_and_unlocks_the_next(made, mocker, wait_for):
    panel, world, _ = made
    mocker.patch.object(panel_mod.messagebox, "askyesno", return_value=True)
    panel._run(0)
    wait_for(lambda: 0 in panel._steps_done)
    assert world.ran("git", "tag", "-a") and not world.ran("git", "push")
    assert [str(b.cget("state")) for b in panel._run_buttons] == ["disabled", "normal"]


def test_the_push_needs_the_tag_typed(made, mocker, wait_for):
    panel, world, _ = made
    panel._steps_done = {0}
    panel._sync_run_buttons()
    ask = mocker.patch.object(panel_mod.simpledialog, "askstring", return_value="wrong")
    panel._run(1)
    wait_for(lambda: "Not run" in str(panel._step_log.cget("text")) and not panel._busy)
    assert ask.called and not world.ran("git", "push")
    mocker.patch.object(panel_mod.simpledialog, "askstring", return_value=TAG)
    panel._run(1)
    wait_for(lambda: 1 in panel._steps_done)
    assert world.ran("git", "push", "origin", f"refs/tags/{TAG}")


def test_a_moved_tip_runs_nothing_and_says_why(made, mocker, wait_for):
    panel, world, _ = made
    world.answers[("git", "rev-parse", "--verify", "-q", "refs/remotes/origin/master^{commit}")] = rfl.RunResult("b" * 40 + "\n", "", 0)
    mocker.patch.object(panel_mod.messagebox, "askyesno", return_value=True)
    panel._run(0)
    wait_for(lambda: "Not run" in str(panel._step_log.cget("text")))
    assert not world.ran("git", "tag") and not panel._steps_done


def test_copy_all_puts_every_command_on_the_clipboard(made):
    panel, _, _ = made
    panel._copy_all_commands()
    assert panel.clipboard_get().splitlines() == [s.display for s in panel._plan.steps]


def test_ticks_survive_a_reopen(made, project, tk_root, wait_for):
    panel, world, _ = made
    (project / ".git").mkdir(exist_ok=True)
    world.answers[("git", "rev-parse", "--git-common-dir")] = rfl.RunResult(".git" + chr(10), "", 0)
    first = next(iter(panel._tick_vars))
    panel._tick_vars[first].set(True)
    panel._save_ticks()
    again = panel_mod.ReleaseCiPanel(tk_root, str(project), types.SimpleNamespace(git_exe="git", raw={}),
                                     rp.detect(str(project)), env=world.env())
    try:
        wait_for(lambda: again._pre is not None)
        assert again._tick_vars[first].get() is True
    finally:
        again.destroy()


def test_the_panel_has_no_way_to_publish():
    import inspect
    src = inspect.getsource(panel_mod)
    for forbidden in ("gh release edit", "draft=false", "--latest", "Publish release"):
        assert forbidden not in src
    labels = [l for l in src.splitlines() if "text=" in l and "Button" in l]
    assert not any("ublish" in l for l in labels)


# ── the Wizard picks the body ────────────────────────────────────────────

def test_tag_only_needs_the_workflow_and_the_project_file(project):
    assert rfl.is_tag_only(str(project)) is False          # workflow alone: the older mode
    (project / "release-flow.json").write_text("{}", encoding="utf-8")
    assert rfl.is_tag_only(str(project)) is True
    (project / ".github" / "workflows" / "release.yml").unlink()
    assert rfl.is_tag_only(str(project)) is False          # file alone: nothing to hand over to


def test_the_wizard_hosts_the_panel_only_for_a_tag_only_project(tk_root, project, mocker):
    import subprocess
    mocker.patch.object(rw, "messagebox")
    mocker.patch.object(panel_mod.ReleaseCiPanel, "refresh", lambda self: None)
    git = rw.shutil.which("git") if hasattr(rw, "shutil") else __import__("shutil").which("git")
    if git is None:
        pytest.skip("needs git")
    subprocess.run([git, "init", "-q", str(project)], check=True)
    cfg = types.SimpleNamespace(git_exe=git, raw={})
    (project / "release-flow.json").write_text("{}", encoding="utf-8")
    dialog = rw.ReleaseWizardDialog(tk_root, str(project), cfg)
    try:
        assert dialog._tag_only and hasattr(dialog, "_ci_panel")
        assert not hasattr(dialog, "_notes_txt")                # none of the legacy steps exist
        assert "workflow" in dialog._profile.label()
    finally:
        dialog.destroy()
