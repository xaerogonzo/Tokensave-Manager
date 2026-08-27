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
import json
import os
import sys

from constants import APP_VERSION
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
    """
    from helpers.test_discovery import (detect_stale_tests, list_test_files,
                                        scan_coverage_gaps)
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
    return Result(EXIT_OK, data,
                  human=f"tests: {data['test_count']} test(s) in "
                        f"{len(files)} file(s), {len(uncovered)} source file(s) "
                        f"with no test, {len(stale)} stale signal(s)")


def _cmd_test_run(args) -> Result:
    """Run the suite once and report structured counts.

    **Exit codes are decided here, not inherited from pytest.** In particular a
    run whose summary could not be parsed is `EXIT_VERIFY_FAILED`, never
    `EXIT_OK`: "we could not find out" is not "it passed", the same distinction
    `DoctorScanResult.ok` exists to preserve.

    **No findings are emitted.** A failing test is not a diagnostic about a
    line of source, and turning a red suite into thousands of Problems entries
    would bury the ones that are.
    """
    import time
    from helpers.smoke_runner import run_smoke_tests
    from helpers.test_lock import TestRunBusy, test_run_lock
    project = _resolve_project(args.project)

    # Checked here rather than inferred from an empty result: "there is no
    # suite" is a prerequisite the user can fix, and it must not look like
    # "the suite could not be read".
    if not os.path.isdir(os.path.join(project, "tests")):
        raise _Prerequisite(f"no tests/ directory in {project}")

    try:
        with test_run_lock(project):
            started = time.monotonic()
            passed, total, output = run_smoke_tests(project)
            duration = time.monotonic() - started
    except TestRunBusy as exc:
        return Result(EXIT_FAILED, {"running": True}, error=str(exc),
                      human=str(exc))

    # `_parse_pytest_summary` returns (passed, passed + failed + errored) and
    # deliberately leaves SKIPPED out of the total, so failures are the
    # remainder of `total` alone. Subtracting skips here as well would count
    # them twice and under-report failures.
    failed = max(0, total - passed)
    skipped = _count_skipped(output)
    data = {"passed": passed, "failed": failed, "skipped": skipped,
            "total": total, "duration_seconds": round(duration, 2),
            "output": output[-_OUTPUT_CAP:]}

    if total == 0 and _collected_nothing(output):
        # pytest SAID "no tests ran". That is a result, read successfully, and
        # it is not the same as failing to read one — which is the whole reason
        # EXIT_VERIFY_FAILED exists. Treating a project that simply has no
        # Python tests (a PowerShell repo with a tests/ directory, say) as
        # unverifiable was this command reporting its own contract backwards.
        data["collected"] = 0
        return Result(EXIT_OK, data,
                      human="test-run: no tests collected")
    if total == 0:
        # It ran, but no summary could be read — a timeout, a launch failure,
        # or a collection error. Reporting EXIT_OK here would say "it passed".
        return Result(EXIT_VERIFY_FAILED, data,
                      error="could not read a pytest summary from the output",
                      human="test-run: could not verify the result")
    human = (f"test-run: {passed} passed, {failed} failed, {skipped} skipped "
             f"in {duration:.1f}s")
    return Result(EXIT_OK if not failed else EXIT_FAILED, data, human=human)


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
    return Result(EXIT_OK, data,
                  human=f"{len(suggestions)} test gap(s) against {base}")


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


def _cmd_status(args) -> Result:
    """One cheap roll-up, so the editor's tree is useful the moment it opens.

    **The payload is closed**, not "whatever the Manager knows": git branch and
    dirtiness, the tokensave index counts, the MCP verdict, and whether a
    commit request is pending. Nothing here walks the tree, runs Doctor, runs
    scout or runs pytest — this is the one command a UI may call on refresh,
    so it has to stay in the tens of milliseconds. If a field cannot be made
    cheap it belongs in its own command, not in here.

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
    mcp: dict = {"configured": os.path.isfile(mcp_path)}
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
        "git": {"branch": (git or {}).get("branch"),
                "dirty": (git or {}).get("dirty") if git else None},
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


_COMMANDS = {
    "checks": _cmd_checks,
    "doctor": _cmd_doctor,
    "scout": _cmd_scout,
    "status": _cmd_status,
    "tests": _cmd_tests,
    "test-run": _cmd_test_run,
    "sync": _cmd_sync,
    "test-gaps": _cmd_test_gaps,
    "mcp-status": _cmd_mcp_status,
    "commit-request": _cmd_commit_request,
}

#: Documented so a caller knows what is safe to run unattended.
READ_ONLY_COMMANDS = frozenset(
    {"checks", "doctor", "scout", "status", "tests", "test-run",
     "test-gaps", "mcp-status"})
MUTATING_COMMANDS = frozenset({"sync", "commit-request"})


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manager-cli",
        description="Headless access to TokenSave Manager operations. "
                    "stdout is always one JSON envelope; stderr is for humans.")
    p.add_argument("--version", action="version",
                   version=f"manager-cli {APP_VERSION} "
                           f"(schema {SCHEMA_VERSION})")
    subs = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = subs.add_parser(name, help=help_text)
        # Required everywhere: see the module docstring on cwd inference.
        sp.add_argument("--project", required=True,
                        help="absolute path to the project root")
        sp.add_argument("--json", action="store_true",
                        help="suppress the human summary on stderr "
                             "(stdout is JSON either way)")
        sp.add_argument("--config", default="",
                        help="path to manager-config.json; defaults to the "
                             "file beside this executable")
        return sp

    add("checks", "run syntax + pyflakes checks (read-only)")

    d = add("doctor", "scan for stale tokensave entries (read-only, never fixes)")
    d.add_argument("--timeout", type=float, default=120.0)

    s = add("sync", "refresh shadow links, then re-index (mutating)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--timeout", type=float, default=None)

    add("scout", "refactor-scout findings from the tokensave index "
                 "(read-only, no LLM)")

    add("status", "one cheap roll-up: git, index, MCP, pending request "
                  "(read-only, never probes)")

    add("tests", "discovery only: what exists, what is uncovered, what "
                 "looks stale (read-only, no pytest run)")

    add("test-run", "run the suite once and report structured counts")

    t = add("test-gaps", "suggest tests for changes against a base ref")
    t.add_argument("--base", default=AUTO_BASE,
                   help="git ref to compare against; 'auto' asks the "
                        "repository for its default branch")

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
