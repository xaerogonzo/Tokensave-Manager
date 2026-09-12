"""What the human asked for in this window — gathered, not interpreted.

This is the sourcing half of Phase C. `doc_grounding.build_session_block`
renders what this returns; nothing here knows about prompts, models or drafts.

WHY IT EXISTS. Measured: Claude Code writes better commit messages and PR
drafts than Claude CLI, which beats Ollama — and since Claude CLI is the same
model family, the variable is **what each one knows**, not model size. A session
that wrote the code knows why; everything invoked afterwards gets a cold diff
and has to infer it. This supplies the missing premise, which turns the model's
job from "infer intent from a diff" into "summarise this explanation".

WHAT IT CLAIMS, PRECISELY. *What the human asked for in this window.* Never
*why this diff exists*. Work spans sessions and some of it is done by hand, so
these are fragments to attribute, not an explanation to assert — the same
discipline that makes `.tokensave/` presence mean
`project_with_tokensave_metadata` rather than `is_indexed`.

TWO SOURCES, ONE PRECEDENCE. The session note when a project has one, mined
transcripts when it does not. The note is better data — captured at session end
by a hook that knew exactly which session had just finished — but the fallback
means the feature works on the day it ships rather than after a week of notes
accumulate. Every fragment carries its **provenance**, so authoritative prompts
never blur into heuristic prose and the A/B can attribute any difference it
finds.

THE PROSE SOURCE IS OFF BY DEFAULT AND CAPPED IN NAMED NUMBERS. `enable_commit_
grounding` already defaults OFF because grounding *"ADDS prompt weight that
small local models handle poorly… qwen2.5-coder copies recent commit subjects
verbatim when overwhelmed"*. Measured here: one day of prompts is ~1,200 tokens
and the assistant prose beside it is ~20,200. Handing the second figure to a
local model is that measured failure, reproduced.
"""

from __future__ import annotations

import dataclasses
import datetime
import glob
import json
import os
import subprocess

from constants import CREATE_NO_WINDOW

#: Provenance. Kept distinct so nothing downstream can treat a heuristic
#: fragment as an authoritative one, and so the A/B can say which source moved
#: the result.
SOURCE_NOTE_PROMPT = "note:prompt"
SOURCE_TRANSCRIPT_PROMPT = "transcript:prompt"
SOURCE_TRANSCRIPT_PROSE = "transcript:prose"

#: Window fallbacks, each a distinct FACT rather than a shrug. A repository
#: with no commits is not the same as one whose history could not be read, and
#: neither is "the last commit was an hour ago".
WINDOW_SINCE_COMMIT = "since the last commit"
WINDOW_NO_COMMITS = "no commit reachable; using the maximum window"
WINDOW_UNREADABLE = "commit history unreadable; using the maximum window"

MAX_WINDOW_DAYS = 14
MAX_FRAGMENTS = 24
MAX_FRAGMENT_CHARS = 600
MAX_PROSE_FRAGMENTS = 6
MAX_TRANSCRIPTS = 8
MAX_TRANSCRIPT_BYTES = 8_000_000


@dataclasses.dataclass(frozen=True)
class Fragment:
    """One thing said, and where it came from."""

    text: str
    source: str


@dataclasses.dataclass(frozen=True)
class SessionContext:
    """`fragments` plus the facts needed to read them honestly."""

    fragments: tuple = ()
    window_reason: str = WINDOW_SINCE_COMMIT
    since: "datetime.datetime | None" = None
    #: Scanning stopped at a cap rather than running out of material. Reported
    #: rather than inferred: a partial gather that looks complete is the
    #: failure mode this whole codebase keeps writing rules about.
    truncated: bool = False
    #: True when a session note supplied the prompts. The fallback works, but
    #: the consumer should be able to say which it got.
    from_note: bool = False

    @property
    def prompts(self) -> tuple:
        return tuple(f for f in self.fragments if f.source != SOURCE_TRANSCRIPT_PROSE)

    @property
    def prose(self) -> tuple:
        return tuple(f for f in self.fragments if f.source == SOURCE_TRANSCRIPT_PROSE)


def last_commit_time(project_path: str, git_exe: str = "") -> "tuple":
    """`(when, reason)`. The degenerate cases are named, never defaulted to now.

    Treating "no commits" as "the last commit was this instant" would silently
    gather nothing on exactly the repositories where there is most to say.
    """
    if not project_path or not os.path.isdir(project_path):
        return None, WINDOW_UNREADABLE
    try:
        proc = subprocess.run(
            [git_exe or "git", "-C", project_path, "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None, WINDOW_UNREADABLE
    stamp = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stamp:
        # An empty repository, or a fresh checkout with no history: a real
        # state, and a different one from a git that would not run.
        return None, WINDOW_NO_COMMITS
    try:
        return (datetime.datetime.fromisoformat(stamp).replace(tzinfo=None),
                WINDOW_SINCE_COMMIT)
    except ValueError:
        return None, WINDOW_UNREADABLE


def _floor(since, reason) -> "tuple":
    """Resolve the window, applying the maximum when there is no commit."""
    cap = datetime.datetime.now() - datetime.timedelta(days=MAX_WINDOW_DAYS)
    if since is None:
        return cap, reason
    # Even a real commit date is clamped: a repository untouched for a year
    # would otherwise drag a year of sessions into one prompt.
    return max(since, cap), reason


def _parse_stamp(text: str):
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _from_note(project_path: str, since) -> list:
    """Fragments from the session note, newest sessions last. `[]` when absent."""
    from helpers.session_note import read_note

    note = read_note(project_path)
    if not note:
        return []
    out = []
    for session in note.get("sessions") or []:
        ended = _parse_stamp(session.get("ended_at") or "")
        if ended is not None and since is not None and ended < since:
            continue
        for said in session.get("prompts") or []:
            text = (said or "").strip()
            if text:
                out.append(Fragment(text[:MAX_FRAGMENT_CHARS],
                                    SOURCE_NOTE_PROMPT))
    return out


def _transcript_dir(project_path: str) -> str:
    from helpers.claude_tasks import _claude_projects_dir, encode_project_path

    base = _claude_projects_dir()
    if not base:
        return ""
    return os.path.join(base, encode_project_path(project_path))


def _from_transcripts(project_path: str, since, want_prose: bool) -> "tuple":
    """`(fragments, truncated)` — the fallback when no note exists.

    Bounded on transcripts scanned AND bytes read, for the reason the session
    note hook is: measured p99 is 112 MB and max 134 MB.
    """
    directory = _transcript_dir(project_path)
    if not directory or not os.path.isdir(directory):
        return (), False
    # Only transcripts that COULD hold an in-window record. A file last
    # written before the window cannot contain one, so skipping it is not a
    # truncation — and saying it is would print "this is a floor" on a gather
    # that is actually complete.
    candidates = []
    for path in glob.glob(os.path.join(directory, "*.jsonl")):
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        except OSError:
            continue
        if since is None or mtime >= since:
            candidates.append((mtime, path))
    candidates.sort(reverse=True)

    truncated = len(candidates) > MAX_TRANSCRIPTS
    prompts, prose, budget = [], [], MAX_TRANSCRIPT_BYTES

    for _mtime, path in candidates[:MAX_TRANSCRIPTS]:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > budget:
            truncated = True
            continue
        budget -= size
        truncated = _scan_one(path, since, want_prose, prompts, prose) or truncated

    prompts = prompts[-MAX_FRAGMENTS:]
    prose = prose[-MAX_PROSE_FRAGMENTS:]
    return tuple(prompts + prose), truncated


def _scan_one(path, since, want_prose, prompts, prose) -> bool:
    """Append fragments from one transcript. Returns whether anything was cut."""
    cut = False
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return False
    with handle:
        for line in handle:
            if '"user"' not in line and (not want_prose or '"assistant"' not in line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            when = _parse_stamp(record.get("timestamp") or "")
            if when is not None and since is not None and when < since:
                continue
            kind = record.get("type")
            content = (record.get("message") or {}).get("content")
            if kind == "user":
                said = _user_text(content)
                if said:
                    prompts.append(Fragment(said[:MAX_FRAGMENT_CHARS],
                                            SOURCE_TRANSCRIPT_PROMPT))
            elif kind == "assistant" and want_prose:
                said = _assistant_text(content)
                if said:
                    prose.append(Fragment(said[:MAX_FRAGMENT_CHARS],
                                          SOURCE_TRANSCRIPT_PROSE))
    return cut


def _user_text(content) -> str:
    """Only what the user actually said.

    Tool results arrive as user-role records — measured at 262 of 267 in one
    real session — and are not something the user said.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _assistant_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def gather(project_path: str, *, include_prompts: bool = True,
           include_prose: bool = False, git_exe: str = "") -> SessionContext:
    """Collect session context for a project. Never raises.

    Precedence: the session note when the project has one, mined transcripts
    otherwise — and `from_note` records which, because "the note supplied this"
    and "we went looking" are different claims.
    """
    if not project_path:
        return SessionContext()
    try:
        since, reason = last_commit_time(project_path, git_exe)
        floor, reason = _floor(since, reason)

        fragments, truncated, from_note = (), False, False
        if include_prompts:
            noted = _from_note(project_path, floor)
            if noted:
                fragments, from_note = tuple(noted[-MAX_FRAGMENTS:]), True
                truncated = len(noted) > MAX_FRAGMENTS

        if not from_note or include_prose:
            mined, cut = _from_transcripts(
                project_path, floor,
                want_prose=include_prose)
            truncated = truncated or cut
            if from_note:
                # The note already supplied prompts; take only prose from the
                # transcripts so the two sources cannot double up.
                mined = tuple(f for f in mined
                              if f.source == SOURCE_TRANSCRIPT_PROSE)
            elif not include_prompts:
                mined = tuple(f for f in mined
                              if f.source == SOURCE_TRANSCRIPT_PROSE)
            fragments = tuple(fragments) + mined

        return SessionContext(fragments=fragments, window_reason=reason,
                              since=floor, truncated=truncated,
                              from_note=from_note)
    except Exception:
        return SessionContext()
