"""cli_test_commands.py - the `tests`, `test-run` and `test-gaps` commands.

Split out of `cli.py` (2026-09-11). One coherent family: discovering tests,
running a selection, and reporting the gaps a diff opens up - together with
the constants and parsers only they use (`RUN_*`, `_classify_run`,
`_collected_nothing`, `_count_skipped`, the list and output caps).

**Why the imports below are split two ways.** Everything stateless comes from
`cli_support` directly. `_is_frozen` and `_load_manager_config` are imported
from `cli` INSIDE the functions that call them, because the suite patches
`cli._is_frozen` and `cli._load_manager_config` by module path. A module-level
import would bind the original once and those patches would silently miss -
the facade-split patch-target trap, which this project has paid for before.
A call-time import reads the attribute at call time, so a patch applies.
"""
from __future__ import annotations

import os

from cli_support import (
    AUTO_BASE, EXIT_FAILED, EXIT_OK,
    EXIT_USAGE, EXIT_VERIFY_FAILED, Result,
    _Prerequisite, _resolve_paths, _resolve_project,
)


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

    from cli import _is_frozen  # late: tests patch cli._is_frozen

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
    from cli import _load_manager_config  # late: tests patch cli._load_manager_config
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


