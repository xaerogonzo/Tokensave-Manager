"""Tests for helpers/handoff_ledger.py -- the shared, append-only handoff ledger.

What matters is what must not happen: a retry writing twice, two agents
finishing together corrupting a line, an old file growing without bound, a
malformed line vanishing without a count, and an agent's claim being filed as a
fact. Each has its own test, and the concurrency one uses real processes.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from helpers import handoff_ledger as hl
from helpers.handoff_ledger import (
    LedgerError,
    append,
    build_entry,
    clear,
    is_ignored,
    ledger_path,
    normalize_agent,
    publish,
    read,
)

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def _entry(**over):
    base = dict(agent="claude", session_id="s1", goal="ship the thing",
                decisions=["use a ledger"], next_steps=["write tests"])
    base.update(over)
    return build_entry(**base)


def _lines(project):
    with open(ledger_path(project), "rb") as fh:
        return [ln for ln in fh.read().split(b"\n") if ln.strip()]


# -- building ---------------------------------------------------------------------

def test_an_entry_has_the_documented_shape():
    entry = _entry()
    for key in ("schema_version", "handoff_id", "kind", "agent", "session_id", "ts",
                "branch", "base_commit", "goal", "decisions", "next_steps",
                "open_questions", "agent_reported_files", "observed_changed_files",
                "provenance"):
        assert key in entry
    assert entry["schema_version"] == hl.SCHEMA_VERSION
    assert entry["kind"] == "handoff" and entry["provenance"] == {"source": "published"}
    assert entry["handoff_id"].startswith("h") and len(entry["handoff_id"]) == 13


def test_the_id_comes_from_the_content_so_a_retry_is_the_same_entry():
    first = _entry()
    time.sleep(0.01)
    again = _entry(branch="other", observed=["M x.py"])     # measured state differs
    assert first["handoff_id"] == again["handoff_id"]


def test_different_content_or_a_different_agent_is_a_different_id():
    assert _entry()["handoff_id"] != _entry(goal="something else")["handoff_id"]
    assert _entry()["handoff_id"] != _entry(agent="cursor")["handoff_id"]
    assert _entry()["handoff_id"] != _entry(session_id="s2")["handoff_id"]


def test_an_agents_file_claims_are_kept_apart_from_what_was_observed():
    entry = _entry(files=["src/a.py"], observed=["M src/b.py"])
    assert entry["agent_reported_files"] == ["src/a.py"]
    assert entry["observed_changed_files"] == ["M src/b.py"]


@pytest.mark.parametrize("good", ["claude", "cursor", "codex-2", "my_agent"])
def test_agent_ids_that_are_accepted(good):
    assert normalize_agent(good) == good


@pytest.mark.parametrize("bad", ["", "  ", "Has Space", "a/b", "x" * 40, "-lead", None])
def test_agent_ids_that_are_refused(bad):
    with pytest.raises(LedgerError):
        normalize_agent(bad)


def test_agent_ids_are_lowercased():
    assert normalize_agent("  Claude ") == "claude"


def test_an_empty_handoff_is_refused():
    with pytest.raises(LedgerError, match="nothing to record"):
        build_entry(agent="claude")
    with pytest.raises(LedgerError):
        build_entry(agent="claude", goal="   ", decisions=["  "])


def test_a_session_marker_may_be_empty_because_a_hook_has_nothing_to_summarise():
    entry = build_entry(agent="cursor", kind="session", session_id="x",
                        source="hook", extra={"reason": "completed"})
    assert entry["kind"] == "session" and entry["provenance"]["reason"] == "completed"


def test_an_unknown_kind_is_refused():
    with pytest.raises(LedgerError):
        build_entry(agent="claude", kind="memo", goal="x")


@pytest.mark.parametrize("field", ["decisions", "next_steps", "open_questions", "files"])
def test_a_list_field_must_be_a_list_of_strings(field):
    with pytest.raises(LedgerError):
        _entry(**{field: "just a string"})
    with pytest.raises(LedgerError):
        _entry(**{field: [1, 2]})


def test_what_was_cut_is_named_not_silent():
    entry = _entry(goal="g" * 900, decisions=["d" * 900] * 30)
    assert len(entry["goal"]) <= hl.MAX_GOAL_CHARS and entry["goal"].endswith("...")
    assert len(entry["decisions"]) == hl.MAX_ITEMS
    assert all(len(d) <= hl.MAX_ITEM_CHARS for d in entry["decisions"])
    assert entry["truncated"] == ["decisions", "goal"]


def test_an_entry_that_fits_has_no_truncated_key():
    assert "truncated" not in _entry()


def test_secrets_are_masked_in_every_text_field():
    key = "sk-abcdefghijklmnop1234"
    entry = _entry(goal=f"use {key}", decisions=[f"key {key}"], next_steps=[key],
                   open_questions=[f"password = hunter2hunter2 {key}"], files=[key])
    assert key not in json.dumps(entry) and "hunter2hunter2" not in json.dumps(entry)
    assert "[redacted]" in entry["goal"]


def test_newlines_are_flattened_so_one_item_is_one_line():
    entry = _entry(decisions=["line one\nline two\r\n  line three"])
    assert entry["decisions"] == ["line one line two line three"]


# -- appending -----------------------------------------------------------------------

def test_append_writes_one_parsable_line_and_creates_the_directory(project):
    result = append(project, _entry())
    assert result.ok and not result.duplicate and result.path == ledger_path(project)
    lines = _lines(project)
    assert len(lines) == 1 and json.loads(lines[0])["goal"] == "ship the thing"


def test_a_retry_writes_nothing_twice(project):
    first = append(project, _entry())
    second = append(project, _entry())
    assert first.ok and second.ok and second.duplicate
    assert second.handoff_id == first.handoff_id
    assert len(_lines(project)) == 1


def test_distinct_entries_each_get_a_line_in_order(project):
    for n in range(3):
        append(project, _entry(goal=f"goal {n}"))
    assert [json.loads(ln)["goal"] for ln in _lines(project)] == ["goal 0", "goal 1", "goal 2"]


def test_an_interrupted_earlier_write_cannot_glue_to_the_next_entry(project):
    append(project, _entry(goal="first"))
    with open(ledger_path(project), "ab") as fh:
        fh.write(b'{"handoff_id":"hpartial","goal":"cut off mid-wri')    # no newline
    append(project, _entry(goal="second"))
    result = read(project)
    assert [e["goal"] for e in result.entries] == ["first", "second"]
    assert result.bad_lines == 1                      # the partial line, counted


def test_a_non_ascii_goal_round_trips(project):
    append(project, _entry(goal="résumé — naïve ✓"))
    assert read(project).entries[0]["goal"] == "résumé — naïve ✓"


# -- retention --------------------------------------------------------------------------

def test_over_the_entry_bound_the_file_is_cut_to_the_newest(project, monkeypatch):
    monkeypatch.setattr(hl, "MAX_ENTRIES", 5)
    monkeypatch.setattr(hl, "KEEP_ENTRIES", 3)
    trimmed = 0
    for n in range(6):
        trimmed += append(project, _entry(goal=f"goal {n}")).trimmed
    goals = [e["goal"] for e in read(project).entries]
    assert goals == ["goal 3", "goal 4", "goal 5"]
    assert trimmed == 3


def test_over_the_byte_bound_the_file_is_cut_too(project, monkeypatch):
    monkeypatch.setattr(hl, "MAX_BYTES", 1500)
    monkeypatch.setattr(hl, "KEEP_ENTRIES", 2)
    for n in range(8):
        append(project, _entry(goal=f"goal {n} " + "x" * 300))
    assert len(read(project).entries) <= 8 and os.path.getsize(ledger_path(project)) < 4000


def test_trimming_keeps_unparsable_lines_it_does_not_judge_them(project, monkeypatch):
    monkeypatch.setattr(hl, "MAX_ENTRIES", 3)
    monkeypatch.setattr(hl, "KEEP_ENTRIES", 3)
    append(project, _entry(goal="a"))
    with open(ledger_path(project), "ab") as fh:
        fh.write(b"garbage line\n")
    append(project, _entry(goal="b"))
    append(project, _entry(goal="c"))             # 4 lines > 3 -> keep newest 3
    assert read(project).bad_lines == 1


# -- locking ----------------------------------------------------------------------------

def test_the_lock_is_released_after_an_append(project):
    append(project, _entry())
    assert not os.path.exists(ledger_path(project) + hl.LOCK_SUFFIX)


def test_a_stale_lock_is_broken(project):
    os.makedirs(os.path.dirname(ledger_path(project)))
    lock = ledger_path(project) + hl.LOCK_SUFFIX
    open(lock, "w").close()
    old = time.time() - hl.STALE_LOCK_SECONDS - 5
    os.utime(lock, (old, old))
    assert append(project, _entry()).ok


def test_a_live_lock_times_out_with_a_message_not_a_hang(project, monkeypatch):
    os.makedirs(os.path.dirname(ledger_path(project)))
    open(ledger_path(project) + hl.LOCK_SUFFIX, "w").close()    # fresh, so live
    monkeypatch.setattr(hl, "LOCK_TIMEOUT_SECONDS", 0.2)
    result = append(project, _entry())
    assert not result.ok and "locked" in result.error
    assert not os.path.exists(ledger_path(project))


def test_two_agents_writing_at_once_never_corrupt_a_line(project):
    """Real processes, not threads: this is the failure two agents ending together
    would actually produce."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from helpers.handoff_ledger import append, build_entry;"
        "who, project = sys.argv[2], sys.argv[3];"
        "[append(project, build_entry(agent=who, goal=f'{who} goal {n}')) for n in range(8)]")
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code, SRC, who, project])
             for who in ("claude", "cursor", "codex", "gemini", "other")]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    result = read(project)
    assert result.bad_lines == 0
    assert len(result.entries) == 40
    assert len({e["handoff_id"] for e in result.entries}) == 40


# -- reading --------------------------------------------------------------------------------

def test_a_missing_ledger_is_absent_not_empty(project):
    result = read(project)
    assert result.state == hl.STATE_ABSENT and result.entries == ()


def test_an_unreadable_ledger_is_not_reported_as_absent(project):
    os.makedirs(ledger_path(project))             # a directory where the file should be
    result = read(project)
    assert result.state == hl.STATE_UNREADABLE and result.error


def test_limit_returns_the_newest_entries_oldest_first(project):
    for n in range(5):
        append(project, _entry(goal=f"goal {n}"))
    assert [e["goal"] for e in read(project, limit=2).entries] == ["goal 3", "goal 4"]
    assert read(project, limit=0).entries == ()


def test_lines_that_are_not_entries_are_counted_never_dropped_silently(project):
    append(project, _entry())
    with open(ledger_path(project), "ab") as fh:
        fh.write(b"not json\n[1,2,3]\n{\"no_id\":true}\n")
    result = read(project)
    assert len(result.entries) == 1 and result.bad_lines == 3


# -- clearing ------------------------------------------------------------------------------------

def test_clear_removes_the_ledger(project):
    append(project, _entry())
    assert clear(project) == (True, "")
    assert read(project).state == hl.STATE_ABSENT


def test_clearing_an_absent_ledger_is_fine(project):
    assert clear(project) == (True, "")


# -- git --------------------------------------------------------------------------------------------

def _git(project, *args):
    subprocess.run(["git", "-C", project, *args], check=True, capture_output=True, text=True)


def _repo(project, ignore=False):
    _git(project, "init", "-q", "-b", "work")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    if ignore:
        with open(os.path.join(project, ".gitignore"), "w") as fh:
            fh.write(".tokensave/\n")
    with open(os.path.join(project, "a.txt"), "w") as fh:
        fh.write("1")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "first")


def test_is_ignored_has_three_answers(project, tmp_path):
    assert is_ignored(str(tmp_path)) is None                        # not a repository
    _repo(project)
    assert is_ignored(project) is False
    with open(os.path.join(project, ".gitignore"), "w") as fh:
        fh.write(".tokensave/\n")
    assert is_ignored(project) is True


def test_publish_measures_the_repository_itself(project):
    _repo(project, ignore=True)
    with open(os.path.join(project, "dirty.txt"), "w") as fh:
        fh.write("x")
    result, warnings = publish(project, agent="cursor", session_id="s9",
                               goal="g", decisions=["d"], files=["claimed.py"])
    assert result.ok and warnings == []
    entry = read(project).entries[0]
    assert entry["branch"] == "work" and len(entry["base_commit"]) == 40
    assert any("dirty.txt" in f for f in entry["observed_changed_files"])
    assert entry["agent_reported_files"] == ["claimed.py"]           # the claim, apart


def _nested(project):
    """A plain folder inside someone else's repository: no `.git` of its own."""
    _repo(project, ignore=False)
    child = os.path.join(project, "child")
    os.makedirs(child)
    return child


def test_a_nested_plain_folder_still_warns_because_its_parent_repo_would_commit_it(project):
    child = _nested(project)
    result, warnings = publish(child, agent="claude", goal="g")
    assert result.ok and any("NOT git-ignored" in w for w in warnings)
    assert result.path == os.path.join(child, ".tokensave", "handoff.jsonl")


def test_a_ledger_inside_a_repository_has_nothing_to_warn_about(project):
    _repo(project, ignore=False)                      # no .gitignore at all
    result, warnings = publish(project, agent="claude", goal="g")
    assert result.ok and not any("git-ignored" in w for w in warnings)


def test_publish_outside_a_repository_still_writes_and_says_why_it_is_thin(project):
    result, warnings = publish(project, agent="claude", goal="g")
    assert result.ok
    assert any("git did not answer" in w for w in warnings)
    assert read(project).entries[0]["branch"] == ""


def test_publishing_the_same_thing_twice_leaves_one_entry(project):
    _repo(project, ignore=True)
    publish(project, agent="claude", goal="g", decisions=["d"])
    again, _ = publish(project, agent="claude", goal="g", decisions=["d"])
    assert again.duplicate and len(read(project).entries) == 1


def test_publishing_nothing_raises_rather_than_writing_noise(project):
    with pytest.raises(LedgerError):
        publish(project, agent="claude")
    assert read(project).state == hl.STATE_ABSENT


def test_a_repository_with_no_commits_gets_a_one_line_warning_not_gits_essay(project):
    _git(project, "init", "-q", "-b", "work")
    result, warnings = publish(project, agent="claude", goal="g")
    assert result.ok
    why = [w for w in warnings if "git did not answer" in w]
    assert why and "\n" not in why[0]


# -- where it lives: the git common dir, so worktrees share it ---------------------------------

def test_in_a_repository_the_ledger_is_inside_dot_git_where_git_cannot_track_it(project):
    _repo(project, ignore=False)
    result, _ = publish(project, agent="claude", goal="g")
    assert result.path == os.path.join(project, ".git", "tokensave", "handoff.jsonl")
    assert hl.in_git_dir(project) is True
    assert _git_out(project, "status", "--short").strip() == ""       # no ignore rule needed
    assert not os.path.exists(os.path.join(project, ".tokensave"))


def _git_out(project, *args):
    return subprocess.run(["git", "-C", project, *args], check=True,
                          capture_output=True, text=True).stdout


def test_a_worktree_and_its_main_checkout_share_one_ledger(project, tmp_path):
    _repo(project, ignore=False)
    wt = str(tmp_path / "wt")
    _git(project, "worktree", "add", "-q", "-b", "feature", wt)
    assert os.path.isfile(os.path.join(wt, ".git"))                  # a pointer file, not a dir
    from_worktree, _ = publish(wt, agent="cursor", goal="written in the worktree")
    assert from_worktree.ok
    seen_from_main = read(project)
    assert [e["goal"] for e in seen_from_main.entries] == ["written in the worktree"]
    assert from_worktree.path == seen_from_main.path == ledger_path(project) == ledger_path(wt)


def test_a_sibling_worktree_sees_what_another_one_wrote(project, tmp_path):
    _repo(project, ignore=False)
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    _git(project, "worktree", "add", "-q", "-b", "fa", a)
    _git(project, "worktree", "add", "-q", "-b", "fb", b)
    publish(a, agent="claude", goal="from a")
    publish(b, agent="cursor", goal="from b")
    assert [e["goal"] for e in read(a).entries] == ["from a", "from b"]
    assert [e["goal"] for e in read(b).entries] == ["from a", "from b"]


def test_each_entry_still_records_which_worktree_branch_it_was_written_from(project, tmp_path):
    _repo(project, ignore=False)
    wt = str(tmp_path / "wt")
    _git(project, "worktree", "add", "-q", "-b", "feature", wt)
    publish(project, agent="claude", goal="on main")
    publish(wt, agent="cursor", goal="on feature")
    assert [e["branch"] for e in read(project).entries] == ["work", "feature"]


def test_outside_a_repository_it_falls_back_to_the_project_folder(project):
    assert ledger_path(project) == os.path.join(project, ".tokensave", "handoff.jsonl")
    assert hl.in_git_dir(project) is False


def test_a_nested_plain_folder_is_not_filed_inside_its_parents_dot_git(project):
    """git walks upward; without the own-`.git` rule this ledger would land in the
    parent repository and be shared with unrelated folders."""
    child = _nested(project)
    assert hl.in_git_dir(child) is False
    assert ".git" not in ledger_path(child).split(os.sep)


def test_the_git_answer_is_remembered_but_a_miss_is_not(project, monkeypatch):
    hl._DIR_CACHE.clear()
    calls = []
    real = subprocess.run

    def counting(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)
    monkeypatch.setattr(hl.subprocess, "run", counting)
    assert ledger_path(project).endswith(os.path.join(".tokensave", "handoff.jsonl"))
    assert calls == []                                    # no `.git`, so git is not even asked
    _repo(project, ignore=False)                          # a repository appears later
    first = ledger_path(project)
    second = ledger_path(project)
    assert first == second and ".git" in first
    assert len([c for c in calls if "--git-common-dir" in c]) == 1


def test_git_missing_falls_back_and_is_not_remembered(project, monkeypatch):
    _repo(project, ignore=False)
    hl._DIR_CACHE.clear()

    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(hl.subprocess, "run", boom)
    assert ledger_path(project) == os.path.join(project, ".tokensave", "handoff.jsonl")
    monkeypatch.undo()
    assert ".git" in ledger_path(project)                # recovered: the failure was not cached


def test_an_older_git_without_path_format_is_asked_again_without_it(project, monkeypatch):
    _repo(project, ignore=False)
    hl._DIR_CACHE.clear()
    real = subprocess.run

    def old_git(argv, **kw):
        if any(a.startswith("--path-format") for a in argv):
            return subprocess.CompletedProcess(argv, 129, "", "unknown option")
        return real(argv, **kw)
    monkeypatch.setattr(hl.subprocess, "run", old_git)
    assert ledger_path(project) == os.path.join(project, ".git", "tokensave", "handoff.jsonl")


def test_the_lock_and_temp_files_live_beside_the_ledger_inside_dot_git(project):
    _repo(project, ignore=False)
    publish(project, agent="claude", goal="g")
    leftovers = os.listdir(os.path.dirname(ledger_path(project)))
    assert leftovers == ["handoff.jsonl"]                # no .lock, no .tmp left behind


# -- session markers are bounded on their own ----------------------------------------------------

def test_a_session_marker_has_no_summary_and_carries_only_what_the_hook_was_told(project):
    result = hl.record_session(project, agent="cursor", session_id="s1", reason="completed",
                               final_status="completed", transcript_path="C:/t/s1.jsonl")
    assert result.ok
    entry = read(project).entries[0]
    assert entry["kind"] == "session" and entry["provenance"]["source"] == "hook"
    assert entry["goal"] == "" and entry["decisions"] == [] and entry["next_steps"] == []


def test_markers_beyond_their_cap_lose_their_oldest_and_never_a_handoff(project, monkeypatch):
    monkeypatch.setattr(hl, "MAX_SESSION_MARKERS", 3)
    append(project, _entry(goal="a real handoff"))
    for n in range(6):
        hl.record_session(project, agent="cursor", session_id=f"s{n}")
    entries = read(project).entries
    markers = [e["session_id"] for e in entries if e["kind"] == "session"]
    assert markers == ["s3", "s4", "s5"]
    assert [e["goal"] for e in entries if e["kind"] == "handoff"] == ["a real handoff"]


def test_a_flood_of_markers_cannot_push_a_handoff_out_through_the_entry_bound(project, monkeypatch):
    monkeypatch.setattr(hl, "MAX_ENTRIES", 8)
    monkeypatch.setattr(hl, "KEEP_ENTRIES", 5)
    monkeypatch.setattr(hl, "MAX_SESSION_MARKERS", 4)
    append(project, _entry(goal="keep me"))
    for n in range(30):
        hl.record_session(project, agent="cursor", session_id=f"s{n}")
    goals = [e["goal"] for e in read(project).entries if e["kind"] == "handoff"]
    assert goals == ["keep me"]


# -- the lock under Windows delete-pending ---------------------------------------------------

def test_a_lock_file_that_is_briefly_denied_is_waited_out_not_lost(project, monkeypatch):
    """Creating a file another process is deleting raises EACCES on Windows. That
    is contention; treating it as a write failure silently lost entries (2 of 25
    runs of five processes). Deterministic stand-in for the race."""
    real_open = os.open
    state = {"denied": 0}

    def flaky(path, flags, *a, **k):
        if str(path).endswith(hl.LOCK_SUFFIX) and state["denied"] < 3:
            state["denied"] += 1
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(hl.os, "open", flaky)
    result = hl.append(project, hl.build_entry(agent="claude", goal="g"))
    assert result.ok and state["denied"] == 3
    assert len(read(project).entries) == 1


def test_a_lock_that_is_really_denied_fails_as_a_write_error_not_a_lock_timeout(
        project, monkeypatch):
    def denied(path, flags, *a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(hl.os, "open", denied)
    orig = hl._locked
    monkeypatch.setattr(hl, "_locked", lambda project, timeout=0.2, git_exe="":
                        orig(project, timeout=0.2, git_exe=git_exe))
    started = hl.time.monotonic()
    result = hl.append(project, hl.build_entry(agent="claude", goal="g"))
    assert not result.ok and "could not write" in result.error
    assert hl.time.monotonic() - started < 3
