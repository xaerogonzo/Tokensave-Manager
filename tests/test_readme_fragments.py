"""README fragments: a PR adds a file under readme.d/, never a line of README.md.

The same design as changelog fragments, for a document whose entries belong in sections. The
failure it removes was measured: two parallel PRs described their feature in the same README
section and the second to merge conflicted.
"""
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import readme_fragments as rf

MINI = ("# Title\n\nintro\n\n"
        "<!-- help:alpha -->\n## Alpha\n\nalpha text\n\n---\n\n"
        "<!-- help:beta -->\n## Beta\n\nbeta text\n\n---\n\n"
        "<!-- help:gamma -->\n## Gamma\n\ngamma text, the last section\n")
ROOT = Path(__file__).resolve().parent.parent


def project(tmp_path, readme=MINI, eol="\n"):
    (tmp_path / "README.md").write_bytes(readme.replace("\n", eol).encode("utf-8"))
    return tmp_path


def frag(root, name, target, body, header=True):
    directory = root / "readme.d"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text((f"<!-- readme: {target} -->\n" if header else "") + body, encoding="utf-8", newline="\n")
    return path


A1 = "202611130100-first-aaaa.md"
A2 = "202611130200-second-bbbb.md"


# -- targets and new ---------------------------------------------------------------------

def test_targets_are_the_help_anchors_in_order(tmp_path):
    assert rf.targets(MINI) == ["alpha", "beta", "gamma"]


def test_an_anchor_mentioned_inside_a_sentence_is_not_a_target():
    assert rf.targets("see `<!-- help:key -->` above\n<!-- help:real -->\n") == ["real"]


def test_new_writes_a_template_that_check_rejects_until_edited(tmp_path):
    root = project(tmp_path)
    path = rf.new_fragment(root, "My Topic", "beta", datetime(2026, 11, 13, 1, 2, tzinfo=timezone.utc))
    assert rf.FRAGMENT_NAME.fullmatch(path.name) and path.name.startswith("202611130102-my-topic-")
    assert path.read_text(encoding="utf-8").startswith("<!-- readme: beta -->\n")
    assert any("placeholder" in e for e in rf.validate(root, MINI))


def test_new_refuses_an_unknown_section_and_lists_the_real_ones(tmp_path):
    with pytest.raises(rf.ReadmeFragmentError) as err:
        rf.new_fragment(project(tmp_path), "x", "nope")
    assert "alpha" in str(err.value) and "gamma" in str(err.value)


def test_new_refuses_a_slug_that_is_a_path(tmp_path):
    with pytest.raises(ValueError):
        rf.new_fragment(project(tmp_path), "../evil", "alpha")


def test_two_new_fragments_in_one_minute_get_different_names(tmp_path):
    root = project(tmp_path)
    now = datetime(2026, 11, 13, 1, 2, tzinfo=timezone.utc)
    assert rf.new_fragment(root, "same", "alpha", now) != rf.new_fragment(root, "same", "alpha", now)


# -- parsing and validating one fragment -------------------------------------------------

def parse(body, name=A1):
    return rf.parse_fragment(Path(name), body)


def test_a_fragment_must_start_by_naming_its_section():
    fragment, problems = parse("just text\n")
    assert fragment is None and "first line" in problems[0]
    fragment, problems = parse("\n\n<!-- readme: alpha -->\nbody\n")        # leading blank lines are fine
    assert fragment.target == "alpha" and fragment.body == "body"


def test_a_level_one_or_two_heading_is_rejected_but_deeper_ones_and_fenced_text_are_not():
    assert "heading" in parse("<!-- readme: alpha -->\n## New section\n")[1][0]
    assert "heading" in parse("<!-- readme: alpha -->\n# New\n")[1][0]
    assert parse("<!-- readme: alpha -->\n### Fine\ntext\n")[1] == []
    assert parse("<!-- readme: alpha -->\n```bash\n# a comment, not a heading\n## also\n```\n")[1] == []


def test_an_anchor_a_conflict_marker_an_open_fence_and_an_empty_body_are_rejected():
    assert "anchor" in parse("<!-- readme: alpha -->\n<!-- help:x -->\n")[1][0]
    assert "conflict" in parse("<!-- readme: alpha -->\ntext\n<<<<<<< HEAD\n")[1][0]
    assert "fence" in parse("<!-- readme: alpha -->\n```\ncode\n")[1][0]
    assert "no text" in parse("<!-- readme: alpha -->\n\n")[1][0]
    assert "placeholder" in parse("<!-- readme: alpha -->\nREPLACE ME: x\n")[1][0]


# -- checking a set of fragments ---------------------------------------------------------

def test_an_unknown_section_is_an_error_naming_the_fragment(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "nowhere", "text\n")
    errors = rf.validate(root, MINI)
    assert errors == [f"readme.d/{A1}: `nowhere` is not a README section (run `targets` for the list)"]


def test_two_fragments_saying_the_same_thing_are_an_error(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "alpha", "same   words\n")
    frag(root, A2, "beta", "same words\n")
    assert any("says the same as" in e for e in rf.validate(root, MINI))


def test_a_misnamed_markdown_file_is_an_error_because_it_would_never_fold(tmp_path):
    root = project(tmp_path)
    frag(root, "notes.md", "alpha", "text\n")
    (root / "readme.d" / "README.md").write_text("rules\n")
    errors = rf.validate(root, MINI)
    assert len(errors) == 1 and "notes.md" in errors[0] and "never fold" in errors[0]


def test_a_fragment_already_in_the_readme_is_stale_and_a_half_present_one_is_an_error(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "alpha", "alpha text\n")                                  # fully present
    frag(root, A2, "beta", "beta text\n\nbrand new paragraph\n")             # half present
    fold, stale, errors = rf.classify(root, MINI)
    assert [f.path.name for f in stale] == [A1] and fold == []
    assert len(errors) == 1 and "only some of its paragraphs" in errors[0]


# -- rendering -----------------------------------------------------------------------

def test_a_fragment_lands_at_the_end_of_its_section_before_the_rule(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "alpha", "new alpha words\n")
    out = rf.build(root, write=False).text
    assert "alpha text\n\nnew alpha words\n\n---\n\n<!-- help:beta -->" in out


def test_the_last_section_with_no_rule_after_it_gets_the_text_at_the_end(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "gamma", "tail words\n")
    assert rf.build(root, write=False).text.endswith("gamma text, the last section\n\ntail words\n")


def test_a_footer_heading_without_an_anchor_stays_below_the_new_text(tmp_path):
    root = project(tmp_path, MINI + "\n---\n\n## Credits\n\nmade by someone\n")
    frag(root, A1, "gamma", "tail words\n")
    out = rf.build(root, write=False).text
    assert out.index("gamma text") < out.index("tail words") < out.index("## Credits")
    assert "tail words\n\n---\n\n## Credits" in out


def test_a_rule_inside_a_section_followed_by_ordinary_text_is_not_its_end(tmp_path):
    root = project(tmp_path, MINI.replace("alpha text\n", "alpha text\n\n---\n\nmore alpha after a rule\n"))
    frag(root, A1, "alpha", "appended\n")
    out = rf.build(root, write=False).text
    assert out.index("more alpha after a rule") < out.index("appended") < out.index("<!-- help:beta -->")


def test_a_rule_inside_a_code_fence_is_not_a_section_end(tmp_path):
    root = project(tmp_path, MINI.replace("alpha text\n", "alpha text\n\n```\n---\n\n## not a heading\n```\n"))
    frag(root, A1, "alpha", "appended\n")
    out = rf.build(root, write=False).text
    assert out.index("## not a heading") < out.index("appended")


def test_two_fragments_for_one_section_are_appended_oldest_first(tmp_path):
    root = project(tmp_path)
    frag(root, A2, "alpha", "second one\n")
    frag(root, A1, "alpha", "first one\n")
    out = rf.build(root, write=False).text
    assert out.index("first one") < out.index("second one") < out.index("---")


def test_everything_outside_the_target_is_byte_for_byte_unchanged(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "beta", "added\n")
    out = rf.build(root, write=False).text
    before, after = MINI.split("<!-- help:beta -->")[0], out.split("<!-- help:beta -->")[0]
    assert before == after
    assert out.split("<!-- help:gamma -->")[1] == MINI.split("<!-- help:gamma -->")[1]


def test_crlf_and_bom_are_kept(tmp_path):
    root = project(tmp_path, "﻿" + MINI, eol="\r\n")
    frag(root, A1, "alpha", "line one\nline two\n")
    out = rf.build(root, write=False).text
    assert out.startswith("﻿") and "\n" not in out.replace("\r\n", "")
    assert "line one\r\nline two\r\n\r\n---" in out


def test_a_fenced_block_in_a_fragment_survives_intact(tmp_path):
    root = project(tmp_path)
    frag(root, A1, "alpha", "```bash\n# keep me\npython x.py\n```\n")
    assert "```bash\n# keep me\npython x.py\n```\n\n---" in rf.build(root, write=False).text


# -- build --------------------------------------------------------------------------

def test_build_folds_writes_and_deletes(tmp_path):
    root = project(tmp_path)
    path = frag(root, A1, "alpha", "new alpha words\n")
    result = rf.build(root, write=True)
    assert result.folded == [path] and not path.exists()
    assert "new alpha words" in (root / "README.md").read_text(encoding="utf-8")
    assert not list(root.glob(".README.*.tmp"))


def test_preview_changes_nothing(tmp_path):
    root = project(tmp_path)
    path = frag(root, A1, "alpha", "new\n")
    rf.build(root, write=False)
    assert path.exists() and (root / "README.md").read_text(encoding="utf-8") == MINI


def test_one_invalid_fragment_blocks_the_whole_build_and_nothing_changes(tmp_path):
    root = project(tmp_path)
    good = frag(root, A1, "alpha", "fine\n")
    frag(root, A2, "nowhere", "bad\n")
    with pytest.raises(rf.ReadmeFragmentError, match="nothing changed"):
        rf.build(root, write=True)
    assert good.exists() and (root / "README.md").read_text(encoding="utf-8") == MINI


def test_a_failed_deletion_is_recovered_by_running_build_again_without_duplicating(tmp_path, monkeypatch):
    root = project(tmp_path)
    path = frag(root, A1, "alpha", "once only\n")
    real = Path.unlink

    def flaky(self, missing_ok=False):
        if self == path:
            raise PermissionError("locked")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", flaky)
    with pytest.raises(rf.ReadmeFragmentError, match="could not be deleted"):
        rf.build(root, write=True)
    assert path.exists() and (root / "README.md").read_text(encoding="utf-8").count("once only") == 1
    monkeypatch.setattr(Path, "unlink", real)
    done = rf.build(root, write=True)
    assert done.stale == [path] and not path.exists()
    assert (root / "README.md").read_text(encoding="utf-8").count("once only") == 1


def test_a_failed_readme_write_leaves_every_fragment_in_place(tmp_path, monkeypatch):
    root = project(tmp_path)
    path = frag(root, A1, "alpha", "words\n")

    def broken(root_, path_, text):
        raise OSError("disk full")
    monkeypatch.setattr(rf, "_write_atomically", broken)
    with pytest.raises(OSError):
        rf.build(root, write=True)
    assert path.exists() and (root / "README.md").read_text(encoding="utf-8") == MINI


def test_build_with_nothing_pending_is_a_no_op(tmp_path):
    root = project(tmp_path)
    assert rf.build(root, write=True).folded == []
    assert (root / "README.md").read_text(encoding="utf-8") == MINI


# -- the command line -------------------------------------------------------------------

def run(root, *args):
    return rf.main(["--root", str(root), *args])


def test_cli_new_check_and_build_round_trip(tmp_path, capsys):
    root = project(tmp_path)
    assert run(root, "targets") == 0 and "beta" in capsys.readouterr().out
    assert run(root, "new", "topic", "alpha") == 0
    path = root / capsys.readouterr().out.strip()
    assert run(root, "check") == 1 and "placeholder" in capsys.readouterr().err
    path.write_text("<!-- readme: alpha -->\nreal words\n", encoding="utf-8")
    assert run(root, "check") == 0 and "1 fragment(s) ok" in capsys.readouterr().out
    assert run(root, "check", "--no-pending") == 1 and "pending" in capsys.readouterr().err
    assert run(root, "build") == 0 and "folded 1" in capsys.readouterr().out
    assert run(root, "check", "--no-pending") == 0


def test_cli_reports_a_bad_target_without_a_traceback(tmp_path, capsys):
    assert run(project(tmp_path), "new", "x", "nope") == 1
    assert capsys.readouterr().err.startswith("readme: 'nope' is not a README section")


# -- the real repository ----------------------------------------------------------------

def test_every_pending_fragment_in_this_repository_is_valid():
    """The gate: runs in the suite, so a broken fragment cannot reach master."""
    text = (ROOT / "README.md").read_bytes().decode("utf-8")
    assert rf.validate(ROOT, text) == []


def test_every_pending_fragment_folds_into_the_real_readme_keeping_its_line_endings():
    text = (ROOT / "README.md").read_bytes().decode("utf-8")
    fold, stale, errors = rf.classify(ROOT, text)
    assert errors == []
    out = rf.render(text, fold)
    assert out.count("\r\n") == out.count("\n") or "\r\n" not in text       # no mixed endings introduced
    assert rf.targets(out) == rf.targets(text)                              # no section added or lost
