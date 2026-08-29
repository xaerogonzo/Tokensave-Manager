/**
 * savings.ts — the Savings & Spend view, rendering the same honest numbers
 * the Manager's own dialog does.
 *
 * The panel this mirrors used to display money *spent*, at API list price, in
 * a card labelled "Value Recouped" — $4132.75 where the real savings figure
 * was $0.14. So the rules it follows are carried here rather than re-derived:
 *
 * * **savings and spend are different quantities with different scopes.**
 *   `gain` is per-project; `cost` is machine-global with no project filter, and
 *   the heading says so rather than a subtitle.
 * * **nothing is derived that upstream did not report.** Cache reads render as
 *   "not reported", because the only available derivation is provably zero.
 * * **unknown is never zero.** Each section has its own state, and a section
 *   that could not be read says so with the reason.
 *
 * Architecture: the **extension host** runs the CLI and posts a normalised
 * model; the webview only renders. No subprocess is reachable from page code,
 * and no data is concatenated into a script block — it arrives by message.
 *
 * A `WebviewViewProvider` rather than a `WebviewPanel`, deliberately: the
 * intent is a surface that stays open beside the tree, which is what a view
 * is. A panel would open a one-off editor tab with a different lifecycle.
 */
import * as vscode from "vscode";
import { CliResult, runCli } from "./cli";

/** Ranges the CLI accepts. Mirrors `helpers/savings.RANGES`. */
export const RANGES = ["today", "7d", "30d", "all"] as const;
export type Range = typeof RANGES[number];

export function isRange(value: unknown): value is Range {
  return typeof value === "string"
    && (RANGES as readonly string[]).includes(value);
}

/**
 * Escape for a **text node**.
 *
 * Separate from the attribute escaper on purpose. One generic `escapeHtml`
 * used in both places is how one of the two contexts ends up subtly wrong, and
 * every string here — file paths, model names, CLI error text — is untrusted
 * content that reaches the page.
 */
export function escapeText(value: unknown): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Escape for a quoted **attribute value**. Adds the quote characters. */
export function escapeAttr(value: unknown): string {
  return escapeText(value)
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** A cryptographically-arbitrary nonce for the CSP's `script-src`. */
export function makeNonce(): string {
  const alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    out += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
  }
  return out;
}

/**
 * The CSP the view is served under.
 *
 * `script-src` takes a nonce rather than `unsafe-inline`: the page renders
 * untrusted strings, and a blanket allowance would make an escaping slip
 * exploitable instead of merely ugly. `default-src 'none'` means anything not
 * named here — images, fonts, connections — is refused outright.
 */
export function contentSecurityPolicy(cspSource: string,
                                      nonce: string): string {
  return [
    "default-src 'none'",
    `style-src 'unsafe-inline' ${cspSource}`,
    `script-src 'nonce-${nonce}'`,
  ].join("; ");
}

/** One section's state. `stale` is why a cached snapshot cannot pass as live. */
export type SectionState = "loading" | "loaded" | "stale" | "unavailable"
  | "error";

export interface SavingsModel {
  range: Range;
  savings: Record<string, unknown> | null;
  savingsHistory: Record<string, unknown> | null;
  spend: Record<string, unknown> | null;
  opportunity: Record<string, unknown> | null;
  spendFetchedAt: number | null;
  error: string | null;
}

/** Normalise a `cost` envelope into the model the page renders. */
export function toModel(result: CliResult, range: Range,
                        spendFetchedAt: number | null): SavingsModel {
  const data = result.envelope?.data as Record<string, unknown> | undefined;
  if (!data) {
    return {
      range, savings: null, savingsHistory: null, spend: null,
      opportunity: null, spendFetchedAt: null,
      error: result.transportError ?? result.envelope?.error
        ?? "the Manager CLI produced no readable result",
    };
  }
  return {
    range,
    savings: (data.savings ?? null) as Record<string, unknown> | null,
    savingsHistory:
      (data.savings_history ?? null) as Record<string, unknown> | null,
    spend: (data.spend ?? null) as Record<string, unknown> | null,
    opportunity: (data.opportunity ?? null) as Record<string, unknown> | null,
    spendFetchedAt,
    error: null,
  };
}

export class SavingsViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "tokensaveManager.savings";

  private view: vscode.WebviewView | undefined;
  private range: Range = "30d";
  private busy = false;
  private spendFetchedAt: number | null = null;

  constructor(private readonly context: vscode.ExtensionContext,
              private readonly folder: () => vscode.WorkspaceFolder |
                undefined) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      // Nothing is loaded from disk, so nothing needs to be reachable.
      localResourceRoots: [],
    };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((message: { type?: string;
                                                 range?: unknown }) => {
      if (message?.type === "range" && isRange(message.range)) {
        this.range = message.range;
        void this.refresh();
      } else if (message?.type === "refresh") {
        void this.refresh();
      }
    });
    void this.refresh();
  }

  async refresh(): Promise<void> {
    const view = this.view;
    const folder = this.folder();
    if (!view) {
      return;
    }
    if (!folder) {
      void view.webview.postMessage({
        type: "model",
        model: { range: this.range, savings: null, savingsHistory: null,
                 spend: null, opportunity: null, spendFetchedAt: null,
                 error: "Open a folder first." },
      });
      return;
    }
    // `cost` ingests accounting rows into tokensave's global ledger, so a
    // second click while one is running would start a second ingest.
    if (this.busy) {
      return;
    }
    this.busy = true;
    void view.webview.postMessage({ type: "loading" });
    try {
      const result = await runCli(this.context, "cost", folder,
                                  ["--range", this.range]);
      this.spendFetchedAt = Date.now();
      void view.webview.postMessage({
        type: "model",
        model: toModel(result, this.range, this.spendFetchedAt),
      });
    } finally {
      this.busy = false;
    }
  }

  private html(webview: vscode.Webview): string {
    const nonce = makeNonce();
    const csp = contentSecurityPolicy(webview.cspSource, nonce);
    const options = RANGES
      .map((r) => `<option value="${escapeAttr(r)}"`
        + `${r === this.range ? " selected" : ""}>${escapeText(r)}</option>`)
      .join("");

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}">
<style>
  body { font-family: var(--vscode-font-family);
         font-size: var(--vscode-font-size);
         color: var(--vscode-foreground); padding: 8px 10px; }
  h2 { font-size: 1.05em; margin: 14px 0 2px; }
  .scope { color: var(--vscode-descriptionForeground); font-size: 0.85em;
           margin: 0 0 6px; }
  .stat { display: flex; justify-content: space-between; gap: 10px;
          padding: 2px 0; }
  .stat b { font-variant-numeric: tabular-nums; }
  .muted { color: var(--vscode-descriptionForeground); }
  .warn { color: var(--vscode-editorWarning-foreground); }
  .err { color: var(--vscode-errorForeground); }
  /* Wide tables scroll inside their own box; the page never scrolls sideways. */
  .tablewrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 0.9em; }
  th, td { text-align: left; padding: 2px 8px 2px 0; white-space: nowrap; }
  th { color: var(--vscode-descriptionForeground); font-weight: 600; }
  td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
  select, button { font-family: inherit; font-size: inherit;
                   color: var(--vscode-foreground);
                   background: var(--vscode-dropdown-background);
                   border: 1px solid var(--vscode-dropdown-border);
                   padding: 2px 6px; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
</style>
</head>
<body>
  <div class="row">
    <label for="range">Savings range</label>
    <select id="range">${options}</select>
    <button id="refresh" title="Re-reads tokensave's accounting">↻</button>
  </div>
  <div id="content"><p class="muted">Loading…</p></div>
<script nonce="${escapeAttr(nonce)}">
(function () {
  const vscode = acquireVsCodeApi();
  const content = document.getElementById("content");

  document.getElementById("range").addEventListener("change", (e) => {
    vscode.postMessage({ type: "range", range: e.target.value });
  });
  document.getElementById("refresh").addEventListener("click", () => {
    vscode.postMessage({ type: "refresh" });
  });

  // Every value below goes in through textContent, never innerHTML, so a
  // model string can never become markup no matter what produced it.
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (cls) { node.className = cls; }
    return node;
  }

  function stat(parent, label, value, note) {
    const row = el("div", null, "stat");
    row.appendChild(el("span", label));
    row.appendChild(el("b", value));
    parent.appendChild(row);
    if (note) { parent.appendChild(el("div", note, "muted")); }
  }

  function table(parent, headers, rows) {
    const wrap = el("div", null, "tablewrap");
    const t = document.createElement("table");
    const thead = document.createElement("tr");
    headers.forEach((h, i) => {
      const th = el("th", h);
      if (i > 0) { th.className = "n"; }
      thead.appendChild(th);
    });
    t.appendChild(thead);
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      r.forEach((c, i) => {
        const td = el("td", c);
        if (i > 0) { td.className = "n"; }
        tr.appendChild(td);
      });
      t.appendChild(tr);
    });
    wrap.appendChild(t);
    parent.appendChild(wrap);
  }

  function section(parent, title, scope, cls) {
    parent.appendChild(el("h2", title, cls));
    if (scope) { parent.appendChild(el("p", scope, "scope")); }
  }

  function money(n) { return "$" + Number(n).toFixed(2); }
  function num(n) { return Number(n).toLocaleString(); }

  function unavailable(parent, reason) {
    parent.appendChild(el("p", "Unavailable — " + reason, "warn"));
  }

  function utcDay(epoch) {
    return new Date(Number(epoch) * 1000).toISOString().slice(0, 10);
  }

  function render(model) {
    content.textContent = "";
    if (model.error) {
      content.appendChild(el("p", model.error, "err"));
      return;
    }

    // ── Savings: the honest figure, and the only project-scoped section.
    const s = model.savings;
    section(content, "Savings",
            s && s.ok ? "From tokensave gain — " + s.scope + " · " + model.range
                      : "From tokensave gain");
    if (!s || !s.ok) {
      unavailable(content, (s && s.reason) || "not reported");
    } else {
      stat(content, "Tokens saved", num(s.saved_tokens));
      stat(content, "Tool calls", num(s.calls));
      // The valuation basis travels with the number: a bare dollar figure
      // with no basis is what made the old panel untrustworthy.
      stat(content, "USD saved", money(s.usd),
           "valued at " + s.usd_basis);

      const h = model.savingsHistory;
      if (h && h.ok && h.days && h.days.length) {
        table(content, ["Day (UTC)", "Tokens", "Calls", "USD"],
              h.days.map((d) => [utcDay(d.day), num(d.saved_tokens),
                                 num(d.calls), money(d.usd)]));
        content.appendChild(el("p",
          "Only days with recorded tool calls appear — a missing day means "
          + "no calls, not zero savings.", "muted"));
      }
    }

    // ── Spend: scope in the heading, because this view opens per project.
    const p = model.spend;
    section(content, "Estimated API spend — this machine, all projects",
            "API list price · not your subscription bill");
    if (!p || !p.ok) {
      unavailable(content, (p && p.reason) || "not reported");
    } else {
      if (model.spendFetchedAt) {
        content.appendChild(el("p",
          "Snapshot: " + p.range + " · read "
          + new Date(model.spendFetchedAt).toLocaleTimeString(), "muted"));
      }
      stat(content, "Estimated spend", money(p.total_cost_usd));
      stat(content, "Input tokens", num(p.total_input_tokens));
      stat(content, "Output tokens", num(p.total_output_tokens));
      // Never a number. The only available derivation is provably zero on
      // every payload, so computing it would fabricate a figure.
      stat(content, "Cache reads", "not reported",
           "tokensave does not export this");

      if (p.by_model && p.by_model.length) {
        table(content, ["Model", "Cost", "Tokens"],
              p.by_model.map((m) => [m.model, money(m.cost), num(m.tokens)]));
      }
      if (p.by_category && p.by_category.length) {
        table(content, ["Category", "Cost", "Turns"],
              p.by_category.map((c) => [c.category, money(c.cost),
                                        num(c.turns)]));
      }
      let note = "These are tokensave's reported figures. The spend above "
        + "cannot be derived from the token counts beside it";
      if (p.implied_usd_per_mtok) {
        note += " — they imply " + money(p.implied_usd_per_mtok)
          + " per million tokens";
      }
      content.appendChild(el("p", note + ".", "muted"));
    }

    // ── Opportunity: turns are authoritative; tokens are not shown at all.
    const o = model.opportunity;
    section(content, "Opportunity",
            "From tokensave discover — turns a tokensave query could have "
            + "served");
    if (!o || !o.ok) {
      unavailable(content, (o && o.reason) || "not reported");
    } else {
      content.appendChild(el("p",
        num(o.replaceable_turns) + " of " + num(o.total_turns)
        + " turns could have been served by a tokensave query."));
      if (o.buckets && o.buckets.length) {
        table(content, ["Tool", "Turns", "Suggested instead"],
              o.buckets.map((b) => [b.tool, num(b.turns), b.suggestion]));
      }
      // Always said, whichever way the check went: "withheld" and "there is
      // no such estimate" are different states and a silent omission reads as
      // neither.
      content.appendChild(el("p",
        o.tokens_trustworthy
          ? "Turn counts are the authoritative figure here. tokensave's "
            + "token-recovery estimates are not shown."
          : "Token-recovery estimates are withheld: tokensave's reported "
            + "accounting failed validation (" + o.token_evidence + ").",
        o.tokens_trustworthy ? "muted" : "warn"));
    }
  }

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message.type === "loading") {
      content.textContent = "";
      content.appendChild(el("p", "Loading…", "muted"));
    } else if (message.type === "model") {
      render(message.model);
    }
  });
}());
</script>
</body>
</html>`;
  }
}
