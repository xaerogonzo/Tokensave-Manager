"""PR description auto-drafting helper.

Pure-function module. Computes the local working-tree diff, feeds it to the
configured LLM, and returns a structured Markdown pull-request description.
No Tkinter imports.
"""

from __future__ import annotations

from helpers.llm import _call_llm
from helpers.commit_messages import _pending_diff


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
    diff_data = _pending_diff(project_path)
    if not diff_data or len(diff_data.strip()) < 50:
        return "Empty diff or changes too trivial to generate a structured PR description."

    max_chars = cfg.raw.get("commit_message_llm", {}).get("max_diff_chars", 24000)
    if len(diff_data) > max_chars:
        diff_data = diff_data[:max_chars] + "\n[Diff truncated for context limits]"

    user_prompt = (
        f"Please draft a comprehensive PR description based on this git diff:\n\n{diff_data}"
    )

    return _call_llm(
        cfg=cfg,
        system_prompt=_PR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=3000,
        timeout=120,
    )
