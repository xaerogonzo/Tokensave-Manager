"""DoctorController — tokensave doctor command group for the Projects tab.

Extracted from ProjectsTabController (Round 5). Roadmap-2 added the monolith
audit pass (file / method / class / complexity caps) — caps and semantics are
canonical in BASIC_INSTRUCTIONS.md (Rule A).

Dependency contract:
  • tab          — the Projects tk.Frame (after() scheduling + winfo_toplevel())
  • cfg          — read-only ManagerConfig (.tokensave_exe + .raw)
  • on_log       — thread-safe log callback  (msg: str, colour: str = "")
  • on_set_running  — (running: bool, label: str) -> None
  • on_set_proc  — (proc_or_none) -> None  updates parent's current_proc so
                   App._auto_refresh can detect when the controller is busy
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers import conpty, housekeeping
from helpers.runtime import log
from helpers.worktree_health import (
    find_orphaned_worktrees_for_project,
    repair_worktree_index,
)

import time

if TYPE_CHECKING:
    from state import ManagerConfig


# ── Doctor subprocess contract ────────────────────────────────────────────────
# ONE definition of how `tokensave doctor` is invoked, shared by the streaming
# run, the housekeeping scan, and the ConPTY purge. Two subtly different
# invocations of the same command is the drift this exists to prevent: without
# it the streaming path could get clean text while the scan path got raw ANSI.

def doctor_env() -> dict:
    """Environment for every `tokensave doctor` invocation.

    ``NO_COLOR`` + ``TERM=dumb`` are set as a best effort, but measured against
    v7.9.0 doctor ignores both and emits ANSI regardless — so every caller must
    strip escapes itself rather than trusting the environment. They stay because
    they cost nothing and a future version may honour them.

    Note also that doctor writes its entire report to **stderr**, not stdout.
    Any caller capturing output must merge the two (``stderr=STDOUT``) or it
    will get an empty transcript and read it as "nothing to report".
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


@dataclass
class DoctorScanResult:
    """Outcome of a non-interactive doctor run used purely to observe state.

    ``ok=False`` means "we don't know", which callers must render differently
    from "nothing to clean" — an empty entry list from a failed scan would
    otherwise read as a clean bill of health.
    """
    ok: bool
    transcript: str = ""
    exit_code: "int | None" = None
    error: str = ""


@dataclass
class PurgeResult:
    """Machine-readable outcome of a purge attempt.

    Replaces the old convention of inferring success by re-parsing log text.

    ``handed_off`` is a **first-class outcome, not an error**: it means the
    operation was correctly delegated to a terminal the user drives, and the
    result is not yet known. The Manager must never report success merely
    because cmd.exe launched or because the terminal process exited — only a
    post-operation scan settles that, which is what `verification_status`
    carries. ``cancelled`` stays distinct from ``process_error`` so backing out
    never reads as a transport failure.
    """
    status: str                       # see the constants below
    exit_code: "int | None" = None
    answers_sent: int = 0
    transcript: str = ""
    stale_before: list = field(default_factory=list)
    stale_after: list = field(default_factory=list)
    verification_status: str = ""     # one of the VERIFY_* values, once known
    error: str = ""

    SUCCESS = "success"
    HANDED_OFF = "handed_off"
    UNAVAILABLE = "unavailable"
    UNEXPECTED_PROMPT = "unexpected_prompt"
    TIMEOUT = "timeout"
    PROCESS_ERROR = "process_error"
    VERIFICATION_FAILED = "verification_failed"
    CANCELLED = "cancelled"

    @property
    def succeeded(self) -> bool:
        return self.status == PurgeResult.SUCCESS


# Verification vocabulary. The post-operation scan — not the mechanism — is
# what determines the outcome, so these are the values that actually matter.
VERIFY_VERIFIED = "verified"        # nothing left
VERIFY_PARTIAL = "partial"          # fewer than before, but not zero
VERIFY_NO_CHANGE = "no_change"      # exactly as many as before
VERIFY_UNVERIFIED = "unverified"    # the scan itself failed — outcome unknown

VERIFY_LABELS = {
    VERIFY_VERIFIED:   "✓ Purge verified — no stale entries remain",
    VERIFY_PARTIAL:    "⚠ Partially purged — {n} remaining",
    VERIFY_NO_CHANGE:  "⚠ No change detected — {n} still reported",
    VERIFY_UNVERIFIED: "? Outcome could not be verified",
}


# The prompt this controller is willing to answer. Deliberately narrow *in
# combination with* conpty's shape detection: conpty only offers text that is
# structurally a prompt (a bracketed y/N choice, or output stopped mid-line),
# and this pattern then requires it to be about purging stale entries. Any other
# prompt — however innocuous — comes back as UNEXPECTED_PROMPT with zero answers
# sent, because answering an unrecognised question on the user's behalf is the
# one failure mode with no undo.
_PURGE_PROMPT_RE = r"purge|stale"

# One human sentence per non-success status. Keeps `_after_purge` free of a
# branching wall and keeps the wording in one place.
_PURGE_EXPLANATIONS = {
    PurgeResult.UNAVAILABLE:
        "This system has no pseudoconsole support, so the purge prompt "
        "can't be answered automatically.",
    PurgeResult.UNEXPECTED_PROMPT:
        "tokensave asked something we don't recognise, so nothing was "
        "answered — finish this in a terminal where you can read it.",
    PurgeResult.TIMEOUT:
        "tokensave didn't finish in time; nothing was confirmed.",
    PurgeResult.PROCESS_ERROR:
        "tokensave doctor could not be run.",
    PurgeResult.VERIFICATION_FAILED:
        "The purge ran but stale entries are still reported afterwards.",
    PurgeResult.CANCELLED:
        "Purge cancelled.",
}


# ── Monolith-audit constants (canonical thresholds — see BASIC_INSTRUCTIONS Rule A) ──

_CAP_FILE_LINES         = 1500  # Doctor warning threshold (BASIC_INSTRUCTIONS aspires to 800)
_CAP_METHOD_LINES       = 100
_CAP_CLASS_METHODS      = 40
_CAP_COMPLEXITY         = 10
_CAP_LAYOUT_COMPLEXITY  = 3

# Layout-method name patterns — qualify for the 100-line carve-out IF cyclomatic
# complexity is also ≤ _CAP_LAYOUT_COMPLEXITY. Naming alone never grants immunity.
_LAYOUT_NAME_RE = re.compile(r"^_?(build|populate|render|layout)(_.*)?$")

# Top-of-file exemption: `# anti-monolith: exempt — <non-empty reason>` appearing
# in the file's comment block BEFORE the first class/def (robust to long docstrings,
# grouped imports, formatter reordering, __future__ blocks).
_EXEMPT_RE = re.compile(r"#\s*anti-monolith:\s*exempt\s*[—-]\s*(\S.*\S)")

# doctor emits `run `tokensave install --agent <id>`` on any integration it
# considers unconfigured. Agent ids are lowercase alnum + hyphen (e.g. roo-code).
_INSTALL_NAG_RE = re.compile(r"tokensave install\s+--agent\s+([a-z0-9-]+)")


def _extract_install_nags(lines: list) -> tuple:
    """Parse doctor output into (actionable_agents, other_count).

    An agent is actionable only when it is BOTH installed on this machine
    AND not already wired. Both halves matter:

    * installed — doctor checks all 20 integrations and nags about every one
      it can't find, which on a typical machine is ~18 agents the user has
      never heard of.
    * not already wired — several agents cover multiple surfaces under one
      ``--agent`` id (Copilot spans VS Code, Insiders, the CLI, JetBrains),
      and doctor emits a separate nag per missing surface. Checking only
      "installed" meant a fully-wired Copilot got re-offered on EVERY doctor
      run, because the nags were about Insiders/JetBrains — surfaces the
      user doesn't have and that re-running install cannot create. A prompt
      that reappears no matter how many times you accept it is worse than
      no prompt.

    Deliberately does NOT treat doctor's ✘-vs-! severity as the signal:
    upstream marks some optional integrations (OpenCode, Kiro) as ✘ even
    when the accompanying text says "if you use it", so severity alone
    would resurface exactly the noise this filters out.
    """
    from helpers.mcp import _tokensave_agent_installed, _tokensave_agent_wired
    seen: list = []
    for line in lines:
        m = _INSTALL_NAG_RE.search(line)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    actionable = [a for a in seen
                  if _tokensave_agent_installed(a) and not _tokensave_agent_wired(a)]
    return actionable, len(seen) - len(actionable)


class DoctorController:
    """Runs `tokensave doctor`, parses stale entries, and offers to purge them."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_set_running: Callable[[bool, str], None],
        on_set_proc: Callable[[object], None],
    ) -> None:
        self._tab           = tab
        self._cfg           = cfg
        self._on_log        = on_log
        self._on_set_running = on_set_running
        self._on_set_proc   = on_set_proc

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Public entry point ────────────────────────────────────────────────────

    def cmd_doctor(self, path: str) -> None:
        """Run doctor + offer purge. Call after require_tokensave guard passes."""
        self._run_with_purge_offer(path)

    # ── Worker helpers ────────────────────────────────────────────────────────

    def _run_with_purge_offer(self, path: str) -> None:
        label = os.path.basename(path)

        def worker():
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            log.info("RUN  tokensave doctor")
            output_lines: list[str] = []
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [self._cfg.tokensave_exe, "doctor"],
                    cwd=path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._on_set_proc(proc)
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    self._on_log(stripped)
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._on_log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                else:
                    self._on_log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self._analyse_doctor_output(
                    path, output_lines, proc.returncode)
                # Roadmap-2: monolith audit always runs after doctor finishes
                self._run_monolith_audit(path)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_doctor")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")

        threading.Thread(target=worker, daemon=True, name="doctor-worker").start()

    def _analyse_doctor_output(self, path: str, output_lines: list,
                               returncode: int) -> None:
        """Parse doctor's output, check worktree health, schedule follow-ups.

        Runs on the doctor worker thread; the only UI work it does directly
        is thread-safe ``_on_log`` calls. Dialogs go through a single
        ``after(0, _offer_followups, …)`` dispatch.
        """
        stale = [e.path for e in housekeeping.parse_stale_entries(output_lines)]
        nagged, other_n = _extract_install_nags(output_lines)
        # Worktree health is orthogonal to whether `tokensave doctor` itself
        # succeeded — it's a git-level check, not parsed from doctor's
        # output — so it runs regardless of returncode.
        orphans = find_orphaned_worktrees_for_project(path, self._cfg.git_exe)
        for o in orphans:
            self._on_log(
                f"  ⚠ worktree '{o['branch'] or o['head']}' at "
                f"{o['worktree_path']} has no tokensave index of "
                "its own — a session started there would silently "
                "get answers about a different checkout.", C["peach"])
        if other_n:
            # Informational only — never worth a modal. These are agents
            # that are either not installed here or already wired.
            self._on_log(
                f"  ({other_n} agent integration"
                f"{'s' if other_n != 1 else ''} mentioned by doctor "
                "need no action — not installed here, or already wired.)",
                C["overlay0"])
        if returncode == 0 and (stale or nagged or orphans):
            # Single dispatcher — never schedule the offers independently,
            # or they stack on top of each other.
            self._tab.after(0, self._offer_followups,
                            path, stale, nagged, other_n, orphans)

    def scan_stale(self, path: str, timeout: float = 120.0) -> DoctorScanResult:
        """Run doctor non-interactively and capture the transcript. Blocking.

        Used by the housekeeping surface to observe state without touching it.
        A timeout or launch failure returns ``ok=False`` rather than an empty
        transcript, so the caller can distinguish "nothing to clean" from
        "we couldn't find out".
        """
        try:
            r = subprocess.run(
                [self._cfg.tokensave_exe, "doctor"],
                cwd=path,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=doctor_env(), creationflags=CREATE_NO_WINDOW,
                timeout=timeout,
            )
        except FileNotFoundError:
            return DoctorScanResult(False, error="tokensave executable not found")
        except subprocess.TimeoutExpired:
            return DoctorScanResult(
                False, error=f"tokensave doctor timed out after {timeout:.0f}s")
        except OSError as e:
            return DoctorScanResult(False, error=str(e))
        return DoctorScanResult(
            True, transcript=_ANSI.sub("", r.stdout or ""),
            exit_code=r.returncode)

    def purge_stale(self, path: str,
                    baseline: "list | None" = None) -> PurgeResult:
        """Begin a purge. Blocking, but may finish in a terminal the user drives.

        Flow::

            baseline scan (reused, never re-run)
                  ↓
            can ConPTY actually attach?  ──no──►  handed_off  (cmd.exe)
                  │yes
            answer the prompt directly   ──────►  verify

        ``baseline`` lets the caller pass a scan it already performed, so the
        purge does not run ``tokensave doctor`` a second time for no reason.
        Whatever list is used here is also the comparison point for
        `verify_purge` — one scan, two jobs.

        A ``handed_off`` return is not a failure. It means the operation is now
        in the user's terminal and its outcome is genuinely unknown until
        `verify_purge` says otherwise.
        """
        if baseline is None:
            before_scan = self.scan_stale(path)
            if not before_scan.ok:
                return PurgeResult(PurgeResult.PROCESS_ERROR,
                                   error=before_scan.error)
            before = housekeeping.parse_stale_entries(before_scan.transcript)
        else:
            before = list(baseline)

        if not before:
            return PurgeResult(PurgeResult.SUCCESS, stale_before=[],
                               stale_after=[],
                               verification_status=VERIFY_VERIFIED)

        # `can_attach` probes rather than assumes: `is_available` only proves
        # the API exists, and on at least one Windows build every call succeeds
        # while the child's output never reaches the pseudoconsole. Gate on the
        # measured answer so a future fix enables this path by itself.
        if not conpty.can_attach():
            return PurgeResult(PurgeResult.HANDED_OFF, stale_before=before)

        # Upper bound only. We do NOT claim to know whether tokensave asks once
        # in aggregate or once per entry — the scan caps how many answers are
        # permissible, and the prompts that actually arrive decide how many are
        # sent. Under-answering degrades to a timeout and the terminal handoff,
        # which is the safe direction to be wrong in.
        policy = conpty.PromptPolicy(
            prompt_id="stale_purge", prompt_regex=_PURGE_PROMPT_RE,
            answer="y\r", max_answers=max(1, len(before)))

        res = conpty.run_interactive(
            [self._cfg.tokensave_exe, "doctor"], path, [policy], timeout_s=120.0)

        status_map = {
            conpty.ConPtyStatus.UNAVAILABLE: PurgeResult.UNAVAILABLE,
            conpty.ConPtyStatus.UNEXPECTED_PROMPT: PurgeResult.UNEXPECTED_PROMPT,
            conpty.ConPtyStatus.TIMEOUT: PurgeResult.TIMEOUT,
            conpty.ConPtyStatus.PROCESS_ERROR: PurgeResult.PROCESS_ERROR,
        }
        if res.status is not conpty.ConPtyStatus.COMPLETED:
            return PurgeResult(
                status_map.get(res.status, PurgeResult.PROCESS_ERROR),
                exit_code=res.exit_code, answers_sent=res.total_answers,
                transcript=res.transcript, stale_before=before, error=res.error)

        verified = self.verify_purge(path, before)
        verified.exit_code = res.exit_code
        verified.answers_sent = res.total_answers
        verified.transcript = res.transcript
        return verified

    def verify_purge(self, path: str, stale_before: list) -> PurgeResult:
        """Re-scan and report what actually happened. Blocking.

        This is the authoritative step. The Manager never concludes anything
        from the fact that a terminal opened or exited — only from comparing a
        fresh scan against the baseline. A scan that fails yields
        ``unverified``, which is deliberately NOT the same as "no change".
        """
        after_scan = self.scan_stale(path)
        if not after_scan.ok:
            return PurgeResult(
                PurgeResult.VERIFICATION_FAILED, stale_before=stale_before,
                verification_status=VERIFY_UNVERIFIED, error=after_scan.error)

        after = housekeeping.parse_stale_entries(after_scan.transcript)
        if not after:
            vstatus = VERIFY_VERIFIED
        elif len(after) < len(stale_before):
            vstatus = VERIFY_PARTIAL
        else:
            vstatus = VERIFY_NO_CHANGE

        return PurgeResult(
            PurgeResult.SUCCESS if vstatus == VERIFY_VERIFIED
            else PurgeResult.VERIFICATION_FAILED,
            stale_before=stale_before, stale_after=after,
            verification_status=vstatus)

    def open_purge_terminal(self, path: str) -> None:
        """Open a real terminal running `tokensave doctor` for the user to confirm in.

        The one thing this does NOT do is imply an outcome. Launching a terminal
        proves nothing about what the user then does in it — `verify_purge` is
        the only thing that settles that. Kept here rather than in the dialog so
        both entry points (the Doctor follow-up chain and Housekeeping) open the
        terminal exactly the same way.
        """
        cmd_line = f'cmd.exe /k ""{self._cfg.tokensave_exe}" doctor"'
        subprocess.Popen(cmd_line, cwd=path,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)

    @staticmethod
    def verification_label(result: PurgeResult) -> str:
        """Human sentence for a verification outcome."""
        template = VERIFY_LABELS.get(result.verification_status, "")
        return template.format(n=len(result.stale_after)) if template else ""

    def _run_purge(self, path: str, baseline: "list | None" = None,
                   on_done: "Callable[[], None] | None" = None) -> None:
        """Purge, then report what actually changed.

        ``on_done`` is dispatched EXACTLY ONCE when this branch is done — either
        directly, or handed to ``_offer_in_cmd`` when the purge moves to a
        terminal. The ``finally`` guard covers the exception path so a crash
        here can't strand the rest of the follow-up sequence.
        """
        label = "doctor (purge)"

        def worker():
            handed_off = False
            self._on_log(f"$ tokensave doctor  [{label}]", C["blue"])
            self._tab.after(0, self._on_set_running, True, label)
            try:
                result = self.purge_stale(path, baseline=baseline)
                handed_off = self._after_purge(path, result, on_done)
            except Exception as e:
                self._on_log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in doctor purge")
            finally:
                self._on_set_proc(None)
                self._tab.after(0, self._on_set_running, False, "")
                if on_done and not handed_off:
                    self._tab.after(0, on_done)

        threading.Thread(target=worker, daemon=True, name="doctor-purge").start()

    def _after_purge(self, path: str, result: PurgeResult,
                     on_done: "Callable[[], None] | None") -> bool:
        """Render a `PurgeResult` into the log. Returns True if ``on_done`` was
        handed to the follow-up prompt — the caller must then NOT fire it
        itself, or the rest of the sequence would run while that prompt is
        still open.
        """
        if result.succeeded:
            n = len(result.stale_before)
            if n:
                self._on_log(
                    f"  ✓ Stale entries purged ({n} removed).", C["green"])
            else:
                self._on_log("  ✓ No stale entries to purge.", C["green"])
            return False

        # A handoff is not a failure — it's the operation moving somewhere the
        # user drives it. Say so plainly, and do NOT imply anything about the
        # result: only the verification scan settles that.
        if result.status == PurgeResult.HANDED_OFF:
            self._on_log(
                "  → tokensave needs a real terminal for its purge prompt; "
                "opening one. Nothing has changed yet.", C["sky"])
        else:
            self._on_log(
                f"  ⚠ {_PURGE_EXPLANATIONS.get(result.status, result.status)}",
                C["peach"])
            label = self.verification_label(result)
            if label:
                self._on_log(f"    {label}", C["peach"])
        if result.error:
            self._on_log(f"    {result.error}", C["overlay0"])

        remaining = len(result.stale_after or result.stale_before)
        self._tab.after(0, self._offer_in_cmd, path, remaining,
                        result.stale_before, on_done)
        return True

    # ── Main-thread dialogs ───────────────────────────────────────────────────

    def _offer_followups(self, path: str, stale: list, nagged: list,
                         other_n: int, orphans: "list | None" = None) -> None:
        """Run the post-doctor prompts strictly one at a time.

        Calling these back-to-back is NOT enough to prevent stacking: a
        blocking ``askyesno`` returns as soon as it's dismissed, but
        ``_offer_purge`` may then spawn a BACKGROUND worker that later needs
        its own prompt ("open Doctor in a terminal?"). That second prompt
        lands whenever the worker finishes — on top of whatever dialog the
        sequencer has since opened. So the purge branch is chained through an
        explicit continuation rather than just called first.

        Order is deliberate:
          1. Stale-purge — async, may prompt again later; everything waits on
             the whole chain, not just on the first dialog closing.
          2. Worktree repair — blocking askyesno; its repair worker only
             logs, never prompts, so nothing has to wait on it.
          3. Agent wiring LAST — it opens a persistent ``grab_set()``
             Toplevel (the picker) that STAYS up. Any askyesno scheduled
             after it would render over a live modal.
        """
        def _then_worktrees() -> None:
            if orphans:
                self._offer_worktree_repair(path, orphans)
            # Only prompt when there is something the user can actually DO.
            # `other_n` alone is informational (already logged by
            # _analyse_doctor_output) and must never raise a modal.
            if nagged:
                self._offer_agent_wiring(nagged, other_n)

        if stale:
            self._offer_purge(path, stale, on_done=_then_worktrees)
        else:
            _then_worktrees()

    def _offer_worktree_repair(self, path: str, orphans: list) -> None:
        """Offer to init/sync-force every worktree missing its own index.

        Not routed through `tokensave branch` — branch tracking copies
        within ONE checkout and syncs from that checkout's own files; it has
        no visibility into another directory's working tree, which is
        exactly the problem here.
        """
        n = len(orphans)
        bullets = "\n".join(
            f"  • {o['branch'] or o['head']}  —  {o['worktree_path']}"
            for o in orphans)
        if not messagebox.askyesno(
                "Give worktrees their own tokensave index?",
                f"Found {n} git worktree{'s' if n != 1 else ''} of "
                f"{os.path.basename(path)} with no `.tokensave/` of its own:"
                f"\n\n{bullets}\n\n"
                "Without one, tokensave answers questions asked there using "
                "this checkout's index instead — confidently, and about the "
                "wrong branch.\n\n"
                f"Initialise {'them' if n != 1 else 'it'} now? Takes a few "
                "seconds per worktree.",
                parent=self._root):
            self._on_log("  (worktree repair skipped)", C["overlay0"])
            return
        self._run_worktree_repair(orphans)

    def _run_worktree_repair(self, orphans: list) -> None:
        def worker():
            any_ok = False
            for o in orphans:
                wt = o["worktree_path"]
                self._on_log(f"$ tokensave init/sync  [{wt}]", C["blue"])
                ok, action, detail = repair_worktree_index(
                    self._cfg.tokensave_exe, wt)
                if ok:
                    any_ok = True
                    verb = "initialized" if action == "init" \
                        else "rebuilt (sync --force)"
                    self._on_log(f"  ✓ {verb}", C["green"])
                else:
                    self._on_log(f"  ✗ failed ({action or 'init'}): {detail}",
                                 C["red"])
            if any_ok:
                # Unconditional — a repair here never takes effect in an
                # ALREADY-RUNNING session. The MCP server resolves its
                # project once at startup, not per tool call, so a session
                # that's been serving the wrong tree keeps doing so until
                # it's restarted.
                self._on_log(
                    "  ⚠ Any Claude Code session already running inside "
                    "these worktrees won't see the new index until it's "
                    "restarted — the MCP server resolves its project at "
                    "startup, not per call.", C["peach"])

        threading.Thread(target=worker, daemon=True,
                         name="doctor-worktree-repair").start()

    def _offer_agent_wiring(self, nagged: list, other_n: int) -> None:
        """Offer to open the agent picker for doctor's `install` nags."""
        if nagged:
            bullets = "\n".join(f"  • {a}" for a in nagged)
            msg = (
                "tokensave doctor suggests wiring these agents — which are "
                f"installed on this machine:\n\n{bullets}\n\n"
            )
        else:
            msg = ""
        if other_n:
            msg += (
                f"doctor also mentioned {other_n} other agent"
                f"{'s' if other_n != 1 else ''} that "
                f"{'are' if other_n != 1 else 'is'} not installed here — "
                "those are safe to ignore unless you actually use them.\n\n"
            )
        msg += "Open the agent picker now?"
        if not messagebox.askyesno(
                "Wire tokensave into agents?", msg, parent=self._root):
            self._on_log("  (agent wiring skipped)", C["overlay0"])
            return
        # Lazy import (Rule 6) — controller → dialog only on the yes path.
        from dialogs.tokensave_mcp_picker import TokensaveMCPPickerDialog
        TokensaveMCPPickerDialog(self._root, self._cfg, preselect=nagged)

    def _offer_purge(self, path: str, stale_paths: list[str],
                     on_done: "Callable[[], None] | None" = None) -> None:
        """Offer the stale-entry purge.

        ``on_done`` fires once this ENTIRE branch is finished — including the
        background purge worker and its own follow-up prompt — so the caller
        can safely open the next dialog only after everything here settles.
        """
        n = len(stale_paths)
        bullets = "\n".join(f"  • {p}" for p in stale_paths)
        msg = (
            f"tokensave doctor found {n} stale project entr"
            f"{'y' if n == 1 else 'ies'} in the global DB.\n\n"
            f"{bullets}\n\n"
            "These projects were registered but their `.tokensave/` "
            "folders are gone — most likely deleted folders.\n\n"
            "Purge them now?  tokensave only offers its purge prompt on a "
            "real terminal, so this may open one for you to confirm in. "
            "Either way the manager re-checks afterwards and reports what "
            "actually changed."
        )
        if not messagebox.askyesno("Purge stale tokensave projects?", msg,
                                   parent=self._root):
            self._on_log("  (purge skipped — stale entries left in place)", C["overlay0"])
            if on_done:
                on_done()
            return
        # Reuse what doctor already told us instead of scanning again — this
        # chain has just run doctor, and its result is still accurate.
        baseline = [housekeeping.StaleEntry(path=p) for p in stale_paths]
        self._run_purge(path, baseline=baseline, on_done=on_done)

    def _offer_in_cmd(self, path: str, n_stale: int,
                      stale_before: "list | None" = None,
                      on_done: "Callable[[], None] | None" = None) -> None:
        """Last link in the purge chain — always releases ``on_done``."""
        try:
            self._offer_in_cmd_inner(path, n_stale, stale_before or [])
        finally:
            if on_done:
                on_done()

    def _offer_in_cmd_inner(self, path: str, n_stale: int,
                            stale_before: list) -> None:
        plural = "entry" if n_stale == 1 else "entries"
        if not messagebox.askyesno(
                "Open Doctor in a new terminal?",
                f"The automatic purge didn't complete, so nothing has "
                f"been changed.\n\n"
                f"Open a new cmd.exe window with `tokensave doctor` "
                f"running there?  You'll see the {n_stale} stale "
                f"{plural} listed and tokensave will ask you to "
                f"confirm — type 'y' and press Enter to purge.\n\n"
                f"The window stays open after, so you can close it "
                f"yourself when done.",
                parent=self._root):
            self._on_log(
                "  (terminal-purge skipped — stale entries still in DB)",
                C["overlay0"])
            return
        try:
            self.open_purge_terminal(path)
            self._on_log(
                "  Opened cmd.exe — type 'y' at the prompt to purge, "
                "then close the window.",
                C["sky"])
            # Deliberately NOT reported as done: a launched terminal proves
            # nothing about the outcome. Verify by re-scanning once the user
            # says they've finished.
            self._tab.after(0, self._offer_verify, path, stale_before)
        except OSError as e:
            self._on_log(f"  ✗ Could not launch cmd.exe: {e}", C["red"])

    def _offer_verify(self, path: str, stale_before: list) -> None:
        """Ask whether the terminal purge is finished, then check for real."""
        if not messagebox.askyesno(
                "Verify cleanup?",
                "Once you've confirmed the purge in the terminal window, the "
                "manager can re-check and tell you what actually changed.\n\n"
                "Verify now?  (You can also do this any time from "
                "🧹 Housekeeping.)",
                parent=self._root):
            self._on_log(
                "  (not verified — outcome unknown until you re-check)",
                C["overlay0"])
            return

        def worker():
            result = self.verify_purge(path, stale_before)
            label = self.verification_label(result)
            colour = (C["green"] if result.verification_status == VERIFY_VERIFIED
                      else C["peach"])
            self._on_log(f"  {label}", colour)
            if result.error:
                self._on_log(f"    {result.error}", C["overlay0"])

        threading.Thread(target=worker, daemon=True,
                         name="doctor-verify").start()

    # ── Monolith audit ────────────────────────────────────────────────────────

    def _run_monolith_audit(self, project_path: str) -> None:
        """Walk *.py under project_path and log cap violations.

        Triggered at the end of cmd_doctor. Caps + semantics are canonical
        in BASIC_INSTRUCTIONS.md Rule A.
        """
        self._on_log("═══ Code-health caps ═══", C["mauve"])

        skip = {p.replace("\\", "/")
                for p in (self._cfg.raw.get("doctor_skip_monolith_paths") or [])}

        violations, exempt_notes, files_scanned = _audit_project_tree(
            project_path, skip)

        self._log_audit_results(violations, exempt_notes, files_scanned)

    def _log_audit_results(
        self,
        violations: list[str],
        exempt_notes: list[str],
        files_scanned: int,
    ) -> None:
        if not violations:
            plural = "s" if files_scanned != 1 else ""
            self._on_log(
                f"  OK — {files_scanned} file{plural} checked, no cap violations.",
                C["green"])
        else:
            plural = "s" if len(violations) != 1 else ""
            self._on_log(
                f"  Found {len(violations)} violation{plural} "
                f"across {files_scanned} files:",
                C["peach"])
            for line in violations:
                self._on_log(line, C["peach"])

        for note in exempt_notes:
            self._on_log(note, C["overlay0"])

    # Output parsing lives in `helpers.housekeeping.parse_stale_entries` — the
    # single canonical parser for doctor's stale block. A local copy used to
    # live here; two parsers for one output section is exactly how they drift.


# ── Module-level audit helpers (AST-based; canonical semantics — Rule A) ──

def _parse_exempt_header(source: str) -> str | None:
    """Return the exemption rationale if present in the file's pre-class/def
    comment block. Returns None if no exemption (or if rationale is empty)."""
    for raw_line in source.splitlines():
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _EXEMPT_RE.search(stripped)
            if m:
                reason = m.group(1).strip()
                return reason if reason else None
            continue
        # Hit a non-comment, non-blank line — exemption can only appear before
        # the first class/def. Anything else (imports, docstrings, decorators,
        # module-level statements) ends the search window.
        return None
    return None


def _method_span_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Method body span — decorators do NOT count toward length.

    `node.lineno` points at the `def` line (after any decorators); `body[-1]`
    has the final statement's end_lineno. This matches the Rule A semantics.
    """
    if not node.body:
        return 1
    end = getattr(node.body[-1], "end_lineno", None) or node.lineno
    return end - node.lineno + 1


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Cyclomatic complexity per Rule A.

    Each if/elif/for/while/except adds 1; each and/or short-circuit adds 1;
    each match arm adds 1; comprehension `if` clauses add 1 each.
    Nested function/class definitions are NOT recursed into — they have
    their own scores.
    """
    score = 1  # base path

    def _visit(n: ast.AST, is_root: bool) -> None:
        nonlocal score
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not is_root:
            return  # nested scope — not our complexity
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            score += 1
        elif isinstance(n, ast.BoolOp):
            score += max(0, len(n.values) - 1)
        elif isinstance(n, ast.comprehension):
            score += len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            score += 1
        for child in ast.iter_child_nodes(n):
            _visit(child, is_root=False)

    _visit(node, is_root=True)
    return score


def _is_layout_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Layout-method carve-out: name pattern + complexity ≤ 3.

    Naming alone never grants immunity (Rule A).
    """
    if not _LAYOUT_NAME_RE.match(node.name):
        return False
    return _cyclomatic_complexity(node) <= _CAP_LAYOUT_COMPLEXITY


_AUDIT_SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".tokensave", ".codegraph",
    ".venv", "venv", "node_modules", "dist", "build",
    ".build", ".onefile-build",
    # `.claude/worktrees/<name>/` holds full checkouts of this same repo, so
    # walking into it audits a second copy of every file and reports each
    # violation twice (442 files / 253 violations instead of ~221 / ~126).
    # Skipped as a whole for the same reason as .git and .tokensave: it is
    # agent-owned state, not project source.
    ".claude",
})

# Non-Python source / prose extensions that get a line-count-only audit.
# Data formats (.json, .xml, .yaml) are intentionally excluded — line count
# isn't meaningful for serialised data.
_AUDIT_TEXT_EXTS = frozenset({
    ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".lua",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".rs", ".go", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".swift", ".kt", ".php", ".sql",
    ".html", ".css", ".scss", ".md",
})


def _audit_project_tree(
    project_path: str,
    skip_rel_paths: set[str],
) -> tuple[list[str], list[str], int]:
    """Walk audit-eligible files; return (violations, exempts, files_scanned).

    Python files (`*.py`) get the full AST audit (methods, classes,
    complexity). Non-Python source/prose files (see `_AUDIT_TEXT_EXTS`)
    get a line-count-only check against the same file cap.
    """
    violations: list[str] = []
    exempt_notes: list[str] = []
    files_scanned = 0

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _AUDIT_SKIP_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, project_path).replace("\\", "/")
            # Support both exact-file matches ("scripts/foo.py") and
            # directory-prefix matches ("scripts" skips everything under it).
            if any(rel == sp or rel.startswith(sp.rstrip("/") + "/")
                   for sp in skip_rel_paths):
                continue
            if ext == ".py":
                result = _audit_python_file(full)
            elif ext in _AUDIT_TEXT_EXTS:
                result = _audit_text_file(full)
            else:
                continue
            files_scanned += 1
            if result is None:
                continue
            if result["exempt"]:
                exempt_notes.append(f"  (exempt: {rel} — {result['exempt_reason']})")
            else:
                violations.extend(f"  {rel}: {v}" for v in result["violations"])

    return violations, exempt_notes, files_scanned


def _audit_text_file(path: str) -> dict | None:
    """Line-count-only audit for non-Python source / prose files.

    Honours the same `# anti-monolith: exempt — <reason>` rationale comment.
    The regex matches the bare phrase, so any comment syntax wrapping it
    works: `# ...` (sh/ps1/lua), `// ...` (js/c/rust), `<!-- ... -->` (md/html).
    Scans the first 20 lines for the exemption marker.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return None

    head = "\n".join(source.splitlines()[:20])
    m = _EXEMPT_RE.search(head)
    if m:
        reason = m.group(1).strip()
        if reason:
            return {"exempt": True, "exempt_reason": reason, "violations": []}

    line_count = source.count("\n") + 1
    if line_count > _CAP_FILE_LINES:
        return {"exempt": False, "exempt_reason": None,
                "violations": [f"file is {line_count} lines (cap {_CAP_FILE_LINES})"]}
    return {"exempt": False, "exempt_reason": None, "violations": []}


def _audit_method_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Return violation strings for a single method/function node."""
    out: list[str] = []
    span = _method_span_lines(node)
    if span > _CAP_METHOD_LINES and not _is_layout_method(node):
        out.append(f"{node.name}() is {span} lines (cap {_CAP_METHOD_LINES})")
    cc = _cyclomatic_complexity(node)
    if cc > _CAP_COMPLEXITY:
        out.append(f"{node.name}() complexity {cc} (cap {_CAP_COMPLEXITY})")
    return out


def _audit_class_node(node: ast.ClassDef) -> list[str]:
    """Return violation strings for a class node."""
    method_count = sum(
        1 for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    if method_count > _CAP_CLASS_METHODS:
        return [f"class {node.name} has {method_count} direct methods "
                f"(cap {_CAP_CLASS_METHODS})"]
    return []


def _audit_python_file(path: str) -> dict | None:
    """Audit a single .py file. Returns dict or None on parse failure.

    Returned dict:
        {"exempt": bool, "exempt_reason": str | None, "violations": list[str]}
    """
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return None

    reason = _parse_exempt_header(source)
    if reason:
        return {"exempt": True, "exempt_reason": reason, "violations": []}

    violations: list[str] = []
    line_count = source.count("\n") + 1
    if line_count > _CAP_FILE_LINES:
        violations.append(f"file is {line_count} lines (cap {_CAP_FILE_LINES})")

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"exempt": False, "exempt_reason": None, "violations": violations}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_audit_method_node(node))
        elif isinstance(node, ast.ClassDef):
            violations.extend(_audit_class_node(node))

    return {"exempt": False, "exempt_reason": None, "violations": violations}


def count_methods_in_class(file_path: str, class_name: str) -> int:
    """Return the AST-direct-children method count for a class.

    Public helper exported for verification scripts (e.g. god-class regression
    checks after extractions). Matches the same semantics the Doctor audit
    uses — never use `dir()` for this; it includes inherited attributes,
    descriptor magic, and runtime additions.
    """
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return sum(
                1 for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    raise LookupError(f"class {class_name!r} not found in {file_path}")
