"""Tests for the PyScope integration — detection, client, and controller.

The properties under test are the ones the integration would be worthless
without, and every one of them is a distinction that is easy to collapse by
accident:

  * "configured", "executable" and "healthy" are three different answers;
  * a read API that cannot answer returns nothing, while the diagnostic API
    keeps the reason;
  * "PyScope says no" and "PyScope could not be asked" are not the same;
  * re-registering a project is a success, not a failure;
  * separators and letter case are normalised for two different reasons and
    on two different platforms.

No real pyscope binary is invoked: every test drives `_run`, the module's one
subprocess boundary, which is exactly what that boundary is for.
"""

import sys

import pytest

import helpers.pyscope as ps
from helpers.detection import _detect_pyscope, _is_pyscope_project


# ── Helpers ──────────────────────────────────────────────────────────────────

def fake_run(mapping, calls=None):
    """A `_run` stand-in driven by a {subcommand: _Run} mapping.

    Keyed on the first argv element so a test can make `version` succeed while
    `projects list` fails — which is the situation the tri-state registration
    answer exists for.
    """
    def _fake(exe, argv, timeout):
        if calls is not None:
            calls.append(list(argv))
        return mapping.get(argv[0], ps._Run(returncode=1, stderr="unexpected call"))
    return _fake


def ok(stdout=""):
    return ps._Run(returncode=0, stdout=stdout)


# ── Detection ────────────────────────────────────────────────────────────────

class TestDetection:

    def test_absent_returns_empty_string_not_the_bare_name(self, monkeypatch):
        """"" is what makes `if cfg.pyscope_exe:` a correct installation test.

        Returning "pyscope" would make the check pass everywhere and defer the
        failure to a subprocess call with a bare command name.
        """
        monkeypatch.setattr("helpers.detection.shutil.which", lambda _n: None)
        monkeypatch.setattr("helpers.detection.os.path.isfile", lambda _p: False)
        assert _detect_pyscope() == ""

    def test_path_wins_first(self, monkeypatch):
        monkeypatch.setattr("helpers.detection.shutil.which",
                            lambda n: r"C:\bin\pyscope.exe" if n == "pyscope.exe" else None)
        assert _detect_pyscope() == r"C:\bin\pyscope.exe"

    def test_falls_back_to_local_bin_where_uv_installs_shims(self, monkeypatch, tmp_path):
        """`uv tool install` writes to ~/.local/bin, not to a package manager's dir.

        This is the same directory the Cursor CLI probe already trusts, which
        is why it is trusted here rather than being a new assumption.
        """
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        shim = home / ".local" / "bin" / "pyscope.exe"
        shim.write_text("", encoding="utf-8")

        monkeypatch.setattr("helpers.detection.shutil.which", lambda _n: None)
        monkeypatch.setattr("helpers.detection.os.path.expanduser",
                            lambda p: str(home) if p == "~" else p)
        assert _detect_pyscope() == str(shim)

    def test_is_pyscope_project_is_about_the_cache_not_registration(self, tmp_path):
        """`.pyscope/` answers "is the cache local", never "does PyScope know this".

        PyScope only writes the directory when git ignores it, so a registered
        project can legitimately have no `.pyscope/` at all. Treating this as a
        registration check would report every such project as unknown.
        """
        assert _is_pyscope_project(str(tmp_path)) is False
        (tmp_path / ".pyscope").mkdir()
        assert _is_pyscope_project(str(tmp_path)) is True


# ── Three states, none inferred from another ─────────────────────────────────

class TestStatus:

    def test_nothing_configured(self):
        result = ps.status("")
        assert (result.configured, result.executable, result.state) == \
            ("", False, ps.STATE_ABSENT)

    def test_configured_but_not_a_file_is_absent(self, tmp_path):
        missing = str(tmp_path / "nope.exe")
        result = ps.status(missing)
        assert result.configured == missing
        assert result.executable is False
        assert result.state == ps.STATE_ABSENT

    def test_launchable_but_failing_is_unhealthy_and_keeps_the_reason(
            self, tmp_path, monkeypatch):
        """The distinction this whole module exists for.

        Reporting a crashing PyScope as "not installed" sends the user to
        install something they already have, and hides the error that would
        have told them what is actually wrong.
        """
        exe = tmp_path / "pyscope.exe"
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(ps, "_run", fake_run(
            {"version": ps._Run(returncode=2, stderr="ImportError: no module named x")}))

        result = ps.status(str(exe))
        assert result.executable is True
        assert result.state == ps.STATE_UNHEALTHY
        assert "ImportError" in result.detail

    def test_succeeds_but_says_nothing_is_malformed_not_ok(self, tmp_path, monkeypatch):
        exe = tmp_path / "pyscope.exe"
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(ps, "_run", fake_run({"version": ok("   \n\n")}))

        result = ps.status(str(exe))
        assert result.state == ps.STATE_MALFORMED
        assert result.version == ""

    def test_timeout_is_unhealthy_not_absent(self, tmp_path, monkeypatch):
        exe = tmp_path / "pyscope.exe"
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(ps, "_run", fake_run({"version": ps._Run(timed_out=True)}))

        result = ps.status(str(exe))
        assert result.executable is True
        assert result.state == ps.STATE_UNHEALTHY

    def test_healthy(self, tmp_path, monkeypatch):
        exe = tmp_path / "pyscope.exe"
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(ps, "_run", fake_run({"version": ok("0.1.0\n")}))

        result = ps.status(str(exe))
        assert (result.executable, result.state, result.version) == \
            (True, ps.STATE_OK, "0.1.0")


class TestReadApisCollapse:
    """Read APIs must never let a caller build a fact out of a failure."""

    @pytest.mark.parametrize("run", [
        ps._Run(returncode=1, stderr="boom"),
        ps._Run(timed_out=True),
        ps._Run(spawn_error="not executable"),
    ])
    def test_version_returns_empty_on_every_failure(self, monkeypatch, run):
        monkeypatch.setattr(ps, "_run", fake_run({"version": run}))
        assert ps.version("pyscope") == ""
        assert ps.is_available("pyscope") is False

    def test_version_uses_the_subcommand_not_a_double_dash_flag(self, monkeypatch):
        """`pyscope --version` is not an option; it exits non-zero with a usage
        error, so a probe written against it reports every healthy install as
        broken."""
        calls = []
        monkeypatch.setattr(ps, "_run", fake_run({"version": ok("0.1.0")}, calls))
        ps.version("pyscope")
        assert calls == [["version"]]

    def test_registered_returns_none_when_it_could_not_be_asked(self, monkeypatch):
        """None and [] are different answers and must not both be falsy-tested.

        [] means PyScope has no projects; None means PyScope did not tell us.
        """
        monkeypatch.setattr(ps, "_run", fake_run(
            {"projects": ps._Run(returncode=1, stderr="boom")}))
        assert ps.registered("pyscope") is None

    def test_registered_returns_empty_list_for_an_empty_registry(self, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run({"projects": ok("[]")}))
        assert ps.registered("pyscope") == []

    def test_unparseable_json_is_none_not_a_partial_list(self, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run({"projects": ok("{not json")}))
        assert ps.registered("pyscope") is None

    def test_analyze_refuses_a_path_that_is_not_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run({"analyze": ok("{}")}))
        assert ps.analyze("pyscope", str(tmp_path / "gone")) is None

    def test_analyze_rejects_a_non_dict_payload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run({"analyze": ok("[1, 2, 3]")}))
        assert ps.analyze("pyscope", str(tmp_path)) is None


# ── Registration is tri-state ────────────────────────────────────────────────

class TestRegistrationState:

    def _registry(self, *roots):
        import json
        return ok(json.dumps([{"name": "x", "root": r} for r in roots]))

    def test_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run(
            {"projects": self._registry(str(tmp_path))}))
        assert ps.registration_state("pyscope", str(tmp_path)) == ps.REG_REGISTERED

    def test_unregistered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_run", fake_run(
            {"projects": self._registry(r"C:\somewhere\else")}))
        assert ps.registration_state("pyscope", str(tmp_path)) == ps.REG_UNREGISTERED

    def test_unaskable_is_unknown_and_not_unregistered(self, tmp_path, monkeypatch):
        """The distinction that decides whether the UI nags.

        "PyScope does not know this project" and "we could not ask PyScope"
        deserve different words; rendering the second as the first tells people
        to register a project that may already be registered.
        """
        monkeypatch.setattr(ps, "_run", fake_run(
            {"projects": ps._Run(returncode=1, stderr="boom")}))
        assert ps.registration_state("pyscope", str(tmp_path)) == ps.REG_UNKNOWN


class TestRegister:

    def _mapping(self, before, after):
        import json

        state = {"n": 0}

        def _fake(exe, argv, timeout):
            if argv[0] == "projects" and argv[1] == "list":
                roots = before if state["n"] == 0 else after
                state["n"] += 1
                return ok(json.dumps([{"root": r} for r in roots]))
            if argv[0] == "projects" and argv[1] == "add":
                return ok("registered")
            return ps._Run(returncode=1)
        return _fake

    def test_new_registration_reports_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ps, "_run", self._mapping([], [str(tmp_path)]))
        result = ps.register("pyscope", str(tmp_path))
        assert result.ok is True
        assert result.changed is True
        assert result.already_present is False

    def test_already_registered_is_success(self, tmp_path, monkeypatch):
        """PyScope's own Registry.add registers-or-updates under one identity.

        Reporting the second run as a failure would make the bulk "Register
        with PyScope" action look broken every time after the first.
        """
        monkeypatch.setattr(ps, "_run",
                            self._mapping([str(tmp_path)], [str(tmp_path)]))
        result = ps.register("pyscope", str(tmp_path))
        assert result.ok is True
        assert result.already_present is True
        assert result.changed is False

    def test_success_exit_code_with_a_disagreeing_registry_is_a_failure(
            self, tmp_path, monkeypatch):
        """The verdict comes from re-reading PyScope, not from the exit code."""
        monkeypatch.setattr(ps, "_run", self._mapping([], []))
        result = ps.register("pyscope", str(tmp_path))
        assert result.ok is False
        assert "still unregistered" in result.detail

    def test_a_project_deleted_before_the_click_is_an_ordinary_result(
            self, tmp_path, monkeypatch):
        """Discovery, listing and clicking are three moments; things move.

        Without the re-check this race surfaces as a subprocess error dialog
        rather than a sentence saying the folder is gone.
        """
        called = []
        monkeypatch.setattr(ps, "_run", fake_run({}, called))
        result = ps.register("pyscope", str(tmp_path / "vanished"))
        assert result.ok is False
        assert "No longer a directory" in result.detail
        assert called == [], "PyScope was invoked for a path that is not there"


# ── Path comparison: two behaviours, two reasons ─────────────────────────────

class TestSamePath:

    def test_separators_are_unified_on_every_platform(self):
        assert ps.same_path(r"C:\proj\src", "C:/proj/src") is True

    def test_trailing_separator_is_not_a_different_project(self):
        assert ps.same_path("C:/proj/", "C:/proj") is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows case semantics")
    def test_case_is_folded_on_windows(self):
        assert ps.same_path("C:/Proj/Src", "c:/proj/src") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX case semantics")
    def test_case_is_significant_on_posix(self):
        """Two genuinely different files, and normcase would not catch it.

        `os.path.normcase` is a no-op on POSIX, so a comparison written in
        terms of it passes Linux CI while doing nothing at all.
        """
        assert ps.same_path("/proj/Src", "/proj/src") is False

    def test_empty_is_never_equal_to_anything(self):
        assert ps.same_path("", "") is False
        assert ps.same_path("", "C:/proj") is False


# ── The subprocess boundary itself ───────────────────────────────────────────

class TestRunBoundary:

    def test_no_exe_is_a_spawn_error_not_an_exception(self):
        run = ps._run("", ["version"], 1)
        assert run.launched is False
        assert run.ok is False

    def test_a_missing_binary_is_a_spawn_error_not_an_exception(self, tmp_path):
        run = ps._run(str(tmp_path / "nope.exe"), ["version"], 5)
        assert run.launched is False
        assert run.spawn_error

    def test_a_directory_is_a_spawn_error(self, tmp_path):
        """`exe` pointing at a folder must not raise out of the client."""
        run = ps._run(str(tmp_path), ["version"], 5)
        assert run.launched is False


# ── Config wiring ────────────────────────────────────────────────────────────

class TestConfigWiring:

    def test_explicit_path_beats_detection(self, monkeypatch):
        """A Settings save must not be silently reverted by whatever is on PATH."""
        from state import ManagerConfig
        monkeypatch.setattr("helpers.detection.shutil.which",
                            lambda _n: r"C:\detected\pyscope.exe")
        cfg = ManagerConfig(raw={"pyscope_exe": r"C:\chosen\pyscope.exe"})
        assert cfg.pyscope_exe == r"C:\chosen\pyscope.exe"

    def test_blank_config_falls_back_to_detection(self, monkeypatch):
        from state import ManagerConfig
        monkeypatch.setattr("helpers.detection.shutil.which",
                            lambda n: r"C:\detected\pyscope.exe"
                            if n == "pyscope.exe" else None)
        cfg = ManagerConfig(raw={"pyscope_exe": ""})
        assert cfg.pyscope_exe == r"C:\detected\pyscope.exe"

    def test_pyscope_is_not_in_the_agent_cli_table(self):
        """Analysis tools and coding-agent CLIs are two registries.

        `AGENT_CLIS` describes CLIs by how they carry a prompt. PyScope carries
        no prompt and drives no agent; a row there would make a non-agent
        resolvable as the configured coding agent.
        """
        from helpers.agent_cli import AGENT_CLIS
        assert "pyscope" not in AGENT_CLIS
        for spec in AGENT_CLIS.values():
            assert "pyscope" not in spec.binaries

    def test_agent_exe_map_does_not_resolve_pyscope(self):
        """The other half of the same rule, at the resolution site."""
        from state import ManagerConfig
        consts = ManagerConfig._agent_exe_for.__code__.co_consts
        assert not any(isinstance(c, str) and "pyscope" in c for c in consts)


# ── The Settings section renders the three states distinctly ─────────────────

class TestSettingsSection:
    """What the section puts on screen, asserted as text rather than looked at.

    A screenshot of this dialog proves less than it appears to: the Settings
    body lives inside a Canvas scroll wrapper, so the live geometry scan skips
    every widget in it (206 of 211 on the run that produced this suite), and a
    capture only shows the viewport. The rendered strings are the claim worth
    checking, and they can be diffed.
    """

    def _section(self, tk_root, monkeypatch, raw=None):
        import tkinter as tk
        from dialogs.settings_pyscope import PyScopeSection

        # Never let the real probe run: `_build` schedules check_status, which
        # would spawn a thread and shell a subprocess during the test.
        monkeypatch.setattr(PyScopeSection, "check_status", lambda self: None)

        dlg = tk.Toplevel(tk_root)
        body = tk.Frame(dlg)
        body.pack()

        class _Cfg:
            pass
        cfg = _Cfg()
        cfg.raw = raw if raw is not None else {}
        return PyScopeSection(dlg, body, cfg), dlg

    def test_absent_shows_all_three_rows_as_unavailable_and_offers_the_command(
            self, tk_root, monkeypatch):
        section, _dlg = self._section(tk_root, monkeypatch)
        section._apply(ps.Status(detail="No PyScope executable configured or detected."))

        assert "not configured" in section._row_configured.cget("text")
        assert section._row_executable.cget("text") == "no"
        assert ps.STATE_ABSENT in section._row_status.cget("text")
        assert section._hint_frame.winfo_manager(), \
            "the install command should be offered when there is nothing to talk to"

    def test_unhealthy_is_not_absent_and_does_not_advise_installing(
            self, tk_root, monkeypatch):
        """The distinction the section exists to draw.

        Advising an install here would be advice for a problem the user does
        not have, and would bury the error that says what is really wrong.
        """
        section, _dlg = self._section(tk_root, monkeypatch)
        section._apply(ps.Status(
            configured=r"C:\bin\pyscope.exe", executable=True,
            state=ps.STATE_UNHEALTHY, detail="pyscope version exited 2: ImportError"))

        assert section._row_executable.cget("text") == "yes"
        assert ps.STATE_UNHEALTHY in section._row_status.cget("text")
        assert "ImportError" in section._row_status.cget("text")
        assert not section._hint_frame.winfo_manager(), \
            "a broken install must not be told to install itself"

    def test_healthy_reports_the_version_and_hides_the_hint(
            self, tk_root, monkeypatch):
        section, _dlg = self._section(tk_root, monkeypatch)
        section._apply(ps.Status(
            configured=r"C:\bin\pyscope.exe", executable=True,
            state=ps.STATE_OK, version="0.1.0", detail="PyScope 0.1.0"))

        assert section._row_configured.cget("text") == r"C:\bin\pyscope.exe"
        assert section._row_executable.cget("text") == "yes"
        assert "0.1.0" in section._row_status.cget("text")
        assert not section._hint_frame.winfo_manager()

    def test_the_hint_comes_back_when_pyscope_goes_away(self, tk_root, monkeypatch):
        """Both directions, because a one-way toggle looks correct until it is used."""
        section, _dlg = self._section(tk_root, monkeypatch)
        section._apply(ps.Status(configured="x", executable=True, state=ps.STATE_OK,
                                 version="0.1.0"))
        assert not section._hint_frame.winfo_manager()
        section._apply(ps.Status())
        assert section._hint_frame.winfo_manager()

    def test_save_into_writes_the_configured_path(self, tk_root, monkeypatch):
        section, _dlg = self._section(
            tk_root, monkeypatch, raw={"pyscope_exe": r"  C:\bin\pyscope.exe  "})
        raw = {}
        assert section.save_into(raw) is True
        assert raw["pyscope_exe"] == r"C:\bin\pyscope.exe"


# ── Launching the GUI stays inside the one subprocess boundary ───────────────

class TestLaunchGui:

    def test_a_missing_directory_never_reaches_a_subprocess(self, tmp_path):
        """Same pre-flight as register(): the folder may have gone."""
        launched, detail = ps.launch_gui("pyscope", str(tmp_path / "gone"))
        assert launched is False
        assert "No longer a directory" in detail

    def test_an_unlaunchable_binary_is_reported_not_raised(self, tmp_path):
        launched, detail = ps.launch_gui(str(tmp_path / "nope.exe"), str(tmp_path))
        assert launched is False
        assert "Could not launch" in detail

    def test_the_controller_does_not_spawn_processes_itself(self):
        """One module owns process creation for PyScope.

        A ``Popen`` in the controller is how the CREATE_NO_WINDOW rule and the
        timeout policy acquire a second implementation that drifts from the
        first — the same argument ``_run`` exists for.

        Asserted against the parsed imports rather than the file text: the
        first version of this test searched for the substring "Popen" and
        failed on the docstring that explains why there is no Popen.
        """
        import ast
        tree = ast.parse(
            open("src/controllers/pyscope_ctrl.py", encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "subprocess" not in imported
        assert "os" in imported, "sanity: the scan can still see real imports"
