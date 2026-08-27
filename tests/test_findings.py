"""tests/test_findings.py — the shared diagnostic shape.

`helpers/findings.py` is the contract three producers (pyflakes/compileall,
Doctor, scout) and one consumer (the VS Code extension) agree on. The rules
worth testing are the ones whose violation produces a *wrong squiggle* rather
than an error: a bad severity, a missing range, a path that resolves against
the wrong root.

Pure module, no I/O, no Tk — no fixtures needed beyond the values themselves.
"""
from __future__ import annotations

import pytest

from helpers.findings import SEVERITIES, Finding, relative_to, to_envelope

SEP = chr(92)                       # a backslash, spelled to stay unambiguous


# ── severity is a closed set, chosen by the producer ──────────────────────

@pytest.mark.parametrize("severity", SEVERITIES)
def test_every_declared_severity_is_accepted(severity):
    assert Finding(file="a.py", line=1, severity=severity).severity == severity


def test_severity_mirrors_vscode_diagnosticseverity():
    """The consumer maps these 1:1, so the set may not drift silently."""
    assert SEVERITIES == ("error", "warning", "information", "hint")


def test_an_unknown_severity_is_refused_at_the_producer():
    """Better a loud failure here than a diagnostic rendered the wrong colour."""
    with pytest.raises(ValueError) as excinfo:
        Finding(file="a.py", line=1, severity="critical")
    assert "critical" in str(excinfo.value)


# ── every finding carries a range, even a point one ───────────────────────

def test_a_point_finding_gets_a_zero_width_range_for_free():
    """A VS Code Diagnostic is a range. If Python shipped only a start, the
    TypeScript side would have to invent an end — the rule in the wrong layer.
    """
    f = Finding(file="a.py", line=7, column=3)
    assert (f.end_line, f.end_column) == (7, 3)


def test_an_explicit_range_is_preserved():
    f = Finding(file="a.py", line=7, column=3, end_line=9, end_column=11)
    assert (f.end_line, f.end_column) == (9, 11)


def test_coordinates_are_one_based_and_untouched():
    """The single 0-based conversion happens in TypeScript, at the boundary."""
    f = Finding(file="a.py", line=1, column=1)
    assert (f.line, f.column, f.end_line, f.end_column) == (1, 1, 1, 1)


# ── the envelope shape ────────────────────────────────────────────────────

def test_envelope_always_carries_every_key():
    """An optional-key contract would make the consumer branch on absence,
    which is a rule living on the wrong side of the boundary."""
    got = to_envelope([Finding(file="a.py", line=1)])[0]
    assert set(got) == {"file", "line", "column", "end_line", "end_column",
                        "severity", "message", "rule", "symbol"}


def test_envelope_key_order_is_fixed():
    """Fixed so a logged envelope diffs readably between runs."""
    got = to_envelope([Finding(file="a.py", line=1)])[0]
    assert list(got) == ["file", "line", "column", "end_line", "end_column",
                         "severity", "message", "rule", "symbol"]


def test_envelope_of_nothing_is_an_empty_list():
    assert to_envelope([]) == []


def test_findings_carry_no_id_field():
    """scout has its own `md5(kind|file|symbol)` id for suppression, but it
    ignores the message and the range — a different notion. Exposing it here
    would publish two things called identity. Logical identity is the tuple
    (producer, file, line, column, rule, message)."""
    assert "id" not in to_envelope([Finding(file="a.py", line=1)])[0]


# ── relative_to: the one place absolute tool paths are undone ─────────────

def test_native_windows_separators_become_repo_relative():
    got = relative_to("C:" + SEP + "proj" + SEP + "src" + SEP + "a.py", "C:" + SEP + "proj")
    assert got == "src/a.py"


def test_posix_separators_become_repo_relative():
    assert relative_to("/home/me/proj/src/a.py", "/home/me/proj") == "src/a.py"


def test_mixed_separators_in_one_path_are_handled():
    """pyflakes echoes the prefix as given and joins the leaf with os.sep, so
    a single line really can carry both styles."""
    assert relative_to("C:/proj/src" + SEP + "a.py", "C:/proj") == "src/a.py"


def test_a_path_outside_the_project_stays_absolute():
    """A relative path would resolve against the workspace folder and send the
    editor to a file that is not the one reported."""
    got = relative_to("C:/elsewhere/a.py", "C:/proj")
    assert got == "C:/elsewhere/a.py"


def test_the_project_root_itself_relativises_to_dot():
    assert relative_to("C:/proj", "C:/proj") == "."


def test_output_never_contains_a_backslash():
    """The envelope is read on a machine that may not be this one."""
    got = relative_to("C:" + SEP + "proj" + SEP + "a" + SEP + "b.py", "C:" + SEP + "proj")
    assert SEP not in got
