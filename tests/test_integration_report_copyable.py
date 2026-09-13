"""tests/test_integration_report_copyable.py — the integration report can be copied.

The report and the LLM audit result were ``state=DISABLED`` Text widgets. Tk
never gives focus to a disabled Text, so a highlighted line could not be copied
with Ctrl+C: the OUTPUT pane's defect in a second place. Both now get the OUTPUT
pane's read-only proxy, so these tests run the real dialog-building methods and
check the widget they produce, not a helper in isolation.
"""
from __future__ import annotations

from unittest import mock

import pytest

tk = pytest.importorskip("tkinter")

from controllers import update_poller as up                 # noqa: E402
from controllers.update_poller import UpdatePollerController  # noqa: E402

pytestmark = pytest.mark.tk

REPORT = (
    "## Tokensave integration check\n"
    "  ⚠  #553   Upgrade resync  OPEN  "
    "https://github.com/aovestdipaperino/tokensave/issues/553\n"
    "### Claude rules file\n"
    "  ✓  verdict: CURRENT\n"
)


def _report_text(dlg):
    stack = [dlg]
    while stack:
        widget = stack.pop()
        if isinstance(widget, tk.Text):
            return widget
        stack.extend(widget.winfo_children())
    raise AssertionError("the dialog has no Text widget")


@pytest.fixture
def poller(tk_root, mock_config):
    ctrl = UpdatePollerController(mock_config, lambda *a: None,
                                  lambda *a: None, tk_root)
    agent = mock.Mock(ok=False, label="Claude Code")
    with mock.patch("helpers.agent_cli.resolve_from", return_value=agent), \
            mock.patch.object(tk.Toplevel, "grab_set"):
        yield ctrl
    dlg = ctrl._integration_dialog
    if dlg is not None and dlg.winfo_exists():
        dlg.destroy()


@pytest.fixture
def report(poller):
    poller._show_integration_output(REPORT)
    return _report_text(poller._integration_dialog)


def test_the_report_text_is_not_disabled(report):
    """A disabled Text never takes focus, so Ctrl+C never reached it."""
    assert str(report.cget("state")) == "normal"


def test_the_report_still_shows_the_whole_report(report):
    assert report.get("1.0", "end-1c") == REPORT


@pytest.mark.parametrize("mutate", [
    lambda t: t.insert("end", "typed"),
    lambda t: t.delete("1.0", "end"),
    lambda t: t.event_generate("<<Paste>>"),
    lambda t: (t.tag_add("sel", "1.0", "2.0"), t.event_generate("<<Cut>>")),
    lambda t: t.tk.eval(t.tk.call("bind", "Text", "<KeyPress>")
                        .replace("%W", t._w).replace("%A", "z")),
])
def test_the_report_cannot_be_edited(report, tk_root, mutate):
    tk_root.clipboard_clear()
    tk_root.clipboard_append("pasted")
    mutate(report)
    assert report.get("1.0", "end-1c") == REPORT


def test_a_selection_copies_exactly_what_is_selected(report, tk_root):
    report.tag_add("sel", "4.0", "4.end")
    report.event_generate("<<Copy>>")
    assert tk_root.clipboard_get() == "  ✓  verdict: CURRENT"


def test_copy_all_copies_the_whole_report(report, tk_root):
    up._copy_all(report)
    assert tk_root.clipboard_get() == REPORT


def test_a_plain_click_on_a_link_opens_it(report):
    with mock.patch.object(up.webbrowser, "open") as opened:
        up._open_link_unless_selecting(report, "https://example.test/553")
    opened.assert_called_once_with("https://example.test/553")


def test_a_drag_that_selects_across_a_link_does_not_open_it(report):
    report.tag_add("sel", "2.0", "2.end")
    with mock.patch.object(up.webbrowser, "open") as opened:
        up._open_link_unless_selecting(report, "https://example.test/553")
    opened.assert_not_called()


def test_links_open_on_release_not_on_press(report):
    """Opening on press would fire at the start of every drag-to-select."""
    link_tags = [t for t in report.tag_names() if t.startswith("url_")]
    assert link_tags
    for tag in link_tags:
        bound = report.tk.call(report._w, "tag", "bind", tag)
        assert "<ButtonRelease-1>" in bound and "<Button-1>" not in bound, bound


def test_the_audit_result_is_copyable_too(poller, tk_root):
    poller._show_audit_result("## Integration report\nno action", None, tk_root)
    dlg = [w for w in tk_root.winfo_children()
           if isinstance(w, tk.Toplevel) and w.title() == "LLM Integration Audit"][-1]
    try:
        text = _report_text(dlg)
        assert str(text.cget("state")) == "normal"
        text.insert("end", "typed")
        assert text.get("1.0", "end-1c") == "## Integration report\nno action"
        text.tag_add("sel", "2.0", "2.end")
        text.event_generate("<<Copy>>")
        assert tk_root.clipboard_get() == "no action"
    finally:
        dlg.destroy()
