"""cli_support.py - the contract and the plumbing every CLI command shares.

Split out of `cli.py` (2026-09-11), which was 1,657 lines against a 1,500
cap. This half is the part that does not belong to any one command: the exit
codes, the envelope, the `Result` it travels in, and the resolvers for
project path, manager config and tokensave executable.

**`_Prerequisite` living here is load-bearing, not tidiness.** It is raised in
one module and caught in another, so there must be exactly ONE class object.
Had each importer defined or re-imported its own through a module that can
also run as `__main__`, the `except` would simply not match and a
prerequisite failure would surface as an unhandled traceback instead of
EXIT_PREREQUISITE. Same reasoning for `Result` and the EXIT_* values.

`cli.py` re-imports every name here into its own namespace, deliberately: the
tests patch `cli._load_manager_config` and `cli._is_frozen`, and a command
defined in `cli.py` resolves those through `cli`'s globals, so those patches
keep working untouched. Commands that moved OUT resolve them through `cli` at
call time for the same reason - see `cli_test_commands`.
"""
from __future__ import annotations

import json
import os

from constants import APP_VERSION
from helpers.findings import to_envelope


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

