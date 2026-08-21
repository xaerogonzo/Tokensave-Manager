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

import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass

# Persisted per-project shadow state (R9-SL1, extended by SL2/SL3). Lives in
# the manager's cache namespace next to last_test_run.json.
# Schema:
#   {"ext_map":     {src_pattern: dst_suffix},
#    "auto_shadow": bool,      # SL2 — regenerate on every sync
#    "generated":   [str]}     # SL3 — project-relative paths WE created
# The wrapper was designed for exactly this in SL1. `generated` is what makes
# stale-shadow cleanup safe rather than a guess from filename shape.
_SHADOW_MAP_DIRNAME  = ".tokensave-manager"
_SHADOW_MAP_FILENAME = "shadow_map.json"


# Default extension map: ZScript → C++, ACS → C, GZDoom text lumps → C++.
# Keys starting with '.' are matched against the file extension
#   (e.g. ".zsc" matches "Blood.zsc" → shadow "Blood.zsc.cpp").
# Keys WITHOUT a leading dot are matched by exact filename, case-insensitive
#   (e.g. "DECORATE" matches the extensionless lump → shadow "DECORATE.cpp").
#
# R9-SL6: the GZDoom lump family below are all extensionless text definition
# lumps that the C++ grammar tolerates similarly to DECORATE. These are
# *first-open defaults only* — SL1 persistence means a user can prune any
# that index poorly for their project; the saved map then wins on reopen.
DEFAULT_SHADOW_EXT_MAP = {
    ".zs":  ".cpp",
    ".zsc": ".cpp",
    ".acs": ".c",
    # Extensionless GZDoom lumps — matched by exact filename (case-insensitive).
    "DECORATE": ".cpp",
    "MAPINFO":  ".cpp",
    "ZMAPINFO": ".cpp",
    "SNDINFO":  ".cpp",
    "SBARINFO": ".cpp",
    "LANGUAGE": ".cpp",
    "GLDEFS":   ".cpp",
    "ANIMDEFS": ".cpp",
    "MENUDEF":  ".cpp",
    "CVARINFO": ".cpp",
    "KEYCONF":  ".cpp",
}

# Suffixes that share inode with their source via os.link(). The skip set
# below prevents walking into virtualenvs, build outputs, etc.
_SHADOW_SKIP_DIRS = {".tokensave", ".git", "node_modules", "__pycache__",
                     ".venv", "venv", "target", "build", "dist", "out"}


def supports_hardlinks(path: str) -> bool:
    """Probe whether the volume holding *path* supports hardlinks (R9-SL4).

    NTFS does; FAT32/exFAT and some network drives don't — there os.link
    fails per-file and the user would only see opaque failure counts.
    Creates a throwaway probe file + link in *path* and cleans both up.
    Returns False on ANY OSError (no write access counts as unsupported —
    generation would fail anyway).
    """
    probe = os.path.join(path, f".shadow_probe_{uuid.uuid4().hex[:8]}")
    link = probe + ".lnk"
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.link(probe, link)
        return True
    except OSError:
        return False
    finally:
        for p in (link, probe):
            try:
                os.remove(p)
            except OSError:
                pass


def shadow_map_path(project_root: str) -> str:
    """Absolute path of the persisted shadow-map file for *project_root*."""
    return os.path.join(project_root, _SHADOW_MAP_DIRNAME,
                        _SHADOW_MAP_FILENAME)


@dataclass(frozen=True)
class ShadowConfig:
    """Everything persisted about one project's shadow links.

    ``generated`` is the part that matters most, and it is why SL3 can be
    safe. A file named ``Blood.zsc.cpp`` sitting next to no ``Blood.zsc``
    *looks* like a stale shadow, but the filesystem cannot tell you whether
    the manager created it or the user did. Deleting on that resemblance
    alone would destroy real work. Recording what we created turns a guess
    into a fact.

    Paths are stored project-relative with forward slashes, so the map
    survives the project being moved or shared across machines.
    """
    ext_map: dict
    auto_shadow: bool = False
    generated: tuple = ()


def _read_raw(project_root: str) -> "dict | None":
    try:
        with open(shadow_map_path(project_root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _clean_ext_map(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in raw.items()
        if str(k).strip() and str(v).strip().startswith(".")
    }


def load_shadow_config(project_root: str) -> "ShadowConfig | None":
    """Read the full persisted state, or None when there is nothing usable.

    Validation is total, matching the SL1 contract: a missing file,
    unparseable JSON, a non-dict ext_map, or a map with no usable entries all
    read as None so callers fall back to ``DEFAULT_SHADOW_EXT_MAP``.
    """
    data = _read_raw(project_root)
    if data is None:
        return None
    ext_map = _clean_ext_map(data.get("ext_map"))
    if not ext_map:
        return None
    generated = tuple(
        _norm_rel(p) for p in data.get("generated", [])
        if isinstance(p, str) and p.strip()
    ) if isinstance(data.get("generated"), list) else ()
    return ShadowConfig(ext_map=ext_map,
                        auto_shadow=bool(data.get("auto_shadow", False)),
                        generated=generated)


def save_shadow_config(project_root: str, config: ShadowConfig) -> str:
    """Persist *config*. Returns the path, or "" when the write fails.

    Written atomically (temp file + ``os.replace``) because SL2 rewrites this
    during every sync: a crash midway through a plain write would leave a
    truncated file, which reads as "no saved map" and silently discards both
    the user's extension map and the provenance record that makes cleanup
    safe. Persistence is still a nicety — a failure here never blocks
    generation.
    """
    path = shadow_map_path(project_root)
    payload = {
        "ext_map": config.ext_map,
        "auto_shadow": bool(config.auto_shadow),
        "generated": sorted(set(config.generated)),
    }
    tmp = ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".shadow_map_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
        return path
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return ""


def load_shadow_map(project_root: str) -> "dict | None":
    """The extension map alone — the SL1 interface the dialog still uses."""
    config = load_shadow_config(project_root)
    return config.ext_map if config else None


def save_shadow_map(project_root: str, ext_map: dict,
                    auto_shadow: "bool | None" = None) -> str:
    """Persist *ext_map*, preserving the fields the caller did not mention.

    Reads the existing config first so saving from the dialog cannot silently
    drop the provenance list — which would turn every recorded shadow back
    into an unprovable candidate and disable SL3 cleanup for that project.
    """
    existing = load_shadow_config(project_root)
    generated = existing.generated if existing else ()
    if auto_shadow is None:
        auto_shadow = existing.auto_shadow if existing else False
    # A shadow whose mapping was just removed is no longer ours to track.
    generated = tuple(p for p in generated if _maps_to_a_shadow(p, ext_map))
    return save_shadow_config(
        project_root,
        ShadowConfig(ext_map=ext_map, auto_shadow=bool(auto_shadow),
                     generated=generated))


def generate_shadow_links(path: str, ext_map: dict, *,
                          on_created=None) -> tuple:
    """
    Walk *path* and create NTFS hardlinks so tokensave can index
    non-standard extensions via an existing tree-sitter grammar.

    Two matching modes, determined by key format:
    - Dot-prefixed keys (".zsc") match by file extension → Blood.zsc → Blood.zsc.cpp
    - Non-dot keys ("DECORATE") match by exact filename, case-insensitive →
      DECORATE → DECORATE.cpp  (handles extensionless Doom lumps)

    Existing shadow files are left untouched.
    Returns (created, skipped, failed) counts.

    *on_created* is called with the absolute path of each link actually
    created. It exists so callers can record provenance (SL3) without this
    function growing a second return shape and breaking its existing
    three-value unpacking at every call site.
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
                    if on_created is not None:
                        on_created(dst)
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


# ── R9-SL5: unindexed-extension scanner ──────────────────────────────────

# Curated set of "good shadow targets" — brace/C-family + tolerant scripts
# that absorb foreign syntax without choking, NOT markup/data formats (there's
# no value shadowing code as .json). Constrains the AI suffix picker AND
# validates its output; also the deterministic fallback is .cpp from this set.
_SHADOW_TARGET_SUFFIXES = {
    ".cpp", ".c", ".cs", ".java", ".js", ".ts",
    ".go", ".rs", ".py", ".lua", ".php", ".rb",
}


def indexed_extensions(project_root: str) -> "set[str] | None":
    """Extensions tokensave has successfully parsed for *project_root*.

    Reads ``.tokensave/tokensave.db`` read-only and returns the set of
    lowercase extensions whose files produced at least one node (node_count
    > 0 = parsed by a grammar, not merely tracked). Returns **None** when the
    DB is absent or unreadable, so the caller can distinguish "nothing
    unindexed" from "no index to compare against — run a sync first".
    """
    db = os.path.join(project_root, ".tokensave", "tokensave.db")
    if not os.path.isfile(db):
        return None
    # uri=True is MANDATORY — without it sqlite3 treats the whole
    # "file:...?mode=ro" string as a literal filename and creates it.
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT path, node_count FROM files").fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    exts = set()
    for path, node_count in rows:
        if node_count and node_count > 0:
            ext = os.path.splitext(str(path))[1].lower()
            if ext:
                exts.add(ext)
    return exts


def suggest_shadow_candidates(project_root: str,
                              ext_map: dict) -> "list[tuple[str, int]]":
    """File extensions present on disk that tokensave is NOT indexing.

    Returns ``[(ext, file_count), ...]`` sorted by count desc (capped at 20)
    for extensions that are: not already parsed (per ``indexed_extensions``),
    not already mapped (``ext_map`` keys), and not a shadow suffix themselves
    (``ext_map`` values, e.g. ``.cpp``). Returns ``[]`` when there's no
    tokensave index to diff against (``indexed_extensions`` is None) — the
    caller shows a "sync first" hint in that case.
    """
    indexed = indexed_extensions(project_root)
    if indexed is None:
        return []
    mapped_exts = {k.lower() for k in ext_map if k.startswith(".")}
    suffixes    = {v.lower() for v in ext_map.values()}
    excluded    = indexed | mapped_exts | suffixes
    counts: "dict[str, int]" = {}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if not ext or ext in excluded:
                continue
            counts[ext] = counts.get(ext, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:20]


def _parse_suffix_lines(text: str) -> "dict[str, str]":
    """Parse ``ext = .suffix`` lines (the ShadowLinksDialog map format) into a
    dict, keeping only entries whose suffix is a known good shadow target."""
    out: "dict[str, str]" = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        src, _, tgt = line.partition("=")
        src, tgt = src.strip().lower(), tgt.strip().lower()
        if src and tgt in _SHADOW_TARGET_SUFFIXES:
            out[src] = tgt
    return out


def ai_suggest_suffixes(cfg, exts: "list[str]") -> "dict[str, str]":
    """Ask the configured LLM for the best shadow suffix per extension.

    Whitelist-constrained both ways: the prompt restricts the model to
    ``_SHADOW_TARGET_SUFFIXES``, and every returned suffix is re-validated
    against it (a hallucinated ``.zscript`` is dropped, leaving that row's
    deterministic ``.cpp`` prefill). Returns ``{}`` on any failure or when no
    LLM is configured — never raises, never blocks the scanner.
    """
    if not exts:
        return {}
    raw = cfg.raw if isinstance(getattr(cfg, "raw", None), dict) else {}
    llm_cfg = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}
    if not llm_cfg.get("provider"):
        return {}
    allowed = ", ".join(sorted(_SHADOW_TARGET_SUFFIXES))
    sys_p = (
        "You map unfamiliar source-file extensions to the closest existing "
        "tree-sitter grammar so a code indexer can parse them. Answer with "
        "one line per extension in the exact form `ext = .suffix`. The "
        f".suffix MUST be chosen from this list: {allowed}. If unsure, answer "
        ".cpp. Output ONLY the mapping lines — no prose, no code fences."
    )
    user_p = "Extensions:\n" + "\n".join(exts)
    try:
        provider = llm_cfg.get("provider", "")
        if provider == "claude_cli":
            from helpers.claude_cli import call_claude_cli_print
            claude_exe = getattr(cfg, "claude_cli_exe", "") or ""
            if not claude_exe:
                return {}
            result = call_claude_cli_print(
                claude_exe, user_p, system_prompt=sys_p,
                timeout=60, model=llm_cfg.get("model") or "")
        else:
            from helpers.llm import _call_llm
            result = _call_llm(llm_cfg, sys_p, user_p)
    except Exception:
        return {}
    if not result:
        return {}
    parsed = _parse_suffix_lines(result)
    wanted = {e.lower() for e in exts}
    return {k: v for k, v in parsed.items() if k in wanted}


# ── R9-SL2/SL3: provenance, freshness, and the four shadow states ────────
#
# A hardlink survives its source. Delete or rename `Blood.zsc` and
# `Blood.zsc.cpp` does not vanish — the inode lives on under the shadow name,
# so the index keeps serving code that no longer has a source, and git sees an
# untracked file the .gitignore pattern may no longer cover.
#
# Detecting that is easy. Acting on it safely is not, because two very
# different files can look identical from the filesystem:
#
#     a shadow WE created, whose source is gone        -> safe to clean up
#     a file the USER created that matches the pattern -> deleting it is data loss
#
# Nothing about the name distinguishes them, which is why `generated` exists.
# Anything we did not record stays a *candidate* and is never removed without
# the user saying so explicitly.

#: Source and shadow are the same inode — the link is intact.
SHADOW_HEALTHY = "healthy"
#: Both files exist but are NOT the same inode. Something replaced one of
#: them: editors that save via rename-replace break hardlinks silently, and
#: the replacement may well be deliberate. Diagnose, never touch.
SHADOW_SUSPICIOUS = "suspicious"
#: Source gone, and we recorded creating this shadow. Cleanup is safe.
SHADOW_STALE = "stale"
#: Source gone, no provenance. Looks like a stale shadow; cannot be proven to
#: be one. Surface it, label the uncertainty, delete nothing silently.
SHADOW_CANDIDATE = "candidate"


@dataclass(frozen=True)
class ShadowFinding:
    """One shadow-shaped file and what could be established about it."""
    shadow: str                  # project-relative, forward slashes
    source: str                  # the source it should be linked to
    state: str
    provenance: bool = False     # did we record creating it?

    @property
    def safe_to_remove(self) -> bool:
        """Only a stale shadow we know we made."""
        return self.state == SHADOW_STALE


def scan_shadows(project_root: str, ext_map: dict,
                 generated: "tuple | None" = None) -> list:
    """Classify every shadow-shaped file under *project_root*.

    *generated* defaults to the persisted provenance list. Pass it explicitly
    to classify against a different record (the tests do).
    """
    if generated is None:
        config = load_shadow_config(project_root)
        generated = config.generated if config else ()
    known = set(generated)

    findings = []
    suffixes = set(ext_map.values())
    src_exts = {k for k in ext_map if k.startswith(".")}
    src_names = {k.upper() for k in ext_map if not k.startswith(".")}

    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _SHADOW_SKIP_DIRS]
        for fname in files:
            base = _shadow_base(fname, suffixes, src_exts, src_names)
            if base is None:
                continue
            shadow_abs = os.path.join(root, fname)
            source_abs = os.path.join(root, base)
            rel = _rel(project_root, shadow_abs)
            findings.append(ShadowFinding(
                shadow=rel,
                source=_rel(project_root, source_abs),
                state=_classify(source_abs, shadow_abs, rel in known),
                provenance=rel in known,
            ))
    return findings


def _classify(source_abs: str, shadow_abs: str, has_provenance: bool) -> str:
    if not os.path.exists(source_abs):
        return SHADOW_STALE if has_provenance else SHADOW_CANDIDATE
    return SHADOW_HEALTHY if _same_file(source_abs, shadow_abs) \
        else SHADOW_SUSPICIOUS


def _same_file(source_abs: str, shadow_abs: str) -> bool:
    """Are these two paths the same inode?

    ``os.path.samefile`` is authoritative here and ``st_nlink`` is not: a link
    count of 2 says the inode has two names somewhere, not that *these two
    paths* are the pair. Using the count would call a coincidence a link.
    """
    try:
        return os.path.samefile(source_abs, shadow_abs)
    except OSError:
        return False


def find_stale_shadows(project_root: str, ext_map: dict,
                       generated: "tuple | None" = None) -> list:
    """Shadows we created whose source is gone. Provenance-backed only.

    Deliberately excludes ``SHADOW_CANDIDATE``: those look identical but
    cannot be shown to be ours, and this list feeds a cleanup button.
    """
    return [f for f in scan_shadows(project_root, ext_map, generated)
            if f.state == SHADOW_STALE]


def remove_stale_shadows(project_root: str, ext_map: dict) -> tuple:
    """Delete provenance-backed stale shadows. Returns (removed, failed).

    Re-scans rather than trusting a list handed in from the UI: the point of
    the provenance record is defeated if a caller can pass an arbitrary path.
    """
    removed = failed = 0
    still_generated = set()
    config = load_shadow_config(project_root)
    if config:
        still_generated = set(config.generated)

    for finding in find_stale_shadows(project_root, ext_map):
        try:
            os.remove(_abs(project_root, finding.shadow))
            removed += 1
            still_generated.discard(finding.shadow)
        except OSError:
            failed += 1
    if config and removed:
        save_shadow_config(project_root, ShadowConfig(
            ext_map=config.ext_map, auto_shadow=config.auto_shadow,
            generated=tuple(sorted(still_generated))))
    return removed, failed


def refresh_shadows(project_root: str, ext_map: "dict | None" = None) -> dict:
    """SL2: regenerate shadows and record what was created.

    The entry point a sync calls. Returns a dict describing what happened, so
    the caller can decide what deserves a log line:

        {"ran": bool, "reason": str, "created": int, "skipped": int,
         "failed": int}

    ``ran`` is False on an unsupported volume — hardlinks are NTFS-only, and
    on exFAT or a network share every single file would fail. That is a
    property of the volume, not an error worth reporting on every sync.
    """
    config = load_shadow_config(project_root)
    if ext_map is None:
        if config is None:
            return {"ran": False, "reason": "no saved map",
                    "created": 0, "skipped": 0, "failed": 0}
        ext_map = config.ext_map

    if not supports_hardlinks(project_root):
        return {"ran": False, "reason": "volume does not support hardlinks",
                "created": 0, "skipped": 0, "failed": 0}

    made = []
    created, skipped, failed = generate_shadow_links(
        project_root, ext_map, on_created=made.append)

    if made or config is None:
        known = set(config.generated) if config else set()
        known.update(_rel(project_root, p) for p in made)
        save_shadow_config(project_root, ShadowConfig(
            ext_map=ext_map,
            auto_shadow=config.auto_shadow if config else False,
            generated=tuple(sorted(known))))
    return {"ran": True, "reason": "", "created": created,
            "skipped": skipped, "failed": failed}


# ── path + name helpers ──────────────────────────────────────────────────

def _shadow_base(fname: str, suffixes: set, src_exts: set,
                 src_names: set) -> "str | None":
    """The source filename a shadow-shaped *fname* would belong to, or None.

    Mirrors ``remove_shadow_links``'s matching so the two never disagree
    about what counts as a shadow.
    """
    for suf in suffixes:
        if not fname.endswith(suf):
            continue
        base = fname[:-len(suf)]
        if not base:
            continue
        if any(base.endswith(e) for e in src_exts) or base.upper() in src_names:
            return base
    return None


def _maps_to_a_shadow(rel_path: str, ext_map: dict) -> bool:
    """Would *rel_path* still be produced by *ext_map*?"""
    return _shadow_base(
        rel_path.rsplit("/", 1)[-1],
        set(ext_map.values()),
        {k for k in ext_map if k.startswith(".")},
        {k.upper() for k in ext_map if not k.startswith(".")},
    ) is not None


def _rel(project_root: str, abs_path: str) -> str:
    try:
        return _norm_rel(os.path.relpath(abs_path, project_root))
    except ValueError:                     # different drive on Windows
        return _norm_rel(abs_path)


def _abs(project_root: str, rel_path: str) -> str:
    return os.path.join(project_root, rel_path.replace("/", os.sep))


def _norm_rel(path: str) -> str:
    """Forward slashes, no leading './' — stable across machines."""
    return path.replace("\\", "/").lstrip("./") if path.startswith("./") \
        else path.replace("\\", "/")
