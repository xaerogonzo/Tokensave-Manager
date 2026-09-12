"""Guards on the baseline compiler.

Two of these are the design rather than a feature. `test_no_function_returns_a
_policy_from_markdown` keeps the artifact from becoming a second policy store,
and `test_compilation_is_the_only_thing_that_changes_the_artifact` keeps the
compiler honest about its own output. If either fails, "the config is the source
of truth" is decoration.
"""

import inspect

from helpers import baseline_blocks as bb
from helpers.instruction_policy import InstructionPolicy

OFF = InstructionPolicy()
ON = InstructionPolicy(agent_may_commit=True, agent_may_push=True)


def _baseline(repo_root):
    import os

    path = os.path.join(repo_root, "templates", "project-baseline.md")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def _repo():
    import os

    # src/helpers/baseline_blocks.py -> repo root: three levels, not two.
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(bb.__file__))))


# ── The two load-bearing invariants ───────────────────────────────────────

def test_no_function_returns_a_policy_from_markdown():
    """The artifact must never become a second source of truth.

    Asserted structurally rather than by discipline: if no public function can
    hand back an `InstructionPolicy` derived from file text, a hand-edited block
    cannot be adopted as intent by anybody, including a future caller who never
    read this module's docstring.
    """
    for name, obj in vars(bb).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        returned = inspect.signature(obj).return_annotation
        assert "InstructionPolicy" not in str(returned), (
            "%s() returns a policy derived from text; drift must be computed "
            "by comparing rendered output, never by parsing intent back out"
            % name)


def test_compilation_is_the_only_thing_that_changes_the_artifact():
    """OFF renders its block, ON renders its own, and OFF again is byte-identical."""
    source = _baseline(_repo())
    off_text, refusal = bb.compile_baseline(source, OFF)
    assert refusal == ""
    assert bb.block_states(off_text, OFF) == {
        "agent_may_commit": bb.CURRENT, "agent_may_push": bb.CURRENT}

    on_text, _ = bb.compile_baseline(off_text, ON)
    assert bb.block_states(on_text, ON) == {
        "agent_may_commit": bb.CURRENT, "agent_may_push": bb.CURRENT}
    assert on_text != off_text

    back, _ = bb.compile_baseline(on_text, OFF)
    assert back == off_text, "off -> on -> off did not return the original bytes"


# ── Drift ─────────────────────────────────────────────────────────────────

def test_a_block_that_disagrees_with_policy_is_drifted_not_adopted():
    """The case the whole architecture exists for.

    A baseline compiled from a DIFFERENT policy — a hand edit, or a recompiled
    file arriving through `git pull` — reads as stale. Nothing here reports the
    file's opinion as the policy.
    """
    source = _baseline(_repo())
    on_text, _ = bb.compile_baseline(source, ON)
    assert bb.block_states(on_text, OFF) == {
        "agent_may_commit": bb.DRIFTED, "agent_may_push": bb.DRIFTED}


def test_line_endings_are_preserved_and_are_never_drift():
    """Whatever the file uses, the compile keeps — and neither form is stale.

    Two defects sit behind this test. The renderer emits LF while the file may
    be CRLF, so a raw comparison reports a freshly compiled baseline as stale
    and offers a repair that changes nothing.

    And the first version of THIS test read the checked-out template to find
    its CRLF — which asserts the platform, not the code. Git stores this repo's
    Markdown with LF and hands Windows a CRLF working copy, so it passed
    locally and went red on Linux CI. Explicit fixtures, both directions.
    """
    crlf = PRE_MIGRATION
    lf = PRE_MIGRATION.replace("\r\n", "\n")
    assert "\r\n" in crlf and "\r\n" not in lf

    out_crlf, _ = bb.compile_baseline(crlf, OFF)
    out_lf, _ = bb.compile_baseline(lf, OFF)
    assert "\r\n" in out_crlf, "CRLF input lost its line endings"
    assert "\r\n" not in out_lf, "LF input was given CRLF it never had"

    for compiled in (out_crlf, out_lf):
        assert bb.block_states(compiled, OFF) == {
            "agent_may_commit": bb.CURRENT, "agent_may_push": bb.CURRENT}


#: The pre-migration baseline, as a fixture rather than the live file.
#: A test that reads the real template to find legacy text can only pass until
#: the migration lands — which is exactly once.
PRE_MIGRATION = (
    "# Project Baseline Rules\r\n\r\n"
    "## Git Discipline\r\n\r\n"
    "- Commit logical units of work\r\n\r\n"
    "> **TokenSave Manager** (if installed): right-click any project → "
    "**\U0001f4dc Git Log**.\r\n"
    ">\r\n"
    "> " + bb.LEGACY_COMMIT_SIGNATURE + " Right-click the project and use the "
    "dialog. This is a preference, not a hard rule.\r\n\r\n"
    "## Code Quality\r\n\r\n- Keep functions small\r\n")


def test_an_uncompiled_baseline_reports_missing():
    assert bb.block_states(PRE_MIGRATION, OFF) == {
        "agent_may_commit": bb.MISSING, "agent_may_push": bb.MISSING}


def test_the_shipped_baseline_stays_compiled_and_current():
    """The ongoing guard: the template in the repo is a compiled artifact.

    If someone hand-edits a managed block, or adds a policy key without
    recompiling, this goes red — which is the whole point of the file being
    downstream of the policy.

    Asserted against BOTH renderings of each key rather than against one
    fixed policy. The earlier version pinned OFF, so the first time somebody
    legitimately ticked "agents may commit" and applied, this went red over a
    correctly compiled file -- it was testing which way the toggle happened to
    be set, not the property it is named for. What must hold is that every
    managed block is SOMETHING THE COMPILER PRODUCES; which of the two it is
    is the user's business, and it lives in manager-config.json.
    """
    source = _baseline(_repo())
    for key in ("agent_may_commit", "agent_may_push"):
        bounds = bb.find_block(source, key)
        assert bounds is not None, "%s block is missing entirely" % key
        start, end = bounds
        body = bb._lf(source[start:end])
        assert body in (bb._lf(bb.render_block(key, OFF)),
                        bb._lf(bb.render_block(key, ON))), (
            "%s has been hand-edited: its text is neither rendering" % key)
    assert bb.findings(source) == ()


# ── The legacy signature ──────────────────────────────────────────────────

def test_the_legacy_instruction_is_found_before_and_gone_after():
    assert bb.findings(PRE_MIGRATION) == (bb.UNMANAGED_LEGACY,)
    compiled, _ = bb.compile_baseline(PRE_MIGRATION, OFF)
    assert bb.findings(compiled) == ()
    assert bb.block_states(compiled, OFF) == {
        "agent_may_commit": bb.CURRENT, "agent_may_push": bb.CURRENT}


def test_prose_that_merely_resembles_policy_is_not_flagged():
    """A fixed signature, not a classifier.

    Recognising arbitrary text that "looks like policy" is a natural-language
    judgement this module has no business making, and a false positive accuses
    the user of drift in their own writing.
    """
    lookalike = ("## Notes\n\nWe prefer to let the manager handle commits, and "
                 "pushing is usually my job, not the agent's.\n")
    assert bb.findings(lookalike) == ()


def test_authored_prose_beside_the_legacy_line_survives():
    """Only the signature line goes; its blockquote sibling is the user's."""
    assert "Git Log" in PRE_MIGRATION
    compiled, _ = bb.compile_baseline(PRE_MIGRATION, OFF)
    assert "Git Log" in compiled, "an authored tip was swept up by the migration"
    assert "preference, not a hard rule" not in compiled



# ── Budget ────────────────────────────────────────────────────────────────

def test_a_write_that_would_breach_the_budget_is_refused_with_its_arithmetic():
    """Refused here rather than discovered by CI later."""
    filler = "x" * (bb.MAX_BASELINE_BYTES - 200)
    text, refusal = bb.compile_baseline(filler, OFF)
    assert text == filler, "a refused compile must not alter the input"
    assert "Refusing to write" in refusal
    assert str(bb.MAX_BASELINE_BYTES) in refusal


def test_budget_is_measured_the_way_the_guard_test_measures():
    """LF-normalised UTF-8 bytes — not the larger on-disk CRLF count.

    A previous budget raise recorded the on-disk figure and overstated the used
    budget by 123 B.
    """
    crlf = "a\r\nb\r\n"
    assert bb.budget_bytes(crlf) == len("a\nb\n".encode("utf-8"))


# ── Validity ──────────────────────────────────────────────────────────────

def test_an_invalid_policy_never_reaches_the_file():
    invalid = InstructionPolicy(agent_may_push=True, agent_may_commit=False)
    source = _baseline(_repo())
    text, refusal = bb.compile_baseline(source, invalid)
    assert text == source
    assert "require" in refusal


def test_rendering_is_deterministic_and_ordered():
    first = bb.render_region(ON)
    assert first == bb.render_region(ON)
    assert first.index("agent_may_commit") < first.index("agent_may_push")


def test_the_push_permission_forbids_rewriting_published_history():
    """Forbidding force-push alone still allows rewrite-then-publish."""
    text = bb.render_block("agent_may_push", ON)
    lowered = text.lower()
    for clause in ("force-push", "tags", "delete a remote branch", "rebase"):
        assert clause in lowered, "push permission is missing a boundary: %s" % clause
