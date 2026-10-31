"""The models the Cursor CLI says this account can name.

`agent --list-models` prints one `id - Label` line per model (measured against
agent 2026.10.22; the fixture is a captured excerpt, not a remembered format).
This module runs it, parses it, and says what it did NOT learn.

WHAT THE LIST DOES AND DOES NOT MEAN. It is the catalogue Cursor will accept as
a name, not the set a plan will let you run: GPT-5.4 Nano is in it on an account
whose plan then refused it ("not included in your weekly usage"). So a model
being listed is reported as exactly that, never as "available", and there is no
call that decides whether a plan allows one -- only running it does.

THREE STATES, NEVER ONE. A fetch that could not run, one that needs a login and
one that ran and returned nothing parseable are different facts with different
fixes. Collapsing the last into "no models" would tell a signed-in person their
account has none. Membership is likewise three-valued: with no usable catalogue a
configured model is "unknown", never "not listed".

A configured model that is missing from the list is KEPT and reported. Models
come and go, and silently swapping a person's pin for another model is the
substitution this project refuses everywhere an agent is chosen.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import subprocess

from constants import CREATE_NO_WINDOW

STATE_OK = "ok"
#: No Cursor CLI to ask.
STATE_UNAVAILABLE = "unavailable"
#: The CLI ran and says the person is not signed in.
STATE_NEEDS_LOGIN = "needs_login"
#: Could not run, timed out, exited non-zero, or printed nothing parseable.
STATE_FAILED = "failed"

LISTED = "listed"
NOT_LISTED = "not_listed"
UNKNOWN = "unknown"

#: `id - Label`. The id is a CLI token (letters, digits, dot, dash); anchoring
#: on that is what keeps the trailing "Tip: use --model <id> ..." prose from
#: being read as a model.
_LINE = re.compile(
    r"^(?P<id>[A-Za-z0-9][A-Za-z0-9._]*(?:-[A-Za-z0-9._]+)*) - (?P<label>\S.*)$")
_NEEDS_LOGIN = ("authentication required", "not logged in")


@dataclasses.dataclass(frozen=True)
class Model:
    id: str
    label: str


@dataclasses.dataclass(frozen=True)
class Catalogue:
    state: str
    models: tuple = ()
    #: Why the state is not OK, in words a person can act on.
    detail: str = ""
    fetched_at: "datetime.datetime | None" = None

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    @property
    def ids(self) -> tuple:
        return tuple(m.id for m in self.models)


def parse_list_models(text: str) -> tuple:
    """Models from `--list-models` output. Lines that are not models are skipped."""
    found = []
    seen = set()
    for line in (text or "").splitlines():
        match = _LINE.match(line.strip())
        if match and match.group("id") not in seen:
            seen.add(match.group("id"))
            found.append(Model(match.group("id"), match.group("label").strip()))
    return tuple(found)


def fetch(exe: str, timeout: int = 45) -> Catalogue:
    """Ask the CLI. Never raises; the failure is the returned state."""
    if not exe:
        return Catalogue(STATE_UNAVAILABLE,
                         detail="The Cursor Agent CLI is not installed or its "
                                "path is not set.")
    try:
        proc = subprocess.run(
            [exe, "--list-models"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return Catalogue(STATE_FAILED, detail=f"Timed out after {timeout}s.")
    except OSError as exc:
        return Catalogue(STATE_FAILED, detail=f"Could not run it: {exc}")
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if any(marker in combined.lower() for marker in _NEEDS_LOGIN):
        return Catalogue(STATE_NEEDS_LOGIN,
                         detail="Not signed in. Run `agent login`, then refresh.")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[:200]
        return Catalogue(STATE_FAILED,
                         detail=f"Exited {proc.returncode}: {tail}" if tail
                         else f"Exited {proc.returncode}.")
    models = parse_list_models(proc.stdout)
    if not models:
        return Catalogue(STATE_FAILED,
                         detail="It ran but printed no models this version of "
                                "the Manager can read.")
    return Catalogue(STATE_OK, models, fetched_at=datetime.datetime.now())


def membership(catalogue: Catalogue, model: str) -> str:
    """Is *model* in the catalogue? Unknown when there is no usable one.

    A bracketed override (`claude-opus-4-8[effort=high]`) names a listed model
    with parameters, so it is checked by the id before the bracket.
    """
    model = (model or "").strip()
    if not model or not catalogue.ok:
        return UNKNOWN
    return LISTED if model.split("[", 1)[0] in catalogue.ids else NOT_LISTED


def describe(catalogue: Catalogue, current: str) -> "tuple[str, str]":
    """`(text, tone)` for a status line; tone is ok / warn / bad."""
    if catalogue.state == STATE_OK:
        when = (catalogue.fetched_at.strftime("%H:%M")
                if catalogue.fetched_at else "")
        text = (f"{len(catalogue.models)} models listed"
                f"{' at ' + when if when else ''}. Listed does not mean your "
                f"plan allows it.")
        if membership(catalogue, current) == NOT_LISTED:
            return (text + f" '{current.strip()}' is not in this list; it is "
                    f"kept as set.", "warn")
        return text, "ok"
    if catalogue.state == STATE_NEEDS_LOGIN:
        return catalogue.detail, "warn"
    return catalogue.detail or "Could not read the model list.", "bad"
