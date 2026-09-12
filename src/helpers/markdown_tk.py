"""The Help renderer's deliberately small markdown subset. NOT an implementation.

Read that first line as a boundary, because this is where a tidy 150-line
function turns into a half-working markdown parser. OpenChem Studio renders the
same corpus through `QTextBrowser.setMarkdown`, which is Qt's own importer and
costs nothing; Tk has no equivalent, so this exists only to cover what the
documents in `helpers/help_docs.HELP_DOCUMENTS` actually use.

`render(markdown)` is PURE: markdown in, `[(text, tag), ...]` out. No Tk, so
every rule below is testable without a display, which is how the ambiguous
cases got pinned rather than discovered.

SUPPORTED
    ATX headings (#, ##, ###+)          h1 / h2 / h3
    **bold**                            bold
    `code`                              code
    fenced code blocks                  code
    - * + bullets, 1. numbered          body, with a bullet glyph
    > block quotes                      dim
    horizontal rules                    dim
    pipe tables                         code, VERBATIM
    [text](url)                         the text; see below
    ![alt](url)                         dropped; see below

EVERYTHING ELSE IS LITERAL -- underscore emphasis, strikethrough, inline HTML,
task lists, footnotes, reference links, setext headings. Not "unimplemented":
asserted to come out as typed, so nobody reads a blank as a bug and starts
adding cases until this is a parser nobody trusts.

TABLES ARE MONOSPACE, VERBATIM, and that is the whole feature. A pipe table
re-flowed into a proportional font is unreadable in a way that looks like
corruption, so the rows go through as written and the `code` tag keeps the
columns aligned.

LINKS RENDER AS THEIR TEXT AND LOSE THE URL, because the pane has nowhere to
send a reader. `README.md` carries well over a hundred of them; rendering
`[Doctor](#doctor)` verbatim would put brackets and anchors through the middle
of every other sentence. Images are dropped entirely -- the badge row at the
top of a README is not help.

THE THREE PRECEDENCE RULES, chosen rather than inherited. They are what a
scanner has to decide, and the corpus contains all three:

    `**not bold**`    code wins; the asterisks are literal inside it
    **bold `code`**   both, with the code span rendered as code
    ***both***        bold "*both", then a literal "*" -- see below

Single `*` is never emphasis here. Recognising it would make `2 * 3 * 4` in a
sentence turn half a paragraph italic, and the documents use `**` throughout.

The third case falls out of that rather than being designed: `**` toggles, the
leftover `*` is ordinary text, and the closing run splits the same way. It is
ugly and it is DEFINED, which is the property that matters -- the corpus
contains no `***` at all (measured: zero across all seven documents), so
special-casing it would be parser for its own sake.
"""

from __future__ import annotations

import re

#: Every tag this module can emit. `controllers/help_tab.py` configures each
#: one, and a test asserts the two lists agree -- a span carrying a tag the
#: widget never configured renders as unstyled body text, silently.
TAGS = ("h1", "h2", "h3", "body", "bold", "code", "dim")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

#: Bullet glyphs by nesting depth. Two levels is what the corpus uses; deeper
#: lists reuse the last one rather than inventing symbols nobody recognises.
_BULLETS = ("•", "◦")


def render(markdown: str) -> list:
    """`[(text, tag), ...]` for the whole document.

    Spans are inserted in order; newlines are part of the text, so a consumer
    is a loop over `insert(END, text, tag)` and nothing else.
    """
    out: list = []
    in_fence = False
    for line in (markdown or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue                                  # the fence itself is markup
        if in_fence:
            out.append((line + "\n", "code"))
            continue
        _block(line, out)
    return out


def _block(line: str, out: list) -> None:
    if not line.strip():
        out.append(("\n", "body"))
        return

    if _RULE.match(line):
        out.append(("─" * 48 + "\n", "dim"))
        return

    heading = _HEADING.match(line)
    if heading is not None:
        level = len(heading.group(1))
        tag = "h1" if level == 1 else ("h2" if level == 2 else "h3")
        out.append((heading.group(2).strip() + "\n", tag))
        return

    if line.lstrip().startswith("|"):
        # Verbatim: the font is doing the alignment.
        out.append((line + "\n", "code"))
        return

    quote = _QUOTE.match(line)
    if quote is not None:
        _inline("  " + quote.group(1), out, base="dim")
        out.append(("\n", "dim"))
        return

    bullet = _BULLET.match(line)
    if bullet is not None:
        depth = min(len(bullet.group(1)) // 2, len(_BULLETS) - 1)
        out.append(("  " * (depth + 1) + _BULLETS[depth] + " ", "body"))
        _inline(bullet.group(2), out)
        out.append(("\n", "body"))
        return

    numbered = _NUMBERED.match(line)
    if numbered is not None:
        depth = min(len(numbered.group(1)) // 2, len(_BULLETS) - 1)
        out.append(("  " * (depth + 1) + numbered.group(2) + ". ", "body"))
        _inline(numbered.group(3), out)
        out.append(("\n", "body"))
        return

    _inline(line, out)
    out.append(("\n", "body"))


def _inline(text: str, out: list, base: str = "body") -> None:
    """Emit one line's spans. `base` is the tag for its unstyled runs."""
    text = _IMAGE.sub("", text)              # dropped before links: same shape
    text = _LINK.sub(r"\1", text)

    bold = False
    buffer = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "`":
            close = text.find("`", index + 1)
            if close > index:
                if buffer:
                    out.append((buffer, "bold" if bold else base))
                    buffer = ""
                # Code wins: whatever is inside is literal, asterisks included.
                out.append((text[index + 1:close], "code"))
                index = close + 1
                continue
        if char == "*" and text[index:index + 2] == "**":
            if buffer:
                out.append((buffer, "bold" if bold else base))
                buffer = ""
            bold = not bold
            index += 2
            continue
        buffer += char
        index += 1
    if buffer:
        out.append((buffer, "bold" if bold else base))


def plain_text(markdown: str) -> str:
    """What `render` would show, as a string. For tests and for search."""
    return "".join(text for text, _tag in render(markdown))
