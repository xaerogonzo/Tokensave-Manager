/**
 * webview.test.js — the document the panel produces, and the parts of it that
 * are load-bearing.
 *
 * A webview's *rendered DOM* is genuinely out of reach: no test here can read
 * a computed colour or measure a gap. But `panel.webview.html` is a plain
 * string, and every decision this panel makes is a decision about what goes
 * into it. So the appearance stays unverified and the logic does not — and
 * `docs/VERIFICATION.md` says so rather than letting "we have integration
 * tests" imply more than it should.
 *
 * ## Two things this file learned the hard way
 *
 * The CSP arrives through `escapeAttr`, so the attribute holds
 * `default-src &#39;none&#39;` rather than `default-src 'none'`. A regex
 * written against the un-escaped form matches nothing and the test fails
 * while the extension is correct — which is exactly the direction a test
 * should fail in, but only once.
 *
 * And `webview.html` is assigned in exactly one place, `resolveWebviewView`.
 * Refreshes go by `postMessage`. The first version of this file assumed a
 * refresh replaced the document and waited thirty seconds for a second render
 * that could never come. The real invariant is better than the assumed one
 * and is asserted below: **data cannot rewrite the shell, because updates
 * never touch the document at all.**
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { workspaceRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

/** Reverse `escapeAttr`, so a CSP can be read as it will be enforced. */
function unescapeAttr(value) {
  return value
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function csp(html) {
  const m = /<meta http-equiv="Content-Security-Policy" content="([^"]*)"/
    .exec(html);
  return m ? unescapeAttr(m[1]) : null;
}

function cspNonce(html) {
  const policy = csp(html);
  if (!policy) return null;
  const m = /'nonce-([A-Za-z0-9]+)'/.exec(policy);
  return m ? m[1] : null;
}

function scriptNonce(html) {
  const m = /<script nonce="([^"]+)"/.exec(html);
  return m ? m[1] : null;
}

describe("savings webview", () => {
  let api;
  let html;

  before(async () => {
    api = await vscode.extensions.getExtension(EXTENSION_ID).activate();
    await vscode.commands.executeCommand("tokensaveManager.savings");
    html = await until("the webview document", () => api.webviewHtml() || null);
  });

  it("declares a deny-by-default Content-Security-Policy", () => {
    const policy = csp(html);
    assert.ok(policy, "no CSP meta tag");
    assert.ok(/default-src 'none'/.test(policy),
              `CSP should deny by default: ${policy}`);
  });

  it("ties the script's nonce to the one the CSP allows", () => {
    // Two random-looking values that do not match means the CSP blocks the
    // page's own script and the panel renders nothing but its shell — which
    // an assertion on "a nonce is present" would happily call a pass.
    const fromCsp = cspNonce(html);
    const fromTag = scriptNonce(html);
    assert.ok(fromCsp, `CSP declares no script nonce: ${csp(html)}`);
    assert.ok(fromTag, "the script tag carries no nonce");
    assert.strictEqual(fromTag, fromCsp,
                       "the script nonce must be the one the CSP allows");
  });

  it("takes a nonce rather than allowing inline scripts wholesale", () => {
    assert.ok(!/script-src[^;]*unsafe-inline/.test(csp(html)),
              "script-src must not fall back to 'unsafe-inline'");
  });

  it("renders the sections a reader needs, in order", () => {
    const savings = html.indexOf("Savings");
    const spend = html.indexOf("Estimated API spend");
    assert.ok(savings >= 0, "no savings section");
    assert.ok(spend >= 0, "no spend section");
    assert.ok(savings < spend,
              "savings must come before spend: the panel exists because the " +
              "two were once conflated, and the honest number leads");
  });

  it("states the spend section's scope inline, not in a subtitle", () => {
    assert.ok(/all projects/.test(html),
              "spend is machine-global and the heading has to say so");
  });

  it("writes the document exactly once, however often it refreshes", async () => {
    const before = api.webviewRenderCount();
    assert.strictEqual(before, 1,
                       `the shell should be written once, saw ${before}`);

    for (let i = 0; i < 3; i += 1) {
      await vscode.commands.executeCommand("tokensaveManager.savings");
    }
    await new Promise((r) => setTimeout(r, 1500));

    // The property, stated as the module header states it: no data is
    // concatenated into the document, so nothing a refresh returns can
    // rewrite the deterministic shell above it — including a response that
    // arrives late or out of order.
    assert.strictEqual(api.webviewRenderCount(), before,
                       "a refresh replaced the document; updates must go by " +
                       "postMessage so data cannot rewrite the shell");
    assert.strictEqual(cspNonce(api.webviewHtml()), cspNonce(html),
                       "the document changed identity without being rewritten");
  });

  it("carries no fixture-derived data in the document itself", () => {
    // The architectural claim in savings.ts's header — "no data is
    // concatenated into a script block; it arrives by message" — checked
    // rather than trusted. If a project path or a savings figure ever appears
    // in the shell, escaping becomes load-bearing where it currently is not.
    const leaked = [workspaceRoot(), path.basename(workspaceRoot())]
      .filter((needle) => needle && html.includes(needle));
    assert.deepStrictEqual(leaked, [],
      `workspace data reached the document: ${leaked.join(", ")}`);
  });

  it("opens exactly one script element", () => {
    const opens = (html.match(/<script\b/g) || []).length;
    const closes = (html.match(/<\/script>/g) || []).length;
    assert.strictEqual(opens, 1, `expected one <script>, found ${opens}`);
    assert.strictEqual(closes, 1, `expected one </script>, found ${closes}`);
  });
});
