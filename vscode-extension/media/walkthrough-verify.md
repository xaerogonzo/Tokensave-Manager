## Why this runs something instead of checking a string

A path that is set but does not work looks exactly like a path that does. The
checkout moved; `python` is not on PATH; the interpreter cannot import what
`cli.py` needs. In every case the setting reads as correct and every command
fails separately, with a different message.

Verification runs the Manager's `commands` call and reports what came back. It
needs no project, so it works before you have opened a folder, and answering
"what can you do" is its entire job.
