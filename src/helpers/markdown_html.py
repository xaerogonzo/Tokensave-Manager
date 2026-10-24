"""Compile the Help corpus into ONE self-contained, read-only HTML page.

The Tk renderer (`markdown_tk`) shows the same documents as styled text in a pane that has nowhere to
send a reader: links lose their URL, tables stay monospace, nothing can be printed. A browser can do
all of that, so the release compiles the documents into a page and the Help tab opens it. This module
is that compiler. It is `markdown_tk`'s sibling and shares its SUBSET on purpose: the same constructs
are understood, everything else is literal, and a parity test holds the two to the same words on the
real corpus, so "what the pane says" and "what the page says" cannot drift apart.

WHAT THE PAGE IS, AND IS NOT
  * Standard library only; pure functions (markdown in, text out). No file or network access here.
  * One file: inline CSS, no script, no image, no font, no request. A `Content-Security-Policy` meta
    forbids everything but its own inline style, so the page stays inert even if a document were
    edited to contain something hostile. Every character of document text is HTML-escaped; inline
    HTML in a document is shown as typed, never interpreted.
  * Deterministic: the same documents and version give the same bytes. No timestamps.
  * Read-only is a property of how it is opened (`help_docs.page_path`), not of the HTML.

LINKS, which the Tk pane drops and a page can keep. `[t](#heading)` goes to that heading in the same
document; `[t](OTHER.md)` and `[t](OTHER.md#heading)` go to the other help document when it is in the
corpus; `http(s):` and `mailto:` become real links (`rel="noopener noreferrer"`); everything else --
a source file, `javascript:`, `file:`, a heading that no longer exists -- renders as its text. A link
that points at a help document or heading that is not there is reported as DEAD rather than quietly
turned into text, because a documentation sweep that renames a heading should hear about it.

HEADINGS get GitHub-style slugs, deduplicated per document; a `<!-- help:key -->` anchor above a
heading adds an `id="help-key"` the Help tab can open the page at. Anchors are markup, never content.
"""

from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass, field

_ANCHOR = re.compile(r"^<!--\s*help:([a-z0-9-]+)\s*-->\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_EXTERNAL = ("http", "https", "mailto")

PAGE_NAME = "help.html"


@dataclass
class DocumentResult:
    """One rendered document. `dead` lists links that point into the corpus and found nothing."""
    html: str
    headings: "list[tuple[str, int, str]]" = field(default_factory=list)   # (id, level, plain text)
    topic_ids: "list[str]" = field(default_factory=list)                    # help-<key> anchors
    dead: "list[str]" = field(default_factory=list)


@dataclass
class Page:
    html: str
    documents: int
    topic_ids: "tuple[str, ...]"
    #: `(document, link)` pairs whose target is a help document or heading that does not exist.
    dead_links: "tuple[tuple[str, str], ...]"


# ── Names ─────────────────────────────────────────────────────────────

def slug(text: str) -> str:
    """GitHub's heading slug: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def doc_id(document: str) -> str:
    """The section id of a document: its path, slugged."""
    return "doc-" + re.sub(r"[^a-z0-9]+", "-", document.replace("\\", "/").lower()).strip("-")


def _plain(text: str) -> str:
    """A heading's text without its inline markup (for ids and the contents list)."""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    return re.sub(r"[`*]", "", text).strip()


def _fence_free(lines):
    """`(line, in_fence)` for each line; the fence markers themselves are skipped."""
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        yield line, in_fence


class _Headings:
    """Assigns each heading of ONE document its id, once, in order (slug, then -1, -2 on repeats)."""

    def __init__(self, document: str):
        self.base = doc_id(document)
        self.seen: "dict[str, int]" = {}
        self.by_slug: "dict[str, str]" = {}

    def assign(self, text: str) -> str:
        base = slug(_plain(text)) or "section"
        count = self.seen.get(base, 0)
        self.seen[base] = count + 1
        ident = "%s--%s%s" % (self.base, base, "" if count == 0 else "-%d" % count)
        self.by_slug.setdefault(base if count == 0 else "%s-%d" % (base, count), ident)
        return ident


def collect_headings(document: str, markdown: str) -> "dict[str, str]":
    """`{slug: id}` for the document's headings (what a `#slug` link can reach)."""
    headings = _Headings(document)
    for line, in_fence in _fence_free((markdown or "").splitlines()):
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match is not None:
            headings.assign(match.group(2))
    return headings.by_slug


# ── Inline ────────────────────────────────────────────────────────────

class _Context:
    """What an inline run needs to resolve a link: where it is, and what exists."""

    def __init__(self, document: str, targets: "dict[str, dict[str, str]]"):
        self.document = document.replace("\\", "/")
        self.targets = targets            # {document: {slug: id}}
        self.dead: "list[str]" = []

    def href(self, url: str) -> "str | None":
        """The href for `url`, or None to render only the text. Records dead corpus links."""
        url = url.strip()
        if not url:
            return None
        if url.startswith("#"):
            found = self.targets.get(self.document, {}).get(url[1:].lower())
            if found is None:
                self.dead.append(url)
                return None
            return "#" + found
        scheme = _SCHEME.match(url)
        if scheme is not None:
            return url if scheme.group(1).lower() in _EXTERNAL else None
        path, _, fragment = url.partition("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(self.document), path)) if path else self.document
        if target not in self.targets:
            return None                    # a source file or anything else outside the help corpus
        if not fragment:
            return "#" + doc_id(target)
        found = self.targets[target].get(fragment.lower())
        if found is None:
            self.dead.append(url)
            return None
        return "#" + found


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str, ctx: _Context, depth: int = 0) -> str:
    """One run of inline markup as HTML. `**` toggles bold and is closed at the end; code wins."""
    text = _IMAGE.sub("", text)
    out: "list[str]" = []
    buffer: "list[str]" = []
    bold = False
    index = 0

    def flush():
        if buffer:
            out.append(_esc("".join(buffer)))
            buffer.clear()

    while index < len(text):
        char = text[index]
        if char == "`":
            close = text.find("`", index + 1)
            if close > index:
                flush()
                out.append("<code>%s</code>" % _esc(text[index + 1:close]))
                index = close + 1
                continue
        if char == "[" and depth < 3:
            match = _LINK.match(text, index)
            if match is not None:
                flush()
                label = _inline(match.group(1), ctx, depth + 1)
                href = ctx.href(match.group(2))
                if href is None:
                    out.append(label)
                elif href.startswith("#"):
                    out.append('<a href="%s">%s</a>' % (_esc(href), label))
                else:
                    out.append('<a href="%s" rel="noopener noreferrer">%s</a>' % (_esc(href), label))
                index = match.end()
                continue
        if char == "*" and text[index:index + 2] == "**":
            flush()
            out.append("</strong>" if bold else "<strong>")
            bold = not bold
            index += 2
            continue
        buffer.append(char)
        index += 1
    flush()
    if bold:
        out.append("</strong>")
    return "".join(out)


# ── Blocks ────────────────────────────────────────────────────────────

def _cells(line: str) -> "list[str]":
    """A pipe-table row's cells: split on `|` outside backticks and not escaped."""
    cells, current, in_code, index = [], [], False, 0
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    while index < len(line):
        char = line[index]
        if char == "\\" and line[index + 1:index + 2] == "|":
            current.append("|")
            index += 2
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def render_document(document: str, markdown: str, targets: "dict[str, dict[str, str]]") -> DocumentResult:
    """One document as the body of a `<section>`. `targets` is `{document: {slug: id}}` for the corpus."""
    ctx = _Context(document, targets)
    headings = _Headings(document)
    result = DocumentResult(html="")
    out: "list[str]" = []
    paragraph: "list[str]" = []
    quote: "list[str]" = []
    table: "list[str]" = []
    lists: "list[tuple[int, str]]" = []       # (indent, "ul" | "ol")
    code: "list[str]" = []
    pending_anchor = ""
    in_fence = False

    def close_paragraph():
        if paragraph:
            out.append("<p>%s</p>" % _inline("\n".join(paragraph), ctx))
            paragraph.clear()

    def close_quote():
        if quote:
            out.append("<blockquote><p>%s</p></blockquote>" % _inline("\n".join(quote), ctx))
            quote.clear()

    def close_table():
        if not table:
            return
        rows = [_cells(r) for r in table]
        head = rows[0] if len(rows) > 1 and _TABLE_SEPARATOR.match(table[1]) else None
        body = rows[2:] if head is not None else rows
        parts = ['<div class="table"><table>']
        if head is not None:
            parts.append("<thead><tr>%s</tr></thead>" % "".join("<th>%s</th>" % _inline(c, ctx) for c in head))
        parts.append("<tbody>%s</tbody></table></div>" % "".join(
            "<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c, ctx) for c in row) for row in body))
        out.append("".join(parts))
        table.clear()

    def close_lists():
        # Every open list holds exactly one open item, so closing is always </li></kind>.
        while lists:
            out.append("</li></%s>" % lists.pop()[1])

    def close_all():
        close_paragraph()
        close_quote()
        close_table()
        close_lists()

    for line in (markdown or "").splitlines():
        if _FENCE.match(line):
            if in_fence:
                out.append("<pre><code>%s</code></pre>" % "\n".join(code))
                code.clear()
            else:
                close_all()
            in_fence = not in_fence
            continue
        if in_fence:
            code.append(_esc(line))
            continue

        if not line.strip():
            close_paragraph()
            close_quote()
            close_table()
            continue

        anchor = _ANCHOR.match(line)
        if anchor is not None:
            pending_anchor = anchor.group(1)
            continue

        heading = _HEADING.match(line)
        if heading is not None:
            close_all()
            level = len(heading.group(1))
            ident = headings.assign(heading.group(2))
            text = _plain(heading.group(2))
            result.headings.append((ident, level, text))
            marker = ""
            if pending_anchor:
                marker = '<a class="topic" id="help-%s"></a>' % pending_anchor
                result.topic_ids.append("help-" + pending_anchor)
                pending_anchor = ""
            out.append('<h%d id="%s">%s%s</h%d>' % (level, ident, marker, _inline(heading.group(2), ctx), level))
            continue

        if _RULE.match(line):
            close_all()
            out.append("<hr>")
            continue

        if line.lstrip().startswith("|"):
            close_paragraph()
            close_quote()
            close_lists()
            table.append(line)
            continue
        close_table()

        quoted = _QUOTE.match(line)
        if quoted is not None:
            close_paragraph()
            close_lists()
            quote.append(quoted.group(1))
            continue
        close_quote()

        item = _BULLET.match(line)
        kind, number = "ul", None
        if item is not None:
            indent, content = len(item.group(1)), item.group(2)
        else:
            item = _NUMBERED.match(line)
            if item is not None:
                kind, indent, number, content = "ol", len(item.group(1)), item.group(2), item.group(3)
        if item is not None:
            close_paragraph()
            while lists and lists[-1][0] > indent:
                out.append("</li></%s>" % lists.pop()[1])
            if lists and lists[-1][0] == indent:
                out.append("</li>")
                if lists[-1][1] != kind:
                    out[-1] = "</li></%s>" % lists.pop()[1]
            if not lists or lists[-1][0] < indent:
                lists.append((indent, kind))
                out.append("<%s%s>" % (kind, ' start="%s"' % number if kind == "ol" and number != "1" else ""))
            out.append("<li>%s" % _inline(content, ctx))
            continue

        if lists and line[:1] in (" ", "\t"):
            out.append(" " + _inline(line.strip(), ctx))        # a continuation of the open item
            continue
        close_lists()
        paragraph.append(line)

    if in_fence:                                  # an unclosed fence runs to the end, as in the Tk pane
        out.append("<pre><code>%s</code></pre>" % "\n".join(code))
    close_all()
    result.html = "\n".join(piece for piece in out if piece != "")
    result.dead = ctx.dead
    return result


# ── The page ──────────────────────────────────────────────────────────

_STYLE = """
:root{--bg:#fff;--fg:#1f2328;--dim:#59636e;--rule:#d1d9e0;--code:#f6f8fa;--accent:#0b5cad;--nav:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--dim:#9198a1;--rule:#30363d;--code:#161b22;--accent:#58a6ff;--nav:#161b22}}
*{box-sizing:border-box}
html{scroll-padding-top:1rem}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:1.2rem 1.5rem;border-bottom:1px solid var(--rule)}
header h1{margin:0;font-size:1.5rem}
header p{margin:.25rem 0 0;color:var(--dim);font-size:.9rem}
.layout{display:flex;align-items:flex-start;gap:0}
nav{flex:0 0 18rem;position:sticky;top:0;max-height:100vh;overflow:auto;padding:1rem 1rem 2rem 1.5rem;background:var(--nav);border-right:1px solid var(--rule);font-size:.9rem}
nav details{margin:.35rem 0}
nav summary{cursor:pointer;font-weight:600}
nav ul{list-style:none;margin:.2rem 0 .4rem;padding-left:.8rem}
nav a{color:var(--fg);text-decoration:none}
nav a:hover{text-decoration:underline}
main{flex:1 1 auto;min-width:0;max-width:52rem;padding:1rem 1.5rem 4rem}
section.doc{margin-bottom:3rem;padding-bottom:1rem;border-bottom:2px solid var(--rule)}
.source{color:var(--dim);font-size:.85rem;margin:0 0 .5rem}
h1,h2,h3,h4,h5,h6{line-height:1.25;margin:1.6em 0 .5em}
h2{border-bottom:1px solid var(--rule);padding-bottom:.25em}
a{color:var(--accent)}
code{background:var(--code);padding:.1em .35em;border-radius:4px;font:.9em ui-monospace,Consolas,monospace}
pre{background:var(--code);padding:.8rem 1rem;border-radius:6px;overflow:auto}
pre code{padding:0;background:none}
blockquote{margin:1em 0;padding:0 1em;color:var(--dim);border-left:4px solid var(--rule)}
.table{overflow-x:auto;margin:1em 0}
table{border-collapse:collapse}
th,td{border:1px solid var(--rule);padding:.35em .7em;text-align:left;vertical-align:top}
th{background:var(--code)}
hr{border:0;border-top:1px solid var(--rule);margin:1.5rem 0}
.topic{display:block;height:0;overflow:hidden}
@media (max-width:52rem){.layout{display:block}nav{position:static;max-height:none;border-right:0;border-bottom:1px solid var(--rule)}}
@media print{nav{display:none}header,main{padding-left:0;padding-right:0}a{color:inherit}}
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"


def build_page(documents: "dict[str, str]", order, *, title: str, version: str) -> Page:
    """The whole help corpus as one page. `order` is the documents' order (it is also the contents order).

    Raises `ValueError` for a document named in `order` that `documents` lacks, because a page with
    fewer documents is a smaller Help that looks complete.
    """
    missing = [d for d in order if d not in documents]
    if missing:
        raise ValueError("cannot compile the help page; missing: " + ", ".join(missing))
    keys = [d.replace("\\", "/") for d in order]
    source = {k: documents[d] for k, d in zip(keys, order)}
    targets = {k: collect_headings(k, text) for k, text in source.items()}

    nav, body, topics, dead = [], [], [], []
    for key in keys:
        rendered = render_document(key, source[key], targets)
        topics.extend(rendered.topic_ids)
        dead.extend((key, link) for link in rendered.dead)
        first = next((h for h in rendered.headings if h[1] == 1), None)
        name = first[2] if first else key
        entries = "".join('<li><a href="#%s">%s</a></li>' % (_esc(i), _esc(t))
                          for i, level, t in rendered.headings if level == 2)
        nav.append('<details><summary><a href="#%s">%s</a></summary><ul>%s</ul></details>'
                   % (doc_id(key), _esc(name), entries))
        body.append('<section class="doc" id="%s"><p class="source">%s</p>\n%s\n</section>'
                    % (doc_id(key), _esc(key), rendered.html))

    page = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="%s">'
        "<title>%s</title><style>%s</style></head>\n<body>"
        '<header><h1>%s</h1><p>Version %s. Compiled from the documentation shipped with this build; read only.</p></header>\n'
        '<div class="layout"><nav aria-label="Contents">%s</nav>\n<main>\n%s\n</main></div>\n</body></html>\n'
    ) % (_esc(_CSP), _esc(title), _STYLE, _esc(title), _esc(version), "\n".join(nav), "\n".join(body))
    return Page(page, len(keys), tuple(topics), tuple(dead))
