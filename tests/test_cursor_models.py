"""Tests for helpers/cursor_models.py and the Settings row that uses it.

The parser is checked against CAPTURED output (tests/fixtures/cursor_models/),
not against a format remembered from a doc: the three things that would have
been guessed wrong are in the fixture on purpose -- ids with dots, a label with a
double space, and the `Tip:` prose after the list.
"""
import os
import subprocess
import types

import pytest

from helpers import cursor_models as cm
from helpers.cursor_models import (
    LISTED,
    NOT_LISTED,
    STATE_FAILED,
    STATE_NEEDS_LOGIN,
    STATE_OK,
    STATE_UNAVAILABLE,
    UNKNOWN,
    Catalogue,
    Model,
    describe,
    fetch,
    membership,
    parse_list_models,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "cursor_models")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def _proc(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture
def run(monkeypatch):
    """Replace subprocess.run in the helper; returns the list of calls."""
    calls = []

    def install(result=None, raises=None):
        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            if raises:
                raise raises
            return result
        monkeypatch.setattr(cm.subprocess, "run", fake)
        return calls
    return install


# -- parsing real output ---------------------------------------------------------

def test_the_captured_list_parses_to_its_sixteen_models():
    models = parse_list_models(_fixture("list_models.txt"))
    assert len(models) == 16
    assert models[0] == Model("claude-opus-4-8-xhigh",
                              "Claude Opus 4.8 1M Extra High")


def test_ids_with_dots_and_many_dashes_survive():
    ids = {m.id for m in parse_list_models(_fixture("list_models.txt"))}
    assert {"gpt-5.6-sol-medium", "claude-4.6-sonnet-medium",
            "muse-wonder-1.0-minimal", "kimi-k2.7-code",
            "deepseek-v41-flash-1017-max", "gpt-5.1"} <= ids


def test_the_tip_prose_after_the_list_is_not_a_model():
    ids = {m.id for m in parse_list_models(_fixture("list_models.txt"))}
    assert not any(i.lower().startswith(("tip", "parameterized")) for i in ids)


def test_labels_keep_their_odd_spacing_and_suffixes():
    by_id = {m.id: m.label for m in parse_list_models(_fixture("list_models.txt"))}
    assert by_id["muse-wonder-1.0-minimal"] == "Muse Wonder  Minimal"
    assert by_id["claude-fable-5-1-high"].endswith("(NO ZDR)")


def test_a_repeated_id_is_listed_once():
    assert len(parse_list_models("a-b - A\na-b - A again\n")) == 1


@pytest.mark.parametrize("text", ["", None, "\n\n", "Loading models...\n",
                                  "Error: Authentication required."])
def test_non_model_output_parses_to_nothing(text):
    assert parse_list_models(text) == ()


# -- fetching: every state is distinct ------------------------------------------------

def test_no_exe_is_unavailable_and_runs_nothing(run):
    calls = run(_proc("x - y"))
    cat = fetch("")
    assert cat.state == STATE_UNAVAILABLE and not calls


def test_a_good_run_returns_the_models_and_a_timestamp(run):
    calls = run(_proc(_fixture("list_models.txt")))
    cat = fetch("agent.cmd")
    assert cat.ok and len(cat.models) == 16 and cat.fetched_at is not None
    argv, kwargs = calls[0]
    assert argv == ["agent.cmd", "--list-models"]
    assert kwargs["stdin"] == subprocess.DEVNULL     # it must never wait on a prompt


def test_the_real_signed_out_message_is_needs_login_not_failed(run):
    run(_proc(stderr=_fixture("needs_login.txt"), returncode=1))
    cat = fetch("agent.cmd")
    assert cat.state == STATE_NEEDS_LOGIN
    assert "agent login" in cat.detail


def test_login_is_recognised_on_stdout_too(run):
    run(_proc(stdout="Not logged in", returncode=0))
    assert fetch("agent.cmd").state == STATE_NEEDS_LOGIN


def test_a_nonzero_exit_is_failed_with_the_tail(run):
    run(_proc(stderr="boom happened", returncode=2))
    cat = fetch("agent.cmd")
    assert cat.state == STATE_FAILED and "boom happened" in cat.detail


def test_a_clean_exit_with_nothing_parseable_is_failed_not_an_empty_list(run):
    """'No models' would tell a signed-in person their account has none."""
    run(_proc("something unexpected\n"))
    cat = fetch("agent.cmd")
    assert cat.state == STATE_FAILED and cat.models == ()
    assert "no models" in cat.detail


def test_a_timeout_is_failed(run):
    run(raises=subprocess.TimeoutExpired("agent", 45))
    cat = fetch("agent.cmd")
    assert cat.state == STATE_FAILED and "Timed out" in cat.detail


def test_a_missing_binary_is_failed(run):
    run(raises=FileNotFoundError("nope"))
    assert fetch("agent.cmd").state == STATE_FAILED


# -- membership is three-valued ----------------------------------------------------------

def _ok():
    return Catalogue(STATE_OK, parse_list_models(_fixture("list_models.txt")))


def test_a_listed_model_is_listed():
    assert membership(_ok(), "cursor-grok-4.5-low") == LISTED


def test_an_unlisted_model_is_not_listed():
    assert membership(_ok(), "claude-haiku-4-5-20251001") == NOT_LISTED


def test_a_bracketed_override_is_checked_by_its_base_id():
    assert membership(_ok(), "claude-opus-4-8-xhigh[effort=high]") == LISTED


def test_without_a_usable_catalogue_nothing_is_not_listed():
    """Unknown is not false: a failed fetch must not mark every pin as missing."""
    for state in (STATE_FAILED, STATE_NEEDS_LOGIN, STATE_UNAVAILABLE):
        assert membership(Catalogue(state), "cursor-grok-4.5-low") == UNKNOWN


def test_an_empty_model_is_auto_and_unknown_not_missing():
    assert membership(_ok(), "") == UNKNOWN
    assert membership(_ok(), "   ") == UNKNOWN


# -- the status line ----------------------------------------------------------------------------

def test_describe_says_listed_is_not_allowed_by_plan():
    text, tone = describe(_ok(), "cursor-grok-4.5-low")
    assert tone == "ok" and "16 models listed" in text
    assert "does not mean your plan allows it" in text


def test_describe_warns_that_a_missing_pin_is_kept_not_replaced():
    text, tone = describe(_ok(), "old-model-x")
    assert tone == "warn" and "'old-model-x' is not in this list" in text
    assert "kept as set" in text


def test_describe_does_not_accuse_a_pin_when_the_list_is_unreadable():
    text, tone = describe(Catalogue(STATE_FAILED, detail="Timed out after 45s."), "x")
    assert tone == "bad" and "not in this list" not in text


def test_describe_login_is_a_warning_with_the_fix():
    text, tone = describe(Catalogue(STATE_NEEDS_LOGIN, detail="Not signed in. Run `agent login`, then refresh."), "")
    assert tone == "warn" and "agent login" in text


# -- the Settings row ----------------------------------------------------------------------------

tk = pytest.importorskip("tkinter")


@pytest.mark.tk
class TestSettingsRow:
    def _section(self, tk_root, mocker, raw=None):
        for det in ("_detect_git", "_detect_gh", "_detect_claude_cli",
                    "_detect_cursor_cli"):
            mocker.patch("dialogs.settings_paths.%s" % det, return_value="")
        from dialogs.settings_paths import PathsSection
        frame = tk.Frame(tk_root)
        cfg = types.SimpleNamespace(
            raw=dict(raw or {}), cursor_cli_exe="agent.cmd", git_exe="")
        section = PathsSection(tk_root, frame, cfg, lambda: None)
        return section, cfg

    def test_the_row_builds_with_the_stored_model_and_asks_nothing_yet(
            self, tk_root, mocker):
        called = mocker.patch("helpers.cursor_models.fetch")
        section, _ = self._section(tk_root, mocker, {"cursor_cli_model": "gpt-5.1"})
        assert section._var_cursor_cli_model.get() == "gpt-5.1"
        assert not called.called          # building the page must not run the CLI

    def test_save_writes_the_model_stripped(self, tk_root, mocker):
        section, _ = self._section(tk_root, mocker)
        section._var_cursor_cli_model.set("  cursor-grok-4.5-low ")
        raw = {}
        section.save_into(raw)
        assert raw["cursor_cli_model"] == "cursor-grok-4.5-low"

    def test_refresh_fills_the_list_and_reports_it(self, tk_root, mocker, wait_for):
        mocker.patch("helpers.cursor_models.fetch", return_value=_ok())
        section, _ = self._section(tk_root, mocker)
        section._refresh_cursor_models()
        wait_for(lambda: "models listed" in section._cursor_models_status.cget("text"))
        assert "cursor-grok-4.5-low" in section._cursor_model_combo.cget("values")
        assert str(section._cursor_models_btn.cget("state")) == "normal"

    def test_a_pinned_model_missing_from_the_list_is_kept(self, tk_root, mocker, wait_for):
        mocker.patch("helpers.cursor_models.fetch", return_value=_ok())
        section, _ = self._section(tk_root, mocker, {"cursor_cli_model": "retired-model"})
        section._refresh_cursor_models()
        wait_for(lambda: "models listed" in section._cursor_models_status.cget("text"))
        assert section._var_cursor_cli_model.get() == "retired-model"
        assert "kept as set" in section._cursor_models_status.cget("text")

    def test_a_failed_fetch_leaves_the_pin_and_the_list_alone(self, tk_root, mocker, wait_for):
        mocker.patch("helpers.cursor_models.fetch", return_value=Catalogue(
            STATE_NEEDS_LOGIN, detail="Not signed in. Run `agent login`, then refresh."))
        section, _ = self._section(tk_root, mocker, {"cursor_cli_model": "gpt-5.1"})
        section._refresh_cursor_models()
        wait_for(lambda: "agent login" in section._cursor_models_status.cget("text"))
        assert section._var_cursor_cli_model.get() == "gpt-5.1"
        assert tuple(section._cursor_model_combo.cget("values")) == ("",)
