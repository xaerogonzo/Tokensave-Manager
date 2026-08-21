"""tests/test_proposal_gate.py — the propose-only invariant, proven.

Architectural rule #1 of this project: *every write action goes through
ProposalDialog with a visible diff and explicit Apply / Reject.* It is the
one gate standing between an AI suggestion and the user's files, and until
now nothing tested it.

What is asserted here is the invariant itself rather than the dialog's
construction:

    build a proposal          -> nothing on disk changes
    Reject                    -> nothing on disk changes
    Apply                     -> exactly the reviewed change, and nothing else
    target changed meanwhile  -> the write is REFUSED

That last one is the point of the whole exercise. A diff dialog is not a
safety gate if the file can change after the diff was drawn and Apply writes
over it regardless — the user would have approved one thing and received
another. The protection exists (`_check_write_race`, sha256 of the content
captured when the proposal was built); these tests pin it, and the two
mutation checks at the bottom prove they would notice if it were removed.

Real files in `tmp_path` throughout: the subject is what ends up on disk, so
mocking the filesystem would assert the model instead of the behaviour.
"""
from __future__ import annotations

import os

import pytest

from agent_tools import _tool_write_file


@pytest.fixture
def project(tmp_path):
    (tmp_path / "notes.md").write_text("original text\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def write_tool(project):
    return _tool_write_file(str(project))


def _build(tool, path="notes.md", content="proposed text\n",
           rationale="because"):
    return tool.proposal_builder(
        {"path": path, "content": content, "rationale": rationale})


def _read(project, name="notes.md"):
    return (project / name).read_text(encoding="utf-8")


# ── proposing changes nothing ────────────────────────────────────────────

def test_building_a_proposal_writes_nothing(write_tool, project):
    """The diff is drawn from memory; the file is untouched until Apply."""
    proposal = _build(write_tool)
    assert proposal.proposed_content == "proposed text\n"
    assert _read(project) == "original text\n"


def test_building_a_proposal_for_a_new_file_creates_nothing(write_tool,
                                                            project):
    """Not even the parent directories, which are only surfaced as intent."""
    proposal = _build(write_tool, path="docs/deep/new.md")
    assert proposal.dirs_to_create
    assert not (project / "docs").exists()


def test_the_proposal_captures_what_the_user_is_reviewing(write_tool):
    """The Original pane and the race check must come from the same read."""
    proposal = _build(write_tool)
    assert proposal.original_content == "original text\n"
    assert proposal.original_hash, "no hash means no race protection"


# ── rejecting changes nothing ────────────────────────────────────────────

def test_rejecting_leaves_the_file_alone(write_tool, project):
    """Reject is modelled as "post_accept is never called".

    That is exactly what `agent._dispatch_tool` does, and it is the reason
    the write lives in `post_accept` rather than in the builder.
    """
    _build(write_tool)
    assert _read(project) == "original text\n"


def test_the_write_tool_cannot_be_invoked_without_the_gate(write_tool):
    """Defence in depth: the plain handler must refuse.

    `is_write=True` tools are routed through proposal_builder + post_accept.
    If dispatch ever regressed to calling `handler`, this refusal is what
    stops a silent ungated write.
    """
    result = write_tool.handler({"path": "notes.md", "content": "x"})
    assert result.startswith("[tool error]")
    assert write_tool.is_write is True
    assert write_tool.proposal_builder is not None
    assert write_tool.post_accept is not None


# ── applying writes exactly what was reviewed ────────────────────────────

def test_applying_writes_the_proposed_content(write_tool, project):
    proposal = _build(write_tool)
    result = write_tool.post_accept(proposal, proposal.proposed_content)
    assert not result.startswith("[")
    assert _read(project) == "proposed text\n"


def test_applying_writes_the_EDITED_content_not_the_proposal(write_tool,
                                                             project):
    """The proposed pane is editable, so the final content is the contract.

    Writing `proposal.proposed_content` instead would silently discard the
    user's corrections while showing them as accepted.
    """
    proposal = _build(write_tool)
    write_tool.post_accept(proposal, "the user rewrote this\n")
    assert _read(project) == "the user rewrote this\n"


def test_applying_touches_only_the_target(write_tool, project):
    (project / "sibling.md").write_text("untouched\n", encoding="utf-8")
    proposal = _build(write_tool)
    write_tool.post_accept(proposal, proposal.proposed_content)
    assert _read(project, "sibling.md") == "untouched\n"


def test_no_temp_file_survives_a_successful_write(write_tool, project):
    """The atomic write goes via `.tmp` + os.replace; nothing may linger."""
    proposal = _build(write_tool)
    write_tool.post_accept(proposal, "x\n")
    assert [p.name for p in project.iterdir() if p.name.endswith(".tmp")] == []


# ── the stale-target gate ────────────────────────────────────────────────

def test_a_file_changed_since_the_preview_refuses_the_write(write_tool,
                                                            project):
    """The strongest assertion here.

    The user reviewed a diff against "original text". By the time they click
    Apply the file says something else, so the diff they approved no longer
    describes the change. Writing anyway would apply an approval the user
    never gave to this content, and silently discard whatever arrived in the
    meantime.
    """
    proposal = _build(write_tool)
    (project / "notes.md").write_text("someone else edited this\n",
                                      encoding="utf-8")

    result = write_tool.post_accept(proposal, proposal.proposed_content)

    assert result.startswith("[write rejected")
    assert "changed on disk" in result
    assert _read(project) == "someone else edited this\n", (
        "the refusal must also preserve the other edit")


def test_a_target_created_since_the_preview_refuses_the_write(write_tool,
                                                              project):
    """The mirror case: the proposal was for a file that did not exist.

    The user approved "create this new file", not "overwrite the one that
    just appeared" — a distinction the empty-hash branch exists to keep.
    """
    proposal = _build(write_tool, path="brand_new.md")
    assert proposal.original_hash == ""
    (project / "brand_new.md").write_text("appeared from elsewhere\n",
                                          encoding="utf-8")

    result = write_tool.post_accept(proposal, "my content\n")

    assert result.startswith("[write rejected")
    assert "created on disk" in result
    assert _read(project, "brand_new.md") == "appeared from elsewhere\n"


def test_an_unchanged_target_is_not_falsely_refused(write_tool, project):
    """The gate must not fire on a file nobody touched.

    A conflict check that cried wolf would be turned off, and then the real
    protection goes with it.
    """
    proposal = _build(write_tool)
    result = write_tool.post_accept(proposal, proposal.proposed_content)
    assert not result.startswith("[write rejected")


def test_rewriting_the_same_bytes_is_still_not_a_conflict(write_tool, project):
    """Identical content is identical hash — no spurious refusal."""
    proposal = _build(write_tool)
    (project / "notes.md").write_text("original text\n", encoding="utf-8")
    result = write_tool.post_accept(proposal, "new\n")
    assert not result.startswith("[write rejected")


# ── containment ──────────────────────────────────────────────────────────

def test_a_path_outside_the_project_is_refused(write_tool):
    """Approval covers a file in the project, not anywhere on the disk."""
    result = _build(write_tool, path="../escape.md")
    assert isinstance(result, str) and result.startswith("[tool error]")
    assert "outside the project root" in result


def test_a_symlink_escaping_the_project_is_refused(write_tool, project,
                                                   tmp_path_factory):
    """The boundary that matters: a link out of the project is not followed.

    Containment is enforced by `_under_project`, which compares realpaths, so
    the escape is caught after resolution rather than by spotting the link.
    """
    outside_dir = tmp_path_factory.mktemp("elsewhere")
    secret = outside_dir / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = project / "escape.md"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks not permitted on this machine")

    result = _build(write_tool, path="escape.md")

    assert isinstance(result, str) and result.startswith("[tool error]")
    assert "outside the project root" in result
    assert secret.read_text(encoding="utf-8") == "secret"


def test_a_symlink_inside_the_project_proposes_against_its_real_target(
        write_tool, project):
    """Resolved, not refused, and the diff shows the file that will change.

    Worth pinning because it documents where the guard actually lives: the
    explicit `os.path.islink` check in the builder is unreachable in practice,
    since `_under_project` has already returned a realpath by then. Nothing is
    lost, because realpath containment is what stops an escape and the user
    reviews the resolved file's real content rather than the link's name.
    """
    real = project / "real.md"
    real.write_text("the actual content", encoding="utf-8")
    link = project / "alias.md"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks not permitted on this machine")

    proposal = _build(write_tool, path="alias.md")

    assert not isinstance(proposal, str)
    assert os.path.basename(proposal.filepath) == "real.md"
    assert proposal.original_content == "the actual content"


# ── the gate can actually fail ───────────────────────────────────────────

def test_the_stale_check_is_load_bearing(write_tool, project, mocker):
    """Disable the race check and the protective test must stop passing.

    A safety test that would pass with the safety removed is decoration.
    """
    import agent_tools
    mocker.patch.object(agent_tools, "_check_write_race", return_value=None)

    proposal = _build(write_tool)
    (project / "notes.md").write_text("someone else edited this\n",
                                      encoding="utf-8")
    write_tool.post_accept(proposal, proposal.proposed_content)

    assert _read(project) == "proposed text\n", (
        "with the check disabled the write lands — which is what the test "
        "above is protecting against")


def test_the_hash_is_what_detects_the_change_not_the_mtime(write_tool,
                                                           project):
    """mtime is diagnostics only, per the WriteProposal contract.

    Content restored to its original bytes is not a conflict even though the
    mtime moved; a mtime-based check would refuse it.
    """
    proposal = _build(write_tool)
    (project / "notes.md").write_text("temporarily different\n",
                                      encoding="utf-8")
    (project / "notes.md").write_text("original text\n", encoding="utf-8")
    result = write_tool.post_accept(proposal, "final\n")
    assert not result.startswith("[write rejected")
    assert _read(project) == "final\n"
