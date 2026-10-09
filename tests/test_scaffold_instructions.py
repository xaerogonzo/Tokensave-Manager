"""Scaffold wires the project's Claude instructions; it no longer writes BASIC.

What this pins. Scaffold used to write `BASIC_INSTRUCTIONS.md` and a baseline
copy but never a `CLAUDE.md` -- the only file Claude Code reads -- so every
freshly scaffolded project started out ORPHANED until somebody retrofitted it.
It now goes through the same planner Retrofit uses, and ends with
`CLAUDE.md -> @project-baseline.md` resolving.

The controller is built with every Tk dependency replaced, as in
`test_retrofit_index.py`: nothing here touches a widget.
"""
from __future__ import annotations

import os

import pytest

from controllers.scaffold_ctrl import ScaffoldRetrofitController
from helpers import instructions_posture as ip

BASELINE = "# Project Baseline Rules\n\n## Tokensave\n"


class _Cfg:
    git_exe = ""                              # no git: alignment is a no-op
    tokensave_exe = "tokensave"
    basic_instructions_template = ""
    raw: dict = {}

    def __init__(self, template_dir):
        self.template_dir = str(template_dir)
        self.baseline_include_line = "@" + os.path.join(
            str(template_dir), "project-baseline.md")


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "project-baseline.md").write_text(BASELINE, encoding="utf-8")
    return d


@pytest.fixture
def ctrl(templates, mocker):
    return ScaffoldRetrofitController(
        tab=mocker.MagicMock(), cfg=_Cfg(templates),
        on_log=lambda *a, **k: None, on_set_running=mocker.MagicMock(),
        on_set_proc=mocker.MagicMock(), on_refresh=mocker.MagicMock(),
        on_commit_offer=mocker.MagicMock(), on_insert_pending=mocker.MagicMock())


def _reach(ctrl, project):
    cfg = ctrl._cfg
    return ip.read_project(
        str(project), project.name, cfg.template_dir,
        ip.parse_baseline_target(cfg.baseline_include_line))


def test_a_scaffolded_project_gets_a_claude_md_that_resolves(
        ctrl, tmp_path):
    project = tmp_path / "newproj"
    project.mkdir()
    ctrl._scaffold_project(str(project), run_init=False)

    assert (project / "CLAUDE.md").is_file(), "the only file Claude Code reads"
    assert not (project / "BASIC_INSTRUCTIONS.md").exists()
    assert (project / "project-baseline.md").is_file()
    assert _reach(ctrl, project).reach == ip.REACH_RESOLVED


def test_scaffold_with_instructions_unticked_writes_nothing(ctrl, tmp_path):
    project = tmp_path / "bare"
    project.mkdir()
    ctrl._scaffold_project(str(project), create_instructions=False,
                           run_init=False)
    assert sorted(os.listdir(project)) == []


def test_scaffolding_over_an_existing_claude_md_keeps_its_text(ctrl, tmp_path):
    project = tmp_path / "existing"
    project.mkdir()
    prose = "# Mine\n\nAlways run the linter.\n"
    (project / "CLAUDE.md").write_text(prose, encoding="utf-8")

    ctrl._scaffold_project(str(project), run_init=False)

    after = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert after.endswith(prose)
    assert "@project-baseline.md" in after
    assert not (project / "BASIC_INSTRUCTIONS.md").exists()


def test_retrofit_no_longer_has_a_basic_instructions_step(ctrl):
    """The checkbox is gone, so the step and its writer must be too -- a
    method nothing can reach is how a retired file comes back."""
    assert not hasattr(ctrl, "_retrofit_add_basic_instructions")
    assert "basic_instructions" not in ctrl._run_retrofit_steps.__code__.co_names


def test_the_scaffold_column_counts_a_claude_md(tmp_path):
    """A project wired the new way has no BASIC file and must not read as bare."""
    from controllers.projects_tab import ProjectsTabController as P
    only_claude = tmp_path / "a"; only_claude.mkdir()
    (only_claude / "CLAUDE.md").write_text("x", encoding="utf-8")
    only_basic = tmp_path / "b"; only_basic.mkdir()
    (only_basic / "BASIC_INSTRUCTIONS.md").write_text("x", encoding="utf-8")
    neither = tmp_path / "c"; neither.mkdir()
    assert P._has_scaffold(str(only_claude))
    assert P._has_scaffold(str(only_basic))
    assert not P._has_scaffold(str(neither))
