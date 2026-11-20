"""helpers/release_postcheck.py: judging a drafted release, and the manual checklist.

Same scripted-runner approach as test_release_flow. The point of most tests is the negative:
a missing, empty or unlisted file is a failure, an unreachable GitHub is `unknown` (never a
tick), and a checksum file is only called verified when GitHub's own digest was compared.
"""
from __future__ import annotations

import json
from pathlib import Path

from helpers import release_flow as rf
from helpers import release_postcheck as pc

TIP = "a" * 40
TAG = "v0.11.0"
ZIP = "App-v0.11.0.zip"
SETUP = "App-v0.11.0-setup.exe"
SUMS = "SHA256SUMS.txt"
H_ZIP, H_SETUP = "1" * 64, "2" * 64


def _cfg(**kw):
    base = dict(assets=("App-v{version}.zip", "App-v{version}-setup.exe", "SHA256SUMS.txt"), workflow="release.yml", source="release-flow.json")
    base.update(kw)
    return rf.FlowConfig(**base)


def _pre():
    return rf.Preflight(state=rf.State(version="0.11.0", tag=TAG, tip=TIP))


def _assets(names=(ZIP, SETUP, SUMS), size=10, digests=True):
    out = []
    for n in names:
        a = {"name": n, "size": size}
        if digests and n == ZIP:
            a["digest"] = "sha256:" + H_ZIP
        if digests and n == SETUP:
            a["digest"] = "sha256:" + H_SETUP
        out.append(a)
    return out


class World:
    def __init__(self, release=None, runs=None, sums=None, remote=TIP, view_rc=0, view_err=""):
        self.release = release if release is not None else {"isDraft": True, "url": "u", "body": "notes", "assets": _assets()}
        self.runs = runs if runs is not None else [{"status": "completed", "conclusion": "success", "url": "r", "event": "push"}]
        self.sums = sums if sums is not None else f"{H_ZIP}  {ZIP}\n{H_SETUP} *{SETUP}\n"
        self.remote, self.view_rc, self.view_err = remote, view_rc, view_err

    def __call__(self, argv, cwd):
        a = list(argv)
        if a[:3] == ["gh", "run", "list"]:
            return rf.RunResult(json.dumps(self.runs), "", 0)
        if a[:3] == ["gh", "release", "view"]:
            return rf.RunResult(json.dumps(self.release), self.view_err, self.view_rc)
        if a[:3] == ["gh", "release", "download"]:
            if self.sums is None:
                return rf.RunResult("", "boom", 1)
            Path(a[a.index("--dir") + 1], SUMS).write_text(self.sums, encoding="utf-8")
            return rf.RunResult("", "", 0)
        if a[:2] == ["git", "ls-remote"]:
            return rf.RunResult(f"{self.remote}\trefs/tags/{TAG}\n" if self.remote else "", "", 0)
        if a[:2] == ["git", "rev-parse"] and "--git-common-dir" in a:
            return rf.RunResult(".git\n", "", 0)
        return rf.RunResult("", "unscripted", 1)

    def env(self):
        return rf.Env(git="git", gh="gh", python="python", run=self)


def _by_id(checks):
    return {c.id: c for c in checks}


def _verify(world, tmp_path, cfg=None):
    return _by_id(pc.verify_draft(world.env(), str(tmp_path), cfg or _cfg(), _pre(), TIP))


def test_a_complete_draft_passes_every_check(tmp_path):
    c = _verify(World(), tmp_path)
    assert {k: v.status for k, v in c.items()} == {"run": rf.OK, "draft": rf.OK, "notes": rf.OK, "tag_sha": rf.OK,
                                                   "assets": rf.OK, "sums": rf.OK}


def test_missing_installer_fails(tmp_path):
    w = World(release={"isDraft": True, "body": "n", "assets": _assets((ZIP, SUMS))})
    c = _verify(w, tmp_path)
    assert c["assets"].status == rf.FAIL and SETUP in c["assets"].detail


def test_zero_size_asset_fails(tmp_path):
    c = _verify(World(release={"isDraft": True, "body": "n", "assets": _assets(size=0)}), tmp_path)
    assert c["assets"].status == rf.FAIL and "empty" in c["assets"].detail


def test_sums_that_omit_a_file_fail(tmp_path):
    c = _verify(World(sums=f"{H_ZIP}  {ZIP}\n"), tmp_path)
    assert c["sums"].status == rf.FAIL and SETUP in c["sums"].detail


def test_hash_that_disagrees_with_githubs_digest_fails(tmp_path):
    c = _verify(World(sums=f"{'9' * 64}  {ZIP}\n{H_SETUP}  {SETUP}\n"), tmp_path)
    assert c["sums"].status == rf.FAIL and ZIP in c["sums"].detail


def test_without_a_digest_the_sums_are_only_partly_verified(tmp_path):
    w = World(release={"isDraft": True, "body": "n", "assets": _assets(digests=False)})
    assert _verify(w, tmp_path)["sums"].status == rf.WARN


def test_undownloadable_sums_are_unknown_not_ok(tmp_path):
    w = World()
    w.sums = None
    assert _verify(w, tmp_path)["sums"].status == rf.UNKNOWN


def test_tag_on_another_commit_fails(tmp_path):
    assert _verify(World(remote="b" * 40), tmp_path)["tag_sha"].status == rf.FAIL


def test_no_release_yet_warns_and_stops(tmp_path):
    c = _verify(World(view_rc=1, view_err="release not found"), tmp_path)
    assert c["draft"].status == rf.WARN and "assets" not in c


def test_unreadable_release_is_unknown(tmp_path):
    c = _verify(World(view_rc=1, view_err="HTTP 401"), tmp_path)
    assert c["draft"].status == rf.UNKNOWN


def test_failed_and_running_workflow_runs(tmp_path):
    assert _verify(World(runs=[{"status": "completed", "conclusion": "failure", "event": "push"}]), tmp_path)["run"].status == rf.FAIL
    assert _verify(World(runs=[{"status": "in_progress", "conclusion": None, "event": "push"}]), tmp_path)["run"].status == rf.WARN
    assert _verify(World(runs=[]), tmp_path)["run"].status == rf.WARN


def test_a_published_release_is_reported_not_failed(tmp_path):
    w = World(release={"isDraft": False, "body": "n", "assets": _assets()})
    assert _verify(w, tmp_path)["draft"].status == rf.INFO


def test_empty_notes_fail(tmp_path):
    assert _verify(World(release={"isDraft": True, "body": " ", "assets": _assets()}), tmp_path)["notes"].status == rf.FAIL


def test_no_assets_configured_is_not_a_pass(tmp_path):
    c = _verify(World(), tmp_path, _cfg(assets=()))
    assert c["assets"].status == rf.WARN


# ── checklist and ticks ──────────────────────────────────────────────────

def test_project_checklist_is_read_and_beats_the_builtin(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "C.md").write_text("# T\n- [ ] one\n- [x] two\ntext\n* [ ] three\n", encoding="utf-8")
    items, source = pc.load_checklist(str(tmp_path), _cfg(checklist="docs/C.md"))
    assert items == ["one", "two", "three"] and source == "docs/C.md"


def test_missing_or_empty_checklist_falls_back_and_says_so(tmp_path):
    items, source = pc.load_checklist(str(tmp_path), _cfg(checklist="docs/none.md"))
    assert items == list(pc.DEFAULT_CHECKLIST) and "built-in" in source
    assert any("Controlled Folder Access" in i for i in items)


def test_ticks_round_trip_per_tag_inside_the_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    env = World().env()
    assert pc.load_ticks(env, str(tmp_path), TAG) == set()
    assert pc.save_ticks(env, str(tmp_path), TAG, {"a", "b"}) == ""
    assert pc.load_ticks(env, str(tmp_path), TAG) == {"a", "b"}
    assert pc.load_ticks(env, str(tmp_path), "v9.9.9") == set()
    assert (tmp_path / ".git" / "tokensave" / "release-v0.11.0.json").is_file()


def test_corrupt_ticks_file_reads_as_nothing_ticked(tmp_path):
    (tmp_path / ".git" / "tokensave").mkdir(parents=True)
    (tmp_path / ".git" / "tokensave" / "release-v0.11.0.json").write_text("{", encoding="utf-8")
    assert pc.load_ticks(World().env(), str(tmp_path), TAG) == set()
