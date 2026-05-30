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

Output should be formatted with these exact markdown headers:
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

Use these exact section headers in this exact order:
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


def generate_pr_draft(cfg, project_path: str, base: str = "", *,
                      on_token=None, on_status=None,
                      coverage_gaps_md: str = "") -> str | None:
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
    def _status(phase: str) -> None:
        if on_status is not None:
            try:
                on_status(phase)
            except Exception:
                pass
    if base:
        try:
            diff_data = _branch_diff(project_path, base, git_exe=cfg.git_exe)
        except Exception:
            diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    else:
        diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    if not diff_data or len(diff_data.strip()) < 50:
        return "Empty diff or changes too trivial to generate a structured PR description."

    # Unified diff cap (Phase D): the old code hard-capped local providers at
    # 8000 chars, so a 250 KB diff fed the model only ~3% of the changes — the
    # main reason local drafts were far worse than Claude CLI. Now the local cap
    # SCALES with the model's context window (num_ctx): with num_ctx=16384 tokens
    # (~4 chars/token) we reserve ~45% for grounding + system prompt + output and
    # allow the rest (~36 KB) for the diff. Cloud keeps a 24 KB default. An
    # explicit `max_diff_chars` always wins.
    _p = cfg.raw.get("commit_message_llm", {})
    _is_local_pre = (
        _p.get("provider") == "ollama"
        or (_p.get("provider") == "openai_compatible"
            and "localhost" in (_p.get("base_url") or ""))
    )
    _explicit_cap = _p.get("max_diff_chars")
    if _is_local_pre:
        _num_ctx = int(_p.get("num_ctx") or 16384)
        _scaled = max(8000, int(_num_ctx * 0.55) * 4)   # ~55% of ctx → chars
        max_chars = int(_explicit_cap) if _explicit_cap else _scaled
    else:
        max_chars = int(_explicit_cap) if _explicit_cap else 24000
    if len(diff_data) > max_chars:
        diff_data = diff_data[:max_chars] + "\n[Diff truncated for context limits]"

    # v4.2: build tokensave+codegraph grounding (roadmap_evidence recipe —
    # codegraph triggers `codegraph affected --stdin` on the changed-files
    # list, surfacing test-impact information for the PR description).
    # v4.6: gated by enable_pr_grounding (per-feature, default ON) rather
    # than the master toggle directly. Lets users opt out of grounding
    # for PR drafting specifically without disabling it everywhere.
    grounding = ""
    if cfg.enable_pr_grounding:
        _status("grounding")
        try:
            from helpers.doc_grounding import (
                build_grounding_block,
                build_codegraph_block,
                build_combined_grounding,
            )
            changed_files = _files_from_diff(diff_data)
            try:
                ts_block = build_grounding_block(
                    project_path, "roadmap_evidence",
                    tokensave_exe=cfg.tokensave_exe,
                )
            except Exception:
                ts_block = ""
            try:
                # v4.3: ensure fresh before grounding call.
                if cfg.codegraph_exe:
                    try:
                        from helpers.codegraph_freshness import ensure_fresh
                        ensure_fresh(project_path, cfg.codegraph_exe)
                    except Exception:
                        pass
                cg_block = build_codegraph_block(
                    project_path, "roadmap_evidence",
                    changed_files=changed_files,
                    codegraph_exe=cfg.codegraph_exe or "",
                )
            except Exception:
                cg_block = ""
            grounding = build_combined_grounding(ts_block, cg_block)
        except Exception:
            grounding = ""

    grounding_section = (
        f"## Affected tests & symbols (auto-attached)\n\n{grounding}\n\n"
        if grounding else ""
    )

    # v4.13: pre-fill the Testing checklist's "Automated" subsection from
    # the last-run cache.
    automated_block = _render_automated_for_pr(project_path)

    if _is_local_pre:
        # Local path: don't double-feed automated_block; Python injects it later.
        user_prompt = (
            f"{grounding_section}"
            f"Please draft a PR description based on this git diff.\n\n"
            f"--- git diff ---\n\n{diff_data}"
        )
    else:
        # Cloud path: instruct the LLM to copy automated_block verbatim.
        user_prompt = (
            f"{grounding_section}"
            f"Please draft a comprehensive PR description based on this git diff.\n\n"
            f"Pre-rendered '### Automated' subsection (copy verbatim into the "
            f"'## Testing checklist' section):\n\n"
            f"{automated_block}\n\n"
            f"--- git diff ---\n\n{diff_data}"
        )

    llm_cfg = cfg.raw.get("commit_message_llm", {})
    _provider = llm_cfg.get("provider", "")
    _is_local = (
        _provider == "ollama"
        or (_provider == "openai_compatible"
            and "localhost" in (llm_cfg.get("base_url") or ""))
    )
    if _is_local and not llm_cfg.get("num_ctx"):
        # Phase D: 16384 (was 8192) so a 14B-class local model can take a much
        # larger diff (the cap above scales to this). Streaming makes the longer
        # generation tolerable. Override via commit_message_llm.num_ctx.
        llm_cfg = {**llm_cfg, "num_ctx": 16384}
    _max_tokens = 1500 if _is_local else 3000
    _timeout    = 300  if _is_local else 120
    _system     = _PR_SYSTEM_PROMPT_LOCAL if _is_local else _PR_SYSTEM_PROMPT

    _status("generating")
    result = _call_llm(
        cfg=llm_cfg,
        system_prompt=_system,
        user_prompt=user_prompt,
        max_tokens=_max_tokens,
        timeout=_timeout,
        on_token=on_token,
    )

    # Local providers: strip any accidental code blocks, then inject the
    # automated checklist block programmatically (more reliable than asking
    # the model to copy-paste it verbatim).
    if _is_local and result:
        result = _clean_local_artifacts(result)
        result = _inject_automated_block(result, automated_block)

    # Both paths: splice the coverage-gaps block (if provided) into the
    # Testing checklist — ordering Automated < Coverage gaps < Manual.
    if result and coverage_gaps_md:
        result = _inject_coverage_gaps(result, coverage_gaps_md)

    return result


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
