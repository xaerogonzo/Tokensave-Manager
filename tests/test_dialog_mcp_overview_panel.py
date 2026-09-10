"""tests/test_dialog_mcp_overview_panel.py — the headline cannot lie, and the badge comes from service.

Two properties, both of which fail silently and both of which have a
real-world original.

**A green headline requires all three facts.** `independent` alone goes green
on a machine where a project is served by nothing; `covered` alone goes green
while Claude Desktop answers every session from one repo; either goes green
when `~/.claude.json` could not be read at all. The MCP dialog has already
shipped a counter reading "13 bound · 13 approved · 0 still to bind" on a
machine where three of the thirteen had never been trusted — a confident
summary computed from an incomplete fact.

**The badge is read from `service`, never from `tier`.** An `EXPLICIT_INERT`
project — a correct `.mcp.json` in an untrusted folder — is served by the
fallback where one exists and by nothing where one does not. Render its badge
from the tier and it carries a green checkmark in both cases, which is false
advertising in exactly the situation the user most needs to notice.

Rendered against a stub host, following `test_dialog_mcp_desktop_panel.py`:
these methods need only `_body` and `_posture`, and building a real
`MCPConfigDialog` would drag in project discovery, `grab_set` and a
modal-bearing constructor to read a handful of labels.
"""
from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.tk

from dialogs.mcp_overview_panel import OverviewMixin
from helpers.mcp_paths import (
    LIFECYCLE_ABSENT,
    LIFECYCLE_PRESENT,
    LIFECYCLE_RETIRED,
    LIFECYCLE_RETURNED,
)
from helpers.mcp_posture import (
    READ_MALFORMED,
    READ_OK,
    TIER_EXPLICIT,
    TIER_EXPLICIT_INERT,
    TIER_MISBOUND,
    TIER_NONE,
    TIER_UNKNOWN,
    ProjectTier,
    classify_posture,
)


class _Host(OverviewMixin):
    def __init__(self, tk_root, posture, show_details=False):
        self._body = tk.Frame(tk_root)
        self._posture = posture
        self._show_details = show_details
        self.rendered = 0

    def _render(self):
        self.rendered += 1

    def texts(self) -> str:
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    out.append(child.cget("text"))
                walk(child)

        walk(self._body)
        return "\n".join(out)

    def button_labels(self) -> list:
        """Every ttk.Button label — `texts()` walks Labels only."""
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if not isinstance(child, tk.Label):
                    try:
                        text = child.cget("text")
                    except tk.TclError:
                        text = ""
                    if text:
                        out.append(str(text))
                walk(child)

        walk(self._body)
        return out

    def rows(self) -> list:
        """`(badge, colour, meaning)` per project row, as rendered."""
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                labels = [c for c in child.winfo_children()
                          if isinstance(c, tk.Label)]
                if len(labels) == 3:
                    out.append(tuple(l.cget("text") for l in labels)
                               + (labels[0].cget("fg"),))
                walk(child)

        walk(self._body)
        return out


def _p(tier, name):
    return ProjectTier(root="/" + name, display_root="/" + name, name=name,
                       tier=tier)


def _posture(*, desktop=LIFECYCLE_RETIRED, userscope=LIFECYCLE_PRESENT,
             tiers=(), desktop_read=READ_OK, userscope_read=READ_OK):
    return classify_posture(desktop_state=desktop, desktop_read=desktop_read,
                            userscope_state=userscope,
                            userscope_read=userscope_read,
                            project_tiers=tiers)


# ── the headline ─────────────────────────────────────────────────────────

def test_the_healthy_machine_says_so_plainly(tk_root):
    """The live state on the author's machine: Desktop retired, fallback
    present, twelve projects bound and four served automatically."""
    host = _Host(tk_root, _posture(
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "b")]))
    host._render_overview()

    text = host.texts()
    assert "Your projects are independent" in text
    assert "2 projects" in text


def test_a_desktop_entry_gets_the_one_project_at_a_time_headline(tk_root):
    host = _Host(tk_root, _posture(desktop=LIFECYCLE_PRESENT,
                                   tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    text = host.texts()
    assert "One project at a time" in text
    assert "independent" not in text.split("How each project")[0].replace(
        "Your projects are independent", "")
    # Names the mechanism, not just the symptom — it is why the fix is to
    # retire the entry rather than to rebind a project.
    assert "app-level" in text


def test_a_misbound_project_outranks_the_desktop_headline(tk_root):
    """Both cost independence, but only one names a specific project the user
    can go and look at."""
    host = _Host(tk_root, _posture(
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_MISBOUND, "Fortuna Lab")]))
    host._render_overview()

    text = host.texts()
    assert "wrong codebase" in text
    assert "Fortuna Lab" in text


def test_an_unserved_project_stops_the_green_headline_and_is_named(tk_root):
    """`independent` is YES here — nothing can answer from another codebase.
    Going green on that alone would be true and useless: `b` has no tokensave
    at all."""
    host = _Host(tk_root, _posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "CleanForge")]))
    host._render_overview()

    text = host.texts()
    assert "Your projects are independent" not in text
    assert "no tokensave at all" in text
    assert "CleanForge" in text


def test_an_unreadable_source_is_named_rather_than_shrugged_at(tk_root):
    """"Could not verify" with no subject is a complaint about our own tooling
    dressed up as a finding."""
    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")],
                                   userscope_read=READ_MALFORMED))
    host._render_overview()

    text = host.texts()
    assert "Your projects are independent" not in text
    assert "Posture incomplete" in text
    assert "~/.claude.json is malformed" in text


def test_the_counts_line_always_states_the_population(tk_root):
    """A table that silently emptied would make the headline vacuous. Same
    rule the geometry scan follows: print what was measured."""
    host = _Host(tk_root, _posture(tiers=[]))
    host._render_overview()

    assert "0 projects" in host.texts()


# ── the badge comes from service ─────────────────────────────────────────

def test_an_inert_project_is_green_with_a_fallback_and_red_without(tk_root):
    """The false-advertising case, asserted on the rendered badge.

    Identical tier, identical file, opposite badge — because the fact that
    decides it is machine-wide and lives nowhere in the project's own config.
    """
    served = _Host(tk_root, _posture(userscope=LIFECYCLE_PRESENT,
                                      tiers=[_p(TIER_EXPLICIT_INERT, "doom")]))
    served._render_overview()
    badges = [r for r in served.rows() if r[1] == "doom"]
    assert badges and badges[0][0] == "✓"
    assert "served automatically" in badges[0][2]

    stranded = _Host(tk_root, _posture(userscope=LIFECYCLE_RETIRED,
                                        tiers=[_p(TIER_EXPLICIT_INERT, "doom")]))
    stranded._render_overview()
    badges = [r for r in stranded.rows() if r[1] == "doom"]
    assert badges and badges[0][0] == "✗", "green badge on an unserved project"
    assert "no tokensave at all" in badges[0][2]


def test_an_unbound_project_is_not_described_as_needing_anything(tk_root):
    """The reframe. A project served by the fallback is working; calling it
    unbound invites a user to fix something that is not broken."""
    host = _Host(tk_root, _posture(tiers=[_p(TIER_NONE, "Token Save Manager")]))
    host._render_overview()

    text = host.texts()
    assert "needs binding" not in text
    assert "served automatically from the session's folder" in text


def test_a_project_we_could_not_inspect_gets_no_confident_badge(tk_root):
    host = _Host(tk_root, _posture(tiers=[_p(TIER_UNKNOWN, "mystery")]))
    host._render_overview()

    rows = [r for r in host.rows() if r[1] == "mystery"]
    assert rows and rows[0][0] == "?"
    assert "could not determine" in rows[0][2]


# ── drift, and the toggle ────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs,what", [
    ({"userscope": LIFECYCLE_RETURNED}, "this entry"),
    ({"desktop": LIFECYCLE_RETURNED}, "Claude Desktop's entry"),
])
def test_a_returned_entry_is_a_quiet_note_beside_its_switch(tk_root, kwargs,
                                                            what):
    """Worth saying — the user's choice was undone and it will happen again —
    but not worth a warning box with buttons. Nothing is broken, the switch
    already shows the true state, and the stale flag underneath is inert while
    the entry exists. The config key is never named: it is our bookkeeping,
    not a thing the user can act on."""
    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")], **kwargs))
    host._render_overview()

    text = host.texts()
    assert "you turned this off before" in text
    assert what in text
    assert "shows what is actually in effect" in text
    for key in ("mcp_user_scope_retired", "mcp_desktop_scope_retired",
                "retirement flag"):
        assert key not in text, "named internal bookkeeping: %r" % key


@pytest.mark.parametrize("state", [LIFECYCLE_RETIRED, LIFECYCLE_ABSENT,
                                   LIFECYCLE_PRESENT])
def test_no_drift_banner_without_drift(tk_root, state):
    host = _Host(tk_root, _posture(userscope=state,
                                    tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    assert "you turned this off before" not in host.texts()


def test_details_are_collapsed_by_default_and_the_toggle_re_renders(tk_root):
    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    # The toggle is the one control the collapsed view must always offer:
    # without it the five panels — and every action in them — are unreachable.
    assert any("Show details" in b for b in host.button_labels()), \
        host.button_labels()
    host._toggle_details()
    assert host._show_details is True
    assert host.rendered == 1, "the toggle must re-render, not mutate in place"


def test_the_expanded_toggle_offers_the_way_back(tk_root):
    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")]),
                 show_details=True)
    host._render_overview()

    assert any("Hide details" in b for b in host.button_labels())


# ── the plan, and the rule that it never performs anything itself ────────

def test_a_healthy_machine_gets_no_plan_at_all(tk_root):
    """Zero steps renders as nothing. Inventing an "all good, but…" section
    for the common case is how a dashboard turns back into a chore list."""
    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    assert "What this needs" not in host.texts()


def test_a_desktop_entry_is_planned_for_with_its_trade_stated(tk_root):
    host = _Host(tk_root, _posture(desktop=LIFECYCLE_PRESENT,
                                    tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    text = host.texts()
    assert "What this needs" in text
    assert "Retire Claude Desktop's tokensave entry" in text
    assert "no tokensave at all" in text, "the trade must precede the click"


def test_alternatives_are_introduced_as_a_choice(tk_root):
    """Two buttons that are individually available and together
    self-contradictory is the specific harm."""
    host = _Host(tk_root, _posture(
        userscope=LIFECYCLE_RETIRED,
        tiers=[_p(TIER_EXPLICIT, "a"), _p(TIER_NONE, "b")]))
    host._render_overview()

    text = host.texts()
    assert "alternatives, not steps" in text
    assert "Restore the machine-wide fallback" in text


def test_the_trust_step_says_no_file_can_do_it(tk_root):
    """Presenting it like the others would make a plan look complete while
    leaving the binding inert."""
    project = _p(TIER_NONE, "a")
    host = _Host(tk_root, _posture(tiers=[project]))
    steps = [s for s in __import__(
        "helpers.mcp_setup", fromlist=["x"]).plan_pin_down(
        host._posture, project) if s.kind == "trust_project"]
    host._render_step(tk.Frame(host._body), steps[0])

    assert "cannot be done by editing a file" in host.texts()


def test_running_a_step_writes_nothing_itself(tk_root, mocker):
    """Every branch of `_run_step` hands off to a writer that already owns its
    confirmation and its timestamped backup. An inline write here would be a
    second path with neither."""
    from helpers.mcp_setup import SetupStep

    host = _Host(tk_root, _posture(tiers=[_p(TIER_NONE, "a")]))
    host._bind_one = mocker.Mock()
    host._approve_one = mocker.Mock()
    apply_fix = mocker.patch("helpers.mcp_classify._apply_mcp_fix")
    approve = mocker.patch("helpers.mcp_approval.approve_project_binding")
    retire = mocker.patch("helpers.mcp_desktop.retire")

    host._run_step(SetupStep(kind="bind_project", label="", target="/a"))
    host._run_step(SetupStep(kind="approve_project", label="", target="/a"))

    host._bind_one.assert_called_once_with("/a")
    host._approve_one.assert_called_once_with("/a")
    assert apply_fix.call_count == 0
    assert approve.call_count == 0
    assert retire.call_count == 0


def test_a_gated_step_opens_details_rather_than_acting(tk_root, mocker):
    """Retiring Desktop's entry re-asks whether Desktop is running immediately
    before writing — a gate answered a minute ago is not a gate — and that
    check runs in the background scan the details panel owns. A second copy
    here would be a second copy of the one guard whose failure silently
    reverts the change."""
    from helpers.mcp_setup import SetupStep

    host = _Host(tk_root, _posture(desktop=LIFECYCLE_PRESENT,
                                    tiers=[_p(TIER_EXPLICIT, "a")]))
    retire = mocker.patch("helpers.mcp_desktop.retire")

    host._run_step(SetupStep(kind="retire_desktop", label=""))

    assert host._show_details is True
    assert host.rendered == 1
    assert retire.call_count == 0


# ── the one switch a user actually has ───────────────────────────────────

def test_the_mode_row_spells_out_both_states_not_just_the_current_one(tk_root):
    """A user who cannot see what they would be giving up is being asked to
    take the manager's word for it — and this is the decision they should
    least have to."""
    host = _Host(tk_root, _posture(desktop=LIFECYCLE_RETIRED,
                                    tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    text = host.texts()
    assert "Claude Desktop chat" in text
    assert "OFF" in text and "ON" in text
    # The cost of each, in the words that matter to a first-time reader.
    assert "no tokensave at all" in text
    assert "serves ONE project" in text
    assert "reversible either way" in text


@pytest.mark.parametrize("state,expect", [
    (LIFECYCLE_RETIRED, "Turn ON"),
    (LIFECYCLE_ABSENT, "Turn ON"),
    (LIFECYCLE_PRESENT, "Turn OFF"),
    (LIFECYCLE_RETURNED, "Turn OFF"),
])
def test_the_button_offers_the_direction_you_are_not_in(tk_root, state, expect):
    host = _Host(tk_root, _posture(desktop=state,
                                    tiers=[_p(TIER_EXPLICIT, "a")]))
    host._render_overview()

    assert any(expect in b for b in host.button_labels()), host.button_labels()


def test_toggling_routes_to_the_writer_for_that_direction(tk_root, mocker):
    """Both directions already existed with their own confirmations, backups
    and running-Desktop guards. The toggle picks between them; it is not a
    third write path."""
    retire = mocker.Mock()
    unretire = mocker.Mock()

    off = _Host(tk_root, _posture(desktop=LIFECYCLE_RETIRED,
                                   tiers=[_p(TIER_EXPLICIT, "a")]))
    off._retire_desktop, off._unretire_desktop = retire, unretire
    off._toggle_desktop_chat()
    assert unretire.call_count == 1 and retire.call_count == 0

    on = _Host(tk_root, _posture(desktop=LIFECYCLE_PRESENT,
                                  tiers=[_p(TIER_EXPLICIT, "a")]))
    on._retire_desktop, on._unretire_desktop = retire, unretire
    on._toggle_desktop_chat()
    assert retire.call_count == 1


# ── the per-project toggle goes both ways ────────────────────────────────

def test_a_bound_project_can_be_released_and_an_unbound_one_bound(tk_root):
    """Binding had a button and unbinding had none, so a project could be
    bound and never released except by editing a file by hand. That
    asymmetry is the same shape as the Desktop migration's."""
    bound = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")]))
    bound._render_overview()
    assert any("Unbind" in b for b in bound.button_labels())

    auto = _Host(tk_root, _posture(tiers=[_p(TIER_NONE, "b")]))
    auto._render_overview()
    assert any("Bind" in b for b in auto.button_labels())


@pytest.mark.parametrize("tier", [TIER_MISBOUND, TIER_UNKNOWN])
def test_no_toggle_is_offered_where_it_would_be_a_guess(tk_root, tier):
    """A misbound project needs a rebind, which the plan above names; an
    unknown one is a state we could not read, where any button is a guess."""
    host = _Host(tk_root, _posture(tiers=[_p(tier, "x")]))
    host._render_overview()

    labels = host.button_labels()
    assert not any("Unbind" in b or "Bind" in b for b in labels), labels


def test_unbinding_names_the_consequence_that_applies_to_this_machine(
        tk_root, mocker):
    """With a fallback it is a no-op in practice; without one it takes
    tokensave away entirely. Which of those is true is a machine-wide fact,
    so the confirmation states the one that applies rather than both."""
    ask = mocker.patch("dialogs.mcp_overview_panel.messagebox.askyesno",
                       return_value=False)

    served = _Host(tk_root, _posture(userscope=LIFECYCLE_PRESENT,
                                      tiers=[_p(TIER_EXPLICIT, "a")]))
    served._unbind_project("/a", "a")
    assert "It keeps working" in ask.call_args[0][1]

    stranded = _Host(tk_root, _posture(userscope=LIFECYCLE_RETIRED,
                                        tiers=[_p(TIER_EXPLICIT, "a")]))
    stranded._unbind_project("/a", "a")
    assert "no tokensave at all" in ask.call_args[0][1]


def test_unbinding_is_confirmed_before_anything_is_removed(tk_root, mocker):
    mocker.patch("dialogs.mcp_overview_panel.messagebox.askyesno",
                 return_value=False)
    remove = mocker.patch("dialogs.mcp_overview_panel.remove_mcp_entry")

    host = _Host(tk_root, _posture(tiers=[_p(TIER_EXPLICIT, "a")]))
    host._unbind_project("/a", "a")

    assert remove.call_count == 0


def test_a_bulk_bind_appears_only_when_it_beats_the_buttons_beside_it(tk_root):
    """A "do all" over one item is just a second name for the button next to
    it, and one more thing to read."""
    one = _Host(tk_root, _posture(tiers=[_p(TIER_NONE, "a")]))
    one._render_overview()
    assert not any("Bind all" in b for b in one.button_labels())

    many = _Host(tk_root, _posture(
        tiers=[_p(TIER_NONE, "a"), _p(TIER_NONE, "b"), _p(TIER_NONE, "c")]))
    many._render_overview()
    assert any("Bind all 3" in b for b in many.button_labels())


def test_the_bulk_bind_lists_what_it_will_touch_before_touching_it(
        tk_root, mocker):
    ask = mocker.patch("dialogs.mcp_overview_panel.messagebox.askyesno",
                       return_value=False)
    apply_fix = mocker.patch("dialogs.mcp_overview_panel._apply_mcp_fix")

    host = _Host(tk_root, _posture(tiers=[_p(TIER_NONE, "alpha"),
                                           _p(TIER_NONE, "beta")]))
    host._bind_all(list(host._posture.projects))

    body = ask.call_args[0][1]
    assert "alpha" in body and "beta" in body
    assert apply_fix.call_count == 0
