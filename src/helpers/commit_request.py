"""Commit-request handoff — external tools propose commits, the manager
handles them.

An external tool (typically a Claude Code session working in the project)
writes ``.tokensave-manager/commit_request.json``:

    {
      "files": ["src/app.py", "tests/test_app.py"],   // repo-relative
      "suggested_scope": "feat(app)",                  // optional
      "note": "lazy-load pystray so CI collection survives",  // optional
      "created_at": "2026-06-09 14:30"                 // optional, display only
    }

The manager surfaces the request as a banner on the Git tab; clicking
through opens the Git Commit dialog with ONLY the requested files
pre-checked and the note shown. The user still reviews, edits, and
clicks Commit — the propose-only invariant holds: nothing is committed
without explicit user action in the manager UI.

The request file is consumed (deleted) when the user commits from a
seeded dialog, or explicitly via the banner's Dismiss button.

Pure-function module — no Tkinter. Safe to call from any thread.
"""

from __future__ import annotations

import hashlib
import json
import os

_REQUEST_DIRNAME  = ".tokensave-manager"
_REQUEST_FILENAME = "commit_request.json"


def request_path(project_root: str) -> str:
    """Absolute path of the commit-request file for *project_root*."""
    return os.path.join(project_root, _REQUEST_DIRNAME, _REQUEST_FILENAME)


def load_commit_request(project_root: str) -> "dict | None":
    """Read + validate the pending commit request, or None.

    Returns a dict with normalised keys:
      files            list[str], repo-relative, forward slashes, non-empty
      suggested_scope  str (may be "")
      note             str (may be "")
      created_at       str (may be "")
    None when the file is missing, unparseable, or has no usable files
    list — callers never need to distinguish those cases.
    """
    path = request_path(project_root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        return None
    files = [str(f).replace("\\", "/").strip()
             for f in raw_files if str(f).strip()]
    if not files:
        return None
    return {
        "files":           files,
        "suggested_scope": str(data.get("suggested_scope") or ""),
        "note":            str(data.get("note") or ""),
        "created_at":      str(data.get("created_at") or ""),
    }


#: What makes two requests the SAME request. `created_at` is deliberately not
#: in here: it records when a request was filed, not what it asks for, so two
#: identical proposals a minute apart must not read as a conflict.
IDENTITY_FIELDS = ("files", "suggested_scope", "note")


def normalise_request(files, suggested_scope: str = "",
                      note: str = "") -> dict:
    """The canonical form of a request, for comparison and for writing.

    One function so both sides normalise identically. They did not: the reader
    stripped whitespace from each path while the CLI's conflict check did not,
    so a path with a trailing space could never compare equal to itself and
    every re-file looked like a conflict.
    """
    return {
        "files": [str(f).replace(chr(92), "/").strip()
                  for f in (files or []) if str(f).strip()],
        "suggested_scope": str(suggested_scope or ""),
        "note": str(note or ""),
    }


def request_identity(request: "dict | None") -> str:
    """A stable hash of what a request ASKS FOR. "" for nothing pending.

    Deterministic by construction — sorted keys, no insignificant whitespace —
    so the answer does not depend on dict ordering or on how the file happened
    to be serialised. Comparing re-serialised JSON text instead would make the
    verdict an artefact of `indent=` and key order rather than of content.
    """
    if not request:
        return ""
    canonical = normalise_request(request.get("files"),
                                  request.get("suggested_scope"),
                                  request.get("note"))
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_commit_request(project_root: str, files: list,
                         suggested_scope: str = "", note: str = "",
                         created_at: str = "") -> str:
    """Write a commit request; returns the file path.

    Provided for tests and for tools that import the manager's helpers
    directly — external tools may equally write the JSON themselves.
    """
    path = request_path(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "files":           [str(f).replace("\\", "/") for f in files],
        "suggested_scope": suggested_scope,
        "note":            note,
        "created_at":      created_at,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def clear_commit_request(project_root: str) -> None:
    """Delete the pending request. Idempotent — missing file is fine."""
    try:
        os.remove(request_path(project_root))
    except OSError:
        pass
