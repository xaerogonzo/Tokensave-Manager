"""Tests for PyScope's MCP wiring — two states, and a scope exception.

The properties here are the ones that decide whether the manager tells the
truth about a half-finished binding:

  * MCP presence and project registration are separate answers, and one
    landing without the other is a PARTIAL outcome rather than a green one;
  * the verdict comes from re-reading both systems, not from return codes;
  * `absent` and `malformed` never collapse, because the second forbids a
    write that the first invites;
  * drift is measured by canonical executable identity, not string equality;
  * user scope is correct for `pyscope` and a hazard for everything else, and
    the exception is keyed on the SERVER so it cannot broaden.

Nothing here touches the real `~/.claude.json`: every test passes an explicit
temp path, which is also why `bind_user_entry` takes one.
"""

import json
import sys

import pytest

import helpers.mcp_pyscope as mp
import helpers.pyscope as ps
from helpers.mcp_scope import (EffectiveScope, SCOPE_USER, SCOPE_PROJECT,
                               describe_effective)


# ── Helpers ──────────────────────────────────────────────────────────────────

def write_cfg(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "claude.json"


@pytest.fixture
def exe(tmp_path):
    p = tmp_path / "pyscope.exe"
    p.write_text("", encoding="utf-8")
    return str(p)


# ── Reading the entry ────────────────────────────────────────────────────────

class TestBindingState:

    def test_missing_file_is_absent(self, cfg_path, exe):
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_ABSENT

    def test_no_pyscope_key_is_absent(self, cfg_path, exe):
        write_cfg(cfg_path, {"mcpServers": {"tokensave": {"command": "x"}}})
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_ABSENT

    def test_broken_json_is_malformed_not_absent(self, cfg_path, exe):
        """A broken config is not an empty one.

        Folding these together is what would let the manager cheerfully
        overwrite a file the user is halfway through editing.
        """
        cfg_path.write_text("{not json", encoding="utf-8")
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_MALFORMED

    def test_entry_pointing_at_the_configured_exe_is_present(self, cfg_path, exe):
        write_cfg(cfg_path, {"mcpServers": {"pyscope": mp.canonical_entry(exe)}})
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_PRESENT

    def test_a_different_spelling_of_the_same_path_is_not_drift(
            self, cfg_path, exe):
        """C:/x/pyscope.exe and C:\\x\\pyscope.exe are one binary.

        A byte comparison would report drift for a path nobody changed, and
        the remedy offered would rewrite the entry to itself.
        """
        write_cfg(cfg_path, {"mcpServers": {
            "pyscope": {"command": exe.replace("\\", "/"), "args": ["mcp"]}}})
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_PRESENT

    def test_a_genuinely_different_binary_is_stale(self, cfg_path, exe, tmp_path):
        other = tmp_path / "old" / "pyscope.exe"
        other.parent.mkdir()
        other.write_text("", encoding="utf-8")
        write_cfg(cfg_path, {"mcpServers": {
            "pyscope": {"command": str(other), "args": ["mcp"]}}})
        state, detail = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_STALE_COMMAND
        assert "old" in detail

    def test_an_entry_without_a_command_is_malformed(self, cfg_path, exe):
        write_cfg(cfg_path, {"mcpServers": {"pyscope": {"args": ["mcp"]}}})
        state, _ = mp.binding_state(exe, str(cfg_path))
        assert state == mp.STATE_MALFORMED


# ── Writing ──────────────────────────────────────────────────────────────────

class TestBindUserEntry:

    def test_every_other_server_survives(self, cfg_path, exe):
        """This file is the user's whole MCP configuration."""
        write_cfg(cfg_path, {
            "mcpServers": {"tokensave": {"command": "ts"},
                           "codegraph": {"command": "cg"}},
            "someOtherKey": {"kept": True},
        })
        ok, _ = mp.bind_user_entry(exe, str(cfg_path))
        assert ok
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"tokensave", "codegraph", "pyscope"}
        assert data["mcpServers"]["tokensave"] == {"command": "ts"}
        assert data["someOtherKey"] == {"kept": True}

    def test_it_refuses_to_write_into_a_malformed_file(self, cfg_path, exe):
        """The refusal is the entire reason malformed is not folded into absent."""
        cfg_path.write_text("{not json", encoding="utf-8")
        ok, why = mp.bind_user_entry(exe, str(cfg_path))
        assert ok is False
        assert "not valid JSON" in why
        assert cfg_path.read_text(encoding="utf-8") == "{not json"

    def test_the_entry_embeds_the_resolved_path_never_a_bare_command(self, exe):
        """The generated config must not depend on the client's PATH.

        Falling back to a bare `pyscope` is the failure this guards: it is the
        reason the manager resolves and caches an absolute path at all.
        """
        entry = mp.canonical_entry(exe)
        assert entry["command"] == exe
        assert entry["command"] != "pyscope"
        assert entry["args"] == ["mcp"]

    def test_no_executable_is_refused(self, cfg_path):
        ok, why = mp.bind_user_entry("", str(cfg_path))
        assert ok is False
        assert "No PyScope executable" in why

    def test_unbind_leaves_the_other_servers(self, cfg_path, exe):
        write_cfg(cfg_path, {"mcpServers": {
            "tokensave": {"command": "ts"}, "pyscope": mp.canonical_entry(exe)}})
        ok, _ = mp.unbind_user_entry(str(cfg_path))
        assert ok
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"tokensave"}

    def test_unbind_on_a_malformed_file_refuses(self, cfg_path):
        cfg_path.write_text("{not json", encoding="utf-8")
        ok, _ = mp.unbind_user_entry(str(cfg_path))
        assert ok is False


# ── Reconciliation ───────────────────────────────────────────────────────────

class TestReconcile:
    """Binding is two steps and is explicitly not atomic."""

    def _fake_register(self, monkeypatch, ok=True, changed=True):
        monkeypatch.setattr(ps, "register", lambda e, p: ps.RegisterResult(
            ok=ok, changed=changed, detail="registered" if ok else "failed"))

    def _fake_reg_state(self, monkeypatch, state):
        monkeypatch.setattr(ps, "registration_state", lambda e, p: state)

    def test_both_halves_landing_is_ok(self, cfg_path, exe, tmp_path, monkeypatch):
        self._fake_register(monkeypatch)
        self._fake_reg_state(monkeypatch, ps.REG_REGISTERED)
        result = mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert result.overall == mp.OVERALL_OK
        assert result.mcp_changed is True
        assert result.mcp_state == mp.STATE_PRESENT

    def test_registration_without_an_entry_is_partial_not_ok(
            self, cfg_path, exe, tmp_path, monkeypatch):
        """An entry the server cannot answer through is half a feature.

        The user has to be able to see WHICH half, so this cannot be reported
        as success or as outright failure.
        """
        cfg_path.write_text("{not json", encoding="utf-8")   # write refused
        self._fake_register(monkeypatch)
        self._fake_reg_state(monkeypatch, ps.REG_REGISTERED)
        result = mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert result.overall == mp.OVERALL_PARTIAL
        assert result.mcp_changed is False

    def test_an_entry_without_registration_is_partial(
            self, cfg_path, exe, tmp_path, monkeypatch):
        self._fake_register(monkeypatch, ok=False, changed=False)
        self._fake_reg_state(monkeypatch, ps.REG_UNREGISTERED)
        result = mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert result.overall == mp.OVERALL_PARTIAL
        assert result.mcp_state == mp.STATE_PRESENT

    def test_an_unaskable_registry_is_partial_never_ok(
            self, cfg_path, exe, tmp_path, monkeypatch):
        """"We could not ask" must not be rendered as "it worked"."""
        self._fake_register(monkeypatch)
        self._fake_reg_state(monkeypatch, ps.REG_UNKNOWN)
        result = mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert result.overall == mp.OVERALL_PARTIAL

    def test_keeping_a_drifted_entry_is_partial_not_green(
            self, cfg_path, exe, tmp_path, monkeypatch):
        """Declining the replacement must not turn the row green.

        The user has a working server pointing somewhere else. Calling that
        "bound" is precisely the false green the two-state model exists for.
        """
        other = tmp_path / "old_pyscope.exe"
        other.write_text("", encoding="utf-8")
        write_cfg(cfg_path, {"mcpServers": {
            "pyscope": {"command": str(other), "args": ["mcp"]}}})
        self._fake_register(monkeypatch)
        self._fake_reg_state(monkeypatch, ps.REG_REGISTERED)
        result = mp.reconcile(exe, str(tmp_path), mp.DRIFT_KEEP, str(cfg_path))
        assert result.overall == mp.OVERALL_PARTIAL
        assert result.mcp_state == mp.STATE_STALE_COMMAND
        assert result.mcp_changed is False

    def test_replacing_a_drifted_entry_repoints_it(
            self, cfg_path, exe, tmp_path, monkeypatch):
        other = tmp_path / "old_pyscope.exe"
        other.write_text("", encoding="utf-8")
        write_cfg(cfg_path, {"mcpServers": {
            "pyscope": {"command": str(other), "args": ["mcp"]}}})
        self._fake_register(monkeypatch)
        self._fake_reg_state(monkeypatch, ps.REG_REGISTERED)
        result = mp.reconcile(exe, str(tmp_path), mp.DRIFT_REPLACE, str(cfg_path))
        assert result.overall == mp.OVERALL_OK
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["pyscope"]["command"] == exe

    def test_cancel_touches_nothing(self, cfg_path, exe, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(ps, "register",
                            lambda e, p: called.append(1) or ps.RegisterResult())
        self._fake_reg_state(monkeypatch, ps.REG_UNREGISTERED)
        result = mp.reconcile(exe, str(tmp_path), mp.DRIFT_CANCEL, str(cfg_path))
        assert result.overall == mp.OVERALL_FAILED
        assert called == [], "cancel must not register either"
        assert not cfg_path.exists()

    def test_registration_happens_before_the_machine_wide_write(
            self, cfg_path, exe, tmp_path, monkeypatch):
        """Order is deliberate, not incidental.

        Advertising a server that answers "unknown project" is the worse
        failure, so the narrow verifiable step goes first.
        """
        order = []
        monkeypatch.setattr(ps, "register", lambda e, p: (
            order.append("register"), ps.RegisterResult(ok=True, changed=True))[1])
        self._fake_reg_state(monkeypatch, ps.REG_REGISTERED)
        real_bind = mp.bind_user_entry
        monkeypatch.setattr(mp, "bind_user_entry", lambda e, p="": (
            order.append("mcp"), real_bind(e, p))[1])
        mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert order == ["register", "mcp"]

    def test_the_verdict_comes_from_the_post_read_not_the_return_code(
            self, cfg_path, exe, tmp_path, monkeypatch):
        """register() claiming success while the registry disagrees is a failure."""
        self._fake_register(monkeypatch, ok=True, changed=True)
        self._fake_reg_state(monkeypatch, ps.REG_UNREGISTERED)
        result = mp.reconcile(exe, str(tmp_path), path=str(cfg_path))
        assert result.overall != mp.OVERALL_OK


# ── The scope exception ──────────────────────────────────────────────────────

class TestUserScopeIsServerSpecific:
    """`pyscope` + user scope is expected; nothing else changes meaning.

    The exception exists because `EffectiveScope.is_shadowed` returns True for
    ANY user scope and its own docstring admits it cannot tell "shadowed" from
    "never bound". For a server that is user-scoped by design that reading is
    backwards: it would advise retiring the only entry there is.
    """

    @pytest.mark.parametrize("server,expected_state", [
        ("pyscope",   "ok"),
        ("tokensave", "project_shadowed"),
        ("codegraph", "project_shadowed"),
        ("anything-else", "project_shadowed"),
    ])
    def test_the_matrix(self, server, expected_state):
        got = EffectiveScope(scope=SCOPE_USER)
        state, _label, _issue = describe_effective(got, server=server)
        assert state == expected_state

    def test_the_exception_is_keyed_on_the_server_not_the_scope(self):
        """A `scope == USER` predicate would silently reclassify tokensave.

        Asserted against the membership set itself, so widening it becomes a
        deliberate edit rather than something a refactor can do by accident.
        """
        from helpers.mcp_scope import USER_SCOPED_SERVERS
        assert "pyscope" in USER_SCOPED_SERVERS
        assert "tokensave" not in USER_SCOPED_SERVERS
        assert "codegraph" not in USER_SCOPED_SERVERS

    def test_pyscope_at_project_scope_is_untouched_by_the_exception(self):
        """The exception covers user scope only; it is not a blanket pass."""
        got = EffectiveScope(scope=SCOPE_PROJECT)
        state, _label, _issue = describe_effective(got, server="pyscope")
        assert state == "ok"


# ── Executable identity ──────────────────────────────────────────────────────

class TestSameExecutable:

    def test_separators_do_not_make_two_binaries(self, exe):
        assert mp.same_executable(exe, exe.replace("\\", "/")) is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows case semantics")
    def test_case_is_folded_on_windows(self, exe):
        assert mp.same_executable(exe.upper(), exe.lower()) is True

    def test_empty_is_never_the_same_as_anything(self, exe):
        assert mp.same_executable("", exe) is False
        assert mp.same_executable(exe, "") is False
