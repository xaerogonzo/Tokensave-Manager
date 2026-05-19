"""
TokenSave Manager
A GUI for managing tokensave projects and controlling which project
Claude Desktop uses via the wrapper script.
"""

import os
import re
import json
import shlex
import shutil
import subprocess
import threading
import time
import logging
import logging.handlers
import ctypes
import sys
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import math
import pystray
from PIL import Image, ImageDraw

_ANSI = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])')

# ── Paths ─────────────────────────────────────────────────────────────────────
# Under Nuitka --onefile, NUITKA_ONEFILE_PARENT is the actual .exe path.
# In dev mode, the script lives in src/ so go up one level.

if os.environ.get("NUITKA_ONEFILE_PARENT"):
    _BASE_DIR = os.path.dirname(os.path.abspath(os.environ["NUITKA_ONEFILE_PARENT"]))
else:
    _BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_CONFIG_PATH = os.path.join(_BASE_DIR, "manager-config.json")
LOG_DIR      = os.path.join(_BASE_DIR, "logs")
LOG_FILE     = os.path.join(LOG_DIR, "manager.log")

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM if present
        return json.load(f)

def _save_config(cfg: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

_cfg = _load_config()

TOKENSAVE    = _cfg.get("tokensave_exe", "")
TEMPLATE_DIR = _cfg.get("template_dir", "") or os.path.join(_BASE_DIR, "templates")
SEARCH_ROOTS = _cfg.get("search_roots", [])


# ── Search-root helpers (support both legacy str and new {"path":…,"label":…} format) ──

def _root_path(r):
    """Return the directory path from a search-root entry (str or dict)."""
    return r if isinstance(r, str) else r["path"]

def _root_label(r):
    """Return the display label for a search-root entry."""
    p = _root_path(r)
    if isinstance(r, str):
        return os.path.basename(p.rstrip("/\\"))
    return r.get("label", os.path.basename(p.rstrip("/\\"))) or os.path.basename(p.rstrip("/\\"))

BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"

DESKTOP_PROJECT_FILE = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".tokensave", "desktop-project.txt",
)
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "target", "build", "dist", "out", ".gradle", "bin", "obj",
}
MAX_DEPTH = 4
CREATE_NO_WINDOW = 0x08000000
AUTO_REFRESH_MS = 60_000  # auto-refresh project list every 60 s

# ── Shadow-link helpers ────────────────────────────────────────────────────────

# Default extension map: ZScript → C++, ACS → C, DECORATE lump → C++.
# Keys starting with '.' are matched against the file extension
#   (e.g. ".zsc" matches "Blood.zsc" → shadow "Blood.zsc.cpp").
# Keys WITHOUT a leading dot are matched by exact filename, case-insensitive
#   (e.g. "DECORATE" matches the extensionless lump → shadow "DECORATE.cpp").
DEFAULT_SHADOW_EXT_MAP = {
    ".zs":  ".cpp",
    ".zsc": ".cpp",
    ".acs": ".c",
    "DECORATE": ".cpp",   # extensionless lump — matched by exact filename
}

_SHADOW_SKIP_DIRS = {".tokensave", ".git", "node_modules", "__pycache__",
                     ".venv", "venv", "target", "build", "dist", "out"}


def generate_shadow_links(path: str, ext_map: dict) -> tuple:
    """
    Walk *path* and create NTFS hardlinks so tokensave can index
    non-standard extensions via an existing tree-sitter grammar.

    Two matching modes, determined by key format:
    - Dot-prefixed keys (".zsc") match by file extension → Blood.zsc → Blood.zsc.cpp
    - Non-dot keys ("DECORATE") match by exact filename, case-insensitive →
      DECORATE → DECORATE.cpp  (handles extensionless Doom lumps)

    Existing shadow files are left untouched.
    Returns (created, skipped, failed) counts.
    """
    created = skipped = failed = 0
    ext_keys  = {k: v for k, v in ext_map.items() if k.startswith(".")}
    name_keys = {k.upper(): v for k, v in ext_map.items() if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext in ext_keys:
                shadow_suffix = ext_keys[ext]
            elif fname.upper() in name_keys:
                shadow_suffix = name_keys[fname.upper()]
            else:
                continue
            src = os.path.join(root, fname)
            dst = src + shadow_suffix
            if os.path.exists(dst):
                skipped += 1
            else:
                try:
                    os.link(src, dst)
                    created += 1
                except OSError:
                    failed += 1
    return created, skipped, failed


def remove_shadow_links(path: str, ext_map: dict) -> int:
    """Delete all shadow hardlink files created by generate_shadow_links."""
    removed = 0
    suffixes  = set(ext_map.values())
    src_exts  = {k for k in ext_map if k.startswith(".")}
    src_names = {k.upper() for k in ext_map if not k.startswith(".")}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            for suf in suffixes:
                if fname.endswith(suf):
                    base = fname[:-len(suf)]
                    if (any(base.endswith(e) for e in src_exts) or
                            base.upper() in src_names):
                        try:
                            os.remove(os.path.join(root, fname))
                            removed += 1
                        except OSError:
                            pass
    return removed


def update_gitignore_for_shadows(path: str, ext_map: dict):
    """
    Append shadow-file patterns to .gitignore (if not already present).
    Creates .gitignore if it doesn't exist.
    Extension-based entries use a glob (*.zsc.cpp); exact-name entries use
    a literal filename (DECORATE.cpp) — no leading wildcard.
    """
    gi_path = os.path.join(path, ".gitignore")
    patterns = []
    for key, val in ext_map.items():
        if key.startswith("."):
            patterns.append(f"*{key}{val}")   # glob:  *.zsc.cpp
        else:
            patterns.append(f"{key}{val}")    # exact: DECORATE.cpp
    try:
        existing = open(gi_path, encoding="utf-8", errors="ignore").read() \
                   if os.path.isfile(gi_path) else ""
        to_add = [p for p in patterns if p not in existing]
        if to_add:
            header = "\n# tokensave shadow extension hardlinks\n"
            with open(gi_path, "a", encoding="utf-8") as f:
                f.write(header + "\n".join(to_add) + "\n")
    except OSError:
        pass


def _is_git_repo(path: str) -> bool:
    """Return True if *path* is inside an initialised git repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False


# Baseline .gitignore written by cmd_git_init when none exists yet.
_BASELINE_GITIGNORE = """\
# Python
__pycache__/
*.pyc
*.pyo

# Nuitka build output
*.onefile-build/
*.build/
dist/

# tokensave index (machine-specific binary)
.tokensave/

# Virtual environments
.venv/
venv/

# OS
Thumbs.db
.DS_Store
"""

# Stop hook injected into .claude/settings.json by _scaffold_git_hook.
# Commits whatever Claude changed at session end — skips if working tree is clean.
_STOP_HOOK_CMD = (
    'git add -A && git diff --cached --quiet || '
    'git commit -m "auto: Claude session"'
)


def _scaffold_git_hook(path: str) -> list:
    """Write/merge a Claude Code Stop hook into .claude/settings.json.

    Creates .claude/ if it doesn't exist. Merges non-destructively — reads
    existing JSON and only appends when our hook isn't already present.
    Returns a list of human-readable action strings (for retrofit summaries).
    """
    settings_dir  = os.path.join(path, ".claude")
    settings_path = os.path.join(settings_dir, "settings.json")
    try:
        os.makedirs(settings_dir, exist_ok=True)
    except OSError:
        return ["Could not create .claude/ directory"]

    existing = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, encoding="utf-8-sig") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # treat as empty rather than failing

    hooks = existing.setdefault("hooks", {})
    stop  = hooks.setdefault("Stop", [])

    # Idempotent: skip if any Stop hook command already starts with "git add -A"
    already = any(
        e.get("type") == "command" and e.get("command", "").startswith("git add -A")
        for entry in stop for e in entry.get("hooks", [])
    )
    if already:
        return ["Auto-commit Stop hook already present — skipped"]

    stop.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": _STOP_HOOK_CMD}],
    })
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return ["Created/updated .claude/settings.json — Stop hook added"]
    except OSError as exc:
        return [f"Could not write .claude/settings.json: {exc}"]


def _setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=500_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger = logging.getLogger("tsm")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger

log = _setup_logger()

# ── Prompt snippets ────────────────────────────────────────────────────────────

PROMPT_SNIPPETS = [
    (
        "Codebase overview",
        "Give me a high-level overview of this project using tokensave_context. "
        "What are the main components, how do they relate, and what is the entry point?"
    ),
    (
        "Find a symbol",
        "Use tokensave_search to find [symbol name]. Then use tokensave_context "
        "to explain what it does, what it calls, and what calls it."
    ),
    (
        "What calls this function?",
        "Use tokensave_callers to find everything that calls [function name]. "
        "Show me the full call chain."
    ),
    (
        "Impact of changing X",
        "Use tokensave_impact to analyze what would be affected if I modify "
        "[function or class name]. Show me the full impact chain."
    ),
    (
        "Code health check",
        "Run tokensave_health, tokensave_complexity, and tokensave_god_class. "
        "Give me a health report — flag god classes, high complexity, and circular dependencies."
    ),
    (
        "Find dead code",
        "Use tokensave_dead_code and tokensave_unused_imports to find any "
        "unused code or imports in this project. List them with file locations."
    ),
    (
        "List all TODOs",
        "Use tokensave_todos to list all TODO and FIXME comments in this project. "
        "Group them by file."
    ),
    (
        "Generate changelog",
        "Use tokensave_changelog to generate a changelog based on recent commits. "
        "Format it as a proper CHANGELOG.md entry."
    ),
    (
        "Module public API",
        "Use tokensave_module_api to show me the public API of [module or file name]. "
        "What does it export and how is it meant to be used?"
    ),
    (
        "Circular dependencies",
        "Use tokensave_circular to find any circular dependencies in this project. "
        "Explain how each one could be resolved."
    ),
    (
        "Largest / most complex files",
        "Use tokensave_largest and tokensave_complexity to find the biggest and most "
        "complex files. Which ones are the best candidates for refactoring?"
    ),
    (
        "Refactor rename preview",
        "Use tokensave_rename_preview to show what would change if I rename "
        "[old name] to [new name]. List every affected file and line."
    ),
]

# ── Colours (Catppuccin Mocha) ─────────────────────────────────────────────────

C = {
    "base":     "#1e1e2e",
    "mantle":   "#181825",
    "crust":    "#11111b",
    "surface0": "#313244",
    "surface1": "#45475a",
    "overlay0": "#6c7086",
    "text":     "#cdd6f4",
    "subtext":  "#bac2de",
    "blue":     "#89b4fa",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "red":      "#f38ba8",
    "lavender": "#b4befe",
    "sky":      "#89dceb",
    "peach":    "#fab387",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_projects():
    projects = []
    for root in SEARCH_ROOTS:
        rpath  = _root_path(root)
        rlabel = _root_label(root)
        if not os.path.isdir(rpath):
            continue
        for dirpath, dirnames, _ in os.walk(rpath):
            rel = os.path.relpath(dirpath, rpath)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= MAX_DEPTH:
                dirnames.clear()
                continue
            has_ts = ".tokensave" in dirnames
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if has_ts:
                db = os.path.join(dirpath, ".tokensave", "tokensave.db")
                if os.path.isfile(db):
                    projects.append({
                        "path":       dirpath,
                        "name":       os.path.basename(dirpath),
                        "db":         db,
                        "mtime":      os.path.getmtime(db),
                        "root_label": rlabel,
                    })
    return sorted(projects, key=lambda p: p["mtime"], reverse=True)


def get_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        p = open(DESKTOP_PROJECT_FILE, encoding="utf-8").read().strip()
        if p and os.path.isfile(os.path.join(p, ".tokensave", "tokensave.db")):
            return p
    return None


def set_pinned(path):
    os.makedirs(os.path.dirname(DESKTOP_PROJECT_FILE), exist_ok=True)
    with open(DESKTOP_PROJECT_FILE, "w", encoding="utf-8") as f:
        f.write(path)


def clear_pinned():
    if os.path.isfile(DESKTOP_PROJECT_FILE):
        os.remove(DESKTOP_PROJECT_FILE)


def fmt_age(mtime):
    diff = datetime.now().timestamp() - mtime
    if diff < 60:         return "just now"
    if diff < 3600:       return f"{int(diff / 60)}m ago"
    if diff < 86400:      return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 7:  return f"{int(diff / 86400)}d ago"
    return datetime.fromtimestamp(mtime).strftime("%b %d")


def load_basic_instructions_template():
    """Load the BASIC_INSTRUCTIONS.md template text, or return a minimal fallback."""
    if os.path.isfile(BASIC_INSTRUCTIONS_TEMPLATE):
        raw = open(BASIC_INSTRUCTIONS_TEMPLATE, encoding="utf-8").read()
        # Replace any @<path>/project-baseline.md line with the current computed path
        # so the written BASIC_INSTRUCTIONS.md always points to the right location.
        # Use a lambda so BASELINE_INCLUDE_LINE is never parsed as a regex
        # replacement string — Windows paths contain backslashes that re.sub
        # would misinterpret as escape sequences (e.g. \p → bad escape error).
        raw = re.sub(r"^@[^\n]*project-baseline\.md",
                     lambda _: BASELINE_INCLUDE_LINE, raw, flags=re.MULTILINE)
        return raw
    # Minimal inline fallback if template file is missing
    return (
        "# [PROJECT NAME] — Basic Instructions\n\n"
        "<!-- CLAUDE: Replace all [PLACEHOLDER] sections on first use. -->\n\n"
        f"{BASELINE_INCLUDE_LINE}\n\n"
        "---\n\n"
        "## Project Overview\n\n"
        "**Name:** [PROJECT NAME]\n"
        "**Stack:** [Languages and frameworks]\n"
        "**Entry point:** [Main file or command]\n"
        "**Purpose:** [One sentence]\n\n"
        "---\n\n"
        "## Architecture\n\n[Replace with high-level structure.]\n\n"
        "---\n\n"
        "## Key Files\n\n[Replace with important files and their roles.]\n\n"
        "---\n\n"
        "## Project-Specific Rules\n\n[Replace or delete this section.]\n"
    )

# ── Single-instance ───────────────────────────────────────────────────────────

_MUTEX_NAME = "TokenSaveManager_SingleInstance"
_mutex_handle = None

def _acquire_instance_lock():
    """Return True if this is the first instance, False if another is running."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS

def _bring_existing_to_front():
    """Find the existing window by title and restore it."""
    hwnd = ctypes.windll.user32.FindWindowW(None, "TokenSave Manager")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)

# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_tray_icon():
    """Generate a 64×64 tray icon: dark circle with a white star."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (30, 30, 46, 255)   # Catppuccin Mocha base
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)
    # Simple 5-point star approximation using a polygon
    cx, cy, r_out, r_in = size / 2, size / 2, 26, 11
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=(137, 180, 250, 255))  # Catppuccin blue
    return img

# ── App ────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TokenSave Manager")
        self.geometry("720x560")
        self.minsize(560, 460)
        self.configure(bg=C["base"])
        self._current_proc = None
        self._stop_requested = False
        log.info("=" * 60)
        log.info("TokenSave Manager started")
        log.info(f"  exe      : {TOKENSAVE}")
        log.info(f"  templates: {TEMPLATE_DIR}")
        log.info(f"  log file : {LOG_FILE}")
        self._style()
        self._build()
        self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self._tray = None
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.bind("<Unmap>", self._on_unmap)
        self.after(300, self._check_config)

    # ── Tray ───────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "TokenSaveManager",
            _make_tray_icon(),
            "TokenSave Manager",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.withdraw()
        log.debug("Window hidden to tray")

    def _on_unmap(self, event):
        if event.widget is self:
            self.after(100, self._maybe_hide)

    def _maybe_hide(self):
        if self.state() == "iconic":
            self.withdraw()

    def _show_from_tray(self, icon=None, item=None):
        self.after(0, self._do_show)

    def _do_show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self, icon=None, item=None):
        log.info("Quit requested from tray")
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)

    # ── Styles ─────────────────────────────────────────────────────────────────

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".",
            background=C["base"], foreground=C["text"],
            font=("Segoe UI", 10), borderwidth=0)

        s.configure("Treeview",
            background=C["mantle"], foreground=C["text"],
            fieldbackground=C["mantle"], rowheight=30,
            font=("Segoe UI", 10))
        s.configure("Treeview.Heading",
            background=C["surface0"], foreground=C["subtext"],
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", C["surface1"])],
            foreground=[("selected", C["text"])])

        s.configure("TButton",
            background=C["surface0"], foreground=C["text"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("TButton",
            background=[("active", C["surface1"]), ("pressed", C["surface1"])])

        s.configure("Primary.TButton",
            background=C["blue"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Primary.TButton",
            background=[("active", C["lavender"]), ("pressed", C["lavender"])])

        s.configure("Action.TButton",
            background=C["peach"], foreground=C["mantle"],
            padding=(10, 5), font=("Segoe UI", 10, "bold"), relief="flat")
        s.map("Action.TButton",
            background=[("active", C["yellow"]), ("pressed", C["yellow"])])

        s.configure("Danger.TButton",
            background=C["surface0"], foreground=C["red"],
            padding=(10, 5), font=("Segoe UI", 10), relief="flat")
        s.map("Danger.TButton",
            background=[("active", C["surface1"])])

        s.configure("TScrollbar",
            background=C["surface0"], troughcolor=C["mantle"],
            bordercolor=C["base"], arrowcolor=C["overlay0"],
            relief="flat")

        s.configure("TSeparator", background=C["surface0"])

        s.configure("TNotebook",
            background=C["base"], borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab",
            background=C["surface0"], foreground=C["subtext"],
            padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab",
            background=[("selected", C["base"])],
            foreground=[("selected", C["blue"])])

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self, bg=C["mantle"], pady=12, padx=16)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="TokenSave Manager",
                 font=("Segoe UI", 15, "bold"),
                 bg=C["mantle"], fg=C["blue"]).pack(side=tk.LEFT)

        self.active_badge = tk.Label(hdr, text="",
            font=("Segoe UI", 9), bg=C["surface0"],
            fg=C["green"], padx=8, pady=3)
        self.active_badge.pack(side=tk.RIGHT)

        # ── Credit bar ──
        tk.Label(self, text="TokenSave Manager  ·  Alexander L Corthell",
                 font=("Segoe UI", 7), bg=C["crust"], fg=C["overlay0"],
                 pady=2).pack(fill=tk.X, side=tk.BOTTOM)

        # ── Separator + Log — packed BEFORE notebook so expand=True doesn't eat it ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=14, side=tk.BOTTOM)

        log_frame = tk.Frame(self, bg=C["base"], padx=14, pady=8)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # ── Notebook ──
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._build_projects_tab()
        self._build_reference_tab()
        self._build_help_tab()

        log_header = tk.Frame(log_frame, bg=C["base"])
        log_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(log_header, text="OUTPUT",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(log_header, text="View Log",
                   command=self._open_log).pack(side=tk.RIGHT, padx=(0, 6))

        self._stop_btn = ttk.Button(log_header, text="■  Stop",
                                    style="Danger.TButton",
                                    command=self._stop_current,
                                    state=tk.DISABLED)
        self._stop_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._running_label = tk.Label(log_header, text="",
                                       font=("Segoe UI", 8),
                                       bg=C["base"], fg=C["yellow"])
        self._running_label.pack(side=tk.RIGHT, padx=(0, 8))

        log_inner = tk.Frame(log_frame, bg=C["mantle"])
        log_inner.pack(fill=tk.X)

        self.log = tk.Text(log_inner, height=4,
            font=("Consolas", 9), bg=C["mantle"], fg=C["green"],
            insertbackground=C["green"], relief=tk.FLAT,
            padx=10, pady=6, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(log_inner, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_projects_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Projects  ")

        # ── Toolbar + hint packed first with side=BOTTOM so they are always
        #    visible — the treeview (expand=True) fills whatever space remains.
        btns = tk.Frame(tab, bg=C["base"], padx=14, pady=6)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        ttk.Button(btns, text="＋  Scaffold",
                   style="Action.TButton",
                   command=self.cmd_scaffold).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="⚙  Retrofit Existing",
                   command=self.cmd_retrofit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="↺↺  Sync All",
                   command=self.cmd_sync_all).pack(side=tk.LEFT)

        ttk.Button(btns, text="⟳  Refresh",
                   command=self.refresh).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btns, text="Settings",
                   command=self.cmd_settings).pack(side=tk.RIGHT, padx=(0, 6))

        tk.Label(tab, text="Right-click any project for actions",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 ).pack(anchor=tk.E, padx=14, pady=(2, 0), side=tk.BOTTOM)

        # ── Project list — fills remaining space ──
        body = tk.Frame(tab, bg=C["base"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="INDEXED PROJECTS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, pady=(0, 6))

        tree_wrap = tk.Frame(body, bg=C["mantle"])
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("active", "path", "synced", "scaffold"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0",       text="Project")
        self.tree.heading("active",   text="")
        self.tree.heading("path",     text="Path")
        self.tree.heading("synced",   text="Last Synced")
        self.tree.heading("scaffold", text="Scaffold")

        self.tree.column("#0",       width=170, stretch=False)
        self.tree.column("active",   width=28,  stretch=False, anchor=tk.CENTER)
        self.tree.column("path",     width=270)
        self.tree.column("synced",   width=90,  stretch=False, anchor=tk.CENTER)
        self.tree.column("scaffold", width=70,  stretch=False, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("active",      foreground=C["green"])
        self.tree.tag_configure("normal",      foreground=C["text"])
        self.tree.tag_configure("scaffold",    foreground=C["peach"])
        self.tree.tag_configure("pending",     foreground=C["yellow"])
        self.tree.tag_configure("category",    foreground=C["blue"],
                                               font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("subcategory", foreground=C["lavender"])

        self.tree.bind("<Button-3>", self._on_right_click)
        self._build_context_menu()

    def _build_reference_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Reference  ")

        # ── Top: CLI cheatsheet ───────────────────────────────────────────────
        tk.Label(tab, text="CLI COMMANDS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(anchor=tk.W, padx=14, pady=(10, 4))

        cli_wrap = tk.Frame(tab, bg=C["mantle"])
        cli_wrap.pack(fill=tk.X, padx=14)

        cli_sb = ttk.Scrollbar(cli_wrap, orient="vertical")
        cli_txt = tk.Text(cli_wrap, height=9, font=("Consolas", 9),
                          bg=C["mantle"], fg=C["text"], relief=tk.FLAT,
                          padx=12, pady=8, wrap=tk.NONE,
                          cursor="arrow", state=tk.NORMAL,
                          yscrollcommand=cli_sb.set)
        cli_sb.configure(command=cli_txt.yview)
        cli_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        cli_sb.pack(side=tk.RIGHT, fill=tk.Y)

        cli_txt.tag_configure("hd",  font=("Segoe UI", 9, "bold"), foreground=C["blue"],   spacing1=8, spacing3=2)
        cli_txt.tag_configure("cmd", font=("Consolas", 9),          foreground=C["peach"],  spacing3=1)
        cli_txt.tag_configure("dim", font=("Consolas", 9),          foreground=C["overlay0"])

        def cli_row(cmd, desc):
            cli_txt.insert(tk.END, f"  {cmd:<38}", "cmd")
            cli_txt.insert(tk.END, f"{desc}\n", "dim")

        def cli_h(t):
            cli_txt.insert(tk.END, f"\n  {t}\n", "hd")

        cli_h("Daily use")
        cli_row("tokensave sync",               "Incremental re-index (fast)")
        cli_row("tokensave sync --force",        "Full re-index from scratch")
        cli_row("tokensave sync --doctor",       "Sync and list what changed")
        cli_row("tokensave status",              "Stats + estimated token savings")
        cli_row("tokensave status --details",    "Stats with node-kind breakdown")
        cli_row("tokensave files",               "List all indexed files")
        cli_row("tokensave monitor",             "Live TUI of MCP tool calls")
        cli_h("Setup")
        cli_row("tokensave init",                "First-time index of a project")
        cli_row("tokensave install --agent claude", "Wire up Claude Code integration")
        cli_row("tokensave daemon",              "Auto-sync daemon (foreground)")
        cli_row("tokensave daemon --enable-autostart", "Install daemon as a service")
        cli_h("Troubleshooting")
        cli_row("tokensave doctor",              "Health check — diagnose issues")
        cli_row("tokensave upgrade",             "Self-update to latest version")
        cli_h("Cost & token tracking")
        cli_row("tokensave cost",                "7-day cost summary")
        cli_row("tokensave cost today",          "Today's spend only")
        cli_row("tokensave cost --by-model",     "Breakdown by Claude model")
        cli_h("Branches")
        cli_row("tokensave branch add",          "Track current git branch")
        cli_row("tokensave branch list",         "View tracked branches + DB sizes")
        cli_row("tokensave branch gc",           "Clean up deleted branches")

        cli_txt.configure(state=tk.DISABLED)

        # ── Bottom: Claude prompt snippets ────────────────────────────────────
        snippets_header = tk.Frame(tab, bg=C["base"])
        snippets_header.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(snippets_header, text="CLAUDE PROMPT SNIPPETS",
                 font=("Segoe UI", 8, "bold"),
                 bg=C["base"], fg=C["overlay0"]).pack(side=tk.LEFT)

        ttk.Button(snippets_header, text="📖  Open Full Guide",
                   command=self._open_guide).pack(side=tk.RIGHT)

        snippets_frame = tk.Frame(tab, bg=C["base"])
        snippets_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        # Left: listbox of snippet titles
        list_wrap = tk.Frame(snippets_frame, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self.snippet_lb = tk.Listbox(
            list_wrap, width=26, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        list_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                                command=self.snippet_lb.yview)
        self.snippet_lb.configure(yscrollcommand=list_sb.set)
        self.snippet_lb.pack(side=tk.LEFT, fill=tk.Y)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Right: preview + copy + add/edit/delete buttons
        right = tk.Frame(snippets_frame, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        prev_wrap = tk.Frame(right, bg=C["mantle"])
        prev_wrap.pack(fill=tk.BOTH, expand=True)

        self._snippet_preview = tk.Text(
            prev_wrap, font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=10, pady=8, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
        )
        self._snippet_preview.pack(fill=tk.BOTH, expand=True)

        copy_row = tk.Frame(right, bg=C["base"])
        copy_row.pack(fill=tk.X, pady=(6, 0))

        self._copy_btn = ttk.Button(copy_row, text="Copy Prompt  ▸",
                                    style="Primary.TButton",
                                    command=self._copy_snippet,
                                    state=tk.DISABLED)
        self._copy_btn.pack(side=tk.LEFT)

        self._copy_status = tk.Label(copy_row, text="",
                                     font=("Segoe UI", 8),
                                     bg=C["base"], fg=C["green"])
        self._copy_status.pack(side=tk.LEFT, padx=(10, 0))

        user_btn_row = tk.Frame(right, bg=C["base"])
        user_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(user_btn_row, text="+ Add Snippet",
                   command=self._add_snippet).pack(side=tk.LEFT, padx=(0, 4))
        self._edit_btn = ttk.Button(user_btn_row, text="Edit",
                                    command=self._edit_snippet, state=tk.DISABLED)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self._delete_btn = ttk.Button(user_btn_row, text="Delete",
                                      style="Danger.TButton",
                                      command=self._delete_snippet, state=tk.DISABLED)
        self._delete_btn.pack(side=tk.LEFT)

        # Populate listbox and build the parallel metadata map
        self._refresh_snippet_list()
        self.snippet_lb.bind("<<ListboxSelect>>", self._on_snippet_select)

    def _refresh_snippet_list(self, reselect_index=None):
        """Rebuild the snippet listbox and _active_snippets_map from scratch."""
        self._active_snippets_map = []
        self.snippet_lb.delete(0, tk.END)

        # Built-in snippets
        for title, text in PROMPT_SNIPPETS:
            self.snippet_lb.insert(tk.END, f"  {title}")
            self._active_snippets_map.append({"type": "builtin", "data": {"title": title, "text": text}})

        # Separator
        self.snippet_lb.insert(tk.END, "  ──── My Snippets ────")
        self._active_snippets_map.append({"type": "separator"})

        # User snippets
        for idx, u in enumerate(_cfg.get("user_snippets", [])):
            self.snippet_lb.insert(tk.END, f"  ✎ {u['title']}")
            self._active_snippets_map.append({"type": "user", "index": idx, "data": u})

        # Restore selection
        if reselect_index is not None and reselect_index < self.snippet_lb.size():
            self.snippet_lb.selection_set(reselect_index)
            self.snippet_lb.event_generate("<<ListboxSelect>>")
        else:
            # Clear preview and reset buttons if nothing to reselect
            self._snippet_preview.configure(state=tk.NORMAL)
            self._snippet_preview.delete("1.0", tk.END)
            self._snippet_preview.configure(state=tk.DISABLED)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")

    def _on_snippet_select(self, _event=None):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            # Deselect separator — don't show anything
            self.snippet_lb.selection_clear(0, tk.END)
            self._copy_btn.configure(state=tk.DISABLED)
            self._edit_btn.configure(state=tk.DISABLED)
            self._delete_btn.configure(state=tk.DISABLED)
            self._copy_status.configure(text="")
            return

        text = meta["data"]["text"]
        self._snippet_preview.configure(state=tk.NORMAL)
        self._snippet_preview.delete("1.0", tk.END)
        self._snippet_preview.insert(tk.END, text)
        self._snippet_preview.configure(state=tk.DISABLED)
        self._copy_btn.configure(state=tk.NORMAL)
        self._copy_status.configure(text="")

        is_user = (meta["type"] == "user")
        self._edit_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)
        self._delete_btn.configure(state=tk.NORMAL if is_user else tk.DISABLED)

    def _copy_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] == "separator":
            return
        self.clipboard_clear()
        self.clipboard_append(meta["data"]["text"])
        self._copy_status.configure(text="✔ Copied!")
        self.after(2000, lambda: self._copy_status.configure(text=""))

    def _add_snippet(self):
        SnippetEditDialog(self, None, self._on_snippet_saved)

    def _edit_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        SnippetEditDialog(self, meta, self._on_snippet_saved)

    def _delete_snippet(self):
        sel = self.snippet_lb.curselection()
        if not sel:
            return
        meta = self._active_snippets_map[sel[0]]
        if meta["type"] != "user":
            return
        title = meta["data"]["title"]
        if not messagebox.askyesno(
            "Delete snippet",
            f"Delete '{title}'?\n\nThis cannot be undone.",
            parent=self,
        ):
            return
        user_snippets = _cfg.get("user_snippets", [])
        idx = meta["index"]
        del user_snippets[idx]
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list()

    def _on_snippet_saved(self, title, text, edit_meta):
        """Callback from SnippetEditDialog — save and refresh."""
        user_snippets = _cfg.get("user_snippets", [])
        if edit_meta is None:
            # Add new snippet
            user_snippets.append({"title": title, "text": text})
            new_idx = len(PROMPT_SNIPPETS) + 1 + len(user_snippets) - 1  # separator + 0-based
        else:
            # Update existing
            idx = edit_meta["index"]
            user_snippets[idx] = {"title": title, "text": text}
            new_idx = len(PROMPT_SNIPPETS) + 1 + idx
        _cfg["user_snippets"] = user_snippets
        _save_config(_cfg)
        self._refresh_snippet_list(reselect_index=new_idx)

    def _open_guide(self):
        guide = os.path.join(_BASE_DIR, "TOKENSAVE_GUIDE.md")
        if os.path.isfile(guide):
            os.startfile(guide)
        else:
            messagebox.showerror("Not found",
                f"Guide not found at:\n{guide}", parent=self)

    def _build_help_tab(self):
        tab = tk.Frame(self.nb, bg=C["base"])
        self.nb.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ── Left: topic list ──────────────────────────────────────────────────
        list_wrap = tk.Frame(pane, bg=C["mantle"])
        list_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._help_lb = tk.Listbox(
            list_wrap, width=20, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Right: content ────────────────────────────────────────────────────
        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(fill=tk.BOTH, expand=True)

        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED,
            yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Text tags (shared across all sections) ────────────────────────────
        self._help_txt.tag_configure("h1",   font=("Segoe UI", 13, "bold"), foreground=C["blue"],
                                     spacing1=14, spacing3=6)
        self._help_txt.tag_configure("h2",   font=("Segoe UI", 10, "bold"), foreground=C["lavender"],
                                     spacing1=10, spacing3=2)
        self._help_txt.tag_configure("warn", font=("Segoe UI", 10, "bold"), foreground=C["yellow"])
        self._help_txt.tag_configure("ok",   font=("Segoe UI", 10, "bold"), foreground=C["green"])
        self._help_txt.tag_configure("dim",  foreground=C["overlay0"])
        self._help_txt.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        self._help_txt.tag_configure("body", foreground=C["text"], spacing3=3)

        # ── Sections ──────────────────────────────────────────────────────────
        self._help_sections = [
            ("  Switching Projects",  self._help_switching),
            ("  Right-click Menu",    self._help_context_menu),
            ("  Scaffold",            self._help_scaffold),
            ("  Retrofit Existing",   self._help_retrofit),
            ("  Nuitka Builds",       self._help_nuitka),
            ("  Scaffold Column",     self._help_scaffold_column),
            ("  Auto-detect",         self._help_autodetect),
            ("  init vs sync",        self._help_init_vs_sync),
            ("  File Locations",      self._help_file_locations),
            ("  About",               self._help_about),
        ]
        for title, _ in self._help_sections:
            self._help_lb.insert(tk.END, title)

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)

        # Show first section on open
        self._help_lb.selection_set(0)
        self._help_sections[0][1]()

    def _on_help_select(self, _event=None):
        sel = self._help_lb.curselection()
        if not sel:
            return
        self._help_sections[sel[0]][1]()

    def _help_show(self, fn):
        """Clear the help text widget, call fn() to fill it, then lock + scroll to top."""
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        fn()
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _hw(self):
        """Return (h1, h2, p, warn, ok, dim, br, ins) writer helpers for _help_txt."""
        t = self._help_txt
        def h1(s):       t.insert(tk.END, s + "\n", "h1")
        def h2(s):       t.insert(tk.END, s + "\n", "h2")
        def p(s):        t.insert(tk.END, s + "\n", "body")
        def warn(s):     t.insert(tk.END, s + "\n", "warn")
        def ok(s):       t.insert(tk.END, s + "\n", "ok")
        def dim(s):      t.insert(tk.END, s + "\n", "dim")
        def br():        t.insert(tk.END, "\n")
        def ins(s, tag): t.insert(tk.END, s, tag)
        return h1, h2, p, warn, ok, dim, br, ins

    # ── Help sections ──────────────────────────────────────────────────────────

    def _help_switching(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Switching Projects")
            warn("⚠  You must restart Claude Desktop after switching the active project.")
            br()
            p("The tokensave wrapper script runs once when Claude Desktop launches. It "
              "reads the active project at startup and stays locked to it for that "
              "session. Changing the pin (★ Set as Active) writes to a config file, "
              "but the already-running server won't pick it up until Claude Desktop "
              "restarts.")
            br()
            p("Workflow for switching:")
            ins("  1. Select the new project in the list\n", "body")
            ins("  2. Click ★ Set as Active\n", "body")
            ins("  3. Fully quit Claude Desktop (File → Quit, not just close the window)\n", "body")
            ins("  4. Relaunch Claude Desktop\n", "body")
            br()
            p("Tip: to go back to whichever project you last synced automatically, "
              "click Auto-detect instead of pinning a specific project.")
        self._help_show(_fill)

    def _help_context_menu(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Right-click Menu")
            p("Right-click any row in the project list for per-project actions. "
              "Global actions are in the toolbar at the bottom.")
            br()

            h2("Toolbar buttons")
            ins("  ＋  Scaffold          ", "body"); ins("Open the scaffold dialog for a folder\n", "dim")
            ins("  ⚙  Retrofit Existing  ", "body"); ins("Add tokensave rules to an existing project\n", "dim")
            ins("  ↺↺ Sync All           ", "body"); ins("Sync every indexed project sequentially\n", "dim")
            ins("  ⟳  Refresh            ", "body"); ins("Manually refresh the list (auto-refreshes every 60 s)\n", "dim")
            br()

            h2("Index management")
            ins("  ★  Set as Active  ", "body"); ins("Pin this project for Claude Desktop (restart Claude to apply)\n", "dim")
            ins("  ↺  Sync           ", "body"); ins("Incrementally re-index changed files\n", "dim")
            ins("  📊  Status         ", "body"); ins("Show node/edge/file counts and last sync time in a popup\n", "dim")
            ins("  ⟳  Force Re-sync  ", "body"); ins("Rebuild the entire code graph from scratch\n", "dim")
            ins("  🔍  Doctor         ", "body"); ins("Check tokensave installation health\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
            ins("  Auto-detect       ", "body"); ins("Clear the pin — wrapper picks the most-recently-synced project\n", "dim")
            br()

            h2("Git")
            ins("  📜  Git Log        ", "body")
            ins("Show last 20 commits + working-tree status from the project's own repo.\n", "dim")
            ins("                    ", "body")
            ins("Nothing is stored in the manager — purely a read-only view.\n", "dim")
            ins("                    ", "body")
            ins("Shows a friendly message if the folder is not a git repo or git is not on PATH.\n", "dim")
            br()

            h2("Navigation")
            ins("  📂  Open Folder    ", "body"); ins("Open the project folder in Windows Explorer\n", "dim")
            ins("  ✏   Open in Editor ", "body"); ins("Launch the configured editor (set in Settings → Editor command)\n", "dim")
            ins("  ⎘  Copy Path       ", "body"); ins("Copy the project folder path to the clipboard\n", "dim")
            br()

            h2("Setup")
            ins("  ⚙  Retrofit…       ", "body")
            ins("Open the Retrofit dialog for the selected project without re-navigating\n", "dim")
            ins("                     ", "body")
            ins("to the folder manually. Same as the toolbar button but pre-filled.\n", "dim")
            ins("  🗑  Remove Index…  ", "body"); ins("Delete .tokensave/ from this folder (project files untouched)\n", "dim")
        self._help_show(_fill)

    def _help_scaffold(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("＋  Scaffold")
            p("Pick any folder — empty or existing — and choose what to create:")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md  ", "body"); ins("— project template for Claude\n", "dim")
            ins("  Run tokensave init             ", "body"); ins("— build the code graph (~10–30 s)\n", "dim")
            ins("  Add Nuitka build files         ", "body"); ins("— copies build.ps1 + build.bat\n", "dim")
            br()
            p("While init runs the project appears in the list immediately as '(indexing…)'. "
              "Claude reads BASIC_INSTRUCTIONS.md on first session and adapts to whatever "
              "structure already exists.")
            br()
            p("If the folder already has a tokensave index, 'Run tokensave init' is "
              "unchecked by default. If BASIC_INSTRUCTIONS.md already exists, the "
              "checkbox notes it will be overwritten.")
        self._help_show(_fill)

    def _help_retrofit(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("⚙  Retrofit Existing")
            p("Add tokensave wiring to a project that already exists — without "
              "touching any of its current files destructively.")
            br()
            ins("  Add tokensave rules to CLAUDE.md  ", "body")
            ins("— prepends a single @include line.\n", "dim")
            ins("                                   ", "body")
            ins("  Non-destructive: all existing content is kept.\n", "dim")
            br()
            ins("  Create BASIC_INSTRUCTIONS.md      ", "body")
            ins("— optional project template for Claude.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if the file already exists.\n", "dim")
            br()
            ins("  Add Nuitka build files            ", "body")
            ins("— copies build.ps1 + build.bat.\n", "dim")
            ins("                                   ", "body")
            ins("  Skipped silently if build.ps1 already exists.\n", "dim")
            br()
            p("After applying, a summary popup lists exactly what was created or skipped.")
        self._help_show(_fill)

    def _help_nuitka(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Nuitka Build Files")
            p("Both Scaffold and Retrofit Existing have an 'Add Nuitka build files' "
              "checkbox. When ticked, two files are copied from the templates folder "
              "into the target project:")
            br()
            ins("  build.ps1  ", "body"); ins("— full Nuitka build script (PowerShell)\n", "dim")
            ins("  build.bat  ", "body"); ins("— one-line launcher that calls build.ps1\n", "dim")
            br()
            p("After applying, open build.ps1 and fill in the two remaining placeholders:")
            br()
            ins("  [ENTRY_SCRIPT]  ", "code"); ins("— path to your main .py file (relative to build.ps1)\n", "dim")
            ins("  [OUTPUT_NAME]   ", "code"); ins("— the desired .exe filename\n", "dim")
            ins("  [PROJECT_NAME]  ", "code"); ins("— already filled in from your folder name\n", "dim")
            br()
            p("Then double-click build.bat to compile. Read NUITKA_GOTCHAS.md (in the "
              "templates folder) for known pitfalls before your first build.")
            br()
            warn("Tip (Claude Code users):  ")
            ins("if you already have a project open in Claude Code you can skip the "
                "button entirely — just tell Claude: 'Set up a Nuitka build pipeline. "
                "Entry script is src/main.py, output name my-tool.exe.'\n"
                "Claude reads the Nuitka instructions from project-baseline.md via "
                "@include and will copy + fill in the templates automatically.", "body")
        self._help_show(_fill)

    def _help_scaffold_column(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("Scaffold Column")
            p("The 'Scaffold' column in the project list shows whether "
              "BASIC_INSTRUCTIONS.md has been created for each project.")
            br()
            ok("✔  BASIC_INSTRUCTIONS.md exists")
            br()
            ins("—  ", "warn"); ins("Not yet scaffolded — use ＋ Scaffold or ⚙ Retrofit Existing\n", "body")
            br()
            p("The column only checks for BASIC_INSTRUCTIONS.md. It does not indicate "
              "whether CLAUDE.md has the @include line or whether Nuitka build files "
              "are present.")
        self._help_show(_fill)

    def _help_autodetect(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("How Auto-detect Works")
            p("The wrapper script (tokensave-wrapper.py / tokensave-wrapper.exe) "
              "runs at Claude Desktop startup and decides which project to serve:")
            br()
            ins("  1. ", "body"); ins("Checks desktop-project.txt — uses that path if present and valid\n", "dim")
            ins("  2. ", "body"); ins("Otherwise scans project roots for .tokensave/tokensave.db files\n", "dim")
            ins("  3. ", "body"); ins("Picks the one with the most recent modification time\n", "dim")
            ins("  4. ", "body"); ins("Starts: tokensave.exe serve -p <chosen path>\n", "dim")
            br()
            p("Running ↺ Sync on a project updates its database timestamp, so the next "
              "Auto-detect restart will naturally pick it up.")
            br()
            p("'Auto-detect' in the right-click menu clears the pin file, switching "
              "back to automatic selection on the next Claude Desktop restart.")
        self._help_show(_fill)

    def _help_init_vs_sync(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("init vs sync")
            h2("tokensave init")
            p("Full first-time index of a project. Run once when setting up a new "
              "project. Builds the complete code graph from scratch. Can take a few "
              "minutes for large codebases.")
            br()
            h2("tokensave sync")
            p("Incremental update — only re-indexes files that changed since the last "
              "run. Fast. Run this any time you want to update the index after making "
              "code changes, or to make Auto-detect pick this project on the next "
              "Claude Desktop restart.")
            br()
            p("The ↺ Sync button in the right-click menu runs 'sync'. If the project "
              "has no index yet, it asks whether to run 'init' instead.")
        self._help_show(_fill)

    def _help_file_locations(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("File Locations")
            ins("Active project pin:  ", "body")
            ins("%USERPROFILE%\\.tokensave\\desktop-project.txt\n", "code")
            ins("Baseline rules:      ", "body")
            ins(os.path.join(TEMPLATE_DIR, "project-baseline.md") + "\n", "code")
            ins("Project template:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "claude-md-template.md") + "\n", "code")
            ins("Nuitka templates:    ", "body")
            ins(os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template") + "\n", "code")
            ins("Wrapper script:      ", "body")
            if os.environ.get("NUITKA_ONEFILE_PARENT"):
                _wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
            else:
                _wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
            ins(_wrapper + "\n", "code")
            ins("Manager log:         ", "body")
            ins(LOG_FILE + "\n", "code")
            ins("Manager config:      ", "body")
            ins(_CONFIG_PATH + "\n", "code")
        self._help_show(_fill)

    def _help_about(self):
        def _fill():
            h1, h2, p, warn, ok, dim, br, ins = self._hw()
            h1("About")
            ins("TokenSave Manager\n", "body")
            ins("Created by Alexander L Corthell\n\n", "dim")
            h2("What this tool does")
            p("Manages tokensave MCP project integrations for Claude Desktop. "
              "Handles project discovery, index sync, project switching, "
              "scaffolding Claude instruction templates, Nuitka build pipelines, "
              "git log / status, folder/editor navigation, and clipboard shortcuts.")
            br()
            h2("What it doesn't do (yet)")
            ins("  • tokensave branch management (branch add/list/gc)\n", "dim")
            ins("  • Daemon start/stop/status\n", "dim")
            ins("  • Cost tracking (tokensave cost)\n", "dim")
            ins("  • Cross-platform support (Windows only)\n", "dim")
            ins("  • Inline git diff / commit details\n", "dim")
        self._help_show(_fill)

    # ── Data ───────────────────────────────────────────────────────────────────

    def _has_scaffold(self, path):
        """Return True if this project has BASIC_INSTRUCTIONS.md."""
        return os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))

    def refresh(self):
        self.projects = find_projects()
        pinned = get_pinned()
        self.active_path = pinned or (self.projects[0]["path"] if self.projects else None)

        for item in self.tree.get_children():
            self.tree.delete(item)

        proj_cats = _cfg.get("project_categories", {})

        # Group projects by (category, subcategory)
        groups: dict = {}
        for p in self.projects:
            ov     = proj_cats.get(p["path"], {})
            cat    = ov.get("category") or p.get("root_label", "Projects")
            subcat = ov.get("subcategory", "")
            groups.setdefault((cat, subcat), []).append(p)

        cat_iids: dict = {}
        for (cat, subcat), projs in sorted(groups.items()):
            # Insert category header row if not yet present
            if cat not in cat_iids:
                ciid = f"cat:{cat}"
                self.tree.insert("", tk.END, iid=ciid, text=cat,
                                 open=True, tags=("category",))
                cat_iids[cat] = ciid

            parent = cat_iids[cat]

            # Insert sub-category header row if specified
            if subcat:
                siid = f"sub:{cat}:{subcat}"
                if not self.tree.exists(siid):
                    self.tree.insert(parent, tk.END, iid=siid, text=subcat,
                                     open=True, tags=("subcategory",))
                parent = siid

            # Insert project rows
            for p in projs:
                is_active    = (p["path"] == self.active_path)
                has_scaffold = self._has_scaffold(p["path"])
                tag  = "active" if is_active else ("scaffold" if not has_scaffold else "normal")
                piid = f"proj:{p['path']}"
                self.tree.insert(parent, tk.END, iid=piid,
                                 text=p["name"],
                                 values=("★" if is_active else "",
                                         p["path"],
                                         fmt_age(p["mtime"]),
                                         "✔" if has_scaffold else "—"),
                                 tags=(tag,))

        if self.active_path:
            name = os.path.basename(self.active_path)
            tag  = "pinned" if pinned else "auto"
            self.active_badge.config(text=f"  ★ {name}  ({tag})  ")
        else:
            self.active_badge.config(text="  No project  ")

    def _build_context_menu(self):
        m = tk.Menu(self, tearoff=0,
                    bg=C["surface0"], fg=C["text"],
                    activebackground=C["surface1"], activeforeground=C["text"],
                    relief=tk.FLAT, bd=0, font=("Segoe UI", 10))
        m.add_command(label="★  Set as Active",  command=self.cmd_set_active)
        m.add_command(label="↺  Sync",           command=self.cmd_sync)
        m.add_command(label="📊  Status",         command=self.cmd_status)
        m.add_command(label="⟳  Force Re-sync",  command=self.cmd_force_sync)
        m.add_command(label="🔍  Doctor",         command=self.cmd_doctor)
        m.add_command(label="📜  Git Log",        command=self.cmd_git_log)
        m.add_command(label="📝  Git Commit…",    command=self.cmd_git_commit)
        m.add_command(label="🔧  Git Init",       command=self.cmd_git_init)
        m.add_separator()
        m.add_command(label="📂  Open Folder",    command=self.cmd_open_folder)
        m.add_command(label="✏   Open in Editor", command=self.cmd_open_editor)
        m.add_command(label="⎘  Copy Path",       command=self.cmd_copy_path)
        m.add_separator()
        m.add_command(label="⚙  Retrofit…",          command=self.cmd_retrofit_selected)
        m.add_command(label="🔗  Shadow Links…",     command=self.cmd_shadow_links)
        m.add_command(label="📁  Assign Category…", command=self.cmd_assign_category)
        m.add_command(label="🗑  Remove Index…",     command=self.cmd_remove)
        m.add_separator()
        m.add_command(label="Auto-detect",        command=self.cmd_auto)
        self._ctx_menu = m

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        if not row.startswith("proj:"):
            return   # no context menu on category/subcategory header rows
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _insert_pending_row(self, path, name):
        """Add a placeholder row while tokensave init is running."""
        self.tree.insert("", 0,
            text=name,
            values=("", path, "(indexing…)", "—"),
            tags=("pending",))

    def _check_config(self):
        problems = []
        if not TOKENSAVE or not os.path.isfile(TOKENSAVE):
            problems.append("tokensave.exe path is missing or invalid")
        if not TEMPLATE_DIR or not os.path.isdir(TEMPLATE_DIR):
            problems.append("Template directory is missing or invalid")
        if not problems:
            return
        note = "Please set the correct paths before using the manager."
        self._log("Config problem: " + " | ".join(problems), C["red"])
        SettingsDialog(self, _cfg, _save_config, self._on_settings_saved,
                       startup_note=note + "\n\n" + "\n".join(f"• {p}" for p in problems))

    def _auto_refresh(self):
        if self._current_proc is None:
            self.refresh()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _log(self, msg, colour=None):
        def _do():
            self.log.configure(state=tk.NORMAL)
            tag = f"col_{colour}"
            self.log.tag_configure(tag, foreground=colour or C["green"])
            self.log.insert(tk.END, msg + "\n", tag)
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Click a project row first.", parent=self)
            return None
        iid = sel[0]
        if not iid.startswith("proj:"):
            messagebox.showwarning("Nothing selected",
                "Select a project row (not a category header).", parent=self)
            return None
        return iid[5:]   # strip "proj:" prefix

    # ── Scaffold ────────────────────────────────────────────────────────────────

    def _scaffold_nuitka_build(self, path) -> list:
        """Copy Nuitka build templates into path, auto-filling [PROJECT_NAME].

        Returns a list of action strings describing what was created (empty if
        both files already existed or if the templates couldn't be found).
        """
        name     = os.path.basename(path)
        src_ps1  = os.path.join(TEMPLATE_DIR, "nuitka-build.ps1.template")
        src_bat  = os.path.join(TEMPLATE_DIR, "nuitka-build.bat.template")
        dst_ps1  = os.path.join(path, "build.ps1")
        dst_bat  = os.path.join(path, "build.bat")
        actions  = []

        if not os.path.isfile(src_ps1) or not os.path.isfile(src_bat):
            self._log("  [WARN] Nuitka templates not found in template directory — skipped",
                      C["yellow"])
            log.warning(f"  NUITKA scaffold: templates missing in {TEMPLATE_DIR}")
            return actions

        # build.ps1 — fill [PROJECT_NAME]; leave [ENTRY_SCRIPT] / [OUTPUT_NAME] for the user
        if os.path.isfile(dst_ps1):
            self._log("  build.ps1 already exists — skipped", C["overlay0"])
            log.info("  build.ps1 already exists — skipped")
        else:
            try:
                with open(src_ps1, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("[PROJECT_NAME]", name)
                with open(dst_ps1, "w", encoding="utf-8") as f:
                    f.write(content)
                self._log(
                    "  Created build.ps1  (edit [ENTRY_SCRIPT] and [OUTPUT_NAME] before building)",
                    C["green"])
                log.info(f"  created build.ps1 in {name}")
                actions.append("Created build.ps1")
            except Exception as e:
                self._log(f"  Error creating build.ps1: {e}", C["red"])
                log.exception("  NUITKA scaffold build.ps1 failed")

        # build.bat — copy as-is (ASCII, no customisation needed)
        if os.path.isfile(dst_bat):
            self._log("  build.bat already exists — skipped", C["overlay0"])
            log.info("  build.bat already exists — skipped")
        else:
            try:
                shutil.copy2(src_bat, dst_bat)
                self._log("  Created build.bat", C["green"])
                log.info(f"  created build.bat in {name}")
                actions.append("Created build.bat")
            except Exception as e:
                self._log(f"  Error creating build.bat: {e}", C["red"])
                log.exception("  NUITKA scaffold build.bat failed")

        if actions:
            self._log(
                "  Tip: open build.ps1, set [ENTRY_SCRIPT] and [OUTPUT_NAME], then run build.bat",
                C["sky"])

        return actions

    def _scaffold_project(self, path, create_bi=True, run_init=True,
                          scaffold_nuitka=False, add_git_hook=False):
        """Write BASIC_INSTRUCTIONS.md and/or run tokensave init."""
        name = os.path.basename(path)
        log.info(f"SCAFFOLD {path}  create_bi={create_bi} run_init={run_init} "
                 f"nuitka={scaffold_nuitka} git_hook={add_git_hook}")

        # Write BASIC_INSTRUCTIONS.md synchronously (it's instant)
        if create_bi:
            basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
            try:
                template = load_basic_instructions_template()
                with open(basic_md, "w", encoding="utf-8") as f:
                    f.write(template)
                self._log(f"  Created BASIC_INSTRUCTIONS.md in {name}", C["green"])
                log.info("  created BASIC_INSTRUCTIONS.md")
            except Exception as e:
                self._log(f"  Error writing BASIC_INSTRUCTIONS.md: {e}", C["red"])
                log.exception("  SCAFFOLD write failed")
                return

        # Copy Nuitka build templates synchronously (instant file copy)
        if scaffold_nuitka:
            self._scaffold_nuitka_build(path)

        # Write/merge auto-commit Stop hook
        if add_git_hook:
            for action in _scaffold_git_hook(path):
                self._log(f"  {action}", C["green"])

        if run_init:
            # Show a pending row immediately so the user sees the project appearing
            self._insert_pending_row(path, name)
            self._log(f"Initializing tokensave index for {name}…", C["yellow"])

            def worker():
                log.info(f"  INIT {path}")
                self.after(0, self._set_running, True, name)
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "init"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            self._log(f"  {stripped}")
                            log.debug(f"  OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ Index built for {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"  INIT done exit=0 [{elapsed:.1f}s]")
                    else:
                        self._log(f"  ✗ Init failed (exit {proc.returncode})", C["red"])
                        log.warning(f"  INIT done exit={proc.returncode} [{elapsed:.1f}s]")
                except Exception as e:
                    self._log(f"  Error during init: {e}", C["red"])
                    log.exception("  INIT exception")
                finally:
                    self._current_proc = None
                    self.after(0, self._set_running, False)
                    self.after(0, self.refresh)

            threading.Thread(target=worker, daemon=True).start()
        else:
            self.refresh()

    # ── Commands ───────────────────────────────────────────────────────────────

    def cmd_set_active(self):
        path = self._selected_path()
        if not path:
            return
        set_pinned(path)
        self._log(f"Pinned → {path}", C["green"])
        self._log("Restart Claude Desktop for the change to take effect.", C["yellow"])
        self.refresh()

    def cmd_auto(self):
        clear_pinned()
        self._log("Auto-detect enabled — wrapper picks the most-recently-synced project.", C["sky"])
        self._log("Restart Claude Desktop for the change to take effect.", C["yellow"])
        self.refresh()

    def _set_running(self, running, label=""):
        if running:
            self._stop_btn.configure(state=tk.NORMAL)
            self._running_label.configure(text=f"⏳ running: {label}")
        else:
            self._stop_btn.configure(state=tk.DISABLED)
            self._running_label.configure(text="")

    def _stop_current(self):
        self._stop_requested = True
        proc = self._current_proc
        if proc and proc.poll() is None:
            proc.kill()
            self._log("  ■ Stopped by user.", C["red"])

    def _open_log(self):
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("No log yet",
                "No log file exists yet — run an operation first.", parent=self)

    def _run(self, args, cwd, label):
        def worker():
            cmd_str = "tokensave " + " ".join(args)
            self._log(f"$ {cmd_str}  [{label}]", C["blue"])
            self.after(0, self._set_running, True, label)
            log.info(f"RUN  {cmd_str}")
            log.debug(f"     cwd={cwd}")
            t0 = time.monotonic()
            try:
                env = os.environ.copy()
                env["NO_COLOR"] = "1"
                env["TERM"] = "dumb"
                proc = subprocess.Popen(
                    [TOKENSAVE] + args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
                self._current_proc = proc
                log.debug(f"     pid={proc.pid}")
                for line in proc.stdout:
                    stripped = _ANSI.sub("", line).rstrip()
                    if not stripped:
                        continue
                    self._log(stripped)
                    log.debug(f"  OUT {stripped}")
                proc.wait()
                elapsed = time.monotonic() - t0
                if proc.returncode == 0:
                    self._log("Done.", C["green"])
                    log.info(f"DONE exit=0  [{elapsed:.1f}s]")
                    # Auto-commit after sync if the toggle is on and this is a git repo
                    if (args and args[0] == "sync"
                            and _cfg.get("auto_commit_after_sync")
                            and _is_git_repo(cwd)):
                        self._log("  Auto-committing sync changes…", C["peach"])
                        self._shell_capture(["git", "-C", cwd, "add", "-A"], cwd)
                        _, staged_rc = self._shell_capture(
                            ["git", "-C", cwd, "diff", "--cached", "--quiet"], cwd)
                        if staged_rc != 0:   # non-zero = staged changes exist
                            cout, crc = self._shell_capture(
                                ["git", "-C", cwd, "commit", "-m",
                                 "chore: tokensave sync"], cwd)
                            col = C["green"] if crc == 0 else C["red"]
                            for line in cout.strip().splitlines()[-3:]:
                                self._log(f"  {line}", col)
                else:
                    self._log(f"Exited with code {proc.returncode}", C["red"])
                    log.warning(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception(f"EXCEPTION in _run({cmd_str})")
            finally:
                self._current_proc = None
                self.after(0, self._set_running, False)
        threading.Thread(target=worker, daemon=True).start()

    def _run_capture(self, args, cwd, label) -> tuple:
        """Run a tokensave command and return (raw_output, returncode, elapsed_s).

        Synchronous — must be called from a background thread.
        Handles _current_proc tracking and _set_running for the duration.
        The caller is responsible for logging and scheduling UI updates.
        """
        cmd_str = "tokensave " + " ".join(args)
        self._log(f"$ {cmd_str}  [{label}]", C["blue"])
        self.after(0, self._set_running, True, label)
        log.info(f"RUN  {cmd_str}")
        log.debug(f"     cwd={cwd}")
        t0 = time.monotonic()
        try:
            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            proc = subprocess.Popen(
                [TOKENSAVE] + args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self._current_proc = proc
            log.debug(f"     pid={proc.pid}")
            raw = proc.stdout.read()
            proc.wait()
            elapsed = time.monotonic() - t0
            log.info(f"DONE exit={proc.returncode}  [{elapsed:.1f}s]")
            log.debug(f"  OUT {raw[:500]}")
            return raw, proc.returncode, elapsed
        finally:
            self._current_proc = None
            self.after(0, self._set_running, False)

    def _shell_capture(self, cmd: list, cwd: str) -> tuple:
        """Run any shell command and return (stdout+stderr, returncode).

        Generic helper — cmd[0] is the executable (not tokensave-specific).
        Synchronous — must be called from a background thread.
        Returns ("Error: '<exe>' not found on system PATH.", 1) if the
        executable is missing so callers always get a displayable string.
        """
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            out = proc.stdout.read()
            proc.wait()
            return out, proc.returncode
        except FileNotFoundError:
            return (f"Error: '{cmd[0]}' not found on system PATH.", 1)

    def cmd_sync(self):
        path = self._selected_path()
        if not path:
            return
        if not os.path.isdir(os.path.join(path, ".tokensave")):
            if messagebox.askyesno(
                "Not yet indexed",
                f"{os.path.basename(path)} has no tokensave index yet.\n\n"
                "Run 'tokensave init' now to build it?\n"
                "(Do this once you have code worth indexing.)",
                parent=self,
            ):
                self._run(["init"], cwd=path, label=os.path.basename(path))
            return
        self._run(["sync"], cwd=path, label=os.path.basename(path))

    def cmd_sync_all(self):
        if not self.projects:
            messagebox.showinfo("No Projects", "No indexed projects found.", parent=self)
            return
        count = len(self.projects)
        if not messagebox.askyesno(
            "Sync All",
            f"Sync all {count} indexed project{'s' if count != 1 else ''}?\n\n"
            "Runs sequentially — may take a while for large projects.",
            parent=self,
        ):
            return

        projects_snapshot = list(self.projects)

        def worker():
            self._stop_requested = False
            self._log(f"↺  Syncing all {count} projects…", C["blue"])
            log.info(f"SYNC ALL — {count} projects")
            self.after(0, self._set_running, True, "all projects")
            ok = fail = 0
            for i, p in enumerate(projects_snapshot, 1):
                if self._stop_requested:
                    self._log(f"  ■ Sync All aborted after {i - 1}/{count}.", C["red"])
                    log.info("SYNC ALL aborted by user")
                    break
                name = p["name"]
                path = p["path"]
                self._log(f"[{i}/{count}] {name}", C["subtext"])
                log.info(f"  SYNC {i}/{count}: {name}")
                t0 = time.monotonic()
                try:
                    env = os.environ.copy()
                    env["NO_COLOR"] = "1"
                    env["TERM"] = "dumb"
                    proc = subprocess.Popen(
                        [TOKENSAVE, "sync"], cwd=path,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        env=env, creationflags=CREATE_NO_WINDOW,
                    )
                    self._current_proc = proc
                    for line in proc.stdout:
                        stripped = _ANSI.sub("", line).rstrip()
                        if stripped:
                            log.debug(f"    OUT {stripped}")
                    proc.wait()
                    elapsed = time.monotonic() - t0
                    if proc.returncode == 0:
                        self._log(f"  ✓ {name}  ({elapsed:.1f}s)", C["green"])
                        log.info(f"    done exit=0 [{elapsed:.1f}s]")
                        ok += 1
                    else:
                        self._log(f"  ✗ {name}  (exit {proc.returncode})", C["red"])
                        log.warning(f"    done exit={proc.returncode} [{elapsed:.1f}s]")
                        fail += 1
                except Exception as e:
                    self._log(f"  ✗ {name}: {e}", C["red"])
                    log.exception(f"  EXCEPTION syncing {name}")
                    fail += 1
                finally:
                    self._current_proc = None

            summary = f"Sync All done — {ok} succeeded"
            if fail:
                summary += f", {fail} failed"
            self._log(summary, C["green"] if not fail else C["peach"])
            log.info(f"SYNC ALL complete — ok={ok} fail={fail}")
            self.after(0, self._set_running, False)
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_status(self):
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            try:
                raw, _rc, elapsed = self._run_capture(["status", "--json"], path, name)
                cleaned = _ANSI.sub("", raw).strip()
                try:
                    data = json.loads(cleaned)
                    log.debug(f"  JSON parsed OK: {len(data)} keys")
                    kb = data.get("db_size_bytes", 0) // 1024
                    self._log(f"  Status OK — {data.get('node_count')} nodes, "
                              f"{data.get('file_count')} files, {kb} KB", C["green"])
                    msg = self._format_status_msg(name, data)
                    self.after(0, lambda m=msg: self._show_status_popup(name, m))
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  JSON parse failed: {e} — raw: {cleaned[:200]}")
                    for line in cleaned.splitlines():
                        if line.strip():
                            self._log(line)
                self._log(f"Done.  [{elapsed:.1f}s]", C["green"])
                self.after(0, self.refresh)
            except Exception as e:
                self._log(f"Error: {e}", C["red"])
                log.exception("EXCEPTION in cmd_status")

        threading.Thread(target=worker, daemon=True).start()

    def _show_status_popup(self, name, msg):
        win = tk.Toplevel(self)
        win.title(f"Status — {name}")
        win.configure(bg=C["base"])
        win.resizable(False, False)
        win.grab_set()

        tk.Label(
            win, text=msg,
            bg=C["base"], fg=C["text"],
            font=("Consolas", 10),
            justify=tk.LEFT,
            padx=20, pady=16,
        ).pack()

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))
        win.transient(self)

    @staticmethod
    def _format_status_msg(name: str, data: dict) -> str:
        """Format a tokensave status JSON dict into a human-readable popup string."""
        kb       = data.get("db_size_bytes", 0) // 1024
        sync_ts  = data.get("last_sync_at", 0)
        sync_str = datetime.fromtimestamp(sync_ts).strftime("%Y-%m-%d %H:%M") if sync_ts else "never"
        dur_ms   = data.get("last_sync_duration_ms", 0)
        dur_str  = f"{dur_ms} ms" if dur_ms else "—"
        kind_lines = "\n".join(
            f"    {k:<14} {v}" for k, v in sorted(data.get("nodes_by_kind", {}).items())
        )
        return (
            f"Project:   {name}\n\n"
            f"Nodes:     {data.get('node_count', '?')}\n"
            f"Edges:     {data.get('edge_count', '?')}\n"
            f"Files:     {data.get('file_count', '?')}\n"
            f"DB size:   {kb} KB\n\n"
            f"Node kinds:\n{kind_lines}\n\n"
            f"Last sync: {sync_str}  ({dur_str})\n"
        )

    def cmd_git_log(self):
        """Show recent git log + working-tree status for the selected project.

        Uses `git -C <path>` so it reads the project's own repo only.
        Nothing is stored in the manager — purely a read-only display.
        """
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)

        def worker():
            log_out, log_rc = self._shell_capture(
                ["git", "-C", path, "log", "--oneline", "-20"], path)
            status_out, _ = self._shell_capture(
                ["git", "-C", path, "status", "--short"], path)

            if log_rc != 0:
                if "not found on system PATH" in log_out:
                    content = log_out
                else:
                    content = "Not a git repository.\n\nRight-click → 🔧 Git Init to initialise one."
                    detail = log_out.strip()
                    if detail:
                        content += f"\n\n{detail}"
            else:
                parts = []
                if log_out.strip():
                    parts.append("── Recent commits ─────────────────────────────────────────────")
                    parts.append(log_out.strip())
                if status_out.strip():
                    parts.append("")
                    parts.append("── Working tree status ────────────────────────────────────────")
                    parts.append(status_out.strip())
                content = "\n".join(parts) if parts else "(no commits yet)"

            self.after(0, lambda c=content: self._show_git_popup(name, c))

        threading.Thread(target=worker, daemon=True).start()

    def _show_git_popup(self, name, content):
        win = tk.Toplevel(self)
        win.title(f"Git — {name}")
        win.configure(bg=C["base"])
        win.resizable(True, True)
        win.grab_set()

        wrap = tk.Frame(win, bg=C["base"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 6))

        vsb = ttk.Scrollbar(wrap, orient="vertical")
        txt = tk.Text(wrap, font=("Consolas", 9), bg=C["mantle"], fg=C["text"],
                      relief=tk.FLAT, padx=10, pady=8, wrap=tk.NONE,
                      cursor="arrow", state=tk.NORMAL,
                      width=72, height=22,
                      yscrollcommand=vsb.set)
        vsb.configure(command=txt.yview)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        txt.insert(tk.END, content)
        txt.configure(state=tk.DISABLED)

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))
        win.transient(self)

    # ── Git Init ───────────────────────────────────────────────────────────────

    def cmd_git_init(self):
        """Right-click: initialise a git repository in the selected project folder.

        git init is near-instantaneous (~5ms) so we run it synchronously on the
        main thread — no need for a background thread, and it keeps the
        messagebox flow simple and race-condition-free.
        """
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)

        # Guard: already a repo?
        if _is_git_repo(path):
            messagebox.showinfo(
                "Already a repository",
                f"{name} is already a git repository.",
                parent=self,
            )
            return

        # Run git init on the main thread (instantaneous)
        self._log(f"Running git init in {name}…", C["peach"])
        out, rc = self._shell_capture(["git", "-C", path, "init"], path)
        col = C["green"] if rc == 0 else C["red"]
        for line in out.strip().splitlines():
            self._log(f"  {line}", col)

        if rc != 0:
            self.refresh()
            return

        # Write a baseline .gitignore if none exists (protects git add -A)
        gi_path = os.path.join(path, ".gitignore")
        if not os.path.isfile(gi_path):
            try:
                with open(gi_path, "w", encoding="utf-8") as f:
                    f.write(_BASELINE_GITIGNORE)
                self._log("  Created baseline .gitignore", C["green"])
            except OSError as e:
                self._log(f"  Warning: could not write .gitignore: {e}", C["yellow"])

        # Ask about initial commit — natural main-thread messagebox, no scheduling
        if messagebox.askyesno(
            "Initial commit",
            f"git init succeeded.\n\nCreate an initial commit now?\n"
            "(stages all files with 'git add -A')",
            parent=self,
        ):
            def do_commit():
                self._shell_capture(["git", "-C", path, "add", "-A"], path)
                cout, crc = self._shell_capture(
                    ["git", "-C", path, "commit", "-m", "Initial commit"], path)
                ccol = C["green"] if crc == 0 else C["red"]
                for line in cout.strip().splitlines()[-4:]:
                    self.after(0, lambda l=line: self._log(f"  {l}", ccol))
                self.after(0, self.refresh)
            threading.Thread(target=do_commit, daemon=True).start()
        else:
            self.refresh()

    # ── Git Commit ─────────────────────────────────────────────────────────────

    def cmd_git_commit(self):
        """Right-click: open the Git Commit dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Fetch status synchronously (fast) so the dialog can show it immediately.
        status_out, _ = self._shell_capture(
            ["git", "-C", path, "status", "--short"], path)
        is_repo = _is_git_repo(path)
        GitCommitDialog(self, path, status_out, is_repo, self._do_git_commit)

    def _do_git_commit(self, path: str, message: str, stage_all: bool):
        """Perform git add + commit in a background thread."""
        name = os.path.basename(path)

        def worker():
            if stage_all:
                out, rc = self._shell_capture(["git", "-C", path, "add", "-A"], path)
                if rc != 0:
                    self.after(0, lambda: self._log(
                        f"git add failed: {out.strip()}", C["red"]))
                    return

            self._log(f"Committing {name}…", C["peach"])
            cout, crc = self._shell_capture(
                ["git", "-C", path, "commit", "-m", message], path)
            col = C["green"] if crc == 0 else C["red"]
            for line in cout.strip().splitlines()[-4:]:
                self.after(0, lambda l=line: self._log(f"  {l}", col))
            self.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def cmd_force_sync(self):
        path = self._selected_path()
        if not path:
            return
        if messagebox.askyesno(
            "Force Re-sync",
            f"Full re-index of {os.path.basename(path)}?\n\n"
            "This rebuilds the entire code graph from scratch.\n"
            "May take a minute for large projects.",
            parent=self,
        ):
            self._run(["sync", "--force"], cwd=path, label=os.path.basename(path))

    def cmd_doctor(self):
        path = self._selected_path()
        if not path:
            return
        self._run(["doctor"], cwd=path, label=os.path.basename(path))

    def cmd_open_folder(self):
        path = self._selected_path()
        if not path:
            return
        os.startfile(path)

    def cmd_open_editor(self):
        path = self._selected_path()
        if not path:
            return
        editor_str = _cfg.get("editor_cmd", "code")
        try:
            cmd = shlex.split(editor_str)
            cmd.append(path)
            subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            messagebox.showerror(
                "Editor not found",
                f"Could not launch '{editor_str}'.\n\n"
                "Set the correct editor command in Settings.",
                parent=self,
            )

    def cmd_copy_path(self):
        path = self._selected_path()
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self._log(f"Copied: {path}", C["sky"])

    def cmd_remove(self):
        path = self._selected_path()
        if not path:
            return
        name = os.path.basename(path)
        ts_dir = os.path.join(path, ".tokensave")
        if not os.path.isdir(ts_dir):
            messagebox.showinfo("Nothing to remove",
                f"{name} has no tokensave index.", parent=self)
            return
        if not messagebox.askyesno(
            "Remove index",
            f"Delete the tokensave index for:\n{path}\n\n"
            f"This removes the .tokensave/ directory only.\n"
            f"Your project files are not affected.\n\n"
            f"Continue?",
            icon="warning", parent=self,
        ):
            return
        try:
            shutil.rmtree(ts_dir)
            self._log(f"Removed .tokensave/ from {name}", C["peach"])
            log.info(f"REMOVE index {ts_dir}")
            self.refresh()
        except Exception as e:
            self._log(f"Error removing index: {e}", C["red"])
            log.exception(f"REMOVE failed: {ts_dir}")
            messagebox.showerror("Remove failed", str(e), parent=self)

    def cmd_settings(self):
        SettingsDialog(self, _cfg, _save_config, self._on_settings_saved)

    def _on_settings_saved(self):
        global TOKENSAVE, TEMPLATE_DIR, SEARCH_ROOTS
        global BASIC_INSTRUCTIONS_TEMPLATE, BASELINE_INCLUDE_LINE
        TOKENSAVE    = _cfg.get("tokensave_exe", "")
        TEMPLATE_DIR = _cfg.get("template_dir", "")
        SEARCH_ROOTS = _cfg.get("search_roots", [])
        BASIC_INSTRUCTIONS_TEMPLATE = os.path.join(TEMPLATE_DIR, "claude-md-template.md")
        BASELINE_INCLUDE_LINE = f"@{TEMPLATE_DIR}\\project-baseline.md"
        self.refresh()
        self._log("Settings saved and applied.", C["green"])

    # ── Shadow Links ───────────────────────────────────────────────────────────

    def cmd_shadow_links(self):
        """Right-click: open the Shadow Links dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        ShadowLinksDialog(self, path, self._do_shadow_links)

    def _do_shadow_links(self, path: str, ext_map: dict, run_sync: bool = True):
        """Generate shadow hardlinks in a background thread, then optionally sync."""
        name = os.path.basename(path)

        def worker():
            try:
                self._log(f"Generating shadow links for {name}…", C["peach"])
                log.info(f"SHADOW LINKS  {path}  map={ext_map}")
                created, skipped, failed = generate_shadow_links(path, ext_map)
                update_gitignore_for_shadows(path, ext_map)
                msg_parts = [f"Created: {created}"]
                if skipped:
                    msg_parts.append(f"Already existed: {skipped}")
                if failed:
                    msg_parts.append(f"Failed: {failed}")
                summary = "  ".join(msg_parts)
                self._log(f"  Shadow links: {summary}", C["green"])
                log.info(f"SHADOW LINKS done: {summary}")

                if run_sync and TOKENSAVE and created > 0:
                    self._log("  Running tokensave sync…", C["blue"])
                    raw, rc, elapsed = self._run_capture(
                        ["sync"], path, "shadow-sync")
                    out = _ANSI.sub("", raw).strip()
                    col = C["green"] if rc == 0 else C["red"]
                    for line in out.splitlines()[-4:]:
                        self._log(f"    {line}", col)

                self.after(0, self.refresh)
                self.after(0, lambda: messagebox.showinfo(
                    "Shadow Links",
                    f"{name}:\n\n{summary}"
                    + (f"\n\nSync {'completed' if rc == 0 else 'failed'}." if run_sync and created > 0 else ""),
                    parent=self))
            except Exception as e:
                log.exception(f"SHADOW LINKS failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror(
                    "Shadow Links failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()

    # ── Category assignment ───────────────────────────────────────────────────

    def cmd_assign_category(self):
        """Right-click: open the Assign Category dialog for the selected project."""
        path = self._selected_path()
        if not path:
            return
        # Build sorted list of all categories and subcategories currently in use
        all_cats: list = []
        all_subs: dict = {}  # cat -> set of subcategories
        for r in SEARCH_ROOTS:
            lbl = _root_label(r)
            if lbl not in all_cats:
                all_cats.append(lbl)
            all_subs.setdefault(lbl, set())
        for ov in _cfg.get("project_categories", {}).values():
            cat = ov.get("category", "")
            sub = ov.get("subcategory", "")
            if cat and cat not in all_cats:
                all_cats.append(cat)
            if cat and sub:
                all_subs.setdefault(cat, set()).add(sub)
        all_cats.sort()
        current = _cfg.get("project_categories", {}).get(path, {})
        AssignCategoryDialog(self, path, sorted(all_cats),
                             {k: sorted(v) for k, v in all_subs.items()},
                             current, self._do_assign_category)

    def _do_assign_category(self, path, cat, subcat):
        """Callback from AssignCategoryDialog — update config and refresh."""
        proj_cats = _cfg.setdefault("project_categories", {})
        if cat is None:
            # Clear override
            proj_cats.pop(path, None)
            self._log(f"  Category override cleared for {os.path.basename(path)}", C["blue"])
        else:
            entry = {"category": cat}
            if subcat:
                entry["subcategory"] = subcat
            proj_cats[path] = entry
            sub_str = f" → {subcat}" if subcat else ""
            self._log(f"  Assigned {os.path.basename(path)} → {cat}{sub_str}", C["blue"])
        _save_config(_cfg)
        self.refresh()

    # ── Scaffold / Retrofit ────────────────────────────────────────────────────

    def cmd_scaffold(self):
        folder = filedialog.askdirectory(title="Select folder to scaffold", parent=self)
        if not folder:
            return
        ScaffoldDialog(self, folder, self._scaffold_project)

    def cmd_retrofit(self):
        """Toolbar button — pick any folder then open the Retrofit dialog."""
        folder = filedialog.askdirectory(
            title="Select existing project to retrofit", parent=self)
        if not folder:
            return
        RetrofitDialog(self, folder, self._do_retrofit)

    def cmd_retrofit_selected(self):
        """Right-click menu — open the Retrofit dialog for the selected project directly."""
        path = self._selected_path()
        if not path:
            return
        RetrofitDialog(self, path, self._do_retrofit)

    def _do_retrofit(self, path, add_tokensave, add_basic_instructions,
                     add_nuitka=False, add_shadow_links=False, shadow_ext_map=None,
                     add_git_hook=False):
        """Run the retrofit in a background thread."""
        name = os.path.basename(path)

        def worker():
            try:
                log.info(f"RETROFIT {path}  ts={add_tokensave} bi={add_basic_instructions} nuitka={add_nuitka}")
                self._log(f"Retrofitting {name}…", C["peach"])
                actions_taken = []

                # ── Tokensave integration ──────────────────────────────────────
                if add_tokensave:
                    claude_md = os.path.join(path, "CLAUDE.md")
                    include_line = BASELINE_INCLUDE_LINE

                    if os.path.isfile(claude_md):
                        content = open(claude_md, encoding="utf-8", errors="ignore").read()
                        if "project-baseline.md" in content:
                            log.info("  CLAUDE.md already has @include — skipped")
                            self._log("  Tokensave already integrated in CLAUDE.md — skipped",
                                      C["overlay0"])
                        else:
                            with open(claude_md, "r+", encoding="utf-8") as f:
                                existing = f.read()
                                f.seek(0)
                                f.write(include_line + "\n\n" + existing)
                            log.info("  prepended @include to CLAUDE.md")
                            self._log("  Added tokensave @include to CLAUDE.md", C["green"])
                            actions_taken.append("Added tokensave rules to CLAUDE.md")
                    else:
                        with open(claude_md, "w", encoding="utf-8") as f:
                            f.write(
                                f"# {name} — Claude Instructions\n\n"
                                f"{include_line}\n"
                            )
                        log.info("  created CLAUDE.md with @include")
                        self._log("  Created CLAUDE.md with tokensave @include", C["green"])
                        actions_taken.append("Created CLAUDE.md with tokensave rules")

                # ── BASIC_INSTRUCTIONS.md ─────────────────────────────────────
                if add_basic_instructions:
                    basic_md = os.path.join(path, "BASIC_INSTRUCTIONS.md")
                    if os.path.isfile(basic_md):
                        log.info("  BASIC_INSTRUCTIONS.md already exists — skipped")
                        self._log("  BASIC_INSTRUCTIONS.md already exists — skipped",
                                  C["overlay0"])
                    else:
                        template = load_basic_instructions_template()
                        with open(basic_md, "w", encoding="utf-8") as f:
                            f.write(template)
                        log.info("  created BASIC_INSTRUCTIONS.md")
                        self._log("  Created BASIC_INSTRUCTIONS.md", C["green"])
                        actions_taken.append("Created BASIC_INSTRUCTIONS.md")

                # ── Nuitka build files ─────────────────────────────────────────
                if add_nuitka:
                    nuitka_actions = self._scaffold_nuitka_build(path)
                    actions_taken.extend(nuitka_actions)

                # ── Shadow extension links ─────────────────────────────────────
                if add_shadow_links:
                    ext_map = shadow_ext_map or DEFAULT_SHADOW_EXT_MAP
                    self._log("  Generating shadow extension links…", C["peach"])
                    created, skipped, failed = generate_shadow_links(path, ext_map)
                    update_gitignore_for_shadows(path, ext_map)
                    sl_msg = f"Shadow links: created {created}"
                    if skipped:
                        sl_msg += f", {skipped} already existed"
                    if failed:
                        sl_msg += f", {failed} failed"
                    self._log(f"  {sl_msg}", C["green"])
                    log.info(f"  {sl_msg}")
                    if created > 0:
                        actions_taken.append(sl_msg)

                # ── Auto-commit Stop hook ──────────────────────────────────────
                if add_git_hook:
                    hook_actions = _scaffold_git_hook(path)
                    for action in hook_actions:
                        self._log(f"  {action}", C["green"])
                    actions_taken.extend(hook_actions)

                log.info(f"RETROFIT complete: {actions_taken or 'nothing changed'}")
                self._log(f"Retrofit complete: {path}", C["green"])
                self.after(0, self.refresh)

                if actions_taken:
                    summary = "\n".join(f"  ✔ {a}" for a in actions_taken)
                    msg = f"{name}:\n\n{summary}"
                    if any(a.startswith("Created build.ps1") for a in actions_taken):
                        msg += "\n\nNext step: open build.ps1 and replace [ENTRY_SCRIPT] and [OUTPUT_NAME] before building."
                else:
                    msg = f"{name}:\n\n  Everything was already up to date — nothing changed."
                self.after(0, lambda: messagebox.showinfo("Retrofit complete", msg, parent=self))

            except Exception as e:
                log.exception(f"RETROFIT failed: {path}")
                self._log(f"  Error: {e}", C["red"])
                self.after(0, lambda: messagebox.showerror("Retrofit failed", str(e), parent=self))

        threading.Thread(target=worker, daemon=True).start()


# ── Retrofit dialog ────────────────────────────────────────────────────────────

class RetrofitDialog(tk.Toplevel):
    """Small dialog with two checkboxes for the retrofit options."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Retrofit Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.callback = callback
        self.path = path

        pad = {"padx": 20, "pady": 8}

        tk.Label(self, text="Retrofit options",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 4))

        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"], fg=C["overlay0"]).pack(
                 anchor=tk.W, padx=20, pady=(0, 10))

        # Checkbox: tokensave integration
        self.var_ts = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Add tokensave rules to CLAUDE.md",
            variable=self.var_ts,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Prepends an @include line so Claude always loads the\n"
                 "  tokensave lookup table. Non-destructive — existing content kept.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self.var_bi = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
            text="Also create BASIC_INSTRUCTIONS.md",
            variable=self.var_bi,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Drops a full project template (overview, architecture,\n"
                 "  key files, rules) for Claude to fill in on first use.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self.var_nuitka = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add Nuitka build files",
            variable=self.var_nuitka,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text=f"  Copies build templates from the templates folder.{chr(10)}"
                 "  Edit [ENTRY_SCRIPT] and [OUTPUT_NAME] in build.ps1 before building.\n"
                 f"  {nuitka_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Shadow extension links
        self.var_shadow = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Generate shadow extension links",
            variable=self.var_shadow,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # Count existing shadow files
        existing_shadows = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        shadow_note = (f"  {existing_shadows} shadow file(s) already exist."
                       if existing_shadows else
                       "  None exist yet — click Apply to create them.")
        tk.Label(self,
            text="  Creates NTFS hardlinks (.zs→.cpp, .zsc→.cpp, .acs→.c, DECORATE→.cpp)\n"
                 "  so tokensave can parse ZScript/ACS/DECORATE as C++/C. Zero disk cost.\n"
                 "  Adds gitignore patterns. Use 🔗 Shadow Links… for custom mappings.\n"
                 f"  {shadow_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 6))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Auto-commit Stop hook
        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_note = "  (already present)" if os.path.isfile(hook_settings) else "  (.claude/settings.json)"
        self.var_hook = tk.BooleanVar(value=False)
        tk.Checkbutton(self,
            text="Add auto-commit Stop hook",
            variable=self.var_hook,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        tk.Label(self,
            text="  Auto-commits when Claude finishes a session in this project.\n"
                 "  Only commits when the working tree has changes. Safe on clean repos.\n"
                 f"  {hook_note.strip()}",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 12))

        # Buttons
        btn_frame = tk.Frame(self, bg=C["base"])
        btn_frame.pack(fill=tk.X, padx=20, pady=(0, 16))

        ttk.Button(btn_frame, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        # Centre over parent
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        ts     = self.var_ts.get()
        bi     = self.var_bi.get()
        nuitka = self.var_nuitka.get()
        shadow = self.var_shadow.get()
        hook   = self.var_hook.get()
        self.destroy()
        if ts or bi or nuitka or shadow or hook:
            self.callback(self.path, ts, bi, nuitka, shadow, add_git_hook=hook)


# ── Scaffold dialog ────────────────────────────────────────────────────────────

class ScaffoldDialog(tk.Toplevel):
    """Options dialog shown before scaffolding a new project."""

    def __init__(self, parent, path, callback):
        super().__init__(parent)
        self.title("Scaffold Project")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self.path = path
        self.callback = callback

        name = os.path.basename(path)
        has_bi = os.path.isfile(os.path.join(path, "BASIC_INSTRUCTIONS.md"))
        has_db = os.path.isfile(os.path.join(path, ".tokensave", "tokensave.db"))

        pad = dict(padx=20, pady=6)

        # Folder display
        tk.Label(self, text="Folder", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=path, bg=C["surface0"], fg=C["text"],
                 font=("Consolas", 9), padx=10, pady=6,
                 wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=20, pady=(2, 10))

        # Checkbox: BASIC_INSTRUCTIONS.md
        self._bi_var = tk.BooleanVar(value=not has_bi)
        bi_text = "Create BASIC_INSTRUCTIONS.md"
        bi_note = "  (already exists — will overwrite)" if has_bi else "  (Claude instruction template)"
        bi_frame = tk.Frame(self, bg=C["base"])
        bi_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(bi_frame, text=bi_text, variable=self._bi_var).pack(side=tk.LEFT)
        tk.Label(bi_frame, text=bi_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Checkbox: tokensave init
        self._init_var = tk.BooleanVar(value=not has_db)
        init_text = "Run tokensave init"
        init_note = "  (already indexed)" if has_db else "  (builds the code graph — ~10–30s)"
        init_frame = tk.Frame(self, bg=C["base"])
        init_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(init_frame, text=init_text, variable=self._init_var).pack(side=tk.LEFT)
        tk.Label(init_frame, text=init_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Info note
        tk.Label(self,
                 text="Project appears in the list immediately while indexing runs in the background.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420).pack(padx=20, pady=(4, 8))

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        # Checkbox: Nuitka build files
        has_ps1 = os.path.isfile(os.path.join(path, "build.ps1"))
        nuitka_note = "  (build.ps1 already exists)" if has_ps1 else "  (build.ps1 + build.bat)"
        self._nuitka_var = tk.BooleanVar(value=False)
        nuitka_frame = tk.Frame(self, bg=C["base"])
        nuitka_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(nuitka_frame, text="Add Nuitka build files",
                        variable=self._nuitka_var).pack(side=tk.LEFT)
        tk.Label(nuitka_frame, text=nuitka_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        tk.Label(self,
                 text="Copies build.ps1 + build.bat from templates. Edit [ENTRY_SCRIPT] and\n"
                      "[OUTPUT_NAME] in build.ps1 before running your first build.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 8))

        # Checkbox: auto-commit Stop hook
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 4))

        hook_settings = os.path.join(path, ".claude", "settings.json")
        hook_exists = os.path.isfile(hook_settings)
        hook_note = "  (already present)" if hook_exists else "  (.claude/settings.json)"
        self._hook_var = tk.BooleanVar(value=False)
        hook_frame = tk.Frame(self, bg=C["base"])
        hook_frame.pack(anchor=tk.W, **pad)
        ttk.Checkbutton(hook_frame, text="Add auto-commit Stop hook",
                        variable=self._hook_var).pack(side=tk.LEFT)
        tk.Label(hook_frame, text=hook_note, bg=C["base"],
                 fg=C["overlay0"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Label(self,
                 text="  Auto-commits when Claude finishes a session in this project.\n"
                      "  Safe: only commits if the working tree has changes.",
                 bg=C["base"], fg=C["overlay0"], font=("Segoe UI", 9),
                 wraplength=420, justify=tk.LEFT).pack(padx=20, pady=(0, 12))

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _apply(self):
        create_bi       = self._bi_var.get()
        run_init        = self._init_var.get()
        scaffold_nuitka = self._nuitka_var.get()
        add_git_hook    = self._hook_var.get()
        self.destroy()
        self.callback(self.path, create_bi=create_bi, run_init=run_init,
                      scaffold_nuitka=scaffold_nuitka, add_git_hook=add_git_hook)


# ── Settings dialog ────────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    """Edit manager-config.json through the GUI."""

    def __init__(self, parent, cfg: dict, save_fn, callback, startup_note=""):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._cfg = cfg
        self._save_fn = save_fn
        self._callback = callback

        pad = dict(padx=20, pady=4)

        if startup_note:
            tk.Label(self, text=startup_note,
                     bg=C["red"], fg=C["mantle"],
                     font=("Segoe UI", 9, "bold"),
                     justify=tk.LEFT, padx=14, pady=8,
                     wraplength=440).pack(fill=tk.X, pady=(0, 4))

        def field_row(label, key, is_file=False, is_dir=False, note=""):
            tk.Label(self, text=label, bg=C["base"], fg=C["subtext"],
                     font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(10, 0))
            row = tk.Frame(self, bg=C["base"])
            row.pack(fill=tk.X, padx=20, pady=(2, 0))
            var = tk.StringVar(value=cfg.get(key, ""))
            entry = ttk.Entry(row, textvariable=var, width=52)
            entry.pack(side=tk.LEFT, padx=(0, 6))
            def browse(v=var, f=is_file, d=is_dir):
                if f:
                    p = filedialog.askopenfilename(
                        title=f"Select {label}", filetypes=[("Executable", "*.exe"), ("All", "*.*")],
                        initialfile=v.get(), parent=self)
                elif d:
                    p = filedialog.askdirectory(title=f"Select {label}", parent=self)
                else:
                    return
                if p:
                    v.set(p)
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT)
            if note:
                tk.Label(row, text=note, bg=C["base"], fg=C["overlay0"],
                         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 0))
            return var

        self._exe_var  = field_row("tokensave.exe  —  path to the tokensave binary",
                                   "tokensave_exe", is_file=True)
        self._tmpl_var = field_row("Template directory  —  folder containing claude-md-template.md and project-baseline.md",
                                   "template_dir", is_dir=True,
                                   note="(leave blank to auto-detect)")
        self._editor_var = field_row(
            "Editor command  —  launched by 'Open in Editor' (e.g. code, code --new-window, notepad)",
            "editor_cmd", note="(flags supported)")

        # ── Search roots — two-column Treeview (label + path) ──
        tk.Label(self,
                 text="Search roots  —  each root's label becomes a category in the project list",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(12, 0))

        roots_frame = tk.Frame(self, bg=C["base"])
        roots_frame.pack(fill=tk.X, padx=20, pady=(4, 0))

        tv_wrap = tk.Frame(roots_frame, bg=C["mantle"])
        tv_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self._roots_tv = ttk.Treeview(
            tv_wrap,
            columns=("label", "path"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self._roots_tv.heading("label", text="Label")
        self._roots_tv.heading("path",  text="Path")
        self._roots_tv.column("label", width=130, stretch=False)
        self._roots_tv.column("path",  width=300)
        roots_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                   command=self._roots_tv.yview)
        self._roots_tv.configure(yscrollcommand=roots_vsb.set)
        self._roots_tv.pack(side=tk.LEFT, fill=tk.X, expand=True)
        roots_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Populate from config (support legacy bare strings)
        for r in cfg.get("search_roots", []):
            lbl  = _root_label(r)
            path_val = _root_path(r)
            self._roots_tv.insert("", tk.END, values=(lbl, path_val))

        root_btns = tk.Frame(roots_frame, bg=C["base"])
        root_btns.pack(side=tk.LEFT, anchor=tk.N)
        ttk.Button(root_btns, text="+ Add",
                   command=self._add_root).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Edit Label",
                   command=self._edit_root_label).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(root_btns, text="Remove",
                   command=self._remove_root).pack(fill=tk.X)

        # ── Auto-commit toggle ──
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(12, 8))

        self._var_autocommit = tk.BooleanVar(value=bool(cfg.get("auto_commit_after_sync", False)))
        tk.Checkbutton(self,
            text="Auto-commit after sync  (git add -A + git commit)",
            variable=self._var_autocommit,
            bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(self,
            text="  Only fires when the project is a git repo and the working tree has changes.\n"
                 "  Commit message: \"chore: tokensave sync\"",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(8, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _add_root(self):
        p = filedialog.askdirectory(title="Add search root", parent=self)
        if not p:
            return
        default_lbl = os.path.basename(p.rstrip("/\\"))
        lbl = simpledialog.askstring(
            "Category label",
            f"Label for this category:\n(shown as the group header in the project list)",
            initialvalue=default_lbl,
            parent=self,
        )
        if lbl is None:
            return   # user cancelled
        self._roots_tv.insert("", tk.END, values=(lbl.strip() or default_lbl, p))

    def _edit_root_label(self):
        sel = self._roots_tv.selection()
        if not sel:
            return
        iid = sel[0]
        cur_lbl  = self._roots_tv.set(iid, "label")
        new_lbl  = simpledialog.askstring(
            "Edit label", "New label:", initialvalue=cur_lbl, parent=self)
        if new_lbl is not None:
            self._roots_tv.set(iid, "label", new_lbl.strip() or cur_lbl)

    def _remove_root(self):
        sel = self._roots_tv.selection()
        if sel:
            self._roots_tv.delete(sel[0])

    def _save(self):
        exe = self._exe_var.get().strip()
        if exe and not os.path.isfile(exe):
            messagebox.showwarning("Not found",
                f"tokensave.exe not found at:\n{exe}", parent=self)
            return
        self._cfg["tokensave_exe"] = exe
        self._cfg["template_dir"]  = self._tmpl_var.get().strip()
        self._cfg["editor_cmd"]    = self._editor_var.get().strip() or "code"
        # python_exe is intentionally not exposed in the UI (used by the .bat
        # launcher only); preserve whatever value is already in the config.
        self._cfg["search_roots"] = [
            {"path": self._roots_tv.set(iid, "path"),
             "label": self._roots_tv.set(iid, "label")}
            for iid in self._roots_tv.get_children()
        ]
        self._cfg["auto_commit_after_sync"] = self._var_autocommit.get()
        self._save_fn(self._cfg)
        self.destroy()
        self._callback()


# ── Snippet edit dialog ────────────────────────────────────────────────────────

class SnippetEditDialog(tk.Toplevel):
    """Add or edit a user-defined prompt snippet."""

    def __init__(self, parent, edit_meta, callback):
        """
        edit_meta: None for new snippet, or the _active_snippets_map entry for editing.
        callback(title, text, edit_meta): called on save; edit_meta is passed back.
        """
        super().__init__(parent)
        self.title("Add Snippet" if edit_meta is None else "Edit Snippet")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._callback = callback
        self._edit_meta = edit_meta

        pad = dict(padx=20, pady=4)

        tk.Label(self,
                 text="Add Snippet" if edit_meta is None else "Edit Snippet",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 8))

        # Title field
        tk.Label(self, text="Title", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._title_var = tk.StringVar(
            value=edit_meta["data"]["title"] if edit_meta else "")
        ttk.Entry(self, textvariable=self._title_var, width=52).pack(
            anchor=tk.W, padx=20, pady=(2, 6))

        # Body field
        tk.Label(self, text="Prompt text", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)

        body_wrap = tk.Frame(self, bg=C["mantle"])
        body_wrap.pack(fill=tk.X, padx=20, pady=(2, 12))
        vsb = ttk.Scrollbar(body_wrap, orient="vertical")
        self._body_txt = tk.Text(
            body_wrap, height=8, width=52,
            font=("Segoe UI", 9), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=8, pady=6, wrap=tk.WORD,
            yscrollcommand=vsb.set,
        )
        vsb.configure(command=self._body_txt.yview)
        self._body_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        if edit_meta:
            self._body_txt.insert(tk.END, edit_meta["data"]["text"])

        # Buttons
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16))
        ttk.Button(btn_row, text="Save", style="Primary.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        title = self._title_var.get().strip().replace("\n", " ")
        text  = self._body_txt.get("1.0", tk.END).strip()

        if not title:
            messagebox.showwarning("Empty title",
                "Please enter a title for this snippet.", parent=self)
            return
        if not text:
            messagebox.showwarning("Empty text",
                "Please enter the prompt text.", parent=self)
            return

        self.destroy()
        self._callback(title, text, self._edit_meta)


# ── Shadow Links dialog ────────────────────────────────────────────────────────

class ShadowLinksDialog(tk.Toplevel):
    """
    Configure and run shadow extension link generation for a project.
    Lets the user review/edit the extension map before applying.
    """

    def __init__(self, parent, path, callback):
        """
        callback(path, ext_map, run_sync): called on Apply.
        ext_map: dict mapping source extension → shadow suffix (e.g. {'.zsc': '.cpp'})
        """
        super().__init__(parent)
        self.title("Shadow Extension Links")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path = path
        self._callback = callback

        pad = dict(padx=20, pady=6)

        tk.Label(self,
                 text="🔗  Shadow Extension Links",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))

        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        tk.Label(self,
            text="Creates NTFS hardlinks with an appended extension so tokensave's\n"
                 "tree-sitter parsers can index non-standard file types. Hardlinks\n"
                 "cost zero extra disk space and update instantly with the source.",
            font=("Segoe UI", 9), bg=C["base"], fg=C["subtext"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 10))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── Extension map editor ──
        tk.Label(self,
                 text="Mapping  (one per line:  .ext = .suffix  or  FILENAME = .suffix)",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        map_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        map_frame.pack(fill=tk.X, padx=20, pady=(0, 4))
        self._map_text = tk.Text(map_frame, height=6, width=36,
                                  bg=C["mantle"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief=tk.FLAT, font=("Consolas", 10),
                                  padx=8, pady=6)
        self._map_text.pack(fill=tk.X)

        # Populate with DEFAULT_SHADOW_EXT_MAP
        for src_ext, tgt_suf in DEFAULT_SHADOW_EXT_MAP.items():
            self._map_text.insert(tk.END, f"{src_ext} = {tgt_suf}\n")

        tk.Label(self,
            text="  .ext = .suffix  →  extension match  (e.g. .txt = .cpp for HyperV files)\n"
                 "  NAME = .suffix  →  exact filename, case-insensitive  (e.g. DECORATE = .cpp)",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 8))

        # ── Status summary ──
        existing = sum(
            1 for r, _, fs in os.walk(path)
            for f in fs
            if any(f.endswith(src + tgt)
                   for src, tgt in DEFAULT_SHADOW_EXT_MAP.items())
        )
        status_col = C["green"] if existing else C["overlay0"]
        status_txt = (f"✔  {existing} shadow file(s) already exist in this project."
                      if existing else "No shadow files found — none created yet.")
        tk.Label(self, text=status_txt,
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=status_col).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))

        # ── Options ──
        self._var_sync = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Run tokensave sync after generating links",
                       variable=self._var_sync,
                       bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
                       activebackground=C["base"], activeforeground=C["text"],
                       font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(8, 16))

        ttk.Button(btn_row, text="Apply", style="Primary.TButton",
                   command=self._apply).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _parse_ext_map(self) -> dict:
        """Parse the text widget content into an ext_map dict.

        Two valid line formats:
          .ext = .suffix   → extension-based match (dot-prefixed key)
          NAME = .suffix   → exact filename match, case-insensitive (e.g. DECORATE)
        Lines starting with '#' and blank lines are ignored.
        """
        ext_map = {}
        for line in self._map_text.get("1.0", tk.END).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            src, _, tgt = line.partition("=")
            src = src.strip()
            tgt = tgt.strip()
            # tgt must be a dot-suffix; src can be a dot-extension OR a bare filename
            if tgt.startswith(".") and src:
                ext_map[src] = tgt
        return ext_map

    def _apply(self):
        ext_map = self._parse_ext_map()
        if not ext_map:
            messagebox.showwarning("No mappings",
                "Please define at least one extension mapping.", parent=self)
            return
        run_sync = self._var_sync.get()
        self.destroy()
        self._callback(self._path, ext_map, run_sync)


# ── Git Commit dialog ──────────────────────────────────────────────────────────

# ── Assign Category dialog ────────────────────────────────────────────────────

class AssignCategoryDialog(tk.Toplevel):
    """Assign or override the category (and optional sub-category) for a project.

    Categories are sourced from search-root labels and existing overrides.
    Both comboboxes are editable so the user can type a new category/sub-category
    without any prior setup.

    Callback signature: callback(path, cat_or_None, subcat_str)
    Passing cat=None means "clear override" (restore root default).
    """

    def __init__(self, parent, path: str,
                 all_cats: list, subs_by_cat: dict,
                 current: dict, callback):
        super().__init__(parent)
        self.title("Assign Category")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path     = path
        self._subs_map = subs_by_cat
        self._callback = callback

        pad = dict(padx=20, pady=4)

        # ── Title ──
        tk.Label(self, text="📁  Assign Category",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 0))
        tk.Label(self, text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 8))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Category ──
        tk.Label(self, text="Category:", bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._cat_var = tk.StringVar(value=current.get("category", ""))
        self._cat_cb  = ttk.Combobox(self, textvariable=self._cat_var,
                                     values=all_cats, width=36)
        self._cat_cb.pack(anchor=tk.W, padx=20, pady=(0, 8))
        self._cat_cb.bind("<<ComboboxSelected>>", self._on_cat_changed)
        self._cat_var.trace_add("write", lambda *_: self._on_cat_changed())

        # ── Sub-category ──
        tk.Label(self, text="Sub-category:  (optional)",
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(anchor=tk.W, **pad)
        self._sub_var = tk.StringVar(value=current.get("subcategory", ""))
        self._sub_cb  = ttk.Combobox(self, textvariable=self._sub_var,
                                     values=subs_by_cat.get(self._cat_var.get(), []),
                                     width=36)
        self._sub_cb.pack(anchor=tk.W, padx=20, pady=(0, 14))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 10))

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(pady=(0, 16), padx=20, anchor=tk.W)
        ttk.Button(btn_row, text="Clear Override",
                   command=self._clear).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="OK", style="Primary.TButton",
                   command=self._ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _on_cat_changed(self, *_):
        cat  = self._cat_var.get()
        subs = self._subs_map.get(cat, [])
        self._sub_cb.configure(values=subs)

    def _ok(self):
        cat    = self._cat_var.get().strip()
        subcat = self._sub_var.get().strip()
        if not cat:
            messagebox.showwarning("Category required",
                "Enter a category name, or click 'Clear Override' to restore the default.",
                parent=self)
            return
        self.destroy()
        self._callback(self._path, cat, subcat)

    def _clear(self):
        self.destroy()
        self._callback(self._path, None, "")


class GitCommitDialog(tk.Toplevel):
    """
    Stage and commit changes in a project's git repository.
    Shows `git status --short` output so the user can see what will be committed,
    then accepts a commit message and runs git add -A + git commit.
    """

    def __init__(self, parent, path, status_text: str, is_repo: bool, callback):
        """
        callback(path, message, stage_all): called on Commit.
        status_text: output of `git status --short` (may be empty).
        is_repo: False disables the Commit button and shows a warning.
        """
        super().__init__(parent)
        self.title(f"Git Commit — {os.path.basename(path)}")
        self.configure(bg=C["base"])
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._path = path
        self._callback = callback

        pad = dict(padx=20, pady=6)

        tk.Label(self,
                 text="📝  Git Commit",
                 font=("Segoe UI", 11, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(anchor=tk.W, padx=20, pady=(16, 2))

        tk.Label(self,
                 text=os.path.basename(path),
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["overlay0"]).pack(anchor=tk.W, padx=20, pady=(0, 10))

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(0, 8))

        # ── Status panel ──
        tk.Label(self,
                 text="Working tree status:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        status_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        status_frame.pack(fill=tk.X, padx=20, pady=(0, 8))

        status_body = status_text.strip() if is_repo else "⚠  Not a git repository — run 🔧 Git Init first."
        if is_repo and not status_body:
            status_body = "(nothing to commit — working tree clean)"

        self._status_txt = tk.Text(status_frame, height=5, width=52,
                                   bg=C["mantle"], fg=C["text"],
                                   relief=tk.FLAT, font=("Consolas", 9),
                                   padx=8, pady=6, state=tk.NORMAL)
        self._status_txt.insert(tk.END, status_body)
        self._status_txt.configure(state=tk.DISABLED)
        self._status_txt.pack(fill=tk.X)

        # ── Stage all checkbox ──
        self._var_stage = tk.BooleanVar(value=True)
        tk.Checkbutton(self,
                       text="Stage all changes  (git add -A)",
                       variable=self._var_stage,
                       bg=C["base"], fg=C["text"], selectcolor=C["surface0"],
                       activebackground=C["base"], activeforeground=C["text"],
                       font=("Segoe UI", 10)).pack(anchor=tk.W, **pad)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=20, pady=(4, 8))

        # ── Commit message ──
        tk.Label(self,
                 text="Commit message:",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 4))

        msg_frame = tk.Frame(self, bg=C["mantle"], relief=tk.FLAT, bd=1)
        msg_frame.pack(fill=tk.X, padx=20, pady=(0, 12))
        self._msg_txt = tk.Text(msg_frame, height=3, width=52,
                                bg=C["mantle"], fg=C["text"],
                                insertbackground=C["text"],
                                relief=tk.FLAT, font=("Segoe UI", 10),
                                padx=8, pady=6)
        self._msg_txt.pack(fill=tk.X)
        self._msg_txt.focus_set()

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 16))

        self._commit_btn = ttk.Button(btn_row, text="Commit",
                                      style="Primary.TButton",
                                      command=self._apply,
                                      state=tk.NORMAL if is_repo else tk.DISABLED)
        self._commit_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel",
                   command=self.destroy).pack(side=tk.LEFT)

        # Bind Enter in message box to commit
        self._msg_txt.bind("<Control-Return>", lambda e: self._apply())

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _apply(self):
        message = self._msg_txt.get("1.0", tk.END).strip()
        if not message:
            messagebox.showwarning("Empty message",
                "Please enter a commit message.", parent=self)
            return
        stage_all = self._var_stage.get()
        self.destroy()
        self._callback(self._path, message, stage_all)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_instance_lock():
        _bring_existing_to_front()
        sys.exit(0)
    app = App()
    app.mainloop()
