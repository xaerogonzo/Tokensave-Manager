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

import os
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

import tkinter as tk
from tkinter import messagebox

from constants import CREATE_NEW_CONSOLE, C, CREATE_NO_WINDOW, _ANSI
from helpers import housekeeping
from helpers.runtime import log
from helpers.tokensave_config import (
    read_strict_tree,
    should_recommend_enabling,
    wrong_graph_risk,
)
from helpers.worktree_health import (
    find_orphaned_worktrees_for_project,
    repair_worktree_index,
)

import time

if TYPE_CHECKING:
    from state import ManagerConfig


# ── Doctor subprocess contract ────────────────────────────────────────────────
# ── Doctor subprocess contract ────────────────────────────────────────────────
# Invoking `tokensave doctor`, parsing it, and judging a purge all moved to
# helpers/doctor_service.py so the headless CLI can reach them — this module
# imports Tk at scope. The controller is now an adapter over that service, in
# the same relationship it already has with helpers/doctor_rules.
from helpers.doctor_service import (
    DoctorScanResult,
    PURGE_EXPLANATIONS,
    PurgeResult,
    VERIFY_VERIFIED,
    extract_install_nags,
)
from helpers import doctor_service


# ── Monolith-audit constants (canonical thresholds — see BASIC_INSTRUCTIONS Rule A) ──

# The audit rules moved to helpers/doctor_rules.py so they can run without
# Tk, which this module imports at scope. Only the entry point is needed here;
# other callers import from helpers.doctor_rules directly.
from helpers.doctor_rules import (                      # noqa: E402
    _audit_project_tree,
    audit_graph_trust,
    audit_shadow_links,
)


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

    def _report_strict_tree(self, path: str, risk_present: bool) -> None:
        """Log the project's strict_tree state (upstream #372 section 2).

        Quiet by design. A malformed config is always worth a warning, and an
        unreadable one is reported as unknown — never as "off", which would be
        asserting a fact we do not have. But a merely disabled setting only
        earns a line when *risk_present* says the wrong-tree failure is
        actually reachable here, because upstream ships it off deliberately
        and a per-run reminder on every project is noise.
        """
        state = read_strict_tree(path)
        if state.is_defect:
            self._on_log(f"  ⚠ tokensave strict_tree: {state.detail}",
                         C["peach"])
        elif not state.is_known:
            self._on_log(f"  ? tokensave strict_tree: {state.detail}",
                         C["overlay0"])
        elif should_recommend_enabling(state, risk_present):
            self._on_log(
                "  ⚠ tokensave strict_tree is off, and the wrong-graph "
                "failure is reachable here — a query answered from another "
                "project's index, or another checkout's, reads as perfectly "
                "normal. Right-click the project \u2192 \U0001f5c2 Index \u2192 "
                "\u201c\U0001f6e1 Enable strict_tree\u2026\u201d to make those calls "
                "fail with both roots named instead.",
                C["peach"])

    def _report_project_binding(self, path: str) -> None:
        """Whether this project binds its own Claude Code MCP server.

        Three states, and only one of them earns advice:

          bound             silent
          unbound           suggest binding, but ONLY when there is more than
                            one indexed project -- with one there is nothing to
                            be wrong about, the same evidence-not-nagging rule
                            `should_recommend_enabling` documents
          bound + shadowed  a DIFFERENT diagnostic. The .mcp.json is correct and
                            something higher-precedence is serving anyway, so
                            "fix the binding" would be useless advice

        Deliberately never says the user-scoped entry is broken. It is
        upstream's canonical shape and it resolves correctly much of the time --
        measured, not assumed. The honest word is "unbound", not "wrong".
        """
        from helpers.mcp import _classify_mcp_entry, _project_mcp_path

        try:
            info = _classify_mcp_entry(_project_mcp_path(path), self._cfg.raw)
        except Exception:                                    # noqa: BLE001
            return

        state = info.get("state")
        if state == "project_mismatch":
            # Worth saying regardless of project count: this file actively
            # points somewhere else, and every answer here comes from another
            # codebase looking entirely normal.
            self._on_log("  \u26a0 " + info.get("issue", "").split(" Apply")[0],
                         C["peach"])
            return

        if state == "ok":
            self._report_binding_is_effective(path)
            return

        if not self._several_projects():
            return
        self._on_log(
            "  \u2139 no project MCP binding \u2014 Claude Code sessions here fall "
            "back to the user-scoped tokensave entry, which resolves by "
            "searching upward from wherever the session started. Right-click "
            "the project \u2192 \U0001f5c2 Index \u2192 \u201c\U0001f50c Bind to this "
            "project\u2026\u201d to make it explicit.",
            C["overlay0"])

    def _report_binding_is_effective(self, path: str) -> None:
        """A binding exists on disk. Is it the one Claude Code actually uses?

        Asked rather than derived: Claude Code resolves local > project > user
        and dedupes by server name, so a correct `.mcp.json` can still be
        overridden. Computing that here would eventually report "bound" while
        something else serves -- exactly the class of confident-wrong answer
        this whole effort is about.

        Costs a CLI call, so it runs only for projects already known to be
        bound; unbound ones are decided from the file alone.
        """
        from helpers.mcp import (APPROVAL_APPROVED, effective_scope,
                                 mcpjson_approval)

        try:
            got = effective_scope(path)
        except Exception:                                    # noqa: BLE001
            return
        if not got.is_known or got.is_project:
            return                       # correct, or we could not tell
        # `claude mcp get` does not read .claude/settings.local.json, so its
        # "pending approval" is a false negative for any project approved
        # there. Checked against the cheap reader before it is repeated.
        try:
            approved = mcpjson_approval(path).state == APPROVAL_APPROVED
        except Exception:                                    # noqa: BLE001
            approved = False
        if got.pending_approval and approved:
            return
        if got.pending_approval:
            self._on_log(
                "  \u23f8 project MCP binding written but not yet approved \u2014 "
                "run `claude` in this project once and approve it.",
                C["overlay0"])
            return
        if got.is_shadowed:
            self._on_log(
                "  \u26a0 this project has a tokensave binding, but Claude Code "
                "is serving the %s-scoped definition instead (it takes "
                "precedence). Editing .mcp.json will not change which server "
                "runs." % got.scope,
                C["peach"])

    def _desktop_servers(self) -> list:
        """Running tokensave servers, memoised for one doctor pass.

        Enumeration shells out to PowerShell/CIM, so it is cached rather than
        repeated per report. The TTL is short because the whole point of the
        check is that the answer changes when Desktop restarts a server.
        """
        import time as _time
        now = _time.time()
        cached = getattr(self, "_srv_cache", None)
        if cached and now - cached[0] < 30:
            return cached[1]
        try:
            from helpers.project_discovery import find_projects
            from helpers.tokensave_daemon import list_tokensave_servers
            projects = [p["path"]
                        for p in find_projects(self._cfg.search_roots)]
            servers = list_tokensave_servers(
                tokensave_exe=self._cfg.tokensave_exe,
                known_projects=projects)
        except Exception:                                    # noqa: BLE001
            servers = []
        self._srv_cache = (now, servers)
        return servers

    def _report_desktop_shadow(self, path: str) -> None:
        """Is Claude Desktop's global tokensave answering for this project?

        The check every other binding report cannot make. `claude mcp get`
        reads `~/.claude.json` and never `claude_desktop_config.json`, so a
        Desktop-registered `tokensave` is invisible to
        `_report_binding_is_effective` above — which is precisely how a
        session in this repo spent four queries believing its index was stale
        while being answered from another project's graph.

        Speaks only for the states the user can act on. A Desktop server that
        happens to be serving THIS project is correct for this project and
        gets silence; so does a dormant entry, because Desktop is closed and
        there is nothing to do. Reporting either would make this the next
        over-eager doctor warning, which this doctor has already had to be
        fixed for once.
        """
        from helpers import mcp_desktop
        from helpers.mcp_shadow import (SHADOW_ACTIVE, SHADOW_UNCERTAIN,
                                        classify_shadow)

        try:
            configs = mcp_desktop.discover_desktop_configs()
            present = mcp_desktop.desktop_entry_present(configs=configs)
            retired = bool(self._cfg.raw.get(
                mcp_desktop.DESKTOP_SCOPE_RETIRED_KEY))
        except Exception:                                    # noqa: BLE001
            return

        if mcp_desktop.lifecycle_state(present, retired) == \
                mcp_desktop.LIFECYCLE_RETURNED:
            self._on_log(
                "  ⚠ Claude Desktop's tokensave entry has come back after "
                "you retired it — an app update or a hand edit can do that. "
                "Until it is retired again it wins the `tokensave` name over "
                "every project binding on this machine.", C["peach"])

        if not present:
            return

        verdict = classify_shadow(path, desktop_entry_present=True,
                                  servers=self._desktop_servers())

        if verdict.state == SHADOW_ACTIVE:
            self._on_log(
                "  ⚠ this project's binding is correct, but Claude "
                "Desktop runs its own tokensave server and that one wins the "
                "name. It is serving %s (PID %s), so questions asked here are "
                "answered from that tree. Settings → MCP Integration → "
                "“Retire Desktop tokensave…”."
                % (verdict.served_project, verdict.pid), C["peach"])
        elif verdict.state == SHADOW_UNCERTAIN:
            self._on_log(
                "  ? Claude Desktop is running a tokensave server, but which "
                "project it serves could not be established — so whether "
                "this binding is being shadowed is unknown.", C["overlay0"])

    def _several_projects(self) -> bool:
        """More than one indexed project, read defensively."""
        try:
            from helpers.project_discovery import find_projects
            return len(find_projects(self._cfg.search_roots)) > 1
        except Exception:                                    # noqa: BLE001
            return False

    def _wrong_graph_risk(self) -> bool:
        """Several indexed projects, and a Desktop pinned to exactly one.

        Read defensively: this only decides whether to print one advisory
        line, so a missing Claude config or an unreadable search root must
        degrade to "no evidence" rather than take the Doctor down with it.
        """
        try:
            from helpers.mcp import _classify_mcp_entry, _mcp_configs
            from helpers.project_discovery import find_projects
            wired = any(
                _classify_mcp_entry(p, self._cfg.raw)["state"] == "ok"
                for _label, p in _mcp_configs())
            count = len(find_projects(self._cfg.search_roots))
        except Exception:                                   # noqa: BLE001
            return False
        return wrong_graph_risk(count, wired)

    def _analyse_doctor_output(self, path: str, output_lines: list,
                               returncode: int) -> None:
        """Parse doctor's output, check worktree health, schedule follow-ups.

        Runs on the doctor worker thread; the only UI work it does directly
        is thread-safe ``_on_log`` calls. Dialogs go through a single
        ``after(0, _offer_followups, …)`` dispatch.
        """
        stale = [e.path for e in housekeeping.parse_stale_entries(output_lines)]
        nagged, other_n = extract_install_nags(output_lines)
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
        self._report_strict_tree(
            path, risk_present=bool(orphans) or self._wrong_graph_risk())
        self._report_project_binding(path)
        self._report_desktop_shadow(path)
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
        """Observe stale state without touching it. Blocking. See the service."""
        return doctor_service.scan_stale(
            path, self._cfg.tokensave_exe, timeout=timeout)

    def purge_stale(self, path: str,
                    baseline: "list | None" = None) -> PurgeResult:
        """Begin a purge; it finishes in a terminal the user drives.

        A ``handed_off`` return is NOT a failure — see the service.
        """
        return doctor_service.purge_stale(
            path, self._cfg.tokensave_exe, baseline=baseline)

    def verify_purge(self, path: str, stale_before: list) -> PurgeResult:
        """Re-scan and report what actually happened. The authoritative step."""
        return doctor_service.verify_purge(
            path, self._cfg.tokensave_exe, stale_before)

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
                         creationflags=CREATE_NEW_CONSOLE)

    @staticmethod
    def verification_label(result: PurgeResult) -> str:
        """Human sentence for a verification outcome."""
        return doctor_service.verification_label(result)

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
                f"  ⚠ {PURGE_EXPLANATIONS.get(result.status, result.status)}",
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
        self._log_shadow_health(project_path)
        self._log_graph_trust(project_path)

    def _log_shadow_health(self, project_path: str) -> None:
        """Warn-only, and silent for projects that do not use shadow links.

        Not a cap violation and deliberately not counted as one: a stale
        shadow is a fact about the working tree, not a code-health defect,
        and folding it into the violation count would move a number that
        other things are measured against.
        """
        notes = audit_shadow_links(project_path)
        if not notes:
            return
        self._on_log("═══ Shadow links ═══", C["mauve"])
        for note in notes:
            self._on_log(note, C["peach"])

    def _log_graph_trust(self, project_path: str) -> None:
        """Warn-only. Silent when the graph is sound, loud when it is not.

        Reports an index defect, never a source defect, so it does not touch
        the violation count and cannot block a push.
        """
        notes = audit_graph_trust(project_path)
        if not notes:
            return
        self._on_log("=== Graph trust ===", C["mauve"])
        for note in notes:
            self._on_log(note, C["peach"])

    def _log_audit_results(
        self,
        violations: list,
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
            for violation in violations:
                # `Violation.__str__` reproduces the pre-R12-10 text
                # exactly, so this log is byte-identical to before.
                self._on_log(str(violation), C["peach"])

        for note in exempt_notes:
            self._on_log(note, C["overlay0"])

    # Output parsing lives in `helpers.housekeeping.parse_stale_entries` — the
    # single canonical parser for doctor's stale block. A local copy used to
    # live here; two parsers for one output section is exactly how they drift.


# ── Module-level audit helpers (AST-based; canonical semantics — Rule A) ──
