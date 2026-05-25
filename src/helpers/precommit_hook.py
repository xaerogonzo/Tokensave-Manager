"""Pre-commit AI review hook — install / remove / detect + review runner.

Roadmap-2 Phase 5b. Two surfaces in one module because they share the
sentinel marker constant and the severity-parsing logic:

  * Install / detect / remove side — used by the UI (projects_tab.py
    controller) to add or pop the hook into `.git/hooks/pre-commit`.
  * Review runner — called from `src/precommit_review.py` (the script
    git actually invokes). Reads the cached diff, picks a backend,
    parses findings, returns whether to block.

Design choices (decided in plan + cross-checked at implementation time):

* **Git pre-commit hook, not Claude Code Stop hook.** The Stop-hook
  variant is logged in `docs/ROADMAP.md` as future work; the git hook
  covers ~95% of the safety value with cleaner semantics. Per Rule E,
  the decision is documented at the trade-off point.

* **Fail-open everywhere.** Network errors, timeouts, missing backend,
  unparseable LLM output, even unhandled exceptions in the reviewer →
  exit 0 with a stderr notice. The hook MUST NEVER block a commit
  because the AI is sick. If users can't trust the hook to stay out
  of their way when it can't help, they uninstall it (and won't
  reinstall).

* **Warn-only by default.** `precommit_severity_threshold = "none"`
  ships disabled-blocking even when findings exist. Users opt in to
  blocking after they've lived with the noise and trust the signal.

* **Recursion guard.** The hook script sets `TOKENSAVE_PRECOMMIT_RUNNING=1`
  before invoking the reviewer. The reviewer's entry path checks this
  env var and exits 0 if set — defends against a future failure mode
  where the reviewer itself touches git state or invokes something
  that re-triggers the hook.

* **Three-value backend choice** (`precommit_review_backend`):
  - `"auto"`  → prefer `claude --print` if `cfg.claude_cli_exe` is set,
                fall back to the configured `_call_llm` provider
  - `"claude_cli"` → force Claude Code CLI; fail-open if not configured
  - `"llm"`   → force the manager's configured LLM provider; fail-open
                if disabled

  Lets the user pick "use my CC subscription" / "use the configured
  per-token provider" / "let the manager decide" — same cost-control
  principle that drove the Roadmap-1 Claude CLI integration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from constants import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from state import ManagerConfig


# Sentinel marker — identifies hooks WE wrote vs. user-installed ones.
# Version suffix lets us evolve the hook script later without false-positives
# on detect/remove for old versions.
_HOOK_MARKER = "# TOKENSAVE-PRECOMMIT-MARKER v1"
_HOOK_PATH_REL = os.path.join(".git", "hooks", "pre-commit")

# Stage 1 reviewer system prompt — copied verbatim from
# `src/dialogs/ai_code_review.py:AICodeReviewDialog._SYSTEM_PROMPT`.
# Keeping a private copy here (rather than importing) means the hook
# script can run without pulling in Tk (and without a circular import).
_REVIEW_SYSTEM_PROMPT = (
    "You are a senior code reviewer reviewing a pending git diff. "
    "Produce a structured Markdown report.\n\n"
    "Output format:\n\n"
    "## ⚠ High severity\n"
    "- <finding> (file:line)\n\n"
    "## ⚡ Medium severity\n"
    "- <finding> (file:line)\n\n"
    "## 💡 Low severity / nits\n"
    "- <finding> (file:line)\n\n"
    "## ℹ Observations\n"
    "- <design note>\n\n"
    "Rules:\n"
    "- One bullet per finding. Cite file:line when possible.\n"
    "- Omit a section entirely if it has nothing — don't write empty sections.\n"
    "- Do NOT repeat the diff back at me.\n"
    "- Focus on correctness, security, and maintainability — not formatting.\n"
    "- Match the project's existing style (judge by surrounding context).\n"
    "- Output ONLY the report. No preamble, no closing remarks."
)


# ── Install / remove / detect ─────────────────────────────────────────────

def hook_path(project_path: str) -> str:
    """Absolute path to the project's .git/hooks/pre-commit file."""
    return os.path.join(project_path, _HOOK_PATH_REL)


def is_pre_commit_hook_installed(project_path: str) -> bool:
    """True if `.git/hooks/pre-commit` exists AND contains our marker.

    A hook the user installed themselves doesn't count — we won't touch it
    on remove() either, so detect/remove stay symmetric.
    """
    p = hook_path(project_path)
    if not os.path.isfile(p):
        return False
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return _HOOK_MARKER in f.read()
    except OSError:
        return False


def install_pre_commit_hook(
    project_path: str,
    python_exe: str,
    reviewer_script: str,
) -> tuple[bool, str]:
    """Write the pre-commit hook script. Returns (ok, message).

    Refuses to overwrite a user-installed hook (one without our marker) —
    surfaces the conflict so the user can resolve manually rather than
    silently destroying their work. Replacing OUR OWN existing hook is
    fine (handles re-install / version upgrade).

    The hook hard-codes absolute paths to `python_exe` and `reviewer_script`
    at install time. If either is moved later, the hook will start failing
    and the user has to re-install via the UI. That trade-off is acceptable
    — the alternative (dynamic lookup) would either require sourcing the
    manager's config at hook time (slow) or assuming a PATH layout (fragile).
    """
    hooks_dir = os.path.join(project_path, ".git", "hooks")
    if not os.path.isdir(os.path.join(project_path, ".git")):
        return False, f"{project_path} is not a git repo (no .git/ directory)"
    try:
        os.makedirs(hooks_dir, exist_ok=True)
    except OSError as e:
        return False, f"could not create {hooks_dir}: {e}"

    p = hook_path(project_path)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        except OSError as e:
            return False, f"could not read existing hook: {e}"
        if _HOOK_MARKER not in existing:
            return False, (
                ".git/hooks/pre-commit already exists and was NOT written "
                "by TokenSave Manager. Refusing to overwrite — back up or "
                "remove it manually first, then retry.")

    # `pythonw.exe` is the WINDOWLESS Python interpreter — used by the
    # manager's bat launcher to suppress the console window. But for a
    # git hook we NEED stdout/stderr to reach git's output, otherwise
    # the user sees no review findings (silent fail-open). Swap to the
    # adjacent python.exe when it exists. Failure path: keep pythonw
    # and tell the user — better than silently swapping to a non-existent
    # path that breaks the hook later.
    python_exe = _prefer_console_python(python_exe)
    script = _render_hook_script(python_exe, reviewer_script)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
    except OSError as e:
        return False, f"could not write hook: {e}"

    # POSIX exec bit — git for Windows honours the file extension / shebang,
    # but on real POSIX (if the user ever shares the repo) the bit matters.
    try:
        os.chmod(p, 0o755)
    except OSError:
        pass  # Windows ignores chmod; not fatal

    return True, "installed"


def remove_pre_commit_hook(project_path: str) -> tuple[bool, str]:
    """Remove our hook. Refuses to touch a user-installed one.

    Returns (ok, message). `(False, "no hook installed")` is the
    "nothing to do" case — UI surfaces it as info, not an error.
    """
    p = hook_path(project_path)
    if not os.path.isfile(p):
        return False, "no pre-commit hook installed"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    except OSError as e:
        return False, f"could not read hook: {e}"
    if _HOOK_MARKER not in existing:
        return False, (
            "pre-commit hook exists but was NOT written by TokenSave Manager — "
            "refusing to delete it. Inspect or remove manually if intended.")
    try:
        os.remove(p)
    except OSError as e:
        return False, f"could not delete hook: {e}"
    return True, "removed"


def _prefer_console_python(python_exe: str) -> str:
    """If `python_exe` is `pythonw.exe`, prefer the adjacent `python.exe`.

    `pythonw.exe` (Windows GUI-mode interpreter) silently discards stdout
    and stderr — fatal for a git hook because the user would see no review
    findings ever. The console interpreter (`python.exe`) lives in the
    same directory in every standard install. Returns the original path
    unchanged if no swap is possible.
    """
    base = os.path.basename(python_exe).lower()
    if base != "pythonw.exe":
        return python_exe
    candidate = os.path.join(os.path.dirname(python_exe), "python.exe")
    if os.path.isfile(candidate):
        return candidate
    return python_exe  # caller will see "pythonw.exe" hook output disappearing


def _render_hook_script(python_exe: str, reviewer_script: str) -> str:
    """Return the body of `.git/hooks/pre-commit` as a string.

    Plain POSIX shell — Git for Windows runs hooks through its bundled
    MSYS `sh`, so this works on Windows too. The script does ONLY:
      1. Recursion-guard env-var check
      2. Set the guard
      3. Exec the reviewer Python script with the user's python_exe
    Everything else (diff reading, backend dispatch, parsing) is in the
    Python script — keeps the shell layer trivial so failure paths are
    obvious.
    """
    # Forward-slash all paths — git's shell on Windows is fine with them
    # and they avoid the backslash-escaping pit.
    py = python_exe.replace("\\", "/")
    rev = reviewer_script.replace("\\", "/")
    return (
        "#!/bin/sh\n"
        f"{_HOOK_MARKER}\n"
        "# Installed by TokenSave Manager. Reviews `git diff --cached`\n"
        "# using your configured AI backend before allowing the commit.\n"
        "# Remove via the manager's right-click menu, or delete this file.\n"
        "\n"
        "# Recursion guard — if the reviewer itself triggers a git commit\n"
        "# in some edge case, don't re-enter ourselves.\n"
        "if [ -n \"$TOKENSAVE_PRECOMMIT_RUNNING\" ]; then\n"
        "    exit 0\n"
        "fi\n"
        "export TOKENSAVE_PRECOMMIT_RUNNING=1\n"
        "\n"
        f"PY='{py}'\n"
        f"REV='{rev}'\n"
        "\n"
        "# Fail-open if either path is missing — never block commits because\n"
        "# the manager was moved.\n"
        "if [ ! -x \"$PY\" ] && [ ! -f \"$PY\" ]; then\n"
        "    echo \"[tokensave precommit] python not found at $PY — skipping review\" >&2\n"
        "    exit 0\n"
        "fi\n"
        "if [ ! -f \"$REV\" ]; then\n"
        "    echo \"[tokensave precommit] reviewer script not found at $REV — skipping review\" >&2\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "exec \"$PY\" \"$REV\"\n"
    )


# ── Review runner — called from src/precommit_review.py ───────────────────

def severity_blocks_commit(severity_summary: dict, threshold: str) -> bool:
    """Map a parsed-findings summary + user threshold to a commit-block decision.

    severity_summary keys: {"high": int, "medium": int, "low": int, "info": int}
    threshold values:
        "none"   → warn-only; never blocks. (default)
        "high"   → block if any high-severity finding.
        "medium" → block if any high- OR medium-severity finding.
    Unknown thresholds resolve to "none" (warn-only) — fail-open.
    """
    t = (threshold or "none").lower()
    if t == "high":
        return severity_summary.get("high", 0) > 0
    if t == "medium":
        return (severity_summary.get("high", 0)
                + severity_summary.get("medium", 0)) > 0
    return False


# Regex matches headers from the Stage 1 system prompt:
#   `## ⚠ High severity` / `## ⚡ Medium severity` / `## 💡 Low ...` / `## ℹ Observations`
# Case-insensitive on the textual part so loose model output still parses.
_SEVERITY_HEADER_RE = re.compile(
    r"^##\s+(?:⚠|⚡|💡|ℹ)?\s*(high|medium|low|observation)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_severity_summary(review_text: str) -> dict:
    """Count bullets per severity section in the review markdown.

    Walks line by line. When we hit a severity header, switches the
    "current bucket" and counts subsequent `- ` bullet lines until the
    next header or end-of-text. Returns {"high": n, "medium": n, "low": n,
    "info": n}. Unparseable input → all-zeros (defensive — never raises).
    """
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    if not review_text:
        return counts
    current: "str | None" = None
    for line in review_text.splitlines():
        m = _SEVERITY_HEADER_RE.match(line)
        if m:
            tag = m.group(1).lower()
            if tag.startswith("high"):
                current = "high"
            elif tag.startswith("med"):
                current = "medium"
            elif tag.startswith("low"):
                current = "low"
            elif tag.startswith("obs"):
                current = "info"
            else:
                current = None
            continue
        if current is not None and line.lstrip().startswith("- "):
            counts[current] += 1
    return counts


def run_review(cfg: "ManagerConfig", diff: str) -> tuple[bool, str, dict]:
    """Run the AI review on `diff` using the configured backend.

    Returns (ok, output_text, severity_summary).
      ok=True  → review completed; output_text is the LLM's markdown report.
      ok=False → backend unavailable or call failed; output_text is a
                 short stderr-bound message explaining why. severity_summary
                 is all-zeros. Caller should fail-open (exit 0) on ok=False.

    Backend selection comes from `cfg.raw["precommit_review_backend"]`:
      "auto" (default) | "claude_cli" | "llm"
    """
    backend = (cfg.raw.get("precommit_review_backend") or "auto").lower()
    max_chars = int(
        (cfg.raw.get("commit_message_llm") or {}).get("max_diff_chars", 24000))
    truncated = len(diff) > max_chars
    diff_for_review = diff[:max_chars]
    user_prompt = (
        "Review the following git diff (staged for commit).\n\n"
        f"```diff\n{diff_for_review}\n```"
        + ("\n\n[diff truncated for length]" if truncated else "")
    )

    text = _dispatch_review_backend(cfg, backend, user_prompt)
    if text is None:
        return False, "AI review backend unavailable or failed", {
            "high": 0, "medium": 0, "low": 0, "info": 0}

    summary = parse_severity_summary(text)
    return True, text, summary


def _dispatch_review_backend(
    cfg: "ManagerConfig", backend: str, user_prompt: str
) -> "str | None":
    """Pick a backend and run it. Returns review text or None on failure."""
    has_cc = bool(cfg.claude_cli_exe)
    llm_cfg = cfg.raw.get("commit_message_llm") or {}
    has_llm = bool(llm_cfg.get("enabled"))

    if backend == "claude_cli":
        if not has_cc:
            print("[tokensave precommit] precommit_review_backend=claude_cli "
                  "but claude_cli_exe is not configured — skipping review",
                  file=sys.stderr)
            return None
        return _call_claude_cli(cfg.claude_cli_exe, user_prompt)

    if backend == "llm":
        if not has_llm:
            print("[tokensave precommit] precommit_review_backend=llm "
                  "but commit_message_llm.enabled is false — skipping review",
                  file=sys.stderr)
            return None
        return _call_via_llm(llm_cfg, user_prompt)

    # "auto" — prefer CC subscription, fall back to per-token LLM provider
    if has_cc:
        result = _call_claude_cli(cfg.claude_cli_exe, user_prompt)
        if result is not None:
            return result
        # CC failed; fall through to LLM if available
    if has_llm:
        return _call_via_llm(llm_cfg, user_prompt)
    print("[tokensave precommit] no backend available "
          "(claude_cli_exe not set, commit_message_llm.enabled false) — "
          "skipping review", file=sys.stderr)
    return None


def _call_claude_cli(claude_exe: str, user_prompt: str) -> "str | None":
    """Invoke `claude --print` with the review system prompt + diff.

    Thin wrapper around helpers.claude_cli.call_claude_cli_print — kept
    here so the pre-commit entry point (precommit_review.py) doesn't need
    to import from the full manager helper tree. Logs failures to stderr
    with the [tokensave precommit] prefix expected by the hook script.
    """
    from helpers.claude_cli import call_claude_cli_print
    result = call_claude_cli_print(
        claude_exe, user_prompt,
        system_prompt=_REVIEW_SYSTEM_PROMPT,
        timeout=30,
    )
    if result is None:
        print("[tokensave precommit] claude --print timed out or failed — "
              "skipping review", file=sys.stderr)
    return result


def _call_via_llm(llm_cfg: dict, user_prompt: str) -> "str | None":
    """Invoke the configured LLM via the manager's `_call_llm` helper.

    Same code path the AI Code Review dialog uses; respects whichever
    provider the user has set up (Anthropic / Ollama / LM Studio / etc.).
    """
    try:
        from helpers.llm import _call_llm
    except ImportError as e:
        print(f"[tokensave precommit] could not import _call_llm: {e}",
              file=sys.stderr)
        return None
    try:
        result = _call_llm(
            llm_cfg, _REVIEW_SYSTEM_PROMPT, user_prompt,
            max_tokens=2000, timeout=30)
    except Exception as e:
        print(f"[tokensave precommit] _call_llm raised: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None
    return result if result else None


# ── Helper: locate the reviewer script for install ────────────────────────

def reviewer_script_path() -> str:
    """Absolute path to `src/precommit_review.py` for the current install.

    Used by `install_pre_commit_hook`. Resolved relative to THIS module's
    location, so `<src>/helpers/precommit_hook.py` → `<src>/precommit_review.py`.
    Re-installing after moving the manager picks up the new path.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "precommit_review.py")
