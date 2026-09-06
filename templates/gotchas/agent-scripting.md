# Editing Files From Scripts (Agent-Authored)

Written after the same class of bug appeared four times in one session. Every
instance was silent: the script reported success, and the damage was a single
wrong character in a file nobody re-read.

---

## 1. Backslashes get eaten between the tool call and Python

**Symptom:** A Windows path written by a script comes out corrupted, and the
script says it succeeded.

| You intended | What landed | Because |
|---|---|---|
| `src\cleanforge\app.py` | `src\cleanforge` + BEL + `pp.py` | `\a` |
| `.tokensave\tokensave.db` | `.tokensave` + TAB + `okensave.db` | `\t` |
| `$ROOT\templates\*` | `$ROOT` + TAB + `emplates\*` | `\t` |
| `tools\uishot\...` | `SyntaxError: truncated \uXXXX escape` | `\u` |

**Cause:** Two layers of escaping — the tool-call encoding and the Python string
literal — and a backslash can be consumed by either. `"\\t"` in your source is
*supposed* to survive as `\t`, but if one layer strips a backslash you get a
tab. The dangerous ones are `\a \b \f \n \r \t \v` (silent corruption) and `\u`
(at least a loud `SyntaxError`).

**Fix — never write a Windows path as a literal in a script:**

```python
BSL = chr(92)
target = "src" + BSL + "myapp" + BSL + "app.py"
```

Or sidestep it entirely: use forward slashes, `pathlib`, or a real raw string
you have verified round-trips. **Then assert the result is clean:**

```python
assert chr(9) not in new_text and chr(7) not in new_text, "escape mangling"
```

**Best fix:** for anything with tricky quoting, write the file with a dedicated
file-writing tool rather than a shell heredoc. Heredocs add a third escaping
layer, and PowerShell here-strings add a fourth.

---

## 2. Assert before you write, so a failed edit changes nothing

**Symptom:** A multi-part edit applies two of three changes, then raises. The
file is left half-edited and you cannot tell which half.

**Fix:** Do every `assert` and every string operation in memory, and write once
at the end:

```python
s = p.read_text(encoding="utf-8")
assert anchor_a in s, "anchor A moved"      # all assertions first
assert anchor_b in s, "anchor B moved"
s = s.replace(anchor_a, new_a, 1)
s = s.replace(anchor_b, new_b, 1)
p.write_text(s, encoding="utf-8")           # single write, or none
```

An `AssertionError` before the write is a **no-op**, which is the behaviour you
want from a failed edit. Verified repeatedly: a script that died on its third
assertion left the file untouched, and the fix was one line rather than a
restore.

Always `count=1` on `replace()`. An unbounded replace silently edits matches you
never looked at.

---

## 3. A multi-line search string fails on CRLF

**Symptom:** `assert old in s` fails on text you can see in the file.

**Cause:** Your search string joins lines with `\n`; the file uses `\r\n`.
Single-line searches work, so the failure looks arbitrary.

**Fix:** Detect and use the file's own ending, reading bytes so nothing is
normalised behind your back:

```python
raw = p.read_bytes().decode("utf-8")
nl = "\r\n" if "\r\n" in raw else "\n"
needle = nl.join(["line one", "line two"])
```

Or anchor on **one distinctive line** and edit by index. Line-based editing has
no newline problem at all.

---

## 4. `lstrip()` does not strip box-drawing characters

**Symptom:** An assertion against a line in an ASCII-art directory tree fails,
and the error prints a line that looks like it should have matched.

**Cause:** `│   └── file.md`.lstrip() returns `│   └── file.md` — `│` (U+2502)
is not whitespace, so nothing is stripped.

**Fix:** Test for a substring rather than a prefix: `assert "file.md" in line`.

---

## 5. A bulk rename will rewrite the document explaining the rename

**Symptom:** After a project-wide find-and-replace, the ADR recording *why the
old name was abandoned* now claims the **new** name has the problem. It reads as
confident nonsense.

**Cause:** Historical documents legitimately contain the old name. A blanket
replace cannot tell "this is the current name" from "this is the name we
stopped using, and here is why".

**Fix:** Exclude documents that are *about* the change, or re-read them
afterwards. Add a note at the top so the next reader is not confused:

```markdown
> Historical note: this document discusses names the project no longer uses.
> They appear deliberately — this is the record of why they were abandoned.
```

**General rule:** after any bulk rename, grep for the old name and look at each
survivor. Some are stale; some are load-bearing history.

---

## 6. Do not re-read a file you just wrote — but do verify the *content*

Editing tools error if a write fails, so re-reading to confirm "did it save" is
wasted work. That is different from verifying the write said what you meant:
grep for the specific string you intended, especially anything containing a
backslash. `grep -n "templates" file.md` would have caught three of the four
bugs above in a second.
