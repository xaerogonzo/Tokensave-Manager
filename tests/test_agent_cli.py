"""Tests for helpers/agent_cli.py — the agent-CLI registry.

Organised around the four contracts the registry exists to hold:

  Contract A  positional-argv prompts have a size budget, PER RUNNER
  Contract B  the system-prompt fallback has one stable serialization
  Contract C  persisted agent ids are validated, never coerced
  Contract D  resolution distinguishes ok / unavailable / unknown_agent

Contracts C and D are exercised from the config side in
`tests/test_backend_compat.py`; what lives here is the registry half
(`get_spec` returning None rather than a default).

Monkeypatching
--------------
`subprocess` and `sys` are module-scope imports in agent_cli, so patches target
`"helpers.agent_cli.*"`. `sys.platform` is patched explicitly in the budget
tests because the budgets are Windows-only by design — a POSIX ARG_MAX is
megabytes and enforcing a ceiling there would invent a failure the platform
does not have.
"""
import os

import pytest

from helpers.agent_cli import (
    AGENT_CLIS,
    BUDGET_MARGIN,
    CLAUDE,
    CMDLINE_BUDGET,
    CREATEPROCESS_BUDGET,
    CURSOR,
    DEFAULT_AGENT_ID,
    INTERACTIVE_SUPPORTED,
    SYSTEM_PROMPT_NATIVE,
    SYSTEM_PROMPT_PREPEND,
    TRANSPORT_ARGV,
    TRANSPORT_STDIN,
    build_print_argv,
    build_spawn_cmdline,
    compose_prompt,
    detect,
    get_spec,
)


# ── The table itself ─────────────────────────────────────────────────────────

def test_both_agents_registered():
    assert set(AGENT_CLIS) == {"claude", "cursor"}


def test_default_agent_is_claude():
    """Every pre-Cursor config implicitly had this value."""
    assert DEFAULT_AGENT_ID == "claude"


def test_claude_and_cursor_have_opposite_capabilities():
    """The two capability axes the registry exists to express."""
    assert CLAUDE.system_prompt_mode == SYSTEM_PROMPT_NATIVE
    assert CLAUDE.prompt_transport == TRANSPORT_STDIN
    assert CURSOR.system_prompt_mode == SYSTEM_PROMPT_PREPEND
    assert CURSOR.prompt_transport == TRANSPORT_ARGV


def test_native_agent_has_a_system_flag_and_prepend_agent_does_not():
    assert CLAUDE.system_flag
    assert CURSOR.system_flag == ""


def test_config_keys_are_distinct_per_agent():
    """A shared key would make one agent's path silently overwrite the other's."""
    assert CLAUDE.config_key_exe != CURSOR.config_key_exe
    assert CLAUDE.config_key_model != CURSOR.config_key_model


def test_cursor_default_model_is_empty():
    """Claude's Haiku id is not a valid Cursor model — copying it would 400."""
    assert CURSOR.default_model == ""


# ── Contract C (registry half): unknown ids resolve to None, never a default ──

@pytest.mark.parametrize("bad", ["cursor_old", "", "  ", "codex", "CLAUDE_"])
def test_get_spec_returns_none_for_unknown_id(bad):
    """Falling back to Claude would run one agent while the UI named another."""
    assert get_spec(bad) is None


def test_get_spec_is_case_and_whitespace_insensitive_for_known_ids():
    assert get_spec("  Cursor ") is CURSOR


def test_get_spec_handles_none():
    assert get_spec(None) is None


# ── Contract B: system-prompt serialization ──────────────────────────────────

def test_compose_both_uses_the_template():
    out = compose_prompt("be terse", "explain X")
    assert out == "[System instructions]\nbe terse\n\n[User request]\nexplain X"


def test_compose_user_only_is_verbatim():
    """No system prompt means no wrapper — byte-identical to a native call."""
    assert compose_prompt("", "explain X") == "explain X"


def test_compose_system_only_omits_an_empty_request_section():
    """An empty '[User request]' would read as a request nobody made."""
    assert compose_prompt("be terse", "") == "[System instructions]\nbe terse"


def test_compose_both_empty():
    assert compose_prompt("", "") == ""


@pytest.mark.parametrize("system,user", [(None, None), (None, "u"), ("s", None)])
def test_compose_tolerates_none(system, user):
    """cfg.get() returns None for a JSON null; str concat on None would raise."""
    compose_prompt(system, user)  # must not raise


def test_compose_preserves_multiline_bodies():
    out = compose_prompt("line1\nline2", "req1\nreq2")
    assert "line1\nline2" in out
    assert "req1\nreq2" in out


def test_native_agent_never_prepends():
    """Claude passes its system prompt via flag; prepending too would duplicate it."""
    argv, stdin_text, err = build_print_argv(
        CLAUDE, "claude.cmd", "explain X", system_prompt="be terse")
    assert err == ""
    assert "[System instructions]" not in stdin_text
    assert stdin_text == "explain X"
    assert "--append-system-prompt" in argv
    assert "be terse" in argv


def test_prepend_agent_folds_system_into_the_positional_prompt():
    argv, stdin_text, err = build_print_argv(
        CURSOR, "cursor-agent", "explain X", system_prompt="be terse")
    assert err == ""
    assert stdin_text is None
    assert argv[-1] == "[System instructions]\nbe terse\n\n[User request]\nexplain X"


# ── build_print_argv shape ───────────────────────────────────────────────────

def test_stdin_agent_keeps_the_prompt_out_of_argv():
    """The whole point of stdin transport: argv stays small and unmangled."""
    argv, stdin_text, err = build_print_argv(CLAUDE, "claude.cmd", "a" * 5000)
    assert err == ""
    assert stdin_text == "a" * 5000
    assert not any("aaaa" in part for part in argv)


def test_argv_agent_carries_the_prompt_positionally():
    argv, stdin_text, err = build_print_argv(CURSOR, "cursor-agent", "hello")
    assert err == ""
    assert stdin_text is None
    assert argv[-1] == "hello"


def test_print_args_are_the_whole_fragment_not_one_flag():
    argv, _, _ = build_print_argv(CURSOR, "cursor-agent", "hi")
    assert argv[:4] == ["cursor-agent", "-p", "--output-format", "text"]


def test_model_flag_omitted_when_model_is_empty():
    argv, _, _ = build_print_argv(CLAUDE, "claude.cmd", "hi", model="")
    assert "--model" not in argv


def test_model_flag_included_when_model_is_set():
    argv, _, _ = build_print_argv(CLAUDE, "claude.cmd", "hi", model="haiku")
    assert argv[argv.index("--model") + 1] == "haiku"


def test_empty_exe_is_refused_before_any_spawn():
    argv, stdin_text, err = build_print_argv(CLAUDE, "", "hi")
    assert argv == []
    assert "not configured" in err


# ── Contract A: budgets, per runner ──────────────────────────────────────────

def _win(monkeypatch):
    monkeypatch.setattr("helpers.agent_cli.sys.platform", "win32")


def _posix(monkeypatch):
    monkeypatch.setattr("helpers.agent_cli.sys.platform", "linux")


def test_argv_agent_rejects_an_oversized_prompt_on_the_createprocess_route(monkeypatch):
    _win(monkeypatch)
    huge = "x" * (CREATEPROCESS_BUDGET + 1000)
    argv, stdin_text, err = build_print_argv(CURSOR, "cursor-agent", huge)
    assert argv == []
    assert stdin_text is None
    assert "prompt too large" in err
    # The error must be actionable: it names the size and the ceiling.
    assert str(CREATEPROCESS_BUDGET - BUDGET_MARGIN) in err


def test_oversized_prompt_is_never_truncated(monkeypatch):
    """Silently sending a shortened prompt is worse than a clear failure."""
    _win(monkeypatch)
    huge = "x" * (CREATEPROCESS_BUDGET + 1000)
    argv, _, err = build_print_argv(CURSOR, "cursor-agent", huge)
    assert err
    assert argv == []


def test_stdin_agent_is_unaffected_by_prompt_size(monkeypatch):
    """A huge prompt rides stdin, so it never touches the command line."""
    _win(monkeypatch)
    huge = "x" * (CREATEPROCESS_BUDGET + 1000)
    argv, stdin_text, err = build_print_argv(CLAUDE, "claude.cmd", huge)
    assert err == ""
    assert stdin_text == huge


def test_cmd_route_has_a_smaller_budget_than_the_createprocess_route():
    """The budget belongs to the runner, not the agent — cmd.exe is stricter."""
    assert CMDLINE_BUDGET < CREATEPROCESS_BUDGET


def test_spawn_rejects_an_instruction_over_the_cmd_budget(monkeypatch):
    _win(monkeypatch)
    huge = "x" * (CMDLINE_BUDGET + 100)
    payload, err = build_spawn_cmdline(CURSOR, "cursor-agent.exe", huge)
    assert payload is None
    assert "prompt too large" in err


def test_a_prompt_between_the_two_budgets_passes_print_but_fails_spawn(monkeypatch):
    """Proves the two routes are measured separately rather than sharing one limit."""
    _win(monkeypatch)
    mid = "x" * (CMDLINE_BUDGET + 500)
    _, _, print_err = build_print_argv(CURSOR, "cursor-agent", mid)
    _, spawn_err = build_spawn_cmdline(CURSOR, "cursor-agent", mid)
    assert print_err == ""
    assert "prompt too large" in spawn_err


def test_budget_measures_the_rendered_line_not_the_raw_prompt(monkeypatch):
    """Quoting expansion is counted, not estimated.

    A prompt of quote characters renders far longer than len(prompt) once
    list2cmdline escapes it, so a raw-length check would wave it through.
    """
    _win(monkeypatch)
    quotes = '"' * (CREATEPROCESS_BUDGET - BUDGET_MARGIN - 100)
    _, _, err = build_print_argv(CURSOR, "cursor-agent", quotes)
    assert "prompt too large" in err


def test_budgets_are_not_enforced_off_windows(monkeypatch):
    """POSIX ARG_MAX is megabytes; a ceiling here would be an invented failure."""
    _posix(monkeypatch)
    huge = "x" * (CREATEPROCESS_BUDGET + 1000)
    argv, _, err = build_print_argv(CURSOR, "cursor-agent", huge)
    assert err == ""
    assert argv[-1] == huge


def test_multiline_prompt_survives_the_argv_route(monkeypatch):
    _posix(monkeypatch)
    body = "line one\nline two\nline three"
    argv, _, err = build_print_argv(CURSOR, "cursor-agent", body)
    assert err == ""
    assert argv[-1] == body


def test_backtick_heavy_prompt_survives_the_argv_route(monkeypatch):
    _posix(monkeypatch)
    body = "explain ```python\nx = 1\n``` and `y`"
    argv, _, err = build_print_argv(CURSOR, "cursor-agent", body)
    assert err == ""
    assert argv[-1] == body


# ── build_spawn_cmdline ──────────────────────────────────────────────────────

def test_spawn_strips_newlines_from_the_instruction(monkeypatch):
    """A stray \\n inside cmd.exe /k fires Enter, dropping the user in a shell."""
    _win(monkeypatch)
    payload, err = build_spawn_cmdline(CLAUDE, "claude.cmd", "do\nthis\r\nnow")
    assert err == ""
    assert "\n" not in payload
    assert "\r" not in payload


def test_interactive_form_appends_no_trailing_prompt_argument(monkeypatch):
    """An empty trailing arg reads as a blank one-shot prompt, not a TUI."""
    _win(monkeypatch)
    interactive, _ = build_spawn_cmdline(CLAUDE, "claude.cmd", "")
    assert interactive == 'cmd.exe /k ""claude.cmd""'
    assert not interactive.endswith('" ""')


def test_windows_uses_the_double_double_quote_wrapper(monkeypatch):
    """cmd.exe strips the outermost quotes when both exe and arg have spaces."""
    _win(monkeypatch)
    payload, _ = build_spawn_cmdline(
        CLAUDE, r"C:\Program Files\claude.cmd", "fix the bug")
    assert payload.startswith('cmd.exe /k ""')
    assert payload.endswith('""')


def test_posix_returns_an_argv_list_not_a_string(monkeypatch):
    _posix(monkeypatch)
    payload, err = build_spawn_cmdline(CURSOR, "cursor-agent", "go", model="m")
    assert err == ""
    assert payload == ["cursor-agent", "--model", "m", "go"]


def test_spawn_empty_exe_is_refused():
    payload, err = build_spawn_cmdline(CURSOR, "", "go")
    assert payload is None
    assert "not configured" in err


def test_both_agents_support_interactive_mode():
    assert CLAUDE.interactive == INTERACTIVE_SUPPORTED
    assert CURSOR.interactive == INTERACTIVE_SUPPORTED


# ── Detection ────────────────────────────────────────────────────────────────

def test_detect_returns_empty_string_when_absent(monkeypatch, fake_home):
    """Empty rather than the bare name, so `if exe:` is a safe install test."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    assert detect(CURSOR) == ""


def test_detect_prefers_path_over_install_dir(monkeypatch, fake_home):
    monkeypatch.setattr("helpers.agent_cli.shutil.which",
                        lambda n: "/on/path/cursor-agent" if n == "cursor-agent.cmd" else None)
    assert detect(CURSOR) == "/on/path/cursor-agent"


def test_detect_finds_cursor_in_local_bin(monkeypatch, fake_home):
    """The Windows installer writes to ~/.local/bin, not ~/.cursor/bin."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    local_bin = os.path.join(fake_home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    binary = os.path.join(local_bin, "cursor-agent.exe")
    with open(binary, "w", encoding="utf-8") as fh:
        fh.write("")
    assert detect(CURSOR) == binary


def test_detect_accepts_the_generic_agent_name_inside_the_install_dir(monkeypatch, fake_home):
    """Cursor installs the binary as both `cursor-agent` and `agent`."""
    monkeypatch.setattr("helpers.agent_cli.shutil.which", lambda _n: None)
    local_bin = os.path.join(fake_home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    binary = os.path.join(local_bin, "agent.exe")
    with open(binary, "w", encoding="utf-8") as fh:
        fh.write("")
    assert detect(CURSOR) == binary


def test_generic_agent_name_is_never_probed_against_path():
    """A bare `agent` on PATH could be any unrelated tool — do not bind to it."""
    assert not any(n in ("agent", "agent.exe", "agent.cmd")
                   for n in CURSOR.binaries)
    assert "agent.exe" in CURSOR.probe_names_for_dirs()


def test_claude_probes_cmd_before_the_bare_name():
    """npm ships .cmd shims on Windows; subprocess needs the full filename."""
    assert CLAUDE.binaries[0] == "claude.cmd"
