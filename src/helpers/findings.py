"""helpers/findings.py — the one shape every diagnostic producer emits.

Three producers feed the CLI's `findings` array — `checks` (pyflakes +
compileall), `doctor`, and `scout` — and one consumer renders it: the VS Code
extension's `DiagnosticCollection`. They agree here, or they do not agree at
all.

The shape is modelled on `refactor_scout.Finding`, which already carried
`file` / `line` / `symbol` / `message`; this adds the two things a VS Code
`Diagnostic` needs and scout did not have — a **range** and a **severity**.

Four rules are enforced rather than documented, because each one is a thing
that silently produces a wrong squiggle instead of an error:

**Coordinates are 1-based, everywhere in Python and everywhere in the
envelope.** The single conversion to VS Code's 0-based `Position` happens in
TypeScript, at the boundary, once. Nothing here or downstream adjusts them
again.

**A range is required, and point-only producers get one for free.** Omitting
`end_line`/`end_column` fills them from `line`/`column`. A `Diagnostic` is a
range, not a point; if Python shipped only a start then TypeScript would have
to invent an end, which is exactly the kind of rule that must not live there.

**Severity is a closed set, chosen by the producer.** A typo raises here rather
than rendering as the wrong colour, and the consumer never infers severity from
`rule`.

**`file` is repo-relative with forward slashes.** Both check tools print
absolute paths with native separators; `relative_to` is the one place that is
undone, so a caller cannot forget and leak a Windows drive path into an
envelope that a different machine will read.

No Tk, no I/O. Safe to call from any thread.
"""
from __future__ import annotations

import dataclasses
import os

#: The only severities an envelope may carry. Ordered most- to least-severe.
#: Mirrors VS Code's `DiagnosticSeverity`, which is what consumes them.
SEVERITIES: tuple = ("error", "warning", "information", "hint")


@dataclasses.dataclass(frozen=True)
class Finding:
    """One diagnostic, in the shape the envelope carries it.

    ``rule`` is a producer-qualified identifier (`pyflakes/F401`,
    `doctor/complexity`, `scout/god_class`) so a consumer can group or filter
    without knowing which command produced the array.

    There is deliberately **no `id` field**. scout computes its own stable id
    for suppression (`md5(kind|file|symbol)`), but that ignores the message and
    the range, so it is not the same notion as identity here. The logical
    identity of a finding is ``(producer, file, line, column, rule, message)``;
    publishing a second thing called "identity" is what goes wrong the moment a
    dismiss feature exists.
    """
    file: str
    line: int
    column: int = 1
    message: str = ""
    severity: str = "warning"
    rule: str = ""
    symbol: str = ""
    end_line: "int | None" = None
    end_column: "int | None" = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"severity {self.severity!r} is not one of {SEVERITIES} — "
                "producers choose from a closed set so the consumer never has "
                "to guess")
        # Frozen, so fill the range defaults the only way a frozen dataclass
        # can. A point finding is a zero-width range at its own position.
        if self.end_line is None:
            object.__setattr__(self, "end_line", self.line)
        if self.end_column is None:
            object.__setattr__(self, "end_column", self.column)


def relative_to(path: str, project_root: str) -> str:
    """*path* as a repo-relative, forward-slash path.

    Both check tools print absolute paths with native separators, and on
    Windows they can even mix the two in one line (`C:/a/b\\c.py`) because
    the prefix is echoed as given while the leaf is joined with `os.sep`.
    Normalising first is what makes `relpath` reliable against either.

    Falls back to the normalised absolute path when no relative form exists (a
    different drive on Windows), because a wrong relative path would resolve
    against the workspace folder and point the editor at the wrong file.
    """
    norm_path = os.path.normpath(path.replace("\\", "/"))
    norm_root = os.path.normpath(project_root.replace("\\", "/"))
    try:
        rel = os.path.relpath(norm_path, norm_root)
    except ValueError:                       # different drive
        return norm_path.replace("\\", "/")
    if rel.startswith(".." + os.sep) or rel == "..":
        # Outside the project: relative would be misleading, absolute is honest.
        return norm_path.replace("\\", "/")
    return rel.replace("\\", "/")


def to_envelope(findings: "list") -> list:
    """Findings as plain dicts, key order fixed for readable diffs in logs.

    Every key is always present. An optional-key contract would make the
    TypeScript side branch on absence, which is a rule living in the wrong
    layer.
    """
    return [
        {
            "file": f.file,
            "line": f.line,
            "column": f.column,
            "end_line": f.end_line,
            "end_column": f.end_column,
            "severity": f.severity,
            "message": f.message,
            "rule": f.rule,
            "symbol": f.symbol,
        }
        for f in findings
    ]
