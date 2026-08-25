"""Tests for controllers/projects_tab.py — ProjectsTabController."""

import os
import pytest
import threading
import time
import tkinter as tk
from tkinter import ttk
from unittest import mock
from types import SimpleNamespace
from typing import Any

tk = pytest.importorskip("tkinter")

from constants import C
from controllers.projects_tab import ProjectsTabController
from state import ManagerConfig


pytestmark = pytest.mark.tk


@pytest.fixture
def mock_config(tmp_path):
    """Minimal ManagerConfig stub."""
    cfg = mock.MagicMock(spec=ManagerConfig)
    cfg.raw = {
        "project_categories": {},
        "tokensave_exe": "/path/to/tokensave",
        "template_dir": str(tmp_path),
        "git_exe": "git",
        "codegraph_exe": "/path/to/codegraph",
        "basic_instructions_template": None,
        "baseline_include_line": None,
        "search_roots": [],
    }
    cfg.tokensave_exe = "/path/to/tokensave"
    cfg.template_dir = str(tmp_path)
    cfg.git_exe = "git"
    cfg.codegraph_exe = "/path/to/codegraph"
    cfg.basic_instructions_template = None
    cfg.baseline_include_line = None
    cfg.search_roots = []
    cfg._saved = False

    def save():
        cfg._saved = True

    cfg.save = save
    return cfg


@pytest.fixture
def callback_mocks():
    """Create mock callbacks."""
    return {
        "get_projects": mock.MagicMock(return_value=[]),
        "on_run": mock.MagicMock(),
        "on_run_capture": mock.MagicMock(return_value=("output", 0, 0.0)),
        "on_shell": mock.MagicMock(return_value=("output", 0)),
        "on_log": mock.MagicMock(),
        "on_commit": mock.MagicMock(),
        "on_refresh": mock.MagicMock(),
        "on_project_select": mock.MagicMock(),
        "on_set_running": mock.MagicMock(),
        "on_settings": mock.MagicMock(),
        "on_seed_ask": mock.MagicMock(),
    }


@pytest.fixture
def controller(tk_root, mock_config, callback_mocks):
    """Create a ProjectsTabController with a real notebook and callbacks."""
    notebook = ttk.Notebook(tk_root)
    notebook.pack()

    ctrl = ProjectsTabController(
        notebook=notebook,
        cfg=mock_config,
        get_projects=callback_mocks["get_projects"],
        on_run=callback_mocks["on_run"],
        on_run_capture=callback_mocks["on_run_capture"],
        on_shell=callback_mocks["on_shell"],
        on_log=callback_mocks["on_log"],
        on_commit=callback_mocks["on_commit"],
        on_refresh=callback_mocks["on_refresh"],
        on_project_select=callback_mocks["on_project_select"],
        on_set_running=callback_mocks["on_set_running"],
        on_settings=callback_mocks["on_settings"],
        on_seed_ask=callback_mocks["on_seed_ask"],
    )
    return ctrl


def test_initialization(controller):
    """Test that ProjectsTabController initializes with correct attributes."""
    assert controller._cfg is not None
    assert controller._tab is not None
    assert isinstance(controller._tab, tk.Frame)
    assert controller._tab.cget("bg") == C["base"]
    assert controller._tree is not None
    assert isinstance(controller._tree, ttk.Treeview)
    assert controller._ctx_menu is not None
    assert isinstance(controller._ctx_menu, tk.Menu)


def test_tab_added_to_notebook(tk_root, mock_config, callback_mocks):
    """Test that the Projects tab is added to the notebook with correct text."""
    notebook = ttk.Notebook(tk_root)
    ctrl = ProjectsTabController(
        notebook=notebook,
        cfg=mock_config,
        get_projects=callback_mocks["get_projects"],
        on_run=callback_mocks["on_run"],
        on_run_capture=callback_mocks["on_run_capture"],
        on_shell=callback_mocks["on_shell"],
        on_log=callback_mocks["on_log"],
        on_commit=callback_mocks["on_commit"],
        on_refresh=callback_mocks["on_refresh"],
        on_project_select=callback_mocks["on_project_select"],
        on_set_running=callback_mocks["on_set_running"],
        on_settings=callback_mocks["on_settings"],
    )

    tabs = notebook.tabs()
    assert len(tabs) >= 1
    assert notebook.tab(tabs[-1], "text") == "  Projects  "


def test_callbacks_stored(controller, callback_mocks):
    """Test that all callbacks are stored as instance attributes."""
    assert controller._on_run is callback_mocks["on_run"]
    assert controller._on_run_capture is callback_mocks["on_run_capture"]
    assert controller._on_shell is callback_mocks["on_shell"]
    assert controller._on_log is callback_mocks["on_log"]
    assert controller._on_commit is callback_mocks["on_commit"]
    assert controller._on_refresh is callback_mocks["on_refresh"]
    assert controller._on_project_select is callback_mocks["on_project_select"]
    assert controller._on_set_running is callback_mocks["on_set_running"]
    assert controller._on_settings is callback_mocks["on_settings"]
    assert controller._on_seed_ask is callback_mocks["on_seed_ask"]


def test_get_projects_stored(controller, callback_mocks):
    """Test that get_projects callback is stored."""
    assert controller._get_projects is callback_mocks["get_projects"]


def test_current_proc_initialization(controller):
    """Test that current_proc is initialized to None."""
    assert controller.current_proc is None


def test_git_status_refresh_flags(controller):
    """Test that git status refresh flags are initialized to False."""
    assert controller._git_status_refresh_cancel is False
    assert controller._git_status_refresh_running is False


def test_git_status_tags_constant():
    """Test that _GIT_STATUS_TAGS class constant is defined correctly."""
    expected_tags = {
        "git_clean", "git_dirty", "git_ahead", "git_behind",
        "git_mixed", "git_pending", "git_none",
    }
    assert ProjectsTabController._GIT_STATUS_TAGS == expected_tags


def test_codegraph_controller_initialized(controller):
    """Test that CodeGraphController is initialized."""
    assert controller._codegraph is not None


def test_doctor_controller_initialized(controller):
    """Test that DoctorController is initialized."""
    assert controller._doctor is not None


def test_scaffold_controller_initialized(controller):
    """Test that ScaffoldRetrofitController is initialized."""
    assert controller._scaffold is not None


def test_sync_controller_initialized(controller):
    """Test that SyncStatusController is initialized."""
    assert controller._sync is not None


def test_gitops_controller_initialized(controller):
    """Test that GitOpsController is initialized."""
    assert controller._gitops is not None


def test_optional_on_seed_ask_none(tk_root, mock_config, callback_mocks):
    """Test that on_seed_ask callback is optional and can be None."""
    notebook = ttk.Notebook(tk_root)
    ctrl = ProjectsTabController(
        notebook=notebook,
        cfg=mock_config,
        get_projects=callback_mocks["get_projects"],
        on_run=callback_mocks["on_run"],
        on_run_capture=callback_mocks["on_run_capture"],
        on_shell=callback_mocks["on_shell"],
        on_log=callback_mocks["on_log"],
        on_commit=callback_mocks["on_commit"],
        on_refresh=callback_mocks["on_refresh"],
        on_project_select=callback_mocks["on_project_select"],
        on_set_running=callback_mocks["on_set_running"],
        on_settings=callback_mocks["on_settings"],
        on_seed_ask=None,
    )
    assert ctrl._on_seed_ask is None


def test_notebook_reference(controller, tk_root):
    """Test that notebook reference is stored correctly."""
    # The controller's _tab should be a child of the notebook
    parent = controller._tab.master
    # parent should be the notebook (or its internal frame)
    assert parent is not None


def test_tree_is_treeview(controller):
    """Test that the tree widget is a ttk.Treeview."""
    assert isinstance(controller._tree, ttk.Treeview)


def test_context_menu_is_menu(controller):
    """Test that the context menu is a tk.Menu."""
    assert isinstance(controller._ctx_menu, tk.Menu)


def test_tab_frame_background_color(controller):
    """Test that the tab frame has the correct background color."""
    assert controller._tab.cget("bg") == C["base"]


def test_config_reference_stored(controller, mock_config):
    """Test that config reference is stored."""
    assert controller._cfg is mock_config


def test_multiple_instances_independent(tk_root, mock_config, callback_mocks):
    """Test that multiple controller instances don't share state."""
    notebook1 = ttk.Notebook(tk_root)
    notebook2 = ttk.Notebook(tk_root)

    ctrl1 = ProjectsTabController(
        notebook=notebook1,
        cfg=mock_config,
        get_projects=callback_mocks["get_projects"],
        on_run=callback_mocks["on_run"],
        on_run_capture=callback_mocks["on_run_capture"],
        on_shell=callback_mocks["on_shell"],
        on_log=callback_mocks["on_log"],
        on_commit=callback_mocks["on_commit"],
        on_refresh=callback_mocks["on_refresh"],
        on_project_select=callback_mocks["on_project_select"],
        on_set_running=callback_mocks["on_set_running"],
        on_settings=callback_mocks["on_settings"],
    )

    ctrl2 = ProjectsTabController(
        notebook=notebook2,
        cfg=mock_config,
        get_projects=callback_mocks["get_projects"],
        on_run=callback_mocks["on_run"],
        on_run_capture=callback_mocks["on_run_capture"],
        on_shell=callback_mocks["on_shell"],
        on_log=callback_mocks["on_log"],
        on_commit=callback_mocks["on_commit"],
        on_refresh=callback_mocks["on_refresh"],
        on_project_select=callback_mocks["on_project_select"],
        on_set_running=callback_mocks["on_set_running"],
        on_settings=callback_mocks["on_settings"],
    )

    # Verify they have different notebook and frame references
    assert ctrl1._notebook is not ctrl2._notebook
    assert ctrl1._tab is not ctrl2._tab
    assert ctrl1._tree is not ctrl2._tree
    assert ctrl1._ctx_menu is not ctrl2._ctx_menu


def test_proc_attribute_can_be_set(controller):
    """Test that current_proc attribute can be set and retrieved."""
    proc_mock = mock.MagicMock()
    controller.current_proc = proc_mock
    assert controller.current_proc is proc_mock


def test_git_refresh_cancel_flag_can_be_set(controller):
    """Test that _git_status_refresh_cancel flag can be set."""
    controller._git_status_refresh_cancel = True
    assert controller._git_status_refresh_cancel is True
    controller._git_status_refresh_cancel = False
    assert controller._git_status_refresh_cancel is False


def test_git_refresh_running_flag_can_be_set(controller):
    """Test that _git_status_refresh_running flag can be set."""
    controller._git_status_refresh_running = True
    assert controller._git_status_refresh_running is True
    controller._git_status_refresh_running = False
    assert controller._git_status_refresh_running is False


def test_constructor_with_all_callbacks(tk_root, mock_config):
    """Test constructor with all callbacks provided."""
    notebook = ttk.Notebook(tk_root)
    mocks = {
        f"mock_{i}": mock.MagicMock() for i in range(11)
    }

    ctrl = ProjectsTabController(
        notebook=notebook,
        cfg=mock_config,
        get_projects=mocks["mock_0"],
        on_run=mocks["mock_1"],
        on_run_capture=mocks["mock_2"],
        on_shell=mocks["mock_3"],
        on_log=mocks["mock_4"],
        on_commit=mocks["mock_5"],
        on_refresh=mocks["mock_6"],
        on_project_select=mocks["mock_7"],
        on_set_running=mocks["mock_8"],
        on_settings=mocks["mock_9"],
        on_seed_ask=mocks["mock_10"],
    )

    assert ctrl._get_projects is mocks["mock_0"]
    assert ctrl._on_run is mocks["mock_1"]
    assert ctrl._on_run_capture is mocks["mock_2"]
    assert ctrl._on_shell is mocks["mock_3"]
    assert ctrl._on_log is mocks["mock_4"]
    assert ctrl._on_commit is mocks["mock_5"]
    assert ctrl._on_refresh is mocks["mock_6"]
    assert ctrl._on_project_select is mocks["mock_7"]
    assert ctrl._on_set_running is mocks["mock_8"]
    assert ctrl._on_settings is mocks["mock_9"]
    assert ctrl._on_seed_ask is mocks["mock_10"]


def test_config_raw_access(controller, mock_config):
    """Test that raw config dict is accessible."""
    assert hasattr(controller._cfg, "raw")
    assert isinstance(controller._cfg.raw, dict)


def test_config_save_method(controller, mock_config):
    """Test that config has save method."""
    assert hasattr(controller._cfg, "save")
    controller._cfg.save()
    assert controller._cfg._saved is True

# ── Housekeeping wiring ───────────────────────────────────────────────────────

def _menu_labels(menu):
    """Every command label in a tk.Menu, DESCENDING into cascades.

    The context menu was regrouped into submenus in Roadmap-9, so a
    top-level-only walk would report perfectly-wired commands as missing.
    Cascade labels themselves are omitted — callers ask about commands.
    """
    return [lbl for _m, _i, lbl in _menu_entries(menu)]


def _menu_entries(menu):
    """(owning_menu, index, label) for every command, cascades included."""
    out = []
    end = menu.index("end")
    if end is None:
        return out
    for i in range(end + 1):
        kind = menu.type(i)
        if kind == "separator":
            continue
        if kind == "cascade":
            child_name = menu.entrycget(i, "menu")
            child = menu.nametowidget(child_name) if child_name else None
            if child is not None:
                out.extend(_menu_entries(child))
            continue
        out.append((menu, i, menu.entrycget(i, "label")))
    return out


def test_context_menu_has_housekeeping_entry(controller):
    """The Housekeeping command is reachable from the Projects context menu.

    Guards the wiring rather than the dialog: the controller can be perfectly
    correct and still be unreachable if nobody adds the menu entry, which is
    invisible until someone right-clicks.
    """
    labels = _menu_labels(controller._ctx_menu)
    assert any("Housekeeping" in lbl for lbl in labels), labels
    # It should sit with Doctor — same family of maintenance actions.
    doctor_at = next(i for i, l in enumerate(labels) if "Doctor" in l)
    house_at = next(i for i, l in enumerate(labels) if "Housekeeping" in l)
    assert house_at == doctor_at + 1


def test_housekeeping_menu_entry_invokes_the_controller(controller, monkeypatch,
                                                        tmp_path):
    """Clicking the entry actually reaches HousekeepingController.

    Invokes the real menu entry so a mis-wired `command=` is caught, not just
    a missing label.
    """
    opened = []
    monkeypatch.setattr(controller._housekeeping, "cmd_housekeeping",
                        lambda path: opened.append(path))
    monkeypatch.setattr(controller._cmd_bar, "get_path", lambda: str(tmp_path))
    monkeypatch.setattr(controller._cmd_bar, "require_tokensave",
                        lambda path: True)

    owner, idx, _lbl = next(
        e for e in _menu_entries(controller._ctx_menu) if "Housekeeping" in e[2])
    owner.invoke(idx)

    assert opened == [str(tmp_path)]


# ── Multi-select batch operations (Roadmap-9) ────────────────────────────

class TestMultiSelectBatch:
    """Selecting several projects and acting on all of them at once.

    The friction being removed: "Set as Active" then operate, once per
    project, for every maintenance pass across a multi-project workspace.
    """

    def _ctrl(self):
        """A bare controller with just the tree attribute the accessors use."""
        ctrl = object.__new__(ProjectsTabController)
        return ctrl

    def test_get_selected_paths_returns_every_project_row(self):
        ctrl = self._ctrl()
        ctrl._tree = SimpleNamespace(
            selection=lambda: ("proj:/a", "proj:/b", "proj:/c"))
        assert ctrl.get_selected_paths() == ["/a", "/b", "/c"]

    def test_get_selected_paths_skips_category_headers(self):
        """A shift-select spanning a group header must not yield the header."""
        ctrl = self._ctrl()
        ctrl._tree = SimpleNamespace(
            selection=lambda: ("cat:My Projects", "proj:/a", "proj:/b"))
        assert ctrl.get_selected_paths() == ["/a", "/b"]

    def test_get_selected_paths_is_empty_without_a_tree(self):
        ctrl = self._ctrl()
        ctrl._tree = None
        assert ctrl.get_selected_paths() == []

    def test_singular_accessor_still_returns_one_path(self):
        """Every existing single-project command depends on this."""
        ctrl = self._ctrl()
        ctrl._tree = SimpleNamespace(selection=lambda: ("proj:/a", "proj:/b"))
        assert ctrl.get_selected_path() == "/a"

    def test_right_click_inside_a_selection_preserves_it(self):
        """Collapsing to the clicked row would make the batch menu unusable."""
        ctrl = self._ctrl()
        set_calls = []
        ctrl._tree = SimpleNamespace(
            identify_row=lambda y: "proj:/b",
            selection=lambda: ("proj:/a", "proj:/b", "proj:/c"),
            selection_set=lambda *a: set_calls.append(a))
        shown = []
        ctrl._show_batch_menu = lambda evt, paths: shown.append(paths)
        ctrl._ctx_menu = SimpleNamespace(
            tk_popup=lambda *a: shown.append("single-menu"))

        ctrl._on_right_click(SimpleNamespace(y=10, x_root=0, y_root=0))

        assert set_calls == [], "selection was collapsed on right-click"
        assert shown == [["/a", "/b", "/c"]]

    def test_right_click_outside_the_selection_selects_that_row(self):
        ctrl = self._ctrl()
        set_calls = []
        ctrl._tree = SimpleNamespace(
            identify_row=lambda y: "proj:/z",
            selection=lambda: ("proj:/a", "proj:/b"),
            selection_set=lambda *a: set_calls.append(a))
        shown = []
        ctrl._show_batch_menu = lambda evt, paths: shown.append(paths)
        ctrl._ctx_menu = SimpleNamespace(
            tk_popup=lambda *a: shown.append("single-menu"))

        ctrl._on_right_click(SimpleNamespace(y=10, x_root=0, y_root=0))
        assert set_calls == [("proj:/z",)]

    def test_single_selection_uses_the_ordinary_menu(self):
        """One project must keep the full command menu, not the batch one."""
        ctrl = self._ctrl()
        ctrl._tree = SimpleNamespace(
            identify_row=lambda y: "proj:/a",
            selection=lambda: ("proj:/a",),
            selection_set=lambda *a: None)
        shown = []
        ctrl._show_batch_menu = lambda evt, paths: shown.append("batch")
        ctrl._ctx_menu = SimpleNamespace(
            tk_popup=lambda *a: shown.append("single-menu"))

        ctrl._on_right_click(SimpleNamespace(y=10, x_root=0, y_root=0))
        assert shown == ["single-menu"]

    def test_cross_project_search_needs_two_projects(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            "dialogs.cross_project_search.CrossProjectSearchDialog",
            lambda parent, paths, cfg: opened.append(paths))
        ctrl = self._ctrl()
        ctrl._open_cross_project_search(["/only-one"])
        assert opened == []


# ── Novice-UX batch (Roadmap-9 Phase 4.2) ────────────────────────────────

class TestContextMenuGrouping:
    """38 flat commands ran past the bottom of a laptop screen.

    Grouping is presentation only — the guarantee that matters is that no
    command was lost or rewired on the way into a cascade.
    """

    @staticmethod
    def _menu_calls():
        import ast
        import pathlib
        src = pathlib.Path("src/controllers/projects_tab.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_build_context_menu")
        cmds, cascades = [], []
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "add_command":
                for kw in node.keywords:
                    if kw.arg == "command":
                        cmds.append(ast.unparse(kw.value))
            elif node.func.attr == "add_cascade":
                for kw in node.keywords:
                    if kw.arg == "label":
                        cascades.append(ast.literal_eval(kw.value))
        return cmds, cascades

    def test_every_command_is_wired_exactly_once(self):
        cmds, _ = self._menu_calls()
        dupes = [c for c in set(cmds) if cmds.count(c) > 1]
        assert dupes == [], f"command wired twice: {dupes}"
        assert len(cmds) == 34, f"expected 34 commands, found {len(cmds)}"

    def test_the_everyday_actions_stay_one_click_away(self):
        """Burying Sync in a submenu would make the common case worse."""
        src = open("src/controllers/projects_tab.py", encoding="utf-8").read()
        body = src[src.index("def _build_context_menu"):
                   src.index("def _on_right_click")]
        top = body[:body.index("add_cascade")]
        for label in ("Set as Active", "Sync", "Status"):
            assert label in top, f"{label!r} was pushed into a submenu"

    def test_destructive_entries_live_under_maintenance(self):
        """Remove Index sitting beside Status is how it gets misclicked."""
        src = open("src/controllers/projects_tab.py", encoding="utf-8").read()
        body = src[src.index("def _build_context_menu"):
                   src.index("def _on_right_click")]
        maint = body[body.index("maint_m = self._submenu"):]
        assert "Remove Index" in maint

    def test_there_are_cascades_at_all(self):
        _, cascades = self._menu_calls()
        assert len(cascades) >= 5, cascades


class TestColumnLegibility:
    """The Projects tab explained none of its own glyph columns."""

    def _source(self):
        return open("src/controllers/projects_tab.py", encoding="utf-8").read()

    def test_the_git_glyphs_have_a_legend(self):
        """Colour was the only thing separating ahead from behind."""
        src = self._source()
        for token in ("✓ clean", "● uncommitted", "↑n", "↓n"):
            assert token in src, f"legend missing {token!r}"

    def test_dim_rows_explain_themselves_and_name_the_remedy(self):
        src = self._source()
        assert "Dimmed row" in src
        assert "Retrofit" in src, "the legend must name the fix, not just the state"

    def test_cg_column_is_not_a_two_letter_mystery(self):
        src = self._source()
        assert 'text="CodeGraph"' in src
        assert 'text="CG"' not in src

    def test_the_retrofit_button_says_what_it_does(self):
        src = self._source()
        assert "Add tokensave to a project" in src
        assert "Retrofit Existing" not in src


# ── strict_tree toggle ───────────────────────────────────────────────────

class TestStrictTreeToggle:
    """One entry, two directions.

    The enable confirmation has always ended with "turn it off again if it
    refuses something it should not" — advice the UI could not follow, because
    the only call site passed `True` as a literal. The writer took a bool and
    its off path was already tested; nothing could reach it. These cover the
    direction that was missing and the labelling that decides which one runs.
    """

    def _ctrl(self):
        from controllers.projects_tab import ProjectsTabController
        return object.__new__(ProjectsTabController)

    def _entry(self, ctrl):
        """A stub menu entry, recording whatever label it is given."""
        seen = {}
        ctrl._strict_tree_entry = (
            SimpleNamespace(entryconfigure=lambda i, label: seen.update(
                index=i, label=label)), 4)
        return seen

    # ── which direction the entry offers ─────────────────────────────────

    def test_a_project_with_it_off_is_offered_enable(self, monkeypatch):
        import controllers.projects_tab as pt
        ctrl = self._ctrl()
        seen = self._entry(ctrl)
        monkeypatch.setattr(
            "helpers.tokensave_config.read_strict_tree",
            lambda p: SimpleNamespace(is_enabled=False))

        ctrl._sync_strict_tree_label("/some/project")

        assert seen["label"] == pt._STRICT_TREE_ON_LABEL

    def test_a_project_with_it_on_is_offered_disable(self, monkeypatch):
        """The whole point: previously there was no way to express this."""
        import controllers.projects_tab as pt
        ctrl = self._ctrl()
        seen = self._entry(ctrl)
        monkeypatch.setattr(
            "helpers.tokensave_config.read_strict_tree",
            lambda p: SimpleNamespace(is_enabled=True))

        ctrl._sync_strict_tree_label("/some/project")

        assert seen["label"] == pt._STRICT_TREE_OFF_LABEL

    def test_an_unreadable_project_is_never_offered_disable(self, monkeypatch):
        """Offering Disable would assert a state we failed to read.

        `read_strict_tree` is deliberate about never reporting an unreadable
        config as "off"; the menu must not undo that by inferring one.
        """
        import controllers.projects_tab as pt
        ctrl = self._ctrl()
        seen = self._entry(ctrl)

        def _boom(path):
            raise OSError("permission denied")

        monkeypatch.setattr("helpers.tokensave_config.read_strict_tree", _boom)

        ctrl._sync_strict_tree_label("/some/project")

        assert seen["label"] == pt._STRICT_TREE_ON_LABEL

    def test_a_menu_that_was_never_built_is_not_an_error(self):
        """`_on_right_click` runs against a stubbed menu in other tests, and
        at runtime nothing guarantees the menu exists before a relabel."""
        ctrl = self._ctrl()
        ctrl._sync_strict_tree_label("/some/project")      # must not raise

    # ── which direction actually runs ────────────────────────────────────

    def test_toggling_a_project_that_is_on_turns_it_off(self, monkeypatch):
        ctrl = self._ctrl()
        ctrl._selected_path = lambda: "/some/project"
        monkeypatch.setattr(
            "helpers.tokensave_config.read_strict_tree",
            lambda p: SimpleNamespace(is_enabled=True))
        calls = []
        ctrl._set_strict_tree = lambda paths, enabled: calls.append(
            (paths, enabled))

        ctrl._toggle_strict_tree_selected()

        assert calls == [(["/some/project"], False)]

    def test_toggling_a_project_that_is_off_turns_it_on(self, monkeypatch):
        ctrl = self._ctrl()
        ctrl._selected_path = lambda: "/some/project"
        monkeypatch.setattr(
            "helpers.tokensave_config.read_strict_tree",
            lambda p: SimpleNamespace(is_enabled=False))
        calls = []
        ctrl._set_strict_tree = lambda paths, enabled: calls.append(
            (paths, enabled))

        ctrl._toggle_strict_tree_selected()

        assert calls == [(["/some/project"], True)]

    # ── the writer is actually asked to turn it off ──────────────────────

    def test_disabling_passes_false_through_to_the_writer(self, monkeypatch):
        """The defect in one line: this argument used to be a literal True."""
        ctrl = self._ctrl()
        ctrl._tab = SimpleNamespace(winfo_toplevel=lambda: None)
        ctrl._on_log = lambda *a, **k: None
        ctrl._on_refresh = lambda: None
        monkeypatch.setattr(
            "controllers.projects_tab.messagebox.askyesno",
            lambda *a, **k: True)
        written = []
        monkeypatch.setattr(
            "helpers.tokensave_config.set_strict_tree",
            lambda path, enabled: (written.append((path, enabled)),
                                   (True, "strict_tree turned off"))[1])

        ctrl._set_strict_tree(["/p1", "/p2"], False)

        assert written == [("/p1", False), ("/p2", False)]

    def test_the_disable_confirmation_says_disable(self, monkeypatch):
        """A dialog titled "Enable" that turns something off is worse than none."""
        ctrl = self._ctrl()
        ctrl._tab = SimpleNamespace(winfo_toplevel=lambda: None)
        ctrl._on_log = lambda *a, **k: None
        ctrl._on_refresh = lambda: None
        asked = {}

        def _ask(title, body, **kw):
            asked["title"], asked["body"] = title, body
            return False                      # decline: nothing should be written

        monkeypatch.setattr(
            "controllers.projects_tab.messagebox.askyesno", _ask)
        written = []
        monkeypatch.setattr(
            "helpers.tokensave_config.set_strict_tree",
            lambda path, enabled: written.append(path))

        ctrl._set_strict_tree(["/p1"], False)

        assert "Disable" in asked["title"]
        assert "OFF" in asked["body"]
        assert written == [], "declining the dialog still wrote"

    def test_the_batch_menu_offers_both_directions(self):
        """A multi-project selection can be mixed, so it gets two entries
        rather than a toggle that would have to guess what it is toggling."""
        import ast
        import pathlib
        src = pathlib.Path("src/controllers/projects_tab.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_show_batch_menu")
        labels = [ast.unparse(kw.value)
                  for node in ast.walk(fn)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "add_command"
                  for kw in node.keywords if kw.arg == "label"]
        strict = [l for l in labels if "strict_tree" in l]
        assert len(strict) == 2, strict
        assert any("Enable" in l for l in strict)
        assert any("Disable" in l for l in strict)


def test_the_strict_tree_entry_index_still_points_at_the_toggle(controller):
    """The captured menu index must survive entries added around it.

    `_build_context_menu` records the toggle's position with `index("end")`
    immediately after adding it, and `_sync_strict_tree_label` later rewrites
    the label at that index. Insert a command above the capture and the toggle
    silently starts relabelling its neighbour instead — no error, no failing
    test, just a menu that lies. Adding the "Bind to this project" entry was
    exactly that hazard, so it is pinned rather than remembered.
    """
    from controllers import projects_tab as pt

    menu, index = controller._strict_tree_entry
    assert menu.entrycget(index, "label") in (
        pt._STRICT_TREE_ON_LABEL, pt._STRICT_TREE_OFF_LABEL)


def test_binding_is_reachable_from_the_index_cascade(controller):
    """Guards the wiring: the dialog can be perfectly correct and unreachable."""
    labels = _menu_labels(controller._ctx_menu)
    assert any("Bind to this project" in lbl for lbl in labels), labels
