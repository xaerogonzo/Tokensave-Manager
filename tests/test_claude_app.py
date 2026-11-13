"""The `claude://code/new` link, and what the Continue-in-Claude-app flow may claim.

Documented (support.claude.com): `claude://code/new?q=<prompt>&folder=<absolute path>`, every value
URL-encoded, `q` cut near 14,000 characters, a folder passed by link always untrusted. A community
report says that on Windows, with the app already running, the link may not open a new window. So
the flow puts the message on the clipboard first and never reports the OS accepting the link as the
composer being filled.
"""
import os
import types
from urllib.parse import parse_qs, urlsplit

import pytest

import controllers.tasks_tab as tasks_tab
import helpers.agent_handoff as ah
from helpers import claude_app
from controllers.tasks_tab import TasksController

TRICKY_FOLDER = "D:\\Random Projects\\Q&A 100% #1 \u00e9\u4e2d\\Repo"
TRICKY_MESSAGE = "Read a/b.md & c=d? 100% #1\nthen \u00e9\u4e2d continue"


# -- the link ----------------------------------------------------------------------------

def decode(url):
    parts = urlsplit(url)
    assert parts.scheme == "claude" and parts.netloc == "code" and parts.path == "/new"
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    assert sorted(query) == ["folder", "q"]
    return query["folder"][0], query["q"][0]


def test_every_value_round__trips_whatever_it_contains():
    url = claude_app.new_session_url(TRICKY_FOLDER, TRICKY_MESSAGE)
    assert decode(url) == (TRICKY_FOLDER, TRICKY_MESSAGE)


def test_nothing_is_left_unescaped_that_could_change_the_links_meaning():
    url = claude_app.new_session_url(TRICKY_FOLDER, TRICKY_MESSAGE)
    query = url.split("?", 1)[1]
    assert query.count("&") == 1 and query.count("=") == 2          # only the two separators
    assert " " not in url and "+" not in url and "#" not in url and "\n" not in url
    assert "%20" in url


def test_the_folder_must_be_absolute_and_the_message_present():
    for bad in ("", "relative/dir", "Repo"):
        with pytest.raises(ValueError, match="absolute"):
            claude_app.new_session_url(bad, "hello")
    with pytest.raises(ValueError, match="no message"):
        claude_app.new_session_url(TRICKY_FOLDER, "  \n ")


def test_a_message_that_is_really_a_brief_is_refused_not_cut():
    with pytest.raises(ValueError, match="never a brief"):
        claude_app.new_session_url(TRICKY_FOLDER, "x" * (claude_app.MAX_MESSAGE_CHARS + 1))
    claude_app.new_session_url(TRICKY_FOLDER, "x" * claude_app.MAX_MESSAGE_CHARS)


def test_registered_reads_the_protocol_handler_and_is_false_without_one(monkeypatch):
    monkeypatch.setattr(claude_app.sys, "platform", "win32")
    seen = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda root, path: seen.append(path) or Key())
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    assert claude_app.registered() is True and seen == ["Software\\Classes\\claude\\shell\\open\\command"]

    def missing(root, path):
        raise FileNotFoundError(path)
    fake.OpenKey = missing
    assert claude_app.registered() is False
    monkeypatch.setattr(claude_app.sys, "platform", "linux")
    assert claude_app.registered() is False


def test_open_url_reports_what_it_knows_the_os_accepted_it_or_could_not(monkeypatch):
    monkeypatch.setattr(claude_app.sys, "platform", "win32")
    opened = []
    monkeypatch.setattr(claude_app.os, "startfile", lambda url: opened.append(url), raising=False)
    assert claude_app.open_url("claude://code/new?x=1") == (True, "") and opened == ["claude://code/new?x=1"]

    def refuse(url):
        raise OSError("no handler")
    monkeypatch.setattr(claude_app.os, "startfile", refuse, raising=False)
    ok, why = claude_app.open_url("claude://code/new?x=1")
    assert not ok and "no handler" in why
    monkeypatch.setattr(claude_app.sys, "platform", "linux")
    assert claude_app.open_url("claude://x")[0] is False


# -- the message ------------------------------------------------------------------------

def test_the_message_names_the_brief_and_the_conversation_file_with_forward_slashes(tmp_path):
    project = str(tmp_path / "Proj")
    brief = os.path.join(project, ".tokensave", "handoffs", "cursor-s.md")
    prepared = ah.Prepared(True, brief_path=brief, project_path=project, prompt_count=3,
                           sidecar_path=brief[:-3] + ".conversation.md")
    message = ah.claude_app_message(prepared)
    assert ".tokensave/handoffs/cursor-s.md" in message and ".tokensave/handoffs/cursor-s.conversation.md" in message
    assert "\\" not in message and "Cursor session" in message
    assert len(message) < claude_app.MAX_MESSAGE_CHARS


def _deep(tmp_path, with_sidecar=True):
    project = tmp_path / "Proj With Spaces"
    brief = project / ".tokensave" / "handoffs" / "cursor-s.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("brief")
    side = brief.with_suffix(".conversation.md")
    side.write_text("conversation")
    return project, ah.Prepared(True, brief_path=str(brief), project_path=str(project), prompt_count=3,
                                sidecar_path=str(side) if with_sidecar else "")


def _paths_in(message):
    import re
    return re.findall(r"`([^`]+\.md)`", message)           # paths are backtick-quoted: spaces are safe


def test_the_message_uses_absolute_paths_that_resolve_from_any_working_directory(tmp_path, monkeypatch):
    """Measured live: for a repository with a commit Claude Desktop starts the session in a NEW
    worktree, where a relative `.tokensave/handoffs/x.md` does not exist ("File does not exist")."""
    project, prepared = _deep(tmp_path)
    worktree = project / ".claude" / "worktrees" / "cursor-session-handoff-1"
    worktree.mkdir(parents=True)
    message = ah.claude_app_message(prepared)
    paths = _paths_in(message)
    assert len(paths) == 2 and all(os.path.isabs(p) for p in paths)
    monkeypatch.chdir(worktree)
    assert all(os.path.exists(p) for p in paths)                       # found from inside the worktree
    assert not os.path.exists(".tokensave/handoffs/cursor-s.md")       # the old relative form was not


def test_the_message_says_where_the_main_checkout_is_and_that_changes_live_there(tmp_path):
    project, prepared = _deep(tmp_path)
    message = ah.claude_app_message(prepared)
    root = str(project).replace("\\", "/")
    assert "if your working directory is a worktree" in message and f"`{root}`" in message
    assert "uncommitted changes" in message and "not in your worktree" in message


def test_a_shallow_brief_names_only_the_brief_and_still_uses_an_absolute_path(tmp_path):
    project, prepared = _deep(tmp_path, with_sidecar=False)
    message = ah.claude_app_message(prepared)
    assert len(_paths_in(message)) == 1 and "conversation" not in message.split("Those are absolute")[0]


def test_even_a_very_long_project_path_keeps_the_message_far_under_the_link_limit(tmp_path):
    deep = tmp_path
    for _ in range(12):
        deep = deep / ("segment-" + "x" * 15)
    project, prepared = _deep(deep)
    message = ah.claude_app_message(prepared)
    assert len(message) < claude_app.MAX_MESSAGE_CHARS
    claude_app.new_session_url(str(project), message)


# -- the flow in the Tasks tab ---------------------------------------------------------

class _Tree:
    def __init__(self, selected):
        self._selected = tuple(selected)

    def selection(self):
        return self._selected


class _Inline:
    def __init__(self, target=None, daemon=None, **_):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def flow(monkeypatch, tmp_path):
    """A controller wired for the Claude-app handoff, every side effect recorded in order."""
    project = tmp_path / "Proj"
    project.mkdir()
    log = types.SimpleNamespace(calls=[], errors=[], infos=[], asked=[], answer=True, clip=[],
                                registered=True, open_ok=True, project=str(project))
    ctrl = object.__new__(TasksController)
    ctrl._cfg = types.SimpleNamespace(git_exe="git", claude_cli_exe="claude.cmd", cursor_cli_exe="agent.cmd",
                                      cursor_cli_model="")
    ctrl._post = lambda fn, *a: fn(*a)
    ctrl._tab = types.SimpleNamespace(
        clipboard_clear=lambda: log.calls.append("clipboard_clear") or log.clip.clear(),
        clipboard_append=lambda text: log.calls.append("clipboard") or log.clip.append(text))
    ctrl._sess_rows = {"sess:cursor:c1": {"session_id": "c1", "title": "T", "agent": "cursor",
                                          "cwd": str(project), "path": str(tmp_path / "t.jsonl")}}
    ctrl._sess_tree = _Tree(("sess:cursor:c1",))
    monkeypatch.setattr(tasks_tab.threading, "Thread", _Inline)
    monkeypatch.setattr(tasks_tab.messagebox, "showerror", lambda t, m, **k: log.errors.append((t, m)))
    monkeypatch.setattr(tasks_tab.messagebox, "showinfo", lambda t, m, **k: log.infos.append(m))
    monkeypatch.setattr(tasks_tab.messagebox, "askyesno",
                        lambda t, m, **k: log.calls.append("asked") or log.asked.append(m) or log.answer)

    def prepare(row, proj, git, source="claude"):
        log.calls.append("prepare")
        brief = os.path.join(proj, ".tokensave", "handoffs", "cursor-c1.md")
        return ah.Prepared(True, brief_path=brief, project_path=proj, prompt_count=2,
                           sidecar_path=brief[:-3] + ".conversation.md", note=getattr(log, "note", ""))
    monkeypatch.setattr(ah, "prepare", prepare)
    monkeypatch.setattr(claude_app, "registered", lambda: log.registered)

    def open_url(url):
        log.calls.append("open")
        log.url = url
        return (True, "") if log.open_ok else (False, "Windows could not open the link: no handler")
    monkeypatch.setattr(claude_app, "open_url", open_url)
    return ctrl, log


def test_nothing_is_written_copied_or_opened_before_the_person_says_yes(flow):
    ctrl, log = flow
    log.answer = False
    ctrl._sess_hand_off("claude_app")
    assert log.calls == ["asked"] and not log.infos and not log.errors


def test_the_order_is_confirm_then_write_then_clipboard_then_link_then_the_dialog(flow):
    ctrl, log = flow
    ctrl._sess_hand_off("claude_app")
    assert log.calls == ["asked", "prepare", "clipboard_clear", "clipboard", "open"]
    folder, message = decode(log.url)
    assert folder == log.project and message == log.clip[0]
    assert ".tokensave/handoffs/cursor-c1.md" in message and "conversation.md" in message
    assert len(log.infos) == 1 and message in log.infos[0] and not log.errors


def test_the_dialog_says_windows_was_asked_and_never_that_claude_opened(flow):
    ctrl, log = flow
    ctrl._sess_hand_off("claude_app")
    text = log.infos[0]
    assert "Windows was asked to open a new Claude session" in text
    assert "trust the folder" in text and "paste the message" in text and "on your clipboard" in text
    assert "has opened" not in text and "was opened" not in text


def test_a_link_windows_refuses_leaves_the_message_on_the_clipboard_and_says_how_to_continue(flow):
    ctrl, log = flow
    log.open_ok = False
    ctrl._sess_hand_off("claude_app")
    assert log.calls[-2:] == ["clipboard", "open"] and log.clip
    text = log.infos[0]
    assert "could not be opened from here" in text and "no handler" in text
    assert "start a new session in" in text and log.clip[0] in text


def test_a_clipboard_another_program_holds_still_shows_the_message(flow):
    import tkinter as tk
    ctrl, log = flow

    def busy():
        raise tk.TclError("clipboard busy")
    ctrl._tab = types.SimpleNamespace(clipboard_clear=busy, clipboard_append=lambda t: None)
    ctrl._sess_hand_off("claude_app")
    assert "could not be copied" in log.infos[0] and "cursor-c1.md" in log.infos[0]


def test_if_the_brief_cannot_be_written_no_link_is_opened_and_nothing_is_copied(flow, monkeypatch):
    ctrl, log = flow
    monkeypatch.setattr(ah, "prepare", lambda *a, **k: log.calls.append("prepare") or ah.Prepared(False, error="disk full"))
    ctrl._sess_hand_off("claude_app")
    assert log.calls == ["asked", "prepare"] and not log.clip
    assert log.errors and "disk full" in log.errors[0][1]


def test_the_not_git_ignored_warning_reaches_the_dialog(flow):
    ctrl, log = flow
    log.note = "git does not ignore the handoff folder here: add `.tokensave/` to .gitignore before committing."
    ctrl._sess_hand_off("claude_app")
    assert "git does not ignore the handoff folder" in log.infos[0]


def test_without_a_registered_claude_link_it_stops_before_anything_else(flow):
    ctrl, log = flow
    log.registered = False
    ctrl._sess_hand_off("claude_app")
    assert log.calls == [] and log.errors and "not registered" in log.errors[0][1]


def test_the_confirmation_names_the_trust_prompt_the_second_file_and_the_service(flow):
    ctrl, log = flow
    ctrl._sess_hand_off("claude_app")
    text = log.asked[0]
    assert "Open the Claude app on:" in text and "trust the folder" in text and "Anthropic" in text
    assert "NEW WORKTREE" in text and "uncommitted changes stay in the folder" in text
    assert "second file" in text and "not an import" in text and log.project in text


# -- folders that were resolved, not recorded -------------------------------------------

def _chat_row(ctrl, **fields):
    row = {"session_id": "c1", "title": "T", "agent": "cursor", "path": "x.jsonl", "cwd": ""}
    row.update(fields)
    ctrl._sess_rows = {"sess:cursor:c1": row}


@pytest.mark.parametrize("state, phrase", [("unmatched", "not one of your known projects"),
                                           ("ambiguous", "none is picked for you")])
def test_an_unresolved_chat_never_gets_a_link_and_says_why(flow, state, phrase):
    ctrl, log = flow
    _chat_row(ctrl, resolution=state)
    ctrl._sess_hand_off("claude_app")
    assert log.calls == [] and log.errors and phrase in log.errors[0][1]


def test_a_project_only_chat_opens_the_project_after_saying_its_checkout_was_not_identified(flow):
    ctrl, log = flow
    _chat_row(ctrl, resolution="project_only", project_path=log.project)
    ctrl._sess_hand_off("claude_app")
    assert "checkout was not identified" in log.asked[0] and log.project in log.asked[0]
    assert decode(log.url)[0] == log.project and log.calls[-1] == "open"


def test_the_terminal_claude_handoff_gets_the_same_resolution_rules(flow, monkeypatch):
    ctrl, log = flow
    _chat_row(ctrl, resolution="unmatched")
    ctrl._sess_hand_off("claude")
    assert log.calls == [] and log.errors and "not one of your known projects" in log.errors[0][1]


def test_a_row_with_a_recorded_folder_ignores_any_resolution_state(flow):
    ctrl, log = flow
    _chat_row(ctrl, resolution="unmatched", cwd=log.project)
    ctrl._sess_hand_off("claude_app")
    assert log.calls[-1] == "open" and decode(log.url)[0] == log.project


# -- who is offered what ----------------------------------------------------------------

def test_cursor_rows_are_offered_the_claude_app_and_claude_rows_are_not(flow):
    ctrl, _ = flow
    ctrl._sess_rows = {"c": {"agent": "cursor"}, "a": {"agent": "claude"}, "d": {}}
    assert ctrl._sess_can_hand_off("c", "claude_app") is True
    assert ctrl._sess_can_hand_off("a", "claude_app") is False and ctrl._sess_can_hand_off("d", "claude_app") is False
    assert ctrl._sess_can_hand_off("a", "cursor_app") is True and ctrl._sess_can_hand_off("c", "cursor_app") is False


def test_the_menu_enables_each_entry_by_row_and_labels_the_terminal_ones(flow):
    ctrl, _ = flow
    entries = {}
    ctrl._sess_menu = types.SimpleNamespace(
        entryconfigure=lambda index, state: entries.__setitem__(index, state),
        tk_popup=lambda x, y: None, grab_release=lambda: None)
    ctrl._sess_tree = types.SimpleNamespace(identify_row=lambda y: "sess:cursor:c1", selection_set=lambda r: None)
    ctrl._sess_right_click(types.SimpleNamespace(y=0, x_root=0, y_root=0))
    # Cursor row: not to Cursor (0, 1); to Claude terminal and app (2, 3); folder open (4) needs a folder.
    assert [entries[i] for i in range(4)] == ["disabled", "disabled", "normal", "normal"]
    import inspect
    source = inspect.getsource(TasksController._build_sessions_panel)
    assert "Continue in Claude (terminal)" in source and "Continue in Claude app" in source
    assert "Continue in Cursor (terminal)" in source and "Continue in Cursor app" in source
