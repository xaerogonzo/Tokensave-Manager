"""Changelog fragments: a PR adds a file under ``changelog.d/``, never a line of CHANGELOG.md.

    python scripts/changelog.py new <slug>          # create changelog.d/<stamp>-<slug>-<hex>.md
    python scripts/changelog.py check               # validate every fragment (read-only)
    python scripts/changelog.py check --no-pending  # release gate: no fragment may remain
    python scripts/changelog.py preview             # print the CHANGELOG.md ``build`` would write
    python scripts/changelog.py build               # fold fragments into [Unreleased], delete them

Why: every PR inserted its entry at the top of the same ``### Added`` / ``### Fixed``
group in ``## [Unreleased]``, so whichever PR merged second conflicted. Two files with
different names cannot conflict.

Adapted from OpenChem Studio's ``tools/changelog.py`` (xaerogonzo/OpenChem-Studio PR #432, merged
as a248c91c9c45d36ef04d34fd19ddf0e095868e51; the tool is identical to PR head 3707d97). That tool takes one ``### Title`` per
fragment; this changelog is grouped by category, so a fragment here holds one or more
``### <Category>`` sections and ``build`` merges each into the matching group. The two are
an adaptation, not a mirror; do not sync them.

Contracts worth knowing:

  * Filename ``YYYYMMDDHHMM-<slug>-<4 hex>.md`` (UTC). The hex keeps same-slug fragments from
    independent branches apart; the stamp is the ORDER, newest first. It needs no Git history,
    so it is the same in a shallow CI clone and for an uncommitted file.
  * ``build`` writes CHANGELOG.md completely before it deletes any fragment, and touches no Git
    index. If a deletion then fails, a re-run recognises a fragment whose bullets are ALL already
    in [Unreleased] as already folded and removes it instead of inserting it twice. A fragment
    that is only partly present is an error: that is a hand edit, not a crash.
  * Only the root CHANGELOG.md is read; its own line ending (LF or CRLF) is kept.

Pure functions plus file IO. Standard library only, no UI imports: safe from any thread, and
the Release Wizard can import it.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_DIR = "changelog.d"

#: What a fragment's filename looks like. README.md can never match it.
FRAGMENT_NAME = re.compile(r"^\d{12}-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{4}\.md$")

#: Category headings a fragment may use, in the order a new group is placed.
CATEGORIES = ("Added", "Changed", "Fixed", "Removed", "Deprecated", "Security", "Docs", "Other")

#: ``new`` writes this; ``check`` rejects it, so an unedited template cannot reach master.
PLACEHOLDER = "REPLACE ME"
TEMPLATE = f"### Added\n- {PLACEHOLDER}: (area) **What changed.** Why it matters.\n"

UNRELEASED = "## [Unreleased]"
_BULLET = re.compile(r"^- \S")


class ChangelogError(ValueError):
    """The request cannot be carried out."""


@dataclass
class BuildResult:
    """What ``build`` did (or, with ``write=False``, would do)."""

    text: str
    folded: list[Path] = field(default_factory=list)  # inserted into [Unreleased]
    stale: list[Path] = field(default_factory=list)   # already in [Unreleased]; only removed


def _norm(line: str) -> str:
    return " ".join(line.lower().split())


def slugify(raw: str) -> str:
    """A safe slug, or a refusal. Separators and ``..`` are refused, not quietly rewritten."""
    if re.search(r"[/\\]|\.\.", raw):
        raise ChangelogError(f"{raw!r} is not a slug: no path separators or '..'")
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not slug:
        raise ChangelogError(f"{raw!r} has no letters or digits to make a slug from")
    return slug


def fragment_paths(root: Path) -> list[Path]:
    """Real fragments, newest first (filename order, reversed)."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    names = sorted((p.name for p in directory.iterdir() if FRAGMENT_NAME.fullmatch(p.name)), reverse=True)
    return [directory / n for n in names]


def new_fragment(root: Path, raw_slug: str, now: datetime | None = None) -> Path:
    """Create a template fragment with an exclusive open, so it can never overwrite one."""
    slug = slugify(raw_slug)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M")
    directory = root / FRAGMENT_DIR
    directory.mkdir(exist_ok=True)
    for _ in range(50):
        path = directory / f"{stamp}-{slug}-{secrets.token_hex(2)}.md"
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(TEMPLATE)
        except FileExistsError:
            continue
        return path
    raise ChangelogError("could not find an unused fragment name")  # pragma: no cover


def _lines(text: str) -> list[str]:
    return text.lstrip("﻿").replace("\r\n", "\n").split("\n")


def _is_fence(line: str) -> bool:
    return line.strip().startswith(("```", "~~~"))


def parse_fragment(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """(sections, problems): ``{category: [bullet block, ...]}``; a block is its lines joined.

    A fenced block hides its lines from every structural rule, and belongs to the bullet it
    sits inside.
    """
    problems: list[str] = []
    sections: dict[str, list[list[str]]] = {}
    current: str | None = None
    block: list[str] | None = None
    in_fence = False
    first_content: str | None = None
    for line in _lines(text):
        if first_content is None and line.strip():
            first_content = line
        if in_fence or _is_fence(line):
            if _is_fence(line):
                in_fence = not in_fence
            if block is None:
                problems.append("a code block must sit inside a bullet")
            else:
                block.append(line)
            continue
        if not line.strip():
            if block is not None:
                block.append(line)
        elif line.startswith("### "):
            current, block = _open_section(line[4:].strip(), sections, problems), None
        elif re.match(r"^#{1,2} ", line):
            problems.append("a '#' or '##' heading would break the changelog's structure")
        elif _BULLET.match(line):
            block = [line]
            if current is None:
                problems.append("a bullet before any '### <Category>' line")
            else:
                sections[current].append(block)
        elif block is not None:
            block.append(line)
        else:
            problems.append(f"text outside a bullet: {line.strip()[:40]!r}")
    problems += _structure_problems(text, sections, first_content, in_fence)
    return {k: ["\n".join(b).rstrip() for b in v] for k, v in sections.items()}, problems


def _open_section(name: str, sections: dict, problems: list[str]) -> str | None:
    if name not in CATEGORIES:
        problems.append(f"unknown category {name!r}; use one of {', '.join(CATEGORIES)}")
        return None
    if name in sections:
        problems.append(f"category {name!r} appears twice; put its bullets under one heading")
        return name
    sections[name] = []
    return name


def _structure_problems(text: str, sections: dict, first_content: str | None, in_fence: bool) -> list[str]:
    problems: list[str] = []
    if in_fence:
        problems.append("a code fence is never closed")
    if first_content is None:
        return ["the file is empty"]
    if not first_content.startswith("### "):
        problems.append("the first line must be a '### <Category>' line (nothing before it)")
    if not sections:
        problems.append("no valid '### <Category>' section")
    problems += [f"category {name!r} has no '- ' bullet" for name, blocks in sections.items() if not blocks]
    if PLACEHOLDER in text:
        problems.append(f"still contains the template placeholder {PLACEHOLDER!r}")
    return problems


def unreleased_span(lines: list[str]) -> tuple[int, int]:
    """(heading index, index of the next '## [' heading or len). Exactly one Unreleased."""
    starts = [i for i, line in enumerate(lines) if line.rstrip() == UNRELEASED]
    if len(starts) != 1:
        raise ChangelogError(f"CHANGELOG.md must have exactly one '{UNRELEASED}' heading, found {len(starts)}")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    return start, end


def unreleased_bullets(changelog: str) -> set[str]:
    """Normalised first lines of every top-level bullet already in [Unreleased]."""
    lines = _lines(changelog)
    start, end = unreleased_span(lines)
    return {_norm(line) for line in lines[start + 1 : end] if _BULLET.match(line)}


def _bullet_keys(sections: dict[str, list[str]]) -> list[str]:
    return [_norm(block.split("\n", 1)[0]) for blocks in sections.values() for block in blocks]


def classify(root: Path, changelog: str) -> tuple[list[tuple[Path, dict]], list[Path], list[str]]:
    """(to fold, already folded, errors). Errors name their file; empty means clean."""
    errors: list[str] = []
    present = unreleased_bullets(changelog)
    seen: dict[str, str] = {}
    fold: list[tuple[Path, dict]] = []
    stale: list[Path] = []
    for path in fragment_paths(root):
        label = f"{FRAGMENT_DIR}/{path.name}"
        sections, problems = parse_fragment(path.read_text(encoding="utf-8"))
        errors += [f"{label}: {p}" for p in problems]
        if problems:
            continue
        keys = _bullet_keys(sections)
        hit = [k for k in keys if k in present]
        errors += [f"{label}: bullet duplicates {seen[k]}" for k in keys if k in seen]
        seen.update({k: label for k in keys})
        if hit and len(hit) == len(keys):
            stale.append(path)
        elif hit:
            errors.append(f"{label}: some bullets are already in [Unreleased] and some are not; edit or delete it")
        else:
            fold.append((path, sections))
    return fold, stale, errors


def validate(root: Path, changelog: str) -> list[str]:
    """Every problem across all fragments. A fragment already folded is not a problem here."""
    try:
        return classify(root, changelog)[2]
    except ChangelogError as exc:
        return [str(exc)]


def render(changelog: str, fragments: list[dict[str, list[str]]]) -> str:
    """The changelog with each category's bullets prepended to its [Unreleased] group.

    ``fragments`` is newest first; within a group the newest bullets end up on top. Pure:
    strings in, string out, which is why preview and build cannot disagree.
    """
    if not fragments:
        return changelog
    eol = "\r\n" if "\r\n" in changelog else "\n"
    lines = _lines(changelog)
    merged = {c: [b for f in fragments for b in f.get(c, [])] for c in CATEGORIES}
    for category in CATEGORIES:
        if merged[category]:
            lines = _merge_category(lines, category, [ln for b in merged[category] for ln in b.split("\n")])
    return eol.join(lines)


def _groups(lines: list[str], start: int, end: int) -> list[tuple[str, int]]:
    """(category name, heading index) for every '### ' heading inside the span."""
    return [(lines[i][4:].strip(), i) for i in range(start + 1, end) if lines[i].startswith("### ")]


def _merge_category(lines: list[str], category: str, block_lines: list[str]) -> list[str]:
    start, end = unreleased_span(lines)
    groups = _groups(lines, start, end)
    existing = next((i for name, i in groups if name == category), None)
    if existing is not None:
        rest = lines[existing + 1 :]
        while rest and not rest[0].strip():
            rest.pop(0)
        return lines[: existing + 1] + [""] + block_lines + rest
    rank = CATEGORIES.index(category)
    later = [i for name, i in groups if name in CATEGORIES and CATEGORIES.index(name) > rank]
    at = min(later) if later else end
    new = [f"### {category}", ""] + block_lines + [""]
    if at == end and at > 0 and lines[at - 1].strip():
        new = [""] + new
    return lines[:at] + new + lines[at:]


@dataclass
class Collected:
    """Every fragment on disk, classified, with their bullets merged per category."""

    sections: dict[str, list[str]] = field(default_factory=dict)  # newest first within a category
    folded: list[Path] = field(default_factory=list)              # to promote into a release
    stale: list[Path] = field(default_factory=list)               # already in [Unreleased]
    errors: list[str] = field(default_factory=list)


def collect(root: Path) -> Collected:
    """Classify the fragments under ``root`` without writing anything.

    A missing or unreadable CHANGELOG.md is an error entry, never an empty result: "no
    fragments" and "could not look" must not read alike.
    """
    try:
        _, changelog = _read(root)
        fold, stale, errors = classify(root, changelog)
    except ChangelogError as exc:
        return Collected(errors=[str(exc)])
    merged: dict[str, list[str]] = {}
    for _, sections in fold:
        for category, blocks in sections.items():
            merged.setdefault(category, []).extend(blocks)
    return Collected(merged, [p for p, _ in fold], stale, errors)


def render_section(version: str, date: str, sections: dict[str, list[str]]) -> str:
    """The versioned release section the fragments become, in canonical category order.

    Same ``## [version] — date`` header and trailing newline as helpers.release's renderer, so
    it goes through ``insert_changelog_release`` unchanged. Pure.
    """
    lines = [f"## [{version.lstrip('v')}] — {date}", ""]
    for category in CATEGORIES:
        if sections.get(category):
            lines += [f"### {category}"] + [ln for b in sections[category] for ln in b.split("\n")] + [""]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _read(root: Path) -> tuple[Path, str]:
    path = root / "CHANGELOG.md"
    try:
        return path, path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ChangelogError(f"cannot read {path}: {exc}") from exc


def _write_atomically(root: Path, path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".CHANGELOG.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build(root: Path, write: bool) -> BuildResult:
    """Validate everything first; write only if ``write``; delete only after the write."""
    path, changelog = _read(root)
    fold, stale, errors = classify(root, changelog)
    if errors:
        raise ChangelogError("invalid fragments, nothing changed:\n  " + "\n  ".join(errors))
    text = render(changelog, [sections for _, sections in fold])
    result = BuildResult(text, [p for p, _ in fold], stale)
    if write:
        if fold:
            _write_atomically(root, path, text)
        _delete_all(result.folded + result.stale)
    return result


def _delete_all(paths: list[Path]) -> None:
    """Delete every path it can; name the survivors. A re-run removes them (they read as stale)."""
    left = []
    for p in paths:
        try:
            p.unlink()
        except OSError as exc:
            left.append(f"{FRAGMENT_DIR}/{p.name} ({exc})")
    if left:
        raise ChangelogError(
            "CHANGELOG.md was updated but these fragments could not be deleted:\n  " + "\n  ".join(left)
            + "\nRun build again once they are free; they are recognised as already folded and only removed."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a template fragment (fails `check` until edited)")
    new.add_argument("slug")
    check = sub.add_parser("check", help="validate fragments; read-only")
    check.add_argument("--no-pending", action="store_true", help="also fail if any fragment exists")
    sub.add_parser("preview", help="print the CHANGELOG.md `build` would write; read-only")
    sub.add_parser("build", help="fold fragments into [Unreleased] and delete them; no git changes")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        return _run(args, root)
    except ChangelogError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, root: Path) -> int:
    if args.command == "new":
        print(new_fragment(root, args.slug).relative_to(root).as_posix())
    elif args.command == "check":
        errors = validate(root, _read(root)[1])
        pending = fragment_paths(root)
        if args.no_pending and pending:
            errors += [f"{FRAGMENT_DIR}/{p.name}: pending; run scripts/changelog.py build" for p in pending]
        if errors:
            print("changelog: problems\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        print(f"changelog: {len(pending)} fragment(s) ok")
    elif args.command == "preview":
        sys.stdout.buffer.write(build(root, write=False).text.encode("utf-8"))
    else:
        done = build(root, write=True)
        n = len(done.folded)
        print(f"changelog: folded {n} fragment(s)" + (f", removed {len(done.stale)} already folded" if done.stale else "")
              if n or done.stale else "changelog: no pending fragments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
