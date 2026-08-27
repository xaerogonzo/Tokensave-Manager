"""tests/test_extension_packaging.py — the VS Code extension's manifest.

The extension is a separate build with its own version number, and the two
ways that goes wrong are both silent:

  * **version drift.** `constants.APP_VERSION` is the canonical source — it
    cannot come from `git describe`, because the Nuitka onefile build ships
    without a repository — and `package.json` carries its own copy. Nothing
    stops them diverging except a test.
  * **packaging the wrong files.** `.vscodeignore` decides what lands in the
    `.vsix`. It used to exclude `bin/**/README.md` and nothing else under
    `bin/`, so a locally built `tokensave-manager-cli.exe` — gitignored, but
    present after any run of build.ps1 — would have been packaged, turning a
    ~14 KB extension into a ~15 MB one that also ships a snapshot CLI.

Both are checked from the manifest rather than by running `vsce`, so this stays
in the Python suite and needs no Node toolchain.
"""
from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_EXT = Path(__file__).resolve().parents[1] / "vscode-extension"


def _manifest() -> dict:
    return json.loads(_EXT.joinpath("package.json").read_text(encoding="utf-8"))


def _vscodeignore() -> list:
    text = _EXT.joinpath(".vscodeignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_the_extension_directory_is_where_we_think():
    assert _EXT.joinpath("package.json").is_file()


# ── version parity ────────────────────────────────────────────────────────

def test_package_version_matches_the_canonical_app_version():
    """One source of truth. A second version constant is how they drift."""
    from constants import APP_VERSION
    assert _manifest()["version"] == APP_VERSION


def test_the_vsix_filename_would_carry_that_same_version():
    """`vsce package` names the artefact `<name>-<target>-<version>.vsix`, so
    parity in the manifest is parity in the released filename."""
    from constants import APP_VERSION
    manifest = _manifest()
    expected = f"{manifest['name']}-win32-x64-{APP_VERSION}.vsix"
    assert expected.endswith(f"-{APP_VERSION}.vsix")
    assert manifest["name"] and manifest["version"] == APP_VERSION


# ── what gets packaged ────────────────────────────────────────────────────

def test_the_whole_bin_tree_is_excluded_not_just_its_readme():
    """The hole this test exists for: `bin/**/README.md` matched exactly one
    file, so a build artefact sitting in `bin/windows-x64/` shipped."""
    patterns = _vscodeignore()
    assert "bin/**" in patterns
    assert "bin/**/README.md" not in patterns, \
        "superseded by bin/**; leaving it suggests the narrow rule is enough"


@pytest.mark.parametrize("pattern", [
    "node_modules/**", "src/**", "test/**", "**/*.ts", "**/*.map",
    "package-lock.json",
])
def test_development_only_files_stay_out_of_the_package(pattern):
    assert pattern in _vscodeignore()


def test_the_compiled_javascript_is_explicitly_kept():
    """`**/*.ts` and `**/*.map` are broad; the negation is what saves out/."""
    assert "!out/**/*.js" in _vscodeignore()


def test_the_negation_comes_after_the_rule_it_negates():
    """Ignore files are order-sensitive, and a negation placed above the rule
    it undoes does nothing at all."""
    patterns = _vscodeignore()
    assert patterns.index("**/*.ts") < patterns.index("!out/**/*.js")


# ── the manifest declares what the extension actually is ──────────────────

def test_a_supported_node_range_is_declared():
    """CI pins a Node version; the manifest records which range that is meant
    to represent, so the two can be checked against each other by a human."""
    assert "node" in _manifest()["engines"]


def test_the_test_script_names_its_files_explicitly():
    """`node --test test/` resolves the directory as a module on Node 24, and
    shell glob expansion differs between a local shell and the CI runner."""
    script = _manifest()["scripts"]["test"]
    assert "test/cli.test.js" in script
    assert "test/diagnostics.test.js" in script


def test_every_test_file_is_actually_run():
    """A test file nobody runs is worse than no test file: it reads as
    coverage while asserting nothing."""
    script = _manifest()["scripts"]["test"]
    for path in sorted(_EXT.joinpath("test").glob("*.test.js")):
        assert f"test/{path.name}" in script, f"{path.name} is never run"


# ── the release-time package check ────────────────────────────────────────
#
# A release artefact is the one build nobody re-checks, so these rules run in
# the release workflow. A release check nobody has seen fail is a release check
# nobody knows works, hence synthetic packages here.

_VERIFY = (Path(__file__).resolve().parents[1]
           / ".github" / "scripts" / "verify_vsix.py")


def _verifier():
    spec = importlib.util.spec_from_file_location("verify_vsix", _VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GOOD_ENTRIES = (
    "extension.vsixmanifest",
    "extension/package.json",
    "extension/readme.md",
    "extension/out/extension.js",
    "extension/out/cli.js",
    "extension/out/diagnostics.js",
    "extension/out/tree.js",
)


def _package(tmp_path, version="9.9.9", entries=_GOOD_ENTRIES, extra=()):
    path = tmp_path / f"tokensave-manager-win32-x64-{version}.vsix"
    with zipfile.ZipFile(path, "w") as archive:
        for name in tuple(entries) + tuple(extra):
            archive.writestr(name, "x")
    return path


def test_the_verifier_script_exists_where_the_workflow_expects_it():
    assert _VERIFY.is_file()


def test_a_well_formed_package_passes(tmp_path):
    verify = _verifier()
    assert verify.check_package(_package(tmp_path), "9.9.9") == []


def test_a_bundled_cli_binary_is_refused(tmp_path):
    """THE check this script exists for. `.vscodeignore` excluded only
    `bin/**/README.md` until Roadmap-13, so a build artefact left in the tree
    would have shipped — 14 KB becoming 15 MB, carrying a snapshot CLI."""
    verify = _verifier()
    package = _package(
        tmp_path,
        extra=("extension/bin/windows-x64/tokensave-manager-cli.exe",))
    problems = verify.check_package(package, "9.9.9")
    assert any("exe" in p for p in problems)


@pytest.mark.parametrize("stray", [
    "extension/src/cli.ts",
    "extension/test/cli.test.js",
    "extension/out/cli.js.map",
    "extension/node_modules/typescript/index.js",
])
def test_development_files_are_refused(tmp_path, stray):
    verify = _verifier()
    problems = verify.check_package(_package(tmp_path, extra=(stray,)), "9.9.9")
    assert problems, f"{stray} should not be packageable"


@pytest.mark.parametrize("missing", [
    "extension/package.json",
    "extension/out/extension.js",
    "extension/out/diagnostics.js",
])
def test_a_package_missing_something_essential_is_refused(tmp_path, missing):
    """An extension that installs and then does nothing is worse than one that
    fails to build."""
    verify = _verifier()
    entries = tuple(e for e in _GOOD_ENTRIES if e != missing)
    problems = verify.check_package(_package(tmp_path, entries=entries), "9.9.9")
    assert problems


def test_a_version_mismatch_is_refused(tmp_path):
    """The third copy of the version — the one inside the built artefact —
    can only be checked here."""
    verify = _verifier()
    problems = verify.check_package(_package(tmp_path, version="1.0.0"), "9.9.9")
    assert any("canonical version" in p for p in problems)


def test_an_oversized_package_is_refused(tmp_path, monkeypatch):
    """A belt-and-braces ceiling: if something large slips past the name
    patterns, the size still gives it away."""
    verify = _verifier()
    monkeypatch.setattr(verify, "MAX_BYTES", 10)
    problems = verify.check_package(_package(tmp_path), "9.9.9")
    assert any("ceiling" in p for p in problems)
