"""The in-app help is compiled into a build, and a build never reads the loose copy.

Three claims are held here, none of them "the bundle exists":

* the format fails closed (a wrong schema, a damaged digest, a payload that will not decode,
  a mapping that is not text are each their own error);
* the SOURCE of the text follows the kind of run: a checkout reads the disk and ignores a stray
  bundle, a build reads the bundle and never falls back to disk, and a build with no usable
  bundle says so per document instead of showing a shorter list;
* `build.ps1` generates the bundle before the GUI compile, compiles it in, and removes it in a
  `finally`.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import sys
import types
import zlib
from pathlib import Path

import pytest

from helpers import help_docs as hd

ROOT = Path(__file__).resolve().parent.parent
BUILD = (ROOT / "build.ps1").read_text(encoding="utf-8")


def _bundle_module(documents: dict):
    schema, digest, payload = hd.encode_bundle(documents)
    return types.SimpleNamespace(SCHEMA=schema, DIGEST=digest, PAYLOAD=payload)


def _payload_of(raw: bytes):
    payload = base64.b64encode(zlib.compress(raw)).decode("ascii")
    return hd.BUNDLE_SCHEMA, hashlib.sha256(payload.encode("ascii")).hexdigest(), payload


def _doc(title: str, key: str = "k") -> str:
    return "# Doc\n\n<!-- help:%s -->\n## %s\n\nbody about %s\n" % (key, title, title.lower())


@pytest.fixture(autouse=True)
def _fresh_cache():
    hd.reset_cache()
    yield
    hd.reset_cache()


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(hd, "_frozen", lambda: True)


def _bundle_of_everything(prefix="b"):
    """A bundle holding every real document, each with one topic of its own."""
    return {d: _doc("Topic %s %d" % (prefix, i), "%s-%d" % (prefix, i)) for i, d in enumerate(hd.HELP_DOCUMENTS)}


# ── the format ──────────────────────────────────────────────────────────

def test_a_bundle_round_trips_including_unicode_and_windows_separators():
    docs = {os.path.join("docs", "A.md"): "café → ok\n", "README.md": "x"}
    schema, digest, payload = hd.encode_bundle(docs)
    back = hd.decode_bundle(schema, digest, payload)
    assert back == {"docs/A.md": "café → ok\n", "README.md": "x"}  # one spelling of a key everywhere


def test_encoding_is_deterministic():
    docs = {"b.md": "2", "a.md": "1"}
    assert hd.encode_bundle(docs) == hd.encode_bundle(dict(reversed(list(docs.items()))))


@pytest.mark.parametrize("make, expect", [
    (lambda: (2,) + hd.encode_bundle({"a": "x"})[1:], "schema"),
    (lambda: (hd.BUNDLE_SCHEMA, "0" * 64, hd.encode_bundle({"a": "x"})[2]), "digest does not match"),
    (lambda: (hd.BUNDLE_SCHEMA, hd.encode_bundle({"a": "x"})[1], hd.encode_bundle({"a": "y"})[2]), "digest does not match"),
    (lambda: _payload_of(b"not json"), "cannot be decoded"),
    (lambda: _payload_of(b"[1, 2]"), "not a mapping"),
    (lambda: _payload_of(b'{"a": 5}'), "not a mapping"),
    (lambda: (hd.BUNDLE_SCHEMA, None, "x"), "not text"),
])
def test_each_way_a_bundle_can_be_wrong_is_its_own_error(make, expect):
    with pytest.raises(hd.BundleError, match=expect):
        hd.decode_bundle(*make())


def test_a_payload_that_is_not_zlib_is_an_error_even_with_a_matching_digest():
    payload = base64.b64encode(b"definitely not zlib").decode("ascii")
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    with pytest.raises(hd.BundleError, match="cannot be decoded"):
        hd.decode_bundle(hd.BUNDLE_SCHEMA, digest, payload)


# ── what the generator may read ─────────────────────────────────────────

def test_source_documents_reads_every_real_document():
    docs = hd.source_documents()
    assert set(docs) == set(hd.HELP_DOCUMENTS) and all(t.strip() for t in docs.values())


@pytest.mark.parametrize("case", ["missing", "empty", "not_utf8"])
def test_a_document_that_is_missing_empty_or_not_utf8_stops_the_build(tmp_path, monkeypatch, case):
    monkeypatch.setattr(hd, "docs_directory", lambda: str(tmp_path))
    for i, d in enumerate(hd.HELP_DOCUMENTS):
        path = tmp_path / d
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_doc("T%d" % i, "k%d" % i), encoding="utf-8")
    victim = tmp_path / hd.HELP_DOCUMENTS[2]
    if case == "missing":
        victim.unlink()
    elif case == "empty":
        victim.write_text("  \n", encoding="utf-8")
    else:
        victim.write_bytes(b"\xff\xfe\x00bad \x80")
    with pytest.raises(hd.BundleError) as caught:
        hd.source_documents()
    assert hd.HELP_DOCUMENTS[2] in str(caught.value)


# ── which source a run reads ────────────────────────────────────────────

def test_a_checkout_reads_the_disk_and_ignores_a_stray_bundle(monkeypatch):
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module(_bundle_of_everything("stray")))
    keys = {t.key for t in hd.topics()}
    assert hd.source() == hd.SOURCE_DISK
    assert not any(k.startswith("stray-") for k in keys) and "right-click-menu" in keys


def test_a_build_reads_only_the_bundle_even_when_the_disk_has_different_text(frozen, monkeypatch, tmp_path):
    monkeypatch.setattr(hd, "docs_directory", lambda: str(tmp_path))  # an empty directory: no loose docs at all
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module(_bundle_of_everything()))
    found, problems = hd.scan()
    assert hd.source() == hd.SOURCE_BUNDLE and problems == ()
    assert [t.key for t in found] == ["b-%d" % i for i in range(len(hd.HELP_DOCUMENTS))]
    assert "body about topic b 0" in hd.topic_markdown("b-0")


def test_a_build_is_not_changed_by_editing_the_loose_copy(frozen, monkeypatch, tmp_path):
    monkeypatch.setattr(hd, "docs_directory", lambda: str(tmp_path))
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module(_bundle_of_everything()))
    before = hd.topic_markdown("b-0")
    loose = tmp_path / hd.HELP_DOCUMENTS[0]
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text(_doc("Edited by a user", "b-0"), encoding="utf-8")
    assert hd.topic_markdown("b-0") == before and "Edited by a user" not in before


def test_a_build_with_no_bundle_reports_every_document_instead_of_a_shorter_list(frozen, monkeypatch):
    def gone():
        raise ImportError("no module")
    monkeypatch.setattr(hd, "_import_bundle", gone)
    found, problems = hd.scan()
    assert found == () and [p.document for p in problems] == list(hd.HELP_DOCUMENTS)
    assert all("no help bundle" in p.detail for p in problems)
    assert hd.corpus_verdict(hd.corpus_report(), frozen=True)  # and the verdict is not clean


def test_a_damaged_bundle_is_reported_as_damaged_not_as_missing(frozen, monkeypatch):
    bad = _bundle_module(_bundle_of_everything())
    bad.DIGEST = "0" * 64
    monkeypatch.setattr(hd, "_import_bundle", lambda: bad)
    problems = hd.scan()[1]
    assert len(problems) == len(hd.HELP_DOCUMENTS) and all("damaged" in p.detail for p in problems)


def test_a_document_absent_from_an_otherwise_good_bundle_is_the_only_problem(frozen, monkeypatch):
    docs = _bundle_of_everything()
    del docs[hd.HELP_DOCUMENTS[3]]
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module(docs))
    found, problems = hd.scan()
    assert [p.document for p in problems] == [hd.HELP_DOCUMENTS[3]] and "not in the help bundle" in problems[0].detail
    assert len(found) == len(hd.HELP_DOCUMENTS) - 1


def test_the_decoded_bundle_is_cached_in_a_build(frozen, monkeypatch):
    calls = []
    def loader():
        calls.append(1)
        return _bundle_module(_bundle_of_everything())
    monkeypatch.setattr(hd, "_import_bundle", loader)
    hd.topics(), hd.topics(), hd.search("topic")
    assert len(calls) == 1


# ── opening a document ──────────────────────────────────────────────────

def test_in_a_checkout_a_document_opens_as_the_real_file():
    path = hd.viewable_path("README.md")
    assert path == hd.document_path("README.md") and hd.can_view("README.md")


def test_in_a_build_a_document_opens_as_a_read_only_copy_of_the_bundle_not_the_loose_file(frozen, monkeypatch, tmp_path):
    docs = _bundle_of_everything()
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module(docs))
    monkeypatch.setattr(hd, "docs_directory", lambda: str(tmp_path))
    assert hd.can_view("README.md")
    path = hd.viewable_path("README.md")
    assert path and not path.startswith(str(tmp_path))
    assert Path(path).read_text(encoding="utf-8") == docs["README.md"]
    assert not os.access(path, os.W_OK) or not (Path(path).stat().st_mode & stat.S_IWUSR)


def test_nothing_is_offered_for_a_document_the_bundle_lacks(frozen, monkeypatch):
    monkeypatch.setattr(hd, "_import_bundle", lambda: _bundle_module({"README.md": _doc("T")}))
    assert hd.can_view("README.md") and not hd.can_view(os.path.join("docs", "USER_GUIDE.md"))
    assert hd.viewable_path(os.path.join("docs", "USER_GUIDE.md")) is None


# ── the verdict a driven run asks for ───────────────────────────────────

def _report(**over):
    base = dict(source=hd.SOURCE_BUNDLE, documents=len(hd.HELP_DOCUMENTS), topics=40, problems=(), unresolved=(), searchable=True)
    base.update(over)
    return hd.CorpusReport(**base)


def test_a_complete_corpus_from_the_right_source_has_no_failures():
    assert hd.corpus_verdict(_report(), frozen=True) == []
    assert hd.corpus_verdict(_report(source=hd.SOURCE_DISK), frozen=False) == []


@pytest.mark.parametrize("report, frozen, expect", [
    (_report(source=hd.SOURCE_DISK), True, "expected 'bundle'"),
    (_report(), False, "expected 'disk'"),
    (_report(documents=3), True, "3 of"),
    (_report(topics=0), True, "no topics"),
    (_report(problems=(hd.Problem("README.md", "damaged"),)), True, "README.md: damaged"),
    (_report(unresolved=("a", "b")), True, "no body"),
    (_report(searchable=False), True, "search found nothing"),
])
def test_each_way_the_corpus_can_be_wrong_is_named(report, frozen, expect):
    assert any(expect in f for f in hd.corpus_verdict(report, frozen)), hd.corpus_verdict(report, frozen)


def test_the_real_corpus_is_clean_from_the_disk():
    assert hd.corpus_verdict(hd.corpus_report(), frozen=False) == []


# ── the generator ───────────────────────────────────────────────────────

def _generator():
    spec = importlib.util.spec_from_file_location("build_help_bundle_script", ROOT / "scripts" / "build_help_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_help_bundle_script"] = module
    spec.loader.exec_module(module)
    return module


def test_the_generated_module_imports_with_three_literals_and_decodes_to_the_source(tmp_path):
    out = tmp_path / "_help_bundle.py"
    assert _generator().main(["--out", str(out)]) == 0
    spec = importlib.util.spec_from_file_location("generated_bundle_probe", out)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hd.decode_bundle(module.SCHEMA, module.DIGEST, module.PAYLOAD) == {
        hd._doc_key(k): v for k, v in hd.source_documents().items()}
    assert not list(tmp_path.glob("*.tmp"))
    text = out.read_text(encoding="utf-8")
    assert "\r" not in text and text.count("\n") == 4  # a header and three literals, no code


def test_the_generator_writes_nothing_and_fails_when_a_document_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hd, "docs_directory", lambda: str(tmp_path))  # no documents there
    out = tmp_path / "_help_bundle.py"
    assert _generator().main(["--out", str(out)]) == 1
    assert not out.exists() and "cannot build the help bundle" in capsys.readouterr().err


# ── build.ps1 wires it ──────────────────────────────────────────────────

def test_the_bundle_is_generated_before_the_gui_compile_and_compiled_in():
    gen = BUILD.index("build_help_bundle.py")
    gui = BUILD.index('Build-Exe "$ROOT\\src\\app.py"')
    assert gen < gui
    gui_line = BUILD[gui:BUILD.index("\n", gui)]
    assert "--include-module=_help_bundle" in gui_line
    for other in ("tokensave-wrapper.py", "cli.py"):
        line = BUILD[BUILD.index('Build-Exe "$ROOT\\src\\%s"' % other):]
        assert "_help_bundle" not in line.split("\n", 1)[0]  # the CLI and the wrapper have no Help tab


def test_a_failed_generator_stops_the_build_and_the_bundle_is_removed_in_a_finally():
    after_gen = BUILD[BUILD.index("build_help_bundle.py"):]
    assert "$LASTEXITCODE -ne 0" in after_gen.split("try {", 1)[0] and "throw" in after_gen.split("try {", 1)[0]
    block = after_gen[after_gen.index("try {"):]
    finally_part = block[block.index("finally {"):block.index("}", block.index("finally {"))]
    assert "Remove-Item" in finally_part and "$HELP_BUNDLE" in finally_part


def test_the_generated_module_is_never_committed():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "src/_help_bundle.py" in [line.strip() for line in ignore]


def test_the_loose_docs_are_still_shipped_as_a_human_copy():
    """The app no longer reads them; people still do."""
    for doc in hd.HELP_DOCUMENTS:
        assert os.path.basename(doc) in BUILD, "%s would stop being shipped" % doc
