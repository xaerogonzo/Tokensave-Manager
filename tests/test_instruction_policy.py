"""Guards on the policy model.

The model exists so that six settings cannot drift apart into six loose
`raw.get()` calls with six different ideas of what a default means. These tests
protect the parts that would fail silently: the migration distinction between
*never configured* and *chose the default*, and the one combination that must
not exist.
"""

import dataclasses

import pytest

from helpers.instruction_policy import (
    POLICY_VERSION,
    RENDERED_KEYS,
    VERSION_KEY,
    InstructionPolicy,
    from_raw,
)


def test_defaults_reproduce_todays_fleet_behaviour():
    """Upgrading the Manager must not grant an agent a permission."""
    policy = InstructionPolicy()
    assert policy.agent_may_commit is False
    assert policy.agent_may_push is False
    assert policy.is_valid


def test_the_model_is_frozen():
    """A policy is a value: the Composer stages a new one, never mutates a live one."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        InstructionPolicy().agent_may_commit = True


def test_push_without_commit_is_invalid_at_the_model_layer():
    """Validity belongs to the model, so no renderer has to rediscover it.

    The semantics are a product decision, stated rather than inferred: this
    Manager treats publication as downstream of agent-created local commits.
    """
    bad = InstructionPolicy(agent_may_push=True, agent_may_commit=False)
    assert not bad.is_valid
    assert "require" in bad.validate()

    good = InstructionPolicy(agent_may_push=True, agent_may_commit=True)
    assert good.is_valid


def test_a_source_preference_survives_its_master_switch():
    """Feature off, choices remembered.

    `session_grounding_prompts` stays True while the master switch is off, and
    nothing normalises it away — otherwise turning the feature on later would
    silently discard what the user picked.
    """
    policy = InstructionPolicy(enable_session_grounding=False,
                               session_grounding_prompts=True)
    assert policy.is_valid
    assert policy.session_grounding_prompts is True
    assert from_raw(policy.to_raw())[0] == policy


def test_absent_keys_migrate_to_defaults_and_are_returned_for_persisting():
    """*Never configured* is not *chose the default*.

    That distinction is why migration exists here rather than being a runtime
    `raw.get(k, default)` forever. If a future release moves a default, an old
    config's silence must not be reinterpreted as consent to the new value — so
    silence is converted into a recorded choice exactly once.
    """
    policy, needs_persist = from_raw({})
    assert policy == InstructionPolicy()
    for field in dataclasses.fields(InstructionPolicy):
        assert field.name in needs_persist
    assert VERSION_KEY in needs_persist


def test_explicitly_stored_values_need_no_persist():
    stored = InstructionPolicy(agent_may_commit=True).to_raw()
    policy, needs_persist = from_raw(stored)
    assert policy.agent_may_commit is True
    assert needs_persist == ()


def test_an_incoherent_stored_combination_is_repaired_downwards():
    """Config is hand-editable, so it repairs rather than refusing to start.

    The direction matters: dropping `push` is safe, while raising `commit`
    would GRANT a permission the config never coherently expressed.
    """
    policy, needs_persist = from_raw({"agent_may_push": True,
                                      "agent_may_commit": False})
    assert policy.agent_may_push is False
    assert policy.agent_may_commit is False
    assert "agent_may_push" in needs_persist


def test_to_raw_writes_every_field_explicitly():
    """Including fields equal to their default, so silence cannot be reinterpreted."""
    raw = InstructionPolicy().to_raw()
    for field in dataclasses.fields(InstructionPolicy):
        assert field.name in raw
    assert raw[VERSION_KEY] == POLICY_VERSION


def test_with_change_rejects_an_unknown_key():
    """A typo would otherwise stage a no-op that looks like a dead toggle."""
    with pytest.raises(KeyError):
        InstructionPolicy().with_change("agent_may_yolo", True)


def test_only_two_keys_are_rendered_into_the_fleet_baseline():
    """Every rendered byte is paid on every message in every project.

    The other four keys are operational — a hook this Manager installs,
    grounding it attaches — and must not quietly start costing fleet bytes.
    """
    assert RENDERED_KEYS == ("agent_may_commit", "agent_may_push")
    names = {f.name for f in dataclasses.fields(InstructionPolicy)}
    assert set(RENDERED_KEYS) < names
