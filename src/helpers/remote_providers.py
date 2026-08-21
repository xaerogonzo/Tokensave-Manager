"""remote_providers — what differs between GitHub, GitLab and Codeberg.

``dialogs/github_setup.py`` is a five-step wizard that is GitHub-specific at
every step: it says "GitHub Setup", links github.com/new, and shells out to
``gh``. Generalising it by threading ``if provider == "github" / elif ...``
through 380 lines would leave the branching smeared across the UI, which is
how a dialog becomes a god-file.

So the provider is an object and the dialog is generic. Everything below is
data or argv construction — no Tk, no subprocess. The dialog decides *when*
to run a command; this module decides *what* the command is.

## Credentials: the rule, and why Codeberg is deliberately unfinished

The manager never accepts, stores, embeds or logs a token. Concretely it must
never appear in ``manager-config.json``, in a remote URL, in argv (visible to
every other process on the machine), or in a log line.

That leaves two honest strategies:

* ``AUTH_CLI`` — the provider ships a CLI that owns its own credential store
  and interactive login. ``gh`` and ``glab`` both do; the manager launches
  ``<cli> auth login`` and never sees what is typed.
* ``AUTH_CREDENTIAL_HELPER`` — no CLI integration. The manager explains how to
  configure git's own credential helper and gets out of the way.

Codeberg is ``AUTH_CREDENTIAL_HELPER`` on purpose. Forgejo's CLI story could
not be verified against a live instance while this was written, and the
alternative — inventing a token flow from documentation and having the manager
hold the secret — is exactly what the rule above forbids. Pushing works today
through the credential helper; if a verified CLI path appears later, it
becomes a third ``AUTH_CLI`` provider and nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The provider's own CLI owns login and the credential store.
AUTH_CLI = "cli"
#: No CLI integration — git's credential helper does it, and we only explain.
AUTH_CREDENTIAL_HELPER = "credential_helper"


@dataclass(frozen=True)
class SetupStep:
    """One numbered step in the setup wizard."""
    number: str
    title: str
    body: str
    url: str = ""                 # optional "open this in a browser"
    action: str = ""              # optional action id the dialog wires up


@dataclass(frozen=True)
class RemoteProvider:
    """Everything that differs between one forge and another."""
    id: str
    display_name: str
    icon: str
    host: str
    new_repo_url: str
    auth_strategy: str
    steps: tuple = field(default_factory=tuple)
    cli_name: str = ""            # "" when there is no CLI integration
    cli_exe_key: str = ""         # ManagerConfig key holding its path
    release_support: bool = False
    signin_hint: str = ""
    # Wizard display fields. Data rather than branches in the dialog, so
    # adding a forge is an entry here and not an `elif` in the UI.
    login_url: str = ""
    signup_url: str = ""
    example_remote_url: str = ""
    repo_advice: str = ""
    cli_display_name: str = ""
    cli_download_url: str = ""
    release_noun: str = "Release"

    # ── auth ─────────────────────────────────────────────────────────────

    def auth_status_argv(self, cli_exe: str = "") -> list:
        """Argv that reports whether the user is signed in, or [] if N/A."""
        if self.auth_strategy != AUTH_CLI:
            return []
        return [cli_exe or self.cli_name, "auth", "status"]

    def auth_login_argv(self, cli_exe: str = "") -> list:
        """Argv that starts an INTERACTIVE login, or [] if N/A.

        Interactive on purpose: the CLI prompts for the secret itself, so the
        manager never handles it. Never pass a token here — it would land in
        argv, which every process on the machine can read.
        """
        if self.auth_strategy != AUTH_CLI:
            return []
        return [cli_exe or self.cli_name, "auth", "login"]

    # ── releases ─────────────────────────────────────────────────────────

    def create_release_argv(self, tag: str, title: str,
                            assets=(), cli_exe: str = "") -> list:
        """Argv to publish a release, or [] when unsupported."""
        if not self.release_support:
            return []
        if self.cli_name == "gh":
            return [cli_exe or "gh", "release", "create", tag,
                    "--title", title, "--generate-notes"] + list(assets)
        if self.cli_name == "glab":
            # glab names the flag --notes and has no --generate-notes.
            argv = [cli_exe or "glab", "release", "create", tag,
                    "--name", title]
            for asset in assets:
                argv += ["--assets-links", asset]
            return argv
        return []

    # ── remote URLs ──────────────────────────────────────────────────────

    def validate_remote_url(self, url: str) -> "tuple[bool, str]":
        """Is *url* a plausible remote for this provider? ``(ok, reason)``.

        Rejects a credentialed URL outright. Someone pasting
        ``https://user:token@host/...`` would otherwise write their token into
        ``.git/config`` in plaintext, where every later ``git remote -v`` and
        every log line would repeat it.
        """
        url = (url or "").strip()
        if not url:
            return False, "Paste the URL of your repository."
        if not (url.startswith("http") or url.startswith("git@")):
            return False, "The URL should start with https:// or git@"
        if "@" in url.split("://")[-1].split("/")[0] and not url.startswith("git@"):
            return False, ("That URL contains a username or token. Use the "
                           "plain repository URL — credentials belong in "
                           "git's credential helper, not in the remote.")
        return True, ""


# ── the providers ────────────────────────────────────────────────────────

GITHUB = RemoteProvider(
    id="github",
    display_name="GitHub",
    icon="\U0001f419",
    host="github.com",
    new_repo_url="https://github.com/new",
    auth_strategy=AUTH_CLI,
    cli_name="gh",
    cli_exe_key="gh_cli_exe",
    release_support=True,
    signin_hint="Sign in with `gh auth login`, or in your browser.",
    login_url="https://github.com/login",
    signup_url="https://github.com/signup",
    example_remote_url="https://github.com/you/my-project.git",
    repo_advice=("Go to github.com/new, fill in the repo name, leave it Public.\n"
                 "Do NOT tick 'Add README' or 'Add .gitignore' — you have those.\n"
                 "Then copy the HTTPS URL it shows you."),
    cli_display_name="GitHub CLI",
    cli_download_url="https://cli.github.com",
    steps=(
        SetupStep("2", "Sign in to GitHub",
                  "Create a free account at github.com if you do not have "
                  "one, then sign in.", url="https://github.com/join"),
        SetupStep("3", "Create the repository",
                  "Make an EMPTY repository — no README, no .gitignore, no "
                  "licence. Anything pre-created has to be merged before your "
                  "first push will go through.",
                  url="https://github.com/new"),
    ),
)

GITLAB = RemoteProvider(
    id="gitlab",
    display_name="GitLab",
    icon="\U0001f98a",
    host="gitlab.com",
    new_repo_url="https://gitlab.com/projects/new",
    auth_strategy=AUTH_CLI,
    cli_name="glab",
    cli_exe_key="glab_cli_exe",
    release_support=True,
    signin_hint="Sign in with `glab auth login`, or in your browser.",
    login_url="https://gitlab.com/users/sign_in",
    signup_url="https://gitlab.com/users/sign_up",
    example_remote_url="https://gitlab.com/you/my-project.git",
    repo_advice=("Create a blank project on gitlab.com, and untick\n"
                 "'Initialize repository with a README' — you have one.\n"
                 "Then copy the HTTPS clone URL."),
    cli_display_name="GitLab CLI",
    cli_download_url="https://gitlab.com/gitlab-org/cli",
    steps=(
        SetupStep("2", "Sign in to GitLab",
                  "Create a free account at gitlab.com if you do not have "
                  "one, then sign in.", url="https://gitlab.com/users/sign_up"),
        SetupStep("3", "Create the project",
                  "Create a BLANK project with 'Initialize repository with a "
                  "README' unticked — anything pre-created has to be merged "
                  "before your first push will go through.",
                  url="https://gitlab.com/projects/new"),
    ),
)

CODEBERG = RemoteProvider(
    id="codeberg",
    display_name="Codeberg",
    icon="\U0001f3d4",
    host="codeberg.org",
    new_repo_url="https://codeberg.org/repo/create",
    auth_strategy=AUTH_CREDENTIAL_HELPER,
    cli_name="",
    cli_exe_key="",
    release_support=False,
    signin_hint=("Codeberg has no CLI integration here. Configure git's "
                 "credential helper once and pushes will work: the manager "
                 "never handles your token."),
    login_url="https://codeberg.org/user/login",
    signup_url="https://codeberg.org/user/sign_up",
    example_remote_url="https://codeberg.org/you/my-project.git",
    repo_advice=("Create a new repository on codeberg.org and untick\n"
                 "'Initialize repository' — you already have commits.\n"
                 "Then copy the HTTPS clone URL."),
    steps=(
        SetupStep("2", "Sign in to Codeberg",
                  "Create a free account at codeberg.org if you do not have "
                  "one, then sign in.", url="https://codeberg.org/user/sign_up"),
        SetupStep("3", "Create the repository",
                  "Create an EMPTY repository — untick 'Initialize "
                  "repository'. Anything pre-created has to be merged before "
                  "your first push will go through.",
                  url="https://codeberg.org/repo/create"),
    ),
)

#: Registry, in the order the UI should offer them.
PROVIDERS = (GITHUB, GITLAB, CODEBERG)
_BY_ID = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str) -> RemoteProvider:
    """Look up by id, defaulting to GitHub.

    Defaulting rather than raising keeps a stale saved id — a provider that
    was removed, or a typo in hand-edited config — from making the setup
    dialog unopenable.
    """
    return _BY_ID.get((provider_id or "").strip().lower(), GITHUB)


def provider_for_url(url: str) -> "RemoteProvider | None":
    """Guess the provider from a remote URL, or None if it is not one of ours.

    Used to label existing remotes. None is a normal answer — self-hosted
    forges are common, and claiming a wrong provider would put the wrong
    sign-in instructions in front of the user.
    """
    text = (url or "").lower()
    for provider in PROVIDERS:
        if provider.host in text:
            return provider
    return None
