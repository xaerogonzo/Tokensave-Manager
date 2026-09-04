"""tests/test_cli_cost.py — the `cost` and `commands` envelopes.

`cost` is the machine-readable form of the panel that was reporting spend as
savings, so its envelope has to keep apart the things the old panel merged:

* **savings and spend are separate sections with separate scopes** — `gain` is
  project-scoped, `cost` is machine-global with no project filter, and the
  envelope says so in each section rather than leaving a consumer to assume.
* **`cache_read_tokens` is always null**, never a derived number. The only
  available derivation is provably zero on every payload.
* **a section that could not be read says so**, and never arrives as zeros.
  "We could not find out" and "you saved nothing" are different answers.
* **the exit code follows the headline.** Savings is what the command exists to
  report; without it the run is `EXIT_VERIFY_FAILED`, not a cheerful zero.

The fetchers are patched at their import site in `helpers.savings` (G-E), so no
test needs a tokensave binary or touches the global ledger — which matters more
than usual here, because the real command writes to it.
"""
from __future__ import annotations

import json

import pytest

import cli
from cli import EXIT_OK, EXIT_VERIFY_FAILED, main
from helpers import savings
from helpers.savings import (
    Discover,
    Gain,
    GainDay,
    Result,
    Spend,
    SpendCategory,
    SpendModel,
)


def _run(capsys, argv):
    code = main(argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture()
def project(tmp_path):
    return str(tmp_path)


@pytest.fixture()
def configured(mocker, tmp_path):
    """A resolvable `tokensave_exe`, so `cost` gets past its prerequisite."""
    mocker.patch.object(cli, "_load_manager_config",
                        lambda *a, **k: {"tokensave_exe": "tokensave.exe"})
    mocker.patch.object(cli, "_tokensave_exe_from",
                        lambda *a, **k: "tokensave.exe")


@pytest.fixture()
def metrics(mocker, configured):
    """Every fetcher succeeding, with the reference machine's real figures."""
    mocker.patch.object(savings, "fetch_gain", lambda exe, p, r, all_projects=False:
                        Result.good(Gain(range=r, project="ALL" if all_projects
                                         else p, saved_tokens=47776, calls=38,
                                         usd=0.143328,
                                         all_projects=all_projects)))
    mocker.patch.object(savings, "fetch_gain_history", lambda exe, p, r:
                        Result.good([GainDay(1787961600, 2871, 3, 0.008613)]))
    mocker.patch.object(savings, "fetch_spend", lambda exe, r, p="":
                        Result.good(Spend(
                            range=r, total_cost_usd=4132.75,
                            total_input_tokens=25231,
                            total_output_tokens=11394156,
                            by_model=(SpendModel("claude-opus-5", 4132.75,
                                                 11419387),),
                            by_category=(SpendCategory("conversation", 4132.75,
                                                       7335),),
                            cache_read_tokens=None,
                            tokens_saved=14499614)))
    mocker.patch.object(savings, "fetch_discover", lambda exe, p, s:
                        Result.good(Discover(since=s, total_turns=12780,
                                             replaceable_turns=239,
                                             buckets=(),
                                             tokens_trustworthy=False,
                                             token_evidence="turns == tokens")))


# ── shape ─────────────────────────────────────────────────────────────────

def test_cost_emits_one_envelope_with_four_sections(capsys, project, metrics):
    code, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["command"] == "cost"
    assert set(env["data"]) == {"savings", "savings_history", "spend",
                                "opportunity", "side_effect"}


def test_cost_declares_its_own_side_effect_class(capsys, project, metrics):
    """A consumer should not have to look the class up elsewhere.

    It matters here: `cost` writes to tokensave's global ledger, so a UI that
    wires it to a casually-operated control turns a display into an ingest.
    """
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert env["data"]["side_effect"] == "observe_refresh"
    assert "cost" not in cli.PURE_READ_COMMANDS


# ── savings and spend stay apart ──────────────────────────────────────────

def test_savings_is_project_scoped_and_carries_its_valuation_basis(
        capsys, project, metrics):
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    section = env["data"]["savings"]
    assert section["ok"]
    assert section["saved_tokens"] == 47776
    assert section["usd"] == pytest.approx(0.143328)
    assert section["usd_basis"] == Gain.USD_BASIS
    assert section["scope"] == project
    assert section["all_projects"] is False


def test_spend_states_that_it_is_machine_global(capsys, project, metrics):
    """The scope travels with the number, because `cost` has no project filter."""
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert env["data"]["spend"]["scope"] == "machine-global, all projects"


def test_all_flag_only_widens_savings(capsys, project, metrics):
    _, env, _ = _run(capsys, ["cost", "--project", project, "--all", "--json"])
    assert env["data"]["savings"]["all_projects"] is True
    assert env["data"]["savings"]["scope"] == "all projects"
    # Spend was machine-global to begin with; --all cannot narrow or widen it.
    assert env["data"]["spend"]["scope"] == "machine-global, all projects"


def test_the_two_dollar_figures_are_never_the_same_field(
        capsys, project, metrics):
    """The specific confusion the old panel shipped: spend rendered as savings."""
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    savings_usd = env["data"]["savings"]["usd"]
    spend_usd = env["data"]["spend"]["total_cost_usd"]
    assert savings_usd != spend_usd
    assert "usd" not in env["data"]["spend"]
    assert "total_cost_usd" not in env["data"]["savings"]


# ── nothing invented ──────────────────────────────────────────────────────

def test_cache_read_tokens_is_null_never_a_number(capsys, project, metrics):
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert env["data"]["spend"]["cache_read_tokens"] is None


def test_spend_publishes_the_basis_beside_the_implied_rate(
        capsys, project, metrics):
    """A rate without its basis is the bug, not the rate.

    This fixture is a **7.10-shaped** payload: no cache fields, so the only
    denominator available is input+output and the rate lands in the hundreds
    per million. That is not a pricing anomaly — it is three quarters of the
    tokens missing from the divisor — and the envelope has to say which
    denominator produced the number so a consumer cannot show it beside a
    7.11 figure as though they were the same statistic.
    """
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    spend = env["data"]["spend"]
    assert spend["totals_reconcile"] is True
    assert spend["implied_usd_per_mtok"] > 300
    assert spend["implied_usd_basis"] == "input_output_only"

    # And the fields that only 7.11 carries are absent, not zeroed.
    assert spend["cache_read_tokens"] is None
    assert spend["total_tokens"] is None
    assert spend["tokens_reconcile"] is None      # cannot say, ≠ disagrees
    assert spend["tokens_saved_spans_range"] is False


def test_tokens_saved_is_published_only_with_the_flag_that_qualifies_it(
        capsys, project, metrics):
    """tokensave 7.11.0 (#473) made `cost.tokens_saved` range-scoped, so it
    stopped being a figure that had to be withheld. It is still machine-global
    though — it equals `gain --all`, never project-scoped `gain` — and on an
    older binary it is still a lifetime counter wearing a range label.

    So publishing the number alone would just move the old ambiguity into the
    envelope. It ships with `tokens_saved_spans_range`, and this test fails if
    either ever appears without the other.
    """
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    spend = env["data"]["spend"]
    assert ("tokens_saved" in spend) == ("tokens_saved_spans_range" in spend)
    assert "tokens_saved" in spend

    # This fixture is 7.10-shaped, so the flag must say "do not label me".
    assert spend["tokens_saved_spans_range"] is False

    # `efficiency_ratio` is derived from it and inherits the scope, so it
    # stays unpublished — nothing consumes it and it has no qualifying flag.
    assert "efficiency_ratio" not in spend

    # The scope note still travels with the section, unchanged by any of this.
    assert "all projects" in spend["scope"]


def test_opportunity_publishes_its_suppression_evidence(
        capsys, project, metrics):
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    opportunity = env["data"]["opportunity"]
    assert opportunity["tokens_trustworthy"] is False
    assert opportunity["token_evidence"] == "turns == tokens"


def test_raw_is_opt_in(capsys, project, metrics):
    """Untouched upstream payloads help diagnosis and would bloat every reply."""
    _, plain, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert "raw" not in plain["data"]["savings"]
    _, verbose, _ = _run(capsys, ["cost", "--project", project, "--raw",
                                  "--json"])
    assert "raw" in verbose["data"]["savings"]


# ── failure is reported, not zeroed ───────────────────────────────────────

def test_an_unreadable_section_says_so_and_leaves_the_others_alone(
        capsys, project, metrics, mocker):
    mocker.patch.object(savings, "fetch_spend",
                        lambda *a, **k: Result.unavailable("ledger locked"))
    code, env, _ = _run(capsys, ["cost", "--project", project, "--json"])

    assert code == EXIT_OK                       # savings still answered
    assert env["data"]["spend"] == {"ok": False, "reason": "ledger locked"}
    assert env["data"]["savings"]["ok"] is True
    assert any("ledger locked" in w for w in env["warnings"])


def test_a_failed_section_never_arrives_as_zeros(capsys, project, metrics,
                                                 mocker):
    mocker.patch.object(savings, "fetch_spend",
                        lambda *a, **k: Result.unavailable("nope"))
    _, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert "total_cost_usd" not in env["data"]["spend"]


def test_missing_savings_is_verify_failed_not_a_cheerful_zero(
        capsys, project, metrics, mocker):
    """Savings is the headline; without it there is no answer, only silence."""
    mocker.patch.object(savings, "fetch_gain",
                        lambda *a, **k: Result.unavailable("no ledger"))
    code, env, _ = _run(capsys, ["cost", "--project", project, "--json"])

    assert code == EXIT_VERIFY_FAILED
    assert env["ok"] is False
    assert env["error"] == "no ledger"
    assert env["data"]["savings"] == {"ok": False, "reason": "no ledger"}


def test_history_failure_is_separate_from_savings_failure(
        capsys, project, metrics, mocker):
    mocker.patch.object(savings, "fetch_gain_history",
                        lambda *a, **k: Result.unavailable("no history"))
    code, env, _ = _run(capsys, ["cost", "--project", project, "--json"])
    assert code == EXIT_OK
    assert env["data"]["savings"]["ok"] is True
    assert env["data"]["savings_history"] == {"ok": False,
                                              "reason": "no history"}


def test_the_human_summary_names_the_valuation_basis(capsys, project, metrics):
    """stderr is for humans, and a bare `$` there would be the same ambiguity."""
    _, _, err = _run(capsys, ["cost", "--project", project])
    assert "47,776" in err
    assert Gain.USD_BASIS in err


# ── range plumbing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("range_", ["today", "7d", "30d", "all"])
def test_every_documented_range_is_accepted(capsys, project, metrics, range_):
    code, env, _ = _run(capsys, ["cost", "--project", project,
                                 "--range", range_, "--json"])
    assert code == EXIT_OK
    assert env["data"]["savings"]["range"] == range_


def test_an_unknown_range_is_a_usage_error(project):
    with pytest.raises(SystemExit) as exc:
        main(["cost", "--project", project, "--range", "fortnight"])
    assert exc.value.code == cli.EXIT_USAGE


# ── the `commands` vocabulary ─────────────────────────────────────────────

def test_commands_needs_no_project(capsys):
    """An editor must be able to ask what it may invoke before opening a folder."""
    code, env, _ = _run(capsys, ["commands", "--json"])
    assert code == EXIT_OK
    assert env["command"] == "commands"


def test_commands_emits_the_whole_table(capsys):
    from helpers.commands import COMMANDS
    _, env, _ = _run(capsys, ["commands", "--json"])
    assert len(env["data"]["commands"]) == len(COMMANDS)
    assert [c["action"] for c in env["data"]["commands"]] == [
        c.action for c in COMMANDS]


def test_commands_explains_each_side_effect_class(capsys):
    _, env, _ = _run(capsys, ["commands", "--json"])
    classes = env["data"]["side_effect_classes"]
    assert set(classes) == {"pure_read", "observe_refresh", "mutating"}
    assert all(text.strip() for text in classes.values())


def test_commands_marks_which_entries_need_a_project(capsys):
    _, env, _ = _run(capsys, ["commands", "--json"])
    by_action = {c["action"]: c for c in env["data"]["commands"]}
    assert by_action["commands"]["requires_project"] is False
    assert by_action["status"]["requires_project"] is True
