"""tests/test_doctor_rules.py — the anti-monolith audit, now Tk-free.

Two things are being protected here.

**Parity.** These rules were moved verbatim out of ``controllers/doctor_ctrl``,
whose module-level ``import tkinter`` was the only reason the audit could not
run on a headless CI runner. A move that changes what the audit reports is not
a move, so the caps, the exemption syntax and every message string must behave
exactly as before.

**Honest thresholds.** The per-directory override exists because ``scripts/``
was blanket-skipped: one-off utilities tripped production caps and blocked
pushes, so the whole directory was hidden — along with anything genuinely wrong
in it. A looser cap still audits. The tests below pin that a skip and a loosened
cap are different things, and that a malformed override degrades to the
defaults rather than taking the audit down.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers import doctor_rules
from helpers.doctor_rules import (
    DEFAULT_CAPS,
    Caps,
    _audit_project_tree,
    _audit_python_file,
    audit_instructions,
    resolve_caps,
)


def _write(root, rel, text):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _complex_fn(name="f", branches=14):
    """A function whose cyclomatic complexity exceeds the default cap."""
    body = "\n".join(f"    if x == {i}: return {i}" for i in range(branches))
    return f"def {name}(x):\n{body}\n    return None\n"


# ── the module is genuinely Tk-free ──────────────────────────────────────────

def test_module_imports_without_tkinter(monkeypatch):
    """The whole point of the move: this must import on a headless runner.

    Simulates tkinter being unavailable and re-imports from scratch.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.split(".")[0] in ("tkinter", "_tkinter"):
            raise ImportError("tkinter unavailable (simulated headless CI)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    sys.modules.pop("helpers.doctor_rules", None)
    mod = importlib.import_module("helpers.doctor_rules")
    assert mod.DEFAULT_CAPS.complexity > 0


def test_source_declares_no_tk_import():
    """Belt and braces: a future edit must not reintroduce Tk here."""
    import helpers.doctor_rules as mod
    with open(mod.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "import tkinter" not in source
    assert "from tkinter" not in source


# ── resolve_caps ──────────────────────────────────────────────────────────────

def test_no_overrides_yields_the_defaults():
    assert resolve_caps("src/app.py", None) == DEFAULT_CAPS
    assert resolve_caps("src/app.py", {}) == DEFAULT_CAPS


def test_unmatched_path_yields_the_defaults():
    assert resolve_caps("src/app.py", {"scripts": {"max_lines": 500}}) \
        == DEFAULT_CAPS


def test_directory_prefix_matches_files_beneath_it():
    caps = resolve_caps("scripts/tool.py", {"scripts": {"max_complexity": 20}})
    assert caps.complexity == 20
    assert caps.file_lines == DEFAULT_CAPS.file_lines, \
        "unspecified caps must keep their defaults"


def test_prefix_match_does_not_bleed_into_a_similarly_named_sibling():
    """`scripts` must not match `scripts_archive/`."""
    caps = resolve_caps("scripts_archive/old.py",
                        {"scripts": {"max_complexity": 20}})
    assert caps == DEFAULT_CAPS


def test_longest_prefix_wins():
    """A subdirectory can tighten what its parent loosened."""
    overrides = {"scripts": {"max_complexity": 20},
                 "scripts/critical": {"max_complexity": 5}}
    assert resolve_caps("scripts/tool.py", overrides).complexity == 20
    assert resolve_caps("scripts/critical/x.py", overrides).complexity == 5


def test_skip_is_distinct_from_a_loose_cap():
    """None means "do not audit"; a Caps means "audit differently"."""
    assert resolve_caps("dist/x.py", {"dist": "skip"}) is None
    assert isinstance(resolve_caps("dist/x.py", {"dist": {"max_lines": 9}}),
                      Caps)


@pytest.mark.parametrize("value", [
    "SKIP", " skip ", "Skip",
])
def test_skip_is_case_and_space_insensitive(value):
    assert resolve_caps("dist/x.py", {"dist": value}) is None


def test_backslash_paths_resolve(monkeypatch):
    """Windows callers pass separators either way."""
    caps = resolve_caps("scripts\\tool.py", {"scripts": {"max_complexity": 20}})
    assert caps.complexity == 20


# ── malformed config degrades, never raises ──────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"scripts": {"max_complexity": "twenty"}},   # string, not int
    {"scripts": {"max_complexity": 0}},          # nonsensical
    {"scripts": {"max_complexity": -5}},         # nonsensical
    {"scripts": {"typo_key": 20}},               # unknown key
    {"scripts": {"max_complexity": True}},       # bool is an int in Python
    {"scripts": 42},                             # not a dict or "skip"
    {"": {"max_lines": 10}},                     # empty key
])
def test_malformed_override_falls_back_to_defaults(bad):
    """This comes from hand-edited JSON; one typo must not break the audit."""
    caps = resolve_caps("scripts/tool.py", bad)
    assert caps == DEFAULT_CAPS


def test_overrides_that_are_not_a_dict_are_ignored():
    assert resolve_caps("scripts/x.py", ["not", "a", "dict"]) == DEFAULT_CAPS


# ── end-to-end through the tree walk ─────────────────────────────────────────

def test_a_loosened_cap_still_reports_the_extreme_case(tmp_path):
    """The argument for tiers over a blanket skip.

    A script that trips the production cap at CC 14 is noise; one at CC 30 is
    a real finding. Skipping the directory loses both.
    """
    _write(tmp_path, "scripts/mild.py", _complex_fn("mild", branches=14))
    _write(tmp_path, "scripts/wild.py", _complex_fn("wild", branches=30))

    strict, _, _ = _audit_project_tree(str(tmp_path), set())
    assert len(strict) == 2, "production caps flag both"

    loose, _, _ = _audit_project_tree(
        str(tmp_path), set(), {"scripts": {"max_complexity": 20}})
    assert len(loose) == 1
    assert "wild()" in str(loose[0]), "the extreme one still surfaces"
    assert "cap 20" in str(loose[0]), "the message reports the cap applied"
    assert loose[0].symbol == "wild", "the symbol travels with the finding"
    assert loose[0].line > 0, "and so does the line it sits on"


def test_skip_via_overrides_scans_nothing_there(tmp_path):
    _write(tmp_path, "scripts/wild.py", _complex_fn("wild", branches=30))
    _write(tmp_path, "src/app.py", "x = 1\n")

    violations, _, scanned = _audit_project_tree(
        str(tmp_path), set(), {"scripts": "skip"})
    assert violations == []
    assert scanned == 1, "the skipped file is not counted as scanned either"


def test_default_call_signature_is_unchanged(tmp_path):
    """Existing callers pass two arguments; that must keep working."""
    _write(tmp_path, "src/app.py", _complex_fn("f", branches=30))
    violations, exempts, scanned = _audit_project_tree(str(tmp_path), set())
    assert scanned == 1 and len(violations) == 1 and exempts == []


def test_exemption_comment_still_wins_over_any_cap(tmp_path):
    """The escape hatch is unchanged by the move."""
    src = ("# anti-monolith: exempt — generated file\n"
           + _complex_fn("f", branches=30))
    path = _write(tmp_path, "src/gen.py", src)
    result = _audit_python_file(path)
    assert result["exempt"] is True
    assert result["violations"] == []


def test_caps_are_reported_in_the_message(tmp_path):
    """Messages quote the cap that applied, so a loosened run is legible."""
    path = _write(tmp_path, "x.py", _complex_fn("f", branches=30))
    default = _audit_python_file(path)
    loosened = _audit_python_file(path, Caps(complexity=25))
    assert "cap 10" in str(default["violations"][0])
    assert "cap 25" in str(loosened["violations"][0])


# ── R12-10: violations became records, and must still render as before ─────
#
# The audit used to hand back formatted strings, which is why a Doctor finding
# could not be clicked to a line while a scout finding could — the auditors
# walk AST nodes and always had `node.lineno`, but discarded it when
# formatting. `Violation` keeps it.
#
# `__str__` reproducing the old text EXACTLY is the property the whole change
# rests on: four consumers render these — the Doctor tab, the Run Checks
# dialog, the pre-push hook and the *generated* CI workflow snippet — and none
# of them were rewritten. Verified against the pre-change implementation on
# this repo's own tree: 152 violations, strings byte-identical.

def test_str_of_a_placed_violation_is_the_historic_two_space_form(tmp_path):
    """`  <rel path>: <message>` — the exact shape every renderer expects."""
    _write(tmp_path, "pkg/mod.py", _complex_fn("f", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert str(violations[0]) == "  pkg/mod.py: f() complexity 31 (cap 10)"


def test_a_file_level_violation_renders_and_points_at_line_one(tmp_path):
    """The file itself is the subject, so there is no narrower position."""
    _write(tmp_path, "big.py", "x = 1\n" * 20)
    violations, _, _ = _audit_project_tree(
        str(tmp_path), set(), {"big.py": {"max_lines": 5}})
    assert str(violations[0]) == "  big.py: file is 21 lines (cap 5)"
    assert (violations[0].line, violations[0].symbol) == (1, "")


def test_a_class_violation_carries_the_class_name_and_its_def_line(tmp_path):
    methods = "\n".join(f"    def m{i}(self): pass" for i in range(45))
    _write(tmp_path, "big.py", "# leading comment\n\nclass Wide:\n" + methods + "\n")
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    hit = next(v for v in violations if "class Wide" in str(v))
    assert hit.symbol == "Wide"
    assert hit.line == 3, "the line the `class` keyword sits on"
    assert str(hit).startswith("  big.py: class Wide has 45 direct methods")


def test_a_method_violation_points_at_its_def_not_the_file_top(tmp_path):
    _write(tmp_path, "m.py", "# pad\n# pad\n" + _complex_fn("deep", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert violations[0].symbol == "deep"
    assert violations[0].line == 3


def test_several_violations_in_one_file_each_keep_their_own_position(tmp_path):
    _write(tmp_path, "two.py",
           _complex_fn("first", branches=30) + "\n" + _complex_fn("second", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert [v.symbol for v in violations] == ["first", "second"]
    assert violations[0].line < violations[1].line
    assert all(v.file == "two.py" for v in violations)


def test_a_path_with_a_space_survives_into_the_rendered_string(tmp_path):
    """This project lives under a path with a space in it."""
    _write(tmp_path, "my dir/mod.py", _complex_fn("f", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert violations[0].file == "my dir/mod.py"
    assert str(violations[0]).startswith("  my dir/mod.py: ")


def test_rendered_paths_use_forward_slashes_on_every_platform(tmp_path):
    _write(tmp_path, "a/b/c.py", _complex_fn("f", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert violations[0].file == "a/b/c.py"
    assert chr(92) not in str(violations[0])


def test_every_violation_from_a_tree_walk_is_placed(tmp_path):
    """An unplaced violation would render without its path — the node-level
    auditors do not know the file, so `_audit_project_tree` must fill it in."""
    _write(tmp_path, "a.py", _complex_fn("f", branches=30))
    _write(tmp_path, "sub/b.py", _complex_fn("g", branches=30))
    violations, _, _ = _audit_project_tree(str(tmp_path), set())
    assert violations, "fixture should trip the cap"
    for violation in violations:
        assert violation.file, "every violation carries its path"
        assert violation.line >= 1
        assert str(violation).startswith("  " + violation.file + ": ")


# ── Index freshness: was this graph built by the tokensave installed? ────
#
# The gap this closes ran 20 days on this repository. tokensave 7.11.1
# shipped a resolver fix; upgrading does not rebuild the graph, and an
# incremental sync does not revisit call sites it has already resolved, so
# the fix sat installed and inert. One `sync --force` then removed 426
# impossible call edges. Nothing compared the installed binary against the
# one that built the index, and the integration check structurally cannot:
# it reads local files and the graph is not one of them.

import json as _json


def _ts_project(tmp_path, *, config="missing", version="7.11.0"):
    """A project directory with a .tokensave/config.json in a given state."""
    ts = tmp_path / ".tokensave"
    ts.mkdir()
    if config == "versioned":
        (ts / "config.json").write_text(
            _json.dumps({"root_dir": "x", "last_indexed_version": version}),
            encoding="utf-8")
    elif config == "no_version_key":
        (ts / "config.json").write_text(_json.dumps({"root_dir": "x"}),
                                        encoding="utf-8")
    elif config == "malformed":
        (ts / "config.json").write_text("{not json", encoding="utf-8")
    return str(tmp_path)


def _fake_version(monkeypatch, value):
    """Stub the installed-version probe rather than the subprocess.

    Patched via `monkeypatch.setattr` on a DIRECT module reference, not a
    string path -- the sys.modules footgun documented for this suite.
    """
    from helpers import doctor_rules
    monkeypatch.setattr(doctor_rules, "_installed_extractor_version",
                        lambda exe: value)


def test_index_freshness_silent_when_versions_agree(tmp_path, monkeypatch):
    """A healthy project must not gain a permanent line."""
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11.1", ""))
    proj = _ts_project(tmp_path, config="versioned", version="7.11.1")
    assert audit_index_extractor_version(proj, "tokensave.exe") == []


def test_index_freshness_silent_on_equivalent_version_spellings(
        tmp_path, monkeypatch):
    """7.11 and 7.11.0 are the same version, and _version_lt pads to say so."""
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11", ""))
    proj = _ts_project(tmp_path, config="versioned", version="7.11.0")
    assert audit_index_extractor_version(proj, "tokensave.exe") == []


def test_index_freshness_fires_on_a_stale_index(tmp_path, monkeypatch):
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11.1", ""))
    proj = _ts_project(tmp_path, config="versioned", version="7.11.0")
    joined = " ".join(audit_index_extractor_version(proj, "tokensave.exe"))
    assert "7.11.0" in joined and "7.11.1" in joined
    # the remedy, and the reason the obvious cheaper one will not work
    assert "sync --force" in joined
    assert "incremental sync does not revisit" in joined


def test_index_freshness_does_not_recommend_sync_on_a_downgrade(
        tmp_path, monkeypatch):
    """`sync --force` here would rebuild with the OLDER extractor.

    Same mismatch, opposite remedy. Giving the stale-index advice for a
    downgrade would discard whatever the newer extractor had fixed, so this
    is the one case where the rule must actively tell the user NOT to run
    the command the other case recommends.
    """
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11.1", ""))
    proj = _ts_project(tmp_path, config="versioned", version="7.12.0")
    joined = " ".join(audit_index_extractor_version(proj, "tokensave.exe"))
    assert "NEWER" in joined
    assert "Do NOT run `sync --force`" in joined
    assert "Upgrade tokensave" in joined


def test_index_freshness_silent_when_not_a_tokensave_project(
        tmp_path, monkeypatch):
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11.1", ""))
    assert audit_index_extractor_version(str(tmp_path), "tokensave.exe") == []


def test_index_freshness_silent_when_tokensave_not_configured(tmp_path):
    """Not configured is not our business; unrunnable is (see below)."""
    from helpers.doctor_rules import audit_index_extractor_version
    proj = _ts_project(tmp_path, config="versioned", version="7.11.0")
    assert audit_index_extractor_version(proj, "") == []


@pytest.mark.parametrize("config,fragment", [
    ("no_version_key", "no last_indexed_version"),
    ("malformed",      "cannot read .tokensave/config.json"),
])
def test_index_freshness_speaks_when_it_could_not_read_the_index(
        tmp_path, monkeypatch, config, fragment):
    """"Could not ask" must never share the quiet path with "they agree".

    That is how an unasked question starts reading as a clean answer, which
    is the whole reason graph_trust has four states instead of a boolean.
    """
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("7.11.1", ""))
    proj = _ts_project(tmp_path, config=config)
    joined = " ".join(audit_index_extractor_version(proj, "tokensave.exe"))
    assert fragment in joined
    assert "unverified rather than confirmed" in joined


def test_index_freshness_speaks_when_the_binary_would_not_run(
        tmp_path, monkeypatch):
    """Knowing one of the two versions is not knowing whether they agree."""
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("", "could not run `tokensave --version` (x)"))
    proj = _ts_project(tmp_path, config="versioned", version="7.11.0")
    joined = " ".join(audit_index_extractor_version(proj, "tokensave.exe"))
    assert "which tokensave is installed" in joined
    assert "unverified rather than confirmed" in joined
    # and it must not guess at a comparison it could not make
    assert "sync --force" not in joined


def test_index_freshness_reports_both_sides_when_both_are_unknown(
        tmp_path, monkeypatch):
    """Two different facts with two different remedies, so two lines."""
    from helpers.doctor_rules import audit_index_extractor_version
    _fake_version(monkeypatch, ("", "could not run `tokensave --version` (x)"))
    proj = _ts_project(tmp_path, config="malformed")
    notes = audit_index_extractor_version(proj, "tokensave.exe")
    joined = " ".join(notes)
    assert "which tokensave built this index" in joined
    assert "which tokensave is installed" in joined


def test_installed_version_probe_parses_the_real_binary_output():
    """The parse is a real contract with a real tool, not an assumed shape."""
    import os
    import shutil
    from helpers.doctor_rules import _installed_extractor_version
    exe = shutil.which("tokensave") or ""
    if not exe or not os.path.isfile(exe):
        pytest.skip("tokensave not on PATH")
    version, problem = _installed_extractor_version(exe)
    assert problem == ""
    assert version.count(".") >= 1


# ── audit_instructions ──────────────────────────────────────────────────────

class TestAuditInstructions:
    """The Doctor's view of whether a project's Claude instructions load.

    The property worth pinning is the one the whole feature was written
    against: an unread state must never be reported as an absence.
    """

    @staticmethod
    def _templates(tmp_path):
        d = tmp_path / "templates"
        d.mkdir()
        (d / "project-baseline.md").write_text("# Baseline\n", encoding="utf-8")
        return d, "@" + str(d / "project-baseline.md")

    def test_resolved_and_small_produces_no_note(self, tmp_path):
        templates, inc = self._templates(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "CLAUDE.md").write_text(inc + "\n", encoding="utf-8")
        assert audit_instructions(str(root), inc, str(templates)) == []

    def test_orphan_is_reported_with_the_reason_it_does_not_load(self, tmp_path):
        templates, inc = self._templates(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "BASIC_INSTRUCTIONS.md").write_text(inc + "\n", encoding="utf-8")
        notes = audit_instructions(str(root), inc, str(templates))
        assert len(notes) == 1
        assert "nothing links it from CLAUDE.md" in notes[0]

    def test_unknown_is_never_phrased_as_missing(self, tmp_path):
        """An unread state and an absent one are different findings.

        Calling the first "missing" is the false certainty this rule exists to
        avoid, so the wording is asserted rather than left to drift.
        """
        templates, inc = self._templates(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "CLAUDE.md").write_bytes(b"\xff\xfe\x00\xc3\x28")
        notes = audit_instructions(str(root), inc, str(templates))
        assert notes
        joined = " ".join(notes).lower()
        assert "could not determine" in joined
        assert "no baseline include anywhere" not in joined
        assert "missing" not in joined

    def test_malformed_configured_baseline_says_so(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        notes = audit_instructions(str(root), "not-an-include", "")
        assert notes and "not a valid include directive" in notes[0]

    def test_oversize_is_phrased_as_cost_not_fault(self, tmp_path):
        templates, inc = self._templates(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "CLAUDE.md").write_text(inc + "\n" + ("x" * 60_000) + "\n",
                                        encoding="utf-8")
        notes = audit_instructions(str(root), inc, str(templates))
        joined = " ".join(notes)
        assert "loads on every message" in joined
        assert "estimated" in joined
        for blaming in ("too large", "violation", "must"):
            assert blaming not in joined.lower()

    def test_thresholds_are_evaluated_on_bytes(self, tmp_path):
        """The token figure is an estimate and must never gate anything."""
        templates, inc = self._templates(tmp_path)
        root = tmp_path / "proj"
        root.mkdir()
        (root / "CLAUDE.md").write_text(inc + "\n" + ("x" * 400) + "\n",
                                        encoding="utf-8")
        assert audit_instructions(str(root), inc, str(templates),
                                  review_bytes=100) != []
        assert audit_instructions(str(root), inc, str(templates),
                                  review_bytes=10_000) == []

    def test_placeholder_note_does_not_claim_it_loads_when_it_does_not(self, tmp_path):
        """Filled-in and loaded are separate questions.

        Two real projects carried an untouched template that no chain reached
        while this note told the user it was loading. The note may only assert
        what it knows.
        """
        templates, inc = self._templates(tmp_path)
        placeholder = inc + "\n\n# [PROJECT NAME]\n"
        (templates / "claude-md-template.md").write_text(placeholder,
                                                         encoding="utf-8")
        root = tmp_path / "proj"
        root.mkdir()
        # CLAUDE.md reaches the baseline directly; BASIC_INSTRUCTIONS.md is
        # present, untouched, and on no chain.
        (root / "CLAUDE.md").write_text(inc + "\n", encoding="utf-8")
        (root / "BASIC_INSTRUCTIONS.md").write_text(placeholder, encoding="utf-8")

        notes = audit_instructions(str(root), inc, str(templates))
        joined = " ".join(notes)
        assert "untouched template" in joined
        assert "not read at all" in joined
        assert "so it loads as-is" not in joined


class TestAuditObservations:
    """The editor snapshot as a population, never as a total.

    Same three rules `TestAuditInstructions` pins, applied to the other
    source: absent is silent, unknown says "could not determine", and nothing
    is ever added to anything.
    """

    def _write(self, root, doc):
        from helpers.observations import snapshot_path
        path = snapshot_path(str(root))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def _envelope(self, rows, **coverage):
        return {
            "observations_schema_version": 1,
            "captured_at": 1789000000,
            "coverage": coverage,
            "observations": rows,
        }

    def _row(self, file="src/a.py", line=7):
        return {"file": file, "line": line, "column": 3, "end_line": line,
                "end_column": 9, "severity": "error", "message": "m",
                "rule": "reportOptionalMemberAccess", "producer": "Pylance"}

    def test_no_snapshot_is_silent(self, tmp_path):
        """Nobody having captured one is normal, not a fault.

        A note here would fire in every project, forever, and teach the reader
        to skip the section.
        """
        assert doctor_rules.audit_observations(str(tmp_path)) == []

    def test_an_unreadable_snapshot_says_could_not_determine(self, tmp_path):
        from helpers.observations import snapshot_path
        path = snapshot_path(str(tmp_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        notes = doctor_rules.audit_observations(str(tmp_path))
        assert notes
        assert "Could not determine" in notes[0]

    def test_unknown_is_never_phrased_as_an_absence(self, tmp_path):
        """The distinction the whole feature is written against.

        "We could not read it" must never render as "there is nothing there".
        """
        self._write(tmp_path, {"observations_schema_version": 99,
                               "observations": []})
        notes = doctor_rules.audit_observations(str(tmp_path))
        joined = " ".join(notes).lower()
        assert "could not determine" in joined
        for absence in ("no observations", "none", "nothing to report",
                        "missing", "empty"):
            assert absence not in joined, (
                "an unread snapshot is described as an absence: %r" % absence)

    def test_a_read_snapshot_reports_its_population(self, tmp_path):
        self._write(tmp_path, self._envelope(
            [self._row(), self._row(file="src/b.py")]))
        joined = " ".join(doctor_rules.audit_observations(str(tmp_path)))
        assert "2 observation(s)" in joined
        assert "2 file(s)" in joined

    def test_analyzed_files_is_reported_as_unknown_not_omitted(self, tmp_path):
        """An absent field reads as zero to whoever scans the output later."""
        self._write(tmp_path, self._envelope([self._row()]))
        joined = " ".join(doctor_rules.audit_observations(str(tmp_path)))
        assert "analyzed files: unknown" in joined

    def test_diagnostic_mode_is_called_a_policy(self, tmp_path):
        self._write(tmp_path,
                    self._envelope([self._row()], diagnostic_mode="workspace"))
        joined = " ".join(doctor_rules.audit_observations(str(tmp_path)))
        assert "workspace" in joined
        assert "POLICY" in joined or "policy" in joined

    def test_the_note_never_claims_these_are_manager_findings(self, tmp_path):
        self._write(tmp_path, self._envelope([self._row()]))
        joined = " ".join(doctor_rules.audit_observations(str(tmp_path)))
        assert "other extensions rendered" in joined
