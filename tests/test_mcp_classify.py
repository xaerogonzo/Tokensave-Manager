"""tests/test_mcp_classify.py — characterization of the MCP classifier + writer.

`_classify_mcp_entry` and `_apply_mcp_fix` are consumed in five places
(`app.py`, `doctor_ctrl`, `sync_ctrl`, `mcp_config`, `settings`) and one of them
gates a write into Claude's own configuration — and until this file they had
**zero direct coverage**. The only test that named the classifier mocked it.

These pin CURRENT behaviour, deliberately, before per-project `.mcp.json` support
changes the classifier. Landing them green against unmodified code is what makes
the later diff provably behaviour-preserving for the shapes that already exist;
without that baseline, "the existing states still work" would be an assertion
rather than a measurement.

So: no aspirational assertions here. If a verdict below looks odd, it is odd on
purpose — it records what the code does today.
"""
from __future__ import annotations

import json
import os

import pytest

from helpers.mcp import (
    PROJECT_PATH_ARG,
    _apply_mcp_fix,
    _canonical_mcp_entry,
    _canonical_project_entry,
    _classify_mcp_entry,
    _project_mcp_path,
    _same_project,
    _wrapper_path,
)


# ── helpers ───────────────────────────────────────────────────────────────

CFG: dict = {}          # a ManagerConfig.raw stand-in; only python_exe is read


def _write(path, servers=None, raw_text=None):
    """Write a Claude-shaped config file. `raw_text` bypasses JSON entirely."""
    if raw_text is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw_text)
        return path
    payload = {} if servers is None else {"mcpServers": servers}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


@pytest.fixture
def code_cfg(tmp_path):
    """A path the classifier treats as Claude Code (basename `.claude.json`)."""
    return str(tmp_path / ".claude.json")


@pytest.fixture
def desktop_cfg(tmp_path):
    """A path the classifier treats as Desktop (anything else)."""
    return str(tmp_path / "claude_desktop_config.json")


# ── the file-level states, identical for both config kinds ────────────────

@pytest.mark.parametrize("which", ["code", "desktop"])
def test_a_missing_file_is_no_file(which, code_cfg, desktop_cfg):
    path = code_cfg if which == "code" else desktop_cfg
    info = _classify_mcp_entry(path, CFG)
    assert info["state"] == "no_file"
    assert info["current"] is None
    # A proposal is offered even with nothing to diff against — the dialog
    # renders "(no current entry — will be added)" from exactly this shape.
    assert info["proposed"]


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_unparseable_json_is_never_reported_as_missing(which, code_cfg, desktop_cfg):
    """The distinction is load-bearing: `missing` invites an Apply that would
    rewrite a file we could not read, discarding whatever it held."""
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, raw_text="{ this is not json")
    info = _classify_mcp_entry(path, CFG)
    assert info["state"] == "unparseable"


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_a_file_without_a_tokensave_entry_is_missing(which, code_cfg, desktop_cfg):
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"other": {"command": "something"}})
    info = _classify_mcp_entry(path, CFG)
    assert info["state"] == "missing"
    assert info["current"] is None


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_a_non_dict_tokensave_entry_is_missing(which, code_cfg, desktop_cfg):
    """Guards the isinstance check — a string entry must not reach the
    checkers, which all assume `.get()` works."""
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"tokensave": "not-a-dict"})
    assert _classify_mcp_entry(path, CFG)["state"] == "missing"


# ── the command shapes ────────────────────────────────────────────────────

@pytest.mark.parametrize("which", ["code", "desktop"])
def test_the_bundled_wrapper_exe_is_ok_everywhere(which, code_cfg, desktop_cfg):
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"tokensave": {
        "command": r"C:\somewhere\tokensave-wrapper.exe", "args": []}})
    assert _classify_mcp_entry(path, CFG)["state"] == "ok"


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_pythonw_plus_the_real_wrapper_py_is_ok(which, code_cfg, desktop_cfg):
    """Uses the wrapper path this installation actually resolves, because the
    checker only says ok when the file is present on disk."""
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"tokensave": {
        "command": r"C:\Python\pythonw.exe", "args": [_wrapper_path()]}})
    assert _classify_mcp_entry(path, CFG)["state"] == "ok"


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_a_wrapper_py_that_does_not_exist_is_wrong_wrapper(
        which, code_cfg, desktop_cfg, tmp_path):
    path = code_cfg if which == "code" else desktop_cfg
    ghost = str(tmp_path / "gone" / "tokensave-wrapper.py")
    _write(path, servers={"tokensave": {
        "command": r"C:\Python\pythonw.exe", "args": [ghost]}})
    assert _classify_mcp_entry(path, CFG)["state"] == "wrong_wrapper"


def test_bare_serve_is_ok_for_claude_code(code_cfg):
    """Recorded as-is, and it is the shape this whole project-binding effort
    exists to replace: `tokensave install --agent claude` writes it, and it
    resolves by an upward search from whatever cwd the client used."""
    _write(code_cfg, servers={"tokensave": {
        "command": r"C:\tools\tokensave.exe", "args": ["serve"]}})
    assert _classify_mcp_entry(code_cfg, CFG)["state"] == "ok"


def test_bare_serve_is_flagged_for_desktop(desktop_cfg):
    """Desktop's server is long-lived and must route through the wrapper to
    honour the pin, so the same command is correct in one file and not the
    other. That asymmetry is the reason scope matters at all."""
    _write(desktop_cfg, servers={"tokensave": {
        "command": r"C:\tools\tokensave.exe", "args": ["serve"]}})
    assert _classify_mcp_entry(desktop_cfg, CFG)["state"] == "direct_serve"


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_serve_with_a_hardcoded_p_is_flagged_in_both_today(
        which, code_cfg, desktop_cfg):
    """The verdict that per-project support has to change.

    In a GLOBAL config `-p` really does lock every session to one project. In a
    project-scoped `.mcp.json` it is exactly right. Pinned here so the change is
    visible as a deliberate edit rather than drift.
    """
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"tokensave": {
        "command": r"C:\tools\tokensave.exe",
        "args": ["serve", "-p", r"D:\Work\ProjectA"]}})
    info = _classify_mcp_entry(path, CFG)
    assert info["state"] == "direct_serve"
    assert r"D:\Work\ProjectA" in info["issue"]


@pytest.mark.parametrize("which", ["code", "desktop"])
def test_an_unrecognised_command_is_wrong_wrapper(which, code_cfg, desktop_cfg):
    path = code_cfg if which == "code" else desktop_cfg
    _write(path, servers={"tokensave": {"command": "npx", "args": ["whatever"]}})
    assert _classify_mcp_entry(path, CFG)["state"] == "wrong_wrapper"


def test_current_is_echoed_back_for_diffing(code_cfg):
    """The dialog renders `current` verbatim; if it were dropped the diff would
    silently show an add where a replace was happening."""
    entry = {"command": "npx", "args": ["whatever"]}
    _write(code_cfg, servers={"tokensave": entry})
    assert _classify_mcp_entry(code_cfg, CFG)["current"] == entry


def test_classification_never_writes(code_cfg):
    """It runs on every startup and on every dialog render."""
    _write(code_cfg, servers={"tokensave": {"command": "npx"}})
    before = os.stat(code_cfg).st_mtime_ns
    for _ in range(3):
        _classify_mcp_entry(code_cfg, CFG)
    assert os.stat(code_cfg).st_mtime_ns == before


# ── the writer ────────────────────────────────────────────────────────────

def test_apply_creates_a_file_and_its_parent(tmp_path):
    path = str(tmp_path / "nested" / "deeper" / ".claude.json")
    ok, msg = _apply_mcp_fix(path, {"command": "x", "args": []})
    assert ok, msg
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["mcpServers"]["tokensave"] == {"command": "x", "args": []}


def test_apply_preserves_other_servers(code_cfg):
    """The single most important property: this file belongs to Claude, and
    other entries in it belong to other tools."""
    _write(code_cfg, servers={
        "codegraph": {"command": "codegraph", "args": ["serve", "--mcp"]},
        "tokensave": {"command": "old", "args": []}})
    ok, _ = _apply_mcp_fix(code_cfg, {"command": "new", "args": []})
    assert ok
    with open(code_cfg, encoding="utf-8") as fh:
        servers = json.load(fh)["mcpServers"]
    assert servers["codegraph"] == {"command": "codegraph", "args": ["serve", "--mcp"]}
    assert servers["tokensave"] == {"command": "new", "args": []}


def test_apply_preserves_unrelated_top_level_keys(code_cfg):
    """`~/.claude.json` carries far more than mcpServers — project history,
    approvals, telemetry. Rewriting the file from scratch would drop it."""
    with open(code_cfg, "w", encoding="utf-8") as fh:
        json.dump({"numStartups": 42, "projects": {"a": {}},
                   "mcpServers": {"tokensave": {"command": "old"}}}, fh)
    ok, _ = _apply_mcp_fix(code_cfg, {"command": "new", "args": []})
    assert ok
    with open(code_cfg, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["numStartups"] == 42
    assert data["projects"] == {"a": {}}


def test_apply_writes_a_backup_when_a_file_existed(code_cfg, tmp_path):
    _write(code_cfg, servers={"tokensave": {"command": "old"}})
    ok, msg = _apply_mcp_fix(code_cfg, {"command": "new", "args": []})
    assert ok
    backups = [p for p in os.listdir(tmp_path) if ".backup." in p]
    assert len(backups) == 1, backups
    assert "backup" in msg
    with open(os.path.join(str(tmp_path), backups[0]), encoding="utf-8") as fh:
        assert json.load(fh)["mcpServers"]["tokensave"]["command"] == "old"


def test_apply_is_idempotent(code_cfg):
    """Applying twice must not accumulate anything or change the result."""
    entry = {"command": "x", "args": ["serve"]}
    _apply_mcp_fix(code_cfg, entry)
    first = open(code_cfg, encoding="utf-8").read()
    _apply_mcp_fix(code_cfg, entry)
    assert open(code_cfg, encoding="utf-8").read() == first


def test_apply_refuses_an_unparseable_file_instead_of_overwriting(code_cfg):
    """Rewriting it would discard content we could not read — the same reason
    `set_strict_tree` refuses a malformed config."""
    _write(code_cfg, raw_text="{ not json")
    ok, msg = _apply_mcp_fix(code_cfg, {"command": "x"})
    assert ok is False
    assert "parse" in msg.lower()
    assert open(code_cfg, encoding="utf-8").read() == "{ not json"


# ── the proposed entry ────────────────────────────────────────────────────

def test_the_canonical_entry_routes_through_the_wrapper():
    """Global/Desktop semantics: the wrapper is what reads the pin. The
    project-scoped entry added later must NOT inherit this."""
    entry = _canonical_mcp_entry({"python_exe": r"C:\Python\pythonw.exe"})
    joined = " ".join([entry["command"], *entry["args"]]).lower()
    assert "tokensave-wrapper" in joined


def test_the_canonical_entry_uses_the_configured_python(code_cfg):
    entry = _canonical_mcp_entry({"python_exe": r"C:\Custom\pythonw.exe"})
    if not entry["command"].lower().endswith(".exe") or entry["args"]:
        assert entry["command"] == r"C:\Custom\pythonw.exe"


# ── project scope ─────────────────────────────────────────────────────────

# Everything below is new behaviour, measured in Roadmap-11 Phase 0 rather than
# assumed: Claude Code spawns a project-scoped MCP server with cwd = the project
# root even when the session started in a subdirectory, so `-p .` names this
# project. The verdicts here invert the global ones on purpose — a hardcoded
# project is a defect in a shared user config and the entire point in a
# project's own file.


def _project(tmp_path, name="projA"):
    """A directory that looks like an indexed tokensave project."""
    root = tmp_path / name
    (root / ".tokensave").mkdir(parents=True)
    return str(root)


def _bind(root, args):
    path = _project_mcp_path(root)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"tokensave": {
            "command": "tokensave", "args": args}}}, fh, indent=2)
    return path


def test_the_portable_form_is_bound_to_this_project(tmp_path):
    root = _project(tmp_path)
    info = _classify_mcp_entry(_bind(root, ["serve", "-p", "."]), CFG)
    assert info["state"] == "ok"
    assert "this project" in info["label"]


def test_a_bare_serve_in_a_project_file_is_unbound(tmp_path):
    """The inverse of the global verdict, and deliberately so: this is the one
    place with enough information to bind explicitly."""
    root = _project(tmp_path)
    info = _classify_mcp_entry(_bind(root, ["serve"]), CFG)
    assert info["state"] == "project_unbound"
    assert "cwd" in info["issue"]


def test_binding_to_a_different_project_is_flagged(tmp_path):
    """The failure the whole feature exists to prevent — and the one that
    looks completely normal from inside a session."""
    root = _project(tmp_path)
    other = _project(tmp_path, "projB")
    info = _classify_mcp_entry(_bind(root, ["serve", "-p", other]), CFG)
    assert info["state"] == "project_mismatch"
    assert "DIFFERENT" in info["label"]


def test_an_absolute_path_to_the_right_project_is_still_flagged(tmp_path):
    """Correct today, wrong for anyone who clones the repo. A `.mcp.json` is
    shared config; a machine path in it is a portability defect, not a win."""
    root = _project(tmp_path)
    info = _classify_mcp_entry(_bind(root, ["serve", "-p", root]), CFG)
    assert info["state"] == "project_absolute"
    assert "this machine" in info["issue"]


def test_a_project_file_proposes_the_portable_template(tmp_path):
    root = _project(tmp_path)
    info = _classify_mcp_entry(_bind(root, ["serve"]), CFG)
    assert info["proposed"] == {"command": "tokensave",
                                "args": ["serve", "-p", "."]}


def test_a_stray_mcp_json_is_not_judged_by_project_rules(tmp_path):
    """Filename alone must not confer project scope.

    Without the `.tokensave/` check, any `.mcp.json` anywhere would be told it
    is "unbound" and offered a binding for a project that does not exist.
    """
    stray = tmp_path / "not-a-project"
    stray.mkdir()
    path = str(stray / ".mcp.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"tokensave": {
            "command": r"C:\tools\tokensave.exe", "args": ["serve"]}}}, fh)
    info = _classify_mcp_entry(path, CFG)
    assert not info["state"].startswith("project_")
    # Falls through to the global rules, which is the conservative outcome.
    assert info["state"] in ("ok", "direct_serve", "wrong_wrapper")


# ── the template's own contract ───────────────────────────────────────────

def test_the_project_entry_contains_no_absolute_path():
    """The structural guarantee. If this ever fails, a machine-specific path is
    reaching a file other people check out."""
    entry = _canonical_project_entry({"python_exe": r"C:\Python\pythonw.exe",
                                      "tokensave_exe": r"D:\tools\tokensave.exe"})
    blob = " ".join([entry["command"], *entry["args"]])
    assert ":" not in blob and "\\" not in blob and "/" not in blob, blob


def test_the_project_entry_does_not_route_through_the_wrapper():
    """The wrapper reads the Desktop pin; a project binding must ignore it."""
    entry = _canonical_project_entry(CFG)
    assert "wrapper" not in " ".join([entry["command"], *entry["args"]]).lower()


def test_the_project_entry_is_the_documented_shape():
    assert _canonical_project_entry(CFG) == {
        "command": "tokensave", "args": ["serve", "-p", PROJECT_PATH_ARG]}


# ── path equivalence ──────────────────────────────────────────────────────

def test_dot_hops_and_trailing_separators_are_the_same_project(tmp_path):
    root = _project(tmp_path)
    assert _same_project(root, os.path.join(root, ".") + os.sep)
    assert _same_project(root, root + os.sep)


def test_distinct_directories_are_not_the_same_project(tmp_path):
    assert not _same_project(_project(tmp_path, "a"), _project(tmp_path, "b"))


def test_empty_paths_are_never_equal():
    """A missing `-p` value must not compare equal to a missing project root
    and quietly read as correctly bound."""
    assert not _same_project("", "")
