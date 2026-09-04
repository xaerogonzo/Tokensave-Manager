"""cli.py — the Manager's headless surface.

Everything the GUI can do that a machine might want to ask for, exposed as
JSON over stdout. Written for three consumers that do not exist yet: VS Code
tasks, a VS Code extension, and eventually a Visual Studio companion server.
Because all three will depend on the shape of this output, the contract is
fixed here rather than discovered later.

**The contract**

  stdout   exactly one complete envelope, and nothing else, ever
  stderr   human diagnostics only
  exit     semantic (see the EXIT_* constants) so a caller can tell
           "doctor found problems" from "the CLI itself failed"

The envelope always carries `schema_version` and `cli_version`, so a consumer
that predates a change can say "this Manager speaks a dialect I don't know"
instead of failing to parse.

**`--project` is mandatory** on every command here. Silent cwd inference is the
exact mistake that produced the MCP scope collision this project spent three
investigations on; it is not going to be reintroduced in the Manager's own
tooling. A caller always knows its project — VS Code tasks pass
`${workspaceFolder}` — so there is nothing to infer.

**What this module may import.** No Tk, and no application controllers.
Outside the standard library it may use the Tk-free `constants`/config layer
and `helpers/`, and nothing else. (The older wording here said "only from
`helpers/`", which was never true — `constants.APP_VERSION` has been imported
since the first commit. Stating the rule accurately is the point of having
one.)

It must also never take the GUI's single-instance lock: the CLI is expected to
run *while* the Manager is open.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from constants import APP_VERSION
from helpers import commands
from helpers.detection import _root_path
from helpers.findings import to_envelope

#: Bumped only when the envelope's shape changes incompatibly.
#:
#: The rule: **adding** an optional field is compatible and does not bump this;
#: removing or renaming a field, or changing its type or meaning, does. That is
#: why `findings` arrived without a bump — a consumer that predates it simply
#: never looks at the key.
SCHEMA_VERSION = 1

EXIT_OK = 0                  # success
EXIT_FAILED = 1              # the operation ran and reported problems
EXIT_USAGE = 2               # invalid invocation (argparse also uses 2)
EXIT_PREREQUISITE = 3        # a required tool or path is missing
EXIT_VERIFY_FAILED = 4       # an operation ran but could not be verified

#: `--base auto` means "ask the repository", not "assume master".
AUTO_BASE = "auto"


class Result:
    """What a command handler returns, before it becomes an envelope."""

    def __init__(self, code: int = EXIT_OK, data: "dict | None" = None,
                 warnings: "list | None" = None, error: str = "",
                 human: str = "", findings: "list | None" = None):
        self.code = code
        self.data = data or {}
        self.warnings = warnings or []
        self.error = error
        self.human = human
        self.findings = findings or []


def _envelope(command: str, result: Result) -> dict:
    """The stable payload. Key order is fixed for readable diffs in logs.

    `findings` is **top-level rather than inside `data`** because it is a
    cross-command contract: `checks`, `doctor` and `scout` all emit the same
    shape, and the consumer renders them identically without knowing which
    command produced them. `data` is the per-command payload, which is a
    different thing. The key is always present — an empty list for commands
    that produce none — so no consumer has to branch on its absence.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "cli_version": APP_VERSION,
        "command": command,
        "ok": result.code == EXIT_OK,
        "data": result.data,
        "findings": to_envelope(result.findings),
        "warnings": result.warnings,
        "error": result.error or None,
    }


def _resolve_project(raw: str) -> str:
    """Absolute, existing project root — or raise for a prerequisite exit."""
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise _Prerequisite(f"project path does not exist: {path}")
    return path


class _Prerequisite(Exception):
    """A required path or executable is missing. Maps to EXIT_PREREQUISITE."""


def _load_manager_config(explicit_path: str = "") -> dict:
    """The Manager's config, from `--config` when given.

    Without it the config is read from beside the executable
    (`constants._CONFIG_PATH`), which is right for a normal install and wrong
    for a CLI shipped somewhere else — the VS Code extension bundles it under
    `extension/bin/`, where no config exists. Rather than have the CLI search
    for one (inference, which this whole roadmap keeps removing), the caller
    says where it is.
    """
    if not explicit_path:
        from helpers.config import _load_config
        return _load_config()
    path = os.path.abspath(os.path.expanduser(explicit_path))
    if not os.path.isfile(path):
        raise _Prerequisite(f"config file does not exist: {path}")
    try:
        with open(path, encoding="utf-8-sig") as fh:   # utf-8-sig strips a BOM
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise _Prerequisite(f"could not read {path}: {exc}") from exc


def _tokensave_exe_from(cfg: dict, where: str) -> str:
    """The configured tokensave, from a config already in hand.

    Split from `_tokensave_exe` so a command needing both the executable and
    other config keys reads the file once. `doctor` needs `tokensave_exe` for
    the stale scan and the `doctor_*` keys for the audit, and loading twice
    doubles the I/O for no benefit.
    """
    exe = (cfg.get("tokensave_exe") or "").strip()
    if not exe:
        raise _Prerequisite(
            f"no tokensave_exe configured in {where} — set it in the "
            "Manager's Settings, or pass --config to point at an install")
    return exe


def _tokensave_exe(explicit_config: str = "") -> str:
    return _tokensave_exe_from(_load_manager_config(explicit_config),
                               explicit_config or "the Manager's config")


def _is_frozen() -> bool:
    """True when running from the Nuitka onefile build.

    Same marker `constants._resolve_base_dir` keys off. Note its VALUE is a
    parent PID, not a path — only its presence is meaningful.
    """
    return bool(os.environ.get("NUITKA_ONEFILE_PARENT"))


# ── commands ─────────────────────────────────────────────────────────────────

def _resolve_paths(project: str, raw_paths: list) -> "tuple[list, list]":
    """Repo-relative paths to filter findings by, plus which of them exist.

    Returns `(requested, matched)`, both repo-relative with forward slashes.

    **A path outside the project is an error, not an empty result.** Silently
    returning no findings for an out-of-root path is indistinguishable from a
    clean file, so an editor sending a stale or mistyped URI would be told
    everything was fine. Same reasoning as the request inbox's containment
    rule, applied to the other direction of travel.

    `matched` is reported separately so a consumer can tell "this file is
    clean" from "that path is not part of this project" — a distinction the
    finding list alone cannot carry, because both look like zero rows.
    """
    root = os.path.normcase(os.path.realpath(project))
    requested, matched = [], []
    for raw in raw_paths:
        candidate = str(raw).replace(chr(92), "/").strip()
        if not candidate:
            continue
        absolute = (candidate if os.path.isabs(candidate)
                    else os.path.join(project, candidate))
        resolved = os.path.normcase(os.path.realpath(absolute))
        if resolved != root and not resolved.startswith(
                root.rstrip("\\/") + os.sep):
            raise _Prerequisite(
                f"path is outside the project: {raw}")
        relative = os.path.relpath(resolved, root).replace(chr(92), "/")
        requested.append(relative)
        if os.path.exists(absolute):
            matched.append(relative)
    return requested, matched


def _filter_findings(findings: list, relative_paths: list) -> list:
    """Only the findings whose file is one of `relative_paths`.

    A filter, not a second format: same producer, same fields, fewer rows. A
    consumer must not have to know whether `--paths` was used to read the
    result.
    """
    if not relative_paths:
        return findings
    wanted = {p.replace(chr(92), "/").lstrip("./") for p in relative_paths}
    kept = []
    for finding in findings:
        name = str(getattr(finding, "file", "") or "").replace(chr(92), "/")
        if name.lstrip("./") in wanted:
            kept.append(finding)
    return kept


def _cmd_checks(args) -> Result:
    """Syntax + pyflakes over the project. Read-only."""
    from helpers.quality_checks import run_pyflakes, run_syntax
    project = _resolve_project(args.project)

    # Both checks shell out to `sys.executable -m compileall / -m pyflakes`.
    # Under the onefile build `sys.executable` is the *extracted* binary, not a
    # Python interpreter, so the subprocess dies with a bare
    # "[WinError 2] The system cannot find the file specified". Say what is
    # actually wrong instead: this needs an interpreter the frozen build has no
    # usable copy of.
    if _is_frozen():
        raise _Prerequisite(
            "`checks` needs a Python interpreter with pyflakes installed, "
            "which the packaged CLI does not provide — run it from a source "
            "checkout, or use the Manager's Run Checks dialog")

    requested, matched = _resolve_paths(project, args.paths)

    syntax = run_syntax(project)
    flakes = run_pyflakes(project)
    # `output` stays the one-line summary the Manager's dialogs show; the full
    # detail now travels as `findings`, which is what the truncation used to
    # hide. Both are kept: one is a status line, the other is the report.
    data = {
        "syntax": {"ok": syntax.ok, "output": syntax.summary},
        "pyflakes": {"ok": flakes.ok, "output": flakes.summary},
    }
    findings = syntax.findings + flakes.findings
    if requested:
        findings = _filter_findings(findings, requested)
        # Reported so "this file is clean" and "that path is not in this
        # project" are distinguishable; both otherwise render as zero rows.
        data["requested_paths"] = requested
        data["matched_paths"] = matched
        # The pass/fail verdict has to follow the filter too, or a scoped run
        # on a clean file would still report the whole project's failures.
        clean = not findings
        return Result(EXIT_OK if clean else EXIT_FAILED, data,
                      findings=findings,
                      human=("checks passed" if clean else
                             f"{len(findings)} finding(s) in "
                             f"{len(requested)} file(s)"))
    if syntax.ok and flakes.ok:
        return Result(EXIT_OK, data, human="checks passed")
    failed = [n for n, ok in (("syntax", syntax.ok), ("pyflakes", flakes.ok))
              if not ok]
    return Result(EXIT_FAILED, data, findings=findings,
                  human=f"checks failed: {', '.join(failed)} "
                        f"({len(findings)} finding(s))")


def _audit_findings(project: str, raw: dict) -> "tuple[list, dict]":
    """The anti-monolith cap audit, as findings plus a summary block.

    Separate from the stale scan because the two have different prerequisites:
    this needs nothing but the source tree, while the scan needs a configured
    `tokensave_exe`. A project without tokensave should still get its audit.

    Cap violations carry `file` and `line` since R12-10 — the auditors always
    had `node.lineno` and used to discard it while formatting.
    """
    from helpers.doctor_rules import _audit_project_tree
    from helpers.findings import Finding

    skip = set(raw.get("doctor_skip_paths") or [])
    overrides = raw.get("doctor_path_overrides") or {}

    violations, exempt, scanned = _audit_project_tree(project, skip, overrides)
    findings = [
        Finding(file=v.file, line=v.line, symbol=v.symbol, message=v.message,
                severity="warning", rule="doctor/audit")
        for v in violations
    ]
    return findings, {"files_scanned": scanned,
                      "violation_count": len(violations),
                      "exempt_count": len(exempt)}


def _cmd_doctor(args) -> Result:
    """Observe stale state and audit the tree. Read-only — never fixes.

    **The audit is advisory and does not move the exit code.** This repo runs
    it `continue-on-error` in its generated CI for exactly that reason: cap
    violations are a health signal, not a broken build, and 152 of them on a
    healthy tree would otherwise make `doctor` permanently red. Stale entries
    are the failure condition, as before — the exit codes here are unchanged.
    """
    from helpers import housekeeping
    from helpers.doctor_service import scan_stale
    project = _resolve_project(args.project)

    # One read of the config for both halves: the audit wants the `doctor_*`
    # keys, the scan wants `tokensave_exe`.
    raw = _load_manager_config(args.config)

    # Audit first: it needs no tokensave, so its findings survive a scan that
    # cannot run at all.
    findings, audit = _audit_findings(project, raw)

    try:
        exe = _tokensave_exe_from(raw, args.config or "the Manager's config")
        scan = scan_stale(project, exe, timeout=args.timeout)
    except _Prerequisite as exc:
        # Still EXIT_PREREQUISITE, not "unverified": a missing `tokensave_exe`
        # is something the user can fix, and code 3 is what makes the extension
        # offer Settings. The audit findings ride along regardless — they never
        # needed tokensave, and withholding them would help nobody.
        return Result(EXIT_PREREQUISITE, {"scanned": False, "audit": audit},
                      findings=findings, error=str(exc),
                      human=f"doctor: audit only ({audit['violation_count']} "
                            f"violation(s)); stale scan skipped: {exc}")
    if not scan.ok:
        # "we could not find out" is NOT "nothing to clean" — the whole point
        # of DoctorScanResult.ok. Report it as unverified, not as clean.
        return Result(EXIT_VERIFY_FAILED, {"scanned": False, "audit": audit},
                      findings=findings, error=scan.error,
                      human=f"doctor scan failed: {scan.error}")

    stale = housekeeping.parse_stale_entries(scan.transcript)
    # tokensave TRUNCATES its own list — ten bullets, then "… and 2 more" — so
    # counting the entries we could name under-reports the total. The header
    # carries the real number and is authoritative; the named list is what we
    # can act on. Reporting only the shorter one presented 12 stale projects
    # as 10.
    named = [getattr(e, "path", str(e)) for e in stale]
    total = housekeeping.parse_stale_total(scan.transcript)
    if total is None:
        total = len(named)
    data = {
        "scanned": True,
        "exit_code": scan.exit_code,
        "stale_count": total,
        "stale": named,
        "stale_truncated": total > len(named),
        "audit": audit,
    }
    advisory = f" ({audit['violation_count']} audit finding(s))" \
        if audit["violation_count"] else ""
    if not total:
        return Result(EXIT_OK, data, findings=findings,
                      human=f"doctor: no stale entries{advisory}")
    shown = f" ({len(named)} listed)" if data["stale_truncated"] else ""
    return Result(EXIT_FAILED, data, findings=findings,
                  human=f"doctor: {total} stale entr"
                        f"{'y' if total == 1 else 'ies'}{shown}{advisory}")


def _cmd_sync(args) -> Result:
    """Refresh shadow links, then re-index. Mutating (local index only)."""
    from helpers.sync_service import run_sync
    project = _resolve_project(args.project)

    res = run_sync(project, _tokensave_exe(args.config), force=args.force,
                   timeout=args.timeout)
    data = {
        "argv": res.argv,
        "returncode": res.returncode,
        # Three-valued: True / False / null, where null means tokensave
        # did not report counts. A caller deciding whether to refresh
        # must not read "we could not tell" as "nothing happened".
        "changed": res.changed,
        "counts": res.counts,
        "shadows": {"ran": res.shadows.ran,
                    "created": res.shadows.created,
                    "failed": res.shadows.failed},
        "output": res.output,
    }
    warnings = []
    if res.shadows.failed:
        warnings.append(
            f"{res.shadows.failed} shadow link(s) could not be created")
    if res.ok:
        return Result(EXIT_OK, data, warnings, human="sync complete")
    if "not found" in res.error:
        return Result(EXIT_PREREQUISITE, data, warnings, error=res.error,
                      human=res.error)
    return Result(EXIT_FAILED, data, warnings, error=res.error,
                  human=res.error or f"sync failed (exit {res.returncode})")


#: Scout kinds, mapped to the closed severity set. Chosen here, in the
#: producer, because the consumer must never infer severity from a rule name.
#: Dead code is the only one that is a probable defect rather than a shape
#: worth knowing about, so it is the only warning.
_SCOUT_SEVERITY = {
    "complexity": "information",
    "god_class": "information",
    "god_file": "information",
    "dead_code": "warning",
}


def _cmd_scout(args) -> Result:
    """Refactor-scout findings. Read-only, and deliberately never fails.

    **No LLM is involved.** `run_scout` reads `.tokensave/tokensave.db`
    directly and returns grounded findings; the LLM only enters the picture
    when a user clicks Investigate in the Manager's dialog, which is a
    different code path entirely.

    Exit stays `EXIT_OK` even with findings, matching `test-gaps`: this is a
    report of suggestions, not a gate. A command that goes red whenever a
    codebase has a complex function is a command people turn off.
    """
    from helpers.findings import Finding
    from helpers.refactor_scout import run_scout
    project = _resolve_project(args.project)

    # Honour the suppressions made in the Manager's scout dialog. Without this
    # a finding dismissed there came straight back in the editor, which makes
    # the Ignore button look broken and is the sort of split-brain the single
    # findings contract exists to avoid.
    try:
        ignored = set(_load_manager_config(args.config)
                      .get("refactor_scout_ignored") or [])
    except _Prerequisite:
        ignored = set()

    try:
        by_kind, suppressed = run_scout(project, ignored)
    except FileNotFoundError as exc:
        # Scout is only as good as the index. Say which index, and how to make
        # one, rather than reporting "no findings" from a tree never scanned.
        raise _Prerequisite(str(exc)) from exc

    findings: list = []
    counts: dict = {}
    for kind, items in by_kind.items():
        counts[kind] = len(items)
        for item in items:
            findings.append(Finding(
                file=item.file, line=item.line, symbol=item.symbol,
                message=item.message,
                severity=_SCOUT_SEVERITY.get(kind, "information"),
                rule=f"scout/{kind}",
            ))
    data = {"counts": counts, "total": len(findings),
            "suppressed": suppressed}
    return Result(EXIT_OK, data, findings=findings,
                  human=f"scout: {len(findings)} finding(s)"
                        + (f", {suppressed} suppressed" if suppressed else ""))


#: Cap on any list this command emits. Counts stay exact; the lists are a
#: convenience. A large monorepo should not make an editor parse a payload with
#: tens of thousands of entries just to draw a tree.
_LIST_CAP = 200


def _capped(items: list, key=None) -> dict:
    """A bounded view of *items*: the values, plus how many there really were."""
    shown = items[:_LIST_CAP]
    block = {"total_count": len(items),
             "truncated": len(items) > _LIST_CAP,
             "items": [key(i) for i in shown] if key else shown}
    return block


def _cmd_tests(args) -> Result:
    """Test discovery: what exists, what is uncovered, what looks stale.

    Read-only and AST-only — no pytest runs here. Use `test-run` for that.

    `--detail` adds `data.test_cases`: one record per `def test_*`, with the
    nodeid to run it by and the 1-based range to put a cursor on. It is opt-in
    because the default payload feeds a tree that only shows counts, and this
    repository alone has ~3000 definitions.

    **A `test_case` is a definition, not a promise pytest will collect it.**
    Discovery is an AST walk, which is what lets it answer with pytest absent
    and without spawning anything; what pytest actually collects is decided by
    a run. Measured on this repository: 2928 definitions against 2927 distinct
    collected bases, the difference being one shadowed name that has since
    been fixed, with nothing discovered that was not collected and nothing
    collected that was not discovered.
    """
    from helpers.test_discovery import (detect_stale_tests, list_test_cases,
                                        list_test_files, scan_coverage_gaps)
    project = _resolve_project(args.project)

    files = list_test_files(project)
    coverage = scan_coverage_gaps(project)
    uncovered = [row for row in coverage if not row.has_tests]
    stale = detect_stale_tests(project)

    data = {
        "test_files": _capped(files, lambda f: {"name": f.name,
                                                "test_count": f.test_count}),
        "test_count": sum(f.test_count for f in files),
        "uncovered": _capped(uncovered, lambda r: r.rel_path),
        "stale": _capped(stale, lambda s: {"test": s.test_name,
                                           "reason": s.reason,
                                           "detail": s.detail}),
    }
    if getattr(args, "detail", False):
        # Deliberately uncapped, like `test-run`'s results and for the same
        # reason: a Test Explorer missing the tail of its own tree is worse
        # than a large payload, and a cap here would be invisible to it.
        data["test_cases"] = [
            {"nodeid": c.nodeid, "name": c.name, "class_name": c.class_name,
             "file": c.file, "line": c.line, "end_line": c.end_line,
             "markers": list(c.markers)}
            for c in list_test_cases(project)]

    return Result(EXIT_OK, data,
                  human=f"tests: {data['test_count']} test(s) in "
                        f"{len(files)} file(s), {len(uncovered)} source file(s) "
                        f"with no test, {len(stale)} stale signal(s)")


#: What a `test-run` actually did, beyond its pass/fail counts.
#:
#: `passed / failed / skipped` cannot distinguish "your tests failed" from
#: "pytest never started" from "the Manager killed the run on a timeout", and a
#: consumer needs to, because the remedy is different in each case. The exit
#: code already says whether the result could be trusted — this says why not.
RUN_COMPLETED = "completed"          # a summary was read
RUN_NO_TESTS = "no_tests"            # pytest said it collected nothing
RUN_TIMEOUT = "timeout"              # the Manager stopped it
RUN_COLLECTION_ERROR = "collection_error"   # it started but could not collect
RUN_PYTEST_MISSING = "pytest_missing"       # it never started
RUN_UNREADABLE = "unreadable"        # it ran, but the output made no sense


def _classify_run(output: str) -> str:
    """Why no pytest summary could be read. Never guesses `completed`.

    Ordered most-specific first, and every branch keyed on something pytest or
    the runner actually prints. A pattern that does not match falls through to
    `unreadable`, which is honest — inventing a more specific cause from an
    unrecognised message would be the same class of confident wrongness this
    roadmap exists to remove.
    """
    text = (output or "").lower()
    if "no module named pytest" in text or "pytest: command not found" in text:
        return RUN_PYTEST_MISSING
    if "timed out" in text or "timeout" in text:
        return RUN_TIMEOUT
    if ("error collecting" in text or "errors during collection" in text
            or "internalerror" in text):
        return RUN_COLLECTION_ERROR
    return RUN_UNREADABLE


def _cmd_test_run(args) -> Result:
    """Run the suite once and report structured counts.

    **Exit codes are decided here, not inherited from pytest.** In particular a
    run whose summary could not be parsed is `EXIT_VERIFY_FAILED`, never
    `EXIT_OK`: "we could not find out" is not "it passed", the same distinction
    `DoctorScanResult.ok` exists to preserve.

    **No findings are emitted.** A failing test is not a diagnostic about a
    line of source, and turning a red suite into thousands of Problems entries
    would bury the ones that are.

    **`data.tests` is always present, and is not capped.** It is an *additive*
    extension to this envelope, not an unchanged one: `schema_version` stays 1
    because adding an optional field is compatible, and every field that was
    here before still means what it did. It is exempt from `_LIST_CAP` on
    purpose -- a truncated result set would leave Test Explorer items silently
    un-attributed, which is a worse failure than a large payload. Measured on
    this repository's own suite under `--markers "not tk"`: 2785 records, a
    436 KB array in a 456 KB envelope.

    **`--tests` and `--markers` are alternatives.** No composition rule is
    defined for the pair -- intersection and precedence are both defensible,
    which is exactly why guessing one would be wrong -- so passing both is a
    usage error naming them.

    **The headline counts still come from pytest's footer.** `data.tests` is
    counted independently from the progress lines, and when the two disagree
    that is reported as a warning rather than reconciled: two measurements of
    one run differing is information, and silently preferring one discards it.
    """
    import time
    from helpers import pytest_report
    from helpers.smoke_runner import parse_pytest_summary, run_pytest_selection
    from helpers.test_lock import TestRunBusy, test_run_lock
    project = _resolve_project(args.project)

    if _is_frozen():
        # `run_pytest_selection` invokes `sys.executable -m pytest`, and under
        # a Nuitka onefile build sys.executable is the EXTRACTED BINARY rather
        # than an interpreter. The subprocess therefore dies with a bare
        # "[WinError 2] The system cannot find the file specified", which is
        # true and useless. Same limitation and same treatment as `checks`.
        raise _Prerequisite(
            "`test-run` needs a Python interpreter with pytest installed, "
            "which the packaged CLI does not provide — point the extension at "
            "a source checkout (tokensaveManager.managerPath), or run the "
            "suite from the Manager's Test Manager dialog")

    nodeids = tuple(getattr(args, "tests", ()) or ())
    markers = (getattr(args, "markers", "") or "").strip()
    if nodeids and markers:
        return Result(EXIT_USAGE,
                      error="--tests and --markers are mutually exclusive; "
                            "pass a marker expression or a list of test ids, "
                            "not both",
                      human="--tests and --markers are mutually exclusive")

    # Checked here rather than inferred from an empty result: "there is no
    # suite" is a prerequisite the user can fix, and it must not look like
    # "the suite could not be read".
    if not os.path.isdir(os.path.join(project, "tests")):
        raise _Prerequisite(f"no tests/ directory in {project}")

    try:
        with test_run_lock(project):
            started = time.monotonic()
            output, junit = run_pytest_selection(project, nodeids=nodeids,
                                                 markers=markers)
            passed, total = parse_pytest_summary(output)
            duration = time.monotonic() - started
    except TestRunBusy as exc:
        # A per-test run during a full run is refused, not queued and not
        # silently skipped: the Explorer has to be able to say "it did not
        # run, and here is why" rather than showing an outcome nobody earned.
        return Result(EXIT_FAILED, {"running": True, "run_state": "busy",
                                    "tests": []},
                      error=str(exc), human=str(exc))

    outcomes = pytest_report.parse_run(output, junit)

    # Attribution is done HERE, not in the extension. A parametrised test
    # reports one result per case (`test_x[a]`, `test_x[b]`) against a single
    # discovered definition (`test_x`), so something has to map the two — and
    # that mapping is where a plausible implementation quietly guesses. Doing
    # it in Python keeps one tested implementation instead of a second,
    # approximate one in TypeScript.
    #
    # The set attributed against is what the caller asked for when it asked by
    # id, and everything discoverable otherwise: a whole-suite run still needs
    # each result tied to the definition an editor can put a cursor on.
    if nodeids:
        requested = list(nodeids)
    else:
        from helpers.test_discovery import list_test_cases
        requested = [c.nodeid for c in list_test_cases(project)]

    per_test = [{"nodeid": a.outcome.nodeid, "outcome": a.outcome.outcome,
                 "duration_seconds": a.outcome.duration,
                 "message": a.outcome.message,
                 # "" means "this result belongs to no requested test", and
                 # `ambiguous` means "it belongs to more than one and picking
                 # would be a guess". Neither may render as a result.
                 "requested": a.requested, "ambiguous": a.ambiguous}
                for a in pytest_report.resolve_identities(outcomes, requested)]
    counted = pytest_report.summarise(outcomes)
    warnings: list = []
    from_lines = counted["passed"] + counted["failed"] + counted["error"]
    if total != from_lines:
        warnings.append(
            f"pytest's footer reports {total} test(s) and its progress lines "
            f"report {from_lines}; the counts below are the footer's")

    # `_parse_pytest_summary` returns (passed, passed + failed + errored) and
    # deliberately leaves SKIPPED out of the total, so failures are the
    # remainder of `total` alone. Subtracting skips here as well would count
    # them twice and under-report failures.
    failed = max(0, total - passed)
    skipped = _count_skipped(output)
    data = {"passed": passed, "failed": failed, "skipped": skipped,
            "total": total, "duration_seconds": round(duration, 2),
            "output": output[-_OUTPUT_CAP:],
            "selection": {"tests": list(nodeids), "markers": markers},
            "tests": per_test}

    if total == 0 and _collected_nothing(output):
        # pytest SAID "no tests ran". That is a result, read successfully, and
        # it is not the same as failing to read one — which is the whole reason
        # EXIT_VERIFY_FAILED exists. Treating a project that simply has no
        # Python tests (a PowerShell repo with a tests/ directory, say) as
        # unverifiable was this command reporting its own contract backwards.
        data["collected"] = 0
        data["run_state"] = RUN_NO_TESTS
        return Result(EXIT_OK, data, warnings,
                      human="test-run: no tests collected")
    if total == 0:
        # It ran, but no summary could be read. Reporting EXIT_OK here would
        # say "it passed"; reporting only EXIT_VERIFY_FAILED says "we could not
        # find out" without saying why, and the remedies differ — install
        # pytest, fix a collection error, or raise the timeout.
        state = _classify_run(output)
        data["run_state"] = state
        return Result(EXIT_VERIFY_FAILED, data, warnings,
                      error=f"could not read a pytest summary from the output "
                            f"({state})",
                      human=f"test-run: could not verify the result ({state})")
    data["run_state"] = RUN_COMPLETED
    human = (f"test-run: {passed} passed, {failed} failed, {skipped} skipped "
             f"in {duration:.1f}s")
    return Result(EXIT_OK if not failed else EXIT_FAILED, data, warnings,
                  human=human)


#: Tail of pytest output kept in the envelope. Enough to see the failures
#: without shipping a megabyte of collection noise to an editor.
_OUTPUT_CAP = 20000


def _collected_nothing(output: str) -> bool:
    """True when pytest positively reported that it found no tests.

    `_parse_pytest_summary` scans for `passed`/`failed`/`error`, and pytest's
    zero-test footer — `===== no tests ran in 0.17s =====` — contains none of
    them, so it returns (0, 0): the same value it returns when the output could
    not be read at all. Those are different answers and only one of them is
    honest to report as unverifiable.

    Keyed on pytest's own summary line rather than on an empty count, so a
    timeout or a crashed collection still falls through to EXIT_VERIFY_FAILED.
    """
    text = (output or "").lower()
    return "no tests ran" in text or "collected 0 items" in text


def _count_skipped(output: str) -> int:
    """Skipped count from pytest's summary line, or 0.

    `_parse_pytest_summary` returns (passed, total) and has never reported
    skips, so this reads the same line for the one number it omits.
    """
    import re
    match = re.search(r"(\d+)\s+skipped", output or "")
    return int(match.group(1)) if match else 0


def _cmd_test_gaps(args) -> Result:
    """Suggest tests for what changed against a base ref. Read-only.

    The base is **verified to exist first**. `git diff` against a ref that is
    not there yields no changed files, which renders as "0 test gaps" — the
    most reassuring possible way to report that the question was never asked.
    `origin/master` is a sensible default, not a guarantee, and a fresh clone
    with no fetch or a repo whose default branch is `main` both hit it.

    `--base auto` (the default) asks the repository which branch its remote
    considers default, via `refs/remotes/origin/HEAD`. That is not a guess
    between `main` and `master` — it is the answer git already holds, and a
    hardcoded `origin/master` default was simply wrong on every repo that uses
    `main`. When the symbolic ref is missing, `auto` refuses and asks for an
    explicit base rather than trying candidates.

    An explicitly named base is honoured exactly as given, and verified. There
    is deliberately no fallback to `master`, `HEAD` or the current branch:
    silently answering a different question than the one asked is the
    inference this whole surface exists to remove.
    """
    from helpers.git import default_base_ref, ref_exists
    from helpers.test_gap_report import suggest_tests_for_diff
    project = _resolve_project(args.project)
    cfg = _load_manager_config(args.config)
    git_exe = (cfg.get("git_exe") or "git").strip() or "git"

    base = args.base
    if base == AUTO_BASE:
        base = default_base_ref(project, git_exe)
        if not base:
            raise _Prerequisite(
                "could not read this repository's default branch "
                "(refs/remotes/origin/HEAD is not set) — pass --base "
                "explicitly, or run `git remote set-head origin --auto`")

    if not ref_exists(project, git_exe, base):
        detected = default_base_ref(project, git_exe)
        hint = f" — this repository's default looks like {detected!r}" \
            if detected and detected != base else ""
        raise _Prerequisite(
            f"base ref {base!r} does not exist in this repository{hint}. "
            "Pass --base with one that does (and `git fetch` first if it is "
            "a remote-tracking ref)")

    suggestions = suggest_tests_for_diff(project, git_exe, base)
    data = {
        "base": base,
        "base_requested": args.base,
        "count": len(suggestions),
        "suggestions": [
            {"source": getattr(s, "source_path", ""),
             "test": getattr(s, "test_path", ""),
             "requires_automation": bool(getattr(s, "requires_automation", False))}
            for s in suggestions
        ],
    }
    requested, matched = _resolve_paths(project, args.paths)
    if requested:
        wanted = {r.lstrip("./") for r in requested}
        data["suggestions"] = [
            s for s in data["suggestions"]
            if str(s.get("source", "")).replace(chr(92), "/").lstrip("./")
            in wanted]
        data["count"] = len(data["suggestions"])
        data["requested_paths"] = requested
        data["matched_paths"] = matched

    return Result(EXIT_OK, data,
                  human=f"{data['count']} test gap(s) against {base}")


def _cmd_mcp_status(args) -> Result:
    """Report MCP binding across the layers that can disagree. Read-only.

    Deliberately does NOT call `effective_scope` by default. That spawns
    `claude mcp get` with `cwd=` the project, and invoking Claude Code in a
    directory it has not seen CREATES a project entry — this Manager littered
    `~/.claude.json` with eight duplicate keys exactly that way. Opt in with
    `--probe-effective` when the contamination is acceptable.
    """
    from helpers.mcp import _classify_mcp_entry, _project_mcp_path
    project = _resolve_project(args.project)

    mcp_path = _project_mcp_path(project)
    layers = {
        "project_config": {"path": mcp_path, "exists": os.path.isfile(mcp_path)},
        "effective_scope": {"probed": False,
                            "note": "not probed - see --probe-effective"},
        "behavioural": {"probed": False,
                        "note": "requires a live MCP client; not observable here"},
    }
    if layers["project_config"]["exists"]:
        try:
            with open(mcp_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            layers["project_config"]["verdict"] = _classify_mcp_entry(mcp_path, cfg)
        except (OSError, ValueError) as exc:
            layers["project_config"]["verdict"] = {"state": "unreadable",
                                                   "detail": str(exc)}

    if args.probe_effective:
        from helpers.mcp import effective_scope
        got = effective_scope(project)
        layers["effective_scope"] = {"probed": True, "scope": str(got)}

    # VS Code's own logs, as a fourth layer. Detect-only and deliberately
    # weak: an empty log means VS Code knew about a server and did not start
    # it, and an absent one means nothing at all -- only 3 of 14 log
    # generations on the machine this was measured on hold any MCP log, and
    # the newest holds none. So this layer never produces a verdict, and the
    # `bound` decision below does not consult it.
    from helpers import vscode_mcp_logs as vsl
    vs = vsl.scan()
    layers["vscode_logs"] = {
        "generations_scanned": vs.generations_scanned,
        "logs_found": vs.logs_found,
        "content_observable": vs.content_observable,
        "summary": vs.summary(),
        "scopes_seen": {
            scope: {"generation": e.generation, "state": e.state,
                    "detail": e.detail,
                    "vscode_label": vsl.VSCODE_LABEL.get(scope, scope)}
            for scope, e in vsl.scopes_for(vs, "tokensave").items()
        },
        "notes": vsl.describe_server(vs, "tokensave"),
    }

    data = {"layers": layers}
    bound = (layers["project_config"].get("verdict") or {}).get("state") == "ok"
    human = "mcp: project binding looks correct" if bound else \
            "mcp: project binding is missing or non-canonical"
    return Result(EXIT_OK if bound else EXIT_FAILED, data, human=human)


def _index_summary(project: str) -> dict:
    """Node/file counts and last-write time from the local tokensave index.

    Two `COUNT(*)` queries against a read-only connection — no `tokensave`
    subprocess, no tree walk. Absent index reports `indexed: False` rather than
    zeroes, because "no index" and "an empty index" are different answers.
    """
    import sqlite3
    db_path = os.path.join(project, ".tokensave", "tokensave.db")
    if not os.path.isfile(db_path):
        return {"indexed": False}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        finally:
            con.close()
        return {"indexed": True, "file_count": files, "node_count": nodes,
                "last_sync_at": int(os.path.getmtime(db_path))}
    except (sqlite3.Error, OSError) as exc:
        # An index we cannot read is not an index with nothing in it.
        return {"indexed": True, "readable": False, "error": str(exc)}


def _git_section(git: "dict | None") -> dict:
    """The `status` envelope's git half.

    `changed_files` is here because `_parse_git_status_v2` was already walking
    every per-file record to set `dirty` and discarding the paths — so naming
    them costs one list append, not a second subprocess. That is what keeps
    this command inside the budget its own docstring sets, and is why the
    commit-request composer does not need a `git-status` command of its own.

    `ahead`/`behind`/`has_remote` are here for the same reason: already
    computed upstream, previously dropped on the floor.

    Every field is `None` when git could not answer. An unreadable repository
    is not a clean one, and a consumer must be able to tell those apart.
    """
    if not git:
        return {"branch": None, "dirty": None, "ahead": None, "behind": None,
                "has_remote": None, "changed_files": None,
                "changed_truncated": None}
    return {
        "branch": git.get("branch"),
        "dirty": git.get("dirty"),
        "ahead": git.get("ahead"),
        "behind": git.get("behind"),
        "has_remote": git.get("has_remote"),
        "changed_files": git.get("changed_files", []),
        # True when the cap dropped some. Reported rather than applied
        # silently, so a short list is never mistaken for a complete one.
        "changed_truncated": git.get("changed_truncated", False),
    }


def _cmd_status(args) -> Result:
    """One cheap roll-up, so the editor's tree is useful the moment it opens.

    **The payload is closed**, not "whatever the Manager knows": git branch,
    dirtiness, remote drift and changed files; the tokensave index counts; the
    MCP verdict; and whether a commit request is pending. Nothing here walks
    the tree, runs Doctor, runs scout or runs pytest — this is the one command
    a UI may call on refresh, so it has to stay in the tens of milliseconds. If
    a field cannot be made cheap it belongs in its own command, not in here.

    `changed_files` passes that bar rather than bending it. The porcelain-v2
    parser was already visiting every per-file record to decide `dirty` and
    throwing the paths away, so naming them adds a list append to a loop that
    already ran — no second subprocess, no tree walk. Same for `ahead`,
    `behind` and `has_remote`, which `read_git_status` has always computed and
    this command used to drop.

    **It never probes.** No `--probe-effective`, no `claude mcp get`: that
    spawn CREATES a project entry in `~/.claude.json`, and this Manager once
    littered that file with eight duplicate keys exactly that way. Having an
    editor trigger it on every window open would rebuild the same problem at a
    higher rate. Probing stays opt-in, on `mcp-status`.
    """
    from helpers.commit_request import load_commit_request
    from helpers.git import read_git_status
    from helpers.mcp import _classify_mcp_entry, _project_mcp_path
    project = _resolve_project(args.project)

    try:
        raw = _load_manager_config(args.config)
    except Exception:                     # noqa: BLE001 - deliberate, see below
        # Broader than the usual `_Prerequisite` catch, and only here: `status`
        # is the roll-up a UI calls to find out what state things are in, so a
        # missing or malformed config is one of the things it should be able to
        # REPORT rather than die of. Every field below has a working
        # no-config default.
        raw = {}
    git_exe = (raw.get("git_exe") or "git").strip() or "git"

    git = read_git_status(project, git_exe)
    mcp_path = _project_mcp_path(project)
    # `probed: False`, always, and stated rather than implied. This command
    # never runs `claude mcp get` — that spawn CREATES a project entry in
    # `~/.claude.json`, and this Manager once littered that file with eight
    # duplicate keys exactly that way. So what follows is a reading of
    # configuration on disk, not a verdict about which scope actually wins, and
    # a consumer that renders a bare "MCP ✓" from it would be overstating what
    # was checked. Effective-scope probing stays opt-in, on `mcp-status`.
    mcp: dict = {"configured": os.path.isfile(mcp_path), "probed": False}
    if mcp["configured"]:
        try:
            with open(mcp_path, encoding="utf-8") as fh:
                mcp["state"] = _classify_mcp_entry(
                    mcp_path, json.load(fh)).get("state", "unknown")
        except (OSError, ValueError):
            mcp["state"] = "unreadable"

    pending = load_commit_request(project)
    data = {
        # `None` where git could not answer — an unreadable repo is not a
        # clean one, and the UI needs to be able to say so.
        "git": _git_section(git),
        "tokensave": _index_summary(project),
        "mcp": mcp,
        "commit_request": {"pending": bool(pending)},
    }
    branch = data["git"]["branch"] or "no branch"
    dirty = " (dirty)" if data["git"]["dirty"] else ""
    return Result(EXIT_OK, data, human=f"status: {branch}{dirty}")


def _cmd_commit_request(args) -> Result:
    """Read or file a commit request. Writing is the only mutating path.

    **Conflict policy.** A pending request is work the user has not yet
    approved in the Manager, so silently replacing it would discard their
    queue. Re-filing the *same* request therefore succeeds idempotently, a
    *different* one is refused, and `--replace` is the explicit override.

    "The same" means `helpers.commit_request.request_identity` — a hash of the
    normalised `files`/`suggested_scope`/`note`, deterministically serialised.
    This docstring used to claim byte-identity, which was never what happened:
    the check compared three fields while the writer re-serialised with
    `indent=2` and no `sort_keys`, so the bytes were neither compared nor
    reproduced. `created_at` is excluded on purpose — when a request was filed
    is not part of what it asks for.
    """
    from helpers.commit_request import (
        load_commit_request, normalise_request, request_identity,
        write_commit_request)
    project = _resolve_project(args.project)

    if not args.files:
        existing = load_commit_request(project)
        return Result(EXIT_OK, {"pending": existing},
                      human="1 pending request" if existing else "no pending request")

    existing = load_commit_request(project)
    proposed = normalise_request(args.files, args.scope, args.note)
    identical = request_identity(existing) == request_identity(proposed)
    if existing and not args.replace and not identical:
        return Result(
            EXIT_FAILED, {"pending": existing},
            error="a different commit request is already pending; "
                  "pass --replace to overwrite it",
            human="refused: a different request is pending (use --replace)")

    path = write_commit_request(project, proposed["files"],
                                proposed["suggested_scope"], proposed["note"])
    return Result(EXIT_OK, {"written": path, "files": proposed["files"],
                            "unchanged": bool(existing and identical)},
                  human=f"commit request written for {len(proposed['files'])} "
                        f"file(s)")


def _cmd_focus(args) -> Result:
    """Raise the running Manager window.

    **Running and focused are separate facts.** Windows routinely refuses
    `SetForegroundWindow` to a background process — the same constraint that
    made driving this app by synthetic mouse input unusable — so "the Manager
    is open but the desktop declined to raise it" is a normal outcome carrying
    a reason, not a failure. Collapsing the two would have the extension report
    the Manager as absent whenever Windows said no.

    Not running is `EXIT_PREREQUISITE`, which is the code the extension already
    treats as "a prerequisite is missing, here is how to fix it".

    `--probe` answers without raising anything. It exists because the status
    bar polls for "is the Manager open?", and a poll that foregrounded the
    window would make the act of asking change the thing being asked about.
    """
    from helpers import manager_ipc
    if args.probe:
        # Report only. The status bar polls this, and a poll that raised the
        # window would steal focus every few minutes — the surface asking the
        # question must not be the thing that changes the answer.
        running = manager_ipc.manager_running()
        result = {"running": running, "focused": False,
                  "reason": "probe only; the window was not raised"}
    else:
        result = manager_ipc.focus_manager()
    if not result["running"]:
        return Result(EXIT_PREREQUISITE, result,
                      error="the Manager is not running",
                      human="the Manager is not running - start it first")
    return Result(EXIT_OK, result,
                  human="Manager focused" if result["focused"]
                        else f"Manager is running ({result['reason']})")


def _cmd_request(args) -> Result:
    """File a request for the running Manager, or ask what became of one.

    **Propose-only holds.** Every action opens a dialog in front of a person;
    none commits, applies or approves. Writing the request file *is* a state
    change - that is what the inbox is - so this is classified `MUTATING`
    rather than pretending otherwise.

    **The project is checked against the Manager's configured search roots.**
    `--project` is an argument, not an authorization: without that check a
    request could steer the GUI at any directory on the machine.
    """
    from helpers import manager_ipc
    project = _resolve_project(args.project)
    raw = _load_manager_config(args.config)
    roots = [_root_path(r) for r in (raw.get("search_roots") or [])] or None

    if args.status:
        state = manager_ipc.status_of(project, args.status, project=project)
        # `unknown` is a real answer, not an error: it is what the ledger says
        # when nothing was ever filed under that id for this project.
        return Result(EXIT_OK, state,
                      human=f"{args.status}: {state['state']}")

    if not args.action:
        return Result(EXIT_USAGE, error="pass --action or --status",
                      human="pass --action or --status")

    payload = {}
    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except ValueError as exc:
            return Result(EXIT_USAGE, error=f"--payload-json is not JSON: {exc}",
                          human=f"--payload-json is not JSON: {exc}")

    try:
        written = manager_ipc.write_request(project, args.action, payload,
                                            known_roots=roots)
    except manager_ipc.RequestError as exc:
        # A refused request is the CLI reporting the protocol's own rules, not
        # a crash: exit 2 so a caller can tell it from a transport failure.
        return Result(EXIT_USAGE, {"accepted": False}, error=str(exc),
                      human=f"refused: {exc}")

    running = manager_ipc.manager_running()
    warnings = ([] if running else
                ["the Manager is not running; the request will be picked up "
                 "when it next starts"])
    return Result(EXIT_OK,
                  {"accepted": True, "id": written["id"],
                   "path": written["path"], "duplicate": written["duplicate"],
                   "manager_running": running},
                  warnings,
                  human=f"queued request {written['id']}"
                        + (" (already pending)" if written["duplicate"] else ""))


def _cmd_commands(args) -> Result:
    """Emit the command vocabulary. The source of truth for four surfaces.

    Project-less on purpose — it describes the Manager's operations rather than
    performing one on a repository, and requiring `--project` would mean the
    extension had to have a folder open before it could learn what it may
    invoke.
    """
    return Result(EXIT_OK, commands.as_json(),
                  human=f"{len(commands.COMMANDS)} command(s)")


def _cmd_cost(args) -> Result:
    """Savings, spend and opportunity, as one envelope.

    Classified `OBSERVE_REFRESH`, not read-only: `cost` and `discover` both
    ingest accounting rows into `~/.tokensave/global.db`. A caller wiring this
    to a control a user operates casually turns a display into repeated
    ingestion, which is why the Manager's own dialog refreshes spend only on an
    explicit click.

    Each section reports independently. One unreadable source degrades that
    section to `ok: false` with a reason; it never blanks the others, and it
    never becomes a zero — "we could not find out" and "you saved nothing" are
    different answers and this envelope keeps them apart.
    """
    from helpers import savings
    project = _resolve_project(args.project)
    raw = _load_manager_config(args.config)
    exe = _tokensave_exe_from(raw, args.config or "the Manager's config")

    range_ = args.range
    gain = savings.fetch_gain(exe, project, range_, all_projects=args.all)
    history = savings.fetch_gain_history(exe, project, range_)
    spend = savings.fetch_spend(exe, range_, project)
    found = savings.fetch_discover(exe, project, range_)

    def _section(result, shape):
        if not result:
            return {"ok": False, "reason": result.reason}
        return {"ok": True, **shape(result.value)}

    def _gain_shape(value):
        out = {"range": value.range, "project": value.project,
               "saved_tokens": value.saved_tokens, "calls": value.calls,
               "usd": value.usd, "all_projects": value.all_projects,
               # The valuation basis travels with the number. A bare dollar
               # figure with no basis is what made the old panel untrustworthy.
               "usd_basis": savings.Gain.USD_BASIS,
               "scope": "all projects" if value.all_projects else project}
        if args.raw:
            out["raw"] = value.raw
        return out

    def _spend_shape(value):
        out = {
            "range": value.range,
            "total_cost_usd": value.total_cost_usd,
            "total_input_tokens": value.total_input_tokens,
            "total_output_tokens": value.total_output_tokens,
            # Read from the payload on tokensave 7.11+ (#472), null on an older
            # binary. Null means "not reported" and must never render as 0 —
            # these are not derived, and under 7.10 the only derivation
            # available was provably zero.
            "cache_read_tokens": value.cache_read_tokens,
            "cache_creation_tokens": value.cache_creation_tokens,
            "total_tokens": value.total_tokens,
            "by_model": [dataclasses.asdict(m) for m in value.by_model],
            "by_category": [dataclasses.asdict(c) for c in value.by_category],
            # Reported so a consumer can say so. `tokens_reconcile` is null on
            # a payload with no `total_tokens` to compare against — "cannot
            # say", which a consumer must not read as "disagrees".
            "totals_reconcile": value.totals_reconcile(),
            "tokens_reconcile": value.tokens_reconcile(),
            # Range-scoped and equal to `gain --all` since 7.11 (#473); a
            # lifetime counter before that. `tokens_saved_spans_range` is the
            # flag a consumer must check before displaying it.
            "tokens_saved": value.tokens_saved,
            "tokens_saved_spans_range": value.spans_range,
            # `cost` has no project filter, and a consumer must not present it
            # beside the project-scoped savings without saying so. That applies
            # to `tokens_saved` too: it matches `gain --all`, never `gain`.
            "scope": "machine-global, all projects",
        }
        # The rate is meaningless without its basis, so they ship together or
        # not at all — a consumer cannot end up rendering a 7.10 figure and a
        # 7.11 figure as the same statistic.
        implied = value.implied_usd_per_mtok()
        out["implied_usd_per_mtok"] = None if implied is None else implied[0]
        out["implied_usd_basis"] = None if implied is None else implied[1]
        if args.raw:
            out["raw"] = value.raw
        return out

    def _discover_shape(value):
        out = {"since": value.since, "total_turns": value.total_turns,
               "replaceable_turns": value.replaceable_turns,
               "buckets": [dataclasses.asdict(b) for b in value.buckets],
               "tokens_trustworthy": value.tokens_trustworthy,
               "token_evidence": value.token_evidence}
        if args.raw:
            out["raw"] = value.raw
        return out

    data = {
        "savings": _section(gain, _gain_shape),
        "savings_history": ({"ok": True,
                             "days": [dataclasses.asdict(d)
                                      for d in history.value]}
                            if history else
                            {"ok": False, "reason": history.reason}),
        "spend": _section(spend, _spend_shape),
        "opportunity": _section(found, _discover_shape),
        "side_effect": commands.OBSERVE_REFRESH,
    }
    warnings = [f"{name}: {section['reason']}"
                for name, section in data.items()
                if isinstance(section, dict) and section.get("ok") is False]

    if not gain:
        # Savings is the headline. Without it there is no answer to the
        # question this command exists to answer, so it does not exit 0 —
        # EXIT_VERIFY_FAILED is this CLI's existing way of saying "it ran but
        # could not be verified" rather than "there is nothing to report".
        return Result(EXIT_VERIFY_FAILED, data, warnings,
                      error=gain.reason,
                      human=f"savings unavailable: {gain.reason}")
    value = gain.value
    return Result(EXIT_OK, data, warnings,
                  human=f"saved {value.saved_tokens:,} tokens over "
                        f"{value.calls} call(s) "
                        f"(${value.usd:,.2f}, {savings.Gain.USD_BASIS})")


def _cmd_graph_trust(args) -> Result:
    """Report how many call edges in the index are impossible.

    Exit is `EXIT_OK` even when the graph is tainted, matching `test-gaps`:
    this describes the indexer, not the project, and a caller wiring it into
    a gate would be failing a build over someone else's bug. The state is in
    the payload for anything that wants to act on it.

    `EXIT_VERIFY_FAILED` is reserved for the two states that could not reach
    a population, because "we could not find out" must not be readable as
    "it is fine" — the same distinction `test-run` already makes.
    """
    from helpers import graph_trust as gt
    project = _resolve_project(args.project)
    report = gt.inspect_graph(project)

    data = {
        "state": report.state,
        "detail": report.detail,
        "edges_examined": report.edges_examined,
        "impossible_edges": report.impossible_edges,
        "source_files_affected": report.source_files_affected,
        "collisions": [{"name": c.target_name, "file": c.target_file,
                        "count": c.count} for c in report.collisions],
        "db_path": report.db_path,
        # Named in the payload rather than left for a consumer to infer:
        # every dimension that reads call edges is affected, and so is the
        # aggregate computed over them.
        "quarantined_metrics": (["acyclicity", "quality_signal"]
                                if report.is_tainted else []),
    }
    # Deliberately emits NO findings. A finding is a diagnostic about a line
    # of source, and the only line a phantom edge can be anchored to is the
    # test double's definition -- which is correct code doing its job. The
    # same reasoning keeps `test-run` out of DIAGNOSTIC_COMMANDS in the
    # extension: a red suite is a fact, but not a fact about that line. The
    # collisions travel in `data` instead, where nothing will render them as
    # a squiggle on a file with nothing wrong with it.
    if not report.is_conclusive:
        return Result(EXIT_VERIFY_FAILED, data, human=report.summary())
    return Result(EXIT_OK, data, human=report.summary())


_COMMANDS = {
    "checks": _cmd_checks,
    "doctor": _cmd_doctor,
    "scout": _cmd_scout,
    "status": _cmd_status,
    "tests": _cmd_tests,
    "test-run": _cmd_test_run,
    "sync": _cmd_sync,
    "graph-trust": _cmd_graph_trust,
    "test-gaps": _cmd_test_gaps,
    "mcp-status": _cmd_mcp_status,
    "commit-request": _cmd_commit_request,
    "cost": _cmd_cost,
    "focus": _cmd_focus,
    "request": _cmd_request,
    "commands": _cmd_commands,
}

#: Commands that describe the Manager rather than acting on a project, so
#: `--project` would be noise. Derived from the table rather than restated, so
#: adding a project-less command cannot leave the parser and this set
#: disagreeing about which one it is.
PROJECTLESS_COMMANDS = frozenset(
    c.cli for c in commands.COMMANDS if c.cli and not c.requires_project)

# Side-effect classification, derived from the single command table in
# `helpers/commands.py` rather than restated here.
#
# The previous `READ_ONLY_COMMANDS` frozenset promised something two of its
# members did not deliver, and the replacements were measured rather than
# reasoned about: `doctor` rewrites `~/.tokensave/state.toml` (byte-identical
# content, mtime moves) and the new `cost` command ingests accounting rows into
# `~/.tokensave/global.db`. Neither touches the project, but neither is a read
# either, so they sit in their own class instead of being filed under a label
# that overstates the guarantee.
PURE_READ_COMMANDS = frozenset(
    c.cli for c in commands.by_side_effect(commands.PURE_READ) if c.cli)
OBSERVE_REFRESH_COMMANDS = frozenset(
    c.cli for c in commands.by_side_effect(commands.OBSERVE_REFRESH) if c.cli)
MUTATING_COMMANDS = frozenset(
    c.cli for c in commands.by_side_effect(commands.MUTATING) if c.cli)

#: Safe to run unattended: nothing here changes the project. It spans two
#: classes because "does not touch your repository" and "writes nothing at all"
#: are different promises, and only the first one is true of `doctor`.
UNATTENDED_SAFE_COMMANDS = PURE_READ_COMMANDS | OBSERVE_REFRESH_COMMANDS


def _build_parser() -> argparse.ArgumentParser:
    # Imported here rather than at module scope: this is the only place that
    # needs these, and `cli.py` keeps its import surface small so a frozen
    # build does not drag the whole helper tree in.
    from helpers.manager_ipc import ACTIONS as request_actions
    from helpers.savings import RANGES as savings_ranges
    p = argparse.ArgumentParser(
        prog="manager-cli",
        description="Headless access to TokenSave Manager operations. "
                    "stdout is always one JSON envelope; stderr is for humans.")
    p.add_argument("--version", action="version",
                   version=f"manager-cli {APP_VERSION} "
                           f"(schema {SCHEMA_VERSION})")
    subs = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str,
            project: bool = True) -> argparse.ArgumentParser:
        sp = subs.add_parser(name, help=help_text)
        # Required for everything that acts on a repository: see the module
        # docstring on cwd inference. `commands` describes the Manager itself,
        # so demanding a project would mean an editor had to open a folder
        # before it could ask what it may invoke.
        if project:
            sp.add_argument("--project", required=True,
                            help="absolute path to the project root")
        sp.add_argument("--json", action="store_true",
                        help="suppress the human summary on stderr "
                             "(stdout is JSON either way)")
        sp.add_argument("--config", default="",
                        help="path to manager-config.json; defaults to the "
                             "file beside this executable")
        return sp

    def add_paths(sp: argparse.ArgumentParser) -> None:
        """`--paths`: scope the findings, without changing their shape."""
        sp.add_argument("--paths", nargs="*", default=[],
                        help="limit findings to these files (repo-relative or "
                             "absolute, but inside the project)")

    add_paths(add("checks", "run syntax + pyflakes checks (read-only)"))

    d = add("doctor", "scan for stale tokensave entries (read-only, never fixes)")
    d.add_argument("--timeout", type=float, default=120.0)

    s = add("sync", "refresh shadow links, then re-index (mutating)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--timeout", type=float, default=None)

    add("scout", "refactor-scout findings from the tokensave index "
                 "(read-only, no LLM)")

    add("status", "one cheap roll-up: git, index, MCP, pending request "
                  "(read-only, never probes)")

    ts = add("tests", "discovery only: what exists, what is uncovered, what "
                      "looks stale (read-only, no pytest run)")
    ts.add_argument("--detail", action="store_true",
                    help="also list every test definition with its nodeid "
                         "and source range")

    tr = add("test-run", "run the suite once and report structured counts")
    tr.add_argument("--tests", nargs="*", default=[],
                    help="pytest node ids to run; omit to run the whole suite")
    tr.add_argument("--markers", default="",
                    help="a pytest marker expression, e.g. \"not tk\". This "
                         "is the Manager's own option name: it is passed to "
                         "pytest as -m, because pytest's own --markers lists "
                         "registered markers rather than selecting tests. "
                         "Mutually exclusive with --tests")

    t = add("test-gaps", "suggest tests for changes against a base ref")
    t.add_argument("--base", default=AUTO_BASE,
                   help="git ref to compare against; 'auto' asks the "
                        "repository for its default branch")
    add_paths(t)

    add("graph-trust", "report how much of the tokensave call graph "
                       "is real (read-only)")

    m = add("mcp-status", "report MCP binding across layers (read-only)")
    m.add_argument("--probe-effective", action="store_true",
                   help="also ask Claude Code which scope wins. NB this can "
                        "create a project entry in ~/.claude.json")

    c = add("commit-request", "read, or file, a commit request (mutating on write)")
    c.add_argument("--files", nargs="*", default=[],
                   help="repo-relative paths; omit to read the pending request")
    c.add_argument("--scope", default="")
    c.add_argument("--note", default="")
    c.add_argument("--replace", action="store_true",
                   help="overwrite a different pending request")

    money = add("cost", "savings, spend and opportunity metrics "
                        "(refreshes tokensave bookkeeping)")
    money.add_argument("--range", default="30d", choices=list(savings_ranges),
                       help="today, 7d, 30d or all")
    money.add_argument("--all", action="store_true",
                       help="savings across every project, not just this one")
    money.add_argument("--raw", action="store_true",
                       help="include the untouched upstream payloads")

    foc = add("focus", "raise the running Manager window (no project needed)",
              project=False)
    foc.add_argument("--probe", action="store_true",
                     help="report whether the Manager is running without "
                          "raising its window")

    req = add("request", "file a Manager request, or read its status")
    req.add_argument("--action", default="",
                     choices=sorted(request_actions) or None,
                     help="which dialog the Manager should open")
    req.add_argument("--payload-json", default="",
                     help="the action's payload, as JSON")
    req.add_argument("--status", default="",
                     help="report what became of a request id instead")

    add("commands", "emit the Manager's command vocabulary (no project needed)",
        project=False)
    return p


def main(argv: "list | None" = None) -> int:
    """Run one command. Returns the process exit code; never raises."""
    args = _build_parser().parse_args(argv)
    handler = _COMMANDS[args.command]

    try:
        result = handler(args)
    except _Prerequisite as exc:
        result = Result(EXIT_PREREQUISITE, error=str(exc), human=str(exc))
    except Exception as exc:                       # noqa: BLE001 - see below
        # A crash must still produce a valid envelope: the extension parses
        # stdout unconditionally, and a traceback there would be unreadable
        # garbage on the one channel that is supposed to be machine-readable.
        result = Result(EXIT_FAILED, error=f"{type(exc).__name__}: {exc}",
                        human=f"unexpected failure: {exc}")

    # stdout: exactly one envelope, nothing else.
    json.dump(_envelope(args.command, result), sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()

    if result.human and not args.json:
        sys.stderr.write(result.human + "\n")
    for warning in result.warnings:
        sys.stderr.write(f"warning: {warning}\n")

    return result.code


if __name__ == "__main__":
    sys.exit(main())
