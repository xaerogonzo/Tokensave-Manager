"""Tests for helpers/agent_handoff.py -- moving a Claude session to Cursor.

The brief mixes three kinds of statement (what the person said, what Claude
said, what git reports) and the whole design is that they stay distinguishable,
so most of these assert on which section a fact lands in and what the framing
says, not just that text appears.
"""
import json
import os
import subprocess

import pytest

from helpers import agent_handoff as ah
from helpers.agent_handoff import (
    HANDOFF_DIR,
    Brief,
    Prepared,
    brief_path,
    build_brief,
    instruction_for,
    launch_cursor,
    observe_repository,
    prepare,
    redact,
    render,
    transcript_cwd,
)
from helpers.session_context import scan_transcript


def _rec(kind, content, **extra):
    rec = {"type": kind, "timestamp": "2026-10-31T13:00:00.000Z",
           "message": {"role": kind, "content": content}}
    rec.update(extra)
    return json.dumps(rec)


def _write(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(records) + "\n")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


@pytest.fixture
def transcript(tmp_path, project):
    path = str(tmp_path / "sess.jsonl")
    _write(path, [
        json.dumps({"type": "queue-operation", "sessionId": "abc"}),
        _rec("user", "Fix the failing build", cwd=project),
        _rec("assistant", [{"type": "text", "text": "Looking at the build."}]),
        # A tool result arrives in the user role and is not something said.
        _rec("user", [{"type": "tool_result", "tool_use_id": "t1",
                       "content": "lots of output"}]),
        _rec("user", "<local-command-stdout>noise</local-command-stdout>"),
        _rec("user", "meta note", isMeta=True),
        _rec("user", "Now also add a test"),
        _rec("assistant", [{"type": "text", "text": "Added the test; build is green."}]),
    ])
    return path


ROW = {"session_id": "abc", "title": "Fix build", "last_activity": 1793000000.0}


# -- cwd ------------------------------------------------------------------

def test_transcript_cwd_reads_the_recorded_directory(transcript, project):
    assert transcript_cwd(transcript) == project


def test_transcript_cwd_is_empty_when_absent_or_unreadable(tmp_path):
    bare = str(tmp_path / "bare.jsonl")
    _write(bare, [_rec("user", "hi")])
    assert transcript_cwd(bare) == ""
    assert transcript_cwd(str(tmp_path / "missing.jsonl")) == ""


# -- what the person asked ---------------------------------------------------

def test_only_what_the_person_said_is_collected(transcript, project):
    brief = build_brief(ROW, transcript, project)
    said = [brief.first_prompt, *brief.recent_prompts]
    assert said == ["Fix the failing build", "Now also add a test"]


def test_claudes_last_words_are_kept_apart_from_the_prompts(transcript, project):
    brief = build_brief(ROW, transcript, project)
    assert brief.last_assistant == "Added the test; build is green."
    assert brief.last_assistant not in brief.recent_prompts


def test_a_long_session_keeps_the_first_prompt_and_the_recent_trail(tmp_path, project):
    path = str(tmp_path / "long.jsonl")
    _write(path, [_rec("user", f"prompt {i}") for i in range(15)])
    brief = build_brief(ROW, path, project)
    assert brief.first_prompt == "prompt 0"
    assert brief.recent_prompts == tuple(f"prompt {i}" for i in range(5, 15))
    # 15 total - first - 10 recent: counted, not silently dropped.
    assert brief.skipped_prompts == 4
    assert "4 prompts between these are not shown" in render(brief)


def test_a_session_with_no_prompts_says_so_instead_of_rendering_a_blank(tmp_path, project):
    path = str(tmp_path / "empty.jsonl")
    _write(path, [json.dumps({"type": "queue-operation"})])
    text = render(build_brief(ROW, path, project))
    assert "No prompts could be read" in text


# -- an oversized transcript is read from its tail, and says so -------------

def test_tail_read_is_reported_as_partial(tmp_path):
    path = str(tmp_path / "big.jsonl")
    _write(path, [_rec("user", f"prompt {i} " + "x" * 200) for i in range(50)])
    prompts, _prose, truncated = scan_transcript(path, max_bytes=2000)
    assert truncated is True
    assert 0 < len(prompts) < 50
    assert prompts[-1].text.startswith("prompt 49")


def test_a_partial_read_does_not_call_its_first_prompt_the_original(monkeypatch, transcript, project):
    from helpers.session_context import Fragment
    monkeypatch.setattr(
        ah, "scan_transcript",
        lambda *_a, **_k: ((Fragment("mid-session", "transcript:prompt"),), (), True))
    text = render(build_brief(ROW, transcript, project))
    assert "Earliest prompt read" in text
    assert "Original request" not in text
    assert "earlier prompts are missing" in text


def test_scan_transcript_survives_a_missing_file(tmp_path):
    assert scan_transcript(str(tmp_path / "nope.jsonl")) == ((), (), False)


# -- secrets ------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnop1234", "ghp_abcdefghijklmnopqrstuv", "AKIAABCDEFGHIJKLMNOP",
])
def test_common_secret_shapes_are_masked(secret):
    assert secret not in redact(f"use {secret} for the call")


def test_assignment_style_secrets_keep_their_name_and_lose_the_value():
    out = redact("password = hunter2hunter2 and api_key: abc123")
    assert "hunter2hunter2" not in out and "abc123" not in out
    assert "password" in out and "api_key" in out


def test_a_secret_in_a_prompt_never_reaches_the_written_brief(tmp_path, project):
    path = str(tmp_path / "s.jsonl")
    _write(path, [_rec("user", "deploy with sk-abcdefghijklmnop1234 now", cwd=project)])
    result = prepare({**ROW, "path": path}, project)
    body = open(result.brief_path, encoding="utf-8").read()
    assert "sk-abcdefghijklmnop1234" not in body
    assert "[redacted]" in body


# -- the rendered contract ----------------------------------------------------

def test_the_brief_states_that_it_is_notes_not_a_restored_session(transcript, project):
    text = render(build_brief(ROW, transcript, project))
    assert "notes, not a restored session" in text
    assert "claim, not a fact" in text
    assert "Claude's memory" in text and "not included" in text


def test_sections_keep_the_three_kinds_of_statement_apart(transcript, project):
    text = render(build_brief(ROW, transcript, project))
    asked = text.index("## What the person asked")
    said = text.index("## What Claude last said")
    seen = text.index("## Observed in the repository now")
    assert asked < said < seen
    assert "Fix the failing build" in text[asked:said]
    assert "build is green" in text[said:seen]
    assert "build is green" not in text[asked:said]


def test_a_failed_git_call_is_reported_not_rendered_as_a_clean_tree(project):
    brief = Brief("s", "t", project, None, git_error="not a git repository")
    text = render(brief)
    assert "git did not answer: not a git repository" in text
    assert "Working tree clean" not in text


# -- observed state ------------------------------------------------------------

def _git(project, *args):
    subprocess.run(["git", "-C", project, *args], check=True,
                   capture_output=True, text=True)


def test_observe_repository_reports_branch_dirty_files_and_commits(project):
    _git(project, "init", "-q", "-b", "work")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    with open(os.path.join(project, "a.txt"), "w") as fh:
        fh.write("1")
    _git(project, "add", "a.txt")
    _git(project, "commit", "-q", "-m", "first commit")
    with open(os.path.join(project, "b.txt"), "w") as fh:
        fh.write("2")
    seen = observe_repository(project)
    assert seen["branch"] == "work"
    assert seen["dirty_total"] == 1 and "b.txt" in seen["dirty"][0]
    assert any("first commit" in c for c in seen["commits"])
    assert seen["git_error"] == ""


def test_observe_repository_names_a_non_repository(project):
    seen = observe_repository(project)
    assert seen["git_error"]
    assert seen["branch"] == "" and seen["commits"] == ()


def test_dirty_files_are_capped_and_the_remainder_counted(monkeypatch, project):
    many = "\n".join(f" M f{i}.py" for i in range(30))

    def fake(git_exe, proj, *args):
        if args[0] == "status":
            return True, many
        return True, "main\n" if args[0] == "rev-parse" else ""
    monkeypatch.setattr(ah, "_git", fake)
    seen = observe_repository(project)
    assert seen["dirty_total"] == 30
    assert len(seen["dirty"]) == ah.MAX_DIRTY_FILES


# -- writing ---------------------------------------------------------------------

def test_prepare_writes_the_brief_under_the_ignored_handoff_dir(transcript, project):
    result = prepare({**ROW, "path": transcript}, project)
    assert result.ok, result.error
    assert result.brief_path == brief_path(project, "abc")
    assert os.path.relpath(result.brief_path, project).startswith(HANDOFF_DIR)
    assert result.prompt_count == 2


def test_prepare_writes_lf_line_endings(transcript, project):
    result = prepare({**ROW, "path": transcript}, project)
    with open(result.brief_path, "rb") as fh:
        assert b"\r\n" not in fh.read()


def test_prepare_refuses_a_session_whose_transcript_is_gone(project):
    result = prepare({**ROW, "path": "/no/such.jsonl"}, project)
    assert not result.ok and "transcript" in result.error


def test_prepare_refuses_a_session_whose_folder_is_gone(transcript, tmp_path):
    result = prepare({**ROW, "path": transcript}, str(tmp_path / "deleted"))
    assert not result.ok and "no longer exists" in result.error


def test_a_session_id_cannot_escape_the_handoff_directory(project):
    path = brief_path(project, "../../evil")
    assert os.path.dirname(path) == os.path.join(project, HANDOFF_DIR)


# -- launching ----------------------------------------------------------------------

def test_the_launch_instruction_is_one_short_line():
    text = instruction_for(r".tokensave\handoffs\abc.md")
    assert "\n" not in text
    assert len(text) < 400
    assert "not verified fact" in text


def test_launch_opens_cursor_in_the_project_with_a_relative_pointer(monkeypatch, project):
    seen = {}

    def fake_spawn(spec, exe, path, instruction, model=""):
        seen.update(spec=spec, exe=exe, path=path, instruction=instruction, model=model)
        return True, ""
    monkeypatch.setattr(ah.agent_cli, "spawn", fake_spawn)
    prepared = Prepared(True, brief_path(project, "abc"), project, 2)
    ok, err = launch_cursor(prepared, "agent.cmd", "m")
    assert ok, err
    assert seen["spec"] is ah.agent_cli.CURSOR
    assert seen["path"] == project and seen["model"] == "m"
    assert HANDOFF_DIR in seen["instruction"]
    assert project not in seen["instruction"]


def test_launch_refuses_an_unprepared_handoff_without_spawning(monkeypatch):
    called = []
    monkeypatch.setattr(ah.agent_cli, "spawn", lambda *a, **k: called.append(1))
    ok, err = launch_cursor(Prepared(False, error="nothing to send"), "agent.cmd")
    assert not ok and err == "nothing to send" and not called


def test_launch_surfaces_a_missing_cursor_cli(project):
    ok, err = launch_cursor(Prepared(True, brief_path(project, "a"), project, 1), "")
    assert not ok and "not configured" in err


# -- the scanner hands over the file --------------------------------------------------

def test_scan_sessions_reports_the_transcript_path(fake_home):
    from helpers.claude_tasks import scan_sessions
    folder = os.path.join(fake_home, ".claude", "projects", "D--proj")
    os.makedirs(folder)
    file = os.path.join(folder, "xyz.jsonl")
    _write(file, [json.dumps({"type": "ai-title", "sessionId": "xyz", "aiTitle": "T"})])
    rows = scan_sessions([])
    assert rows and rows[0]["path"] == file


# -- measured on a real session: harness text is not what the person said ----

def test_injected_system_reminders_are_not_part_of_the_original_request(tmp_path, project):
    path = str(tmp_path / "s.jsonl")
    _write(path, [_rec(
        "user",
        "<system-reminder>\nYou are in a worktree.\n</system-reminder>\n\n"
        "Please add the thing", cwd=project)])
    brief = build_brief(ROW, path, project)
    assert brief.first_prompt == "Please add the thing"
    assert "worktree" not in render(brief)


def test_a_prompt_that_was_only_a_system_reminder_is_dropped(tmp_path, project):
    path = str(tmp_path / "s.jsonl")
    _write(path, [_rec("user", "<system-reminder>only this</system-reminder>"),
                  _rec("user", "the real ask")])
    brief = build_brief(ROW, path, project)
    assert brief.first_prompt == "the real ask"
    assert brief.recent_prompts == ()


def test_background_task_notifications_are_not_reported_as_asked(tmp_path, project):
    path = str(tmp_path / "s.jsonl")
    _write(path, [_rec("user", "run the suite"),
                  _rec("user", "<task-notification><task-id>x</task-id></task-notification>"),
                  _rec("user", "now commit")])
    brief = build_brief(ROW, path, project)
    assert [brief.first_prompt, *brief.recent_prompts] == ["run the suite", "now commit"]


def test_a_long_original_request_is_not_cut_at_the_window_scans_cap(tmp_path, project):
    path = str(tmp_path / "s.jsonl")
    ask = "word " * 300                       # 1500 chars; the window cap is 600
    _write(path, [_rec("user", ask)])
    brief = build_brief(ROW, path, project)
    assert len(brief.first_prompt) > 600
    assert brief.first_prompt == ask.strip()


def test_the_window_scan_keeps_its_small_cap(tmp_path):
    """Handoffs raise the cap per call; the shared default must not move."""
    path = str(tmp_path / "s.jsonl")
    _write(path, [_rec("user", "x" * 1500)])
    prompts, _, _ = scan_transcript(path)
    assert len(prompts[0].text) == 600


def test_the_placeholder_title_is_not_rendered_as_a_name(transcript, project):
    text = render(build_brief({**ROW, "title": "(untitled)"}, transcript, project))
    assert "((untitled))" not in text
    assert "(untitled)" in text          # rendered once, as the fallback label
