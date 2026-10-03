"""tests/test_tokensave_versions.py -- observed versions and the release interval.

The behaviour that matters: a baseline that is missing, damaged or stale is
reported as such, never as "nothing was skipped".
"""
from __future__ import annotations

import pytest

from helpers import tokensave_versions as tv


def _rel(tag, date="2026-09-01", body="notes"):
    return {"tag_name": tag, "published_at": date + "T00:00:00Z", "body": body}


class TestVersionKey:
    def test_prefix_and_plain_are_equal(self):
        assert tv.version_key("v7.14.1") == tv.version_key("7.14.1")

    def test_four_part_boundaries(self):
        k = tv.version_key
        assert k("1.0.4") < k("1.0.4.1") < k("1.0.5")
        assert k("1.0.4.1") == k("1.0.4.1")

    @pytest.mark.parametrize("bad", ["banana", "", None, 7, "7.14", {}, "1.2.3.4.5"])
    def test_garbage_is_none(self, bad):
        assert tv.version_key(bad) is None

    def test_prerelease_suffix_ignored(self):
        assert tv.version_key("v6.1.1-rc.1") == (6, 1, 1, 0)

    def test_canonical_drops_prefix(self):
        assert tv.canonical("v7.14.1") == "7.14.1"
        assert tv.canonical("1.0.4.1") == "1.0.4.1"
        assert tv.canonical("junk") is None


class TestObserve:
    def test_first_run_seeds_without_a_transition(self):
        raw: dict = {}
        assert tv.observe(raw, "7.14.1") is True
        assert raw == {tv.KEY_LAST_SEEN: "7.14.1"}

    def test_then_a_newer_version_records_one_transition(self):
        raw: dict = {}
        tv.observe(raw, "7.14.1")
        assert tv.observe(raw, "v7.15.0", now="T1") is True
        assert raw[tv.KEY_LAST_SEEN] == "7.15.0"
        assert raw[tv.KEY_TRANSITION] == {
            "from": "7.14.1", "to": "7.15.0", "observed_at": "T1"}

    def test_same_version_changes_nothing(self):
        raw = {tv.KEY_LAST_SEEN: "7.14.1"}
        assert tv.observe(raw, "7.14.1") is False
        assert tv.observe(raw, "v7.14.1") is False
        assert raw == {tv.KEY_LAST_SEEN: "7.14.1"}

    def test_downgrade_moves_last_seen_only(self):
        raw = {tv.KEY_LAST_SEEN: "7.14.1",
               tv.KEY_TRANSITION: {"from": "7.13.0", "to": "7.14.1",
                                   "observed_at": "T0"}}
        assert tv.observe(raw, "7.13.0") is True
        assert raw[tv.KEY_LAST_SEEN] == "7.13.0"
        assert raw[tv.KEY_TRANSITION]["to"] == "7.14.1"
        assert tv.span_for(raw, "7.13.0").state == tv.STALE

    @pytest.mark.parametrize("bad", [None, "", "banana", "7.14"])
    def test_unparseable_installed_mutates_nothing(self, bad):
        raw = {tv.KEY_LAST_SEEN: "7.13.0"}
        assert tv.observe(raw, bad) is False
        assert raw == {tv.KEY_LAST_SEEN: "7.13.0"}
        empty: dict = {}
        assert tv.observe(empty, bad) is False
        assert empty == {}

    @pytest.mark.parametrize("damaged", ["banana", {}, 7, None, ["7.13.0"]])
    def test_malformed_last_seen_is_left_alone(self, damaged):
        raw = {tv.KEY_LAST_SEEN: damaged}
        assert tv.observe(raw, "7.14.1") is False
        assert raw == {tv.KEY_LAST_SEEN: damaged}
        assert tv.span_for(raw, "7.14.1").state == tv.UNKNOWN


class TestSpanFor:
    def _raw(self, frm, to):
        return {tv.KEY_TRANSITION: {"from": frm, "to": to, "observed_at": "T"}}

    def test_recorded(self):
        s = tv.span_for(self._raw("7.13.0", "7.14.1"), "7.14.1")
        assert (s.state, s.from_, s.to, s.observed_at) == (
            tv.RECORDED, "7.13.0", "7.14.1", "T")

    def test_record_for_another_version_is_stale(self):
        s = tv.span_for(self._raw("7.13.0", "7.14.0"), "7.14.1")
        assert s.state == tv.STALE and s.to == "7.14.0"

    def test_missing_record_is_unknown(self):
        assert tv.span_for({}, "7.14.1").state == tv.UNKNOWN

    @pytest.mark.parametrize("rec", [
        "7.13.0 -> 7.14.1",
        {"from": "banana", "to": "7.14.1", "observed_at": "T"},
        {"from": "7.13.0", "to": "7.14.1"},
        {"from": "7.14.1", "to": "7.13.0", "observed_at": "T"},
        {"from": "7.14.1", "to": "7.14.1", "observed_at": "T"},
    ])
    def test_malformed_or_contradictory_record_is_unknown(self, rec):
        assert tv.span_for({tv.KEY_TRANSITION: rec}, "7.14.1").state == tv.UNKNOWN

    def test_backwards_record_never_yields_an_interval(self):
        s = tv.span_for(self._raw("7.15.0", "7.16.0"), "7.14.0")
        assert s.state == tv.STALE
        assert tv.version_key(s.from_) < tv.version_key(s.to)

    def test_unparseable_installed_is_unknown(self):
        assert tv.span_for(self._raw("7.13.0", "7.14.1"), None).state == tv.UNKNOWN

    def test_override_is_used_and_never_recorded(self):
        raw: dict = {}
        s = tv.span_for(raw, "7.14.1", override="v7.13.0")
        assert (s.state, s.from_, s.to) == (tv.OVERRIDDEN, "7.13.0", "7.14.1")
        assert raw == {}

    @pytest.mark.parametrize("override", ["7.14.1", "7.15.0", "banana"])
    def test_override_not_older_than_installed_is_unknown(self, override):
        assert tv.span_for({}, "7.14.1", override=override).state == tv.UNKNOWN

    def test_override_beats_a_recorded_transition(self):
        s = tv.span_for(self._raw("7.14.0", "7.14.1"), "7.14.1", override="7.12.0")
        assert (s.state, s.from_) == (tv.OVERRIDDEN, "7.12.0")


class TestReleasesInSpan:
    def test_the_motivating_case_marks_the_skipped_version(self):
        out = tv.releases_in_span(
            [_rel("v7.14.1"), _rel("v7.14.0"), _rel("v7.13.0")], "7.13.0", "7.14.1")
        assert [(r.version, r.status) for r in out.releases] == [
            ("7.14.0", tv.SKIPPED), ("7.14.1", tv.INSTALLED)]
        assert out.from_release_found is True

    def test_several_skipped_versions_and_future_releases_excluded(self):
        rels = [_rel(t) for t in (
            "v7.16.0", "v7.15.1", "v7.15.0", "v7.14.1", "v7.14.0", "v7.13.0", "v7.12.0")]
        out = tv.releases_in_span(rels, "7.13.0", "7.15.1")
        assert [(r.version, r.status) for r in out.releases] == [
            ("7.14.0", tv.SKIPPED), ("7.14.1", tv.SKIPPED),
            ("7.15.0", tv.SKIPPED), ("7.15.1", tv.INSTALLED)]

    def test_order_comes_from_versions_not_the_api(self):
        a = tv.releases_in_span([_rel("v7.14.0"), _rel("v7.14.1")], "7.13.0", "7.14.1")
        b = tv.releases_in_span([_rel("v7.14.1"), _rel("v7.14.0")], "7.13.0", "7.14.1")
        assert a.releases == b.releases

    def test_from_itself_is_context_not_covered(self):
        out = tv.releases_in_span([_rel("v7.13.0"), _rel("v7.14.1")], "7.13.0", "7.14.1")
        assert [r.version for r in out.releases] == ["7.14.1"]

    def test_missing_from_release_is_reported_not_hidden(self):
        out = tv.releases_in_span([_rel("v7.14.0"), _rel("v7.14.1")], "7.13.0", "7.14.1")
        assert out.from_release_found is False
        assert len(out.releases) == 2

    def test_malformed_and_duplicate_tags_follow_the_old_selection_rules(self):
        rels = [_rel("nightly"), _rel(""), {"tag_name": None}, "junk",
                _rel("v7.14.0", body="first"), _rel("v7.14.0", body="dup"),
                _rel("v7.14.1")]
        out = tv.releases_in_span(rels, "7.13.0", "7.14.1")
        assert [(r.version, r.body) for r in out.releases] == [
            ("7.14.0", "first"), ("7.14.1", "notes")]

    def test_four_part_hotfix_inside_the_interval(self):
        out = tv.releases_in_span(
            [_rel("v1.0.4.1"), _rel("v1.0.5")], "1.0.4", "1.0.5")
        assert [r.version for r in out.releases] == ["1.0.4.1", "1.0.5"]

    def test_empty_interval_is_empty_not_an_error(self):
        out = tv.releases_in_span([_rel("v7.13.0")], "7.13.0", "7.13.1")
        assert out.releases == ()
