"""tests/test_mcp_posture_identity.py — one directory is one project.

`independent` and `covered` are aggregate properties: they are computed over
the whole project list, so a directory that appears twice is not a cosmetic
duplicate — it is a wrong denominator. "2 projects unserved" when one project
is unserved sends the user looking for a project that does not exist, and the
same duplication would let one row read `EXPLICIT` while its twin reads `NONE`.

This is not hypothetical here. `~/.claude.json` on the author's machine held 55
project keys for far fewer projects, differing only by path separator and case
— `normalize_project_key` exists precisely because that is the normal state,
and `canonical_launch_dir` documents this manager's own status checks as one of
the things that minted them. Search roots can overlap the same way.

So `read_posture` keys by `normalize_project_key` and keeps the first spelling
for display. Nothing new is invented: reusing the module that already owns
Windows path comparison is the point, and a third comparison would be a third
answer.
"""
from __future__ import annotations

import os


from helpers import mcp_posture as posture
from helpers.mcp_posture import TIER_NONE


class _Cfg:
    def __init__(self, roots=()):
        self.raw = {}
        self.search_roots = list(roots)


def _indexed(tmp_path, name):
    """A directory that looks like a real tokensave project."""
    root = tmp_path / name
    (root / ".tokensave").mkdir(parents=True)
    return str(root)


def _no_globals(mocker):
    """Pin the machine-wide facts so only the project list varies."""
    mocker.patch("helpers.mcp_desktop.desktop_entry_present",
                 return_value=False)
    mocker.patch("helpers.mcp_projects.read_claude_projects", return_value={})


def test_two_spellings_of_one_directory_yield_one_project(tmp_path, mocker):
    _no_globals(mocker)
    root = _indexed(tmp_path, "Fortuna Lab")
    both_spellings = [
        {"name": "Fortuna Lab", "path": root.replace("/", os.sep)},
        {"name": "Fortuna Lab", "path": root.replace(os.sep, "/")},
    ]
    mocker.patch.object(posture, "_discover", return_value=both_spellings)

    got = posture.read_posture(_Cfg())

    assert len(got.projects) == 1, [p.display_root for p in got.projects]


def test_a_duplicate_cannot_inflate_the_unserved_count(tmp_path, mocker):
    """The aggregate the duplicate would corrupt. With no fallback, an unbound
    project is genuinely unserved — but it is ONE project, and the headline
    names them."""
    _no_globals(mocker)
    root = _indexed(tmp_path, "CleanForge")
    mocker.patch.object(posture, "_discover", return_value=[
        {"name": "CleanForge", "path": root},
        {"name": "CleanForge", "path": root + os.sep},
        {"name": "CleanForge", "path": os.path.join(root, ".", "")},
    ])

    got = posture.read_posture(_Cfg())

    assert len(got.projects) == 1
    assert got.projects[0].tier == TIER_NONE


def test_the_key_is_canonical_and_the_display_path_is_not(tmp_path, mocker):
    """Both are needed. Aggregating by the display path re-creates the bug;
    showing the canonical key would print a lower-cased path the user does not
    recognise as theirs."""
    _no_globals(mocker)
    root = _indexed(tmp_path, "PolyScour")
    spelled = root.replace("/", os.sep)
    mocker.patch.object(posture, "_discover",
                        return_value=[{"name": "PolyScour", "path": spelled}])

    got = posture.read_posture(_Cfg())
    project = got.projects[0]

    from helpers.mcp_projects import normalize_project_key
    assert project.root == normalize_project_key(spelled)
    assert project.display_root == spelled


def test_a_project_without_an_index_is_not_a_project_here(tmp_path, mocker):
    """Without `.tokensave/` there is nothing to serve, and the classifier
    (correctly) refuses project scope for such a path — so a row for it would
    fall through to the GLOBAL wrapper proposal and offer to write this
    machine's absolute paths into a shared project file."""
    _no_globals(mocker)
    indexed = _indexed(tmp_path, "Indexed")
    bare = tmp_path / "JustAFolder"
    bare.mkdir()
    mocker.patch.object(posture, "_discover", return_value=[
        {"name": "Indexed", "path": indexed},
        {"name": "JustAFolder", "path": str(bare)},
    ])

    got = posture.read_posture(_Cfg())

    assert [p.name for p in got.projects] == ["Indexed"]


def test_discovery_failure_is_no_projects_rather_than_an_exception(mocker):
    """The overview is the first thing the MCP dialog renders; a discovery
    error must not take down the dialog opened to diagnose it."""
    _no_globals(mocker)
    mocker.patch("helpers.project_discovery.find_projects",
                 side_effect=OSError("unreachable root"))

    got = posture.read_posture(_Cfg(roots=[r"Z:\gone"]))

    assert got.projects == ()
