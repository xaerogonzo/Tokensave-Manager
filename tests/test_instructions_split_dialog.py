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


def _tick(dialog, *indices):
    """Tick sections by index. Nothing is ticked on open, by design."""
    for index, var in dialog._vars.items():
        var.set(index in indices)
    dialog._recompute()


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
    dialog._suggest()
    dialog._preview()

    assert _tree(root) == before, "interacting with the proposal wrote to disk"
    assert not os.path.exists(os.path.join(root, DEFAULT_TARGET))


def test_applying_writes_exactly_the_files_the_proposal_showed(
        tk_root, loggy, wait_for, mocker):
    """No extra files, and the bytes are the ones that were on screen."""
    root, _text = loggy
    dialog = _open(tk_root, root, wait_for)
    _tick(dialog, 2, 3)
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

    _tick(dialog, 1, 2)

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert "@include" in dialog._reason["text"]


def test_nothing_is_ticked_on_open(tk_root, loggy, wait_for):
    """The decision this dialog exists to put in front of a person.

    The byte budget guesses a TAIL, and that guess was wrong on three of the
    four real files it met -- once selecting an operational section at the end
    of the document. A pre-ticked box reads as a recommendation, so there is
    none; `Suggest from budget` offers the guess to anyone who asks for it.
    """
    root, _text = loggy
    dialog = _open(tk_root, root, wait_for)

    assert not dialog._selected(), "the dialog pre-selected sections"
    assert str(dialog._apply_btn["state"]) == "disabled"
    # Guidance, not a refusal: an empty list on open is the starting state.
    assert "Tick the sections" in dialog._reason["text"]
    assert "Cannot apply" not in dialog._reason["text"]

    dialog._suggest()
    assert dialog._selected(), "the budget offered nothing when asked"


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
    _tick(dialog, 2, 3)
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


# ── the section tree on screen ───────────────────────────────────────────────

def _doc3(spec, preamble="@BASIC_INSTRUCTIONS.md"):
    """`spec` is [(level, title, body), ...]."""
    out = ["# Demo — notes", "", preamble, ""]
    for level, title, body in spec:
        out += ["%s %s" % ("#" * level, title), "", body, ""]
    return "\n".join(out)


@pytest.fixture
def nested(tmp_path):
    """One project's real shape: an operational section holding lessons."""
    text = _doc3([(2, "Commands", "x" * 200),
                  (2, "Verification standard", "y" * 400),
                  (3, "Lesson A", "a" * 9_000),
                  (3, "Lesson B", "b" * 9_000),
                  (2, "Packaging traps", "z" * 200)])
    return _project(tmp_path, text), text


def test_a_subsection_gets_its_own_row(tk_root, nested, wait_for):
    """`##` is not the unit of a lesson, and the list has to show that."""
    root, _text = nested
    dialog = _open(tk_root, root, wait_for)

    titles = [s.title for s in dialog._sections]
    assert "Lesson A" in titles and "Lesson B" in titles
    levels = {s.title: s.level for s in dialog._sections}
    assert levels["Verification standard"] == 2
    assert levels["Lesson A"] == 3


def test_ticking_a_parent_ticks_and_locks_its_children(tk_root, nested,
                                                       wait_for):
    """The screen must not disagree with what is about to be written.

    `compute_split` expands a ticked parent to its descendants. If the rows did
    not mirror that, the preview would under-report what moves.
    """
    root, _text = nested
    dialog = _open(tk_root, root, wait_for)
    parent = next(s for s in dialog._sections
                  if s.title == "Verification standard")
    kids = [s for s in dialog._sections if s.parent == parent.index]
    assert len(kids) == 2

    dialog._vars[parent.index].set(True)
    dialog._on_tick(parent.index)

    for kid in kids:
        assert dialog._vars[kid.index].get() is True
        assert str(dialog._boxes[kid.index]["state"]) == "disabled"
    assert {s.title for s in dialog._plan.moved} == {
        "Verification standard", "Lesson A", "Lesson B"}

    # ...and unticking releases them again.
    dialog._vars[parent.index].set(False)
    dialog._on_tick(parent.index)
    for kid in kids:
        assert str(dialog._boxes[kid.index]["state"]) == "normal"


def _row_texts(dialog, title):
    """Every label in the rendered row whose title matches."""
    for row in dialog._body.winfo_children():
        texts = []
        for child in row.winfo_children():
            try:
                texts.append(str(child["text"]))
            except tk.TclError:
                pass
        if any(x.startswith(title) for x in texts):
            return texts
    return []


def test_a_parent_row_shows_what_a_reader_actually_pays(tk_root, nested,
                                                        wait_for):
    """Read off the ROW, not the model.

    A parent's own body is 400 B; the section costs 18 KB because of its two
    subsections. Showing `size` there would put "400 B" beside a section whose
    real price is forty times that, and asserting on the dataclass instead of
    the widget would not notice.
    """
    root, _text = nested
    dialog = _open(tk_root, root, wait_for)
    parent = next(s for s in dialog._sections
                  if s.title == "Verification standard")
    assert parent.size < 1_000 < parent.total_size

    texts = _row_texts(dialog, "Verification standard")
    assert texts, "the row was not rendered"
    sizes = [x for x in texts if x.endswith(" B")]
    assert sizes == ["%s B" % f"{parent.total_size:,}"], sizes


def test_a_second_split_adds_to_the_target_instead_of_refusing(
        tk_root, nested, wait_for, mocker):
    """Granularity is what makes a second split worth doing at all."""
    root, _text = nested
    first = _open(tk_root, root, wait_for)
    parent = next(s for s in first._sections
                  if s.title == "Verification standard")
    _tick(first, parent.index)
    mocker.patch.object(split_dialog.messagebox, "askyesno", return_value=True)
    mocker.patch.object(split_dialog.messagebox, "showinfo")
    first._apply()
    wait_for(lambda: os.path.exists(os.path.join(root, DEFAULT_TARGET)),
             timeout_s=3.0)

    # Re-open on the file the first split produced.
    second = _open(tk_root, root, wait_for)
    commands = next(s for s in second._sections if s.title == "Commands")
    _tick(second, commands.index)

    assert second._plan.appending is True
    assert "adds to" in second._totals["text"]
    assert str(second._apply_btn["state"]) == "normal"
    assert "Cannot apply" not in second._reason["text"]


def test_a_target_someone_else_wrote_is_refused_on_open(tk_root, tmp_path,
                                                        wait_for):
    """Appending to a person's own notes is still a surprise."""
    root = _project(tmp_path, _doc3([(2, "Head", "x" * 200),
                                     (2, "Log", "z" * 60_000)]))
    os.makedirs(os.path.join(root, "docs"))
    with open(os.path.join(root, DEFAULT_TARGET), "w", encoding="utf-8") as fh:
        fh.write("# My own notes\n\nnothing to do with the split\n")

    dialog = _open(tk_root, root, wait_for)

    assert str(dialog._apply_btn["state"]) == "disabled"
    assert "not written by this tool" in dialog._reason["text"]


# ── two defects the driven check found, and the unit tests had not ───────────

def _split_once(tk_root, root, wait_for, mocker, title):
    """Apply one split, so the next dialog opens on an already-split file."""
    dialog = _open(tk_root, root, wait_for)
    section = next(s for s in dialog._sections if s.title == title)
    _tick(dialog, section.index)
    mocker.patch.object(split_dialog.messagebox, "askyesno", return_value=True)
    mocker.patch.object(split_dialog.messagebox, "showinfo")
    dialog._apply()
    wait_for(lambda: os.path.exists(os.path.join(root, DEFAULT_TARGET)),
             timeout_s=3.0)


def test_the_footer_says_adds_to_before_anything_is_ticked(
        tk_root, nested, wait_for, mocker):
    """`plan.appending` is False whenever nothing is ticked.

    The plan short-circuits before computing it, so reading the verb off the
    plan printed "creates docs/LESSONS.md" next to a file that plainly existed.
    The verb is a fact about the project, not about the selection -- which is
    why this asserts on the OPENING state.
    """
    root, _text = nested
    _split_once(tk_root, root, wait_for, mocker, "Verification standard")

    second = _open(tk_root, root, wait_for)

    assert not second._selected(), "nothing should be ticked on open"
    assert "adds to" in second._totals["text"], second._totals["text"]
    assert "creates" not in second._totals["text"]


def test_the_previous_index_cannot_be_moved(tk_root, nested, wait_for, mocker):
    """Ticking it would lose the index AND break the next merge.

    The index is rewritten in place on every split. Sending it to the target
    removes it from the file that loads it, and leaves the next split with
    nothing to merge into -- so that one writes a second index beside it.
    """
    root, _text = nested
    _split_once(tk_root, root, wait_for, mocker, "Verification standard")

    second = _open(tk_root, root, wait_for)
    index_row = next(s for s in second._sections
                     if s.title == "Lessons (moved out of this file)")

    assert str(second._boxes[index_row.index]["state"]) == "disabled"
