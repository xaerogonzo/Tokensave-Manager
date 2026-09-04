"""tests/test_vscode_mcp_logs.py — reading VS Code's logs without over-reading them.

The backlog entry that asked for this (R12-3) said parsing the tail of these
files "gives connection state and stderr without spawning anything". Measured
before building: **25 MCP logs across 14 generations on this machine, every
one zero bytes** — including Pylance's and Azure MCP's, which work. The entry
was written from the existence of the files, not from reading them.

So the tests below pin three things the implementation must not get wrong:

* an **empty** log means VS Code knew about the server and did not start it —
  not that it failed, and emphatically not that it "has never worked";
* an **absent** log means nothing at all, because only 3 of 14 generations
  hold any MCP log and the newest holds none;
* the scan reports **how much it looked at**, so "nothing found" can be told
  apart from "nowhere to look".

The non-empty fixture is the real shape, transcribed from
`docs/vscode-mcp-matrix.md`, which captured it on 2026-08-26.
"""
from __future__ import annotations

import io
import os

from helpers.vscode_mcp_logs import (
    SCOPE_EXTENSION,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    STATE_CONFIGURED,
    STATE_STARTED,
    describe_server,
    scan,
    scopes_for,
)

# Real content, from the matrix's capture of the user-scope entry that
# R12-1 later removed.
CRASHED_LOG = """[info]    Starting server tokensave
[info]    Connection state: Running
[warning] [server stderr] Multiple tokensave projects found - pass -p <path>
[warning] [server stderr] Error: config error: no TokenSave index found
[info]    Connection state: Error Process exited with code 1
"""

RUNNING_LOG = """[info]    Starting server tokensave
[info]    Connection state: Running
"""


def _write(root, generation, window, filename, content=""):
    d = os.path.join(str(root), generation, window)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, filename)
    io.open(p, "w", encoding="utf-8", newline="\n").write(content)
    return p


# ── population ───────────────────────────────────────────────────────────

def test_a_missing_log_root_is_not_a_finding(tmp_path):
    r = scan(str(tmp_path / "nope"))
    assert r.generations_scanned == 0
    assert r.servers == ()
    assert "no VS Code log directory" in r.summary()
    assert "does not exist" in r.detail


def test_the_scan_reports_what_it_looked_at(tmp_path):
    """"Nothing found" and "nowhere to look" are different claims."""
    _write(tmp_path, "20260830T010000", "window1", "not-an-mcp-log.log", "x")
    r = scan(str(tmp_path))
    assert r.generations_scanned == 1
    assert r.logs_found == 0
    assert "1 log generation" in r.summary()


# ── the three states ─────────────────────────────────────────────────────

def test_an_empty_log_means_configured_not_broken(tmp_path):
    """The measured reality on this machine, and the easiest thing to misread."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log", "")
    r = scan(str(tmp_path))
    (entry,) = r.servers
    assert entry.state == STATE_CONFIGURED
    assert entry.scope == SCOPE_WORKSPACE
    assert entry.name == "tokensave"
    assert not entry.was_started
    assert r.content_observable is False
    # the words that must never appear
    text = " ".join(describe_server(r, "tokensave")).lower()
    assert "never worked" not in text
    assert "shadow" not in text


def test_a_started_log_reports_its_connection_state(tmp_path):
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.tokensave.log", RUNNING_LOG)
    r = scan(str(tmp_path))
    (entry,) = r.servers
    assert entry.state == STATE_STARTED
    assert entry.scope == SCOPE_USER
    assert "Running" in entry.detail
    assert r.content_observable is True


def test_an_error_after_running_wins(tmp_path):
    """`Running` then `Error` is a crash, not a success.

    The real capture has both lines in that order — VS Code reports the
    process as Running before it exits 1 — so a check that stops at the first
    `Running` calls a crashed server healthy.
    """
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.tokensave.log", CRASHED_LOG)
    r = scan(str(tmp_path))
    (entry,) = r.servers
    assert entry.state == STATE_STARTED
    assert "error" in entry.detail.lower()
    assert "exited with code 1" in entry.detail


# ── scope naming ─────────────────────────────────────────────────────────

def test_each_source_filename_maps_to_its_scope(tmp_path):
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.alpha.log")
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.beta.log")
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.ms-python.vscode-pylancepylance mcp server.log")
    got = {s.name: s.scope for s in scan(str(tmp_path)).servers}
    assert got == {"alpha": SCOPE_USER, "beta": SCOPE_WORKSPACE,
                   "ms-python.vscode-pylancepylance mcp server": SCOPE_EXTENSION}


def test_the_catch_all_does_not_swallow_the_specific_patterns(tmp_path):
    """Ordering guard: `mcpServer.<anything>.log` matches the user-scope name
    too, so a reordered classifier would file every user entry as an
    extension's and lose the scope question entirely."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.tokensave.log")
    (entry,) = scan(str(tmp_path)).servers
    assert entry.scope == SCOPE_USER
    assert entry.name == "tokensave"


def test_vs_codes_own_label_is_quoted_not_translated(tmp_path):
    """R12-6: the Manager and VS Code must not disagree on screen."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log")
    r = scan(str(tmp_path))
    text = " ".join(describe_server(r, "tokensave"))
    assert "Built-In" in text
    assert "workspace-dot-mcp" in text


# ── recency, and what absence does not mean ──────────────────────────────

def test_absence_from_the_newest_generation_is_not_a_finding(tmp_path):
    """Measured: only 3 of 14 generations here hold any MCP log, and the
    newest holds none. A server missing from the latest generation has not
    stopped working — nobody opened a window that used MCP."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log")
    os.makedirs(os.path.join(str(tmp_path), "20260830T999999", "window1"))
    r = scan(str(tmp_path))
    assert r.newest_generation == "20260830T999999"
    assert r.generations_with_logs == ("20260830T010000",)
    # still reported, and reported with the generation it came from
    lines = describe_server(r, "tokensave")
    assert lines and "20260830T010000" in lines[0]


def test_an_unseen_server_produces_no_lines(tmp_path):
    """"Not observed" is not "broken", so it gets no warning at all."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log")
    assert describe_server(scan(str(tmp_path)), "codegraph") == []


def test_the_newest_observation_per_scope_wins(tmp_path):
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log", "")
    _write(tmp_path, "20260830T020000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log", RUNNING_LOG)
    got = scopes_for(scan(str(tmp_path)), "tokensave")
    assert got[SCOPE_WORKSPACE].generation == "20260830T020000"
    assert got[SCOPE_WORKSPACE].state == STATE_STARTED


def test_two_scopes_is_reported_as_a_question_not_a_verdict(tmp_path):
    """These logs do not record which scope VS Code picked, so the module
    must not pretend to know. Saying "shadowing" here would repeat the
    mistake `memory/desktop_mcp_scope_collision.md` exists to prevent."""
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.tokensave.log")
    _write(tmp_path, "20260830T010000", "window2",
           "mcpServer.workspace-dot-mcp.0.tokensave.log")
    lines = describe_server(scan(str(tmp_path)), "tokensave")
    joined = " ".join(lines)
    assert "more than one scope" in joined
    assert "not a verdict" in joined
    assert "shadow" not in joined.lower()


# ── the CLI layer ────────────────────────────────────────────────────────

def test_mcp_status_carries_the_vscode_layer(capsys, tmp_path, mocker):
    """Detect-only means it appears in the payload and changes no verdict."""
    import cli
    from helpers import vscode_mcp_logs as vsl
    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.workspace-dot-mcp.0.tokensave.log")
    mocker.patch.object(vsl, "default_log_root", return_value=str(tmp_path))

    cli.main(["mcp-status", "--project", str(tmp_path)])
    out, _ = capsys.readouterr()
    import json as _json
    layer = _json.loads(out)["data"]["layers"]["vscode_logs"]
    assert layer["logs_found"] == 1
    assert layer["content_observable"] is False
    assert "workspace" in layer["scopes_seen"]


def test_the_vscode_layer_does_not_decide_binding(capsys, tmp_path, mocker):
    """The exit code must come from the project config alone.

    VS Code's logs are historical and machine-local; letting them move a
    binding verdict would make the same repository report differently on two
    machines for reasons that have nothing to do with the repository.
    """
    import cli
    from helpers import vscode_mcp_logs as vsl
    mocker.patch.object(vsl, "default_log_root", return_value=str(tmp_path))
    code_without = cli.main(["mcp-status", "--project", str(tmp_path)])
    capsys.readouterr()

    _write(tmp_path, "20260830T010000", "window1",
           "mcpServer.mcp.config.usrlocal.tokensave.log", CRASHED_LOG)
    code_with = cli.main(["mcp-status", "--project", str(tmp_path)])
    out, _ = capsys.readouterr()
    import json as _json
    layer = _json.loads(out)["data"]["layers"]["vscode_logs"]

    assert layer["logs_found"] == 1            # it did see the crash
    assert code_with == code_without           # and it changed nothing
