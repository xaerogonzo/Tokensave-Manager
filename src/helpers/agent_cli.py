"""Agent-CLI registry — one capability table, many coding-agent CLIs.

The Manager shells out to a coding-agent CLI in a dozen places (commit-message
suggestion, Draft PR, the Ask tab, the pre-commit review hook, test generation,
the integration audit). Every one of those used to name Claude Code directly.

Adding a second agent by copying that branch would have doubled the call sites
and doubled them again for a third, so the differences live in a table instead.
**The fields describe capabilities, not vendors** — `system_prompt_mode`,
`prompt_transport` — which is what keeps "a new agent is one row" true. A field
named `is_cursor` would not.

`helpers/claude_cli.py` is a thin compatibility shim over this module: it keeps
its five public names and their exact signatures so existing call sites and
their tests are unchanged.

Three runners, deliberately not derived from one another
--------------------------------------------------------
``call_print`` captures output; ``spawn`` opens a terminal with an instruction;
``spawn_interactive`` opens one with **no trailing prompt argument**. That last
distinction is load-bearing, not stylistic: appending an empty instruction hands
the CLI a blank positional argument, which it treats as an empty one-shot prompt
instead of entering interactive mode. Collapsing these into one runner
reintroduces that bug.

Command-line budgets (why ``build_*`` can fail before spawning)
---------------------------------------------------------------
An agent whose ``prompt_transport`` is ``argv`` carries the whole prompt on the
command line, and this Manager's prompts are not small — code review, test
generation and PR drafts are routinely multi-KB. Claude uses stdin precisely to
dodge Windows argv mangling, so an argv agent needs a guard rather than a hope.

**The budget belongs to the runner, not the agent**, because the two routes
reach the OS differently:

    call_print          argv list -> CreateProcess     ~32767 chars
    spawn / interactive cmd.exe /k "..."               ~8191 chars

The check measures the *rendered* command line (``subprocess.list2cmdline`` for
the argv route, the assembled string for the cmd.exe route) rather than
estimating from the prompt length, so quoting expansion is counted rather than
guessed. Over budget returns a distinct error. It never truncates, never
silently drops the prompt, and never pretends stdin is available.

Enforced on Windows only; POSIX ``ARG_MAX`` is megabytes and a limit there would
be inventing a failure the platform does not have.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import threading

from constants import CREATE_NEW_CONSOLE, CREATE_NO_WINDOW
from helpers.runtime import log

# ── Capability vocabulary ────────────────────────────────────────────────────

#: The CLI has a flag that takes a system prompt (Claude: --append-system-prompt).
SYSTEM_PROMPT_NATIVE = "native"
#: The CLI has no such flag; the system prompt is folded into the user prompt.
SYSTEM_PROMPT_PREPEND = "prepend"

#: The prompt is piped to the process's stdin (immune to argv mangling).
TRANSPORT_STDIN = "stdin"
#: The prompt is a positional argument (subject to the budgets above).
TRANSPORT_ARGV = "argv"

INTERACTIVE_SUPPORTED = "supported"
INTERACTIVE_UNSUPPORTED = "unsupported"

#: Exact serialization for SYSTEM_PROMPT_PREPEND. Frozen as a constant because
#: two agents drifting into two different prompt structures is invisible until
#: their output quality diverges and nobody can say why.
PREPEND_TEMPLATE = "[System instructions]\n{system}\n\n[User request]\n{user}"

#: Windows command-line ceilings. CreateProcess accepts ~32767 UTF-16 units;
#: cmd.exe truncates its own line at ~8191. The margin absorbs the environment
#: block and any quoting the runner adds after measurement.
CREATEPROCESS_BUDGET = 32767
CMDLINE_BUDGET = 8191
BUDGET_MARGIN = 256


# ── The spec ─────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class AgentCLISpec:
    """One coding-agent CLI, described by what it supports.

    ``binaries`` is probed against PATH; ``dir_binaries`` is probed inside
    ``extra_dirs`` and falls back to ``binaries`` when empty. They are separate
    because Cursor installs its binary under both ``cursor-agent`` and the
    strikingly generic ``agent``. Probing a bare ``agent`` against PATH would
    happily bind to any unrelated tool of that name, so the generic alias is
    only trusted inside Cursor's own install directory.
    """

    id: str
    label: str
    binaries: tuple
    extra_dirs: tuple
    print_args: tuple
    model_flag: str
    system_prompt_mode: str
    system_flag: str
    prompt_transport: str
    interactive: str
    config_key_exe: str
    config_key_model: str
    default_model: str
    dir_binaries: tuple = ()

    def probe_names_for_dirs(self) -> tuple:
        """Binary names to look for inside ``extra_dirs``."""
        return self.dir_binaries or self.binaries


CLAUDE = AgentCLISpec(
    id="claude",
    label="Claude Code CLI",
    # npm installs `claude` as a .cmd shim on Windows; subprocess needs the
    # full filename including the extension, so .cmd is probed first.
    binaries=("claude.cmd", "claude"),
    extra_dirs=(r"%APPDATA%\npm", r"%USERPROFILE%\AppData\Roaming\npm"),
    print_args=("--print",),
    model_flag="--model",
    system_prompt_mode=SYSTEM_PROMPT_NATIVE,
    system_flag="--append-system-prompt",
    prompt_transport=TRANSPORT_STDIN,
    interactive=INTERACTIVE_SUPPORTED,
    config_key_exe="claude_cli_exe",
    config_key_model="claude_cli_model",
    # The Manager's automated calls need to be fast; the user's own default
    # (from ~/.claude/settings.json) would often be Opus.
    default_model="claude-haiku-4-5-20251001",
)

CURSOR = AgentCLISpec(
    id="cursor",
    label="Cursor Agent CLI",
    binaries=("cursor-agent.cmd", "cursor-agent.exe", "cursor-agent"),
    # The Windows installer (`irm 'https://cursor.com/install?win32=true'`)
    # writes to ~/.local/bin, NOT ~/.cursor/bin.
    extra_dirs=(r"%USERPROFILE%\.local\bin", "~/.local/bin"),
    dir_binaries=("cursor-agent.cmd", "cursor-agent.exe", "cursor-agent",
                  "agent.cmd", "agent.exe", "agent"),
    print_args=("-p", "--output-format", "text"),
    model_flag="--model",
    # Cursor has no --append-system-prompt equivalent.
    system_prompt_mode=SYSTEM_PROMPT_PREPEND,
    system_flag="",
    # Cursor's docs describe the prompt as a positional argument and say
    # nothing about stdin. Assumed argv until measured against a live install;
    # flip to TRANSPORT_STDIN once `echo hi | cursor-agent -p` is confirmed.
    prompt_transport=TRANSPORT_ARGV,
    interactive=INTERACTIVE_SUPPORTED,
    config_key_exe="cursor_cli_exe",
    config_key_model="cursor_cli_model",
    # Empty: let Cursor pick. Claude's Haiku id is not a valid Cursor model.
    default_model="",
)

AGENT_CLIS: dict = {CLAUDE.id: CLAUDE, CURSOR.id: CURSOR}

#: The agent assumed when `agent_cli` is absent from config — the value every
#: pre-Cursor configuration implicitly had.
DEFAULT_AGENT_ID = CLAUDE.id


def get_spec(agent_id: str) -> "AgentCLISpec | None":
    """Look up a spec by id. Returns None for an unknown id — never a default.

    Falling back to Claude here would run one agent while the UI named another.
    """
    return AGENT_CLIS.get((agent_id or "").strip().lower())


# Per-thread record of WHY the most recent call_print returned None
# (timeout / missing-or-unrunnable binary / non-zero exit / over budget).
# Mirrors helpers.llm._tls so callers can surface the real cause instead of
# guessing. Must be read on the same thread that made the call.
_tls = threading.local()


def get_last_cli_error() -> "str | None":
    """The specific failure from the most recent call_print on THIS thread."""
    return getattr(_tls, "last_error", None)


# ── Detection ────────────────────────────────────────────────────────────────

def _expand_dir(raw: str) -> str:
    """Expand %VARS% and ~ at CALL time.

    Never at module scope: tests redirect $USERPROFILE / $APPDATA via the
    `fake_home` fixture, and a module-level constant would capture the
    developer's real home before the fixture runs. Enforced project-wide by
    tests/test_no_import_time_path_resolution.py (G-L).
    """
    return os.path.expanduser(os.path.expandvars(raw))


def detect(spec: AgentCLISpec) -> str:
    """Absolute path to the agent's binary, or "" when not found.

    Empty string rather than the bare command name, so callers can test
    `if exe:` without accidentally spawning an unresolved name.
    """
    for name in spec.binaries:
        found = shutil.which(name)
        if found:
            return found
    for raw_dir in spec.extra_dirs:
        directory = _expand_dir(raw_dir)
        if not directory:
            continue
        for name in spec.probe_names_for_dirs():
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


# ── Prompt composition (Contract B) ──────────────────────────────────────────

def compose_prompt(system_prompt: str, user_prompt: str) -> str:
    """Fold a system prompt into a user prompt for SYSTEM_PROMPT_PREPEND agents.

    Three cases, so that an agent without a system-prompt flag still receives a
    clearly delimited instruction block:

      * no system prompt  -> the user prompt verbatim (no wrapper at all, so a
        plain call is byte-identical to what a native-flag agent would send)
      * no user prompt    -> the system block alone; an empty "[User request]"
        section would read as a request the caller never made
      * both              -> PREPEND_TEMPLATE

    Callers must NOT invoke this for SYSTEM_PROMPT_NATIVE agents — those pass
    the system prompt through their own flag, and prepending as well would
    duplicate it.
    """
    system = (system_prompt or "").strip()
    user = (user_prompt or "").strip()
    if not system:
        return user
    if not user:
        return f"[System instructions]\n{system}"
    return PREPEND_TEMPLATE.format(system=system, user=user)


# ── Command-line budgets (Contract A) ────────────────────────────────────────

def _over_budget(rendered: str, budget: int) -> str:
    """Return an error string when *rendered* cannot be spawned, else "".

    Windows-only: POSIX ARG_MAX is megabytes, and enforcing a ceiling there
    would invent a failure the platform does not have.
    """
    if sys.platform != "win32":
        return ""
    limit = budget - BUDGET_MARGIN
    size = len(rendered)
    if size <= limit:
        return ""
    return (f"prompt too large for positional CLI input "
            f"({size} chars, budget {limit})")


# ── argv / command-line construction (pure, no subprocess) ───────────────────

def build_print_argv(
    spec: AgentCLISpec,
    exe: str,
    prompt: str,
    system_prompt: str = "",
    model: str = "",
) -> tuple:
    """Assemble the one-shot print invocation.

    Returns ``(argv, stdin_text, err)``. ``stdin_text`` is None when the agent
    carries its prompt in argv. A non-empty ``err`` means do not spawn.
    """
    if not exe:
        return [], None, f"{spec.label} is not configured."

    argv = [exe, *spec.print_args]
    if model:
        argv += [spec.model_flag, model]

    if spec.system_prompt_mode == SYSTEM_PROMPT_NATIVE:
        if system_prompt:
            argv += [spec.system_flag, system_prompt]
        body = prompt or ""
    else:
        body = compose_prompt(system_prompt, prompt)

    if spec.prompt_transport == TRANSPORT_STDIN:
        stdin_text = body
    else:
        argv.append(body)
        stdin_text = None

    err = _over_budget(subprocess.list2cmdline(argv), CREATEPROCESS_BUDGET)
    if err:
        return [], None, err
    return argv, stdin_text, ""


def _quote_win(value: str) -> str:
    """cmd.exe-safe wrapping for one segment of the ""outer"" construction."""
    return value.replace('"', '')


def build_spawn_cmdline(
    spec: AgentCLISpec,
    exe: str,
    instruction: str = "",
    model: str = "",
) -> tuple:
    """Assemble the detached-terminal invocation.

    Returns ``(payload, err)``. ``payload`` is a **string** on Windows (a raw
    cmd.exe command line) and a **list** elsewhere — the same split the previous
    Claude-only helper made, and for the same reason: when both the exe path and
    the instruction contain spaces, Python's list-to-cmdline conversion makes
    cmd.exe strip the outermost quotes of the compound expression. The canonical
    ``""outer""`` double-double-quote wrapper is the documented workaround, and
    it requires a raw string rather than a list.

    An empty *instruction* produces the interactive form, which appends **no**
    trailing prompt argument at all.
    """
    if not exe:
        return None, f"{spec.label} is not configured."

    # A stray newline inside `cmd.exe /k` reads as pressing Enter, dropping the
    # CLI into an empty shell before the prompt ever lands.
    instruction = (instruction or "").replace("\r", " ").replace("\n", " ").strip()

    if sys.platform != "win32":
        argv = [exe]
        if model:
            argv += [spec.model_flag, model]
        if instruction:
            argv.append(instruction)
        return argv, ""

    model_flag = f' {spec.model_flag} "{_quote_win(model)}"' if model else ""
    if instruction:
        cmd_str = f'cmd.exe /k ""{exe}"{model_flag} "{_quote_win(instruction)}""'
    else:
        cmd_str = f'cmd.exe /k ""{exe}"{model_flag}"'

    err = _over_budget(cmd_str, CMDLINE_BUDGET)
    if err:
        return None, err
    return cmd_str, ""


# ── Runners ──────────────────────────────────────────────────────────────────

def call_print(
    spec: AgentCLISpec,
    exe: str,
    prompt: str,
    system_prompt: str = "",
    timeout: int = 45,
    model: str = "",
    cwd: "str | None" = None,
) -> "str | None":
    """Run the agent non-interactively and return its stdout text.

    Returns the stripped stdout, or None on timeout / error / empty output.
    The reason is recorded in this thread's `get_last_cli_error()`.

    ``cwd`` matters more than it looks: a coding-agent CLI loads CLAUDE.md /
    AGENTS.md from its working directory, which pushes it into agentic mode.
    Pass a neutral directory for tasks where project context derails the model
    (commit-message generation) and the project path for tasks that benefit
    from it (code review).
    """
    _tls.last_error = None
    argv, stdin_text, err = build_print_argv(
        spec, exe, prompt, system_prompt=system_prompt, model=model)
    if err:
        _tls.last_error = err
        return None

    try:
        proc = subprocess.run(
            argv,
            input=stdin_text if stdin_text is not None else None,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        _tls.last_error = f"timed out after {timeout}s"
        return None
    except OSError as e:
        _tls.last_error = f"executable not found or not runnable: {e}"
        return None

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        _tls.last_error = (f"exited {proc.returncode}: {stderr[:400]}" if stderr
                           else f"exited {proc.returncode} (no stderr)")
        # Through the manager's logger, not sys.stderr — always captured even
        # under pythonw.exe / windowed Nuitka builds where sys.stderr is None.
        log.warning("%s --print exited %d: %s",
                    spec.id, proc.returncode, stderr[:400])
        return None

    out = (proc.stdout or "").strip()
    if not out:
        _tls.last_error = "returned empty output"
        return None
    return out


def _spawn(spec, exe, project_path, instruction, model) -> tuple:
    """Shared body of spawn / spawn_interactive. Returns (ok, error_message)."""
    payload, err = build_spawn_cmdline(spec, exe, instruction, model)
    if err:
        return False, err
    try:
        if sys.platform == "win32":
            subprocess.Popen(payload, cwd=project_path,
                             creationflags=CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen(payload, cwd=project_path)
        return True, ""
    except Exception as e:
        return False, str(e)


def spawn(spec: AgentCLISpec, exe: str, project_path: str,
          instruction: str, model: str = "") -> tuple:
    """Open a new terminal running the agent with *instruction*.

    The window uses ``/k`` so it stays open after the agent exits, letting the
    user read the output and keep interacting.
    """
    if not exe:
        return False, _not_configured(spec)
    return _spawn(spec, exe, project_path, instruction, model)


def spawn_interactive(spec: AgentCLISpec, exe: str, project_path: str,
                      model: str = "") -> tuple:
    """Open a new terminal running the agent as an interactive TUI.

    NO instruction is passed — see the module docstring on why this cannot be
    ``spawn(..., instruction="")`` with a shared code path.
    """
    if not exe:
        return False, _not_configured(spec)
    if spec.interactive != INTERACTIVE_SUPPORTED:
        return False, f"{spec.label} has no interactive mode."
    return _spawn(spec, exe, project_path, "", model)


def _not_configured(spec: AgentCLISpec) -> str:
    return (f"{spec.label} is not configured. "
            f"Set the path in Settings → {spec.label}.")


# ── Resolution (Contract D) ──────────────────────────────────────────────────

#: The selected agent is known and its binary was found.
RESOLUTION_OK = "ok"
#: The selected agent is known, but no binary could be found or configured.
RESOLUTION_UNAVAILABLE = "unavailable"
#: Config names an agent this build does not have. NOT a synonym for "absent".
RESOLUTION_UNKNOWN_AGENT = "unknown_agent"


@dataclasses.dataclass(frozen=True)
class AgentResolution:
    """The answer to "which agent CLI should this call use, and can it run?".

    Callers used to write their own error text from a bare exe string, which
    collapsed three different situations into one "not configured" message.
    They are genuinely different and deserve different words:

        ok              spawn it
        unavailable     the right agent, but nothing to run — offer Settings
        unknown_agent   config names something unrecognised — a typo or a
                        downgrade, and running *any* agent here would be wrong

    `requested_id` is retained so the unknown-agent message can quote the
    offending value back to the user instead of saying "invalid".
    """

    spec: "AgentCLISpec | None"
    exe: str
    state: str
    requested_id: str = ""

    @property
    def ok(self) -> bool:
        return self.state == RESOLUTION_OK

    @property
    def label(self) -> str:
        """Display name for buttons and tooltips; safe when unresolved."""
        return self.spec.label if self.spec else "Agent CLI"

    def error_message(self) -> str:
        """User-facing reason this cannot run, or "" when it can."""
        if self.state == RESOLUTION_OK:
            return ""
        if self.state == RESOLUTION_UNKNOWN_AGENT:
            known = ", ".join(sorted(AGENT_CLIS))
            return (f"Unknown agent CLI {self.requested_id!r} in settings. "
                    f"Known agents: {known}. "
                    f"Fix 'agent_cli' in manager-config.json or pick one in "
                    f"Settings → AI → Agent CLI.")
        return (f"{self.label} is not installed or its path is not set. "
                f"Set it in Settings → Paths, or choose a different agent in "
                f"Settings → AI → Agent CLI.")


def resolve(agent_id: str, exe_lookup) -> AgentResolution:
    """Resolve a persisted agent id plus a path lookup into an AgentResolution.

    `exe_lookup` takes a spec and returns the configured-or-detected path, so
    this stays free of ManagerConfig and is testable with a plain lambda.

    An EMPTY id means "not set", which every pre-Cursor config effectively was,
    so it takes the default. A NON-EMPTY unrecognised id is invalid
    configuration and is reported as such — never quietly mapped onto the
    default, which would run Claude while the settings file said otherwise.
    """
    requested = (agent_id or "").strip()
    spec = get_spec(requested or DEFAULT_AGENT_ID)
    if spec is None:
        return AgentResolution(None, "", RESOLUTION_UNKNOWN_AGENT, requested)
    exe = exe_lookup(spec) or ""
    state = RESOLUTION_OK if exe else RESOLUTION_UNAVAILABLE
    return AgentResolution(spec, exe, state, spec.id)


def resolve_from(cfg) -> AgentResolution:
    """Resolve the agent CLI from a ManagerConfig, or anything shaped like one.

    A real `ManagerConfig` answers `resolve_agent_cli()`. Several call sites are
    also reached with lightweight config doubles that predate the selector and
    carry only `claude_cli_exe`; for those this degrades to the pre-Cursor
    behaviour instead of raising. The tolerance lives HERE, in one documented
    place, rather than as a `getattr` fallback repeated at every call site —
    seven copies of a compatibility shim is how the shim stops being reviewed.
    """
    own = getattr(cfg, "resolve_agent_cli", None)
    if callable(own):
        return own()
    exe = getattr(cfg, "claude_cli_exe", "") or ""
    return AgentResolution(
        CLAUDE, exe,
        RESOLUTION_OK if exe else RESOLUTION_UNAVAILABLE, CLAUDE.id)


def model_from(cfg, spec: "AgentCLISpec | None") -> str:
    """Pinned model for *spec*, tolerant of the same config doubles."""
    if spec is None:
        return ""
    own = getattr(cfg, "agent_model_for", None)
    if callable(own):
        return own(spec)
    return getattr(cfg, spec.config_key_model, "") or ""
