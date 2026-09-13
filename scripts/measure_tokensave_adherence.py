"""Did the read nudge change anything? Count, do not guess.

Reads the Claude Code transcripts under ``~/.claude/projects`` and reports, per
project and per PHASE, every population of read-like call beside the
``tokensave_read`` calls that actually happened.

THE DENOMINATOR IS THE POINT, so three rules shape this whole script.

**Eligibility is the hook's own predicate, imported.** ``helpers.read_nudge``
renders the hook as an importable module and this imports it -- there is no
second copy of the extension allowlist, the slice test or the size gate to drift
from the deployed one. A ratio computed by a near-copy of the predicate is the
number most worth getting wrong quietly.

**No population is left out, and none is summed into a ratio.** The first
version printed "tokensave_read per eligible whole-file read: 709.7%" -- over
31 eligible whole-file reads, while 279 Read SLICES and every shell page
(`sed -n`, `cat`) sat outside the denominator. Measured 2026-09-13 on one
OpenChem session: tokensave was used throughout exploration, and once
implementation began the reading moved to exactly those uncounted calls. So
each population is its own column, split by phase (before and after a session's
first Edit/Write), and there is no headline ratio to hide behind.

**Completeness is a field, never an inference.** ``transcripts_failed`` and
``results_unrecoverable`` are reported beside the counts, because a parse
failure rendering as ``tokensave_read = 0`` would read exactly like the problem
this feature was built to fix.

It is observational telemetry, not an experiment. It can support "the reading
pattern changed after the nudge was installed"; it cannot support "this read
caused that ``tokensave_read``".

    python scripts/measure_tokensave_adherence.py
    python scripts/measure_tokensave_adherence.py --since 2026-09-04 --until 2026-09-11
    python scripts/measure_tokensave_adherence.py --json > before.json
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from helpers.read_nudge import predicate_module      # noqa: E402

NUDGE = predicate_module()

PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

#: A read whose tool_result never arrived -- interrupted, or truncated out of
#: the transcript. Structurally different from every verdict the predicate can
#: return, so it gets its own bucket rather than being folded into `small`.
UNRECOVERABLE = "results_unrecoverable"

EXPLORE = "explore"
IMPLEMENT = "implement"
PHASES = (EXPLORE, IMPLEMENT)

#: Tools that write a file. The first one in a transcript starts IMPLEMENT.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

#: A slice counts as "for an Edit" when an Edit/Write of the same file follows
#: within this many tool calls. The Edit tool requires a Read first, so those
#: slices are not locating -- and must not be counted as if they were.
EDIT_LOOKAHEAD = 15

#: Populations, in column order.
WHOLE = "whole"
ELIGIBLE_COL = "eligible"
SLICE_FOR_EDIT = "slice_for_edit"
SLICE_LOCATING = "slice_locating"
SHELL_PAGE = "shell_page"
TS_READ = "ts_read"
COLUMNS = (WHOLE, ELIGIBLE_COL, SLICE_FOR_EDIT, SLICE_LOCATING, SHELL_PAGE,
           TS_READ)

#: A newline separates commands too: multi-line shell calls are common here.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_PAGERS = frozenset({"cat", "head", "tail", "get-content", "gc", "type"})


def shell_page_target(command):
    """The file a shell command pages through, or "". Pure.

    Recognises `sed -n <range>p FILE`, `cat FILE`, `head`/`tail [...] FILE` and
    `Get-Content FILE`, only for an extension the hook would advise on. A
    heredoc (`cat > x <<EOF`), `sed -i`, and a pager reading a pipe (no file
    argument) are not pages. Deliberately narrow: `grep -n PATTERN FILE` is a
    search, which tokensave's own PreToolUse hook already polices.
    """
    command = command or ""
    # A heredoc's BODY is data, often Python, and must not be read as commands.
    if "<<" in command:
        command = command.split("<<", 1)[0]
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            continue
        target = _segment_target(words)
        if target:
            return target
    return ""


def _segment_target(words):
    if not words:
        return ""
    verb = words[0].lower()
    if any(w.startswith((">", "<")) or w in (">", ">>", "<<") for w in words):
        return ""
    if verb == "sed":
        if "-n" not in words or any(w.startswith("-i") for w in words):
            return ""
        files = [w for w in words[1:] if not w.startswith("-")
                 and not re.fullmatch(r"\d*,?\$?\d*p", w)]
    elif verb in _PAGERS:
        files = [w for w in words[1:] if not w.startswith("-")
                 and not re.fullmatch(r"\d+", w)]
    else:
        return ""
    for word in files:
        if os.path.splitext(word)[1].lower() in NUDGE.NUDGE_EXTENSIONS:
            return word
    return ""


def _parse_timestamp(text):
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _blocks(record):
    content = (record.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _serialised_bytes(body):
    try:
        text = body if isinstance(body, str) else json.dumps(body)
    except (TypeError, ValueError):
        return None
    return len(text.encode("utf-8", "replace"))


def _same_file(path):
    return os.path.normcase(os.path.normpath(path or ""))


class ProjectTally:
    """One project's counts. Every field is reported; none is derived."""

    def __init__(self, name):
        self.name = name
        self.verdicts = collections.Counter()
        self.tools = collections.Counter()
        self.phases = {phase: collections.Counter() for phase in PHASES}
        self.unrecoverable = 0

    @property
    def whole_file_reads(self):
        """Reads that were not slices and not a foreign tool."""
        return sum(count for verdict, count in self.verdicts.items()
                   if verdict not in (NUDGE.SLICED, NUDGE.NOT_READ,
                                      NUDGE.NO_PATH))

    @property
    def eligible(self):
        return self.verdicts[NUDGE.ELIGIBLE]

    @property
    def tokensave_reads(self):
        return self.tools["tokensave_read"]

    @property
    def tokensave_calls(self):
        return sum(self.tools.values())

    @property
    def any_reading(self):
        return any(sum(c.values()) for c in self.phases.values())


def scan(since, until, projects_dir=PROJECTS_DIR):
    """`(tallies, meta)`. Never raises on one bad transcript."""
    tallies = {}
    meta = {"transcripts_scanned": 0, "transcripts_failed": 0,
            "records_unparsed": 0}
    if not os.path.isdir(projects_dir):
        meta["projects_dir_missing"] = True
        return tallies, meta

    for entry in sorted(os.listdir(projects_dir)):
        directory = os.path.join(projects_dir, entry)
        if not os.path.isdir(directory):
            continue
        for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
            try:
                _scan_one(path, since, until, tallies, meta)
                meta["transcripts_scanned"] += 1
            except OSError:
                meta["transcripts_failed"] += 1
    return tallies, meta


def _in_window(record, since, until):
    stamp = _parse_timestamp(record.get("timestamp"))
    if stamp is None:
        return True
    naive = stamp.replace(tzinfo=None)
    return not ((since and naive < since) or (until and naive >= until))


def _scan_one(path, since, until, tallies, meta):
    """Collect one transcript's calls in order, then classify them together.

    Two facts need the whole sequence: which phase a call is in, and whether a
    slice was followed by an Edit of the same file.
    """
    events = []
    by_id = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                meta["records_unparsed"] += 1
                continue
            if not _in_window(record, since, until):
                continue
            cwd = record.get("cwd") or ""
            project = cwd or os.path.basename(os.path.dirname(path))
            tally = tallies.setdefault(project, ProjectTally(project))
            for block in _blocks(record):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    event = {"name": block.get("name") or "", "tally": tally,
                             "input": block.get("input") or {}, "cwd": cwd,
                             "size": None}
                    events.append(event)
                    by_id[block.get("id")] = event
                elif block.get("type") == "tool_result":
                    event = by_id.get(block.get("tool_use_id"))
                    if event is not None:
                        event["size"] = _serialised_bytes(block.get("content"))
    _classify(events)


def _writes_the_project(event):
    """An Edit/Write inside the session's own directory.

    A plan-mode session writes its plan file under `~/.claude/plans` before a
    line of code changes; counting that as the start of implementation moved
    the phase boundary 17 minutes early on the session this was measured on.
    """
    if event["name"] not in WRITE_TOOLS:
        return False
    target = _same_file(event["input"].get("file_path")
                        or event["input"].get("notebook_path") or "")
    root = _same_file(event["cwd"])
    return bool(root) and (target == root or target.startswith(
        root.rstrip("\\/") + os.sep))


def _classify(events):
    phase = EXPLORE
    for index, event in enumerate(events):
        name, tally = event["name"], event["tally"]
        if name in WRITE_TOOLS:
            if _writes_the_project(event):
                phase = IMPLEMENT
            continue
        counts = tally.phases[phase]
        if name.startswith("mcp__tokensave__"):
            tool = name.replace("mcp__tokensave__", "")
            tally.tools[tool] += 1
            if tool == "tokensave_read":
                counts[TS_READ] += 1
        elif name in ("Bash", "PowerShell"):
            if shell_page_target(event["input"].get("command")):
                counts[SHELL_PAGE] += 1
        elif name == "Read":
            _classify_read(events, index, event, counts)


def _classify_read(events, index, event, counts):
    tally, tool_input = event["tally"], event["input"]
    if event["size"] is None:
        tally.unrecoverable += 1
        return
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    verdict = NUDGE.classify_read(
        "Read", tool_input, event["size"],
        NUDGE.project_with_tokensave_metadata(path, event["cwd"]))
    tally.verdicts[verdict] += 1
    if verdict == NUDGE.SLICED:
        target = _same_file(path)
        edited = any(later["name"] in WRITE_TOOLS and _same_file(
            later["input"].get("file_path") or later["input"].get("path"))
            == target for later in events[index + 1:index + 1 + EDIT_LOOKAHEAD])
        counts[SLICE_FOR_EDIT if edited else SLICE_LOCATING] += 1
    elif verdict not in (NUDGE.NOT_READ, NUDGE.NO_PATH):
        counts[WHOLE] += 1
        if verdict == NUDGE.ELIGIBLE:
            counts[ELIGIBLE_COL] += 1


def render(tallies, meta, since, until):
    lines = []
    window = "%s .. %s" % (since.date() if since else "(all)",
                           until.date() if until else "(now)")
    lines.append("Window: %s" % window)
    lines.append("Phase: 'implement' starts at a session's first Edit/Write. "
                 "A slice is 'for_edit' when an Edit of that file follows "
                 "within %d calls." % EDIT_LOOKAHEAD)
    lines.append("")
    # No ratio anywhere. Each population is its own column, because a single
    # number over one of them is how 279 uncounted slices read as 709.7%.
    header = ("%-26s %-9s %6s %8s %9s %9s %7s %8s" %
              ("project", "phase", "whole", "eligible", "slice_edt",
               "slice_loc", "sh_page", "ts_read"))
    lines.append(header)
    lines.append("-" * len(header))
    totals = {phase: collections.Counter() for phase in PHASES}
    unrecoverable = 0
    for _key, tally in sorted(tallies.items(), key=lambda kv: kv[0].lower()):
        unrecoverable += tally.unrecoverable
        if not tally.any_reading:
            continue
        for phase in PHASES:
            counts = tally.phases[phase]
            totals[phase].update(counts)
            lines.append(_row(os.path.basename(tally.name.rstrip("\\/"))[:26],
                              phase, counts))
    lines.append("-" * len(header))
    for phase in PHASES:
        lines.append(_row("TOTAL", phase, totals[phase]))
    lines.append("")
    complete = (meta["transcripts_failed"] == 0
                and meta["records_unparsed"] == 0 and unrecoverable == 0)
    lines.append("transcripts scanned %d, failed %d, records unparsed %d, "
                 "results unrecoverable %d"
                 % (meta["transcripts_scanned"], meta["transcripts_failed"],
                    meta["records_unparsed"], unrecoverable))
    lines.append("complete: %s%s" % (
        complete,
        "" if complete else "  <- the counts above are a FLOOR, not a total"))
    return "\n".join(lines)


def _row(label, phase, counts):
    return "%-26s %-9s %6d %8d %9d %9d %7d %8d" % (
        label, phase, counts[WHOLE], counts[ELIGIBLE_COL],
        counts[SLICE_FOR_EDIT], counts[SLICE_LOCATING], counts[SHELL_PAGE],
        counts[TS_READ])


def as_json(tallies, meta, since, until):
    """The full population, so a later run cannot mistake a collapsed
    denominator for an improvement."""
    projects = {}
    totals = {phase: collections.Counter() for phase in PHASES}
    unrecoverable = 0
    for key, tally in tallies.items():
        unrecoverable += tally.unrecoverable
        if not tally.any_reading and not tally.tokensave_calls:
            continue
        for phase in PHASES:
            totals[phase].update(tally.phases[phase])
        projects[key] = {
            "verdicts": dict(tally.verdicts),
            "tokensave_tools": dict(tally.tools),
            "phases": {phase: {col: tally.phases[phase][col]
                               for col in COLUMNS} for phase in PHASES},
            "results_unrecoverable": tally.unrecoverable,
        }
    return {
        "window": {"since": since.isoformat() if since else None,
                   "until": until.isoformat() if until else None},
        "min_response_bytes": NUDGE.MIN_RESPONSE_BYTES,
        "edit_lookahead": EDIT_LOOKAHEAD,
        "projects_discovered": len(projects),
        "totals": {phase: {col: totals[phase][col] for col in COLUMNS}
                   for phase in PHASES},
        "completeness": {
            "transcripts_scanned": meta["transcripts_scanned"],
            "transcripts_failed": meta["transcripts_failed"],
            "records_unparsed": meta["records_unparsed"],
            "results_unrecoverable": unrecoverable,
            "complete": (meta["transcripts_failed"] == 0
                         and meta["records_unparsed"] == 0
                         and unrecoverable == 0),
        },
        "projects": projects,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="ISO date, inclusive (default: 7 days ago)")
    parser.add_argument("--until", help="ISO date, exclusive (default: now)")
    parser.add_argument("--days", type=int, default=7,
                        help="window length when --since is absent (default 7)")
    parser.add_argument("--json", action="store_true",
                        help="emit the full population as JSON")
    args = parser.parse_args(argv)

    until = (datetime.datetime.fromisoformat(args.until) if args.until
             else datetime.datetime.now())
    since = (datetime.datetime.fromisoformat(args.since) if args.since
             else until - datetime.timedelta(days=args.days))

    tallies, meta = scan(since, until)
    if args.json:
        print(json.dumps(as_json(tallies, meta, since, until), indent=2))
    else:
        print(render(tallies, meta, since, until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
