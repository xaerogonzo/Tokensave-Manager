# Live-test fixture workspace

A **template**, not a working tree. `test/integration/runTests.js` copies it to
`.vscode-test/workspace/` and runs `git init` there, so the copy under test is
disposable and this directory is never a nested git repository.

`src/broken.py` is defective on purpose and must stay that way — see its
docstring.
