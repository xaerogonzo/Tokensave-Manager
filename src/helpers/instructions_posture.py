"""instructions_posture — does a project's Claude instruction chain resolve?

## Why this exists

Retrofit wrote `BASIC_INSTRUCTIONS.md` into projects for months and nothing
ever asked whether anything read it. Claude Code loads `CLAUDE.md`;
`BASIC_INSTRUCTIONS.md` is this project's own convention and arrives only when
`CLAUDE.md` includes it. Measured across seventeen projects: eleven carried a
correct `@...project-baseline.md` line inside a file nothing linked to, and the
baseline reached five.

A file being present is not the same as a file being loaded. That is the whole
bug class, and it is the same one `mcp_posture` was written for.

## What `reach` claims, and what it does not

`reach` means **the documented include chain resolves from `CLAUDE.md` under
this module's parser**. It does NOT mean Claude loaded those bytes — there is no
session here to observe.

That is why the positive state is `REACH_RESOLVED` and not `REACH_LOADED`, and
why the panel says "baseline chain resolves". A UI-string convention drifts; a
constant name travels to every call site. The Roadmap-11 MCP dialog rendered a
file-content verdict as effective scope and put a green check on ten projects
served by something else. Same shape, new place.

## Parser scope is deliberately wider than repair scope

**This module understands a bounded, general include graph. Retrofit repairs
exactly one topology.** Do not "simplify" that difference away.

Resolving broadly is what stops the fleet panel offering a fix to a project that
already works through an undocumented-but-valid chain — a "fix" that would add a
second path to the same baseline. Reading only this module, it is easy to
conclude Retrofit should repair anything this can resolve. It must not.

## Two facts kept apart

`carriage` is what the project's files declare. `reach` is what the chain from
`CLAUDE.md` actually arrives at. A project can carry a perfectly correct include
and never resolve it — that is every one of those eleven orphans, and a summary
computed from `carriage` alone puts a green badge on all of them.

## Identity, not spelling

A reached baseline is compared to the configured one by **canonical resolved
path**, so the same file spelled with different separators or relative segments
is the same baseline. Conversely a matching basename somewhere else is a
DIFFERENT baseline: `project-baseline.md` is a filename, not an identity.

Pure apart from :func:`read_posture` and the small readers it calls — stdlib
only, no Tkinter, safe from any thread.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from helpers import baseline_copy as bc
from helpers.mcp_projects import (
    external_includes_approved,
    normalize_project_key,
    read_claude_projects_strict,
    resolve_project_record,
)
from helpers.project_discovery import find_projects

# ── vocabulary ───────────────────────────────────────────────────────────

#: Does the documented chain resolve from `CLAUDE.md`?
REACH_RESOLVED = "resolved"
#: Resolves to a baseline that is not the configured one.
REACH_STALE = "stale"
#: No baseline on the chain, but an unreached file carries a directive.
REACH_ORPHANED = "orphaned"
#: No baseline directive anywhere.
REACH_ABSENT = "absent"
#: A fact needed to decide could not be obtained. Never a confident absence.
REACH_UNKNOWN = "unknown"

#: What the project's own files declare, independent of what loads.
CARRIAGE_CURRENT = "current"
CARRIAGE_STALE = "stale"
CARRIAGE_NONE = "none"
CARRIAGE_UNKNOWN = "unknown"

#: Heuristics. Reported, never acted on automatically.
ADVISORY_DOUBLE_LOAD = "double_load"
ADVISORY_DUPLICATE_DIRECTIVE = "duplicate_directive"
ADVISORY_DUPLICATE_CONTENT = "duplicate_content"
ADVISORY_NONSTANDARD_CHAIN = "nonstandard_chain"
ADVISORY_CONTRADICTS = "contradicts"
ADVISORY_PLACEHOLDER = "placeholder"
ADVISORY_INDENTED_DIRECTIVE = "indented_directive"

#: Is the reached baseline the configured one's CONTENT? Kept apart from the
#: path in `reached_baseline`: a localized project reaches its own copy, a
#: different file, and saying that path "is" the template would make a path
#: comparison secretly become a content comparison.
CONTENT_MATCH = "match"
CONTENT_MISMATCH = "mismatch"
CONTENT_UNKNOWN = "unknown"

#: Will Claude Code load the chain? An OBSERVED fact about Claude Code, not a
#: Manager invariant: files outside the project load only when the project's
#: record approves external includes (measured 2026-09-13, see
#: `helpers/baseline_copy.py`). Approved is still not the goal state - an
#: absolute include is non-portable - so it is diagnostic, never a green light.
DELIVERY_IN_PROJECT = "in_project"
DELIVERY_EXTERNAL_APPROVED = "external_approved"
DELIVERY_EXTERNAL_BLOCKED = "external_blocked"
#: A directive on the chain is spelled so Claude Code does not parse it.
DELIVERY_UNPARSEABLE = "unparseable"
DELIVERY_UNKNOWN = "unknown"

#: Sentinel: `read_project` loads `~/.claude.json` itself when not handed one.
_LOAD = object()

#: Traversal bounds. This runs fleet-wide, so a pathological include graph must
#: terminate rather than turn the panel into a filesystem scanner.
MAX_DEPTH = 8
MAX_FILES = 32
MAX_BYTES = 2 * 1024 * 1024

CLAUDE_MD = "CLAUDE.md"
BASIC_MD = "BASIC_INSTRUCTIONS.md"

#: How a baseline file is RECOGNISED. Whether it is the CURRENT one is a
#: separate question answered by canonical path comparison — see the module
#: docstring. These two must not be confused; that confusion is `REACH_STALE`.
BASELINE_BASENAME = "project-baseline.md"

_FOLLOWABLE_SUFFIXES = (".md", ".markdown")

#: Phrases that indicate a project instructs file-scanning first. One project's
#: `CLAUDE.md` opens with exactly this, which is a measured cause of the
#: Read/Grep turns this whole feature exists to reduce.
_CONTRADICT_PATTERNS = (
    re.compile(r"\bprefer\b[^.\n]{0,40}\b(grep|glob)\b", re.I),
    re.compile(r"\bexplore\s+subagent\b", re.I),
    re.compile(r"\btoken[- ]saving tool usage\b", re.I),
)


# ── directive parsing ────────────────────────────────────────────────────

#: A directive is `@` at COLUMN 0 followed by a non-space path.
_DIRECTIVE_RE = re.compile(r"^@(\S.*?)\s*$")
#: The same thing, indented. Deliberately captured rather than ignored — see
#: `scan_text`.
_INDENTED_RE = re.compile(r"^[ \t]+@(\S.*?)\s*$")
#: Backtick or tilde fence, possibly indented, with an optional info string.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class FileScan:
    """One file's directives. Pure product of its text."""

    path: str
    size: int
    #: (lineno, raw_target) at column 0.
    directives: tuple = ()
    #: (lineno, raw_target) indented — recognised but NOT followed.
    indented: tuple = ()
    unclosed_fence: bool = False


def scan_text(text: str) -> "tuple[tuple, tuple, bool]":
    """Split *text* into (directives, indented, unclosed_fence). Pure.

    The fence model is deliberately small and is not a Markdown implementation:
    a run of three or more backticks or tildes opens a block, and the first
    same-character run of at least that length with no info string closes it.
    Directives are not recognised in between. An unclosed fence suppresses the
    remainder of the file and is reported rather than guessed at.

    Indented directives are returned SEPARATELY instead of being silently
    dropped or silently followed. Whether Claude Code honours an indented `@`
    line is not something this project has measured, and instruction files are
    full of lists and quoted examples. Calling it "not a directive" risks
    proposing a repair that duplicates a chain which already works; calling it
    a directive risks a false green. The caller decides, and only where it
    could change the answer.
    """
    directives: list = []
    indented: list = []
    fence_char = ""
    fence_len = 0

    for lineno, line in enumerate(text.splitlines(), 1):
        fence = _FENCE_RE.match(line)
        if fence:
            run, info = fence.group(1), fence.group(2)
            if not fence_char:
                fence_char, fence_len = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len and not info.strip():
                fence_char, fence_len = "", 0
            continue
        if fence_char:
            continue
        hit = _DIRECTIVE_RE.match(line)
        if hit:
            directives.append((lineno, hit.group(1)))
            continue
        soft = _INDENTED_RE.match(line)
        if soft:
            indented.append((lineno, soft.group(1)))

    return tuple(directives), tuple(indented), bool(fence_char)


def canonical(path: str) -> str:
    """The comparison form for a filesystem path.

    `normcase` folds case and separators **on Windows**, and is a **no-op on
    POSIX** — so this comparison is case-insensitive on one platform and
    case-sensitive on the other. That is correct rather than a gap:
    `/TMP/vendor` genuinely is a different directory from `/tmp/vendor`, and
    folding case there would match paths the user never named.

    The sentence this replaces said Windows "is where every path in this
    project lives", and that assumption licensed a test which asserted the
    Windows answer on both platforms. It passed for months and failed the first
    time CI ran it on Linux. Correctness here must not depend on case-folding;
    it is applied for Windows' benefit only. `manager_ipc.canonical_project`
    carries the same warning, from the same lesson.

    Deliberately no `realpath`: the configured baseline is not realpath'd
    either, and canonicalising one side differently from the other is how two
    spellings of one file become two files.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def parse_baseline_target(include_line: str) -> "str | None":
    """Canonical path the configured baseline include line names, or None.

    Runs the SAME parser over the configured value. Without this the module
    would trust a config string it never validated, and a malformed
    `baseline_include_line` would silently make every project on the machine
    look stale rather than making the configuration look wrong.
    """
    if not include_line:
        return None
    directives, _indented, _unclosed = scan_text(include_line.strip())
    if len(directives) != 1:
        return None
    raw = directives[0][1]
    if not raw.lower().endswith(_FOLLOWABLE_SUFFIXES):
        return None
    return canonical(raw)


def is_baseline(path: str) -> bool:
    """Is *path* a baseline file BY NAME? Says nothing about which one."""
    return os.path.basename(path).lower() == BASELINE_BASENAME


def unescape_target(raw: str) -> str:
    """A directive target as a filesystem path: `\\ ` is an escaped space."""
    return raw.replace("\\ ", " ")


_UNESCAPED_SPACE = re.compile(r"(?<!\\)\s")


def claude_code_parses(raw: str) -> bool:
    """Would Claude Code load a directive spelled *raw*? Measured, not documented.

    Canaries on 2026-09-13, each file inside the project: `@rel.md` and an
    absolute forward-slash path with `\\ ` escapes loaded; the same absolute
    path with raw spaces did NOT, and a backslash path with no spaces at all
    (`@C:\\dir\\abs.md`) did NOT. So a space must be escaped and a backslash may
    appear only as that escape. This parser is wider on purpose - it resolves
    what a human meant - and this predicate says what Claude Code will do.
    """
    return ("\\" not in raw.replace("\\ ", "")
            and not _UNESCAPED_SPACE.search(raw))


# ── the walk ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChainScan:
    """Raw result of walking the include graph. Carries no verdicts."""

    files: tuple = ()
    total_bytes: int = 0
    #: (canonical_target, source_path, lineno) for every baseline reached.
    baselines: tuple = ()
    #: Reasons the walk is incomplete. Each is a human-readable string.
    unreadable: tuple = ()
    escapes: tuple = ()
    cycles: tuple = ()
    indented: tuple = ()
    bounds: str = ""
    unclosed_fences: tuple = ()
    #: canonical path -> the spelling actually on disk. Comparisons use the
    #: canonical form; humans are shown the real one. Deriving a display name
    #: from `normcase` output renamed a project's CLAUDE.md to lowercase in
    #: the confirmation dialog, which is a file the user does not have.
    display: tuple = ()

    def shown(self, path: str) -> str:
        for canonical_path, as_written in self.display:
            if canonical_path == path:
                return as_written
        return path

    @property
    def incomplete(self) -> bool:
        return bool(self.unreadable or self.escapes or self.cycles
                    or self.bounds or self.indented)

    @property
    def paths(self) -> tuple:
        return tuple(f.path for f in self.files)


def _read(path: str) -> "str | None":
    """File text, or None when it cannot be read. utf-8-sig tolerates a BOM."""
    try:
        with open(path, encoding="utf-8-sig", errors="strict") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _within(path: str, boundaries: tuple) -> bool:
    for boundary in boundaries:
        if path == boundary or path.startswith(boundary + os.sep):
            return True
    return False


def _expand_directives(path: str, as_written: str, depth: int,
                       ancestors: frozenset, directives: tuple,
                       boundaries: tuple,
                       label: str) -> "tuple[list, list, list]":
    """Turn one file's directives into (baselines, escapes, queue entries).

    Split out of `walk_chain` so the traversal reads as a traversal. The one
    rule worth keeping in view is the order of the two tests below.
    """
    baselines: list = []
    escapes: list = []
    queued: list = []
    base = os.path.dirname(as_written)

    for lineno, raw in directives:
        if not raw.lower().endswith(_FOLLOWABLE_SUFFIXES):
            continue
        spelled = unescape_target(raw)
        raw_target = (spelled if os.path.isabs(spelled)
                      else os.path.join(base, spelled))
        target = canonical(raw_target)

        if is_baseline(target):
            # Recognised BEFORE the boundary test, and deliberately so.
            # A pointer left behind by a moved `template_dir` is precisely a
            # baseline sitting outside the current one, so testing the boundary
            # first buries the single most important verdict this module makes
            # ("stale") under "could not determine" — which is exactly what it
            # did to the first real project that had one. Recognising a NAME is
            # not reading a FILE, and only the latter is what the boundary
            # exists to restrain.
            baselines.append((target, path, lineno))
            if _within(target, boundaries):
                # Queued too: a baseline may include something itself, and its
                # bytes count toward the weight the panel reports.
                queued.append((target, raw_target, depth + 1,
                               ancestors | {path}))
            continue

        if not _within(target, boundaries):
            escapes.append("%s:%d -> outside project and template dir"
                           % (label, lineno))
            continue
        queued.append((target, raw_target, depth + 1, ancestors | {path}))

    return baselines, escapes, queued


def walk_chain(project_root: str, template_dir: str,
               start: str = CLAUDE_MD) -> ChainScan:
    """Follow `@` includes from *start*, bounded. Pure apart from reading files.

    The permitted boundary is the project subtree **or the configured template
    directory**, and the second half is load-bearing rather than a convenience:
    the shared baseline lives in `template_dir` by design, so a boundary of
    "inside the project" alone would refuse to follow the one include this
    whole feature is about, and classify every correctly wired project as
    unknown.

    Absolute targets are accepted syntactically; whether they are FOLLOWED is
    decided here.
    """
    root = canonical(project_root)
    boundaries = (root,)
    if template_dir:
        boundaries = (root, canonical(template_dir))

    files: list = []
    baselines: list = []
    unreadable: list = []
    escapes: list = []
    cycles: list = []
    indented: list = []
    unclosed: list = []
    bounds = ""
    total = 0
    seen: set = set()

    def label(path: str) -> str:
        try:
            return os.path.relpath(path, root)
        except ValueError:
            return path

    # (path, depth, ancestors). Breadth-first so the shallowest resolution is
    # found before a bound can be hit on a deep unrelated branch.
    #
    # Ancestry is carried per entry rather than relying on the visited set,
    # because those answer different questions. A file included twice is a
    # DUPLICATE — a diamond, which real instruction files contain — while a
    # cycle is a back edge to something already on this route. Conflating them
    # reported "include cycle" for an ordinary repeated include and pushed the
    # project to UNKNOWN, hiding the duplicate advisory that was the real find.
    first = os.path.join(project_root, start)
    queue = [(canonical(first), first, 0, frozenset())]
    shown: list = []

    while queue:
        path, as_written, depth, ancestors = queue.pop(0)
        if path in ancestors:
            cycles.append(label(path))
            continue
        if path in seen:
            # Already scanned by another route; its directives, and any
            # baseline among them, were recorded at that visit.
            continue
        seen.add(path)

        if depth > MAX_DEPTH:
            bounds = "include depth over %d at %s" % (MAX_DEPTH, label(path))
            break
        if len(files) >= MAX_FILES:
            bounds = "more than %d included files" % MAX_FILES
            break

        if not os.path.isfile(path):
            # A missing CLAUDE.md is an absence, not a read failure: the file
            # simply is not there, which is a fact we know rather than one we
            # could not obtain. Anything else missing was named by a directive
            # that we therefore cannot resolve.
            if depth > 0:
                unreadable.append("%s (named but missing)" % label(path))
            continue

        text = _read(path)
        if text is None:
            unreadable.append("%s (unreadable)" % label(path))
            continue

        total += len(text.encode("utf-8", errors="replace"))
        if total > MAX_BYTES:
            bounds = "included text over %d bytes" % MAX_BYTES
            break

        shown.append((path, as_written))
        directives, soft, fence_open = scan_text(text)
        files.append(FileScan(path=path, size=len(text), directives=directives,
                              indented=soft, unclosed_fence=fence_open))
        if fence_open:
            unclosed.append(label(path))
        for lineno, raw in soft:
            indented.append("%s:%d" % (label(path), lineno))

        found, escaped, queued = _expand_directives(
            path, as_written, depth, ancestors, directives, boundaries,
            label(path))
        baselines.extend(found)
        escapes.extend(escaped)
        queue.extend(queued)

    return ChainScan(
        files=tuple(files), total_bytes=total, baselines=tuple(baselines),
        unreadable=tuple(unreadable), escapes=tuple(escapes),
        cycles=tuple(cycles), indented=tuple(indented), bounds=bounds,
        unclosed_fences=tuple(unclosed), display=tuple(shown),
    )


# ── one project's verdict ────────────────────────────────────────────────

@dataclass(frozen=True)
class ProjectInstructions:
    """One project's instruction posture. Carries evidence, not just a badge."""

    #: Canonical identity for aggregation; `display_root` is for showing.
    root: str
    display_root: str
    name: str

    carriage: str = CARRIAGE_NONE
    reach: str = REACH_ABSENT

    #: Kept BEFORE the verdict collapses them, so a finding can say which
    #: baseline was reached and where the stale line lives.
    #: The file the chain actually reaches, as a canonical PATH. For a
    #: localized project that is its own `project-baseline.md`.
    reached_baseline: str = ""
    reached_is_current: bool = False
    stale_at: str = ""
    #: CONTENT identity with the configured template, separately from the path.
    baseline_match: str = ""
    #: Which installation's baseline this project follows, for ownership: the
    #: reached path when it is the template itself, the configured template
    #: when a copy matches it, and "" when neither can be said.
    baseline_source: str = ""
    #: `baseline_copy` state of `<root>/project-baseline.md` when reached.
    copy_state: str = ""
    #: The sha the copy's header records, so a refresh can compare first.
    copy_recorded_sha: str = ""
    delivery: str = ""
    #: (relative file, lineno, raw target) for every baseline directive in
    #: CLAUDE.md and BASIC_INSTRUCTIONS.md, reached or not. What a localize
    #: step repairs, captured so the writer can compare before it applies.
    baseline_directives: tuple = ()

    weight_bytes: int = 0
    advisories: tuple = ()
    #: Why the answer is UNKNOWN, or any other note worth showing on the row.
    detail: str = ""
    chain: tuple = ()
    has_basic: bool = False
    basic_carries: bool = False
    #: Deliberately kept IN the population rather than filtered out of it.
    #: Hiding an excluded project would move the denominator silently, and a
    #: vendor clone that is not wired is a fact worth showing, not an absence.
    excluded: bool = False
    exclude_reason: str = ""

    @property
    def estimated_tokens(self) -> int:
        """A byte/4 ESTIMATE, never tokenizer output.

        Reported alongside `weight_bytes`, never added to it — they are the
        same content in two units. Thresholds are evaluated on bytes so no
        second approximate threshold rides on this.
        """
        return self.weight_bytes // 4

    @property
    def repairable(self) -> bool:
        """May the panel offer a one-click fix on this row?

        Never on UNKNOWN — any button there is a guess. Never on a chain that
        already resolves through an undocumented topology: the only repair this
        module's writer knows how to make would add a SECOND path to a baseline
        that already arrives, which is a defect, not a fix.
        """
        if self.excluded:
            return False
        if self.reach == REACH_UNKNOWN or self.delivery == DELIVERY_UNKNOWN:
            return False
        if ADVISORY_NONSTANDARD_CHAIN in self.advisories:
            return False
        if ADVISORY_DUPLICATE_DIRECTIVE in self.advisories:
            return False
        if self.reach == REACH_RESOLVED:
            # Resolving is not loading: a chain that leaves the project is
            # dropped by Claude Code unless approved, and non-portable if it is.
            return self.delivery in (DELIVERY_EXTERNAL_APPROVED,
                                     DELIVERY_EXTERNAL_BLOCKED,
                                     DELIVERY_UNPARSEABLE)
        return self.reach in (REACH_ORPHANED, REACH_ABSENT, REACH_STALE)

    @property
    def healthy(self) -> bool:
        """The only state a panel may render green."""
        return (self.reach == REACH_RESOLVED
                and self.delivery == DELIVERY_IN_PROJECT)


def _documented_shape(scan: ChainScan, project_root: str,
                      baselines: tuple) -> bool:
    """Is the resolution the shape Retrofit knows how to write?

    `CLAUDE.md` -> `BASIC_INSTRUCTIONS.md` -> baseline, or `CLAUDE.md` ->
    baseline directly. Anything else resolves fine and is left alone.
    *baselines* is every path that counts as the configured baseline: the
    template itself, and the project's copy when that copy is current.
    """
    root = canonical(project_root)
    claude = canonical(os.path.join(root, CLAUDE_MD))
    basic = canonical(os.path.join(root, BASIC_MD))
    for target, source, _lineno in scan.baselines:
        if target in baselines and source in (claude, basic):
            return True
    return False


def _target_of(raw: str, base: str) -> str:
    spelled = unescape_target(raw)
    return canonical(spelled if os.path.isabs(spelled)
                     else os.path.join(base, spelled))


def _carriage_of(scan: ChainScan, standalone: "FileScan | None",
                 matching: tuple) -> "tuple[str, str]":
    """What the project's files DECLARE, reached or not. Returns (carriage, stale_at).

    Deliberately separate from the reach decision. Carriage is a statement
    about the files; reach is a statement about the chain, and the whole
    module exists because those two were being answered as one.
    """
    carried_current = any(b[0] in matching for b in scan.baselines)
    carried_other = any(b[0] not in matching for b in scan.baselines)
    stale_at = ""

    if standalone is not None:
        base = os.path.dirname(standalone.path)
        for lineno, raw in standalone.directives:
            if not raw.lower().endswith(_FOLLOWABLE_SUFFIXES):
                continue
            target = _target_of(raw, base)
            if not is_baseline(target):
                continue
            if target in matching:
                carried_current = True
            else:
                carried_other = True
                stale_at = "%s:%d" % (BASIC_MD, lineno)

    if carried_current:
        return CARRIAGE_CURRENT, stale_at
    if carried_other:
        return CARRIAGE_STALE, stale_at
    return CARRIAGE_NONE, stale_at


def _duplicate_basic_directives(scan: ChainScan, project_root: str) -> bool:
    """More than one `@BASIC_INSTRUCTIONS.md` in `CLAUDE.md`."""
    claude_path = canonical(os.path.join(project_root, CLAUDE_MD))
    basic_path = canonical(os.path.join(project_root, BASIC_MD))
    for scanned in scan.files:
        if scanned.path != claude_path:
            continue
        hits = 0
        for _lineno, raw in scanned.directives:
            target = canonical(raw if os.path.isabs(raw)
                               else os.path.join(project_root, raw))
            if target == basic_path:
                hits += 1
        if hits > 1:
            return True
    return False


def _text_advisories(scan: ChainScan, texts: dict, current_baseline: str,
                     project_root: str) -> list:
    """Advisories that come from reading the prose. All heuristics."""
    found: list = []
    baseline_text = texts.get(current_baseline, "")
    # The project's own copy restates the baseline by construction.
    skip = (current_baseline,
            canonical(os.path.join(project_root, BASELINE_BASENAME)))

    if baseline_text:
        for scanned in scan.files:
            if scanned.path in skip:
                continue
            body = texts.get(scanned.path, "")
            if body and _restates_baseline(body, baseline_text):
                found.append(ADVISORY_DUPLICATE_CONTENT)
                break

    # An unreachable CLAUDE.md still gets read for contradictions: a project
    # instructing Grep-first matters most precisely when the shared rule is
    # NOT arriving to argue with it.
    bodies = [texts.get(s.path, "") for s in scan.files]
    if not scan.files:
        bodies = [texts.get(canonical(os.path.join(project_root, CLAUDE_MD)), "")]
    for body in bodies:
        if body and any(p.search(body) for p in _CONTRADICT_PATTERNS):
            found.append(ADVISORY_CONTRADICTS)
            break

    if texts.get("__placeholder__"):
        found.append(ADVISORY_PLACEHOLDER)
    return found


_COPY_DETAIL = {
    bc.COPY_OUTDATED: "project-baseline.md is an outdated copy of the template",
    bc.COPY_EDITED: "project-baseline.md was edited by hand since it was copied",
    bc.COPY_INVALID: "project-baseline.md has a damaged Manager header",
    bc.COPY_UNREADABLE: "project-baseline.md could not be read",
}


def _copy_verdict(copy_state: str, copy_path: str, evidence: dict,
                  carriage: str, advisories: list) -> "tuple[str, str, dict]":
    """The verdict for a reached Manager copy that is NOT current."""
    evidence = dict(evidence, reached_baseline=copy_path,
                    detail=_COPY_DETAIL.get(copy_state, copy_state),
                    advisories=tuple(advisories))
    if copy_state in (bc.COPY_OUTDATED, bc.COPY_EDITED):
        return REACH_STALE, carriage, dict(evidence,
                                           baseline_match=CONTENT_MISMATCH)
    return REACH_UNKNOWN, carriage, dict(evidence, baseline_match=CONTENT_UNKNOWN)


def _other_baseline_verdict(scan: ChainScan, reached: tuple, project_root: str,
                            current_baseline: str, copy_path: str,
                            is_copy: bool, copy_state: str, evidence: dict,
                            carriage: str,
                            advisories: list) -> "tuple[str, str, dict]":
    """The verdict when the chain reaches a baseline other than the configured
    one (or a copy that does not match it)."""
    target, source, lineno = reached
    evidence = dict(evidence, stale_at="%s:%d" % (
        os.path.relpath(scan.shown(source), project_root), lineno))
    if is_copy and target == copy_path:
        return _copy_verdict(copy_state, copy_path, evidence, carriage,
                             advisories)
    evidence.update(reached_baseline=target, baseline_match=CONTENT_UNKNOWN,
                    detail="reaches %s; configured baseline is %s" % (
                        target, current_baseline))
    if target == copy_path:
        # A file of the copy's name the Manager did not write: not an
        # installation's baseline, so it owns nothing.
        evidence["detail"] = "project-baseline.md here was not written by the Manager"
    else:
        evidence["baseline_source"] = target
    return REACH_STALE, carriage, dict(evidence, advisories=tuple(advisories))


def classify(scan: ChainScan, standalone: "FileScan | None",
             current_baseline: "str | None", project_root: str,
             texts: "dict | None" = None,
             copy_state: str = "") -> "tuple[str, str, dict]":
    """Decide `(reach, carriage, evidence)` from already-gathered facts. Pure.

    ### Why positive evidence outranks incompleteness

    An unreadable branch makes a NEGATIVE verdict unsafe — we must not say
    "absent" about a place we could not look. It does not make a POSITIVE one
    unsafe: if the chain demonstrably arrives at the configured baseline, that
    is direct evidence, and downgrading it to unknown would under-report a
    project that is genuinely fine and hide it from the fleet count.

    So `UNKNOWN` preempts `STALE` / `ORPHANED` / `ABSENT`, and never
    `RESOLVED`. The same reasoning governs indented directives: they can only
    ever ADD a path, so they matter only when nothing was found.

    ### A Manager copy is judged by content

    *copy_state* is `baseline_copy`'s verdict on `<root>/project-baseline.md`.
    A CURRENT copy counts as the configured baseline for reach, while
    `reached_baseline` still names the copy's own path and `baseline_match`
    says the match is by content. An unmanaged file of that name is not a copy
    and keeps the ordinary "a different baseline" verdict.
    """
    texts = texts or {}
    evidence: dict = {"reached_baseline": "", "reached_is_current": False,
                      "stale_at": "", "detail": "", "baseline_match": "",
                      "baseline_source": ""}

    if current_baseline is None:
        return REACH_UNKNOWN, CARRIAGE_UNKNOWN, dict(
            evidence, detail="configured baseline include line is not a valid "
                             "include directive", advisories=())

    copy_path = canonical(os.path.join(project_root, BASELINE_BASENAME))
    is_copy = (copy_path != current_baseline and copy_state not in
               ("", bc.COPY_ABSENT, bc.COPY_UNMANAGED))
    matching = (current_baseline,)
    if is_copy and copy_state == bc.COPY_CURRENT:
        matching = (current_baseline, copy_path)

    carriage, carried_stale_at = _carriage_of(scan, standalone, matching)
    evidence["stale_at"] = carried_stale_at

    advisories = _text_advisories(scan, texts, current_baseline, project_root)
    if _duplicate_basic_directives(scan, project_root):
        advisories.append(ADVISORY_DUPLICATE_DIRECTIVE)

    reached_current = [b for b in scan.baselines if b[0] in matching]
    reached_other = [b for b in scan.baselines if b[0] not in matching]

    if reached_current:
        evidence.update(reached_baseline=reached_current[0][0],
                        reached_is_current=True, baseline_match=CONTENT_MATCH,
                        baseline_source=current_baseline)
        if len(reached_current) > 1:
            advisories.append(ADVISORY_DOUBLE_LOAD)
        if not _documented_shape(scan, project_root, matching):
            advisories.append(ADVISORY_NONSTANDARD_CHAIN)
        return REACH_RESOLVED, carriage, dict(evidence,
                                              advisories=tuple(advisories))

    # Nothing arrived. Only now can incompleteness change the answer.
    if scan.incomplete:
        if scan.indented:
            advisories.append(ADVISORY_INDENTED_DIRECTIVE)
        evidence["detail"] = _unknown_reason(scan)
        return REACH_UNKNOWN, carriage, dict(evidence,
                                             advisories=tuple(advisories))

    if reached_other:
        return _other_baseline_verdict(scan, reached_other[0], project_root,
                                       current_baseline, copy_path, is_copy,
                                       copy_state, evidence, carriage,
                                       advisories)

    if carriage != CARRIAGE_NONE:
        evidence["detail"] = (
            "%s carries a baseline include, but nothing links it from %s"
            % (BASIC_MD, CLAUDE_MD))
        return REACH_ORPHANED, carriage, dict(evidence,
                                              advisories=tuple(advisories))

    return REACH_ABSENT, carriage, dict(evidence, advisories=tuple(advisories))


def _unknown_reason(scan: ChainScan) -> str:
    """One concise sentence naming WHY, so the row is actionable.

    A red row with no button and no explanation is not a finding.
    """
    if scan.bounds:
        return scan.bounds
    if scan.cycles:
        return "include cycle at %s" % scan.cycles[0]
    if scan.escapes:
        return "include target outside project and template directory (%s)" % (
            scan.escapes[0],)
    if scan.unreadable:
        return "could not read %s" % scan.unreadable[0]
    if scan.indented:
        return ("indented include directive at %s — cannot tell whether it is "
                "followed" % scan.indented[0])
    return "chain could not be established"


def _restates_baseline(body: str, baseline_text: str) -> bool:
    """Does *body* inline a chunk of the baseline verbatim? Heuristic.

    Compares `##` headings rather than prose: a hand-copied section keeps its
    heading, and headings are cheap to compare and unlikely to collide by
    accident. Two or more shared headings is the signal — one can legitimately
    be a project writing about the same topic.
    """
    def headings(text: str) -> set:
        return {line.strip().lower() for line in text.splitlines()
                if line.startswith("## ")}

    shared = headings(body) & headings(baseline_text)
    return len(shared) >= 2


# ── the fleet ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FleetInstructions:
    """Every discovered project's posture, plus how it was obtained."""

    projects: tuple = ()
    #: Canonical path of the configured baseline, or "" when unusable.
    baseline: str = ""
    baseline_ok: bool = True

    def counts(self) -> dict:
        """Distribution across every reach state.

        Returned whole rather than as one "resolved" number: a move from 5 to
        16 that quietly produced a new UNKNOWN is exactly what a single count
        would hide.
        """
        out = {REACH_RESOLVED: 0, REACH_ORPHANED: 0, REACH_STALE: 0,
               REACH_ABSENT: 0, REACH_UNKNOWN: 0}
        for project in self.projects:
            out[project.reach] = out.get(project.reach, 0) + 1
        return out

    @property
    def total(self) -> int:
        """The denominator, from live discovery. Never a hardcoded constant."""
        return len(self.projects)

    def over(self, threshold_bytes: int) -> tuple:
        return tuple(p for p in self.projects
                     if p.weight_bytes > threshold_bytes)


def excluded_roots(cfg) -> tuple:
    """Canonical project roots the user has excluded from wiring.

    Upstream clones are the case this exists for. Two of them were wired by a
    bulk run before anyone noticed they were somebody else's repository, and
    reverting that by hand does not survive the next run — so the exclusion has
    to live somewhere the writer consults, not in a person's memory.
    """
    raw = getattr(cfg, "raw", None) or {}
    return tuple(canonical(str(entry))
                 for entry in (raw.get("instructions_skip_paths") or [])
                 if str(entry).strip())


def delivery_of(scan: ChainScan, project_root: str,
                claude_projects) -> "tuple[str, str]":
    """`(delivery, detail)`: will Claude Code load this chain? Observed facts.

    Three checks, strongest first:

    1. **Spelling.** A directive on the chain that Claude Code does not parse
       (`claude_code_parses`) loads nothing, wherever it points.
    2. **Containment,** by `realpath` - unlike `canonical`, which avoids it for
       identity - because the junction canary showed Claude Code resolving a
       link to its real location before deciding it is outside the project.
    3. **Approval** of external includes, read off the project's record.

    *claude_projects* is the `projects` map, or None when `~/.claude.json`
    could not be read. An unreadable config or a path that cannot be resolved
    is UNKNOWN, never blocked.
    """
    reached = set(scan.paths) | {b[0] for b in scan.baselines}
    for scanned in scan.files:
        base = os.path.dirname(scanned.path)
        for lineno, raw in scanned.directives:
            if (raw.lower().endswith(_FOLLOWABLE_SUFFIXES)
                    and _target_of(raw, base) in reached
                    and not claude_code_parses(raw)):
                return DELIVERY_UNPARSEABLE, (
                    "%s:%d is spelled in a way Claude Code does not load "
                    "(a backslash, or an unescaped space)"
                    % (os.path.basename(scan.shown(scanned.path)), lineno))
    try:
        root = canonical(os.path.realpath(project_root))
        outside = [path for path in sorted(reached)
                   if not _within(canonical(os.path.realpath(path)), (root,))]
    except (OSError, ValueError):
        return DELIVERY_UNKNOWN, "could not resolve the chain's real paths"
    if not outside:
        return DELIVERY_IN_PROJECT, ""
    if claude_projects is None:
        return DELIVERY_UNKNOWN, "~/.claude.json could not be read"
    approved = external_includes_approved(
        resolve_project_record(project_root, claude_projects))
    if approved is None:
        return DELIVERY_UNKNOWN, "~/.claude.json could not be read"
    if approved:
        return DELIVERY_EXTERNAL_APPROVED, (
            "includes %s from outside the project (approved on this machine "
            "only)" % outside[0])
    return DELIVERY_EXTERNAL_BLOCKED, (
        "includes %s from outside the project, which Claude Code does not load "
        "without approval" % outside[0])


def read_posture(roots: list, cfg) -> FleetInstructions:
    """Scan every discovered project. The only function here that does IO."""
    baseline = parse_baseline_target(getattr(cfg, "baseline_include_line", ""))
    template_dir = getattr(cfg, "template_dir", "") or ""

    template_text = ""
    if baseline:
        template_text = _read(baseline) or ""

    placeholder_text = ""
    template_file = getattr(cfg, "basic_instructions_template", "") or ""
    if template_file:
        placeholder_text = _read(template_file) or ""

    claude_projects = read_claude_projects_strict()
    skips = excluded_roots(cfg)
    seen: set = set()
    projects: list = []
    for entry in find_projects(roots):
        path = entry["path"]
        key = normalize_project_key(path)
        if key in seen:
            continue
        seen.add(key)
        project = read_project(path, entry.get("name") or
                               os.path.basename(path), template_dir,
                               baseline, template_text, placeholder_text,
                               claude_projects)
        if canonical(path) in skips:
            project = replace(project, excluded=True,
                              exclude_reason="excluded in manager-config.json "
                                             "(instructions_skip_paths)")
        projects.append(project)

    return FleetInstructions(projects=tuple(projects), baseline=baseline or "",
                             baseline_ok=baseline is not None)


def _baseline_directives(project_root: str, texts_by_name: dict) -> tuple:
    """(file, lineno, raw) for every baseline directive in the two wired files."""
    found = []
    for name in (CLAUDE_MD, BASIC_MD):
        text = texts_by_name.get(name)
        if not text:
            continue
        directives, _soft, _fence = scan_text(text)
        for lineno, raw in directives:
            if is_baseline(unescape_target(raw)):
                found.append((name, lineno, raw))
    return tuple(found)


def _copy_state_of(project_root: str, baseline_text: str) -> "tuple[str, str]":
    """(copy state, recorded sha) for `<root>/project-baseline.md`."""
    facts = bc.read_copy(os.path.join(project_root, BASELINE_BASENAME))
    if facts.exists and facts.header == bc.HEADER_OK and not baseline_text:
        # Current or outdated cannot be decided against a template we could not
        # read, and guessing "outdated" would offer to overwrite it.
        return bc.COPY_UNREADABLE, facts.recorded_sha
    return (bc.classify_copy(facts, bc.content_sha(baseline_text)),
            facts.recorded_sha)


def _gather_texts(scan: ChainScan, project_root: str, baseline: "str | None",
                  baseline_text: str) -> dict:
    """Canonical path -> text, for the content advisories."""
    texts: dict = {}
    for scanned in scan.files:
        texts[scanned.path] = _read(scanned.path) or ""
    if not scan.files:
        claude = canonical(os.path.join(project_root, CLAUDE_MD))
        body = _read(claude)
        if body is not None:
            texts[claude] = body
    if baseline and baseline_text:
        texts[baseline] = baseline_text
    return texts


def _read_standalone(basic_path: str, placeholder_text: str,
                     texts: dict) -> "FileScan | None":
    """BASIC_INSTRUCTIONS.md scanned on its own, reached or not."""
    basic_text = _read(basic_path)
    if basic_text is None:
        return None
    if placeholder_text and basic_text.strip() == placeholder_text.strip():
        texts["__placeholder__"] = True
    directives, soft, fence = scan_text(basic_text)
    return FileScan(path=basic_path, size=len(basic_text),
                    directives=directives, indented=soft, unclosed_fence=fence)


def read_project(project_root: str, name: str, template_dir: str,
                 baseline: "str | None", baseline_text: str = "",
                 placeholder_text: str = "",
                 claude_projects=_LOAD) -> ProjectInstructions:
    """One project's posture. Separated so tests can drive it directly.

    *claude_projects* is the `~/.claude.json` projects map (None: unreadable);
    omitted, it is read here.
    """
    if baseline and not baseline_text:
        baseline_text = _read(baseline) or ""
    if claude_projects is _LOAD:
        claude_projects = read_claude_projects_strict()
    scan = walk_chain(project_root, template_dir)
    texts = _gather_texts(scan, project_root, baseline, baseline_text)

    basic_path = canonical(os.path.join(project_root, BASIC_MD))
    has_basic = os.path.isfile(basic_path)
    standalone = _read_standalone(basic_path, placeholder_text, texts) \
        if has_basic else None

    copy_state, recorded_sha = _copy_state_of(project_root, baseline_text)
    reach, carriage, evidence = classify(scan, standalone, baseline,
                                         project_root, texts, copy_state)
    delivery, delivery_detail = delivery_of(scan, project_root, claude_projects)
    detail = evidence.get("detail", "")
    if delivery != DELIVERY_IN_PROJECT and delivery_detail:
        detail = "; ".join(part for part in (detail, delivery_detail) if part)

    by_name = {CLAUDE_MD: texts.get(canonical(os.path.join(project_root,
                                                           CLAUDE_MD))),
               BASIC_MD: _read(basic_path) if has_basic else None}

    return ProjectInstructions(
        root=normalize_project_key(project_root),
        display_root=project_root,
        name=name,
        carriage=carriage,
        reach=reach,
        reached_baseline=evidence.get("reached_baseline", ""),
        reached_is_current=evidence.get("reached_is_current", False),
        stale_at=evidence.get("stale_at", ""),
        baseline_match=evidence.get("baseline_match", ""),
        baseline_source=evidence.get("baseline_source", ""),
        copy_state=copy_state,
        copy_recorded_sha=recorded_sha,
        delivery=delivery,
        baseline_directives=_baseline_directives(project_root, by_name),
        weight_bytes=scan.total_bytes,
        advisories=evidence.get("advisories", ()),
        detail=detail,
        chain=scan.paths,
        has_basic=has_basic,
        basic_carries=bool(standalone and any(
            is_baseline(canonical(os.path.join(project_root, raw)))
            for _n, raw in standalone.directives)),
    )
