"""Regenerate the manager-owned region of an existing PR body.

Draft PR only ever *creates*. Updating an open PR meant running
`gh pr edit --body-file` by hand, which replaces the whole body and silently
discards anything a human wrote on GitHub.

So the body is split in two: a region between manager markers, which this
rewrites, and everything else, which it never touches.

## The concurrency contract, and why it is scoped to the region

There is a window between reading a PR body and writing it back. A
whole-body comparison closes that window but aborts on the very case the
feature is meant to support — someone adding a note *outside* the markers
while the draft is being generated.

So the guard compares only the manager-owned region:

    1. fetch body A
    2. record A's region
    3. generate replacement region B
    4. fetch body C immediately before writing
    5. if C's region != A's region -> ABORT (someone edited what we own)
    6. otherwise splice B into C, preserving everything outside the region

Step 6 writes into **C, not A**. Writing the spliced A would revert any edit
made outside the region during the window — quietly undoing a human's work
while reporting success.

Writes go through ``pr_checklist.update_pr_body``, i.e. stdin-piped
``gh api ... --input -``. Never ``-f body=``, which URL-encodes the value and
mangles the ``&``, ``=`` and ``#`` that PR bodies are full of.
"""

from __future__ import annotations

from dataclasses import dataclass

from helpers.pr_checklist import get_open_pr, update_pr_body

BEGIN_MARKER = "<!-- tokensave-manager:pr-body v1 -->"
END_MARKER = "<!-- /tokensave-manager:pr-body v1 -->"

# Outcomes. Each is a distinct thing that happened, because "it didn't work"
# is not actionable and the remedies differ.
OK = "ok"
NO_PR = "no_pr"
NO_REGION = "no_region"
UNCHANGED = "unchanged"
CONFLICT = "conflict"
WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class RefreshResult:
    status: str
    message: str
    pr_number: int = 0
    old_region: str = ""
    new_region: str = ""

    def __bool__(self) -> bool:
        return self.status in (OK, UNCHANGED)

    @property
    def changed(self) -> bool:
        return self.status == OK


def has_region(body: str) -> bool:
    """True when *body* carries a well-formed manager region."""
    return extract_region(body) is not None


def extract_region(body: str) -> "str | None":
    """The text between the markers, or None when there is no valid region.

    None covers a missing marker, and also markers in the wrong order — a
    reversed pair is not an empty region, it is a body we do not understand,
    and splicing into it would corrupt it.
    """
    if not body:
        return None
    start = body.find(BEGIN_MARKER)
    if start == -1:
        return None
    end = body.find(END_MARKER, start + len(BEGIN_MARKER))
    if end == -1:
        return None
    return body[start + len(BEGIN_MARKER):end]


def wrap_region(content: str) -> str:
    """Wrap freshly generated content in the markers."""
    return f"{BEGIN_MARKER}\n{content.strip()}\n{END_MARKER}"


def replace_region(body: str, new_content: str) -> "str | None":
    """Splice *new_content* into *body*'s region, preserving everything else.

    Returns None when *body* has no valid region, so the caller must decide
    rather than being handed a body with content appended somewhere arbitrary.
    """
    if not body:
        return None
    start = body.find(BEGIN_MARKER)
    if start == -1:
        return None
    end = body.find(END_MARKER, start + len(BEGIN_MARKER))
    if end == -1:
        return None
    return (body[:start]
            + wrap_region(new_content)
            + body[end + len(END_MARKER):])


def plan_refresh(old_body: str, latest_body: str,
                 new_content: str) -> RefreshResult:
    """Decide what to do, given the body we read and the one on the server now.

    Pure: no network, so the whole concurrency rule is directly testable.
    """
    old_region = extract_region(old_body)
    if old_region is None:
        return RefreshResult(
            NO_REGION,
            "This PR body has no manager-managed section, so there is "
            "nothing to refresh without overwriting hand-written text. "
            "Regenerate the body with Draft PR first.")

    latest_region = extract_region(latest_body)
    if latest_region is None:
        return RefreshResult(
            CONFLICT,
            "The manager-managed section was removed on GitHub since this "
            "draft was generated — refresh aborted.")

    if latest_region != old_region:
        return RefreshResult(
            CONFLICT,
            "The manager-managed section changed on GitHub since this draft "
            "was generated — refresh aborted so that edit is not lost. "
            "Re-open the refresh to start from the current body.",
            old_region=old_region, new_region=latest_region)

    if latest_region.strip() == new_content.strip():
        return RefreshResult(UNCHANGED, "PR body already up to date.",
                             old_region=old_region)

    return RefreshResult(OK, "ready", old_region=old_region,
                         new_region=new_content)


def refresh_pr_body(gh_exe: str, project_root: str, old_body: str,
                    new_content: str,
                    pr_number: "int | None" = None) -> RefreshResult:
    """Re-read the PR, verify nothing moved under us, then splice and write.

    *old_body* is the body the draft was generated against — the caller keeps
    it from when it built the preview, which is what makes the staleness
    check meaningful.
    """
    pr = get_open_pr(gh_exe, project_root)
    if pr is None:
        return RefreshResult(
            NO_PR, "No open PR detected for this branch. Push the branch "
                   "and open a PR first.")
    number = pr_number or pr.get("number")
    latest_body = pr.get("body") or ""
    if not isinstance(number, int):
        return RefreshResult(NO_PR,
                             "Could not read the PR number from `gh pr view`.")

    plan = plan_refresh(old_body, latest_body, new_content)
    if plan.status != OK:
        return RefreshResult(plan.status, plan.message, pr_number=number,
                             old_region=plan.old_region,
                             new_region=plan.new_region)

    # Splice into the body we just fetched, NOT the one we started from, so
    # edits made outside the region during the window survive.
    spliced = replace_region(latest_body, new_content)
    if spliced is None:
        return RefreshResult(
            CONFLICT, "The manager-managed section could not be located in "
                      "the current PR body — refresh aborted.",
            pr_number=number)

    ok, msg = update_pr_body(gh_exe, project_root, number, spliced)
    if not ok:
        return RefreshResult(WRITE_FAILED,
                             f"PR #{number} update failed: {msg}",
                             pr_number=number)
    return RefreshResult(OK, f"PR #{number} body refreshed.",
                         pr_number=number,
                         old_region=plan.old_region, new_region=new_content)
