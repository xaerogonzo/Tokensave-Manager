"""tests/test_tokensave_rules.py — the Claude rules-file drift check and repair.

What is protected, in order of how quietly each could go wrong:

* **The comparison grants no equivalence upstream does not.** A CRLF file,
  a case change or collapsed whitespace is DRIFTED, because upstream's writer
  would rewrite it; saying CURRENT there is the failure this module exists to
  stop.
* **Nothing is written without proof it is still the previewed file** and
  still recognisably tokensave's, and no backup ever overwrites another.
* **Generation that touched real state is UNKNOWN**, whatever the file says.

The subprocess is always faked here, so no test can reach the real home.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from helpers import tokensave_rules as tr

BODY = (
    "## MANDATORY: No Explore Agents When Tokensave Is Available\n\n"
    "- Before ANY code research task, use `tokensave_context`, "
    "`tokensave_files`, `tokensave_read`, or `tokensave_affected`.\n\n"
    "## Prefer tokensave MCP tools\n\n"
    "Use `tokensave_files`, and `tokensave_affected`.\n\n"
    "To read a file's contents, use `tokensave_read`.\n"
)
OLD_BODY = BODY.replace("`tokensave_read`, ", "").replace(
    "\nTo read a file's contents, use `tokensave_read`.\n", "")


def _gen(text=BODY, state=tr.GEN_OK):
    raw = text.encode("utf-8")
    return tr.Generation(state, text=text, raw=raw,
                         diagnostic="" if state == tr.GEN_OK else "boom")


def _install(home, text):
    path = tr.installed_rules_path(str(home))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    return path


# ── generate_expected ────────────────────────────────────────────────────

class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _fake_exe(tmp_path):
    exe = tmp_path / "tokensave.exe"
    exe.write_bytes(b"")
    return str(exe)


def test_generation_is_isolated_and_names_what_it_wrote(tmp_path):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        rules = os.path.join(kw["cwd"], ".claude", "rules")
        os.makedirs(rules)
        with open(os.path.join(rules, "tokensave.md"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(BODY)
        with open(os.path.join(kw["cwd"], ".mcp.json"), "w") as fh:
            fh.write("{}")
        return _Proc()

    gen = tr.generate_expected(_fake_exe(tmp_path), _run=run)
    assert gen.state == tr.GEN_OK and gen.text == BODY
    assert seen["argv"][1:] == ["install", "--agent", "claude", "--local",
                                "--git-hook", "no"]
    env = seen["env"]
    assert env["HOME"] == env["USERPROFILE"]
    assert os.path.commonpath([env["HOME"], seen["cwd"]]) != ""
    assert "CLAUDE_CONFIG_DIR" not in env
    assert os.path.expanduser("~") not in (env["HOME"],)
    assert seen["stdin"] is subprocess.DEVNULL
    assert gen.written_files == ("proj/.claude/rules/tokensave.md",
                                 "proj/.mcp.json")


def test_generation_states_are_distinct(tmp_path):
    exe = _fake_exe(tmp_path)
    assert tr.generate_expected(str(tmp_path / "nope.exe")).state == \
        tr.GEN_EXE_MISSING
    assert tr.generate_expected(
        exe, _run=lambda a, **k: _Proc(rc=2, err="bad")).state == \
        tr.GEN_INSTALL_FAILED
    assert tr.generate_expected(exe, _run=lambda a, **k: _Proc()).state == \
        tr.GEN_RULES_MISSING

    def empty(argv, **kw):
        rules = os.path.join(kw["cwd"], ".claude", "rules")
        os.makedirs(rules)
        open(os.path.join(rules, "tokensave.md"), "w").close()
        return _Proc()
    assert tr.generate_expected(exe, _run=empty).state == tr.GEN_RULES_EMPTY

    def raises(argv, **kw):
        raise OSError("denied")
    assert tr.generate_expected(exe, _run=raises).state == \
        tr.GEN_INSTALL_FAILED


# ── semantic tool check ──────────────────────────────────────────────────

def test_missing_tools_reads_the_sections_not_the_file():
    assert tr.missing_tools(BODY) == {"Prefer tokensave MCP tools": [],
                                      "MANDATORY": []}
    old = tr.missing_tools(OLD_BODY)
    assert old["Prefer tokensave MCP tools"] == ["tokensave_read"]
    assert old["MANDATORY"] == ["tokensave_read"]
    # a mention outside both sections does not count
    stray = OLD_BODY + "\n## Other\n\nSee `tokensave_read`.\n"
    assert tr.missing_tools(stray)["Prefer tokensave MCP tools"] == \
        ["tokensave_read"]


# ── drift ────────────────────────────────────────────────────────────────

def test_current_when_only_trailing_whitespace_differs(tmp_path):
    path = _install(tmp_path, BODY + "\n\n")
    assert tr.drift(path, _gen()).state == tr.CURRENT


@pytest.mark.parametrize("variant", [
    OLD_BODY,
    BODY.replace("\n", "\r\n"),              # no newline translation
    BODY.replace("Prefer", "prefer", 1).replace(
        "## prefer", "## Prefer") + " ",     # sanity: still same headings
    BODY.replace("use `tokensave_context`", "USE `tokensave_context`"),
    BODY.replace("Use `tokensave_files`,", "Use  `tokensave_files`,"),
])
def test_no_equivalence_upstream_does_not_grant(tmp_path, variant):
    path = _install(tmp_path, variant)
    v = tr.drift(path, _gen())
    if variant.rstrip() == BODY.rstrip():
        assert v.state == tr.CURRENT
    else:
        assert v.state == tr.DRIFTED and v.diff


def test_unknown_when_generation_failed(tmp_path):
    path = _install(tmp_path, OLD_BODY)
    assert tr.drift(path, _gen(state=tr.GEN_INSTALL_FAILED)).state == tr.UNKNOWN


def test_unknown_when_absent(tmp_path):
    v = tr.drift(tr.installed_rules_path(str(tmp_path)), _gen())
    assert v.state == tr.UNKNOWN and "install" in v.reason


def test_unknown_when_not_recognisably_tokensaves(tmp_path):
    path = _install(tmp_path, "# My own notes\n\nnothing shared\n")
    v = tr.drift(path, _gen())
    assert v.state == tr.UNKNOWN and "not recognisably" in v.reason


def test_unknown_for_a_symlink(tmp_path):
    target = tmp_path / "real.md"
    target.write_text(OLD_BODY, encoding="utf-8")
    link = tr.installed_rules_path(str(tmp_path))
    os.makedirs(os.path.dirname(link))
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges here")
    assert tr.drift(link, _gen()).state == tr.UNKNOWN


# ── guard ────────────────────────────────────────────────────────────────

def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_guard_ignores_churn_outside_mcp_servers(tmp_path):
    home = str(tmp_path)
    cj = os.path.join(home, ".claude.json")
    _write_json(cj, {"numStartups": 1, "mcpServers": {"a": {}}})
    before = tr.snapshot_global_state(home)
    _write_json(cj, {"numStartups": 2, "mcpServers": {"a": {}}})
    assert tr.guard_changes(before, tr.snapshot_global_state(home)) == []
    _write_json(cj, {"numStartups": 2, "mcpServers": {"a": {}, "tokensave": {}}})
    assert tr.guard_changes(before, tr.snapshot_global_state(home)) == \
        ["~/.claude.json mcpServers"]


def test_guard_sees_installed_agents_and_new_rules_files(tmp_path):
    home = str(tmp_path)
    state = tr.state_toml_path(home)
    os.makedirs(os.path.dirname(state))
    with open(state, "w", encoding="utf-8") as fh:
        fh.write('pending_upload = 1\ninstalled_agents = ["copilot"]\n')
    before = tr.snapshot_global_state(home)
    with open(state, "w", encoding="utf-8") as fh:        # counters churn: fine
        fh.write('pending_upload = 9\ninstalled_agents = ["copilot"]\n')
    assert tr.guard_changes(before, tr.snapshot_global_state(home)) == []
    with open(state, "w", encoding="utf-8") as fh:
        fh.write('installed_agents = ["copilot", "claude"]\n')
    _install(tmp_path, BODY)
    changed = tr.guard_changes(before, tr.snapshot_global_state(home))
    assert "state.toml installed_agents" in changed
    assert "~/.claude/rules/tokensave.md" in changed


def test_evaluate_is_unknown_when_generation_changed_real_state(tmp_path):
    _install(tmp_path, OLD_BODY)
    calls = iter([{"k": "a"}, {"k": "b"}])
    report = tr.evaluate("x", home=str(tmp_path),
                         _generate=lambda exe: _gen(),
                         _snapshot=lambda home, git: next(calls))
    assert report.verdict.state == tr.UNKNOWN
    assert report.guard_changed == ("k",)


def test_claude_tracked_states_the_fact(tmp_path):
    state = tr.state_toml_path(str(tmp_path))
    assert tr.claude_tracked(state).value is None
    os.makedirs(os.path.dirname(state))
    with open(state, "w", encoding="utf-8") as fh:
        fh.write('installed_agents = ["copilot"]\n')
    t = tr.claude_tracked(state)
    assert t.value is False and t.fact == 'installed_agents = ["copilot"]'


# ── refresh ──────────────────────────────────────────────────────────────

def _evaluate_for(home):
    def ev(exe, *, home=home, git_exe=""):
        return tr.evaluate(exe, home=home, _generate=lambda e: _gen(),
                           _snapshot=lambda h, g: {})
    return ev


def test_refresh_replaces_with_a_backup(tmp_path):
    home = str(tmp_path)
    path = _install(tmp_path, OLD_BODY)
    sha = tr.sha256_bytes(OLD_BODY.encode())
    ok, msg = tr.refresh("x", sha, home=home, _evaluate=_evaluate_for(home),
                         _now=lambda: 1000.0)
    assert ok, msg
    with open(path, "rb") as fh:
        assert fh.read() == BODY.encode()
    with open(path + ".backup.1000000", "rb") as fh:
        assert fh.read() == OLD_BODY.encode()
    assert tr.missing_tools(BODY)["MANDATORY"] == []


def test_refresh_refuses_when_the_file_changed_since_preview(tmp_path):
    home = str(tmp_path)
    path = _install(tmp_path, OLD_BODY + "\nedited by hand\n")
    ok, msg = tr.refresh("x", tr.sha256_bytes(OLD_BODY.encode()), home=home,
                         _evaluate=_evaluate_for(home))
    assert not ok and "changed since preview" in msg
    with open(path, "rb") as fh:
        assert b"edited by hand" in fh.read()


def test_refresh_refuses_unless_drifted(tmp_path):
    home = str(tmp_path)
    _install(tmp_path, BODY)
    ok, msg = tr.refresh("x", tr.sha256_bytes(BODY.encode()), home=home,
                         _evaluate=_evaluate_for(home))
    assert not ok and "current" in msg


def test_backups_never_overwrite_each_other(tmp_path):
    home = str(tmp_path)
    path = _install(tmp_path, OLD_BODY)
    first = path + ".backup.1000000"
    with open(first, "wb") as fh:
        fh.write(b"an earlier backup")
    ok, _ = tr.refresh("x", tr.sha256_bytes(OLD_BODY.encode()), home=home,
                       _evaluate=_evaluate_for(home), _now=lambda: 1000.0)
    assert ok
    with open(first, "rb") as fh:
        assert fh.read() == b"an earlier backup"
    with open(first + "-1", "rb") as fh:
        assert fh.read() == OLD_BODY.encode()
