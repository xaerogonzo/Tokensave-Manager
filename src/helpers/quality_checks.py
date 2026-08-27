"""Pure quality-check runner functions — no Tk dependency.

These are the deterministic (non-LLM) check implementations shared between:
  * dialogs/checks_dialog.py  — the interactive Run Checks dialog (UI)
  * prepush_runner.py         — the headless pre-push git hook runner
  * cli.py                    — the headless `checks` command

Kept here so no consumer has to import tkinter just to run a subprocess.

**Two outputs from one run.** Each tool is invoked once and its raw output
feeds two branches: a structured `Finding` list for the VS Code Problems
panel, and the one-line summary the Manager's dialogs have always shown. The
summary's truncation (`first line (+N more)`) is deliberate — it is a status
line, not a report — so it must not be "simplified" into joining every line.
That would silently change the Run Checks dialog while every new test passed.

**The parsers are pure and the runners do the I/O**, the same split
`helpers/ci_workflow.py` and `helpers/vscode_tasks.py` use. Every format below
was captured from a real invocation into `tests/fixtures/quality_checks/`
rather than written from memory; the README there records why each fixture
exists.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import subprocess
import sys

from constants import CREATE_NO_WINDOW
from helpers.findings import Finding, relative_to

#: A single and a double quote. Spelled this way so the character class is
#: unambiguous to read next to the literals it strips.
_QUOTES = chr(39) + chr(34)

# ── output formats, as measured ──────────────────────────────────────────────

# pyflakes stdout: `<path>:<line>:<col>: <message>`. On Windows the path itself
# contains a colon (the drive letter), so splitting on ":" does not work —
# anchor on the first `:<digits>:<digits>: ` instead, with a non-greedy path.
_PYFLAKES_WITH_COL = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+)$")

# pyflakes ALSO writes a second, column-less form — to **stderr**, for files it
# could not parse at all ("source code string cannot contain null bytes").
# run_pyflakes_check concatenates both streams, so both shapes arrive together.
_PYFLAKES_NO_COL = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<msg>.+)$")

# compileall emits a multi-line block per failing file:
#
#     *** Error compiling '<repr of path>'...
#       File "<path>", line N
#         <source line, stripped and re-indented>
#         <caret line>
#     SyntaxError: <message>
#
# Both paths are Python string literals, so both are parsed as such. A block
# may have NO `File` line at all, when the error is raised before a position
# exists — then the header is the only path there is, and there is no line.
_COMPILEALL_HEADER = re.compile(r"^\*\*\* Error compiling (?P<repr>.+)\.\.\.$")
_COMPILEALL_FILE = re.compile(r"^  File (?P<repr>.+), line (?P<line>\d+)")
_COMPILEALL_ERROR = re.compile(r"^(?P<kind>[A-Za-z_][A-Za-z0-9_]*): (?P<msg>.*)$")


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One tool run, in both the shapes its consumers need."""
    ok: bool
    summary: str            # the legacy one-line status text
    findings: list          # structured, for the envelope
    output: str             # raw combined stdout + stderr


# ── parsers (pure) ───────────────────────────────────────────────────────────

def parse_pyflakes_output(text: str, project_root: str,
                          severity: str = "warning") -> list:
    """Findings from one of pyflakes' output streams. Never raises.

    A line matching neither form is **ignored**, not fatal: one unexpected line
    must never stop the valid findings after it from reaching the editor.

    `severity` is the caller's to choose because pyflakes itself splits the two
    cases across streams — see `run_pyflakes`.
    """
    out: list = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        column = 1
        match = _PYFLAKES_WITH_COL.match(line)
        if match:
            column = max(1, int(match.group("col")))
        else:
            match = _PYFLAKES_NO_COL.match(line)
            if not match:
                continue
        out.append(Finding(
            file=relative_to(match.group("file"), project_root),
            line=max(1, int(match.group("line"))),
            column=column,
            message=match.group("msg").strip(),
            severity=severity,
            # pyflakes' CLI prints no rule codes, so there are none to report.
            # Inventing "F401"-style codes here would fabricate detail the tool
            # did not give us.
            rule="pyflakes",
        ))
    return out


def _repr_path(repr_text: str) -> str:
    """The path out of the `*** Error compiling '...'` header.

    That header prints a `repr()` of the path, so every separator is escaped
    and it must be parsed as a literal to be recovered. Falls back to stripping
    the quotes if it is somehow not a valid one.
    """
    text = repr_text.strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text.strip(_QUOTES)
    return value if isinstance(value, str) else text.strip(_QUOTES)


def _quoted_path(text: str) -> str:
    """The path out of the `File "...", line N` line.

    **This one is NOT a literal**, despite the quotes: the traceback formatter
    prints the path raw, so a Windows path arrives with single separators.
    Feeding it to `literal_eval` silently corrupts it — a backslash before `b`
    or `t` becomes a backspace or a tab, so `src` + sep + `bad1.py` parses as
    `srcad1.py` and the editor is sent to a file that does not exist. The two
    forms appear in the SAME block, which is why they get separate functions
    rather than one that guesses.
    """
    return text.strip().strip(_QUOTES)


def parse_compileall_output(text: str, project_root: str) -> list:
    """Findings from `compileall -q` output. Never raises.

    **Position is line-level, and deliberately so.** Each block carries a caret
    line that looks like it gives a column, but the caret is aligned to the
    *stripped, re-indented* source line compileall prints, not to the file: an
    open paren at real column 13 inside an indented method prints its caret at
    offset 5. Recovering the true column would mean re-reading the source,
    which a pure parser must not do and which would be wrong anyway if the file
    changed since. So the column is reported as 1 rather than confidently
    wrong — a squiggle in the wrong place is worse than one on the whole line.
    """
    out: list = []
    pending: dict = {}

    def flush() -> None:
        if not pending:
            return
        out.append(Finding(
            file=relative_to(pending["file"], project_root),
            line=pending.get("line", 1),
            column=1,
            message=pending.get("message", "could not be compiled"),
            severity="error",
            rule=pending.get("rule", "compileall"),
        ))
        pending.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        header = _COMPILEALL_HEADER.match(line)
        if header:
            flush()                       # a header ends the previous block
            pending["file"] = _repr_path(header.group("repr"))
            continue
        if not pending:
            continue
        located = _COMPILEALL_FILE.match(line)
        if located:
            # Prefer this path: it carries the line number the header lacks.
            pending["file"] = _quoted_path(located.group("repr"))
            pending["line"] = max(1, int(located.group("line")))
            continue
        failure = _COMPILEALL_ERROR.match(line)
        if failure:
            pending["message"] = failure.group("msg").strip()
            pending["rule"] = "compileall/" + failure.group("kind")
            flush()                       # the error line closes the block
    flush()                               # a block that never got its error line
    return out


# ── summaries (pure) ─────────────────────────────────────────────────────────

def _summarise_syntax(ok: bool, output: str) -> str:
    """The legacy one-line syntax summary. Unchanged text, on purpose."""
    if ok:
        return "passed (0 errors)"
    errors = output.strip()
    first_line = errors.splitlines()[0] if errors else "syntax error"
    return first_line[:200]


def _summarise_pyflakes(ok: bool, output: str) -> str:
    """The legacy one-line pyflakes summary. Unchanged text, on purpose.

    Counts non-empty **output lines**, not findings — the two can differ, and
    the historical number is the one the dialogs have always shown.
    """
    if ok:
        return "passed (0 warnings)"
    lines = [l for l in output.strip().splitlines() if l.strip()]
    count = len(lines)
    summary = lines[0][:200] if lines else "warnings found"
    if count > 1:
        summary += f" (+{count - 1} more)"
    return summary


# ── runners (I/O) ────────────────────────────────────────────────────────────

def _run_module(project_path: str, argv: list):
    """Invoke `python -m <argv>` with the project root as cwd."""
    return subprocess.run(
        [sys.executable, "-m", *argv],
        capture_output=True,
        text=True,
        cwd=project_path,
        creationflags=CREATE_NO_WINDOW,
    )


def run_syntax(project_path: str) -> CheckResult:
    """Run ``python -m compileall src/ -q``, in both output shapes."""
    src = os.path.join(project_path, "src")
    result = _run_module(project_path, ["compileall", src, "-q"])
    output = result.stdout + result.stderr
    ok = result.returncode == 0
    return CheckResult(
        ok=ok,
        summary=_summarise_syntax(ok, output),
        findings=[] if ok else parse_compileall_output(output, project_path),
        output=output,
    )


def run_pyflakes(project_path: str) -> CheckResult:
    """Run ``python -m pyflakes src/``, in both output shapes.

    **The two streams are parsed separately, because pyflakes means different
    things by them** — measured, not assumed:

      stdout   the file was analysed; these are its findings   -> warning
      stderr   the file could NOT be analysed at all           -> error

    So `duplicate argument 'a'` and `'return' outside function` arrive on
    stdout (the tree parsed, a later check failed), while a genuine parse
    failure and an unreadable file arrive on stderr — the latter in a second,
    column-less format. Concatenating the streams first throws that away and
    reports a file that could not be read at all as a lint warning.

    The distinction is the tool's own, so using it is not inference.

    The legacy `summary` still counts the combined text, because that is the
    number the Manager's dialogs have always shown.
    """
    src = os.path.join(project_path, "src")
    result = _run_module(project_path, ["pyflakes", src])
    output = result.stdout + result.stderr
    ok = result.returncode == 0
    findings: list = []
    if not ok:
        findings = (parse_pyflakes_output(result.stdout, project_path, "warning")
                    + parse_pyflakes_output(result.stderr, project_path, "error"))
    return CheckResult(
        ok=ok,
        summary=_summarise_pyflakes(ok, output),
        findings=findings,
        output=output,
    )


# ── legacy two-tuple API (unchanged behaviour) ───────────────────────────────

def run_syntax_check(project_path: str) -> tuple[bool, str]:
    """Run ``python -m compileall src/ -q``. Returns (passed, summary)."""
    result = run_syntax(project_path)
    return result.ok, result.summary


def run_pyflakes_check(project_path: str) -> tuple[bool, str]:
    """Run ``python -m pyflakes src/``. Returns (passed, summary)."""
    result = run_pyflakes(project_path)
    return result.ok, result.summary
