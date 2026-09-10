"""tests/test_mcp_posture_purity.py — the aggregate must not become a classifier.

The whole point of `mcp_posture` is that the MCP surface had five panels each
correct about its own question and nothing that composed them. Adding a sixth
opinion — one that re-reads `~/.claude.json`, re-decides trust, or re-derives
what an entry means — would leave the surface exactly as it was, plus one more
place for the answers to disagree.

`classify_posture` and `service_of` are therefore pure aggregation: they
consume facts that `mcp_classify`, `mcp_projects` and `mcp_approval` already
decided, and they consume them as strings. This guard makes that mechanical
instead of aspirational, in the family of `test_no_import_time_path_resolution`
and `test_mcp_split`: every underlying reader is replaced with something that
raises, and the pure functions must still answer.

The failure it catches is gradual and plausible — someone adds a "but let me
just check whether it's really approved" to `classify_posture` and it works
fine, because on a developer's machine the reader is right there.
"""
from __future__ import annotations

import builtins

import pytest

from helpers import mcp_approval, mcp_classify, mcp_paths, mcp_projects
from helpers import mcp_posture as posture
from helpers.mcp_paths import LIFECYCLE_PRESENT, LIFECYCLE_RETIRED
from helpers.mcp_posture import (
    READ_OK,
    TIER_EXPLICIT,
    TIER_EXPLICIT_INERT,
    TIER_NONE,
    VERDICT_NO,
    VERDICT_YES,
    ProjectTier,
    classify_posture,
    service_of,
    tier_of,
)

#: Every reader the aggregate is allowed to consume *results* from, and must
#: never call itself. Named on the module that owns each one, so a call through
#: any import path is caught.
FORBIDDEN = [
    (mcp_classify, "_classify_mcp_entry"),
    (mcp_approval, "annotate_project_binding"),
    (mcp_approval, "mcpjson_approval"),
    (mcp_approval, "local_settings_approval"),
    (mcp_projects, "project_trust_state"),
    (mcp_projects, "read_claude_projects"),
    (mcp_paths, "_project_mcp_path"),
    (mcp_paths, "_mcp_code_cfg_path"),
]


@pytest.fixture
def no_readers(mocker):
    """Every underlying reader raises, and so does `open`."""
    for module, name in FORBIDDEN:
        mocker.patch.object(
            module, name,
            side_effect=AssertionError(
                "classify_posture called %s.%s — it must consume facts, "
                "not re-derive them" % (module.__name__, name)))

    real_open = builtins.open

    def _no_open(*a, **kw):
        raise AssertionError("classify_posture opened a file: %r" % (a[:1],))

    mocker.patch.object(builtins, "open", _no_open)
    yield
    builtins.open = real_open


def _p(tier, name="proj"):
    return ProjectTier(root="/" + name, display_root="/" + name, name=name,
                       tier=tier)


def test_classify_posture_touches_no_reader_and_no_file(no_readers):
    got = classify_posture(
        desktop_state=LIFECYCLE_RETIRED, desktop_read=READ_OK,
        userscope_state=LIFECYCLE_PRESENT, userscope_read=READ_OK,
        project_tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "b")])

    assert got.independent == VERDICT_YES
    assert got.covered == VERDICT_YES
    assert got.automatic_fallback is True


def test_service_of_and_tier_of_are_pure_too(no_readers):
    """The two helpers the renderer calls per row. A file read here would run
    once per project, per render, on the Tk thread."""
    assert service_of(TIER_EXPLICIT_INERT, True) == "automatic"
    assert service_of(TIER_EXPLICIT_INERT, False) == "unserved"
    assert tier_of("ok", True) == TIER_EXPLICIT
    assert tier_of("ok", False) == TIER_EXPLICIT_INERT


def test_it_answers_about_its_inputs_even_when_they_contradict_reality(
        no_readers):
    """Fed a state that does not match this machine, it reports on what it was
    given rather than going back to disk to check.

    That is the contract, and it is what makes the function testable at all:
    the developer's own `~/.claude.json` must be irrelevant to the result.
    """
    got = classify_posture(
        desktop_state=LIFECYCLE_PRESENT, desktop_read=READ_OK,
        userscope_state=LIFECYCLE_RETIRED, userscope_read=READ_OK,
        project_tiers=[_p(TIER_NONE, "a")])

    assert got.independent == VERDICT_NO
    assert got.covered == VERDICT_NO
    assert got.automatic_fallback is False


def test_read_posture_is_the_only_io_boundary():
    """Stated as a signature check, so the split cannot quietly reverse.

    If `classify_posture` ever needs a config dict, someone has decided the
    aggregate should parse — and that is the decision this file exists to make
    visible in review.
    """
    import inspect

    params = set(inspect.signature(classify_posture).parameters)
    assert params == {"desktop_state", "desktop_read", "userscope_state",
                      "userscope_read", "project_tiers"}, params
    # No `cfg`, no `raw`, no path.
    assert not any(p in params for p in ("cfg", "raw", "path", "projects_json"))


def test_read_posture_never_raises_when_every_reader_falls_over(mocker):
    """A degraded read must produce a degraded posture, not an exception.

    The overview is the first thing the MCP dialog renders. A reader that
    throws here would take down the dialog the user opened to diagnose it.
    """
    mocker.patch.object(posture, "_discover", return_value=[])
    mocker.patch.object(
        mcp_classify, "_classify_mcp_entry",
        side_effect=OSError("boom"))

    class _Cfg:
        raw = {}
        search_roots = []

    got = posture.read_posture(_Cfg())
    assert got.reads_ok is False
    assert got.headline_ok is False
