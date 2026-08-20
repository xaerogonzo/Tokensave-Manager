"""Read tokensave's per-project config — currently the ``strict_tree`` switch.

tokensave v7.10.0 added ``strict_tree`` (upstream #372 §2). With it on, a
``tokensave_*`` call that would be answered from the wrong working tree
*errors* instead of returning a plausible-looking answer about a checkout you
are not in. Without it, both wrong-tree detections tokensave already has (a
borrowed worktree index, and a branch that drifted under a running server) are
advisory only: a warning prefixed to an answer the tools produce anyway.

Why the manager cares: an empty or wrong result reads as "no such symbol"
rather than "wrong tree", so every grounding surface built on tokensave
inherits the mistake with no signal. That is not hypothetical here — a session
in this very repository had `tokensave_status` answer from an unrelated
project, with the wrong file count and an unrelated branch name, and nothing in
the response said so.

The setting lives in ``<project>/.tokensave/config.json`` as a JSON boolean.
Note that is NOT the global ``~/.tokensave/config.toml``, which holds different
keys entirely.

Pure module — stdlib only, no Tkinter, safe from any thread.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Verdicts. `UNREADABLE` is deliberately distinct from `DISABLED`: "we could
# not determine the setting" must never be rendered as "the setting is off".
ENABLED    = "enabled"
DISABLED   = "disabled"
MISSING    = "missing"
MALFORMED  = "malformed"
UNREADABLE = "unreadable"

_CONFIG_REL = os.path.join(".tokensave", "config.json")


@dataclass(frozen=True)
class StrictTreeState:
    """What ``strict_tree`` is set to, and how confident we are about it."""

    verdict: str
    detail: str

    @property
    def is_enabled(self) -> bool:
        """True only when the setting is definitely on."""
        return self.verdict == ENABLED

    @property
    def is_known(self) -> bool:
        """False when we could not read the setting at all.

        Callers must branch on this before saying anything about the value —
        an unreadable config tells us nothing, and reporting it as "off" would
        be inventing a fact.
        """
        return self.verdict not in (UNREADABLE, MALFORMED)

    @property
    def is_defect(self) -> bool:
        """True only for states that are actually wrong.

        Deliberately excludes DISABLED and MISSING. Upstream ships
        ``strict_tree`` off by default on purpose — sharing one index across a
        family of worktrees is a legitimate setup — so an off setting is a
        choice to inform, not a fault to flag. Only a config we cannot parse
        is a defect in its own right.
        """
        return self.verdict == MALFORMED


def config_path(project_root: str) -> str:
    """Absolute path to the project's tokensave config."""
    return os.path.join(project_root, _CONFIG_REL)


def read_strict_tree(project_root: str) -> StrictTreeState:
    """Return the project's ``strict_tree`` state.

    Never raises: every failure resolves to UNREADABLE/MALFORMED with a
    human-readable detail, because this runs inside a Doctor pass whose other
    checks must not be lost to one bad file.
    """
    path = config_path(project_root)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return StrictTreeState(
            UNREADABLE,
            "no .tokensave/config.json — not an initialised tokensave project, "
            "or the config has been removed")
    except (OSError, UnicodeDecodeError) as exc:
        return StrictTreeState(
            UNREADABLE, f"could not read .tokensave/config.json ({exc})")
    except json.JSONDecodeError as exc:
        return StrictTreeState(
            MALFORMED, f".tokensave/config.json is not valid JSON ({exc})")

    if not isinstance(raw, dict):
        return StrictTreeState(
            MALFORMED,
            ".tokensave/config.json does not contain a JSON object")

    if "strict_tree" not in raw:
        return StrictTreeState(
            MISSING,
            "no strict_tree key — written by a tokensave older than v7.10.0; "
            "the effective behaviour is off")

    value = raw["strict_tree"]
    if not isinstance(value, bool):
        return StrictTreeState(
            MALFORMED,
            f"strict_tree should be true or false, found {value!r}")

    if value:
        return StrictTreeState(
            ENABLED, "wrong-tree answers are refused rather than returned")
    return StrictTreeState(
        DISABLED,
        "wrong-tree detections are advisory only — a query answered from "
        "another checkout comes back looking normal")


def should_recommend_enabling(state: StrictTreeState,
                              risk_present: bool) -> bool:
    """Whether to actively suggest turning ``strict_tree`` on.

    Recommend only when the setting is off AND the caller has evidence the
    wrong-tree failure is actually reachable for this project (today: git
    worktrees with no index of their own).

    The unconditional version of this check would print on every Doctor run
    for every project, which is how a useful signal becomes noise the user
    learns to scroll past — the same nagging problem this Doctor has already
    had to fix once for agent-install prompts. An unknown state never
    triggers a recommendation either: we would be guessing.
    """
    return risk_present and state.verdict in (DISABLED, MISSING)
