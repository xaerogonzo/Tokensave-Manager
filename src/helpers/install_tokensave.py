"""install_tokensave — GitHub-release-driven lifecycle for the tokensave binary (v4.8).

The tokensave project ships prebuilt Windows binaries on its GitHub
Releases page; the manager downloads + extracts them to a per-user
location so installation never requires admin privileges or any
third-party package manager.

Public surface (the Tool Manager dialog consumes these):
  * ``latest_tokensave_release()`` — poll GitHub releases API, return
    ``(version, asset_url, err)``. G-E: HTTP 403 (rate limit) gets its
    own error tag so the caller surfaces a specific message.
  * ``download_tokensave_zip(asset_url, dest_zip_path)`` — stream
    download with magic-byte verification (G-E defence-in-depth against
    a JSON error response that slipped past the status-code check).
  * ``extract_tokensave_zip(zip_path, extract_dir)`` — manual
    member-by-member extraction with Zip-Slip guards (G-C).
  * ``manager_install_dir()`` — ``%LOCALAPPDATA%\\TokenSaveManager\\bin``.
  * ``install_tokensave_via_download(on_log, on_progress)`` — full
    end-to-end orchestration.
  * ``is_manager_installed(exe_path)`` — True iff exe_path is under
    the manager-owned dir, gates auto-delete on Uninstall.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
import zipfile
from typing import Callable, Optional, Tuple

_GITHUB_RELEASES_API = (
    "https://api.github.com/repos/aovestdipaperino/tokensave/releases/latest"
)
_RELEASES_HUMAN_URL = (
    "https://github.com/aovestdipaperino/tokensave/releases/latest"
)
# Asset filename pattern for Windows binaries (verified live on the
# tokensave releases page on 2026-05-27).
_WINDOWS_ASSET_SUFFIX = "-x86_64-windows.zip"

# Magic bytes for a ZIP archive — G-E magic-byte verification catches
# a JSON / HTML error body that somehow slipped past the status-code
# guard. PK\x03\x04 is the canonical local-file-header signature.
_ZIP_MAGIC = b"PK\x03\x04"


# ── Manager-owned install location ────────────────────────────────────────────

def manager_install_dir() -> str:
    """Return ``%LOCALAPPDATA%\\TokenSaveManager\\bin\\`` (created if absent).

    Per-user, no admin required, isolated from any user-side manual
    install. Manager owns this directory exclusively — only binaries
    extracted here get auto-deleted on Uninstall.
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    target = os.path.join(local, "TokenSaveManager", "bin")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        pass
    return target


def is_manager_installed(exe_path: str) -> bool:
    """Return True iff ``exe_path`` lives inside ``manager_install_dir()``.

    Uses ``os.path.commonpath()`` to avoid prefix-substring spoofing
    (e.g. ``C:\\TokenSaveManager-Lookalike\\…`` is correctly classified
    as NOT manager-owned even though it shares a literal prefix string
    with the real manager dir).
    """
    if not exe_path:
        return False
    try:
        target = os.path.abspath(manager_install_dir())
        exe    = os.path.abspath(exe_path)
        if not os.path.exists(exe):
            return False
        common = os.path.commonpath([target, exe])
        return common == target
    except (ValueError, OSError):
        return False


# ── GitHub-releases polling (G-E rate-limit aware) ────────────────────────────

def latest_tokensave_release() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Poll the GitHub releases API for the latest tokensave version.

    Returns ``(version, asset_url, err)`` where:
      * On success: ``(version_str, asset_url, None)``
      * On HTTP 403: ``(None, None, 'rate_limit')`` so caller can show
        the specific "GitHub anonymous rate limit hit" message with
        the Releases-page URL fallback. G-E.
      * On any other error: ``(None, None, '<short_err_descriptor>')``

    Bounded 10 s timeout. Does NOT use auth — relies on the anonymous
    60 req/hr quota per IP. The existing ``UpdatePollerController``
    also hits this endpoint hourly; G-E acknowledges users may exhaust
    the quota and ships a clean error path instead of a confused
    download failure.
    """
    req = urllib.request.Request(
        _GITHUB_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "TokenSaveManager/v4.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # G-E: distinguish the rate-limit case explicitly
        if exc.code == 403:
            return None, None, "rate_limit"
        return None, None, f"http_{exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, None, f"network_err: {exc}"
    if status != 200:
        return None, None, f"unexpected_status_{status}"
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, None, f"json_err: {exc}"
    version = (data.get("tag_name") or "").lstrip("v") or None
    asset_url = None
    for asset in (data.get("assets") or []):
        name = asset.get("name") or ""
        if name.endswith(_WINDOWS_ASSET_SUFFIX):
            asset_url = asset.get("browser_download_url")
            break
    if not version or not asset_url:
        return None, None, "no_windows_asset"
    return version, asset_url, None


def releases_human_url() -> str:
    """Return the human-friendly Releases page URL (for the rate-limit hint)."""
    return _RELEASES_HUMAN_URL


# ── Download with magic-byte verification (G-E) ──────────────────────────────

def download_tokensave_zip(
    asset_url: str,
    dest_zip_path: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, str]:
    """Stream the Windows zip to ``dest_zip_path``.

    ``on_progress(bytes_done, total_bytes)`` is called per chunk if
    provided so the Tool Manager dialog can render a progress hint.
    ``total_bytes`` may be 0 if the server doesn't send Content-Length.

    G-E defence-in-depth: after the download completes, opens the
    resulting file and verifies the first 4 bytes are the ZIP local-
    file-header signature ``PK\\x03\\x04``. Catches the case where
    some upstream issue served a JSON / HTML error body that the
    status-code guard in ``latest_tokensave_release`` missed. On
    magic-byte failure, deletes the bad file and returns
    ``(False, 'not_a_zip')``.

    Returns ``(ok, err_or_empty)``.
    """
    req = urllib.request.Request(
        asset_url, headers={"User-Agent": "TokenSaveManager/v4.8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            done = 0
            chunk = 64 * 1024
            with open(dest_zip_path, "wb") as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    if on_progress is not None:
                        try:
                            on_progress(done, total)
                        except Exception:
                            pass
    except urllib.error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"network_err: {exc}"

    # G-E magic-byte verification
    try:
        with open(dest_zip_path, "rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        return False, f"read_back_err: {exc}"
    if head != _ZIP_MAGIC:
        try:
            os.unlink(dest_zip_path)
        except OSError:
            pass
        return False, "not_a_zip"
    return True, ""


# ── Zip-Slip-safe extraction (G-C) ───────────────────────────────────────────

def _is_safe_zip_member(member_name: str, extract_dir_abs: str) -> bool:
    """Return False if ``member_name`` would escape ``extract_dir_abs``.

    G-C: rejects parent-traversal (``..``), absolute paths
    (``/foo`` or ``C:\\foo``), and any computed target whose abspath
    doesn't start with ``extract_dir_abs + os.sep``.
    """
    # Normalise separator chars — zip entries use '/' regardless of OS
    name = member_name.replace("\\", "/")
    # Reject absolute paths and Windows drive-letter paths outright
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return False
    # Reject any segment that's exactly '..'
    parts = name.split("/")
    if any(p == ".." for p in parts):
        return False
    # Final abspath check
    target = os.path.abspath(os.path.join(extract_dir_abs, *parts))
    return target.startswith(extract_dir_abs + os.sep) or target == extract_dir_abs


def extract_tokensave_zip(zip_path: str, extract_dir: str
                            ) -> Tuple[str, str]:
    """Extract the tokensave zip with Zip-Slip guards.

    Returns ``(abs_path_to_tokensave_exe, err_or_empty)``. On a
    suspicious archive member (parent traversal, absolute path, etc.)
    aborts on the FIRST violation without extracting anything else and
    returns ``('', 'zip_slip')``. Otherwise extracts every safe member
    and returns the absolute path to the extracted ``tokensave.exe``.

    If the archive contains no ``tokensave.exe``, returns
    ``('', 'no_exe_in_archive')``.
    """
    try:
        os.makedirs(extract_dir, exist_ok=True)
    except OSError as exc:
        return "", f"mkdir_err: {exc}"
    extract_dir_abs = os.path.abspath(extract_dir)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # First pass: validate every member BEFORE writing anything.
            # If even one is suspicious, abort cleanly.
            for member in zf.namelist():
                if not _is_safe_zip_member(member, extract_dir_abs):
                    return "", "zip_slip"
            # Second pass: actually extract.
            for member in zf.namelist():
                # Skip pure directories
                if member.endswith("/"):
                    continue
                rel_parts = member.replace("\\", "/").split("/")
                target = os.path.join(extract_dir_abs, *rel_parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, OSError) as exc:
        return "", f"extract_err: {exc}"

    # Locate the extracted tokensave.exe (zip layout may include a
    # top-level versioned dir like ``tokensave-vX.Y.Z-x86_64-windows/``).
    for root, _dirs, files in os.walk(extract_dir_abs):
        for fname in files:
            if fname.lower() == "tokensave.exe":
                return os.path.join(root, fname), ""
    return "", "no_exe_in_archive"


# ── End-to-end orchestration ──────────────────────────────────────────────────

def install_tokensave_via_download(
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, str]:
    """Full install flow: poll → download → extract → return ``(ok, path_or_err)``.

    The Tool Manager dialog calls this in a worker thread and then,
    on success, updates ``cfg.raw['tokensave_exe']`` + ``cfg.save()``.

    Failure tags returned for ``ok=False``:
      * ``rate_limit``      — G-E GitHub anonymous-quota hit
      * ``http_<code>``     — non-200 response from GitHub API
      * ``no_windows_asset`` — release exists but no Windows zip asset
      * ``not_a_zip``       — G-E magic-byte guard tripped
      * ``zip_slip``        — G-C archive contained a suspicious member
      * ``no_exe_in_archive`` — extraction succeeded but no tokensave.exe
      * various ``network_err: …`` / ``extract_err: …`` strings
    """
    def _log(msg: str) -> None:
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    _log("Querying GitHub releases for the latest tokensave version…")
    version, asset_url, err = latest_tokensave_release()
    if err == "rate_limit":
        _log(
            "GitHub rate limit exceeded for this IP. Try again later, "
            f"or download manually from {releases_human_url()}"
        )
        return False, "rate_limit"
    if err or not version or not asset_url:
        _log(f"Could not resolve a Windows asset: {err or 'unknown'}")
        return False, err or "no_asset"

    _log(f"Latest release: v{version}")
    _log(f"Asset URL: {asset_url}")

    target_dir = manager_install_dir()
    zip_path = os.path.join(target_dir, f"tokensave-v{version}.zip")
    _log(f"Downloading to {zip_path}…")
    ok, derr = download_tokensave_zip(asset_url, zip_path, on_progress)
    if not ok:
        if derr == "not_a_zip":
            _log(
                "Downloaded file is not a valid zip — possibly a server "
                "error page or rate-limit response. Try again later."
            )
        else:
            _log(f"Download failed: {derr}")
        return False, derr

    _log("Verifying + extracting archive…")
    exe_path, eerr = extract_tokensave_zip(zip_path, target_dir)
    if eerr == "zip_slip":
        _log(
            "Refusing to extract — archive contains a suspicious member "
            "(parent traversal or absolute path). Aborting install."
        )
        return False, "zip_slip"
    if eerr:
        _log(f"Extract failed: {eerr}")
        return False, eerr

    # Clean up the zip — leaving it around just wastes %LOCALAPPDATA% space
    try:
        os.unlink(zip_path)
    except OSError:
        pass

    _log(f"✓ Installed: {exe_path}")
    return True, exe_path
