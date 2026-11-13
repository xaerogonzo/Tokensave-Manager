"""A deep, evidence-labelled handoff brief for a Cursor session.

The shallow brief (`agent_handoff.build_brief`) keeps the first prompt, the last ten
one-lined prompts and ONE assistant message. Measured on a 671 KB Cursor chat: 48
prompts, 63 replies and about 600 tool calls, so the receiving agent got a thin slice
of a long piece of work. This module reads the whole transcript once, streaming, and
keeps what carries the work: every prompt with the outcome of that turn, the plans and
checkpoints the agent recorded, the files it touched, its last shell commands, how the
session ended, and the whole conversation as a separate file.

WHAT EACH PART CLAIMS (the receiving agent is told, in the brief):

  * prompts and replies are quoted from the transcript;
  * plans, steps and questions are what the AGENT recorded, not what was decided;
  * an answer to a question is the person's NEXT message, never inferred;
  * files and commands are what the agent ATTEMPTED: Cursor transcripts record no tool
    results (0 `tool_result` records in 25 transcripts), so success is unknowable from
    them. Each path is checked against git NOW as a separate, measured fact;
  * how a turn ended is Cursor's own `turn_ended` marker (success / error / aborted).

BOUNDED, AND SAYS SO. The brief is capped at 60 KiB of UTF-8, enforced per section
(each section has its own byte cap, and the caps sum to less than the total), so the
pointers and the repository state can never be the part that gets cut. Every omission
is counted and reported in the brief. Parsing streams: one bad or oversized record is
skipped and counted, never fatal, and memory stays bounded however long the chat is.

REDACTION. Every fragment is extracted, normalised, redacted, THEN truncated, so a
secret cut in half is still masked. The brief and the conversation file hold redacted
excerpts; the original transcript is not redacted, and the brief says so.

The transcript is only read.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import subprocess

from constants import CREATE_NO_WINDOW
from helpers import cursor_transcript as ct

#: The whole brief, UTF-8. A hard limit, not an estimate.
HARD_CAP = 60 * 1024
#: Per-section byte caps. They sum to less than HARD_CAP (asserted in the tests), so
#: the sum is the guarantee and no tail is ever chopped to fit.
SECTION_CAPS = {"framing": 3000, "goal": 4000, "ending": 2500, "timeline": 21000,
                "plans": 8000, "files": 9000, "last": 6000, "repo": 3500,
                "pointers": 2500}

#: The conversation file, UTF-8.
SIDECAR_CAP = 1024 * 1024

MAX_RECORD_BYTES = 2 * 1024 * 1024       # a record larger than this is skipped
FIRST_TURNS, LAST_TURNS, ALL_TURNS_UP_TO = 10, 40, 60
GOAL_CHARS, PROMPT_CHARS, OUTCOME_CHARS, LAST_CHARS = 2000, 300, 400, 4000
SIDECAR_PROMPT_CHARS, SIDECAR_REPLY_CHARS = 8000, 4000
MAX_PATHS, SHOWN_PATHS, SHOWN_COMMANDS = 2000, 40, 15
#: Distinct checkouts whose `git status` is read for the file cross-check. One chat
#: measured edits across five `_scratch` worktrees of one repository.
MAX_ROOTS = 8
ENDING_PROMPT_CHARS = 600
SHOWN_PLANS, SHOWN_STEPS, SHOWN_QUESTIONS = 3, 20, 10

_EDIT_TOOLS = {"StrReplace": "edit", "EditNotebook": "edit", "Write": "write",
               "Delete": "delete"}


@dataclasses.dataclass
class Turn:
    n: int
    prompt: str
    prompt_cut: bool = False
    outcome: str = ""
    outcome_cut: bool = False
    status: str = ""          # Cursor's turn_ended status; "" = no marker seen
    error: str = ""


@dataclasses.dataclass
class Facts:
    """Everything read from one transcript, bounded."""
    size_bytes: int = 0
    records: int = 0
    skipped: int = 0
    turn_count: int = 0
    first_prompt: str = ""
    first_cut: bool = False
    last_prompt: str = ""
    last_text: str = ""
    last_text_cut: bool = False
    first_turns: list = dataclasses.field(default_factory=list)
    recent_turns: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=ALL_TURNS_UP_TO - FIRST_TURNS))
    plans: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=SHOWN_PLANS))
    plan_count: int = 0
    steps: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=SHOWN_STEPS))
    step_count: int = 0
    questions: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=SHOWN_QUESTIONS))
    question_count: int = 0
    paths: dict = dataclasses.field(default_factory=dict)      # path -> Counter of ops
    paths_dropped: int = 0
    commands: collections.deque = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=SHOWN_COMMANDS))
    command_count: int = 0
    subagents: int = 0
    tools: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    prompts_cut: int = 0
    outcomes_cut: int = 0
    sidecar: str = ""
    sidecar_cut: int = 0      # items left out of the conversation file


def _clip(text: str, chars: int) -> "tuple[str, bool]":
    """`(text cut to chars, whether it was cut)`. One line breaks stay."""
    if len(text) <= chars:
        return text, False
    return text[:chars - 3].rstrip() + "...", True


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _clip_bytes(text: str, cap: int, what: str) -> str:
    """Cut at a character boundary so the UTF-8 length is at most `cap`."""
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    note = f"\n\n_({what} cut at {cap} bytes)_\n"
    keep = raw[:max(0, cap - len(note.encode("utf-8")))].decode("utf-8", "ignore")
    return keep + note


class _Sidecar:
    """The conversation file, built under a byte cap that is counted, not guessed."""

    def __init__(self, cap: int) -> None:
        self.cap, self.size, self.parts, self.cut = cap, 0, [], 0

    def add(self, text: str) -> None:
        size = len(text.encode("utf-8"))
        if self.cut or self.size + size > self.cap - 400:
            self.cut += 1
            return
        self.parts.append(text)
        self.size += size

    def text(self) -> str:
        if self.cut:
            self.parts.append(f"\n_(The conversation file reached {self.cap // 1024} KiB: "
                              f"{self.cut} later items are not included. The original "
                              "transcript has them.)_\n")
        return "".join(self.parts)


def _text_blocks(content) -> list:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"]


def read_facts(path: str, redact=None) -> Facts:
    """Stream one Cursor transcript into bounded `Facts`. Never raises."""
    if redact is None:
        from helpers.agent_handoff import redact
    f = Facts()
    side = _Sidecar(SIDECAR_CAP)
    turn: "Turn | None" = None
    turn_tools: collections.Counter = collections.Counter()
    pending = None            # the last AskQuestion, waiting for the person's next message

    def close() -> None:
        nonlocal turn, turn_tools
        if turn is None:
            return
        f.outcomes_cut += turn.outcome_cut
        if turn_tools:
            side.add("_(tools: " + ", ".join(f"{n} x{c}" for n, c in
                                             turn_tools.most_common()) + ")_\n")
        end = turn.status or "no end marker"
        side.add(f"_(turn ended: {end}{': ' + turn.error if turn.error else ''})_\n\n")
        (f.first_turns if turn.n <= FIRST_TURNS else f.recent_turns).append(turn)
        turn, turn_tools = None, collections.Counter()

    try:
        f.size_bytes = os.path.getsize(path)
        handle = open(path, "rb")
    except OSError:
        return f
    with handle:
        for raw in handle:
            if len(raw) > MAX_RECORD_BYTES:
                f.skipped += 1
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                f.skipped += 1
                continue
            if not isinstance(rec, dict):
                f.skipped += 1
                continue
            f.records += 1
            if rec.get("type") == "turn_ended":
                if turn is not None:
                    turn.status = str(rec.get("status") or "ended")
                    err = rec.get("error")
                    turn.error = _clip(redact(_oneline(str(err))), 200)[0] if err else ""
                continue
            role = rec.get("role")
            blocks = (rec.get("message") or {}).get("content")
            if role == "user":
                said = ct.user_query("\n".join(_text_blocks(blocks)))
                if not said:
                    continue
                close()
                full = redact(said)
                f.turn_count += 1
                short, cut = _clip(_oneline(full), PROMPT_CHARS)
                turn = Turn(f.turn_count, short, prompt_cut=cut)
                f.prompts_cut += cut
                f.last_prompt = _clip(full, GOAL_CHARS)[0]
                if f.turn_count == 1:
                    f.first_prompt, f.first_cut = _clip(full, GOAL_CHARS)
                if pending is not None:
                    pending["answer"] = short
                    pending = None
                side.add(f"## Turn {f.turn_count}: the person\n\n"
                         f"{_clip(full, SIDECAR_PROMPT_CHARS)[0]}\n\n")
            elif role == "assistant" and turn is not None:
                asked = _read_assistant(f, turn, turn_tools, blocks, redact, side)
                pending = asked if asked is not None else pending
    close()
    f.sidecar_cut = side.cut
    f.sidecar = side.text()
    return f


def _question(f: Facts, data: dict, redact) -> dict:
    """An AskQuestion call. Its answer is filled in by the person's next message."""
    asked = [redact(_oneline(str(q.get("prompt") or "")))[:200]
             for q in (data.get("questions") or []) if isinstance(q, dict)]
    item = {"title": redact(_oneline(str(data.get("title") or "")))[:120],
            "asked": asked[:5], "answer": ""}
    f.questions.append(item)
    f.question_count += 1
    return item


def _read_assistant(f: Facts, turn: Turn, turn_tools, blocks, redact, side):
    """Fold one assistant record in. Returns a question it asked, if any."""
    asked = None
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = redact(str(block.get("text") or "").strip())
            if not text:
                continue
            turn.outcome, turn.outcome_cut = _clip(_oneline(text), OUTCOME_CHARS)
            f.last_text, f.last_text_cut = _clip(text, LAST_CHARS)
            side.add(f"### Reply\n\n{_clip(text, SIDECAR_REPLY_CHARS)[0]}\n\n")
        elif block.get("type") == "tool_use":
            name = str(block.get("name") or "?")
            data = block.get("input") if isinstance(block.get("input"), dict) else {}
            f.tools[name] += 1
            turn_tools[name] += 1
            if name == "AskQuestion":
                asked = _question(f, data, redact)
            else:
                _read_tool(f, name, data, redact)
    return asked


def _read_tool(f: Facts, name: str, data: dict, redact) -> None:
    if name in _EDIT_TOOLS:
        path = str(data.get("path") or data.get("target_notebook") or "")
        if path:
            if path in f.paths or len(f.paths) < MAX_PATHS:
                f.paths.setdefault(path, collections.Counter())[_EDIT_TOOLS[name]] += 1
            else:
                f.paths_dropped += 1
    elif name == "Shell":
        line = _oneline(str(data.get("command") or ""))
        if line:
            f.command_count += 1
            f.commands.append(_clip(redact(line), 200)[0])
    elif name == "CreatePlan":
        f.plan_count += 1
        todos = [redact(_oneline(str(t.get("content") or "")))[:140]
                 for t in (data.get("todos") or []) if isinstance(t, dict)]
        f.plans.append({"name": redact(_oneline(str(data.get("name") or "")))[:120],
                        "overview": _clip(redact(_oneline(str(data.get("overview") or ""))), 600)[0],
                        "todos": todos[:20], "todo_total": len(todos)})
    elif name == "UpdateCurrentStep":
        step = redact(_oneline(str(data.get("current_step") or data.get("final_summary") or "")))
        if step:
            f.step_count += 1
            f.steps.append(_clip(step, 240)[0])
    elif name == "Task":
        f.subagents += 1


# -- Rendering ----------------------------------------------------------------------

def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.strip().splitlines()) or "> "


def _rel(path: str, project: str) -> "tuple[str, bool]":
    """`(path shown, inside the project)`. A path inside the project is shown
    relative to it with forward slashes; anything else stays as the agent wrote it."""
    if project and os.path.isabs(path):
        try:
            base = os.path.normcase(os.path.normpath(project))
            full = os.path.normcase(os.path.normpath(path))
            if full == base or full.startswith(base + os.sep):
                return os.path.relpath(os.path.normpath(path), os.path.normpath(project)
                                       ).replace("\\", "/"), True
        except ValueError:
            pass
        return path, False
    return path.replace("\\", "/"), True


def status_map(git_exe: str, project: str) -> "dict | None":
    """`{git path (casefolded): code}` from `git status` now, or None if git did not answer."""
    try:
        proc = subprocess.run([git_exe or "git", "-C", project, "status", "--porcelain"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        found[name.replace("\\", "/").casefold()] = line[:2]
    return found


def ignore_state(git_exe: str, project: str, rel_file: str) -> str:
    """"ignored" / "not_ignored" / "unknown": whether git ignores the brief's folder.
    The baseline `.gitignore` is a convention, not a fact, so it is asked."""
    try:
        rc = subprocess.run([git_exe or "git", "-C", project, "check-ignore", "-q", rel_file],
                            capture_output=True, timeout=15,
                            creationflags=CREATE_NO_WINDOW).returncode
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return {0: "ignored", 1: "not_ignored"}.get(rc, "unknown")


def _git_root(path: str) -> str:
    """The nearest ancestor of an absolute path holding a `.git` (a directory in a
    clone, a file in a worktree), looking up at most 12 levels. "" if none."""
    cur = os.path.dirname(os.path.normpath(path))
    for _ in range(12):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def _file_state(rel: str, root: str, status) -> str:
    """Git NOW for one path. `status` is a map, None (git did not answer) or False
    (more checkouts than MAX_ROOTS: not read)."""
    if not root:
        return ("its folder no longer exists, not checked" if rel and not os.path.exists(rel)
                else "not in a git checkout, not checked")
    if status is False:
        return f"not checked (more than {MAX_ROOTS} checkouts)"
    if status is None:
        return "git state unknown (git did not answer)"
    key = rel.casefold()
    if key in status:
        return "untracked now" if status[key] == "??" else "modified in git now"
    if any(k.endswith("/") and key.startswith(k) for k in status):
        return "untracked now"
    if not os.path.exists(os.path.join(root, rel)):
        return "not present now"
    return "unchanged in git now"


class _Out:
    def __init__(self) -> None:
        self.cuts: list = []

    def fit(self, text: str, key: str) -> str:
        cap = SECTION_CAPS[key]
        if _bytes(text) > cap:
            self.cuts.append(key)
        return _clip_bytes(text, cap, key + " section")


def _turn_line(turn: Turn, last: bool) -> str:
    out = turn.outcome or "no final reply"
    label = ""
    if turn.status == "error":
        label = f" [turn ended in an error{': ' + turn.error if turn.error else ''}]"
    elif turn.status == "aborted":
        label = " [turn aborted]"
    elif turn.status and turn.status != "success":
        label = f" [turn ended: {turn.status}]"
    elif not turn.status and last:
        # Mid-transcript turns often carry no marker (measured), so only the last
        # one can mean "interrupted".
        label = " [no end marker: interrupted or still running]"
    return f"{turn.n}. {turn.prompt}\n   - {out}{label}"


def _timeline(f: Facts) -> "tuple[str, int]":
    turns = list(f.first_turns) + list(f.recent_turns)
    omitted = 0
    head, tail = list(f.first_turns), list(f.recent_turns)
    if f.turn_count > ALL_TURNS_UP_TO:
        tail = tail[-LAST_TURNS:]
        omitted = f.turn_count - len(head) - len(tail)
    lines = ["## Timeline: each prompt with how that turn ended", "",
             "_Prompts are the person's words; the line under each is the agent's last "
             "text reply in that turn, or what Cursor recorded about how it ended._", ""]
    last_n = turns[-1].n if turns else 0
    lines += [_turn_line(t, t.n == last_n) for t in head]
    if omitted:
        lines += ["", f"_({omitted} turns in between are not shown.)_", ""]
    lines += [_turn_line(t, t.n == last_n) for t in tail]
    return "\n".join(lines) + "\n", omitted


def _plans(f: Facts) -> str:
    lines = ["## Plans and agent-recorded checkpoints", "",
             "_What the AGENT recorded. It shows what was planned and marked, not why or "
             "what was agreed._", ""]
    if not (f.plan_count or f.step_count or f.question_count or f.subagents):
        return "\n".join(lines + ["_None recorded._", ""])
    for plan in f.plans:
        lines.append(f"**Plan: {plan['name'] or 'unnamed'}**")
        if plan["overview"]:
            lines.append(plan["overview"])
        lines += [f"- [ ] {t}" for t in plan["todos"][:12]]
        if plan["todo_total"] > 12:
            lines.append(f"- ... and {plan['todo_total'] - 12} more items")
        lines.append("")
    if f.plan_count > len(f.plans):
        lines += [f"_({f.plan_count - len(f.plans)} earlier plans are not shown.)_", ""]
    if f.steps:
        lines += ["**Checkpoints the agent recorded (oldest first):**", ""]
        lines += [f"- {s}" for s in f.steps]
        if f.step_count > len(f.steps):
            lines.append(f"- _({f.step_count - len(f.steps)} earlier checkpoints are not shown.)_")
        lines.append("")
    if f.questions:
        lines += ["**Questions the agent asked, and the person's next message:**", ""]
        for q in f.questions:
            lines.append(f"- {q['title'] or 'question'}: " + "; ".join(q["asked"]))
            lines.append("  - answer: " + (q["answer"] or "no answer recorded"))
        if f.question_count > len(f.questions):
            lines.append(f"- _({f.question_count - len(f.questions)} earlier questions are not shown.)_")
        lines.append("")
    if f.subagents:
        lines += [f"The agent started {f.subagents} subagent task"
                  f"{'s' if f.subagents != 1 else ''}.", ""]
    return "\n".join(lines)


def _status_source(meta: dict):
    """`status_for(root)` for the file cross-check: the caller's, or one built from
    the project's own status map (tests and simple callers)."""
    given = meta.get("status_for")
    if callable(given):
        return given
    own = meta.get("file_status")
    project = os.path.normcase(os.path.normpath(meta.get("project_path") or ""))
    return lambda root: own if os.path.normcase(os.path.normpath(root)) == project else None


def _files(f: Facts, project: str, status_for) -> "tuple[str, int]":
    lines = ["## Files touched by agent operations", "",
             "_ATTEMPTS, not results: Cursor's transcript records no tool results, so an "
             "edit may have failed. The last part of each line is git NOW, measured "
             "separately, in the checkout the file belongs to._", ""]
    rows = []
    for path, ops in f.paths.items():
        if os.path.isabs(path):
            root = _git_root(path)
            rel = os.path.relpath(os.path.normpath(path), root).replace("\\", "/") if root \
                else path.replace("\\", "/")
        else:
            root, rel = project, path.replace("\\", "/")
        inside = bool(root) and os.path.normcase(os.path.normpath(root)) == \
            os.path.normcase(os.path.normpath(project))
        shown = rel if inside or not root else f"{os.path.basename(root)}: {rel}"
        rows.append((-sum(ops.values()), shown.casefold(), shown, root, rel, ops))
    rows.sort()
    roots_read: dict = {}
    for _neg, _key, shown, root, rel, ops in rows[:SHOWN_PATHS]:
        if root and root not in roots_read:
            roots_read[root] = status_for(root) if len(roots_read) < MAX_ROOTS else False
        what = ", ".join(f"{n} {k}" for k, n in sorted(ops.items()))
        lines.append(f"- `{shown}`: {what} attempted; "
                     f"{_file_state(rel, root, roots_read.get(root))}")
    hidden = max(0, len(rows) - SHOWN_PATHS) + f.paths_dropped
    if not rows:
        lines.append("_No file operations recorded._")
    if hidden:
        lines.append(f"- _({hidden} more paths are not shown.)_")
    lines += ["", f"**Last shell commands** (of {f.command_count}; what was run, not what happened):", ""]
    if f.commands:
        lines += [f"{i}. `{c}`" for i, c in enumerate(f.commands, 1)]
        if f.command_count > len(f.commands):
            lines.append(f"- _({f.command_count - len(f.commands)} earlier commands are not shown.)_")
    else:
        lines.append("_None recorded._")
    return "\n".join(lines) + "\n", hidden


def _ending(f: Facts) -> str:
    turns = list(f.recent_turns) or list(f.first_turns)
    last = turns[-1] if turns else None
    lines = ["## How the session ended", ""]
    if last is None:
        lines.append("_No turns could be read._")
    elif last.status == "success":
        lines.append("The last turn ended normally. The person's last message was:")
    elif last.status == "error":
        lines.append("**The last turn ended in an ERROR"
                     + (f": {last.error.rstrip('. ')}" if last.error else "") + ".** The agent may have "
                     "stopped before finishing what the last message asked. That message was:")
    elif last.status:
        lines.append(f"The last turn ended as `{last.status}`. The person's last message was:")
    else:
        lines.append("The transcript ends with NO end marker on the last turn, so it was "
                     "interrupted or is still running. The person's last message was:")
    if last is not None:
        lines += ["", _quote(_clip(f.last_prompt or last.prompt, ENDING_PROMPT_CHARS)[0])]
    return "\n".join(lines) + "\n"


def _repo(repo: dict) -> str:
    lines = ["## Observed in the repository now", ""]
    if repo.get("git_error"):
        return "\n".join(lines + [f"_git did not answer: {repo['git_error']}_", ""])
    lines.append(f"- Branch: `{repo.get('branch', '')}`")
    dirty, total = repo.get("dirty") or (), repo.get("dirty_total") or 0
    if total:
        lines += [f"- Uncommitted changes ({total}; not attributed to this session):", ""]
        lines += [f"  - `{ln}`" for ln in dirty]
        if total > len(dirty):
            lines.append(f"  - ... and {total - len(dirty)} more")
    else:
        lines.append("- Working tree clean")
    if repo.get("commits"):
        lines += ["- Recent commits:", ""] + [f"  - {c}" for c in repo["commits"]]
    return "\n".join(lines) + "\n"


def _pointers(meta: dict) -> str:
    lines = ["## Where the rest is", ""]
    if meta.get("sidecar_rel"):
        lines.append(f"- The whole conversation, as redacted text with no tool payloads: "
                     f"`{meta['sidecar_rel']}`. Read the parts you need instead of asking "
                     "the person to repeat themselves.")
    if meta.get("transcript_path"):
        lines.append(f"- The original Cursor transcript: `{meta['transcript_path']}` (JSON "
                     "lines: `role`, `message.content`, `turn_ended`). It is NOT redacted, "
                     "may sit outside this workspace, and may ask for a permission to read.")
    if meta.get("ignore") == "not_ignored":
        lines.append("- **Warning:** git does not ignore this folder here, so these files "
                     "could be committed. Add `.tokensave/` to `.gitignore` first.")
    elif meta.get("ignore") == "unknown":
        lines.append("- Whether git ignores this folder could not be checked.")
    return "\n".join(lines) + "\n"


def _omissions(f: Facts, timeline_omitted: int, files_hidden: int, cuts: list) -> list:
    items = [
        (timeline_omitted, "turns in the middle of the timeline"),
        (f.prompts_cut, f"prompts shortened to {PROMPT_CHARS} characters"),
        (f.outcomes_cut, f"turn outcomes shortened to {OUTCOME_CHARS} characters"),
        (files_hidden, "file paths"),
        (max(0, f.command_count - len(f.commands)), "earlier shell commands"),
        (max(0, f.plan_count - len(f.plans)), "earlier plans"),
        (max(0, f.step_count - len(f.steps)), "earlier checkpoints"),
        (max(0, f.question_count - len(f.questions)), "earlier questions"),
        (1 if f.last_text_cut else 0, f"the last message (cut at {LAST_CHARS} characters)"),
        (f.skipped, "transcript records that could not be read"),
        (f.sidecar_cut, "items in the conversation file"),
        (len(cuts), "sections cut to fit the size limit (" + ", ".join(cuts) + ")"),
    ]
    return [f"{n} {what}" for n, what in items if n]


def render_brief(f: Facts, meta: dict) -> str:
    """The brief, UTF-8 at most HARD_CAP bytes, every omission reported."""
    out = _Out()
    project = meta.get("project_path") or ""
    timeline, t_omitted = _timeline(f)
    files, hidden = _files(f, project, _status_source(meta))
    goal_lines = ["## The original request", ""]
    goal_lines += [_quote(f.first_prompt)] if f.first_prompt else ["_No prompt could be read._"]
    if f.first_cut:
        goal_lines.append(f"\n_(Cut at {GOAL_CHARS} characters.)_")
    last_lines = ["## What Cursor last said (an excerpt, not verified)", "",
                  _quote(f.last_text) if f.last_text else "_Nothing readable._"]
    body = [out.fit("\n".join(goal_lines) + "\n", "goal"),
            out.fit(_ending(f), "ending"),
            out.fit(timeline, "timeline"),
            out.fit(_plans(f), "plans"),
            out.fit(files, "files"),
            out.fit("\n".join(last_lines) + "\n", "last"),
            out.fit(_repo(meta.get("repo") or {}), "repo"),
            out.fit(_pointers(meta), "pointers")]
    when = meta["last_active"].strftime("%Y-%m-%d %H:%M") if meta.get("last_active") else "unknown"
    left = _omissions(f, t_omitted, hidden, out.cuts)
    framing = [
        "# Handoff from a Cursor session", "",
        f"- Session: `{str(meta.get('session_id', ''))[:80]}` ({_clip(_oneline(str(meta.get('title') or '')), 120)[0] or 'untitled'})",
        f"- Project: `{project}`",
        f"- Last active: {when}",
        f"- Transcript: {f.size_bytes // 1024} KB, {f.records} records read, {f.skipped} "
        f"skipped, {f.turn_count} prompts",
        "", "> **These are notes, not a restored session.** Prompts and replies are quoted "
        "from the transcript; plans, steps and questions are what the agent recorded; files "
        "and commands are what it ATTEMPTED (no results are recorded). Only the repository "
        "section is measured. Cursor's words are a claim, not a fact: check the repository "
        "before acting. Cursor's memory is not included; the project's shared rules are in "
        "`AGENTS.md`. Secrets are masked best-effort in this file and the conversation file.",
        "",
        ("**Left out of this brief:** " + "; ".join(left) + ".") if left
        else "**Left out of this brief:** nothing.", ""]
    text = "\n".join([out.fit("\n".join(framing), "framing")] + body)
    text = _clip_bytes(text, HARD_CAP, "brief") if _bytes(text) > HARD_CAP else text
    return text.replace("\r\n", "\n")
