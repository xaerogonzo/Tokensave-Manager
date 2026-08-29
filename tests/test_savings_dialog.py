"""tests/test_savings_dialog.py — SavingsDialog (Tk-marked).

The dialog this replaced showed money *spent* in a card labelled "Value
Recouped" — $4132.75 where the real savings figure was $0.14 — and a lifetime
all-projects counter under a subtitle reading "past 7 days". So these tests are
mostly about what the dialog is now forbidden from doing:

* **The range selector must not re-run `cost`.** `cost` ingests rows into
  tokensave's global ledger. A selector wired to it turns a display control
  into repeated writes, which is why Savings and Spend have separate range
  semantics rather than one shared selector.
* **A late worker must not overwrite a newer selection.** Three sections fetch
  concurrently, so switching range mid-flight is a race that happens rather
  than one that might.
* **A second Refresh click must not start a second ingest.**
* **A failed refresh must not leave the previous snapshot looking current.**
* **`0` is never shown for "we could not find out".**

`helpers.savings` is patched at its **import site** inside the dialog module
(G-E), and the fetches are made synchronous so no test depends on thread
timing — the guards under test are about ordering, which is expressed through
the generation counter, not through wall-clock races.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from helpers.savings import (
    Discover,
    Gain,
    GainDay,
    Result,
    Spend,
    SpendCategory,
    SpendModel,
)
import dialogs.cost_viewer as cost_viewer
from dialogs.cost_viewer import SavingsDialog

pytestmark = pytest.mark.tk


# ── Canned upstream values ────────────────────────────────────────────────

def _gain(range_="30d", saved=47776, calls=38, usd=0.143328, every=False):
    return Result.good(Gain(range=range_, project="ALL" if every else "C:/p",
                            saved_tokens=saved, calls=calls, usd=usd,
                            all_projects=every))


def _history():
    return Result.good([GainDay(day=1787961600, saved_tokens=2871, calls=3,
                                usd=0.008613)])


def _spend(range_="30d", total=4132.75):
    return Result.good(Spend(
        range=range_, total_cost_usd=total,
        total_input_tokens=25231, total_output_tokens=11394156,
        by_model=(SpendModel("claude-opus-5", total, 11419387),),
        by_category=(SpendCategory("conversation", total, 7335),),
        cache_read_tokens=None, lifetime_tokens_saved=14499614))


def _discover():
    return Result.good(Discover(since="30d", total_turns=12780,
                                replaceable_turns=239, buckets=(),
                                tokens_trustworthy=False,
                                token_evidence="turns == tokens"))


class _Calls:
    """Records which tokensave subcommands the dialog actually invoked."""

    def __init__(self):
        self.gain = []
        self.history = []
        self.spend = []
        self.discover = []


@pytest.fixture
def seams(mocker):
    """Patch the fetchers at their import site, synchronously.

    `threading.Thread` is replaced with a runner that executes the target
    inline, and `_post` with a direct call, so a test observes the final state
    without driving an event loop. The generation guard is ordering logic, not
    timing logic, so this loses nothing it is meant to catch.
    """
    calls = _Calls()

    def _fake_gain(exe, project, range_, all_projects=False):
        calls.gain.append((range_, all_projects))
        return _gain(range_, every=all_projects)

    def _fake_history(exe, project, range_):
        calls.history.append(range_)
        return _history()

    def _fake_spend(exe, range_, project=""):
        calls.spend.append(range_)
        return _spend(range_)

    def _fake_discover(exe, project, since):
        calls.discover.append(since)
        return _discover()

    mocker.patch.object(cost_viewer, "fetch_gain", _fake_gain)
    mocker.patch.object(cost_viewer, "fetch_gain_history", _fake_history)
    mocker.patch.object(cost_viewer, "fetch_spend", _fake_spend)
    mocker.patch.object(cost_viewer, "fetch_discover", _fake_discover)

    class _Inline:
        def __init__(self, target=None, daemon=False, **kw):
            self._target = target

        def start(self):
            self._target()

    mocker.patch.object(cost_viewer.threading, "Thread", _Inline)
    mocker.patch.object(SavingsDialog, "_post",
                        lambda self, fn, *a: fn(*a))
    return calls


def _dialog(tk_root, mock_config, project="C:/p"):
    dlg = SavingsDialog(tk_root, mock_config, project)
    tk_root.update_idletasks()
    return dlg


def _texts(widget) -> str:
    """All label text under a widget, flattened — for asserting on render."""
    out = []
    for child in widget.winfo_children():
        try:
            out.append(str(child.cget("text")))
        except tk.TclError:
            pass
        out.append(_texts(child))
    return " ".join(out)


# ── The range selector must not touch `cost` ──────────────────────────────

def test_opening_fetches_each_source_once(tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    assert seams.gain == [("30d", False)]
    assert seams.spend == ["30d"]
    dlg.destroy()


def test_changing_range_does_not_rerun_cost(tk_root, mock_config, seams):
    """The core anti-ingest guard.

    `cost` writes to tokensave's global ledger. Flicking through four ranges
    must re-read `gain` four times and `cost` not at all — otherwise a display
    control has quietly become an ingestion trigger.
    """
    dlg = _dialog(tk_root, mock_config)
    before = len(seams.spend)

    for value in ("today", "7d", "all"):
        dlg._range.set(value)
        dlg._refresh_savings()

    assert [r for r, _ in seams.gain] == ["30d", "today", "7d", "all"]
    assert len(seams.spend) == before        # cost untouched
    dlg.destroy()


def test_all_projects_toggle_only_affects_savings(tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    before = len(seams.spend)

    dlg._all_projects.set(True)
    dlg._refresh_savings()

    assert seams.gain[-1] == ("30d", True)
    assert len(seams.spend) == before
    dlg.destroy()


def test_explicit_refresh_is_the_only_thing_that_reruns_cost(
        tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    before = len(seams.spend)
    dlg._refresh_spend()
    assert len(seams.spend) == before + 1
    dlg.destroy()


# ── Generation guard ──────────────────────────────────────────────────────

def test_a_late_worker_cannot_overwrite_a_newer_selection(
        tk_root, mock_config, seams):
    """A `30d` result landing after the user switched to `today` is dropped.

    Simulated by applying a result stamped with a superseded generation, which
    is exactly what a slow worker holds.
    """
    dlg = _dialog(tk_root, mock_config)

    dlg._range.set("today")
    dlg._refresh_savings()
    current = dlg._savings_gen

    dlg._apply_savings(current - 1, _gain("30d", saved=999_999), _history(),
                       "30d")

    rendered = _texts(dlg._savings.frame)
    assert "999,999" not in rendered
    assert "47,776" in rendered
    dlg.destroy()


def test_a_current_generation_result_is_applied(tk_root, mock_config, seams):
    """The guard rejects stale results, not all results."""
    dlg = _dialog(tk_root, mock_config)
    dlg._apply_savings(dlg._savings_gen, _gain(saved=123_456), _history(),
                       "30d")
    assert "123,456" in _texts(dlg._savings.frame)
    dlg.destroy()


def test_refreshing_spend_does_not_discard_in_flight_savings(
        tk_root, mock_config, seams):
    """The sections' staleness is independent, because the sections are.

    Caught in the real window, not here: with one shared counter, opening the
    dialog bumped the generation via `_refresh_spend` after `_refresh_savings`
    had already launched, so the savings result came back looking stale and was
    dropped — leaving Savings on "Loading…" permanently. The inline-threaded
    fixtures could not reproduce it, because they never interleaved.
    """
    dlg = _dialog(tk_root, mock_config)
    savings_gen = dlg._savings_gen

    dlg._refresh_spend()
    assert dlg._savings_gen == savings_gen        # untouched by a spend refresh

    dlg._apply_savings(savings_gen, _gain(saved=555_000), _history(), "30d")
    assert "555,000" in _texts(dlg._savings.frame)
    dlg.destroy()


def test_refreshing_savings_does_not_discard_in_flight_spend(
        tk_root, mock_config, seams):
    """The same independence, in the other direction."""
    dlg = _dialog(tk_root, mock_config)
    spend_gen = dlg._spend_gen

    dlg._range.set("today")
    dlg._refresh_savings()
    assert dlg._spend_gen == spend_gen

    dlg._apply_spend(spend_gen, _spend(total=1234.56), "30d")
    assert "$1,234.56" in _texts(dlg._spend.frame)
    dlg.destroy()


def test_stale_opportunity_result_is_dropped(tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    dlg._apply_opportunity(dlg._savings_gen - 1,
                           Result.unavailable("should not appear"))
    assert "should not appear" not in _texts(dlg._opportunity.frame)
    dlg.destroy()


# ── Spend refresh: in-flight guard and stale labelling ────────────────────

def test_second_refresh_click_does_not_start_a_second_ingest(
        tk_root, mock_config, mocker):
    """`cost` writes, so a double-click must not double-ingest."""
    calls = []
    mocker.patch.object(cost_viewer, "fetch_gain",
                        lambda *a, **k: _gain())
    mocker.patch.object(cost_viewer, "fetch_gain_history",
                        lambda *a, **k: _history())
    mocker.patch.object(cost_viewer, "fetch_discover",
                        lambda *a, **k: _discover())

    def _hanging_spend(exe, range_, project=""):
        calls.append(range_)
        return _spend(range_)

    mocker.patch.object(cost_viewer, "fetch_spend", _hanging_spend)

    # A Thread that never runs leaves the dialog mid-flight, which is the
    # state the guard exists for.
    class _NeverRuns:
        def __init__(self, target=None, daemon=False, **kw):
            pass

        def start(self):
            pass

    mocker.patch.object(cost_viewer.threading, "Thread", _NeverRuns)
    dlg = SavingsDialog(tk_root, mock_config, "C:/p")
    tk_root.update_idletasks()

    assert dlg._spend_busy is True
    dlg._refresh_spend()
    dlg._refresh_spend()
    assert calls == []                      # nothing ever started a second run
    dlg.destroy()


def test_refresh_button_re_enables_even_for_a_stale_result(
        tk_root, mock_config, seams):
    """Otherwise the only control that can refresh Spend is stranded."""
    dlg = _dialog(tk_root, mock_config)
    dlg._refresh_spend()
    dlg._apply_spend(dlg._spend_gen - 1, _spend(), "30d")

    assert dlg._spend_busy is False
    assert str(dlg._spend_btn.cget("state")) == "normal"
    dlg.destroy()


def test_failed_refresh_labels_the_previous_snapshot_stale(
        tk_root, mock_config, seams):
    """A retained snapshot must never pass as current.

    The figures stay on screen — throwing away readable data helps nobody —
    but the stamp says so.
    """
    dlg = _dialog(tk_root, mock_config)
    assert "snapshot" in dlg._spend_stamp.cget("text")

    dlg._refresh_spend()
    dlg._apply_spend(dlg._spend_gen, Result.unavailable("tokensave timed out"),
                     "30d")

    assert "stale" in dlg._spend_stamp.cget("text")
    dlg.destroy()


def test_first_refresh_failure_is_unavailable_not_zero(
        tk_root, mock_config, mocker):
    """With no prior snapshot, a failure reads as unavailable — never `$0.00`."""
    mocker.patch.object(cost_viewer, "fetch_gain", lambda *a, **k: _gain())
    mocker.patch.object(cost_viewer, "fetch_gain_history",
                        lambda *a, **k: _history())
    mocker.patch.object(cost_viewer, "fetch_discover",
                        lambda *a, **k: _discover())
    mocker.patch.object(cost_viewer, "fetch_spend",
                        lambda *a, **k: Result.unavailable("no ledger"))

    class _Inline:
        def __init__(self, target=None, daemon=False, **kw):
            self._target = target

        def start(self):
            self._target()

    mocker.patch.object(cost_viewer.threading, "Thread", _Inline)
    mocker.patch.object(SavingsDialog, "_post",
                        lambda self, fn, *a: fn(*a))

    dlg = SavingsDialog(tk_root, mock_config, "C:/p")
    tk_root.update_idletasks()

    rendered = _texts(dlg._spend.frame)
    assert "Unavailable" in rendered and "no ledger" in rendered
    assert "$0.00" not in rendered
    dlg.destroy()


# ── What the panel says ───────────────────────────────────────────────────

def test_savings_shows_the_valuation_basis(tk_root, mock_config, seams):
    """A bare `$` with no basis is how the old panel became untrustworthy."""
    dlg = _dialog(tk_root, mock_config)
    assert Gain.USD_BASIS in _texts(dlg._savings.frame)
    dlg.destroy()


def test_spend_reports_cache_reads_as_not_reported(tk_root, mock_config, seams):
    """Never a number: the only available derivation is provably zero."""
    dlg = _dialog(tk_root, mock_config)
    rendered = _texts(dlg._spend.frame)
    assert "not reported" in rendered
    dlg.destroy()


def test_spend_heading_states_its_scope_inline(tk_root, mock_config, seams):
    """The dialog opens from a project row; "all projects" must not be skimmed.

    In the heading rather than the subtitle, deliberately.
    """
    dlg = _dialog(tk_root, mock_config)
    assert "all projects" in dlg._spend._title_lbl.cget("text")
    assert "list price" in dlg._spend._subtitle_lbl.cget("text").lower()
    dlg.destroy()


def test_spend_says_its_totals_do_not_follow_from_its_tokens(
        tk_root, mock_config, seams):
    """So a user checking the arithmetic blames the right party."""
    assert "cannot be derived" in _texts(_dialog(tk_root, mock_config)._spend.frame)


def test_savings_history_column_is_labelled_utc(tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    assert "Day (UTC)" in _texts(dlg._savings.frame)
    dlg.destroy()


def test_opportunity_says_estimates_are_withheld_not_absent(
        tk_root, mock_config, seams):
    """"Withheld" and "no estimate exists" are different states."""
    dlg = _dialog(tk_root, mock_config)
    rendered = _texts(dlg._opportunity.frame)
    assert "withheld" in rendered.lower()
    assert "turns == tokens" in rendered      # the recorded evidence
    dlg.destroy()


def test_opportunity_explains_missing_tokens_even_when_evidence_is_absent(
        tk_root, mock_config, mocker):
    """Tokens are never displayed, so the reason is never omitted either.

    Found by driving the real window: at `30d` the degenerate identity does
    not hold, so the "withheld" notice vanished — leaving turns, no tokens and
    no explanation. That is the ambiguity the section exists to remove, just in
    the other direction.
    """
    trusted = Result.good(Discover(
        since="30d", total_turns=44320, replaceable_turns=1266, buckets=(),
        tokens_trustworthy=True, token_evidence=""))

    mocker.patch.object(cost_viewer, "fetch_gain", lambda *a, **k: _gain())
    mocker.patch.object(cost_viewer, "fetch_gain_history",
                        lambda *a, **k: _history())
    mocker.patch.object(cost_viewer, "fetch_spend", lambda *a, **k: _spend())
    mocker.patch.object(cost_viewer, "fetch_discover",
                        lambda *a, **k: trusted)

    class _Inline:
        def __init__(self, target=None, daemon=False, **kw):
            self._target = target

        def start(self):
            self._target()

    mocker.patch.object(cost_viewer.threading, "Thread", _Inline)
    mocker.patch.object(SavingsDialog, "_post",
                        lambda self, fn, *a: fn(*a))

    dlg = SavingsDialog(tk_root, mock_config, "C:/p")
    tk_root.update_idletasks()

    rendered = _texts(dlg._opportunity.frame)
    assert "not shown" in rendered
    assert "authoritative" in rendered
    dlg.destroy()


def test_savings_subtitle_names_the_scope_it_is_showing(
        tk_root, mock_config, seams):
    dlg = _dialog(tk_root, mock_config)
    assert "C:/p" in dlg._savings._subtitle_lbl.cget("text")

    dlg._all_projects.set(True)
    dlg._refresh_savings()
    assert "all projects" in dlg._savings._subtitle_lbl.cget("text")
    dlg.destroy()


def test_no_value_recouped_label_anywhere(tk_root, mock_config, seams):
    """The specific wrong label, kept out by name."""
    dlg = _dialog(tk_root, mock_config)
    assert "Value Recouped" not in _texts(dlg.body)
    dlg.destroy()
