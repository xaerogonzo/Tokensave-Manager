"""OllamaModelManagerDialog — browse, pull, and delete Ollama models in-app.

Uses Ollama's native REST API (not the OpenAI-compatible /v1 surface):
  - `GET  /api/version`  — connection check
  - `GET  /api/tags`     — list installed models (name, size)
  - `POST /api/show`     — per-model details (context length)
  - `POST /api/pull`     — download a new model, with streaming progress
  - `DELETE /api/delete` — remove a model

The Pull operation streams newline-delimited JSON. Cancellation works by
holding a reference to the open HTTPResponse and calling `.close()` on
it from the main thread — that unblocks the worker thread's `read()`
immediately. A `threading.Event` alone would not (the worker is
syscall-blocked inside the network stack).

This dialog has ZERO global reads — it talks only to a user-supplied
Ollama server URL. Per the Phase B convention for genuinely-cfg-free
leaf dialogs, no `cfg: ManagerConfig` parameter is added.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox

from constants import C
from helpers.llm import _iter_json_lines


class OllamaModelManagerDialog(tk.Toplevel):
    """Browse, pull, and delete Ollama models without leaving the manager.

    Uses Ollama's native REST API (not the OpenAI-compatible /v1 surface):
      - `GET  /api/version`  — connection check
      - `GET  /api/tags`     — list installed models (name, size)
      - `POST /api/show`     — per-model details (context length)
      - `POST /api/pull`     — download a new model, with streaming progress
      - `DELETE /api/delete` — remove a model

    The Pull operation streams newline-delimited JSON. Cancellation works by
    holding a reference to the open HTTPResponse and calling `.close()` on
    it from the main thread — that unblocks the worker thread's `read()`
    immediately. A `threading.Event` alone would not (the worker is
    syscall-blocked inside the network stack).

    Pure read-only with respect to the project — no project-level state is
    touched. The user's saved `commit_message_llm.model` is only updated if
    they explicitly click "Use for AI features".
    """

    PRESET_MODELS = [
        # Coder-tuned (top of the roadmap recommendations)
        "qwen2.5-coder:14b",
        "qwen2.5-coder:7b",
        "deepseek-coder-v2:16b",
        # General instruction-tuned
        "qwen2.5:14b",
        "qwen2.5:7b",
        "mistral-nemo:12b",
        # Smaller / fast
        "llama3.1:8b",
        "llama3.2",
        "llama3.2:3b",
    ]

    def __init__(self, parent, base_url: str = "http://localhost:11434",
                 on_use_for_ai=None):
        super().__init__(parent)
        self.title("Ollama Model Manager")
        self.configure(bg=C["base"])
        self.resizable(True, True)
        self.minsize(640, 500)
        self.geometry("780x620")
        self.grab_set()

        self._base_url = base_url.rstrip("/") if base_url else "http://localhost:11434"
        self._on_use_for_ai = on_use_for_ai
        self._current_response = None      # type: ignore[assignment]
        self._pull_cancelled = False
        self._pull_active = False

        self._build_header_section()
        self._build_server_url_section()
        self._build_installed_models_section()
        self._build_pull_section()
        self._build_close_section()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Initial population.
        self.after(80, self._check_connection)
        self.after(160, self._refresh_models)

    def _build_header_section(self):
        """Title row."""
        hdr = tk.Frame(self, bg=C["base"])
        hdr.pack(fill=tk.X, padx=18, pady=(14, 4))
        tk.Label(hdr, text="🦙  Ollama Model Manager",
                 font=("Segoe UI", 13, "bold"),
                 bg=C["base"], fg=C["blue"]).pack(side=tk.LEFT)

    def _build_server_url_section(self):
        """Server-URL entry + Check-connection button + status label."""
        url_row = tk.Frame(self, bg=C["base"])
        url_row.pack(fill=tk.X, padx=18, pady=(2, 2))
        tk.Label(url_row, text="Server:", width=8, anchor=tk.W,
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._var_base_url = tk.StringVar(value=self._base_url)
        ttk.Entry(url_row, textvariable=self._var_base_url,
                  width=42).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(url_row, text="Check connection",
                   command=self._check_connection).pack(side=tk.LEFT)

        self._conn_lbl = tk.Label(
            self, text="(click 'Check connection' to verify)",
            font=("Segoe UI", 9, "italic"),
            bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT, anchor=tk.W)
        self._conn_lbl.pack(fill=tk.X, padx=18, pady=(0, 8))

    def _build_installed_models_section(self):
        """Installed-models LabelFrame: Treeview + scrollbar + per-model buttons."""
        list_frame = tk.LabelFrame(
            self, text=" Installed models ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 6))

        tv_wrap = tk.Frame(list_frame, bg=C["mantle"])
        tv_wrap.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._tv = ttk.Treeview(
            tv_wrap, columns=("size", "context"),
            show="tree headings", height=8)
        self._tv.heading("#0",      text="Model")
        self._tv.heading("size",    text="Size")
        self._tv.heading("context", text="Context window")
        self._tv.column("#0",      width=320, anchor=tk.W)
        self._tv.column("size",    width=100, anchor=tk.E)
        self._tv.column("context", width=140, anchor=tk.E)
        tv_vsb = ttk.Scrollbar(tv_wrap, orient="vertical",
                                command=self._tv.yview)
        self._tv.configure(yscrollcommand=tv_vsb.set)
        self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tv_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv.bind("<<TreeviewSelect>>", self._on_select)

        list_btns = tk.Frame(list_frame, bg=C["base"])
        list_btns.pack(fill=tk.X, padx=4, pady=(4, 4))
        ttk.Button(list_btns, text="↻ Refresh",
                   command=self._refresh_models).pack(side=tk.LEFT)
        self._use_btn = ttk.Button(
            list_btns, text="Use for AI features",
            command=self._use_for_ai, state=tk.DISABLED)
        self._use_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._del_btn = ttk.Button(
            list_btns, text="🗑 Delete",
            command=self._delete_selected, state=tk.DISABLED)
        self._del_btn.pack(side=tk.LEFT, padx=(8, 0))

    def _build_pull_section(self):
        """Pull LabelFrame: preset combobox + Pull button + progress bar + status."""
        pull_frame = tk.LabelFrame(
            self, text=" Pull a new model ",
            bg=C["base"], fg=C["subtext"],
            font=("Segoe UI", 9), bd=1, relief=tk.FLAT)
        pull_frame.pack(fill=tk.X, padx=18, pady=(0, 6))

        pull_row = tk.Frame(pull_frame, bg=C["base"])
        pull_row.pack(fill=tk.X, padx=4, pady=(6, 4))
        tk.Label(pull_row, text="Model:", width=8, anchor=tk.W,
                 bg=C["base"], fg=C["subtext"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self._var_pull = tk.StringVar(value=self.PRESET_MODELS[0])
        self._pull_combo = ttk.Combobox(
            pull_row, textvariable=self._var_pull,
            values=self.PRESET_MODELS, width=28)
        self._pull_combo.pack(side=tk.LEFT, padx=(0, 6))
        self._pull_btn = ttk.Button(
            pull_row, text="Pull", command=self._start_pull)
        self._pull_btn.pack(side=tk.LEFT)

        self._progress = ttk.Progressbar(
            pull_frame, orient="horizontal", mode="determinate",
            maximum=100, value=0)
        self._progress.pack(fill=tk.X, padx=6, pady=(4, 2))
        self._pull_status = tk.Label(
            pull_frame, text="(idle)", font=("Segoe UI", 8),
            bg=C["base"], fg=C["overlay0"],
            anchor=tk.W, justify=tk.LEFT)
        self._pull_status.pack(fill=tk.X, padx=8, pady=(0, 6))

    def _build_close_section(self):
        """Bottom Close button row."""
        btn_row = tk.Frame(self, bg=C["base"])
        btn_row.pack(fill=tk.X, padx=18, pady=(0, 14))
        ttk.Button(btn_row, text="Close",
                   command=self._on_close).pack(side=tk.RIGHT)

    # ── Networking helpers ──────────────────────────────────────────────

    def _server(self) -> str:
        v = self._var_base_url.get().strip().rstrip("/")
        return v or "http://localhost:11434"

    def _check_connection(self):
        url = self._server() + "/api/version"
        self._conn_lbl.configure(
            text="⟳  Checking connection…", fg=C["peach"])

        def _worker():
            import urllib.request, urllib.error, json as _json
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                ver = data.get("version", "?")
                self.after(0, self._conn_lbl.configure,
                    {"text": f"✓  Connected — Ollama {ver}", "fg": C["green"]})
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, _json.JSONDecodeError, OSError) as e:
                self.after(0, self._conn_lbl.configure,
                    {"text": f"✗  Not reachable at {self._server()} — "
                             f"is the Ollama service running? ({type(e).__name__})",
                     "fg": C["red"]})

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-version").start()

    def _refresh_models(self):
        url = self._server() + "/api/tags"

        def _worker():
            import urllib.request, urllib.error, json as _json
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, _json.JSONDecodeError, OSError):
                self.after(0, self._populate_models, [])
                return
            models = data.get("models") or []
            # Enrich each with context length via /api/show. This is N HTTP
            # calls — fine for typical (< 20) installed model counts, and
            # we cap at 25 to avoid pathological cases.
            enriched = []
            for m in models[:25]:
                name = m.get("name") or m.get("model") or ""
                size = int(m.get("size") or 0)
                ctx = self._fetch_context_length(name)
                enriched.append({"name": name, "size": size, "context": ctx})
            self.after(0, self._populate_models, enriched)

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-tags").start()

    def _fetch_context_length(self, name: str) -> int | None:
        import urllib.request, urllib.error, json as _json
        url = self._server() + "/api/show"
        payload = _json.dumps({"name": name}).encode("utf-8")
        req = urllib.request.Request(
            url, method="POST", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, _json.JSONDecodeError, OSError):
            return None
        # Ollama 0.3+ exposes model_info.<arch>.context_length; older
        # versions use parameters.num_ctx. Try both, fall back to None.
        mi = data.get("model_info") or {}
        for k, v in mi.items():
            if k.endswith(".context_length") and isinstance(v, int):
                return v
        params = data.get("parameters") or ""
        if isinstance(params, str):
            for line in params.splitlines():
                if line.lower().startswith("num_ctx"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1].isdigit():
                        return int(parts[-1])
        return None

    def _populate_models(self, rows: list[dict]):
        self._tv.delete(*self._tv.get_children())
        if not rows:
            self._tv.insert("", tk.END, text="(no models found — pull one below)",
                            values=("", ""))
            return
        for r in rows:
            size_h = self._human_bytes(r["size"])
            ctx_h = "—" if r["context"] is None else f"{r['context']:,}"
            self._tv.insert("", tk.END, text=r["name"], values=(size_h, ctx_h))

    @staticmethod
    def _human_bytes(n: int) -> str:
        if n <= 0:
            return "—"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024.0:
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Selection-driven actions ────────────────────────────────────────

    def _on_select(self, _evt=None):
        sel = self._tv.selection()
        state = tk.NORMAL if sel and self._tv.item(sel[0], "text") else tk.DISABLED
        self._use_btn.configure(state=state)
        self._del_btn.configure(state=state)

    def _selected_model(self) -> str:
        sel = self._tv.selection()
        if not sel:
            return ""
        name = self._tv.item(sel[0], "text") or ""
        return "" if name.startswith("(no models") else name

    def _use_for_ai(self):
        name = self._selected_model()
        if not name:
            return
        if self._on_use_for_ai:
            self._on_use_for_ai(name, self._server())
            messagebox.showinfo(
                "Set as AI model",
                f"'{name}' will be used for AI features.\n\n"
                "Provider set to 'ollama'.",
                parent=self)
        else:
            # No callback wired — copy to clipboard as a fallback.
            self.clipboard_clear()
            self.clipboard_append(name)
            messagebox.showinfo(
                "Copied", f"'{name}' copied to clipboard.\n"
                "Paste it into Settings → AI commit messages → Model.",
                parent=self)

    def _delete_selected(self):
        name = self._selected_model()
        if not name:
            return
        if not messagebox.askyesno(
                "Delete model",
                f"Delete '{name}' from Ollama?\n\n"
                "This frees disk space. You can re-pull it later.",
                parent=self):
            return

        def _worker(model=name):
            import urllib.request, urllib.error, json as _json
            url = self._server() + "/api/delete"
            payload = _json.dumps({"name": model}).encode("utf-8")
            req = urllib.request.Request(
                url, method="DELETE", data=payload,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                ok = True
                err = ""
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError) as e:
                ok = False
                err = f"{type(e).__name__}: {e}"
            self.after(0, self._after_delete, model, ok, err)

        threading.Thread(target=_worker, daemon=True,
                         name="ollama-delete").start()

    def _after_delete(self, name: str, ok: bool, err: str):
        if ok:
            self._refresh_models()
        else:
            messagebox.showerror(
                "Delete failed",
                f"Could not delete '{name}'.\n\n{err}",
                parent=self)

    # ── Pull (streaming) ────────────────────────────────────────────────

    def _start_pull(self):
        if self._pull_active:
            # Button is in Cancel mode — second click cancels.
            self._cancel_pull()
            return
        name = self._var_pull.get().strip()
        if not name:
            return
        self._pull_active = True
        self._pull_cancelled = False
        self._progress.configure(value=0, maximum=100)
        self._pull_status.configure(
            text=f"⟳  Pulling {name}…", fg=C["peach"])
        self._pull_btn.configure(text="Cancel")
        self._pull_combo.configure(state=tk.DISABLED)

        def _worker(model=name):
            import urllib.request, urllib.error, json as _json
            url = self._server() + "/api/pull"
            payload = _json.dumps({"name": model, "stream": True}).encode("utf-8")
            req = urllib.request.Request(
                url, method="POST", data=payload,
                headers={"Content-Type": "application/json"})
            try:
                # NOTE: no `with` block — we need the response object to
                # remain accessible from the main thread so a Cancel click
                # can call .close() on it to break this worker out of read().
                response = urllib.request.urlopen(req, timeout=30)
                self._current_response = response
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError) as e:
                self.after(0, self._after_pull, model, False,
                           f"{type(e).__name__}: {e}")
                return
            try:
                for event in _iter_json_lines(response):
                    if self._pull_cancelled:
                        break
                    status = event.get("status") or ""
                    total = event.get("total")
                    completed = event.get("completed")
                    if isinstance(total, int) and total > 0 and isinstance(completed, int):
                        pct = max(0.0, min(100.0, 100.0 * completed / total))
                        msg = (f"{status} — "
                               f"{self._human_bytes(completed)} / "
                               f"{self._human_bytes(total)}  ({pct:.0f}%)")
                        self.after(0, self._update_pull_progress, pct, msg)
                    elif status:
                        self.after(0, self._update_pull_status, status)
                    if status == "success":
                        self.after(0, self._after_pull, model, True, "")
                        return
            finally:
                try:
                    response.close()
                except OSError:
                    pass
                self._current_response = None
            # Stream ended without an explicit "success" — could be cancel,
            # network drop, or an error event. Cancelled path takes priority.
            if self._pull_cancelled:
                self.after(0, self._after_pull, model, False, "cancelled")
            else:
                self.after(0, self._after_pull, model, False,
                           "Stream ended without success")

        self._pull_thread = threading.Thread(
            target=_worker, daemon=True, name="ollama-pull")
        self._pull_thread.start()

    def _cancel_pull(self):
        self._pull_cancelled = True
        self._pull_status.configure(text="⏹  Cancelling…", fg=C["overlay0"])
        # Sever the socket immediately so the worker thread's read() unblocks.
        resp = self._current_response
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass

    def _update_pull_progress(self, pct: float, msg: str):
        self._progress.configure(value=pct)
        self._pull_status.configure(text=msg, fg=C["peach"])

    def _update_pull_status(self, status: str):
        self._pull_status.configure(text=status, fg=C["overlay0"])

    def _after_pull(self, name: str, ok: bool, err: str):
        self._pull_active = False
        self._pull_btn.configure(text="Pull")
        self._pull_combo.configure(state=tk.NORMAL)
        if ok:
            self._progress.configure(value=100)
            self._pull_status.configure(
                text=f"✓  Pulled {name} successfully.", fg=C["green"])
            self._refresh_models()
        else:
            self._progress.configure(value=0)
            if err == "cancelled":
                self._pull_status.configure(
                    text=f"⏹  Cancelled. Partial download for {name} is kept "
                         f"by Ollama — re-run Pull to resume (layers are "
                         f"deduplicated).",
                    fg=C["overlay0"])
            else:
                self._pull_status.configure(
                    text=f"✗  Pull failed: {err}", fg=C["red"])

    # ── Close ───────────────────────────────────────────────────────────

    def _on_close(self):
        if self._pull_active:
            # Cancel any in-flight pull so we don't leave the thread blocked.
            self._cancel_pull()
        try:
            self.destroy()
        except tk.TclError:
            pass
