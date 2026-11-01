# cursor_models fixtures

Captured from `agent --list-models`, CLI version 2026.10.22-27dcf5f, 2026-10-31.

- `list_models.txt` is an **excerpt** (16 of the 270 the real CLI printed on this account) chosen to cover the
  shapes that matter: ids with dots and many dashes, a label with a double space
  (`Muse Wonder  Minimal`), a trailing `(NO ZDR)` in a label, and the blank line
  plus `Tip:` prose that follows the list and must not be read as a model.
- `needs_login.txt` is the real signed-out output of `agent models`. The signed-out
  output of `--list-models` itself was not captured separately; the parser keys
  on the message text, so a different wording would read as FAILED, not as a model list.

If Cursor changes this format, replace these with a fresh capture. Do not edit
them by hand to make a parser change pass.
