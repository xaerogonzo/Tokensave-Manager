"""git_hooks_env — where git will *actually* look for a hook, and who owns it.

## The bug this module exists to prevent

`precommit_hook.py` and `prepush_hook.py` both defined "installed" as *the
file exists at `<project>/.git/hooks/<name>` and carries our marker*. That is
the wrong predicate twice over, and both failures are silent — the Manager
reports a healthy ✓ while git runs nothing.

**`core.hooksPath` replaces the hooks directory; it does not add to it.** When
it is set, git resolves every hook from there with *no fallback* to
`.git/hooks`. tokensave 7.11.0's global `githooks on` sets it, so accepting
that offer switches the Manager's pre-commit and pre-push off. Measured:

    core.hooksPath unset   ->  git rev-parse --git-path hooks  =  .git/hooks
    core.hooksPath set     ->  git rev-parse --git-path hooks  =  <that path>

**A linked worktree has no `.git` directory.** `<worktree>/.git` is a *file*
pointing at `<main>/.git/worktrees/<name>`, so `<worktree>/.git/hooks` is not
a directory and cannot be made into one. Git shares one hook directory across
every worktree, resolved from `--git-common-dir` rather than `--git-dir`.
Measured on a main checkout plus two linked worktrees: all three resolve to
the main checkout's `.git/hooks`, while `--git-dir` differs for each.

So string-joining `.git/hooks` onto a project path is wrong in a worktree and
wrong under `core.hooksPath`. **Ask git.** `git rev-parse --git-path hooks`
answers both at once — it honours `core.hooksPath` and resolves through the
common dir — which is why it is the primary read here and the config is
consulted only to *explain* the answer.

## The predicate that replaces "our file exists"

    git resolves <name> to the hook the Manager intends

which is a question about a directory git names, not a path we composed. A
hook file that exists somewhere git will not look is `INERT`, and that is a
third state — never folded into "not installed", because the two need
opposite advice. "You have no hook" says install one. "Your hook is on disk
and git is ignoring it" says fix the routing; installing again writes the same
file to the same ignored place.

## Ownership is evidence, not a substring

`ownership` is decided by comparing *resolved* paths and by reading markers —
never by looking for "tokensave" in the configured string. Three reasons, and
the third is the one that bites:

* `C:\\Tokensave\\hooks`, `\\\\server\\share\\tokensave\\hooks` and a relative
  `relative/hooks` would all match a substring test while meaning entirely
  different things.
* A path can name tokensave's directory without tokensave having written
  anything into it.
* **The Manager's own hook marker contains the word TOKENSAVE.**
  `# TOKENSAVE-PRECOMMIT-MARKER v1` and tokensave's `# tokensave: auto-sync`
  both match a case-insensitive search for "tokensave", so a naive check
  reports the Manager's own hooks as tokensave's.

`resolved_path` travels with the verdict so the Doctor can show its working
rather than asserting a conclusion.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from constants import CREATE_NO_WINDOW

_GIT_TIMEOUT = 15

# Marker substrings that identify who wrote a hook file. Matched
# case-sensitively and with their punctuation, because the two overlap on a
# case-insensitive search for "tokensave" -- see the module docstring.
_TOKENSAVE_MARKERS = ("# tokensave:",)
_MANAGER_MARKERS = ("# TOKENSAVE-PRECOMMIT-MARKER", "# TOKENSAVE-PREPUSH-MARKER")

# ── ownership of the effective hooks directory ───────────────────────────────
OWNER_REPO = "repo"             # the repository's own shared hook directory
OWNER_TOKENSAVE = "tokensave"   # elsewhere, and tokensave's hooks are in it
OWNER_OTHER = "other"           # elsewhere, and nothing of tokensave's is
OWNER_UNKNOWN = "unknown"       # could not read it

# ── where core.hooksPath came from ───────────────────────────────────────────
ORIGIN_UNSET = "unset"
ORIGIN_LOCAL = "local"
ORIGIN_GLOBAL = "global"
ORIGIN_SYSTEM = "system"
ORIGIN_WORKTREE = "worktree"
ORIGIN_OTHER = "other"          # command line, environment, an include file
ORIGIN_UNKNOWN = "unknown"

#: `git config --show-origin` prefixes, mapped to the scope they denote. The
#: file paths are compared separately; this only classifies the prefix.
_ORIGIN_PREFIXES = (
    ("command line:", ORIGIN_OTHER),
    ("standard input:", ORIGIN_OTHER),
    ("blob:", ORIGIN_OTHER),
)


@dataclass(frozen=True)
class HooksEnv:
    """Where git will look for hooks in one repository, and why.

    Facts only. Whether a given hook is installed, inert or absent is decided
    by the caller against `hooks_dir` -- this class does not know which hooks
    anyone wanted.
    """

    #: Absolute path git will read hooks from. "" when git could not answer.
    hooks_dir: str = ""
    #: The repository's own shared hook directory (`<git-common-dir>/hooks`),
    #: which is where hooks live when nothing has been redirected.
    default_hooks_dir: str = ""
    #: Raw `core.hooksPath`, exactly as configured. None when unset.
    config_value: "str | None" = None
    #: Which config scope set it. See the ORIGIN_* constants.
    origin: str = ORIGIN_UNSET
    #: The config file that set it, when `--show-origin` named one.
    origin_file: str = ""
    #: Who owns `hooks_dir`. See the OWNER_* constants.
    ownership: str = OWNER_REPO
    #: `hooks_dir` after symlink and case resolution -- what the comparison
    #: was actually made against, so a verdict can show its working.
    resolved_path: str = ""
    #: False when git could not be asked at all. Callers must not treat a
    #: failure to read as evidence that nothing is redirected.
    ok: bool = False
    reason: str = ""

    @property
    def redirected(self) -> bool:
        """Is git reading hooks from somewhere other than this repository's own?

        The question every caller actually has. Deliberately derived from the
        resolved paths rather than from `config_value` being set: a
        `core.hooksPath` pointing back at the repository's own hook directory
        redirects nothing, and reporting it as a problem would send a user off
        to fix a setting that is already harmless.
        """
        if not self.ok or not self.hooks_dir or not self.default_hooks_dir:
            return False
        return not _same_path(self.hooks_dir, self.default_hooks_dir)

    def hook_file(self, name: str) -> str:
        """Absolute path of hook *name*, where git will actually look for it."""
        return os.path.join(self.hooks_dir, name) if self.hooks_dir else ""


def _same_path(a: str, b: str) -> bool:
    """Do two paths name the same directory?

    `os.path.samefile` is the honest test -- it follows symlinks and resolves
    junctions, which a string comparison cannot -- but it raises when either
    side does not exist yet, which is routine here: a configured hooks
    directory that has not been created is exactly the state worth reporting.
    So it falls back to a normalised string comparison rather than to a guess.
    """
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
            os.path.abspath(b))


def _git(git_exe: str, repo: str, args: list) -> "tuple[str, int]":
    """Run git in *repo*, returning `(stdout, returncode)`. Never raises.

    stdout only: `--show-origin` writes its answer there, and folding stderr
    in would let a warning become part of a path.
    """
    try:
        proc = subprocess.run(
            [git_exe or "git", "-C", repo] + args,
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc), 1
    return (proc.stdout or "").strip(), proc.returncode


def read_hooks_env(project_path: str, git_exe: str = "git") -> HooksEnv:
    """Ask git where it will look for hooks in *project_path*.

    Fail-closed on `ok`, never on the verdict: when git cannot be reached the
    result reports `ok=False` with a reason, and every field keeps its neutral
    default. A caller must branch on `ok` -- treating an unreadable repository
    as "nothing is redirected" is the same class of mistake as treating an
    unreadable savings figure as zero.
    """
    if not project_path or not os.path.isdir(project_path):
        return HooksEnv(reason="not a directory: %s" % project_path)

    common, code = _git(git_exe, project_path, ["rev-parse", "--git-common-dir"])
    if code != 0 or not common:
        return HooksEnv(reason="not a git repository, or git is unavailable")
    # Relative in a plain checkout ('.git'), absolute from a linked worktree.
    default_dir = os.path.abspath(
        os.path.join(project_path, common, "hooks")
        if not os.path.isabs(common) else os.path.join(common, "hooks"))

    # The effective directory, straight from git: this already honours
    # core.hooksPath *and* resolves through the common dir for a worktree, so
    # it is one call where composing the answer ourselves needs two and gets
    # a linked worktree wrong.
    hooks, code = _git(git_exe, project_path, ["rev-parse", "--git-path", "hooks"])
    if code != 0 or not hooks:
        return HooksEnv(reason="git could not resolve the hooks path")
    hooks_dir = os.path.abspath(
        hooks if os.path.isabs(hooks) else os.path.join(project_path, hooks))

    config_value, origin, origin_file = _read_config_origin(git_exe, project_path)
    ownership = _classify_owner(hooks_dir, default_dir)

    return HooksEnv(
        hooks_dir=hooks_dir,
        default_hooks_dir=default_dir,
        config_value=config_value,
        origin=origin,
        origin_file=origin_file,
        ownership=ownership,
        resolved_path=_resolve(hooks_dir),
        ok=True,
    )


def _read_config_origin(git_exe: str, repo: str) -> "tuple[str | None, str, str]":
    """`core.hooksPath`, the scope that set it, and the file that did.

    Only ever an *explanation*: the effective directory has already been
    settled by `--git-path`. This exists so the Doctor can say *which config
    to edit*, which is the difference between advice a user can act on and a
    statement that something is wrong.
    """
    out, code = _git(git_exe, repo,
                     ["config", "--show-origin", "--get", "core.hooksPath"])
    if code != 0 or not out:
        return None, ORIGIN_UNSET, ""
    # `<origin>\t<value>`; a tab, because a Windows path contains colons.
    origin_field, _, value = out.partition("\t")
    if not value:
        return None, ORIGIN_UNKNOWN, ""
    for prefix, scope in _ORIGIN_PREFIXES:
        if origin_field.startswith(prefix):
            return value.strip(), scope, ""
    if origin_field.startswith("file:"):
        path = origin_field[len("file:"):]
        return value.strip(), _scope_of(path), path
    return value.strip(), ORIGIN_UNKNOWN, origin_field


def _scope_of(config_file: str) -> str:
    """Which scope a config file represents, by where it sits.

    Git does not label the scope in `--show-origin`, so it is inferred from
    the path: a repository's own config lives inside its git directory,
    everything else is a user- or system-level file. `config.worktree` is
    called out separately because it is per-worktree and therefore repairable
    somewhere different from `.git/config`.

    **The repository's own config arrives as a relative path.** Measured:
    `git config --show-origin --get core.hooksPath` reports
    `file:.git/config` for a value set by a plain `git config ...`, not an
    absolute path. An earlier version of this function tested only for an
    embedded `/.git/`, so the commonest case of all -- someone set it in this
    repository -- was reported as `global`, sending a user to edit the wrong
    file. Relative forms are therefore matched explicitly.
    """
    name = os.path.basename(config_file)
    lowered = config_file.replace("\\", "/").lower().lstrip("./")
    if name == "config.worktree":
        return ORIGIN_WORKTREE
    if lowered.startswith(".git/") or lowered == "config":
        return ORIGIN_LOCAL
    full = config_file.replace("\\", "/").lower()
    if "/.git/" in full or full.startswith(".git/"):
        return ORIGIN_LOCAL
    if "/etc/" in full or "programdata" in full or "mingw" in full:
        return ORIGIN_SYSTEM
    return ORIGIN_GLOBAL


def _resolve(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _classify_owner(hooks_dir: str, default_dir: str) -> str:
    """Who owns the directory git reads hooks from.

    Path comparison first, then file contents -- never the configured string.
    A directory that does not exist is `OWNER_OTHER` rather than
    `OWNER_UNKNOWN`: we know it is not the repository's own and we know
    tokensave has written nothing into it, which is the whole question.
    """
    if _same_path(hooks_dir, default_dir):
        return OWNER_REPO
    try:
        names = os.listdir(hooks_dir)
    except OSError:
        return OWNER_OTHER
    for name in names:
        try:
            with open(os.path.join(hooks_dir, name), encoding="utf-8",
                      errors="replace") as fh:
                body = fh.read()
        except OSError:
            continue
        if any(m in body for m in _MANAGER_MARKERS):
            continue          # ours, and never evidence that it is tokensave's
        if any(m in body for m in _TOKENSAVE_MARKERS):
            return OWNER_TOKENSAVE
    return OWNER_OTHER


def remediation(env: HooksEnv) -> str:
    """What to tell a user whose hooks are being ignored. "" when they are not.

    The order is not cosmetic and not a menu. `tokensave githooks on --local`
    writes hooks into the repository's own directory and **writes no git
    config at all** -- verified against 7.11.0 by diffing
    `git config --list --show-origin` across the call -- which is what lets
    tokensave's `post-*` hooks and the Manager's `pre-*` hooks share one
    directory. But it does nothing about a `core.hooksPath` that is already in
    effect: run in that state, tokensave writes the files, warns that git will
    not read them, and **still exits 0**. So the redirect has to be cleared
    first, and a caller cannot detect any of this from an exit code.

    A `core.hooksPath` the user set themselves is never something to undo on
    their behalf -- for that case this names the file to edit and stops.
    """
    if not env.ok or not env.redirected:
        return ""
    where = "%s (%s)" % (env.origin_file or env.origin, env.origin)
    if env.ownership == OWNER_TOKENSAVE:
        return ("tokensave's global git hooks claimed core.hooksPath, so git "
                "reads every hook from %s and ignores this repository's own. "
                "Run `tokensave githooks off` to release it, then "
                "`tokensave githooks on --local` if you still want tokensave's "
                "hooks -- the local form writes no git config, so both sets "
                "coexist." % env.hooks_dir)
    return ("core.hooksPath is set to %s by %s, so git ignores this "
            "repository's own hook directory. The Manager will not change a "
            "setting it did not make: unset it there, or point it at %s."
            % (env.hooks_dir, where, env.default_hooks_dir))
