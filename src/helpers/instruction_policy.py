"""What the fleet is told, and what this Manager does — as one validated model.

`templates/project-baseline.md` is `@include`d by every wired project, so the
behavioural instructions in it are fleet-wide policy. This module makes that
policy a **value the Manager owns** rather than prose somebody edits, and the
baseline becomes its compiled artifact:

    manager-config.json -> InstructionPolicy -> baseline_blocks -> the baseline

**The direction of that arrow is the design.** If the compiled file disagrees
with the config, the config is right and the file is stale. Nothing in this
module or its callers ever reads policy back out of the Markdown -- a
hand-edited block is reported as drift, never adopted as intent. Without that
rule the artifact quietly becomes a second policy store and the two disagree
forever.

**Policy is per Manager INSTALLATION, not per repository.** It lives in this
install's config and compiles into the baseline under *its* `template_dir`. The
compiled artifact is version-controlled, so a `git pull` can hand you another
install's compiled policy while your config says something else. That is drift,
reported and repairable -- and it is why nothing recompiles on startup: pulling
a commit must never silently change what fifteen projects are told.

**Six fields, but only two render.** `agent_may_commit` and `agent_may_push`
become text in the baseline; the rest are operational (a hook this Manager
installs, grounding this Manager attaches). Keeping the rendered set small is
deliberate -- every rendered byte is paid on every message in every project.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

#: Bumped when the SEMANTICS of a key change or a default moves. Migration reads
#: it to tell "this config predates the key" from "the user chose this value".
POLICY_VERSION = 1

VERSION_KEY = "instruction_policy_version"

#: The only two fields that become text in the baseline, in the order they are
#: always rendered. Order is fixed here rather than derived from a dict so that
#: toggling one key can never reorder another's block and churn the diff.
RENDERED_KEYS = ("agent_may_commit", "agent_may_push")


@dataclass(frozen=True)
class InstructionPolicy:
    """The whole policy, as one object every consumer shares.

    Frozen because a policy is a value: the Composer stages a *new* one and
    applies it, rather than mutating a live object that three other callers
    might be reading.

    Defaults reproduce today's behaviour exactly. Both git permissions are
    False, which renders the instruction the fleet already carries, so nobody's
    agent gains a permission by upgrading the Manager.
    """

    agent_may_commit: bool = False
    agent_may_push: bool = False
    enable_session_note: bool = False
    enable_session_grounding: bool = False
    session_grounding_prompts: bool = True
    session_grounding_prose: bool = False

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> str:
        """`""` when coherent, else the reason. Called before any render or save.

        Validity is the MODEL's job, never the renderer's. If the renderer
        owned this rule, every other caller that builds a policy -- a future
        Settings tab, a migration, a test fixture -- would have to rediscover
        it, and one of them eventually would not.

        The chosen semantics, stated rather than left to be inferred from the
        English: **push requires commit**, because this Manager treats
        publication as downstream of agent-created local commits. The
        alternative reading (push permits publishing commits the agent did not
        make) is coherent in the abstract and is NOT what this product means.
        """
        if self.agent_may_push and not self.agent_may_commit:
            return ("Automatic pushes require automatic commits: publication is "
                    "downstream of the commits being published. Enable commits "
                    "first, or leave both off.")
        return ""

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ── Config round-trip ─────────────────────────────────────────────────

    def to_raw(self) -> dict:
        """The keys this policy owns, ready to merge into `cfg.raw`.

        Every field is written explicitly -- including ones equal to their
        default -- so that a later release changing a default cannot
        retroactively reinterpret this config's silence.
        """
        out = {field.name: bool(getattr(self, field.name))
               for field in fields(self)}
        out[VERSION_KEY] = POLICY_VERSION
        return out

    def with_change(self, key: str, value: bool) -> "InstructionPolicy":
        """A new policy with one field changed. Raises on an unknown key.

        Unknown keys raise rather than being ignored: a typo in a caller would
        otherwise silently stage a no-op and look like a toggle that does
        nothing.
        """
        if key not in {field.name for field in fields(self)}:
            raise KeyError("no such policy key: %r" % key)
        return replace(self, **{key: bool(value)})


def defaults() -> InstructionPolicy:
    return InstructionPolicy()


def from_raw(raw: dict) -> "tuple[InstructionPolicy, tuple]":
    """Read a policy out of `cfg.raw`. `(policy, keys_needing_persist)`.

    **An absent key is "never configured", which is not "chose the default".**
    That distinction is the whole reason migration exists here rather than
    being a runtime `raw.get(k, default)` forever: when a future release moves
    a default, an old config's silence must not be reinterpreted as consent to
    the new value. The same rule `install_dir` already keeps -- no record is
    not evidence of a move.

    So absent keys resolve to the established default AND are returned for the
    caller to persist, which converts silence into a recorded choice once.

    An invalid stored combination is repaired to the nearest coherent policy
    rather than raising: config is hand-editable, and a Manager that refuses to
    start because someone typed two booleans is worse than one that corrects
    them and says so. The repair is reported through `keys_needing_persist`.
    """
    raw = raw or {}
    values = {}
    needs_persist = []
    for field in fields(InstructionPolicy):
        if field.name in raw:
            values[field.name] = bool(raw.get(field.name))
        else:
            values[field.name] = field.default
            needs_persist.append(field.name)

    policy = InstructionPolicy(**values)
    if not policy.is_valid:
        # The only incoherent combination is push-without-commit; dropping push
        # is the safe direction, since the alternative would GRANT a permission
        # the config never coherently expressed.
        policy = policy.with_change("agent_may_push", False)
        needs_persist.append("agent_may_push")

    if raw.get(VERSION_KEY) != POLICY_VERSION:
        needs_persist.append(VERSION_KEY)

    return policy, tuple(dict.fromkeys(needs_persist))
