"""PR description auto-drafting helper.

Pure-function module. Computes the local working-tree diff, feeds it to the
configured LLM, and returns a structured Markdown pull-request description.
No Tkinter imports.

v4.13: emits a ``## Testing checklist`` section at the end of the body with
the manager-managed marker. The "Automated" subsection is pre-filled from
the local pytest results cache (if any); the "Manual" subsection is
LLM-generated from the commit context. Test Manager's "🔁 Sync PR
Checklist" button updates the Automated subsection in-place after fresh
test runs.
"""

from __future__ import annotations

import subprocess

from helpers.llm import _call_llm
from helpers.commit_messages import _pending_diff, _files_from_diff
from constants import CREATE_NO_WINDOW


_PR_SYSTEM_PROMPT = """\
You are a senior technical writer and developer assistant.
Analyze the git diff and provide a detailed, highly professional, clean markdown \
pull request description.

FORMATTING RULES (follow exactly):
- Write ALL sections in clear PROSE only. Do NOT include code snippets, \
  code blocks (```), raw patches, diffs, or file content anywhere in your output.
- "## Technical Implementation Details" must describe WHAT changed and WHY at \
  a conceptual level — never reproduce code or diffs.
- Keep each section focused on what a human reviewer needs to understand or act on.
- Name ONLY file paths and modules that LITERALLY appear in the git diff below. \
  If a path is not present in the raw diff lines, treat it as non-existent and \
  omit it. State only what the git diff and the commit list show; if a detail is \
  not derivable from them, omit it rather than guessing.

Generate ONLY these exact markdown headers, in this order, and end your response \
immediately after the content of the last one:
## Summary of Changes
## Technical Implementation Details
## Threat Model & Security Implications (State 'None' if trivial)
## Manual Verification & Testing Steps Performed
## Testing checklist

The Testing checklist section MUST be the LAST section. Render it exactly \
in this format (copy the HTML comment marker and the "### Automated" \
subsection verbatim from the user prompt; generate the "### Manual" \
subsection's bullet points based on the changed files):

## Testing checklist
<!-- tokensave-manager:testing-checklist v1 -->
### Automated (verified by `pytest -m "not tk"`)
- [<TICK_OR_BLANK>] Test suite passes locally (<N>/<M> passed as of <TIMESTAMP>)
- [ ] CI test-gate job passes on this PR (check GitHub Actions tab)

### Manual (please verify before merge)
- [ ] <One bullet per UI flow or smoke check implied by the changed files>
- [ ] <... 2-6 bullets total ...>

Critically: keep the marker comment <!-- tokensave-manager:testing-checklist v1 --> \
exactly as written — the manager's "Sync PR Checklist" feature relies on it \
to identify the section it owns.
"""


_PR_SYSTEM_PROMPT_LOCAL = """\
You are a technical writer drafting a pull request description.
Write a professional, concise Markdown PR description based on the git diff.

STRICT RULES — follow exactly:
- Write ALL sections in clear PROSE. Do NOT include code snippets, code blocks \
  (```), raw patches, diffs, or any file content.
- Describe WHAT changed and WHY — not how the code looks.
- Be concise: PR reviewers skim; prefer short paragraphs and bullet points.
- "## Technical Implementation Details" explains decisions and changed \
  behaviour — never pastes code.
- Name ONLY file paths and modules that LITERALLY appear in the git diff below. \
  If a path is not present in the raw diff lines, treat it as non-existent and \
  omit it entirely.
- State only what the git diff and the commit list show. If a detail is not \
  derivable from them, omit it — do not guess or fill gaps.

Generate ONLY these exact section headers, in this order, and end your response \
immediately after the content of the last one:
## Summary of Changes
## Technical Implementation Details
## Threat Model & Security Implications
## Manual Verification & Testing Steps Performed
## Testing checklist

For "## Threat Model & Security Implications": write "None" if the changes \
are trivial or internal only.

For "## Testing checklist": output exactly the following two lines, then a \
"### Manual" subsection with 2-5 smoke-check bullets implied by the diff. \
Do NOT write a "### Automated" subsection — it will be added automatically.

## Testing checklist
<!-- tokensave-manager:testing-checklist v1 -->
### Manual (please verify before merge)
- [ ] <one smoke check per meaningful UI flow or changed behaviour>
- [ ] <2-5 bullets total>
"""


def _branch_diff(repo_path: str, base: str, git_exe: str = "git") -> str:
    """Diff committed branch history using pure object-store refs.

    Uses ``git diff <merge-base> HEAD`` — a two-commit comparison that reads
    only the git object store, not the working directory. Consistent with the
    CLI path (``git diff base...HEAD``). Works correctly in no-console Windows
    subprocesses (pythonw.exe + CREATE_NO_WINDOW) where working-tree reads
    silently return empty output.
    """
    _git = git_exe or "git"
    # encoding="utf-8" + errors="replace" is REQUIRED: without an explicit
    # encoding, text=True decodes with the Windows locale codec (cp1252), which
    # raises UnicodeDecodeError the moment a diff contains a byte it can't map
    # (emoji ✓ 📦, box-drawing, accented names, etc.). That exception used to
    # bubble up to the caller and surface as a bogus "empty diff" message.
    try:
        merge_base = subprocess.check_output(
            [_git, "merge-base", base, "HEAD"],
            cwd=repo_path, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        ).strip()
        return subprocess.check_output(
            [_git, "diff", merge_base, "HEAD"],
            cwd=repo_path, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        # merge-base failed (shallow clone, no common history, detached HEAD)
        # — fall back to the most recent commit diff as best-effort context.
        try:
            return subprocess.check_output(
                [_git, "diff", "HEAD~1", "HEAD"],
                cwd=repo_path, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            return ""


def _branch_commit_log(repo_path: str, base: str, git_exe: str = "git",
                       max_commits: int = 200) -> str:
    """Bulleted, oldest-first list of this branch's commit subjects vs *base*.

    A factual scaffold prepended to the PR prompt so the model enumerates changes
    that actually exist (mitigates the local-model "invent files/sections" failure
    mode). ``--reverse`` yields oldest→newest, so the list reads as the branch's
    narrative AND, if the cap is hit, drops the LATE polish commits rather than the
    foundational ones (new files / core interfaces). Subjects are token-cheap, so
    the cap is generous (200); a longer branch is Claude-CLI territory. Returns ""
    on any git failure / no commits.
    """
    _git = git_exe or "git"
    try:
        out = subprocess.check_output(
            [_git, "log", f"{base}..HEAD", "--reverse", "--pretty=format:- %s"],
            cwd=repo_path, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) > max_commits:
        omitted = len(lines) - max_commits
        lines = lines[:max_commits] + [
            f"- (+{omitted} later commits omitted — use the Claude CLI draft path "
            "for full fidelity)"]
    return "\n".join(lines)


def _safe_grounding(fn, *args, **kwargs) -> str:
    """Call *fn* with *args*/*kwargs*; return ``""`` on any exception.

    Used by :func:`_build_grounding_section` to flatten the nested
    ``try/except`` pattern that inflated the CC of
    :func:`_build_pr_prompt_and_call`.
    """
    try:
        return fn(*args, **kwargs) or ""
    except Exception:
        return ""


def _build_grounding_section(cfg, diff_data: str, project_path: str,
                             on_status) -> str:
    """Build the grounding markdown section (tokensave + codegraph).

    Calls ``on_status("grounding")`` before starting, then assembles the
    tokensave roadmap-evidence block and the codegraph affected-symbol block.
    Returns the formatted section header + body, or ``""`` if grounding is
    disabled, unavailable, or fails.  Fail-open: all sub-failures are caught
    by :func:`_safe_grounding` so the caller always receives a string.
    """
    if not cfg.enable_pr_grounding:
        return ""
    if on_status is not None:
        try:
            on_status("grounding")
        except Exception:
            pass
    try:
        from helpers.doc_grounding import (
            build_grounding_block,
            build_codegraph_block,
            build_combined_grounding,
        )
        changed_files = _files_from_diff(diff_data)
        ts_block = _safe_grounding(
            build_grounding_block, project_path, "roadmap_evidence",
            tokensave_exe=cfg.tokensave_exe)
        if cfg.codegraph_exe:
            _safe_grounding(
                __import__("helpers.codegraph_freshness",
                            fromlist=["ensure_fresh"]).ensure_fresh,
                project_path, cfg.codegraph_exe)
        cg_block = _safe_grounding(
            build_codegraph_block, project_path, "roadmap_evidence",
            changed_files=changed_files,
            codegraph_exe=cfg.codegraph_exe or "")
        combined = build_combined_grounding(ts_block, cg_block)
        return (f"## Affected tests & symbols (auto-attached)\n\n{combined}\n\n"
                if combined.strip() else "")
    except Exception:
        return ""


def _build_pr_prompt_and_call(
    cfg,
    diff_data: str,
    commit_log: str,
    project_path: str,
    *,
    on_token=None,
    on_status=None,
    coverage_gaps_md: str = "",
) -> "str | None":
    """Assemble grounding, build the LLM prompt, call the LLM, post-process.

    Separated from :func:`generate_pr_draft` so the orchestrator stays thin
    and this phase can be tested or stubbed independently.  The grounding block
    assembly is delegated to :func:`_build_grounding_section` to keep this
    function's CC below 10.

    Args:
        cfg:              ManagerConfig (read at call time).
        diff_data:        Pre-truncated git diff text.
        commit_log:       Pre-rendered bulleted commit log (may be "").
        project_path:     Absolute repo root (for grounding).
        on_token:         Streaming token callback; forwarded to ``_call_llm``.
        on_status:        Phase-boundary callback (``"grounding"``, ``"generating"``).
        coverage_gaps_md: Pre-rendered coverage-gaps block spliced into checklist.

    Returns:
        Raw LLM markdown string, or None if no LLM is configured.
    """
    def _status(phase: str) -> None:
        if on_status is not None:
            try:
                on_status(phase)
            except Exception:
                pass

    llm_cfg = cfg.raw.get("commit_message_llm", {})
    _provider = llm_cfg.get("provider", "")
    is_local = (
        _provider == "ollama"
        or (_provider == "openai_compatible"
            and "localhost" in (llm_cfg.get("base_url") or ""))
    )

    # v4.2 / v4.6: tokensave + codegraph grounding, gated per-feature.
    # Grounding also fires on_status("grounding") internally.
    grounding_section = _build_grounding_section(cfg, diff_data, project_path,
                                                 on_status)

    # v4.13: pre-fill the Testing checklist's "Automated" subsection.
    automated_block = _render_automated_for_pr(project_path)
    commit_section = (
        f"--- commits on this branch (oldest first) ---\n\n{commit_log}\n\n"
        if commit_log else ""
    )

    if is_local:
        # Local path: don't double-feed automated_block; Python injects it later.
        user_prompt = (
            f"{grounding_section}"
            f"{commit_section}"
            f"Please draft a PR description based on these commits and this git diff.\n\n"
            f"--- git diff ---\n\n{diff_data}"
        )
    else:
        # Cloud path: instruct the LLM to copy automated_block verbatim.
        user_prompt = (
            f"{grounding_section}"
            f"{commit_section}"
            f"Please draft a comprehensive PR description based on these commits and "
            f"this git diff.\n\n"
            f"Pre-rendered '### Automated' subsection (copy verbatim into the "
            f"'## Testing checklist' section):\n\n"
            f"{automated_block}\n\n"
            f"--- git diff ---\n\n{diff_data}"
        )

    if is_local and not llm_cfg.get("num_ctx"):
        # Phase D: 16384 so a 14B-class local model can take a much larger diff.
        llm_cfg = {**llm_cfg, "num_ctx": 16384}
    max_tokens = 1500 if is_local else 3000
    timeout    = 300  if is_local else 120
    system     = _PR_SYSTEM_PROMPT_LOCAL if is_local else _PR_SYSTEM_PROMPT

    _status("generating")
    result = _call_llm(
        cfg=llm_cfg,
        system_prompt=system,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        on_token=on_token,
    )

    # Local: strip accidental code blocks, then inject automated checklist.
    if is_local and result:
        result = _clean_local_artifacts(result)
        result = _inject_automated_block(result, automated_block)

    # Both paths: splice coverage-gaps block (Automated < Gaps < Manual).
    if result and coverage_gaps_md:
        result = _inject_coverage_gaps(result, coverage_gaps_md)

    return result


def generate_pr_draft(cfg, project_path: str, base: str = "", *,
                      on_token=None, on_status=None,
                      coverage_gaps_md: str = "") -> "str | None":
    """Compute local working-tree changes and ask the LLM to draft a PR description.

    Args:
        cfg: ManagerConfig instance (read at call time — never snapshot).
        project_path: Absolute path to the git repository root.
        base: PR base ref (e.g. "origin/master"); empty → working-tree diff.
        on_token: optional callback(delta_str) for streaming — forwarded to
            `_call_llm`. Runs on the calling thread; UI callers must marshal to
            the Tk thread themselves.
        on_status: optional callback(phase_str) called at phase boundaries
            ("grounding", "generating") so the UI can show progress before any
            token arrives.
        coverage_gaps_md: optional pre-rendered "### Coverage gaps" block
            (built by the controller via :func:`_render_coverage_gaps`) spliced
            into the Testing checklist before "### Manual". Kept as a param so
            this module stays decoupled from `test_gap_report`.

    Returns:
        Markdown string from the LLM, or an error/empty-diff message string.
        Returns None only if _call_llm itself returns None (no LLM configured).
    """
    # ── 1. Gather diff ────────────────────────────────────────────────────────
    if base:
        try:
            diff_data = _branch_diff(project_path, base, git_exe=cfg.git_exe)
        except Exception:
            diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    else:
        diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    if not diff_data or len(diff_data.strip()) < 50:
        return "Empty diff or changes too trivial to generate a structured PR description."

    # ── 2. Cap diff to provider context window ────────────────────────────────
    # Local cap SCALES with num_ctx (~55% for content, ~4 chars/token).
    # Cloud keeps a 24 KB default. An explicit max_diff_chars always wins.
    _p = cfg.raw.get("commit_message_llm", {})
    _is_local = (
        _p.get("provider") == "ollama"
        or (_p.get("provider") == "openai_compatible"
            and "localhost" in (_p.get("base_url") or ""))
    )
    _explicit_cap = _p.get("max_diff_chars")
    if _is_local:
        _num_ctx = int(_p.get("num_ctx") or 16384)
        _scaled = max(8000, int(_num_ctx * 0.55) * 4)
        max_chars = int(_explicit_cap) if _explicit_cap else _scaled
    else:
        max_chars = int(_explicit_cap) if _explicit_cap else 24000
    if len(diff_data) > max_chars:
        diff_data = diff_data[:max_chars] + "\n[Diff truncated for context limits]"

    # ── 3. Commit log (factual scaffold for the LLM) ─────────────────────────
    commit_log = _branch_commit_log(project_path, base, git_exe=cfg.git_exe)

    # ── 4. Grounding + prompt + LLM call + post-processing ───────────────────
    return _build_pr_prompt_and_call(
        cfg, diff_data, commit_log, project_path,
        on_token=on_token,
        on_status=on_status,
        coverage_gaps_md=coverage_gaps_md,
    )


def _render_automated_for_pr(project_path: str) -> str:
    """Render the ``### Automated`` subsection for the PR template prompt.

    Reads the manager's last-run cache (``.tokensave-manager/last_test_run.json``).
    Falls back to a "untested" template when the cache is absent — the
    user still gets the checklist structure; they just need to run tests
    and click "Sync PR Checklist" later to populate the ticks.

    Returned content is literal markdown — the system prompt instructs
    the LLM to copy it verbatim into the final body. Format matches
    :func:`helpers.pr_checklist.format_automated_section` so the
    Sync-PR-Checklist button can later update it in place.
    """
    try:
        from helpers.test_discovery import load_last_run_results
        cache = load_last_run_results(project_path)
    except Exception:
        cache = {}

    if not isinstance(cache, dict):
        cache = {}
    results = cache.get("results") if isinstance(cache.get("results"), dict) else {}
    passed = sum(int(r.get("passed", 0)) for r in (results or {}).values()
                   if isinstance(r, dict))
    total  = sum(int(r.get("total", 0))  for r in (results or {}).values()
                   if isinstance(r, dict))
    ran_at = cache.get("ran_at") or "not yet run"
    tick = "x" if (total > 0 and passed == total) else " "

    return (
        "### Automated (verified by `pytest -m \"not tk\"`)\n"
        f"- [{tick}] Test suite passes locally "
        f"({passed}/{total} passed as of {ran_at})\n"
        "- [ ] CI test-gate job passes on this PR (check GitHub Actions tab)\n"
    )


def _clean_local_artifacts(text: str) -> str:
    """Remove triple-backtick code blocks that local models sometimes include.

    Despite explicit prose-only instructions, small models occasionally paste
    diff snippets inside ``` blocks. This defensive pass strips them without
    touching the rest of the output.

    The `[ \\t]*` on the closing fence handles trailing spaces that small models
    sometimes emit before the newline (e.g. "```  \\n"). Global `.strip()` is
    intentionally omitted — it would destroy intentional structural whitespace
    (leading blank lines, section spacing) in the PR body.
    """
    import re
    return re.sub(r'```[^\n]*\n.*?```[ \t]*\n?', '', text, flags=re.DOTALL)


def _inject_automated_block(text: str, automated_block: str) -> str:
    """Insert the pre-rendered Automated subsection before ### Manual in the checklist.

    The local-model prompt asks for ### Manual but not ### Automated.
    This function splices the automated_block (computed from the test-run cache)
    into the right location so the final output matches the cloud-path format
    expected by the Sync PR Checklist feature.
    """
    marker = "### Manual"
    idx = text.find(marker)
    if idx != -1:
        return text[:idx] + automated_block + "\n" + text[idx:]
    # Fallback: the model omitted the checklist entirely — append full structure
    # including a ### Manual stub so the Sync PR Checklist template contract is met.
    return (
        text
        + "\n\n## Testing checklist\n"
        "<!-- tokensave-manager:testing-checklist v1 -->\n"
        + automated_block
        + "\n### Manual (please verify before merge)\n"
        "- [ ] Manually verify the key changed behaviour\n"
    )


def _render_coverage_gaps(suggestions) -> str:
    """Render a ``### Coverage gaps`` checklist block from SuggestedTest items.

    Only entries with ``requires_automation`` True are included — Tk-dialog and
    unclassified files are deliberately excluded (they're low-ROI and would
    create blocked checklist lines for files we don't intend to auto-test; they
    remain visible in the local test-gap panel). Backticks in paths are escaped
    so an unusual filename can't shatter the surrounding markdown.

    Returns "" when there are no qualifying gaps, so callers can treat the
    result as "splice only if truthy".
    """
    gaps = [s for s in (suggestions or [])
            if getattr(s, "requires_automation", False)]
    if not gaps:
        return ""
    lines = ["### Coverage gaps (changed files with no test file)"]
    for s in gaps:
        safe = (s.rel_path or "").replace("`", "'")   # neutralise markdown
        lines.append(
            f"- [ ] `{safe}` — no test yet ({s.template}) — scaffold candidate"
        )
    return "\n".join(lines) + "\n"


def _inject_coverage_gaps(text: str, block: str) -> str:
    """Splice a coverage-gaps block into the Testing checklist before ### Manual.

    Runs for BOTH local and cloud paths (cloud already has ### Automated from the
    LLM; local gets it via _inject_automated_block first). Inserting before
    "### Manual" yields the ordering Automated < Coverage gaps < Manual. If no
    "### Manual" marker exists, append the block at the end as a best-effort
    fallback.
    """
    if not block:
        return text
    marker = "### Manual"
    idx = text.find(marker)
    if idx != -1:
        return text[:idx] + block + "\n" + text[idx:]
    return text.rstrip() + "\n\n" + block


def _mark_gaps_addressed(body: str, rel_paths) -> str:
    """Flip ``- [ ]`` → ``- [x]`` for coverage-gap lines whose path was just tested.

    Used when the user generates tests inside the Draft PR dialog's embedded panel:
    the static ``### Coverage gaps`` checklist in the body would otherwise still show
    a now-covered file as an open gap. We mark it addressed (``[x]``) rather than
    delete the line — non-destructive and useful reviewer context.

    Surgical and safe:
      * matches ONLY lines shaped ``- [ ] `<path>` …`` (the renderer's format), so a
        ``### Manual`` bullet that happens to mention the same path is left alone;
      * paths are escaped exactly as :func:`_render_coverage_gaps` escapes them
        (backtick → apostrophe) so an unusual filename still matches;
      * idempotent — an already-``[x]`` line is unchanged;
      * no-op for an empty list, an unmatched path, or a body with no gaps block.

    Returns the (possibly unchanged) body.
    """
    if not body or not rel_paths:
        return body
    wanted = {(p or "").replace("`", "'") for p in rel_paths}
    out = []
    for line in body.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("- [ ] `"):
            # Extract the backticked path token: ``- [ ] `<path>` — …``
            rest = stripped[len("- [ ] `"):]
            end = rest.find("`")
            if end != -1 and rest[:end] in wanted:
                line = line.replace("- [ ] ", "- [x] ", 1)
        out.append(line)
    return "\n".join(out)
