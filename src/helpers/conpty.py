"""Windows pseudoconsole (ConPTY) transport — give a console program a real TTY.

Some CLIs only prompt when ``isatty()`` is true, so piping ``y`` into a
pipe-backed subprocess does nothing. This module hands such a program an actual
pseudoconsole, so its prompt appears and can be answered.

**Optional, and not the production path.** Nothing in housekeeping correctness
depends on it: the authoritative route is a terminal handoff plus a verification
scan, which proves the *result* rather than trusting the mechanism. Callers gate
on `can_attach`, which probes rather than assumes.

**It knows nothing about tokensave.** The caller supplies argv and a list of
`PromptPolicy` and gets back a typed `ConPtyResult` — the next command needing a
TTY should reuse this unchanged.

Safety model
------------
A real terminal makes every prompt *look* answerable, so:

  * an answer is written only on a match against a caller-supplied policy,
    never speculatively;
  * ``max_answers`` is a ceiling supplied by the caller — this module never
    guesses how many prompts to expect;
  * prompt-shaped output matching no policy ends the run as
    ``UNEXPECTED_PROMPT`` with **zero answers sent**.

Detection is shape-based over an accumulated rolling buffer, never one
``ReadFile`` chunk: a prompt can arrive split across reads, and several can
arrive in one. Two shapes count — a bracketed choice (``[y/N]``) anywhere, or a
line ending in ``?``/``:`` **with no trailing newline** at the end of what we
have read. That second rule is what keeps ordinary prose containing ``?`` from
tripping the safety path: output that ended a line moved on, output that stopped
mid-line is waiting.

Known limitation (measured: Windows 11 build 26200, CPython 3.13)
------------------------------------------------------------------
The child attaches to the pseudoconsole but its std handles still resolve to the
inherited console, so its output never reaches the PTY pipe. Every call reports
success — ``CreatePseudoConsole`` hr=0, ``UpdateProcThreadAttribute`` rc=1,
``CreateProcessW`` rc=1 with ``GetLastError=0`` — and the 86-byte init frame
carries ``ESC]0;…cmd.exe BEL``, which proves the attach itself worked. Ruled out
by direct experiment: struct layout (104/112), attribute-list alignment, ``cast``
vs ``addressof``, ``bInheritHandles`` both ways, inheritable pipe handles,
renderer flush delay up to 2.5s, a parent with no console at all, and COORD
marshalling both by-value and packed. The one configuration that does capture
output (``STARTF_USESTDHANDLES`` aimed at the pipes) makes ``isatty()`` false,
which defeats the purpose — against real tokensave it lists the stale entries
and never prompts.

`can_attach` exists so this resolves empirically: if a future build fixes it,
the path lights up with no code change.

Lifecycle notes: the reader always runs on its own thread and may observe EOF;
``ClosePseudoConsole`` is never called from that thread; teardown lives in one
``finally``; ``ReleasePseudoConsole`` is used when present (Win11 24H2+) but is
never required, since the floor is Windows 10 1809.

Import safety: stdlib only, and ``ctypes.wintypes`` is deliberately unused —
importing it raises off-Windows and would abort pytest collection in CI. The
structures below use plain ``ctypes`` primitives, so this imports cleanly on
Linux where `is_available` simply returns False.
"""

from __future__ import annotations

import codecs
import ctypes
import os
import re
import sys
import threading
from ctypes import (
    POINTER, Structure, byref, c_int, c_short, c_size_t, c_ubyte, c_ulong,
    c_ushort, c_void_p, c_wchar_p, sizeof,
)
from dataclasses import dataclass, field
from enum import Enum

# ── Win32 constants ───────────────────────────────────────────────────────────
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
# The Manager is a GUI app: a console app spawned from it must never flash a
# window. Harmless alongside a pseudoconsole, which has no window of its own.
_CREATE_NO_WINDOW = 0x08000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_OBJECT_0 = 0x00000000

# Symbols the implementation actually calls. `is_available()` probes this exact
# list rather than a build number — a version check would claim support on a
# system where one of these is missing for any other reason.
_REQUIRED_SYMBOLS = (
    "CreatePseudoConsole", "ClosePseudoConsole",
    "InitializeProcThreadAttributeList", "UpdateProcThreadAttribute",
    "DeleteProcThreadAttributeList", "CreateProcessW", "CreatePipe",
    "CloseHandle", "ReadFile", "WriteFile", "TerminateProcess",
    "WaitForSingleObject", "GetExitCodeProcess",
)

# Bracketed choice — strong signal, matched anywhere in the buffer.
_CHOICE_RE = re.compile(r"[\[(][yY]\s*/\s*[nN][\])]|[\[(][nN]\s*/\s*[yY][\])]")
# Waiting-mid-line — only valid at the very end of the buffer (no newline after).
_TAIL_RE = re.compile(r"[^\r\n]{0,200}[?:]\s*\Z")

# Keep the rolling buffer bounded without ever cutting a prompt in half.
_BUF_MAX = 16384
_BUF_KEEP = 4096
# How much text before a candidate counts as its "line" for policy matching.
_CONTEXT_BACK = 400


class ConPtyStatus(str, Enum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    UNEXPECTED_PROMPT = "unexpected_prompt"
    TIMEOUT = "timeout"
    PROCESS_ERROR = "process_error"


@dataclass(frozen=True)
class PromptPolicy:
    """One prompt this caller is willing to answer.

    ``prompt_regex`` is searched against the prompt's own line (not the whole
    transcript), so an unrelated earlier mention of the same words cannot make a
    later prompt look recognised.
    """
    prompt_id: str
    prompt_regex: str
    answer: str
    max_answers: int


@dataclass
class ConPtyResult:
    status: ConPtyStatus
    exit_code: "int | None" = None
    transcript: str = ""
    answers_sent: dict = field(default_factory=dict)
    error: str = ""

    @property
    def total_answers(self) -> int:
        return sum(self.answers_sent.values())


# ── Win32 structures (plain ctypes primitives — see module docstring) ─────────

class _COORD(Structure):
    _fields_ = [("X", c_short), ("Y", c_short)]


class _STARTUPINFOW(Structure):
    _fields_ = [
        ("cb", c_ulong), ("lpReserved", c_wchar_p), ("lpDesktop", c_wchar_p),
        ("lpTitle", c_wchar_p), ("dwX", c_ulong), ("dwY", c_ulong),
        ("dwXSize", c_ulong), ("dwYSize", c_ulong), ("dwXCountChars", c_ulong),
        ("dwYCountChars", c_ulong), ("dwFillAttribute", c_ulong),
        ("dwFlags", c_ulong), ("wShowWindow", c_ushort), ("cbReserved2", c_ushort),
        ("lpReserved2", POINTER(c_ubyte)), ("hStdInput", c_void_p),
        ("hStdOutput", c_void_p), ("hStdError", c_void_p),
    ]


class _STARTUPINFOEXW(Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]


class _PROCESS_INFORMATION(Structure):
    _fields_ = [
        ("hProcess", c_void_p), ("hThread", c_void_p),
        ("dwProcessId", c_ulong), ("dwThreadId", c_ulong),
    ]


class _SECURITY_ATTRIBUTES(Structure):
    _fields_ = [
        ("nLength", c_ulong), ("lpSecurityDescriptor", c_void_p),
        ("bInheritHandle", c_int),
    ]


def _kernel32():
    """Return kernel32 with argtypes configured, or None if unusable."""
    if sys.platform != "win32":
        return None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    # argtypes are load-bearing, not decoration: without them ctypes marshals
    # pointer arguments as C int and silently truncates them to 32 bits on x64.
    sigs = {
        "CreatePseudoConsole": ([_COORD, c_void_p, c_void_p, c_ulong,
                                 POINTER(c_void_p)], c_int),      # HRESULT
        "ClosePseudoConsole": ([c_void_p], None),
        "CreatePipe": ([POINTER(c_void_p), POINTER(c_void_p),
                        POINTER(_SECURITY_ATTRIBUTES), c_ulong], c_int),
        "InitializeProcThreadAttributeList": (
            [c_void_p, c_ulong, c_ulong, POINTER(c_size_t)], c_int),
        "UpdateProcThreadAttribute": (
            [c_void_p, c_ulong, c_size_t, c_void_p, c_size_t, c_void_p,
             POINTER(c_size_t)], c_int),
        "DeleteProcThreadAttributeList": ([c_void_p], None),
        "CreateProcessW": ([c_wchar_p, c_wchar_p, c_void_p, c_void_p, c_int,
                            c_ulong, c_void_p, c_wchar_p,
                            POINTER(_STARTUPINFOEXW),
                            POINTER(_PROCESS_INFORMATION)], c_int),
        "ReadFile": ([c_void_p, c_void_p, c_ulong, POINTER(c_ulong),
                      c_void_p], c_int),
        "WriteFile": ([c_void_p, c_void_p, c_ulong, POINTER(c_ulong),
                       c_void_p], c_int),
        "CloseHandle": ([c_void_p], c_int),
        "TerminateProcess": ([c_void_p, c_ulong], c_int),
        "WaitForSingleObject": ([c_void_p, c_ulong], c_ulong),
        "GetExitCodeProcess": ([c_void_p, POINTER(c_ulong)], c_int),
    }
    try:
        for name, (argtypes, restype) in sigs.items():
            fn = getattr(k, name)
            fn.argtypes = argtypes
            fn.restype = restype
    except (AttributeError, TypeError):
        return None
    return k


def is_available() -> bool:
    """True when a pseudoconsole can actually be created on this system.

    Probes the concrete symbol surface the implementation calls rather than
    comparing Windows build numbers: a version check would claim support on a
    system where one of these entry points is missing for any other reason.
    Callers must treat False as "use your fallback path", never as an error.
    """
    k = _kernel32()
    if k is None:
        return False
    return all(hasattr(k, name) for name in _REQUIRED_SYMBOLS)


def budget_left(answers: dict, policy: PromptPolicy) -> bool:
    """True while ``policy`` may still be answered.

    Split out as a plain function so the "never exceed the caller's ceiling"
    rule is directly testable without standing up a real pseudoconsole.
    """
    return answers.get(policy.prompt_id, 0) < policy.max_answers


_ATTACH_PROBE: "bool | None" = None


def can_attach(force: bool = False) -> bool:
    """True when a child *actually* reaches the pseudoconsole, proven by probe.

    `is_available` only says the API exists. That is not the same thing: on the
    development machine every call succeeds and the child still writes to the
    inherited console (see the module docstring). Callers that care about
    correctness must gate on this, not on `is_available`.

    Runs one trivial command and checks whether its output came back through
    the PTY. Cached for the life of the process — the answer is a property of
    the OS build, not of the moment — with ``force`` to re-probe in tests.
    """
    global _ATTACH_PROBE
    if _ATTACH_PROBE is not None and not force:
        return _ATTACH_PROBE
    if not is_available():
        _ATTACH_PROBE = False
        return False
    marker = "__conpty_probe_ok__"
    try:
        res = run_interactive(
            ["cmd.exe", "/c", "echo", marker], os.getcwd(), [], timeout_s=10.0)
        _ATTACH_PROBE = (res.status is ConPtyStatus.COMPLETED
                         and marker in res.transcript)
    except Exception:                                       # pragma: no cover
        _ATTACH_PROBE = False
    return _ATTACH_PROBE


class _PromptScanner:
    """Rolling-buffer prompt detector.

    Owns the accumulated (ANSI-free) output and hands back prompt candidates as
    they complete. Buffer-based rather than chunk-based so a prompt split across
    reads is still found, and several prompts inside one read are each returned.
    """

    def __init__(self, policies: "list[PromptPolicy]"):
        self._policies = [(p, re.compile(p.prompt_regex, re.IGNORECASE))
                          for p in policies]
        self._buf = ""

    def feed(self, chunk: str) -> "list[tuple[str, PromptPolicy | None]]":
        """Add output; return [(prompt_text, policy_or_None)] for new prompts.

        A `None` policy means prompt-shaped text that no policy claims — the
        caller must treat that as `UNEXPECTED_PROMPT` and stop.
        """
        self._buf += chunk
        found: list = []
        while True:
            span = self._next_candidate()
            if span is None:
                break
            start, end = span
            ctx_start = max(0, start - _CONTEXT_BACK)
            line_break = self._buf.rfind("\n", ctx_start, start)
            if line_break != -1:
                ctx_start = line_break + 1
            context = self._buf[ctx_start:end]
            self._buf = self._buf[end:]
            found.append((context.strip(), self._match_policy(context)))
        self._trim()
        return found

    def _next_candidate(self) -> "tuple[int, int] | None":
        m = _CHOICE_RE.search(self._buf)
        if m:
            return m.start(), m.end()
        # Shape 2 only counts at the very end of what we have read: output that
        # stopped mid-line is waiting, output that ended a line moved on.
        t = _TAIL_RE.search(self._buf)
        if t and t.end() == len(self._buf) and self._buf.strip():
            return t.start(), t.end()
        return None

    def _match_policy(self, context: str) -> "PromptPolicy | None":
        for policy, rx in self._policies:
            if rx.search(context):
                return policy
        return None

    def _trim(self) -> None:
        if len(self._buf) > _BUF_MAX:
            self._buf = self._buf[-_BUF_KEEP:]


def run_interactive(argv: "list[str]", cwd: str,
                    policies: "list[PromptPolicy]",
                    timeout_s: float = 60.0,
                    cols: int = 120, rows: int = 40) -> ConPtyResult:
    """Run ``argv`` attached to a pseudoconsole, answering only known prompts.

    Returns a `ConPtyResult`; never raises for an expected failure mode. The
    caller is responsible for deciding what each status means and for having a
    fallback when `is_available()` is False.
    """
    k = _kernel32()
    if k is None or not is_available():
        return ConPtyResult(status=ConPtyStatus.UNAVAILABLE,
                            error="pseudoconsole API unavailable")

    scanner = _PromptScanner(policies)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    answers: dict = {}
    transcript: list = []
    unexpected: list = []
    lock = threading.Lock()

    h_in_r = c_void_p()
    h_in_w = c_void_p()
    h_out_r = c_void_p()
    h_out_w = c_void_p()
    h_pc = c_void_p()
    attr_list = None
    pi = _PROCESS_INFORMATION()
    reader: "threading.Thread | None" = None
    spawned = False
    status = ConPtyStatus.COMPLETED
    exit_code: "int | None" = None
    error = ""

    def _write_answer(policy: PromptPolicy) -> None:
        if not budget_left(answers, policy):
            return                      # budget spent — stay silent
        sent = answers.get(policy.prompt_id, 0)
        data = policy.answer.encode("utf-8")
        written = c_ulong(0)
        if k.WriteFile(h_in_w, data, len(data), byref(written), None):
            answers[policy.prompt_id] = sent + 1

    def _reader() -> None:
        buf = ctypes.create_string_buffer(4096)
        n = c_ulong(0)
        while True:
            ok = k.ReadFile(h_out_r, buf, 4096, byref(n), None)
            if not ok or n.value == 0:
                break                   # EOF / broken pipe: pseudoconsole closed
            text = decoder.decode(buf.raw[:n.value])
            if not text:
                continue
            with lock:
                transcript.append(text)
                for prompt_text, policy in scanner.feed(text):
                    if policy is None:
                        unexpected.append(prompt_text)
                        return          # stop reading; teardown handles the rest
                    _write_answer(policy)

    try:
        sa = _SECURITY_ATTRIBUTES(sizeof(_SECURITY_ATTRIBUTES), None, 0)
        if not k.CreatePipe(byref(h_in_r), byref(h_in_w), byref(sa), 0):
            return ConPtyResult(status=ConPtyStatus.PROCESS_ERROR,
                                error="CreatePipe(input) failed")
        if not k.CreatePipe(byref(h_out_r), byref(h_out_w), byref(sa), 0):
            return ConPtyResult(status=ConPtyStatus.PROCESS_ERROR,
                                error="CreatePipe(output) failed")

        hr = k.CreatePseudoConsole(_COORD(cols, rows), h_in_r, h_out_w,
                                   0, byref(h_pc))
        if hr != 0:
            return ConPtyResult(status=ConPtyStatus.PROCESS_ERROR,
                                error=f"CreatePseudoConsole failed (hr=0x{hr:08x})")

        # ConPTY duplicated both ends it was handed; the parent's copies are
        # dead weight and would keep the pipes alive past teardown.
        k.CloseHandle(h_in_r); h_in_r = c_void_p()
        k.CloseHandle(h_out_w); h_out_w = c_void_p()

        size = c_size_t(0)
        k.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
        attr_list = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attr_list, 1, 0, byref(size)):
            return ConPtyResult(status=ConPtyStatus.PROCESS_ERROR,
                                error="InitializeProcThreadAttributeList failed")
        if not k.UpdateProcThreadAttribute(
                attr_list, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                h_pc, sizeof(c_void_p), None, None):
            return ConPtyResult(status=ConPtyStatus.PROCESS_ERROR,
                                error="UpdateProcThreadAttribute failed")

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = sizeof(_STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(attr_list, c_void_p)

        cmdline = _cmdline(argv)
        if not k.CreateProcessW(
                None, cmdline, None, None, 0,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW, None,
                cwd or None, byref(si), byref(pi)):
            return ConPtyResult(
                status=ConPtyStatus.PROCESS_ERROR,
                error=f"CreateProcessW failed (err={ctypes.get_last_error()})")
        spawned = True

        reader = threading.Thread(target=_reader, daemon=True,
                                  name="conpty-reader")
        reader.start()

        waited = k.WaitForSingleObject(pi.hProcess, int(timeout_s * 1000))
        if waited == _WAIT_TIMEOUT:
            status = ConPtyStatus.TIMEOUT
        elif waited == _WAIT_OBJECT_0:
            code = c_ulong(0)
            if k.GetExitCodeProcess(pi.hProcess, byref(code)):
                exit_code = int(code.value)
    except OSError as e:                                    # pragma: no cover
        status = ConPtyStatus.PROCESS_ERROR
        error = str(e)
    finally:
        # ── single teardown path ──────────────────────────────────────────────
        # Order: stop the child, break its output so the reader sees EOF, join
        # the reader, then release every handle. ClosePseudoConsole runs HERE,
        # on this thread — never on the reader thread.
        try:
            if spawned and status is ConPtyStatus.TIMEOUT:
                k.TerminateProcess(pi.hProcess, 1)
            if h_in_w:
                k.CloseHandle(h_in_w); h_in_w = c_void_p()
            if h_pc:
                release = getattr(k, "ReleasePseudoConsole", None)
                if release is not None:
                    # Win11 24H2+: breaks the host/client lifetime loop first.
                    try:
                        release.argtypes = [c_void_p]
                        release(h_pc)
                    except (AttributeError, OSError):
                        pass
                k.ClosePseudoConsole(h_pc)
                h_pc = c_void_p()
            if reader is not None:
                reader.join(timeout=5.0)
            if h_out_r:
                k.CloseHandle(h_out_r)
            if h_in_r:
                k.CloseHandle(h_in_r)
            if h_out_w:
                k.CloseHandle(h_out_w)
            if attr_list is not None:
                k.DeleteProcThreadAttributeList(ctypes.cast(attr_list, c_void_p))
            if spawned:
                if pi.hThread:
                    k.CloseHandle(pi.hThread)
                if pi.hProcess:
                    k.CloseHandle(pi.hProcess)
        except OSError:                                     # pragma: no cover
            pass

    if unexpected:
        # Precedence is deliberate: an unrecognised prompt is the finding worth
        # reporting even if the process then exited cleanly on its own.
        return ConPtyResult(
            status=ConPtyStatus.UNEXPECTED_PROMPT, exit_code=exit_code,
            transcript="".join(transcript), answers_sent=answers,
            error=f"unrecognised prompt: {unexpected[0][:200]}")

    return ConPtyResult(status=status, exit_code=exit_code,
                        transcript="".join(transcript),
                        answers_sent=answers, error=error)


def _cmdline(argv: "list[str]") -> str:
    """Quote argv the way CreateProcessW expects, via the stdlib's own rules."""
    import subprocess
    return subprocess.list2cmdline(list(argv))
