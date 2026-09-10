"""Guards on `helpers/headless_analyzers.py`.

Two things are load-bearing here and neither is obvious from the module:

**The severity policy is tested apart from the parser**, so a future parser
refactor cannot move severity semantics by accident. It is a Manager-owned
decision, not a detail of reading JSON.

**Every non-success state is asserted to be non-success.** A tool that is
missing, or that ran and failed, must never produce the same result shape as a
tool that ran and found nothing — that equivalence is the entire failure mode
this module was written against.
"""

import dataclasses
import json
import os
import subprocess

import pytest

from helpers.headless_analyzers import (
    ANALYZERS, AVAILABILITY, AVAILABILITY_TEXT, BY_KEY, FAILED, MISSING,
    READY, UNCONFIGURED, AnalyzerSpec, parse_markdownlint_output,
    parse_pyright_json, parse_ruff_json, resolve, run, ruff_severity, run_all,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "headless_analyzers")


def fixture(name: str, project_root: str) -> str:
    """A captured fixture with its `<PROJECT>` placeholder resolved.

    **The root is JSON-escaped before substitution, and it has to be.** These
    fixtures are JSON documents, so a Windows root dropped in raw turns
    `C:\\Users\\...` into a string full of invalid escapes and the payload stops
    parsing — which reads as "the parser is broken" rather than "the test
    harness built bad input". `json.dumps` produces the exact escaping the
    format wants; slicing off its quotes leaves the inner form.

    Same class of trap as `quality_checks`'s `<PROJECT_ESC>`, which exists
    because one line of a compileall block escapes its path and the next does
    not.
    """
    escaped = json.dumps(project_root)[1:-1]
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read().replace("<PROJECT>", escaped)


# ── the table's shape (project rule D2) ──────────────────────────────────────

class TestTheTable:
    def test_every_row_has_a_parser_and_probe_names(self):
        for spec in ANALYZERS:
            assert callable(spec.parse), spec.key
            assert spec.probe_names, spec.key
            assert spec.config_key, spec.key

    def test_no_row_carries_a_vendor_named_field(self):
        """A field named `is_ruff` re-creates the cascade the table replaces.

        Checks the dataclass's *fields*, not its values: the rule is about the
        shape of the spec, which is the thing a new analyzer would be tempted
        to bend.
        """
        names = {f.name for f in dataclasses.fields(AnalyzerSpec)}
        vendors = {spec.key for spec in ANALYZERS} | {
            "ruff", "pyright", "markdownlint", "pyflakes"}
        offenders = [n for n in names
                     if any(v in n.lower() for v in vendors)]
        assert offenders == [], (
            "AnalyzerSpec fields name a vendor: %s. Describe the capability "
            "instead (probe_names, argv, parse)." % offenders)

    def test_probe_order_is_per_analyzer_not_per_platform(self):
        """One hard-coded order would be wrong for one of these rows.

        ruff is a native binary, so `.exe` leads. pyright and markdownlint are
        npm shims, where the BARE name is the one that fails: `CreateProcess`
        appends only `.exe` and ignores PATHEXT, so `pyright` raises WinError 2
        from Python while working fine in a shell. Both were confirmed on this
        machine at `%APPDATA%\\npm\\pyright.cmd`.
        """
        assert BY_KEY["ruff"].probe_names[0] == "ruff.exe"
        assert BY_KEY["pyright"].probe_names[0] == "pyright.cmd"
        assert BY_KEY["markdownlint"].probe_names[0].endswith(".cmd")

    def test_the_stream_is_declared_per_row(self):
        """The measured one, and silent when wrong.

        markdownlint writes findings to stderr and a banner to stdout. A runner
        that reads stdout for every tool parses the banner, finds nothing, and
        reports markdown clean while the tool says `5 issues in 1 file`.
        """
        assert BY_KEY["ruff"].stream == "stdout"
        assert BY_KEY["pyright"].stream == "stdout"
        assert BY_KEY["markdownlint"].stream == "stderr"

    def test_targets_are_declared_per_row(self):
        """markdownlint will not walk a directory; it wants a glob."""
        assert BY_KEY["ruff"].default_targets == (".",)
        assert BY_KEY["markdownlint"].default_targets == ("**/*.md",)

    def test_a_spec_without_a_callable_parser_is_refused(self):
        with pytest.raises(ValueError):
            AnalyzerSpec(key="x", label="x", config_key="x_exe",
                         probe_names=("x",), argv=(), parse="not callable")

    def test_a_spec_without_probe_names_is_refused(self):
        with pytest.raises(ValueError):
            AnalyzerSpec(key="x", label="x", config_key="x_exe",
                         probe_names=(), argv=(), parse=parse_ruff_json)


# ── the severity policy, on its own ──────────────────────────────────────────

class TestRuffSeverityPolicy:
    """Tested without the parser. This is a policy, not a parsing detail."""

    def test_a_parse_failure_is_an_error(self):
        # Measured: ruff reports this as `invalid-syntax`, NOT as an E9 code.
        # A policy written from memory looks for E9 and misses every one.
        assert ruff_severity("invalid-syntax") == "error"

    def test_a_row_with_no_code_is_an_error(self):
        """Some ruff versions emit a syntax error with no code at all.

        Defaulting an unknown row to `warning` would file exactly those under
        the colour that says "the file still compiles".
        """
        assert ruff_severity(None) == "error"

    def test_the_e9_family_is_an_error(self):
        assert ruff_severity("E902") == "error"

    def test_a_non_e9_pycodestyle_rule_is_only_a_warning(self):
        """The boundary is at E9, not at E.

        Without this the test only ever proves the obvious `F401` case and
        never exercises where the policy actually draws its line — `E701` is
        pycodestyle, parse-adjacent, and still just an opinion about a file
        that compiles perfectly well.
        """
        assert ruff_severity("E701") == "warning"
        assert ruff_severity("E501") == "warning"

    def test_an_ordinary_lint_rule_is_a_warning(self):
        assert ruff_severity("F401") == "warning"

    def test_the_policy_ignores_ruffs_own_severity_field(self):
        """The measurement that justifies having a policy at all.

        The fixture is a real run: 28 distinct codes, and **every one of them
        says `"severity": "error"`** — including `S110` and `BLE001`, which
        this codebase does deliberately. Forwarding that field would paint
        every unused import in the colour reserved for code that does not
        compile.
        """
        rows = json.load(open(os.path.join(FIXTURES, "ruff_many_codes.json"),
                              encoding="utf-8"))
        assert {r["severity"] for r in rows} == {"error"}, (
            "fixture no longer shows the uniform-severity behaviour this "
            "policy exists to work around; re-check before relaxing it")

        parsed = parse_ruff_json(json.dumps(rows), "C:/proj")
        assert parsed, "fixture produced no findings"
        # None of those 28 codes is a syntax failure, so the Manager calls them
        # all warnings even though ruff called them all errors.
        assert {f.severity for f in parsed} == {"warning"}


# ── the parser ───────────────────────────────────────────────────────────────

class TestParseRuffJson:
    def test_the_captured_pair(self, tmp_path):
        root = str(tmp_path)
        found = parse_ruff_json(
            fixture("ruff_unused_and_syntax.json", root), root)
        assert len(found) == 2
        by_rule = {f.rule: f for f in found}
        assert by_rule["ruff/invalid-syntax"].severity == "error"
        assert by_rule["ruff/F401"].severity == "warning"
        assert by_rule["ruff/F401"].message == "`os` imported but unused"

    def test_mixed_separators_normalise_to_a_relative_forward_slash_path(
            self, tmp_path):
        """The captured filename is `<PROJECT>/src\\unused.py`.

        Forward from the argument, back for the leaf, in one path. That is the
        format rather than a typo, and a squiggle lands on the wrong file if it
        is not normalised.
        """
        root = str(tmp_path)
        found = parse_ruff_json(
            fixture("ruff_unused_and_syntax.json", root), root)
        assert sorted(f.file for f in found) == [
            "src/syntaxerr.py", "src/unused.py"]

    def test_coordinates_stay_one_based_and_carry_a_range(self, tmp_path):
        root = str(tmp_path)
        found = parse_ruff_json(
            fixture("ruff_unused_and_syntax.json", root), root)
        unused = next(f for f in found if f.rule == "ruff/F401")
        assert (unused.line, unused.column) == (1, 8)
        assert (unused.end_line, unused.end_column) == (1, 10)

    def test_a_clean_run_is_an_empty_list(self):
        assert parse_ruff_json("[]", "C:/proj") == []

    def test_output_that_is_not_an_array_raises(self):
        """A failed run, not zero findings. The caller turns this into FAILED."""
        with pytest.raises(ValueError):
            parse_ruff_json('{"error": "boom"}', "C:/proj")
        with pytest.raises(ValueError):
            parse_ruff_json("not json at all", "C:/proj")

    def test_one_unusable_row_does_not_discard_the_rest(self):
        payload = json.dumps([
            {"nonsense": True},
            {"filename": "C:/proj/a.py", "location": {"row": 3, "column": 2},
             "code": "F401", "message": "unused"},
        ])
        found = parse_ruff_json(payload, "C:/proj")
        assert len(found) == 1
        assert found[0].line == 3

    def test_a_row_with_no_code_still_produces_a_finding(self):
        payload = json.dumps([
            {"filename": "C:/proj/a.py", "location": {"row": 1, "column": 1},
             "code": None, "message": "unexpected EOF"},
        ])
        found = parse_ruff_json(payload, "C:/proj")
        assert len(found) == 1
        assert found[0].severity == "error"
        assert found[0].rule == "ruff/invalid-syntax"


# ── availability: four states, and none of them is "passed" ──────────────────

class TestParsePyrightJson:
    def test_coordinates_are_converted_from_zero_based(self, tmp_path):
        """The load-bearing one.

        pyright reports `"line": 6` for line **7**. The envelope is 1-based
        everywhere in Python, and the single conversion to VS Code's 0-based
        `Position` happens once in TypeScript — so forwarding pyright's raw
        range puts every squiggle one line above the code it is about, which
        reads as an editor off-by-one rather than as a parser bug.
        """
        root = str(tmp_path)
        found = parse_pyright_json(fixture("pyright_types.json", root), root)
        optional = next(f for f in found
                        if f.rule == "pyright/reportOptionalMemberAccess")
        # Captured as line 6 / character 15; the source line is 7.
        assert (optional.line, optional.column) == (7, 16)
        assert (optional.end_line, optional.end_column) == (7, 21)
        assert min(f.line for f in found) >= 1

    def test_severity_is_forwarded_unlike_ruffs(self, tmp_path):
        """pyright's severity carries a real decision, so it is trusted.

        The contrast with `ruff_severity` is the point: one field varies with
        the project's own configuration, the other said `error` 1,812 times.
        """
        root = str(tmp_path)
        found = parse_pyright_json(fixture("pyright_types.json", root), root)
        assert {f.severity for f in found} == {"error"}

    def test_rules_survive_as_codes(self, tmp_path):
        root = str(tmp_path)
        found = parse_pyright_json(fixture("pyright_types.json", root), root)
        assert "pyright/reportOptionalMemberAccess" in {f.rule for f in found}
        assert "pyright/reportArgumentType" in {f.rule for f in found}

    def test_a_clean_run_is_an_empty_list(self, tmp_path):
        root = str(tmp_path)
        assert parse_pyright_json(fixture("pyright_clean.json", root), root) == []

    def test_a_top_level_array_is_refused(self):
        """ruff's shape handed to pyright's parser is a failed run, not zero.

        Silently accepting it would report every project clean.
        """
        with pytest.raises(ValueError):
            parse_pyright_json("[]", "C:/proj")

    def test_a_diagnostic_without_a_rule_still_becomes_a_finding(self):
        payload = json.dumps({"generalDiagnostics": [
            {"file": "C:/proj/a.py", "severity": "error", "message": "boom",
             "range": {"start": {"line": 0, "character": 0},
                       "end": {"line": 0, "character": 3}}}]})
        found = parse_pyright_json(payload, "C:/proj")
        assert len(found) == 1
        assert found[0].rule == "pyright"
        assert found[0].line == 1


class TestParseMarkdownlintOutput:
    def test_both_line_shapes_are_read(self, tmp_path):
        """`file:line` and `file:line:col`, exactly as pyflakes has two."""
        found = parse_markdownlint_output(
            fixture("markdownlint_issues.txt", str(tmp_path)), str(tmp_path))
        assert len(found) == 5
        by_rule = {f.rule: f for f in found}
        # No column in the captured line -> 1, not a guess.
        assert (by_rule["markdownlint/MD001"].line,
                by_rule["markdownlint/MD001"].column) == (3, 1)
        # Column present.
        assert (by_rule["markdownlint/MD009"].line,
                by_rule["markdownlint/MD009"].column) == (7, 10)

    def test_the_banner_on_stdout_parses_to_nothing(self, tmp_path):
        """The measurement behind `AnalyzerSpec.stream`.

        This is what a runner reading the wrong stream would hand the parser.
        It yields zero findings and no error — a clean bill of health taken
        from a version banner. The parser cannot defend against that; the
        `stream` field is what does.
        """
        banner = fixture("markdownlint_banner_stdout.txt", str(tmp_path))
        assert "Summary: 5 issues" in banner, "fixture no longer the banner"
        assert parse_markdownlint_output(banner, str(tmp_path)) == []

    def test_severity_is_uniform_so_the_manager_assigns_it(self, tmp_path):
        """Every captured line says `error`; none of them stops anything."""
        raw = fixture("markdownlint_issues.txt", str(tmp_path))
        assert raw.count(" error ") == 5
        found = parse_markdownlint_output(raw, str(tmp_path))
        assert {f.severity for f in found} == {"warning"}

    def test_a_clean_run_is_an_empty_list(self, tmp_path):
        assert parse_markdownlint_output(
            fixture("markdownlint_clean.txt", str(tmp_path)),
            str(tmp_path)) == []

    def test_the_rule_name_half_stays_in_the_code_or_message(self, tmp_path):
        found = parse_markdownlint_output(
            fixture("markdownlint_issues.txt", str(tmp_path)), str(tmp_path))
        rules = {f.rule for f in found}
        assert "markdownlint/MD040" in rules
        # The descriptive half is not lost, just not in the code.
        assert any("Fenced code blocks" in f.message for f in found)


class TestAvailability:
    def test_nothing_configured_and_nothing_on_path(self, monkeypatch):
        monkeypatch.setattr("helpers.headless_analyzers.shutil.which",
                            lambda name: None)
        state, exe = resolve(BY_KEY["ruff"], "")
        assert (state, exe) == (UNCONFIGURED, "")

    def test_a_configured_path_that_is_not_there_is_missing_not_unconfigured(
            self, tmp_path):
        """The user said where it lives and it is not there.

        Falling back to PATH here would run a *different* binary than the one
        that was named, and report success for it.
        """
        ghost = str(tmp_path / "nope" / "ruff.exe")
        state, exe = resolve(BY_KEY["ruff"], ghost)
        assert state == MISSING
        assert exe == ghost

    def test_a_configured_path_that_exists_wins_over_path(self, tmp_path,
                                                          monkeypatch):
        real = tmp_path / "ruff.exe"
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr("helpers.headless_analyzers.shutil.which",
                            lambda name: "C:/somewhere/else/ruff.exe")
        state, exe = resolve(BY_KEY["ruff"], str(real))
        assert (state, exe) == (READY, str(real))

    def test_path_is_used_when_nothing_is_configured(self, monkeypatch):
        monkeypatch.setattr("helpers.headless_analyzers.shutil.which",
                            lambda name: "C:/bin/ruff.exe"
                            if name == "ruff.exe" else None)
        assert resolve(BY_KEY["ruff"], "") == (READY, "C:/bin/ruff.exe")

    def test_every_state_has_display_text(self):
        assert set(AVAILABILITY_TEXT) == set(AVAILABILITY)


class TestRunNeverReportsAMissingToolAsClean:
    """The one-liner this module exists for.

    `MISSING`, `UNCONFIGURED` and `FAILED` must each be distinguishable from a
    clean run, in the result AND in the text a person reads.
    """

    def test_a_missing_analyzer_is_not_ok_and_does_not_say_passed(self,
                                                                 tmp_path):
        result = run(BY_KEY["ruff"], str(tmp_path),
                     configured=str(tmp_path / "absent.exe"))
        assert result.availability == MISSING
        assert result.ok is False
        assert result.findings == []
        assert "pass" not in result.summary.lower()

    def test_an_unconfigured_analyzer_is_not_ok_and_did_not_run(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.headless_analyzers.shutil.which",
                            lambda name: None)
        result = run(BY_KEY["ruff"], str(tmp_path))
        assert result.availability == UNCONFIGURED
        assert result.ok is False
        assert result.ran is False
        assert "pass" not in result.summary.lower()

    def test_a_nonzero_exit_outside_the_allowed_set_is_failed(
            self, tmp_path, monkeypatch):
        exe = tmp_path / "ruff.exe"
        exe.write_text("", encoding="utf-8")

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="bad config")

        monkeypatch.setattr("helpers.headless_analyzers.subprocess.run",
                            fake_run)
        result = run(BY_KEY["ruff"], str(tmp_path), configured=str(exe))
        assert result.availability == FAILED
        assert result.ok is False
        assert result.ran is True
        assert "bad config" in result.detail

    def test_unparseable_output_is_failed_rather_than_zero_findings(
            self, tmp_path, monkeypatch):
        """The most dangerous one: exit 0 and output we cannot read.

        Treating that as an empty finding list is a clean bill of health the
        tool never gave.
        """
        exe = tmp_path / "ruff.exe"
        exe.write_text("", encoding="utf-8")

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="<html>proxy error</html>",
                stderr="")

        monkeypatch.setattr("helpers.headless_analyzers.subprocess.run",
                            fake_run)
        result = run(BY_KEY["ruff"], str(tmp_path), configured=str(exe))
        assert result.availability == FAILED
        assert result.findings == []
        assert "pass" not in result.summary.lower()

    def test_a_spawn_failure_is_failed_not_an_exception(self, tmp_path,
                                                        monkeypatch):
        exe = tmp_path / "ruff.exe"
        exe.write_text("", encoding="utf-8")

        def boom(*_a, **_k):
            raise OSError("WinError 2")

        monkeypatch.setattr("helpers.headless_analyzers.subprocess.run", boom)
        result = run(BY_KEY["ruff"], str(tmp_path), configured=str(exe))
        assert result.availability == FAILED
        assert "WinError 2" in result.detail

    def test_a_clean_run_reports_zero_and_is_ok(self, tmp_path, monkeypatch):
        exe = tmp_path / "ruff.exe"
        exe.write_text("", encoding="utf-8")

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr="")

        monkeypatch.setattr("helpers.headless_analyzers.subprocess.run",
                            fake_run)
        result = run(BY_KEY["ruff"], str(tmp_path), configured=str(exe))
        assert (result.availability, result.ok, result.rows) == (READY, True, 0)
        assert result.summary == "passed (0 findings)"

    def test_findings_do_not_make_the_run_not_ok(self, tmp_path, monkeypatch):
        """`ok` means "the tool ran", not "the tool was happy"."""
        exe = tmp_path / "ruff.exe"
        exe.write_text("", encoding="utf-8")
        payload = fixture("ruff_unused_and_syntax.json", str(tmp_path))

        def fake_run(*_a, **_k):
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=payload, stderr="")

        monkeypatch.setattr("helpers.headless_analyzers.subprocess.run",
                            fake_run)
        result = run(BY_KEY["ruff"], str(tmp_path), configured=str(exe))
        assert result.ok is True
        assert result.rows == 2

    def test_run_all_returns_one_result_per_row_in_table_order(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr("helpers.headless_analyzers.shutil.which",
                            lambda name: None)
        results = run_all(str(tmp_path))
        assert [r.key for r in results] == [a.key for a in ANALYZERS]
