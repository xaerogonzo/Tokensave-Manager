"""helpers/observations.py — diagnostics the Manager RECEIVES, already rendered.

**The dividing line.** A producer the Manager *runs and parses* emits
`findings`: the Manager chose the invocation, read the raw output and assigned
the severity (`helpers/quality_checks.py`, `helpers/headless_analyzers.py`). A
verdict the Manager *receives already-rendered* — the editor's Problems panel,
where Pylance decided the rule, the position and the severity — is an
**observation**, and it travels in its own envelope.

That is not fussiness about naming. `vscode-extension/src/diagnostics.ts` holds
one law — *"Python owns rules, positions and severity; the envelope is the
boundary; this file renders"* — and routing the return path through `findings`
would break it with the feature that reuses it. Pylance's
`reportOptionalMemberAccess` is not a Manager rule and never will be.

**The number that does not exist.** `vscode.languages.getDiagnostics()` returns
entries for resources that *have* diagnostics. It does not enumerate what was
analysed, so a clean file Pylance examined and a file it never opened are
indistinguishable in that return value. `analyzed_files` is therefore three-
valued and **`None` (unknown) for the editor source**, forever unless an editor
API appears that can answer it. Saying "0 findings over 47 analyzed files" when
the second half is a guess is the failure this whole module is shaped against.

**Never summed.** No function here returns a combined population or a combined
count across sources. `merge_rows` returns rows carrying `sources`, never a
number, and the two source reports keep their own coverage untouched. A single
headline count over a whole-project run and an editor snapshot covering three
open files would be arithmetic on incomparable populations.

**Unknown is never zero.** A source that could not be read reports
`READ_UNKNOWN`, and its coverage is unknown rather than empty.

Pure apart from `read_snapshot`. No Tk. Safe to call from any thread.
"""

from __future__ import annotations

import dataclasses
import json
import os

# ── the wire format ──────────────────────────────────────────────────────────

#: Bumped when the observations envelope changes incompatibly.
#:
#: A **third** version namespace, deliberately separate from `cli.SCHEMA_VERSION`
#: (what the CLI returns) and `manager_ipc.REQUEST_SCHEMA_VERSION` (what a
#: producer writes into the inbox). Three formats, three producers, three
#: independent compatibility windows; folding them together makes "your
#: extension is out of date" unreadable.
OBSERVATIONS_SCHEMA_VERSION = 1

#: The explicit support window. **A set, not a `<=` comparison.** Backward
#: compatibility is a window that widens deliberately with a test behind it, not
#: "every lower integer forever".
SUPPORTED_SCHEMAS = frozenset({1})

#: Where the extension writes its snapshot. Inside the gitignored
#: `.tokensave-manager/` namespace, beside the commit-request inbox.
SNAPSHOT_DIRNAME = ".tokensave-manager"
SNAPSHOT_FILENAME = "observations.json"

#: A snapshot is a handoff, not a bulk transfer.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_OBSERVATIONS = 20_000

# ── read status ──────────────────────────────────────────────────────────────

READ_OK = "ok"
#: Could not be read. **Never rendered as zero findings.**
READ_UNKNOWN = "unknown"
#: Read fine, and there is simply no snapshot. Distinct from UNKNOWN: one means
#: "nobody has captured one", the other means "we could not tell".
READ_ABSENT = "absent"

#: Source identities. Kept as constants because they are compared, not printed.
SOURCE_EDITOR = "editor"
SOURCE_HEADLESS = "headless"

SEVERITIES = ("error", "warning", "information", "hint")


class SnapshotError(ValueError):
    """A snapshot that must not be trusted. `reason` is user-facing."""


# ── the shapes ───────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Coverage:
    """What a source actually looked at. Three fields, one of them three-valued.

    `analyzed_files` is `None` when unknown, which for the editor source is
    always. It is deliberately NOT defaulted to `files_with_diagnostics`: those
    answer different questions, and quietly substituting one turns "we cannot
    know" into a confident number.
    """

    #: Rows the source reported. Always knowable.
    diagnostic_entries: int = 0
    #: Distinct files those rows touched. Always knowable.
    files_with_diagnostics: int = 0
    #: How many files were examined. `None` = unknown.
    analyzed_files: "int | None" = None
    #: The editor's configured analysis policy, verbatim, or "".
    #:
    #: **Policy, not coverage.** `workspace` says what the editor was told to
    #: do; it is not evidence that anything in the declared population was
    #: analysed at capture time. Rendered beside the population, never instead
    #: of it.
    diagnostic_mode: str = ""
    #: Free text naming what the source covered, e.g. "whole project" or the
    #: `--paths` scope. Display only.
    scope: str = ""

    @property
    def analyzed_files_text(self) -> str:
        """What a person reads. Never invents a number."""
        if self.analyzed_files is None:
            return "analyzed files: unknown"
        return "analyzed files: %d" % self.analyzed_files


@dataclasses.dataclass(frozen=True)
class Observation:
    """One already-rendered diagnostic.

    Coordinates are **1-based**, like everything else in this project's
    envelopes. VS Code's are 0-based; the extension converts once, at the
    boundary, exactly as `diagnostics.ts` converts once in the other direction.
    """

    file: str
    line: int
    column: int = 1
    end_line: int = 0
    end_column: int = 0
    severity: str = "warning"
    message: str = ""
    rule: str = ""
    #: Who rendered it — "Pylance", "ruff", the extension id. Display only; it
    #: is not an identity and is never compared.
    producer: str = ""

    @property
    def identity(self) -> tuple:
        """`(file, line, column, rule)` — and nothing else.

        Message and severity are deliberately **excluded**. Two tools
        describing the same defect in different words are still the same
        defect, and the whole point of the merge is to show that they disagree
        about the wording rather than to pick one.
        """
        return (self.file, self.line, self.column, self.rule)


@dataclasses.dataclass(frozen=True)
class SourceReport:
    """One source's contribution. Carries its own population and status.

    There is deliberately **no `__add__`, and no accessor that combines this
    report with another.** The invariant is behavioural — no public API in this
    module produces a combined population or count — and this shape is how it is
    achieved rather than the rule itself.
    """

    key: str
    label: str
    read_status: str = READ_OK
    coverage: Coverage = dataclasses.field(default_factory=Coverage)
    rows: tuple = ()
    #: Unix epoch seconds, or 0 when unknown. Displayed, **never thresholded**:
    #: a three-day-old snapshot is a fact about when it was taken, not an error.
    as_of: int = 0
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.read_status == READ_OK


@dataclasses.dataclass(frozen=True)
class MergedRow:
    """One identity, and every source that reported it.

    Each contributing source keeps **its own severity and message, verbatim**.
    There is no synthesized severity, no precedence rule and no merged message —
    a `max(severities)` here would be a hidden reconciliation, which is the
    thing `mcp_posture.py` exists to warn about wearing a one-line disguise.

    **A source may contribute more than one observation to one identity, and
    that is not a duplicate.** Measured on this repository: ruff reports
    `UP035` twice on a single import line (`typing.Tuple` and `typing.List` are
    two deprecations at one position), and pyright reports
    `reportAttributeAccessIssue` ten times at one position — once per union
    member. So `(file, line, column, code)` is **not a unique key within a
    source**, and an implementation that keeps one observation per source drops
    real findings: 557 of them, on the first live run that looked.

    Hence `sources` maps a source key to a TUPLE of observations rather than to
    one. Nothing is discarded, which is the rule this whole module is built on.
    """

    file: str
    line: int
    column: int
    rule: str
    #: `(source_key, (Observation, ...))` pairs, in the order the sources were
    #: given. Each source appears **at most once**, so `len(sources)` answers
    #: "how many sources saw this" and never "how many rows were there".
    sources: tuple = ()

    @property
    def source_keys(self) -> tuple:
        return tuple(key for key, _obs in self.sources)

    @property
    def observations(self) -> tuple:
        """Every contributing observation, flattened. Nothing is lost."""
        return tuple(o for _k, group in self.sources for o in group)

    @property
    def shared(self) -> bool:
        """Reported by **more than one source**.

        Distinct from "more than one observation": a single source reporting a
        position twice is not corroboration, and the earlier version of this
        conflated them — a live run reported 321 rows "seen by both" on a
        project with no editor snapshot at all.
        """
        return len(self.sources) > 1

    @property
    def agreed(self) -> bool:
        """Does everything reported at this identity agree on how bad it is?"""
        return len({o.severity for o in self.observations}) == 1


# ── pure computation ─────────────────────────────────────────────────────────

def _compute_coverage(rows, scope: str = "", diagnostic_mode: str = "",
                      analyzed_files: "int | None" = None) -> Coverage:
    """Coverage for a list of observations. `analyzed_files` stays unknown."""
    return Coverage(
        diagnostic_entries=len(rows),
        files_with_diagnostics=len({r.file for r in rows}),
        analyzed_files=analyzed_files,
        diagnostic_mode=diagnostic_mode,
        scope=scope,
    )


def merge_rows(reports) -> list:
    """Identical identities collapsed **across sources**, nothing discarded.

    Returns `MergedRow`s, never a count. Order is stable: first appearance
    across the reports in the order given, so a re-render does not reshuffle.

    **Grouping is per identity AND per source.** A source that reports the same
    `(file, line, column, code)` more than once is reporting more than one
    thing — two deprecations on one import line, ten union members at one
    attribute access — so every observation is kept and only the *sources* are
    deduplicated against each other. Collapsing within a source would be the
    merge silently deleting findings, which is the failure this module exists
    to prevent rather than commit.
    """
    order: list = []
    seen: dict = {}
    for report in reports:
        for obs in report.rows:
            key = obs.identity
            if key not in seen:
                seen[key] = {}
                order.append(key)
            seen[key].setdefault(report.key, []).append(obs)
    out = []
    for key in order:
        by_source = seen[key]
        first = next(iter(by_source.values()))[0]
        out.append(MergedRow(
            file=first.file, line=first.line, column=first.column,
            rule=first.rule,
            sources=tuple((source, tuple(group))
                          for source, group in by_source.items())))
    return out


def findings_to_report(findings, key: str = SOURCE_HEADLESS,
                       label: str = "headless", scope: str = "whole project",
                       as_of: int = 0) -> SourceReport:
    """Adapt a `helpers.findings.Finding` list into a source report.

    The headless half CAN state a real population — the project tree, or the
    `--paths` scope — which is exactly why the two sources are never merged into
    one number. `analyzed_files` is left unknown here too: a linter reports what
    it flagged, not what it opened.
    """
    def _split(rule: str) -> "tuple[str, str]":
        """`ruff/F401` -> `("ruff", "F401")`. The split IS the adapter's job.

        A `Finding.rule` is deliberately producer-qualified so a consumer can
        group without knowing which command produced it. An **observation
        identity** wants the opposite: the bare code, because pyright run
        headlessly and Pylance running in the editor report
        `reportOptionalMemberAccess` for the same defect, and a qualified form
        would make the one comparison this feature exists for impossible.

        A rule with no code at all (pyflakes prints none) yields an empty
        code, which is honest — there is nothing to match on but position.
        """
        producer, _, code = (rule or "").partition("/")
        return producer, code

    rows = []
    for f in findings:
        producer, code = _split(f.rule)
        rows.append(Observation(
            file=f.file, line=f.line, column=f.column,
            end_line=f.end_line or f.line, end_column=f.end_column or f.column,
            severity=f.severity, message=f.message, rule=code,
            producer=producer))
    rows = tuple(rows)
    return SourceReport(key=key, label=label, read_status=READ_OK,
                        coverage=_compute_coverage(rows, scope=scope),
                        rows=rows, as_of=as_of)


# ── validation ───────────────────────────────────────────────────────────────

def schema_problem(doc) -> "str | None":
    """Why an envelope's schema is unusable, or `None` when it is fine.

    Mirrors `cli.ts`'s `schemaProblem`, including the bug that one had to be
    fixed for: an **absent** version must be refused explicitly, because a
    `version > SUPPORTED` test lets `undefined` through and reads a malformed
    document as a version-1 one.

    A newer envelope is refused outright and **never partially parsed**. "Close
    enough" is how a renamed field gets silently read as the one it replaced.
    """
    if not isinstance(doc, dict):
        return "the snapshot is not a JSON object"
    version = doc.get("observations_schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return ("the snapshot carried no observations_schema_version; "
                "this is not an observations envelope")
    if version in SUPPORTED_SCHEMAS:
        return None
    if version > max(SUPPORTED_SCHEMAS):
        return ("the extension wrote observations schema %d; this Manager "
                "understands %s. Update the Manager."
                % (version, ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMAS))))
    return ("observations schema %d predates anything this Manager supports "
            "(known: %s)"
            % (version, ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMAS))))


def _safe_relative(raw: str) -> str:
    """A repo-relative, forward-slash path, or raise.

    The snapshot sits in the project directory and anything on the machine can
    write it, so a path is checked rather than trusted — the same reasoning
    `manager_ipc._validate_commit_path` records. An absolute path or one that
    climbs out is refused outright rather than filtered: a snapshot that names
    something it may not is wrong, and honouring the acceptable half of it hides
    that.
    """
    path = str(raw or "").replace(chr(92), "/").strip()
    if not path:
        raise SnapshotError("an observation names no file")
    if os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
        raise SnapshotError("observation path is absolute: %s" % raw)
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise SnapshotError("observation path escapes the project: %s" % raw)
    return "/".join(parts)


def _compute_observation(row) -> Observation:
    """One validated row. Raises `SnapshotError` on anything unusable."""
    if not isinstance(row, dict):
        raise SnapshotError("an observation is not a JSON object")
    severity = str(row.get("severity") or "warning")
    if severity not in SEVERITIES:
        # The producer chose it and we do not second-guess it, but an unknown
        # value is a format problem rather than something to silently coerce.
        raise SnapshotError("unknown severity %r" % severity)
    def _coord(name: str, default: int) -> int:
        """A coordinate, where **0 is a value and not an absence**.

        `int(row.get(name) or 1)` reads a literal `0` as missing and silently
        substitutes 1 — which would defeat the 1-based check immediately below
        by repairing exactly the input it exists to catch. Same shape as the
        `if ($Config.Thing)` trap in `gotchas/powershell-silent-failures.md`,
        and it was written here first before this test caught it.
        """
        value = row.get(name)
        return default if value is None else int(value)

    try:
        line = _coord("line", 1)
        column = _coord("column", 1)
        end_line = _coord("end_line", line)
        end_column = _coord("end_column", column)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("non-numeric position: %s" % exc) from exc
    if line < 1 or column < 1:
        # 1-based everywhere. A 0 here means the producer forwarded VS Code's
        # own 0-based Position without converting, which would put every row
        # one line up.
        raise SnapshotError(
            "position is not 1-based (line=%d column=%d); the extension "
            "converts VS Code's 0-based Position at the boundary"
            % (line, column))
    return Observation(
        file=_safe_relative(row.get("file")),
        line=line, column=column, end_line=end_line, end_column=end_column,
        severity=severity,
        message=str(row.get("message") or ""),
        rule=str(row.get("rule") or ""),
        producer=str(row.get("producer") or ""),
    )


def compute_report(doc) -> SourceReport:
    """A validated `SourceReport` from a parsed envelope. Pure.

    Raises `SnapshotError` with a user-facing reason. The caller turns that into
    `READ_UNKNOWN` — never into an empty report, which would be the snapshot
    reporting a clean editor it never described.
    """
    problem = schema_problem(doc)
    if problem:
        raise SnapshotError(problem)

    raw_rows = doc.get("observations")
    if not isinstance(raw_rows, list):
        raise SnapshotError("the snapshot carried no observations array")
    if len(raw_rows) > MAX_OBSERVATIONS:
        raise SnapshotError("%d observations; the cap is %d"
                            % (len(raw_rows), MAX_OBSERVATIONS))

    rows = tuple(_compute_observation(r) for r in raw_rows)

    raw_cov = doc.get("coverage")
    if not isinstance(raw_cov, dict):
        raw_cov = {}
    analyzed = raw_cov.get("analyzed_files")
    if analyzed is not None and not isinstance(analyzed, int):
        analyzed = None
    coverage = Coverage(
        diagnostic_entries=len(rows),
        files_with_diagnostics=len({r.file for r in rows}),
        # Trusted only if the producer actually claimed it. Absent stays
        # unknown; it is never filled in from the row count.
        analyzed_files=analyzed,
        diagnostic_mode=str(raw_cov.get("diagnostic_mode") or ""),
        scope=str(raw_cov.get("scope") or ""),
    )
    captured = doc.get("captured_at")
    return SourceReport(
        key=SOURCE_EDITOR,
        label=str(doc.get("editor_label") or "editor"),
        read_status=READ_OK,
        coverage=coverage,
        rows=rows,
        as_of=int(captured) if isinstance(captured, int) else 0,
    )


# ── the one IO boundary ──────────────────────────────────────────────────────

def snapshot_path(project_root: str) -> str:
    return os.path.join(project_root, SNAPSHOT_DIRNAME, SNAPSHOT_FILENAME)


def read_snapshot(project_root: str) -> SourceReport:
    """The editor's snapshot for a project. Never raises.

    Every failure produces a report whose `read_status` is `READ_UNKNOWN` or
    `READ_ABSENT` and whose coverage is unknown — never an empty `READ_OK`,
    which is a clean editor nobody observed.
    """
    path = snapshot_path(project_root)
    if not os.path.isfile(path):
        return SourceReport(
            key=SOURCE_EDITOR, label="editor", read_status=READ_ABSENT,
            detail="no snapshot has been captured for this project")
    try:
        size = os.path.getsize(path)
        if size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot is %d bytes; the cap is %d"
                                % (size, MAX_SNAPSHOT_BYTES))
        with open(path, encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return SourceReport(key=SOURCE_EDITOR, label="editor",
                            read_status=READ_UNKNOWN,
                            detail="could not read the snapshot: %s" % exc)
    except SnapshotError as exc:
        return SourceReport(key=SOURCE_EDITOR, label="editor",
                            read_status=READ_UNKNOWN, detail=str(exc))
    try:
        return compute_report(doc)
    except SnapshotError as exc:
        return SourceReport(key=SOURCE_EDITOR, label="editor",
                            read_status=READ_UNKNOWN, detail=str(exc))
