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
    try:
        merge_base = subprocess.check_output(
            [_git, "merge-base", base, "HEAD"],
            cwd=repo_path, text=True,
            creationflags=CREATE_NO_WINDOW,
        ).strip()
        return subprocess.check_output(
            [_git, "diff", merge_base, "HEAD"],
            cwd=repo_path, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        # merge-base failed (shallow clone, no common history, detached HEAD)
        # — fall back to the most recent commit diff as best-effort context.
        try:
            return subprocess.check_output(
                [_git, "diff", "HEAD~1", "HEAD"],
                cwd=repo_path, text=True,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            return ""


def generate_pr_draft(cfg, project_path: str, base: str = "") -> str | None:
    """Compute local working-tree changes and ask the LLM to draft a PR description.

    Args:
        cfg: ManagerConfig instance (read at call time — never snapshot).
        project_path: Absolute path to the git repository root.

    Returns:
        Markdown string from the LLM, or an error/empty-diff message string.
        Returns None only if _call_llm itself returns None (no LLM configured).
    """
    if base:
        try:
            diff_data = _branch_diff(project_path, base, git_exe=cfg.git_exe)
        except Exception:
            diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    else:
        diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    if not diff_data or len(diff_data.strip()) < 50:
        return "Empty diff or changes too trivial to generate a structured PR description."

    max_chars = cfg.raw.get("commit_message_llm", {}).get("max_diff_chars", 24000)
    if len(diff_data) > max_chars:
        diff_data = diff_data[:max_chars] + "\n[Diff truncated for context limits]"

    # Additional cap for local providers — must run before user_prompt is assembled.
    # Mirrors the is_local pattern in commit_messages._call_llm_for_commit_message.
    _p = cfg.raw.get("commit_message_llm", {})
    _is_local_pre = (
        _p.get("provider") == "ollama"
        or (_p.get("provider") == "openai_compatible"
            and "localhost" in (_p.get("base_url") or ""))
    )
    if _is_local_pre:
        _local_max = min(max_chars, 8000)
        if len(diff_data) > _local_max:
            diff_data = diff_data[:_local_max] + "\n[Diff truncated for local model context limit]"

    # v4.2: build tokensave+codegraph grounding (roadmap_evidence recipe —
    # codegraph triggers `codegraph affected --stdin` on the changed-files
    # list, surfacing test-impact information for the PR description).
    # v4.6: gated by enable_pr_grounding (per-feature, default ON) rather
    # than the master toggle directly. Lets users opt out of grounding
    # for PR drafting specifically without disabling it everywhere.
    grounding = ""
    if cfg.enable_pr_grounding:
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
        llm_cfg = {**llm_cfg, "num_ctx": 8192}
    _max_tokens = 1500 if _is_local else 3000
    _timeout    = 300  if _is_local else 120
    _system     = _PR_SYSTEM_PROMPT_LOCAL if _is_local else _PR_SYSTEM_PROMPT

    result = _call_llm(
        cfg=llm_cfg,
        system_prompt=_system,
        user_prompt=user_prompt,
        max_tokens=_max_tokens,
        timeout=_timeout,
    )

    # Local providers: strip any accidental code blocks, then inject the
    # automated checklist block programmatically (more reliable than asking
    # the model to copy-paste it verbatim).
    if _is_local and result:
        result = _clean_local_artifacts(result)
        result = _inject_automated_block(result, automated_block)

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
