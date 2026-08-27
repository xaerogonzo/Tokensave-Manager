"""tests/test_quality_checks_findings.py — the structured half of `checks`.

`tests/test_quality_checks.py` covers the legacy one-line summaries and is
deliberately left untouched by this work: those tests passing unchanged IS the
parity guarantee that the Run Checks dialog still behaves as it did.

This file covers the new half — the parsers that turn the same raw output into
`Finding` records for the VS Code Problems panel.

Every format asserted here was **captured from a real invocation** into
`tests/fixtures/quality_checks/`, not written from memory. Three of those
fixtures exist because the captured output disagreed with what a reasonable
person would have assumed, and each has a test below named for what it caught.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers.quality_checks import (
    parse_compileall_output,
    parse_pyflakes_output,
    run_pyflakes,
    run_pyflakes_check,
    run_syntax,
    run_syntax_check,
)

SEP = chr(92)                       # a backslash, spelled to stay unambiguous
_FIXTURES = Path(__file__).parent / "fixtures" / "quality_checks"

WIN_ROOT = "C:" + SEP + "proj"
POSIX_ROOT = "/home/me/proj"


def _fixture(name: str, root: str) -> str:
    """A fixture with its placeholders resolved against *root*.

    `<PROJECT_ESC>` exists because compileall's `*** Error compiling` header
    prints a `repr()` of the path — every separator doubled — while the
    `File "...", line N` line below it prints the same path raw. Both forms
    appear in one block.
    """
    text = _FIXTURES.joinpath(name).read_text(encoding="utf-8")
    return (text.replace("<PROJECT_ESC>", root.replace(SEP, SEP + SEP))
                .replace("<PROJECT>", root))


def test_the_fixture_directory_is_populated():
    """A silently-empty glob would make the parametrised tests vacuous."""
    assert list(_FIXTURES.glob("*.txt"))


# ── pyflakes ──────────────────────────────────────────────────────────────

def test_pyflakes_reports_every_finding_not_just_the_first():
    """The bug this whole workstream started from: the one-line summary
    truncates to `first line (+N more)`, so the extension could never show
    more than one problem."""
    got = parse_pyflakes_output(_fixture("pyflakes_many.txt", WIN_ROOT), WIN_ROOT)
    assert len(got) == 5


def test_pyflakes_keeps_the_real_line_and_column():
    got = parse_pyflakes_output(_fixture("pyflakes_many.txt", WIN_ROOT), WIN_ROOT)
    positions = [(f.file, f.line, f.column) for f in got]
    assert ("src/flakey.py", 7, 12) in positions


def test_pyflakes_clean_output_yields_nothing():
    assert parse_pyflakes_output(_fixture("pyflakes_clean.txt", WIN_ROOT),
                                 WIN_ROOT) == []


def test_pyflakes_posix_separators_are_handled():
    """The shape CI actually produces — the Windows fixtures alone would let a
    separator assumption through."""
    got = parse_pyflakes_output(_fixture("pyflakes_posix.txt", POSIX_ROOT),
                                POSIX_ROOT)
    assert [f.file for f in got] == ["src/flakey.py", "src/flakey.py"]


def test_pyflakes_column_less_form_is_not_dropped():
    """pyflakes writes THIS form to stderr, without a column:
    `<path>:<line>: <message>`. A parser that requires a column silently loses
    every file that could not be read at all."""
    got = parse_pyflakes_output(
        _fixture("pyflakes_stderr_no_column.txt", WIN_ROOT), WIN_ROOT)
    assert len(got) == 1
    assert (got[0].line, got[0].column) == (1, 1)


def test_pyflakes_path_colon_does_not_confuse_the_split():
    """A Windows path contains a colon (`C:`), so splitting the line on ":"
    puts the drive letter in the filename and the rest nowhere."""
    got = parse_pyflakes_output(_fixture("pyflakes_one.txt", WIN_ROOT), WIN_ROOT)
    assert got[0].file == "src/flakey2.py"


def test_pyflakes_severity_is_the_callers_choice():
    """`run_pyflakes` passes a different one per stream — see the stream-split
    test below."""
    got = parse_pyflakes_output(_fixture("pyflakes_one.txt", WIN_ROOT),
                                WIN_ROOT, severity="error")
    assert got[0].severity == "error"


def test_pyflakes_reports_no_rule_codes_it_was_not_given():
    """The CLI prints no F-codes, so inventing `F401` would fabricate detail."""
    got = parse_pyflakes_output(_fixture("pyflakes_one.txt", WIN_ROOT), WIN_ROOT)
    assert got[0].rule == "pyflakes"


# ── compileall ────────────────────────────────────────────────────────────

def test_compileall_associates_each_header_with_its_own_block():
    """Three blocks, blank-line separated. Associating each header with the
    block below it is where a state machine goes wrong."""
    got = parse_compileall_output(_fixture("compileall_many.txt", WIN_ROOT),
                                  WIN_ROOT)
    assert [(f.file, f.message) for f in got] == [
        ("src/bad1.py", "invalid syntax"),
        ("src/bad2.py", "'(' was never closed"),
        ("src/bad3.py", "expected ':'"),
    ]


def test_compileall_block_without_a_file_line_is_still_reported():
    """CAUGHT BY CAPTURE: when the error is raised before a position exists
    (null bytes in the source) the block is just a header and a SyntaxError
    line. A parser that requires the `File` line drops the finding entirely."""
    got = parse_compileall_output(
        _fixture("compileall_no_file_line.txt", WIN_ROOT), WIN_ROOT)
    assert len(got) == 1
    assert got[0].file == "src/badbytes.py"
    assert got[0].line == 1


def test_compileall_file_line_is_not_parsed_as_a_python_literal():
    """CAUGHT BY CAPTURE, and the sharpest trap here.

    The header holds a `repr()` (separators doubled); the `File "..."` line
    holds the path RAW. Feeding the raw one to `ast.literal_eval` interprets
    the separator as an escape, so `src` + sep + `bad1.py` silently becomes
    `srcad1.py` (backspace) and `src` + sep + `toplevel...` becomes a tab.
    The editor is then sent to a file that does not exist.
    """
    got = parse_compileall_output(_fixture("compileall_one.txt", WIN_ROOT),
                                  WIN_ROOT)
    assert got[0].file == "src/bad1.py"
    for bad in ("\b", "\t", "srcad1"):
        assert bad not in got[0].file


def test_compileall_tab_forming_path_survives():
    """The same trap with a different escape: sep + `t` is a tab."""
    got = parse_compileall_output(
        _fixture("compileall_multichar_caret.txt", WIN_ROOT), WIN_ROOT)
    assert got[0].file == "src/toplevel_return.py"


def test_compileall_path_with_a_space_survives():
    """This project lives under a path with a space, and that has already cost
    it one upstream bug report."""
    got = parse_compileall_output(
        _fixture("compileall_path_with_space.txt", WIN_ROOT), WIN_ROOT)
    assert got[0].file == "src/bad 1.py"


def test_compileall_position_is_line_level_on_purpose():
    """CAUGHT BY CAPTURE: the caret line looks like it gives a column, but it
    is aligned to the stripped, re-indented source line compileall prints, not
    to the file — an open paren at real column 13 prints its caret at offset 5.
    Reporting column 1 is honest; deriving one from the caret is confidently
    wrong, and a squiggle in the wrong place is worse than one on the line.
    """
    got = parse_compileall_output(_fixture("compileall_many.txt", WIN_ROOT),
                                  WIN_ROOT)
    assert {f.column for f in got} == {1}


def test_compileall_marks_syntax_failures_as_errors():
    got = parse_compileall_output(_fixture("compileall_one.txt", WIN_ROOT),
                                  WIN_ROOT)
    assert got[0].severity == "error"
    assert got[0].rule == "compileall/SyntaxError"


def test_compileall_clean_output_yields_nothing():
    assert parse_compileall_output(_fixture("compileall_clean.txt", WIN_ROOT),
                                   WIN_ROOT) == []


# ── degrade, do not abort ─────────────────────────────────────────────────

@pytest.mark.parametrize("noise", [
    "warning: something entirely unexpected",
    "",
    "   ",
    "Traceback (most recent call last):",
    "::::",
])
def test_one_unparseable_line_does_not_hide_the_findings_after_it(noise):
    """A single odd line must never stop valid findings reaching the editor."""
    text = noise + "\n" + _fixture("pyflakes_many.txt", WIN_ROOT)
    assert len(parse_pyflakes_output(text, WIN_ROOT)) == 5


def test_parsers_never_raise_on_garbage():
    junk = "\x00 not output at all \n:::\n*** Error compiling\n  File\n"
    assert parse_pyflakes_output(junk, WIN_ROOT) == []
    assert parse_compileall_output(junk, WIN_ROOT) == []


# ── every finding is repo-relative ────────────────────────────────────────

@pytest.mark.parametrize("name,parser", [
    ("pyflakes_many.txt", parse_pyflakes_output),
    ("compileall_many.txt", parse_compileall_output),
])
def test_no_finding_leaks_an_absolute_or_native_path(name, parser):
    """The envelope is read on a machine that may not be this one."""
    for finding in parser(_fixture(name, WIN_ROOT), WIN_ROOT):
        assert SEP not in finding.file
        assert not finding.file.startswith("C:")


# ── live round trip: the tools still emit what the fixtures claim ─────────

def _project(tmp_path: Path, **files: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        src.joinpath(name.replace("__", ".")).write_text(body, encoding="utf-8")
    return tmp_path


def test_live_compileall_output_still_matches_the_parser(tmp_path):
    """Drift detection. compileall is stdlib, so this runs everywhere: if a
    future Python changes the block format, this fails with a real diff rather
    than the fixtures quietly describing a format nothing emits any more."""
    project = _project(tmp_path, bad__py="def f(:\n    pass\n",
                       fine__py="def ok():\n    return 1\n")
    result = run_syntax(str(project))
    assert result.ok is False
    assert [(f.file, f.severity) for f in result.findings] == [
        ("src/bad.py", "error")]
    assert "invalid syntax" in result.findings[0].message


def test_live_compileall_is_clean_when_it_should_be(tmp_path):
    project = _project(tmp_path, fine__py="def ok():\n    return 1\n")
    result = run_syntax(str(project))
    assert result.ok is True
    assert result.findings == []


@pytest.mark.skipif(shutil.which(sys.executable) is None, reason="no interpreter")
def test_live_pyflakes_splits_its_two_streams_by_severity(tmp_path):
    """MEASURED, not assumed: pyflakes puts findings for a file it analysed on
    stdout, and files it could not analyse at all on stderr. Concatenating the
    streams reports an unreadable file as a lint warning.
    """
    if subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                      capture_output=True).returncode != 0:
        pytest.skip("pyflakes not installed")
    project = _project(
        tmp_path,
        lint__py="import os\n",                       # analysable -> stdout
        broken__py="def f(:\n    pass\n",             # unparseable -> stderr
    )
    by_severity = {}
    for finding in run_pyflakes(str(project)).findings:
        by_severity.setdefault(finding.severity, []).append(finding.file)
    assert by_severity.get("warning") == ["src/lint.py"]
    assert by_severity.get("error") == ["src/broken.py"]


# ── the two APIs never disagree ───────────────────────────────────────────

def test_legacy_wrappers_return_the_same_verdict_as_the_full_result(tmp_path):
    """`run_*_check` is a thin summary view of `run_*`. If they could disagree,
    the Run Checks dialog and the Problems panel would tell different stories
    about the same run."""
    project = _project(tmp_path, bad__py="def f(:\n    pass\n")
    full = run_syntax(str(project))
    assert run_syntax_check(str(project)) == (full.ok, full.summary)

    project2 = _project(tmp_path / "clean", fine__py="x = 1\n")
    full2 = run_pyflakes(str(project2))
    assert run_pyflakes_check(str(project2)) == (full2.ok, full2.summary)


def test_the_summary_truncation_is_deliberate_and_still_there(tmp_path):
    """It is a status line, not a report. A later refactor that "simplifies"
    the wrapper into joining every line would change the Run Checks dialog
    while every test in this file still passed — so assert it here too."""
    project = _project(tmp_path,
                       a__py="import os\nimport sys\nimport json\n")
    result = run_pyflakes(str(project))
    assert "more)" in result.summary
    assert len(result.summary.splitlines()) == 1
    assert len(result.findings) == 3
