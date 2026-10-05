"""Which tokensave versions the Manager has OBSERVED, and which releases lie
between two of them.

The integration audit used to ask "what releases are newer than the binary I
see right now?" -- an empty list right after an upgrade, which read as "nothing
to review". The question it needs is "what interval was observed, and which
releases fall inside it?". That needs a recorded baseline, and a baseline can be
missing, damaged, or stale. Each of those is its own state and none of them is
ever rendered as "no newer releases" (BASIC_INSTRUCTIONS D1b: unknown is never
false).

Two config keys, both stored without a ``v`` prefix:

``tokensave_last_seen_version``
    The last version observed on disk.
``tokensave_version_transition``
    ``{"from", "to", "observed_at"}`` -- the latest observed UPWARD change. It is
    a transition, not an "upgrade": the Manager saw two different versions, not
    what replaced the binary, and ``observed_at`` is when it noticed.

Pure: no UI, no subprocess, no clock reads except via the ``now`` parameter.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import Iterable

KEY_LAST_SEEN = "tokensave_last_seen_version"
KEY_TRANSITION = "tokensave_version_transition"

RECORDED = "RECORDED"
OVERRIDDEN = "OVERRIDDEN"
STALE = "STALE"
UNKNOWN = "UNKNOWN"

SKIPPED = "skipped"
INSTALLED = "installed"

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?![\d.])")


def version_key(value) -> tuple[int, int, int, int] | None:
    """Parse ``7.14.1`` / ``v7.14.1`` / ``1.0.4.1`` into a comparable tuple.

    ``None`` on anything else. A pre-release suffix is ignored, as in the
    integration script. This is its own primitive because
    ``helpers.detection._version_lt`` falls back to a STRING comparison when
    parsing fails, which orders garbage silently.
    """
    if not isinstance(value, str):
        return None
    m = _VERSION.match(value.strip())
    if not m:
        return None
    major, minor, patch, hotfix = m.groups()
    return (int(major), int(minor), int(patch), int(hotfix or 0))


def canonical(value) -> str | None:
    """The stored/compared spelling: no ``v`` prefix. ``None`` if unparseable."""
    key = version_key(value)
    if key is None:
        return None
    text = value.strip().lstrip("v")
    return re.match(r"\d+\.\d+\.\d+(?:\.\d+)?", text).group(0)


def display(value: str) -> str:
    """The report spelling: ``v7.14.1``."""
    return "v" + value.lstrip("v")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _transition(raw: dict):
    """``(from, to, observed_at)`` when the stored transition is well formed
    and ``from < to``; otherwise ``None``."""
    rec = raw.get(KEY_TRANSITION)
    if not isinstance(rec, dict):
        return None
    frm, to = canonical(rec.get("from")), canonical(rec.get("to"))
    observed = rec.get("observed_at")
    if frm is None or to is None or not isinstance(observed, str):
        return None
    if not version_key(frm) < version_key(to):
        return None
    return frm, to, observed


def observe(raw: dict, installed, now: str | None = None) -> bool:
    """Record what is installed. Returns True when ``raw`` changed (caller saves).

    * unparseable ``installed``      -> nothing is touched.
    * no ``last_seen``               -> seeded; no transition (nothing to compare).
    * malformed ``last_seen``        -> left exactly as found. Reseeding would
      destroy the evidence that something is wrong; the span reads UNKNOWN.
    * same version                   -> no change, so background polling does not
      write the file.
    * newer                          -> transition recorded, ``last_seen`` advanced.
    * older (downgrade)              -> ``last_seen`` advanced only. The previous
      transition then reads STALE because its ``to`` no longer matches.
    """
    new = canonical(installed)
    if new is None:
        return False
    if KEY_LAST_SEEN not in raw:
        raw[KEY_LAST_SEEN] = new
        return True
    old = canonical(raw[KEY_LAST_SEEN])
    if old is None:
        return False
    if version_key(old) == version_key(new):
        return False
    if version_key(new) > version_key(old):
        raw[KEY_TRANSITION] = {
            "from": old, "to": new, "observed_at": now or _now_iso()}
    raw[KEY_LAST_SEEN] = new
    return True


@dataclasses.dataclass(frozen=True)
class Span:
    state: str
    from_: str | None = None
    to: str | None = None
    observed_at: str | None = None


def span_for(raw: dict, installed, override=None) -> Span:
    """Decide which interval the audit covers, and how far to trust it.

    ``override`` is a user-supplied FROM (``--from``). It is report-only input
    and never written back, so it can never become recorded provenance.
    """
    to = canonical(installed)
    if to is None:
        return Span(UNKNOWN)
    if override is not None:
        frm = canonical(override)
        if frm is not None and version_key(frm) < version_key(to):
            return Span(OVERRIDDEN, frm, to)
        return Span(UNKNOWN)
    rec = _transition(raw)
    if rec is None:
        return Span(UNKNOWN)
    frm, rec_to, observed = rec
    if version_key(rec_to) == version_key(to):
        return Span(RECORDED, frm, to, observed)
    return Span(STALE, frm, rec_to, observed)


def format_install_dates(released: str | None, released_err: str | None,
                         mtime: float | None) -> list[str]:
    """The two dates beside ``Installed:``, as indented report lines.

    ``released`` is the GitHub ``published_at`` of the installed tag; ``mtime``
    is when the binary was last WRITTEN, which a copy or rebuild also moves, so
    it is labelled as that and never as "upgraded on". Either may be unknown and
    then says so with the reason: a missing line would read as "nothing to
    report" (BASIC_INSTRUCTIONS D1b).
    """
    if released:
        rel = f"  Released:   {released[:10]}   (GitHub release published)"
    else:
        rel = f"  Released:   unknown ({released_err or 'no release date found'})"
    if mtime is None:
        disk = "  On disk:    unknown (tokensave_exe not found)"
    else:
        day = datetime.datetime.fromtimestamp(mtime).date().isoformat()
        disk = f"  On disk:    {day}   (tokensave.exe last written)"
    return [rel, disk]


@dataclasses.dataclass(frozen=True)
class SpanRelease:
    tag: str
    version: str
    status: str
    published: str
    body: str


@dataclasses.dataclass(frozen=True)
class SpanReleases:
    releases: tuple[SpanRelease, ...]
    #: A published release exists whose tag is the FROM version. Independent
    #: of whether the fetch was complete: a complete list can still lack it.
    from_release_found: bool


def releases_in_span(releases: Iterable[dict], from_: str, to: str) -> SpanReleases:
    """Releases in ``(from_, to]``, sorted by version, never by API order.

    ``skipped`` is strictly between; ``installed`` is ``to``. Releases newer
    than ``to`` are not part of an interval that has already happened.
    Selection is unchanged from the script this replaces: a release counts if
    its tag parses, duplicates collapse, and nothing is filtered by draft or
    pre-release status.
    """
    lo, hi = version_key(from_), version_key(to)
    seen: dict[tuple, SpanRelease] = {}
    found_from = False
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tag_name") or ""
        key = version_key(tag)
        if key is None:
            continue
        if key == lo:
            found_from = True
            continue
        if not lo < key <= hi or key in seen:
            continue
        seen[key] = SpanRelease(
            tag=tag,
            version=canonical(tag),
            status=INSTALLED if key == hi else SKIPPED,
            published=(rel.get("published_at") or "")[:10],
            body=(rel.get("body") or "").strip(),
        )
    ordered = tuple(seen[k] for k in sorted(seen))
    return SpanReleases(ordered, found_from)
