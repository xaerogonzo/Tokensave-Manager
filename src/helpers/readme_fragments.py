"""README fragments: a PR adds a file under ``readme.d/``, never a line of README.md.

    python scripts/readme_fragments.py targets                  # the sections a fragment may name
    python scripts/readme_fragments.py new <slug> <target>      # create readme.d/<stamp>-<slug>-<hex>.md
    python scripts/readme_fragments.py check                    # validate every fragment (read-only)
    python scripts/readme_fragments.py check --no-pending       # release gate: no fragment may remain
    python scripts/readme_fragments.py preview                  # print the README.md ``build`` would write
    python scripts/readme_fragments.py build                    # fold fragments into README.md, delete them

Why: README.md is one long file with a hand-kept section per feature, and parallel PRs
that each describe their feature in the same section conflict on it (it happened on a
PR that documented the Tasks tab while another did the same). Two files with different
names cannot conflict. It is the changelog-fragment design (``changelog_fragments``)
applied to a document whose entries belong in sections rather than in one list.

A fragment's FIRST line names the section, by the key of its ``<!-- help:KEY -->``
anchor in README.md, and the rest is Markdown appended to the END of that section::

    <!-- readme: tasks-tab -->
    A paragraph, a list, a table. Normal README prose.

Contracts worth knowing:

  * Filename ``YYYYMMDDHHMM-<slug>-<4 hex>.md`` (UTC), the same as changelog fragments. Several
    fragments for one section are appended oldest first. A ``.md`` file in ``readme.d`` that does
    not match the pattern is an ERROR: it would otherwise never be folded and nobody would know.
  * A section ends before the first ``---`` rule that is followed by an anchor, a ``#``/``##``
    heading or the end of the file (so a footer such as ``## Credits`` stays below the text), else
    before the next anchor. The rule and the blank lines around it stay where they were. README.md's
    own line endings are kept.
  * A fragment may not hold a level-1 or level-2 heading (it would start a new section the anchors
    do not know about), an anchor comment, a conflict marker or an unclosed fence.
  * ``build`` validates everything first, writes README.md completely, and only then deletes
    fragments. If a deletion fails, a re-run recognises a fragment whose paragraphs are ALL already
    in README.md as folded and only removes it. A fragment that is only partly present is an
    error: that is a hand edit, not a crash.
  * Until the fold runs (at release preparation) README.md, and therefore the in-app Help in a
    checkout, does not show a pending fragment. That is the price of never conflicting. The Release
    Wizard runs the fold itself (``apply_readme``, then ``git rm`` of the fragments, in one commit
    with the CHANGELOG), so ``build`` is for folding by hand outside a release.
  * Nothing enforces that a PR adds a fragment. ``check`` validates the ones that exist.

Pure functions plus file IO, standard library only, no UI imports.
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

from helpers.changelog_fragments import FRAGMENT_NAME, slugify

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_DIR = "readme.d"
README = "README.md"

TARGET_LINE = re.compile(r"^<!--\s*readme:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*-->\s*$")
ANCHOR_LINE = re.compile(r"^<!--\s*help:([a-z0-9]+(?:-[a-z0-9]+)*)\s*-->\s*$")
_HEADING = re.compile(r"^#{1,2}\s")
_CONFLICT = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")

#: ``new`` writes this; ``check`` rejects it, so an unedited template cannot reach master.
PLACEHOLDER = "REPLACE ME"


class ReadmeFragmentError(ValueError):
    """The request cannot be carried out."""


@dataclass
class Fragment:
    path: Path
    target: str
    body: str


@dataclass
class BuildResult:
    """What ``build`` did (or, with ``write=False``, would do)."""

    text: str
    folded: list[Path] = field(default_factory=list)   # inserted into README.md
    stale: list[Path] = field(default_factory=list)    # already in README.md; only removed


@dataclass
class Inspection:
    """A read-only look at ``readme.d`` for a caller that must decide before it writes anything."""

    fold: list[Path] = field(default_factory=list)
    stale: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[Path]:
        return self.fold + self.stale


def _norm(text: str) -> str:
    return " ".join(text.split())


def _lines(text: str) -> list[str]:
    return text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")


def _is_fence(line: str) -> bool:
    return line.strip().startswith(("```", "~~~"))


def targets(readme_text: str) -> list[str]:
    """The sections a fragment may name: the keys of README.md's ``<!-- help:KEY -->`` anchors."""
    return [m.group(1) for line in _lines(readme_text) if (m := ANCHOR_LINE.match(line))]


def fragment_paths(root: Path) -> list[Path]:
    """Real fragments, oldest first (filename order)."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    return [directory / n for n in sorted(p.name for p in directory.iterdir()
                                          if FRAGMENT_NAME.fullmatch(p.name))]


def misnamed(root: Path) -> list[Path]:
    """Markdown files in ``readme.d`` (other than its README) that no ``build`` would ever fold."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    return [directory / p.name for p in sorted(directory.iterdir())
            if p.suffix == ".md" and p.name != "README.md" and not FRAGMENT_NAME.fullmatch(p.name)]


def new_fragment(root: Path, raw_slug: str, target: str, now: datetime | None = None) -> Path:
    """Create a template fragment with an exclusive open, so it can never overwrite one."""
    slug = slugify(raw_slug)
    known = targets(_read(root)[1])
    if target not in known:
        raise ReadmeFragmentError(
            f"{target!r} is not a README section. The sections are:\n  " + "\n  ".join(known))
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M")
    directory = root / FRAGMENT_DIR
    directory.mkdir(exist_ok=True)
    for _ in range(50):
        path = directory / f"{stamp}-{slug}-{secrets.token_hex(2)}.md"
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(f"<!-- readme: {target} -->\n{PLACEHOLDER}: what a reader of this section "
                             "should now know.\n")
        except FileExistsError:
            continue
        return path
    raise ReadmeFragmentError("could not find an unused fragment name")  # pragma: no cover


def parse_fragment(path: Path, text: str) -> tuple[Fragment | None, list[str]]:
    """`(fragment, problems)`. A fragment with problems is returned as None."""
    name = f"{FRAGMENT_DIR}/{path.name}"
    lines = _lines(text)
    while lines and not lines[0].strip():
        lines.pop(0)
    problems: list[str] = []
    match = TARGET_LINE.match(lines[0]) if lines else None
    if not match:
        return None, [f"{name}: the first line must be `<!-- readme: <section> -->`"]
    body_lines = lines[1:]
    fenced = False
    for number, line in enumerate(body_lines, start=2):
        if _is_fence(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if _HEADING.match(line):
            problems.append(f"{name}:{number}: a level-1 or level-2 heading would start a section the "
                            "anchors do not know about; use `###` or lower")
        if ANCHOR_LINE.match(line) or TARGET_LINE.match(line):
            problems.append(f"{name}:{number}: an anchor comment belongs to README.md, not a fragment")
        if _CONFLICT.match(line):
            problems.append(f"{name}:{number}: a merge conflict marker")
    if fenced:
        problems.append(f"{name}: a code fence is never closed")
    body = "\n".join(body_lines).strip("\n")
    if not body.strip():
        problems.append(f"{name}: there is no text after the first line")
    if PLACEHOLDER in body:
        problems.append(f"{name}: still has the template placeholder `{PLACEHOLDER}`")
    if problems:
        return None, problems
    return Fragment(path, match.group(1), body), []


def _paragraphs(body: str) -> list[str]:
    return [p for p in (_norm(chunk) for chunk in re.split(r"\n\s*\n", body)) if p]


def classify(root: Path, readme_text: str) -> tuple[list[Fragment], list[Fragment], list[str]]:
    """`(to fold, already folded, errors)` over every fragment."""
    errors = [f"{FRAGMENT_DIR}/{p.name}: not named YYYYMMDDHHMM-<slug>-<4 hex>.md, so build would "
              "never fold it" for p in misnamed(root)]
    known = set(targets(readme_text))
    readme_norm = _norm(readme_text)
    fold: list[Fragment] = []
    stale: list[Fragment] = []
    seen: dict[str, Path] = {}
    for path in fragment_paths(root):
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{FRAGMENT_DIR}/{path.name}: cannot read it: {exc}")
            continue
        fragment, problems = parse_fragment(path, text)
        if fragment is None:
            errors += problems
            continue
        name = f"{FRAGMENT_DIR}/{path.name}"
        if fragment.target not in known:
            errors.append(f"{name}: `{fragment.target}` is not a README section "
                          "(run `targets` for the list)")
            continue
        key = _norm(fragment.body)
        if key in seen:
            errors.append(f"{name}: says the same as {FRAGMENT_DIR}/{seen[key].name}")
            continue
        seen[key] = path
        present = [p in readme_norm for p in _paragraphs(fragment.body)]
        if present and all(present):
            stale.append(fragment)
        elif any(present):
            errors.append(f"{name}: only some of its paragraphs are already in README.md; "
                          "that is a hand edit. Remove the duplicated paragraphs or the fragment")
        else:
            fold.append(fragment)
    return fold, stale, errors


def validate(root: Path, readme_text: str) -> list[str]:
    return classify(root, readme_text)[2]


def _section_end(lines: list[str], start: int) -> int:
    """Index at which a section ends: before the first `---` rule (outside a code fence) that is
    followed by an anchor, a level-1 or level-2 heading or the end of the file, else before the next
    anchor. Blank lines before that point are not part of the section's text.

    A rule alone is not the end: READMEs use `---` between ideas inside a section, so only a rule
    that precedes something which starts a new part counts. `## Credits` after the last section
    is the case that made this necessary (it has no anchor, and a fragment must not land below it).
    """
    stop = next((i for i in range(start + 1, len(lines)) if ANCHOR_LINE.match(lines[i])), len(lines))
    fenced = False
    end = stop
    for i in range(start + 1, stop):
        if _is_fence(lines[i]):
            fenced = not fenced
        elif not fenced and lines[i].strip() == "---":
            after = next((lines[j] for j in range(i + 1, len(lines)) if lines[j].strip()), None)
            if after is None or ANCHOR_LINE.match(after) or _HEADING.match(after):
                end = i
                break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _insert_at_section_end(lines: list[str], target: str, body_lines: list[str]) -> list[str]:
    """Insert `body_lines` at the end of `target`'s section."""
    start = next(i for i, line in enumerate(lines) if (m := ANCHOR_LINE.match(line)) and m.group(1) == target)
    end = _section_end(lines, start)
    return lines[:end] + [""] + body_lines + lines[end:]


def render(readme_text: str, fold: list[Fragment]) -> str:
    """README.md with every fragment appended to its section, oldest first. Line endings kept."""
    eol = "\r\n" if "\r\n" in readme_text else "\n"
    bom = "\ufeff" if readme_text.startswith("\ufeff") else ""
    lines = _lines(readme_text)
    for fragment in fold:
        lines = _insert_at_section_end(lines, fragment.target, fragment.body.split("\n"))
    return bom + eol.join(lines)


def _read(root: Path) -> tuple[Path, str]:
    path = root / README
    try:
        return path, path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ReadmeFragmentError(f"cannot read {path}: {exc}") from exc


def _write_atomically(root: Path, path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".README.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def inspect(root: Path) -> Inspection:
    """What ``build`` would do, without doing it. No ``readme.d`` is an empty inspection; an unreadable
    README.md with fragments present is an error, never silence."""
    if not (root / FRAGMENT_DIR).is_dir():
        return Inspection()
    try:
        readme = _read(root)[1]
    except ReadmeFragmentError as exc:
        return Inspection(errors=[str(exc)])
    fold, stale, errors = classify(root, readme)
    return Inspection([f.path for f in fold], [f.path for f in stale], errors)


def _plan(root: Path) -> tuple[Path, BuildResult]:
    path, readme = _read(root)
    fold, stale, errors = classify(root, readme)
    if errors:
        raise ReadmeFragmentError("invalid fragments, nothing changed:\n  " + "\n  ".join(errors))
    return path, BuildResult(render(readme, fold), [f.path for f in fold], [f.path for f in stale])


def apply_readme(root: Path) -> BuildResult:
    """Validate everything, then write README.md. Delete nothing: the caller removes the fragments
    (the Release Wizard does it with ``git rm``, in the same commit as the README).

    Idempotent: afterwards the fragments read as already folded, so a second call changes nothing.
    """
    path, result = _plan(root)
    if result.folded:
        _write_atomically(root, path, result.text)
    return result


def build(root: Path, write: bool) -> BuildResult:
    """Validate everything first; write only if ``write``; delete only after the write."""
    if not write:
        return _plan(root)[1]
    result = apply_readme(root)
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
        raise ReadmeFragmentError(
            "README.md was updated but these fragments could not be deleted:\n  " + "\n  ".join(left)
            + "\nRun build again once they are free; they are recognised as already folded and only removed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("targets", help="list the README sections a fragment may name")
    new = sub.add_parser("new", help="create a template fragment (fails `check` until edited)")
    new.add_argument("slug")
    new.add_argument("target", help="a section key from `targets`")
    check = sub.add_parser("check", help="validate fragments; read-only")
    check.add_argument("--no-pending", action="store_true", help="also fail if any fragment exists")
    sub.add_parser("preview", help="print the README.md `build` would write; read-only")
    sub.add_parser("build", help="fold fragments into README.md and delete them; no git changes")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        return _run(args, root)
    except ReadmeFragmentError as exc:
        print(f"readme: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, root: Path) -> int:
    if args.command == "targets":
        print("\n".join(targets(_read(root)[1])))
    elif args.command == "new":
        print(new_fragment(root, args.slug, args.target).relative_to(root).as_posix())
    elif args.command == "check":
        errors = validate(root, _read(root)[1])
        pending = fragment_paths(root)
        if args.no_pending and pending:
            errors += [f"{FRAGMENT_DIR}/{p.name}: pending; run scripts/readme_fragments.py build" for p in pending]
        if errors:
            print("readme: problems\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        print(f"readme: {len(pending)} fragment(s) ok")
    elif args.command == "preview":
        sys.stdout.buffer.write(build(root, write=False).text.encode("utf-8"))
    else:
        done = build(root, write=True)
        n = len(done.folded)
        print(f"readme: folded {n} fragment(s)" + (f", removed {len(done.stale)} already folded" if done.stale else "")
              if n or done.stale else "readme: no pending fragments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
