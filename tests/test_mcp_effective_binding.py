"""tests/test_mcp_effective_binding.py — a correct file is not a serving server.

The bug these pin: ten projects each had a `.mcp.json` binding tokensave with
`-p .`, the MCP dialog reported "bound to this project" for every one, and
`claude mcp get tokensave` reported `Scope: User config` in all ten. The
bindings had never once taken effect. `_classify_mcp_entry` reads the project
file and nothing else, so its "ok" can only ever mean "this file says the right
thing" — presenting that as "this is the server Claude Code runs" is the whole
defect.

Three tiers, cheapest first, and each tier here is tested for what it is allowed
to conclude as much as for what it detects:

  1. the file            — `_classify_mcp_entry`, covered in test_mcp_classify
  2. `~/.claude.json`     — approval + local-scope shadow, free, no subprocess
  3. `claude mcp get`     — the only authority on which server actually runs

The path-key tests are not cosmetic. Claude Code stores per-project approval
under the directory spelling a session was launched with, so `D:\\P\\Foo` and
`D:/P/Foo` are two independent records of one folder, and a reader that
exact-matches finds whichever one it happens to ask for.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers.mcp import (
    APPROVAL_AMBIGUOUS,
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    APPROVAL_UNKNOWN,
    ADVISORY_STATES,
    SCOPE_ABSENT,
    SCOPE_LOCAL,
    SCOPE_PROJECT,
    SCOPE_UNKNOWN,
    SCOPE_USER,
    EffectiveScope,
    annotate_project_binding,
    canonical_launch_dir,
    describe_effective,
    duplicate_project_keys,
    local_scope_shadow,
    local_settings_approval,
    matching_project_keys,
    mcpjson_approval,
    normalize_project_key,
    read_claude_projects,
)

#: Deliberately a drive that does not exist. `mcpjson_approval` now reads
#: `<root>/.claude/settings*.json` first, so a real path here would make
#: these tests read the developer's own machine -- which is exactly how
#: they first went green against live state instead of their fixtures.
WIN = r"Z:\Proj\Fortuna Lab"
NIX = "Z:/Proj/Fortuna Lab"
LOW = "z:/Proj/Fortuna Lab"


def _ok_info():
    """The file-level verdict this module exists to second-guess."""
    return {"state": "ok", "label": "\u2713 bound to this project",
            "issue": "", "current": {}, "proposed": {}}


# ── tier 2a: path keys ────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [WIN, NIX, LOW, NIX + "/", WIN + "\\",
                                  "Z:/Proj/./Fortuna Lab"])
def test_normalize_collapses_every_spelling_of_one_directory(path):
    assert normalize_project_key(path) == normalize_project_key(WIN)


def test_normalize_is_stable_on_both_platforms():
    """The whole point is a verdict that does not depend on the host OS.

    These keys are Windows paths even when the suite runs on Linux CI, so the
    separator and drive-letter folding cannot be delegated to `os.path`.
    """
    assert normalize_project_key(WIN) == normalize_project_key(NIX)
    assert normalize_project_key(LOW) == normalize_project_key(NIX)


def test_normalize_keeps_distinct_directories_distinct():
    assert normalize_project_key(WIN) != normalize_project_key(
        r"Z:\Proj\Fortuna Labs")
    assert normalize_project_key("") == ""


def test_matching_project_keys_finds_all_spellings():
    projects = {WIN: {}, NIX: {}, r"D:\Random Projects\Other": {}}
    got = matching_project_keys(NIX, projects)
    assert sorted(got) == sorted([WIN, NIX])


def test_duplicate_project_keys_groups_and_ignores_singletons():
    projects = {WIN: {}, NIX: {}, r"D:\Random Projects\Other": {}}
    dups = duplicate_project_keys(projects=projects)
    assert len(dups) == 1
    assert sorted(next(iter(dups.values()))) == sorted([WIN, NIX])


def test_canonical_launch_dir_reuses_an_existing_key(tmp_path):
    """Reusing a spelling Claude Code already has beats inventing a new one.

    `effective_scope` was itself minting duplicates by spawning `claude` with
    whatever spelling project discovery produced.
    """
    real = tmp_path / "Proj"
    real.mkdir()
    existing = str(real)
    projects = {existing: {}}
    got = canonical_launch_dir(str(real).replace(os.sep, "/"),
                               projects=projects)
    assert got == existing


def test_canonical_launch_dir_falls_back_to_os_form(tmp_path):
    real = tmp_path / "Proj"
    real.mkdir()
    got = canonical_launch_dir(str(real), projects={})
    assert got == os.path.normpath(os.path.abspath(str(real)))


def test_read_claude_projects_degrades_to_empty(tmp_path):
    """A missing or corrupt Claude config must read as "nothing known"."""
    assert read_claude_projects(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_claude_projects(str(bad)) == {}


def test_read_claude_projects_parses_the_map(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"projects": {WIN: {"a": 1}}}), encoding="utf-8")
    assert read_claude_projects(str(p)) == {WIN: {"a": 1}}


# ── tier 2b: approval ─────────────────────────────────────────────────────

def test_approval_pending_when_neither_list_names_the_server():
    """The live failure: both lists empty means never approved, not approved."""
    projects = {WIN: {"enabledMcpjsonServers": [],
                      "disabledMcpjsonServers": []}}
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_PENDING
    assert got.blocks_binding is True
    assert got.is_approved is False


def test_approval_approved_via_enabled_list():
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]}}
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_APPROVED
    assert got.is_approved is True
    assert got.blocks_binding is False


def test_approval_approved_via_enable_all():
    projects = {WIN: {"enableAllProjectMcpServers": True}}
    assert mcpjson_approval(WIN, projects=projects).state == APPROVAL_APPROVED


def test_approval_rejected_beats_absence_from_enabled():
    projects = {WIN: {"disabledMcpjsonServers": ["tokensave"]}}
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_REJECTED
    assert got.blocks_binding is True


def test_approval_reads_through_a_differently_spelled_key():
    """An approval recorded under one spelling must be found from the other."""
    projects = {NIX: {"enabledMcpjsonServers": ["tokensave"]}}
    assert mcpjson_approval(WIN, projects=projects).state == APPROVAL_APPROVED


def test_approval_unknown_when_claude_has_never_seen_the_project():
    """Absence of a record is not evidence, so it must not block."""
    got = mcpjson_approval(WIN, projects={r"D:\Other": {}})
    assert got.state == APPROVAL_UNKNOWN
    assert got.blocks_binding is False


def test_approval_ambiguous_when_duplicate_keys_disagree():
    """Which record applies depends on how the session was launched.

    Reported as ambiguous rather than resolved to either side: a row that said
    "bound" here would be right only by luck.
    """
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]},
                NIX: {"enabledMcpjsonServers": []}}
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_AMBIGUOUS
    assert got.blocks_binding is True
    assert len(got.keys) == 2


def test_approval_agreeing_duplicates_are_not_ambiguous():
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]},
                NIX: {"enabledMcpjsonServers": ["tokensave"]}}
    assert mcpjson_approval(WIN, projects=projects).state == APPROVAL_APPROVED


def test_approval_is_per_server_name():
    projects = {WIN: {"enabledMcpjsonServers": ["codegraph"]}}
    assert mcpjson_approval(WIN, "tokensave",
                            projects=projects).state == APPROVAL_PENDING
    assert mcpjson_approval(WIN, "codegraph",
                            projects=projects).state == APPROVAL_APPROVED


# ── tier 2c: local-scope shadow ───────────────────────────────────────────

def test_local_scope_shadow_detected():
    projects = {WIN: {"mcpServers": {"tokensave": {"command": "x"}}}}
    assert local_scope_shadow(WIN, projects=projects) == [WIN]


def test_local_scope_shadow_ignores_other_servers():
    projects = {WIN: {"mcpServers": {"codegraph": {"command": "x"}}}}
    assert local_scope_shadow(WIN, projects=projects) == []


# ── composing the tiers ───────────────────────────────────────────────────

def test_annotate_downgrades_ok_to_unapproved():
    projects = {WIN: {"enabledMcpjsonServers": []}}
    got = annotate_project_binding(_ok_info(), WIN, projects=projects)
    assert got["state"] == "project_unapproved"
    assert got["state"] in ADVISORY_STATES
    assert "approve" in got["issue"].lower()


def test_annotate_leaves_an_approved_binding_alone():
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]}}
    got = annotate_project_binding(_ok_info(), WIN, projects=projects)
    assert got["state"] == "ok"


def test_annotate_leaves_unknown_alone():
    """No Claude record must not be reported as a problem with the binding."""
    got = annotate_project_binding(_ok_info(), WIN, projects={})
    assert got["state"] == "ok"


def test_annotate_local_shadow_outranks_approval():
    """Local scope wins outright, so it is the more useful thing to say."""
    projects = {WIN: {"enabledMcpjsonServers": [],
                      "mcpServers": {"tokensave": {"command": "x"}}}}
    got = annotate_project_binding(_ok_info(), WIN, projects=projects)
    assert got["state"] == "project_local_shadow"


def test_annotate_reports_ambiguous_keys():
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]},
                NIX: {"enabledMcpjsonServers": []}}
    got = annotate_project_binding(_ok_info(), WIN, projects=projects)
    assert got["state"] == "project_key_ambiguous"


def test_annotate_only_ever_downgrades():
    """A file-level defect is what the user should fix first.

    Overwriting "your .mcp.json points at another project" with an approval
    note would bury the more serious finding.
    """
    broken = {"state": "project_mismatch", "label": "x", "issue": "y"}
    projects = {WIN: {"enabledMcpjsonServers": []}}
    assert annotate_project_binding(broken, WIN,
                                    projects=projects) is broken


def test_every_advisory_state_is_produced_by_something():
    """Guards against an ADVISORY_STATES entry that nothing can ever emit.

    A stale name here would silently stop suppressing Apply for the row it was
    meant to cover, because the dialog keys off membership in this set.
    """
    produced = set()
    for projects in (
        {WIN: {"enabledMcpjsonServers": []}},
        {WIN: {"disabledMcpjsonServers": ["tokensave"]}},
        {WIN: {"mcpServers": {"tokensave": {}}}},
        {WIN: {"enabledMcpjsonServers": ["tokensave"]},
         NIX: {"enabledMcpjsonServers": []}},
    ):
        produced.add(annotate_project_binding(
            _ok_info(), WIN, projects=projects)["state"])
    produced.add(describe_effective(
        EffectiveScope(SCOPE_USER, connected=True))[0])
    assert ADVISORY_STATES <= produced


# ── tier 3: what Claude Code reports ──────────────────────────────────────

def test_describe_effective_project_is_the_only_green_verdict():
    state, label, issue = describe_effective(EffectiveScope(SCOPE_PROJECT))
    assert state == "ok"
    assert "verified" in label
    assert issue == ""


def test_describe_effective_user_scope_is_shadowed():
    state, label, issue = describe_effective(
        EffectiveScope(SCOPE_USER, connected=True))
    assert state == "project_shadowed"
    assert "user" in label
    assert "will not change which server runs" in issue


def test_describe_effective_local_scope_is_shadowed():
    state, label, _ = describe_effective(EffectiveScope(SCOPE_LOCAL))
    assert state == "project_shadowed"
    assert "local" in label


def test_describe_effective_pending_approval_beats_scope():
    state, _label, _issue = describe_effective(
        EffectiveScope(SCOPE_USER, pending_approval=True))
    assert state == "project_unapproved"


def test_describe_effective_absent_server():
    state, label, _ = describe_effective(EffectiveScope(SCOPE_ABSENT))
    assert state == "missing"
    assert "no tokensave" in label


@pytest.mark.parametrize("got", [None, EffectiveScope(SCOPE_UNKNOWN)])
def test_describe_effective_returns_none_when_it_cannot_tell(got):
    """An unreachable `claude` is a fact about our tooling, not the binding.

    The dialog restores the file-level verdict on None; returning a "could not
    verify" badge instead would replace a true statement with a complaint.
    """
    assert describe_effective(got) is None


# ── tier 2b': .claude/settings.local.json is where approval really lives ───
#
# Measured 2026-08-25: approvals written into `~/.claude.json` were migrated
# out into `<project>/.claude/settings.local.json` within ~12 seconds, and the
# field was STRIPPED from the duplicate path keys on the way. A reader that
# only consulted `~/.claude.json` therefore reported a working Fortuna Lab as
# "approval depends on how you launch" — a warning about a project that was
# provably serving its own graph at the time.


def _local(tmp_path, payload, name="settings.local.json"):
    d = tmp_path / ".claude"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")
    return str(tmp_path)


def test_local_settings_beat_the_claude_json_keys(tmp_path):
    """The project file wins: it is what Claude Code actually honours."""
    root = _local(tmp_path, {"enabledMcpjsonServers": ["tokensave"]})
    projects = {root: {"enabledMcpjsonServers": []}}      # stale, says no
    got = mcpjson_approval(root, projects=projects)
    assert got.state == APPROVAL_APPROVED
    assert "settings.local.json" in got.detail


def test_local_settings_can_also_reject(tmp_path):
    root = _local(tmp_path, {"disabledMcpjsonServers": ["tokensave"]})
    assert mcpjson_approval(root, projects={}).state == APPROVAL_REJECTED


def test_settings_local_wins_over_settings_json(tmp_path):
    """`settings.local.json` is the machine-local override Claude Code writes."""
    _local(tmp_path, {"enabledMcpjsonServers": []}, "settings.json")
    root = _local(tmp_path, {"enabledMcpjsonServers": ["tokensave"]})
    assert mcpjson_approval(root, projects={}).state == APPROVAL_APPROVED


def test_plain_settings_json_is_consulted_when_local_is_silent(tmp_path):
    root = _local(tmp_path, {"enabledMcpjsonServers": ["tokensave"]},
                  "settings.json")
    _local(tmp_path, {"permissions": {}})                  # no opinion
    assert mcpjson_approval(root, projects={}).state == APPROVAL_APPROVED


def test_an_unreadable_settings_file_falls_through(tmp_path):
    """Corrupt JSON must not be read as a verdict."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text("{not json", encoding="utf-8")
    projects = {str(tmp_path): {"enabledMcpjsonServers": ["tokensave"]}}
    assert mcpjson_approval(str(tmp_path),
                            projects=projects).state == APPROVAL_APPROVED


def test_local_settings_approval_is_none_when_silent(tmp_path):
    root = _local(tmp_path, {"permissions": {"allow": []}})
    assert local_settings_approval(root) is None


# ── silence is not dissent ────────────────────────────────────────────────

def test_a_key_with_no_recorded_approval_is_skipped_not_counted():
    """Claude Code strips the field from duplicate keys during migration.

    Counting that silence as a dissenting vote made "the duplicates disagree"
    the normal post-migration state and produced ambiguity warnings for
    projects that were working.
    """
    projects = {WIN: {"enabledMcpjsonServers": ["tokensave"]},
                NIX: {"hasTrustDialogAccepted": True}}      # silent
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_APPROVED
    assert got.keys == (WIN,)


def test_an_empty_enabled_list_is_still_an_opinion():
    """`[]` says "nothing approved"; an absent key says nothing at all."""
    projects = {WIN: {"enabledMcpjsonServers": []}}
    assert mcpjson_approval(WIN, projects=projects).state == APPROVAL_PENDING


def test_entries_that_all_stay_silent_are_unknown_not_pending():
    """Unknown does not block; pending does. The difference matters."""
    projects = {WIN: {"hasTrustDialogAccepted": True}, NIX: {}}
    got = mcpjson_approval(WIN, projects=projects)
    assert got.state == APPROVAL_UNKNOWN
    assert got.blocks_binding is False


# ── tier 3 must not override a true row with a stale one ──────────────────
#
# `claude mcp get` reported "⏸ Pending approval" for Fortuna Lab while its
# server was demonstrably running and serving that project's own graph — it
# does not read `.claude/settings.local.json`. Left unchecked, tier 3 turned
# every correctly-approved row back into "⚠ written, not yet approved".


def test_pending_from_the_client_is_ignored_when_approval_is_known():
    got = EffectiveScope(SCOPE_USER, pending_approval=True)
    assert describe_effective(got, approval=APPROVAL_APPROVED) is None


@pytest.mark.parametrize("approval", [None, APPROVAL_PENDING,
                                      APPROVAL_UNKNOWN, APPROVAL_REJECTED])
def test_pending_is_still_reported_when_approval_is_not_established(approval):
    """Only a POSITIVE local approval suppresses it; silence does not."""
    got = EffectiveScope(SCOPE_USER, pending_approval=True)
    state, _label, _issue = describe_effective(got, approval=approval)
    assert state == "project_unapproved"


def test_a_known_approval_does_not_suppress_a_shadow_verdict():
    """`mcp get` IS authoritative about which scope wins.

    Only its approval reporting is unreliable; suppressing the shadow verdict
    too would hide the failure this whole tier exists to catch.
    """
    got = EffectiveScope(SCOPE_USER, connected=True)
    state, _label, _issue = describe_effective(got, approval=APPROVAL_APPROVED)
    assert state == "project_shadowed"


def test_a_known_approval_does_not_fake_a_verified_row():
    """Approval is not proof of serving — the row must still say what it is."""
    got = EffectiveScope(SCOPE_LOCAL)
    state, _label, _issue = describe_effective(got, approval=APPROVAL_APPROVED)
    assert state == "project_shadowed"
