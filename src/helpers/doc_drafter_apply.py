"""compute_apply (pure) + io_apply (write) per doc type.

Split out of helpers/doc_drafter.py (Roadmap-8 god-file split).
Import via the ``helpers.doc_drafter`` facade — it re-exports
every name, so call sites and tests are unchanged.
"""

from __future__ import annotations

import os
from helpers.doc_drafter_prompts import (
    _extract_subsection_headers,
)
from helpers.doc_drafter_filters import (
    _LITERAL_PLACEHOLDER_RE,
    _filter_bullets,
    _mirror_contract_check,
    architecture_parse_draft,
    generic_parse_draft,
    parse_grouped_bullets,
    roadmap_parse_draft,
    split_readme_subsection,
)




def changelog_compute_apply(full_text, pairs_list):
    """PURE: apply grouped bullet pairs to full file text. Returns (new_text, ok, msg)."""
    from helpers.changelog_patch import _compute_insert_unreleased_bullets
    if not pairs_list:
        return full_text, False, "No bullet pairs to apply."
    simulated = full_text
    for section, bullets in pairs_list:
        simulated, ok, msg = _compute_insert_unreleased_bullets(
            simulated, section, bullets)
        if not ok:
            return full_text, False, f"{section}: {msg}"
    return simulated, True, "ok"



def readme_compute_apply(full_text, header_line, bullets):
    """PURE: splice readme sub-section into full file text. Returns (new_text, ok, msg)."""
    from helpers.readme_patch import _compute_insert_readme_highlights_subsection
    return _compute_insert_readme_highlights_subsection(
        full_text, header_line, bullets)



def changelog_io_apply(target_path, draft_text):
    """Apply changelog draft. Returns (ok, msg, stats_dict)."""
    from helpers.changelog_patch import read_section_bullets, insert_unreleased_bullets
    pairs = parse_grouped_bullets(draft_text)
    if not pairs:
        return False, (
            "Draft is missing '### Section' headers — CHANGELOG mode requires "
            "bullets grouped under ### Added / ### Fixed / ### Changed / "
            "### Removed."
        ), {}
    applied = []
    total_truncated = total_duplicates = total_noop = 0
    empty_after_filter = 0
    for section, bullets in pairs:
        existing = read_section_bullets(target_path, section)
        filtered, trunc_n, dup_n, noop_n = _filter_bullets(bullets, existing)
        total_truncated += trunc_n
        total_duplicates += dup_n
        total_noop += noop_n
        if not filtered.strip():
            empty_after_filter += 1
            continue
        ok, msg = insert_unreleased_bullets(target_path, section, filtered)
        if not ok:
            stats = {"truncated": total_truncated,
                     "duplicates": total_duplicates, "noop": total_noop}
            return False, f"{section}: {msg} (applied so far: {applied})", stats
        applied.append(section)
    stats = {"truncated": total_truncated,
             "duplicates": total_duplicates, "noop": total_noop}
    if not applied:
        return False, (
            f"All bullets filtered out ({total_truncated} truncated, "
            f"{total_duplicates} duplicates, {total_noop} placeholders).  "
            "Click Regenerate to retry."
        ), stats
    summary = f"appended to {len(applied)} section(s): {', '.join(applied)}"
    if empty_after_filter:
        summary += f" ({empty_after_filter} section(s) emptied by filter)"
    return True, summary, stats



def readme_io_apply(target_path, draft_text):
    """Apply readme sub-section draft. Returns (ok, msg, stats_dict)."""
    from helpers.readme_patch import (
        read_subsection_bullets, insert_readme_highlights_subsection,
        read_highlights,
    )
    parsed = split_readme_subsection(draft_text)
    if parsed is None:
        return False, (
            "Draft is missing a `**Bold header**` line — README mode requires "
            "a sub-section header as the first non-blank line."
        ), {}
    header_line, bullets = parsed
    if _LITERAL_PLACEHOLDER_RE.search(header_line):
        candidates = _extract_subsection_headers(read_highlights(target_path))
        candidates_msg = "\n  ".join("• " + h for h in candidates[:5])
        return False, (
            f"Header contains a literal template placeholder: "
            f"{header_line!r}.  Edit the draft to use a real header, "
            f"or click Regenerate.  Existing candidates to REUSE if "
            f"your bullets belong:\n  "
            f"{candidates_msg if candidates else '(none — your sub-section is brand new; use a concrete number like Roadmap-6 or Roadmap-7)'}"
        ), {}
    existing = read_subsection_bullets(target_path, header_line)
    filtered, trunc_n, dup_n, noop_n = _filter_bullets(
        bullets, existing, dedup_against_existing=False)
    stats = {"truncated": trunc_n, "duplicates": dup_n, "noop": noop_n}
    if not filtered.strip():
        return False, (
            f"All bullets filtered out ({trunc_n} truncated, {dup_n} "
            f"self-duplicates, {noop_n} placeholders).  README's REPLACE "
            "patcher would DELETE the existing sub-section if applied with "
            "no bullets.  Click Regenerate."
        ), stats
    draft_bullets = [ln.lstrip() for ln in filtered.splitlines()
                     if ln.lstrip().startswith(("- ", "* "))]
    ok, kept, missing, examples = _mirror_contract_check(
        draft_bullets, existing)
    if not ok:
        bullet_list = "\n  ".join("• " + ex for ex in examples)
        extra = (f" (showing {len(examples)} of {missing})"
                 if missing > len(examples) else "")
        return False, (
            f"Mirror-contract safety abort: draft preserves only "
            f"{kept}/{kept + missing} existing bullets "
            f"({int(100 * kept / max(1, kept + missing))}%).  "
            f"README's REPLACE patcher would DELETE {missing} "
            f"preserved bullet(s){extra}:\n  {bullet_list}\n\n"
            f"Click Regenerate to retry, or manually edit the draft "
            f"to add the missing bullets back before Apply."
        ), stats
    ok2, msg2 = insert_readme_highlights_subsection(
        target_path, header_line,
        filtered + "\n" if filtered else "\n")
    return ok2, msg2, stats



# ── Phase 2 compute_apply (PURE) functions ────────────────────────────────────

def _apply_sections(full_text, sections, compute_fn, known_titles, *,
                     allow_new=False, title_for=None):
    """Shared multi-section apply helper.

    Iterates through ``sections``, validating each title against
    ``known_titles`` (unless ``allow_new`` is True). For each known title:
    runs ``compute_fn(simulated, *section_args)`` and accumulates the
    cumulative simulated state. Returns ``(simulated, ok, msg, stats)``.

    Args:
      full_text:    Starting document text.
      sections:     List of section tuples. Each item is splatted into compute_fn
                    AFTER ``simulated``: e.g. (title, body) for architecture,
                    (n, theme, body) for roadmap.
      compute_fn:   `_compute_insert_*` function. Signature: (text, *args) -> (new_text, ok, msg).
      known_titles: Set of lowercased titles that are valid update targets.
                    Unknown titles get rejected (refused, not auto-appended).
      allow_new:    If True, unknown titles are accepted (auto-append). Default
                    False — refuses hallucinations.
      title_for:    Callable section_args -> "human title" for skip reporting.
                    Default: stringify first arg.

    Stats dict contains ``applied`` (list of titles) and ``skipped``
    (list of (title, reason) pairs) for UI surfacing.
    """
    if title_for is None:
        title_for = lambda args: str(args[0])  # noqa: E731

    if not sections:
        return full_text, False, "Draft produced no `## Section` block.", {
            "applied": [], "skipped": []
        }

    simulated = full_text
    applied, skipped = [], []
    for section_args in sections:
        title = title_for(section_args)
        if not allow_new and title.lower() not in known_titles:
            skipped.append((
                title,
                "title not in existing document — refused to auto-append "
                "(likely hallucinated)"
            ))
            continue
        next_state, ok, msg = compute_fn(simulated, *section_args)
        if ok:
            simulated = next_state
            applied.append(title)
        else:
            skipped.append((title, msg))

    stats = {"applied": applied, "skipped": skipped}

    if not applied:
        return full_text, False, (
            "All sections rejected: "
            + "; ".join(f"{t}: {m}" for t, m in skipped)
        ), stats
    if skipped:
        msg = (f"Applied {len(applied)}/{len(sections)}; skipped: "
               + ", ".join(f"{t} ({m})" for t, m in skipped))
    else:
        msg = f"ok ({len(applied)} section(s))"
    return simulated, True, msg, stats



def architecture_compute_apply(full_text, sections):
    """PURE: apply N `## Title` sections. Returns (new_text, ok, msg).

    Validates titles against existing `## ` headings; unknown titles are
    REFUSED (not auto-appended). Multi-section partial-apply: known-good
    sections apply against the cumulative simulated state; skipped sections
    are reported in the msg suffix.
    """
    from helpers.architecture_patch import (
        _compute_insert_architecture_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_architecture_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    return simulated, ok, msg



def roadmap_compute_apply(full_text, sections):
    """PURE: apply N `## Roadmap N — Theme` sections. Returns (new_text, ok, msg).

    Unlike architecture/generic, roadmap ALLOWS new sections — adding a new
    `## Roadmap N` for a new phase is a valid user workflow.
    """
    from helpers.roadmap_patch import (
        _compute_insert_roadmap_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    # Title for skip reporting: "Roadmap N" prefix derived from the args
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_roadmap_section,
        known, allow_new=True,
        title_for=lambda args: f"Roadmap {args[0]} — {args[1]}",
    )
    return simulated, ok, msg



def memory_compute_apply(full_text, new_body):
    """PURE: replace memory body. Returns (new_text, ok, msg).

    Memory is single-section by design — the entire draft is the new body.
    No multi-section migration applies here.
    """
    from helpers.memory_patch import _compute_insert_memory_body
    if new_body is None:
        return full_text, False, "Draft body is empty."
    return _compute_insert_memory_body(full_text, new_body)



def generic_compute_apply(full_text, sections):
    """PURE: apply N `## Title` sections. Returns (new_text, ok, msg).

    Same shape as architecture_compute_apply — validates titles, refuses
    hallucinations, partial-apply on per-section failure.
    """
    from helpers.generic_doc_patch import (
        _compute_insert_generic_section, _list_section_titles,
    )
    known = {t.lower() for t in _list_section_titles(full_text or "")}
    simulated, ok, msg, _stats = _apply_sections(
        full_text, sections, _compute_insert_generic_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    return simulated, ok, msg



# ── Phase 2 io_apply functions ────────────────────────────────────────────────

def _read_file_text(path):
    """Read file as UTF-8 with BOM tolerance. Returns "" on missing or read error."""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""



def architecture_io_apply(target_path, draft_text):
    """Apply architecture draft (multi-section). Returns (ok, msg, stats_dict).

    Reads file once → runs compute_apply (validates + iterates) → writes
    once atomically. Returns stats including applied/skipped section lists
    for UI surfacing.
    """
    from helpers.architecture_patch import (
        _compute_insert_architecture_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = architecture_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Section Name` heading — architecture "
            "mode requires the output to start with a level-2 heading."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_architecture_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats



def roadmap_io_apply(target_path, draft_text):
    """Apply roadmap draft (multi-section, allow_new=True). Returns (ok, msg, stats_dict)."""
    from helpers.roadmap_patch import (
        _compute_insert_roadmap_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = roadmap_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Roadmap N` heading — roadmap mode "
            "requires the output to start with '## Roadmap <number>'."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_roadmap_section,
        known, allow_new=True,
        title_for=lambda args: f"Roadmap {args[0]} — {args[1]}",
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats



def memory_io_apply(target_path, draft_text):
    """Apply memory draft. Returns (ok, msg, stats_dict). Single-section."""
    from helpers.memory_patch import insert_memory_body
    body = (draft_text or "").strip()
    if not body:
        return False, "Draft is empty.", {}
    ok, msg = insert_memory_body(target_path, body)
    return ok, msg, {}



def generic_io_apply(target_path, draft_text):
    """Apply generic doc draft (multi-section). Returns (ok, msg, stats_dict)."""
    from helpers.generic_doc_patch import (
        _compute_insert_generic_section, _list_section_titles,
    )
    from helpers.io_utils import _atomic_write

    (sections,) = generic_parse_draft(draft_text)
    if not sections:
        return False, (
            "Draft is missing a `## Section Name` heading — generic doc "
            "mode requires the output to start with a level-2 heading."
        ), {}

    full_text = _read_file_text(target_path)
    known = {t.lower() for t in _list_section_titles(full_text)}
    simulated, ok, msg, stats = _apply_sections(
        full_text, sections, _compute_insert_generic_section,
        known, allow_new=False, title_for=lambda args: args[0],
    )
    if not ok:
        return False, msg, stats

    write_ok, write_msg = _atomic_write(target_path, simulated, msg)
    if not write_ok:
        return False, write_msg, stats
    return True, msg, stats
