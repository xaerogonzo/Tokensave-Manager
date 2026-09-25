# The VS Code driver

A real VS Code, worked the way a person works it, with a screenshot and a checklist
for every step. Written for PyScope's extension first; this is the same driver.

```bash
npm run drive -- smoke              # does the driver work at all
npm run drive -- menu               # editor / Explorer context menus, palette, status bar menu
npm run drive -- views              # Projects tree (both roots) and Savings & Spend
npm run drive -- smoke menu views   # several, one editor each
npm run drive -- menu --visible     # on your own desktop, to watch it happen
```

Each run writes `drive-out/<scenario>/index.html` and `report.md`: every step, every
check, and the screenshots.

`lib/` is **byte-for-byte the same** as `vscode-extension/drive/lib/` in PyScope
(`Random Projects/Pet Python/extension/drive/lib`), which is canonical. Its README
explains the design, the isolation (throwaway profile, private desktop) and the
things learned the hard way; read that one. Change `lib/` there and copy it here.
What is specific to this extension is `run.js` and `scenarios/`.

## What is specific here

- **The workspace is the live suite's own fixture**, built by
  `test/integration/shared.js`: two git roots with different numbers of uncommitted
  files, one of them containing a file the checks flag. The driver looks at the states
  the suite asserts about, and inherits the fixture's own proof that it can produce
  them.
- **The extension activates once startup has finished** (`onStartupFinished`). It
  used to declare no `activationEvents`, so a freshly opened window had no TokenSave
  status bar item, save watchers or CodeLens until a view or command was first used;
  the driver found that, and `smoke` now holds the extension to the new behaviour
  (`scenarios/common.js`).
- **One context submenu.** *Checks This File*, *Test Gaps for This File*, *Propose a
  Commit…* and the two Manager commands sit under a single **TokenSave Manager ›**
  entry in the editor and Explorer menus; `menu` checks there are no loose entries.
- **The status item is found by accessibility label**, not text: its text goes from
  `TokenSave: ?` to a git summary like `master ●4`, and only the label keeps the name.
- **An activity bar entry is an icon**, named by `aria-label`, not by text.

## Not run by CI

It needs a local VS Code and a screen (or a private desktop). It is a tool for
looking at the extension, not a gate.
