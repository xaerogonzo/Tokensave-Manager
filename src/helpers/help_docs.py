"""In-application help, read from the repository's own markdown.

THE POINT IS THAT THERE IS ONLY ONE COPY. Help written into Python goes stale
against the documentation the moment either is edited, and nothing catches it
because both are prose. Measured before this module existed: the Help tab was
72 KB of hand-written Python across 25 topics and never mentioned **10 of 19**
current features -- Agent Policy, Housekeeping, Savings, Test Gaps,
Cross-project search, Workspace builder, Private repos, Release Wizard, Scrub
history, Cursor. `README.md` mentioned 12 of 14 of the same list. The
documentation was already better than the help; it just was not the help.

So the shipped documents ARE the help. A documentation sweep updates what the
application shows, with no second place to remember.

Ported from OpenChem Studio's `src/openchem/help.py`, which is the same design
proven on a different toolkit. Its central decision comes across intact:

TOPICS ARE KEYED FROM THE MARKDOWN, NOT FROM A TABLE HERE. Each topic is an
HTML comment placed immediately above a heading:

    <!-- help:right-click-menu -->
    ## Right-Click Menu

The obvious alternative -- a registry here mapping `"right-click-menu"` to the
heading text -- breaks silently the first time somebody rewords a heading,
which is exactly the edit a documentation sweep makes. With the key in the
document the heading can be rewritten freely and the anchor travels with it.
HTML comments render as nothing on GitHub, so the anchors cost a reader
nothing.

THE OWNERSHIP BOUNDARY, so it is not mistaken later:

    Python   owns WHICH DOCUMENTS are user-facing help sources  (HELP_DOCUMENTS)
    Markdown owns WHAT TOPICS exist inside them                 (<!-- help:key -->)

A list of documents has no silent-staleness failure mode; a list of headings
does.

WHAT THIS TIGHTENS OVER THE ORIGINAL. OpenChem logs a warning and carries on
when a document is missing or a key is duplicated. Here a missing document is
reported as an explicit `Problem` the UI shows, because a quietly shorter topic
list is the "unknown is never false" failure this codebase keeps writing rules
about -- a reader cannot tell a document that has nothing to say from one that
failed to load. Duplicate keys are a test failure; at run time the first wins,
deterministically, and says so.
"""

from __future__ import annotations

import dataclasses
import os
import re

from constants import _BASE_DIR

#: Searched in this order, which is also the order topics are listed.
#:
#: Deliberately NOT every markdown file in the repository. ARCHITECTURE,
#: ROADMAP, VERIFICATION, CHANGELOG and the gotcha notes are written for people
#: working ON the Manager rather than with it, and putting them here would bury
#: the documents that answer a user's question. That is the line this tuple
#: draws, and the only place it is drawn.
#:
#: TOKENSAVE_GUIDE.md is about the tokensave CLI rather than this Manager --
#: a different program, still a user's question.
HELP_DOCUMENTS = (
    "README.md",
    os.path.join("docs", "USER_GUIDE.md"),
    os.path.join("docs", "GITHUB_GUIDE.md"),
    os.path.join("docs", "RELOCATING.md"),
    os.path.join("docs", "UPGRADE_INTEGRATION.md"),
    os.path.join("docs", "PYSCOPE_INTEGRATION.md"),
    "TOKENSAVE_GUIDE.md",
)

#: Runtime values a topic may interpolate, as `{{name}}`. An ALLOWLIST: a
#: placeholder outside it is a typo, and `tests/test_help_docs.py` fails on
#: one rather than letting `{{template_dri}}` ship as broken help.
#:
#: Three topics need this -- file locations, the settings reference and about
#: -- and the alternative was keeping three Python topics beside the markdown
#: ones, which is most of the staleness problem preserved for three files.
PLACEHOLDER_KEYS = (
    "template_dir",
    "install_dir",
    "config_path",
    "log_file",
    "app_version",
    "wrapper_path",
)

_ANCHOR = re.compile(r"^<!--\s*help:([a-z0-9-]+)\s*-->\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


@dataclasses.dataclass(frozen=True)
class HelpTopic:
    key: str
    #: Taken from the heading the anchor sits above, so the sidebar and the
    #: document can never disagree about what a section is called.
    title: str
    document: str
    level: int


@dataclasses.dataclass(frozen=True)
class Problem:
    """Something wrong with the corpus, stated rather than swallowed."""

    document: str
    detail: str


class HelpUnavailable(RuntimeError):
    """A topic or document that should be there is not."""


def docs_directory() -> str:
    """Where the corpus lives: the installation root.

    One location rather than the original's two candidates, because this
    project already has a constant for exactly this question -- `_BASE_DIR` is
    the repo root in a checkout and the directory beside the exe in a build,
    which is where `build.ps1` puts `README.md` and `docs\\`.
    """
    return _BASE_DIR


def document_path(document: str) -> str:
    return os.path.join(docs_directory(), document)


def _read(document: str) -> "tuple | None":
    """The document's lines, or None when it cannot be read."""
    try:
        with open(document_path(document), encoding="utf-8-sig") as handle:
            return tuple(handle.read().splitlines())
    except OSError:
        return None


def _next_heading(lines, start: int):
    """The first heading at or after `start`, skipping blank lines only.

    Anything else between the anchor and a heading means the anchor is not
    labelling that heading, so it is not treated as one.
    """
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        match = _HEADING.match(line)
        return (len(match.group(1)), match.group(2)) if match else None
    return None


def scan() -> "tuple":
    """`(topics, problems)` for the whole corpus.

    Not cached. The original caches because its documents cannot change under
    a running application; here they are the repository's own files, and the
    person most likely to have Help open is the one editing them. Reading six
    files costs microseconds and removes a `reload()` somebody has to remember
    to call.
    """
    topics, problems, seen = [], [], {}
    for document in HELP_DOCUMENTS:
        lines = _read(document)
        if lines is None:
            # Reported, never silently skipped: a shorter topic list is
            # indistinguishable from a document with nothing to say.
            problems.append(Problem(document, "could not be read"))
            continue
        for index, line in enumerate(lines):
            match = _ANCHOR.match(line)
            if match is None:
                continue
            key = match.group(1)
            heading = _next_heading(lines, index)
            if heading is None:
                problems.append(Problem(
                    document, "anchor %r is not above a heading" % key))
                continue
            if key in seen:
                problems.append(Problem(
                    document,
                    "duplicate key %r (already in %s); keeping the first"
                    % (key, seen[key])))
                continue
            seen[key] = document
            level, title = heading
            topics.append(HelpTopic(key=key, title=title, document=document,
                                    level=level))
    return tuple(topics), tuple(problems)


def topics() -> "tuple":
    """Every anchored topic, in document order then anchor order."""
    return scan()[0]


def problems() -> "tuple":
    return scan()[1]


def topic(key: str) -> HelpTopic:
    for candidate in topics():
        if candidate.key == key:
            return candidate
    raise HelpUnavailable("No help topic keyed %r" % key)


def topic_markdown(key: str) -> str:
    """The section a topic anchors, heading included.

    The section ends at the next heading of the SAME OR HIGHER level, so
    `## Git Workflow` carries its `### Committing` subsection along with it
    while `### Committing` on its own does not swallow the section that
    follows. Sub-topics being readable both alone and as part of their parent
    is what makes per-section help useful.
    """
    found = topic(key)
    lines = _read(found.document)
    if lines is None:
        raise HelpUnavailable("Help document could not be read: %s"
                              % found.document)
    body, collecting = [], False
    for line in lines:
        if not collecting:
            match = _ANCHOR.match(line)
            if match is not None and match.group(1) == key:
                collecting = True
            continue
        heading = _HEADING.match(line)
        if heading is not None and len(heading.group(1)) <= found.level and body:
            break
        # Anchors are markup for this module, not content for the reader.
        if _ANCHOR.match(line) is None:
            body.append(line)
    return "\n".join(body).strip()


def document_markdown(document: str) -> str:
    """A whole document, with the anchors stripped."""
    if document not in HELP_DOCUMENTS:
        raise HelpUnavailable("Not a help document: %s" % document)
    lines = _read(document)
    if lines is None:
        raise HelpUnavailable("Help document could not be read: %s" % document)
    return "\n".join(line for line in lines
                     if _ANCHOR.match(line) is None).strip()


# ── Placeholders ──────────────────────────────────────────────────────────


def placeholders_in(text: str) -> "tuple":
    """Every `{{name}}` the text uses, in order of first appearance."""
    out = []
    for name in _PLACEHOLDER.findall(text or ""):
        if name not in out:
            out.append(name)
    return tuple(out)


def unknown_placeholders(text: str) -> "tuple":
    """Placeholders outside the allowlist. A typo, caught by a test."""
    return tuple(n for n in placeholders_in(text) if n not in PLACEHOLDER_KEYS)


def substitute(text: str, values: dict) -> str:
    """Fill allowlisted placeholders. Applied AFTER section extraction.

    Scoped to the extracted section rather than run globally over a document,
    so a brace sequence elsewhere in a 89 KB README cannot be touched.

    It DOES apply inside fenced code, deliberately: the file-locations topic
    is a list of paths shown in monospace, which is most of the reason
    placeholders exist here at all.

    A known key with no value renders "unknown" -- never an empty string,
    which would silently produce a sentence that reads as though there were
    nothing to say.
    """
    def _one(match):
        name = match.group(1)
        if name not in PLACEHOLDER_KEYS:
            return match.group(0)     # left visible; a test fails on it
        value = values.get(name)
        return str(value) if value else "unknown"

    return _PLACEHOLDER.sub(_one, text or "")


# ── Search ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SearchHit:
    topic: HelpTopic
    #: How many times the query appears in the section body. Ranks results,
    #: and tells a reader whether a topic mentions the term once in passing or
    #: is actually about it.
    occurrences: int
    #: True when the query is in the heading. Ranked above body matches:
    #: someone typing "gitignore" wants the Gitignore section first, not the
    #: scaffolding section that mentions it twice.
    in_title: bool
    #: One line of context, so a hit whose title looks unrelated explains
    #: itself without being opened.
    snippet: str


def search(query: str) -> "tuple":
    """Topics matching `query`, best first.

    Searches SECTION BODIES, not just headings. Title-only filtering answers
    "what is this feature called", which is the question a reader who already
    knows the answer would ask. The real one is "what does it say about
    worktrees" -- a word in no heading here and in the text of several
    sections.

    Plain case-insensitive substring matching, deliberately: this is a few
    thousand lines of prose, so anything cleverer would add ways to be
    surprised without adding reach.

    The ranking is a contract, not an accident: title match, then occurrence
    count descending, then corpus order. Asserted by `tests/test_help_docs.py`.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return ()
    found = topics()
    order = {t.key: i for i, t in enumerate(found)}
    hits = []
    for item in found:
        body = topic_markdown(item.key)
        occurrences = body.lower().count(needle)
        in_title = needle in item.title.lower()
        if not occurrences and not in_title:
            continue
        hits.append(SearchHit(topic=item, occurrences=occurrences,
                              in_title=in_title,
                              snippet=_snippet(body, needle)))
    hits.sort(key=lambda hit: (not hit.in_title, -hit.occurrences,
                               order[hit.topic.key]))
    return tuple(hits)


def _snippet(body: str, needle: str, width: int = 90) -> str:
    """The first line containing `needle`, trimmed around the match.

    Line-based rather than a character window around the offset, because a
    fixed window cuts words in half at both ends and reads like corruption.
    """
    for line in body.splitlines():
        position = line.lower().find(needle)
        if position < 0:
            continue
        stripped = line.strip("#> ").strip()
        if len(stripped) <= width:
            return stripped
        position = max(stripped.lower().find(needle), 0)
        start = max(0, position - width // 3)
        end = min(len(stripped), start + width)
        return (("..." if start else "") + stripped[start:end].strip()
                + ("..." if end < len(stripped) else ""))
    return ""
