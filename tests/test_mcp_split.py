"""tests/test_mcp_split.py — the two rules that make the mcp split safe.

`helpers/mcp.py` was 1590 lines and is now a re-export facade over six
family modules. Two properties hold it together, and both fail silently.

**Identity, not existence.** Twenty modules import from `helpers.mcp`, and
several take private names directly — `dialogs/mcp_config.py` alone pulls
seven. A facade that merely *has* an attribute called `_classify_mcp_entry`
passes any `hasattr` check while serving a stale copy left behind during the
split. So these assert the facade's object **is** the family module's object.

**Direction.** `helpers/mcp_shadow.py` and `helpers/mcp_desktop.py` already
reached into `helpers.mcp` for private names before the split. If they keep
importing the facade while the facade imports the family, that is a runtime
import cycle waiting for the first module that needs one of them. The rule
is one-way and a grep-level guard outlives anyone's memory of why.
"""
from __future__ import annotations

import importlib
import io
import os

import pytest

FAMILY = (
    "mcp_paths", "mcp_projects", "mcp_classify",
    "mcp_approval", "mcp_scope", "mcp_agents",
)

#: Siblings that predate the split and must import the leaf, not the facade.
SIBLINGS = ("mcp_shadow", "mcp_desktop")


def _src_root() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _read(rel: str) -> str:
    return io.open(os.path.join(_src_root(), rel), encoding="utf-8").read()


# ── identity ─────────────────────────────────────────────────────────────

def test_every_exported_name_is_the_family_modules_own_object():
    """A stale duplicate in the facade would pass `hasattr` and fail here."""
    facade = importlib.import_module("helpers.mcp")
    owners = {name: importlib.import_module(f"helpers.{mod}")
              for mod in FAMILY
              for name in getattr(importlib.import_module(f"helpers.{mod}"),
                                  "__dict__", {})}
    checked = 0
    for name in facade.__all__:
        got = getattr(facade, name)
        owner = owners.get(name)
        assert owner is not None, f"{name} is exported by no family module"
        assert getattr(owner, name) is got, (
            f"helpers.mcp.{name} is not {owner.__name__}.{name} — "
            f"a stale copy survived the split")
        checked += 1
    # Report the population: an __all__ that silently emptied would make
    # every assertion above vacuous.
    assert checked >= 70, f"only {checked} names checked; __all__ looks short"


def test_the_private_names_call_sites_actually_use_are_re_exported():
    """Pinned by name, because these are what would break a 20-file change.

    Taken from the real import sites, not invented: `dialogs/mcp_config.py`,
    `app.py`, `cli.py`, `doctor_ctrl.py`, `sync_ctrl.py`, `settings*.py`,
    `doctor_service.py` and `tool_manager.py`.
    """
    facade = importlib.import_module("helpers.mcp")
    for name in ("_mcp_configs", "_classify_mcp_entry", "_apply_mcp_fix",
                 "_is_claude_running", "_mcp_code_cfg_path",
                 "_project_mcp_path", "_canonical_mcp_entry",
                 "_claude_code_mcp_has_codegraph", "_tokensave_agent_installed",
                 "_tokensave_agent_wired", "_same_project",
                 "_resolve_desktop_cfg_path", "_write_json_atomic",
                 "_mcp_desktop_cfg_path"):
        assert hasattr(facade, name), f"facade dropped {name}"


# ── direction ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("module", FAMILY)
def test_no_family_module_imports_the_facade(module):
    """The invariant the whole layering rests on."""
    text = _read(os.path.join("helpers", f"{module}.py"))
    for forbidden in ("from helpers.mcp import",
                      "from helpers import mcp\n",
                      "import helpers.mcp\n"):
        assert forbidden not in text, (
            f"helpers/{module}.py imports the facade — that closes a cycle "
            f"the moment the facade imports this module")


@pytest.mark.parametrize("module", SIBLINGS)
def test_siblings_import_the_leaf_not_the_facade(module):
    """These two are why `mcp_paths` exists as a separate leaf at all."""
    text = _read(os.path.join("helpers", f"{module}.py"))
    assert "from helpers.mcp import" not in text, (
        f"helpers/{module}.py still imports the facade; it must import "
        f"helpers.mcp_paths so the facade can import the family safely")
    assert "from helpers.mcp_paths import" in text


def test_the_leaf_imports_no_sibling():
    """`mcp_paths` is the bottom of the family; nothing below it to import."""
    text = _read(os.path.join("helpers", "mcp_paths.py"))
    for other in FAMILY:
        if other == "mcp_paths":
            continue
        assert f"helpers.{other}" not in text, (
            f"mcp_paths imports {other}; it is supposed to be the leaf")


def test_the_facade_is_only_a_facade():
    """No logic may accumulate here, or the split quietly reverses.

    A facade that grows a function is a seventh module nobody named, and the
    next person to look for `_classify_mcp_entry` finds two of them.
    """
    import ast
    tree = ast.parse(_read(os.path.join("helpers", "mcp.py")))
    for node in tree.body:
        assert isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr,
                                 ast.Assign)), (
            f"helpers/mcp.py grew a {type(node).__name__} at line "
            f"{node.lineno}; it is a re-export facade, not a module")


# ── the dialog's mixin composition ───────────────────────────────────────

#: Which mixin each moved method must resolve to. Pinned by name because a
#: reorder of the bases changes the answer silently — Python picks the first
#: base that defines the name and reports nothing.
OWNERS = {
    "_render_duplicate_keys":       "DuplicateKeysMixin",
    "_toggle_dups":                 "DuplicateKeysMixin",
    "_render_user_scope_migration": "UserScopeMigrationMixin",
    "_apply_verification":          "UserScopeMigrationMixin",
    "_approve_all":                 "UserScopeMigrationMixin",
    "_migration_status":            "UserScopeMigrationMixin",
    "_render_block":                "EntryBlocksMixin",
    "_render_block_actions":        "EntryBlocksMixin",
    "_badge_colour":                "EntryBlocksMixin",
    # Deliberately NOT moved: a known Doctor complexity offender, and moving
    # it would have put two changes in one commit.
    "_render_projects_section":     "MCPConfigDialog",
    "_apply":                       "MCPConfigDialog",
    "_render":                      "MCPConfigDialog",
}


def test_every_moved_method_resolves_to_its_intended_mixin():
    """Guards the base order, which nothing else would notice breaking.

    Not introspection for its own sake: three mixins on one class means the
    MRO decides which implementation wins, and a future base reorder that
    shadowed `_render_block` would still construct, still render, and be
    wrong.
    """
    from dialogs.mcp_config import MCPConfigDialog
    for method, owner in OWNERS.items():
        got = getattr(MCPConfigDialog, method, None)
        assert got is not None, f"{method} vanished in the split"
        assert got.__qualname__.split(".")[0] == owner, (
            f"{method} resolves to {got.__qualname__}, expected {owner}")


def test_no_mixin_shadows_another():
    """Each moved name is defined exactly once across the bases."""
    from dialogs.mcp_config import MCPConfigDialog
    for method in OWNERS:
        definers = [c.__name__ for c in MCPConfigDialog.__mro__
                    if method in vars(c)]
        assert len(definers) == 1, (
            f"{method} is defined in {definers} — one of them is dead code "
            f"that the MRO silently discards")


def test_the_dialogs_public_construction_signature_is_unchanged():
    """The split is only safe if callers cannot tell it happened."""
    import inspect
    from dialogs.mcp_config import MCPConfigDialog
    sig = inspect.signature(MCPConfigDialog.__init__)
    assert list(sig.parameters) == ["self", "parent", "cfg", "focus_project"]
    assert sig.parameters["focus_project"].default == ""


def test_the_mixins_are_never_instantiated_alone():
    """They read `self` attributes the host owns, so they are not dialogs.

    Asserted by shape rather than by trying to construct one: a mixin that
    grew a `__init__` is a class pretending to be standalone, and that is
    the change worth catching.
    """
    from dialogs import (mcp_blocks_panel, mcp_duplicates_panel,
                         mcp_migration_panel)
    for mod, name in ((mcp_duplicates_panel, "DuplicateKeysMixin"),
                      (mcp_migration_panel, "UserScopeMigrationMixin"),
                      (mcp_blocks_panel, "EntryBlocksMixin")):
        cls = getattr(mod, name)
        assert cls.__bases__ == (object,), f"{name} grew a base class"
        assert "__init__" not in vars(cls), f"{name} grew an __init__"
