"""tests/test_pytest_report.py — per-test results, and refusing to guess.

Fixture-driven throughout: the inputs are real pytest output captured from a
project built to carry every awkward case at once. See
`tests/fixtures/pytest_report/README.md` for what each case proves and why the
JUnit XML turned out not to be an identity source.

The assertions divide into two kinds, and the second kind is the point of the
module. Parsing tests check that a known format is read correctly. Attribution
tests check that an *unknowable* answer is reported as unknowable — because the
alternative is a Test Explorer that shows a green tick on a test whose result
belongs to a different one.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from helpers.pytest_report import (  # noqa: E402
    ERROR, FAILED, PASSED, SKIPPED, Attribution, TestOutcome, junit_key,
    parse_junit_xml, parse_run, parse_verbose_lines, resolve_identities,
    split_nodeid, summarise)

_FIXTURES = _ROOT / "tests" / "fixtures" / "pytest_report"


@pytest.fixture(scope="module")
def verbose_text() -> str:
    return (_FIXTURES / "verbose_run.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def junit_text() -> str:
    return (_FIXTURES / "report_xunit2.xml").read_text(encoding="utf-8")


# ── split_nodeid: the separator that is not a separator ─────────────────────


def test_module_level_nodeid_has_no_class():
    assert split_nodeid("tests/test_alpha.py::test_ordinary") == (
        "tests/test_alpha.py", (), "test_ordinary")


def test_class_method_nodeid_keeps_the_class():
    assert split_nodeid("tests/test_alpha.py::TestOne::test_method") == (
        "tests/test_alpha.py", ("TestOne",), "test_method")


def test_nested_classes_accumulate_in_order():
    """pytest addresses nested classes by chaining, outermost first."""
    assert split_nodeid(
        "tests/test_alpha.py::TestOuter::TestInner::test_nested") == (
        "tests/test_alpha.py", ("TestOuter", "TestInner"), "test_nested")


def test_a_parameter_id_containing_the_separator_is_not_split():
    """The one that breaks `nodeid.split("::")`.

    A `@pytest.mark.parametrize` over `"a::b"` puts the nodeid separator inside
    the parameter id, and a naive split reads `test_x[a` as a class name.
    """
    assert split_nodeid(
        "tests/test_alpha.py::test_param_containing_separator[a::b]") == (
        "tests/test_alpha.py", (), "test_param_containing_separator[a::b]")


def test_a_parameter_id_with_brackets_and_separators_survives_both():
    assert split_nodeid(
        "tests/test_alpha.py::test_x[c[d]::e]") == (
        "tests/test_alpha.py", (), "test_x[c[d]::e]")


def test_a_parametrised_class_method_keeps_class_and_parameter():
    assert split_nodeid("tests/t.py::TestC::test_m[a::b]") == (
        "tests/t.py", ("TestC",), "test_m[a::b]")


def test_something_that_is_not_a_nodeid_comes_back_whole():
    """Odd input yields a usable record, not an exception."""
    assert split_nodeid("not a nodeid") == ("", (), "not a nodeid")


# ── junit_key: the one-way derivation ───────────────────────────────────────


def test_junit_key_dots_the_module_path():
    assert junit_key("tests/test_alpha.py::test_ordinary") == (
        "tests.test_alpha", "test_ordinary")


def test_junit_key_appends_the_class_chain():
    assert junit_key(
        "tests/test_alpha.py::TestOuter::TestInner::test_nested") == (
        "tests.test_alpha.TestOuter.TestInner", "test_nested")


def test_junit_key_normalises_backslashes():
    """A Windows-spelled nodeid must derive the same key as a POSIX one.

    Two spellings of one test deriving two keys is how the same test gets
    enriched twice, or not at all, depending on which side produced the string.
    """
    assert junit_key(r"tests\test_alpha.py::test_ordinary") == junit_key(
        "tests/test_alpha.py::test_ordinary")


# ── parse_verbose_lines ─────────────────────────────────────────────────────


def test_every_collected_test_is_parsed(verbose_text):
    """18 tests ran in the captured fixture; all 18 come back."""
    assert len(parse_verbose_lines(verbose_text)) == 18


def test_all_four_outcome_kinds_are_recognised(verbose_text):
    outcomes = dict(parse_verbose_lines(verbose_text))
    assert outcomes["tests/test_alpha.py::test_ordinary"] == PASSED
    assert outcomes["tests/test_alpha.py::test_fails"] == FAILED
    assert outcomes["tests/test_alpha.py::test_errors"] == ERROR
    assert outcomes["tests/test_alpha.py::test_skipped"] == SKIPPED


def test_a_parameter_id_containing_spaces_is_kept_whole(verbose_text):
    """The nodeid group has to allow spaces, so it cannot be `\\S+`."""
    ids = [nodeid for nodeid, _ in parse_verbose_lines(verbose_text)]
    assert "tests/test_alpha.py::test_parametrised[with space]" in ids


def test_awkward_parameter_ids_all_survive(verbose_text):
    ids = [nodeid for nodeid, _ in parse_verbose_lines(verbose_text)]
    for suffix in ("[plain]", "[with space]", "[sl/ash]", "[br[ack]et]",
                   "[3]", "[None]"):
        assert f"tests/test_alpha.py::test_parametrised{suffix}" in ids, suffix


def test_the_rfE_summary_block_is_not_read_as_progress():
    """`FAILED tests/x.py::y` puts the outcome first and is not a result line.

    Counting it would double-report every failure in a run using `-rfE`, which
    `smoke_runner.run_gate` does.
    """
    text = ("tests/t.py::test_a PASSED\n"
            "FAILED tests/t.py::test_a - AssertionError: PASSED elsewhere\n")
    assert parse_verbose_lines(text) == [("tests/t.py::test_a", PASSED)]


def test_the_error_section_header_is_not_a_result():
    """The ERRORS block prints `file /proj/tests/x.py, line 12` — no `::`."""
    text = "file /proj/tests/test_alpha.py, line 12\n"
    assert parse_verbose_lines(text) == []


def test_skipped_with_a_trailing_reason_still_parses():
    text = "tests/t.py::test_s SKIPPED (deliberate skip)               [ 25%]\n"
    assert parse_verbose_lines(text) == [("tests/t.py::test_s", SKIPPED)]


# ── parse_junit_xml: enrichment only ────────────────────────────────────────


def test_junit_gives_a_duration_for_every_case(junit_text):
    entries = parse_junit_xml(junit_text)
    assert len(entries) == 18
    assert all(e["duration"] is not None for e in entries.values())


def test_junit_carries_the_failure_message(junit_text):
    entry = parse_junit_xml(junit_text)[("tests.test_alpha", "test_fails")]
    assert "AssertionError: deliberate" in entry["message"]


def test_junit_carries_the_skip_reason(junit_text):
    entry = parse_junit_xml(junit_text)[("tests.test_alpha", "test_skipped")]
    assert "deliberate skip" in entry["message"]


def test_junit_distinguishes_two_classes_with_the_same_method_name(junit_text):
    """The clash the dotted classname *does* resolve, unlike file-vs-class."""
    entries = parse_junit_xml(junit_text)
    assert ("tests.test_alpha.TestOne", "test_shared_method_name") in entries
    assert ("tests.test_alpha.TestTwo", "test_shared_method_name") in entries


def test_the_default_junit_family_carries_no_location(junit_text):
    """Measured, and the reason location comes from `test_discovery` instead.

    If a future pytest starts emitting `file`/`line` under `xunit2`, this test
    fails and the decision recorded in the module docstring can be revisited —
    which is the point of asserting an absence.
    """
    assert 'file="' not in junit_text
    assert 'line="' not in junit_text


def test_malformed_xml_degrades_rather_than_raising():
    """A run read from the `-v` lines must not be lost to the optional half."""
    assert parse_junit_xml("<testsuites><not closed") == {}


# ── parse_run: the join ─────────────────────────────────────────────────────


def test_the_join_enriches_every_result(verbose_text, junit_text):
    results = parse_run(verbose_text, junit_text)
    assert len(results) == 18
    assert all(r.duration is not None for r in results)


def test_the_summary_counts_the_captured_run(verbose_text, junit_text):
    counts = summarise(parse_run(verbose_text, junit_text))
    assert counts == {"passed": 15, "failed": 1, "error": 1, "skipped": 1,
                      "xfailed": 0, "xpassed": 0}


def test_every_outcome_key_is_present_even_at_zero(verbose_text):
    """A missing key and a zero are different statements."""
    counts = summarise(parse_run(verbose_text))
    assert counts["xfailed"] == 0 and "xfailed" in counts


def test_results_survive_without_any_xml(verbose_text):
    results = parse_run(verbose_text)
    assert len(results) == 18
    assert all(r.duration is None and r.message == "" for r in results)


def test_two_classes_with_one_method_name_get_their_own_messages(junit_text):
    """The join must not merge them onto one entry."""
    text = ("tests/test_alpha.py::TestOne::test_shared_method_name PASSED\n"
            "tests/test_alpha.py::TestTwo::test_shared_method_name PASSED\n")
    results = parse_run(text, junit_text)
    assert len(results) == 2
    assert results[0].nodeid != results[1].nodeid


def test_an_unknown_outcome_word_is_refused():
    """The closed set is enforced, not documented."""
    with pytest.raises(ValueError):
        TestOutcome(nodeid="tests/t.py::test_a", outcome="probably_fine")


# ── resolve_identities: the part that must not guess ────────────────────────


def test_an_exact_match_attributes_to_itself(verbose_text):
    results = parse_run(verbose_text)
    attributed = resolve_identities(
        results, ["tests/test_alpha.py::test_ordinary"])
    hit = [a for a in attributed if a.requested]
    assert len(hit) == 1
    assert hit[0].outcome.nodeid == "tests/test_alpha.py::test_ordinary"


def test_parametrised_expansions_attribute_to_the_requested_prefix(
        verbose_text):
    """Six runs, one requested id — the whole reason attribution exists."""
    results = parse_run(verbose_text)
    attributed = resolve_identities(
        results, ["tests/test_alpha.py::test_parametrised"])
    hit = [a for a in attributed
           if a.requested == "tests/test_alpha.py::test_parametrised"]
    assert len(hit) == 6
    assert not any(a.ambiguous for a in attributed)


def test_a_result_nobody_asked_for_is_left_unattributed(verbose_text):
    results = parse_run(verbose_text)
    attributed = resolve_identities(
        results, ["tests/test_alpha.py::test_ordinary"])
    loose = [a for a in attributed if not a.requested and not a.ambiguous]
    assert len(loose) == 17


def test_a_prefix_must_be_followed_by_a_bracket_not_just_any_text():
    """`test_a` must not swallow `test_ab`.

    Prefix matching without the `[` boundary attributes every result whose name
    merely starts with the requested one, which is a silent mis-attribution
    between two genuinely different tests.
    """
    results = [TestOutcome(nodeid="tests/t.py::test_ab", outcome=PASSED)]
    attributed = resolve_identities(results, ["tests/t.py::test_a"])
    assert attributed[0].requested == ""
    assert not attributed[0].ambiguous


def test_a_colliding_requested_id_is_reported_ambiguous_not_guessed():
    """Two requested tests sharing a nodeid: attribute to neither.

    This is the shadowing defect `tests/test_no_shadowed_tests.py` prevents, so
    it should not occur — but "should not" is not "cannot", and picking one is
    exactly the confident wrong answer this module is shaped to avoid.
    """
    results = [TestOutcome(nodeid="tests/t.py::test_a[1]", outcome=PASSED)]
    attributed = resolve_identities(results, ["tests/t.py::test_a"] * 2)
    assert attributed[0].ambiguous
    assert attributed[0].requested == ""


def test_an_exact_collision_is_flagged_while_still_attributing():
    """An exact duplicate still names the test, but marks the doubt.

    Different from the prefix case: there is only one id it could be, so the
    attribution is safe; what is unsafe is presenting it as unambiguous.
    """
    results = [TestOutcome(nodeid="tests/t.py::test_a", outcome=PASSED)]
    attributed = resolve_identities(results, ["tests/t.py::test_a"] * 2)
    assert attributed[0].requested == "tests/t.py::test_a"
    assert attributed[0].ambiguous


def test_attribution_never_invents_a_requested_id():
    """Whatever it reports must be something the caller actually asked for."""
    results = [TestOutcome(nodeid="tests/t.py::test_z[1]", outcome=PASSED)]
    requested = ["tests/t.py::test_a", "tests/t.py::test_b"]
    for attributed in resolve_identities(results, requested):
        assert attributed.requested in ("",) + tuple(requested)


def test_the_attribution_record_defaults_to_saying_nothing():
    """An `Attribution` built with only an outcome claims neither."""
    record = Attribution(
        outcome=TestOutcome(nodeid="tests/t.py::test_a", outcome=PASSED))
    assert record.requested == "" and record.ambiguous is False


# ── The two halves agree ────────────────────────────────────────────────────


def test_discovery_ids_attribute_to_the_fixture_run():
    """The real end-to-end shape: AST ids in, pytest ids out, nothing lost.

    Uses the fixture's own source files, so this checks that
    `test_discovery`'s nodeid construction and this module's parsing agree
    about the same 18 results — the two were written separately and only this
    test says they meet.
    """
    import ast

    from helpers.test_discovery import _collect_test_defs

    requested = []
    for name, rel in (("source_test_alpha.py.txt", "tests/test_alpha.py"),
                      ("source_test_beta.py.txt", "tests/test_beta.py")):
        tree = ast.parse((_FIXTURES / name).read_text(encoding="utf-8"))
        for found in _collect_test_defs(tree):
            parts = [rel] + list(found.class_chain) + [found.node.name]
            requested.append("::".join(parts))

    verbose = (_FIXTURES / "verbose_run.txt").read_text(encoding="utf-8")
    attributed = resolve_identities(parse_run(verbose), requested)

    assert len(attributed) == 18
    assert not any(a.ambiguous for a in attributed)
    unattributed = [a.outcome.nodeid for a in attributed if not a.requested]
    assert unattributed == [], (
        "every result should map back to a discovered definition; these did "
        f"not: {unattributed}")


def test_a_requested_file_claims_the_tests_inside_it():
    """`--tests tests/test_x.py` is a legal pytest selector.

    Found by running the plan's own verification command by hand: every result
    came back unattributed, because the only prefix rule was the parametrised
    one. The Explorer never hit it — it descends to leaf node ids before
    running — which is exactly the gap a suite built around one caller leaves.
    """
    results = [
        TestOutcome(nodeid="tests/test_x.py::test_a", outcome=PASSED),
        TestOutcome(nodeid="tests/test_x.py::TestC::test_b", outcome=PASSED),
    ]
    attributed = resolve_identities(results, ["tests/test_x.py"])
    assert [a.requested for a in attributed] == ["tests/test_x.py"] * 2


def test_a_requested_file_does_not_claim_a_similarly_named_one():
    """The separator boundary, in the file direction.

    Without requiring `::` after the prefix, `tests/test_a.py` would claim
    every result from `tests/test_ab.py` — two genuinely different files.
    """
    results = [TestOutcome(nodeid="tests/test_ab.py::test_x", outcome=PASSED)]
    attributed = resolve_identities(results, ["tests/test_a.py"])
    assert attributed[0].requested == ""
    assert not attributed[0].ambiguous


def test_a_file_and_a_test_inside_it_requested_together_are_ambiguous():
    """Asking for both is a genuine ambiguity, and it is reported as one
    rather than resolved by whichever rule happens to run first."""
    results = [TestOutcome(nodeid="tests/test_x.py::test_a[1]",
                           outcome=PASSED)]
    attributed = resolve_identities(
        results, ["tests/test_x.py", "tests/test_x.py::test_a"])
    assert attributed[0].ambiguous
    assert attributed[0].requested == ""
