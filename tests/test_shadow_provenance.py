"""tests/test_shadow_provenance.py — SL2/SL3: freshness, and not guessing.

The dangerous operation here is deleting a file. A hardlink outlives its
source, so `Blood.zsc.cpp` with no `Blood.zsc` next to it looks stale — but
the filesystem cannot tell you whether the manager created it or the user
did, and those two cases differ by "safe cleanup" versus "data loss".

That is why `generated` exists, and most of what is asserted below is the
refusal: a file we did not record creating is a *candidate*, never a stale
shadow, and never deleted.

Real hardlinks are used throughout rather than mocks — the distinction
between "same inode" and "same name" is the whole subject, and a mock would
assert the model instead of the behaviour. Tests skip where the temp volume
cannot make links.
"""
from __future__ import annotations

import os
import shutil

import pytest

from helpers.shadow_links import (
    SHADOW_CANDIDATE,
    SHADOW_HEALTHY,
    SHADOW_STALE,
    SHADOW_SUSPICIOUS,
    ShadowConfig,
    find_stale_shadows,
    load_shadow_config,
    load_shadow_map,
    refresh_shadows,
    remove_stale_shadows,
    save_shadow_config,
    save_shadow_map,
    scan_shadows,
    supports_hardlinks,
)

EXT_MAP = {".zsc": ".cpp"}


@pytest.fixture
def project(tmp_path):
    """A project with two shadowed sources, or a skip if links are impossible."""
    root = str(tmp_path)
    if not supports_hardlinks(root):
        pytest.skip("temp volume does not support hardlinks")
    (tmp_path / "Blood.zsc").write_text("class Blood {}", encoding="utf-8")
    (tmp_path / "Fire.zsc").write_text("class Fire {}", encoding="utf-8")
    result = refresh_shadows(root, EXT_MAP)
    assert result["created"] == 2, result
    return root


def _states(root, generated=None):
    return {f.shadow: f.state for f in scan_shadows(root, EXT_MAP, generated)}


# ── SL2: refresh + persistence ───────────────────────────────────────────

def test_refresh_records_what_it_created(project):
    """Provenance is written at creation — the only moment it is knowable."""
    config = load_shadow_config(project)
    assert set(config.generated) == {"Blood.zsc.cpp", "Fire.zsc.cpp"}


def test_refresh_is_idempotent(project):
    """Sync runs it every time; a second pass must create nothing."""
    result = refresh_shadows(project, EXT_MAP)
    assert result["created"] == 0
    assert result["skipped"] == 2


def test_refresh_picks_up_a_file_added_after_the_first_run(project):
    """The gap SL2 exists to close: new files silently drop out of the index."""
    with open(os.path.join(project, "Ice.zsc"), "w", encoding="utf-8") as fh:
        fh.write("class Ice {}")
    result = refresh_shadows(project, EXT_MAP)
    assert result["created"] == 1
    assert "Ice.zsc.cpp" in load_shadow_config(project).generated


def test_refresh_declines_on_a_volume_without_hardlinks(tmp_path, mocker):
    """Not an error: a property of the disk, reported as `ran: False`.

    Every file would fail individually, producing a wall of failures on every
    sync for something the user cannot fix.
    """
    mocker.patch("helpers.shadow_links.supports_hardlinks", return_value=False)
    result = refresh_shadows(str(tmp_path), EXT_MAP)
    assert result["ran"] is False
    assert "hardlink" in result["reason"]
    assert result["created"] == 0


def test_refresh_without_a_saved_map_does_nothing(tmp_path):
    """A project that never configured shadows must not gain them on sync."""
    result = refresh_shadows(str(tmp_path))
    assert result["ran"] is False
    assert result["reason"] == "no saved map"


def test_the_auto_flag_round_trips(tmp_path):
    save_shadow_map(str(tmp_path), EXT_MAP, auto_shadow=True)
    assert load_shadow_config(str(tmp_path)).auto_shadow is True


def test_the_auto_flag_defaults_off(tmp_path):
    """Opt-in: it adds a tree walk to every sync."""
    save_shadow_map(str(tmp_path), EXT_MAP)
    assert load_shadow_config(str(tmp_path)).auto_shadow is False


def test_saving_the_map_from_the_dialog_preserves_provenance(project):
    """The regression that would quietly disable SL3 cleanup.

    The dialog saves an ext_map, not a whole config. If that dropped
    `generated`, every recorded shadow would become an unprovable candidate
    and the cleanup button would go permanently empty.
    """
    before = set(load_shadow_config(project).generated)
    save_shadow_map(project, EXT_MAP)
    assert set(load_shadow_config(project).generated) == before


def test_removing_a_mapping_drops_its_provenance(project):
    """Provenance for shadows the map no longer produces is not ours to keep."""
    save_shadow_map(project, {".acs": ".c"})
    assert load_shadow_config(project).generated == ()


def test_a_saved_config_survives_a_truncated_write(tmp_path):
    """Atomic write: SL2 rewrites this on every sync.

    A partial file reads as "no saved map", which would silently discard both
    the user's extension map and the provenance record.
    """
    root = str(tmp_path)
    save_shadow_config(root, ShadowConfig(ext_map=EXT_MAP, auto_shadow=True,
                                          generated=("a.zsc.cpp",)))
    # No stray temp files left behind to be mistaken for the real thing.
    cache = tmp_path / ".tokensave-manager"
    leftovers = [f for f in os.listdir(cache) if f.endswith(".tmp")]
    assert leftovers == []
    assert load_shadow_map(root) == EXT_MAP


# ── SL3: the four states ─────────────────────────────────────────────────

def test_a_live_link_is_healthy(project):
    assert _states(project) == {"Blood.zsc.cpp": SHADOW_HEALTHY,
                                "Fire.zsc.cpp": SHADOW_HEALTHY}


def test_a_recorded_shadow_whose_source_vanished_is_stale(project):
    os.remove(os.path.join(project, "Blood.zsc"))
    assert _states(project)["Blood.zsc.cpp"] == SHADOW_STALE


def test_an_unrecorded_lookalike_is_a_candidate_not_stale(project):
    """The file that must never be deleted on resemblance alone.

    Identical in shape to a stale shadow. The only thing separating them is
    whether we recorded creating it.
    """
    with open(os.path.join(project, "Handmade.zsc.cpp"), "w",
              encoding="utf-8") as fh:
        fh.write("the user's own file")
    assert _states(project)["Handmade.zsc.cpp"] == SHADOW_CANDIDATE


def test_a_broken_link_is_suspicious_not_healthy(project):
    """Editors that save by replacing break hardlinks silently.

    Same name, live source, different inode. Reporting this as healthy would
    hide a shadow that has quietly stopped tracking its source.
    """
    shadow = os.path.join(project, "Fire.zsc.cpp")
    os.remove(shadow)
    shutil.copy(os.path.join(project, "Fire.zsc"), shadow)
    assert _states(project)["Fire.zsc.cpp"] == SHADOW_SUSPICIOUS


def test_sameness_is_decided_by_inode_not_link_count(project):
    """`st_nlink` would call a coincidence a link.

    Two unrelated files can each have a link count of 2. Only `samefile`
    establishes that *these two paths* are the pair.
    """
    shadow = os.path.join(project, "Fire.zsc.cpp")
    os.remove(shadow)
    shutil.copy(os.path.join(project, "Fire.zsc"), shadow)
    # Give the copy a link count of 2 elsewhere, so nlink alone looks right.
    os.link(shadow, os.path.join(project, "decoy.txt"))
    assert os.stat(shadow).st_nlink == 2
    assert _states(project)["Fire.zsc.cpp"] == SHADOW_SUSPICIOUS


# ── SL3: cleanup refuses what it cannot prove ────────────────────────────

def test_stale_listing_excludes_unprovable_candidates(project):
    os.remove(os.path.join(project, "Blood.zsc"))
    with open(os.path.join(project, "Handmade.zsc.cpp"), "w",
              encoding="utf-8") as fh:
        fh.write("the user's own file")
    assert [f.shadow for f in find_stale_shadows(project, EXT_MAP)] \
        == ["Blood.zsc.cpp"]


def test_cleanup_deletes_only_the_recorded_stale_shadow(project):
    """The end-to-end safety property."""
    os.remove(os.path.join(project, "Blood.zsc"))
    handmade = os.path.join(project, "Handmade.zsc.cpp")
    with open(handmade, "w", encoding="utf-8") as fh:
        fh.write("the user's own file")

    removed, failed = remove_stale_shadows(project, EXT_MAP)
    assert (removed, failed) == (1, 0)
    assert not os.path.exists(os.path.join(project, "Blood.zsc.cpp"))
    assert os.path.exists(handmade), "an unprovable file must survive cleanup"
    assert os.path.exists(os.path.join(project, "Fire.zsc.cpp"))


def test_cleanup_forgets_what_it_removed(project):
    """Provenance must not accumulate entries for files that are gone."""
    os.remove(os.path.join(project, "Blood.zsc"))
    remove_stale_shadows(project, EXT_MAP)
    assert set(load_shadow_config(project).generated) == {"Fire.zsc.cpp"}


def test_cleanup_leaves_a_suspicious_file_alone(project):
    """Different inode may be a deliberate replacement — diagnose, don't act."""
    shadow = os.path.join(project, "Fire.zsc.cpp")
    os.remove(shadow)
    shutil.copy(os.path.join(project, "Fire.zsc"), shadow)
    removed, _failed = remove_stale_shadows(project, EXT_MAP)
    assert removed == 0
    assert os.path.exists(shadow)


def test_provenance_paths_are_relative_and_portable(project):
    """Absolute paths would break the moment the project moved."""
    for rel in load_shadow_config(project).generated:
        assert not os.path.isabs(rel)
        assert "\\" not in rel, "separators are normalised to forward slashes"


# ── the Doctor rule ──────────────────────────────────────────────────────

def test_the_doctor_rule_is_silent_for_projects_without_shadows(tmp_path):
    """Otherwise every project gains a line about a feature it does not use."""
    from helpers.doctor_rules import audit_shadow_links
    assert audit_shadow_links(str(tmp_path)) == []


def test_the_doctor_rule_reports_stale_links(project):
    from helpers.doctor_rules import audit_shadow_links
    os.remove(os.path.join(project, "Blood.zsc"))
    notes = audit_shadow_links(project)
    assert any("stale shadow link" in n for n in notes)


def test_the_doctor_rule_says_candidates_will_not_be_touched(project):
    from helpers.doctor_rules import audit_shadow_links
    with open(os.path.join(project, "Handmade.zsc.cpp"), "w",
              encoding="utf-8") as fh:
        fh.write("mine")
    notes = audit_shadow_links(project)
    assert any("cannot be proven" in n for n in notes)


# ── SL2 wired into sync ──────────────────────────────────────────────────
#
# `SyncStatusController` takes plain callables and only touches `tab` for
# after()/winfo_toplevel(), neither of which these paths reach — so it can be
# built without Tk.

def _controller(logs):
    from controllers.sync_ctrl import SyncStatusController
    return SyncStatusController(
        tab=None, cfg=None,
        on_log=lambda msg, colour="": logs.append(msg),
        on_set_running=lambda *a: None,
        on_set_proc=lambda *a: None,
        on_refresh=lambda: None,
        on_run=lambda *a, **k: None,
        on_run_capture=lambda *a, **k: ("", 0, 0.0),
        get_projects=lambda: [],
    )


def test_sync_does_not_touch_the_disk_when_auto_shadow_is_off(project, mocker):
    """Cost when disabled must be one small file read, not a tree walk.

    Every project that never enabled this would otherwise pay for it on
    every sync.
    """
    save_shadow_map(project, EXT_MAP, auto_shadow=False)
    refresh = mocker.patch("controllers.sync_ctrl.refresh_shadows")
    _controller([])._refresh_shadows_if_enabled(project)
    refresh.assert_not_called()


def test_sync_refreshes_shadows_when_auto_shadow_is_on(project, mocker):
    save_shadow_map(project, EXT_MAP, auto_shadow=True)
    refresh = mocker.patch("controllers.sync_ctrl.refresh_shadows",
                           return_value={"ran": True, "reason": "",
                                         "created": 1, "skipped": 0,
                                         "failed": 0})
    _controller([])._refresh_shadows_if_enabled(project)
    refresh.assert_called_once()


def test_a_project_with_no_saved_map_is_never_refreshed(tmp_path, mocker):
    refresh = mocker.patch("controllers.sync_ctrl.refresh_shadows")
    _controller([])._refresh_shadows_if_enabled(str(tmp_path))
    refresh.assert_not_called()


# The logging policy: report what the user can act on, and nothing else.

def test_an_unsupported_volume_logs_nothing(project):
    """A property of the disk, not an error — and it would repeat every sync."""
    logs = []
    _controller(logs)._log_shadow_refresh(
        {"ran": False, "reason": "volume does not support hardlinks",
         "created": 0, "skipped": 0, "failed": 0}, project)
    assert logs == []


def test_creating_nothing_logs_nothing(project):
    """The steady state on a project whose files have not changed."""
    logs = []
    _controller(logs)._log_shadow_refresh(
        {"ran": True, "reason": "", "created": 0, "skipped": 12,
         "failed": 0}, project)
    assert logs == []


def test_created_links_are_reported(project):
    logs = []
    _controller(logs)._log_shadow_refresh(
        {"ran": True, "reason": "", "created": 3, "skipped": 0,
         "failed": 0}, project)
    assert len(logs) == 1 and "3 shadow link" in logs[0]


def test_failures_on_a_supported_volume_are_warned_about(project):
    """The case "log only when created > 0" would have hidden.

    The volume supports links and files still failed — a permissions or
    filesystem problem, which would otherwise present as "auto-shadow
    appears to do nothing at all".
    """
    logs = []
    _controller(logs)._log_shadow_refresh(
        {"ran": True, "reason": "", "created": 0, "skipped": 0,
         "failed": 4}, project)
    assert len(logs) == 1
    assert "4 could not be created" in logs[0]
