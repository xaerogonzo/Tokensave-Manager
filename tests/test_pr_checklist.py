"""tests/test_pr_checklist.py — helpers/pr_checklist.py (v4.13).

Tests the sync logic for the manager-managed ``## Testing checklist``
section. Mocks ``subprocess.run`` at the import site (G-E) for the gh
CLI wrappers; never invokes real ``gh``.

V-A coverage: ``update_pr_body`` MUST use ``--input -`` (stdin-piped
JSON), NOT ``-f body=...`` (URL-encoded form). The argv test pins this
down.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from helpers.pr_checklist import (
    _MARKER,
    format_automated_section,
    get_open_pr,
    sync_checklist_section,
    sync_pr_checklist,
    update_pr_body,
)


def _proc(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── format_automated_section ─────────────────────────────────────────────

def test_format_automated_ticks_when_all_pass():
    """All tests passing → first checkbox is [x]."""
    out = format_automated_section({"passed": 12, "total": 12, "ran_at": "now"})
    assert "- [x] Test suite passes locally (12/12 passed as of now)" in out
    # CI checkbox is never auto-ticked.
    assert "- [ ] CI test-gate job passes" in out


def test_format_automated_unticked_when_failures():
    """Any failure → first checkbox stays [ ]."""
    out = format_automated_section({"passed": 10, "total": 12, "ran_at": "now"})
    assert "- [ ] Test suite passes locally (10/12 passed as of now)" in out


def test_format_automated_unticked_when_zero_total():
    """Edge: no tests ran → can't claim "all passed", unticked."""
    out = format_automated_section({"passed": 0, "total": 0, "ran_at": "never"})
    assert "- [ ] Test suite passes locally (0/0 passed as of never)" in out


# ── sync_checklist_section ───────────────────────────────────────────────

def _sample_body(passed: int = 0, total: int = 0,
                  manual_items: list = None) -> str:
    """Build a synthetic PR body matching the v4.13 template shape."""
    if manual_items is None:
        manual_items = ["Open the dialog", "Click the button"]
    manual = "\n".join(f"- [ ] {item}" for item in manual_items)
    return (
        "## Summary\nThis PR ships things.\n\n"
        "## Testing checklist\n"
        f"{_MARKER}\n"
        "### Automated (verified by `pytest -m \"not tk\"`)\n"
        f"- [ ] Test suite passes locally ({passed}/{total} passed as of unknown)\n"
        "- [ ] CI test-gate job passes on this PR\n\n"
        "### Manual (please verify before merge)\n"
        f"{manual}\n\n"
        "## Notes\nFollow-up: nothing.\n"
    )


def test_sync_refuses_without_marker():
    """Refuse to touch hand-written / third-party PR bodies."""
    body = "## Summary\nHand-written.\n\n## Testing\n- [ ] do stuff\n"
    new, changed = sync_checklist_section(body, {"passed": 5, "total": 5})
    assert changed is False
    assert new == body


def test_sync_updates_automated_subsection_only():
    """Manual subsection is byte-identical pre/post sync."""
    body = _sample_body(passed=0, total=0)
    new, changed = sync_checklist_section(
        body, {"passed": 290, "total": 290, "ran_at": "2026-05-27"})
    assert changed is True
    # Automated now ticked with new counts.
    assert "290/290 passed" in new
    assert "- [x] Test suite passes locally" in new
    # Manual section preserved verbatim.
    assert "- [ ] Open the dialog" in new
    assert "- [ ] Click the button" in new
    # Notes section untouched.
    assert "## Notes" in new
    assert "Follow-up: nothing." in new


def test_sync_idempotent_when_already_in_sync():
    """A second sync with the same results should report no change."""
    body = _sample_body(passed=0, total=0)
    results = {"passed": 290, "total": 290, "ran_at": "2026-05-27"}
    after_first, _ = sync_checklist_section(body, results)
    after_second, changed = sync_checklist_section(after_first, results)
    assert changed is False
    assert after_second == after_first


def test_sync_preserves_text_above_checklist():
    body = _sample_body(passed=0, total=0)
    new, _ = sync_checklist_section(
        body, {"passed": 1, "total": 1, "ran_at": "now"})
    assert new.startswith("## Summary\nThis PR ships things.")


# ── update_pr_body (V-A: stdin-piped JSON) ───────────────────────────────

def test_update_pr_body_uses_stdin_input_dash(tmp_path, mocker):
    """V-A: the gh api invocation MUST use ``--input -`` (stdin) so
    multi-KB markdown bodies with #/&/= specials aren't URL-encoded."""
    mock_run = mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        side_effect=[
            # First call: `gh repo view --json owner,name`
            _proc(rc=0, stdout='{"owner":{"login":"owner"},"name":"repo"}'),
            # Second call: `gh api ... -X PATCH --input -`
            _proc(rc=0, stdout=""),
        ],
    )
    ok, _msg = update_pr_body(
        "gh", str(tmp_path), 7,
        "body with & and = and # specials\n## multi-line section")
    assert ok is True
    # Second call's argv must contain --input -, NOT -f body=...
    second_call = mock_run.call_args_list[1]
    args, kwargs = second_call
    cmd = args[0]
    assert "--input" in cmd
    assert "-" in cmd
    assert "-X" in cmd
    assert "PATCH" in cmd
    # V-A: -f body= must NOT appear (would URL-encode markdown specials).
    assert not any(a.startswith("-f") for a in cmd if isinstance(a, str))
    # The body is passed via stdin (input=).
    assert kwargs.get("input") is not None
    # And it's JSON-encoded with the actual body content.
    payload = json.loads(kwargs["input"])
    assert payload["body"].startswith("body with & and =")
    assert "# multi-line section" in payload["body"]


def test_update_pr_body_fails_when_repo_resolution_fails(tmp_path, mocker):
    mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        return_value=_proc(rc=1, stderr="no remote"),
    )
    ok, msg = update_pr_body("gh", str(tmp_path), 1, "body")
    assert ok is False
    assert "owner/repo" in msg.lower()


def test_update_pr_body_surfaces_gh_api_error(tmp_path, mocker):
    """When gh api returns rc!=0 the error string is surfaced."""
    mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        side_effect=[
            _proc(rc=0, stdout='{"owner":{"login":"o"},"name":"r"}'),
            _proc(rc=1, stderr="HTTP 403: forbidden"),
        ],
    )
    ok, msg = update_pr_body("gh", str(tmp_path), 7, "body")
    assert ok is False
    assert "403" in msg or "forbidden" in msg.lower()


# ── get_open_pr ──────────────────────────────────────────────────────────

def test_get_open_pr_returns_dict_on_success(tmp_path, mocker):
    payload = '{"number": 42, "body": "x", "title": "T"}'
    mocker.patch("helpers.pr_checklist.subprocess.run",
                 return_value=_proc(rc=0, stdout=payload))
    pr = get_open_pr("gh", str(tmp_path))
    assert pr == {"number": 42, "body": "x", "title": "T"}


def test_get_open_pr_returns_none_when_no_pr(tmp_path, mocker):
    mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        return_value=_proc(rc=1, stderr="no pull requests found"),
    )
    assert get_open_pr("gh", str(tmp_path)) is None


# ── sync_pr_checklist (end-to-end) ───────────────────────────────────────

def test_sync_pr_checklist_no_pr_returns_clear_message(tmp_path, mocker):
    mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        return_value=_proc(rc=1, stderr="no PR"),
    )
    ok, msg = sync_pr_checklist("gh", str(tmp_path),
                                {"passed": 1, "total": 1, "ran_at": "now"})
    assert ok is False
    assert "no open PR" in msg.lower() or "push" in msg.lower()


def test_sync_pr_checklist_refuses_when_no_marker(tmp_path, mocker):
    pr_payload = json.dumps({
        "number": 1,
        "body": "## Summary\nManual body without manager marker.\n",
        "title": "X",
    })
    mocker.patch(
        "helpers.pr_checklist.subprocess.run",
        return_value=_proc(rc=0, stdout=pr_payload),
    )
    ok, msg = sync_pr_checklist("gh", str(tmp_path),
                                {"passed": 1, "total": 1, "ran_at": "now"})
    assert ok is False
    assert "marker" in msg.lower() or "manager-managed" in msg.lower()
