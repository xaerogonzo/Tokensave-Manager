"""helpers/tokensave_rules.py — is ~/.claude/rules/tokensave.md what THIS tokensave writes?

WHY THIS EXISTS. tokensave 7.12.1 shipped #540 (the rules file now names
``tokensave_read``) and the file on this machine stayed at its Aug 30 text.
tokensave refreshes that file on upgrade only by re-running ``install`` for the
agents in ``~/.tokensave/state.toml`` ``installed_agents``; the resync never
re-detects agents, and Claude is detected only through a user-scope
``mcpServers.tokensave`` entry -- the entry the Manager retires. So on a
machine with project-scoped MCP only, no rules-text fix ever arrives. This
module repairs the LOCAL ARTIFACT; the upstream gap remains
(docs/upstream-issues/tokensave-resync-skips-claude-rules.md).

HOW THE EXPECTED TEXT IS OBTAINED. Not reconstructed from upstream source --
the binary is asked. ``install --agent claude --local --git-hook no`` runs in a
temp project with ``HOME``/``USERPROFILE`` pointed at a temp home. Upstream's
global and local installs write the rules body through the same call
(``write_managed_rules_file(dir/rules/tokensave.md, rules_for_agent("claude"))``),
so the generated file is the artifact a real install would write.

WHAT CANNOT BE ISOLATED, SO IT IS MEASURED. tokensave reads HOME/USERPROFILE
for agent config, but finds ``state.toml`` through ``dirs::home_dir()``, which
on Windows asks the Known Folder API and ignores the environment; and it saves
that file at startup. So a guard snapshots the real candidates before and
after generation and any difference makes the result UNKNOWN. ``~/.claude.json``
is compared on its ``mcpServers`` subtree only -- Claude Code rewrites the rest
of that file constantly, so bytes would false-positive on every run.

THE COMPARISON IS UPSTREAM'S, NOTHING MORE. ``write_managed_rules_file`` skips
the write when ``existing.trim_end() == body.trim_end()``. This reads raw
bytes, decodes UTF-8 with no BOM strip and no newline translation, and
compares ``rstrip()`` of both. No case folding, no whitespace collapsing: an
equivalence upstream does not grant would let this say CURRENT about a file
upstream would rewrite.

OWNERSHIP. The Claude rules file carries no ownership marker upstream (the
whole file is tokensave's by path). So "recognisably tokensave's" is a
deliberate compatibility heuristic: not a symlink, and sharing at least one
``## `` heading with the body the installed binary generates now. Anything
else is UNKNOWN and nothing is written.
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time

from constants import CREATE_NO_WINDOW

# Generation states
GEN_OK = "ok"
GEN_EXE_MISSING = "exe_missing"
GEN_INSTALL_FAILED = "install_failed"
GEN_RULES_MISSING = "rules_missing"
GEN_RULES_UNREADABLE = "rules_unreadable"
GEN_RULES_EMPTY = "rules_empty"

# Verdicts
CURRENT = "current"
DRIFTED = "drifted"
UNKNOWN = "unknown"

#: The tools #540 is about, plus two neighbours that must survive any rewrite.
REQUIRED_TOOLS = ("tokensave_read", "tokensave_files", "tokensave_affected")

_TOOL_RE = re.compile(r"`(tokensave_[a-z_]+)`")
_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)


def installed_rules_path(home: "str | None" = None) -> str:
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "rules", "tokensave.md")


def state_toml_path(home: "str | None" = None) -> str:
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".tokensave", "state.toml")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Generation ───────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Generation:
    state: str
    text: str = ""
    raw: bytes = b""
    diagnostic: str = ""
    #: Every file the isolated install wrote, relative to the temp root.
    written_files: tuple = ()


def generate_expected(ts_exe: str, *, timeout: float = 60,
                      _run=subprocess.run) -> Generation:
    """Ask the installed binary what it would write for Claude."""
    if not ts_exe or not os.path.isfile(ts_exe):
        return Generation(GEN_EXE_MISSING,
                          diagnostic=f"tokensave executable not found: {ts_exe!r}")
    with tempfile.TemporaryDirectory(prefix="tsm-rules-") as tmp:
        home = os.path.join(tmp, "home")
        proj = os.path.join(tmp, "proj")
        os.makedirs(home)
        os.makedirs(proj)
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["NO_COLOR"] = "1"
        argv = [ts_exe, "install", "--agent", "claude", "--local",
                "--git-hook", "no"]
        try:
            proc = _run(argv, cwd=proj, env=env, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout,
                        creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            return Generation(GEN_INSTALL_FAILED,
                              diagnostic=f"could not run tokensave install ({exc})")
        written = _walk(tmp)
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
            return Generation(GEN_INSTALL_FAILED, written_files=written,
                              diagnostic=f"install exited {proc.returncode}: {tail}")
        rules = os.path.join(proj, ".claude", "rules", "tokensave.md")
        if not os.path.isfile(rules):
            return Generation(GEN_RULES_MISSING, written_files=written,
                              diagnostic="install succeeded but wrote no "
                                         ".claude/rules/tokensave.md")
        try:
            with open(rules, "rb") as fh:
                raw = fh.read()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return Generation(GEN_RULES_UNREADABLE, written_files=written,
                              diagnostic=f"generated rules unreadable ({exc})")
        if not text.strip():
            return Generation(GEN_RULES_EMPTY, written_files=written,
                              diagnostic="generated rules file is empty")
        return Generation(GEN_OK, text=text, raw=raw, written_files=written)


def _walk(root: str) -> tuple:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace("\\", "/"))
    return tuple(sorted(out))


# ── Semantic checks on a rules body ──────────────────────────────────────

def section(text: str, heading_prefix: str) -> str:
    """The body under the first ``## `` heading starting with *heading_prefix*."""
    lines = text.replace("\r\n", "\n").split("\n")
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].startswith(heading_prefix)
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def missing_tools(text: str) -> dict:
    """``{section: [missing tool names]}`` for the two tool-routing sections.

    Parsed from backticked names inside each section rather than counted
    anywhere in the file, so two incidental mentions in prose cannot pass.
    """
    result = {}
    for heading in ("Prefer tokensave MCP tools", "MANDATORY"):
        names = set(_TOOL_RE.findall(section(text, heading)))
        result[heading] = [t for t in REQUIRED_TOOLS if t not in names]
    return result


# ── Global-state guard ───────────────────────────────────────────────────

def _json_subtree(path: str, *keys: str) -> str:
    if not os.path.exists(path):
        return "<absent>"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"<unreadable: {exc}>"
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return json.dumps(data, sort_keys=True)


def _file_sha(path: str) -> str:
    if not os.path.exists(path):
        return "<absent>"
    try:
        with open(path, "rb") as fh:
            return sha256_bytes(fh.read())
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _state_value(path: str, key: str) -> str:
    if not os.path.exists(path):
        return "<absent>"
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        return f"<unreadable: {exc}>"
    return json.dumps(data.get(key), sort_keys=True)


def _git_hooks_path(git_exe: str, _run=subprocess.run) -> str:
    if not git_exe:
        return "<not checked: no git executable configured>"
    try:
        proc = _run([git_exe, "config", "--global", "--get", "core.hooksPath"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<not checked: {exc}>"
    if proc.returncode == 1:
        return "<unset>"
    return (proc.stdout or "").strip()


def snapshot_global_state(home: "str | None" = None, git_exe: str = "",
                          *, _run=subprocess.run) -> dict:
    """The non-redirectable half of the mutation boundary, as comparable values."""
    home = home or os.path.expanduser("~")
    claude = os.path.join(home, ".claude")
    snap = {
        "~/.claude.json mcpServers":
            _json_subtree(os.path.join(home, ".claude.json"), "mcpServers"),
        "~/.claude/settings.json hooks":
            _json_subtree(os.path.join(claude, "settings.json"), "hooks"),
        "~/.claude/settings.json permissions":
            _json_subtree(os.path.join(claude, "settings.json"), "permissions"),
        "~/.claude/CLAUDE.md": _file_sha(os.path.join(claude, "CLAUDE.md")),
        "~/.tokensave/config.toml":
            _file_sha(os.path.join(home, ".tokensave", "config.toml")),
        "state.toml installed_agents":
            _state_value(state_toml_path(home), "installed_agents"),
        "state.toml wildcard_permissions":
            _state_value(state_toml_path(home), "wildcard_permissions"),
        "git --global core.hooksPath": _git_hooks_path(git_exe, _run),
    }
    rules_dir = os.path.join(claude, "rules")
    for rel in (_walk(rules_dir) if os.path.isdir(rules_dir) else ()):
        snap[f"~/.claude/rules/{rel}"] = _file_sha(os.path.join(rules_dir, rel))
    return snap


def guard_changes(before: dict, after: dict) -> list:
    """Keys whose value differs, including keys that appeared or vanished."""
    return sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k))


# ── Tracking and drift ───────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Tracked:
    value: "bool | None"
    fact: str


def claude_tracked(state_toml: str) -> Tracked:
    """Whether tokensave's upgrade resync will ever refresh the Claude file."""
    raw = _state_value(state_toml, "installed_agents")
    if raw.startswith("<"):
        return Tracked(None, f"state.toml {raw}")
    agents = json.loads(raw)
    if not isinstance(agents, list):
        return Tracked(None, f"installed_agents is not a list ({raw})")
    return Tracked("claude" in agents, f"installed_agents = {raw}")


@dataclasses.dataclass(frozen=True)
class Verdict:
    state: str
    reason: str = ""
    installed_sha: str = ""
    expected_sha: str = ""
    diff: str = ""


def drift(installed_path: str, gen: Generation) -> Verdict:
    if gen.state != GEN_OK:
        return Verdict(UNKNOWN, f"could not generate the expected rules: "
                                f"{gen.diagnostic}")
    expected_sha = sha256_bytes(gen.raw)
    if os.path.islink(installed_path):
        return Verdict(UNKNOWN, "the installed rules file is a symlink; "
                                "refusing to judge or replace it",
                       expected_sha=expected_sha)
    if not os.path.exists(installed_path):
        return Verdict(UNKNOWN, "no installed rules file -- creating one is "
                                "`tokensave install`'s decision, not a repair",
                       expected_sha=expected_sha)
    try:
        with open(installed_path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Verdict(UNKNOWN, f"installed rules unreadable ({exc})",
                       expected_sha=expected_sha)
    installed_sha = sha256_bytes(raw)
    headings = set(_HEADING_RE.findall(gen.text.replace("\r\n", "\n")))
    mine = set(_HEADING_RE.findall(text.replace("\r\n", "\n")))
    if not headings & mine:
        return Verdict(UNKNOWN, "the installed file shares no heading with "
                                "what tokensave generates, so it is not "
                                "recognisably tokensave's; refusing to replace",
                       installed_sha, expected_sha)
    if text.rstrip() == gen.text.rstrip():
        return Verdict(CURRENT, "", installed_sha, expected_sha)
    diff = "".join(difflib.unified_diff(
        text.replace("\r\n", "\n").splitlines(keepends=True),
        gen.text.replace("\r\n", "\n").splitlines(keepends=True),
        fromfile="installed", tofile="tokensave would write"))
    if not diff:
        # The display diff normalises line endings to stay readable; the
        # verdict does not. Say what differs rather than showing nothing.
        diff = ("(line endings differ -- installed "
                f"{'CRLF' if chr(13) in text else 'LF'}, tokensave writes "
                f"{'CRLF' if chr(13) in gen.text else 'LF'}; text otherwise "
                "identical)\n")
    return Verdict(DRIFTED, "differs from what the installed tokensave writes",
                   installed_sha, expected_sha, diff)


# ── Evaluation and refresh ───────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class RulesReport:
    generation: Generation
    verdict: Verdict
    tracked: Tracked
    guard_changed: tuple
    #: Required tools absent from the GENERATED body -- should always be empty.
    missing: dict
    #: Required tools absent from the INSTALLED file -- the user-facing gap.
    installed_missing: dict


def evaluate(ts_exe: str, *, home: "str | None" = None, git_exe: str = "",
             _generate=generate_expected, _snapshot=snapshot_global_state
             ) -> RulesReport:
    """Generate, guard, and judge -- read-only apart from the temp dir."""
    home = home or os.path.expanduser("~")
    before = _snapshot(home, git_exe)
    gen = _generate(ts_exe)
    changed = tuple(guard_changes(before, _snapshot(home, git_exe)))
    verdict = drift(installed_rules_path(home), gen)
    if changed:
        verdict = Verdict(UNKNOWN, "generating the expected rules changed real "
                                   "state: " + ", ".join(changed),
                          verdict.installed_sha, verdict.expected_sha)
    missing = missing_tools(gen.text) if gen.state == GEN_OK else {}
    return RulesReport(gen, verdict, claude_tracked(state_toml_path(home)),
                       changed, missing,
                       _installed_missing(installed_rules_path(home)))


def _installed_missing(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return missing_tools(fh.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def _exclusive_backup(path: str, data: bytes, now_ms: int) -> str:
    """Write a backup that never overwrites an earlier one."""
    base = f"{path}.backup.{now_ms}"
    for n in range(1000):
        cand = base if n == 0 else f"{base}-{n}"
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return cand
    raise FileExistsError(f"no free backup name for {path}")


def refresh(ts_exe: str, preview_sha: str, *, home: "str | None" = None,
            git_exe: str = "", _evaluate=evaluate,
            _now=time.time) -> "tuple[bool, str]":
    """Replace the installed rules with the generated ones. ``(ok, message)``.

    Re-reads and re-evaluates first: if the installed file is no longer the
    one the user previewed (by sha), or the verdict is not DRIFTED, nothing
    is written.
    """
    home = home or os.path.expanduser("~")
    report = _evaluate(ts_exe, home=home, git_exe=git_exe)
    v = report.verdict
    if v.state != DRIFTED:
        return (False, f"not replaced: verdict is {v.state}"
                       + (f" ({v.reason})" if v.reason else ""))
    if v.installed_sha != preview_sha:
        return (False, "not replaced: state changed since preview (installed "
                       f"sha {v.installed_sha[:12]} != previewed "
                       f"{preview_sha[:12]})")
    path = installed_rules_path(home)
    with open(path, "rb") as fh:
        current = fh.read()
    if sha256_bytes(current) != preview_sha:
        return (False, "not replaced: state changed since preview")
    backup = _exclusive_backup(path, current, int(_now() * 1000))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(report.generation.raw)
    os.replace(tmp, path)
    return (True, f"replaced {path} (backup: {os.path.basename(backup)})")
