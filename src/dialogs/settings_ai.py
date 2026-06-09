"""AISection — the AI-related blocks of the Settings dialog.

Extracted verbatim from dialogs/settings.py (Roadmap-8 god-file split).
Builds, in original visual order: AI backend selection (Draft PR /
commit message / grounding toggles), Ollama (model manager shortcut,
num_ctx, warm-up), AI commit messages (provider grid, presets,
options), and Ask Tab AI.

House pattern: the dialog handle is used for Tk plumbing only
(``after()``, ``winfo_exists()``, child-dialog parenting).
``save_into(raw)`` is this section's slice of the Save contract.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from constants import C
from theme import themed_checkbutton

if TYPE_CHECKING:
    from state import ManagerConfig


def _probe_loaded_model(base_url: str) -> str:
    """Query the /v1/models endpoint and return the first non-embedding model id.

    Used by the AI-preset buttons to auto-fill the Model field when the local
    server is reachable. Returns "" on any network or parse failure.
    """
    import urllib.request, urllib.error, json as _json
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, _json.JSONDecodeError):
        return ""
    for m in (data.get("data") or []):
        mid = m.get("id", "")
        lid = mid.lower()
        if mid and "embed" not in lid and "rerank" not in lid and "whisper" not in lid:
            return mid
    return ""


class AISection:
    """Backend selection + Ollama + AI commit messages + Ask Tab AI."""

    def __init__(self, dialog: tk.Toplevel, body: tk.Frame,
                 cfg: "ManagerConfig") -> None:
        self._dlg = dialog
        self._cfg = cfg
        raw = cfg.raw
        self._build_backend_selection_section(body, raw)
        self._build_ollama_section(body, raw)
        self._build_ai_section(body, raw)
        self._build_ask_ai_section(body, raw)

    def save_into(self, raw: dict) -> bool:
        """Write this section's fields into raw. Always succeeds."""
        raw["draft_pr_backend"]        = self._var_draft_pr_backend.get()
        raw["commit_message_backend"]  = self._var_commit_msg_backend.get()
        raw["enable_llm_grounding"]    = bool(self._var_enable_llm_grounding.get())
        raw["enable_commit_grounding"] = bool(self._var_enable_commit_grounding.get())
        raw["enable_pr_grounding"]     = bool(self._var_enable_pr_grounding.get())
        # Persist AI commit-message settings (preserves any unknown keys
        # the user may have added manually via JSON edit).
        existing_llm = raw.get("commit_message_llm") or {}
        try:
            min_diff_lines = int(self._var_llm_min_diff.get())
        except ValueError:
            min_diff_lines = 10
        existing_llm.update({
            "enabled":     self._var_llm_enabled.get(),
            "provider":    self._var_llm_provider.get().strip() or "anthropic",
            "model":       self._var_llm_model.get().strip(),
            "api_key_env": self._var_llm_keyenv.get().strip(),
            "base_url":    self._var_llm_base_url.get().strip(),
            "min_diff_lines": max(0, min_diff_lines),
            "use_for_sync_autocommit": self._var_llm_for_sync.get(),
        })
        # Fill in defaults that other helpers expect
        existing_llm.setdefault("max_diff_chars", 24000)
        existing_llm.setdefault("timeout_seconds", 90)
        raw["commit_message_llm"] = existing_llm
        # Ask Tab AI config — written independently from commit_message_llm.
        raw["ask_tab_llm"] = {
            "enabled":     self._var_ask_enabled.get(),
            "provider":    self._var_ask_provider.get(),
            "model":       self._var_ask_model.get().strip(),
            "api_key_env": self._var_ask_keyenv.get().strip(),
            "base_url":    self._var_ask_base_url.get().strip(),
        }
        raw["ollama_num_ctx"]   = self._var_ollama_num_ctx.get()
        raw["ollama_warmup"]    = self._var_ollama_warmup.get()
        return True

    # ── Section builders (original visual order) ─────────────────────────

    def _build_backend_selection_section(self, body, raw):
        """AI backend selection LabelFrame: Draft PR, commit message, grounding."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        lf = tk.LabelFrame(body, text="AI backend selection",
                           bg=C["base"], fg=C["subtext"],
                           font=("Segoe UI", 9, "bold"),
                           relief=tk.GROOVE, bd=1)
        lf.pack(fill=tk.X, padx=20, pady=(0, 8))

        # Draft PR
        tk.Label(lf, text="Draft PR",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=12, pady=(6, 2))
        self._var_draft_pr_backend = tk.StringVar(
            value=raw.get("draft_pr_backend") or "auto")
        pr_row = tk.Frame(lf, bg=C["base"])
        pr_row.pack(anchor=tk.W, padx=12, pady=(0, 2))
        for val, label in [("auto", "Auto"), ("claude_cli", "Claude Code CLI"), ("llm", "API key")]:
            ttk.Radiobutton(pr_row, text=label, value=val,
                            variable=self._var_draft_pr_backend).pack(side=tk.LEFT, padx=(0, 14))
        tk.Label(lf,
            text="  Auto: prefers Claude Code CLI if configured, falls back to API key.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=24, pady=(0, 6))

        ttk.Separator(lf, orient="horizontal").pack(fill=tk.X, padx=8, pady=(0, 6))

        # Commit message
        tk.Label(lf, text="Commit message (Suggest button)",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=12, pady=(0, 2))
        self._var_commit_msg_backend = tk.StringVar(
            value=raw.get("commit_message_backend") or "auto")
        cm_row = tk.Frame(lf, bg=C["base"])
        cm_row.pack(anchor=tk.W, padx=12, pady=(0, 2))
        for val, label in [
            ("auto",       "Claude CLI → LLM"),
            ("llm_first",  "LLM → Claude CLI"),
            ("claude_cli", "Claude CLI only"),
            ("llm",        "LLM only"),
        ]:
            ttk.Radiobutton(cm_row, text=label, value=val,
                            variable=self._var_commit_msg_backend).pack(
                            side=tk.LEFT, padx=(0, 14))
        tk.Label(lf,
            text="  Claude CLI uses your subscription (no API credits). LLM uses the provider in the AI section below.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=24, pady=(0, 8))

        ttk.Separator(lf, orient="horizontal").pack(fill=tk.X, padx=8, pady=(0, 6))

        # Code-graph grounding (v4.2) — master toggle for tokensave/codegraph
        # context injection across commit-message, PR, code-review, doc-drafter.
        tk.Label(lf, text="Code-graph grounding",
                 font=("Segoe UI", 9, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=12, pady=(0, 2))
        # Default ON — matches ManagerConfig.enable_llm_grounding default.
        _grounding_initial = raw.get("enable_llm_grounding")
        if _grounding_initial is None:
            _grounding_initial = True
        self._var_enable_llm_grounding = tk.BooleanVar(value=bool(_grounding_initial))
        grounding_chk = ttk.Checkbutton(
            lf, text="Enable tokensave + codegraph grounding for LLM features",
            variable=self._var_enable_llm_grounding,
        )
        grounding_chk.pack(anchor=tk.W, padx=12, pady=(0, 2))
        try:
            from theme import _Tooltip
            _Tooltip(
                grounding_chk,
                "When on, the manager injects tokensave + codegraph context "
                "into commit-message drafts, PR drafts, AI code review, the "
                "Ask tab's Claude CLI path, and the doc drafter. Silently "
                "skipped when neither tool is indexed for the current "
                "project. Turn off if grounding produces noisy output.",
            )
        except Exception:
            pass
        tk.Label(lf,
            text="  Adds structural facts (callers, callees, affected tests) to the prompt.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=24, pady=(0, 8))

        # v4.6: per-feature opt-IN for commit messages. Default OFF because
        # live testing showed grounding hurts commit-message quality on big
        # multi-file diffs (small models copy recent commit subjects verbatim).
        _commit_grounding_initial = raw.get("enable_commit_grounding")
        if _commit_grounding_initial is None:
            _commit_grounding_initial = False
        self._var_enable_commit_grounding = tk.BooleanVar(
            value=bool(_commit_grounding_initial))
        commit_grounding_chk = ttk.Checkbutton(
            lf,
            text="    └─ Also use grounding for commit messages (opt-in)",
            variable=self._var_enable_commit_grounding,
        )
        commit_grounding_chk.pack(anchor=tk.W, padx=12, pady=(0, 2))
        try:
            from theme import _Tooltip
            _Tooltip(
                commit_grounding_chk,
                "Off by default. Commit messages are summaries of the staged "
                "diff — adding repository context tends to confuse small "
                "models (qwen2.5-coder copies recent commit subjects "
                "verbatim) and pushes the prompt past Claude CLI's output "
                "budget. Turn ON to experiment; revert if the Suggest button "
                "produces poor results.",
            )
        except Exception:
            pass
        tk.Label(lf,
            text="    Recommended OFF — small models copy recent subjects when overwhelmed.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=24, pady=(0, 8))

        # v4.6: per-feature opt-in for Draft PR grounding. Defaults to ON
        # because PRs benefit a lot from test-impact + symbol-reference
        # context, and the backends that draft them (Claude CLI, cloud
        # APIs) handle the extra prompt weight well.
        _pr_grounding_initial = raw.get("enable_pr_grounding")
        if _pr_grounding_initial is None:
            _pr_grounding_initial = True
        self._var_enable_pr_grounding = tk.BooleanVar(
            value=bool(_pr_grounding_initial))
        pr_grounding_chk = ttk.Checkbutton(
            lf,
            text="    └─ Also use grounding for Draft PR (recommended)",
            variable=self._var_enable_pr_grounding,
        )
        pr_grounding_chk.pack(anchor=tk.W, padx=12, pady=(0, 2))
        try:
            from theme import _Tooltip
            _Tooltip(
                pr_grounding_chk,
                "ON by default. PR descriptions benefit strongly from "
                "test-impact mapping (codegraph affected --stdin) and "
                "symbol-reference context. Claude CLI and cloud APIs "
                "handle the extra prompt weight comfortably. Manager "
                "pre-builds the grounding for the CLI path AND nudges "
                "the CLI to use its own MCP tools if codegraph/"
                "tokensave are wired into Claude Code's MCP config.",
            )
        except Exception:
            pass
        tk.Label(lf,
            text="    Adds test-impact + symbol-reference context to the PR draft.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=24, pady=(0, 8))

    def _build_ollama_section(self, body, raw):
        """Ollama model manager shortcut, num_ctx spinbox, and warm-up toggle."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="Ollama", font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        ollama_row = tk.Frame(body, bg=C["base"])
        ollama_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        ttk.Button(ollama_row, text="🦙  Manage Ollama Models…",
                   command=self._open_ollama_manager).pack(side=tk.LEFT)
        tk.Label(body,
            text="  Browse installed models, pull new ones, see context windows.\n"
                 "  Uses Ollama's native REST API at the base URL configured below.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

        # num_ctx spinbox
        num_ctx_row = tk.Frame(body, bg=C["base"])
        num_ctx_row.pack(anchor=tk.W, padx=20, pady=(0, 4))
        tk.Label(num_ctx_row, text="Context window (num_ctx):",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["subtext"]).pack(side=tk.LEFT)
        self._var_ollama_num_ctx = tk.IntVar(
            value=int(raw.get("ollama_num_ctx", 4096)))
        ttk.Spinbox(num_ctx_row, from_=512, to=131072, increment=512,
                    textvariable=self._var_ollama_num_ctx,
                    width=8).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(num_ctx_row, text="  tokens  (0 = model default)",
                 font=("Segoe UI", 8), bg=C["base"],
                 fg=C["overlay0"]).pack(side=tk.LEFT)

        # warm-up checkbox
        self._var_ollama_warmup = tk.BooleanVar(
            value=bool(raw.get("ollama_warmup", False)))
        themed_checkbutton(body,
            text="Warm up Ollama before first Generate (loads model into VRAM early)",
            variable=self._var_ollama_warmup,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(0, 6))

    def _build_ai_section(self, body, raw):
        """AI commit messages — provider, model, key env, presets, options."""
        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="AI commit messages",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))

        llm_cfg = raw.get("commit_message_llm") or {}
        self._var_llm_enabled = tk.BooleanVar(value=bool(llm_cfg.get("enabled", False)))
        themed_checkbutton(body,
            text="Use AI to generate commit message suggestions",
            variable=self._var_llm_enabled,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 4))

        self._build_ai_provider_grid(body, llm_cfg)
        self._build_ai_presets(body)
        self._build_ai_options(body, llm_cfg)

    def _build_ask_ai_section(self, body, raw):
        """Ask Tab AI — provider, model, key env, base URL (separate from commit-msg LLM).

        Falls back to commit_message_llm values on first run so existing users
        see no change; once the user saves, ask_tab_llm is written independently.
        """
        ask_cfg = raw.get("ask_tab_llm") or raw.get("commit_message_llm") or {}

        ttk.Separator(body, orient="horizontal").pack(fill=tk.X, padx=20, pady=(8, 8))
        tk.Label(body, text="Ask Tab AI",
                 font=("Segoe UI", 10, "bold"),
                 bg=C["base"], fg=C["text"]).pack(anchor=tk.W, padx=20, pady=(0, 2))
        tk.Label(body,
                 text="  Configure the AI backend for the 🤖 Ask tab independently from\n"
                      "  commit-message AI. Supports all providers plus Claude CLI.",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT).pack(anchor=tk.W, padx=20, pady=(0, 4))

        self._var_ask_enabled = tk.BooleanVar(value=bool(ask_cfg.get("enabled", False)))
        themed_checkbutton(body,
            text="Enable AI in the Ask tab",
            variable=self._var_ask_enabled,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 10)).pack(anchor=tk.W, padx=20, pady=(0, 4))

        ask_grid = tk.Frame(body, bg=C["base"])
        ask_grid.pack(fill=tk.X, padx=36, pady=(0, 6))

        def _row(label_txt, widget):
            row = tk.Frame(ask_grid, bg=C["base"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label_txt, width=18, anchor=tk.W,
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["subtext"]).pack(side=tk.LEFT)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._var_ask_provider = tk.StringVar(
            value=ask_cfg.get("provider", "ollama"))
        provider_box = ttk.Combobox(ask_grid, textvariable=self._var_ask_provider,
            values=["claude_cli", "ollama", "anthropic", "openai", "openai_compatible"],
            state="readonly", width=22)
        _row("Provider:", provider_box)

        self._var_ask_model = tk.StringVar(value=ask_cfg.get("model", ""))
        _row("Model:", ttk.Entry(ask_grid, textvariable=self._var_ask_model))

        self._var_ask_keyenv = tk.StringVar(
            value=ask_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        self._ask_keyenv_entry = ttk.Entry(ask_grid, textvariable=self._var_ask_keyenv)
        _row("API key env var:", self._ask_keyenv_entry)

        self._var_ask_base_url = tk.StringVar(value=ask_cfg.get("base_url", ""))
        self._ask_base_url_entry = ttk.Entry(ask_grid, textvariable=self._var_ask_base_url)
        _row("Base URL:", self._ask_base_url_entry)

        # Reactive: disable key/URL fields when claude_cli is selected (it uses
        # system-level auth; key env / base URL are irrelevant).
        def _on_ask_provider_changed(*_):
            is_cli = self._var_ask_provider.get() == "claude_cli"
            state = tk.DISABLED if is_cli else tk.NORMAL
            self._ask_keyenv_entry.configure(state=state)
            self._ask_base_url_entry.configure(state=state)
        self._var_ask_provider.trace_add("write", _on_ask_provider_changed)
        _on_ask_provider_changed()  # apply on initial render

        tk.Label(body,
            text="  Set provider to 'claude_cli' to use the Claude CLI binary configured\n"
                 "  in the Paths section above. No API key needed — uses your local auth.\n"
                 "  Other providers use the same format as AI commit messages.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    # ── AI-section sub-builders ──────────────────────────────────────────

    def _build_ai_provider_grid(self, body, llm_cfg):
        """Provider / model / key / base-URL 4-row grid."""
        llm_grid = tk.Frame(body, bg=C["base"])
        llm_grid.pack(fill=tk.X, padx=36, pady=(0, 6))

        def _row(label_txt, widget):
            row = tk.Frame(llm_grid, bg=C["base"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=label_txt, width=18, anchor=tk.W,
                     font=("Segoe UI", 9), bg=C["base"],
                     fg=C["subtext"]).pack(side=tk.LEFT)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._var_llm_provider = tk.StringVar(value=llm_cfg.get("provider", "anthropic"))
        provider_box = ttk.Combobox(llm_grid, textvariable=self._var_llm_provider,
            values=["ollama", "anthropic", "openai", "openai_compatible"],
            state="readonly", width=22)
        _row("Provider:", provider_box)

        self._var_llm_model = tk.StringVar(value=llm_cfg.get("model", "claude-haiku-4-5"))
        _row("Model:", ttk.Entry(llm_grid, textvariable=self._var_llm_model))

        self._var_llm_keyenv = tk.StringVar(value=llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        _row("API key env var:", ttk.Entry(llm_grid, textvariable=self._var_llm_keyenv))

        self._var_llm_base_url = tk.StringVar(value=llm_cfg.get("base_url", ""))
        _row("Base URL:", ttk.Entry(llm_grid, textvariable=self._var_llm_base_url))

    def _build_ai_presets(self, body):
        """Anthropic / LM Studio / Ollama quick-preset buttons + feedback label."""
        preset_row = tk.Frame(body, bg=C["base"])
        preset_row.pack(anchor=tk.W, padx=36, pady=(0, 4))
        tk.Label(preset_row, text="Quick presets:", font=("Segoe UI", 9),
                 bg=C["base"], fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 8))

        # Hint label must exist BEFORE the preset callbacks reference it.
        self._llm_preset_hint = tk.Label(body, text="", font=("Segoe UI", 8),
                                         bg=C["base"], fg=C["overlay0"],
                                         justify=tk.LEFT, wraplength=620, anchor=tk.W)
        self._llm_preset_hint.pack(anchor=tk.W, padx=36, pady=(2, 0), fill=tk.X)

        def _apply_lm_studio():
            self._var_llm_provider.set("openai_compatible")
            base = "http://localhost:1234"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            self._llm_preset_hint.configure(text="⏳  Checking LM Studio server…", fg=C["overlay0"])

            def _probe():
                detected = _probe_loaded_model(base)
                def _apply():
                    if not self._dlg.winfo_exists():
                        return
                    if detected:
                        self._var_llm_model.set(detected)
                        self._llm_preset_hint.configure(text=f"✓  Using loaded model: {detected}", fg=C["green"])
                    else:
                        self._llm_preset_hint.configure(
                            text="⚠  LM Studio server not reachable at http://localhost:1234 — "
                                 "start the Local Server in LM Studio's '</>' panel and load a model, "
                                 "then click this preset again.", fg=C["peach"])
                self._dlg.after(0, _apply)
            threading.Thread(target=_probe, daemon=True).start()

        def _apply_ollama():
            self._var_llm_provider.set("ollama")
            base = "http://localhost:11434"
            self._var_llm_base_url.set(base)
            self._var_llm_keyenv.set("")
            self._llm_preset_hint.configure(text="⏳  Checking Ollama server…", fg=C["overlay0"])

            def _probe():
                detected = _probe_loaded_model(base)
                def _apply():
                    if not self._dlg.winfo_exists():
                        return
                    if detected:
                        self._var_llm_model.set(detected)
                        self._llm_preset_hint.configure(text=f"✓  Using Ollama model: {detected}", fg=C["green"])
                    else:
                        if not self._var_llm_model.get() or "claude" in self._var_llm_model.get():
                            self._var_llm_model.set("qwen2.5-coder:14b")
                        self._llm_preset_hint.configure(
                            text="⚠  Ollama not reachable at http://localhost:11434 — "
                                 "make sure the Ollama service is running and run "
                                 "`ollama pull qwen2.5-coder:14b` (or any chat model), "
                                 "then click this preset again.", fg=C["peach"])
                self._dlg.after(0, _apply)
            threading.Thread(target=_probe, daemon=True).start()

        def _apply_anthropic():
            self._var_llm_provider.set("anthropic")
            self._var_llm_base_url.set("")
            self._var_llm_keyenv.set("ANTHROPIC_API_KEY")
            if not self._var_llm_model.get() or "/" in self._var_llm_model.get():
                self._var_llm_model.set("claude-haiku-4-5")
            self._llm_preset_hint.configure(
                text="ℹ  Set the ANTHROPIC_API_KEY environment variable (get a "
                     "key at console.anthropic.com).  Haiku is cheapest "
                     "(~$0.0005/commit); Sonnet/Opus are higher-fidelity.", fg=C["blue"])

        ttk.Button(preset_row, text="Anthropic", command=_apply_anthropic).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="LM Studio", command=_apply_lm_studio).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(preset_row, text="Ollama",    command=_apply_ollama).pack(side=tk.LEFT)

        # Persistent gotcha note — both Ollama and LM Studio load models into
        # RAM/VRAM independently. Running them at the same time can starve the
        # second loader (the error message blames "system memory" without
        # hinting that another inference daemon is holding the difference).
        tk.Label(body,
                 text=("ⓘ  Running Ollama and LM Studio at the same time can "
                       "break model loading — both reserve RAM/VRAM "
                       "independently. If a load fails with \"more system "
                       "memory\" while the other tool is running, unload one "
                       "first (LM Studio → Eject;  `ollama stop <model>`)."),
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 justify=tk.LEFT, wraplength=620, anchor=tk.W
                 ).pack(anchor=tk.W, padx=36, pady=(4, 0), fill=tk.X)

    def _build_ai_options(self, body, llm_cfg):
        """Min-diff-lines spinner, sync auto-commit toggle, disclaimer."""
        min_row = tk.Frame(body, bg=C["base"])
        min_row.pack(anchor=tk.W, padx=36, pady=(2, 0))
        self._var_llm_min_diff = tk.StringVar(value=str(llm_cfg.get("min_diff_lines", 10)))
        tk.Label(min_row, text="Min diff lines (smaller commits skip AI):",
                 font=("Segoe UI", 9), bg=C["base"],
                 fg=C["subtext"]).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(min_row, textvariable=self._var_llm_min_diff, width=6).pack(side=tk.LEFT)

        self._var_llm_for_sync = tk.BooleanVar(value=bool(llm_cfg.get("use_for_sync_autocommit", False)))
        themed_checkbutton(body,
            text="Also use AI for sync auto-commit messages (disables amend-stacking)",
            variable=self._var_llm_for_sync,
            bg=C["base"], fg=C["text"],
            activebackground=C["base"], activeforeground=C["text"],
            font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20, pady=(6, 2))
        tk.Label(body,
            text="  AI runs only when toggled ON. Silent fallback on any error\n"
                 "  (missing key, network failure, timeout). Anthropic Claude Haiku\n"
                 "  costs ~$0.0005 per commit.",
            font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
            justify=tk.LEFT).pack(anchor=tk.W, padx=36, pady=(0, 8))

    # ── Cross-dialog launcher (lazy-imported per Rule 6) ─────────────────

    def _open_ollama_manager(self):
        """Launch the Ollama Model Manager dialog.

        Uses whatever base URL is currently typed in the AI commit messages
        section (so editing the URL takes effect without saving Settings
        first). Falls back to http://localhost:11434 if blank. When the
        user clicks "Use for AI features" on a model in the dialog, the
        callback updates the provider/model/base-url fields in this very
        Settings dialog — they still have to click Save to persist.

        Lazy import (Rule 6) for the same reason as _open_mcp_configurator.
        """
        from dialogs.ollama_model_mgr import OllamaModelManagerDialog
        base_url = self._var_llm_base_url.get().strip() \
                   or "http://localhost:11434"

        def _on_use(model_name: str, server_url: str):
            self._var_llm_provider.set("ollama")
            self._var_llm_model.set(model_name)
            self._var_llm_base_url.set(server_url)
            self._var_llm_keyenv.set("")
            # Auto-enable AI features when the user explicitly picks a model.
            self._var_llm_enabled.set(True)
            if hasattr(self, "_llm_preset_hint"):
                self._llm_preset_hint.configure(
                    text=f"✓  Using Ollama model: {model_name}.  "
                         f"Click Save to persist.",
                    fg=C["green"])

        OllamaModelManagerDialog(
            self._dlg, base_url=base_url, on_use_for_ai=_on_use)
