"""Hand a Claude Code session to another agent, without that agent's importer.

WHY IT EXISTS. Cursor advertises importing Claude Code conversations, and on at
least one machine here it never ran (a server-side gate, per Cursor's own
agent; the importer is not something this Manager can switch on). What a person
actually needs when they move a piece of work between agents is small: what was
asked, where it stands, and a way to start. That is buildable from files the
Manager already reads.

WHAT IT CLAIMS, PRECISELY. The brief is **notes, not a restored session**:

  * *what the person asked* is copied from the transcript (their words);
  * *Claude's last message* is an excerpt of what Claude SAID, not of what is
    true;
  * *observed in the repository now* is measured by the Manager with git.

Those three carry different weight, so they are different sections with the
difference stated at the top, and the receiving agent is told to verify before
acting. Claude's own memory and `CLAUDE.md` are NOT carried: `CLAUDE.md`'s
baseline is already in `AGENTS.md`, and per-project memory is a separate
decision a person should make.

The brief goes to a FILE and the receiving agent is pointed at it. The
alternative, putting it in the launch command, is capped near 8 KB by cmd.exe
and has newlines stripped (`agent_cli.build_spawn_cmdline`), which would
flatten the structure that makes it readable.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess

from constants import CREATE_NO_WINDOW
from helpers import agent_cli, cursor_ide
from helpers.session_context import (find_cursor_transcript,
                                     scan_cursor_transcript, scan_transcript)

#: Prompts kept besides the first. The first is the original goal; the rest are
#: the recent trail. A middle that is dropped is said to be dropped.
MAX_RECENT_PROMPTS = 10
MAX_DIRTY_FILES = 20
MAX_COMMITS = 5
#: Per-fragment cap for a handoff. The window scan's 600 cut a real original
#: request mid-sentence; 2000 holds a long one and still bounds the file.
FRAGMENT_CHARS = 2000

#: Harness text that rides inside a user record. Measured on a real session:
#: `<system-reminder>` blocks are pasted into the FIRST prompt, and
#: `<task-notification>` records arrive in the user role, so both were being
#: reported as things the person said.
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_HARNESS_ONLY = ("<task-notification>",)

#: (long name, short name) per source agent. The short name is what the brief
#: calls the agent in prose; an unknown source is a typo in code, not a default.
SOURCE_NAMES = {"claude": ("Claude Code", "Claude"), "cursor": ("Cursor", "Cursor")}


def _names(source: str) -> tuple:
    if source not in SOURCE_NAMES:
        raise ValueError(f"unknown handoff source {source!r}")
    return SOURCE_NAMES[source]


#: Where briefs live, relative to the project. `.tokensave/` is git-ignored by
#: the baseline, so a brief (which quotes the person's prompts) is machine-local
#: and never committed by accident.
HANDOFF_DIR = os.path.join(".tokensave", "handoffs")

#: Best-effort only, and named so. A prompt can hold a pasted key; these catch
#: the common shapes, not every secret.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)\S+"),
)


def clean_prompt(text: str) -> str:
    """What the person wrote, with harness-injected text removed. May be `""`."""
    text = _SYSTEM_REMINDER.sub("", text).strip()
    if text.startswith(_HARNESS_ONLY):
        return ""
    return text


def redact(text: str) -> str:
    """Mask common secret shapes. Best effort, not a guarantee."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


@dataclasses.dataclass(frozen=True)
class Brief:
    """Everything a handoff says, before it is rendered."""

    session_id: str
    title: str
    project_path: str
    last_active: "datetime.datetime | None"
    first_prompt: str = ""
    recent_prompts: tuple = ()
    #: Prompts that were neither the first nor recent. Counted, not hidden.
    skipped_prompts: int = 0
    last_assistant: str = ""
    #: The transcript was read from its tail, so the "first" prompt may not be
    #: the session's first.
    partial: bool = False
    branch: str = ""
    dirty: tuple = ()
    dirty_total: int = 0
    commits: tuple = ()
    #: `""` when git answered; otherwise why the observed section is empty.
    git_error: str = ""
    #: Which agent's session this came from; decides the wording, not the facts.
    source: str = "claude"


@dataclasses.dataclass(frozen=True)
class Prepared:
    """A brief that has been written, ready to launch."""

    ok: bool
    brief_path: str = ""
    project_path: str = ""
    prompt_count: int = 0
    error: str = ""


def _git(git_exe: str, project: str, *args: str) -> "tuple[bool, str]":
    try:
        proc = subprocess.run(
            [git_exe or "git", "-C", project, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip()[:200] or f"exit {proc.returncode}"
    return True, proc.stdout


def observe_repository(project: str, git_exe: str = "") -> dict:
    """Branch, dirty files and recent commits, as git reports them now."""
    out = {"branch": "", "dirty": (), "dirty_total": 0, "commits": (),
           "git_error": ""}
    ok, branch = _git(git_exe, project, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        out["git_error"] = branch or "git did not answer"
        return out
    out["branch"] = branch.strip()
    ok, status = _git(git_exe, project, "status", "--short")
    if ok:
        lines = [ln.rstrip() for ln in status.splitlines() if ln.strip()]
        out["dirty_total"] = len(lines)
        out["dirty"] = tuple(lines[:MAX_DIRTY_FILES])
    ok, log = _git(git_exe, project, "log", f"-{MAX_COMMITS}", "--format=%h %s")
    if ok:
        out["commits"] = tuple(ln for ln in log.splitlines() if ln.strip())
    return out


def build_brief(row: dict, transcript_path: str, project_path: str,
                git_exe: str = "", source: str = "claude") -> Brief:
    """Assemble a brief from one agent session. Never raises on a bad file."""
    _names(source)
    scan = scan_cursor_transcript if source == "cursor" else scan_transcript
    prompts, prose, partial = scan(
        transcript_path, want_prose=True, fragment_chars=FRAGMENT_CHARS)
    texts = [redact(c) for c in (clean_prompt(f.text) for f in prompts) if c]
    first = texts[0] if texts else ""
    rest = texts[1:]
    recent = tuple(rest[-MAX_RECENT_PROMPTS:])
    skipped = max(0, len(rest) - len(recent))
    last_said = redact(prose[-1].text) if prose else ""
    when = None
    ts = row.get("last_activity")
    if ts:
        try:
            when = datetime.datetime.fromtimestamp(float(ts))
        except (OSError, ValueError, OverflowError):
            when = None
    seen = observe_repository(project_path, git_exe)
    return Brief(
        session_id=str(row.get("session_id") or ""),
        title="" if row.get("title") == "(untitled)" else str(row.get("title") or ""),
        project_path=project_path, last_active=when,
        first_prompt=first, recent_prompts=recent, skipped_prompts=skipped,
        last_assistant=last_said, partial=partial,
        branch=seen["branch"], dirty=seen["dirty"],
        dirty_total=seen["dirty_total"], commits=seen["commits"],
        git_error=seen["git_error"], source=source)


def render(brief: Brief) -> str:
    """The brief as markdown. The framing paragraph is the contract."""
    when = brief.last_active.strftime("%Y-%m-%d %H:%M") if brief.last_active else "unknown"
    long_name, short = _names(brief.source)
    lines = [
        f"# Handoff from a {long_name} session",
        "",
        f"- Session: `{brief.session_id}` ({brief.title or 'untitled'})",
        f"- Project: `{brief.project_path}`",
        f"- Last active: {when}",
        "",
        "> **These are notes, not a restored session.** The first two sections are",
        f"> what the person asked and what {short} last said: {short}'s words are a",
        "> claim, not a fact. The third is measured from git at the moment this was",
        f"> written. Check the repository before acting on any of it. {short}'s memory",
        "> is not included; the project's shared rules are in `AGENTS.md`.",
        "",
        "## What the person asked (their words)",
        "",
    ]
    if not brief.first_prompt:
        lines += ["_No prompts could be read from the transcript._", ""]
    else:
        label = ("Earliest prompt read" if brief.partial else "Original request")
        lines += [f"**{label}:**", "", _quote(brief.first_prompt), ""]
        if brief.skipped_prompts:
            noun = "prompt" if brief.skipped_prompts == 1 else "prompts"
            lines += [f"_({brief.skipped_prompts} {noun} between these are not shown.)_", ""]
        if brief.recent_prompts:
            lines += ["**Most recent, oldest first:**", ""]
            lines += [f"{n}. {_oneline(p)}"
                      for n, p in enumerate(brief.recent_prompts, 1)]
            lines.append("")
        if brief.partial:
            lines += ["_The transcript was too large to read whole, so earlier "
                      "prompts are missing._", ""]
    lines += [f"## What {short} last said (an excerpt, not verified)", ""]
    lines += [_quote(brief.last_assistant) if brief.last_assistant
              else "_Nothing readable._", ""]
    lines += ["## Observed in the repository now", ""]
    if brief.git_error:
        lines += [f"_git did not answer: {brief.git_error}_", ""]
    else:
        lines += [f"- Branch: `{brief.branch}`"]
        if brief.dirty_total:
            lines += [f"- Uncommitted changes ({brief.dirty_total}):", ""]
            lines += [f"  - `{ln}`" for ln in brief.dirty]
            if brief.dirty_total > len(brief.dirty):
                lines.append(f"  - ... and {brief.dirty_total - len(brief.dirty)} more")
        else:
            lines += ["- Working tree clean"]
        if brief.commits:
            lines += ["- Recent commits:", ""]
            lines += [f"  - {c}" for c in brief.commits]
        lines.append("")
    lines += ["## Suggested start", "",
              "Read the sections above, confirm the current state with git and "
              "the code, then ask the person what to continue with if the next "
              "step is not obvious from their last prompt.", ""]
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.strip().splitlines()) or "> "


def _oneline(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 400 else flat[:397] + "..."


def brief_path(project_path: str, session_id: str, source: str = "claude") -> str:
    """Where a session's brief goes. Claude keeps its plain name; any other
    source is prefixed, because two agents' session ids are not drawn from
    disjoint spaces and one must never overwrite the other's brief."""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id) or "session"
    if source != "claude":
        safe = f"{source}-{safe}"
    return os.path.join(project_path, HANDOFF_DIR, f"{safe}.md")


def prepare(row: dict, project_path: str, git_exe: str = "",
            source: str = "claude") -> Prepared:
    """Read the session, write its brief, and say where. Never raises."""
    _names(source)
    transcript = row.get("path") or ""
    if not transcript and source == "cursor":
        # Cursor rows come from meta.json, which does not name the transcript.
        transcript = find_cursor_transcript(str(row.get("session_id") or ""))
    if not transcript or not os.path.isfile(transcript):
        return Prepared(False, error="The session's transcript file is gone.")
    if not project_path or not os.path.isdir(project_path):
        return Prepared(False, error=(
            f"The folder this session ran in no longer exists: "
            f"{project_path or '(unknown)'}"))
    brief = build_brief(row, transcript, project_path, git_exe, source)
    target = brief_path(project_path, brief.session_id, source)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(brief))
    except OSError as exc:
        return Prepared(False, error=f"Could not write {target}: {exc}")
    count = (1 if brief.first_prompt else 0) + len(brief.recent_prompts)
    return Prepared(True, target, project_path, count)


def instruction_for(path: str, source: str = "claude") -> str:
    """The short launch instruction. Stays far under the cmd.exe budget."""
    return (f"A person is moving work here from a {_names(source)[0]} session. Read "
            f"{path} for what was asked and where it stood. It is notes, not "
            f"verified fact: check the repository first, then continue.")


def launch_cursor(prepared: Prepared, exe: str, model: str = "") -> "tuple[bool, str]":
    """Open Cursor's interactive agent in the project, pointed at the brief."""
    if not prepared.ok:
        return False, prepared.error
    rel = os.path.relpath(prepared.brief_path, prepared.project_path)
    return agent_cli.spawn(agent_cli.CURSOR, exe, prepared.project_path,
                           instruction_for(rel), model)


def open_in_cursor_app(prepared: Prepared, source: str = "claude",
                       exe: str = "") -> "tuple[bool, str, str]":
    """Open the project in the Cursor APP and return `(ok, error, message)`.

    The app's agent cannot be started from outside, so this does what can be done:
    open the folder, and hand back the one message to paste into a new chat there.
    That message points the agent at the brief exactly as the terminal launch does.
    """
    if not prepared.ok:
        return False, prepared.error, ""
    ok, err = cursor_ide.open_folder(prepared.project_path, exe)
    if not ok:
        return False, err, ""
    rel = os.path.relpath(prepared.brief_path, prepared.project_path)
    return True, "", instruction_for(rel, source)


def launch_claude(prepared: Prepared, exe: str, source: str = "cursor") -> "tuple[bool, str]":
    """Open Claude Code's interactive session in the project, pointed at the brief.

    No `--model` is passed on purpose. The Manager's pinned Claude model is the
    fast, cheap one its automated calls want (Haiku); a person continuing their
    own work wants whatever they normally run, which is Claude's own default.
    """
    if not prepared.ok:
        return False, prepared.error
    rel = os.path.relpath(prepared.brief_path, prepared.project_path)
    return agent_cli.spawn(agent_cli.CLAUDE, exe, prepared.project_path,
                           instruction_for(rel, source), "")
