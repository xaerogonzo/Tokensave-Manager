"""tests/test_savings.py — the savings/spend parsers.

`helpers/savings.py` replaced a helper that was reporting a number off by
roughly 30,000× in the wrong direction: it scraped `tokensave cost`'s Cost
column — money *spent* — and the dialog rendered it as "Value Recouped". These
tests pin the distinctions that made that possible, so it cannot come back
quietly.

Four of them exist because a specific wrong thing was measured and would
otherwise be re-derived by anyone reading the upstream JSON:

* **Cache reads are never a number.** The export has no such field, and
  `sum(by_model.tokens) - (total_input + total_output)` is exactly `0`, so the
  obvious derivation yields a fabricated zero. `test_cache_reads_*` fails if any
  code path starts producing one.
* **`cost`'s `tokens_saved` is not savings.** It is a lifetime, all-projects
  counter identical for every range. Savings come from `gain`, which for the
  same machine reported four orders of magnitude less.
* **Unknown is not zero.** Every failure path must produce `unavailable` with a
  reason; a parser that returned zeroed structs is what let the old dialog show
  `0` for "we could not find out".
* **Streams are never merged.** stdout is pure JSON and the
  `Ingested or refreshed N …` line goes to stderr. The fixtures carry both, so a
  future upstream change that moves the line is a failing test rather than a
  corrupted parse.

Fixtures are captured verbatim from tokensave 7.10.0 — see
`tests/fixtures/savings/README.md` for provenance and the single sanitising
edit. Hand-written JSON here would defeat the point: it would keep passing
through a schema change.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from helpers.savings import (
    Discover,
    Gain,
    GainDay,
    Result,
    Spend,
    _first_json,
    _NoPayload,
    parse_discover,
    parse_gain,
    parse_gain_history,
    parse_spend,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "savings"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Stream separation and payload extraction ─────────────────────────────────


def test_captured_stdout_is_pure_json():
    """Every captured stdout parses with no preamble stripping at all.

    This is the measured shape, and the reason `_first_json` is defensive
    rather than load-bearing. If this ever fails, the preamble has moved to
    stdout and the wrappers' stream handling needs revisiting — not the
    parsers.
    """
    for name in ("gain_project_30d.json", "gain_all_30d.json",
                 "gain_history_7d.json", "cost_7d.json", "cost_all.json",
                 "discover_7d.json"):
        json.loads(fixture(name))          # raises if a preamble crept in


def test_preamble_fixture_is_on_stderr_not_stdout():
    """The `Ingested…` line belongs to stderr, and stdout stayed clean.

    Pins the corrected measurement. The original reading came from a `2>&1`
    run, which merges the streams and makes stderr look like a stdout preamble
    — the same trap this repo already hit with tokensave's `doctor`.
    """
    assert "Ingested" in fixture("cost_all.stderr")
    assert "Ingested" not in fixture("cost_all.json")
    assert parse_spend(fixture("cost_all.json")).ok


def test_first_json_skips_a_stdout_preamble_if_one_ever_appears():
    """Defensive path: synthetic, because nothing observed produces it."""
    text = fixture("synthetic_stdout_preamble.txt")
    assert text.startswith("Ingested")
    assert json.loads(_first_json(text))["range"] == "7d"
    assert parse_spend(text).ok


def test_first_json_raises_when_there_is_no_payload():
    with pytest.raises(_NoPayload):
        _first_json(fixture("synthetic_no_payload.txt"))


@pytest.mark.parametrize("parser", [parse_gain, parse_gain_history,
                                    parse_spend, parse_discover])
def test_no_payload_becomes_unavailable_not_an_exception(parser):
    """`_NoPayload` never escapes; it becomes a reason a caller can render."""
    result = parser(fixture("synthetic_no_payload.txt"))
    assert result.ok is False
    assert "no JSON payload" in result.reason


@pytest.mark.parametrize("parser", [parse_gain, parse_gain_history,
                                    parse_spend, parse_discover])
@pytest.mark.parametrize("text", ["", "   ", "{ broken", "[1, 2"])
def test_malformed_input_is_unavailable_with_a_reason(parser, text):
    result = parser(text)
    assert result.ok is False
    assert result.reason
    assert result.value is None      # never a zero-filled struct


# ── gain: the honest savings figure ──────────────────────────────────────────


def test_parse_gain_project_scoped():
    result = parse_gain(fixture("gain_project_30d.json"))
    assert result.ok
    gain: Gain = result.value
    assert gain.range == "30d"
    assert gain.saved_tokens == 47776
    assert gain.calls == 38
    assert gain.usd == pytest.approx(0.143328)
    assert gain.all_projects is False


def test_parse_gain_all_projects_differs_only_in_scope():
    """`--all` is a safe single toggle: same fields, same units, same basis.

    Only `project` changes, to the literal `"ALL"`. If upstream ever made
    `--all` mean something structurally different, a project/all switch in the
    UI would silently compare unlike quantities — so it is pinned here.
    """
    one: Gain = parse_gain(fixture("gain_project_30d.json")).value
    every: Gain = parse_gain(fixture("gain_all_30d.json")).value

    assert every.all_projects is True
    assert every.project == "ALL"
    assert every.range == one.range
    assert set(one.raw) == set(every.raw)


def test_gain_carries_its_valuation_basis():
    """A bare `$` with no basis is how the old panel became untrustworthy."""
    assert Gain.USD_BASIS == "Sonnet input rates"


def test_gain_rejects_a_payload_missing_an_identifying_field():
    """A missing `saved_tokens` fails the parse rather than defaulting to 0.

    Zero savings and unknown savings must never render the same way.
    """
    payload = json.loads(fixture("gain_project_30d.json"))
    del payload["saved_tokens"]
    result = parse_gain(json.dumps(payload))
    assert result.ok is False
    assert "saved_tokens" in result.reason


# ── gain --history: sparse, bounded, UTC ─────────────────────────────────────


def test_parse_gain_history_is_utc_midnight_aligned():
    """`day` is a UTC midnight epoch, so callers label the column UTC.

    Formatting it in local time would move rows across day boundaries for
    anyone not on UTC.
    """
    days = parse_gain_history(fixture("gain_history_7d.json")).value
    assert days
    assert all(d.day % 86400 == 0 for d in days)


def test_parse_gain_history_is_newest_first():
    days = parse_gain_history(fixture("gain_history_7d.json")).value
    assert [d.day for d in days] == sorted((d.day for d in days), reverse=True)


def test_parse_gain_history_is_sparse_not_dense():
    """Fewer rows than days in the range — absence means "no calls", not zero.

    The 7d fixture covers a span wider than its row count. A caller that
    back-filled the gaps with zeros would be inventing days.
    """
    days = parse_gain_history(fixture("gain_history_7d.json")).value
    span = (max(d.day for d in days) - min(d.day for d in days)) // 86400 + 1
    assert len(days) < span


def test_parse_gain_history_empty_series_is_ok_not_unavailable():
    """No recorded days is an answer; it is not a failure to read."""
    result = parse_gain_history("[]")
    assert result.ok
    assert result.value == []


def test_parse_gain_history_skips_rows_without_a_day():
    rows = json.loads(fixture("gain_history_7d.json"))
    rows.append({"saved_tokens": 5, "calls": 1, "usd": 0.1})   # no `day`
    days = parse_gain_history(json.dumps(rows)).value
    assert len(days) == len(rows) - 1
    assert all(isinstance(d, GainDay) for d in days)


# ── cost: spend, and the two things it must never claim ──────────────────────


def test_parse_spend_reads_the_export_verbatim():
    spend: Spend = parse_spend(fixture("cost_7d.json")).value
    raw = json.loads(fixture("cost_7d.json"))
    assert spend.total_cost_usd == raw["total_cost_usd"]
    assert spend.total_input_tokens == raw["total_input_tokens"]
    assert len(spend.by_model) == len(raw["by_model"])
    assert len(spend.by_category) == len(raw["by_category"])


def test_cache_reads_are_never_a_number():
    """The export has no cache-read field, so `Spend` reports absence.

    The guard that matters: an earlier design derived this as
    `sum(by_model.tokens) - (input + output)`, which is exactly zero on every
    payload — a fabricated `0` in the module written to stop fabricating
    numbers.
    """
    for name in ("cost_7d.json", "cost_all.json"):
        spend: Spend = parse_spend(fixture(name)).value
        assert spend.cache_read_tokens is None


def test_the_cache_read_derivation_would_have_been_exactly_zero():
    """Documents *why* the field is None, against the real payload.

    If upstream ever adds genuine cache-read accounting this difference stops
    being zero, and this test is the thing that notices.
    """
    raw = json.loads(fixture("cost_7d.json"))
    by_model_total = sum(m["tokens"] for m in raw["by_model"])
    assert by_model_total - (raw["total_input_tokens"]
                             + raw["total_output_tokens"]) == 0


def test_the_710_payloads_carry_no_cache_field():
    """Why `cache_read_tokens` is `int | None` rather than `int`.

    These captures predate tokensave 7.11.0 (#472). They are kept precisely
    because the older shape is still a supported input, and "this binary did
    not report it" must stay distinguishable from "it reported zero".
    """
    for name in ("cost_7d.json", "cost_all.json"):
        raw = json.loads(fixture(name))
        assert not [k for k in raw if "cache" in k.lower()]


def test_the_711_payloads_carry_every_priced_category():
    """The #472 fix, pinned against real captures.

    All four categories present, and `total_tokens` equal to their sum — so a
    consumer never has to add them up itself and get a different answer.
    """
    for name in ("cost_711_today.json", "cost_711_7d.json",
                 "cost_711_30d.json", "cost_711_all.json"):
        spend: Spend = parse_spend(fixture(name)).value
        assert spend.cache_read_tokens is not None
        assert spend.cache_creation_tokens is not None
        assert spend.total_tokens is not None
        assert spend.category_totals_sum() == spend.total_tokens
        assert spend.tokens_reconcile() is True
        # Cache reads dominate: that is the whole reason the old denominator
        # produced a rate in the hundreds per million.
        assert spend.cache_read_tokens > spend.total_output_tokens


def test_a_710_payload_still_reports_absence_rather_than_zero():
    spend: Spend = parse_spend(fixture("cost_7d.json")).value
    assert spend.cache_read_tokens is None
    assert spend.cache_creation_tokens is None
    assert spend.total_tokens is None
    assert spend.category_totals_sum() is None
    # "Cannot say" — a caller must not read this as "the totals disagree".
    assert spend.tokens_reconcile() is None
    assert spend.spans_range is False


def test_tokens_saved_never_collides_with_gains_own_field():
    """Two different quantities, so two different names, on both versions."""
    for name in ("cost_7d.json", "cost_711_7d.json"):
        spend: Spend = parse_spend(fixture(name)).value
        assert spend.tokens_saved > 0
        assert not hasattr(spend, "saved_tokens")   # `Gain`'s name, not ours


def test_a_710_tokens_saved_is_a_lifetime_counter():
    """Identical for every range — the #473 defect, kept as the old contract.

    `spans_range` is False here, which is what forbids a caller from labelling
    this figure with the range it was requested for.
    """
    seven: Spend = parse_spend(fixture("cost_7d.json")).value
    every: Spend = parse_spend(fixture("cost_all.json")).value
    assert seven.range != every.range
    assert seven.tokens_saved == every.tokens_saved
    assert seven.spans_range is False


def test_a_711_tokens_saved_is_scoped_to_its_range():
    """The #473 fix: four ranges, four different figures, strictly growing."""
    figures = [parse_spend(fixture("cost_711_%s.json" % r)).value
               for r in ("today", "7d", "30d", "all")]
    assert all(s.spans_range for s in figures)
    saved = [s.tokens_saved for s in figures]
    assert len(set(saved)) == 4, saved
    assert saved == sorted(saved), saved


def test_a_711_tokens_saved_equals_gain_all_and_not_project_gain():
    """The scope that survived the #473 fix, and the trap it leaves.

    `cost` has no project filter, so its savings figure matches `gain --all`
    **exactly** — measured equal on all four ranges — and does not match
    project-scoped `gain`. Showing it beside the project figure would rebuild
    the scope blur this module exists to remove, so the agreement is asserted
    against the all-projects reading and the disagreement against the other.
    """
    for range_ in ("today", "7d", "30d", "all"):
        spend: Spend = parse_spend(fixture("cost_711_%s.json" % range_)).value
        every: Gain = parse_gain(fixture("gain_711_all_%s.json" % range_)).value
        assert spend.tokens_saved == every.saved_tokens, range_

    project: Gain = parse_gain(fixture("gain_project_30d.json")).value
    thirty: Spend = parse_spend(fixture("cost_711_30d.json")).value
    assert thirty.tokens_saved != project.saved_tokens


def test_the_710_implied_rate_is_high_because_its_denominator_is_short():
    """The old M3 finding, re-read correctly.

    The three cost totals agree exactly, so the export is internally
    consistent. The implied price per million still matches no Claude rate —
    but the cause is a denominator counting only input and output while the
    dominant category is cache reads, not a cost tokensave mispriced. The
    basis says which denominator was used, so the number cannot be mistaken
    for the 7.11 one.
    """
    spend: Spend = parse_spend(fixture("cost_7d.json")).value
    assert spend.totals_reconcile()
    rate, basis = spend.implied_usd_per_mtok()
    assert rate > 300                             # observed ≈ $361.51/Mtok
    assert basis == Spend.BASIS_IO_ONLY


def test_the_711_implied_rate_lands_in_a_plausible_band():
    """Counting all four categories, the arithmetic closes.

    Measured $0.58–$0.65/Mtok across every range — a blended rate a reader can
    sanity-check, where the same machine read $272–$342 on the short
    denominator. The band is deliberately loose: this pins "explicable", not a
    price list.
    """
    for range_ in ("today", "7d", "30d", "all"):
        spend: Spend = parse_spend(fixture("cost_711_%s.json" % range_)).value
        rate, basis = spend.implied_usd_per_mtok()
        assert basis == Spend.BASIS_TOTAL
        assert 0.01 < rate < 10.0, (range_, rate)


def test_the_basis_is_never_returned_without_the_rate():
    """They travel as one value, so a caller cannot drop half of it."""
    for name in ("cost_7d.json", "cost_711_7d.json"):
        result = parse_spend(fixture(name)).value.implied_usd_per_mtok()
        assert isinstance(result, tuple) and len(result) == 2


def test_implied_rate_is_none_with_no_tokens():
    spend: Spend = parse_spend(json.dumps(
        {"range": "today", "total_cost_usd": 0.0,
         "total_input_tokens": 0, "total_output_tokens": 0})).value
    assert spend.implied_usd_per_mtok() is None


def test_a_zero_total_tokens_does_not_divide():
    """`total_tokens: 0` is present-and-zero, and must not become a ZeroDivision
    nor silently fall through to the input+output denominator."""
    spend: Spend = parse_spend(json.dumps(
        {"range": "today", "total_cost_usd": 1.0, "total_input_tokens": 0,
         "total_output_tokens": 0, "total_cache_read_tokens": 0,
         "total_cache_creation_tokens": 0, "total_tokens": 0})).value
    assert spend.total_tokens == 0                # present, not absent
    assert spend.implied_usd_per_mtok() is None


def test_tokens_reconcile_is_exact_not_rounded():
    """Tokens are integers. A one-token disagreement is a disagreement."""
    raw = json.loads(fixture("cost_711_7d.json"))
    raw["by_model"][0]["tokens"] += 1
    assert parse_spend(json.dumps(raw)).value.tokens_reconcile() is False


def test_spend_tolerates_the_synthetic_model_row():
    """`<synthetic>` rows appear upstream with zero cost and zero tokens."""
    spend: Spend = parse_spend(fixture("cost_7d.json")).value
    synthetic = [m for m in spend.by_model if m.model == "<synthetic>"]
    assert synthetic and synthetic[0].tokens == 0


def test_spend_rejects_a_payload_missing_its_total():
    payload = json.loads(fixture("cost_7d.json"))
    del payload["total_cost_usd"]
    result = parse_spend(json.dumps(payload))
    assert result.ok is False
    assert "total_cost_usd" in result.reason


# ── discover: turns authoritative, tokens quarantined ────────────────────────


def test_discover_turn_counts_are_read():
    discover: Discover = parse_discover(fixture("discover_7d.json")).value
    assert discover.since == "7d"
    assert discover.total_turns == 12780
    assert discover.replaceable_turns == 239
    assert {b.bucket for b in discover.buckets} == {"read", "grep"}


def test_discover_tokens_are_suppressed_with_recorded_evidence():
    """Suppression is evidence-recorded, not a heuristic.

    The observed payload reports recoverable tokens exactly equal to
    replaceable turns. What is stored is *that observation*, so an upstream fix
    that legitimately produced one token per turn would not stay hidden.
    """
    discover: Discover = parse_discover(fixture("discover_7d.json")).value
    assert discover.tokens_trustworthy is False
    assert "replaceable_turns" in discover.token_evidence
    assert "239" in discover.token_evidence


def test_discover_buckets_expose_no_token_columns():
    """Turn counts reach the typed surface; token estimates stay in `raw`."""
    discover: Discover = parse_discover(fixture("discover_7d.json")).value
    bucket = discover.buckets[0]
    assert not [f for f in bucket.__dataclass_fields__ if "token" in f]
    assert "recoverable_input_tokens" in discover.raw["buckets"][0]


def test_discover_trusts_tokens_when_the_identity_breaks():
    """A payload whose accounting looks real is not suppressed.

    Guards the failure mode of over-suppression: hard-coding "≈ equal means
    untrusted" would hide a fixed upstream forever.
    """
    raw = json.loads(fixture("discover_7d.json"))
    raw["total_recoverable_input_tokens"] = 41_500
    for i, bucket in enumerate(raw["buckets"]):
        bucket["recoverable_input_tokens"] = 20_000 + i

    discover: Discover = parse_discover(json.dumps(raw)).value
    assert discover.tokens_trustworthy is True
    assert discover.token_evidence == ""


def test_discover_with_no_buckets_is_ok():
    result = parse_discover(json.dumps(
        {"since": "today", "total_turns": 0, "replaceable_turns": 0,
         "buckets": []}))
    assert result.ok
    assert result.value.buckets == ()


# ── Result contract ──────────────────────────────────────────────────────────


def test_result_is_falsy_when_unavailable():
    """So `if not result:` reads correctly at every call site."""
    assert not Result.unavailable("nope")
    assert Result.good(1)


def test_unavailable_carries_no_value():
    result = Result.unavailable("because")
    assert result.value is None
    assert result.reason == "because"
