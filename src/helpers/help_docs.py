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

WHERE THE TEXT COMES FROM, which is the one decision this module makes about
delivery. A SOURCE CHECKOUT reads the repository's own files, every time: the
person most likely to have Help open is the person editing the documents. A
FROZEN BUILD reads a bundle compiled into the executable and NEVER falls back
to disk, so a release's help is read-only and cannot drift from the build:
`dist\\docs\\` is a human copy the application does not read, and a save there
used to be able to drop an anchor and leave a topic empty. `build.ps1` generates
the bundle from `HELP_DOCUMENTS` (`scripts/build_help_bundle.py`), so there is
still exactly one list of documents. A frozen build whose bundle is missing,
altered or short says so as a `Problem` per document; it does not quietly show
fewer topics. The digest detects accidental damage only: it rides inside the
same unsigned executable, so it is not authenticity.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import html
import importlib
import json
import os
import re
import stat
import tempfile
import zlib

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


# ── Where the text comes from ─────────────────────────────────────────────

SOURCE_DISK = "disk"
SOURCE_BUNDLE = "bundle"

#: The generated module `build.ps1` compiles in. Absent from a checkout (it is
#: gitignored), and ignored there even if a stale copy is lying around.
BUNDLE_MODULE = "_help_bundle"
BUNDLE_SCHEMA = 1
#: The compiled HTML page rides in the same generated module as its own literals (`PAGES_*`), with
#: its own digest, so the markdown payload above keeps its shape.
PAGES_SCHEMA = 1
PAGE_NAME = "help.html"


class BundleError(RuntimeError):
    """The bundle cannot be used, and says why."""


def _frozen() -> bool:
    """True in a packaged build. The same signal `constants._resolve_base_dir`
    uses; a function so a test can stand in for a build."""
    return bool(os.environ.get("NUITKA_ONEFILE_PARENT"))


def source() -> str:
    return SOURCE_BUNDLE if _frozen() else SOURCE_DISK


def _doc_key(document: str) -> str:
    """The bundle's spelling of a document path: forward slashes on every platform."""
    return document.replace(os.sep, "/").replace("\\", "/")


def encode_bundle(documents: dict) -> "tuple[int, str, str]":
    """`(schema, digest, payload)` for `{document: text}`. Pure.

    The payload is base64 of zlib of JSON, so it is a plain string literal in a
    generated module: no code runs on import, and nothing but text is trusted.
    """
    raw = json.dumps({_doc_key(k): v for k, v in documents.items()},
                     ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return BUNDLE_SCHEMA, hashlib.sha256(payload.encode("ascii")).hexdigest(), payload


def encode_pages(pages: dict) -> "tuple[int, str, str]":
    """`(schema, digest, payload)` for `{name: html}`, packed exactly as the documents are."""
    raw = json.dumps(pages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return PAGES_SCHEMA, hashlib.sha256(payload.encode("ascii")).hexdigest(), payload


def decode_pages(schema, digest, payload) -> dict:
    """The `{name: html}` a bundle holds, or `BundleError` saying what is wrong."""
    if schema != PAGES_SCHEMA:
        raise BundleError("pages schema %r, expected %r" % (schema, PAGES_SCHEMA))
    return _unpack(digest, payload, "pages")


def decode_bundle(schema, digest, payload) -> dict:
    """The `{document: text}` a bundle holds, or `BundleError` saying what is wrong."""
    if schema != BUNDLE_SCHEMA:
        raise BundleError("bundle schema %r, expected %r" % (schema, BUNDLE_SCHEMA))
    return _unpack(digest, payload, "bundle")


def _unpack(digest, payload, what: str) -> dict:
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise BundleError("%s fields are not text" % what)
    if hashlib.sha256(payload.encode("ascii", "replace")).hexdigest() != digest:
        raise BundleError("%s digest does not match its payload (damaged)" % what)
    try:
        data = json.loads(zlib.decompress(base64.b64decode(payload.encode("ascii"))).decode("utf-8"))
    except (ValueError, zlib.error) as exc:
        raise BundleError("%s payload cannot be decoded: %s" % (what, exc)) from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise BundleError("%s payload is not a mapping of names to text" % what)
    return data


def source_documents() -> dict:
    """`{document: text}` for every help document, read from disk.

    The generator's input. Raises `BundleError` for a document that is missing,
    empty or not UTF-8: a bundle built from fewer documents is a smaller Help,
    silently, and the build must stop instead.
    """
    out, bad = {}, []
    for document in HELP_DOCUMENTS:
        try:
            with open(document_path(document), encoding="utf-8-sig") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            bad.append("%s: %s" % (document, exc))
            continue
        if not text.strip():
            bad.append("%s: is empty" % document)
            continue
        out[document] = text
    if bad:
        raise BundleError("cannot build the help bundle: " + "; ".join(bad))
    return out


def _import_bundle():
    """The generated module, or ImportError. A seam, so tests need no build."""
    return importlib.import_module(BUNDLE_MODULE)


_bundle_cache: "list" = []


def reset_cache() -> None:
    """Forget the decoded bundle. A frozen build never needs this; a test does."""
    del _bundle_cache[:]
    del _pages_cache[:]


def _bundle_documents() -> "tuple[dict | None, str]":
    """`(documents, reason)`: the decoded bundle, or None and why not. Cached."""
    if _bundle_cache:
        return _bundle_cache[0]
    try:
        module = _import_bundle()
        result = (decode_bundle(getattr(module, "SCHEMA", None), getattr(module, "DIGEST", None),
                                getattr(module, "PAYLOAD", None)), "")
    except ImportError:
        result = (None, "this build has no help bundle (%s was not compiled in)" % BUNDLE_MODULE)
    except BundleError as exc:
        result = (None, str(exc))
    _bundle_cache.append(result)
    return result


_pages_cache: "list" = []


def _bundle_pages() -> "tuple[dict | None, str]":
    """`(pages, reason)`: the compiled HTML the bundle holds, or None and why not. Cached."""
    if _pages_cache:
        return _pages_cache[0]
    try:
        module = _import_bundle()
        if not hasattr(module, "PAGES_PAYLOAD"):
            raise BundleError("this build has no compiled help page (the bundle predates it)")
        result = (decode_pages(getattr(module, "PAGES_SCHEMA", None), getattr(module, "PAGES_DIGEST", None),
                               getattr(module, "PAGES_PAYLOAD", None)), "")
    except ImportError:
        result = (None, "this build has no help bundle (%s was not compiled in)" % BUNDLE_MODULE)
    except BundleError as exc:
        result = (None, str(exc))
    _pages_cache.append(result)
    return result


def _unreadable_reason(document: str) -> str:
    """Why `_read` returned None, for the Problem that states it."""
    if _frozen():
        documents, reason = _bundle_documents()
        return reason if documents is None else "is not in the help bundle"
    return "could not be read"


def can_view(document: str) -> bool:
    """Is there something to open for `document`? Makes no file, so it is cheap to ask on every click."""
    if not _frozen():
        return os.path.isfile(document_path(document))
    documents, _ = _bundle_documents()
    return documents is not None and _doc_key(document) in documents


def viewable_path(document: str) -> "str | None":
    """A file a person can open to read `document`.

    In a checkout that is the real file (they edit the documents). In a build it
    is a READ-ONLY temporary copy of what the bundle holds, never `dist\\docs\\`:
    opening that one would invite an edit that changes nothing Help shows.
    """
    if not _frozen():
        path = document_path(document)
        return path if os.path.isfile(path) else None
    documents, _ = _bundle_documents()
    text = None if documents is None else documents.get(_doc_key(document))
    if text is None:
        return None
    folder = tempfile.mkdtemp(prefix="tokensave-manager-help-")
    path = os.path.join(folder, os.path.basename(document))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.chmod(path, stat.S_IREAD)
    return path


# ── The compiled HTML page ────────────────────────────────────────────────

def compile_page(documents: "dict | None" = None, version: str = ""):
    """The whole corpus as one `markdown_html.Page`. The generator's job and a checkout's preview.

    A link into the corpus that points at nothing (a heading that was renamed, a document that was
    removed) stops the compile: a page that quietly drops a link is the shorter-Help failure again.
    """
    from helpers import markdown_html
    page = markdown_html.build_page(source_documents() if documents is None else documents, HELP_DOCUMENTS,
                                    title="TokenSave Manager Help", version=version)
    if page.dead_links:
        raise BundleError("the help page has links into the help that point at nothing: " + "; ".join(
            "%s -> %s" % pair for pair in page.dead_links[:8]))
    return page


def static_values(version: str) -> dict:
    """What `{{name}}` says in the copy shipped in the docs folder, which has no machine to ask.

    Neutral descriptions, never a developer's own paths: the page is compiled on the build machine.
    """
    return {
        "template_dir": "the templates folder beside the program",
        "install_dir": "the folder TokenSave Manager is installed in",
        "config_path": "%LOCALAPPDATA%\\TokenSaveManager\\manager-config.json",
        "log_file": "%LOCALAPPDATA%\\TokenSaveManager\\logs",
        "app_version": version,
        "wrapper_path": "tokensave-wrapper.exe beside the program",
    }


def fill_page(page_html: str, values: dict) -> str:
    """`substitute` for a finished page: allowlisted placeholders only, values HTML-escaped.

    Escaping is the point: a path with `&` or `<` in it must not become markup in a page that is
    opened in a browser.
    """
    def _one(match):
        name = match.group(1)
        if name not in PLACEHOLDER_KEYS:
            return match.group(0)
        value = values.get(name)
        return html.escape(str(value), quote=True) if value else "unknown"

    return _PLACEHOLDER.sub(_one, page_html or "")


def page_html() -> "str | None":
    """The compiled page, or None when there is none to show.

    A build reads the page compiled INTO it and never builds one; a checkout compiles the documents
    on disk, so an edit shows without a build step.
    """
    if _frozen():
        pages, _ = _bundle_pages()
        return None if pages is None else pages.get(PAGE_NAME)
    try:
        from constants import APP_VERSION
        return compile_page(version=APP_VERSION).html
    except (BundleError, OSError):
        return None


def page_problem() -> str:
    """Why the compiled page cannot be served, or "" when it can. Empty means nothing is wrong."""
    if not _frozen():
        return ""
    pages, reason = _bundle_pages()
    if pages is None:
        return reason
    text = pages.get(PAGE_NAME)
    if not text:
        return "the help bundle has no %s" % PAGE_NAME
    gone = [t.key for t in topics() if 'id="help-%s"' % t.key not in text]
    return "the compiled page has no anchor for: " + ", ".join(gone[:5]) if gone else ""


def page_uri(values: dict, key: str = "", document: str = "") -> "str | None":
    """A `file:` URI for a READ-ONLY temporary copy of the page, at a topic or a document.

    Made now, not on every topic click, and never under the docs folder beside the executable: opening that
    would invite an edit that changes nothing Help shows. Placeholders are filled for this machine.
    """
    text = page_html()
    if not text:
        return None
    text = fill_page(text, values)
    folder = tempfile.mkdtemp(prefix="tokensave-manager-help-")
    path = os.path.join(folder, PAGE_NAME)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.chmod(path, stat.S_IREAD)
    fragment = ""
    if key and 'id="help-%s"' % key in text:
        fragment = "help-" + key
    elif document:
        from helpers import markdown_html
        candidate = markdown_html.doc_id(_doc_key(document))
        fragment = candidate if 'id="%s"' % candidate in text else ""
    from pathlib import Path
    return Path(path).as_uri() + ("#" + fragment if fragment else "")


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
    """The document's lines, or None when it cannot be read.

    In a build ONLY the bundle is consulted; in a checkout ONLY the disk is.
    Neither falls back to the other, because a fallback is how a release shows
    text the build was not made from.
    """
    if _frozen():
        documents, _ = _bundle_documents()
        text = None if documents is None else documents.get(_doc_key(document))
        return None if text is None else tuple(text.splitlines())
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
            problems.append(Problem(document, _unreadable_reason(document)))
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


# ── A verdict on the corpus, for a driven run ─────────────────────────────

@dataclasses.dataclass(frozen=True)
class CorpusReport:
    source: str
    documents: int
    topics: int
    problems: "tuple"
    #: Topic keys whose section could not be extracted.
    unresolved: "tuple"
    searchable: bool
    #: Why the compiled HTML page cannot be served in a build; "" when it can (and always in a checkout).
    page_problem: str = ""


def corpus_report() -> CorpusReport:
    """What Help can actually serve right now. Reads through the same paths a user does."""
    found, problems = scan()
    unresolved = []
    for item in found:
        try:
            if not topic_markdown(item.key).strip():
                unresolved.append(item.key)
        except HelpUnavailable:
            unresolved.append(item.key)
    needle = found[0].title.split()[0] if found else ""
    return CorpusReport(source(), len(HELP_DOCUMENTS) - len([p for p in problems if "duplicate" not in p.detail
                                                           and "anchor" not in p.detail]),
                        len(found), problems, tuple(unresolved), bool(needle and search(needle)), page_problem())


def corpus_verdict(report: CorpusReport, frozen: bool) -> "list[str]":
    """Every reason the corpus is not what this kind of run must serve. Empty means it is.

    A build must serve the BUNDLE and a checkout the DISK: the verdict is what
    turns "Help opened" into "Help is the help this build was made from".
    """
    want = SOURCE_BUNDLE if frozen else SOURCE_DISK
    out = []
    if report.source != want:
        out.append("help is read from %r, expected %r" % (report.source, want))
    if report.documents != len(HELP_DOCUMENTS):
        out.append("%d of %d help documents loaded" % (report.documents, len(HELP_DOCUMENTS)))
    out += ["%s: %s" % (p.document, p.detail) for p in report.problems]
    if not report.topics:
        out.append("no topics")
    if report.unresolved:
        out.append("topics with no body: %s" % ", ".join(report.unresolved[:5]))
    if not report.searchable:
        out.append("search found nothing for a word in the first topic's title")
    if frozen and report.page_problem:
        out.append("html page: " + report.page_problem)
    return out
