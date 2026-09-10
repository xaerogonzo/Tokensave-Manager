"""helpers/headless_analyzers.py — external analyzers the Manager runs itself.

**The dividing line this module sits on.** A producer the Manager *runs and
parses* emits `findings`: the Manager chose the invocation, read the raw output
and assigned the severity, exactly as it already does for pyflakes' two streams.
A verdict the Manager *receives already-rendered* — the editor's Problems panel
— is an **observation** and lives in `helpers/observations.py`. The two never
merge, and neither is ever summed into the other.

So everything here is an ordinary `findings` producer and needs no new plumbing:
same `Finding` shape, same envelope, same `DiagnosticStore` partition.

**One capability table, rows not branches** (project rule D2). Adding an
analyzer is adding a row plus a fixture captured from a real run. A field named
`is_ruff` re-creates the cascade the table replaces.

The table earns its keep on the first row: probe order is **per analyzer**, not
per platform. ruff is a native `.exe`, so `.exe` comes first; an npm-installed
tool is a `.cmd` shim, where the bare name is the one that fails. One hard-coded
order would be wrong for one of them.

**Availability is four states, not a boolean.** `configured != executable !=
healthy` is settled doctrine here (`helpers/pyscope.status`,
`resolve_agent_cli`'s three states), and the state that matters is `FAILED` —
the one that would otherwise render as "clean".

**Parsers are pure; runners do the I/O**, the split `quality_checks.py` uses.

No Tk. Safe to call from any thread.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess

from constants import CREATE_NO_WINDOW
from helpers.findings import Finding, relative_to

# ── availability ─────────────────────────────────────────────────────────────

#: Nothing configured and nothing on PATH. Not an error — the tool is optional.
UNCONFIGURED = "unconfigured"
#: A path was configured (or found) and is not there. This IS an error.
MISSING = "missing"
#: Resolved and executable.
READY = "ready"
#: It ran and could not complete. **Never renders as clean.**
FAILED = "failed"

AVAILABILITY = (UNCONFIGURED, MISSING, READY, FAILED)

#: What each state says to a person. Beside the constants so a new state cannot
#: be added without someone deciding what it reads as.
AVAILABILITY_TEXT = {
    UNCONFIGURED: "not configured",
    MISSING: "configured, executable missing",
    READY: "ready",
    FAILED: "failed to run",
}


# ── the shape one analyzer run produces ──────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class AnalyzerResult:
    """One analyzer, one invocation.

    ``ok`` is deliberately **not** "no findings". An analyzer that could not run
    is not ok, and an analyzer that ran and found things did its job. Collapsing
    those is how a missing tool reports as a clean bill of health.
    """

    key: str
    availability: str
    findings: list = dataclasses.field(default_factory=list)
    summary: str = ""
    detail: str = ""
    exe: str = ""
    #: Rows the tool emitted, before any `--paths` filtering. Reported because
    #: the count is the thing a reader needs in order to trust or distrust the
    #: rest, and it is not recoverable from a filtered finding list.
    rows: int = 0

    @property
    def ok(self) -> bool:
        return self.availability == READY

    @property
    def ran(self) -> bool:
        """Did the tool actually execute? `UNCONFIGURED` is not a failure."""
        return self.availability in (READY, FAILED)


# ── ruff ─────────────────────────────────────────────────────────────────────

#: Codes that mean **the file does not parse**, which is categorically different
#: from a lint opinion about a file that does.
#:
#: Measured, and it is the correction a policy written from memory gets wrong:
#: ruff reports a parse failure as `"code": "invalid-syntax"`, **not** as an
#: `E9xx` code. `E9` is kept because pycodestyle's E9 family is still reachable
#: when a configuration selects it, and `None` because some ruff versions emit a
#: syntax error with no code at all.
_RUFF_SYNTAX_CODES = frozenset({"invalid-syntax", "syntax-error"})


def ruff_severity(code: "str | None") -> str:
    """The Manager's severity policy for ruff. A policy, not a parsing detail.

    **ruff's own `severity` field is not consulted, and that is the whole
    point.** It exists — and on a real run over this repository's `src/` it read
    `"error"` for **1,812 rows out of 1,812**, including `S110`
    (try-except-pass) and `BLE001` (blind except), both of which this codebase
    does deliberately and documents. A field that says `error` about everything
    carries no information, and forwarding it would paint every unused import in
    the colour reserved for code that does not compile.

    That is the same judgement `quality_checks.run_pyflakes` already exercises
    over pyflakes' two streams: the producer decides, from something it can
    actually reason about. Here that is the rule code.

    Tested separately from the parser, so a future parser refactor cannot move
    severity semantics by accident.
    """
    if code is None:
        return "error"
    if code in _RUFF_SYNTAX_CODES or code.startswith("E9"):
        return "error"
    return "warning"


def parse_ruff_json(text: str, project_root: str) -> list:
    """Findings from `ruff check --output-format json`. Never raises on rows.

    Raises `ValueError` only when the payload is not the expected JSON array —
    that is a failed *run*, not a finding, and the caller turns it into
    `FAILED` rather than into silence.

    A row missing the fields a `Finding` needs is skipped rather than fatal: one
    unexpected row must not stop the valid ones after it from reaching the
    editor. That is `parse_pyflakes_output`'s rule, and the reason is the same.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("ruff JSON output was not an array")

    out: list = []
    for row in data:
        if not isinstance(row, dict):
            continue
        filename = row.get("filename")
        location = row.get("location") or {}
        if not filename or not isinstance(location, dict):
            continue
        end = row.get("end_location") or {}
        code = row.get("code")
        # A row with no code still has a message and a position, and dropping it
        # would drop exactly the syntax errors on the versions that omit it.
        rule = "ruff/%s" % (code or "invalid-syntax")
        out.append(Finding(
            file=relative_to(str(filename), project_root),
            line=max(1, int(location.get("row") or 1)),
            column=max(1, int(location.get("column") or 1)),
            end_line=max(1, int(end.get("row") or location.get("row") or 1)),
            end_column=max(1, int(end.get("column")
                                  or location.get("column") or 1)),
            message=str(row.get("message") or "").strip(),
            severity=ruff_severity(code),
            rule=rule,
        ))
    return out


def _summarise(key: str, findings: list, rows: int) -> str:
    """The one-line status text, in the shape the Run Checks rows already use.

    **One line, enforced rather than assumed.** pyright's messages are
    genuinely multi-line — it appends the assignability chain under the first
    sentence — and a raw newline here breaks a Tk row label and a CLI summary
    both. Only the first line is a summary; the rest is detail, and it travels
    on the `Finding` where a reader can see all of it.
    """
    if rows == 0:
        return "passed (0 findings)"
    first = findings[0] if findings else None
    if first is None:
        return "%d findings" % rows
    lead = (first.message or "").splitlines()
    head = ("%s:%d %s" % (first.file, first.line,
                          lead[0] if lead else ""))[:160]
    return "%s%s" % (head, " (+%d more)" % (rows - 1) if rows > 1 else "")


# ── the table ────────────────────────────────────────────────────────────────

# ── pyright ──────────────────────────────────────────────────────────────────

#: pyright's severities, mapped onto the envelope's closed set.
#:
#: **Forwarded, unlike ruff's** — and the contrast is the reason severity policy
#: belongs to the row rather than to this module. pyright's field varies with
#: `typeCheckingMode` and per-rule configuration, so it carries the project's
#: own decision about how much a given rule matters. ruff's said `error` for
#: 1,812 rows out of 1,812 and carried nothing.
_PYRIGHT_SEVERITY = {
    "error": "error",
    "warning": "warning",
    "information": "information",
}


def parse_pyright_json(text: str, project_root: str) -> list:
    """Findings from `pyright --outputjson`. Raises `ValueError` on a bad payload.

    Two things here are measured rather than remembered, and both are silent
    when wrong:

    **The payload is an object with `generalDiagnostics`**, not a top-level
    array like ruff's. A parser that iterates the document finds nothing and
    reports a clean project.

    **`range` is 0-BASED.** `"line": 6` is line 7. The envelope is 1-based
    everywhere in Python — `helpers/findings` states it, and the single
    conversion to VS Code's 0-based `Position` happens once in TypeScript — so
    every coordinate is incremented here, at the boundary where the tool's
    convention ends. Forwarding it raw puts every squiggle one line above the
    code it is about, which looks like an off-by-one in the editor rather than
    like a parser bug.
    """
    doc = json.loads(text)
    if not isinstance(doc, dict) or "generalDiagnostics" not in doc:
        raise ValueError(
            "pyright JSON output carried no generalDiagnostics array")
    rows = doc.get("generalDiagnostics")
    if not isinstance(rows, list):
        raise ValueError("pyright generalDiagnostics was not an array")

    out: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filename = row.get("file")
        rng = row.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or start
        if not filename or not isinstance(start, dict):
            continue
        rule = row.get("rule") or ""
        out.append(Finding(
            file=relative_to(str(filename), project_root),
            # +1: pyright is 0-based, the envelope is 1-based.
            line=int(start.get("line") or 0) + 1,
            column=int(start.get("character") or 0) + 1,
            end_line=int((end or {}).get("line") or start.get("line") or 0) + 1,
            end_column=int((end or {}).get("character")
                           or start.get("character") or 0) + 1,
            message=str(row.get("message") or "").strip(),
            severity=_PYRIGHT_SEVERITY.get(row.get("severity"), "warning"),
            # A diagnostic without a rule is still a diagnostic; grouping it
            # under the producer beats dropping it.
            rule="pyright/%s" % rule if rule else "pyright",
        ))
    return out


# ── markdownlint ─────────────────────────────────────────────────────────────

# `markdownlint-cli2` prints two shapes, and which one you get depends on
# whether the rule knows a column:
#
#     bad.md:3 error MD001/heading-increment Heading levels should only ...
#     bad.md:7:10 error MD009/no-trailing-spaces Trailing spaces [Expected: 0 ...]
#
# Non-greedy path with the digits anchored, for the same reason pyflakes needs
# it: a Windows path contains a colon of its own.
_MDL_WITH_COL = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+) +\w+ +(?P<rule>\S+) +(?P<msg>.+)$")
_MDL_NO_COL = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+) +\w+ +(?P<rule>\S+) +(?P<msg>.+)$")


def parse_markdownlint_output(text: str, project_root: str) -> list:
    """Findings from `markdownlint-cli2`'s default output. Never raises.

    **The findings are on stderr; stdout carries only the banner and the
    summary.** Measured, and inverted from both the obvious guess and from
    ruff. A runner that reads stdout gets

        markdownlint-cli2 v0.23.2 (markdownlint v0.41.1)
        Finding: *.md
        Linting: 2 files
        Summary: 5 issues in 1 file

    parses zero findings out of it, and reports the project clean while the
    tool is saying otherwise. `AnalyzerSpec.stream` is what stops that, and it
    is the reason that field exists.

    Severity is **not** taken from the tool. Every line says `error`, which is
    the ruff situation again: a uniform field carries no information. Markdown
    style violations do not stop anything from working, so they are warnings.
    """
    out: list = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        column = 1
        match = _MDL_WITH_COL.match(line)
        if match:
            column = max(1, int(match.group("col")))
        else:
            match = _MDL_NO_COL.match(line)
            if not match:
                continue           # the banner, or anything else unexpected
        # `MD001/heading-increment` -> code `MD001`; the descriptive half is
        # already in the message.
        code = match.group("rule").split("/")[0]
        out.append(Finding(
            file=relative_to(match.group("file"), project_root),
            line=max(1, int(match.group("line"))),
            column=column,
            message=match.group("msg").strip(),
            severity="warning",
            rule="markdownlint/%s" % code,
        ))
    return out


@dataclasses.dataclass(frozen=True)
class AnalyzerSpec:
    """One analyzer, described by capability rather than by vendor."""

    #: Stable identifier. Referenced by config keys and check rows.
    key: str
    label: str
    #: `manager-config.json` key holding an explicit path. Empty string when
    #: absent, never the bare command name — the documented convention, and what
    #: stops a caller shelling a bare name (see `gotchas/windows-subprocess.md`).
    config_key: str
    #: Probe order, **per analyzer**. `.exe`-first for a native binary,
    #: `.cmd`-first for an npm shim. This field is why this is a table.
    probe_names: tuple
    #: Fixed arguments; the targets are appended.
    argv: tuple
    #: Pure `(text, project_root) -> list[Finding]`.
    parse: object
    #: Exit codes that mean "ran fine, here are the rows". Anything else is
    #: `FAILED`. All three use 0 for clean and 1 for "found violations"; 2 is a
    #: real error, and treating it as a clean run is the failure this guards.
    ok_returncodes: tuple = (0, 1)
    #: Which stream the FINDINGS arrive on. **Measured per tool, not assumed.**
    #: ruff and pyright print JSON to stdout; `markdownlint-cli2` puts its
    #: findings on **stderr** and only its banner on stdout. A runner that reads
    #: stdout for all three parses a banner, finds nothing, and reports markdown
    #: clean while the tool is saying otherwise.
    stream: str = "stdout"
    #: What to analyse when the caller names nothing, relative to the project
    #: root (which is the subprocess cwd). A source linter takes the tree; a
    #: markdown linter needs a glob, because it will not walk a directory.
    default_targets: tuple = (".",)
    #: Directories to probe after PATH. Covers the case
    #: `install_codegraph.detect_codegraph_after_install` records: a global
    #: install lands somewhere PATH will not mention until the shell is
    #: restarted, and without this the row reads "not configured" immediately
    #: after a successful install.
    fallback_dirs: tuple = ()
    #: Which package manager obtains it, and under what name. Empty when the
    #: Manager has no install route and the row should say so rather than
    #: offering a button. See `helpers/install_analyzers.py`.
    installer: str = ""
    package: str = ""

    def __post_init__(self) -> None:
        if not callable(self.parse):
            raise ValueError("%s: parse must be callable" % self.key)
        if not self.probe_names:
            raise ValueError("%s: probe_names must not be empty" % self.key)


#: The one table. Every row's output format was captured from a real run into
#: `tests/fixtures/headless_analyzers/`; none was written from memory, and the
#: two facts that would have been wrong if it had been are recorded there.
#:
#: Note how little the three rows have in common — different probe order,
#: different argv, different stream, different severity policy, different notion
#: of a target. That is the argument for the table: every one of those
#: differences would otherwise be an `if` at a call site.
ANALYZERS: tuple = (
    AnalyzerSpec(
        key="ruff",
        label="ruff",
        config_key="ruff_exe",
        # A native binary, so `.exe` leads.
        probe_names=("ruff.exe", "ruff"),
        # `--no-cache` is load-bearing, and it was measured rather than
        # assumed: without it ruff writes a `.ruff_cache/` directory into the
        # project being analysed. That is a side effect the user did not ask
        # for from a read-only report, and it would need gitignoring in every
        # project the Manager ever pointed at. pyright and markdownlint were
        # checked on the same clean tree and write nothing.
        #
        # This is what keeps `analyze` in PURE_READ rather than pushing it into
        # OBSERVE_REFRESH beside `doctor`.
        argv=("check", "--no-cache", "--output-format", "json"),
        parse=parse_ruff_json,
        # NOT npm. The `ruff` package on that registry is an unrelated ES6
        # coroutine library; only WASM bindings exist under @astral-sh. uv is
        # the route this codebase already knows -- PyScope arrives the same
        # way, which is why ~/.local/bin is already a trusted location.
        fallback_dirs=("~/.local/bin",),
        installer="uv", package="ruff",
    ),
    AnalyzerSpec(
        key="pyright",
        label="pyright",
        config_key="pyright_exe",
        # An npm shim: `.cmd` FIRST. `CreateProcess` appends only `.exe` and
        # ignores PATHEXT, so the bare name raises WinError 2 from Python while
        # working perfectly in a shell. See `gotchas/windows-subprocess.md`.
        probe_names=("pyright.cmd", "pyright"),
        argv=("--outputjson",),
        parse=parse_pyright_json,
        fallback_dirs=("%APPDATA%/npm",),
        installer="npm", package="pyright",
    ),
    AnalyzerSpec(
        key="markdownlint",
        label="markdownlint",
        config_key="markdownlint_exe",
        # Two names, because the modern package installs the `-cli2` binary and
        # the older one does not. Probing both is how the row absorbs that
        # rather than a call site doing it.
        probe_names=("markdownlint-cli2.cmd", "markdownlint-cli2",
                     "markdownlint.cmd", "markdownlint"),
        argv=(),
        parse=parse_markdownlint_output,
        # The measured one. Findings on stderr, banner on stdout.
        stream="stderr",
        # It will not walk a directory; it wants a glob.
        default_targets=("**/*.md",),
        fallback_dirs=("%APPDATA%/npm",),
        installer="npm", package="markdownlint-cli2",
    ),
)

BY_KEY = {a.key: a for a in ANALYZERS}


# ── detection and running (I/O) ──────────────────────────────────────────────

def resolve(spec: AnalyzerSpec, configured: str = "") -> "tuple[str, str]":
    """`(availability, exe)` for one analyzer. Does not run it.

    An explicitly configured path that is not there is `MISSING`, never
    `UNCONFIGURED`: the user told us where it lives and it is not there, which
    is a fixable error rather than an absent optional tool. Silently falling
    back to PATH would run a *different* binary than the one that was named.
    """
    configured = (configured or "").strip()
    if configured:
        return (READY, configured) if os.path.isfile(configured) \
            else (MISSING, configured)
    for name in spec.probe_names:
        found = shutil.which(name)
        if found:
            return READY, found
    # PATH did not know it. A global install that has just happened often
    # lands somewhere the current process's PATH will not mention until the
    # shell restarts, and reporting "not configured" straight after a
    # successful install is how a working feature looks broken.
    #
    # `expanduser` inside the function, never at import: the Manager is
    # launched with a fixed HOME in tests, and resolving at import time would
    # bake in whatever the environment said when the module first loaded.
    for directory in spec.fallback_dirs:
        base = os.path.expandvars(os.path.expanduser(directory))
        for name in spec.probe_names:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return READY, candidate
    return UNCONFIGURED, ""


def run(spec: AnalyzerSpec, project_root: str, configured: str = "",
        targets: "list | None" = None,
        timeout: int = 120) -> AnalyzerResult:
    """Run one analyzer over *project_root*. Never raises.

    Every non-success path produces `MISSING`, `UNCONFIGURED` or `FAILED` — and
    never an empty `READY`, which would be the tool reporting a clean bill of
    health it did not earn.
    """
    availability, exe = resolve(spec, configured)
    if availability != READY:
        return AnalyzerResult(
            key=spec.key, availability=availability, exe=exe,
            summary=AVAILABILITY_TEXT[availability],
            detail=("configured as %s, which is not a file" % exe
                    if availability == MISSING else
                    "not found as %s" % " / ".join(spec.probe_names)))

    argv = [exe, *spec.argv, *(targets or list(spec.default_targets))]
    try:
        proc = subprocess.run(
            argv, capture_output=True, cwd=project_root, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            # Decode explicitly. A legacy console codepage decides otherwise,
            # and `errors="replace"` keeps a stray byte from raising out of a
            # parser and reading like our own crash.
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AnalyzerResult(key=spec.key, availability=FAILED, exe=exe,
                              summary=AVAILABILITY_TEXT[FAILED],
                              detail=str(exc))

    if proc.returncode not in spec.ok_returncodes:
        return AnalyzerResult(
            key=spec.key, availability=FAILED, exe=exe,
            summary=AVAILABILITY_TEXT[FAILED],
            detail=("exit %d: %s" % (proc.returncode,
                                     (proc.stderr or "").strip()[:300])))

    # Read the stream this tool actually writes findings to. Getting it wrong
    # is silent: the other stream parses to zero rows and reports clean.
    raw = proc.stdout if spec.stream == "stdout" else proc.stderr
    try:
        found = spec.parse(raw or "", project_root)
    except (ValueError, TypeError) as exc:
        # It exited acceptably and produced something we cannot read. That is a
        # failed run, not zero findings.
        return AnalyzerResult(key=spec.key, availability=FAILED, exe=exe,
                              summary=AVAILABILITY_TEXT[FAILED],
                              detail="could not parse output: %s" % exc)

    return AnalyzerResult(key=spec.key, availability=READY, exe=exe,
                          findings=found, rows=len(found),
                          summary=_summarise(spec.key, found, len(found)))


def run_all(project_root: str, config: "dict | None" = None,
            targets: "list | None" = None) -> list:
    """Every analyzer in the table, in table order. Never raises."""
    config = config or {}
    return [run(spec, project_root, config.get(spec.config_key, ""), targets)
            for spec in ANALYZERS]
