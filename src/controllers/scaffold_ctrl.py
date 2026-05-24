"""ScaffoldRetrofitController — scaffold / retrofit commands for the Projects tab.

Extracted from ProjectsTabController (Round 5).

Dependency contract:
  • tab             — the Projects tk.Frame (after() + winfo_toplevel())
  • cfg             — read-only ManagerConfig (.tokensave_exe, .template_dir,
                      .basic_instructions_template, .baseline_include_line)
  • on_log          — thread-safe log callback  (msg: str, colour: str = "")
  • on_set_running  — (running: bool, label: str) -> None
  • on_set_proc     — (proc_or_none) -> None
  • on_refresh      — () -> None
  • on_commit_offer — (path: str, label: str) -> None
  • on_insert_pending — (path: str, name: str) -> None  (adds pending row to tree)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Callable

import tkinter as tk

from constants import C, CREATE_NO_WINDOW, _ANSI
from helpers.project_discovery import load_basic_instructions_template
from helpers.runtime import log
from helpers.scaffold import _scaffold_git_hook
from helpers.shadow_links import (
    DEFAULT_SHADOW_EXT_MAP,
    generate_shadow_links,
    update_gitignore_for_shadows,
)
from dialogs.retrofit import RetrofitDialog
from dialogs.scaffold import ScaffoldDialog

if TYPE_CHECKING:
    from state import ManagerConfig


class ScaffoldRetrofitController:
    """Handles scaffold-new-project and retrofit-existing-project commands."""

    def __init__(
        self,
        tab: tk.Frame,
        cfg: "ManagerConfig",
        on_log: Callable,
        on_set_running: Callable[[bool, str], None],
        on_set_proc: Callable[[object], None],
        on_refresh: Callable[[], None],
        on_commit_offer: Callable[[str, str], None],
        on_insert_pending: Callable[[str, str], None],
    ) -> None:
        self._tab              = tab
        self._cfg              = cfg
        self._on_log           = on_log
        self._on_set_running   = on_set_running
        self._on_set_proc      = on_set_proc
        self._on_refresh       = on_refresh
        self._on_commit_offer  = on_commit_offer
        self._on_insert_pending = on_insert_pending

    @property
    def _root(self) -> tk.Tk:
        return self._tab.winfo_toplevel()

    # ── Public commands ───────────────────────────────────────────────────────

    def cmd_scaffold(self) -> None:
        folder = filedialog.askdirectory(title="Select folder to scaffold",
                                         parent=self._root)
        if not folder:
            return
        ScaffoldDialog(self._root, folder, self._scaffold_project)

    def cmd_retrofit(self) -> None:
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self._root)
        if not folder:
            return
        RetrofitDialog(self._root, folder, self._do_retrofit)

    def cmd_retrofit_selected(self, path: str) -> None:
        RetrofitDialog(self._root, path, self._do_retrofit)

    # ── Scaffold internals ────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path: str) -> list[str]:
        """Copy Nuitka build templates into path.  Returns list of action strings."""
        name     = os.path.basename(path)
        src_ps1  = os.path.join(self._cfg.template_dir, "nuitka-build.ps1.template")
        src_bat  = os.path.join(self._cfg.template_dir, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._on_log("  [WARN] Nuitka templates not found in template directory — skipped",
                         C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {self._cfg.template_dir}")
            return actions

        if os.path.isfile(dst_ps1):
            self._on_log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._on_log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._on_log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        if os.path.isfile(dst_bat):
            self._on_log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._on_log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._on_log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._on_log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])
        return actions

    def _scaffold_project(self, path: str, create_bi: bool = True,
                          run_init: bool = True, scaffold_nuitka: bool = False,
                          add_git_hook: bool = False) -> None:
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template(
                    self._cfg.basic_instructions_template, self._cfg.baseline_include_line)
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._on_log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._on_log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._on_log(f"  {action}", C["green"])

        if run_init:
            self._on_insert_pending(path, name)
            self._on_log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self._tab.after(0, self._on_set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [self._cfg.tokensave_exe, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._on_set_proc(proc)
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._on_log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._on_log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._on_log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._on_log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self._on_set_proc(None)
                    self._tab.after(0, self._on_set_running, False, "")
                    self._tab.after(0, self._on_refresh)
                    self._tab.after(0, lambda: self._on_commit_offer(path, "scaffold files"))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self._on_refresh()
            self._on_commit_offer(path, "scaffold files")

    # ── Retrofit internals ────────────────────────────────────────────────────

    def _do_retrofit(self, path: str, add_tokensave: bool,
                     add_basic_instructions: bool,
                     add_nuitka: bool = False,
                     add_shadow_links: bool = False,
                     shadow_ext_map: dict | None = None,
                     add_git_hook: bool = False) -> None:
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)
        flags = {
            "tokensave":          add_tokensave,
            "basic_instructions": add_basic_instructions,
            "nuitka":             add_nuitka,
            "shadow_links":       add_shadow_links,
            "shadow_ext_map":     shadow_ext_map or DEFAULT_SHADOW_EXT_MAP,
            "git_hook":           add_git_hook,
        }

        def worker():
            try:
                self._log_retrofit_start(path, name, flags)
                actions = self._run_retrofit_steps(path, name, flags)
                self._retrofit_finish(path, name, actions)
            except Exception as e:
                self._retrofit_error(path, e)

        threading.Thread(target=worker, daemon=True).start()

    # ── _do_retrofit helpers ──────────────────────────────────────────────────

    def _log_retrofit_start(self, path: str, name: str, flags: dict) -> None:
        log.info(f"RETROFIT {path}  ts={flags['tokensave']} "
                 f"bi={flags['basic_instructions']} nuitka={flags['nuitka']}")
        self._on_log(f"Retrofitting {name}…", C["peach"])

    def _run_retrofit_steps(self, path: str, name: str, flags: dict) -> list[str]:
        """Run each enabled retrofit step in order. Returns combined action list."""
        actions: list[str] = []
        if flags["tokensave"]:
            actions.extend(self._retrofit_add_tokensave(path, name))
        if flags["basic_instructions"]:
            actions.extend(self._retrofit_add_basic_instructions(path))
        if flags["nuitka"]:
            actions.extend(self._scaffold_nuitka_build(path))
        if flags["shadow_links"]:
            actions.extend(self._retrofit_add_shadow_links(path, flags["shadow_ext_map"]))
        if flags["git_hook"]:
            hook_actions = _scaffold_git_hook(path)
            for action in hook_actions:
                self._on_log(f"  {action}", C["green"])
            actions.extend(hook_actions)
        return actions

    def _retrofit_finish(self, path: str, name: str, actions: list[str]) -> None:
        """Log completion, refresh, show summary messagebox, and offer commit."""
        log.info(f"RETROFIT complete: {actions or 'nothing changed'}")
        self._on_log(f"Retrofit complete: {path}", C["green"])
        self._tab.after(0, self._on_refresh)

        msg = self._build_retrofit_summary(name, actions)
        self._tab.after(0, lambda: messagebox.showinfo(
            "Retrofit complete", msg, parent=self._root))
        if actions:
            self._tab.after(0, lambda: self._on_commit_offer(path, "retrofit additions"))

    @staticmethod
    def _build_retrofit_summary(name: str, actions: list[str]) -> str:
        """Build the user-visible summary string from the actions list."""
        if not actions:
            return f"{name}:\n\n  Everything was already up to date — nothing changed."
        summary = "\n".join(f"  ✔ {a}" for a in actions)
        msg = f"{name}:\n\n{summary}"
        if any(a.startswith("Created build.ps1") for a in actions):
            msg += ("\n\nNext step: open build.ps1 and replace "
                    "[ENTRY_SCRIPT] and [OUTPUT_NAME] before building.")
        return msg

    def _retrofit_error(self, path: str, e: Exception) -> None:
        """Handle worker exceptions — log + error messagebox on main thread."""
        log.exception(f"RETROFIT failed: {path}")
        self._on_log(f"  Error: {e}", C["red"])
        self._tab.after(0, lambda: messagebox.showerror(
            "Retrofit failed", str(e), parent=self._root))

    def _retrofit_add_tokensave(self, path: str, name: str) -> list[str]:
        """Prepend the @include line to CLAUDE.md (or create it). Returns actions taken."""
        claude_md = os.path.join(path, "CLAUDE.md")
        include_line = self._cfg.baseline_include_line
        if os.path.isfile(claude_md):
            content = open(claude_md, encoding="utf-8", errors="ignore").read()
            if "project-baseline.md" in content:
                log.info("  CLAUDE.md already has @include — skipped")
                self._on_log("  Tokensave already integrated in CLAUDE.md — skipped", C["overlay0"])
                return []
            with open(claude_md, "r+", encoding="utf-8") as f:
                existing = f.read()
                f.seek(0)
                f.write(include_line + "\n\n" + existing)
            log.info("  prepended @include to CLAUDE.md")
            self._on_log("  Added tokensave @include to CLAUDE.md", C["green"])
            return ["Added tokensave rules to CLAUDE.md"]
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(f"# {name} — Claude Instructions\n\n{include_line}\n")
        log.info("  created CLAUDE.md with @include")
        self._on_log("  Created CLAUDE.md with tokensave @include", C["green"])
        return ["Created CLAUDE.md with tokensave rules"]

    def _retrofit_add_basic_instructions(self, path: str) -> list[str]:
        """Write BASIC_INSTRUCTIONS.md from the template. Returns actions taken."""
        basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
        if os.path.isfile(basic_md):
            log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
            self._on_log("  BASIC_INSTRUCTIONS.md already exists — skipped", C["overlay0"])
            return []
        template = load_basic_instructions_template(
            self._cfg.basic_instructions_template, self._cfg.baseline_include_line)
        with open(basic_md, "w", encoding="utf-8") as f:
            f.write(template)
        log.info("  created BASIC_INSTRUCTIONS.md")
        self._on_log("  Created BASIC_INSTRUCTIONS.md", C["green"])
        return ["Created BASIC_INSTRUCTIONS.md"]

    def _retrofit_add_shadow_links(self, path: str, ext_map: dict) -> list[str]:
        """Generate shadow extension links and update .gitignore. Returns actions taken."""
        self._on_log("  Generating shadow extension links…", C["peach"])
        created, skipped, failed = generate_shadow_links(path, ext_map)
        update_gitignore_for_shadows(path, ext_map)
        msg = f"Shadow links: created {created}"
        if skipped:
            msg += f", {skipped} already existed"
        if failed:
            msg += f", {failed} failed"
        self._on_log(f"  {msg}", C["green"])
        log.info(f"  {msg}")
        return [msg] if created > 0 else []
