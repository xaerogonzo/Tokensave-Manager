"""Savings and spend metrics, read from `tokensave gain` / `cost` / `discover`.

This module exists because the panel it feeds was telling the user something
false. The old `helpers/daemon_cost.py` scraped `tokensave cost`'s human table
with a regex, captured the **Cost** column, and the dialog rendered it as
"Value Recouped" — money *spent*, at API list price, presented as money
*saved*. On the reference machine that read $4132.75 where the real figure,
from the ledger that actually records savings, was $0.14.

So the split here is the whole point, and it is three-way rather than two:

    savings      `gain`      what tokensave saved you. Project-scoped.
    spend        `cost`      what your agent traffic would list-price at.
                             Machine-global, all projects, no filter exists.
    opportunity  `discover`  turns a tokensave query could have served.

Those are different quantities with different scopes, and nothing here is
allowed to blur them.

**Everything is measured, not assumed.** The facts below were established
against tokensave 7.10.0 with the streams separated, and each one changed the
code:

* `cost --export json` **has no cache-read field**, and
  `sum(by_model.tokens) - (total_input + total_output)` is exactly `0`. An
  earlier design derived cache reads from that subtraction, which would have
  rendered `Cache reads: 0` forever — inventing a number in the module written
  to stop inventing numbers. `Spend.cache_read_tokens` is therefore `None`,
  permanently, and callers must render "not reported".
* `cost`'s own numbers do not explain each other. All three totals reconcile
  ($4158.4742 = total = sum(by_model) = sum(by_category)) but imply
  **$361.51 per million tokens**, which matches no Claude price. The cost is
  computed from usage this export does not contain, so the figures are
  reported as tokensave's, never recomputed or "corrected" here.
* `cost`'s `tokens_saved` is a **lifetime, all-projects** counter, identical
  for every range, and `efficiency_ratio` is that fixed numerator over a
  moving denominator. Both are parsed so a caller can see they exist; neither
  is fit to display, and `Spend` says so in its own docstring.
* `discover`'s token estimates are currently degenerate — recoverable tokens
  equal replaceable turns exactly, in every bucket. Turn counts are
  authoritative; the token estimate is quarantined behind a recorded piece of
  evidence rather than a heuristic, so an upstream fix that legitimately
  produces one token per turn is not hidden forever.

**Streams are never merged.** `cost` and `discover` sometimes print
`Ingested or refreshed N local accounting rows.` before answering. That was
originally recorded as a stdout preamble the parser had to skip; measured with
stdout and stderr separated, it goes to **stderr** and stdout is pure JSON. The
earlier reading came from a `2>&1` run — the same trap this repo already hit
with tokensave's `doctor`. `_first_json` survives as a defensive helper in case
upstream ever moves the line, not as a required step.

Pure-builder + IO-wrapper, the house pattern (`helpers/ci_workflow.py` is the
model): every `parse_*` is a pure function over a stdout string and is testable
with no binary present; the `fetch_*` wrappers own the subprocess. No Tkinter —
safe to call from any thread.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from constants import CREATE_NO_WINDOW

#: Ranges tokensave accepts for both `gain --range` and `cost <RANGE>`.
RANGES = ("today", "7d", "30d", "all")

#: Seconds before a metrics subprocess is abandoned. These are local SQLite
#: reads; a hang means something is wrong, not that it needs longer.
_TIMEOUT = 15.0


class _NoPayload(Exception):
    """No JSON found in a command's stdout. Private — never escapes."""


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Result:
    """A parsed value, or a reason there isn't one.

    Three-state on purpose. The failure this module was written to fix came
    from a helper that returned zeros when it could not read anything, so the
    dialog could not tell "you saved nothing" from "we could not find out" and
    rendered both as `0`. Callers must branch on `ok` and render `reason`;
    there is deliberately no `value or 0` shortcut available to them.
    """

    ok: bool
    value: object = None
    reason: str = ""

    @staticmethod
    def good(value) -> "Result":
        return Result(True, value, "")

    @staticmethod
    def unavailable(reason: str) -> "Result":
        return Result(False, None, reason)

    def __bool__(self) -> bool:
        return self.ok


# ── Typed models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gain:
    """One `tokensave gain` reading — the honest savings figure.

    `usd` is valued at **Sonnet input rates**, which is tokensave's own choice
    (its table column reads `USD saved (Sonnet input)`). A caller displaying a
    bare `$` without that basis reproduces the ambiguity this module exists to
    remove, so the basis travels with the number.
    """

    range: str
    project: str
    saved_tokens: int
    calls: int
    usd: float
    #: True when this reading came from `--all` rather than one project.
    all_projects: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    #: The valuation basis, for display beside `usd`.
    USD_BASIS = "Sonnet input rates"


@dataclass(frozen=True)
class GainDay:
    """One day of `gain --history`.

    `day` is a **UTC midnight epoch** — every observed value satisfies
    `day % 86400 == 0`. Formatting it in local time would shift rows across day
    boundaries for anyone east or west of UTC, so callers label the column UTC
    and convert nothing.
    """

    day: int
    saved_tokens: int
    calls: int
    usd: float


@dataclass(frozen=True)
class SpendModel:
    model: str
    cost: float
    tokens: int


@dataclass(frozen=True)
class SpendCategory:
    category: str
    cost: float
    turns: int


@dataclass(frozen=True)
class Spend:
    """One `tokensave cost` reading — **estimated API list price, spent**.

    Not savings, and not a bill. It is machine-global across every project and
    agent: tokensave offers no project filter for it, so a caller must not
    present it beside a project-scoped figure without saying which is which.

    Three fields carry warnings rather than data:

    `cache_read_tokens` is always `None`. The export contains no such field and
    it cannot be derived — `sum(by_model.tokens)` equals
    `total_input_tokens + total_output_tokens` exactly, so any subtraction
    yields zero. Render "not reported", never a number.

    `lifetime_tokens_saved` and `efficiency_ratio` are parsed only so callers
    can see they exist. The first is a lifetime, all-projects counter identical
    for every range; the second divides it by a moving denominator. Neither is
    displayable, and `Gain` is where savings actually come from.
    """

    range: str
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    by_model: tuple = ()
    by_category: tuple = ()
    #: Always None — see the class docstring. Present so the absence is typed.
    cache_read_tokens: "int | None" = None
    lifetime_tokens_saved: int = 0
    efficiency_ratio: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    def totals_reconcile(self) -> bool:
        """Do `by_model` and `by_category` sum to `total_cost_usd`?

        They did on every payload measured. This is here so a caller can notice
        if that stops being true, not because anything depends on it.
        """
        cents = round(self.total_cost_usd, 4)
        return (round(sum(m.cost for m in self.by_model), 4) == cents
                and round(sum(c.cost for c in self.by_category), 4) == cents)

    def implied_usd_per_mtok(self) -> "float | None":
        """`total_cost_usd` per million tokens, or None with no tokens.

        Observed at **$361.51/Mtok**, which matches no Claude price — the cost
        is computed from usage the export does not carry. Exposed so the
        mismatch can be shown or filed upstream rather than silently papered
        over by recomputing the cost from the tokens shown.
        """
        tokens = self.total_input_tokens + self.total_output_tokens
        if tokens <= 0:
            return None
        return self.total_cost_usd / tokens * 1_000_000


@dataclass(frozen=True)
class DiscoverBucket:
    bucket: str
    tool: str
    suggestion: str
    turns: int


@dataclass(frozen=True)
class Discover:
    """One `tokensave discover` reading — turns a query could have served.

    **Turn counts are authoritative; token estimates are not.** Every payload
    measured reported `total_recoverable_input_tokens == replaceable_turns`
    exactly, and the same one-token-per-turn identity inside every bucket —
    an accounting artefact, not a measurement.

    The suppression is evidence-recorded rather than heuristic. `token_evidence`
    names the specific check that failed, and `tokens_trustworthy` is False only
    while it fails. Encoding "roughly equal means untrusted" as a rule would
    hide a future upstream fix that legitimately produced one token per turn.
    """

    since: str
    total_turns: int
    replaceable_turns: int
    buckets: tuple = ()
    tokens_trustworthy: bool = False
    token_evidence: str = ""
    raw: dict = field(default_factory=dict, repr=False)


# ── Pure parsers ─────────────────────────────────────────────────────────────


def _first_json(text: str) -> str:
    """Slice from the first `{` or `[` to the end. Defensive only.

    Measured behaviour is that stdout is pure JSON and the
    `Ingested or refreshed N local accounting rows.` line goes to stderr, so in
    normal operation this finds position 0 and changes nothing. It stays
    because the alternative — assuming a stream shape upstream never promised —
    is what produced the wrong reading in the first place.

    Raises `_NoPayload` when there is no JSON at all, so an empty or
    error-only stdout becomes a stated reason rather than a `JSONDecodeError`
    surfacing from three frames down.
    """
    candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not candidates:
        raise _NoPayload("no JSON payload")
    return text[min(candidates):]


def _loads(text: str):
    """`_first_json` + `json.loads`, with both failures spoken the same way."""
    try:
        return json.loads(_first_json(text))
    except _NoPayload as exc:
        raise _NoPayload(str(exc)) from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise _NoPayload(f"unreadable JSON: {exc}") from None


def _num(value, default=0):
    """A number from upstream, or `default` when the field is missing/odd.

    Only for fields whose absence is survivable. A missing *identifying* field
    fails the parse instead — a `Gain` with a silently-zeroed `saved_tokens`
    would be exactly the fabricated figure this module removes.
    """
    return value if isinstance(value, (int, float)) and not isinstance(
        value, bool) else default


def parse_gain(text: str) -> Result:
    """Parse `tokensave gain --json` (with or without `--all`).

    `--all` differs from the project form in exactly one field — `project` is
    the literal `"ALL"` — with the same range, units and valuation basis, which
    is what makes a project/all toggle a safe single switch for a caller.
    """
    try:
        data = _loads(text)
    except _NoPayload as exc:
        return Result.unavailable(str(exc))
    if not isinstance(data, dict):
        return Result.unavailable("expected a JSON object")
    for required in ("saved_tokens", "calls", "usd"):
        if required not in data:
            return Result.unavailable(f"missing field: {required}")

    project = str(data.get("project", ""))
    return Result.good(Gain(
        range=str(data.get("range", "")),
        project=project,
        saved_tokens=int(_num(data["saved_tokens"])),
        calls=int(_num(data["calls"])),
        usd=float(_num(data["usd"], 0.0)),
        all_projects=(project == "ALL"),
        raw=data,
    ))


def parse_gain_history(text: str) -> Result:
    """Parse `tokensave gain --history --json` into newest-first days.

    The series is **sparse** — only days with activity appear, so 30 days can
    yield 8 rows — and **bounded** by the requested range. A caller must not
    infer a gap means zero; it means no tool calls were recorded.
    """
    try:
        data = _loads(text)
    except _NoPayload as exc:
        return Result.unavailable(str(exc))
    if not isinstance(data, list):
        return Result.unavailable("expected a JSON array")

    days = []
    for row in data:
        if not isinstance(row, dict) or "day" not in row:
            continue
        days.append(GainDay(
            day=int(_num(row["day"])),
            saved_tokens=int(_num(row.get("saved_tokens"))),
            calls=int(_num(row.get("calls"))),
            usd=float(_num(row.get("usd"), 0.0)),
        ))
    days.sort(key=lambda d: d.day, reverse=True)
    return Result.good(days)


def parse_spend(text: str) -> Result:
    """Parse `tokensave cost --export json`, verbatim.

    Nothing is derived, recomputed or reconciled here. The export's own
    arithmetic does not close (see `Spend.implied_usd_per_mtok`), and a helper
    that quietly "fixed" that would be asserting a cost model tokensave never
    published.
    """
    try:
        data = _loads(text)
    except _NoPayload as exc:
        return Result.unavailable(str(exc))
    if not isinstance(data, dict):
        return Result.unavailable("expected a JSON object")
    if "total_cost_usd" not in data:
        return Result.unavailable("missing field: total_cost_usd")

    by_model = tuple(
        SpendModel(str(m.get("model", "")), float(_num(m.get("cost"), 0.0)),
                   int(_num(m.get("tokens"))))
        for m in data.get("by_model", []) if isinstance(m, dict))
    by_category = tuple(
        SpendCategory(str(c.get("category", "")),
                      float(_num(c.get("cost"), 0.0)),
                      int(_num(c.get("turns"))))
        for c in data.get("by_category", []) if isinstance(c, dict))

    return Result.good(Spend(
        range=str(data.get("range", "")),
        total_cost_usd=float(_num(data["total_cost_usd"], 0.0)),
        total_input_tokens=int(_num(data.get("total_input_tokens"))),
        total_output_tokens=int(_num(data.get("total_output_tokens"))),
        by_model=by_model,
        by_category=by_category,
        # Not read from the payload, because there is no such field, and not
        # derived, because the derivation is provably always zero.
        cache_read_tokens=None,
        lifetime_tokens_saved=int(_num(data.get("tokens_saved"))),
        efficiency_ratio=float(_num(data.get("efficiency_ratio"), 0.0)),
        raw=data,
    ))


def _token_evidence(data: dict) -> str:
    """Why `discover`'s token estimate is not trusted, or "" when it is.

    Returns the failing observation in full, so the reason travels with the
    suppression instead of living in a comment. Two checks, both measured:
    the totals identity, and the same identity inside every bucket.
    """
    recoverable = _num(data.get("total_recoverable_input_tokens"), None)
    replaceable = _num(data.get("replaceable_turns"), None)
    if recoverable is not None and replaceable is not None \
            and recoverable == replaceable and replaceable > 0:
        return (f"total_recoverable_input_tokens ({recoverable}) equals "
                f"replaceable_turns ({replaceable}) exactly")

    buckets = [b for b in data.get("buckets", []) if isinstance(b, dict)]
    per_bucket = [b for b in buckets
                  if _num(b.get("recoverable_input_tokens"), None)
                  == _num(b.get("turns"), None)
                  and _num(b.get("turns"), 0) > 0]
    if buckets and len(per_bucket) == len(buckets):
        return (f"every bucket ({len(buckets)}) reports "
                f"recoverable_input_tokens == turns")
    return ""


def parse_discover(text: str) -> Result:
    """Parse `tokensave discover --json`.

    Buckets keep `turns` and drop the token columns from the typed surface —
    they remain in `raw` for diagnosis. `tokens_trustworthy` and
    `token_evidence` tell a caller whether the omission is "withheld" or
    "genuinely absent", which are different things to render.
    """
    try:
        data = _loads(text)
    except _NoPayload as exc:
        return Result.unavailable(str(exc))
    if not isinstance(data, dict):
        return Result.unavailable("expected a JSON object")

    buckets = tuple(
        DiscoverBucket(str(b.get("bucket", "")), str(b.get("tool", "")),
                       str(b.get("suggestion", "")), int(_num(b.get("turns"))))
        for b in data.get("buckets", []) if isinstance(b, dict))
    evidence = _token_evidence(data)

    return Result.good(Discover(
        since=str(data.get("since", "")),
        total_turns=int(_num(data.get("total_turns"))),
        replaceable_turns=int(_num(data.get("replaceable_turns"))),
        buckets=buckets,
        tokens_trustworthy=not evidence,
        token_evidence=evidence,
        raw=data,
    ))


# ── IO wrappers ──────────────────────────────────────────────────────────────


def _run(exe: str, args: list, cwd: str = "") -> Result:
    """Run a tokensave subcommand and return its **stdout** as a Result.

    stdout and stderr are captured **separately and never merged**. That is the
    load-bearing detail: `cost` and `discover` write
    `Ingested or refreshed N local accounting rows.` to stderr, and reading them
    through `2>&1` is what produced this module's one wrong measurement. On
    failure the reason is stderr's text, which is where tokensave puts its
    diagnostics.
    """
    if not exe:
        return Result.unavailable("no tokensave executable configured")
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,          # separate pipes, not merged
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=_TIMEOUT,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return Result.unavailable(f"tokensave not found: {exe}")
    except subprocess.TimeoutExpired:
        return Result.unavailable(f"tokensave timed out after {_TIMEOUT:g}s")
    except OSError as exc:
        return Result.unavailable(f"could not run tokensave: {exc}")

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return Result.unavailable(
            detail[-1] if detail else f"tokensave exited {proc.returncode}")
    return Result.good(proc.stdout or "")


def fetch_gain(exe: str, project: str, range_: str = "30d",
               all_projects: bool = False) -> Result:
    """`tokensave gain` for one project, or `--all`. Does not write.

    Measured `PURE_READ`: neither this nor `fetch_gain_history` advances
    `~/.tokensave/global.db-wal`, unlike `fetch_spend` and `fetch_discover`.
    """
    args = ["gain", "--json", "--range", range_]
    if all_projects:
        args.append("--all")
    out = _run(exe, args, cwd=project)
    return out if not out.ok else parse_gain(out.value)


def fetch_gain_history(exe: str, project: str, range_: str = "30d") -> Result:
    """`tokensave gain --history`. Does not write."""
    out = _run(exe, ["gain", "--history", "--json", "--range", range_],
               cwd=project)
    return out if not out.ok else parse_gain_history(out.value)


def fetch_spend(exe: str, range_: str = "30d", project: str = "") -> Result:
    """`tokensave cost --export json`. **Refreshes tokensave bookkeeping.**

    Measured to write to `~/.tokensave/global.db`, so this is `OBSERVE_REFRESH`
    rather than a pure read. A caller must not wire it to a control a user
    operates casually — a range selector that re-ran this on every click would
    turn a display into an ingestion trigger.

    `project` only sets the working directory; `cost` is machine-global and has
    no project filter.
    """
    out = _run(exe, ["cost", range_, "--export", "json"], cwd=project)
    return out if not out.ok else parse_spend(out.value)


def fetch_discover(exe: str, project: str, since: str = "30d") -> Result:
    """`tokensave discover --json`. **Refreshes tokensave bookkeeping.**

    Writes, like `fetch_spend`. Same caution about casual re-invocation.
    """
    out = _run(exe, ["discover", "--json", "--since", since], cwd=project)
    return out if not out.ok else parse_discover(out.value)
