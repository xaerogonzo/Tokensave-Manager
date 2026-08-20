"""tests/test_pr_body_refresh.py — refreshing the manager-owned PR region.

The whole risk here is losing someone's writing. Two ways that happens:

  * overwriting an edit made to the manager's own section between read and
    write (the classic lost-update race);
  * reverting an edit made ELSEWHERE in the body, by splicing into the stale
    copy we started from instead of the one currently on the server.

The second is the subtler one, and it is the reason the guard is scoped to
the region rather than the whole body: a whole-body comparison would refuse
the very case the feature exists to support — a human adding notes outside
the markers while a draft is generated.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from helpers import pr_body_refresh as mod
from helpers.pr_body_refresh import (
    BEGIN_MARKER,
    CONFLICT,
    END_MARKER,
    NO_PR,
    NO_REGION,
    OK,
    UNCHANGED,
    WRITE_FAILED,
    extract_region,
    plan_refresh,
    refresh_pr_body,
    replace_region,
    wrap_region,
)


def _body(region="generated summary", before="## Notes\nhand written\n",
          after="\n## Manual\nmore hand writing\n"):
    return f"{before}{BEGIN_MARKER}\n{region}\n{END_MARKER}{after}"


# ── region extraction ─────────────────────────────────────────────────────────

def test_extracts_the_region_between_markers():
    assert extract_region(_body("hello")).strip() == "hello"


@pytest.mark.parametrize("body", [
    "", None,
    "no markers at all",
    f"only a start {BEGIN_MARKER} and nothing else",
    f"only an end {END_MARKER}",
])
def test_missing_or_partial_markers_yield_none(body):
    assert extract_region(body) is None


def test_reversed_markers_are_not_an_empty_region():
    """A reversed pair is a body we do not understand. Splicing into it
    would corrupt it, so it must not read as "region present but empty"."""
    assert extract_region(f"{END_MARKER}\nstuff\n{BEGIN_MARKER}") is None


def test_replace_preserves_everything_outside_the_region():
    body = _body("old text")
    out = replace_region(body, "new text")
    assert "hand written" in out
    assert "more hand writing" in out
    assert "old text" not in out
    assert extract_region(out).strip() == "new text"


def test_replace_without_a_region_returns_none_rather_than_appending():
    assert replace_region("plain body", "new") is None


def test_wrap_round_trips():
    assert extract_region(wrap_region("content")).strip() == "content"


# ── the concurrency rule ──────────────────────────────────────────────────────

def test_untouched_region_is_safe_to_write():
    old = _body("v1")
    plan = plan_refresh(old, old, "v2")
    assert plan.status == OK
    assert plan.changed


def test_edit_to_our_own_region_aborts():
    """The lost-update case: someone rewrote the section we are about to
    replace, so replacing it would discard their edit."""
    old = _body("v1")
    latest = _body("someone edited this by hand on GitHub")
    plan = plan_refresh(old, latest, "v2")
    assert plan.status == CONFLICT
    assert "changed on GitHub" in plan.message
    assert not plan


def test_edit_outside_the_region_does_NOT_abort():
    """The case a whole-body comparison would wrongly reject.

    Someone appends a note below the managed section while the draft is
    being generated. Our region is untouched, so the refresh is safe.
    """
    old = _body("v1")
    latest = _body("v1", after="\n## Manual\nmore hand writing\nPS: added later\n")
    plan = plan_refresh(old, latest, "v2")
    assert plan.status == OK


def test_removed_region_aborts():
    plan = plan_refresh(_body("v1"), "someone deleted the whole section", "v2")
    assert plan.status == CONFLICT


def test_body_without_a_region_is_refused_with_a_remedy():
    """Refusing is right — but say what to do instead."""
    plan = plan_refresh("hand written body", "hand written body", "v2")
    assert plan.status == NO_REGION
    assert "Draft PR" in plan.message


def test_identical_content_is_reported_as_unchanged_not_written():
    plan = plan_refresh(_body("same"), _body("same"), "same")
    assert plan.status == UNCHANGED
    assert plan            # truthy: nothing wrong happened
    assert not plan.changed


def test_whitespace_only_difference_counts_as_unchanged():
    plan = plan_refresh(_body("same"), _body("same"), "  same\n\n")
    assert plan.status == UNCHANGED


# ── end to end, gh mocked at the import site ─────────────────────────────────

def _pr(body, number=12):
    return {"number": number, "body": body}


def test_splices_into_the_CURRENT_body_not_the_stale_one():
    """The subtle data-loss bug this ordering prevents.

    A note is added outside the region after we read. Writing the spliced
    STALE body would silently revert it while reporting success.
    """
    old = _body("v1")
    latest = _body("v1", after="\n## Manual\nmore hand writing\nPS: added later\n")
    written = {}

    with patch.object(mod, "get_open_pr", return_value=_pr(latest)), \
         patch.object(mod, "update_pr_body",
                      side_effect=lambda *a: (written.update(body=a[3]), (True, "ok"))[1]):
        res = refresh_pr_body("gh", ".", old, "v2")

    assert res.status == OK
    assert "PS: added later" in written["body"], \
        "an edit made outside the region was reverted"
    assert extract_region(written["body"]).strip() == "v2"


def test_conflict_never_writes():
    with patch.object(mod, "get_open_pr",
                      return_value=_pr(_body("edited by a human"))), \
         patch.object(mod, "update_pr_body") as writer:
        res = refresh_pr_body("gh", ".", _body("v1"), "v2")
    assert res.status == CONFLICT
    writer.assert_not_called()


def test_unchanged_never_writes():
    with patch.object(mod, "get_open_pr", return_value=_pr(_body("same"))), \
         patch.object(mod, "update_pr_body") as writer:
        res = refresh_pr_body("gh", ".", _body("same"), "same")
    assert res.status == UNCHANGED
    writer.assert_not_called()


def test_no_open_pr_is_reported_distinctly():
    with patch.object(mod, "get_open_pr", return_value=None):
        res = refresh_pr_body("gh", ".", _body("v1"), "v2")
    assert res.status == NO_PR
    assert not res


def test_write_failure_is_surfaced_not_swallowed():
    with patch.object(mod, "get_open_pr", return_value=_pr(_body("v1"))), \
         patch.object(mod, "update_pr_body", return_value=(False, "403")):
        res = refresh_pr_body("gh", ".", _body("v1"), "v2")
    assert res.status == WRITE_FAILED
    assert "403" in res.message
    assert not res


def test_markdown_specials_survive_the_splice():
    """The reason writes go through stdin-piped gh api, not -f body=."""
    payload = "uses `--cov-fail-under=14` & handles # and = fine"
    written = {}
    with patch.object(mod, "get_open_pr", return_value=_pr(_body("v1"))), \
         patch.object(mod, "update_pr_body",
                      side_effect=lambda *a: (written.update(body=a[3]), (True, "ok"))[1]):
        refresh_pr_body("gh", ".", _body("v1"), payload)
    assert payload in written["body"]


# ── the loop closes: drafts carry the region ─────────────────────────────

def test_draft_bodies_are_wrapped_so_they_can_be_refreshed_later():
    """Without this the NO_REGION remedy ("regenerate with Draft PR") would
    be false advice — the regenerated body would have no region either."""
    from controllers.pr_draft_ctrl import PRDraftCtrl
    captured = {}

    class _Tmp:
        name = "/tmp/pr-body-x.md"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, text):
            captured["written"] = text

    stub = object.__new__(PRDraftCtrl)
    with patch("tempfile.NamedTemporaryFile", return_value=_Tmp()), \
         patch("subprocess.run", return_value=None), \
         patch("os.unlink"):
        try:
            PRDraftCtrl._open_pr_via_gh(stub, "gh", ".", "## Summary\nbody",
                                        dlg=None)
        except Exception:
            pass          # we only care what reached the temp file

    assert "written" in captured, "the body never reached the temp file"
    assert extract_region(captured["written"]) is not None


def test_an_already_wrapped_body_is_not_double_wrapped():
    from controllers.pr_draft_ctrl import PRDraftCtrl
    captured = {}

    class _Tmp:
        name = "/tmp/pr-body-x.md"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, text):
            captured["written"] = text

    already = wrap_region("## Summary\nbody")
    stub = object.__new__(PRDraftCtrl)
    with patch("tempfile.NamedTemporaryFile", return_value=_Tmp()), \
         patch("subprocess.run", return_value=None), \
         patch("os.unlink"):
        try:
            PRDraftCtrl._open_pr_via_gh(stub, "gh", ".", already, dlg=None)
        except Exception:
            pass
    assert captured["written"].count(BEGIN_MARKER) == 1
