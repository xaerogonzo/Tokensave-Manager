"""multi_remote — pushing one branch to several remotes, safely.

Pushing to N remotes is not "run the one-remote push N times". Three things
change, and each has a way of going wrong that is invisible until it costs
something.

## 1. Upstream tracking is not the same question as "who receives this"

The single-remote path runs ``git push -u origin HEAD``. Looping that over
three remotes leaves the branch tracking whichever ran **last** — a silent
edit to the user's git config, made by a button that said "push". So this
module treats them as two separate inputs: ``upstream_remote`` names the one
remote (if any) that ``-u`` applies to, and every other selected remote gets
a plain push.

## 2. ``--force-with-lease`` weakens silently across remotes

Bare ``--force-with-lease`` leases against the *local* remote-tracking ref,
``refs/remotes/<remote>/<branch>``. That is a reasonable default for a remote
you fetch from constantly, and close to meaningless for the other two: a
remote never fetched has no tracking ref at all, and a stale one describes
where the remote was days ago. Either way git happily overwrites, and the
confirmation dialog's promise that it "refuses to overwrite commits someone
else pushed" quietly stops being true.

So the expected tip is read from **the remote itself** immediately before
pushing::

    tip = git ls-remote <remote> refs/heads/<branch>     (read-only)
    push --force-with-lease=refs/heads/<branch>:<tip>

A branch that does not exist on the remote yet gets ``:`` with an empty
expectation, which git defines as "this ref must not already exist" — the
correct lease for a new branch, rather than an unprotected push.

There is deliberately **no ``--force`` fallback**. If the lease is refused,
that is the feature working.

## 3. A remote is not a URL

Git allows several ``pushurl`` entries per remote and pushes to all of them,
and fetch and push URLs can differ. Reporting ``origin ✓`` when one of its
two destinations was rejected would be a lie, so results are parsed per
destination out of git's own ``To <url>`` blocks.

## Credentials

Remote URLs can carry embedded credentials (``https://user:token@host/...``).
Anything from ``git remote -v`` may therefore contain a secret, so every URL
that reaches a log, a dialog or a result string goes through
:func:`redact_url` first. This module never accepts, stores, or constructs a
credentialed URL — authentication is the provider CLI's business (see
``helpers/remote_providers.py``).

No Tk. Pure enough to test without a network: every git call is one
subprocess boundary.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

try:
    from constants import _GIT_ENV_NO_PROMPT
except ImportError:                                     # standalone / test use
    _GIT_ENV_NO_PROMPT = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

_GIT_TIMEOUT = 120          # a push can be slow on a big repo / slow link
_LS_REMOTE_TIMEOUT = 30

# Outcome kinds. Strings so they survive logs and JSON without ceremony.
PUSH_OK = "ok"
PUSH_AUTH = "auth"
PUSH_LEASE = "lease"
PUSH_REJECTED = "rejected"
PUSH_ERROR = "error"

_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)"
                                 r"(?P<cred>[^/@\s]+)@")
_TO_LINE_RE = re.compile(r"^To\s+(.+?)\s*$")
_REJECT_RE = re.compile(r"^\s*!\s*\[(?P<why>[^\]]+)\]")


@dataclass(frozen=True)
class Remote:
    """A git remote as git actually models it: a name and several URLs."""
    name: str
    fetch_urls: tuple = ()
    push_urls: tuple = ()

    @property
    def destinations(self) -> tuple:
        """Where a push to this remote actually lands.

        Git falls back to the fetch URL when no explicit ``pushurl`` is set,
        which is the common case; when ``pushurl`` entries exist they replace
        it rather than adding to it.
        """
        return self.push_urls or self.fetch_urls

    @property
    def safe_destinations(self) -> tuple:
        return tuple(redact_url(u) for u in self.destinations)


@dataclass(frozen=True)
class DestinationResult:
    """One ``To <url>`` block from git's output."""
    url: str                       # already redacted
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class RemotePushResult:
    """What happened for one selected remote."""
    remote: str
    ok: bool
    kind: str = PUSH_OK
    detail: str = ""
    destinations: tuple = field(default_factory=tuple)

    @property
    def is_partial(self) -> bool:
        """Some of this remote's push URLs succeeded and others did not."""
        if len(self.destinations) < 2:
            return False
        oks = [d.ok for d in self.destinations]
        return any(oks) and not all(oks)


@dataclass(frozen=True)
class PushOutcome:
    """The whole operation, with per-remote detail preserved."""
    branch: str
    results: tuple = field(default_factory=tuple)
    upstream_remote: str = ""
    forced: bool = False

    @property
    def ok_remotes(self) -> tuple:
        return tuple(r for r in self.results if r.ok)

    @property
    def failed_remotes(self) -> tuple:
        return tuple(r for r in self.results if not r.ok)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def is_partial(self) -> bool:
        """Some remotes took the push and others did not.

        The caller must not flatten this to "failed": after a history rewrite
        it means the rewritten history landed in some places and the old
        history — including whatever was being removed — is still on the
        others.
        """
        return bool(self.ok_remotes) and bool(self.failed_remotes)

    def summary(self) -> str:
        return "%d/%d remote%s succeeded" % (
            len(self.ok_remotes), len(self.results),
            "" if len(self.results) == 1 else "s")


# ── discovery ─────────────────────────────────────────────────────────────

def list_remotes(git_exe: str, repo: str) -> list:
    """Every configured remote, with its fetch and push URLs.

    Parses ``git remote -v``, which prints one line per (name, url, direction)
    and repeats the name for each additional ``pushurl``.
    """
    out, rc = _git(git_exe, repo, ["remote", "-v"])
    if rc != 0:
        return []
    fetch, push = {}, {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, url, direction = parts[0], parts[1], parts[2].strip("()")
        bucket = fetch if direction == "fetch" else push
        bucket.setdefault(name, [])
        if url not in bucket[name]:
            bucket[name].append(url)
    names = list(dict.fromkeys(list(fetch) + list(push)))
    return [Remote(name=n,
                   fetch_urls=tuple(fetch.get(n, ())),
                   push_urls=tuple(push.get(n, ())))
            for n in names]


def current_branch(git_exe: str, repo: str) -> "str | None":
    out, rc = _git(git_exe, repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    name = out.strip()
    if rc != 0 or not name or name == "HEAD":
        return None                      # detached HEAD has no branch to push
    return name


def remote_tip(git_exe: str, repo: str, remote: str,
               branch: str) -> "tuple[str | None, bool]":
    """The remote's CURRENT sha for *branch*: ``(sha_or_None, known)``.

    ``known`` is False when the remote could not be consulted at all (offline,
    auth, bad name). That is deliberately distinct from "the branch does not
    exist there" (``(None, True)``): the first must abort a lease, the second
    is a legitimate new-branch push.
    """
    ref = "refs/heads/%s" % branch
    out, rc = _git(git_exe, repo, ["ls-remote", remote, ref],
                   timeout=_LS_REMOTE_TIMEOUT)
    if rc != 0:
        return None, False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0], True
    return None, True


# ── selection ─────────────────────────────────────────────────────────────

def reconcile_selection(selected, live_remotes) -> tuple:
    """Drop saved names that no longer exist; keep the live order.

    A remote can be renamed or removed outside the manager, and a saved
    selection naming a remote that is gone would either error at push time or,
    worse, quietly push nowhere while reporting success for a list of one.
    Git's current view is authoritative.
    """
    live = [r.name if isinstance(r, Remote) else str(r) for r in live_remotes]
    chosen = set(selected or ())
    return tuple(name for name in live if name in chosen)


# ── pushing ───────────────────────────────────────────────────────────────

def push(git_exe: str, repo: str, remotes, *, branch: "str | None" = None,
         upstream_remote: str = "", force_with_lease: bool = False,
         runner=None) -> PushOutcome:
    """Push *branch* to each named remote, independently.

    Each remote is pushed and reported on its own: one failing does not stop
    the others, because "the secret is still on codeberg" is exactly the
    situation the caller needs told about, and aborting the loop would hide
    which remotes are in which state.

    *runner* exists for tests — it stands in for the subprocess boundary and
    receives the fully-built argv, which is what the parity tests assert on.
    """
    names = [r.name if isinstance(r, Remote) else str(r) for r in remotes]
    branch = branch or current_branch(git_exe, repo)
    if not branch:
        return PushOutcome(branch="", results=(), forced=force_with_lease)

    results = []
    for name in names:
        results.append(_push_one(git_exe, repo, name, branch,
                                 set_upstream=(name == upstream_remote),
                                 force_with_lease=force_with_lease,
                                 runner=runner))
    return PushOutcome(branch=branch, results=tuple(results),
                       upstream_remote=upstream_remote,
                       forced=force_with_lease)


def _push_one(git_exe: str, repo: str, remote: str, branch: str, *,
              set_upstream: bool, force_with_lease: bool,
              runner=None) -> RemotePushResult:
    argv = ["push"]
    if set_upstream:
        argv.append("-u")

    if force_with_lease:
        tip, known = remote_tip(git_exe, repo, remote, branch) \
            if runner is None else _runner_tip(runner, git_exe, repo,
                                               remote, branch)
        if not known:
            # No lease can be honest here: we do not know what we would be
            # overwriting. Refusing is the whole point of --force-with-lease,
            # so refuse rather than downgrading to an unprotected push.
            return RemotePushResult(
                remote=remote, ok=False, kind=PUSH_ERROR,
                detail=("could not read the current state of %s, so a "
                        "force-push cannot be made safe — not pushed"
                        % remote))
        # An empty expectation means "must not already exist", which is the
        # correct lease for a branch this remote has never seen.
        argv.append("--force-with-lease=refs/heads/%s:%s"
                    % (branch, tip or ""))

    argv += [remote, "HEAD:refs/heads/%s" % branch]
    out, rc = (_git(git_exe, repo, argv, timeout=_GIT_TIMEOUT)
               if runner is None else runner(argv))

    destinations = _parse_destinations(out)
    if rc == 0:
        return RemotePushResult(remote=remote, ok=True, kind=PUSH_OK,
                                detail=_first_line(out),
                                destinations=destinations)
    kind = _classify_failure(out)
    return RemotePushResult(remote=remote, ok=False, kind=kind,
                            detail=_failure_detail(kind, out),
                            destinations=destinations)


def _runner_tip(runner, git_exe, repo, remote, branch):
    """Route the ls-remote probe through an injected runner too."""
    out, rc = runner(["ls-remote", remote, "refs/heads/%s" % branch])
    if rc != 0:
        return None, False
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "refs/heads/%s" % branch:
            return parts[0], True
    return None, True


# ── output parsing ────────────────────────────────────────────────────────

def _parse_destinations(output: str) -> tuple:
    """Split git's output into one result per ``To <url>`` block.

    Git prints a block per push URL, so a remote with two ``pushurl`` entries
    produces two — which is the only way to report that one landed and the
    other did not.
    """
    blocks, current = [], None
    for line in (output or "").splitlines():
        match = _TO_LINE_RE.match(line)
        if match:
            current = {"url": match.group(1).strip(), "ok": True, "why": ""}
            blocks.append(current)
            continue
        if current is None:
            continue
        reject = _REJECT_RE.match(line)
        if reject:
            current["ok"] = False
            current["why"] = reject.group("why").strip()
    return tuple(DestinationResult(url=redact_url(b["url"]), ok=b["ok"],
                                   detail=b["why"])
                 for b in blocks)


def _classify_failure(output: str) -> str:
    text = (output or "").lower()
    if "stale info" in text or "force-with-lease" in text:
        return PUSH_LEASE
    if any(s in text for s in ("authentication", "could not read username",
                              "permission denied", "403", "access denied",
                              "invalid username or password",
                              "terminal prompts disabled")):
        return PUSH_AUTH
    if "rejected" in text or "non-fast-forward" in text or "fetch first" in text:
        return PUSH_REJECTED
    return PUSH_ERROR


def _failure_detail(kind: str, output: str) -> str:
    canned = {
        PUSH_LEASE: ("refused: the remote moved since it was checked, so the "
                     "force-push would have discarded someone else's commits"),
        PUSH_AUTH: "authentication failed",
        PUSH_REJECTED: "rejected — the remote has commits you do not have",
    }
    tail = _first_line(output)
    base = canned.get(kind, "push failed")
    return "%s (%s)" % (base, redact_url(tail)) if tail else base


def _first_line(output: str) -> str:
    for line in reversed((output or "").strip().splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("To "):
            return stripped[:200]
    return ""


# ── credentials ───────────────────────────────────────────────────────────

def redact_url(url: str) -> str:
    """Strip embedded credentials from a URL before it is shown or logged.

    ``git remote -v`` happily prints ``https://user:ghp_xxx@github.com/...``
    if that is how the remote was configured, so any URL originating from git
    config must be treated as possibly secret-bearing.
    """
    if not url:
        return url
    return _URL_CREDENTIALS_RE.sub(lambda m: m.group("scheme") + "***@", url)


# ── the one subprocess boundary ───────────────────────────────────────────

def _git(git_exe: str, repo: str, args: list,
         timeout: int = _GIT_TIMEOUT) -> "tuple[str, int]":
    """Run git, returning ``(stdout+stderr, returncode)``. Never raises.

    ``GIT_TERMINAL_PROMPT=0`` matters more here than elsewhere: several
    remotes means several chances to block forever on a hidden credential
    prompt with no console attached.
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", repo] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_GIT_ENV_NO_PROMPT, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "timed out after %ds" % timeout, 1
    except OSError as exc:
        return str(exc), 1
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode
