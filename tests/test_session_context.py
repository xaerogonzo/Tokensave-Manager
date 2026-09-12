"""Guards on session context and the fourth grounding block.

The ones that carry weight: `test_the_session_block_is_passed_first`, because
argument position decides what survives a fixed cap; `test_provenance_never_
blurs`, because an authoritative prompt and a heuristic sentence must not become
the same thing; and `test_the_block_claims_intent_not_explanation`, because the
heading is the claim and overclaiming here is how a draft starts asserting why a
diff exists.
"""

import datetime
import json
import os
import subprocess

import pytest

from helpers import doc_grounding, session_context as sc


def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args),
                   capture_output=True, check=False)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "first")
    return root


def _note(root, prompts, ended=None):
    directory = root / ".claude"
    directory.mkdir(exist_ok=True)
    stamp = ended or datetime.datetime.now().isoformat(timespec="seconds")
    (directory / "session-note.json").write_text(json.dumps({
        "version": 1,
        "sessions": [{"session_id": "s1", "ended_at": stamp,
                      "cwd": str(root), "prompts": prompts}],
    }), encoding="utf-8")


def _transcripts(fake_home, project_root, records):
    from helpers.claude_tasks import encode_project_path

    directory = (fake_home / ".claude" / "projects"
                 / encode_project_path(str(project_root)))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "t.jsonl"
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            record.setdefault("timestamp", now)
            handle.write(json.dumps(record) + "\n")
    return path


# ── The window ────────────────────────────────────────────────────────────

def test_the_last_commit_bounds_the_window(repo):
    when, reason = sc.last_commit_time(str(repo))
    assert reason == sc.WINDOW_SINCE_COMMIT
    assert isinstance(when, datetime.datetime)


def test_a_repository_with_no_commit_is_its_own_state(tmp_path):
    """Not defaulted to 'now', which would gather nothing exactly where there
    is most to say."""
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init")
    when, reason = sc.last_commit_time(str(empty))
    assert when is None
    assert reason == sc.WINDOW_NO_COMMITS


def test_an_unreadable_path_is_distinguished_from_no_commits(tmp_path):
    when, reason = sc.last_commit_time(str(tmp_path / "nowhere"))
    assert (when, reason) == (None, sc.WINDOW_UNREADABLE)


def test_even_a_real_commit_date_is_clamped_to_the_maximum_window():
    """A repository untouched for a year must not drag a year into a prompt."""
    ancient = datetime.datetime.now() - datetime.timedelta(days=400)
    floor, _reason = sc._floor(ancient, sc.WINDOW_SINCE_COMMIT)
    assert floor > ancient
    assert floor <= datetime.datetime.now()


# ── Source precedence and provenance ──────────────────────────────────────

def test_the_note_is_preferred_and_says_so(repo, fake_home):
    _note(repo, ["why I did the thing"])
    context = sc.gather(str(repo))
    assert context.from_note is True
    assert [f.text for f in context.prompts] == ["why I did the thing"]
    assert {f.source for f in context.prompts} == {sc.SOURCE_NOTE_PROMPT}


def test_transcripts_are_the_fallback_when_no_note_exists(repo, fake_home):
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": "mined from the transcript"}}])
    context = sc.gather(str(repo))
    assert context.from_note is False
    assert [f.text for f in context.prompts] == ["mined from the transcript"]
    assert {f.source for f in context.prompts} == {sc.SOURCE_TRANSCRIPT_PROMPT}


def test_tool_results_are_not_something_the_user_said(repo, fake_home):
    """Measured at 262 of 267 user-role records in one real session."""
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "noise"}]}},
        {"type": "user", "message": {"content": "the real ask"}}])
    context = sc.gather(str(repo))
    assert [f.text for f in context.prompts] == ["the real ask"]


def test_harness_records_are_not_something_the_user_said(repo, fake_home):
    """A second population `_user_text` structurally cannot see.

    Measured on a real window: 4 of the 9 fragments returned were plumbing —
    a compaction summary, the caveat block, a `/compact` echo and its stdout.
    Every one is `type: "user"` carrying an ordinary text block, so nothing
    about the CONTENT shape separates them from a person typing.
    """
    _transcripts(fake_home, repo, [
        {"type": "user", "isCompactSummary": True,
         "message": {"content": "This session is being continued from a "
                                "previous conversation that ran out of..."}},
        {"type": "user", "isMeta": True,
         "message": {"content": "<local-command-caveat>Caveat: the messages "
                                "below...</local-command-caveat>"}},
        {"type": "user", "message": {"content":
            "<command-name>/compact</command-name> "
            "<command-message>compact</command-message>"}},
        {"type": "user", "message": {"content":
            "<local-command-stdout>Compacted </local-command-stdout>"}},
        {"type": "user", "message": {"content": "let's do the A/B now"}}])
    context = sc.gather(str(repo))
    assert [f.text for f in context.prompts] == ["let's do the A/B now"]


def test_an_unrecognised_record_still_counts_as_the_users(repo, fake_home):
    """The filter is NEGATIVE on purpose, and this is what that buys.

    Requiring a marker such as `promptSource` to be PRESENT reads as tidier
    and would silently empty the window on every transcript written before
    that field existed. A shape nobody has seen yet must lose no real prompt.
    """
    _transcripts(fake_home, repo, [
        {"type": "user", "someFieldNobodyHasSeenYet": True,
         "message": {"content": "still a real ask"}}])
    context = sc.gather(str(repo))
    assert [f.text for f in context.prompts] == ["still a real ask"]


def test_an_oversized_transcript_is_read_from_its_tail(repo, fake_home,
                                                       monkeypatch):
    """The defect that killed this feature on its own repository.

    Candidates are sorted newest-first, so the transcript most likely to
    exceed the byte budget is the live session — the one file that can hold
    the window. Skipping it returned zero fragments, and because
    `build_session_block` renders nothing when there are no fragments, the
    `truncated` flag that would have said so was dropped on the floor: the
    caller could not tell "too big to read" from "nothing was said".

    Measured live on 2026-09-12 — the active transcript crossed the 8 MB cap
    at 07:58 and every gather afterwards came back empty, silently, on
    exactly the sessions with the most to say.
    """
    padding = [{"type": "user", "message": {"content": "old ask %d" % i}}
               for i in range(200)]
    _transcripts(fake_home, repo, padding + [
        {"type": "user", "message": {"content": "the recent ask"}}])
    monkeypatch.setattr(sc, "MAX_TRANSCRIPT_BYTES", 400)

    context = sc.gather(str(repo))
    assert [f.text for f in context.prompts][-1:] == ["the recent ask"]
    assert context.truncated is True, "a tail IS partial and must say so"


def test_a_transcript_within_budget_is_not_reported_partial(repo, fake_home):
    """The other half: reading a tail is partial, reading a file is not."""
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": "small enough"}}])
    context = sc.gather(str(repo))
    assert [f.text for f in context.prompts] == ["small enough"]
    assert context.truncated is False


def test_prose_is_off_by_default(repo, fake_home):
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": "ask"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "assistant prose"}]}}])
    assert sc.gather(str(repo)).prose == ()
    assert sc.gather(str(repo), include_prose=True).prose != ()


def test_provenance_never_blurs(repo, fake_home):
    """A note prompt and a mined sentence must not become the same thing."""
    _note(repo, ["authoritative"])
    _transcripts(fake_home, repo, [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "heuristic"}]}}])
    context = sc.gather(str(repo), include_prose=True)
    sources = {f.text: f.source for f in context.fragments}
    assert sources["authoritative"] == sc.SOURCE_NOTE_PROMPT
    assert sources["heuristic"] == sc.SOURCE_TRANSCRIPT_PROSE


def test_the_note_and_transcripts_do_not_double_up(repo, fake_home):
    """With a note present, transcripts contribute prose only."""
    _note(repo, ["the ask"])
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": "the ask"}}])
    context = sc.gather(str(repo), include_prose=True)
    assert [f.text for f in context.prompts] == ["the ask"]


def test_a_bad_project_path_is_an_empty_context_not_an_exception(fake_home):
    assert sc.gather("").fragments == ()
    assert sc.gather("/no/such/place").fragments == ()


# ── The rendered block ────────────────────────────────────────────────────

def test_the_block_claims_intent_not_explanation(repo, fake_home):
    """The heading is the claim, and it must stay narrow.

    Work spans sessions and some is done by hand, so these are fragments to
    attribute — not an explanation of the diff to assert.
    """
    _note(repo, ["make the thing faster"])
    block = doc_grounding.build_session_block(str(repo))
    assert "What the human asked for" in block
    lowered = block.lower()
    assert "why this diff" not in lowered
    assert "evidence of intent, not as an explanation" in lowered


def test_an_empty_context_renders_nothing(repo, fake_home):
    assert doc_grounding.build_session_block(str(repo)) == ""
    assert doc_grounding.build_session_block("") == ""


def test_both_sources_off_renders_nothing(repo, fake_home):
    _note(repo, ["the ask"])
    assert doc_grounding.build_session_block(
        str(repo), include_prompts=False, include_prose=False) == ""


def test_the_block_is_capped(repo, fake_home):
    _note(repo, ["x" * 400 for _ in range(40)])
    block = doc_grounding.build_session_block(str(repo))
    assert len(block) <= doc_grounding._MAX_SESSION_CHARS


def test_a_partial_gather_says_so(repo, fake_home, monkeypatch):
    monkeypatch.setattr(sc, "MAX_FRAGMENTS", 2)
    _note(repo, ["one", "two", "three", "four"])
    block = doc_grounding.build_session_block(str(repo))
    assert "**partial**" in block, "a capped gather rendered as if complete"


def test_prose_is_labelled_lower_confidence(repo, fake_home):
    _transcripts(fake_home, repo, [
        {"type": "user", "message": {"content": "ask"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "prose"}]}}])
    block = doc_grounding.build_session_block(str(repo), include_prose=True)
    assert "not the user's words" in block


def test_a_missing_commit_boundary_is_stated(tmp_path, fake_home, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init")
    _note(empty, ["the ask"])
    block = doc_grounding.build_session_block(str(empty))
    assert "maximum window" in block


# ── Wiring ────────────────────────────────────────────────────────────────

def test_the_session_block_is_passed_first(monkeypatch, repo, fake_home,
                                           mock_config):
    """Argument position decides what survives truncation.

    `build_combined_grounding` caps at a fixed size regardless of source count
    and dedup preserves first-seen order, so a session block passed last would
    be the first thing dropped on a busy project.
    """
    from helpers import pr_draft

    seen = {}

    def spy(*blocks, **kwargs):
        seen["blocks"] = blocks
        return "combined"

    monkeypatch.setattr(doc_grounding, "build_combined_grounding", spy)
    monkeypatch.setattr(pr_draft, "_session_grounding",
                        lambda *a, **k: "SESSION-BLOCK")
    monkeypatch.setattr(doc_grounding, "build_grounding_block",
                        lambda *a, **k: "ts")
    monkeypatch.setattr(doc_grounding, "build_codegraph_block",
                        lambda *a, **k: "cg")

    mock_config.raw["enable_llm_grounding"] = True
    mock_config.raw["enable_pr_grounding"] = True
    pr_draft._build_grounding_section(mock_config, "", str(repo), None)

    assert seen["blocks"][0] == "SESSION-BLOCK", (
        "the session block must be first: %r" % (seen["blocks"],))


def test_session_grounding_has_its_own_gate(repo, fake_home, mock_config):
    """Not folded into `enable_pr_grounding`.

    They answer different questions — what the code IS versus what a person
    asked for — and folding them would make "session context ON" a dead toggle
    for anyone who turned codebase grounding off because it bloated prompts.
    """
    from helpers import pr_draft
    from helpers.instruction_policy import InstructionPolicy

    _note(repo, ["the ask"])

    class Arm:
        instruction_policy = InstructionPolicy(enable_session_grounding=True)
        git_exe = "git"

    assert pr_draft._session_grounding(Arm(), str(repo)) != ""

    Arm.instruction_policy = InstructionPolicy(enable_session_grounding=False)
    assert pr_draft._session_grounding(Arm(), str(repo)) == ""


def test_the_three_grounding_keys_are_independent():
    """Use it at all / include prompts / include prose are three decisions."""
    from helpers.instruction_policy import InstructionPolicy

    policy = InstructionPolicy(enable_session_grounding=True,
                               session_grounding_prompts=False,
                               session_grounding_prose=True)
    assert policy.is_valid
    assert policy.session_grounding_prompts is False
    assert policy.session_grounding_prose is True


def test_the_ab_harness_declares_three_arms_and_a_fixed_rule():
    """Two arms cannot answer prompts-vs-prose, which is the product question."""
    import importlib.util

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(sc.__file__)))
    path = os.path.join(os.path.dirname(repo_root), "scripts",
                        "ab_rationale.py")
    spec = importlib.util.spec_from_file_location("ab_rationale", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert [name for name, _ in module.ARMS] == [
        "control", "prompts", "prompts+prose"]
    assert "defaulted OFF" in module.WINNER_RULE
    assert "never decisive" in module.WINNER_RULE
