"""tests/test_instructions_split_dialog.py — the split proposal (Tk-marked).

Two of these are the load-bearing pair the plan named, and they guard the
separation the whole feature rests on:

* **generating the proposal writes nothing** — the dialog reads, computes and
  renders a ~1 MB file, and the project on disk is byte-identical afterwards;
* **applying writes exactly the files the proposal represented** — no more
  files, and no content computed after the click.

The rest cover the refusals, because a refusal that renders as a greyed button
with no sentence beside it is the same defect as a red row with no explanation.
"""
from __future__ import annotations

import os

import pytest

# Skip the module where Tk is unavailable (SSH without DISPLAY).
tk = pytest.importorskip("tkinter")

from dialogs import instructions_split as split_dialog
from dialogs.instructions_split import SplitProposalDialog
from helpers.instructions_split import DEFAULT_TARGET

pytestmark = pytest.mark.tk


# ── fixtures ─────────────────────────────────────────────────────────────────

def _doc(sections, preamble="@BASIC_INSTRUCTIONS.md"):
    out = ["# Demo — notes", "", preamble, ""]
    for title, body in sections:
        out += ["## %s" % title, "", body, ""]
    return "\n".join(out)


def _project(tmp_path, text):
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8", newline="")
    return str(tmp_path)


def _tree(root):
    """Every file's relative path and exact bytes.

    Content rather than mtime: a filesystem whose timestamps are coarse would
    make an mtime comparison pass over a real rewrite.
    """
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                out[os.path.relpath(full, root)] = handle.read()
    return out


def _open(tk_root, root, wait_for, name="Demo"):
    dialog = SplitProposalDialog(tk_root, root, name)
    wait_for(lambda: bool(dialog._sections), timeout_s=3.0)
    return dialog


@pytest.fixture
def loggy(tmp_path):
    """A project whose tail is a log: three small sections, then two big."""
    text = _doc([("Commands", "x" * 200),
                 ("File map", "y" * 200),
                 ("Lesson one", "a" * 40_000),
                 ("Lesson two", "b" * 40_000)])
    return _project(tmp_path, text), text


# ── the load-bearing pair ────────────────────────────────────────────────────

def test_building_the_proposal_writes_nothing(tk_root, loggy, wait_for):
    """Read, compute, re-select, preview — and the project is untouched.

    "Nothing is written until the complete transformation has been shown" is
    the rule the helper's docstring states; this is the half of it that lives
    on the Tk side, where a stray write would be easiest to add by accident.
    """
    root, _text = loggy
    before = _tree(root)

    dialog = _open(tk_root, root, wait_for)
    assert _tree(root) == before, "rendering the proposal touched the project"

    # Re-select every section, then none, then preview: still nothing.
    for var in dialog._vars.values():
        var.set(True)
    dialog._recompute()
    for var in dialog._vars.values():
        var.set(False)
    dialog._recompute()
    dialog._reset()
    dialog._preview()

    assert _tree(root) == before, "interacting with the proposal wrote to disk"
    assert not os.path.exists(os.path.join(root, DEFAULT_TARGET))


def test_applying_writes_exactly_the_files_the_proposal_showed(
        tk_root, loggy, wait_for, mocker):
    """No extra files, and the bytes are the ones that were on screen."""
    root, _text = loggy
    dialog = _open(tk_root, root, wait_for)
    mocker.patch.object(split_dialog.messagebox, "askyesno", return_value=True)
    mocker.patch.object(split_dialog.messagebox, "showinfo")

    plan = dialog._plan                      # exactly what the dialog showed
    before = set(_tree(root))

    dialog._apply()
    wait_for(lambda: os.path.exists(os.path.join(root, DEFAULT_TARGET)),
             timeout_s=3.0)

    after = _tree(root)
    assert set(after) - before == {os.path.normpath(DEFAULT_TARGET)}, \
        "apply created a file the proposal never represented"

    with open(os.path.join(root, "CLAUDE.md"), encoding="utf-8",
              newline="") as handle:
        assert handle.read() == plan.new_source
    with open(os.path.join(root, DEFAULT_TARGET), encoding="utf-8",
              newline="") as handle:
        assert handle.read() == plan.new_target


# ── refusals, each with a sentence ───────────────────────────────────────────

def test_a_ticked_chain_section_disables_apply_and_says_why(
        tk_root, tmp_path, wait_for):
    """Moving the @include would break what the file exists to do."""
    root = _project(tmp_path, _doc([("Head", "x" * 200),
                                    ("Carries", "@shared/extra.md"),
                                    ("Log", "z" * 60_000)]))
    dialog = _open(tk_root, root, wait_for)

    for index, var in dialog._vars.items():
        var.set(index >= 1)
    dialog._recompute()

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert "@include" in dialog._reason["text"]


def test_selecting_nothing_refuses_rather_than_pretending(
        tk_root, loggy, wait_for):
    root, _text = loggy
    dialog = _open(tk_root, root, wait_for)
    for var in dialog._vars.values():
        var.set(False)
    dialog._recompute()

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert dialog._reason["text"].strip()


def test_an_existing_target_is_refused_before_the_click(
        tk_root, tmp_path, wait_for):
    """Stated up front, not discovered after Apply."""
    root = _project(tmp_path, _doc([("Head", "x" * 200),
                                    ("Log", "z" * 60_000)]))
    os.makedirs(os.path.join(root, "docs"))
    with open(os.path.join(root, DEFAULT_TARGET), "w", encoding="utf-8") as fh:
        fh.write("mine")

    dialog = _open(tk_root, root, wait_for)

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert "already exists" in dialog._reason["text"]


def test_a_source_that_changed_after_the_preview_writes_nothing(
        tk_root, loggy, wait_for, mocker):
    """The guard that matters: a live session appended while this was open."""
    root, text = loggy
    dialog = _open(tk_root, root, wait_for)
    mocker.patch.object(split_dialog.messagebox, "askyesno", return_value=True)
    warned = mocker.patch.object(split_dialog.messagebox, "showwarning")

    with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8",
              newline="") as handle:
        handle.write(text + "\n## Appended by a live session\n\nnew\n")
    after_append = _tree(root)

    dialog._apply()
    wait_for(lambda: warned.called, timeout_s=3.0)

    assert _tree(root) == after_append, "a stale plan overwrote the file"
    assert not os.path.exists(os.path.join(root, DEFAULT_TARGET))
    assert "changed since" in dialog._reason["text"]


# ── where the offer appears ──────────────────────────────────────────────────

def test_the_split_button_appears_only_on_oversized_rows(tk_root, mocker,
                                                         mock_config,
                                                         wait_for):
    """Per project, and only where the weight actually justifies the offer."""
    from dialogs import instructions_overview
    from helpers.instructions_posture import (
        FleetInstructions, ProjectInstructions, REACH_RESOLVED,
    )

    big = ProjectInstructions(root="d:/big", display_root="D:/big", name="Big",
                              reach=REACH_RESOLVED, weight_bytes=300_000)
    small = ProjectInstructions(root="d:/small", display_root="D:/small",
                                name="Small", reach=REACH_RESOLVED,
                                weight_bytes=4_000)
    mocker.patch.object(instructions_overview, "read_posture",
                        return_value=FleetInstructions(projects=(big, small)))

    dialog = instructions_overview.InstructionsDialog(tk_root, mock_config)
    # Not "any label exists": the placeholder "Scanning..." is a label, so that
    # predicate is satisfied before the rows the test is about are built.
    wait_for(lambda: dialog._fleet is not None, timeout_s=3.0)

    labels = _labels(dialog._body)
    assert labels.count("Split\u2026") == 1, labels


def test_the_button_actually_opens_the_proposal(tk_root, mocker, mock_config,
                                                loggy, wait_for):
    """The command runs only on click, so a bad argument there would ship.

    Not a spy on the constructor: the wiring passes `display_root` and two
    keywords, and a wrong name in any of them raises at click time and nowhere
    earlier. This drives the real path into a real dialog.
    """
    from dialogs import instructions_overview
    from helpers.instructions_posture import (
        FleetInstructions, ProjectInstructions, REACH_RESOLVED,
    )

    root, _text = loggy
    big = ProjectInstructions(root=root.lower(), display_root=root,
                              name="Loggy", reach=REACH_RESOLVED,
                              weight_bytes=300_000)
    mocker.patch.object(instructions_overview, "read_posture",
                        return_value=FleetInstructions(projects=(big,)))

    fleet = instructions_overview.InstructionsDialog(tk_root, mock_config)
    wait_for(lambda: fleet._fleet is not None, timeout_s=3.0)
    fleet._split_one(big)

    opened = [w for w in fleet.winfo_children()
              if isinstance(w, SplitProposalDialog)]
    assert len(opened) == 1, "the row button did not open the proposal"
    wait_for(lambda: bool(opened[0]._sections), timeout_s=3.0)
    assert opened[0]._plan is not None


def _labels(widget):
    """Every button label under a widget, depth-first."""
    out = []
    for child in widget.winfo_children():
        try:
            out.append(str(child["text"]))
        except tk.TclError:
            pass
        out.extend(_labels(child))
    return out
