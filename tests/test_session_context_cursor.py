"""Cursor's sessions as a source of "what the person asked", beside Claude's.

Before this, `gather` knew about Claude's note and Claude's transcripts only, so
a person who spent the afternoon in Cursor had it left out of the commit message
and PR draft grounding entirely, and a Claude note being present was enough to
hide everything else. The properties worth pinning: Cursor's prompts are
additive (a note does not suppress them), the two are ordered by time, the
combined result still obeys ONE size budget, and Cursor's prose is never filed
as the person's words.
"""
import datetime
import json
import os
import time

import pytest

from helpers import session_context as sc
from helpers.session_context import (
    LABEL_CLAUDE,
    LABEL_CURSOR,
    LABEL_NOTE,
    PROSE_SOURCES,
    SOURCE_CURSOR_PROMPT,
    SOURCE_CURSOR_PROSE,
    SOURCE_NOTE_PROMPT,
    SOURCE_TRANSCRIPT_PROSE,
    Fragment,
    gather,
    is_prose,
)

NOW_MS = int(time.time() * 1000)
HOUR = 3600 * 1000


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _chat(home, project, session_id, prompts, answers=(), updated_ms=NOW_MS, cwd=None):
    """A Cursor chat on disk: its meta.json and its transcript."""
    chat = os.path.join(home, ".cursor", "chats", "bucket", session_id)
    os.makedirs(chat)
    with open(os.path.join(chat, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"schemaVersion": 1, "createdAtMs": updated_ms,
                   "updatedAtMs": updated_ms, "cwd": cwd or project}, fh)
    folder = os.path.join(home, ".cursor", "projects", "any-name",
                          "agent-transcripts", session_id)
    os.makedirs(folder)
    lines = []
    for ask in prompts:
        lines.append(json.dumps({"role": "user", "message": {"content": [
            {"type": "text", "text": f"<timestamp>t</timestamp>\n<user_query>\n{ask}\n</user_query>"}]}}))
    for said in answers:
        lines.append(json.dumps({"role": "assistant", "message": {"content": [
            {"type": "text", "text": said}]}}))
    lines.append(json.dumps({"type": "turn_ended", "status": "success"}))
    with open(os.path.join(folder, session_id + ".jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _note(project, prompts, ended: datetime.datetime):
    from helpers.session_note import note_path
    path = note_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"sessions": [{"ended_at": ended.astimezone().isoformat(),
                                 "prompts": prompts}]}, fh)


# -- Cursor is a source ---------------------------------------------------------------

def test_a_cursor_prompt_reaches_the_gathered_context(fake_home, project):
    _chat(fake_home, project, "c1", ["wire the ledger into the Tasks tab"])
    ctx = gather(project)
    assert [f.text for f in ctx.prompts] == ["wire the ledger into the Tasks tab"]
    assert ctx.prompts[0].source == SOURCE_CURSOR_PROMPT
    assert ctx.sources == (LABEL_CURSOR,)


def test_a_claude_note_does_not_hide_cursors_prompts(fake_home, project):
    """The note used to be the whole answer; a Cursor afternoon vanished behind it."""
    _note(project, ["from the Claude session"], datetime.datetime.now() - datetime.timedelta(hours=3))
    _chat(fake_home, project, "c1", ["from the Cursor session"], updated_ms=NOW_MS - HOUR)
    ctx = gather(project)
    assert [f.text for f in ctx.prompts] == ["from the Claude session",
                                             "from the Cursor session"]
    assert ctx.from_note is True
    assert ctx.sources == (LABEL_NOTE, LABEL_CURSOR)


def test_the_two_sources_are_ordered_by_time_not_by_which_was_read_first(fake_home, project):
    _note(project, ["claude, later"], datetime.datetime.now() - datetime.timedelta(minutes=5))
    _chat(fake_home, project, "c1", ["cursor, earlier"], updated_ms=NOW_MS - 5 * HOUR)
    assert [f.text for f in gather(project).prompts] == ["cursor, earlier", "claude, later"]


def test_cursor_chats_are_ordered_among_themselves_by_when_they_ended(fake_home, project):
    _chat(fake_home, project, "newer", ["second"], updated_ms=NOW_MS - HOUR)
    _chat(fake_home, project, "older", ["first"], updated_ms=NOW_MS - 3 * HOUR)
    assert [f.text for f in gather(project).prompts] == ["first", "second"]


def test_prompts_within_one_chat_keep_the_order_they_were_asked(fake_home, project):
    _chat(fake_home, project, "c1", ["one", "two", "three"])
    assert [f.text for f in gather(project).prompts] == ["one", "two", "three"]


# -- one budget, not two ------------------------------------------------------------------

def test_two_sources_share_one_prompt_budget_and_the_newest_survive(fake_home, project):
    for n in range(20):
        _chat(fake_home, project, f"c{n:02d}", [f"cursor {n}"],
              updated_ms=NOW_MS - (40 - n) * 60_000)
    _note(project, [f"claude {n}" for n in range(20)],
          datetime.datetime.now() - datetime.timedelta(days=2))
    ctx = gather(project)
    assert len(ctx.prompts) <= sc.MAX_FRAGMENTS
    assert ctx.truncated is True
    assert "cursor 19" in [f.text for f in ctx.prompts]            # newest kept
    assert "claude 0" not in [f.text for f in ctx.prompts]         # oldest dropped


def test_nothing_is_marked_cut_when_everything_fits(fake_home, project):
    _chat(fake_home, project, "c1", ["a", "b"])
    assert gather(project).truncated is False


# -- whose chats count ----------------------------------------------------------------------------

def test_another_projects_chats_are_not_this_projects_prompts(fake_home, project, tmp_path):
    other = str(tmp_path / "elsewhere")
    os.makedirs(other)
    _chat(fake_home, project, "mine", ["about this project"])
    _chat(fake_home, other, "theirs", ["about another project"])
    assert [f.text for f in gather(project).prompts] == ["about this project"]


def test_a_chat_older_than_the_window_is_left_out(fake_home, project):
    old_ms = NOW_MS - 30 * 24 * HOUR
    _chat(fake_home, project, "ancient", ["a month ago"], updated_ms=old_ms)
    _chat(fake_home, project, "fresh", ["today"])
    assert [f.text for f in gather(project).prompts] == ["today"]


def test_a_chat_with_no_transcript_is_skipped_not_fatal(fake_home, project):
    _chat(fake_home, project, "ghost", ["lost"])
    os.remove(os.path.join(fake_home, ".cursor", "projects", "any-name",
                           "agent-transcripts", "ghost", "ghost.jsonl"))
    _chat(fake_home, project, "real", ["kept"])
    assert [f.text for f in gather(project).prompts] == ["kept"]


def test_prompts_the_manager_composed_itself_are_not_the_persons(fake_home, project):
    """Its commit-message and review calls run through `agent -p` and leave a
    transcript here; their user turn is the Manager talking."""
    _chat(fake_home, project, "auto", ["[System instructions]\nbe terse\n\n[User request]\ndiff..."])
    _chat(fake_home, project, "human", ["please fix the build"])
    assert [f.text for f in gather(project).prompts] == ["please fix the build"]


# -- prose is never the person's words -----------------------------------------------------------------

def test_cursor_prose_is_prose_and_never_a_prompt(fake_home, project):
    _chat(fake_home, project, "c1", ["do it"], answers=["I did it and also this"])
    ctx = gather(project, include_prose=True)
    assert [f.text for f in ctx.prompts] == ["do it"]
    assert [f.text for f in ctx.prose] == ["I did it and also this"]
    assert ctx.prose[0].source == SOURCE_CURSOR_PROSE


def test_prose_is_not_gathered_unless_asked_for(fake_home, project):
    _chat(fake_home, project, "c1", ["do it"], answers=["an answer"])
    assert gather(project).prose == ()


def test_prompts_can_be_switched_off_leaving_only_prose(fake_home, project):
    _chat(fake_home, project, "c1", ["do it"], answers=["an answer"])
    ctx = gather(project, include_prompts=False, include_prose=True)
    assert ctx.prompts == () and [f.text for f in ctx.prose] == ["an answer"]


def test_every_agents_prose_is_recognised_as_prose():
    assert SOURCE_TRANSCRIPT_PROSE in PROSE_SOURCES and SOURCE_CURSOR_PROSE in PROSE_SOURCES
    assert is_prose(Fragment("x", SOURCE_CURSOR_PROSE))
    assert not is_prose(Fragment("x", SOURCE_CURSOR_PROMPT))
    assert not is_prose(Fragment("x", SOURCE_NOTE_PROMPT))


def test_the_grounding_block_files_cursor_prose_under_assistant_notes_not_asked_for(
        fake_home, project):
    """The hazard this guards: the consumer compared against Claude's prose
    constant alone, so Cursor's prose would have been listed as the person's words."""
    from helpers.doc_grounding import build_session_block
    _chat(fake_home, project, "c1", ["fix the login bug"], answers=["I rewrote the whole module"])
    block = build_session_block(project, include_prose=True)
    asked, said = block.split("**Assistant notes")
    assert "fix the login bug" in asked and "I rewrote the whole module" not in asked
    assert "I rewrote the whole module" in said


def test_the_grounding_block_names_every_origin_that_contributed(fake_home, project):
    from helpers.doc_grounding import build_session_block
    _note(project, ["claude ask"], datetime.datetime.now() - datetime.timedelta(hours=2))
    _chat(fake_home, project, "c1", ["cursor ask"], updated_ms=NOW_MS - HOUR)
    block = build_session_block(project)
    assert "source: session notes + Cursor transcripts" in block
    assert "claude ask" in block and "cursor ask" in block


def test_a_cursor_only_window_is_not_described_as_a_claude_note(fake_home, project):
    from helpers.doc_grounding import build_session_block
    _chat(fake_home, project, "c1", ["cursor ask"])
    block = build_session_block(project)
    assert "source: Cursor transcripts" in block and "session notes" not in block


# -- the old behaviour is intact --------------------------------------------------------------------------

def test_a_project_with_no_cursor_chats_gathers_exactly_what_it_did_before(fake_home, project):
    _note(project, ["only claude"], datetime.datetime.now() - datetime.timedelta(hours=1))
    ctx = gather(project)
    assert [f.text for f in ctx.prompts] == ["only claude"]
    assert ctx.sources == (LABEL_NOTE,) and ctx.from_note is True


def test_a_fragment_equals_the_same_words_whatever_the_clock_says():
    """`when` is not part of identity: every existing caller compares text+source."""
    early = Fragment("a", SOURCE_NOTE_PROMPT, datetime.datetime(2020, 1, 1))
    late = Fragment("a", SOURCE_NOTE_PROMPT, datetime.datetime(2030, 1, 1))
    assert early == late and Fragment("a", SOURCE_NOTE_PROMPT) == early


def test_a_failing_cursor_scan_leaves_the_claude_result_untouched(fake_home, project, monkeypatch):
    _note(project, ["claude ask"], datetime.datetime.now() - datetime.timedelta(hours=1))
    monkeypatch.setattr("helpers.cursor_tasks.scan_cursor_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx = gather(project)
    assert [f.text for f in ctx.prompts] == ["claude ask"]
    assert ctx.sources == (LABEL_NOTE,)


def test_gather_never_raises_whatever_the_cursor_folder_holds(fake_home, project):
    os.makedirs(os.path.join(fake_home, ".cursor", "chats", "b", "bad"))
    with open(os.path.join(fake_home, ".cursor", "chats", "b", "bad", "meta.json"), "w") as fh:
        fh.write("{ not json")
    assert gather(project).fragments == ()
