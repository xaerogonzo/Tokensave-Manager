"""Manager IPC — an inbox the running Manager drains, and a way to raise it.

VS Code can already run the Manager's CLI, but it has never been able to reach
the *running* Manager. Everything it proposes lands in a file and waits for the
user to happen to refresh the right tab. This module is the channel that closes
that loop, chosen over a named pipe or a socket deliberately: no port, no
firewall prompt, no server thread, and every piece of it is a pure function that
can be tested without a GUI.

**Propose-only holds, stated precisely.** No action here mutates the project,
applies a proposal, commits, or changes approval state. Each one opens a dialog
in front of a person. Writing a request *is* a state change — that is what the
inbox is — so the invariant is about what the Manager will do on the other end,
not a claim that nothing is written.

**The payload is untrusted input.** The request file sits in the project
directory and anything on the machine can write it. This is a local IPC
protocol, so every field is validated at write time *and* again at drain time,
against an allowlist and a per-action schema. `--project` is an argument, not an
authorization: a request naming a directory outside the Manager's configured
search roots is refused rather than obeyed.

**Absence never means success.** An earlier design deleted each request on
dispatch and offered a status lookup, which cannot work: once the file is gone,
"drained" is indistinguishable from "never existed", "rejected" or
"quarantined". So a durable acknowledgement is written to `done/` *before* the
pending file is removed, and the invariant is:

    a request is never removed from pending/ until an acknowledgement exists

A crash between the two leaves a duplicate the next drain resolves, which is a
recoverable state; the alternative leaves an unexplainable gap.

Layout::

    .tokensave-manager/requests/
        pending/<created_at>-<id>.json    the request
        done/<created_at>-<id>.json       {id, action, project, outcome, at, detail}

No Tkinter — safe to call from any thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

#: Bumped when the request wire format changes incompatibly. This is a
#: *separate* format from the CLI envelope's `schema_version`: the envelope is
#: what the CLI returns, this is what a producer writes into the inbox, and the
#: extension will be a second producer of it. An unsupported version is
#: rejected with that reason rather than lumped in with malformed JSON, so the
#: two failures stay distinguishable in the ledger.
REQUEST_SCHEMA_VERSION = 1

_REQUEST_DIRNAME = ".tokensave-manager"
_REQUESTS_SUBDIR = "requests"
_PENDING = "pending"
_DONE = "done"

#: Every action opens a dialog. There is deliberately no action that performs
#: an operation, and no free-form "command" field for one to hide in.
ACTIONS = frozenset({
    "commit", "doctor", "test-manager", "savings", "doc-updates",
    "open-project",
})

#: What each action's payload may contain: (required, optional).
#:
#: Spelled out per action rather than checked as "no unknown keys" against
#: nothing, because an unnamed schema is not a constraint. Note that only
#: `commit` carries a payload at all — in particular `open-project` does NOT
#: take a project, because the request envelope already has one and two
#: possible project identities in a security-relevant message is an ambiguity,
#: not a convenience.
PAYLOAD_SCHEMA = {
    "commit": (("files",), ("scope", "note")),
    "doctor": ((), ()),
    "test-manager": ((), ()),
    "savings": ((), ()),
    "doc-updates": ((), ()),
    "open-project": ((), ()),
}

#: Caps. A request is a handoff, not a bulk transfer.
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_FILES = 500

#: Acknowledgement outcomes. `quarantined` must never render as `drained`.
DRAINED = "drained"
REJECTED = "rejected"
QUARANTINED = "quarantined"

#: Request states reported by `status_of`.
UNKNOWN = "unknown"
PENDING = "pending"

#: How many times a transient failure is retried before the request is
#: quarantined. Transient means "the Manager could not act just now" — a busy
#: dialog, a project not yet loaded — and deleting on the first stumble would
#: lose the user's action.
MAX_ATTEMPTS = 5

#: Acknowledgement retention. The correlation id is for debugging a handoff,
#: not for accumulating a permanent history database inside someone's project.
MAX_DONE_RECORDS = 200
MAX_DONE_AGE_SECONDS = 14 * 24 * 3600

#: The Manager's single-instance mutex and window title, mirrored from
#: `helpers/runtime.py`. Mirrored rather than imported because importing
#: `runtime` pulls in the logger and the tray, and because `cli.py` must never
#: touch the module that *acquires* this lock.
MUTEX_NAME = "TokenSaveManager_SingleInstance"
WINDOW_TITLE = "TokenSave Manager"


class RequestError(ValueError):
    """A request that must not be dispatched. `reason` is user-facing."""


# ── Paths ────────────────────────────────────────────────────────────────────


def requests_dir(project_root: str) -> str:
    return os.path.join(project_root, _REQUEST_DIRNAME, _REQUESTS_SUBDIR)


def pending_dir(project_root: str) -> str:
    return os.path.join(requests_dir(project_root), _PENDING)


def done_dir(project_root: str) -> str:
    return os.path.join(requests_dir(project_root), _DONE)


# ── Canonical identity ───────────────────────────────────────────────────────


def canonical_project(path: str) -> str:
    """The one spelling of a project path that comparisons may use.

    `realpath` before `normcase`, so `C:/p/foo/..` and a junction or symlink
    alias cannot produce different authorization verdicts for the same
    directory — the whole point of canonicalising in a security check.

    `os.path.normcase` is a **no-op on POSIX**, which this codebase has already
    been caught by once in CI. Containment therefore must not depend on
    case-folding for its correctness: it is applied for Windows' benefit, and
    on POSIX the comparison is simply case-sensitive, which is correct there.
    """
    if not path:
        return ""
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _canonical_blob(project: str, action: str, payload: dict) -> str:
    """The exact bytes an id is taken over.

    Spelled out because the TypeScript extension is a second producer and has
    to agree byte-for-byte: UTF-8, sorted keys, no insignificant whitespace.
    `commit_request.py::request_identity` already established this convention;
    following it beats inventing a second one that differs in a way nobody
    notices until two ids disagree.
    """
    return json.dumps(
        {
            "v": REQUEST_SCHEMA_VERSION,
            "project": canonical_project(project),
            "action": (action or "").strip().lower(),
            "payload": _normalise_payload(action, payload),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_id(project: str, action: str, payload: dict) -> str:
    """A stable 12-hex identity for a request.

    **The project is part of it.** Without that, two projects filing the same
    request collide, and a status lookup cannot say which one it answered for.
    """
    blob = _canonical_blob(project, action, payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _normalise_payload(action: str, payload: "dict | None") -> dict:
    """Payload in canonical form. Pure; does not validate."""
    payload = payload or {}
    if action != "commit":
        return {}
    files = payload.get("files") or []
    return {
        "files": [str(f).replace(chr(92), "/").strip()
                  for f in files if str(f).strip()],
        "scope": str(payload.get("scope") or ""),
        "note": str(payload.get("note") or ""),
    }


# ── Validation ───────────────────────────────────────────────────────────────


def _contained(project_canon: str, candidate: str) -> bool:
    """Is `candidate` inside `project_canon`? Both already canonical."""
    if not candidate or not project_canon:
        return False
    if candidate == project_canon:
        return True
    return candidate.startswith(project_canon.rstrip("\\/") + os.sep)


def validate(request: dict, known_roots: "list | None" = None) -> dict:
    """Return the normalised request, or raise `RequestError`.

    Called at write time *and* at drain time. Both, because the file is
    writable by anything on the machine: validating only on the way in would
    check the one producer that already behaves.
    """
    if not isinstance(request, dict):
        raise RequestError("request is not a JSON object")

    version = request.get("request_schema_version")
    if version != REQUEST_SCHEMA_VERSION:
        # Distinct from "malformed" on purpose: a newer producer against an
        # older Manager is a legible situation with an obvious remedy, and
        # telling the user to update is better than telling them their file is
        # broken.
        raise RequestError(
            f"unsupported request_schema_version {version!r} "
            f"(this Manager speaks {REQUEST_SCHEMA_VERSION})")

    action = str(request.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise RequestError(f"unknown action {action!r}")

    project = str(request.get("project") or "")
    canon = canonical_project(project)
    if not canon:
        raise RequestError("request names no project")
    if not os.path.isdir(canon):
        raise RequestError(f"project is not a directory: {project}")
    if known_roots is not None and not _within_known_roots(canon, known_roots):
        # `--project` is an argument, not an authorization. Without this a
        # request could steer the GUI at any directory on the machine.
        raise RequestError(
            "project is outside the Manager's configured search roots")

    payload = request.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise RequestError("payload is not a JSON object")
    _validate_payload(action, payload, canon)

    created_at = request.get("created_at")
    if not isinstance(created_at, int) or created_at <= 0:
        raise RequestError("created_at must be a positive UTC epoch integer")

    return {
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "project": project,
        "action": action,
        "payload": _normalise_payload(action, payload),
        "created_at": created_at,
    }


def _within_known_roots(project_canon: str, known_roots: list) -> bool:
    return any(_contained(canonical_project(root), project_canon)
               or canonical_project(root) == project_canon
               for root in known_roots if root)


def _validate_payload(action: str, payload: dict, project_canon: str) -> None:
    required, optional = PAYLOAD_SCHEMA[action]
    allowed = set(required) | set(optional)

    unknown = set(payload) - allowed
    if unknown:
        raise RequestError(
            f"{action}: unexpected payload key(s): {', '.join(sorted(unknown))}")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise RequestError(f"{action}: missing payload key(s): "
                           f"{', '.join(missing)}")

    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise RequestError(
            f"payload is {len(encoded)} bytes; the cap is {MAX_PAYLOAD_BYTES}")

    if action != "commit":
        return

    files = payload.get("files")
    if not isinstance(files, list):
        raise RequestError("commit: files must be a list")
    if len(files) > MAX_FILES:
        raise RequestError(f"commit: {len(files)} files; the cap is {MAX_FILES}")

    for raw in files:
        _validate_commit_path(str(raw), project_canon)


def _validate_commit_path(raw: str, project_canon: str) -> None:
    """A commit file must resolve inside the request's own project.

    Otherwise a request could name project A while listing files under B, and
    the Manager would seed a commit dialog that reaches across a project
    boundary. Refused outright rather than filtered: a request that asks for
    something it may not have is wrong, and silently honouring the acceptable
    half of it would hide that.
    """
    path = raw.replace(chr(92), "/").strip()
    if not path:
        raise RequestError("commit: empty file path")
    if os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
        resolved = canonical_project(path)
    else:
        resolved = canonical_project(os.path.join(project_canon, path))
    if not _contained(project_canon, resolved):
        raise RequestError(f"commit: file escapes the project: {raw}")


# ── Writing ──────────────────────────────────────────────────────────────────


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON so a concurrent reader never sees half of it.

    The Manager's drain runs while the extension writes. Without the
    temp-then-replace, it observes a truncated file and logs a malformed
    request that was never malformed.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _filename(created_at: int, ident: str) -> str:
    """`<created_at>-<id>.json`.

    The timestamp leads so the queue is FIFO by filename. Sorting by hash would
    be arbitrary, and sorting by mtime would make the order depend on the
    filesystem rather than on when the user asked.
    """
    return f"{created_at:011d}-{ident}.json"


def write_request(project_root: str, action: str,
                  payload: "dict | None" = None,
                  created_at: "int | None" = None,
                  known_roots: "list | None" = None) -> dict:
    """File a request. Returns `{"id", "path", "duplicate"}`.

    An identical request already pending is a **no-op**, not a second entry:
    the id is a hash of what the request asks for, so re-filing the same thing
    is the same thing.
    """
    request = {
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "project": project_root,
        "action": action,
        "payload": payload or {},
        "created_at": int(created_at if created_at is not None else time.time()),
    }
    normalised = validate(request, known_roots)
    ident = request_id(normalised["project"], normalised["action"],
                       normalised["payload"])
    path = os.path.join(pending_dir(project_root),
                        _filename(normalised["created_at"], ident))

    for existing in _pending_paths(project_root):
        if existing.endswith(f"-{ident}.json"):
            return {"id": ident, "path": existing, "duplicate": True}

    _atomic_write_json(path, {**normalised, "id": ident})
    return {"id": ident, "path": path, "duplicate": False}


# ── Reading ──────────────────────────────────────────────────────────────────


def _pending_paths(project_root: str) -> list:
    directory = pending_dir(project_root)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(".json")]


def load_requests(project_root: str,
                  known_roots: "list | None" = None) -> "tuple[list, list]":
    """Every valid pending request, FIFO, plus what was skipped and why.

    Returns `(requests, skipped)` where `skipped` is a list of
    `{"path", "reason"}`. The second half exists because a malformed request
    that simply disappears reads to the user as "nothing happened" — the drain
    logs these rather than swallowing them.
    """
    requests, skipped = [], []
    for path in _pending_paths(project_root):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            skipped.append({"path": path, "reason": f"unreadable: {exc}"})
            continue
        try:
            request = validate(raw, known_roots)
        except RequestError as exc:
            skipped.append({"path": path, "reason": str(exc)})
            continue
        request["id"] = raw.get("id") or request_id(
            request["project"], request["action"], request["payload"])
        request["path"] = path
        request["attempts"] = int(raw.get("attempts") or 0)
        requests.append(request)
    return requests, skipped


# ── Acknowledgement ledger ───────────────────────────────────────────────────


def acknowledge(project_root: str, request: dict, outcome: str,
                detail: str = "", at: "int | None" = None) -> str:
    """Record an outcome, then remove the pending file. Order matters.

    The pending file is deleted **only after** the acknowledgement exists on
    disk. A crash between the two leaves a request that will be dispatched
    twice, which the id makes recoverable; the reverse order leaves a request
    that vanished with no record, which nothing can explain afterwards.
    """
    ident = request.get("id") or request_id(
        request.get("project", ""), request.get("action", ""),
        request.get("payload", {}))
    created = int(request.get("created_at") or 0)
    record = {
        "id": ident,
        "action": request.get("action", ""),
        "project": request.get("project", ""),
        "outcome": outcome,
        "at": int(at if at is not None else time.time()),
        "detail": detail,
    }
    path = os.path.join(done_dir(project_root), _filename(created, ident))
    _atomic_write_json(path, record)

    if not os.path.isfile(path):
        # Never reached in practice; if it ever is, keeping the pending file is
        # the safe half of the invariant.
        raise OSError(f"acknowledgement not written: {path}")

    pending = request.get("path")
    if pending:
        try:
            os.remove(pending)
        except OSError:
            pass
    return path


def record_attempt(request: dict) -> int:
    """Bump a transient failure's counter in place. Returns the new count.

    A transient failure leaves the request **pending** so the next drain
    retries it. Deleting on the first stumble — a busy dialog, a project not
    yet loaded — would silently lose the user's action, which is exactly the
    outcome the inbox exists to prevent.
    """
    path = request.get("path")
    attempts = int(request.get("attempts") or 0) + 1
    request["attempts"] = attempts
    if not path or not os.path.isfile(path):
        return attempts
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["attempts"] = attempts
        _atomic_write_json(path, raw)
    except (OSError, ValueError):
        pass
    return attempts


def status_of(project_root: str, ident: str,
              project: str = "") -> dict:
    """What became of a request: `{"state", ...}`.

    `state` is one of `unknown` / `pending` / `drained` / `rejected` /
    `quarantined`. Never inferred from a missing file — that is the reasoning
    the acknowledgement ledger exists to replace.

    When `project` is given, the acknowledgement's stored project must match
    it, so a cross-project status query fails loudly rather than answering
    about a different repository that happens to share an id.
    """
    for path in _pending_paths(project_root):
        if path.endswith(f"-{ident}.json"):
            return {"state": PENDING, "id": ident, "path": path}

    directory = done_dir(project_root)
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(f"-{ident}.json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if project and canonical_project(record.get("project", "")) \
                != canonical_project(project):
            return {"state": UNKNOWN, "id": ident,
                    "reason": "that id belongs to a different project"}
        return {"state": record.get("outcome", UNKNOWN), "id": ident,
                "action": record.get("action", ""),
                "at": record.get("at"), "detail": record.get("detail", "")}
    return {"state": UNKNOWN, "id": ident}


def completed_outcome(project_root: str, ident: str) -> str:
    """The recorded outcome for `ident`, or "" if there is none.

    The drain's recovery hook. If a crash lands between the acknowledgement
    write and the pending delete, the request is still queued *and* already
    has a record — so a drain that dispatched blindly would act twice. Checking
    here lets it finish the interrupted delete instead, which is what "the next
    drain resolves it by id" actually means.

    Deliberately separate from `status_of`, which answers the consumer's
    question ("is my request still queued?") where the queue is the truth. This
    answers the drain's question ("did I already do this?"), where the ledger
    is.
    """
    directory = done_dir(project_root)
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        return ""
    for name in names:
        if not name.endswith(f"-{ident}.json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                return str(json.load(fh).get("outcome") or "")
        except (OSError, ValueError, UnicodeDecodeError):
            return ""
    return ""


def prune_acknowledgements(project_root: str, now: "float | None" = None,
                           max_records: int = MAX_DONE_RECORDS,
                           max_age: int = MAX_DONE_AGE_SECONDS) -> int:
    """Trim `done/` to the retention policy. Returns how many were removed.

    **Only `done/`.** An active request must never be deleted or quarantined
    for being old — the pruner and the queue are separate concerns, and
    conflating them would turn a slow week into lost work.
    """
    directory = done_dir(project_root)
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return 0

    now = time.time() if now is None else now
    doomed = set()
    for name in names:
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) > max_age:
                doomed.add(name)
        except OSError:
            continue
    survivors = [n for n in names if n not in doomed]
    if len(survivors) > max_records:
        doomed.update(survivors[:len(survivors) - max_records])

    removed = 0
    for name in doomed:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    return removed


# ── The running Manager ──────────────────────────────────────────────────────


def manager_running() -> bool:
    """Is a Manager instance holding the single-instance mutex?

    **`OpenMutexW`, never `CreateMutexW`.** `runtime.py` uses the latter, which
    *creates* the mutex when it is absent — calling that from the CLI would
    both answer the question wrongly and take the GUI's lock, which `cli.py`'s
    own contract forbids. Opening a handle and closing it immediately observes
    without participating.

    Windows-only; returns False elsewhere rather than raising.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def focus_manager() -> dict:
    """Raise the Manager's window. Returns `{running, focused, reason}`.

    Running and focused are **separate facts** and are never collapsed. Windows
    routinely refuses `SetForegroundWindow` to a background process — the same
    constraint that made driving this app by synthetic mouse input unusable —
    so "the Manager is open but the OS declined to raise it" is a normal
    outcome, not an error.

    A window title is **not** an identity: any process can create a window
    called "TokenSave Manager". Candidates are resolved to their owning process
    and checked before one is raised.
    """
    result = {"running": manager_running(), "focused": False, "reason": ""}
    if os.name != "nt":
        result["reason"] = "focus is Windows-only"
        return result
    if not result["running"]:
        result["reason"] = "no Manager instance is running"
        return result

    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = _find_manager_window(user32)
        if not hwnd:
            result["reason"] = ("a Manager instance holds the lock but no "
                                "window of its own could be identified")
            return result
        user32.ShowWindow(hwnd, 9)                      # SW_RESTORE
        ok = bool(user32.SetForegroundWindow(hwnd))
        result["focused"] = ok
        if not ok:
            result["reason"] = ("Windows declined to bring the window "
                                "forward; it is running and restored")
        return result
    except Exception as exc:                            # noqa: BLE001
        result["reason"] = f"could not focus the Manager: {exc}"
        return result


def _find_manager_window(user32):
    """A window titled like the Manager AND owned by a Manager process.

    The title check alone is what makes an impostor possible, so it only
    narrows the search; `_owns_manager_process` decides.
    """
    import ctypes

    found = []

    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == WINDOW_TITLE and _owns_manager_process(hwnd):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(ENUM(_visit), None)
    return found[0] if found else None


def _owns_manager_process(hwnd) -> bool:
    """Does this window belong to a process that could be the Manager?

    Resolves the window to its PID and looks at the executable's own name. It
    is a Python GUI, so the image is an interpreter or the frozen build rather
    than something uniquely named — which is why this is a *narrowing* check
    layered on the mutex, not a proof of identity on its own. It is enough to
    stop an unrelated application that happens to use the same window title
    from being raised, which is the failure a title-only match allows.
    """
    import ctypes

    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid))
        if not pid.value:
            return False

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return False
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return False
            image = os.path.basename(buffer.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                   # noqa: BLE001
        return False

    return (image.startswith("python") or image.startswith("pythonw")
            or "tokensave" in image)
