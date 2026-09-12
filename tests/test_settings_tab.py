"""tests/test_settings_tab.py — SettingsTabController (Tk-marked).

The seven behaviour cases from `test_dialog_settings.py` are ported verbatim
in intent: Settings changed host, not configuration semantics, and if any of
them had to be weakened the conversion broke something.

The rest guard what the tab conversion introduced and what it exposed:

  * the staged save, including the NESTED key a shallow copy loses
  * `cfg.raw` object identity across a commit
  * section write order, now that five pages share one dict
  * dirty across page switch / Save / Revert
  * page ownership, and deep-linking by page key
  * the parent-walk ban, which is a rule rather than one banned spelling

Mocking notes
-------------
Detection helpers are patched at each SECTION module's import site (G-E), so
no PATH probing or subprocess spawns happen during a build.
`_mcp_configs` is patched so the status row never reads the real
`~/.claude.json`.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

pytestmark = pytest.mark.tk


def _build(tk_root, cfg, mocker, save_fn=None, callback=None):
    """Construct the controller with detection + MCP helpers stubbed.

    Note what is NOT stubbed onto `tk_root`: the old test put
    `cmd_upgrade_tokensave` there because `PathsSection` reached the App as
    `self._dlg.master`. The callbacks are injected now, which is the point —
    a wrong host can no longer pass by accident.
    """
    for det in ("_detect_git", "_detect_gh", "_detect_claude_cli",
                "_detect_cursor_cli"):
        mocker.patch("dialogs.settings_paths.%s" % det, return_value="")
    for det in ("_detect_codegraph", "_detect_npm"):
        mocker.patch("dialogs.settings_codegraph.%s" % det, return_value="")
    mocker.patch("dialogs.settings_mcp._mcp_configs", return_value=[])
    for mod in ("settings_paths", "settings_codegraph"):
        mocker.patch("dialogs.%s.subprocess.run" % mod,
                     side_effect=AssertionError("unexpected subprocess.run"))

    from controllers.settings_tab import SettingsTabController

    notebook = ttk.Notebook(tk_root)
    ctl = SettingsTabController(
        notebook, cfg,
        host=tk_root,
        save_fn=save_fn or (lambda: None),
        on_saved=callback or (lambda: None),
        on_upgrade_tokensave=lambda: None,
        on_integration_check=lambda: None,
        get_tokensave_versions=lambda: ("7.11.1", None),
    )
    ctl._settle()          # adopt the clean baseline without waiting 800 ms
    return ctl


# ── Ported: configuration-mutation semantics are unchanged ──────────────────

def test_constructs_with_every_page(tk_root, mock_config, mocker):
    from controllers.settings_tab import PAGES

    ctl = _build(tk_root, mock_config, mocker)
    assert len(ctl._inner.tabs()) == len(PAGES)


def test_save_round_trips_seeded_values(tk_root, mock_config, mocker):
    """Values loaded from raw survive an untouched open -> Save cycle."""
    raw = mock_config.raw
    raw.update({
        "template_dir":           "D:/templates",
        "git_exe":                "C:/git/git.exe",
        "claude_cli_exe":         "C:/npm/claude.cmd",
        "claude_cli_model":       "claude-sonnet-4-6",
        "draft_pr_backend":       "llm",
        "commit_message_backend": "claude_cli",
        "ollama_num_ctx":         8192,
        "ollama_warmup":          True,
        "commit_message_llm": {
            "enabled": True, "provider": "ollama",
            "model": "qwen2.5-coder:14b", "api_key_env": "",
            "base_url": "http://localhost:11434", "min_diff_lines": 5,
        },
    })
    saved, called = [], []
    ctl = _build(tk_root, mock_config, mocker,
                 save_fn=lambda: saved.append(True),
                 callback=lambda: called.append(True))
    ctl._save()

    assert saved and called
    assert raw["template_dir"]           == "D:/templates"
    assert raw["git_exe"]                == "C:/git/git.exe"
    assert raw["claude_cli_exe"]         == "C:/npm/claude.cmd"
    assert raw["claude_cli_model"]       == "claude-sonnet-4-6"
    assert raw["draft_pr_backend"]       == "llm"
    assert raw["commit_message_backend"] == "claude_cli"
    assert raw["ollama_num_ctx"]         == 8192
    assert raw["ollama_warmup"]          is True
    llm = raw["commit_message_llm"]
    assert llm["provider"]       == "ollama"
    assert llm["model"]          == "qwen2.5-coder:14b"
    assert llm["min_diff_lines"] == 5
    assert raw["editor_cmd"] == "code"              # blank -> "code"
    assert llm["max_diff_chars"]  == 24000
    assert llm["timeout_seconds"] == 90
    assert "ask_tab_llm" in raw
    assert raw["enable_llm_grounding"]    is True
    assert raw["enable_commit_grounding"] is True
    assert raw["enable_pr_grounding"]     is True


def test_save_aborts_when_tokensave_exe_missing(tk_root, mock_config, mocker):
    """A non-existent tokensave_exe path blocks the save entirely."""
    mock_config.raw["tokensave_exe"] = "Z:/definitely/not/here/tokensave.exe"
    saved = []
    ctl = _build(tk_root, mock_config, mocker,
                 save_fn=lambda: saved.append(True))
    warn = mocker.patch("dialogs.settings_paths.messagebox.showwarning")
    ctl._save()
    warn.assert_called_once()
    assert not saved


def test_save_writes_ask_tab_llm_independently(tk_root, mock_config, mocker):
    mock_config.raw["ask_tab_llm"] = {
        "enabled": True, "provider": "claude_cli",
        "model": "", "api_key_env": "", "base_url": "",
    }
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    ask = mock_config.raw["ask_tab_llm"]
    assert ask["enabled"] is True
    assert ask["provider"] == "claude_cli"


def test_cursor_path_and_agent_selector_round_trip(tk_root, mock_config, mocker):
    """Both halves of Cursor support survive an open/Save cycle.

    They live on different PAGES now (Paths owns the executable, AI owns the
    selector), which is exactly the arrangement where one can be wired and
    the other silently forgotten.
    """
    raw = mock_config.raw
    raw.update({
        "cursor_cli_exe": "C:/Users/x/.local/bin/cursor-agent.exe",
        "agent_cli":      "cursor",
    })
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    assert raw["cursor_cli_exe"] == "C:/Users/x/.local/bin/cursor-agent.exe"
    assert raw["agent_cli"] == "cursor"


def test_agent_selector_defaults_to_claude_on_a_pre_cursor_config(
        tk_root, mock_config, mocker):
    """Opening and saving Settings must not migrate an existing install."""
    assert "agent_cli" not in mock_config.raw
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    assert mock_config.raw["agent_cli"] == "claude"


def test_saved_selector_resolves_to_that_agent(tk_root, mock_config, mocker):
    """End to end: what Settings writes is what resolve_agent_cli reads."""
    mock_config.raw.update({
        "agent_cli": "cursor",
        "cursor_cli_exe": "C:/x/cursor-agent.exe",
    })
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    from state import ManagerConfig
    res = ManagerConfig(raw=dict(mock_config.raw)).resolve_agent_cli()
    assert res.spec.id == "cursor"
    assert res.exe == "C:/x/cursor-agent.exe"


# ── The staged transaction ──────────────────────────────────────────────────

def test_abort_leaves_the_live_config_untouched(tk_root, mock_config, mocker):
    """The defect the tab would have made permanent.

    The dialog mutated `cfg.raw` section by section and only wrote to disk
    once every section returned True, so aborting in section three left
    sections one and two applied to the object the whole application reads.
    """
    raw = mock_config.raw
    raw.update({
        "tokensave_exe": "Z:/definitely/not/here/tokensave.exe",
        "template_dir":  "D:/original",
        "editor_cmd":    "notepad",
    })
    before = __import__("copy").deepcopy(raw)
    ctl = _build(tk_root, mock_config, mocker)
    ctl._paths._tmpl_var.set("D:/changed-but-never-saved")
    mocker.patch("dialogs.settings_paths.messagebox.showwarning")
    ctl._save()
    assert raw == before


def test_abort_does_not_leak_through_a_nested_dict(tk_root, mock_config, mocker):
    """The case `dict(cfg.raw)` silently loses.

    `AISection.save_into` does `existing = raw.get("commit_message_llm")`
    then `existing.update(...)` -- an in-place write. With a shallow staging
    copy the staged dict holds the SAME object, so the write lands on the
    live config even though the save aborted a section later.
    """
    raw = mock_config.raw
    raw.update({
        "tokensave_exe": "Z:/definitely/not/here/tokensave.exe",
        "commit_message_llm": {"enabled": False, "provider": "anthropic",
                               "model": "claude-haiku-4-5",
                               "api_key_env": "ANTHROPIC_API_KEY",
                               "base_url": "", "min_diff_lines": 10},
    })
    ctl = _build(tk_root, mock_config, mocker)
    ctl._ai._var_llm_model.set("some-other-model")
    mocker.patch("dialogs.settings_paths.messagebox.showwarning")
    ctl._save()
    assert raw["commit_message_llm"]["model"] == "claude-haiku-4-5"


def test_commit_preserves_the_raw_dict_identity(tk_root, mock_config, mocker):
    """Modules hold a reference to `cfg.raw`; rebinding it strands them."""
    raw = mock_config.raw
    ctl = _build(tk_root, mock_config, mocker)
    ctl._save()
    assert mock_config.raw is raw


def test_sections_write_in_a_fixed_order(tk_root, mock_config, mocker):
    """Five pages now share one staging dict, so order is load-bearing."""
    ctl = _build(tk_root, mock_config, mocker)
    assert [type(s).__name__ for s in ctl._sections] == [
        "PathsSection", "ProjectsSection", "GitPolicySection", "AISection",
        "CodegraphSection", "PyScopeSection", "McpStatusSection"]


def test_a_failing_section_blocks_every_later_one(tk_root, mock_config, mocker):
    """Abort means abort: nothing after the refusing section runs."""
    mock_config.raw["tokensave_exe"] = "Z:/nope/tokensave.exe"
    ctl = _build(tk_root, mock_config, mocker)
    mocker.patch("dialogs.settings_paths.messagebox.showwarning")
    reached = []
    for section in ctl._sections[1:]:
        section.save_into = lambda _raw, s=section: (
            reached.append(type(s).__name__) or True)
    ctl._save()
    assert reached == []


# ── Dirty lifecycle ─────────────────────────────────────────────────────────

def test_opens_clean(tk_root, mock_config, mocker):
    """Auto-detection fills two path entries on a build timer, so a
    trace-only implementation reports unsaved changes on an untouched page."""
    ctl = _build(tk_root, mock_config, mocker)
    assert ctl.is_dirty() is False
    assert not ctl._bar.winfo_ismapped()


def test_an_edit_makes_it_dirty_and_undoing_makes_it_clean(
        tk_root, mock_config, mocker):
    ctl = _build(tk_root, mock_config, mocker)
    original = ctl._paths._editor_var.get()
    ctl._paths._editor_var.set("subl")
    assert ctl.is_dirty() is True
    ctl._paths._editor_var.set(original)
    assert ctl.is_dirty() is False


def test_a_new_search_root_makes_it_dirty(tk_root, mock_config, mocker):
    """The Treeview is not a Tk variable, which is why `snapshot()` exists."""
    ctl = _build(tk_root, mock_config, mocker)
    ctl._projects._tv.insert("", tk.END, values=("Added", "D:/added"))
    assert ctl.is_dirty() is True


def test_edits_survive_switching_pages(tk_root, mock_config, mocker):
    ctl = _build(tk_root, mock_config, mocker)
    ctl._paths._editor_var.set("subl")
    ctl.select_page("ai")
    ctl.select_page("paths")
    assert ctl._paths._editor_var.get() == "subl"
    assert ctl.is_dirty() is True


def test_save_clears_dirty(tk_root, mock_config, mocker):
    ctl = _build(tk_root, mock_config, mocker)
    ctl._paths._editor_var.set("subl")
    ctl._save()
    assert ctl.is_dirty() is False


def test_revert_restores_and_ends_clean(tk_root, mock_config, mocker):
    """Revert reassigns every variable, which fires every trace.

    Without the hydration guard it would rebuild the pages and leave its own
    "unsaved changes" bar on screen -- so this asserts the dirty flag, not
    just that the values came back.
    """
    ctl = _build(tk_root, mock_config, mocker)
    ctl._paths._editor_var.set("subl")
    assert ctl.is_dirty() is True
    ctl._revert()
    ctl._settle()
    assert ctl._paths._editor_var.get() != "subl"
    assert ctl.is_dirty() is False


def test_revert_keeps_the_current_page(tk_root, mock_config, mocker):
    ctl = _build(tk_root, mock_config, mocker)
    ctl.select_page("git")
    ctl._paths._editor_var.set("subl")
    ctl._revert()
    assert str(ctl._inner.select()) == str(ctl._pages["git"])


# ── Pages ───────────────────────────────────────────────────────────────────

def test_deep_link_selects_the_named_page(tk_root, mock_config, mocker):
    """Asserted on the SELECTED page, not on the call being made."""
    ctl = _build(tk_root, mock_config, mocker)
    ctl.select_page("integrations")
    assert str(ctl._inner.select()) == str(ctl._pages["integrations"])


def test_every_page_key_resolves(tk_root, mock_config, mocker):
    from controllers.settings_tab import PAGES

    ctl = _build(tk_root, mock_config, mocker)
    for key, _title in PAGES:
        ctl.select_page(key)
        assert str(ctl._inner.select()) == str(ctl._pages[key])


def test_an_unknown_page_key_changes_nothing(tk_root, mock_config, mocker):
    ctl = _build(tk_root, mock_config, mocker)
    ctl.select_page("ai")
    ctl.select_page("no-such-page")
    assert str(ctl._inner.select()) == str(ctl._pages["ai"])


def test_every_page_renders_something(tk_root, mock_config, mocker):
    """A page whose sections silently failed to build looks like an empty tab."""
    ctl = _build(tk_root, mock_config, mocker)
    for key in ctl._pages:
        assert _descendants(ctl._pages[key]), "the %s page is empty" % key


def test_no_section_leaked_onto_the_tab_itself(tk_root, mock_config, mocker):
    """The re-parenting mistake: renders correctly, wrong lifetime.

    A section packed onto the tab frame instead of its page body would look
    almost right and then outlive the page. The tab's own direct children are
    exactly three: the startup-note label, the dirty bar, and the notebook.
    """
    ctl = _build(tk_root, mock_config, mocker)
    assert len(ctl._tab.winfo_children()) == 3


def test_sections_that_hold_a_widget_hold_one_inside_their_page(
        tk_root, mock_config, mocker):
    """Not every section keeps a widget reference -- `GitPolicySection` owns
    only variables -- so this asserts about the ones that do rather than
    making them store something for the test's benefit."""
    ctl = _build(tk_root, mock_config, mocker)
    expected = {"_paths": "paths", "_projects": "projects", "_ai": "ai",
                "_codegraph": "integrations", "_pyscope": "integrations"}
    checked = 0
    for attribute, page_key in expected.items():
        widget = _a_widget_of(getattr(ctl, attribute))
        if widget is None:
            continue
        checked += 1
        assert _is_inside(widget, ctl._pages[page_key]), (
            "%s renders outside the %s page" % (attribute, page_key))
    assert checked >= 3, "the probe found too few widgets to mean anything"


def _a_widget_of(section):
    """A widget the section BUILT -- never the host it was handed.

    `self._host` is a widget too, and picking it up would make every section
    look like it renders outside its page.
    """
    for name, value in vars(section).items():
        if name != "_host" and isinstance(value, tk.Misc):
            return value
    return None


def _descendants(widget):
    out = list(widget.winfo_children())
    for child in list(out):
        out.extend(_descendants(child))
    return out


def _is_inside(widget, ancestor) -> bool:
    return str(widget).startswith(str(ancestor) + ".")


# ── The rule, not one banned spelling ───────────────────────────────────────

def test_no_settings_module_walks_up_the_widget_tree():
    """`PathsSection` used to reach the App as `self._dlg.master`.

    In a Notebook page `.master` is the Notebook, so
    `getattr(host, "_tokensave_current_version", None)` returns None silently
    and permanently while the buttons raise. Banning only `.master` would
    invite `winfo_toplevel()` back in its place, so the ban is on discovery
    itself: these modules RECEIVE what they need.
    """
    import ast
    import glob
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    patterns = [os.path.join(here, "..", "src", "dialogs", "settings_*.py"),
                os.path.join(here, "..", "src", "controllers",
                             "settings_tab.py")]
    banned_attrs = {"master"}
    banned_calls = {"winfo_toplevel", "nametowidget"}
    offenders = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            # Parsed rather than grepped: the modules DOCUMENT this rule, and
            # a textual scan reports their own explanation of it as a
            # violation.
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if node.attr in banned_attrs:
                        offenders.append("%s:%d .%s"
                                         % (os.path.basename(path),
                                            node.lineno, node.attr))
                    elif node.attr in banned_calls:
                        offenders.append("%s:%d .%s()"
                                         % (os.path.basename(path),
                                            node.lineno, node.attr))
    assert offenders == [], (
        "settings modules must receive their host, never discover it: %s"
        % offenders)
