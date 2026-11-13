"""The deep Cursor handoff brief: what it carries, what it says it cannot know, and its limits.

Measured on one 763 KB desktop chat: 34 prompts, 476 records, 64 edited paths in five `_scratch`
checkouts, 171 shell commands, a plan, a question, and a last turn that ended in
"Weekly usage limit reached". The old brief carried the first prompt, ten one-lined prompts and one
message. Cursor transcripts record NO tool results, so files and commands are attempts.

Fixtures are the real record shapes with invented content.
"""
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from helpers import agent_handoff as ah
from helpers import handoff_digest as hd

SECRET = "sk-abcdefghijklmnopqrstuvwx"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def user(text):
    return {"role": "user", "message": {"content": [
        {"type": "text", "text": f"<timestamp>Mon</timestamp>\n<user_query>\n{text}\n</user_query>"}]}}


def reply(text):
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool(_tool, **data):
    return {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": _tool, "input": data}]}}


def ended(status="success", error=None):
    rec = {"type": "turn_ended", "status": status}
    if error:
        rec["error"] = error
    return rec


def write(tmp_path, records, name="t.jsonl"):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")
    return str(path)


def brief(tmp_path, records, project=None, **meta):
    path = write(tmp_path, records)
    facts = hd.read_facts(path)
    base = {"session_id": "s1", "title": "T", "project_path": project or str(tmp_path),
            "last_active": None, "repo": {"branch": "master", "dirty": (), "dirty_total": 0,
                                           "commits": ("abc1234 something",), "git_error": ""},
            "sidecar_rel": ".tokensave/handoffs/cursor-s1.conversation.md",
            "transcript_path": path, "ignore": "ignored", "status_for": lambda root: {}}
    base.update(meta)
    return hd.render_brief(facts, base), facts


def section(text, title):
    start = text.index("## " + title)
    rest = text[start + 3:]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def test_the_section_caps_sum_to_less_than_the_total_so_no_tail_is_ever_chopped():
    assert sum(hd.SECTION_CAPS.values()) < hd.HARD_CAP


# -- turns: each prompt kept with its own outcome ---------------------------------------

def test_every_prompt_is_paired_with_that_turns_last_reply(tmp_path):
    text, facts = brief(tmp_path, [
        user("first ask"), reply("working"), reply("first answer"), ended(),
        user("second ask"), reply("second answer"), ended()])
    tl = section(text, "Timeline")
    assert "1. first ask\n   - first answer" in tl          # the LAST text of the turn, not the first
    assert "2. second ask\n   - second answer" in tl
    assert facts.turn_count == 2


def test_a_user_record_with_no_query_is_not_a_turn(tmp_path):
    bare = {"role": "user", "message": {"content": [{"type": "text", "text": "<timestamp>t</timestamp>"}]}}
    _, facts = brief(tmp_path, [bare, user("real"), reply("ok"), ended()])
    assert facts.turn_count == 1


def test_a_turn_with_no_reply_text_says_so_and_never_invents_one(tmp_path):
    text, _ = brief(tmp_path, [user("ask"), tool("Shell", command="ls"), ended()])
    assert "1. ask\n   - no final reply" in section(text, "Timeline")


def test_an_error_ending_is_reported_in_cursors_words_without_a_doubled_stop(tmp_path):
    text, _ = brief(tmp_path, [user("do it"), reply("starting"),
                               ended("error", "Weekly usage limit reached. It resets in 3 days.")])
    end = section(text, "How the session ended")
    assert "ERROR: Weekly usage limit reached. It resets in 3 days." in end and ".." not in end
    assert "[turn ended in an error" in section(text, "Timeline")


def test_aborted_and_unknown_statuses_are_shown_as_recorded(tmp_path):
    text, _ = brief(tmp_path, [user("a"), reply("x"), ended("aborted"), user("b"), reply("y"), ended("weird")])
    tl = section(text, "Timeline")
    assert "[turn aborted]" in tl and "[turn ended: weird]" in tl


def test_only_the_last_turn_is_called_interrupted_when_markers_are_missing(tmp_path):
    text, _ = brief(tmp_path, [user("a"), reply("x"), user("b"), reply("y")])
    tl = section(text, "Timeline")
    assert tl.count("no end marker") == 1 and tl.index("no end marker") > tl.index("2. b")
    assert "NO end marker" in section(text, "How the session ended")


def test_the_last_prompt_is_quoted_in_the_ending_and_bounded(tmp_path):
    text, _ = brief(tmp_path, [user("early"), reply("k"), ended(), user("L" * 5000), reply("z"), ended()])
    end = section(text, "How the session ended")
    assert end.count("L") <= hd.ENDING_PROMPT_CHARS + 3 and "L" * 500 in end


# -- the timeline's window and its accounting -------------------------------------------

def _turns(n):
    records = []
    for i in range(1, n + 1):
        records += [user(f"prompt number {i}"), reply(f"outcome number {i}"), ended()]
    return records


def test_sixty_turns_or_fewer_are_all_shown(tmp_path):
    text, _ = brief(tmp_path, _turns(60))
    tl = section(text, "Timeline")
    assert "1. prompt number 1\n" in tl and "60. prompt number 60\n" in tl and "in between" not in tl


def test_over_sixty_turns_show_the_first_ten_and_last_forty_and_count_the_rest(tmp_path):
    text, facts = brief(tmp_path, _turns(100))
    tl = section(text, "Timeline")
    assert "10. prompt number 10\n" in tl and "11. prompt number 11\n" not in tl
    assert "60. prompt number 60\n" not in tl and "61. prompt number 61\n" in tl and "100. prompt number 100\n" in tl
    assert "50 turns in between are not shown" in tl
    assert "50 turns in the middle of the timeline" in text.split("## The original request")[0]


def test_shortened_prompts_and_outcomes_are_counted_in_the_left_out_line(tmp_path):
    text, _ = brief(tmp_path, [user("p" * 900), reply("o" * 900), ended(), user("short"), reply("short"), ended()])
    head = text.split("## The original request")[0]
    assert "1 prompts shortened to 300 characters" in head and "1 turn outcomes shortened to 400 characters" in head
    tl = section(text, "Timeline")
    assert "p" * 301 not in tl and tl.count("...") == 2


def test_a_clean_short_session_reports_that_nothing_was_left_out(tmp_path):
    text, _ = brief(tmp_path, _turns(3))
    assert "**Left out of this brief:** nothing." in text


# -- plans, checkpoints, questions: what the agent recorded -----------------------------

def test_plans_steps_and_questions_are_labelled_as_what_the_agent_recorded(tmp_path):
    text, _ = brief(tmp_path, [
        user("plan it"),
        tool("CreatePlan", name="Salt engine", overview="Investigate only", plan="long body",
             todos=[{"id": "1", "content": "write the note"}, {"id": "2", "content": "no src work"}]),
        tool("UpdateCurrentStep", current_step="reading the existing salt code"),
        tool("AskQuestion", title="Where to start", questions=[{"id": "q", "prompt": "Which branch?", "options": []}]),
        ended(), user("use master"), reply("ok"), ended()])
    plans = section(text, "Plans and agent-recorded checkpoints")
    assert "What the AGENT recorded" in plans and "Plan: Salt engine" in plans
    assert "- [ ] write the note" in plans and "reading the existing salt code" in plans
    assert "Where to start: Which branch?" in plans and "answer: use master" in plans


def test_a_question_never_gets_an_answer_the_person_did_not_give(tmp_path):
    text, _ = brief(tmp_path, [user("q"), tool("AskQuestion", title="T", questions=[{"prompt": "Which?"}]), ended()])
    assert "answer: no answer recorded" in section(text, "Plans and agent-recorded checkpoints")


def test_only_the_latest_plans_are_kept_and_the_rest_counted(tmp_path):
    records = [user("go")] + [tool("CreatePlan", name=f"plan {i}", overview="", plan="", todos=[]) for i in range(5)] + [ended()]
    text, facts = brief(tmp_path, records)
    plans = section(text, "Plans and agent-recorded checkpoints")
    assert "plan 4" in plans and "plan 1" not in plans and "2 earlier plans are not shown" in plans
    assert "2 earlier plans" in text.split("## The original request")[0]


# -- files and commands: attempts, checked against git now ------------------------------

def _checkout(parent, name):
    root = parent / name
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere\n")        # a worktree's .git is a file
    return root


def test_files_are_attempts_cross_checked_in_the_checkout_each_belongs_to(tmp_path):
    main, other = _checkout(tmp_path, "Main"), _checkout(tmp_path, "Other")
    (main / "src").mkdir()
    for name in ("kept.py", "new.py", "quiet.py"):
        (main / "src" / name).write_text("x")
    (other / "doc.md").write_text("x")
    text, _ = brief(tmp_path, [
        user("edit things"),
        tool("StrReplace", path=str(main / "src" / "kept.py"), old_string="a", new_string="b"),
        tool("StrReplace", path=str(main / "src" / "kept.py"), old_string="b", new_string="c"),
        tool("Write", path=str(main / "src" / "new.py"), contents="x"),
        tool("StrReplace", path=str(main / "src" / "quiet.py"), old_string="a", new_string="b"),
        tool("Delete", path=str(main / "src" / "gone.py")),
        tool("StrReplace", path=str(other / "doc.md"), old_string="a", new_string="b"),
        ended()], project=str(main),
        status_for=lambda root: {"src/kept.py": " M", "src/new.py": "??"} if root == str(main) else {})
    files = section(text, "Files touched by agent operations")
    assert "ATTEMPTS, not results" in files
    assert "`src/kept.py`: 2 edit attempted; modified in git now" in files
    assert "`src/new.py`: 1 write attempted; untracked now" in files
    assert "`src/quiet.py`: 1 edit attempted; unchanged in git now" in files
    assert "`src/gone.py`: 1 delete attempted; not present now" in files
    assert "`Other: doc.md`: 1 edit attempted; unchanged in git now" in files


def test_a_path_whose_checkout_is_gone_says_so_instead_of_guessing(tmp_path):
    gone = tmp_path / "removed-worktree" / "a.py"
    text, _ = brief(tmp_path, [user("x"), tool("Write", path=str(gone), contents="x"), ended()])
    assert "its folder no longer exists, not checked" in section(text, "Files touched by agent operations")


def test_git_not_answering_is_unknown_never_clean(tmp_path):
    main = _checkout(tmp_path, "Main")
    (main / "a.py").write_text("x")
    text, _ = brief(tmp_path, [user("x"), tool("StrReplace", path=str(main / "a.py"), old_string="a", new_string="b"), ended()],
                    project=str(main), status_for=lambda root: None)
    assert "git state unknown (git did not answer)" in section(text, "Files touched by agent operations")


def test_more_checkouts_than_the_limit_are_not_read(tmp_path):
    calls = []
    records = [user("x")]
    for i in range(hd.MAX_ROOTS + 2):
        root = _checkout(tmp_path, f"R{i}")
        (root / "f.py").write_text("x")
        records.append(tool("StrReplace", path=str(root / "f.py"), old_string="a", new_string="b"))
    text, _ = brief(tmp_path, records + [ended()], project=str(tmp_path / "R0"),
                    status_for=lambda root: calls.append(root) or {})
    assert len(calls) == hd.MAX_ROOTS
    assert f"not checked (more than {hd.MAX_ROOTS} checkouts)" in text


def test_paths_beyond_the_shown_limit_are_counted(tmp_path):
    main = _checkout(tmp_path, "Main")
    records = [user("x")] + [tool("Write", path=str(main / f"f{i:03d}.py"), contents="x") for i in range(60)] + [ended()]
    text, _ = brief(tmp_path, records, project=str(main))
    assert "20 more paths are not shown" in section(text, "Files touched by agent operations")
    assert "20 file paths" in text.split("## The original request")[0]


def test_the_last_fifteen_shell_commands_are_shown_and_the_rest_counted(tmp_path):
    records = [user("x")] + [tool("Shell", command=f"echo step {i}") for i in range(20)] + [ended()]
    text, _ = brief(tmp_path, records)
    files = section(text, "Files touched by agent operations")
    assert "echo step 19" in files and "echo step 5" in files and "echo step 4`" not in files
    assert "5 earlier commands are not shown" in files and "(of 20;" in files


# -- redaction: everywhere, and before truncation ---------------------------------------

def test_secrets_are_masked_in_every_fragment_of_the_brief_and_the_conversation_file(tmp_path):
    main = _checkout(tmp_path, "Main")
    text, facts = brief(tmp_path, [
        user(f"my key is {SECRET} please use it"), reply(f"I will use {SECRET}"),
        tool("CreatePlan", name=f"plan {SECRET}", overview=f"ov {SECRET}", plan="p",
             todos=[{"id": "1", "content": f"todo {SECRET}"}]),
        tool("UpdateCurrentStep", current_step=f"step {SECRET}"),
        tool("Shell", command=f"curl -H 'token: {SECRET}' https://x"),
        tool("AskQuestion", title=f"q {SECRET}", questions=[{"prompt": f"which {SECRET}"}]),
        ended("error", f"failed with {SECRET}"), user(f"answer {SECRET}"), reply("done"), ended()], project=str(main))
    assert SECRET not in text and SECRET not in facts.sidecar
    assert "[redacted]" in text


def test_a_secret_cut_by_truncation_is_still_masked(tmp_path):
    long = "x " * 140 + SECRET + " tail"                       # the secret straddles the 300-char cut
    text, _ = brief(tmp_path, [user(long), reply("ok"), ended()])
    assert SECRET[:12] not in text


# -- the hard limits --------------------------------------------------------------------

def test_the_brief_never_exceeds_its_byte_limit_even_for_a_worst_case_chat(tmp_path):
    main = _checkout(tmp_path, "Main")
    big = "é中" * 3000
    records = []
    for i in range(300):
        records += [user(big), reply(big), tool("CreatePlan", name=big, overview=big, plan=big,
                                                todos=[{"content": big}] * 30),
                    tool("Shell", command=big), tool("UpdateCurrentStep", current_step=big),
                    tool("Write", path=str(main / f"{big[:20]}{i}.py"), contents="x"), ended()]
    records += [tool("AskQuestion", title=big, questions=[{"prompt": big}] * 8)]
    text, _ = brief(tmp_path, records, project=str(main), ignore="not_ignored", title="é" * 5000)
    assert len(text.encode("utf-8")) <= hd.HARD_CAP
    assert "## Observed in the repository now" in text and "## Where the rest is" in text   # never the cut part
    assert "cut at" in text or "sections cut" in text


def test_a_huge_title_cannot_crowd_out_the_sections_behind_it(tmp_path):
    text, _ = brief(tmp_path, [user("ask"), reply("ok"), ended()], title="é" * 5000)
    head = text.split("## The original request")[0]
    assert len(head.encode("utf-8")) <= hd.SECTION_CAPS["framing"]
    assert "These are notes, not a restored session" in head          # the contract always survives


def test_a_section_cut_to_fit_is_named_in_the_left_out_line(tmp_path):
    text, _ = brief(tmp_path, [user("é中" * 1000), reply("ok"), ended()])        # 2000 chars of 3-byte text
    assert "sections cut to fit the size limit (goal)" in text.split("## The original request")[0]


def test_the_conversation_file_is_capped_and_says_what_it_left_out(tmp_path):
    records = []
    for i in range(200):
        records += [user("é中" * 4000), reply("ok " * 100), ended()]
    text, facts = brief(tmp_path, records)
    assert len(facts.sidecar.encode("utf-8")) <= hd.SIDECAR_CAP and facts.sidecar_cut > 0
    assert f"{facts.sidecar_cut} later items are not included" in facts.sidecar
    assert "items in the conversation file" in text.split("## The original request")[0]


def test_the_conversation_file_holds_every_turn_when_it_fits(tmp_path):
    _, facts = brief(tmp_path, _turns(5))
    for i in range(1, 6):
        assert f"## Turn {i}: the person" in facts.sidecar and f"outcome number {i}" in facts.sidecar
    assert "turn ended: success" in facts.sidecar and facts.sidecar_cut == 0


# -- reading is bounded and tolerant ----------------------------------------------------

def test_bad_records_are_skipped_and_counted_and_the_rest_still_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "MAX_RECORD_BYTES", 5000)
    path = write(tmp_path, ["not json", "[1, 2]", user("fine"), json.dumps(reply("x" * 9000)), reply("ok"), ended()])
    facts = hd.read_facts(path)
    assert facts.skipped == 3 and facts.turn_count == 1 and facts.records == 3


def test_a_missing_transcript_reads_as_empty_not_an_error(tmp_path):
    facts = hd.read_facts(str(tmp_path / "nope.jsonl"))
    assert facts.turn_count == 0 and facts.records == 0


def test_the_transcript_is_only_read(tmp_path):
    path = write(tmp_path, _turns(5))
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    hd.read_facts(path)
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before


def test_the_brief_is_utf8_with_lf_line_endings(tmp_path):
    text, _ = brief(tmp_path, [user("héllo 中"), reply("ok"), ended()])
    assert "\r" not in text and "héllo 中" in text


# -- git helpers ------------------------------------------------------------------------

@needs_git
def test_status_map_and_ignore_state_ask_git(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("a")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=n",
                    "commit", "-q", "-m", "x"], check=True)
    (tmp_path / "tracked.txt").write_text("changed")
    (tmp_path / "fresh.txt").write_text("new")
    found = hd.status_map("git", str(tmp_path))
    assert found["tracked.txt"].strip() == "M" and found["fresh.txt"] == "??"
    assert hd.ignore_state("git", str(tmp_path), ".tokensave/handoffs/x.md") == "not_ignored"
    (tmp_path / ".gitignore").write_text(".tokensave/\n")
    assert hd.ignore_state("git", str(tmp_path), ".tokensave/handoffs/x.md") == "ignored"


def test_git_failing_is_none_or_unknown_never_clean_or_ignored(tmp_path):
    assert hd.status_map("git", str(tmp_path / "not a repo")) is None
    assert hd.ignore_state("no-such-git-binary", str(tmp_path), "x") == "unknown"


# -- prepare(): the deep brief is what gets written for a Cursor session ----------------

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Proj"
    root.mkdir()
    return str(root)


def _cursor_transcript(tmp_path, records):
    return write(tmp_path, records, name="cursor-chat.jsonl")


@needs_git
def test_prepare_writes_the_brief_and_the_conversation_file_for_a_cursor_session(tmp_path, project):
    subprocess.run(["git", "init", "-q", project], check=True)
    open(os.path.join(project, ".gitignore"), "w").write(".tokensave/\n")
    path = _cursor_transcript(tmp_path, _turns(4))
    result = ah.prepare({"session_id": "sess9", "title": "T", "path": path}, project, "git", "cursor")
    assert result.ok, result.error
    assert os.path.basename(result.brief_path) == "cursor-sess9.md"
    assert os.path.basename(result.sidecar_path) == "cursor-sess9.conversation.md"
    assert result.prompt_count == 4 and result.note == ""
    text = open(result.brief_path, encoding="utf-8").read()
    assert text.startswith("# Handoff from a Cursor session") and "## Timeline" in text
    assert "## Turn 4: the person" in open(result.sidecar_path, encoding="utf-8").read()


@needs_git
def test_prepare_says_when_git_does_not_ignore_the_handoff_folder(tmp_path, project):
    subprocess.run(["git", "init", "-q", project], check=True)
    result = ah.prepare({"session_id": "s", "path": _cursor_transcript(tmp_path, _turns(1))}, project, "git", "cursor")
    assert result.ok and "does not ignore" in result.note
    assert "Warning" in open(result.brief_path, encoding="utf-8").read()


def test_prepare_falls_back_to_the_shallow_brief_for_a_transcript_it_cannot_read(tmp_path, project):
    path = _cursor_transcript(tmp_path, [reply("only assistant text"), "garbage"])
    result = ah.prepare({"session_id": "s", "path": path}, project, "git", "cursor")
    assert result.ok and result.sidecar_path == ""
    assert "Handoff from a Cursor session" in open(result.brief_path, encoding="utf-8").read()


def test_prepare_for_a_claude_session_is_unchanged(tmp_path, project):
    rec = [{"type": "user", "message": {"content": "do the thing"}, "cwd": project, "sessionId": "c1"},
           {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}]
    path = write(tmp_path, rec, name="claude.jsonl")
    result = ah.prepare({"session_id": "c1", "title": "T", "path": path}, project, "git", "claude")
    assert result.ok and result.sidecar_path == ""
    assert "Handoff from a Claude Code session" in open(result.brief_path, encoding="utf-8").read()


def test_the_launch_text_points_at_the_conversation_file_only_when_there_is_one():
    plain = ah.instruction_for(".tokensave/handoffs/a.md", "cursor")
    deep = ah.instruction_for(".tokensave/handoffs/a.md", "cursor", ".tokensave/handoffs/a.conversation.md")
    assert "conversation" not in plain
    assert "a.conversation.md" in deep and "rather than asking the person to repeat" in deep
    with pytest.raises(ValueError):
        ah.instruction_for("p", "codex", "s")


def test_a_deep_handoff_hands_the_receiving_agent_both_files(tmp_path, project, monkeypatch):
    seen = {}
    monkeypatch.setattr(ah.agent_cli, "spawn", lambda spec, exe, path, ins, model="":
                        seen.update(ins=ins) or (True, ""))
    brief_file = os.path.join(project, ".tokensave", "handoffs", "cursor-s.md")
    prepared = ah.Prepared(True, brief_path=brief_file, project_path=project, prompt_count=3,
                           sidecar_path=brief_file[:-3] + ".conversation.md")
    ok, err = ah.launch_claude(prepared, "claude.cmd")
    assert ok, err
    assert "cursor-s.md" in seen["ins"] and "cursor-s.conversation.md" in seen["ins"]
