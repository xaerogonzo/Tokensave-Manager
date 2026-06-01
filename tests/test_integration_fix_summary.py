"""tests/test_integration_fix_summary.py — Apply-Fixes change summary.

`UpdatePollerController._summarize_fix_output` is a pure static method that
parses the integration-check `--fix` script output into a human-readable
"what changed" message. No Tk root needed.
"""
from __future__ import annotations

from controllers.update_poller import UpdatePollerController

_summarize = UpdatePollerController._summarize_fix_output


def test_no_changes_when_no_lifecycle_lines():
    out = (
        "## Tokensave integration check — 2026-05-30\n"
        "  [--fix mode: lifecycle mutations will be applied]\n"
        "### Upstream-issue docs (docs/upstream-issues/)\n"
        "  ✓  foo.md   STATUS: MOOT\n"
        "### Next steps\n  Free checks above are complete.\n"
    )
    summary = _summarize(out)
    assert "Nothing to apply" in summary
    assert "unchanged" in summary


def test_summary_counts_created_stub():
    out = (
        "### Upstream issue lifecycle (--fix applied)\n"
        "  📄  Created docs/upstream-issues/issue-101.md stub\n"
        "  (commit the lifecycle changes to keep the repo clean)\n"
    )
    summary = _summarize(out)
    assert summary.startswith("✓  Applied 1 change")
    assert "Created 1 stub" in summary
    assert "issue-101.md" in summary


def test_summary_counts_archived():
    out = (
        "### Upstream issue lifecycle (--fix applied)\n"
        "  📦  Archived #87 (issue-87.md) → archived/\n"
    )
    summary = _summarize(out)
    assert summary.startswith("✓  Applied 1 change")
    assert "Archived 1 resolved issue" in summary
    assert "#87" in summary


def test_summary_counts_mixed_actions():
    out = (
        "### Upstream issue lifecycle (--fix applied)\n"
        "  📄  Created docs/upstream-issues/issue-101.md stub\n"
        "  📄  Created docs/upstream-issues/issue-102.md stub\n"
        "  📦  Archived #87 (issue-87.md) → archived/\n"
    )
    summary = _summarize(out)
    assert summary.startswith("✓  Applied 3 changes")
    assert "Created 2 stubs" in summary
    assert "Archived 1 resolved issue" in summary


def test_summary_empty_output_is_no_change():
    assert "Nothing to apply" in _summarize("")
