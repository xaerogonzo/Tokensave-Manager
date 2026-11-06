# Cursor User Rules Policy Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Agent Policy is applied, Manager upserts (or pastes) one titled Cursor User Rule so Cursor's baked-in "only commit when asked" does not silently override `AGENTS.md`.

**Architecture:** `InstructionPolicy` stays the source of truth. A new helper renders a machine-wide User Rule card and, if a local JSON store is injectable or discovered, upserts it with backup and dry-run. Cursor 3.29.9's titled-rule store was not a documented local file; missing/unknown store is a structured skip plus paste, never a write into `state.vscdb` or Cursor.exe. The Instruction Composer preview and apply call this once per apply.

**Tech Stack:** Python 3, pytest, existing `helpers.agent_rules._write_atomic`, Tk Composer dialog.

## Global Constraints

- Never write `%LOCALAPPDATA%\Programs\cursor\**` or workbench JS.
- Never emit force-push or hook-bypass permission.
- Do not parse a policy out of Markdown or out of Cursor's store; re-render and compare.
- Do not scan `state.vscdb` (592 MB chat blobs) on preview or apply.
- Retrofit must not N-write the global card; Composer apply runs the upsert once.
- Markdown in this repo is CRLF.
- Tests use fake stores only, never live `%APPDATA%\Cursor`.

## Files

- Create: `src/helpers/cursor_user_rules.py` — render, discover, plan, apply
- Create: `tests/test_cursor_user_rules.py`
- Modify: `src/dialogs/instruction_composer.py` — preview lines + one apply call
- Modify: `docs/ARCHITECTURE.md` — one helper line; bump helpers count
- Modify: `README.md` — Git & Policy paragraph
- Create: `changelog.d/YYYYMMDDHHMM-cursor-user-rules-sync-xxxx.md`

## Interfaces (locked)

```python
RULE_TITLE = "TokenSave git policy"
MARKER = "<!-- TOKENSAVE-MANAGED: cursor-git-policy -->"
STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"

@dataclasses.dataclass(frozen=True)
class ApplyResult:
    status: str          # updated | unchanged | skipped
    reason: str = ""
    path: str = ""
    paste: str = ""
    backup_path: str = ""
    changed: bool = False

def render_body(policy: InstructionPolicy) -> str: ...
def render_paste(policy: InstructionPolicy) -> str: ...
def log_line(result: ApplyResult) -> str: ...
def preview_lines(policy: InstructionPolicy, store_path: str = "",
                  environ: dict | None = None) -> list[str]: ...
def discover_store(environ: dict | None = None) -> str: ...
def plan_upsert(existing: str | None, policy: InstructionPolicy
                ) -> tuple[str | None, bool, str]:
    """(new_text_or_None, changed, skip_reason). None text means skip."""
def apply(policy: InstructionPolicy, store_path: str = "",
          dry_run: bool = False, backup_dir: str = "",
          environ: dict | None = None, now_ms: int | None = None
          ) -> ApplyResult: ...
```

JSON store schema (only format we write):

```json
{"version": 1, "rules": [{"id": "tokensave-git-policy", "title": "...", "content": "..."}]}
```

Other rules in the array are preserved. Unknown JSON / missing `rules` list → skip, bytes unchanged.

`discover_store`: `TOKENSAVE_CURSOR_USER_RULES` env if set and non-empty; otherwise `""`. Do not invent a file Cursor will not read.

---

### Task 1: Render the User Rule body

**Files:**
- Create: `src/helpers/cursor_user_rules.py` (render + constants first)
- Test: `tests/test_cursor_user_rules.py`

**Interfaces:**
- Consumes: `InstructionPolicy`
- Produces: `render_body`, `render_paste`, `MARKER`, `RULE_TITLE`

- [ ] **Step 1: Write failing tests** for ON and OFF bodies containing MARKER, pointing at AGENTS.md TOKENSAVE-MANAGED blocks, naming the baked-in Cursor sentence when commits are on, and never mentioning `--no-verify` or force-push as allowed. `render_paste` starts with the title.

- [ ] **Step 2: Run** `python -m pytest tests/test_cursor_user_rules.py -v --tb=short` — expect import/collection fail.

- [ ] **Step 3: Implement render_body / render_paste only.**

- [ ] **Step 4: Re-run those tests — PASS.**

- [ ] **Step 5: Commit** `feat(policy): render Cursor User Rule text from Agent Policy`

---

### Task 2: Plan and apply against a fake JSON store

**Files:**
- Modify: `src/helpers/cursor_user_rules.py`
- Test: `tests/test_cursor_user_rules.py`

**Interfaces:**
- Produces: `ApplyResult`, `plan_upsert`, `apply`, `log_line`, `discover_store`, `preview_lines`

- [ ] **Step 1: Failing tests**
  - missing store_path → skipped, paste contains body, no file created
  - invalid JSON / missing rules key → skipped, fixture bytes unchanged
  - first apply creates card; second apply unchanged
  - other rules in the array survive
  - toggle commit off rewrites body, does not delete the card
  - dry_run does not write
  - backup contains previous body
  - store_path under `Programs/cursor` → skipped
  - discover_store reads TOKENSAVE_CURSOR_USER_RULES only
  - log_line includes status and skip reason or path

- [ ] **Step 2: Run tests — FAIL on missing apply.**

- [ ] **Step 3: Implement plan_upsert + apply + discover + preview_lines.** Reuse `helpers.agent_rules._write_atomic`. Backup like `cursor_hooks._backup` (`path.backup.{ms}`).

- [ ] **Step 4: Tests PASS.**

- [ ] **Step 5: Commit** `feat(policy): upsert TokenSave git policy User Rule with backup and paste fallback`

---

### Task 3: Wire Instruction Composer

**Files:**
- Modify: `src/dialogs/instruction_composer.py` (`_describe`, `_apply`)
- Test: `tests/test_cursor_user_rules.py` (preview_lines already covers the text the dialog uses; add `test_preview_lines_name_the_store_or_the_paste`)

- [ ] **Step 1: Failing test** that `preview_lines` mentions the title and either a store path or paste fallback.

- [ ] **Step 2:** In `_describe`, append `"\n".join(preview_lines(self._staged))`. In `_apply`, after a successful baseline write, `result = cursor_user_rules.apply(self._staged)` then `_on_log(log_line(result))`; if skipped, also log `result.paste`. Mention the outcome in the Applied info box (one extra sentence, full paste stays in the log).

- [ ] **Step 3: Composer still compiles baseline first; User Rule failure does not roll back the baseline.**

- [ ] **Step 4: Commit** `feat(policy): apply Cursor User Rule from the Agent Policy Composer`

---

### Task 4: Docs

**Files:**
- `README.md` Settings → Git & Policy
- `docs/ARCHITECTURE.md` helpers list (count 158 → 159) + one line for `cursor_user_rules.py` next to `cursor_hooks.py`
- `changelog.d/` via `python scripts/changelog.py new cursor-user-rules-sync`

- [ ] **Step 1: README paragraph:** Cursor Customize → Rules are text-only. The "only commit when asked" line is a Cursor runtime default, not a card in that list. Agent Policy apply upserts (or pastes) the `TokenSave git policy` User Rule from the same policy that compiles `AGENTS.md`.

- [ ] **Step 2: changelog fragment `### Added` bullet, (policy) area.**

- [ ] **Step 3: `python scripts/changelog.py check`**

- [ ] **Step 4: Commit** `docs: Cursor User Rule sync from Agent Policy`

---

### Task 5: Verify

- [ ] Run `python -m pytest tests/test_cursor_user_rules.py tests/test_baseline_blocks.py tests/test_instruction_policy.py -v --tb=short`
- [ ] Confirm no write under Programs\cursor in tests
- [ ] Spec coverage: paste fallback, backup, idempotent, Composer once, no Cursor.exe patch
