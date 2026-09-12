"""Guards on the session note and the shared hook machinery.

The ones that matter most are not the extraction cases. They are
`test_off_removes_only_our_entry`, because a toggle that does not turn the
behaviour off is not a toggle; `test_off_removes_the_script_too`, because a
leftover script makes every later audit report a drift the user deliberately
created; and `test_it_imports_nothing_that_drafts`, which keeps the note a
reusable artifact rather than a private detail of one consumer.
"""

import ast
import json
import os
import subprocess
import sys

import pytest

from helpers import claude_hooks, session_note as sn


@pytest.fixture(scope="module")
def note():
    """The hook, imported from the rendered script — not a second copy."""
    return sn.predicate_module()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    return root


def _transcript(path, prompts, extra=()):
    with open(path, "w", encoding="utf-8") as handle:
        for said in prompts:
            handle.write(json.dumps(
                {"type": "user", "message": {"content": said}}) + "\n")
        for line in extra:
            handle.write(json.dumps(line) + "\n")
    return str(path)


# ── Which project a note belongs to ───────────────────────────────────────

def test_an_empty_cwd_writes_nothing_rather_than_guessing(note):
    """`os.path.abspath("")` is the hook process's directory.

    Without a guard ahead of it, a payload with no cwd would file the note
    against wherever the hook happened to run.
    """
    assert note.project_root("") == ""
    assert note.project_root(None) == ""


def test_the_project_is_the_git_root_of_the_declared_cwd(note, repo):
    deep = repo / "src" / "helpers"
    deep.mkdir(parents=True)
    assert note.project_root(str(deep)) == str(repo)


def test_a_directory_outside_any_repository_is_not_a_project(note, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert note.project_root(str(plain)) == ""


def test_a_worktree_pointer_file_counts_as_a_repository(note, tmp_path):
    """`.git` is a FILE in a worktree — the project's `_is_local_git_repo` rule."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../real/.git\n", encoding="utf-8")
    assert note.project_root(str(worktree)) == str(worktree)


# ── What it captures ──────────────────────────────────────────────────────

def test_only_the_users_own_text_is_captured(note, tmp_path):
    path = _transcript(tmp_path / "t.jsonl", ["first ask", "second ask"], extra=[
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "262 of these in a real session"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "assistant prose"}]}},
    ])
    prompts, truncated = note.extract_prompts(path)
    assert prompts == ["first ask", "second ask"]
    assert truncated is False
    assert "assistant prose" not in "".join(prompts)


def test_text_blocks_inside_a_user_message_are_captured(note, tmp_path):
    """A prompt sent with an image arrives as blocks, not a bare string."""
    path = tmp_path / "t.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "look at this"}]}}) + "\n")
    prompts, _ = note.extract_prompts(str(path))
    assert prompts == ["look at this"]


def test_prompt_count_and_length_are_capped(note, tmp_path):
    many = ["ask %d %s" % (i, "x" * 5000) for i in range(note.MAX_PROMPTS + 10)]
    path = _transcript(tmp_path / "t.jsonl", many)
    prompts, truncated = note.extract_prompts(path)
    assert len(prompts) == note.MAX_PROMPTS
    assert truncated is True
    assert all(len(p) <= note.MAX_PROMPT_CHARS for p in prompts)
    # The tail is kept: recent prompts describe the work being committed.
    assert prompts[-1].startswith("ask %d" % (note.MAX_PROMPTS + 9))


def test_a_missing_transcript_is_silence_not_a_crash(note, tmp_path):
    assert note.extract_prompts(str(tmp_path / "nope.jsonl")) == ([], False)


# ── Writing the note ──────────────────────────────────────────────────────

def test_the_same_session_replaces_rather_than_accumulates(note, repo, tmp_path):
    path = _transcript(tmp_path / "t.jsonl", ["the ask"])
    payload = {"cwd": str(repo), "transcript_path": path, "session_id": "s1"}
    note.record(payload)
    note.record(payload)
    data = json.load(open(note.os.path.join(str(repo), ".claude",
                                            "session-note.json"), encoding="utf-8"))
    assert len(data["sessions"]) == 1


def test_the_note_is_a_rolling_window(note, repo, tmp_path):
    path = _transcript(tmp_path / "t.jsonl", ["the ask"])
    for index in range(note.MAX_SESSIONS + 5):
        note.record({"cwd": str(repo), "transcript_path": path,
                     "session_id": "s%d" % index})
    data = json.load(open(os.path.join(str(repo), ".claude", "session-note.json"),
                          encoding="utf-8"))
    assert len(data["sessions"]) == note.MAX_SESSIONS
    assert data["sessions"][-1]["session_id"] == "s%d" % (note.MAX_SESSIONS + 4)


def test_a_malformed_note_is_left_exactly_as_it_is(note, repo, tmp_path):
    """A Stop hook must never corrupt the artifact it produces."""
    directory = repo / ".claude"
    directory.mkdir()
    broken = directory / "session-note.json"
    broken.write_text("{ not json at all", encoding="utf-8")
    path = _transcript(tmp_path / "t.jsonl", ["the ask"])
    assert note.record({"cwd": str(repo), "transcript_path": path,
                        "session_id": "s1"}) == ""
    assert broken.read_text(encoding="utf-8") == "{ not json at all"


def test_nothing_is_written_outside_a_repository(note, tmp_path):
    path = _transcript(tmp_path / "t.jsonl", ["the ask"])
    assert note.record({"cwd": str(tmp_path), "transcript_path": path,
                        "session_id": "s1"}) == ""


def test_a_session_with_no_prompts_writes_nothing(note, repo, tmp_path):
    path = _transcript(tmp_path / "t.jsonl", [])
    assert note.record({"cwd": str(repo), "transcript_path": path,
                        "session_id": "s1"}) == ""


def test_fail_open_on_malformed_stdin(note):
    proc = subprocess.run([sys.executable, note.__file__], input="not json",
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── The toggle, both ways ─────────────────────────────────────────────────

@pytest.fixture
def paths(tmp_path):
    return (str(tmp_path / "settings.json"), str(tmp_path / "hooks" / "note.py"))


def _settings(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


OTHER_STOP_HOOK = {"hooks": [{"type": "command", "command": "python",
                              "args": ["C:/proj/.claude/auto-commit-helper.py"]}]}


def test_on_installs_one_owned_entry_beside_other_stop_hooks(paths):
    settings_path, script_path = paths
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump({"hooks": {"Stop": [OTHER_STOP_HOOK]}}, handle)

    ok, error, _actions = sn.install(sys.executable, settings_path, script_path)
    assert (ok, error) == (True, "")
    entries = _settings(settings_path)["hooks"]["Stop"]
    assert len(entries) == 2
    assert OTHER_STOP_HOOK in entries
    assert sn.installed_state(settings_path, script_path) == (sn.CURRENT, "")


def test_off_removes_only_our_entry(paths):
    """A toggle that does not stop the behaviour is a preference, not a toggle."""
    settings_path, script_path = paths
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump({"hooks": {"Stop": [OTHER_STOP_HOOK]}}, handle)
    sn.install(sys.executable, settings_path, script_path)

    ok, error, _actions = sn.uninstall(settings_path, script_path)
    assert (ok, error) == (True, "")
    entries = _settings(settings_path)["hooks"]["Stop"]
    assert entries == [OTHER_STOP_HOOK], "an unrelated Stop hook was disturbed"
    assert sn.installed_state(settings_path, script_path)[0] == sn.ABSENT


def test_off_removes_the_script_too(paths):
    """Otherwise every later audit reports a drift the user deliberately created.

    `installed_state` distinguishes never-installed (silence) from
    installed-then-unregistered (loud), so a leftover script turns a deliberate
    OFF into a permanent false alarm.
    """
    settings_path, script_path = paths
    sn.install(sys.executable, settings_path, script_path)
    assert os.path.exists(script_path)
    sn.uninstall(settings_path, script_path)
    assert not os.path.exists(script_path)


def test_turning_off_something_already_off_is_success(paths):
    settings_path, script_path = paths
    ok, error, actions = sn.uninstall(settings_path, script_path)
    assert (ok, error) == (True, "")
    assert any("Nothing to remove" in line for line in actions)


def test_uninstall_refuses_a_settings_file_it_cannot_parse(paths):
    settings_path, script_path = paths
    with open(settings_path, "w", encoding="utf-8") as handle:
        handle.write("{ broken")
    ok, error, _actions = sn.uninstall(settings_path, script_path)
    assert ok is False
    assert "Refusing to write" in error
    assert open(settings_path, encoding="utf-8").read() == "{ broken"


def test_an_empty_event_list_is_not_left_behind(paths):
    settings_path, script_path = paths
    sn.install(sys.executable, settings_path, script_path)
    sn.uninstall(settings_path, script_path)
    assert "Stop" not in _settings(settings_path).get("hooks", {})


def test_a_foreign_stop_hook_is_never_claimed(paths):
    settings_path, script_path = paths
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump({"hooks": {"Stop": [OTHER_STOP_HOOK]}}, handle)
    assert claude_hooks.is_owned_entry(sn.SPEC, OTHER_STOP_HOOK, script_path) is False
    assert sn.installed_state(settings_path, script_path)[0] == sn.ABSENT


# ── Layering ──────────────────────────────────────────────────────────────

def test_it_imports_nothing_that_drafts():
    """The note is a reusable artifact, not a private detail of one consumer.

    Phase C's grounding imports THIS; the arrow must not point back, or the
    same session context cannot later serve commit messages, the Ask tab or the
    doc drafter without dragging PR-drafting along.
    """
    source = open(sn.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update("%s.%s" % (node.module, a.name) for a in node.names)
    for forbidden in ("pr_draft", "commit_messages", "doc_grounding",
                      "doc_drafter", "llm"):
        assert not any(forbidden in name for name in imported), (
            "session_note imports %s; the dependency runs one way" % forbidden)


def test_read_note_round_trips(repo, tmp_path, note):
    path = _transcript(tmp_path / "t.jsonl", ["why this change happened"])
    note.record({"cwd": str(repo), "transcript_path": path, "session_id": "s1"})
    data = sn.read_note(str(repo))
    assert data["sessions"][0]["prompts"] == ["why this change happened"]
    assert sn.read_note(str(tmp_path / "nowhere")) is None
