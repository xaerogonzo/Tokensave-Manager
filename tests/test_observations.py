"""Guards on `helpers/observations.py`.

Four of these are the whole reason the module exists, and each one is a way a
report can be confidently wrong:

* an unread source rendering as zero findings;
* `analyzed_files` being filled in from something that is not it;
* two sources disagreeing and one of them silently winning;
* two populations being added together.
"""

import inspect
import json
import os

import pytest

from helpers import observations as obs
from helpers.observations import (
    MAX_OBSERVATIONS, OBSERVATIONS_SCHEMA_VERSION, READ_ABSENT, READ_OK,
    READ_UNKNOWN, SOURCE_EDITOR, SOURCE_HEADLESS, Coverage, MergedRow,
    Observation, SnapshotError, SourceReport, compute_report,
    findings_to_report, merge_rows, read_snapshot, schema_problem,
    snapshot_path,
)
from helpers.findings import Finding


def envelope(rows=None, **over):
    doc = {
        "observations_schema_version": OBSERVATIONS_SCHEMA_VERSION,
        "captured_at": 1789000000,
        "editor_label": "VS Code",
        "coverage": {"diagnostic_mode": "openFilesOnly"},
        "observations": rows if rows is not None else [],
    }
    doc.update(over)
    return doc


def row(file="src/a.py", line=7, column=16, rule="pyright/reportOptionalMemberAccess",
        severity="error", message="\"upper\" is not a known attribute of \"None\"",
        producer="Pylance"):
    return {"file": file, "line": line, "column": column, "end_line": line,
            "end_column": column + 5, "rule": rule, "severity": severity,
            "message": message, "producer": producer}


def write_snapshot(project_root, doc):
    path = snapshot_path(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


# ── unknown is never zero ────────────────────────────────────────────────────

class TestUnknownIsNeverZero:
    def test_a_missing_snapshot_is_absent_not_ok(self, tmp_path):
        report = read_snapshot(str(tmp_path))
        assert report.read_status == READ_ABSENT
        assert report.known is False
        assert report.coverage.analyzed_files is None

    def test_absent_and_unknown_are_different_states(self, tmp_path):
        """"Nobody captured one" and "we could not tell" want different words.

        Collapsing them would tell a user to go capture a snapshot that is
        already there and simply unreadable.
        """
        absent = read_snapshot(str(tmp_path))
        write_snapshot(str(tmp_path), {"nonsense": True})
        unreadable = read_snapshot(str(tmp_path))
        assert absent.read_status == READ_ABSENT
        assert unreadable.read_status == READ_UNKNOWN
        assert absent.read_status != unreadable.read_status

    def test_unparseable_json_is_unknown_not_empty(self, tmp_path):
        path = snapshot_path(str(tmp_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        report = read_snapshot(str(tmp_path))
        assert report.read_status == READ_UNKNOWN
        assert report.rows == ()
        assert report.known is False

    def test_an_oversized_snapshot_is_unknown(self, tmp_path, monkeypatch):
        write_snapshot(str(tmp_path), envelope())
        monkeypatch.setattr(obs, "MAX_SNAPSHOT_BYTES", 1)
        report = read_snapshot(str(tmp_path))
        assert report.read_status == READ_UNKNOWN
        assert "cap" in report.detail

    def test_a_genuinely_empty_snapshot_is_ok_and_zero(self, tmp_path):
        """The other half. An editor with nothing to say is a real answer."""
        write_snapshot(str(tmp_path), envelope([]))
        report = read_snapshot(str(tmp_path))
        assert report.read_status == READ_OK
        assert report.known is True
        assert report.coverage.diagnostic_entries == 0


# ── the number that does not exist ───────────────────────────────────────────

class TestAnalyzedFilesStaysUnknown:
    def test_it_is_unknown_when_the_producer_does_not_claim_it(self, tmp_path):
        """`getDiagnostics()` cannot enumerate what was analysed.

        A clean file Pylance examined and a file it never opened are
        indistinguishable in that return value, so the honest answer is
        unknown — and it must not quietly become a number.
        """
        write_snapshot(str(tmp_path), envelope([row(), row(file="src/b.py")]))
        report = read_snapshot(str(tmp_path))
        assert report.coverage.analyzed_files is None
        assert report.coverage.analyzed_files_text == "analyzed files: unknown"

    def test_it_is_not_defaulted_from_files_with_diagnostics(self, tmp_path):
        """The specific substitution that would look completely reasonable."""
        write_snapshot(str(tmp_path),
                       envelope([row(), row(file="src/b.py"), row(file="src/c.py")]))
        report = read_snapshot(str(tmp_path))
        assert report.coverage.files_with_diagnostics == 3
        assert report.coverage.analyzed_files is None

    def test_a_producer_that_does_claim_it_is_believed(self, tmp_path):
        doc = envelope([row()])
        doc["coverage"]["analyzed_files"] = 47
        write_snapshot(str(tmp_path), doc)
        report = read_snapshot(str(tmp_path))
        assert report.coverage.analyzed_files == 47
        assert report.coverage.analyzed_files_text == "analyzed files: 47"

    def test_a_non_integer_claim_is_discarded_rather_than_coerced(self, tmp_path):
        doc = envelope([row()])
        doc["coverage"]["analyzed_files"] = "lots"
        write_snapshot(str(tmp_path), doc)
        assert read_snapshot(str(tmp_path)).coverage.analyzed_files is None

    def test_diagnostic_mode_is_carried_but_is_not_coverage(self, tmp_path):
        """It describes the configured POLICY, not what was analysed.

        Both are on the report; neither stands in for the other.
        """
        doc = envelope([row()])
        doc["coverage"]["diagnostic_mode"] = "workspace"
        write_snapshot(str(tmp_path), doc)
        report = read_snapshot(str(tmp_path))
        assert report.coverage.diagnostic_mode == "workspace"
        assert report.coverage.analyzed_files is None


# ── the merge contract ───────────────────────────────────────────────────────

def _two_disagreeing_reports():
    """Same identity; different severity AND different wording."""
    editor = SourceReport(
        key=SOURCE_EDITOR, label="VS Code",
        rows=(Observation(file="src/a.py", line=7, column=16, rule="F401",
                          severity="error", message="import is never used",
                          producer="Pylance"),
              Observation(file="src/z.py", line=1, column=1, rule="F821",
                          severity="error", message="undefined name",
                          producer="Pylance")),
        coverage=Coverage(diagnostic_entries=2, files_with_diagnostics=2))
    headless = SourceReport(
        key=SOURCE_HEADLESS, label="ruff",
        rows=(Observation(file="src/a.py", line=7, column=16, rule="F401",
                          severity="warning", message="`os` imported but unused",
                          producer="ruff"),),
        coverage=Coverage(diagnostic_entries=1, files_with_diagnostics=1))
    return editor, headless


class TestMergeKeepsBothSources:
    def test_one_row_two_sources(self):
        editor, headless = _two_disagreeing_reports()
        merged = merge_rows([editor, headless])
        shared = [m for m in merged if m.rule == "F401"]
        assert len(shared) == 1
        assert shared[0].source_keys == (SOURCE_EDITOR, SOURCE_HEADLESS)

    def test_each_source_keeps_its_own_severity_and_message(self):
        """No synthesized severity, no precedence, no merged message.

        Without this, someone writes `row.severity = max(severities)` and the
        disagreement — which is the interesting part — disappears.
        """
        editor, headless = _two_disagreeing_reports()
        shared = next(m for m in merge_rows([editor, headless])
                      if m.rule == "F401")
        by_source = dict(shared.sources)
        assert by_source[SOURCE_EDITOR][0].severity == "error"
        assert by_source[SOURCE_HEADLESS][0].severity == "warning"
        assert by_source[SOURCE_EDITOR][0].message == "import is never used"
        assert by_source[SOURCE_HEADLESS][0].message == "`os` imported but unused"

    def test_one_source_reporting_a_position_twice_keeps_both(self):
        """Measured on this repository, and it broke the first implementation.

        `(file, line, column, code)` is NOT unique within a source. ruff emits
        `UP035` twice on one import line — `typing.Tuple` and `typing.List` are
        two deprecations at one position — and pyright emits
        `reportAttributeAccessIssue` once per union member, ten deep. Keeping
        one observation per source discarded 557 real findings on the first
        live run that counted them.
        """
        twice = SourceReport(
            key=SOURCE_HEADLESS, label="ruff",
            rows=(Observation(file="src/a.py", line=3, column=1, rule="UP035",
                              severity="warning", producer="ruff",
                              message="`typing.Tuple` is deprecated"),
                  Observation(file="src/a.py", line=3, column=1, rule="UP035",
                              severity="warning", producer="ruff",
                              message="`typing.List` is deprecated")))
        merged = merge_rows([twice])
        assert len(merged) == 1
        assert len(merged[0].observations) == 2, "a finding was discarded"
        assert {o.message for o in merged[0].observations} == {
            "`typing.Tuple` is deprecated", "`typing.List` is deprecated"}

    def test_one_source_twice_is_not_corroboration(self):
        """`shared` means "more than one SOURCE saw this", never "two rows".

        The earlier version conflated them, and a live run reported 321 rows
        "seen by both" on a project with no editor snapshot at all.
        """
        twice = SourceReport(
            key=SOURCE_HEADLESS, label="ruff",
            rows=(Observation(file="src/a.py", line=3, column=1, rule="UP035",
                              severity="warning", message="one"),
                  Observation(file="src/a.py", line=3, column=1, rule="UP035",
                              severity="warning", message="two")))
        row = merge_rows([twice])[0]
        assert row.shared is False
        assert row.source_keys == (SOURCE_HEADLESS,)
        assert len(row.sources) == 1

    def test_a_merged_row_exposes_no_single_severity(self):
        """The shape refuses the question rather than answering it wrongly."""
        assert not hasattr(MergedRow, "severity")
        assert not hasattr(MergedRow, "message")

    def test_disagreement_is_reported_not_resolved(self):
        editor, headless = _two_disagreeing_reports()
        shared = next(m for m in merge_rows([editor, headless])
                      if m.rule == "F401")
        assert shared.agreed is False

    def test_a_row_only_one_source_saw_survives(self):
        editor, headless = _two_disagreeing_reports()
        merged = merge_rows([editor, headless])
        lone = next(m for m in merged if m.rule == "F821")
        assert lone.source_keys == (SOURCE_EDITOR,)

    def test_identity_ignores_message_and_severity(self):
        """Two tools wording the same defect differently are one defect."""
        a = Observation(file="src/a.py", line=1, column=1, rule="F401",
                        severity="error", message="one way")
        b = Observation(file="src/a.py", line=1, column=1, rule="F401",
                        severity="hint", message="another way")
        assert a.identity == b.identity

    def test_merging_does_not_mutate_either_source_population(self):
        editor, headless = _two_disagreeing_reports()
        before = (editor.coverage, headless.coverage)
        merge_rows([editor, headless])
        assert (editor.coverage, headless.coverage) == before
        assert editor.coverage.diagnostic_entries == 2
        assert headless.coverage.diagnostic_entries == 1

    def test_order_is_stable(self):
        editor, headless = _two_disagreeing_reports()
        first = [(m.file, m.line, m.rule) for m in merge_rows([editor, headless])]
        second = [(m.file, m.line, m.rule) for m in merge_rows([editor, headless])]
        assert first == second


# ── nothing sums ─────────────────────────────────────────────────────────────

class TestNoCombinedAggregate:
    """The invariant is behavioural: no public API produces a combined count.

    Deliberately NOT a grep for the word "total" — that is the degeneracy
    `gotchas/verifying-a-gui.md` warns about, checking prose instead of a
    contract.
    """

    def test_no_public_callable_taking_reports_returns_a_number(self):
        editor, headless = _two_disagreeing_reports()
        checked = 0
        for name, fn in vars(obs).items():
            if name.startswith("_") or not callable(fn):
                continue
            if not inspect.isfunction(fn):
                continue
            params = list(inspect.signature(fn).parameters)
            if not params or params[0] not in ("reports", "findings"):
                continue
            checked += 1
            result = fn([editor, headless]) if params[0] == "reports" else fn([])
            assert not isinstance(result, (int, float)), (
                "%s returns a scalar over multiple sources" % name)
        # A guard that examined nothing reports the same green as one that
        # examined everything. State the population.
        assert checked >= 1, "the guard examined no callables; it proves nothing"

    def test_source_report_cannot_be_added(self):
        editor, headless = _two_disagreeing_reports()
        with pytest.raises(TypeError):
            editor + headless

    def test_source_report_exposes_no_cross_source_accessor(self):
        editor, _ = _two_disagreeing_reports()
        names = [n for n in dir(editor) if not n.startswith("_")]
        for name in names:
            value = getattr(editor, name)
            assert not callable(value) or name in ("count", "index"), (
                "SourceReport.%s takes arguments; a cross-source accessor "
                "would live exactly here" % name)


# ── the schema window ────────────────────────────────────────────────────────

class TestSchemaWindow:
    def test_an_absent_version_is_refused_as_not_an_envelope(self):
        """The `undefined > 1` trap `cli.ts` already had to be fixed for.

        A missing version must not sail through a `version > SUPPORTED` test
        and be read as version 1.
        """
        problem = schema_problem({"observations": []})
        assert problem is not None
        assert "not an observations envelope" in problem

    def test_a_boolean_is_not_a_version(self):
        assert schema_problem({"observations_schema_version": True}) is not None

    def test_the_current_version_is_accepted(self):
        assert schema_problem(
            {"observations_schema_version": OBSERVATIONS_SCHEMA_VERSION}) is None

    def test_a_newer_version_is_refused_and_says_to_update(self):
        problem = schema_problem({"observations_schema_version": 99})
        assert problem is not None
        assert "Update the Manager" in problem

    def test_a_newer_version_is_never_partially_parsed(self, tmp_path):
        """Refused whole. "Close enough" is how a renamed field gets misread."""
        doc = envelope([row()])
        doc["observations_schema_version"] = 99
        write_snapshot(str(tmp_path), doc)
        report = read_snapshot(str(tmp_path))
        assert report.read_status == READ_UNKNOWN
        assert report.rows == ()

    def test_an_older_unknown_version_is_refused_differently(self):
        problem = schema_problem({"observations_schema_version": 0})
        assert problem is not None
        assert "predates" in problem

    def test_a_version_problem_and_malformed_json_read_differently(self,
                                                                   tmp_path):
        """"Your extension is out of date" and "your file is broken" have
        different remedies, and the report has to keep them apart."""
        doc = envelope([row()])
        doc["observations_schema_version"] = 99
        write_snapshot(str(tmp_path), doc)
        version_detail = read_snapshot(str(tmp_path)).detail

        with open(snapshot_path(str(tmp_path)), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        broken_detail = read_snapshot(str(tmp_path)).detail

        assert version_detail != broken_detail
        assert "Update the Manager" in version_detail
        assert "could not read" in broken_detail


# ── row validation ───────────────────────────────────────────────────────────

class TestRowValidation:
    def test_an_absolute_path_is_refused(self):
        with pytest.raises(SnapshotError):
            compute_report(envelope([row(file="C:/elsewhere/a.py")]))

    def test_a_path_that_climbs_out_is_refused(self):
        with pytest.raises(SnapshotError):
            compute_report(envelope([row(file="../../secrets.py")]))

    def test_a_zero_position_is_refused_as_not_one_based(self):
        """A 0 means the producer forwarded VS Code's own 0-based Position.

        Accepting it puts every row one line above the code it describes, which
        reads as an editor off-by-one rather than as a contract violation.
        """
        with pytest.raises(SnapshotError) as caught:
            compute_report(envelope([row(line=0)]))
        assert "1-based" in str(caught.value)

    def test_an_unknown_severity_is_refused(self):
        with pytest.raises(SnapshotError):
            compute_report(envelope([row(severity="catastrophe")]))

    def test_too_many_observations_are_refused(self):
        doc = envelope([])
        doc["observations"] = [row()] * (MAX_OBSERVATIONS + 1)
        with pytest.raises(SnapshotError):
            compute_report(doc)

    def test_backslash_paths_are_normalised(self):
        report = compute_report(envelope([row(file="src\\sub\\a.py")]))
        assert report.rows[0].file == "src/sub/a.py"


# ── the headless adapter ─────────────────────────────────────────────────────

class TestFindingsToReport:
    def test_a_findings_list_becomes_a_source_with_a_real_scope(self):
        report = findings_to_report(
            [Finding(file="src/a.py", line=3, column=2, message="m",
                     rule="ruff/F401")],
            scope="whole project")
        assert report.key == SOURCE_HEADLESS
        assert report.coverage.scope == "whole project"
        assert report.coverage.diagnostic_entries == 1
        assert report.rows[0].producer == "ruff"

    def test_the_producer_prefix_is_split_off_the_identity(self):
        """The comparison this whole feature exists to make.

        Headless pyright emits `pyright/reportOptionalMemberAccess`; Pylance in
        the editor emits the bare `reportOptionalMemberAccess` with
        `source: "Pylance"`. Leaving the prefix on the rule would file them as
        two different defects and the merge would never fire once.
        """
        headless = findings_to_report(
            [Finding(file="src/a.py", line=7, column=16, message="m",
                     rule="pyright/reportOptionalMemberAccess")])
        editor = SourceReport(
            key=SOURCE_EDITOR, label="VS Code",
            rows=(Observation(file="src/a.py", line=7, column=16,
                              rule="reportOptionalMemberAccess",
                              producer="Pylance", severity="error"),))
        assert headless.rows[0].rule == "reportOptionalMemberAccess"
        assert headless.rows[0].producer == "pyright"
        merged = merge_rows([headless, editor])
        assert len(merged) == 1, "the two sources did not recognise each other"
        assert merged[0].source_keys == (SOURCE_HEADLESS, SOURCE_EDITOR)

    def test_a_rule_with_no_code_keeps_an_empty_identity(self):
        """pyflakes prints no rule codes, so there is nothing to match on."""
        report = findings_to_report(
            [Finding(file="src/a.py", line=1, rule="pyflakes")])
        assert report.rows[0].producer == "pyflakes"
        assert report.rows[0].rule == ""

    def test_the_headless_side_also_leaves_analyzed_files_unknown(self):
        """A linter reports what it flagged, not what it opened."""
        report = findings_to_report([Finding(file="src/a.py", line=1)])
        assert report.coverage.analyzed_files is None


# ── the load-bearing integration case ────────────────────────────────────────

class TestTwoPopulationsNeverBecomeOne:
    """A partial editor snapshot beside a whole-project run.

    This is the case the whole design exists for. The editor covered three
    open files; the headless run covered the tree. Merging their counts would
    produce a number that looks complete and is arithmetic on incomparable
    populations — the `verification_oracle` population failure in a new place.
    """

    def _fixtures(self):
        editor_rows = tuple(
            Observation(file="src/open%d.py" % n, line=n, column=1,
                        rule="reportOptionalMemberAccess", severity="error",
                        message="editor", producer="Pylance")
            for n in (1, 2, 3))
        editor = SourceReport(
            key=SOURCE_EDITOR, label="VS Code", read_status=READ_OK,
            rows=editor_rows, as_of=1789000000,
            coverage=Coverage(
                diagnostic_entries=3, files_with_diagnostics=3,
                analyzed_files=None,             # the editor cannot say
                diagnostic_mode="openFilesOnly",
                scope="files the editor has diagnostics for"))
        headless = findings_to_report(
            [Finding(file="src/whole%d.py" % n, line=n, rule="ruff/F401")
             for n in range(1, 21)],
            scope="whole project")
        return editor, headless

    def test_the_two_populations_stay_separate(self):
        editor, headless = self._fixtures()
        assert editor.coverage.diagnostic_entries == 3
        assert headless.coverage.diagnostic_entries == 20
        assert editor.coverage.scope != headless.coverage.scope
        # The editor cannot state an analyzed population; the headless half's
        # scope IS its population. That asymmetry is why they never merge.
        assert editor.coverage.analyzed_files is None
        assert headless.coverage.scope == "whole project"

    def test_merging_produces_rows_not_a_count(self):
        editor, headless = self._fixtures()
        merged = merge_rows([editor, headless])
        assert isinstance(merged, list)
        assert all(isinstance(m, MergedRow) for m in merged)
        # 23 disjoint identities -> 23 rows. That is a row count, not a
        # combined population: nothing here claims 23 files were examined.
        assert len(merged) == 23
        assert all(len(m.sources) == 1 for m in merged)

    def test_neither_coverage_moves_when_they_are_merged(self):
        editor, headless = self._fixtures()
        before = (editor.coverage, headless.coverage)
        merge_rows([editor, headless])
        assert (editor.coverage, headless.coverage) == before

    def test_a_reader_can_always_tell_which_source_said_what(self):
        editor, headless = self._fixtures()
        for merged in merge_rows([editor, headless]):
            assert merged.source_keys, "a row with no attributed source"
            for key, _obs in merged.sources:
                assert key in (SOURCE_EDITOR, SOURCE_HEADLESS)
