"""tests/test_integration_upgrade_span.py -- the Upgrade span section of the checker.

The failure this section exists to prevent: after an upgrade the old report
printed "No newer releases found", which read as "nothing to review" while a
whole release (v7.14.0) had been skipped.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from helpers import tokensave_versions as tv

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "check_tokensave_integration.py"


def _load():
    spec = importlib.util.spec_from_file_location("_check_tokensave_span", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _rel(tag, body="notes", date="2026-09-01"):
    return {"tag_name": tag, "published_at": date + "T00:00:00Z", "body": body}


def _render(mod, span, releases=(), complete=True, err=None, available=None):
    found = tv.releases_in_span(
        releases, span.from_ or "0.0.0", span.to or "0.0.0")
    return mod._format_upgrade_span(
        span, found, complete, err, "owner/repo", available)


class TestFormat:
    def test_recorded_lists_the_skipped_version_with_notes(self):
        mod = _load()
        span = tv.Span(tv.RECORDED, "7.13.0", "7.14.1", "2026-10-02T23:14:52Z")
        out = _render(mod, span, [_rel("v7.14.1", "B"), _rel("v7.14.0", "A"),
                                  _rel("v7.13.0")])
        assert "State:    RECORDED" in out
        assert "From:     v7.13.0" in out
        assert "observed 2026-10-02T23:14:52Z" in out
        assert "Coverage: v7.14.0 [skipped], v7.14.1 [installed]" in out
        assert "#### v7.14.0 [skipped]" in out and "#### v7.14.1 [installed]" in out
        assert out.index("v7.14.0 [skipped] —") < out.index("v7.14.1 [installed] —")
        assert "No newer releases" not in out

    @pytest.mark.parametrize("span", [
        tv.Span(tv.UNKNOWN),
        tv.Span(tv.STALE, "7.13.0", "7.14.0", "T"),
    ])
    def test_unknown_and_stale_never_read_as_clean(self, span):
        out = _render(_load(), span)
        assert f"State:    {span.state}" in out
        assert "cannot tell whether releases were skipped" in out
        assert "--from <version>" in out
        assert "No newer releases" not in out
        assert "[skipped]" not in out

    def test_truncated_notes_say_so_and_give_the_command(self):
        mod = _load()
        span = tv.Span(tv.OVERRIDDEN, "7.13.0", "7.14.1")
        out = _render(mod, span, [_rel("v7.14.1", "x" * 5000)])
        assert "Notes: truncated to 1500 chars" in out
        assert "gh release view v7.14.1 --repo owner/repo" in out
        assert "x" * 1500 in out and "x" * 1501 not in out

    def test_short_notes_are_not_marked_truncated(self):
        out = _render(_load(), tv.Span(tv.OVERRIDDEN, "7.13.0", "7.14.1"),
                      [_rel("v7.14.1", "short")])
        assert "truncated" not in out

    def test_incomplete_list_is_partial_and_says_not_to_trust_it(self):
        out = _render(_load(), tv.Span(tv.RECORDED, "7.0.0", "7.14.1", "T"),
                      [_rel("v7.14.1")], complete=False, err="stopped after 10 pages")
        assert "State:    PARTIAL" in out
        assert "Basis:    RECORDED" in out
        assert "NOT proven complete (stopped after 10 pages)" in out

    def test_complete_list_without_from_release_is_not_called_partial(self):
        out = _render(_load(), tv.Span(tv.RECORDED, "7.13.0", "7.14.1", "T"),
                      [_rel("v7.14.0"), _rel("v7.14.1")])
        assert "State:    RECORDED" in out
        assert "PARTIAL" not in out
        assert "no release entry for v7.13.0" in out

    def test_newer_release_is_a_separate_line_outside_the_span(self):
        out = _render(_load(), tv.Span(tv.RECORDED, "7.13.0", "7.14.1", "T"),
                      [_rel("v7.14.1"), _rel("v7.15.0")], available="7.15.0")
        assert "Newer release available: v7.15.0" in out
        assert "Coverage: v7.14.1 [installed]" in out
        assert "v7.15.0 [" not in out


class _Pages:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, gh_exe, endpoint, body=None):
        self.calls.append(endpoint)
        page = int(endpoint.rsplit("page=", 1)[1])
        if page > len(self.pages):
            return [], None
        return self.pages[page - 1], None


class TestPagination:
    def test_pages_until_the_from_boundary_is_reached(self, monkeypatch):
        mod = _load()
        page1 = [_rel(f"v8.{i}.0") for i in range(100, 0, -1)]
        page2 = [_rel("v8.0.0"), _rel("v7.1.0"), _rel("v7.0.0")]
        fake = _Pages([page1, page2])
        monkeypatch.setattr(mod, "_fetch_gh_json", fake)
        got, complete, err = mod._fetch_releases_back_to("o/r", "gh", "7.0.0")
        assert complete is True and err is None
        assert len(fake.calls) == 2
        found = tv.releases_in_span(got, "7.0.0", "8.100.0")
        assert len(found.releases) == 102 and found.from_release_found

    def test_a_fifty_one_release_span_is_not_cut_at_fifty(self, monkeypatch):
        mod = _load()
        rels = [_rel(f"v7.{i}.0") for i in range(60, 0, -1)]
        monkeypatch.setattr(mod, "_fetch_gh_json", _Pages([rels]))
        got, complete, _ = mod._fetch_releases_back_to("o/r", "gh", "7.9.0")
        found = tv.releases_in_span(got, "7.9.0", "7.60.0")
        assert complete and len(found.releases) == 51

    def test_page_cap_is_partial_not_complete(self, monkeypatch):
        mod = _load()
        full = [_rel(f"v9.{i}.0") for i in range(100)]
        fake = _Pages([full] * 50)
        monkeypatch.setattr(mod, "_fetch_gh_json", fake)
        _, complete, err = mod._fetch_releases_back_to("o/r", "gh", "1.0.0")
        assert complete is False and "10 pages" in err
        assert len(fake.calls) == 10

    def test_failed_page_is_partial_with_the_error(self, monkeypatch):
        mod = _load()

        def boom(gh_exe, endpoint, body=None):
            return None, "gh exited 1: rate limited"
        monkeypatch.setattr(mod, "_fetch_gh_json", boom)
        got, complete, err = mod._fetch_releases_back_to("o/r", "gh", "7.0.0")
        assert got == [] and complete is False and "rate limited" in err


class TestParseFrom:
    def test_from_is_normalised(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(sys, "argv", ["x", "--from", "v7.13.0"])
        assert mod._parse_args()["from"] == "7.13.0"

    def test_garbage_from_fails_loudly(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(sys, "argv", ["x", "--from", "garbage"])
        with pytest.raises(SystemExit) as exc:
            mod._parse_args()
        assert "not a version" in str(exc.value)
