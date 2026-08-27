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

import os

import pytest

from helpers.doctor_rules import (
    DEFAULT_CAPS,
    Caps,
    _audit_project_tree,
    _audit_python_file,
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
