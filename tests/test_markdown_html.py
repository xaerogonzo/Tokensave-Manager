"""The compiled help page says what the Help pane says, and cannot do anything else.

Four kinds of claim, none of them "the HTML looks right":

* **parity** -- on the real corpus the page and `markdown_tk` carry the same words in the same
  order, so the two presentations of one document cannot drift apart;
* **inertness** -- no script, no resource, no markup from a document survives as markup, and the
  page declares a Content-Security-Policy that forbids everything but its own style;
* **links** -- internal links land on ids that exist; a link into the help that points at nothing
  stops the build instead of quietly turning into text;
* **delivery** -- the page rides in the build's bundle, a build never compiles one, a placeholder
  value cannot become markup, and the Help tab opens the page in a build and the file in a checkout.
"""
from __future__ import annotations

import importlib.util
import re
import stat
import sys
import types
from html.parser import HTMLParser
from pathlib import Path

import pytest

from helpers import help_docs as hd
from helpers import markdown_html as mh
from helpers import markdown_tk as tk

ROOT = Path(__file__).resolve().parent.parent
BUILD = (ROOT / "build.ps1").read_text(encoding="utf-8")
DOC = "docs/X.md"


def _render(markdown: str, document: str = DOC, others: "dict[str, str] | None" = None):
    sources = {document: markdown, **(others or {})}
    targets = {k: mh.collect_headings(k, v) for k, v in sources.items()}
    return mh.render_document(document, markdown, targets)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: "list[str]" = []

    def handle_data(self, data):
        self.parts.append(data)


def _words(fragment: str) -> "list[str]":
    parser = _Text()
    parser.feed(fragment)
    return re.findall(r"\w+", " ".join(parser.parts).casefold())


# ── the subset ─────────────────────────────────────────────────────────

def test_headings_get_github_slugs_made_unique_per_document():
    out = _render("# Title\n## Setup\n## Setup\n### What's `new`?\n")
    assert [h[0] for h in out.headings] == [
        "doc-docs-x-md--title", "doc-docs-x-md--setup", "doc-docs-x-md--setup-1", "doc-docs-x-md--whats-new"]
    assert 'id="doc-docs-x-md--setup-1"' in out.html


def test_a_help_anchor_becomes_an_id_and_is_never_content():
    out = _render("<!-- help:right-click-menu -->\n## Right-Click Menu\n\ntext\n")
    assert 'id="help-right-click-menu"' in out.html and out.topic_ids == ["help-right-click-menu"]
    assert "<!--" not in out.html and "help:" not in " ".join(_words(out.html))


def test_bold_toggles_code_wins_and_an_unclosed_bold_is_closed():
    assert _render("**b** and `**not bold**` and **open").html == (
        "<p><strong>b</strong> and <code>**not bold**</code> and <strong>open</strong></p>")


def test_single_asterisks_and_underscores_are_literal_like_the_pane():
    assert _render("2 * 3 * 4 and _x_ and ~~y~~").html == "<p>2 * 3 * 4 and _x_ and ~~y~~</p>"


def test_inline_html_is_shown_as_typed_never_interpreted():
    out = _render("a <b>bold</b> <script>alert(1)</script> &amp; <!-- c -->").html
    assert "<script>" not in out and "<b>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp;amp; &lt;!-- c --&gt;" in out


def test_lists_nest_numbered_lists_keep_their_start_and_a_kind_change_closes_the_list():
    out = _render("- a\n  - b\n  - c\n- d\n\n3. three\n4. four\n").html.replace("\n", "")
    assert out == ("<ul><li>a<ul><li>b</li><li>c</li></ul></li><li>d</li></ul>"
                   '<ol start="3"><li>three</li><li>four</li></ol>')
    assert _render("- a\n1. b\n").html.count("<ul>") == 1 and _render("- a\n1. b\n").html.count("<ol>") == 1


def test_an_indented_line_continues_the_open_item_rather_than_starting_a_paragraph():
    html_ = _render("- first\n  second\n- third\n").html
    assert "<li>first\n second" in html_ and html_.count("<li>") == 2 and "<p>" not in html_


def test_tables_become_real_tables_and_a_pipe_inside_code_or_escaped_stays_in_its_cell():
    out = _render("| A | B |\n|---|:-:|\n| 1 | `x|y` |\n| 2 | a\\|b |\n").html
    assert "<thead><tr><th>A</th><th>B</th></tr></thead>" in out
    assert "<td><code>x|y</code></td>" in out and "<td>a|b</td>" in out
    assert out.count("<tr>") == 3


def test_a_table_with_no_separator_row_has_no_header():
    out = _render("| a | b |\n| c | d |\n").html
    assert "<thead>" not in out and out.count("<tr>") == 2


def test_quotes_rules_and_fenced_code_are_escaped_and_an_unclosed_fence_runs_to_the_end():
    out = _render("> quoted <x>\n\n---\n\n```\n<b>raw</b>\nline2\n```\n\n```\nopen <i>").html
    assert "<blockquote><p>quoted &lt;x&gt;</p></blockquote>" in out and "<hr>" in out
    assert "<pre><code>&lt;b&gt;raw&lt;/b&gt;\nline2</code></pre>" in out
    assert out.endswith("<pre><code>open &lt;i&gt;</code></pre>")


def test_images_are_dropped_like_the_pane_drops_them():
    assert _render("![badge](https://img.shields.io/x.svg) text").html == "<p> text</p>"


# ── links ──────────────────────────────────────────────────────────────

def test_an_internal_anchor_goes_to_that_heading_and_a_missing_one_is_dead():
    out = _render("# Top\n\n[go](#top) and [gone](#nowhere)\n")
    assert '<a href="#doc-docs-x-md--top">go</a>' in out.html and "gone" in out.html
    assert out.dead == ["#nowhere"] and "nowhere" not in out.html.replace("gone", "")


def test_a_link_to_another_help_document_resolves_relative_to_the_linking_one():
    other = "docs/Y.md"
    md = "# T\n[a](Y.md) [b](Y.md#sec) [c](../README.md) [d](Y.md#nope)\n"
    out = _render(md, others={other: "# Y\n## Sec\n", "README.md": "# R\n"})
    assert 'href="#doc-docs-y-md"' in out.html and 'href="#doc-docs-y-md--sec"' in out.html
    assert 'href="#doc-readme-md"' in out.html
    assert out.dead == ["Y.md#nope"]


def test_external_links_are_real_links_and_everything_else_is_text():
    out = _render("[w](https://e.com/?a=1&b=2) [m](mailto:a@b.c) [j](javascript:x) [f](file:///c:/x) "
                  "[s](src/app.py) [d](data:text/html;base64,AAAA)").html
    assert 'href="https://e.com/?a=1&amp;b=2" rel="noopener noreferrer">w</a>' in out
    assert 'href="mailto:a@b.c"' in out
    for dropped in ("javascript", "file:", "src/app.py", "data:"):
        assert dropped not in out.replace("[", "")
    assert out.count("<a ") == 2


def test_a_quote_in_a_link_target_cannot_break_out_of_the_attribute():
    out = _render('[x](https://e.com/" onmouseover="alert(1))').html
    assert 'onmouseover="' not in out and "&quot;" in out


# ── the page is inert ──────────────────────────────────────────────────

_DOCS = {"README.md": "# T\n\n<!-- help:one -->\n## One\n\ntext [x](#one) `c`\n"}


def test_the_page_declares_a_policy_that_forbids_everything_but_its_own_style_and_loads_nothing():
    page = mh.build_page(_DOCS, ["README.md"], title="T", version="1.2.3").html
    assert "default-src &#x27;none&#x27;" in page and "style-src &#x27;unsafe-inline&#x27;" in page
    for banned in ("<script", "<iframe", "<img", "<link", "<object", "@import", "url(", " src=", "javascript:"):
        assert banned not in page, banned
    assert re.findall(r"""href=["']([^"']*)["']""", page) and all(
        h.startswith("#") for h in re.findall(r"""href=["']([^"']*)["']""", page))


def test_the_page_is_deterministic():
    one = mh.build_page(_DOCS, ["README.md"], title="T", version="1.2.3").html
    assert one == mh.build_page(dict(_DOCS), ["README.md"], title="T", version="1.2.3").html


def test_a_page_with_a_document_missing_is_refused_not_shortened():
    with pytest.raises(ValueError, match="missing: docs/B.md"):
        mh.build_page(_DOCS, ["README.md", "docs/B.md"], title="T", version="1")


def test_the_title_and_version_are_escaped():
    page = mh.build_page(_DOCS, ["README.md"], title="<T>", version='"v"').html
    assert "<title>&lt;T&gt;</title>" in page and "Version &quot;v&quot;." in page


# ── the real corpus ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def corpus():
    docs = hd.source_documents()
    return docs, mh.build_page(docs, hd.HELP_DOCUMENTS, title="TokenSave Manager Help", version="0")


def test_every_help_document_is_in_the_page_and_every_topic_has_its_anchor(corpus):
    _, page = corpus
    assert page.documents == len(hd.HELP_DOCUMENTS)
    assert set(page.topic_ids) == {"help-" + t.key for t in hd.topics()}
    for document in hd.HELP_DOCUMENTS:
        assert 'id="%s"' % mh.doc_id(document.replace("\\", "/")) in page.html


def test_the_real_documents_have_no_link_into_the_help_that_points_at_nothing(corpus):
    assert corpus[1].dead_links == ()


def test_every_id_is_unique_and_every_internal_link_lands_on_one(corpus):
    html_ = corpus[1].html
    ids = re.findall(r'\bid="([^"]+)"', html_)
    assert len(ids) == len(set(ids))
    for target in re.findall(r'href="#([^"]+)"', html_):
        assert target in set(ids), target


def test_the_real_page_loads_nothing_and_runs_nothing(corpus):
    html_ = corpus[1].html
    for banned in ("<script", "<iframe", "<img", "<link", "<object", "@import", "url(", " src="):
        assert banned not in html_, banned
    assert all(h.startswith(("#", "http://", "https://", "mailto:")) for h in re.findall(r'href="([^"]*)"', html_))
    assert 'rel="noopener noreferrer"' in html_


def _pane_words(markdown: str) -> "list[str]":
    # The pane prints a list item's number itself; <ol> renders it from CSS. Only indented lines are list items.
    lines = [re.sub(r"^ {2,}\d+\. ", "", line) for line in tk.plain_text(markdown).splitlines()]
    return re.findall(r"\w+", " ".join(lines).casefold())


@pytest.mark.parametrize("document", hd.HELP_DOCUMENTS)
def test_the_page_says_exactly_what_the_pane_says_word_for_word(corpus, document):
    docs, _ = corpus
    key = document.replace("\\", "/")
    stripped = "\n".join(line for line in docs[document].splitlines() if not mh._ANCHOR.match(line))
    targets = {d.replace("\\", "/"): mh.collect_headings(d.replace("\\", "/"), t) for d, t in docs.items()}
    rendered = mh.render_document(key, stripped, targets)
    assert _words(rendered.html) == _pane_words(stripped)


def test_the_parity_test_can_fail():
    # Planted: drop a word on the HTML side and the comparison must notice.
    md = "# T\n\nalpha beta gamma\n"
    assert _words(_render(md).html) == _pane_words(md)
    assert _words(_render(md).html.replace("beta", "")) != _pane_words(md)


# ── help_docs: compile, fill, serve ────────────────────────────────────

def test_compile_refuses_a_page_with_a_link_into_the_help_that_points_at_nothing():
    docs = {d: "# T\n\n[x](#missing)\n" for d in hd.HELP_DOCUMENTS}
    with pytest.raises(hd.BundleError, match="point at nothing"):
        hd.compile_page(docs, "1")


def test_placeholders_are_filled_with_escaped_values_and_unknown_names_are_left_alone():
    page = "<code>{{config_path}}</code> {{app_version}} {{template_dir}} {{nope}}"
    out = hd.fill_page(page, {"config_path": 'C:\\a&b\\<x>"y"', "app_version": "2.7.0"})
    assert out == "<code>C:\\a&amp;b\\&lt;x&gt;&quot;y&quot;</code> 2.7.0 unknown {{nope}}"


def test_the_shipped_copy_has_no_raw_placeholder_and_no_developer_path():
    spec = importlib.util.spec_from_file_location("build_help_bundle_probe", ROOT / "scripts" / "build_help_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_help_bundle_probe"] = module
    spec.loader.exec_module(module)
    text = module.static_page()
    assert not re.search(r"\{\{\s*[a-z0-9_]+\s*\}\}", text)
    assert str(ROOT) not in text and "%LOCALAPPDATA%\\TokenSaveManager\\manager-config.json" in text


def _fake_build(monkeypatch, html_: "str | None", keys=("k",)):
    hd.reset_cache()
    monkeypatch.setattr(hd, "_frozen", lambda: True)
    docs = {d: "# D\n\n<!-- help:%s -->\n## T %d\n\nbody\n" % (keys[0] + str(i), i) for i, d in enumerate(hd.HELP_DOCUMENTS)}
    ns = dict(zip(("SCHEMA", "DIGEST", "PAYLOAD"), hd.encode_bundle(docs)))
    if html_ is not None:
        ns.update(zip(("PAGES_SCHEMA", "PAGES_DIGEST", "PAGES_PAYLOAD"), hd.encode_pages({hd.PAGE_NAME: html_})))
    monkeypatch.setattr(hd, "_import_bundle", lambda: types.SimpleNamespace(**ns))
    return docs


def test_a_build_serves_the_page_compiled_into_it_and_never_compiles_one(monkeypatch):
    _fake_build(monkeypatch, '<p id="help-k0"></p><p id="doc-readme-md"></p>{{app_version}}')
    monkeypatch.setattr(hd, "compile_page", lambda *a, **k: pytest.fail("a build must not compile a page"))
    uri = hd.page_uri({"app_version": "9.9"}, key="k0", document="README.md")
    assert uri.endswith("#help-k0") and uri.startswith("file:")
    path = Path(__import__("urllib.request").request.url2pathname(uri[len("file:"):].split("#")[0].lstrip("/")))
    assert path.read_text(encoding="utf-8").endswith("9.9") and not path.stat().st_mode & stat.S_IWRITE


def test_the_fragment_falls_back_to_the_document_then_to_nothing(monkeypatch):
    _fake_build(monkeypatch, '<p id="doc-readme-md"></p>')
    assert hd.page_uri({}, key="zzz", document="README.md").endswith("#doc-readme-md")
    assert "#" not in hd.page_uri({}, key="zzz", document="docs/GITHUB_GUIDE.md")


def test_a_build_without_a_page_says_so_and_serves_none(monkeypatch):
    _fake_build(monkeypatch, None)
    assert hd.page_html() is None and hd.page_uri({}) is None
    assert "no compiled help page" in hd.page_problem()


def test_a_page_missing_a_topics_anchor_is_a_problem_the_verdict_reports(monkeypatch):
    _fake_build(monkeypatch, '<p id="help-k0"></p>')
    assert "no anchor for" in hd.page_problem()
    report = hd.corpus_report()
    assert any(line.startswith("html page:") for line in hd.corpus_verdict(report, True))


def test_a_complete_page_is_no_problem_and_a_checkout_never_reports_one(monkeypatch):
    docs = _fake_build(monkeypatch, "x")
    every = "".join('<p id="help-%s"></p>' % t.key for t in hd.topics())
    _fake_build(monkeypatch, every)
    assert hd.page_problem() == "" and docs
    monkeypatch.setattr(hd, "_frozen", lambda: False)
    assert hd.page_problem() == ""


def test_a_checkout_compiles_the_documents_on_disk_so_an_edit_shows_without_a_build():
    hd.reset_cache()
    text = hd.page_html()
    assert text and text.startswith("<!doctype html>") and 'id="help-right-click-menu"' in text


# ── the build and the Help tab ─────────────────────────────────────────

def test_the_page_is_compiled_before_the_gui_and_shipped_and_required():
    assert BUILD.index("--page-out") < BUILD.index('Build-Exe "$ROOT\\src\\app.py"')
    assert 'docs\\help.html' in BUILD[BUILD.index("--page-out"):BUILD.index("--page-out") + 200]
    from helpers import release_zip as rz
    assert "docs/help.html" in rz.REQUIRED


class _Stub:
    def _placeholder_values(self):
        return {"app_version": "1"}


@pytest.fixture
def tab(monkeypatch):
    from controllers import help_tab
    opened = []
    monkeypatch.setattr(help_tab, "_open_doc", lambda p: opened.append(("file", p)))
    monkeypatch.setattr(help_tab.webbrowser, "open", lambda u: opened.append(("web", u)) or True)
    return help_tab, opened


def test_in_a_build_the_button_opens_the_page_at_the_topic(tab, monkeypatch):
    help_tab, opened = tab
    monkeypatch.setattr(hd, "source", lambda: hd.SOURCE_BUNDLE)
    monkeypatch.setattr(hd, "page_uri", lambda values, key="", document="": "file:///p.html#help-" + key)
    help_tab.HelpTabController._open_document(_Stub(), "README.md", "k")
    assert opened == [("web", "file:///p.html#help-k")]


def test_in_a_build_with_no_page_it_falls_back_to_the_bundle_copy_of_the_markdown(tab, monkeypatch):
    help_tab, opened = tab
    monkeypatch.setattr(hd, "source", lambda: hd.SOURCE_BUNDLE)
    monkeypatch.setattr(hd, "page_uri", lambda *a, **k: None)
    monkeypatch.setattr(hd, "viewable_path", lambda document: "C:/tmp/README.md")
    help_tab.HelpTabController._open_document(_Stub(), "README.md", "k")
    assert opened == [("file", "C:/tmp/README.md")]


def test_in_a_checkout_the_button_still_opens_the_real_file_because_that_is_what_is_edited(tab, monkeypatch):
    help_tab, opened = tab
    monkeypatch.setattr(hd, "source", lambda: hd.SOURCE_DISK)
    monkeypatch.setattr(hd, "page_uri", lambda *a, **k: pytest.fail("a checkout opens the file"))
    monkeypatch.setattr(hd, "viewable_path", lambda document: "D:/repo/README.md")
    help_tab.HelpTabController._open_document(_Stub(), "README.md", "k")
    assert opened == [("file", "D:/repo/README.md")]
