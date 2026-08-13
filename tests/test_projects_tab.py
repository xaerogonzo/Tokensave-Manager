"""Tests for controllers/projects_tab.py — ProjectsTabController."""

import os
import pytest
import threading
import time
import tkinter as tk
from tkinter import ttk
from unittest import mock
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
    """Every command label in a tk.Menu, skipping separators."""
    out = []
    for i in range(menu.index("end") + 1):
        if menu.type(i) == "separator":
            continue
        out.append(menu.entrycget(i, "label"))
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

    idx = next(i for i in range(controller._ctx_menu.index("end") + 1)
               if controller._ctx_menu.type(i) != "separator"
               and "Housekeeping" in controller._ctx_menu.entrycget(i, "label"))
    controller._ctx_menu.invoke(idx)

    assert opened == [str(tmp_path)]
