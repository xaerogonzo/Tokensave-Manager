"""tests/test_remote_providers.py — the provider split, and the token rule.

Two jobs.

**Parity.** Generalising a working GitHub wizard is only safe if the GitHub
path comes out the other side running the same commands. "The dialog opens
and GitHub seems to work" is not that, so the release command is compared
argv-for-argv against what `dialogs/github_setup.py` builds today, and the
flag order is checked against that file's source so the two cannot drift
apart silently.

**Credentials.** The rule is that the manager never accepts, stores, embeds
or logs a token. That is asserted from both directions: no provider can be
made to put a secret in argv, and a credentialed URL is refused before it can
reach `.git/config` — where every later `git remote -v` would repeat it.
"""
from __future__ import annotations

import inspect

import pytest

from helpers.remote_providers import (
    AUTH_CLI,
    AUTH_CREDENTIAL_HELPER,
    CODEBERG,
    GITHUB,
    GITLAB,
    PROVIDERS,
    get_provider,
    provider_for_url,
)


# ── parity with the pre-refactor GitHub path ─────────────────────────────

def test_the_github_release_command_is_unchanged():
    """Transcribed from `github_setup.py::_create_release` before the split:

        cmd = ["gh", "release", "create", tag,
               "--title", title, "--generate-notes"] + exe_files

    Argv equality is the contract that matters — it is what actually reaches
    GitHub.
    """
    assert GITHUB.create_release_argv(
        "v1.0.0", "My Release", ["dist/a.exe", "dist/b.exe"]) == [
        "gh", "release", "create", "v1.0.0",
        "--title", "My Release", "--generate-notes",
        "dist/a.exe", "dist/b.exe"]


def test_the_release_command_survives_having_no_assets():
    """The "create a release without uploading anything" branch."""
    assert GITHUB.create_release_argv("v1.0.0", "T") == [
        "gh", "release", "create", "v1.0.0", "--title", "T",
        "--generate-notes"]


def test_the_github_dialog_is_a_thin_wrapper_over_the_provider():
    """The other half of the parity chain, after the refactor landed.

    Before the split this asserted that `github_setup.py` still built the
    literal `gh release create ...` argv. It now delegates, so the guarantee
    is made of two links instead: the wrapper resolves to the GITHUB
    provider, and the test above pins that provider's argv to what the
    literal used to be. Both must hold for the GitHub path to be unchanged.
    """
    from dialogs.github_setup import GitHubSetupDialog
    from dialogs.remote_setup import RemoteSetupDialog

    assert issubclass(GitHubSetupDialog, RemoteSetupDialog)
    # The wrapper exists to bind one provider and nothing else; anything more
    # in it is GitHub-specific behaviour that escaped the registry.
    body = inspect.getsource(GitHubSetupDialog)
    assert "provider=GITHUB" in body
    assert body.count("def ") == 1, "the wrapper grew logic of its own"


def test_a_configured_cli_path_is_honoured_over_the_bare_name():
    """The manager stores an absolute path when the CLI is not on PATH."""
    argv = GITHUB.create_release_argv("v1", "T", cli_exe=r"C:\tools\gh.exe")
    assert argv[0] == r"C:\tools\gh.exe"


# ── the credential rule ──────────────────────────────────────────────────

def test_no_provider_can_put_a_token_in_argv():
    """argv is world-readable on both platforms this ships to.

    The login commands are interactive precisely so the CLI collects the
    secret itself and the manager never touches it.
    """
    for provider in PROVIDERS:
        for argv in (provider.auth_login_argv(), provider.auth_status_argv()):
            assert all("token" not in a.lower() for a in argv), provider.id
            assert not any(len(a) > 30 and a.isalnum() for a in argv)


def test_cli_providers_log_in_interactively():
    assert GITHUB.auth_login_argv() == ["gh", "auth", "login"]
    assert GITLAB.auth_login_argv() == ["glab", "auth", "login"]


def test_codeberg_offers_no_cli_commands_at_all():
    """Deliberately unfinished rather than invented.

    Forgejo's CLI story could not be verified against a live instance, and
    guessing one would have meant the manager holding a token. Pushing works
    through git's credential helper today.
    """
    assert CODEBERG.auth_strategy == AUTH_CREDENTIAL_HELPER
    assert CODEBERG.auth_login_argv() == []
    assert CODEBERG.auth_status_argv() == []
    assert CODEBERG.create_release_argv("v1", "T") == []
    assert "never handles your token" in CODEBERG.signin_hint


def test_a_credentialed_url_is_refused():
    """It would otherwise be written into .git/config in plaintext."""
    ok, reason = GITHUB.validate_remote_url(
        "https://alice:ghp_secret@github.com/o/r.git")
    assert ok is False
    assert "token" in reason


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/o/r.git", True),
    ("git@github.com:o/r.git", True),
    ("https://codeberg.org/o/r", True),
    ("ftp://github.com/o/r.git", False),
    ("not a url", False),
    ("", False),
])
def test_remote_url_validation(url, ok):
    assert GITHUB.validate_remote_url(url)[0] is ok


def test_an_ssh_url_is_not_mistaken_for_a_credentialed_one():
    """`git@host:path` legitimately contains an @ and must still pass."""
    assert GITHUB.validate_remote_url("git@codeberg.org:o/r.git")[0] is True


# ── registry integrity ───────────────────────────────────────────────────

def test_every_provider_is_internally_consistent():
    """A CLI provider needs a CLI; a helper provider must not claim one."""
    for provider in PROVIDERS:
        if provider.auth_strategy == AUTH_CLI:
            assert provider.cli_name, provider.id
            assert provider.cli_exe_key, provider.id
        else:
            assert not provider.cli_name, provider.id
        if provider.release_support:
            assert provider.create_release_argv("v1", "T"), provider.id


def test_provider_ids_are_unique():
    ids = [p.id for p in PROVIDERS]
    assert len(ids) == len(set(ids))


def test_an_unknown_provider_id_falls_back_rather_than_raising():
    """A stale or hand-edited id must not make the dialog unopenable."""
    assert get_provider("nope").id == "github"
    assert get_provider("").id == "github"
    assert get_provider(None).id == "github"


def test_provider_lookup_is_case_insensitive():
    assert get_provider("GitLab").id == "gitlab"


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r.git", "github"),
    ("https://gitlab.com/o/r.git", "gitlab"),
    ("git@codeberg.org:o/r.git", "codeberg"),
])
def test_a_remote_url_identifies_its_provider(url, expected):
    assert provider_for_url(url).id == expected


def test_a_self_hosted_remote_is_not_claimed_by_anyone():
    """Guessing would put the wrong sign-in instructions in front of the user."""
    assert provider_for_url("https://git.mycorp.internal/o/r.git") is None
    assert provider_for_url("") is None
