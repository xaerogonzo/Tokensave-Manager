"""tests/test_dialog_pure_helpers.py — the computed values inside four dialogs.

`OllamaModelManagerDialog`, `PrivateRepoSetupDialog`,
`PrivateRepoManagerDialog` and settings' `AISection` each compute something
that is not a widget and was not tested: a size string, a destination path, a
health verdict, and the config dictionary that gets written to disk.

The config one carries the most weight. `AISection.save_into` is the only
thing standing between the Settings dialog and `manager-config.json`, and its
job is as much about what it **preserves** as what it writes — a user who
hand-edited a key into `commit_message_llm` should not lose it by opening
Settings and clicking Save.

``object.__new__`` plus the attributes each method reads, as in
``tests/test_dialog_gitignore.py``.
"""
from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter")

from dialogs.ollama_model_mgr import OllamaModelManagerDialog
from dialogs.private_repo_mgr import PrivateRepoManagerDialog
from dialogs.private_repo_setup import PrivateRepoSetupDialog
from dialogs.settings_ai import AISection


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


# ── OllamaModelManagerDialog._human_bytes ───────────────────────────────

@pytest.mark.parametrize("size,expected", [
    (0, "—"),
    (-1, "—"),
    (512, "512 B"),
    (1024, "1.0 KB"),
    (1536, "1.5 KB"),
    (1024 ** 2, "1.0 MB"),
    (1024 ** 3, "1.0 GB"),
])
def test_human_bytes_formats_each_magnitude(size, expected):
    assert OllamaModelManagerDialog._human_bytes(size) == expected


def test_human_bytes_shows_bytes_without_a_decimal():
    """A model listing reading "512.0 B" would be noise, not precision."""
    assert OllamaModelManagerDialog._human_bytes(999) == "999 B"


def test_human_bytes_reports_nothing_rather_than_zero_for_a_missing_size():
    """0 means "the API did not tell us", not "this model is empty"."""
    assert OllamaModelManagerDialog._human_bytes(0) == "—"


# ── OllamaModelManagerDialog._server ────────────────────────────────────

def _ollama(url):
    dlg = object.__new__(OllamaModelManagerDialog)
    dlg._var_base_url = _Var(url)
    return dlg


def test_server_falls_back_to_localhost_when_blank():
    assert _ollama("   ")._server() == "http://localhost:11434"


def test_server_strips_a_trailing_slash():
    """Endpoints are appended to this; a trailing slash yields '//api/tags'."""
    assert _ollama("http://box:11434/")._server() == "http://box:11434"


def test_server_keeps_a_custom_host():
    assert _ollama("  http://gpu-box:9999 ")._server() == "http://gpu-box:9999"


# ── PrivateRepoSetupDialog._default_dest ────────────────────────────────

def _setup_dialog():
    return object.__new__(PrivateRepoSetupDialog)


def test_default_dest_lands_under_a_private_repos_folder():
    dest = _setup_dialog()._default_dest("C:/work/thing", "thing")
    assert os.path.join("Private-Repos", "thing-") in dest


def test_two_projects_with_the_same_name_get_different_destinations():
    """The reason the hash is there (G9): same leaf name, different source.

    Without it, two projects called "docs" would sync into one another's
    private repo.
    """
    dlg = _setup_dialog()
    a = dlg._default_dest("C:/work/alpha/docs", "docs")
    b = dlg._default_dest("C:/work/beta/docs", "docs")
    assert a != b


def test_the_destination_is_stable_for_one_source():
    """It is recomputed on every open; a changing default would strand the
    previous repo."""
    dlg = _setup_dialog()
    first = dlg._default_dest("C:/work/alpha/docs", "docs")
    second = dlg._default_dest("C:/work/alpha/docs", "docs")
    assert first == second


def test_the_destination_does_not_depend_on_path_spelling():
    """abspath is applied first, so a relative-ish spelling of the same
    directory does not produce a second private repo."""
    dlg = _setup_dialog()
    plain = dlg._default_dest(os.path.abspath("C:/work/alpha"), "alpha")
    dotted = dlg._default_dest(os.path.abspath("C:/work/./alpha"), "alpha")
    assert plain == dotted


# ── PrivateRepoManagerDialog._health_status ─────────────────────────────

def _mgr(dest):
    dlg = object.__new__(PrivateRepoManagerDialog)
    dlg._dest_var = _Var(dest)
    return dlg


def test_health_reports_healthy_for_an_existing_folder(tmp_path):
    msg, _colour = _mgr(str(tmp_path))._health_status()
    assert "healthy" in msg.lower()


def test_health_reports_missing_for_a_folder_that_is_not_there(tmp_path):
    msg, _colour = _mgr(str(tmp_path / "gone"))._health_status()
    assert "not found" in msg.lower()


def test_health_does_not_treat_a_blank_destination_as_healthy():
    """`os.path.isdir("")` is False, but this is worth pinning: a blank
    destination reading as a healthy repo would hide an unconfigured sync."""
    msg, _colour = _mgr("   ")._health_status()
    assert "not found" in msg.lower()


# ── AISection.save_into ─────────────────────────────────────────────────

_AI_VARS = {
    "_var_draft_pr_backend": "auto",
    "_var_commit_msg_backend": "llm_first",
    "_var_enable_llm_grounding": True,
    "_var_enable_commit_grounding": False,
    "_var_enable_pr_grounding": True,
    "_var_llm_min_diff": "25",
    "_var_llm_enabled": True,
    "_var_llm_provider": "ollama",
    "_var_llm_model": "  qwen2.5-coder  ",
    "_var_llm_keyenv": "",
    "_var_llm_base_url": "http://localhost:11434",
    "_var_llm_for_sync": False,
    "_var_ask_enabled": True,
    "_var_ask_provider": "anthropic",
    "_var_ask_model": " claude ",
    "_var_ask_keyenv": "ANTHROPIC_API_KEY",
    "_var_ask_base_url": "",
    "_var_ollama_num_ctx": 8192,
    "_var_ollama_warmup": True,
}


def _ai_section(**overrides):
    section = object.__new__(AISection)
    values = dict(_AI_VARS)
    values.update(overrides)
    for name, value in values.items():
        setattr(section, name, _Var(value))
    return section


def test_save_into_writes_the_backend_choices():
    raw: dict = {}
    _ai_section().save_into(raw)
    assert raw["draft_pr_backend"] == "auto"
    assert raw["commit_message_backend"] == "llm_first"


def test_save_into_preserves_keys_it_does_not_own():
    """A user who hand-edited a key into commit_message_llm must not lose it
    by opening Settings and clicking Save. This is why the code updates the
    existing dict rather than replacing it."""
    raw = {"commit_message_llm": {"hand_edited": "keep me"}}
    _ai_section().save_into(raw)
    assert raw["commit_message_llm"]["hand_edited"] == "keep me"


def test_save_into_strips_whitespace_from_the_model_name():
    raw: dict = {}
    _ai_section().save_into(raw)
    assert raw["commit_message_llm"]["model"] == "qwen2.5-coder"


def test_a_blank_provider_falls_back_rather_than_writing_empty():
    """An empty provider would be written to config and fail at dispatch."""
    raw: dict = {}
    _ai_section(_var_llm_provider="   ").save_into(raw)
    assert raw["commit_message_llm"]["provider"] == "anthropic"


def test_a_non_numeric_min_diff_falls_back_to_the_default():
    """The field is free text; "abc" must not propagate into config."""
    raw: dict = {}
    _ai_section(_var_llm_min_diff="not a number").save_into(raw)
    assert raw["commit_message_llm"]["min_diff_lines"] == 10


def test_a_negative_min_diff_is_clamped_to_zero():
    raw: dict = {}
    _ai_section(_var_llm_min_diff="-5").save_into(raw)
    assert raw["commit_message_llm"]["min_diff_lines"] == 0


def test_defaults_are_filled_but_never_overwrite_an_existing_value():
    """setdefault, not assignment — a tuned timeout survives a Save."""
    raw = {"commit_message_llm": {"timeout_seconds": 300}}
    _ai_section().save_into(raw)
    assert raw["commit_message_llm"]["timeout_seconds"] == 300
    assert raw["commit_message_llm"]["max_diff_chars"] == 24000


def test_the_ask_tab_config_is_written_independently():
    """Ask-tab settings are their own block — sharing commit_message_llm was
    the bug that made changing one silently change the other."""
    raw: dict = {}
    _ai_section().save_into(raw)
    assert raw["ask_tab_llm"]["provider"] == "anthropic"
    assert raw["ask_tab_llm"]["model"] == "claude"
    assert raw["commit_message_llm"]["provider"] == "ollama"


def test_save_into_reports_success():
    assert _ai_section().save_into({}) is True
