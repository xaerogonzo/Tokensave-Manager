"""Hardlink-based shadow-file management for tokensave.

tokensave's indexer recognises files by extension (it looks up the right
tree-sitter grammar by extension). When a project uses a non-standard
extension (e.g. ZScript .zsc, ACS .acs, or extensionless Doom lumps like
DECORATE), we create NTFS hardlinks so the same bytes appear under a
recognised extension — `Blood.zsc` gets a sibling `Blood.zsc.cpp` link,
and tokensave indexes both as C++ code.

All three functions are pure: they take a project path and an ext_map
({src_pattern: dst_suffix}) and operate on the filesystem. No module
globals are read.
"""

from __future__ import annotations

import os


# Suffixes that share inode with their source via os.link(). The skip set
# below prevents walking into virtualenvs, build outputs, etc.
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
