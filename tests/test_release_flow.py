"""helpers/release_flow.py and release_profile.py: pre-flight, the tag plan, and running one step.

Everything runs against a SCRIPTED runner (no git, no gh, no network): each test states what the
outside world answers and asserts what the tool concludes. The properties that matter are the
negative ones, so most tests are about what must NOT happen: an unreadable source is never a
tick, a published release is never touched, and no command outside the four whitelisted shapes
can be produced or run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers import release_flow as rf
from helpers import release_profile as rp

TIP = "a" * 40
OLD = "b" * 40
TAG = "v0.11.0"


class World:
    """A scripted git + gh. `answers` maps an argv prefix (tuple) to a RunResult; longest prefix wins."""

    def __init__(self, over=None):
        self.calls: list = []
        self.answers = {
            ("git", "fetch"): rf.RunResult("", "", 0),
            ("git", "symbolic-ref"): rf.RunResult("origin/master\n", "", 0),
            ("git", "rev-parse", "--verify", "-q", "refs/remotes/origin/master^{commit}"): rf.RunResult(TIP + "\n", "", 0),
            ("git", "rev-parse", "HEAD"): rf.RunResult(TIP + "\n", "", 0),
            ("git", "rev-parse", "--abbrev-ref"): rf.RunResult("master\n", "", 0),
            ("git", "status"): rf.RunResult("", "", 0),
            ("git", "rev-list"): rf.RunResult("0\t0\n", "", 0),
            ("git", "rev-parse", "--verify", "-q", f"refs/tags/{TAG}^{{commit}}"): rf.RunResult("", "", 1),
            ("git", "ls-remote"): rf.RunResult("", "", 0),
            ("gh", "release", "view"): rf.RunResult("", "release not found", 1),
            ("gh", "run", "list"): rf.RunResult(json.dumps([{"workflowName": "ci", "status": "completed",
                                                             "conclusion": "success", "url": "u"}]), "", 0),
            ("gh", "pr", "list"): rf.RunResult("[]", "", 0),
            ("python",): rf.RunResult("ok", "", 0),
            ("gh", "release", "delete"): rf.RunResult("", "", 0),
            ("git", "push"): rf.RunResult("", "", 0),
            ("git", "tag"): rf.RunResult("", "", 0),
        }
        self.answers.update(over or {})

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        key = tuple(argv)
        for n in range(len(key), 0, -1):
            hit = self.answers.get(key[:n])
            if hit is not None:
                return hit
        return rf.RunResult("", f"unscripted: {argv}", 1)

    def ran(self, *prefix) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)

    def env(self) -> rf.Env:
        # Executables are their bare names so the script keys read plainly.
        return rf.Env(git="git", gh="gh", python="python", run=self)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.11.0"\n', encoding="utf-8")
    (tmp_path / "tools").mkdir()
    for name in ("release_info.py", "changelog.py"):
        (tmp_path / "tools" / name).write_text("", encoding="utf-8")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yml").write_text(
        "name: release\non:\n  push:\n    tags:\n      - 'v*'\npermissions: {}\n"
        "jobs:\n  d:\n    steps:\n      - run: gh release create \"$T\" --draft\n", encoding="utf-8")
    return tmp_path


def _gather(world: World, project: Path, tag: str = ""):
    cfg, problems = rf.load_config(str(project))
    assert problems == []
    return rf.gather(world.env(), str(project), cfg, tag), cfg


def _status(pre, check_id):
    return pre.get(check_id).status


# ── pre-flight ───────────────────────────────────────────────────────────

def test_clean_world_is_ready(project):
    pre, _ = _gather(World(), project)
    assert pre.verdict == rf.READY, [(c.id, c.status, c.detail) for c in pre.checks]
    assert pre.state.tag == TAG and pre.state.tip == TIP


def test_an_unreadable_source_is_never_a_tick(project):
    """gh not signed in: release state and CI are UNKNOWN, so the verdict cannot be READY."""
    broken = rf.RunResult("", "gh: not logged in", 4)
    pre, _ = _gather(World({("gh", "release", "view"): broken, ("gh", "run", "list"): broken}), project)
    assert _status(pre, "release_state") == rf.UNKNOWN
    assert _status(pre, "ci") == rf.UNKNOWN
    assert pre.verdict == rf.UNCONFIRMED


def test_failed_ci_on_the_tip_blocks(project):
    red = rf.RunResult(json.dumps([{"workflowName": "ci", "status": "completed", "conclusion": "failure", "url": "u"}]), "", 0)
    pre, _ = _gather(World({("gh", "run", "list"): red}), project)
    assert _status(pre, "ci") == rf.FAIL
    assert pre.verdict == rf.BLOCKED


def test_running_or_missing_ci_is_not_green(project):
    running = rf.RunResult(json.dumps([{"workflowName": "ci", "status": "in_progress", "conclusion": "", "url": "u"}]), "", 0)
    assert _status(_gather(World({("gh", "run", "list"): running}), project)[0], "ci") == rf.WARN
    assert _status(_gather(World({("gh", "run", "list"): rf.RunResult("[]", "", 0)}), project)[0], "ci") == rf.WARN


def test_the_release_workflows_own_runs_are_not_ci(project):
    only_release = rf.RunResult(json.dumps([{"workflowName": "release", "status": "completed",
                                             "conclusion": "success", "url": "u"}]), "", 0)
    pre, _ = _gather(World({("gh", "run", "list"): only_release}), project)
    assert _status(pre, "ci") == rf.WARN


def test_local_commits_not_on_origin_block(project):
    pre, _ = _gather(World({("git", "rev-list"): rf.RunResult("2\t0\n", "", 0)}), project)
    assert _status(pre, "sync") == rf.FAIL
    assert pre.verdict == rf.BLOCKED


def test_behind_origin_warns_and_plans_on_the_origin_tip(project):
    pre, cfg = _gather(World({("git", "rev-list"): rf.RunResult("0\t3\n", "", 0),
                                ("git", "rev-parse", "HEAD"): rf.RunResult(OLD + "\n", "", 0)}), project)
    assert _status(pre, "sync") == rf.WARN
    assert pre.state.tip == TIP
    # A script run in a tree that is not the tip would judge other files: unknown, not ok.
    assert _status(pre, "tag_rule") == rf.UNKNOWN


def test_project_script_failure_is_the_projects_verdict(project):
    world = World({("python", "tools/release_info.py", "check-tag"): rf.RunResult("", "release_info: tag says 0.10.0", 1)})
    pre, _ = _gather(world, project)
    assert _status(pre, "tag_rule") == rf.FAIL
    assert "release_info" in pre.get("tag_rule").detail
    assert pre.verdict == rf.BLOCKED


def test_a_long_script_failure_is_shown_per_line_not_cut_mid_word(project):
    noisy = "changelog: 2 fragment(s) pending\n" + "\n".join(f"  changelog.d/{n}-{'x' * 130}.md: pending" for n in range(5))
    pre, _ = _gather(World({("python", "tools/changelog.py"): rf.RunResult("", noisy, 1)}), project)
    detail = pre.get("fragments").detail
    assert pre.get("fragments").status == rf.FAIL
    assert detail.startswith("changelog: 2 fragment(s) pending | ") and "(+3 more line(s))" in detail
    assert len(detail) < 400


def test_missing_script_is_unknown_not_ok(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    cfg, _ = rf.load_config(str(tmp_path))
    pre = rf.gather(World({("git", "rev-parse", "--verify", "-q", "refs/tags/v1.0.0^{commit}"): rf.RunResult("", "", 1)}).env(),
                    str(tmp_path), cfg)
    assert _status(pre, "tag_rule") == rf.UNKNOWN and _status(pre, "notes") == rf.UNKNOWN


def test_uncommitted_changes_warn_but_do_not_block(project):
    pre, _ = _gather(World({("git", "status"): rf.RunResult(" M x.py\n", "", 0)}), project)
    assert _status(pre, "clean") == rf.WARN
    assert pre.verdict == rf.UNCONFIRMED


def test_the_tag_must_name_this_version(project):
    pre, _ = _gather(World(), project, tag="v9.9.9")
    # The project's own check-tag script (scripted here as passing) is asked; the pattern guard is ours.
    assert pre.state.tag == "v9.9.9"
    pre2, _ = _gather(World(), project, tag="release-1")
    assert pre2.verdict == rf.BLOCKED and pre2.get("version").status == rf.FAIL


def test_annotated_remote_tag_uses_the_peeled_commit(project):
    ls = f"{'c' * 40}\trefs/tags/{TAG}\n{OLD}\trefs/tags/{TAG}^{{}}\n"
    pre, _ = _gather(World({("git", "ls-remote"): rf.RunResult(ls, "", 0)}), project)
    assert pre.state.remote_tag_sha == OLD
    assert _status(pre, "tag_state") == rf.WARN


# ── plans ────────────────────────────────────────────────────────────────

def _plan(project, over=None):
    pre, cfg = _gather(World(over), project)
    return rf.make_plan(pre, cfg), pre


def test_fresh_tag_plan_is_tag_then_push(project):
    plan, _ = _plan(project)
    assert [s.kind for s in plan.steps] == [rf.KIND_TAG, rf.KIND_PUSH_TAG]
    assert plan.steps[0].argv == ("git", "tag", "-a", TAG, TIP, "-m", "Release 0.11.0")
    assert plan.steps[1].argv == ("git", "push", "origin", f"refs/tags/{TAG}")
    assert plan.steps[1].display == f"git push origin refs/tags/{TAG}"


def test_retag_with_a_draft_in_the_way(project):
    plan, _ = _plan(
        project,
        {("gh", "release", "view"): rf.RunResult(json.dumps({"isDraft": True, "url": "u"}), "", 0),
           ("git", "ls-remote"): rf.RunResult(f"{OLD}\trefs/tags/{TAG}\n", "", 0),
           ("git", "rev-parse", "--verify", "-q", f"refs/tags/{TAG}^{{commit}}"): rf.RunResult(OLD + "\n", "", 0)})
    assert [s.kind for s in plan.steps] == [rf.KIND_DELETE_RELEASE, rf.KIND_DELETE_REMOTE_TAG,
                                           rf.KIND_TAG, rf.KIND_PUSH_TAG]
    assert plan.steps[0].argv == ("gh", "release", "delete", TAG, "--yes")
    assert plan.steps[1].argv == ("git", "push", "origin", f":refs/tags/{TAG}")
    assert plan.steps[2].argv[:3] == ("git", "tag", "-f")
    assert "--cleanup-tag" not in " ".join(" ".join(s.argv) for s in plan.steps)


def test_published_release_is_refused_and_offers_no_commands(project):
    plan, pre = _plan(project, {("gh", "release", "view"): rf.RunResult(json.dumps({"isDraft": False, "url": "u"}), "", 0)})
    assert plan.steps == () and "published" in plan.message
    assert pre.verdict == rf.BLOCKED


def test_unknown_release_state_offers_no_commands(project):
    plan, _ = _plan(project, {("gh", "release", "view"): rf.RunResult("", "HTTP 401", 1)})
    assert plan.steps == () and "release exists" in plan.message


def test_a_blocked_preflight_offers_no_commands(project):
    plan, _ = _plan(project, {("git", "rev-list"): rf.RunResult("1\t0\n", "", 0)})
    assert plan.steps == () and "Blocked" in plan.message


def test_local_tag_already_at_tip_is_not_recreated(project):
    plan, _ = _plan(project, {("git", "rev-parse", "--verify", "-q", f"refs/tags/{TAG}^{{commit}}"): rf.RunResult(TIP + "\n", "", 0)})
    assert [s.kind for s in plan.steps] == [rf.KIND_PUSH_TAG]


# ── the whitelist ────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ("git", "push", "origin", "master"),
    ("git", "push", "--force", "origin", f"refs/tags/{TAG}"),
    ("git", "push", "origin", f"+refs/tags/{TAG}"),
    ("git", "push", "origin", f"refs/tags/{TAG}", "--force"),
    ("git", "push", "--follow-tags"),
    ("gh", "release", "edit", TAG, "--draft=false"),
    ("gh", "release", "delete", TAG, "--yes", "--cleanup-tag"),
    ("gh", "release", "create", TAG),
    ("git", "reset", "--hard"),
    ("git", "tag", "-d", TAG),
    ("git", "tag", "-a", TAG, OLD, "-m", "x"),           # not the planned commit
    ("git", "tag", "-a", "v9.9.9", TIP, "-m", "x"),      # not the planned tag
])
@pytest.mark.parametrize("kind", [rf.KIND_TAG, rf.KIND_PUSH_TAG, rf.KIND_DELETE_REMOTE_TAG, rf.KIND_DELETE_RELEASE])
def test_nothing_outside_the_four_shapes_may_run(argv, kind):
    assert rf.step_problems(rf.Step(kind, "t", argv), TAG, TIP)


def test_the_shapes_the_planner_makes_are_exactly_the_allowed_ones(project):
    for over in ({}, {("gh", "release", "view"): rf.RunResult(json.dumps({"isDraft": True}), "", 0),
                      ("git", "ls-remote"): rf.RunResult(f"{OLD}\trefs/tags/{TAG}\n", "", 0)}):
        plan, _ = _plan(project, over)
        for step in plan.steps:
            assert rf.step_problems(step, plan.tag, plan.tip) == []


def test_no_module_source_mentions_a_publish_verb():
    src = Path(rf.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"', "'")))
    for forbidden in ('"publish"', "release edit", '"edit"', "--force", "draft=false", "--latest"):
        assert forbidden not in code.replace("`", ""), forbidden


# ── running a step ───────────────────────────────────────────────────────

def _ready_plan(project, world=None):
    world = world or World()
    pre, cfg = _gather(world, project)
    return world, rf.make_plan(pre, cfg)


def test_run_step_needs_the_tag_typed_for_a_push(project):
    world, plan = _ready_plan(project)
    push = plan.steps[-1]
    res = rf.run_step(world.env(), str(project), plan, push, typed="")
    assert res.refused and not world.ran("git", "push")
    res = rf.run_step(world.env(), str(project), plan, push, typed="v0.11.1")
    assert res.refused and not world.ran("git", "push")
    res = rf.run_step(world.env(), str(project), plan, push, typed=TAG)
    assert res.ok and world.ran("git", "push", "origin", f"refs/tags/{TAG}")


def test_local_tag_step_needs_no_typing(project):
    world, plan = _ready_plan(project)
    res = rf.run_step(world.env(), str(project), plan, plan.steps[0])
    assert res.ok and world.ran("git", "tag", "-a")


def test_run_step_refuses_when_the_tip_moved(project):
    world, plan = _ready_plan(project)
    world.answers[("git", "rev-parse", "--verify", "-q", "refs/remotes/origin/master^{commit}")] = rf.RunResult(OLD + "\n", "", 0)
    res = rf.run_step(world.env(), str(project), plan, plan.steps[-1], typed=TAG)
    assert "moved" in res.refused and not world.ran("git", "push")


def test_run_step_refuses_a_step_that_is_not_in_the_plan(project):
    world, plan = _ready_plan(project)
    foreign = rf.Step(rf.KIND_PUSH_TAG, "x", ("git", "push", "origin", f"refs/tags/{TAG}"), "other note")
    assert rf.run_step(world.env(), str(project), plan, foreign, typed=TAG).refused


def test_delete_release_rechecks_that_it_is_still_a_draft(project):
    draft = rf.RunResult(json.dumps({"isDraft": True, "url": "u"}), "", 0)
    world = World({("gh", "release", "view"): draft, ("git", "ls-remote"): rf.RunResult(f"{OLD}\trefs/tags/{TAG}\n", "", 0)})
    world, plan = _ready_plan(project, world)
    # It was published between planning and running.
    world.answers[("gh", "release", "view")] = rf.RunResult(json.dumps({"isDraft": False}), "", 0)
    res = rf.run_step(world.env(), str(project), plan, plan.steps[0], typed=TAG)
    assert "draft" in res.refused and not world.ran("gh", "release", "delete")


def test_a_failing_command_is_reported_not_swallowed(project):
    world, plan = _ready_plan(project)
    world.answers[("git", "push")] = rf.RunResult("", "remote: permission denied", 1)
    res = rf.run_step(world.env(), str(project), plan, plan.steps[-1], typed=TAG)
    assert not res.ok and "permission denied" in res.text and not res.refused


# ── config ───────────────────────────────────────────────────────────────

def test_config_unknown_key_and_bad_types_are_reported(tmp_path):
    (tmp_path / "release-flow.json").write_text(json.dumps({"assets": "nope", "bogus": 1, "tag_message": "x {version}"}), encoding="utf-8")
    cfg, problems = rf.load_config(str(tmp_path))
    assert any("bogus" in p for p in problems) and any("assets" in p for p in problems)
    assert cfg.assets == () and cfg.tag_message == "x {version}" and cfg.source == rf.CONFIG_FILE


def test_config_not_json_falls_back_with_a_message(tmp_path):
    (tmp_path / "release-flow.json").write_text("{", encoding="utf-8")
    cfg, problems = rf.load_config(str(tmp_path))
    assert problems and cfg.source == "defaults"


def test_version_regex_and_missing_file(tmp_path):
    (tmp_path / "v.txt").write_text("VERSION=2.3.4\n", encoding="utf-8")
    cfg = rf.FlowConfig(version_file="v.txt", version_regex=r"VERSION=(\S+)")
    assert rf.read_version(str(tmp_path), cfg) == ("2.3.4", "")
    assert rf.read_version(str(tmp_path), rf.FlowConfig(version_file="nope.toml"))[1]


# ── profile detection ────────────────────────────────────────────────────

def test_pyinstaller_inno_workflow_project(tmp_path):
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "app.spec").write_text("", encoding="utf-8")
    (tmp_path / "packaging" / "app.iss").write_text("", encoding="utf-8")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yml").write_text("on:\n  push:\n    tags: ['v*']\njobs:\n  j:\n    steps:\n      - run: gh release create x --draft\n", encoding="utf-8")
    prof = rp.detect(str(tmp_path))
    assert prof.build_systems == ("pyinstaller",) and prof.installers == ("inno",) and prof.ci_draft
    assert "PyInstaller" in prof.label() and "Inno Setup" in prof.label() and "workflow" in prof.label()
    assert any(e.source.startswith("packaging/app.spec") for e in prof.evidence)


def test_nuitka_project_with_a_local_build(tmp_path):
    (tmp_path / "build.ps1").write_text("python -m nuitka --onefile app.py", encoding="utf-8")
    prof = rp.detect(str(tmp_path))
    assert prof.build_systems == ("nuitka",) and prof.release == rp.PROFILE_LOCAL_BUILD and not prof.ci_draft


def test_the_word_in_prose_elsewhere_does_not_count(tmp_path):
    (tmp_path / "build.ps1").write_text("# builds the app\n", encoding="utf-8")
    assert rp.detect(str(tmp_path)).build_systems == ()
    assert "not recognised" in rp.detect(str(tmp_path)).label()


def test_a_workflow_that_does_not_draft_is_not_ci_draft(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yml").write_text("on:\n  push:\n    tags: ['v*']\njobs:\n  j:\n    steps:\n      - run: gh release create x\n", encoding="utf-8")
    assert not rp.detect(str(tmp_path)).ci_draft
