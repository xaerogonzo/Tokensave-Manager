"""tests/test_cli.py — the headless contract.

`src/cli.py` is the seam three future consumers depend on (VS Code tasks, the
VS Code extension, a Visual Studio companion server), so its output shape is a
contract rather than an implementation detail. These tests pin the parts that
would break a consumer silently:

  * **stdout carries exactly one complete envelope and nothing else.** A
    consumer parses that stream unconditionally; a stray print, a traceback, or
    half an envelope on a bad invocation turns a machine-readable channel into
    garbage.
  * **exit codes are semantic**, so "doctor found problems" is distinguishable
    from "the CLI itself failed" without scraping stderr.
  * **`--project` is mandatory.** Silent cwd inference is the exact mistake
    behind the MCP scope collision; the guard exists so it cannot come back.
  * **`mcp-status` does not probe `effective_scope` by default** — that spawns
    `claude mcp get` with a cwd, and invoking Claude Code in an unseen
    directory CREATES a project entry. This Manager littered `~/.claude.json`
    with eight duplicate keys exactly that way.
  * **a pending commit request is never silently destroyed**, because it
    represents work the user has not yet approved in the Manager.
"""
from __future__ import annotations

import json
import pathlib

import pytest

import cli
from cli import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PREREQUISITE,
    EXIT_USAGE,
    EXIT_VERIFY_FAILED,
    main,
)


def _run(capsys, argv):
    """Run main(argv); return (code, parsed_stdout_or_None, stderr)."""
    code = main(argv)
    out, err = capsys.readouterr()
    parsed = json.loads(out) if out.strip() else None
    return code, parsed, err


@pytest.fixture()
def project(tmp_path):
    return str(tmp_path)


# ── the envelope ────────────────────────────────────────────────────────────

def test_envelope_carries_every_contract_field(capsys, project):
    code, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    assert set(env) == {"schema_version", "cli_version", "command", "ok",
                        "data", "findings", "warnings", "error"}
    assert env["schema_version"] == cli.SCHEMA_VERSION
    assert env["cli_version"], "cli_version must never be empty"
    assert env["command"] == "mcp-status"


def test_findings_is_present_on_every_command_even_with_none(capsys, project):
    """`findings` is a cross-command contract, so the consumer never has to
    branch on its absence. A command that produces none sends an empty list,
    not a missing key."""
    for argv in (["mcp-status"], ["commit-request"]):
        _, env, _ = _run(capsys, argv + ["--project", project, "--json"])
        assert env["findings"] == []


def test_adding_findings_did_not_bump_the_schema(capsys, project):
    """The compatibility rule: ADDING an optional field is compatible, so a
    consumer that predates `findings` simply never reads the key. Removing or
    renaming one, or changing its meaning, is what bumps this."""
    _, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    assert env["schema_version"] == 1


def test_cli_version_comes_from_the_single_canonical_source():
    """Establishing a second version constant is how they drift apart."""
    from constants import APP_VERSION
    assert cli.APP_VERSION is APP_VERSION


def test_stdout_is_exactly_one_json_document(capsys, project):
    main(["mcp-status", "--project", project, "--json"])
    out, _ = capsys.readouterr()
    assert out.count("\n") == 1, "more than one line reached stdout"
    json.loads(out)                       # raises if anything else crept in


def test_ok_is_false_whenever_the_exit_code_is_not_zero(capsys, project):
    code, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    assert env["ok"] is (code == EXIT_OK)


def test_the_human_summary_goes_to_stderr_not_stdout(capsys, project):
    _, env, err = _run(capsys, ["mcp-status", "--project", project])
    assert err.strip(), "expected a human summary on stderr"
    assert "mcp" in err.lower()


def test_json_flag_silences_the_human_summary(capsys, project):
    _, _, err = _run(capsys, ["mcp-status", "--project", project, "--json"])
    assert err == ""


# ── usage errors must not emit a partial payload ────────────────────────────

@pytest.mark.parametrize("argv", [
    ["mcp-status"],                        # --project omitted
    ["definitely-not-a-command", "--project", "."],
    [],                                    # no subcommand
])
def test_invalid_usage_exits_2_with_empty_stdout(capsys, argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == EXIT_USAGE
    out, _ = capsys.readouterr()
    assert out == "", "a usage error must never put anything on stdout"


#: Commands that act on a repository. `commands` describes the Manager itself
#: and is deliberately project-less, so it is not subject to the two contract
#: tests below — but it IS asserted to be the only exception, so a future
#: command cannot quietly opt out of passing an explicit project.
_PROJECT_COMMANDS = sorted(set(cli._COMMANDS) - cli.PROJECTLESS_COMMANDS)


def test_the_project_less_commands_are_the_ones_about_the_manager():
    """The cwd-inference guard's exceptions, pinned by name.

    Both describe or act on the Manager itself rather than a repository:
    `commands` emits the vocabulary, `focus` raises the window. Demanding a
    project would mean an editor had to open a folder before it could ask what
    it may invoke, or bring the Manager forward.
    """
    assert cli.PROJECTLESS_COMMANDS == {"commands", "focus"}


@pytest.mark.parametrize("command", _PROJECT_COMMANDS)
def test_every_command_requires_an_explicit_project(capsys, command):
    """No command that touches a repository may fall back to the ambient cwd."""
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == EXIT_USAGE


# ── exit-code semantics ─────────────────────────────────────────────────────

def test_a_missing_project_is_a_prerequisite_failure(capsys, tmp_path):
    code, env, _ = _run(capsys, ["mcp-status", "--project",
                                 str(tmp_path / "nope"), "--json"])
    assert code == EXIT_PREREQUISITE
    assert "does not exist" in env["error"]


def test_a_missing_tokensave_is_a_prerequisite_failure(capsys, project, mocker):
    mocker.patch("helpers.config._load_config", return_value={})
    code, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert code == EXIT_PREREQUISITE
    assert "tokensave_exe" in env["error"]


def test_an_unreadable_doctor_scan_is_unverified_not_clean(capsys, project, mocker):
    """The distinction the whole purge contract rests on."""
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(False, error="boom"))
    code, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert code == EXIT_VERIFY_FAILED
    assert env["data"]["scanned"] is False
    assert env["data"].get("stale_count") is None, \
        "a failed scan must not report a stale count of any kind"


def test_stale_entries_are_an_expected_failure_not_a_crash(capsys, project, mocker):
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript="x", exit_code=0))
    mocker.patch("helpers.housekeeping.parse_stale_entries",
                 return_value=["a", "b"])
    code, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert code == EXIT_FAILED
    assert env["data"]["stale_count"] == 2


def test_an_unexpected_exception_still_yields_a_valid_envelope(capsys, project, mocker):
    """A traceback on stdout would be unreadable garbage to the extension."""
    mocker.patch("helpers.config._load_config",
                 side_effect=RuntimeError("kaboom"))
    code, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert code == EXIT_FAILED
    assert "kaboom" in env["error"]
    assert env["ok"] is False


# ── read-only vs mutating is documented and complete ────────────────────────

def test_every_command_is_classified_exactly_once():
    """The three classes partition the command set — no gaps, no overlaps."""
    classes = (cli.PURE_READ_COMMANDS, cli.OBSERVE_REFRESH_COMMANDS,
               cli.MUTATING_COMMANDS)
    assert set().union(*classes) == set(cli._COMMANDS)
    for i, first in enumerate(classes):
        for second in classes[i + 1:]:
            assert not (first & second)


def test_doctor_never_touches_the_project_but_is_not_a_pure_read():
    """Measured, not assumed — and the reason the old label was wrong.

    `doctor` still never applies a fix, which is the promise that matters to a
    user. But it is not write-free: it rewrites `~/.tokensave/state.toml` with
    byte-identical content, moving only the mtime, while touching neither
    `global.db` nor its WAL. A WAL-only check would have cleared it, which is
    exactly why the classification is measured against a broader snapshot and
    against an idle control run.
    """
    assert "doctor" in cli.OBSERVE_REFRESH_COMMANDS
    assert "doctor" in cli.UNATTENDED_SAFE_COMMANDS   # safe for the project
    assert "doctor" not in cli.PURE_READ_COMMANDS     # but not write-free
    assert "doctor" not in cli.MUTATING_COMMANDS


def test_cost_is_not_read_only_because_it_ingests():
    """`cost` and `discover` write rows into ~/.tokensave/global.db."""
    assert "cost" in cli.OBSERVE_REFRESH_COMMANDS
    assert "cost" not in cli.PURE_READ_COMMANDS


def test_unattended_safe_spans_both_non_mutating_classes():
    """"Does not touch your repository" is a weaker claim than "writes nothing".

    Both are worth being able to ask for; conflating them is what produced a
    `READ_ONLY_COMMANDS` set two of whose members wrote to disk.
    """
    assert cli.UNATTENDED_SAFE_COMMANDS == (
        cli.PURE_READ_COMMANDS | cli.OBSERVE_REFRESH_COMMANDS)
    assert not (cli.UNATTENDED_SAFE_COMMANDS & cli.MUTATING_COMMANDS)


# ── the contamination guard ─────────────────────────────────────────────────

def test_mcp_status_does_not_probe_effective_scope_by_default(capsys, project, mocker):
    """Probing spawns `claude mcp get`, which CREATES a project entry."""
    probe = mocker.patch("helpers.mcp.effective_scope")
    _, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    probe.assert_not_called()
    assert env["data"]["layers"]["effective_scope"]["probed"] is False


def test_mcp_status_probes_only_when_explicitly_asked(capsys, project, mocker):
    probe = mocker.patch("helpers.mcp.effective_scope", return_value="user")
    _, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json",
                              "--probe-effective"])
    probe.assert_called_once()
    assert env["data"]["layers"]["effective_scope"]["probed"] is True


def test_mcp_status_reports_the_layers_separately(capsys, project):
    """Configuration truth and behavioural truth must not be collapsed.

    An exact-set assertion, so adding a layer is a decision rather than
    drift. `vscode_logs` was added in Roadmap-16 and is a THIRD kind of
    truth, which is why it is its own layer rather than folded into
    either of the first two: it is **observed history** -- what VS Code
    did in windows that are already closed. It cannot say what the
    configuration is now, and it cannot say what a client would do next.
    """
    _, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    layers = env["data"]["layers"]
    assert set(layers) == {"project_config", "effective_scope",
                           "behavioural", "vscode_logs"}
    assert layers["behavioural"]["probed"] is False, \
        "the CLI cannot observe behavioural truth and must not imply it can"
    assert "content_observable" in layers["vscode_logs"], \
        "the log layer must say whether it could read anything at all"


# ── commit-request: never destroy a pending approval ────────────────────────

def test_reading_reports_no_pending_request_for_a_clean_project(capsys, project):
    code, env, _ = _run(capsys, ["commit-request", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["pending"] is None


def test_writing_then_reading_round_trips(capsys, project):
    main(["commit-request", "--project", project, "--files", "a.py",
          "--scope", "fix(x)", "--note", "n", "--json"])
    capsys.readouterr()
    _, env, _ = _run(capsys, ["commit-request", "--project", project, "--json"])
    pending = env["data"]["pending"]
    assert pending["files"] == ["a.py"]
    assert pending["suggested_scope"] == "fix(x)"


def test_a_conflicting_request_is_refused_and_the_original_survives(capsys, project):
    main(["commit-request", "--project", project, "--files", "first.py",
          "--note", "original", "--json"])
    capsys.readouterr()

    code, env, _ = _run(capsys, ["commit-request", "--project", project,
                                 "--files", "second.py", "--json"])
    assert code == EXIT_FAILED
    assert "--replace" in env["error"]

    _, after, _ = _run(capsys, ["commit-request", "--project", project, "--json"])
    assert after["data"]["pending"]["files"] == ["first.py"], \
        "the pending request the user has not approved was destroyed"


def test_replace_overwrites_deliberately(capsys, project):
    main(["commit-request", "--project", project, "--files", "first.py", "--json"])
    capsys.readouterr()
    code, _, _ = _run(capsys, ["commit-request", "--project", project,
                               "--files", "second.py", "--replace", "--json"])
    assert code == EXIT_OK
    _, after, _ = _run(capsys, ["commit-request", "--project", project, "--json"])
    assert after["data"]["pending"]["files"] == ["second.py"]


def test_an_identical_request_is_not_treated_as_a_conflict(capsys, project):
    """Re-filing the same request is idempotent, not an error."""
    argv = ["commit-request", "--project", project, "--files", "a.py",
            "--scope", "fix(x)", "--note", "n", "--json"]
    main(argv)
    capsys.readouterr()
    code, _, _ = _run(capsys, argv)
    assert code == EXIT_OK


def test_checks_in_a_frozen_build_explains_itself(capsys, project, mocker):
    """A packaged CLI cannot run `checks`, and must say why.

    Both checks shell out to `sys.executable -m ...`, which under the onefile
    build is the extracted binary rather than an interpreter. Left alone the
    user gets "[WinError 2] The system cannot find the file specified", which
    names neither the cause nor the fix.
    """
    mocker.patch("cli._is_frozen", return_value=True)
    code, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert code == EXIT_PREREQUISITE
    assert "Python interpreter" in env["error"]
    assert "WinError" not in env["error"]


def _check_result(ok=True, summary="passed", findings=()):
    """A CheckResult stand-in for the two runners `checks` calls."""
    from helpers.quality_checks import CheckResult
    return CheckResult(ok=ok, summary=summary, findings=list(findings),
                       output=summary)


def test_checks_still_runs_from_a_source_checkout(capsys, project, mocker):
    mocker.patch("cli._is_frozen", return_value=False)
    mocker.patch("helpers.quality_checks.run_syntax",
                 return_value=_check_result())
    mocker.patch("helpers.quality_checks.run_pyflakes",
                 return_value=_check_result())
    code, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["syntax"]["ok"] is True
    assert env["findings"] == []


def test_checks_reports_every_finding_not_just_the_first(capsys, project,
                                                         mocker):
    """The limitation this round exists to remove: `data.*.output` is a
    truncated status line (`first line (+N more)`), so before `findings` the
    extension could never show more than one problem."""
    from helpers.findings import Finding
    mocker.patch("cli._is_frozen", return_value=False)
    mocker.patch("helpers.quality_checks.run_syntax",
                 return_value=_check_result())
    mocker.patch("helpers.quality_checks.run_pyflakes", return_value=_check_result(
        ok=False,
        summary="src/a.py:1:1: 'os' imported but unused (+2 more)",
        findings=[Finding(file="src/a.py", line=n, column=1, rule="pyflakes",
                          message=f"finding {n}") for n in (1, 2, 3)]))
    code, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert code == EXIT_FAILED
    assert len(env["findings"]) == 3
    assert [f["line"] for f in env["findings"]] == [1, 2, 3]
    # The summary keeps its truncation — it is a status line, not the report.
    assert "more)" in env["data"]["pyflakes"]["output"]


def test_check_findings_carry_the_full_diagnostic_shape(capsys, project,
                                                        mocker):
    """The consumer maps these straight to a VS Code Diagnostic, so a missing
    key there is a crash in the editor rather than a test failure here."""
    from helpers.findings import Finding
    mocker.patch("cli._is_frozen", return_value=False)
    mocker.patch("helpers.quality_checks.run_syntax", return_value=_check_result(
        ok=False, summary="boom",
        findings=[Finding(file="src/a.py", line=4, column=2, severity="error",
                          message="invalid syntax",
                          rule="compileall/SyntaxError")]))
    mocker.patch("helpers.quality_checks.run_pyflakes",
                 return_value=_check_result())
    _, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert env["findings"][0] == {
        "file": "src/a.py", "line": 4, "column": 2,
        "end_line": 4, "end_column": 2,
        "severity": "error", "message": "invalid syntax",
        "rule": "compileall/SyntaxError", "symbol": "",
    }


# ── --config: how a relocated CLI is pointed at an install ──────────────────

def test_an_explicit_config_is_used_instead_of_the_one_beside_the_exe(
        capsys, project, tmp_path, mocker):
    """R12-8: the extension ships the CLI where no config lives."""
    cfg = tmp_path / "manager-config.json"
    cfg.write_text(json.dumps({"tokensave_exe": "from-explicit-config"}),
                   encoding="utf-8")
    beside = mocker.patch("helpers.config._load_config",
                          return_value={"tokensave_exe": "from-beside-exe"})
    scan = mocker.patch("helpers.doctor_service.scan_stale",
                        return_value=__import__(
                            "helpers.doctor_service", fromlist=["x"]
                        ).DoctorScanResult(True, transcript="", exit_code=0))

    _run(capsys, ["doctor", "--project", project, "--config", str(cfg), "--json"])
    beside.assert_not_called()
    assert scan.call_args.args[1] == "from-explicit-config"


def test_a_missing_config_file_is_a_prerequisite_failure(capsys, project, tmp_path):
    code, env, _ = _run(capsys, ["doctor", "--project", project,
                                 "--config", str(tmp_path / "nope.json"), "--json"])
    assert code == EXIT_PREREQUISITE
    assert "config file does not exist" in env["error"]


def test_an_unreadable_config_names_the_file(capsys, project, tmp_path):
    bad = tmp_path / "manager-config.json"
    bad.write_text("{ not json", encoding="utf-8")
    code, env, _ = _run(capsys, ["doctor", "--project", project,
                                 "--config", str(bad), "--json"])
    assert code == EXIT_PREREQUISITE
    assert str(bad) in env["error"]


def test_without_config_the_beside_exe_file_is_still_used(capsys, project, mocker):
    """The default must not change for a normal install."""
    beside = mocker.patch("helpers.config._load_config",
                          return_value={"tokensave_exe": "beside"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=__import__(
                     "helpers.doctor_service", fromlist=["x"]
                 ).DoctorScanResult(True, transcript="", exit_code=0))
    _run(capsys, ["doctor", "--project", project, "--json"])
    beside.assert_called_once()


@pytest.mark.parametrize("command", _PROJECT_COMMANDS)
def test_every_command_accepts_config(capsys, project, command):
    """A caller should not have to remember which commands read config."""
    parser = cli._build_parser()
    args = parser.parse_args([command, "--project", project, "--config", "x.json"])
    assert args.config == "x.json"


# ── the extension speaks the same contract ──────────────────────────────────

def test_the_vs_code_extension_exit_table_matches_this_one():
    """A cross-language drift guard.

    `vscode-extension/src/cli.ts` restates the exit codes so TypeScript can
    branch on them. Restated constants drift, and the failure would be silent
    and nasty: every CLI failure rendered as the wrong kind in the editor —
    "found problems" where the truth was "a prerequisite is missing".

    Parsed from the source rather than the compiled output so the check holds
    on a checkout that has never run `npm run compile`.
    """
    import re
    ts = pathlib.Path("vscode-extension/src/cli.ts")
    if not ts.is_file():
        pytest.skip("extension source not present")

    block = re.search(r"export const EXIT = \{(.*?)\} as const;",
                      ts.read_text(encoding="utf-8"), re.S)
    assert block, "could not find the EXIT table in cli.ts"
    declared = {name: int(value) for name, value in
                re.findall(r"(\w+):\s*(\d+)", block.group(1))}

    assert declared == {
        "OK": EXIT_OK,
        "FAILED": EXIT_FAILED,
        "USAGE": EXIT_USAGE,
        "PREREQUISITE": EXIT_PREREQUISITE,
        "VERIFY_FAILED": EXIT_VERIFY_FAILED,
    }


def test_the_extension_understands_the_current_envelope_schema():
    """If the CLI's schema outruns the extension's, the editor stops parsing.

    Bumping SCHEMA_VERSION without updating the extension is legitimate — it is
    exactly what `cli_version` and the guard exist for — but it must be a
    deliberate act, not a surprise found by a user.
    """
    import re
    ts = pathlib.Path("vscode-extension/src/cli.ts")
    if not ts.is_file():
        pytest.skip("extension source not present")
    match = re.search(r"SUPPORTED_SCHEMA = (\d+)", ts.read_text(encoding="utf-8"))
    assert match, "could not find SUPPORTED_SCHEMA in cli.ts"
    assert int(match.group(1)) == cli.SCHEMA_VERSION, (
        "the extension and the CLI disagree about the envelope schema; "
        "update vscode-extension/src/cli.ts")


# ── the CLI must coexist with a running GUI ─────────────────────────────────

def test_the_cli_never_takes_the_guis_single_instance_lock():
    """Taking it would make the CLI fight a Manager the user has open."""
    source = (__import__("pathlib").Path(cli.__file__)).read_text(encoding="utf-8")
    assert "_acquire_instance_lock" not in source


def test_importing_the_cli_does_not_drag_in_tk():
    """The console build must run without the GUI's dependencies."""
    import subprocess
    import sys as _sys
    from pathlib import Path
    src = str(Path(cli.__file__).parent)
    proc = subprocess.run(
        [_sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{src}'); import cli; "
         "sys.exit(1 if 'tkinter' in sys.modules else 0)"],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"cli.py pulled in tkinter: {proc.stderr}"


# ── doctor: the audit producer (R12-10) ─────────────────────────────────────

def _audit_project(tmp_path, name="mod.py", branches=30):
    """A project whose one file trips the complexity cap."""
    src = tmp_path / "pkg"
    src.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    if x == {i}: return {i}" for i in range(branches))
    src.joinpath(name).write_text(f"def wide(x):\n{body}\n    return None\n",
                                  encoding="utf-8")
    return str(tmp_path)


def test_doctor_audit_findings_carry_file_and_line(capsys, tmp_path, mocker):
    """R12-10: cap violations used to be plain strings, so a Doctor finding
    could not be clicked to a line while a scout finding could."""
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript="", exit_code=0))
    mocker.patch("helpers.housekeeping.parse_stale_entries", return_value=[])
    _, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                              "--json"])
    finding = env["findings"][0]
    assert finding["file"] == "pkg/mod.py"
    assert finding["line"] == 1
    assert finding["symbol"] == "wide"
    assert finding["rule"] == "doctor/audit"
    assert finding["severity"] == "warning"


def test_doctor_audit_is_advisory_and_does_not_fail_the_command(
        capsys, tmp_path, mocker):
    """Cap violations are a health signal, not a broken build — this repo runs
    the same audit `continue-on-error` in its generated CI. A tree with 144 of
    them must not make `doctor` permanently red."""
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript="", exit_code=0))
    mocker.patch("helpers.housekeeping.parse_stale_entries", return_value=[])
    code, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                                 "--json"])
    assert code == EXIT_OK, "the audit is advisory"
    assert env["findings"], "but it still reports"
    assert env["data"]["audit"]["violation_count"] >= 1


def test_stale_entries_still_drive_the_exit_code(capsys, tmp_path, mocker):
    """The pre-existing contract, unchanged by the audit arriving."""
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript="x", exit_code=0))
    mocker.patch("helpers.housekeeping.parse_stale_entries", return_value=["a"])
    code, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                                 "--json"])
    assert code == EXIT_FAILED
    assert env["data"]["stale_count"] == 1


def test_the_audit_survives_a_stale_scan_that_cannot_run(capsys, tmp_path):
    """The two halves have different prerequisites: the audit needs only the
    source tree. A project with no tokensave configured should still get it,
    and the code stays 3 so the extension can still offer Settings."""
    cfg = tmp_path / "manager-config.json"
    cfg.write_text(json.dumps({"git_exe": "git"}), encoding="utf-8")
    code, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                                 "--json", "--config", str(cfg)])
    assert code == EXIT_PREREQUISITE
    assert "tokensave_exe" in env["error"]
    assert env["data"]["scanned"] is False
    assert env["findings"], "the audit ran regardless"


def test_a_config_path_that_does_not_exist_still_fails_fast(capsys, tmp_path):
    """Naming a file that is not there is a user error, and saying so beats
    quietly proceeding with an empty config — the audit is not a reason to
    swallow a bad `--config`."""
    code, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                                 "--json", "--config", "no-such-config.json"])
    assert code == EXIT_PREREQUISITE
    assert "does not exist" in env["error"]


def test_doctor_findings_never_leak_a_native_path(capsys, tmp_path, mocker):
    from helpers.doctor_service import DoctorScanResult
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript="", exit_code=0))
    mocker.patch("helpers.housekeeping.parse_stale_entries", return_value=[])
    _, env, _ = _run(capsys, ["doctor", "--project", _audit_project(tmp_path),
                              "--json"])
    for finding in env["findings"]:
        assert chr(92) not in finding["file"]


# ── scout: the third findings producer ──────────────────────────────────────

def _scout_finding(kind="complexity", **kw):
    from helpers.refactor_scout import Finding as ScoutFinding
    base = dict(id="abc", kind=kind, file="src/a.py", symbol="wide", line=12,
                message="Cyclomatic complexity 23 (cap: 10)")
    base.update(kw)
    return ScoutFinding(**base)


def test_scout_maps_every_kind_to_the_closed_severity_set(capsys, project,
                                                          mocker):
    """Severity is chosen HERE, in the producer. The consumer must never infer
    it from the rule name, so the mapping needs a test on this side."""
    from helpers.findings import SEVERITIES
    mocker.patch("helpers.refactor_scout.run_scout", return_value=(
        {"complexity": [_scout_finding("complexity")],
         "god_class": [_scout_finding("god_class")],
         "god_file": [_scout_finding("god_file")],
         "dead_code": [_scout_finding("dead_code")]}, 0))
    _, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    by_rule = {f["rule"]: f["severity"] for f in env["findings"]}
    assert by_rule == {
        "scout/complexity": "information",
        "scout/god_class": "information",
        "scout/god_file": "information",
        "scout/dead_code": "warning",
    }
    assert set(by_rule.values()) <= set(SEVERITIES)


def test_scout_carries_the_position_the_index_already_knew(capsys, project,
                                                           mocker):
    mocker.patch("helpers.refactor_scout.run_scout",
                 return_value=({"complexity": [_scout_finding()]}, 0))
    _, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert env["findings"][0]["file"] == "src/a.py"
    assert env["findings"][0]["line"] == 12
    assert env["findings"][0]["symbol"] == "wide"


def test_scout_does_not_fail_the_command_when_it_finds_things(capsys, project,
                                                              mocker):
    """A report, not a gate — same as test-gaps. A command that goes red
    because a codebase has a complex function is one people switch off."""
    mocker.patch("helpers.refactor_scout.run_scout",
                 return_value=({"complexity": [_scout_finding()]}, 0))
    code, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["total"] == 1


def test_scout_without_an_index_says_so_instead_of_reporting_clean(
        capsys, project, mocker):
    """"No index" and "nothing to report" are different answers, and only one
    of them is actionable."""
    mocker.patch("helpers.refactor_scout.run_scout",
                 side_effect=FileNotFoundError("No tokensave index at X"))
    code, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert code == EXIT_PREREQUISITE
    assert "tokensave index" in env["error"]
    assert env["findings"] == []


def test_scout_reports_suppressed_findings_without_listing_them(capsys,
                                                                project,
                                                                mocker):
    mocker.patch("helpers.refactor_scout.run_scout",
                 return_value=({"complexity": [_scout_finding()]}, 4))
    _, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert env["data"]["suppressed"] == 4
    assert len(env["findings"]) == 1


def test_scout_does_not_publish_its_internal_suppression_id(capsys, project,
                                                            mocker):
    """scout's `md5(kind|file|symbol)` ignores the message and the range, so it
    is not the envelope's notion of identity. Publishing it would put two
    different things called "identity" into the contract."""
    mocker.patch("helpers.refactor_scout.run_scout",
                 return_value=({"complexity": [_scout_finding()]}, 0))
    _, env, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert "id" not in env["findings"][0]


def test_scout_is_a_pure_read():
    assert "scout" in cli.PURE_READ_COMMANDS


# ── status: cheap, closed, and non-probing ──────────────────────────────────

def test_status_payload_is_closed(capsys, project):
    """"One roll-up" is an invitation to dump the whole Manager into JSON.
    The keys are fixed so that stays a decision rather than a drift."""
    _, env, _ = _run(capsys, ["status", "--project", project, "--json"])
    assert set(env["data"]) == {"git", "tokensave", "mcp", "commit_request"}
    assert set(env["data"]["git"]) == {
        "branch", "dirty", "ahead", "behind", "has_remote", "changed_files",
        "changed_truncated"}


def test_status_names_the_changed_files(capsys, tmp_path, mocker):
    """The commit-request composer's data source.

    It lives here rather than in a `git-status` command of its own because it
    is free: the porcelain-v2 parser already visited every per-file record to
    decide `dirty` and threw the paths away. That is what keeps this inside the
    "tens of milliseconds" budget the command's own docstring sets.
    """
    mocker.patch("helpers.git.read_git_status", return_value={
        "branch": "main", "dirty": True, "ahead": 2, "behind": 1,
        "has_remote": True, "changed_truncated": False,
        "changed_files": [
            {"path": "src/app.py", "status": "modified"},
            {"path": "notes.txt", "status": "untracked"},
            {"path": "b.py", "status": "renamed", "old_path": "a.py"},
        ],
    })
    _, env, _ = _run(capsys, ["status", "--project", str(tmp_path), "--json"])
    git = env["data"]["git"]

    assert git["ahead"] == 2 and git["behind"] == 1
    assert git["has_remote"] is True
    assert [f["path"] for f in git["changed_files"]] == [
        "src/app.py", "notes.txt", "b.py"]
    assert git["changed_files"][2]["old_path"] == "a.py"


def test_status_unreadable_repo_reports_changed_files_and_ahead_as_unknown(
        capsys, tmp_path, mocker):
    """`None`, not `[]`. An unreadable repository is not a clean one, and a
    composer offering "no files changed" would be inventing an answer."""
    mocker.patch("helpers.git.read_git_status", return_value=None)
    _, env, _ = _run(capsys, ["status", "--project", str(tmp_path), "--json"])
    git = env["data"]["git"]

    assert git["changed_files"] is None
    assert git["dirty"] is None
    assert git["ahead"] is None


def test_status_says_it_did_not_probe_mcp(capsys, project):
    """`configured` is a fact about a file; it is not an effective-scope verdict.

    Without `probed`, a consumer reading `configured: true` could reasonably
    render a bare "MCP ✓" — which is the claim this command specifically cannot
    make, because probing is what creates `~/.claude.json` entries.
    """
    _, env, _ = _run(capsys, ["status", "--project", project, "--json"])
    assert env["data"]["mcp"]["probed"] is False


def test_status_reports_a_truncated_file_list_as_truncated(
        capsys, tmp_path, mocker):
    mocker.patch("helpers.git.read_git_status", return_value={
        "branch": "main", "dirty": True, "ahead": 0, "behind": 0,
        "has_remote": False,
        "changed_files": [{"path": "f.py", "status": "modified"}],
        "changed_truncated": True,
    })
    _, env, _ = _run(capsys, ["status", "--project", str(tmp_path), "--json"])
    assert env["data"]["git"]["changed_truncated"] is True


def test_status_never_probes_effective_scope(capsys, project, mocker):
    """THE regression test for this command.

    `effective_scope` spawns `claude mcp get` with a cwd, and invoking Claude
    Code in a directory it has not seen CREATES a project entry — this Manager
    littered `~/.claude.json` with eight duplicate keys that way. `status` is
    the one command a UI may call on every refresh, so it probing would
    rebuild that problem at a much higher rate.
    """
    probe = mocker.patch("helpers.mcp.effective_scope")
    _run(capsys, ["status", "--project", project, "--json"])
    probe.assert_not_called()


def test_mcp_status_also_does_not_probe_unless_asked(capsys, project, mocker):
    probe = mocker.patch("helpers.mcp.effective_scope")
    _run(capsys, ["mcp-status", "--project", project, "--json"])
    probe.assert_not_called()


def test_status_runs_neither_doctor_nor_scout_nor_pytest(capsys, project,
                                                         mocker):
    """It must stay in the tens of milliseconds. Anything that walks the tree
    belongs in its own command."""
    audit = mocker.patch("helpers.doctor_rules._audit_project_tree")
    scout = mocker.patch("helpers.refactor_scout.run_scout")
    _run(capsys, ["status", "--project", project, "--json"])
    audit.assert_not_called()
    scout.assert_not_called()


def test_status_reports_an_unreadable_repo_as_unknown_not_clean(
        capsys, project, mocker):
    """`dirty: None` is "we could not find out". Reporting False there would
    tell the user their tree is clean when nothing was actually checked."""
    mocker.patch("helpers.git.read_git_status", return_value=None)
    _, env, _ = _run(capsys, ["status", "--project", project, "--json"])
    assert env["data"]["git"]["dirty"] is None
    assert env["data"]["git"]["branch"] is None


def test_status_distinguishes_no_index_from_an_empty_one(capsys, project):
    _, env, _ = _run(capsys, ["status", "--project", project, "--json"])
    assert env["data"]["tokensave"] == {"indexed": False}, \
        "an absent index reports absent, not a count of zero"


def test_status_reads_the_index_counts_when_there_is_one(capsys, tmp_path):
    import sqlite3
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    con = sqlite3.connect(str(ts / "tokensave.db"))
    con.execute("CREATE TABLE files (id INTEGER)")
    con.execute("CREATE TABLE nodes (id INTEGER)")
    con.executemany("INSERT INTO files VALUES (?)", [(1,), (2,), (3,)])
    con.execute("INSERT INTO nodes VALUES (1)")
    con.commit()
    con.close()
    _, env, _ = _run(capsys, ["status", "--project", str(tmp_path), "--json"])
    index = env["data"]["tokensave"]
    assert index["indexed"] is True
    assert (index["file_count"], index["node_count"]) == (3, 1)
    assert index["last_sync_at"] > 0


def test_an_unreadable_index_is_not_reported_as_an_empty_one(capsys, tmp_path):
    """A file that is not a database still means "there is an index here" —
    reporting zero files would read as a successfully-scanned empty project."""
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    ts.joinpath("tokensave.db").write_text("not a database", encoding="utf-8")
    _, env, _ = _run(capsys, ["status", "--project", str(tmp_path), "--json"])
    index = env["data"]["tokensave"]
    assert index["indexed"] is True
    assert index["readable"] is False
    assert "file_count" not in index


@pytest.mark.parametrize("config", [
    pytest.param({}, id="no-config"),
    pytest.param(RuntimeError("unreadable"), id="broken-config"),
])
def test_status_answers_even_when_the_config_does_not(capsys, project, mocker,
                                                      config):
    """An editor may open a project on a machine with no configured Manager,
    or with a corrupt config. `status` is what a UI calls to find out what
    state things are in, so a bad config is something it should REPORT rather
    than die of."""
    if isinstance(config, Exception):
        mocker.patch("helpers.config._load_config", side_effect=config)
    else:
        mocker.patch("helpers.config._load_config", return_value=config)
    code, env, _ = _run(capsys, ["status", "--project", project, "--json"])
    assert code == EXIT_OK
    assert set(env["data"]) == {"git", "tokensave", "mcp", "commit_request"}


def test_status_is_read_only():
    assert "status" in cli.PURE_READ_COMMANDS


# ── contract fixes: sync `changed`, request identity, base-ref honesty ──────

def _sync_result(**kw):
    from helpers.sync_service import ShadowPrep, SyncResult
    base = dict(ok=True, returncode=0, output="", argv=["sync"],
                shadows=ShadowPrep(), error="", counts=None)
    base.update(kw)
    return SyncResult(**base)


@pytest.mark.parametrize("counts,expected", [
    pytest.param({"added": 0, "modified": 0, "removed": 0}, False, id="no-op"),
    pytest.param({"added": 3, "modified": 4, "removed": 0}, True, id="changed"),
    pytest.param(None, None, id="tool-did-not-say"),
])
def test_sync_reports_changed_as_three_valued(capsys, project, mocker,
                                              counts, expected):
    """`null` is not `false`. A caller deciding whether to refresh must never
    read "we could not tell" as "nothing happened" — the same rule behind
    DoctorScanResult.ok and EXIT_VERIFY_FAILED."""
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.sync_service.run_sync",
                 return_value=_sync_result(counts=counts))
    _, env, _ = _run(capsys, ["sync", "--project", project, "--json"])
    assert env["data"]["changed"] is expected


def test_sync_changed_is_observed_not_inferred_from_the_exit_code(
        capsys, project, mocker):
    """A zero exit says the command ran, not that it did anything."""
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.sync_service.run_sync",
                 return_value=_sync_result(ok=True, returncode=0, counts=None))
    _, env, _ = _run(capsys, ["sync", "--project", project, "--json"])
    assert env["ok"] is True
    assert env["data"]["changed"] is None


def test_refiling_an_identical_request_is_idempotent(capsys, project):
    """Same request twice is not a conflict — it is the same request."""
    argv = ["commit-request", "--project", project, "--json",
            "--files", "src/a.py", "src/b.py", "--scope", "core"]
    code, _, _ = _run(capsys, argv)
    assert code == EXIT_OK
    code, env, _ = _run(capsys, argv)
    assert code == EXIT_OK
    assert env["data"]["unchanged"] is True


def test_identity_ignores_whitespace_and_the_timestamp(capsys, project):
    """The old field-by-field check compared unstripped paths against a reader
    that stripped them, so a trailing space made a request differ from itself."""
    _run(capsys, ["commit-request", "--project", project, "--json",
                  "--files", "src/a.py"])
    code, env, _ = _run(capsys, ["commit-request", "--project", project,
                                 "--json", "--files", "src/a.py "])
    assert code == EXIT_OK, "a trailing space is not a different request"
    assert env["data"]["unchanged"] is True


def test_a_genuinely_different_request_is_still_refused(capsys, project):
    """A pending request is work the user has not approved yet."""
    _run(capsys, ["commit-request", "--project", project, "--json",
                  "--files", "src/a.py"])
    code, env, _ = _run(capsys, ["commit-request", "--project", project,
                                 "--json", "--files", "src/other.py"])
    assert code == EXIT_FAILED
    assert "--replace" in env["error"]


def test_replace_overrides_the_refusal(capsys, project):
    _run(capsys, ["commit-request", "--project", project, "--json",
                  "--files", "src/a.py"])
    code, env, _ = _run(capsys, ["commit-request", "--project", project,
                                 "--json", "--files", "src/other.py",
                                 "--replace"])
    assert code == EXIT_OK
    assert env["data"]["files"] == ["src/other.py"]


def test_a_base_ref_that_does_not_exist_is_refused_not_answered(
        capsys, project, mocker):
    """`git diff` against a missing ref yields no changed files, which renders
    as "0 test gaps" — the most reassuring possible way to report that the
    question was never asked."""
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    mocker.patch("helpers.git.ref_exists", return_value=False)
    suggest = mocker.patch("helpers.test_gap_report.suggest_tests_for_diff")
    code, env, _ = _run(capsys, ["test-gaps", "--project", project,
                                 "--base", "origin/nope", "--json"])
    assert code == EXIT_PREREQUISITE
    assert "origin/nope" in env["error"]
    # It must not answer a question it was never able to ask.
    suggest.assert_not_called()


def test_a_missing_base_never_silently_falls_back(capsys, project, mocker):
    """No quiet substitution of master, HEAD or the current branch."""
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    exists = mocker.patch("helpers.git.ref_exists", return_value=False)
    _run(capsys, ["test-gaps", "--project", project, "--base", "origin/nope",
                  "--json"])
    assert [c.args[-1] for c in exists.call_args_list] == ["origin/nope"]


# ── tests / test-run ────────────────────────────────────────────────────────

def _suite(tmp_path, body):
    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    tests.joinpath("test_x.py").write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_tests_command_runs_no_pytest(capsys, project, mocker):
    """Discovery is AST-only. `test-run` is the one that spends 50 seconds."""
    run = mocker.patch("helpers.smoke_runner.run_smoke_tests")
    code, _, _ = _run(capsys, ["tests", "--project", project, "--json"])
    assert code == EXIT_OK
    run.assert_not_called()


def test_tests_counts_are_exact_but_lists_are_capped(capsys, project, mocker):
    """A monorepo must not make an editor parse tens of thousands of entries
    to draw a tree. Counts stay exact; the lists are the convenience."""
    from helpers.test_discovery import TestFileInfo
    many = [TestFileInfo(path=f"t{i}.py", name=f"t{i}.py", test_count=1)
            for i in range(cli._LIST_CAP + 25)]
    mocker.patch("helpers.test_discovery.list_test_files", return_value=many)
    mocker.patch("helpers.test_discovery.scan_coverage_gaps", return_value=[])
    mocker.patch("helpers.test_discovery.detect_stale_tests", return_value=[])
    _, env, _ = _run(capsys, ["tests", "--project", project, "--json"])
    block = env["data"]["test_files"]
    assert block["total_count"] == cli._LIST_CAP + 25
    assert block["truncated"] is True
    assert len(block["items"]) == cli._LIST_CAP
    assert env["data"]["test_count"] == cli._LIST_CAP + 25, "counts stay exact"


def test_a_short_list_is_not_marked_truncated(capsys, project, mocker):
    mocker.patch("helpers.test_discovery.list_test_files", return_value=[])
    mocker.patch("helpers.test_discovery.scan_coverage_gaps", return_value=[])
    mocker.patch("helpers.test_discovery.detect_stale_tests", return_value=[])
    _, env, _ = _run(capsys, ["tests", "--project", project, "--json"])
    assert env["data"]["test_files"]["truncated"] is False


def test_test_run_counts_passed_failed_and_skipped(capsys, tmp_path):
    """Against a suite with a known mix, so the arithmetic is pinned rather
    than assumed: `_parse_pytest_summary` returns (passed, passed+failed+
    errored) and leaves SKIPPED out of the total, so subtracting skips from it
    would count them twice."""
    project = _suite(tmp_path, "import pytest\n"
                               "def test_a(): assert 1\n"
                               "def test_b(): assert 1\n"
                               "@pytest.mark.skip\n"
                               "def test_c(): assert 0\n"
                               "def test_d(): assert 0\n")
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_FAILED
    data = env["data"]
    assert (data["passed"], data["failed"], data["skipped"]) == (2, 1, 1)
    assert data["duration_seconds"] >= 0


def test_a_green_suite_exits_zero(capsys, tmp_path):
    project = _suite(tmp_path, "def test_a(): assert 1\n")
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["failed"] == 0


def test_test_run_emits_no_findings(capsys, tmp_path):
    """A failing test is not a diagnostic about a line of source. Turning a red
    suite into thousands of Problems entries would bury the real ones."""
    project = _suite(tmp_path, "def test_a(): assert 0\n")
    _, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert env["findings"] == []
    assert env["data"]["output"], "the detail belongs in output instead"


def test_an_unreadable_summary_is_unverified_not_passing(capsys, tmp_path,
                                                         mocker):
    """A timeout or a collection error must never render as a green run."""
    project = _suite(tmp_path, "def test_a(): assert 1\n")
    mocker.patch("helpers.smoke_runner.run_smoke_tests",
                 return_value=(0, 0, "pytest timed out after 300 s."))
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_VERIFY_FAILED
    assert "could not read" in env["error"]


def test_a_project_with_no_suite_is_a_prerequisite_not_a_failure(capsys,
                                                                 project):
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_PREREQUISITE
    assert "tests/" in env["error"]


def test_a_second_run_is_refused_while_one_holds_the_lock(capsys, tmp_path):
    """Two clicks a second apart must not start two suites over one tree."""
    from helpers.test_lock import test_run_lock
    project = _suite(tmp_path, "def test_a(): assert 1\n")
    with test_run_lock(project):
        code, env, _ = _run(capsys, ["test-run", "--project", project,
                                     "--json"])
    assert code == EXIT_FAILED
    assert "already in progress" in env["error"]
    assert env["data"]["running"] is True


def test_the_lock_is_released_so_the_next_run_succeeds(capsys, tmp_path):
    project = _suite(tmp_path, "def test_a(): assert 1\n")
    assert _run(capsys, ["test-run", "--project", project, "--json"])[0] == EXIT_OK
    assert _run(capsys, ["test-run", "--project", project, "--json"])[0] == EXIT_OK


def test_two_different_projects_can_run_at_once(capsys, tmp_path):
    """The lock is per-project, so unrelated trees never block each other."""
    from helpers.test_lock import TestRunBusy, test_run_lock
    a = _suite(tmp_path / "a", "def test_a(): assert 1\n")
    b = _suite(tmp_path / "b", "def test_a(): assert 1\n")
    with test_run_lock(a):
        try:
            with test_run_lock(b):
                pass
        except TestRunBusy:                     # pragma: no cover
            pytest.fail("a lock in one project blocked another")


def test_test_run_is_observe_refresh_and_tests_is_a_pure_read():
    """`tests` only discovers; `test-run` actually runs pytest.

    Running a suite leaves `.pytest_cache` and coverage artefacts behind, so it
    is not a pure read even though it changes no source. Discovery writes
    nothing at all.
    """
    assert "test-run" in cli.OBSERVE_REFRESH_COMMANDS
    assert "tests" in cli.PURE_READ_COMMANDS


# ── found in real use, 2026-08-27 ───────────────────────────────────────────

def test_a_project_with_no_python_tests_is_not_reported_as_unverifiable(
        capsys, tmp_path):
    """Found on a PowerShell project with a tests/ directory: pytest collected
    0 items and said "no tests ran", and this command called that "could not
    verify the result".

    `_parse_pytest_summary` scans for passed/failed/error, and pytest's
    zero-test footer contains none of them, so it returns (0, 0) — the same
    value it returns when the output was unreadable. Conflating them is exactly
    what EXIT_VERIFY_FAILED exists to prevent, committed by the code enforcing
    it.
    """
    project = _suite(tmp_path, "# a file with no tests in it\n")
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["collected"] == 0
    assert env["error"] is None


def test_an_actually_unreadable_run_is_still_unverifiable(capsys, tmp_path,
                                                          mocker):
    """The fix must not swallow the case it was carved out of."""
    project = _suite(tmp_path, "def test_a(): assert 1\n")
    mocker.patch("helpers.smoke_runner.run_smoke_tests",
                 return_value=(0, 0, "pytest timed out after 300 s."))
    code, env, _ = _run(capsys, ["test-run", "--project", project, "--json"])
    assert code == EXIT_VERIFY_FAILED
    assert "could not read" in env["error"]


@pytest.mark.parametrize("output,expected", [
    pytest.param("===== no tests ran in 0.17s =====", True, id="no-tests-ran"),
    pytest.param("collecting ... collected 0 items", True, id="collected-zero"),
    pytest.param("pytest timed out after 300 s.", False, id="timeout"),
    pytest.param("", False, id="nothing-at-all"),
    pytest.param("INTERNALERROR> boom", False, id="crash"),
])
def test_only_pytest_saying_so_counts_as_zero_tests(output, expected):
    assert cli._collected_nothing(output) is expected


def test_test_gaps_asks_the_repo_for_its_default_branch(capsys, project,
                                                        mocker):
    """`origin/master` as a hardcoded default is simply wrong on every repo
    that uses `main` — and it presented as "0 test gaps", not as an error.
    `auto` reads refs/remotes/origin/HEAD, which is the answer git already
    holds rather than a guess between the two names."""
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    mocker.patch("helpers.git.default_base_ref", return_value="origin/main")
    mocker.patch("helpers.git.ref_exists", return_value=True)
    suggest = mocker.patch("helpers.test_gap_report.suggest_tests_for_diff",
                           return_value=[])
    code, env, _ = _run(capsys, ["test-gaps", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["base"] == "origin/main"
    assert env["data"]["base_requested"] == cli.AUTO_BASE
    assert suggest.call_args.args[-1] == "origin/main"


def test_an_explicit_base_is_never_replaced_by_the_detected_one(capsys,
                                                                project,
                                                                mocker):
    """Honouring what was asked for is the whole point; `auto` is opt-in."""
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    detect = mocker.patch("helpers.git.default_base_ref",
                          return_value="origin/main")
    mocker.patch("helpers.git.ref_exists", return_value=True)
    suggest = mocker.patch("helpers.test_gap_report.suggest_tests_for_diff",
                           return_value=[])
    _, env, _ = _run(capsys, ["test-gaps", "--project", project, "--json",
                              "--base", "v1.0"])
    assert env["data"]["base"] == "v1.0"
    assert suggest.call_args.args[-1] == "v1.0"
    detect.assert_not_called()


def test_auto_refuses_rather_than_guessing_when_the_repo_will_not_say(
        capsys, project, mocker):
    """No trying `main` then `master`: picking one is how a diff ends up
    answering a different question than the one asked."""
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    mocker.patch("helpers.git.default_base_ref", return_value=None)
    code, env, _ = _run(capsys, ["test-gaps", "--project", project, "--json"])
    assert code == EXIT_PREREQUISITE
    assert "default branch" in env["error"]


def test_a_bad_explicit_base_names_the_one_that_would_have_worked(capsys,
                                                                  project,
                                                                  mocker):
    mocker.patch("helpers.config._load_config", return_value={"git_exe": "git"})
    mocker.patch("helpers.git.default_base_ref", return_value="origin/main")
    mocker.patch("helpers.git.ref_exists", return_value=False)
    _, env, _ = _run(capsys, ["test-gaps", "--project", project, "--json",
                              "--base", "origin/master"])
    assert "origin/master" in env["error"]
    assert "origin/main" in env["error"], "the hint is the useful half"


def test_scout_honours_suppressions_made_in_the_manager(capsys, project,
                                                        mocker):
    """A finding dismissed in the Manager's scout dialog came straight back in
    the editor, because this command called run_scout with no ignored set. That
    makes the Ignore button look broken, and is the split-brain the single
    findings contract exists to avoid."""
    mocker.patch("helpers.config._load_config",
                 return_value={"refactor_scout_ignored": ["abc", "def"]})
    run = mocker.patch("helpers.refactor_scout.run_scout",
                       return_value=({}, 2))
    _run(capsys, ["scout", "--project", project, "--json"])
    assert run.call_args.args[1] == {"abc", "def"}


def test_scout_still_runs_when_there_is_no_config_to_read(capsys, project,
                                                          mocker):
    mocker.patch("helpers.config._load_config", return_value={})
    run = mocker.patch("helpers.refactor_scout.run_scout",
                       return_value=({}, 0))
    code, _, _ = _run(capsys, ["scout", "--project", project, "--json"])
    assert code == EXIT_OK
    assert run.call_args.args[1] == set()


def test_doctor_reports_the_count_tokensave_gives_not_the_bullets_it_shows(
        capsys, project, mocker):
    """tokensave TRUNCATES its stale list — ten bullets, then "… and 2 more" —
    so counting the entries we could name reported 12 stale projects as 10."""
    from helpers.doctor_service import DoctorScanResult
    transcript = ("  ! 12 stale project(s) in global DB (registered but gone):\n"
                  "      - D:/a\n      - D:/b\n      ... and 10 more\n"
                  "        Re-run `tokensave doctor` interactively to purge.\n")
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript=transcript,
                                               exit_code=0))
    _, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert env["data"]["stale_count"] == 12
    assert len(env["data"]["stale"]) == 2
    assert env["data"]["stale_truncated"] is True


def test_an_untruncated_stale_list_is_not_marked_truncated(capsys, project,
                                                           mocker):
    from helpers.doctor_service import DoctorScanResult
    transcript = ("  ! 2 stale project(s) in global DB (registered but gone):\n"
                  "      - D:/a\n      - D:/b\n"
                  "        Re-run `tokensave doctor` interactively to purge.\n")
    mocker.patch("helpers.config._load_config",
                 return_value={"tokensave_exe": "tokensave"})
    mocker.patch("helpers.doctor_service.scan_stale",
                 return_value=DoctorScanResult(True, transcript=transcript,
                                               exit_code=0))
    _, env, _ = _run(capsys, ["doctor", "--project", project, "--json"])
    assert env["data"]["stale_count"] == 2
    assert env["data"]["stale_truncated"] is False
