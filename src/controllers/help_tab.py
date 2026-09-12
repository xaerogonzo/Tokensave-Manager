"""HelpTabController — the Help tab, rendered from the repository's markdown.

WHAT CHANGED, AND WHY IT MATTERS MORE THAN THE RENDERER. This used to be 72 KB
of hand-written Python across three `help_topics_*` modules, and editing help
meant editing Python. Measured before the change: those 25 topics never
mentioned **10 of 19** current features, while `README.md` already covered 12 of
14 of the same list. The documentation was better than the help; it simply was
not the help.

Now it is. `helpers/help_docs.py` finds topics by `<!-- help:key -->` anchors in
the shipped documents, `helpers/markdown_tk.py` turns a section into tagged
spans, and this file is a list, a search box and a Text widget. A documentation
sweep updates the application.

THREE THINGS THIS FILE OWNS, and nothing else does:

  * the tag palette. `markdown_tk.TAGS` names every tag the renderer can emit,
    and a test asserts each one is configured here -- a span carrying an
    unconfigured tag renders as unstyled body text, silently.
  * the placeholder values. `help_docs` owns the ALLOWLIST of names; this owns
    what they resolve to on this machine, because it is the only side with a
    `cfg`.
  * corpus problems. A document that could not be read is listed as a problem
    rather than quietly dropping its topics, so a shorter list always has a
    reason attached.

The three manager dialogs that used to sit in the left nav are gone from here.
They were actions, discovered through the documentation surface because it had
a spare column; they live in Settings -> Paths & Tools, and Test Manager also on
the Git tab. Help explains them; it no longer launches them.

Inline "Explain" still streams a short LLM summary, and "Ask" still pre-fills
the Ask tab.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

from constants import C, _BASE_DIR, _CONFIG_PATH, LOG_FILE
from helpers import help_docs
from helpers.markdown_tk import render

if TYPE_CHECKING:
    from state import ManagerConfig


#: The row that shows corpus problems. A sentinel rather than a topic key, so
#: it can never collide with one that came from a document.
_PROBLEMS_KEY = "\x00problems"


def _open_doc(path: str) -> None:
    """Open a documentation file in the system default viewer."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        try:
            subprocess.run(["open", path], check=True)
        except Exception:
            pass
    else:
        try:
            subprocess.run(["xdg-open", path], check=True)
        except Exception:
            from tkinter import messagebox
            messagebox.showwarning(
                "Cannot open file",
                "Could not open the document automatically.\nPath: %s" % path)


class HelpTabController:
    """Topic list + search, over the markdown in `help_docs.HELP_DOCUMENTS`."""

    def __init__(
        self,
        notebook: "ttk.Notebook",
        cfg: "ManagerConfig",
        *,
        on_seed_ask: Optional[Callable[[str, str], None]] = None,
        on_llm_cfg: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._cfg = cfg
        self._on_seed_ask = on_seed_ask
        self._on_llm_cfg = on_llm_cfg

        # Streaming explain state -- a monotonic id discards zombie tokens from
        # a stream whose section the user has already navigated away from.
        self._explain_req_id: int = 0
        self._explain_running: bool = False
        self._current_explain_text: Optional[str] = None

        self._rows: list = []          # listbox index -> topic key
        self._current_key: str = ""
        self._build(notebook)

    # -- Construction ----------------------------------------------------

    def _build(self, notebook: "ttk.Notebook") -> None:
        tab = tk.Frame(notebook, bg=C["base"])
        notebook.add(tab, text="  Help  ")

        pane = tk.Frame(tab, bg=C["base"])
        pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        left_wrap = tk.Frame(pane, bg=C["base"])
        left_wrap.pack(side=tk.LEFT, fill=tk.Y)

        self._query = tk.StringVar()
        ttk.Entry(left_wrap, textvariable=self._query,
                  width=32).pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        self._query.trace_add("write", lambda *_a: self._refresh_list())
        tk.Label(left_wrap,
                 text="Search reads the text of every topic, not just titles",
                 font=("Segoe UI", 8), bg=C["base"], fg=C["overlay0"],
                 wraplength=215, justify=tk.LEFT).pack(side=tk.TOP,
                                                       anchor=tk.W,
                                                       pady=(0, 6))

        list_wrap = tk.Frame(left_wrap, bg=C["mantle"])
        list_wrap.pack(side=tk.TOP, fill=tk.Y, expand=True)
        self._help_lb = tk.Listbox(
            list_wrap, width=34, font=("Segoe UI", 9),
            bg=C["mantle"], fg=C["text"], selectbackground=C["surface1"],
            selectforeground=C["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
        )
        lb_sb = ttk.Scrollbar(list_wrap, orient="vertical",
                              command=self._help_lb.yview)
        self._help_lb.configure(yscrollcommand=lb_sb.set)
        self._help_lb.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = tk.Frame(pane, bg=C["base"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self._footer = tk.Frame(right, bg=C["base"])
        self._footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self._doc_btn = ttk.Button(self._footer, text="\U0001f4c4 Open docs")
        self._explain_btn = ttk.Button(
            self._footer, text="\U0001f50d Explain",
            command=self._help_explain_clicked)
        self._ask_btn = ttk.Button(self._footer, text="\U0001f916 Ask")

        content_wrap = tk.Frame(right, bg=C["base"])
        content_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        hsb = ttk.Scrollbar(content_wrap, orient="vertical")
        self._help_txt = tk.Text(
            content_wrap, font=("Segoe UI", 10), bg=C["mantle"], fg=C["text"],
            relief=tk.FLAT, padx=16, pady=12, wrap=tk.WORD,
            cursor="arrow", state=tk.DISABLED, yscrollcommand=hsb.set,
        )
        hsb.configure(command=self._help_txt.yview)
        self._help_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._configure_tags()

        self._help_lb.bind("<<ListboxSelect>>", self._on_help_select)
        self._refresh_list()
        for index, key in enumerate(self._rows):
            if key:                       # skip the document headers
                self._help_lb.selection_set(index)
                self._show(key)
                break

    def _configure_tags(self) -> None:
        """Every tag `markdown_tk` can emit, plus the two Explain uses.

        A span whose tag was never configured renders as plain body text and
        nothing reports it, so `tests/test_help_tab.py` checks this list
        against `markdown_tk.TAGS` rather than trusting the two to stay in
        step by hand.
        """
        t = self._help_txt
        t.tag_configure("h1", font=("Segoe UI", 14, "bold"),
                        foreground=C["blue"], spacing1=14, spacing3=6)
        t.tag_configure("h2", font=("Segoe UI", 11, "bold"),
                        foreground=C["lavender"], spacing1=12, spacing3=3)
        t.tag_configure("h3", font=("Segoe UI", 10, "bold"),
                        foreground=C["subtext"], spacing1=8, spacing3=2)
        t.tag_configure("body", foreground=C["text"], spacing3=3)
        t.tag_configure("bold", font=("Segoe UI", 10, "bold"),
                        foreground=C["text"])
        t.tag_configure("code", font=("Consolas", 9), foreground=C["peach"])
        t.tag_configure("dim", foreground=C["overlay0"])
        t.tag_configure("warn", font=("Segoe UI", 10, "bold"),
                        foreground=C["yellow"])
        t.tag_configure("ok", font=("Segoe UI", 10, "bold"),
                        foreground=C["green"])

    # -- The list --------------------------------------------------------

    def _refresh_list(self) -> None:
        """Rebuild the sidebar: the table of contents, or search results."""
        query = self._query.get().strip()
        self._help_lb.delete(0, tk.END)
        self._rows = []

        found, problems = help_docs.scan()
        if problems:
            self._help_lb.insert(tk.END, "  ⚠  %d corpus problem%s"
                                 % (len(problems),
                                    "" if len(problems) == 1 else "s"))
            self._rows.append(_PROBLEMS_KEY)

        if query:
            hits = help_docs.search(query)
            for hit in hits:
                self._help_lb.insert(tk.END, "  " + hit.topic.title)
                self._rows.append(hit.topic.key)
            if not hits:
                self._help_lb.insert(tk.END, "  (nothing matches)")
                self._rows.append("")
            return

        # Grouped by document. 51 topics in one flat column reads as a heap;
        # the grouping is also the only place a reader can see that help and
        # the repository's documents are the same thing.
        current = ""
        for item in found:
            if item.document != current:
                current = item.document
                self._help_lb.insert(
                    tk.END, "  " + os.path.basename(current).replace(".md", ""))
                self._help_lb.itemconfig(self._help_lb.size() - 1,
                                         foreground=C["overlay0"])
                self._rows.append("")          # a header selects nothing
            indent = "    " + ("    " if item.level >= 3 else "")
            self._help_lb.insert(tk.END, indent + item.title)
            self._rows.append(item.key)

    def _on_help_select(self, _event=None) -> None:
        selection = self._help_lb.curselection()
        if not selection:
            return
        index = selection[0]
        key = self._rows[index] if index < len(self._rows) else ""
        if key:
            self._show(key)

    # -- Rendering -------------------------------------------------------

    def _placeholder_values(self) -> dict:
        """What `{{name}}` resolves to on this machine.

        `help_docs` owns which names are legal; this owns what they mean,
        because it is the side that has a `cfg`. A name with no value renders
        "unknown" rather than an empty string -- an empty one silently reads
        as though there were nothing to say.
        """
        from constants import APP_VERSION

        wrapper = os.path.join(_BASE_DIR, "tokensave-wrapper.exe")
        if not os.path.isfile(wrapper):
            wrapper = os.path.join(_BASE_DIR, "src", "tokensave-wrapper.py")
        return {
            "template_dir": self._cfg.template_dir,
            "install_dir": _BASE_DIR,
            "config_path": _CONFIG_PATH,
            "log_file": LOG_FILE,
            "app_version": APP_VERSION,
            "wrapper_path": wrapper,
        }

    def _show(self, key: str) -> None:
        self._explain_req_id += 1          # invalidate any in-flight stream
        self._current_key = key

        if key == _PROBLEMS_KEY:
            self._render_problems()
            return

        try:
            markdown = help_docs.topic_markdown(key)
            item = help_docs.topic(key)
        except help_docs.HelpUnavailable as exc:
            self._fill([("This topic could not be loaded.\n\n", "warn"),
                        (str(exc) + "\n", "dim")])
            self._wire_footer(None, "")
            return

        text = help_docs.substitute(markdown, self._placeholder_values())
        self._fill(render(text))
        self._wire_footer(help_docs.document_path(item.document), item.title)

    def _render_problems(self) -> None:
        spans = [("Help corpus problems\n", "h1"),
                 ("These documents are part of the help but could not be "
                  "used. The topics they carry are missing from the list "
                  "above -- which is why this row exists, rather than the "
                  "list simply being shorter.\n\n", "body")]
        for problem in help_docs.scan()[1]:
            spans.append((problem.document + "\n", "bold"))
            spans.append(("  " + problem.detail + "\n\n", "dim"))
        self._fill(spans)
        self._wire_footer(None, "")

    def _fill(self, spans) -> None:
        self._help_txt.configure(state=tk.NORMAL)
        self._help_txt.delete("1.0", tk.END)
        for text, tag in spans:
            self._help_txt.insert(tk.END, text, tag)
        self._help_txt.mark_set("baseline_end", "end-1c")
        self._help_txt.mark_gravity("baseline_end", tk.LEFT)
        self._help_txt.configure(state=tk.DISABLED)
        self._help_txt.yview_moveto(0)

    def _wire_footer(self, doc_path, title: str) -> None:
        if doc_path and os.path.isfile(doc_path):
            self._doc_btn.configure(command=lambda p=doc_path: _open_doc(p))
            self._doc_btn.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._doc_btn.pack_forget()

        self._current_explain_text = title
        if title:
            state = tk.DISABLED if self._explain_running else tk.NORMAL
            self._explain_btn.configure(text="\U0001f50d Explain", state=state)
            self._explain_btn.pack(side=tk.LEFT, padx=(0, 6))
        else:
            self._explain_btn.pack_forget()

        if title and self._on_seed_ask:
            question = ("Explain this TokenSave Manager topic and how to use "
                        "it: %s" % title)
            self._ask_btn.configure(
                command=lambda q=question: self._on_seed_ask(q, _BASE_DIR))
            self._ask_btn.pack(side=tk.LEFT)
        else:
            self._ask_btn.pack_forget()

    # -- Inline Explain (unchanged behaviour) ----------------------------

    def _help_explain_clicked(self) -> None:
        explain_text = self._current_explain_text
        if not explain_text:
            return
        llm_cfg: dict = self._on_llm_cfg() if self._on_llm_cfg else {}
        if not llm_cfg.get("enabled"):
            self._replace_tail(
                "\n\nConfigure an LLM in Settings → AI to use inline "
                "explanations.", "dim")
            return

        self._explain_req_id += 1
        my_id = self._explain_req_id
        ctx = self._help_txt.get("1.0", "baseline_end")[:800]
        self._explain_running = True
        self._explain_btn.configure(state=tk.DISABLED)
        threading.Thread(target=self._help_explain_worker,
                         args=(my_id, ctx, explain_text, llm_cfg),
                         daemon=True).start()

    def _help_explain_worker(self, req_id: int, ctx: str, explain_text: str,
                             llm_cfg: dict) -> None:
        """Background thread: stream an LLM explanation into the pane."""
        from helpers.llm import _call_llm

        system = ("You are a concise help assistant for TokenSave Manager. "
                  "Explain this topic clearly with a practical example. "
                  "3-5 sentences.")
        user = "Topic: %s\n\nContext:\n%s" % (explain_text, ctx)
        stream_state = {"first": True}

        def on_token(tok: str) -> None:
            is_first = stream_state["first"]
            stream_state["first"] = False

            def _put(t: str = tok, req: int = req_id, first: bool = is_first):
                if self._explain_req_id != req:
                    return             # zombie token from an abandoned stream
                self._help_txt.configure(state=tk.NORMAL)
                if first:
                    try:
                        self._help_txt.delete("baseline_end", tk.END)
                    except tk.TclError:
                        pass
                    self._help_txt.insert(tk.END, "\n\n", "body")
                self._help_txt.insert(tk.END, t, "dim")
                self._help_txt.see(tk.END)
                self._help_txt.configure(state=tk.DISABLED)

            self._help_txt.after(0, _put)

        try:
            result = _call_llm(llm_cfg, system, user, max_tokens=300,
                               timeout=30, on_token=on_token)
            if stream_state["first"]:
                tail = result or ("LLM did not return a response. Check "
                                  "Settings → AI.")

                def _put_result(r: str = tail, req: int = req_id):
                    if self._explain_req_id != req:
                        return
                    self._replace_tail("\n\n" + r, "dim")

                self._help_txt.after(0, _put_result)
        except Exception as exc:
            err = str(exc)

            def _show_err(m: str = err, req: int = req_id):
                if self._explain_req_id != req:
                    return
                self._replace_tail("\n\nError: " + m, "warn")

            self._help_txt.after(0, _show_err)
        finally:
            def _cleanup():
                self._explain_running = False
                try:
                    if self._explain_btn.winfo_ismapped():
                        self._explain_btn.configure(state=tk.NORMAL)
                except tk.TclError:
                    pass

            self._help_txt.after(0, _cleanup)

    def _replace_tail(self, text: str, tag: str) -> None:
        """Swap whatever a previous Explain left after the rendered topic."""
        self._help_txt.configure(state=tk.NORMAL)
        try:
            self._help_txt.delete("baseline_end", tk.END)
        except tk.TclError:
            pass
        self._help_txt.insert(tk.END, text, tag)
        self._help_txt.see(tk.END)
        self._help_txt.configure(state=tk.DISABLED)
