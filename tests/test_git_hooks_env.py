"""tests/test_git_hooks_env.py — where git actually looks for hooks.

**These use real repositories on purpose.** The thing under test is git's own
routing — `core.hooksPath` overriding with no fallback, and a linked worktree
sharing the main checkout's hook directory — and a mock of `git rev-parse`
would only assert that we remember what we already believed. The two bugs this
module was written to fix were both cases where what we believed was wrong, so
mocking here would reproduce them exactly.

Each repository is built in `tmp_path` and configured with `-c` or local
config only. Nothing writes to the user's global config: a test that changes
`core.hooksPath` globally would switch off git hooks for every repository on
the machine for as long as it ran, and leave them off if it crashed.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from helpers.git_hooks_env import (
    ORIGIN_GLOBAL,
    ORIGIN_LOCAL,
    ORIGIN_UNSET,
    OWNER_OTHER,
    OWNER_REPO,
    OWNER_TOKENSAVE,
    HooksEnv,
    read_hooks_env,
    remediation,
)

#: Real markers, copied from the two writers. The overlap is the point: both
#: contain "tokensave" case-insensitively, and one is ours.
TOKENSAVE_HOOK = "#!/bin/sh\n# tokensave: auto-sync\nexit 0\n"
MANAGER_HOOK = "#!/bin/sh\n# TOKENSAVE-PRECOMMIT-MARKER v1\nexit 0\n"


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real repository with one commit, and no global config touched."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# ── the ordinary case ────────────────────────────────────────────────────────

def test_an_untouched_repo_reads_its_own_hooks(repo):
    env = read_hooks_env(str(repo))
    assert env.ok
    assert env.origin == ORIGIN_UNSET
    assert env.config_value is None
    assert env.ownership == OWNER_REPO
    assert not env.redirected
    assert os.path.samefile(env.hooks_dir, repo / ".git" / "hooks")
    assert remediation(env) == ""


def test_hook_file_names_a_path_inside_the_effective_directory(repo):
    env = read_hooks_env(str(repo))
    assert env.hook_file("pre-commit") == os.path.join(env.hooks_dir,
                                                       "pre-commit")


# ── core.hooksPath: the silent switch-off ────────────────────────────────────

def test_a_set_hooks_path_redirects_and_is_reported(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(repo, "config", "core.hooksPath", str(elsewhere))

    env = read_hooks_env(str(repo))
    assert env.redirected
    assert os.path.samefile(env.hooks_dir, elsewhere)
    assert env.config_value == str(elsewhere)
    assert remediation(env)


def test_the_scope_that_set_it_is_named_so_the_advice_is_actionable(repo,
                                                                    tmp_path):
    """`git config --show-origin` reports the repo's own config as the
    *relative* `.git/config`, not an absolute path.

    The first version of `_scope_of` only looked for an embedded `/.git/`, so
    the commonest case of all — someone set it in this repository — came back
    `global`, which would send a user to edit `~/.gitconfig` and find nothing.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(repo, "config", "core.hooksPath", str(elsewhere))
    env = read_hooks_env(str(repo))
    assert env.origin == ORIGIN_LOCAL, env.origin_file


def test_a_hooks_path_pointing_back_at_the_repo_is_not_a_redirect(repo):
    """Set, but harmless. Reporting it would send a user to fix a non-problem.

    This is why `redirected` compares resolved paths rather than testing
    whether `config_value` happens to be set.
    """
    _git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))
    env = read_hooks_env(str(repo))
    assert env.config_value is not None
    assert not env.redirected
    assert env.ownership == OWNER_REPO
    assert remediation(env) == ""


# ── ownership is evidence, not a substring ───────────────────────────────────

def test_a_directory_holding_tokensaves_hooks_is_owned_by_tokensave(repo,
                                                                    tmp_path):
    hooks = tmp_path / "ts"
    hooks.mkdir()
    (hooks / "post-commit").write_text(TOKENSAVE_HOOK, encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(hooks))

    env = read_hooks_env(str(repo))
    assert env.ownership == OWNER_TOKENSAVE
    assert "githooks off" in remediation(env)
    assert "--local" in remediation(env)


def test_our_own_marker_is_not_mistaken_for_tokensaves(repo, tmp_path):
    """The trap a case-insensitive substring test walks straight into.

    `# TOKENSAVE-PRECOMMIT-MARKER v1` is the *Manager's* marker and contains
    the word tokensave. Classifying it as tokensave's would make the Doctor
    tell a user to run `tokensave githooks off` to fix hooks tokensave never
    wrote — which would not fix anything, and names the wrong culprit.
    """
    hooks = tmp_path / "mgr"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(MANAGER_HOOK, encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(hooks))

    env = read_hooks_env(str(repo))
    assert env.ownership == OWNER_OTHER
    assert "githooks off" not in remediation(env)


@pytest.mark.parametrize("name", ["Tokensave", "tokensave_hooks", "TOKENSAVE"])
def test_a_path_that_merely_says_tokensave_is_not_evidence(repo, tmp_path,
                                                           name):
    """An empty directory named after tokensave owns nothing.

    Covers the spellings a substring check would collapse together, including
    the case variations that differ on POSIX.
    """
    hooks = tmp_path / name
    hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(hooks))
    assert read_hooks_env(str(repo)).ownership == OWNER_OTHER


def test_a_hooks_path_that_does_not_exist_is_still_a_redirect(repo, tmp_path):
    """git honours it regardless, so every hook is dead. Not `unknown`."""
    missing = tmp_path / "nope"
    _git(repo, "config", "core.hooksPath", str(missing))
    env = read_hooks_env(str(repo))
    assert env.redirected
    assert env.ownership == OWNER_OTHER
    assert remediation(env)


# ── linked worktrees ─────────────────────────────────────────────────────────

@pytest.fixture
def worktrees(repo, tmp_path):
    """A main checkout plus two linked worktrees."""
    a, b = tmp_path / "wt_a", tmp_path / "wt_b"
    _git(repo, "worktree", "add", "-q", str(a), "-b", "wta")
    _git(repo, "worktree", "add", "-q", str(b), "-b", "wtb")
    return repo, a, b


def test_every_worktree_resolves_to_the_one_shared_hook_directory(worktrees):
    """Git keeps one hook directory per repository, not per worktree.

    Resolving through `--git-dir` instead of `--git-common-dir` would give
    each worktree its own `.git/worktrees/<name>/hooks`, which git never
    reads — so a hook installed from a worktree would never run.
    """
    main, a, b = worktrees
    dirs = [read_hooks_env(str(p)).hooks_dir for p in (main, a, b)]
    assert all(dirs), dirs
    assert os.path.samefile(dirs[0], dirs[1])
    assert os.path.samefile(dirs[0], dirs[2])
    assert os.path.samefile(dirs[0], main / ".git" / "hooks")


def test_a_worktrees_dot_git_is_a_file_which_is_why_joining_fails(worktrees):
    """The mechanism behind the second bug, pinned so it stays understood.

    `<worktree>/.git` is a file pointing at the shared metadata. So
    `os.path.join(worktree, ".git", "hooks")` — what the hook modules used to
    do — names a path under a *file*, which cannot be created and can never
    hold a hook.
    """
    _main, a, _b = worktrees
    assert (a / ".git").is_file()
    assert not (a / ".git" / "hooks").is_dir()


def test_a_redirect_applies_to_worktrees_too(worktrees, tmp_path):
    main, a, _b = worktrees
    elsewhere = tmp_path / "shared_hooks"
    elsewhere.mkdir()
    _git(main, "config", "core.hooksPath", str(elsewhere))
    assert read_hooks_env(str(a)).redirected


# ── failure is reported, never inferred as "fine" ────────────────────────────

def test_a_non_repository_is_not_reported_as_healthy(tmp_path):
    """`ok=False`, and `redirected` must not read as a clean bill of health."""
    plain = tmp_path / "plain"
    plain.mkdir()
    env = read_hooks_env(str(plain))
    assert not env.ok
    assert env.reason
    assert not env.redirected            # no claim either way
    assert remediation(env) == ""        # nothing to advise


def test_a_missing_directory_is_reported(tmp_path):
    env = read_hooks_env(str(tmp_path / "absent"))
    assert not env.ok
    assert "not a directory" in env.reason


def test_a_default_constructed_env_claims_nothing():
    """`ok` defaults False, so a forgotten branch cannot read as "no redirect"."""
    env = HooksEnv()
    assert not env.ok
    assert not env.redirected
    assert env.hook_file("pre-commit") == ""


# ── the third state: on disk, and ignored ────────────────────────────────────
#
# These live here rather than in the per-hook suites because the property is
# about git's routing, and both hook modules implement it identically against
# this module's answer.

def _install_marker_hook(repo, name, body):
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / name).write_text(body, encoding="utf-8")


@pytest.mark.parametrize("module_name,hook_name", [
    ("helpers.precommit_hook", "pre-commit"),
    ("helpers.prepush_hook", "pre-push"),
])
def test_a_stranded_hook_reports_inert_not_installed(repo, tmp_path,
                                                     module_name, hook_name):
    """The bug in one assertion.

    The hook file exists and carries our marker, so the old predicate said
    "installed" and the UI showed a ✓. git resolves hooks from somewhere else
    entirely, so it runs nothing. `INERT` is what makes those two facts
    expressible at once.
    """
    import importlib
    mod = importlib.import_module(module_name)
    state_fn = getattr(mod, hook_name.replace("-", "_") + "_hook_state")

    _install_marker_hook(repo, hook_name,
                         "#!/bin/sh\n%s\nexit 0\n" % mod._HOOK_MARKER)
    assert state_fn(str(repo)) == mod.INSTALLED

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(repo, "config", "core.hooksPath", str(elsewhere))

    assert state_fn(str(repo)) == mod.INERT
    # And the boolean answers "will it run", which is False.
    installed_fn = getattr(mod, "is_%s_hook_installed"
                           % hook_name.replace("-", "_"))
    assert installed_fn(str(repo)) is False


@pytest.mark.parametrize("module_name,hook_name", [
    ("helpers.precommit_hook", "pre-commit"),
    ("helpers.prepush_hook", "pre-push"),
])
def test_absent_stays_absent_under_a_redirect(repo, tmp_path, module_name,
                                              hook_name):
    """No hook anywhere is `ABSENT`, redirect or not.

    `INERT` must mean "yours exists and is ignored". Letting a redirect alone
    produce it would tell every user of a tokensave-hooked machine that they
    have a stranded hook they never installed.
    """
    import importlib
    mod = importlib.import_module(module_name)
    state_fn = getattr(mod, hook_name.replace("-", "_") + "_hook_state")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(repo, "config", "core.hooksPath", str(elsewhere))
    assert state_fn(str(repo)) == mod.ABSENT


@pytest.mark.parametrize("module_name,hook_name", [
    ("helpers.precommit_hook", "pre-commit"),
    ("helpers.prepush_hook", "pre-push"),
])
def test_a_hook_in_the_redirected_directory_is_simply_installed(
        repo, tmp_path, module_name, hook_name):
    """If ours is where git reads, it runs. Nothing to warn about."""
    import importlib
    mod = importlib.import_module(module_name)
    state_fn = getattr(mod, hook_name.replace("-", "_") + "_hook_state")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / hook_name).write_text(
        "#!/bin/sh\n%s\nexit 0\n" % mod._HOOK_MARKER, encoding="utf-8")
    _git(repo, "config", "core.hooksPath", str(elsewhere))

    assert state_fn(str(repo)) == mod.INSTALLED


def test_hook_path_points_where_git_reads_not_where_we_would_have_guessed(
        worktrees):
    """The worktree half, through the hook module's own entry point.

    `hook_path` used to be `os.path.join(project, ".git", "hooks", name)`,
    which in a linked worktree names a path under a *file*. Every install
    there failed and every check reported "not installed", permanently.
    """
    from helpers.precommit_hook import hook_path
    _main, a, _b = worktrees
    got = hook_path(str(a))
    assert os.path.isdir(os.path.dirname(got)), got
    assert not got.startswith(str(a) + os.sep + ".git")
