"""tests/test_doctor_install_nags.py — parse `tokensave doctor` install nags.

Pure-function tests (no Tk). doctor checks all 20 agent integrations and nags
about every unconfigured one; `_extract_install_nags` reduces that to the
agents actually present on the machine so the follow-up prompt isn't noise.
"""
from __future__ import annotations

from helpers.doctor_service import extract_install_nags


# Trimmed from a real `tokensave doctor` v7.8.1 run. Note doctor uses BOTH
# an em-dash and a double-hyphen as the "— run ..." separator.
_REAL_OUTPUT = [
    "Claude Code integration",
    "  ✔ MCP server registered in ~/.claude.json",
    "OpenCode integration",
    "  ! ~/.config/opencode/opencode.json not found — run "
    "`tokensave install --agent opencode` if you use OpenCode",
    "  ✘ tokensave.md does not exist — run "
    "`tokensave install --agent opencode`",
    "Codex CLI integration",
    "  ! ~/.codex/config.toml not found — run "
    "`tokensave install --agent codex` if you use Codex CLI",
    "Roo Code integration",
    "  ! cline_mcp_settings.json not found — run "
    "`tokensave install --agent roo-code` if you use Roo Code",
    "Kiro integration",
    "  ✘ Kiro tokensave agent NOT installed at ~/.kiro/agents/tokensave.json "
    "-- run `tokensave install --agent kiro`",
    "Run tokensave install to fix most issues.",
]


def _no_agents_installed(_agent_id):
    return False


def test_returns_empty_when_no_nags(mocker):
    assert extract_install_nags(["all good", "Done."]) == ([], 0)


def test_undetected_agents_collapse_to_a_count(mocker):
    """The whole point: 4 nagged agents the user doesn't have → 0 actionable."""
    mocker.patch("helpers.mcp._tokensave_agent_installed",
                 side_effect=_no_agents_installed)
    actionable, other = extract_install_nags(_REAL_OUTPUT)
    assert actionable == []
    assert other == 4          # opencode, codex, roo-code, kiro


def test_detected_agent_is_actionable(mocker):
    mocker.patch("helpers.mcp._tokensave_agent_installed",
                 side_effect=lambda a: a == "codex")
    actionable, other = extract_install_nags(_REAL_OUTPUT)
    assert actionable == ["codex"]
    assert other == 3


def test_duplicate_nags_for_one_agent_counted_once(mocker):
    """opencode is nagged twice in the real output; it must not double-count."""
    mocker.patch("helpers.mcp._tokensave_agent_installed",
                 side_effect=lambda a: a == "opencode")
    actionable, other = extract_install_nags(_REAL_OUTPUT)
    assert actionable == ["opencode"]
    assert other == 3


def test_hyphenated_agent_id_parses(mocker):
    """`roo-code` must survive the id regex."""
    mocker.patch("helpers.mcp._tokensave_agent_installed",
                 return_value=True)
    actionable, _ = extract_install_nags(
        ["! x not found - run `tokensave install --agent roo-code`"])
    assert actionable == ["roo-code"]


def test_severity_is_not_the_filter(mocker):
    """A ✘-marked agent that isn't installed stays non-actionable.

    Upstream marks some optional integrations (OpenCode, Kiro) as ✘ even
    though the text says "if you use it" — trusting severity would put
    tools the user has never installed back into the prompt.
    """
    mocker.patch("helpers.mcp._tokensave_agent_installed",
                 side_effect=_no_agents_installed)
    actionable, other = extract_install_nags([
        "  ✘ Kiro tokensave agent NOT installed -- run "
        "`tokensave install --agent kiro`",
    ])
    assert actionable == []
    assert other == 1


# ── Already-wired agents must stop nagging (permanence) ───────────────────

def test_already_wired_agent_is_not_actionable(mocker):
    """REGRESSION: an agent that is installed AND already wired must drop out.

    Copilot spans several surfaces under one --agent id (VS Code, Insiders,
    CLI, JetBrains) and doctor nags per missing surface. Filtering on
    "installed" alone re-offered a fully-wired Copilot on EVERY doctor run,
    because the nags were about surfaces that don't exist here and that
    re-running install cannot create.
    """
    mocker.patch("helpers.mcp._tokensave_agent_installed", return_value=True)
    mocker.patch("helpers.mcp._tokensave_agent_wired", return_value=True)
    actionable, other = extract_install_nags([
        "! Insiders mcp.json not found - run `tokensave install --agent copilot`",
    ])
    assert actionable == []
    assert other == 1


def test_installed_but_unwired_agent_is_actionable(mocker):
    """The genuine case must still surface — this is the whole feature."""
    mocker.patch("helpers.mcp._tokensave_agent_installed", return_value=True)
    mocker.patch("helpers.mcp._tokensave_agent_wired", return_value=False)
    actionable, other = extract_install_nags([
        "! cursor/mcp.json not found - run `tokensave install --agent cursor`",
    ])
    assert actionable == ["cursor"]
    assert other == 0


def test_wired_check_short_circuits_uninstalled_agents(mocker):
    """Not installed => not actionable regardless of wired state."""
    mocker.patch("helpers.mcp._tokensave_agent_installed", return_value=False)
    mocker.patch("helpers.mcp._tokensave_agent_wired", return_value=False)
    actionable, other = extract_install_nags([
        "! run `tokensave install --agent kiro`",
    ])
    assert actionable == []
    assert other == 1


#: A rules path as doctor prints it on Windows, built rather than written
#: so the backslashes survive every layer that handles this file.
WIN_RULES = 'C:\\Users\\u\\.claude\\rules\\tokensave.md'


# ── tokensave 7.11.0 added two sections to doctor's output ───────────────────

def test_the_711_index_scope_section_produces_no_nags(mocker):
    """Upstream #450 added an "Index scope" block; #441 added a rules-version
    line. Neither offers an agent to install, so neither may reach the picker.

    Lines taken from a real `tokensave doctor v7.11.0` run, warning forms
    included — the failing shapes matter more than the passing ones, since a
    ✔ line was never going to match anything. One carries a real Windows path,
    because that is what the line looks like on the machine this ran on.
    """
    mocker.patch("helpers.mcp._tokensave_agent_installed", return_value=True)
    mocker.patch("helpers.mcp._tokensave_agent_wired", return_value=False)
    actionable, other = extract_install_nags([
        "Index scope",
        "  ✔ Home directory is not indexed as a project",
        "  ✔ Index size is unremarkable: 78.2 MB",
        "  ✔ No stale projects in global DB",
        "  ✔ rules up to date in %s (version 95f0007b664a86ae)" % WIN_RULES,
        # The warning forms of both, which is where a loose pattern would bite.
        "  ! $HOME is initialized as a tokensave project",
        "  ! Index has grown past 5 GB: 29.9 GB",
        "  ! rules are out of date in %s" % WIN_RULES,
    ])
    assert actionable == []
    assert other == 0


def test_a_real_nag_still_surfaces_from_alongside_the_new_sections(mocker):
    """The filter must be narrow, not merely quiet.

    A test that only asserted "the new lines produce nothing" would also pass
    against a function that had stopped working entirely, so the genuine nag
    is interleaved with them here.
    """
    mocker.patch("helpers.mcp._tokensave_agent_installed", return_value=True)
    mocker.patch("helpers.mcp._tokensave_agent_wired", return_value=False)
    actionable, other = extract_install_nags([
        "Index scope",
        "  ✔ Home directory is not indexed as a project",
        "  ! cursor/mcp.json not found - run `tokensave install --agent cursor`",
        "  ✔ rules up to date in ~/.claude/rules/tokensave.md (version abc123)",
    ])
    assert actionable == ["cursor"]
    assert other == 0
