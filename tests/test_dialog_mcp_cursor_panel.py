"""tests/test_dialog_mcp_cursor_panel.py — the Cursor panel actually renders.

`helpers/mcp_cursor.py` carries the logic and `tests/test_cursor_mcp.py` covers
it. What is left is the renderer, and a panel that raises inside `_render`
takes the whole MCP dialog down with it — so the states worth checking here are
the ones a developer is least likely to have on screen while working: a
malformed file, a binding pointing at another project, a machine with no Cursor.

Rendered against a stub host rather than the real `MCPConfigDialog`, following
`test_dialog_mcp_desktop_panel.py`: the panel needs only `_body`, `_cfg`,
`_render()` and `_open_file()`, and building the real dialog would drag in
project discovery, `grab_set` and a modal constructor to inspect some labels.
"""
from __future__ import annotations

import json
import os
import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from dialogs.mcp_cursor_panel import CursorBindingMixin
from helpers.mcp_cursor import SERVER_KEY, cursor_project_mcp_path

ENTRY = {"command": "tokensave", "args": ["serve", "-p", "."]}


class _Host(CursorBindingMixin):
    """The collaborators the panel reads, and nothing else."""

    def __init__(self, tk_root, cfg):
        self._body = tk.Frame(tk_root)
        self._cfg = cfg
        self.rendered = 0
        self.opened = []

    def _render(self):
        self.rendered += 1

    def _open_file(self, path):
        self.opened.append(path)

    def texts(self):
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    out.append(child.cget("text"))
                walk(child)

        walk(self._body)
        return out

    def buttons(self):
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                text = ""
                try:
                    text = child.cget("text")
                except tk.TclError:
                    pass
                if child.winfo_class() in ("TButton", "Button") and text:
                    out.append(text)
                walk(child)

        walk(self._body)
        return out

    def blob(self):
        return " ".join(self.texts())


def _project(tmp_path, name="Proj", indexed=True):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if indexed:
        (root / ".tokensave").mkdir(exist_ok=True)
    return str(root)


def _write_cursor(root, payload):
    path = cursor_project_mcp_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)


@pytest.fixture
def wired(mocker):
    """Discovery + PATH readiness stubbed; each test supplies its projects."""
    def _apply(projects, path_ready=True):
        mocker.patch("helpers.project_discovery.find_projects",
                     return_value=[{"path": p, "name": os.path.basename(p)}
                                   for p in projects])
        state = mocker.MagicMock()
        state.is_ready = path_ready
        mocker.patch("helpers.path_setup.read_state", return_value=state)
    return _apply


# ── Cursor absent ────────────────────────────────────────────────────────────

def test_no_cursor_and_nothing_bound_renders_one_quiet_line(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """A permanently empty section would be noise; nothing at all would make
    the feature undiscoverable. One line is the honest middle."""
    wired([_project(tmp_path)])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert len(host.texts()) == 1
    assert "Cursor not detected" in host.blob()
    assert host.buttons() == []


def test_the_full_section_appears_once_cursor_exists(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    wired([_project(tmp_path)])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Per-project bindings  (Cursor)" in host.blob()


def test_an_existing_binding_shows_the_section_even_without_cursor(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """Someone bound a project then uninstalled Cursor; hiding the row would
    leave a config file nothing in the UI mentions."""
    root = _project(tmp_path)
    _write_cursor(root, {"mcpServers": {SERVER_KEY: ENTRY}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Per-project bindings  (Cursor)" in host.blob()


# ── Row states ───────────────────────────────────────────────────────────────

def test_an_unbound_project_offers_bind(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    wired([_project(tmp_path)])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Bind" in host.buttons()


def test_a_malformed_file_is_reported_and_gets_NO_bind_button(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """The load-bearing refusal: binding would discard whatever the user was
    mid-way through writing. Broken is not empty."""
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    _write_cursor(root, "{half written")
    wired([root])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    blob = host.blob()
    assert "not valid JSON" in blob
    assert "not overwritten" in blob
    assert "Bind" not in host.buttons()
    assert "Rebind" not in host.buttons()


def test_a_malformed_file_still_offers_open_so_it_can_be_fixed(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    _write_cursor(root, "{half written")
    wired([root])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Open file" in host.buttons()


def test_a_binding_pointing_elsewhere_names_the_other_project(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    other = str(tmp_path / "SomewhereElse")
    _write_cursor(root, {"mcpServers": {SERVER_KEY: {
        "command": "tokensave", "args": ["serve", "-p", other]}}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "SomewhereElse" in host.blob()
    assert "Bound to a different project" in host.blob()


def test_a_bound_project_collapses_behind_a_count(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    _write_cursor(root, {"mcpServers": {SERVER_KEY: ENTRY}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "1 project bound in Cursor" in host.blob()
    assert "show" in host.buttons()


def test_expanding_bound_projects_shows_unbind(
        tk_root, mock_config, wired, fake_home, tmp_path):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    _write_cursor(root, {"mcpServers": {SERVER_KEY: ENTRY}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._show_cursor_bound = True
    host._render_cursor_section()
    assert "Unbind" in host.buttons()


# ── Prerequisite ─────────────────────────────────────────────────────────────

def test_bind_is_withheld_until_tokensave_resolves_on_path(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """The entry says `"command": "tokensave"` so the file stays portable;
    binding before that resolves writes a config Cursor cannot start."""
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    wired([_project(tmp_path)], path_ready=False)
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Bind" not in host.buttons()
    assert "does not resolve as a command" in host.blob()


# ── Actions ──────────────────────────────────────────────────────────────────

def test_bind_writes_the_portable_entry_and_re_renders(
        tk_root, mock_config, wired, fake_home, tmp_path, mocker):
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    wired([root])
    mocker.patch("dialogs.mcp_cursor_panel.messagebox.showinfo")
    host = _Host(tk_root, mock_config)
    host._cursor_bind(root)

    with open(cursor_project_mcp_path(root), encoding="utf-8") as fh:
        written = json.load(fh)
    entry = written["mcpServers"][SERVER_KEY]
    assert entry["args"][-1] == "."          # portable, not this machine's path
    assert str(tmp_path) not in json.dumps(entry)
    assert host.rendered == 1


def test_unbind_removes_only_our_key_and_re_renders(
        tk_root, mock_config, wired, fake_home, tmp_path):
    root = _project(tmp_path)
    _write_cursor(root, {"mcpServers": {SERVER_KEY: ENTRY,
                                        "other": {"command": "x"}}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._cursor_unbind(root)
    with open(cursor_project_mcp_path(root), encoding="utf-8") as fh:
        written = json.load(fh)
    assert SERVER_KEY not in written["mcpServers"]
    assert "other" in written["mcpServers"]
    assert host.rendered == 1


def test_bind_failure_surfaces_an_error_and_does_not_re_render(
        tk_root, mock_config, wired, fake_home, tmp_path, mocker):
    root = _project(tmp_path)
    _write_cursor(root, "{broken")
    wired([root])
    err = mocker.patch("dialogs.mcp_cursor_panel.messagebox.showerror")
    host = _Host(tk_root, mock_config)
    host._cursor_bind(root)
    err.assert_called_once()
    assert host.rendered == 0


# ── Negative space ───────────────────────────────────────────────────────────

def test_the_panel_never_renders_a_trust_verdict(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """Cursor has no trust gate. Borrowing Claude's vocabulary would invent a
    state the product cannot produce.

    Asserted against Claude's verdict PHRASES rather than the bare word
    "trust": the panel's intro deliberately says Cursor has *no* trust prompt,
    which is exactly the kind of thing a reader coming from the Claude section
    needs told. Banning the word outright would delete a useful sentence to
    satisfy the test.
    """
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    root = _project(tmp_path)
    _write_cursor(root, {"mcpServers": {SERVER_KEY: ENTRY}})
    wired([root])
    host = _Host(tk_root, mock_config)
    host._show_cursor_bound = True
    host._render_cursor_section()
    blob = host.blob().lower()
    for verdict in ("untrusted", "not trusted", "trust dialog",
                    "hastrustdialogaccepted", "trusted folder",
                    "blocked by trust"):
        assert verdict not in blob, verdict
    # Where it does appear, it is the disclaimer and nothing else.
    assert blob.count("trust") == 1
    assert "no trust prompt" in blob


def test_unindexed_projects_are_not_offered(
        tk_root, mock_config, wired, fake_home, tmp_path):
    """No tokensave index means there is nothing to bind a server to."""
    os.makedirs(os.path.join(str(fake_home), ".cursor"), exist_ok=True)
    wired([_project(tmp_path, name="Bare", indexed=False)])
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()
    assert "Bind" not in host.buttons()


def test_a_discovery_failure_does_not_take_the_dialog_down(
        tk_root, mock_config, fake_home, mocker):
    mocker.patch("helpers.project_discovery.find_projects",
                 side_effect=RuntimeError("boom"))
    host = _Host(tk_root, mock_config)
    host._render_cursor_section()          # must not raise
    assert "Cursor not detected" in host.blob()
