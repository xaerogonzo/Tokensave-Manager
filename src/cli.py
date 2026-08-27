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

This module imports only from `helpers/`, which is Tk-free, and it must never
take the GUI's single-instance lock: the CLI is expected to run *while* the
Manager is open.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from constants import APP_VERSION

#: Bumped only when the envelope's shape changes incompatibly.
SCHEMA_VERSION = 1

EXIT_OK = 0                  # success
EXIT_FAILED = 1              # the operation ran and reported problems
EXIT_USAGE = 2               # invalid invocation (argparse also uses 2)
EXIT_PREREQUISITE = 3        # a required tool or path is missing
EXIT_VERIFY_FAILED = 4       # an operation ran but could not be verified


class Result:
    """What a command handler returns, before it becomes an envelope."""

    def __init__(self, code: int = EXIT_OK, data: "dict | None" = None,
                 warnings: "list | None" = None, error: str = "",
                 human: str = ""):
        self.code = code
        self.data = data or {}
        self.warnings = warnings or []
        self.error = error
        self.human = human


def _envelope(command: str, result: Result) -> dict:
    """The stable payload. Key order is fixed for readable diffs in logs."""
    return {
        "schema_version": SCHEMA_VERSION,
        "cli_version": APP_VERSION,
        "command": command,
        "ok": result.code == EXIT_OK,
        "data": result.data,
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


def _tokensave_exe(explicit_config: str = "") -> str:
    cfg = _load_manager_config(explicit_config)
    exe = (cfg.get("tokensave_exe") or "").strip()
    if not exe:
        where = explicit_config or "the Manager's config"
        raise _Prerequisite(
            f"no tokensave_exe configured in {where} — set it in the "
            "Manager's Settings, or pass --config to point at an install")
    return exe


def _is_frozen() -> bool:
    """True when running from the Nuitka onefile build.

    Same marker `constants._resolve_base_dir` keys off. Note its VALUE is a
    parent PID, not a path — only its presence is meaningful.
    """
    return bool(os.environ.get("NUITKA_ONEFILE_PARENT"))


# ── commands ─────────────────────────────────────────────────────────────────

def _cmd_checks(args) -> Result:
    """Syntax + pyflakes over the project. Read-only."""
    from helpers.quality_checks import run_pyflakes_check, run_syntax_check
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

    syntax_ok, syntax_out = run_syntax_check(project)
    flakes_ok, flakes_out = run_pyflakes_check(project)
    data = {
        "syntax": {"ok": syntax_ok, "output": syntax_out},
        "pyflakes": {"ok": flakes_ok, "output": flakes_out},
    }
    if syntax_ok and flakes_ok:
        return Result(EXIT_OK, data, human="checks passed")
    failed = [n for n, ok in (("syntax", syntax_ok), ("pyflakes", flakes_ok))
              if not ok]
    return Result(EXIT_FAILED, data,
                  human="checks failed: " + ", ".join(failed))


def _cmd_doctor(args) -> Result:
    """Observe stale state. Read-only — never applies a fix."""
    from helpers import housekeeping
    from helpers.doctor_service import scan_stale
    project = _resolve_project(args.project)

    scan = scan_stale(project, _tokensave_exe(args.config),
                      timeout=args.timeout)
    if not scan.ok:
        # "we could not find out" is NOT "nothing to clean" — the whole point
        # of DoctorScanResult.ok. Report it as unverified, not as clean.
        return Result(EXIT_VERIFY_FAILED, {"scanned": False},
                      error=scan.error, human=f"doctor scan failed: {scan.error}")

    stale = housekeeping.parse_stale_entries(scan.transcript)
    data = {
        "scanned": True,
        "exit_code": scan.exit_code,
        "stale_count": len(stale),
        "stale": [getattr(e, "path", str(e)) for e in stale],
    }
    if not stale:
        return Result(EXIT_OK, data, human="doctor: no stale entries")
    return Result(EXIT_FAILED, data,
                  human=f"doctor: {len(stale)} stale entr"
                        f"{'y' if len(stale) == 1 else 'ies'}")


def _cmd_sync(args) -> Result:
    """Refresh shadow links, then re-index. Mutating (local index only)."""
    from helpers.sync_service import run_sync
    project = _resolve_project(args.project)

    res = run_sync(project, _tokensave_exe(args.config), force=args.force,
                   timeout=args.timeout)
    data = {
        "argv": res.argv,
        "returncode": res.returncode,
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


def _cmd_test_gaps(args) -> Result:
    """Suggest tests for what changed against a base ref. Read-only."""
    from helpers.test_gap_report import suggest_tests_for_diff
    project = _resolve_project(args.project)
    cfg = _load_manager_config(args.config)
    git_exe = (cfg.get("git_exe") or "git").strip() or "git"

    suggestions = suggest_tests_for_diff(project, git_exe, args.base)
    data = {
        "base": args.base,
        "count": len(suggestions),
        "suggestions": [
            {"source": getattr(s, "source_path", ""),
             "test": getattr(s, "test_path", ""),
             "requires_automation": bool(getattr(s, "requires_automation", False))}
            for s in suggestions
        ],
    }
    return Result(EXIT_OK, data,
                  human=f"{len(suggestions)} test gap(s) against {args.base}")


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


def _cmd_commit_request(args) -> Result:
    """Read or file a commit request. Writing is the only mutating path.

    Conflict policy: an existing request is overwritten only when byte-identical,
    and otherwise refused. A pending request is something the user has not yet
    approved in the Manager, and silently replacing it would discard their queue.
    """
    from helpers.commit_request import load_commit_request, write_commit_request
    project = _resolve_project(args.project)

    if not args.files:
        existing = load_commit_request(project)
        return Result(EXIT_OK, {"pending": existing},
                      human="1 pending request" if existing else "no pending request")

    existing = load_commit_request(project)
    proposed = {"files": [str(f).replace("\\", "/") for f in args.files],
                "suggested_scope": args.scope, "note": args.note}
    if existing and not args.replace:
        same = all(existing.get(k) == v for k, v in proposed.items())
        if not same:
            return Result(
                EXIT_FAILED, {"pending": existing},
                error="a different commit request is already pending; "
                      "pass --replace to overwrite it",
                human="refused: a different request is pending (use --replace)")

    path = write_commit_request(project, args.files, args.scope, args.note)
    return Result(EXIT_OK, {"written": path, "files": proposed["files"]},
                  human=f"commit request written for {len(args.files)} file(s)")


_COMMANDS = {
    "checks": _cmd_checks,
    "doctor": _cmd_doctor,
    "sync": _cmd_sync,
    "test-gaps": _cmd_test_gaps,
    "mcp-status": _cmd_mcp_status,
    "commit-request": _cmd_commit_request,
}

#: Documented so a caller knows what is safe to run unattended.
READ_ONLY_COMMANDS = frozenset({"checks", "doctor", "test-gaps", "mcp-status"})
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

    t = add("test-gaps", "suggest tests for changes against a base ref")
    t.add_argument("--base", default="origin/master")

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
