"""tests/test_cli_paths.py — `--paths` on `checks` and `test-gaps`.

The editor context-menu entries ("Checks this file") need per-file scoping. The
risk they introduce is a specific one: running whole-workspace work under a
per-file label, or — worse — reporting a clean bill of health for a path the
command never looked at.

So the contract has three parts, and the last two are what make it honest:

* `--paths` is a **filter, not a second format**: same producer, same fields,
  fewer rows, and the pass/fail verdict follows the filter.
* a path **outside the project is an error**, because silently returning zero
  findings for it is indistinguishable from a clean file;
* `requested_paths` / `matched_paths` are reported, so "this file is clean" and
  "that path is not part of this project" can be told apart — both otherwise
  render as an empty list.
"""
from __future__ import annotations

import json

import pytest

import cli
from cli import EXIT_FAILED, EXIT_OK, EXIT_PREREQUISITE, main
from helpers import quality_checks
from helpers.findings import Finding


def _run(capsys, argv):
    code = main(argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("import sys\n", encoding="utf-8")
    return str(tmp_path)


class _Outcome:
    def __init__(self, findings):
        self.ok = not findings
        self.summary = f"{len(findings)} finding(s)"
        self.findings = findings


def _finding(path, message):
    return Finding(file=path, line=1, column=1, end_line=1, end_column=1,
                   severity="warning", message=message, rule="pyflakes",
                   symbol="")


@pytest.fixture()
def two_files_with_findings(mocker):
    mocker.patch.object(quality_checks, "run_syntax",
                        lambda p: _Outcome([]))
    mocker.patch.object(quality_checks, "run_pyflakes", lambda p: _Outcome([
        _finding("src/a.py", "'os' imported but unused"),
        _finding("src/b.py", "'sys' imported but unused"),
    ]))


# ── checks ────────────────────────────────────────────────────────────────

def test_without_paths_every_finding_is_reported(capsys, project,
                                                 two_files_with_findings):
    code, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert code == EXIT_FAILED
    assert len(env["findings"]) == 2
    assert "requested_paths" not in env["data"]


def test_paths_filters_to_the_named_file(capsys, project,
                                         two_files_with_findings):
    code, env, _ = _run(capsys, ["checks", "--project", project,
                                 "--paths", "src/a.py", "--json"])
    assert code == EXIT_FAILED
    assert [f["file"] for f in env["findings"]] == ["src/a.py"]


def test_the_verdict_follows_the_filter(capsys, project,
                                        two_files_with_findings):
    """A scoped run on a clean file must not inherit the project's failures.

    Without this the context-menu entry would report a problem in a file the
    user is looking at, sourced from one they are not.
    """
    code, env, _ = _run(capsys, ["checks", "--project", project,
                                 "--paths", "src/clean.py", "--json"])
    assert code == EXIT_OK
    assert env["findings"] == []


def test_a_filtered_finding_keeps_its_shape(capsys, project,
                                            two_files_with_findings):
    """Same producer, same fields — a consumer must not need to know."""
    _, whole, _ = _run(capsys, ["checks", "--project", project, "--json"])
    _, scoped, _ = _run(capsys, ["checks", "--project", project,
                                 "--paths", "src/a.py", "--json"])
    assert set(scoped["findings"][0]) == set(whole["findings"][0])


def test_requested_and_matched_are_reported(capsys, project,
                                            two_files_with_findings):
    _, env, _ = _run(capsys, ["checks", "--project", project,
                              "--paths", "src/a.py", "--json"])
    assert env["data"]["requested_paths"] == ["src/a.py"]
    assert env["data"]["matched_paths"] == ["src/a.py"]


def test_a_path_that_does_not_exist_is_requested_but_not_matched(
        capsys, project, two_files_with_findings):
    """"clean" and "not part of this project" must be distinguishable.

    Both produce an empty finding list, so the finding list alone cannot carry
    the difference — a typo in a resource URI would otherwise read as a perfect
    bill of health.
    """
    _, env, _ = _run(capsys, ["checks", "--project", project,
                              "--paths", "src/typo.py", "--json"])
    assert env["data"]["requested_paths"] == ["src/typo.py"]
    assert env["data"]["matched_paths"] == []


def test_a_path_outside_the_project_is_a_prerequisite_error(
        capsys, project, two_files_with_findings, tmp_path):
    """Not an empty result. The per-file analogue of the inbox's containment
    rule, applied to the other direction of travel."""
    outside = tmp_path.parent / "elsewhere.py"
    code, env, _ = _run(capsys, ["checks", "--project", project,
                                 "--paths", str(outside), "--json"])
    assert code == EXIT_PREREQUISITE
    assert "outside the project" in env["error"]


def test_a_traversal_out_of_the_project_is_refused(capsys, project,
                                                   two_files_with_findings):
    code, env, _ = _run(capsys, ["checks", "--project", project,
                                 "--paths", "../../etc/passwd", "--json"])
    assert code == EXIT_PREREQUISITE
    assert "outside the project" in env["error"]


def test_an_absolute_in_project_path_is_accepted(capsys, project,
                                                 two_files_with_findings):
    """VS Code hands out absolute URIs; they are normalised, not rejected."""
    import os
    absolute = os.path.join(project, "src", "a.py")
    _, env, _ = _run(capsys, ["checks", "--project", project,
                              "--paths", absolute, "--json"])
    assert env["data"]["requested_paths"] == ["src/a.py"]
    assert [f["file"] for f in env["findings"]] == ["src/a.py"]


def test_several_paths_are_all_honoured(capsys, project,
                                        two_files_with_findings):
    _, env, _ = _run(capsys, ["checks", "--project", project,
                              "--paths", "src/a.py", "src/b.py", "--json"])
    assert {f["file"] for f in env["findings"]} == {"src/a.py", "src/b.py"}


# ── test-gaps ─────────────────────────────────────────────────────────────

class _Suggestion:
    def __init__(self, source, test):
        self.source_path = source
        self.test_path = test
        self.requires_automation = False


@pytest.fixture()
def gaps(mocker):
    from helpers import test_gap_report
    mocker.patch.object(cli, "_load_manager_config", lambda *a, **k: {})
    mocker.patch("helpers.git.default_base_ref", lambda *a, **k: "main")
    mocker.patch("helpers.git.ref_exists", lambda *a, **k: True)
    mocker.patch.object(test_gap_report, "suggest_tests_for_diff",
                        lambda *a, **k: [
                            _Suggestion("src/a.py", "tests/test_a.py"),
                            _Suggestion("src/b.py", "tests/test_b.py"),
                        ])


def test_test_gaps_filters_its_suggestions(capsys, project, gaps):
    """It emits suggestions rather than findings, so the filter is over
    `source` — same contract for the caller either way."""
    _, env, _ = _run(capsys, ["test-gaps", "--project", project,
                              "--paths", "src/a.py", "--json"])
    assert [s["source"] for s in env["data"]["suggestions"]] == ["src/a.py"]


def test_test_gaps_count_follows_the_filter(capsys, project, gaps):
    """`count` is what a UI renders; leaving it at the unfiltered total would
    have the panel disagree with its own list."""
    _, env, _ = _run(capsys, ["test-gaps", "--project", project,
                              "--paths", "src/a.py", "--json"])
    assert env["data"]["count"] == 1
    assert len(env["data"]["suggestions"]) == 1


def test_test_gaps_without_paths_is_unfiltered(capsys, project, gaps):
    _, env, _ = _run(capsys, ["test-gaps", "--project", project, "--json"])
    assert env["data"]["count"] == 2
    assert "requested_paths" not in env["data"]


def test_test_gaps_refuses_an_out_of_project_path(capsys, project, gaps,
                                                  tmp_path):
    code, env, _ = _run(capsys, ["test-gaps", "--project", project,
                                 "--paths", str(tmp_path.parent / "x.py"),
                                 "--json"])
    assert code == EXIT_PREREQUISITE
    assert "outside the project" in env["error"]


# ── The table agrees ──────────────────────────────────────────────────────

def test_the_command_table_marks_exactly_these_two(capsys):
    """So the extension can tell which entries may be scoped per-file, rather
    than discovering it from a usage error."""
    from helpers.commands import COMMANDS
    assert {c.cli for c in COMMANDS if c.accepts_paths} == {"checks",
                                                            "test-gaps"}


# ── test-run's failure taxonomy (D3) ──────────────────────────────────────

@pytest.mark.parametrize("output,expected", [
    ("ModuleNotFoundError: No module named pytest", "pytest_missing"),
    ("pytest: command not found", "pytest_missing"),
    ("the run timed out after 600s", "timeout"),
    ("ERROR collecting tests/test_x.py", "collection_error"),
    ("!!! INTERNALERROR >", "collection_error"),
    ("something nobody has seen before", "unreadable"),
])
def test_a_failed_run_is_classified_by_what_it_printed(output, expected):
    """`passed/failed/skipped` cannot say why nothing was read.

    "Your tests failed", "pytest never started" and "the Manager killed it on a
    timeout" have different remedies, and the counts look identical in all
    three. An unrecognised message falls through to `unreadable` rather than
    being assigned a plausible cause it has not earned.
    """
    assert cli._classify_run(output) == expected


def test_the_classifier_never_reports_completed():
    """It is only consulted when no summary could be read, so claiming the run
    completed would be the one answer it must never give."""
    for text in ("", "anything at all", "5 passed"):
        assert cli._classify_run(text) != "completed"
