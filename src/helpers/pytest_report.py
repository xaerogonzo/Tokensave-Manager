"""helpers/pytest_report.py — what a pytest run actually did, per test.

`smoke_runner` has always answered "how many passed", which is all a gate
needs. A Test Explorer needs the other question — *which* ones — and that turns
out to be a question about **identity**, not about parsing.

Two responsibilities, kept apart on purpose:

    parse_run(...)          the formats pytest emits
    resolve_identities(...) which requested test each result belongs to

They are separate because only the second one can be *ambiguous*, and an
ambiguity that gets absorbed into a parser becomes a confident wrong answer.

## Where identity comes from, and why not from the obvious place

Every claim below was measured against a fixture project carrying all seven
awkward cases (`tests/fixtures/pytest_report/`), not reasoned about.

**The JUnit XML cannot identify a test.** Its `classname` is a *dotted* path —
``tests.test_alpha.TestOne`` — and dotted paths are lossy: that string is
equally consistent with module ``tests.test_alpha`` plus class ``TestOne`` and
with a package ``tests/test_alpha/`` containing ``TestOne.py``. Nothing in the
document says which. Reconstructing a nodeid from it is a guess dressed as a
parse.

**And it carries no location.** Under pytest 9's default ``junit_family``
(``xunit2``) a ``<testcase>`` has no ``file`` and no ``line`` attribute at all.
The legacy ``xunit1`` family does emit them — with **native Windows
separators** (``tests\\test_alpha.py``), which is the producer-dependent
spelling `helpers/findings.relative_to` exists to stop reaching an editor. So
neither family is a location source worth having, and location is not taken
from here: it comes from `helpers.test_discovery`, which already knows the
1-based range of every definition.

**The `-v` progress lines carry exact nodeids**, including parametrised ids and
nested classes (``tests/test_alpha.py::TestOuter::TestInner::test_nested``),
and this project's ``addopts`` already forces ``-v``. So they are the identity
spine, and the XML supplies only what it is good at: per-test duration and the
failure message.

**The join runs nodeid → XML, never the reverse.** Deriving
``tests.test_alpha.TestOne`` + ``test_method`` from a nodeid is a function;
going back is not. Two nodeids that derive the same key are reported as
ambiguous rather than merged.

## The separator is not a separator

``::`` occurs *inside* parameter ids. This is real, not theoretical — a
``@pytest.mark.parametrize`` over ``"a::b"`` produces::

    tests/test_alpha.py::test_param_containing_separator[a::b]

so ``nodeid.split("::")`` yields ``test_param_containing_separator[a`` and
reads it as a class name. :func:`split_nodeid` splits the file off first and
then treats the first segment containing ``[`` as the function, rejoining
everything after it — because parameters are only ever on the last component.

No Tk, no I/O, no subprocess. Pure functions over text.
"""

from __future__ import annotations

import dataclasses
import re
import xml.etree.ElementTree as ET

#: Outcomes this module will report. A closed set, chosen here rather than
#: passed through, so a consumer never has to handle a word pytest invented.
PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
XFAILED = "xfailed"
XPASSED = "xpassed"

OUTCOMES: tuple = (PASSED, FAILED, ERROR, SKIPPED, XFAILED, XPASSED)

#: pytest's verbose spelling → ours.
_VERBOSE_OUTCOMES = {
    "PASSED": PASSED,
    "FAILED": FAILED,
    "ERROR": ERROR,
    "SKIPPED": SKIPPED,
    "XFAIL": XFAILED,
    "XPASS": XPASSED,
}

#: A verbose progress line: a nodeid, then the outcome word.
#:
#: Anchored on ``.py::`` so the run's other sections cannot match — the ERRORS
#: header prints ``file /proj/tests/test_alpha.py, line 12`` (no ``::``) and the
#: ``-rfE`` summary prints the outcome *first* (``FAILED tests/x.py::y``), which
#: the leading-outcome guard in :func:`parse_verbose_lines` rejects.
#:
#: The nodeid group is non-greedy and allows spaces, because parameter ids
#: contain them (``test_parametrised[with space]``).
_VERBOSE_LINE = re.compile(
    r"^(?P<nodeid>\S*\.py::.+?)[ \t]+"
    r"(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?=[ \t]|\(|$)")

#: Lines that begin with an outcome are the `-rfE` summary, not progress.
_SUMMARY_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")


@dataclasses.dataclass(frozen=True)
class TestOutcome:
    """What happened to one test pytest actually ran.

    ``nodeid`` is pytest's own, verbatim — including any ``[param]`` suffix.
    It is **not** necessarily one of the ids `test_discovery` produced; see
    :func:`resolve_identities` for the mapping between the two.

    ``duration`` is seconds, or ``None`` when no XML was available.
    ``message`` is the failure/error/skip reason, or "" for a pass.
    """

    #: pytest collects any class named `Test*`, and warns when it cannot.
    #: This is a result record, not a test case.
    __test__ = False

    nodeid: str
    outcome: str
    duration: "float | None" = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"{self.nodeid}: unknown outcome "
                             f"{self.outcome!r}")


@dataclasses.dataclass(frozen=True)
class Attribution:
    """Which requested test a result belongs to, or why that is unanswerable.

    ``requested`` is the id the caller asked to run — an AST-discovered
    `TestCase.nodeid`. ``ambiguous`` means the result could belong to more
    than one requested test and this module refuses to pick; the consumer
    must render that as "could not attribute", never as a result.
    """

    outcome: TestOutcome
    requested: str = ""
    ambiguous: bool = False


# ── Nodeid structure ─────────────────────────────────────────────────────────


def split_nodeid(nodeid: str) -> tuple:
    """``(file, class_chain, name)`` for a pytest nodeid.

    ``name`` keeps its ``[param]`` suffix, which may itself contain ``::``.

    Returns ``("", (), nodeid)`` for anything without a ``.py::``, so a caller
    handling odd input gets a usable record rather than an exception.
    """
    marker = ".py::"
    cut = nodeid.find(marker)
    if cut == -1:
        return "", (), nodeid
    file = nodeid[:cut + 3]
    rest = nodeid[cut + len(marker):]

    parts = rest.split("::")
    # Parameters live only on the final component, so the first segment that
    # opens a bracket IS the function; everything after it was split out of a
    # parameter id and belongs back on the end.
    for i, part in enumerate(parts):
        if "[" in part:
            return file, tuple(parts[:i]), "::".join(parts[i:])
    return file, tuple(parts[:-1]), parts[-1]


def junit_key(nodeid: str) -> tuple:
    """The ``(classname, name)`` the XML would file this nodeid under.

    One-way on purpose: path → dotted is a function, dotted → path is not.
    """
    file, classes, name = split_nodeid(nodeid)
    module = file[:-3].replace("\\", "/").strip("/").replace("/", ".")
    classname = ".".join((module,) + classes) if module else ".".join(classes)
    return classname, name


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_verbose_lines(text: str) -> list:
    """``[(nodeid, outcome)]`` from pytest's ``-v`` progress lines.

    Order is the order pytest ran them. Duplicate nodeids are preserved rather
    than collapsed — a rerun plugin can legitimately report one twice, and
    deciding which to keep is not this function's call.
    """
    out: list = []
    for line in (text or "").splitlines():
        stripped = line.rstrip()
        if _SUMMARY_LINE.match(stripped):
            continue
        match = _VERBOSE_LINE.match(stripped)
        if match:
            out.append((match.group("nodeid").rstrip(),
                        _VERBOSE_OUTCOMES[match.group("outcome")]))
    return out


def parse_junit_xml(text: str) -> dict:
    """``{(classname, name): {"duration", "message"}}`` from a JUnit report.

    Enrichment only — see the module docstring on why this is not an identity
    source. Malformed XML yields an empty mapping rather than raising: a run
    whose results were read from the ``-v`` lines must not be lost because the
    optional half could not be parsed.
    """
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError:
        return {}

    found: dict = {}
    for case in root.iter("testcase"):
        try:
            duration = float(case.get("time") or 0.0)
        except (TypeError, ValueError):
            duration = None
        message = ""
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                # The attribute is the summary, the text is the traceback.
                # Both are useful and the attribute alone is often one line.
                message = (child.get("message") or "").strip()
                body = (child.text or "").strip()
                if body and body != message:
                    message = f"{message}\n\n{body}" if message else body
                break
        found[(case.get("classname") or "", case.get("name") or "")] = {
            "duration": duration, "message": message}
    return found


def parse_run(verbose_text: str, junit_text: str = "") -> list:
    """The run, as :class:`TestOutcome` records.

    Identity and outcome come from *verbose_text*; duration and message are
    joined on from *junit_text* when it is supplied. A nodeid with no XML entry
    keeps its outcome and simply has no duration — the report is degraded, not
    dropped.

    **Ambiguity is not silently resolved here either.** Two nodeids deriving
    the same XML key (which a nested-class layout can produce) means neither
    can be enriched with confidence, so both are left unenriched.
    """
    enrichment = parse_junit_xml(junit_text) if junit_text else {}

    lines = parse_verbose_lines(verbose_text)
    key_counts: dict = {}
    for nodeid, _ in lines:
        key = junit_key(nodeid)
        key_counts[key] = key_counts.get(key, 0) + 1

    out: list = []
    for nodeid, outcome in lines:
        key = junit_key(nodeid)
        extra = enrichment.get(key) if key_counts.get(key) == 1 else None
        out.append(TestOutcome(
            nodeid=nodeid,
            outcome=outcome,
            duration=(extra or {}).get("duration"),
            message=(extra or {}).get("message", "")))
    return out


# ── Attribution ──────────────────────────────────────────────────────────────


def resolve_identities(outcomes: list, requested: list) -> list:
    """Attach each result to the requested test it belongs to.

    *requested* is a list of AST-discovered nodeids (`TestCase.nodeid`). Those
    are **prefixes** of what pytest reports for a parametrised test, so the
    match is:

      1. **exact** — the run reported the same id that was asked for;
      2. **parametrised** — the run's id is ``<requested>[...]``;
      3. neither — the result is returned unattributed (``requested == ""``),
         which is the honest answer for a test the caller never asked about.

    Rule 2 is applied only when it lands on **exactly one** candidate. It
    cannot normally collide, because two definitions with the same nodeid are
    the shadowing defect `tests/test_no_shadowed_tests.py` exists to prevent —
    but "cannot normally" is not "cannot", and a best guess between two
    candidate tests is precisely the failure this module is shaped to avoid.
    So a collision sets ``ambiguous`` and attributes to neither.
    """
    exact = {}
    for nodeid in requested:
        exact.setdefault(nodeid, 0)
        exact[nodeid] += 1

    out: list = []
    for outcome in outcomes:
        if exact.get(outcome.nodeid):
            out.append(Attribution(outcome=outcome, requested=outcome.nodeid,
                                   ambiguous=exact[outcome.nodeid] > 1))
            continue
        candidates = [n for n in exact if outcome.nodeid.startswith(n + "[")]
        if len(candidates) == 1 and exact[candidates[0]] == 1:
            out.append(Attribution(outcome=outcome, requested=candidates[0]))
        elif len(candidates) > 1 or (candidates and exact[candidates[0]] > 1):
            out.append(Attribution(outcome=outcome, ambiguous=True))
        else:
            out.append(Attribution(outcome=outcome))
    return out


def summarise(outcomes: list) -> dict:
    """Counts by outcome, with every key present even at zero.

    A missing key and a zero are different statements and only one of them is
    true of a run that had no skips.
    """
    counts = {name: 0 for name in OUTCOMES}
    for outcome in outcomes:
        counts[outcome.outcome] += 1
    return counts
