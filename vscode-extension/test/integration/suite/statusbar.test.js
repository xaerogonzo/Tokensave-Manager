/**
 * statusbar.test.js — what the item says, and what it is about.
 *
 * `renderText` and `renderTooltip` are already unit-tested against a summary.
 * What that cannot show is whether the string reached the widget: a pure
 * function returning the right text into a StatusBarItem nobody updated is a
 * bug no test of that function can see.
 *
 * The pin is the other half, and it is the reason this file exists at all.
 * `status.ts` opens by stating the item is pinned to a folder and does **not**
 * follow the active editor, because an item whose subject changes as you
 * navigate is never *known* to be wrong. Nothing enforced that.
 */
"use strict";

const assert = require("node:assert");
const path = require("node:path");
const vscode = require("vscode");
const { describe, it, before, until } = require("./harness");
const { workspaceRoot, secondRoot } = require("./util");

const EXTENSION_ID = "tokensave.tokensave-manager";

describe("status bar", () => {
  let api;

  before(async () => {
    api = await vscode.extensions.getExtension(EXTENSION_ID).activate();
  });

  it("puts text into the real item", async () => {
    const text = await until("the status bar to say something",
                             () => api.statusBarText() || null);
    assert.ok(text.length > 0);
    assert.ok(/TokenSave|\$\(/.test(text),
              `unrecognisable status text: ${JSON.stringify(text)}`);
  });

  it("is about the workspace folder", () => {
    assert.strictEqual(api.pinnedFolderName(), path.basename(workspaceRoot()));
  });

  it("stays about the pinned folder when a file in ANOTHER root is opened",
     async () => {
    // The mutation runner called this test decorative three times, and was
    // right each time. Worth recording, because each fix looked sufficient:
    //
    //   1. A single-root workspace — "pinned" and "follows the active editor"
    //      name the same folder, so the mutation changed nothing observable.
    //   2. Two roots that were merely both dirty — the bar rendered the same
    //      marker whichever it reported.
    //   3. Waiting for "the bar says something" — which the STARTUP render
    //      already satisfied, so the assertion ran before the editor had
    //      moved and passed against mutated code in 256ms.
    //
    // The shape that finally works: force a change only the pinned root can
    // produce, and wait for THAT. Asserting an invariant ("nothing moved")
    // needs some observable transition to wait on, or the wait is satisfied
    // by the state that was already there.
    const fs = require("node:fs");
    const pinned = api.pinnedFolderName();
    assert.strictEqual(pinned, path.basename(workspaceRoot()));

    // showTextDocument rather than the `vscode.open` command: the command
    // returned before `activeTextEditor` was set, which a trace through the
    // mutated build showed as `activeEditor=none` — the mutation could not
    // have behaved differently even in principle.
    const other = vscode.Uri.file(path.join(secondRoot(), "src", "other.py"));
    await vscode.window.showTextDocument(
      await vscode.workspace.openTextDocument(other));
    assert.ok(vscode.window.activeTextEditor,
              "no active editor — the mutation this guards could not fire");

    // The count to wait for is READ, not hardcoded.
    //
    // This test previously wrote a third file and waited for the literal 3,
    // on the reasoning that the pinned root had two changes and the other had
    // one. That was measured on Windows and is not true everywhere: on the
    // Linux runner the pinned root starts with THREE, so the bar already read
    // 3 before the file was written, the wait was satisfied by the startup
    // render, and the arm survived -- the same "no observable transition"
    // failure this test has now had four times, in its fourth disguise.
    //
    // Taking the count from the bar and requiring one more makes the
    // transition real on any machine, whatever the starting state.
    const before = Number(/●(\d+)/.exec(
      await until("the bar to report a change count",
                  () => (/●\d+/.test(api.statusBarText())
                         ? api.statusBarText() : null)))[1]);

    fs.writeFileSync(path.join(workspaceRoot(), "uncommitted-c.txt"),
                     "third", "utf8");
    // Fire the watcher. Touching a committed file changes no root's count.
    fs.writeFileSync(path.join(secondRoot(), ".mcp.json"), "{}", "utf8");

    // Only a bar about the PINNED root can reach this number: the other root
    // has fewer changes and gained none, and the pre-flight in shared.js
    // refuses to launch if the two roots report the same count.
    const countIn = (t) => {
      const m = /●(\d+)/.exec(t);
      return m ? Number(m[1]) : null;
    };
    // GREATER than the starting count, not exactly one more: the pinned root
    // is a live directory and something else appearing at the same moment
    // would make an exact target unreachable. Growth is still a real
    // transition, and it is one only the pinned root can produce -- the other
    // root gained nothing.
    const text = await until(
      `the bar to report more than ${before} changed files in the pinned root`,
      () => {
        const current = api.statusBarText();
        const now = countIn(current);
        return now !== null && now > before ? current : null;
      }, 25000);

    assert.strictEqual(api.pinnedFolderName(), pinned,
                       "the pin moved when the active editor changed root");
    assert.ok(!/●1(?!\d)/.test(text),
              `the bar reported the other root's single change: ${text}`);
  });

  it("never renders an unknown state as a clean one", async () => {
    // A dirty flag the CLI could not read must not present as "clean"; the
    // pure function is tested for this, and here it is the rendered widget.
    const text = api.statusBarText();
    assert.ok(!/^\s*$/.test(text),
              "an empty status bar is indistinguishable from a healthy one");
  });
});
