"""Tests for helpers/install_identity.

Two questions that stop being the same one as soon as a second installation
exists, and the tests are arranged that way:

    where am I?          FIRST_RUN / SAME / MOVED
    who owns the fleet?  OWNED_HERE / OWNED_ELSEWHERE / SPLIT / UNOWNED

The load-bearing pair is at the bottom: `SAME + OWNED_HERE` must offer nothing,
and `SAME + OWNED_ELSEWHERE` must report ownership and offer **no relocation**.
The second is the one that pins the non-conflation — another install owning the
fleet is not this install having moved, and treating it as one would turn a
report into a silent transfer of fifteen repositories.
"""

import os
import types

import pytest

from helpers import install_identity as ii


def proj(name, reached=""):
    """A posture stand-in. `read_ownership` is pure and reads two fields."""
    return types.SimpleNamespace(name=name, reached_baseline=reached)


def base(tmp_path, name):
    d = tmp_path / name
    (d / "templates").mkdir(parents=True)
    (d / "templates" / "project-baseline.md").write_text(
        "# baseline\n", encoding="utf-8")
    return d


# ── where am I ───────────────────────────────────────────────────────────────

class TestIdentity:
    def test_no_record_is_first_run_and_not_a_move(self, tmp_path):
        """Absence of a record is not evidence of a move.

        Conflating them makes every first run after an upgrade announce a
        relocation that never happened -- and then offer to rewrite the fleet.
        """
        ident = ii.read_identity({}, str(tmp_path))
        assert ident.state == ii.IDENTITY_FIRST_RUN
        assert ident.moved is False

    def test_the_same_directory_is_same(self, tmp_path):
        ident = ii.read_identity({"install_dir": str(tmp_path)}, str(tmp_path))
        assert ident.state == ii.IDENTITY_SAME

    def test_separators_do_not_make_a_move(self, tmp_path):
        forward = str(tmp_path).replace(os.sep, "/")
        ident = ii.read_identity({"install_dir": forward}, str(tmp_path))
        assert ident.state == ii.IDENTITY_SAME

    def test_case_folding_is_asserted_per_platform(self, tmp_path):
        """`canonical` folds case on Windows and is a NO-OP on POSIX.

        Asserted both ways rather than assumed: a test that hard-coded the
        Windows answer passed for months here and failed the first time CI ran
        it on Linux. `/TMP/x` genuinely is a different directory from `/tmp/x`.
        """
        ident = ii.read_identity({"install_dir": str(tmp_path).upper()},
                                 str(tmp_path))
        if os.name == "nt":
            assert ident.state == ii.IDENTITY_SAME
        else:
            assert ident.state == ii.IDENTITY_MOVED

    def test_a_different_directory_is_a_move(self, tmp_path):
        ident = ii.read_identity({"install_dir": str(tmp_path / "old")},
                                 str(tmp_path / "new"))
        assert ident.moved is True
        assert ident.recorded_display.endswith("old")

    def test_an_alias_reads_as_a_different_install(self, tmp_path):
        """The CONTRACT, not a limitation.

        Identity is lexical, matching `instructions_posture.canonical`, which
        deliberately does not `realpath`. Filesystem alias identity is out of
        scope; changing that would change baseline identity fleet-wide.
        """
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        try:
            os.symlink(str(real), str(alias), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("this platform or user cannot create a dir symlink")
        ident = ii.read_identity({"install_dir": str(real)}, str(alias))
        assert ident.state == ii.IDENTITY_MOVED


# ── who owns the fleet ───────────────────────────────────────────────────────

class TestOwnership:
    def test_every_project_here_is_owned_here(self, tmp_path):
        here = str(tmp_path / "t")
        fleet = [proj("a", os.path.join(here, "project-baseline.md")),
                 proj("b", os.path.join(here, "project-baseline.md"))]
        own = ii.read_ownership(fleet, here)
        assert own.state == ii.OWNED_HERE
        assert own.owners[0].count == 2

    def test_every_project_elsewhere_is_owned_elsewhere(self, tmp_path):
        here = str(tmp_path / "t")
        other = str(tmp_path / "dist" / "templates")
        own = ii.read_ownership(
            [proj("a", os.path.join(other, "project-baseline.md"))], here)
        assert own.state == ii.OWNED_ELSEWHERE
        assert own.owners[0].display_dir == other

    def test_two_owners_is_split_with_counts_not_a_label(self, tmp_path):
        """A bare SPLIT says nothing actionable.

        Reachable today: a source checkout and a dist build write different
        baseline paths into the same projects, so whichever ran last wins.
        """
        a = str(tmp_path / "src" / "templates")
        b = str(tmp_path / "dist" / "templates")
        fleet = [proj("p1", os.path.join(a, "project-baseline.md")),
                 proj("p2", os.path.join(a, "project-baseline.md")),
                 proj("p3", os.path.join(b, "project-baseline.md"))]
        own = ii.read_ownership(fleet, a)

        assert own.state == ii.OWNERSHIP_SPLIT
        assert [o.count for o in own.owners] == [2, 1]
        assert own.owners[0].display_dir == a
        assert set(own.owners[1].projects) == {"p3"}

    def test_two_spellings_of_one_directory_are_not_a_split(self, tmp_path):
        """Grouped by `canonical`, so a separator difference is not a second
        owner. On POSIX case still separates them, which is correct."""
        here = str(tmp_path / "t")
        fleet = [proj("a", os.path.join(here, "project-baseline.md")),
                 proj("b", here.replace(os.sep, "/") + "/project-baseline.md")]
        own = ii.read_ownership(fleet, here)
        assert own.state == ii.OWNED_HERE
        assert len(own.owners) == 1

    def test_unowned_is_precise_no_project_reaches_a_baseline(self, tmp_path):
        own = ii.read_ownership([proj("a"), proj("b")], str(tmp_path))
        assert own.state == ii.OWNERSHIP_UNOWNED
        assert own.owners == ()
        assert own.unresolved == ("a", "b")

    def test_unresolved_projects_never_move_the_verdict(self, tmp_path):
        """They are ordinary wiring work, counted separately.

        Folding them into an owner's tally would overload "unknown" with a
        second meaning, which this project has repeatedly benefited from
        refusing to do.
        """
        here = str(tmp_path / "t")
        fleet = [proj("a", os.path.join(here, "project-baseline.md")),
                 proj("b"), proj("c")]
        own = ii.read_ownership(fleet, here)
        assert own.state == ii.OWNED_HERE
        assert own.owners[0].count == 1
        assert own.unresolved == ("b", "c")


# ── what a move invalidates ──────────────────────────────────────────────────

class TestRelocatableKeys:
    def _moved(self, tmp_path):
        return ii.read_identity({"install_dir": str(tmp_path / "old")},
                                str(tmp_path / "new"))

    def test_a_template_dir_inside_the_old_install_is_repointed(self,
                                                                tmp_path):
        raw = {"template_dir": str(tmp_path / "old" / "templates")}
        updates = ii.relocatable_updates(raw, self._moved(tmp_path))
        assert len(updates) == 1
        key, old, new = updates[0]
        assert key == "template_dir"
        assert old.endswith("templates")
        assert new == os.path.normpath(str(tmp_path / "new" / "templates"))

    def test_a_template_dir_outside_the_old_install_stays_put(self, tmp_path):
        """Somebody pointed it at a shared folder on purpose."""
        raw = {"template_dir": str(tmp_path / "shared" / "templates")}
        assert ii.relocatable_updates(raw, self._moved(tmp_path)) == ()

    def test_search_roots_inside_the_old_install_is_never_rewritten(self,
                                                                    tmp_path):
        """Why this is an allowlist and not a computed containment rule.

        A search root CAN live inside the Manager's folder. A rule that
        rewrote anything found there would silently repoint the user's project
        discovery as a side effect of moving the application.
        """
        raw = {"search_roots": [str(tmp_path / "old" / "projects")],
               "template_dir": str(tmp_path / "old" / "templates")}
        updates = ii.relocatable_updates(raw, self._moved(tmp_path))
        assert [u[0] for u in updates] == ["template_dir"]

    def test_an_external_tool_path_is_never_rewritten(self, tmp_path):
        raw = {"tokensave_exe": str(tmp_path / "old" / "tokensave.exe")}
        assert ii.relocatable_updates(raw, self._moved(tmp_path)) == ()

    def test_nothing_is_rewritten_when_nothing_moved(self, tmp_path):
        ident = ii.read_identity({"install_dir": str(tmp_path)}, str(tmp_path))
        raw = {"template_dir": str(tmp_path / "templates")}
        assert ii.relocatable_updates(raw, ident) == ()


# ── baseline evidence, which is not a version ────────────────────────────────

class TestBaselineFacts:
    def test_a_missing_baseline_is_not_an_exception(self, tmp_path):
        facts = ii.baseline_facts(str(tmp_path / "nope.md"))
        assert facts.exists is False
        assert facts.content_hash == ""

    def test_same_size_and_different_content_still_differ(self, tmp_path):
        """Size and mtime can coincide; the hash is why they are both carried.

        Two DIFFERENT baselines must not be able to look equivalent to somebody
        reading only a date and a byte count.
        """
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("AAAA", encoding="utf-8")
        b.write_text("BBBB", encoding="utf-8")
        fa, fb = ii.baseline_facts(str(a)), ii.baseline_facts(str(b))
        assert fa.size == fb.size
        assert fa.differs_from(fb) is True


# ── the two questions combined ───────────────────────────────────────────────

class TestRelocationPlan:
    def _plan(self, tmp_path, raw, fleet, here, install_dir=None):
        ident = ii.read_identity(
            {"install_dir": install_dir or raw.get("install_dir", "")},
            str(tmp_path / "new"))
        own = ii.read_ownership(fleet, here)
        return ii.relocation_plan(ident, own, raw, here)

    def test_same_and_owned_here_offers_nothing(self, tmp_path):
        """The healthy case. No dialog, no plan, no writes."""
        here = str(base(tmp_path, "new") / "templates")
        ident = ii.read_identity({"install_dir": str(tmp_path / "new")},
                                 str(tmp_path / "new"))
        own = ii.read_ownership(
            [proj("a", os.path.join(here, "project-baseline.md"))], here)
        plan = ii.relocation_plan(ident, own, {"template_dir": here}, here)

        assert ident.state == ii.IDENTITY_SAME
        assert own.state == ii.OWNED_HERE
        assert plan.offers_bulk is False
        assert plan.projects == ()
        assert plan.config_updates == ()

    def test_same_and_owned_elsewhere_reports_and_offers_no_relocation(
            self, tmp_path):
        """The pin on the non-conflation.

        I have not moved; another installation owns the fleet. That is a state,
        not an instruction. This module cannot know whether it reflects a
        deliberate handover, an interrupted repair, or a second install quietly
        taking them -- so it reports, and taking ownership stays an operation
        somebody invokes.
        """
        here = str(base(tmp_path, "new") / "templates")
        other = str(base(tmp_path, "dist") / "templates")
        ident = ii.read_identity({"install_dir": str(tmp_path / "new")},
                                 str(tmp_path / "new"))
        own = ii.read_ownership(
            [proj("a", os.path.join(other, "project-baseline.md"))], here)
        plan = ii.relocation_plan(ident, own, {"template_dir": here}, here)

        assert ident.moved is False
        assert own.state == ii.OWNED_ELSEWHERE
        # Reported...
        assert plan.ownership.owners[0].display_dir == other
        # ...and nothing is proposed, because nothing about this says "move".
        assert plan.config_updates == ()

    def test_a_split_fleet_refuses_bulk_repair(self, tmp_path):
        here = str(base(tmp_path, "new") / "templates")
        other = str(base(tmp_path, "dist") / "templates")
        fleet = [proj("a", os.path.join(here, "project-baseline.md")),
                 proj("b", os.path.join(other, "project-baseline.md"))]
        plan = self._plan(tmp_path, {"template_dir": here}, fleet, here,
                          install_dir=str(tmp_path / "old"))
        assert plan.ownership.is_split is True
        assert plan.blocked
        assert plan.offers_bulk is False

    def test_an_unowned_fleet_refuses_bulk_repair(self, tmp_path):
        here = str(base(tmp_path, "new") / "templates")
        plan = self._plan(tmp_path, {"template_dir": here},
                          [proj("a"), proj("b")], here,
                          install_dir=str(tmp_path / "old"))
        assert plan.blocked
        assert "ordinary wiring" in plan.blocked

    def test_any_unresolved_project_refuses_bulk_repair(self, tmp_path):
        """Otherwise "one action" becomes "one action that fixed half of it"."""
        here = str(base(tmp_path, "new") / "templates")
        other = str(base(tmp_path, "old") / "templates")
        fleet = [proj("a", os.path.join(other, "project-baseline.md")),
                 proj("b")]
        plan = self._plan(tmp_path, {"template_dir": other}, fleet, here,
                          install_dir=str(tmp_path / "old"))
        assert plan.blocked
        assert plan.offers_bulk is False

    def test_a_clean_move_offers_the_repoint(self, tmp_path):
        old_base = base(tmp_path, "old")
        new_base = base(tmp_path, "new")
        here = str(new_base / "templates")
        other = str(old_base / "templates")
        fleet = [proj("a", os.path.join(other, "project-baseline.md")),
                 proj("b", os.path.join(other, "project-baseline.md"))]
        plan = self._plan(tmp_path, {"template_dir": other}, fleet, here,
                          install_dir=str(tmp_path / "old"))

        assert plan.identity.moved is True
        assert plan.blocked == ""
        assert plan.offers_bulk is True
        assert set(plan.projects) == {"a", "b"}
        assert [u[0] for u in plan.config_updates] == ["template_dir"]

    def test_a_content_difference_is_reported_as_a_downgrade_risk(self,
                                                                  tmp_path):
        """The measured condition: a stale release shipped a baseline four
        months older than the source. Shown, never decided."""
        old_base = base(tmp_path, "old")
        new_base = base(tmp_path, "new")
        (new_base / "templates" / "project-baseline.md").write_text(
            "# baseline, much newer and longer\n", encoding="utf-8")
        here = str(new_base / "templates")
        other = str(old_base / "templates")
        fleet = [proj("a", os.path.join(other, "project-baseline.md"))]
        plan = self._plan(tmp_path, {"template_dir": other}, fleet, here,
                          install_dir=str(tmp_path / "old"))

        assert plan.downgrades is True
        assert plan.shipped_baseline.content_hash
        assert plan.reached_baseline.content_hash
