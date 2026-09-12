"""Compile an `InstructionPolicy` into the fleet baseline, and detect drift.

The baseline is a **compiled artifact**. This module writes it and reads it back
to compare — and deliberately offers **no function that returns a policy from
Markdown**. That absence is the architecture, not an oversight: the moment
something can read intent out of the file, a hand-edited block becomes a second
source of truth and the two disagree forever. Drift is therefore computed by
rendering the policy and comparing text, never by parsing a policy out.

WHAT IT TOUCHES. Only text between its own markers, which carry the policy key:

    <!-- TOKENSAVE-MANAGED: agent_may_commit:begin -->
    ...rendered instruction...
    <!-- TOKENSAVE-MANAGED: agent_may_commit:end -->

Everything else in the file is authored prose and is never rewritten — the rule
`instructions_wiring` already keeps, applied to a file that is now policy.

A NOTE ON THE MIGRATION, because it is not what it first looks like. The legacy
text this replaces says *"Prefer the manager's Git Commit dialog… This is a
preference, not a hard rule"*, and the baseline says **nothing whatever about
pushing**. So `agent_may_commit=False` is a deliberate STRENGTHENING of what the
fleet is told, and `agent_may_push=False` is entirely new instruction. This is
not a semantics-preserving migration and must not be described as one; the
Composer shows the exact before/after so the change is seen rather than
inferred.
"""

from __future__ import annotations

from helpers.instruction_policy import RENDERED_KEYS, InstructionPolicy

#: The budget guard in `tests/test_baseline_budget.py` is canonical; this
#: mirrors its measurement so the Composer can refuse a write the guard would
#: later fail. Measured the same way it measures: text-mode read, LF-normalised,
#: UTF-8 encoded. On-disk bytes are larger (one per line, the file is CRLF), and
#: a raise once overstated the used budget by 123 B by reading the wrong one.
MAX_BASELINE_BYTES = 12_000

_MARK = "<!-- TOKENSAVE-MANAGED: %s:%s -->"

#: Where the managed region is inserted on a file that has none yet. Chosen
#: because the instructions being compiled are git instructions and belong
#: beside the rest of them; a reader should not have to learn a new section.
_ANCHOR_HEADING = "## Git Discipline"

#: The exact legacy sentence being migrated. A FIXED SIGNATURE, not a
#: classifier: "does this prose look like policy?" is a natural-language
#: judgement this module has no business making, and a false positive would
#: accuse the user of drift in their own writing.
LEGACY_COMMIT_SIGNATURE = (
    "**Prefer the manager's Git Commit dialog over committing via Claude "
    "Code CLI.**")

# ── Per-key states ────────────────────────────────────────────────────────
CURRENT = "current"
DRIFTED = "drifted"
MISSING = "missing"

# ── File-level findings ───────────────────────────────────────────────────
UNMANAGED_LEGACY = "unmanaged_legacy"


#: What each key renders, in each state. Both states of both keys produce text:
#: an OFF policy is an instruction too, and the fleet needs to be told it.
_RENDERINGS = {
    ("agent_may_commit", False): (
        "**Do not create git commits.** The TokenSave Manager commits for this "
        "project — right-click it there and use **📝 Git Commit…**, which drafts "
        "the message with a locally-configured LLM at near-zero cost. Leave the "
        "work staged or in the working tree and say what you changed."
    ),
    ("agent_may_commit", True): (
        "**You may create a local git commit** when the work the user asked for "
        "is complete. Commit logical units, explain *why* in the message, and "
        "do not commit generated files or secrets."
    ),
    ("agent_may_push", False): (
        "**Do not push to any remote.** Publishing is the user's decision; leave "
        "commits local and say that they are ready."
    ),
    ("agent_may_push", True): (
        "**You may push the current branch to its configured upstream.** Never "
        "force-push. Never push tags and never delete a remote branch. Do not "
        "amend, rebase or reset commits that have already been pushed — "
        "forbidding force-push alone still leaves room to rewrite history and "
        "publish the result."
    ),
}


def _lf(text: str) -> str:
    """LF-normalised. One definition, because three call sites need the same one."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def budget_bytes(text: str) -> int:
    """Size the way the guard test sizes, so a refusal here matches a failure there."""
    return len(_lf(text).encode("utf-8"))


def render_block(key: str, policy: InstructionPolicy) -> str:
    """One key's managed block, markers included. Deterministic."""
    body = _RENDERINGS[(key, bool(getattr(policy, key)))]
    return "%s\n%s\n%s" % (_MARK % (key, "begin"), body, _MARK % (key, "end"))


def render_region(policy: InstructionPolicy) -> str:
    """Every rendered key, in `RENDERED_KEYS` order.

    Order is fixed rather than derived, so toggling one key can never reorder
    another's block and produce a diff nobody asked for.
    """
    return "\n\n".join(render_block(key, policy) for key in RENDERED_KEYS)


def find_block(text: str, key: str) -> "tuple | None":
    """`(start, end)` spanning one key's block including markers, or None."""
    begin = _MARK % (key, "begin")
    end = _MARK % (key, "end")
    start = (text or "").find(begin)
    if start < 0:
        return None
    stop = text.find(end, start)
    if stop < 0:
        return None
    return (start, stop + len(end))


def block_states(text: str, policy: InstructionPolicy) -> dict:
    """Per-key `CURRENT` / `DRIFTED` / `MISSING`, by comparing rendered text.

    Note what this does NOT do: it never decides what the policy *is* from what
    the file says. A block reading "you may commit" while the policy says
    otherwise is `DRIFTED` — a stale artifact — and the config is untouched.
    """
    states = {}
    for key in RENDERED_KEYS:
        bounds = find_block(text, key)
        if bounds is None:
            states[key] = MISSING
            continue
        # Compare LF-normalised. The file is CRLF and `render_block` emits LF,
        # so a raw comparison reports DRIFTED on a file this module has just
        # written — a freshly compiled baseline that claims to be stale, and a
        # repair button that changes nothing.
        present = _lf(text[bounds[0]:bounds[1]])
        states[key] = CURRENT if present == _lf(render_block(key, policy)) \
            else DRIFTED
    return states


def findings(text: str) -> tuple:
    """File-level observations that are not per-key drift.

    Only one today: the legacy instruction still present outside our markers.
    Reported so the user can see it; never rewritten, because prose outside the
    markers is theirs.
    """
    body = _lf(text)
    for key in RENDERED_KEYS:
        bounds = find_block(body, key)
        if bounds:
            body = body[:bounds[0]] + body[bounds[1]:]
    return (UNMANAGED_LEGACY,) if LEGACY_COMMIT_SIGNATURE in body else ()


def _newline_of(text: str) -> str:
    """Delegated to the one owner of this fact, never re-implemented here."""
    from helpers.instructions_wiring import dominant_newline

    return dominant_newline(text or "")


def _strip_legacy(text: str) -> str:
    """Remove the legacy blockquote line, and the `>` separator left dangling.

    Scoped to the exact signature line. The sibling Git-Log tip in the same
    blockquote is authored prose and stays.
    """
    lines = text.split("\n")
    out = []
    for line in lines:
        if LEGACY_COMMIT_SIGNATURE in line:
            # Drop a bare ">" immediately above it, which existed only to
            # separate this paragraph from the tip that remains.
            if out and out[-1].strip() in (">", ">\r"):
                out.pop()
            continue
        out.append(line)
    return "\n".join(out)


def compile_baseline(text: str, policy: InstructionPolicy) -> "tuple[str, str]":
    """`(new_text, refusal)`. Refusal is `""` when the write may proceed.

    Idempotent by construction: existing blocks are replaced in place, so a
    second compile of the same policy is a no-op, and off→on→off returns the
    original bytes.
    """
    invalid = policy.validate()
    if invalid:
        return text, invalid

    newline = _newline_of(text)
    body = _strip_legacy(_lf(text))

    region = render_region(policy)
    first = find_block(body, RENDERED_KEYS[0])
    if first is None:
        body = _insert_region(body, region)
    else:
        # Replace each existing block in place; append any key that has none.
        for key in RENDERED_KEYS:
            bounds = find_block(body, key)
            rendered = render_block(key, policy)
            if bounds:
                body = body[:bounds[0]] + rendered + body[bounds[1]:]
            else:
                anchor = find_block(body, RENDERED_KEYS[0])
                at = anchor[1] if anchor else len(body)
                body = body[:at] + "\n\n" + rendered + body[at:]

    size = budget_bytes(body)
    if size > MAX_BASELINE_BYTES:
        return text, (
            "Refusing to write: the compiled baseline would be %d B, over the "
            "%d B review budget that tests/test_baseline_budget.py enforces "
            "(%d B over). Every byte here is paid on every message in every "
            "wired project." % (size, MAX_BASELINE_BYTES,
                                size - MAX_BASELINE_BYTES))

    return body.replace("\n", newline) if newline != "\n" else body, ""


def _insert_region(body: str, region: str) -> str:
    """Place a first-time region under the git heading, or at the end."""
    at = body.find(_ANCHOR_HEADING)
    if at < 0:
        return body.rstrip("\n") + "\n\n" + region + "\n"
    # After the heading's own paragraph block, so the authored bullets keep
    # their position and the compiled instructions follow them.
    nxt = body.find("\n## ", at + len(_ANCHOR_HEADING))
    if nxt < 0:
        return body.rstrip("\n") + "\n\n" + region + "\n"
    return body[:nxt] + "\n" + region + "\n" + body[nxt:]
