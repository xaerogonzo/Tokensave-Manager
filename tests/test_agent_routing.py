"""Per-feature agent routing: one default, optional overrides, no rescue.

The contract is mostly about what must NOT happen. A named agent that is
missing must not quietly become the default, an unrecognised id must not be
read as "inherit", and a malformed table must be loud -- each of those would run
a job with an agent the person did not choose while the settings said
otherwise.
"""
import ast
import os
import types

import pytest

from helpers import agent_cli as ac
from helpers.agent_cli import (
    FEATURES,
    INHERIT,
    RESOLUTION_OK,
    RESOLUTION_UNAVAILABLE,
    RESOLUTION_UNKNOWN_AGENT,
    resolve_for_feature,
    resolve_from,
)
from state import ManagerConfig

HAVE_BOTH = lambda spec: "/x/" + spec.id          # noqa: E731
ONLY_CLAUDE = lambda spec: "/x/claude" if spec.id == "claude" else ""   # noqa: E731
FEATURE = ac.FEATURE_COMMIT_MESSAGE


# -- the table -----------------------------------------------------------------

def test_feature_ids_are_unique_and_every_constant_is_listed():
    ids = [fid for fid, _ in FEATURES]
    assert len(ids) == len(set(ids))
    constants = {v for k, v in vars(ac).items() if k.startswith("FEATURE_")}
    assert constants == set(ids)


def test_every_feature_has_a_label():
    assert all(label.strip() for _, label in FEATURES)


# -- inheriting ---------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    None, {}, {FEATURE: None}, {FEATURE: ""}, {FEATURE: "  "}, {FEATURE: INHERIT},
    {FEATURE: "Inherit"}, {"some_other_feature": "cursor"},
])
def test_an_absent_or_inherit_override_means_the_default(overrides):
    res = resolve_for_feature("claude", overrides, FEATURE, HAVE_BOTH)
    assert res.ok and res.spec.id == "claude" and res.feature == ""


def test_inherit_follows_whatever_the_default_is():
    res = resolve_for_feature("cursor", {FEATURE: INHERIT}, FEATURE, HAVE_BOTH)
    assert res.spec.id == "cursor"


# -- overriding ----------------------------------------------------------------------

def test_an_override_picks_that_agent_and_its_own_exe():
    res = resolve_for_feature("claude", {FEATURE: "cursor"}, FEATURE, HAVE_BOTH)
    assert res.ok and res.spec.id == "cursor" and res.exe == "/x/cursor"
    assert res.feature == FEATURE


def test_an_override_is_case_and_whitespace_tolerant_like_the_default():
    res = resolve_for_feature("claude", {FEATURE: "  Cursor "}, FEATURE, HAVE_BOTH)
    assert res.spec.id == "cursor"


def test_other_features_are_unaffected_by_one_override():
    overrides = {FEATURE: "cursor"}
    other = resolve_for_feature("claude", overrides, ac.FEATURE_ASK, HAVE_BOTH)
    assert other.spec.id == "claude"


# -- no rescue ---------------------------------------------------------------------------

def test_a_named_agent_that_is_missing_does_not_fall_back_to_the_default():
    res = resolve_for_feature("claude", {FEATURE: "cursor"}, FEATURE, ONLY_CLAUDE)
    assert res.state == RESOLUTION_UNAVAILABLE
    assert res.spec.id == "cursor"
    assert not res.ok and res.exe == ""


def test_an_unknown_id_is_an_error_naming_the_feature_not_the_default():
    res = resolve_for_feature("claude", {FEATURE: "codex"}, FEATURE, HAVE_BOTH)
    assert res.state == RESOLUTION_UNKNOWN_AGENT and res.spec is None
    msg = res.error_message()
    assert "'codex'" in msg and "Commit message" in msg
    assert "agent_overrides" in msg and "Agent per feature" in msg


def test_a_non_string_override_is_unknown_not_inherit():
    res = resolve_for_feature("claude", {FEATURE: 5}, FEATURE, HAVE_BOTH)
    assert res.state == RESOLUTION_UNKNOWN_AGENT


@pytest.mark.parametrize("bad", [["cursor"], "cursor", 3])
def test_a_malformed_table_is_loud(bad):
    res = resolve_for_feature("claude", bad, FEATURE, HAVE_BOTH)
    assert res.state == RESOLUTION_UNKNOWN_AGENT and res.spec is None
    assert "not an object" in res.error_message()
    assert "Commit message" in res.error_message()


def test_the_default_agents_own_faults_keep_their_original_wording():
    """An inherit that lands on a missing default is the DEFAULT's problem."""
    res = resolve_for_feature("cursor", None, FEATURE, ONLY_CLAUDE)
    assert res.state == RESOLUTION_UNAVAILABLE and res.feature == ""
    assert "Agent CLI" in res.error_message()
    assert "Agent per feature" not in res.error_message()


def test_unavailable_override_message_points_at_the_per_feature_setting():
    res = resolve_for_feature("claude", {FEATURE: "cursor"}, FEATURE, ONLY_CLAUDE)
    msg = res.error_message()
    assert "Commit message" in msg and "Agent per feature" in msg


def test_a_typo_in_a_feature_id_is_a_programming_error_not_inherit():
    with pytest.raises(ValueError):
        resolve_for_feature("claude", {}, "comit_message", HAVE_BOTH)


# -- through ManagerConfig ----------------------------------------------------------------

def _cfg(**raw):
    base = {"claude_cli_exe": "/x/claude", "cursor_cli_exe": "/x/cursor"}
    base.update(raw)
    return ManagerConfig(raw=base)


def test_no_feature_means_the_default_exactly_as_before_routing():
    cfg = _cfg(agent_cli="claude", agent_overrides={FEATURE: "cursor"})
    assert cfg.resolve_agent_cli().spec.id == "claude"


def test_a_feature_call_honours_the_override_from_config():
    cfg = _cfg(agent_cli="claude", agent_overrides={FEATURE: "cursor"})
    assert cfg.resolve_agent_cli(FEATURE).spec.id == "cursor"
    assert cfg.resolve_agent_cli(ac.FEATURE_REVIEW).spec.id == "claude"


def test_overrides_are_read_at_call_time_not_snapshotted():
    """A Settings save mutates raw in place; a stale snapshot would ignore it."""
    cfg = _cfg(agent_cli="claude")
    assert cfg.resolve_agent_cli(FEATURE).spec.id == "claude"
    cfg.raw["agent_overrides"] = {FEATURE: "cursor"}
    assert cfg.resolve_agent_cli(FEATURE).spec.id == "cursor"


def test_the_overrides_property_is_returned_raw():
    assert _cfg(agent_overrides=["x"]).agent_overrides == ["x"]
    assert _cfg().agent_overrides is None


def test_the_model_follows_the_chosen_agent_not_the_default():
    cfg = _cfg(agent_cli="claude", agent_overrides={FEATURE: "cursor"},
               cursor_cli_model="grok", claude_cli_model="haiku")
    res = cfg.resolve_agent_cli(FEATURE)
    assert ac.model_from(cfg, res.spec) == "grok"


# -- the compatibility shim for config doubles ------------------------------------------------

def test_resolve_from_passes_a_feature_only_when_one_is_named():
    seen = []
    cfg = types.SimpleNamespace(
        resolve_agent_cli=lambda *a: seen.append(a) or "R")
    resolve_from(cfg)
    resolve_from(cfg, FEATURE)
    assert seen == [(), (FEATURE,)]


def test_a_double_that_predates_routing_still_works_without_a_feature():
    cfg = types.SimpleNamespace(resolve_agent_cli=lambda: "R")
    assert resolve_from(cfg) == "R"


# -- every call site names its feature ----------------------------------------------------------

#: Sites that deliberately use the DEFAULT agent: the Git tab's "open the agent"
#: button and its label, which are not any one feature's job.
_DEFAULT_ON_PURPOSE = {os.path.join("controllers", "git_tab.py"): 2}


def _unrouted_calls():
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    found = {}
    for folder, _dirs, files in os.walk(src):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, src)
            if rel in (os.path.join("helpers", "agent_cli.py"), "state.py"):
                continue
            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if called == "resolve_agent_cli" and not node.args:
                    found[rel] = found.get(rel, 0) + 1
                elif called == "resolve_from" and len(node.args) < 2:
                    found[rel] = found.get(rel, 0) + 1
    return found


def test_no_call_site_forgets_to_name_its_feature():
    """A new caller that omits the feature silently ignores the person's routing."""
    assert _unrouted_calls() == _DEFAULT_ON_PURPOSE
