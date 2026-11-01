"""Mirror the ledger's decisions into tokensave's own decision memory.

The ledger (`handoff_ledger`) is the record. tokensave's `record_decision` store
is a DERIVED INDEX of it: it exists so a session that asks tokensave what was
decided in this project gets the answer without knowing the ledger exists. If
the two ever disagree the ledger wins, and re-running the mirror repairs the
index.

Three properties, each because the alternative fails quietly:

* **Idempotent without a state file.** `record_decision` has no key, so a
  naive retry writes every decision twice. Rather than remember what was
  mirrored in a sidecar (which goes on saying "done" after the tokensave
  database is rebuilt or wiped), every entry is checked against the memory
  itself: each recorded decision carries the tag `handoff:<handoff_id>`, and a
  decision is written only when no decision with that tag and that text is
  there. A half-finished earlier run therefore resumes instead of duplicating.
* **Pending is a state, not a failure to hide.** tokensave missing, a call
  timing out, or a malformed reply leaves the entry `pending`. The ledger is
  untouched either way, and nothing here raises.
* **Decisions only.** Goals, next steps and open questions are session
  handoff material, not durable decisions; session markers carry no words.
  Only an entry's `decisions` are mirrored.

No UI imports: this is safe to call from the CLI, a hook or a controller.
"""
import dataclasses
import json
import subprocess

from constants import CREATE_NO_WINDOW
from helpers import handoff_ledger as ledger

STATE_MIRRORED = "mirrored"      # at least one decision was written now
STATE_ALREADY = "already"        # every decision was already in the memory
STATE_NONE = "none"              # nothing in this entry is a decision
STATE_PENDING = "pending"        # could not be mirrored; try again later

TAG_ALL = "handoff"
TAG_PREFIX = "handoff:"

#: A recall returns at most this many; a handoff has at most `MAX_ITEMS` decisions.
RECALL_LIMIT = 200
CALL_TIMEOUT_SECONDS = 15
MAX_REASON_CHARS = 300


@dataclasses.dataclass(frozen=True)
class MirrorResult:
    handoff_id: str
    state: str
    recorded: int = 0
    error: str = ""


def id_tag(handoff_id: str) -> str:
    return TAG_PREFIX + handoff_id


def decisions_of(entry: dict) -> list:
    """The decision strings of a handoff entry; `[]` for anything else."""
    if entry.get("kind") != ledger.KIND_HANDOFF:
        return []
    return [d for d in (entry.get("decisions") or []) if isinstance(d, str) and d.strip()]


def _call(exe: str, tool: str, project: str, payload: dict) -> "tuple[object, str]":
    """`(decoded reply, "")` or `(None, why)`. Never raises."""
    if not exe:
        return None, "tokensave is not configured (no tokensave_exe)"
    try:
        proc = subprocess.run(
            [exe, "tool", tool, "--project", project, "--args", json.dumps(payload)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CALL_TIMEOUT_SECONDS, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run tokensave: {exc}"
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"tokensave {tool} failed: {why[0] if why else 'exit ' + str(proc.returncode)}"
    try:
        return json.loads(proc.stdout), ""
    except ValueError:
        return None, f"tokensave {tool} returned something that is not JSON"


def _already_there(exe: str, project: str, handoff_id: str) -> "tuple[set | None, str]":
    """The decision texts already stored for this handoff, or `(None, why)`.

    `None` is "could not find out", which is not the same as "nothing there":
    writing on a failed lookup is exactly how a retry duplicates.
    """
    reply, why = _call(exe, "session_recall", project,
                       {"query": f'"{handoff_id}"', "limit": RECALL_LIMIT})
    if reply is None:
        return None, why
    rows = reply.get("decisions") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return None, "tokensave session_recall returned no decisions list"
    tag = id_tag(handoff_id)
    return {str(r.get("text", "")) for r in rows
            if isinstance(r, dict) and tag in (r.get("tags") or [])}, ""


def mirror_entry(project: str, entry: dict, tokensave_exe: str) -> MirrorResult:
    """Make the memory hold every decision of one ledger entry."""
    hid = str(entry.get("handoff_id") or "")
    decisions = decisions_of(entry)
    if not hid or not decisions:
        return MirrorResult(hid, STATE_NONE)
    have, why = _already_there(tokensave_exe, project, hid)
    if have is None:
        return MirrorResult(hid, STATE_PENDING, error=why)
    goal = str(entry.get("goal") or "").strip()
    who = f"handoff {hid} by {entry.get('agent', '?')}"
    reason = (f"{goal} [{who}]" if goal else f"[{who}]")[:MAX_REASON_CHARS]
    files = [f for f in (entry.get("agent_reported_files") or []) if isinstance(f, str)]
    tags = [TAG_ALL, id_tag(hid), f"agent:{entry.get('agent', '?')}"]
    written = 0
    for text in decisions:
        if text in have:
            continue
        reply, why = _call(tokensave_exe, "record_decision", project,
                           {"text": text, "reason": reason, "tags": tags, "files": files})
        if reply is None:
            # Stop at the first failure: what was written is tagged, so the next
            # run sees it and carries on from here.
            return MirrorResult(hid, STATE_PENDING, recorded=written, error=why)
        written += 1
    return MirrorResult(hid, STATE_MIRRORED if written else STATE_ALREADY,
                        recorded=written)


def mirror(project: str, tokensave_exe: str, git_exe: str = "",
           only: str = "") -> "tuple[list, str]":
    """Mirror the ledger (or just the entry `only`). Returns `(results, error)`.

    `error` is set when the ledger itself could not be read, which is a
    different fact from "there was nothing to mirror".
    """
    seen = ledger.read(project, git_exe=git_exe)
    if seen.state == ledger.STATE_UNREADABLE:
        return [], seen.error or "the ledger could not be read"
    results = []
    for entry in seen.entries:
        if only and entry.get("handoff_id") != only:
            continue
        if decisions_of(entry):
            results.append(mirror_entry(project, entry, tokensave_exe))
    return results, ""
