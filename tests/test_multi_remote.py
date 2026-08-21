"""tests/test_multi_remote.py — pushing to several remotes without lying.

Three failure modes drive almost everything here, and none of them raises:

* looping ``push -u`` over N remotes silently repoints the branch's upstream
  at whichever ran last;
* bare ``--force-with-lease`` leases against a local tracking ref that a
  rarely-fetched remote may not even have, so the protection the confirmation
  dialog promises quietly evaporates;
* reporting ``origin ✓`` when one of that remote's two push URLs was rejected.

Every git call goes through an injected ``runner``, so the argv git would
actually receive is what gets asserted — the same technique the GitHub parity
test uses to prove the provider refactor changed no commands.
"""
from __future__ import annotations

import pytest

from helpers import multi_remote as mr
from helpers.multi_remote import (
    PUSH_AUTH,
    PUSH_ERROR,
    PUSH_LEASE,
    PUSH_REJECTED,
    Remote,
    push,
    reconcile_selection,
    redact_url,
)

TIP = "a" * 40


class FakeGit:
    """Records argv and replays canned results, keyed by the git subcommand."""

    def __init__(self, ls_remote=None, pushes=None):
        self.calls = []
        self._ls = ls_remote if ls_remote is not None else {}
        self._pushes = pushes or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "ls-remote":
            remote = argv[1]
            if remote in self._ls:
                sha = self._ls[remote]
                if sha is None:                     # branch absent, remote fine
                    return "", 0
                return "%s\t%s\n" % (sha, argv[2]), 0
            return "fatal: repository not found", 128   # remote unreachable
        if argv[0] == "push":
            remote = next(a for a in argv[1:] if not a.startswith("-")
                          and ":" not in a)
            return self._pushes.get(remote, ("To https://x/%s\n" % remote, 0))
        return "", 0

    def pushes(self):
        return [c for c in self.calls if c[0] == "push"]

    def argv_for(self, remote):
        return next(c for c in self.pushes() if remote in c)


def _ok(remote="origin"):
    return ("To https://example.com/%s.git\n"
            "   1234567..89abcde  main -> main\n" % remote, 0)


def _lease_rejected(remote="origin"):
    return ("To https://example.com/%s.git\n"
            " ! [rejected]        main -> main (stale info)\n"
            "error: failed to push some refs\n" % remote, 1)


# ── upstream is a separate question from "who receives this" ─────────────

def test_only_the_designated_remote_gets_set_upstream():
    """The silent-config-edit bug: -u on every remote repoints tracking.

    The branch would end up tracking whichever remote happened to run last,
    changed by a button that only said "push".
    """
    git = FakeGit()
    push("git", ".", ["origin", "gitlab", "codeberg"], branch="main",
         upstream_remote="origin", runner=git)
    assert "-u" in git.argv_for("origin")
    assert "-u" not in git.argv_for("gitlab")
    assert "-u" not in git.argv_for("codeberg")


def test_no_upstream_is_set_when_none_is_designated():
    git = FakeGit()
    push("git", ".", ["origin", "gitlab"], branch="main", runner=git)
    assert all("-u" not in c for c in git.pushes())


def test_the_branch_is_pushed_by_explicit_refspec():
    """`HEAD:refs/heads/<branch>` — not bare HEAD.

    Bare HEAD resolves against whatever the remote thinks the branch is
    called, which need not match locally.
    """
    git = FakeGit()
    push("git", ".", ["origin"], branch="feature-x", runner=git)
    assert "HEAD:refs/heads/feature-x" in git.argv_for("origin")


# ── force-with-lease: read the expectation from the remote itself ───────

def test_the_lease_expectation_comes_from_the_remote_not_the_tracking_ref():
    """A rarely-fetched remote's local tracking ref is stale or absent.

    Bare --force-with-lease would lease against that, which is how the
    protection silently stops applying to exactly the remotes most likely to
    have diverged.
    """
    git = FakeGit(ls_remote={"origin": TIP})
    push("git", ".", ["origin"], branch="main", force_with_lease=True,
         runner=git)
    assert ["ls-remote", "origin", "refs/heads/main"] in git.calls
    assert "--force-with-lease=refs/heads/main:%s" % TIP \
        in git.argv_for("origin")


def test_a_branch_absent_from_the_remote_leases_on_must_not_exist():
    """Git reads an empty expectation as "this ref must not already exist".

    That is a real lease for a new branch, rather than an unprotected push
    dressed up as a safe one.
    """
    git = FakeGit(ls_remote={"origin": None})
    push("git", ".", ["origin"], branch="new-branch", force_with_lease=True,
         runner=git)
    assert "--force-with-lease=refs/heads/new-branch:" \
        in git.argv_for("origin")


def test_an_unreachable_remote_refuses_the_force_push_entirely():
    """Not knowing what would be overwritten is a reason to stop.

    Downgrading to an unprotected push here would defeat the entire feature,
    on precisely the remote whose state is unknown.
    """
    git = FakeGit(ls_remote={})           # ls-remote fails for every remote
    outcome = push("git", ".", ["origin"], branch="main",
                   force_with_lease=True, runner=git)
    assert outcome.results[0].ok is False
    assert outcome.results[0].kind == PUSH_ERROR
    assert "cannot be made safe" in outcome.results[0].detail
    assert git.pushes() == [], "nothing may be pushed when the lease is unknowable"


def test_force_is_never_used_anywhere():
    """The invariant, asserted across every path that could regress it."""
    git = FakeGit(ls_remote={"origin": TIP, "gitlab": None})
    push("git", ".", ["origin", "gitlab"], branch="main",
         force_with_lease=True, upstream_remote="origin", runner=git)
    for call in git.pushes():
        assert "--force" not in call
        assert "-f" not in call
        assert all(not a.startswith("--force=") for a in call)


def test_a_normal_push_carries_no_lease_at_all():
    git = FakeGit()
    push("git", ".", ["origin"], branch="main", runner=git)
    assert git.calls == [["push", "origin", "HEAD:refs/heads/main"]]


# ── independent per-remote outcomes ─────────────────────────────────────

def test_one_remote_failing_does_not_stop_the_others():
    """"The secret is still on codeberg" is the thing the caller must learn.

    Aborting the loop would hide which remotes ended up in which state.
    """
    git = FakeGit(ls_remote={"origin": TIP, "codeberg": TIP},
                  pushes={"origin": _lease_rejected("origin"),
                          "codeberg": _ok("codeberg")})
    outcome = push("git", ".", ["origin", "codeberg"], branch="main",
                   force_with_lease=True, runner=git)
    assert len(git.pushes()) == 2
    by_name = {r.remote: r for r in outcome.results}
    assert by_name["origin"].ok is False
    assert by_name["origin"].kind == PUSH_LEASE
    assert by_name["codeberg"].ok is True


def test_a_mixed_result_is_partial_not_failed():
    """Flattening this to "failed" loses the half that landed."""
    git = FakeGit(pushes={"origin": _ok("origin"),
                          "gitlab": ("fatal: Authentication failed", 1)})
    outcome = push("git", ".", ["origin", "gitlab"], branch="main", runner=git)
    assert outcome.is_partial is True
    assert outcome.all_ok is False
    assert outcome.summary() == "1/2 remotes succeeded"


def test_everything_succeeding_is_not_partial():
    git = FakeGit(pushes={"origin": _ok(), "gitlab": _ok()})
    outcome = push("git", ".", ["origin", "gitlab"], branch="main", runner=git)
    assert outcome.all_ok is True
    assert outcome.is_partial is False


def test_everything_failing_is_not_partial_either():
    git = FakeGit(pushes={"origin": ("fatal: nope", 1),
                          "gitlab": ("fatal: nope", 1)})
    outcome = push("git", ".", ["origin", "gitlab"], branch="main", runner=git)
    assert outcome.is_partial is False
    assert outcome.all_ok is False


@pytest.mark.parametrize("output,expected", [
    ("! [rejected] main -> main (stale info)", PUSH_LEASE),
    ("fatal: Authentication failed for 'https://x'", PUSH_AUTH),
    ("could not read Username for 'https://x'", PUSH_AUTH),
    ("fatal: terminal prompts disabled", PUSH_AUTH),
    ("! [rejected] main -> main (non-fast-forward)", PUSH_REJECTED),
    ("fatal: something unexpected", PUSH_ERROR),
])
def test_failures_are_classified_so_the_ui_can_say_what_to_do(output, expected):
    git = FakeGit(pushes={"origin": (output, 1)})
    outcome = push("git", ".", ["origin"], branch="main", runner=git)
    assert outcome.results[0].kind == expected


def test_pushing_to_nothing_yields_nothing():
    git = FakeGit()
    outcome = push("git", ".", [], branch="main", runner=git)
    assert outcome.results == ()
    assert outcome.all_ok is False and outcome.is_partial is False
    assert git.calls == []


# ── a remote is not a URL ───────────────────────────────────────────────

def test_two_push_urls_are_reported_separately():
    """`origin ✓` would be a lie when one of its destinations was rejected."""
    git = FakeGit(pushes={"origin": (
        "To https://a.example.com/r.git\n"
        "   1234567..89abcde  main -> main\n"
        "To https://b.example.com/r.git\n"
        " ! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs\n", 1)})
    outcome = push("git", ".", ["origin"], branch="main", runner=git)
    result = outcome.results[0]
    assert len(result.destinations) == 2
    assert [d.ok for d in result.destinations] == [True, False]
    assert result.is_partial is True


def test_a_single_destination_is_not_reported_as_partial():
    git = FakeGit(pushes={"origin": _ok()})
    outcome = push("git", ".", ["origin"], branch="main", runner=git)
    assert outcome.results[0].is_partial is False


def test_push_urls_replace_the_fetch_url_rather_than_adding_to_it():
    """Git's own rule — a configured pushurl is where the push goes."""
    plain = Remote("origin", fetch_urls=("https://f/",))
    assert plain.destinations == ("https://f/",)
    both = Remote("origin", fetch_urls=("https://f/",),
                  push_urls=("https://p1/", "https://p2/"))
    assert both.destinations == ("https://p1/", "https://p2/")


# ── credentials never reach a log or a dialog ───────────────────────────

def test_credentials_are_stripped_from_urls():
    assert redact_url("https://alice:ghp_secret@github.com/o/r.git") \
        == "https://***@github.com/o/r.git"
    assert redact_url("https://github.com/o/r.git") \
        == "https://github.com/o/r.git"
    assert redact_url("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert redact_url("") == ""


def test_a_credentialed_url_does_not_survive_into_a_result():
    """git prints the remote URL back at you, token and all."""
    git = FakeGit(pushes={"origin": (
        "To https://alice:ghp_secret@github.com/o/r.git\n"
        " ! [rejected] main -> main (fetch first)\n"
        "error: failed to push to "
        "'https://alice:ghp_secret@github.com/o/r.git'\n", 1)})
    outcome = push("git", ".", ["origin"], branch="main", runner=git)
    blob = repr(outcome.results[0])
    assert "ghp_secret" not in blob
    assert "***@github.com" in blob


def test_remote_destinations_are_redacted_for_display():
    remote = Remote("origin",
                    fetch_urls=("https://bob:tok@codeberg.org/a/b.git",))
    assert "tok" not in " ".join(remote.safe_destinations)


# ── selection reconciliation ────────────────────────────────────────────

def test_a_renamed_or_deleted_remote_drops_out_of_the_selection():
    """Git's current view is authoritative over anything saved earlier.

    A saved name that no longer exists would either error at push time or,
    worse, silently shrink the push while still reporting success.
    """
    live = [Remote("origin"), Remote("codeberg")]
    assert reconcile_selection(["origin", "gitlab"], live) == ("origin",)


def test_reconciliation_follows_the_live_order():
    live = [Remote("a"), Remote("b"), Remote("c")]
    assert reconcile_selection(["c", "a"], live) == ("a", "c")


def test_an_empty_saved_selection_stays_empty():
    assert reconcile_selection(None, [Remote("origin")]) == ()
    assert reconcile_selection([], [Remote("origin")]) == ()


# ── remote parsing ──────────────────────────────────────────────────────

def test_remote_listing_groups_fetch_and_push_urls(mocker):
    mocker.patch.object(mr, "_git", return_value=(
        "origin\thttps://f/r.git (fetch)\n"
        "origin\thttps://p1/r.git (push)\n"
        "origin\thttps://p2/r.git (push)\n"
        "backup\tgit@host:r.git (fetch)\n"
        "backup\tgit@host:r.git (push)\n", 0))
    remotes = {r.name: r for r in mr.list_remotes("git", ".")}
    assert remotes["origin"].fetch_urls == ("https://f/r.git",)
    assert remotes["origin"].push_urls == ("https://p1/r.git",
                                           "https://p2/r.git")
    assert remotes["backup"].destinations == ("git@host:r.git",)


def test_a_failed_remote_listing_is_fail_open(mocker):
    mocker.patch.object(mr, "_git", return_value=("not a repo", 128))
    assert mr.list_remotes("git", ".") == []


def test_a_detached_head_has_no_branch_to_push(mocker):
    mocker.patch.object(mr, "_git", return_value=("HEAD\n", 0))
    assert mr.current_branch("git", ".") is None
