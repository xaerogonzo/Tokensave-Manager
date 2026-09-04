"""vscode_mcp_logs — what VS Code's own logs say about MCP servers.

VS Code writes one log per MCP server per window, and the **filename** names
the scope it loaded the entry from:

    mcpServer.mcp.config.usrlocal.<name>.log      user scope
    mcpServer.workspace-dot-mcp.<n>.<name>.log    workspace root .mcp.json

`workspace-dot-mcp` is VS Code's own word for the root `.mcp.json` surface.
Reading these answers "which scope is this server coming from" without
spawning anything, which is the whole point: the Manager should never have to
start a server to find out how it is wired.

**Three states, and the middle one is why this module is not a boolean.**
Measured on this machine across 14 log generations: 25 MCP logs, every one of
them **zero bytes** — including Pylance's and Azure MCP's, servers that
demonstrably work. VS Code creates the log slot when it *knows about* a
server and writes to it only when it actually *starts* one. So:

    CONFIGURED  log exists, empty      VS Code knew about it; not started here
    STARTED     log has content        it tried; the content says how it went
    (absent)    no log at all          says nothing -- see below

`docs/vscode-mcp-matrix.md` records the non-empty shape from 2026-08-26, so
both are real; this machine currently shows only the first.

**Absence is not evidence.** Only 3 of those 14 generations contain any MCP
log, and the newest contains none at all. A server missing from the latest
generation has not "stopped working" — nobody opened a window that used MCP.
Every report here therefore carries the generation it came from, and this
module will not convert a missing file into a verdict.

**It never says "shadowing".** A dead entry and a shadowing entry are
different failures with different fixes; conflating them is the mistake
`memory/desktop_mcp_scope_collision.md` exists to prevent.

Pure: no subprocess, no Tk. Read-only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: VS Code's own labels, quoted rather than translated. Its MCP Servers view
#: files a workspace `.mcp.json` server under "Built-In" while its logs call
#: the same source `workspace-dot-mcp`; neither word means what a user would
#: guess, so anything rendering this should show VS Code's word beside ours.
SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
SCOPE_EXTENSION = "extension"

VSCODE_LABEL = {
    SCOPE_USER:      "user scope (mcp.config.usrlocal)",
    SCOPE_WORKSPACE: 'workspace .mcp.json (VS Code calls it "Built-In"; '
                     "its logs call it workspace-dot-mcp)",
    SCOPE_EXTENSION: "contributed by an extension",
}

#: Log exists but is empty: VS Code knew about the server and did not start it
#: in that window. NOT a failure, and emphatically not "never worked".
STATE_CONFIGURED = "configured"
#: Log has content: it was started. `detail` carries what the log said.
STATE_STARTED = "started"

_USER_RE = re.compile(r"^mcpServer\.mcp\.config\.usrlocal\.(?P<name>.+)\.log$")
_WS_RE = re.compile(r"^mcpServer\.workspace-dot-mcp\.(?P<idx>\d+)\.(?P<name>.+)\.log$")
_EXT_RE = re.compile(r"^mcpServer\.(?P<name>.+)\.log$")

#: Lines VS Code writes that carry connection state, most decisive last.
_RUNNING_RE = re.compile(r"Connection state:\s*Running", re.I)
_ERROR_RE = re.compile(r"Connection state:\s*Error(?P<tail>.*)", re.I)


@dataclass(frozen=True)
class ServerLog:
    """One MCP server's log slot in one window of one log generation."""
    name: str
    scope: str
    state: str
    generation: str
    window: str
    path: str
    size: int = 0
    detail: str = ""

    @property
    def was_started(self) -> bool:
        return self.state == STATE_STARTED

    def describe(self) -> str:
        where = VSCODE_LABEL.get(self.scope, self.scope)
        if self.state == STATE_CONFIGURED:
            return (f"{self.name}: known to VS Code from {where}; not started "
                    f"in this window (log generation {self.generation})")
        return (f"{self.name}: started from {where} "
                f"(log generation {self.generation}) — {self.detail}")


@dataclass(frozen=True)
class LogScan:
    """What one pass over the log tree found, and how much it looked at.

    ``generations_scanned`` and ``logs_found`` are not decoration: "no
    servers seen" across 14 generations and across 0 are the same empty list
    and different claims, and the second one usually means the log root moved.
    """
    root: str
    generations_scanned: int = 0
    generations_with_logs: tuple = field(default_factory=tuple)
    newest_generation: str = ""
    logs_found: int = 0
    logs_with_content: int = 0
    servers: tuple = field(default_factory=tuple)
    unreadable: bool = False
    detail: str = ""

    @property
    def content_observable(self) -> bool:
        """False when every log was empty, so connection state is unknowable.

        Kept as its own property because the alternative is a caller reading
        "no errors found" out of a scan that could not have found one.
        """
        return self.logs_with_content > 0

    def for_server(self, name: str) -> tuple:
        return tuple(s for s in self.servers if s.name == name)

    def summary(self) -> str:
        if self.unreadable:
            return f"VS Code MCP logs unreadable — {self.detail}"
        if not self.generations_scanned:
            return f"no VS Code log directory at {self.root}"
        if not self.logs_found:
            return (f"no MCP server logs across {self.generations_scanned} "
                    f"log generation(s) — VS Code has not run one here")
        base = (f"{self.logs_found} MCP server log(s) across "
                f"{self.generations_scanned} generation(s)")
        if not self.content_observable:
            return (base + "; every one empty, so VS Code knew about these "
                           "servers but did not start them — connection "
                           "state is not observable")
        return base + f"; {self.logs_with_content} with content"


def default_log_root() -> str:
    """``%APPDATA%/Code/logs``. Returns the path whether or not it exists."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "Code", "logs")


def _classify(filename: str):
    """Return ``(scope, name)`` for an MCP log filename, else ``None``.

    Order matters: the extension pattern is a catch-all and would swallow the
    two specific ones.
    """
    m = _USER_RE.match(filename)
    if m:
        return SCOPE_USER, m.group("name")
    m = _WS_RE.match(filename)
    if m:
        return SCOPE_WORKSPACE, m.group("name")
    m = _EXT_RE.match(filename)
    if m:
        return SCOPE_EXTENSION, m.group("name")
    return None


def _read_state(path: str, size: int) -> tuple:
    """Return ``(state, detail)`` for one log file."""
    if size <= 0:
        return STATE_CONFIGURED, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(64_000)
    except OSError as exc:
        return STATE_CONFIGURED, f"unreadable ({exc})"
    err = None
    for match in _ERROR_RE.finditer(text):
        err = (match.group("tail") or "").strip()
    if err is not None:
        return STATE_STARTED, f"connection error: {err}" if err else "connection error"
    if _RUNNING_RE.search(text):
        return STATE_STARTED, "connection state: Running"
    return STATE_STARTED, "started; log carries no connection-state line"


def scan(root: str = "") -> LogScan:
    """Walk VS Code's log generations and report every MCP server log slot."""
    root = root or default_log_root()
    if not os.path.isdir(root):
        return LogScan(root=root, detail="log directory does not exist")

    try:
        generations = sorted(d for d in os.listdir(root)
                             if os.path.isdir(os.path.join(root, d)))
    except OSError as exc:
        return LogScan(root=root, unreadable=True, detail=str(exc))

    servers = []
    with_logs = []
    total = with_content = 0
    for gen in generations:
        gen_dir = os.path.join(root, gen)
        try:
            windows = sorted(w for w in os.listdir(gen_dir)
                             if os.path.isdir(os.path.join(gen_dir, w)))
        except OSError:
            continue
        for window in windows:
            win_dir = os.path.join(gen_dir, window)
            try:
                entries = sorted(os.listdir(win_dir))
            except OSError:
                continue
            for entry in entries:
                if not entry.startswith("mcpServer."):
                    continue
                got = _classify(entry)
                if got is None:
                    continue
                scope, name = got
                path = os.path.join(win_dir, entry)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                state, detail = _read_state(path, size)
                total += 1
                if size > 0:
                    with_content += 1
                if gen not in with_logs:
                    with_logs.append(gen)
                servers.append(ServerLog(
                    name=name, scope=scope, state=state, generation=gen,
                    window=window, path=path, size=size, detail=detail))

    return LogScan(
        root=root,
        generations_scanned=len(generations),
        generations_with_logs=tuple(with_logs),
        newest_generation=generations[-1] if generations else "",
        logs_found=total,
        logs_with_content=with_content,
        servers=tuple(servers),
    )


def scopes_for(report: LogScan, name: str) -> dict:
    """Newest observation of *name* per scope: ``{scope: ServerLog}``.

    Answers the one question these logs can answer well — which surfaces VS
    Code has loaded this server from — without touching the question they
    cannot, which is whether it currently works.
    """
    best: dict = {}
    for entry in report.for_server(name):
        prev = best.get(entry.scope)
        if prev is None or entry.generation > prev.generation:
            best[entry.scope] = entry
    return best


def describe_server(report: LogScan, name: str) -> list:
    """Human-readable, warn-only lines about one server. Never a verdict.

    Silent when nothing was seen: a server with no log has not been observed,
    and "not observed" is not "broken". Saying otherwise is how a month-old
    absence becomes a current warning.
    """
    seen = scopes_for(report, name)
    if not seen:
        return []
    lines = [entry.describe() for _scope, entry in sorted(seen.items())]
    if len(seen) > 1:
        lines.append(
            f"{name} is configured in more than one scope "
            f"({', '.join(sorted(seen))}). VS Code picks one; these logs do "
            f"not record which, so this is a thing to check, not a verdict.")
    if not report.content_observable:
        lines.append(
            "Every MCP log VS Code has written here is empty, so whether "
            "these servers connected cannot be read from them.")
    return lines
