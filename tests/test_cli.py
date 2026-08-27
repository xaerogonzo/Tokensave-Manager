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
                        "data", "warnings", "error"}
    assert env["schema_version"] == cli.SCHEMA_VERSION
    assert env["cli_version"], "cli_version must never be empty"
    assert env["command"] == "mcp-status"


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


@pytest.mark.parametrize("command", sorted(cli._COMMANDS))
def test_every_command_requires_an_explicit_project(capsys, command):
    """No command may fall back to the ambient cwd."""
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

def test_every_command_is_classified_as_read_only_or_mutating():
    classified = cli.READ_ONLY_COMMANDS | cli.MUTATING_COMMANDS
    assert classified == set(cli._COMMANDS)
    assert not (cli.READ_ONLY_COMMANDS & cli.MUTATING_COMMANDS)


def test_doctor_is_read_only_so_it_never_applies_a_fix():
    assert "doctor" in cli.READ_ONLY_COMMANDS


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
    """Configuration truth and behavioural truth must not be collapsed."""
    _, env, _ = _run(capsys, ["mcp-status", "--project", project, "--json"])
    layers = env["data"]["layers"]
    assert set(layers) == {"project_config", "effective_scope", "behavioural"}
    assert layers["behavioural"]["probed"] is False, \
        "the CLI cannot observe behavioural truth and must not imply it can"


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


def test_checks_still_runs_from_a_source_checkout(capsys, project, mocker):
    mocker.patch("cli._is_frozen", return_value=False)
    mocker.patch("helpers.quality_checks.run_syntax_check",
                 return_value=(True, "passed"))
    mocker.patch("helpers.quality_checks.run_pyflakes_check",
                 return_value=(True, "passed"))
    code, env, _ = _run(capsys, ["checks", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["syntax"]["ok"] is True


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


@pytest.mark.parametrize("command", sorted(cli._COMMANDS))
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
