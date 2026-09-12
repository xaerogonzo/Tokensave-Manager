"""Did the read nudge change anything? Count, do not guess.

Reads the Claude Code transcripts under ``~/.claude/projects`` and reports, per
project, how many whole-file ``Read`` calls were *eligible* for the advisory
against how many ``tokensave_read`` calls actually happened.

THE DENOMINATOR IS THE POINT, so two rules shape this whole script.

**Eligibility is the hook's own predicate, imported.** ``helpers.read_nudge``
renders the hook as an importable module and this imports it -- there is no
second copy of the extension allowlist, the slice test or the size gate to drift
from the deployed one. A ratio computed by a near-copy of the predicate is the
number most worth getting wrong quietly.

**Completeness is a field, never an inference.** ``transcripts_failed`` and
``results_unrecoverable`` are reported beside the counts, because a parse
failure rendering as ``tokensave_read = 0`` would read exactly like the problem
this feature was built to fix. *No data* and *no calls* are different facts and
only one of them is bad news.

It is observational telemetry, not an experiment. It can support "the adoption
ratio changed after the nudge was installed"; it cannot support "this read
caused that ``tokensave_read``", and the output should not be read that way.

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


class ProjectTally:
    """One project's counts. Every field is reported; none is derived."""

    def __init__(self, name):
        self.name = name
        self.verdicts = collections.Counter()
        self.tools = collections.Counter()
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


def _scan_one(path, since, until, tallies, meta):
    pending = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                meta["records_unparsed"] += 1
                continue

            stamp = _parse_timestamp(record.get("timestamp"))
            if stamp is not None:
                naive = stamp.replace(tzinfo=None)
                if since and naive < since:
                    continue
                if until and naive >= until:
                    continue

            cwd = record.get("cwd") or ""
            project = cwd or os.path.basename(os.path.dirname(path))
            tally = tallies.setdefault(project, ProjectTally(project))

            for block in _blocks(record):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    _note_tool_use(block, cwd, tally, pending)
                elif block.get("type") == "tool_result":
                    _note_tool_result(block, pending)

    # A Read whose result never arrived cannot be classified. Say so.
    for tally, _inp, _cwd in pending.values():
        tally.unrecoverable += 1


def _note_tool_use(block, cwd, tally, pending):
    name = block.get("name") or ""
    if name.startswith("mcp__tokensave__"):
        tally.tools[name.replace("mcp__tokensave__", "")] += 1
        return
    if name != "Read":
        return
    # Held until its result arrives: the size gate needs the payload, and the
    # payload is the thing the hook actually sees.
    pending[block.get("id")] = (tally, block.get("input") or {}, cwd)


def _note_tool_result(block, pending):
    held = pending.pop(block.get("tool_use_id"), None)
    if held is None:
        return
    tally, tool_input, cwd = held
    size = _serialised_bytes(block.get("content"))
    if size is None:
        tally.unrecoverable += 1
        return
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    verdict = NUDGE.classify_read(
        "Read", tool_input, size,
        NUDGE.project_with_tokensave_metadata(path, cwd))
    tally.verdicts[verdict] += 1


def render(tallies, meta, since, until):
    lines = []
    window = "%s .. %s" % (since.date() if since else "(all)",
                           until.date() if until else "(now)")
    lines.append("Window: %s" % window)
    lines.append("")
    # Every whole-file read appears in exactly one of excl/no_proj/small/
    # ELIGIBLE, so the row accounts for the whole population. A table that
    # showed only the eligible count would hide a denominator moving.
    header = ("%-30s %6s %7s %6s %8s %6s %9s %8s" %
              ("project", "whole", "sliced", "excl", "no_proj", "small",
               "ELIGIBLE", "ts_read"))
    lines.append(header)
    lines.append("-" * len(header))

    totals = collections.Counter()
    projects_with_eligible = 0
    projects_with_ts_read = 0
    for _key, tally in sorted(tallies.items(),
                              key=lambda kv: -kv[1].whole_file_reads):
        if not tally.whole_file_reads and not tally.tokensave_calls:
            continue
        if tally.eligible:
            projects_with_eligible += 1
        if tally.tokensave_reads:
            projects_with_ts_read += 1
        lines.append("%-30s %6d %7d %6d %8d %6d %9d %8d" % (
            os.path.basename(tally.name.rstrip("\\/"))[:30],
            tally.whole_file_reads,
            tally.verdicts[NUDGE.SLICED],
            tally.verdicts[NUDGE.EXCLUDED_EXTENSION],
            tally.verdicts[NUDGE.NO_METADATA_PROJECT],
            tally.verdicts[NUDGE.SMALL],
            tally.eligible,
            tally.tokensave_reads))
        totals["whole"] += tally.whole_file_reads
        totals["sliced"] += tally.verdicts[NUDGE.SLICED]
        totals["excl"] += tally.verdicts[NUDGE.EXCLUDED_EXTENSION]
        totals["no_proj"] += tally.verdicts[NUDGE.NO_METADATA_PROJECT]
        totals["small"] += tally.verdicts[NUDGE.SMALL]
        totals["eligible"] += tally.eligible
        totals["ts_read"] += tally.tokensave_reads
        totals["unrecoverable"] += tally.unrecoverable

    lines.append("-" * len(header))
    lines.append("%-30s %6d %7d %6d %8d %6d %9d %8d" % (
        "TOTAL", totals["whole"], totals["sliced"], totals["excl"],
        totals["no_proj"], totals["small"], totals["eligible"],
        totals["ts_read"]))
    lines.append("")

    ratio = (100.0 * totals["ts_read"] / totals["eligible"]
             if totals["eligible"] else 0.0)
    lines.append("tokensave_read per eligible whole-file read: %.1f%% "
                 "(%d / %d)" % (ratio, totals["ts_read"], totals["eligible"]))
    lines.append("projects with an eligible read: %d   with any tokensave_read: %d"
                 % (projects_with_eligible, projects_with_ts_read))
    lines.append("")
    complete = (meta["transcripts_failed"] == 0
                and meta["records_unparsed"] == 0
                and totals["unrecoverable"] == 0)
    lines.append("transcripts scanned %d, failed %d, records unparsed %d, "
                 "results unrecoverable %d"
                 % (meta["transcripts_scanned"], meta["transcripts_failed"],
                    meta["records_unparsed"], totals["unrecoverable"]))
    lines.append("complete: %s%s" % (
        complete,
        "" if complete else "  <- the counts above are a FLOOR, not a total"))
    return "\n".join(lines)


def as_json(tallies, meta, since, until):
    """The full population, so a later run cannot mistake a collapsed
    denominator for an improvement."""
    projects = {}
    for key, tally in tallies.items():
        if not tally.whole_file_reads and not tally.tokensave_calls:
            continue
        projects[key] = {
            "verdicts": dict(tally.verdicts),
            "tokensave_tools": dict(tally.tools),
            "whole_file_reads": tally.whole_file_reads,
            "eligible_whole_file_reads": tally.eligible,
            "results_unrecoverable": tally.unrecoverable,
        }
    totals = collections.Counter()
    for data in projects.values():
        totals["whole_file_reads"] += data["whole_file_reads"]
        totals["eligible_whole_file_reads"] += data["eligible_whole_file_reads"]
        totals["sliced_reads"] += data["verdicts"].get(NUDGE.SLICED, 0)
        totals["small_whole_file_reads"] += data["verdicts"].get(NUDGE.SMALL, 0)
        totals["excluded_extension_reads"] += data["verdicts"].get(
            NUDGE.EXCLUDED_EXTENSION, 0)
        totals["reads_outside_a_metadata_project"] += data["verdicts"].get(
            NUDGE.NO_METADATA_PROJECT, 0)
        totals["tokensave_read_calls"] += data["tokensave_tools"].get(
            "tokensave_read", 0)
        totals["results_unrecoverable"] += data["results_unrecoverable"]
    return {
        "window": {"since": since.isoformat() if since else None,
                   "until": until.isoformat() if until else None},
        "min_response_bytes": NUDGE.MIN_RESPONSE_BYTES,
        "projects_discovered": len(projects),
        "projects_with_eligible_reads": sum(
            1 for d in projects.values() if d["eligible_whole_file_reads"]),
        "projects_with_tokensave_read": sum(
            1 for d in projects.values()
            if d["tokensave_tools"].get("tokensave_read")),
        "totals": dict(totals),
        "completeness": {
            "transcripts_scanned": meta["transcripts_scanned"],
            "transcripts_failed": meta["transcripts_failed"],
            "records_unparsed": meta["records_unparsed"],
            "results_unrecoverable": totals["results_unrecoverable"],
            "complete": (meta["transcripts_failed"] == 0
                         and meta["records_unparsed"] == 0
                         and totals["results_unrecoverable"] == 0),
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
