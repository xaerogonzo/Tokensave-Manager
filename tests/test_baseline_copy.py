"""Tests for helpers/baseline_copy - the per-project copy of the baseline.

The safety model rests on three things, and each has its own tests: the header
grammar (a person's file is never taken for ours), one hash definition used on
every side (a CRLF checkout is never "edited"), and the write policy (only an
absent or outdated copy is ever written, and a current one is left alone).
"""

import os

import pytest

from helpers import baseline_copy as bc

TEMPLATE = "# Baseline\n\n## Rules\n"
SHA = bc.content_sha(TEMPLATE)


# -- header grammar -------------------------------------------------------

def test_a_rendered_copy_parses_with_its_own_sha():
    facts = bc.parse_copy(bc.render_copy(TEMPLATE))
    assert facts.header == bc.HEADER_OK
    assert facts.recorded_sha == SHA == facts.body_sha


@pytest.mark.parametrize("first_line", [
    "<!-- tokensave-manager:copy project-baseline.md -->",
    "<!-- tokensave-manager:copy project-baseline.md sha256=garbage -->",
    "<!-- tokensave-manager:copy project-baseline.md sha256=%s -->" % SHA.upper(),
    "<!-- tokensave-manager:copy project-baseline.md sha256=%s -->" % SHA[:63],
    "<!-- tokensave-manager:copy other.md sha256=%s -->" % SHA,
    "<!-- tokensave-manager:copy project-baseline.md sha256=%s" % SHA,
])
def test_a_damaged_manager_header_is_invalid_not_unmanaged(first_line):
    """A damaged Manager file must not look like a person's own file."""
    facts = bc.parse_copy(first_line + "\n" + TEMPLATE)
    assert facts.header == bc.HEADER_INVALID
    assert bc.classify_copy(facts, SHA) == bc.COPY_INVALID


def test_the_marker_anywhere_but_line_one_is_unmanaged():
    text = ("# My baseline\n\n<!-- tokensave-manager:copy project-baseline.md "
            "sha256=%s -->\n" % SHA)
    assert bc.classify_copy(bc.parse_copy(text), SHA) == bc.COPY_UNMANAGED


def test_human_text_after_the_hash_is_allowed():
    line = ("<!-- tokensave-manager:copy project-baseline.md sha256=%s "
            "anything a person reads -->" % SHA)
    assert bc.parse_copy(line + "\n" + TEMPLATE).header == bc.HEADER_OK


# -- states ---------------------------------------------------------------

def _facts(recorded, body):
    return bc.CopyFacts(exists=True, readable=True, header=bc.HEADER_OK,
                        recorded_sha=recorded, body_sha=body)


@pytest.mark.parametrize("facts,template_sha,state", [
    (bc.CopyFacts(), "a" * 64, bc.COPY_ABSENT),
    (bc.CopyFacts(exists=True), "a" * 64, bc.COPY_UNREADABLE),
    (bc.CopyFacts(exists=True, readable=True), "a" * 64, bc.COPY_UNMANAGED),
    (bc.CopyFacts(exists=True, readable=True, header=bc.HEADER_INVALID),
     "a" * 64, bc.COPY_INVALID),
    (_facts("a" * 64, "b" * 64), "a" * 64, bc.COPY_EDITED),
    (_facts("a" * 64, "a" * 64), "a" * 64, bc.COPY_CURRENT),
    (_facts("a" * 64, "a" * 64), "c" * 64, bc.COPY_OUTDATED),
])
def test_classify_copy_states(facts, template_sha, state):
    assert bc.classify_copy(facts, template_sha) == state


def test_edited_is_decided_before_outdated():
    """A hand edit must not be offered as an ordinary refresh."""
    assert bc.classify_copy(_facts("a" * 64, "b" * 64), "c" * 64) == bc.COPY_EDITED


def test_a_crlf_checkout_of_an_lf_render_is_current():
    crlf = bc.render_copy(TEMPLATE).replace("\n", "\r\n")
    assert bc.classify_copy(bc.parse_copy(crlf), SHA) == bc.COPY_CURRENT


def test_a_crlf_template_hashes_like_its_lf_twin():
    assert bc.content_sha(TEMPLATE.replace("\n", "\r\n")) == SHA


# -- rendering and writing ------------------------------------------------

def test_render_is_deterministic_and_keeps_the_templates_newline():
    crlf = TEMPLATE.replace("\n", "\r\n")
    assert bc.render_copy(crlf) == bc.render_copy(crlf)
    first, rest = bc.render_copy(crlf).split("\r\n", 1)
    assert rest == crlf
    assert "sha256=%s" % SHA in first


def test_an_absent_copy_is_written_and_verified(tmp_path):
    result = bc.write_copy(str(tmp_path), TEMPLATE)
    assert (result.ok, result.changed) == (True, True)
    assert (tmp_path / "project-baseline.md").read_text(encoding="utf-8") == \
        bc.render_copy(TEMPLATE)


def test_a_second_write_is_a_no_op_with_no_mtime_churn(tmp_path):
    bc.write_copy(str(tmp_path), TEMPLATE)
    path = tmp_path / "project-baseline.md"
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    result = bc.write_copy(str(tmp_path), TEMPLATE)
    assert (result.ok, result.changed) == (True, False)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before


def test_an_outdated_copy_is_refreshed(tmp_path):
    (tmp_path / "project-baseline.md").write_text(bc.render_copy("# old\n"),
                                                  encoding="utf-8")
    result = bc.write_copy(str(tmp_path), TEMPLATE)
    assert result.ok and result.changed
    assert result.state_before == bc.COPY_OUTDATED


def test_a_refresh_refuses_when_the_recorded_sha_moved(tmp_path):
    (tmp_path / "project-baseline.md").write_text(bc.render_copy("# old\n"),
                                                  encoding="utf-8")
    result = bc.write_copy(str(tmp_path), TEMPLATE,
                           expect_recorded_sha="f" * 64)
    assert not result.ok and "changed since the plan" in result.error


@pytest.mark.parametrize("content", [
    "# a person's own baseline\n",
    bc.render_copy("# old\n") + "hand edit\n",
    "<!-- tokensave-manager:copy project-baseline.md sha256=bad -->\n",
])
def test_files_that_are_not_ours_to_replace_are_never_overwritten(tmp_path,
                                                                  content):
    path = tmp_path / "project-baseline.md"
    path.write_bytes(content.encode("utf-8"))
    result = bc.write_copy(str(tmp_path), TEMPLATE)
    assert not result.ok
    assert path.read_bytes() == content.encode("utf-8")


def test_an_undecodable_copy_is_unreadable_and_never_overwritten(tmp_path):
    path = tmp_path / "project-baseline.md"
    path.write_bytes(b"\xff\xfe\x00\xc3\x28")
    assert bc.classify_copy(bc.read_copy(str(path)), SHA) == bc.COPY_UNREADABLE
    assert not bc.write_copy(str(tmp_path), TEMPLATE).ok
    assert path.read_bytes() == b"\xff\xfe\x00\xc3\x28"


def test_an_empty_template_writes_nothing(tmp_path):
    result = bc.write_copy(str(tmp_path), "")
    assert not result.ok
    assert not (tmp_path / "project-baseline.md").exists()


# -- the shipped BASIC_INSTRUCTIONS template ---------------------------------

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "templates")


def test_the_shipped_template_carries_the_relative_include():
    """The template must already say what the loader would rewrite it to.

    Claude Code loads nothing from an absolute Windows include (see
    templates/gotchas/claude-md-external-includes.md). The template carried one
    for months after the loader learned to rewrite it, which was invisible in
    every scaffolded file and wrong in any copy made by hand -- and it kept the
    placeholder check, which compares the whole file to the template, from ever
    matching an untouched scaffold.
    """
    from helpers.project_discovery import load_basic_instructions_template

    path = os.path.join(TEMPLATES_DIR, "claude-md-template.md")
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    include_lines = [line for line in raw.splitlines()
                     if line.startswith("@") and "project-baseline.md" in line]
    assert include_lines == ["@project-baseline.md"]

    written = load_basic_instructions_template(path, "@project-baseline.md")
    assert written.strip() == raw.strip(), (
        "a scaffolded, untouched BASIC_INSTRUCTIONS.md must equal the template")
