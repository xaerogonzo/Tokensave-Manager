"""tests/test_tokensave_config_writer.py — turning strict_tree on, safely.

`strict_tree` is what makes a wrong-tree query *error* instead of returning a
plausible answer about a checkout you are not in. Roadmap-9 shipped the reader
and stopped, because the manager had no way to write this file — so the Doctor
could say "you should enable this" and offer nothing. Measured before the
writer existed: enabled in **zero** of sixteen indexed projects.

The file belongs to tokensave, not to the manager, which sets the shape of
every test here: preserve what we do not understand, never invent the file,
and never leave it half-written — a truncated `config.json` does not read as
"broken", it reads as "not a tokensave project", so the damage would be
invisible.
"""
from __future__ import annotations

import json
import os

from helpers.tokensave_config import (
    DISABLED,
    ENABLED,
    MALFORMED,
    read_strict_tree,
    set_strict_tree,
)


def _project(tmp_path, config=None, *, with_marker=True):
    root = tmp_path / "proj"
    if with_marker:
        (root / ".tokensave").mkdir(parents=True)
        if config is not None:
            (root / ".tokensave" / "config.json").write_text(
                json.dumps(config, indent=2), encoding="utf-8")
    else:
        root.mkdir(parents=True)
    return str(root)


def _config(root):
    with open(os.path.join(root, ".tokensave", "config.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


# ── the happy path ───────────────────────────────────────────────────────

def test_enabling_flips_the_setting(tmp_path):
    root = _project(tmp_path, {"strict_tree": False})
    ok, detail = set_strict_tree(root, True)
    assert ok, detail
    assert read_strict_tree(root).verdict == ENABLED


def test_disabling_works_too(tmp_path):
    """Reversible: an over-strict setup must not need hand-editing to undo."""
    root = _project(tmp_path, {"strict_tree": True})
    assert set_strict_tree(root, False)[0] is True
    assert read_strict_tree(root).verdict == DISABLED


def test_adding_the_key_where_it_was_absent(tmp_path):
    """A config written by a tokensave older than v7.10.0 has no such key."""
    root = _project(tmp_path, {"other": 1})
    assert set_strict_tree(root, True)[0] is True
    assert read_strict_tree(root).verdict == ENABLED


def test_setting_it_twice_is_a_no_op_that_still_reports_success(tmp_path):
    root = _project(tmp_path, {"strict_tree": True})
    ok, detail = set_strict_tree(root, True)
    assert ok is True
    assert "already" in detail


# ── the file is tokensave's, not ours ────────────────────────────────────

def test_unrecognised_keys_survive(tmp_path):
    """Anything we do not recognise is a key a newer tokensave added.

    Rewriting the file from its known keys would silently drop them, and the
    loss would only surface later as tokensave behaving oddly.
    """
    root = _project(tmp_path, {
        "strict_tree": False,
        "future_key": [1, 2, 3],
        "nested": {"deep": {"value": True}},
    })
    set_strict_tree(root, True)
    after = _config(root)
    assert after["future_key"] == [1, 2, 3]
    assert after["nested"] == {"deep": {"value": True}}
    assert after["strict_tree"] is True


def test_the_value_written_is_a_real_boolean(tmp_path):
    """The reader treats a non-bool as MALFORMED, so a truthy int would break
    the very check this is meant to enable."""
    root = _project(tmp_path, {"strict_tree": False})
    set_strict_tree(root, True)
    assert _config(root)["strict_tree"] is True


# ── never invent a config ────────────────────────────────────────────────

def test_a_project_with_no_tokensave_directory_is_refused(tmp_path):
    """It is probably not a tokensave project at all.

    Creating the file would have the manager assert something it has not
    established, and leave a config behind for a project that has no index.
    """
    root = _project(tmp_path, with_marker=False)
    ok, detail = set_strict_tree(root, True)
    assert ok is False
    assert "tokensave init" in detail
    assert not os.path.exists(os.path.join(root, ".tokensave"))


def test_a_missing_config_file_is_refused_not_created(tmp_path):
    root = _project(tmp_path, config=None)
    ok, detail = set_strict_tree(root, True)
    assert ok is False
    assert "does not exist" in detail
    assert not os.path.exists(
        os.path.join(root, ".tokensave", "config.json"))


# ── never destroy what cannot be parsed ──────────────────────────────────

def test_an_unparseable_config_is_refused_and_left_alone(tmp_path):
    """Rewriting it would discard contents we were unable to read.

    "Fix or delete it" is the honest instruction: the manager cannot know
    what the file was trying to say.
    """
    root = _project(tmp_path, {"placeholder": True})
    path = os.path.join(root, ".tokensave", "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")

    ok, detail = set_strict_tree(root, True)

    assert ok is False
    assert "not valid JSON" in detail
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == "{not valid json"
    assert read_strict_tree(root).verdict == MALFORMED


def test_a_config_that_is_not_an_object_is_refused(tmp_path):
    root = _project(tmp_path, ["a", "list"])
    ok, detail = set_strict_tree(root, True)
    assert ok is False
    assert "JSON object" in detail


# ── atomicity ────────────────────────────────────────────────────────────

def test_no_temp_file_is_left_behind(tmp_path):
    """A stray .tmp beside config.json is litter in the user's repo."""
    root = _project(tmp_path, {"strict_tree": False})
    set_strict_tree(root, True)
    leftovers = [f for f in os.listdir(os.path.join(root, ".tokensave"))
                 if f.endswith(".tmp")]
    assert leftovers == []


def test_a_failed_write_leaves_the_original_intact(tmp_path, mocker):
    """The reason this write is atomic.

    A half-written config.json does not report as damaged — it reads as "not
    an initialised tokensave project", so the failure would vanish rather
    than announce itself.
    """
    import helpers.tokensave_config as tc
    root = _project(tmp_path, {"strict_tree": False, "keep": "me"})
    mocker.patch.object(tc.os, "replace", side_effect=OSError("disk full"))

    ok, detail = set_strict_tree(root, True)

    assert ok is False
    assert "could not write" in detail
    assert _config(root) == {"strict_tree": False, "keep": "me"}
    assert read_strict_tree(root).verdict == DISABLED


def test_a_failed_write_cleans_up_its_temp_file(tmp_path, mocker):
    import helpers.tokensave_config as tc
    root = _project(tmp_path, {"strict_tree": False})
    mocker.patch.object(tc.os, "replace", side_effect=OSError("disk full"))

    set_strict_tree(root, True)

    leftovers = [f for f in os.listdir(os.path.join(root, ".tokensave"))
                 if f.endswith(".tmp")]
    assert leftovers == []


# ── the reader's contract still holds after a write ──────────────────────

def test_an_unreadable_state_is_never_reported_as_disabled(tmp_path):
    """The distinction the reader was built around, re-asserted here.

    "We could not determine the setting" and "the setting is off" lead to
    different actions, and conflating them is how a project silently stays
    unprotected.
    """
    root = _project(tmp_path, config=None)
    assert read_strict_tree(root).verdict != DISABLED
    assert read_strict_tree(root).is_known is False
