"""Tests for helpers/instructions_posture.

The load-bearing one is `test_baseline_carriage_without_claude_reach_is_orphaned`:
eleven real projects carried a correct baseline include inside a file nothing
linked to, and every check the Manager had said they were fine. If that test
ever goes green for the wrong reason, the module has lost its purpose.
"""

import os

import pytest

from helpers import instructions_posture as ip


BASELINE_TEXT = (
    "# Project Baseline Rules\n\n"
    "## Tokensave: Use It First\n\ntable\n\n"
    "## Documentation Discipline\n\nrules\n"
)

FENCE_BACKTICK = "`" * 3
FENCE_LONG = "`" * 4
FENCE_TILDE = "~" * 3


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "project-baseline.md").write_text(BASELINE_TEXT, encoding="utf-8")
    return d


def make_project(tmp_path, name="proj", claude=None, basic=None):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if claude is not None:
        (root / "CLAUDE.md").write_text(claude, encoding="utf-8")
    if basic is not None:
        (root / "BASIC_INSTRUCTIONS.md").write_text(basic, encoding="utf-8")
    return root


def posture(root, templates, baseline="__default__", placeholder=""):
    if baseline == "__default__":
        baseline = ip.canonical(str(templates / "project-baseline.md"))
    return ip.read_project(str(root), root.name, str(templates), baseline,
                           BASELINE_TEXT, placeholder)


def inc_line(templates):
    return "@" + str(templates / "project-baseline.md")


# -- the original defect --------------------------------------------------

def test_baseline_carriage_without_claude_reach_is_orphaned(tmp_path, templates):
    """A correct include in a file nothing links to is ORPHANED, not RESOLVED.

    This is the shape of eleven real projects. `carriage` must still report
    that the declaration is correct: the file is not wrong, it is unreachable.
    """
    root = make_project(tmp_path, basic=inc_line(templates) + "\n\n# Notes\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_ORPHANED
    assert p.carriage == ip.CARRIAGE_CURRENT
    assert p.repairable is True


def test_claude_md_linking_basic_resolves(tmp_path, templates):
    root = make_project(tmp_path,
                        claude="# Proj\n\n@BASIC_INSTRUCTIONS.md\n",
                        basic=inc_line(templates) + "\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_RESOLVED
    assert p.reached_is_current is True
    assert ip.ADVISORY_NONSTANDARD_CHAIN not in p.advisories


def test_basic_reached_but_carrying_no_baseline_is_absent(tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic="# Notes only\n")
    assert posture(root, templates).reach == ip.REACH_ABSENT


# -- stale: identity, not spelling ----------------------------------------

def test_stale_path_with_prose_mention_of_the_filename(tmp_path, templates):
    """Proves the parser parses rather than substring-matching in a new costume.

    The bug this replaces was a substring test for the filename. A fixture that
    merely mentions that filename in prose must not move the verdict.
    """
    old = tmp_path / "old"
    old.mkdir()
    (old / "project-baseline.md").write_text(BASELINE_TEXT, encoding="utf-8")
    body = ("@" + str(old / "project-baseline.md") + "\n\n"
            "We used to keep project-baseline.md somewhere else.\n")
    root = make_project(tmp_path, claude=body)
    p = posture(root, templates)
    assert p.reach == ip.REACH_STALE
    assert p.stale_at.endswith(":1")
    assert "project-baseline.md" in p.reached_baseline


def test_separator_and_relative_spellings_are_the_same_baseline(tmp_path, templates):
    target = str(templates / "project-baseline.md")
    spellings = [target, target.replace(os.sep, "/"),
                 os.path.join("..", "templates", "project-baseline.md")]
    for i, spelling in enumerate(spellings):
        root = make_project(tmp_path, name="spell%d" % i,
                            claude="@" + spelling + "\n")
        assert posture(root, templates).reach == ip.REACH_RESOLVED, spelling


def test_matching_basename_elsewhere_is_a_different_baseline(tmp_path, templates):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "project-baseline.md").write_text("other\n", encoding="utf-8")
    root = make_project(tmp_path,
                        claude="@" + str(elsewhere / "project-baseline.md") + "\n")
    assert posture(root, templates).reach == ip.REACH_STALE


def test_baseline_outside_the_project_is_followed(tmp_path, templates):
    """The shared baseline lives in template_dir BY DESIGN.

    A boundary of "inside the project" alone would refuse the one include this
    whole feature exists for, and call every correct project unknown.
    """
    root = make_project(tmp_path, claude=inc_line(templates) + "\n")
    assert posture(root, templates).reach == ip.REACH_RESOLVED


def test_stale_baseline_outside_the_boundary_is_stale_not_unknown(tmp_path, templates):
    """A pointer left behind by a moved template_dir is the headline case.

    Recognising the NAME happens before the boundary test; only READING the
    file is what the boundary restrains. Getting this backwards buried a real
    project's stale pointer under "could not determine".
    """
    stale_dir = tmp_path / "far" / "away" / "templates"
    stale_dir.mkdir(parents=True)
    (stale_dir / "project-baseline.md").write_text("old\n", encoding="utf-8")
    root = make_project(tmp_path,
                        claude="@" + str(stale_dir / "project-baseline.md") + "\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_STALE
    assert p.detail


# -- the configured baseline is validated by the same parser --------------

def test_malformed_configured_baseline_is_unknown(tmp_path, templates):
    root = make_project(tmp_path, claude=inc_line(templates) + "\n")
    p = posture(root, templates, baseline=None)
    assert p.reach == ip.REACH_UNKNOWN
    assert "configured baseline" in p.detail


@pytest.mark.parametrize("line,ok", [
    (r"@D:\t\project-baseline.md", True),
    ("@D:/t/project-baseline.md", True),
    ("", False),
    ("   ", False),
    ("D:/t/project-baseline.md", False),      # no leading @
    ("@D:/t/project-baseline.txt", False),    # not markdown
    ("@a.md\n@b.md", False),                  # ambiguous
])
def test_parse_baseline_target(line, ok):
    assert (ip.parse_baseline_target(line) is not None) is ok


# -- directive grammar ----------------------------------------------------

def test_fenced_directives_are_not_followed(tmp_path, templates):
    for i, fence in enumerate([FENCE_BACKTICK, FENCE_TILDE, FENCE_LONG]):
        body = "# Doc\n\n%s\n%s\n%s\n" % (fence, inc_line(templates), fence)
        root = make_project(tmp_path, name="fence%d" % i, claude=body)
        assert posture(root, templates).reach == ip.REACH_ABSENT, fence


def test_fence_with_info_string_opens_and_bare_fence_closes(tmp_path, templates):
    body = ("%s python\nx = 1\n%s\n\n%s\n"
            % (FENCE_BACKTICK, FENCE_BACKTICK, inc_line(templates)))
    root = make_project(tmp_path, claude=body)
    assert posture(root, templates).reach == ip.REACH_RESOLVED


def test_unclosed_fence_suppresses_the_remainder(tmp_path, templates):
    root = make_project(
        tmp_path,
        claude="%s\nnever closed\n\n%s\n" % (FENCE_BACKTICK, inc_line(templates)))
    scan = ip.walk_chain(str(root), str(templates))
    assert scan.unclosed_fences
    assert not scan.baselines


def test_prose_mention_is_not_a_directive():
    directives, indented, _ = ip.scan_text("See @notes.md for details.\n")
    assert directives == ()
    assert indented == ()


def test_indented_directive_is_unknown_when_nothing_resolved(tmp_path, templates):
    """We have not measured whether Claude follows an indented include line.

    Guessing "no" risks proposing a repair that duplicates a working chain;
    guessing "yes" risks a false green. Unknown is the only honest answer, and
    it withholds the fix button.
    """
    root = make_project(tmp_path,
                        claude="# Doc\n\n  " + inc_line(templates) + "\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_UNKNOWN
    assert ip.ADVISORY_INDENTED_DIRECTIVE in p.advisories
    assert p.repairable is False
    assert "indented" in p.detail


def test_indented_directive_does_not_spoil_a_resolved_chain(tmp_path, templates):
    """An indented line can only ever ADD a path, so it cannot unmake one."""
    root = make_project(tmp_path,
                        claude=inc_line(templates) + "\n\n  @maybe.md\n")
    assert posture(root, templates).reach == ip.REACH_RESOLVED


# -- boundary and bounds --------------------------------------------------

def test_escape_above_the_project_is_unknown(tmp_path, templates):
    (tmp_path / "outside.md").write_text("x\n", encoding="utf-8")
    root = make_project(tmp_path, name="deep/proj", claude="@../../outside.md\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_UNKNOWN
    assert "outside project" in p.detail


def test_escape_is_measured_from_the_containing_file(tmp_path, templates):
    """A relative target in BASIC_INSTRUCTIONS.md resolves against ITS folder."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("y\n", encoding="utf-8")
    root = make_project(tmp_path, name="a/b/proj",
                        claude="@BASIC_INSTRUCTIONS.md\n",
                        basic="@../../../outside/x.md\n")
    assert posture(root, templates).reach == ip.REACH_UNKNOWN


def test_cycle_terminates_and_is_unknown(tmp_path, templates):
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic="@CLAUDE.md\n")
    assert posture(root, templates).reach == ip.REACH_UNKNOWN


def test_self_include_terminates(tmp_path, templates):
    root = make_project(tmp_path, claude="@CLAUDE.md\n")
    assert posture(root, templates).reach in (ip.REACH_UNKNOWN, ip.REACH_ABSENT)


def test_file_count_bound(tmp_path, templates):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(ip.MAX_FILES + 6):
        (root / ("f%d.md" % i)).write_text("@f%d.md\n" % (i + 1), encoding="utf-8")
    (root / "CLAUDE.md").write_text("@f0.md\n", encoding="utf-8")
    scan = ip.walk_chain(str(root), str(templates))
    assert scan.bounds
    assert len(scan.files) <= ip.MAX_FILES


def test_unreadable_claude_md_is_unknown_never_absent(tmp_path, templates):
    root = tmp_path / "bad"
    root.mkdir()
    (root / "CLAUDE.md").write_bytes(b"\xff\xfe\x00invalid utf-8 \xc3\x28")
    p = posture(root, templates)
    assert p.reach == ip.REACH_UNKNOWN


def test_missing_claude_md_is_absent_not_unknown(tmp_path, templates):
    """Absence we can see is a fact, not a failure to look."""
    root = make_project(tmp_path)
    assert posture(root, templates).reach == ip.REACH_ABSENT


# -- positive evidence outranks incompleteness ----------------------------

def test_resolved_survives_an_unreadable_sibling_branch(tmp_path, templates):
    """UNKNOWN preempts the NEGATIVE verdicts only.

    If the chain demonstrably arrives at the configured baseline, that is
    direct evidence. Downgrading it would under-report a project that is fine
    and hide it from the fleet count.
    """
    root = make_project(
        tmp_path,
        claude=inc_line(templates) + "\n@BASIC_INSTRUCTIONS.md\n")
    (root / "BASIC_INSTRUCTIONS.md").write_bytes(b"\xc3\x28\xff")
    assert posture(root, templates).reach == ip.REACH_RESOLVED


# -- advisories -----------------------------------------------------------

def test_two_paths_to_the_baseline_flag_double_load(tmp_path, templates):
    inc = inc_line(templates)
    root = make_project(tmp_path, claude=inc + "\n@BASIC_INSTRUCTIONS.md\n",
                        basic=inc + "\n")
    p = posture(root, templates)
    assert p.reach == ip.REACH_RESOLVED
    assert ip.ADVISORY_DOUBLE_LOAD in p.advisories


def test_duplicate_basic_directive_is_flagged_and_not_repairable(tmp_path, templates):
    root = make_project(
        tmp_path,
        claude="@BASIC_INSTRUCTIONS.md\n\ntext\n\n@BASIC_INSTRUCTIONS.md\n",
        basic="# nothing\n")
    p = posture(root, templates)
    assert ip.ADVISORY_DUPLICATE_DIRECTIVE in p.advisories
    assert p.repairable is False


def test_nonstandard_chain_resolves_but_is_not_repairable(tmp_path, templates):
    """Resolving through an undocumented hop is fine. Fixing it would not be."""
    root = make_project(tmp_path, claude="@docs/instructions.md\n")
    docs = root / "docs"
    docs.mkdir()
    (docs / "instructions.md").write_text(inc_line(templates) + "\n",
                                          encoding="utf-8")
    p = posture(root, templates)
    assert p.reach == ip.REACH_RESOLVED
    assert ip.ADVISORY_NONSTANDARD_CHAIN in p.advisories
    assert p.repairable is False


def test_contradictory_instructions_are_flagged(tmp_path, templates):
    root = make_project(
        tmp_path,
        claude=inc_line(templates)
        + "\n\n## Token-Saving Tool Usage\n\nUse the Explore subagent.\n")
    assert ip.ADVISORY_CONTRADICTS in posture(root, templates).advisories


def test_inline_restatement_of_the_baseline_is_flagged(tmp_path, templates):
    root = make_project(
        tmp_path,
        claude=inc_line(templates)
        + "\n\n## Tokensave: Use It First\n\ncopy\n\n"
          "## Documentation Discipline\n\ncopy\n")
    assert ip.ADVISORY_DUPLICATE_CONTENT in posture(root, templates).advisories


def test_untouched_placeholder_is_flagged(tmp_path, templates):
    placeholder = inc_line(templates) + "\n\n# [PROJECT NAME]\n"
    root = make_project(tmp_path, claude="@BASIC_INSTRUCTIONS.md\n",
                        basic=placeholder)
    p = posture(root, templates, placeholder=placeholder)
    assert ip.ADVISORY_PLACEHOLDER in p.advisories


# -- weight ---------------------------------------------------------------

def test_weight_reports_bytes_and_tokens_separately(tmp_path, templates):
    root = make_project(tmp_path, claude=inc_line(templates) + "\n")
    p = posture(root, templates)
    assert p.weight_bytes > 0
    assert p.estimated_tokens == p.weight_bytes // 4


# -- fleet aggregation ----------------------------------------------------

def test_one_directory_reached_through_two_roots_is_counted_once(
        tmp_path, templates, monkeypatch):
    """Population properties need canonical identity before counting."""
    root = make_project(tmp_path, claude=inc_line(templates) + "\n")
    duplicated = [
        {"path": str(root), "name": root.name},
        {"path": str(root).replace(os.sep, "/"), "name": root.name},
    ]
    monkeypatch.setattr(ip, "find_projects", lambda roots: duplicated)

    class Cfg:
        baseline_include_line = ""
        template_dir = ""
        basic_instructions_template = ""

    cfg = Cfg()
    cfg.baseline_include_line = inc_line(templates)
    cfg.template_dir = str(templates)
    fleet = ip.read_posture(["ignored"], cfg)
    assert fleet.total == 1


def test_counts_report_every_state_not_just_resolved(tmp_path, templates,
                                                     monkeypatch):
    resolved = make_project(tmp_path, name="ok", claude=inc_line(templates) + "\n")
    orphan = make_project(tmp_path, name="orph", basic=inc_line(templates) + "\n")
    entries = [{"path": str(resolved), "name": "ok"},
               {"path": str(orphan), "name": "orph"}]
    monkeypatch.setattr(ip, "find_projects", lambda roots: entries)

    class Cfg:
        pass

    cfg = Cfg()
    cfg.baseline_include_line = inc_line(templates)
    cfg.template_dir = str(templates)
    cfg.basic_instructions_template = ""
    counts = ip.read_posture(["ignored"], cfg).counts()
    assert counts[ip.REACH_RESOLVED] == 1
    assert counts[ip.REACH_ORPHANED] == 1
    assert set(counts) >= {ip.REACH_RESOLVED, ip.REACH_ORPHANED,
                           ip.REACH_STALE, ip.REACH_ABSENT, ip.REACH_UNKNOWN}


# -- exclusion ------------------------------------------------------------

def test_excluded_project_stays_in_the_population_but_is_not_repairable(
        tmp_path, templates, monkeypatch):
    """A vendor clone is shown and counted, never silently filtered out.

    Two upstream clones were wired by a bulk run before anyone noticed whose
    repositories they were. Reverting that by hand does not survive the next
    run, so the exclusion lives where the writer consults it. Hiding them
    instead would move the fleet denominator without saying so.
    """
    clone = make_project(tmp_path, name="vendor", claude="# upstream\n")
    mine = make_project(tmp_path, name="mine", claude=inc_line(templates) + "\n")
    entries = [{"path": str(clone), "name": "vendor"},
               {"path": str(mine), "name": "mine"}]
    monkeypatch.setattr(ip, "find_projects", lambda roots: entries)

    class Cfg:
        pass

    cfg = Cfg()
    cfg.baseline_include_line = inc_line(templates)
    cfg.template_dir = str(templates)
    cfg.basic_instructions_template = ""
    cfg.raw = {"instructions_skip_paths": [str(clone)]}

    fleet = ip.read_posture(["ignored"], cfg)
    assert fleet.total == 2                       # still counted
    by_name = {p.name: p for p in fleet.projects}
    assert by_name["vendor"].excluded is True
    assert by_name["vendor"].repairable is False  # and never written to
    assert by_name["vendor"].reach == ip.REACH_ABSENT   # still classified
    assert by_name["mine"].excluded is False


def test_exclusion_matches_on_canonical_path(tmp_path, templates, monkeypatch):
    """Separator and case spellings name the same directory."""
    clone = make_project(tmp_path, name="vendor", claude="# upstream\n")
    entries = [{"path": str(clone), "name": "vendor"}]
    monkeypatch.setattr(ip, "find_projects", lambda roots: entries)

    class Cfg:
        pass

    cfg = Cfg()
    cfg.baseline_include_line = inc_line(templates)
    cfg.template_dir = str(templates)
    cfg.basic_instructions_template = ""
    cfg.raw = {"instructions_skip_paths": [str(clone).replace(os.sep, "/").upper()]}

    assert ip.read_posture(["x"], cfg).projects[0].excluded is True
