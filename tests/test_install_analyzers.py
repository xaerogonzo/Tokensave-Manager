"""Guards on `helpers/install_analyzers.py` and the install half of the table.

The load-bearing ones:

* **ruff is not on npm** — encoding a measurement so it is not re-litigated,
  and because the package that IS called `ruff` there is something else
  entirely;
* a **missing package manager** is a state, not an error, and never causes a
  command to be attempted anyway;
* every declared install route is actually runnable — a row that names a
  manager the table does not have would fail only when someone clicked.
"""

import os
import subprocess

import pytest

from helpers import install_analyzers as installers
from helpers.install_analyzers import (
    MANAGER_MISSING, MANAGERS, ManagerSpec, availability, command_hint,
    install, manager_for, uninstall, update,
)
from helpers.headless_analyzers import ANALYZERS, BY_KEY


# ── the install half of the analyzer table ───────────────────────────────────

class TestTheTableDeclaresRunnableRoutes:
    def test_every_declared_installer_exists_in_the_manager_table(self):
        """A row naming an unknown manager fails only when someone clicks it."""
        for spec in ANALYZERS:
            if spec.installer:
                assert spec.installer in MANAGERS, spec.key

    def test_every_row_with_an_installer_names_a_package(self):
        for spec in ANALYZERS:
            if spec.installer:
                assert spec.package, spec.key

    def test_ruff_is_not_obtained_from_npm(self):
        """Measured, and the reason is worth keeping.

        The `ruff` package on the npm registry is an unrelated ES6-generator
        coroutine library at version 1.5.4. Installing it would put something
        entirely wrong on the machine while every log line reported success —
        and `@astral-sh/ruff` does not exist; only WASM bindings are published
        under that scope. ruff comes from PyPI via uv, which is the route this
        codebase already uses for PyScope.
        """
        assert BY_KEY["ruff"].installer == "uv"
        assert BY_KEY["ruff"].package == "ruff"

    def test_the_npm_rows_use_their_real_package_names(self):
        assert BY_KEY["pyright"].package == "pyright"
        # The modern package is the `-cli2` one; the bare name is the old CLI.
        assert BY_KEY["markdownlint"].package == "markdownlint-cli2"

    def test_a_uv_installed_tool_is_probed_where_uv_writes_it(self):
        """`uv tool install` writes to ~/.local/bin, which PATH may not list.

        Without the fallback the row reads "not configured" immediately after a
        successful install — a working feature that looks broken.
        """
        assert "~/.local/bin" in BY_KEY["ruff"].fallback_dirs

    def test_the_npm_rows_are_probed_where_npm_writes_them(self):
        for key in ("pyright", "markdownlint"):
            assert any("npm" in d for d in BY_KEY[key].fallback_dirs), key


class TestCommandHint:
    def test_it_is_the_command_a_person_would_actually_type(self):
        assert command_hint(BY_KEY["ruff"]) == "uv tool install ruff"
        assert command_hint(BY_KEY["pyright"]) == "npm install -g pyright"
        assert (command_hint(BY_KEY["markdownlint"])
                == "npm install -g markdownlint-cli2")

    def test_a_row_with_no_route_offers_no_command(self):
        from helpers.headless_analyzers import AnalyzerSpec, parse_ruff_json
        orphan = AnalyzerSpec(key="x", label="x", config_key="x_exe",
                              probe_names=("x",), argv=(),
                              parse=parse_ruff_json)
        assert command_hint(orphan) == ""
        assert manager_for(orphan) is None


# ── a missing package manager is a state ─────────────────────────────────────

class TestManagerMissing:
    def _absent(self, monkeypatch, key):
        spec = MANAGERS[key]
        monkeypatch.setitem(
            MANAGERS, key,
            ManagerSpec(key=spec.key, label=spec.label, detect=lambda: "",
                        install=spec.install, update=spec.update,
                        uninstall=spec.uninstall,
                        install_manager_hint=spec.install_manager_hint))

    def test_availability_reports_missing_with_a_hint(self, monkeypatch):
        self._absent(monkeypatch, "uv")
        state, detail = availability(BY_KEY["ruff"])
        assert state == MANAGER_MISSING
        assert "uv" in detail

    def test_a_row_with_no_route_is_also_missing_but_says_so_differently(self):
        from helpers.headless_analyzers import AnalyzerSpec, parse_ruff_json
        orphan = AnalyzerSpec(key="x", label="x", config_key="x_exe",
                              probe_names=("x",), argv=(),
                              parse=parse_ruff_json)
        state, detail = availability(orphan)
        assert state == MANAGER_MISSING
        assert "no install route" in detail

    @pytest.mark.parametrize("verb", [install, update, uninstall])
    def test_the_command_is_not_attempted_when_the_manager_is_absent(
            self, verb, monkeypatch):
        """Refused before spawning, so the user gets a hint and not a WinError.

        Running the verb anyway produces "the system cannot find the file
        specified", which names our own bug rather than their missing tool.
        """
        self._absent(monkeypatch, "uv")

        def explode(*_a, **_k):                       # pragma: no cover
            raise AssertionError("a subprocess was spawned anyway")

        monkeypatch.setattr(installers.subprocess, "run", explode)
        ok, log = verb(BY_KEY["ruff"])
        assert ok is False
        assert "uv is not installed" in log


# ── running a verb ───────────────────────────────────────────────────────────

class TestRunning:
    def _present(self, monkeypatch, key, exe):
        spec = MANAGERS[key]
        monkeypatch.setitem(
            MANAGERS, key,
            ManagerSpec(key=spec.key, label=spec.label, detect=lambda: exe,
                        install=spec.install, update=spec.update,
                        uninstall=spec.uninstall,
                        install_manager_hint=spec.install_manager_hint))

    def test_the_verb_argv_is_substituted_and_ordered(self, tmp_path,
                                                      monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))
        seen = {}

        def fake(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "done", "")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        ok, _log = install(BY_KEY["ruff"])
        assert ok is True
        assert seen["argv"] == [str(exe), "tool", "install", "ruff"]

    def test_update_and_uninstall_use_their_own_verbs(self, tmp_path,
                                                      monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv[1:])
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        update(BY_KEY["ruff"])
        uninstall(BY_KEY["ruff"])
        assert calls == [["tool", "upgrade", "ruff"],
                         ["tool", "uninstall", "ruff"]]

    def test_npm_update_pins_latest_rather_than_using_npm_update(self,
                                                                 tmp_path,
                                                                 monkeypatch):
        """`npm update -g` sometimes silently no-ops on Windows.

        The same decision `install_codegraph` already records, carried into
        the table rather than restated at a call site.
        """
        exe = tmp_path / "npm.cmd"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "npm", str(exe))
        seen = {}

        def fake(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        update(BY_KEY["pyright"])
        assert seen["argv"][1:] == ["install", "-g", "pyright@latest"]

    def test_a_nonzero_exit_is_a_failure_with_the_output_kept(self, tmp_path,
                                                              monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))

        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "no such package")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        ok, log = install(BY_KEY["ruff"])
        assert ok is False
        assert "no such package" in log

    def test_a_timeout_is_reported_not_raised(self, tmp_path, monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))

        def fake(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 300)

        monkeypatch.setattr(installers.subprocess, "run", fake)
        ok, log = install(BY_KEY["ruff"])
        assert ok is False
        assert "timed out" in log

    def test_a_spawn_failure_is_reported_not_raised(self, tmp_path,
                                                    monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))

        def fake(argv, **kwargs):
            raise OSError("WinError 2")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        ok, log = install(BY_KEY["ruff"])
        assert ok is False
        assert "WinError 2" in log

    def test_a_manager_path_that_is_not_a_file_never_spawns(self, tmp_path,
                                                            monkeypatch):
        """The bare-name guard, one layer down.

        `detect` returning something that is not on disk must not reach
        `subprocess`, or the error names our command line rather than the
        missing tool.
        """
        self._present(monkeypatch, "uv", str(tmp_path / "ghost.exe"))

        def explode(*_a, **_k):                       # pragma: no cover
            raise AssertionError("spawned a non-existent manager")

        monkeypatch.setattr(installers.subprocess, "run", explode)
        ok, log = install(BY_KEY["ruff"])
        assert ok is False
        assert "not found" in log

    def test_log_lines_reach_the_callback(self, tmp_path, monkeypatch):
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))

        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "line one\nline two", "")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        seen = []
        install(BY_KEY["ruff"], on_log=seen.append)
        assert any("uv tool install ruff" in s for s in seen)
        assert "line one" in seen and "line two" in seen

    def test_a_raising_log_callback_does_not_break_the_install(self, tmp_path,
                                                               monkeypatch):
        """A destroyed Tk widget on the other end must not fail the install."""
        exe = tmp_path / "uv.exe"
        exe.write_text("", encoding="utf-8")
        self._present(monkeypatch, "uv", str(exe))

        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "out", "")

        def boom(_line):
            raise RuntimeError("widget is gone")

        monkeypatch.setattr(installers.subprocess, "run", fake)
        ok, _log = install(BY_KEY["ruff"], on_log=boom)
        assert ok is True


# ── detection ────────────────────────────────────────────────────────────────

class TestDetectUv:
    def test_exe_is_probed_before_the_bare_name(self, monkeypatch):
        """A native binary, so `.exe` leads — `CreateProcess` ignores PATHEXT."""
        from helpers import detection
        asked = []

        def fake_which(name):
            asked.append(name)
            return "C:/bin/uv.exe" if name == "uv.exe" else None

        monkeypatch.setattr(detection.shutil, "which", fake_which)
        assert detection._detect_uv() == "C:/bin/uv.exe"
        assert asked[0] == "uv.exe"

    def test_local_bin_is_the_fallback(self, monkeypatch, tmp_path):
        from helpers import detection
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        (home / ".local" / "bin" / "uv.exe").write_text("", encoding="utf-8")
        monkeypatch.setattr(detection.shutil, "which", lambda name: None)
        monkeypatch.setattr(detection.os.path, "expanduser",
                            lambda p: str(home) if p == "~" else p)
        assert detection._detect_uv().startswith(str(home))

    def test_absent_is_an_empty_string_never_the_bare_name(self, monkeypatch):
        """So `if exe:` means installed, and nothing shells a bare command."""
        from helpers import detection
        monkeypatch.setattr(detection.shutil, "which", lambda name: None)
        monkeypatch.setattr(detection.os.path, "expanduser",
                            lambda p: "/nonexistent-home")
        assert detection._detect_uv() == ""


# ── the probe fallback on the analyzer table ─────────────────────────────────

def test_a_fallback_directory_is_probed_when_path_does_not_know(monkeypatch,
                                                                tmp_path):
    """The "installed but PATH not refreshed" case, end to end."""
    import helpers.headless_analyzers as analyzers
    from helpers.headless_analyzers import READY, AnalyzerSpec, resolve

    (tmp_path / "ruff.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(analyzers.shutil, "which", lambda name: None)
    spec = AnalyzerSpec(
        key="ruff", label="ruff", config_key="ruff_exe",
        probe_names=("ruff.exe", "ruff"), argv=(),
        parse=analyzers.parse_ruff_json, fallback_dirs=(str(tmp_path),))
    state, exe = resolve(spec, "")
    assert state == READY
    assert exe == os.path.join(str(tmp_path), "ruff.exe")
