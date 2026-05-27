"""PR description auto-drafting helper.

Pure-function module. Computes the local working-tree diff, feeds it to the
configured LLM, and returns a structured Markdown pull-request description.
No Tkinter imports.
"""

from __future__ import annotations

from helpers.llm import _call_llm
from helpers.commit_messages import _pending_diff, _files_from_diff


_PR_SYSTEM_PROMPT = """\
You are a senior technical writer and developer assistant.
Analyze the git diff and provide a detailed, highly professional, clean markdown \
pull request description.
Output should be formatted with these exact markdown headers:
## Summary of Changes
## Technical Implementation Details
## Threat Model & Security Implications (State 'None' if trivial)
## Manual Verification & Testing Steps Performed
"""


def generate_pr_draft(cfg, project_path: str) -> str | None:
    """Compute local working-tree changes and ask the LLM to draft a PR description.

    Args:
        cfg: ManagerConfig instance (read at call time — never snapshot).
        project_path: Absolute path to the git repository root.

    Returns:
        Markdown string from the LLM, or an error/empty-diff message string.
        Returns None only if _call_llm itself returns None (no LLM configured).
    """
    diff_data = _pending_diff(project_path, git_exe=cfg.git_exe)
    if not diff_data or len(diff_data.strip()) < 50:
        return "Empty diff or changes too trivial to generate a structured PR description."

    max_chars = cfg.raw.get("commit_message_llm", {}).get("max_diff_chars", 24000)
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
    user_prompt = (
        f"{grounding_section}"
        f"Please draft a comprehensive PR description based on this git diff:\n\n{diff_data}"
    )

    return _call_llm(
        cfg=cfg,
        system_prompt=_PR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=3000,
        timeout=120,
    )
